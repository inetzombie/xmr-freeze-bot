"""XRP Ledger watcher: issuer freezes on issued tokens via public rippled/Clio JSON-RPC.

Detected, for each configured issuer account:
- `TrustSet` sent by the issuer with tfSetFreeze (individual freeze) or tfSetDeepFreeze (deep freeze)
  on a holder's trust line -> kind "freeze"
- `AccountSet` sent by the issuer with SetFlag=asfGlobalFreeze -> kind "freeze", holder = issuer,
  amount unknown (so it is recorded but not posted, like any other unknown-amount freeze)
- `Clawback` sent by the issuer -> kind "destroy" (recorded, not posted)

`account_tx` on a stablecoin issuer returns every payment that ripples through it, so the bot asks
for one transaction type at a time (`tx_type`, honoured by Clio servers such as s1/s2.ripple.com).
Servers that ignore the filter still work: the bot notices and falls back to one unfiltered pass,
and all filtering is re-done locally anyway. The cursor is the validated ledger index.
"""
import logging
from decimal import Decimal

from .http import HTTPError, request_json
from .models import FreezeEvent

log = logging.getLogger(__name__)
RIPPLE_EPOCH = 946684800
TF_SET_FREEZE = 0x00100000
TF_SET_DEEP_FREEZE = 0x00400000
ASF_GLOBAL_FREEZE = 7
TX_TYPES = ("TrustSet", "AccountSet", "Clawback")


class XrplRPCError(Exception):
    pass


class XrplClient:
    def __init__(self, urls):
        self.urls = list(urls)

    def _post(self, url, method, params):
        r = request_json(url, {"method": method, "params": [params]})
        res = r.get("result") if isinstance(r, dict) else None
        if not isinstance(res, dict) or res.get("status") == "error" or "error" in res:
            raise XrplRPCError(f"{method}: {(res or r).get('error', 'bad response')}")
        return res

    def call(self, method, params):
        errors = []
        for url in self.urls:
            try:
                return self._post(url, method, params)
            except (HTTPError, XrplRPCError) as e:
                errors.append(f"{url}: {e}")
        raise XrplRPCError(f"{method} failed on all servers: {errors}")

    def validated_ledger(self):
        return int(self.call("ledger", {"ledger_index": "validated"})["ledger_index"])

    def trust_line_balance(self, holder, issuer, currency):
        res = self.call("account_lines", {"account": holder, "peer": issuer, "ledger_index": "validated"})
        for line in res.get("lines", []):
            if line.get("currency") == currency:
                return line["balance"]
        return None

    def issuer_txs(self, account, lo, hi, types=TX_TYPES, limit=400):
        """All txs of `types` affecting `account` in ledgers [lo, hi]. Tries servers in order;
        paging markers are server-specific, so a failover restarts the range on the next server."""
        errors = []
        for url in self.urls:
            try:
                return self._issuer_txs(url, account, lo, hi, types, limit)
            except (HTTPError, XrplRPCError, KeyError) as e:
                errors.append(f"{url}: {e}")
        raise XrplRPCError(f"account_tx failed on all servers: {errors}")

    def _issuer_txs(self, url, account, lo, hi, types, limit):
        out = {}
        for tx_type in types:
            marker = None
            while True:
                p = {"account": account, "ledger_index_min": lo, "ledger_index_max": hi,
                     "limit": limit, "forward": True, "tx_type": tx_type}
                if marker:
                    p["marker"] = marker
                res = self._post(url, "account_tx", p)
                for item in res.get("transactions", []):
                    tx = normalize(item)
                    if tx["TransactionType"] != tx_type:
                        # server ignores tx_type (plain rippled): one unfiltered pass instead
                        return self._unfiltered(url, account, lo, hi, types, limit)
                    out[tx["hash"]] = tx
                marker = res.get("marker")
                if not marker:
                    break
        return sorted(out.values(), key=lambda t: (t["ledger_index"], t.get("_tx_index", 0)))

    def _unfiltered(self, url, account, lo, hi, types, limit):
        out, marker = {}, None
        while True:
            p = {"account": account, "ledger_index_min": lo, "ledger_index_max": hi, "limit": limit, "forward": True}
            if marker:
                p["marker"] = marker
            res = self._post(url, "account_tx", p)
            for item in res.get("transactions", []):
                tx = normalize(item)
                if tx["TransactionType"] in types:
                    out[tx["hash"]] = tx
            marker = res.get("marker")
            if not marker:
                break
        return sorted(out.values(), key=lambda t: (t["ledger_index"], t.get("_tx_index", 0)))


def normalize(item):
    """Flatten an account_tx entry (API v1 `tx` or v2 `tx_json`) into one dict with hash/ledger/meta."""
    tx = dict(item.get("tx_json") or item.get("tx") or {})
    tx.setdefault("hash", item.get("hash"))
    tx.setdefault("ledger_index", item.get("ledger_index"))
    if item.get("date") is not None:
        tx.setdefault("date", item["date"])
    meta = item.get("meta") or {}
    tx["meta"] = meta
    tx["_tx_index"] = meta.get("TransactionIndex", 0) if isinstance(meta, dict) else 0
    tx["ledger_index"] = int(tx["ledger_index"])
    return tx


