"""Stellar watcher: issuer freezes on classic assets via Horizon.

On Stellar an issuer with AUTH_REVOCABLE can freeze a holder's trustline by clearing its
`authorized` flag (`set_trust_line_flags`, or the older `allow_trust` with authorize=false).
Only the issuer can do this, and every such operation shows up in the issuer's own operations
feed, so the bot pages `/accounts/{issuer}/operations` per configured issuer.

The chain cursor is a Horizon operation paging token (ledger << 32 | tx << 12 | op), so it is
an ordinary integer like EVM block numbers and the generic run_chain loop works unchanged.
"""
import calendar
import logging
import urllib.parse
import time
from decimal import Decimal

from .http import HTTPError, request_json
from .models import FreezeEvent

log = logging.getLogger(__name__)
AUTHORIZED = 1                    # TrustLineFlags.AUTHORIZED_FLAG
MAINTAIN_LIABILITIES = 2          # AUTHORIZED_TO_MAINTAIN_LIABILITIES_FLAG
STELLAR_DECIMALS = 7
FREEZE_OPS = ("set_trust_line_flags", "allow_trust")


def to_raw(amount_str, decimals=STELLAR_DECIMALS):
    return int(Decimal(amount_str).scaleb(decimals))


def ledger_cursor(ledger):
    """Smallest paging token in a ledger."""
    return int(ledger) << 32


class HorizonClient:
    def __init__(self, urls):
        self.urls = [u.rstrip("/") for u in urls]

    def get(self, path):
        errors = []
        for base in self.urls:
            try:
                return request_json(base + path)
            except HTTPError as e:
                errors.append(str(e))
        raise HTTPError(f"horizon {path.split('?')[0]} failed: {errors}")

    def latest_ledger(self):
        return int(self.get("/")["history_latest_ledger"])

    def operations(self, account, cursor, limit=200):
        q = urllib.parse.urlencode({"cursor": cursor, "order": "asc", "limit": limit})
        return self.get(f"/accounts/{account}/operations?{q}")["_embedded"]["records"]

    def balance(self, account, code, issuer):
        acct = self.get(f"/accounts/{account}")
        for b in acct.get("balances", []):
            if b.get("asset_code") == code and b.get("asset_issuer") == issuer:
                return b["balance"]
        return None


def is_freeze(op):
    """True when the op removes the trustline's full authorization."""
    if op.get("type") == "set_trust_line_flags":
        return AUTHORIZED in (op.get("clear_flags") or []) or "authorized" in (op.get("clear_flags_s") or [])
    if op.get("type") == "allow_trust":
        return op.get("authorize") is False
    return False


def _ts(created_at):
    try:
        return calendar.timegm(time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


class StellarWatcher:
    def __init__(self, chain_cfg, client=None):
        self.cfg = chain_cfg
        self.name = chain_cfg["name"]
        self.client = client or HorizonClient(chain_cfg.get("horizon_urls", ["https://horizon.stellar.org"]))
        # (issuer, asset_code) -> asset config
        self.assets = {(i["issuer"], a["code"]): a for i in chain_cfg["issuers"] for a in i["assets"]}
        self.issuers = [i["issuer"] for i in chain_cfg["issuers"]]
        self.page_limit = int(chain_cfg.get("page_limit", 200))
        self.max_pages = int(chain_cfg.get("max_pages_per_poll", 50))

    def head(self):
        # last paging token of the latest fully ingested ledger
        return ledger_cursor(self.client.latest_ledger() + 1) - 1

    def default_start(self, head):
        return max(0, ledger_cursor((head >> 32) - int(self.cfg.get("start_lookback_ledgers", 17280))))

    def scan(self, from_cursor, to_cursor):
        events = []
        for issuer in self.issuers:
            cursor, pages = from_cursor - 1, 0  # Horizon cursors are exclusive
            while True:
                recs = self.client.operations(issuer, cursor, self.page_limit)
                pages += 1
                done = False
                for op in recs:
                    if int(op["paging_token"]) > to_cursor:
                        done = True
                        break
                    ev = self.event_from_op(issuer, op)
                    if ev:
                        events.append(ev)
                    cursor = op["paging_token"]
                if done or len(recs) < self.page_limit:
                    break
                if pages >= self.max_pages:
                    # very busy issuer: stop here and let the cursor resume from the last op seen
                    to_cursor = int(cursor)
                    break
        events.sort(key=lambda e: (e.block, e.log_index))
        yield events, to_cursor

    def event_from_op(self, issuer, op):
        if op.get("transaction_successful") is False or op.get("source_account") != issuer:
            return None
        typ = op.get("type")
        if typ in FREEZE_OPS and is_freeze(op):
            holder, kind, post, amount_str, source = op.get("trustor"), "freeze", True, None, "unknown"
        elif typ == "clawback":
            holder, kind, post, amount_str, source = op.get("from"), "destroy", False, op.get("amount"), "op"
        else:
            return None
        code = op.get("asset_code")
        asset = self.assets.get((issuer, code))
        if kind == "freeze":
            try:
                # Frozen trustlines can't send, so the current balance is the frozen amount
                # unless it was clawed back later.
                amount_str = self.client.balance(holder, code, issuer)
                source = "balance@latest" if amount_str is not None else "unknown"
            except HTTPError as e:
                log.warning("stellar balance lookup failed: %s", e)
        tid = int(op["paging_token"])
        ev = FreezeEvent(
            chain=self.name, symbol=asset["symbol"] if asset else code, decimals=STELLAR_DECIMALS,
            event=typ, kind=kind, post=post and asset is not None, holder=holder,
            amount_raw=None if amount_str is None else to_raw(amount_str), amount_source=source,
            block=tid >> 32, tx=op["transaction_hash"], log_index=tid & 0xFFF,
            explorer_url=self.cfg["explorer_tx"].format(tx=op["transaction_hash"]),
            timestamp=_ts(op.get("created_at")),
        )
        if asset is None:
            ev.skip_reason = "not-allowlisted"
        return ev