def freeze_kind(tx, issuer):
    """(event_name, kind) for an issuer freeze/clawback tx, else None."""
    if tx.get("Account") != issuer:
        return None
    meta = tx.get("meta") or {}
    if isinstance(meta, dict) and meta.get("TransactionResult", "tesSUCCESS") != "tesSUCCESS":
        return None
    t, flags = tx.get("TransactionType"), int(tx.get("Flags", 0) or 0)
    if t == "TrustSet":
        if flags & TF_SET_DEEP_FREEZE:
            return "TrustSet:tfSetDeepFreeze", "freeze"
        if flags & TF_SET_FREEZE:
            return "TrustSet:tfSetFreeze", "freeze"
    elif t == "AccountSet" and int(tx.get("SetFlag", -1)) == ASF_GLOBAL_FREEZE:
        return "AccountSet:asfGlobalFreeze", "freeze"
    elif t == "Clawback":
        return "Clawback", "destroy"
    return None


def balance_from_meta(tx, holder, issuer, currency):
    """Holder's trust-line balance after the tx, from the RippleState node in its metadata."""
    for node in (tx.get("meta") or {}).get("AffectedNodes", []):
        n = node.get("ModifiedNode") or node.get("CreatedNode")
        if not n or n.get("LedgerEntryType") != "RippleState":
            continue
        f = n.get("FinalFields") or n.get("NewFields") or {}
        lo, hi = (f.get("LowLimit") or {}), (f.get("HighLimit") or {})
        if {lo.get("issuer"), hi.get("issuer")} == {holder, issuer} and (f.get("Balance") or {}).get("currency", currency) == currency:
            return str(abs(Decimal(f["Balance"]["value"])))
    return None


class XrplWatcher:
    def __init__(self, chain_cfg, client=None):
        self.cfg = chain_cfg
        self.name = chain_cfg["name"]
        self.client = client or XrplClient(chain_cfg["rpc_urls"])
        self.issuers = chain_cfg["issuers"]
        self.max_range = int(chain_cfg.get("max_ledger_range", 200))

    def head(self):
        return self.client.validated_ledger()

    def default_start(self, head):
        return max(1, head - int(self.cfg.get("start_lookback_ledgers", 300)))

    def scan(self, from_ledger, to_ledger):
        lo = from_ledger
        while lo <= to_ledger:
            hi = min(lo + self.max_range - 1, to_ledger)
            events = []
            for iss in self.issuers:
                for tx in self.client.issuer_txs(iss["issuer"], lo, hi):
                    ev = self.event_from_tx(iss, tx)
                    if ev:
                        events.append(ev)
            events.sort(key=lambda e: (e.block, e.log_index))
            yield events, hi
            lo = hi + 1

    def event_from_tx(self, iss, tx):
        issuer = iss["issuer"]
        k = freeze_kind(tx, issuer)
        if not k:
            return None
        event, kind = k
        currencies = {c["currency"]: c for c in iss["currencies"]}
        amount_str, source, post = None, "unknown", kind == "freeze"
        if tx["TransactionType"] == "TrustSet":
            holder, currency = tx["LimitAmount"]["issuer"], tx["LimitAmount"]["currency"]
            amount_str = balance_from_meta(tx, holder, issuer, currency)
            source = "tx-meta-balance" if amount_str is not None else "unknown"
            if amount_str is None:
                try:
                    bal = self.client.trust_line_balance(holder, issuer, currency)
                    if bal is not None:
                        amount_str, source = str(abs(Decimal(bal))), "balance@latest"
                except XrplRPCError as e:
                    log.warning("xrpl balance lookup failed: %s", e)
        elif tx["TransactionType"] == "Clawback":
            amt = tx.get("Amount") or {}
            holder, currency = amt.get("issuer"), amt.get("currency")
            amount_str, source, post = amt.get("value"), "tx", False
        else:  # global freeze: every holder at once, no single amount
            holder, currency = issuer, (iss["currencies"][0]["currency"] if len(iss["currencies"]) == 1 else None)
        cur = currencies.get(currency)
        symbol = cur["symbol"] if cur else (iss["currencies"][0]["symbol"] if currency is None else currency)
        decimals = int((cur or iss["currencies"][0]).get("decimals", 6))
        ev = FreezeEvent(
            chain=self.name, symbol=symbol, decimals=decimals, event=event, kind=kind,
            post=post and (cur is not None or currency is None), holder=holder,
            amount_raw=None if amount_str is None else int(Decimal(amount_str).scaleb(decimals)),
            amount_source=source, block=tx["ledger_index"], tx=tx["hash"], log_index=tx.get("_tx_index", 0),
            explorer_url=self.cfg["explorer_tx"].format(tx=tx["hash"]),
            timestamp=(tx["date"] + RIPPLE_EPOCH) if tx.get("date") is not None else None,
        )
        if kind == "freeze" and not ev.post:
            ev.skip_reason = "not-allowlisted"
        return ev
