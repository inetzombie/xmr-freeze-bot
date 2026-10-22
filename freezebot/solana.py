"""Solana watcher: native SPL Token / Token-2022 `freezeAccount`, including inner (CPI) instructions.

Scanning every Solana block is too heavy for free RPC tiers, so the bot watches the freeze
authorities of the configured mints (read from each mint account at startup, so authority
rotations are picked up on restart) plus any `extra_watch_addresses`. Every freeze found in those
transactions is recorded; only allow-listed mints, or freezes worth more than `usd_threshold`,
are posted.
"""
import logging
import os
import time
import urllib.parse
from decimal import Decimal

from .http import HTTPError, request_json
from .models import FreezeEvent

log = logging.getLogger(__name__)
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
}


class SolanaRPCError(Exception):
    pass


def read_helius_key(env_name="HELIUS_API_KEY", file_env="HELIUS_API_KEY_FILE"):
    """Key from $HELIUS_API_KEY, or from the file named by $HELIUS_API_KEY_FILE
    (either a bare key or KEY=VALUE lines). Never stored in config or the repo."""
    key = os.environ.get(env_name, "").strip()
    path = os.environ.get(file_env, "").strip()
    if not key and path and os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if not v:
                key = k.strip()
            elif k.strip() == env_name:
                key = v.strip().strip('"').strip("'")
            if key:
                break
    return key or None


class SolanaClient:
    def __init__(self, urls, delay=0.0):
        self.urls = list(urls)
        self.delay = delay

    def call(self, method, params):
        errors = []
        for url in self.urls:
            if self.delay:
                time.sleep(self.delay)
            try:
                r = request_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except HTTPError as e:
                errors.append(str(e).split("?")[0])  # don't leak api-key query strings in logs
                continue
            if "error" in r:
                errors.append(str(r["error"]))
                continue
            return r["result"]
        raise SolanaRPCError(f"{method} failed: {errors}")

    def slot(self):
        return self.call("getSlot", [{"commitment": "finalized"}])

    def mint_info(self, mint):
        v = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
        return v["data"]["parsed"]["info"]

    def signatures(self, address, before=None, limit=1000):
        opts = {"limit": limit, "commitment": "finalized"}
        if before:
            opts["before"] = before
        return self.call("getSignaturesForAddress", [address, opts])

    def transaction(self, sig):
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "finalized",
                                                  "maxSupportedTransactionVersion": 0}])

    def token_account_balance(self, account):
        return int(self.call("getTokenAccountBalance", [account])["value"]["amount"])


def extract_freezes(tx):
    """Return [(position, info)] for every freezeAccount in a jsonParsed tx, top-level and inner."""
    out = []
    msg = tx["transaction"]["message"]
    for i, ix in enumerate(msg["instructions"]):
        if _is_freeze(ix):
            out.append((i * 1000, ix["parsed"]["info"]))
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for j, ix in enumerate(group["instructions"]):
            if _is_freeze(ix):
                out.append((group["index"] * 1000 + j + 1, ix["parsed"]["info"]))
    return out


def _is_freeze(ix):
    p = ix.get("parsed")
    return (ix.get("programId") in TOKEN_PROGRAMS and isinstance(p, dict)
            and p.get("type") == "freezeAccount")


def balance_from_meta(tx, account):
    """(amount_raw, decimals, owner) for a token account from the tx's own token balances."""
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    meta = tx.get("meta") or {}
    for bal in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or []):
        if bal["accountIndex"] < len(keys) and keys[bal["accountIndex"]] == account:
            ui = bal["uiTokenAmount"]
            return int(ui["amount"]), ui["decimals"], bal.get("owner")
    return None, None, None


class JupiterPrices:
    """Free, keyless USD prices and symbols for the USD threshold on non-allow-listed mints."""

    def __init__(self, base="https://lite-api.jup.ag"):
        self.base = base

    def usd_price(self, mint):
        try:
            r = request_json(f"{self.base}/price/v3?ids={mint}", retries=2)
            return Decimal(str(r[mint]["usdPrice"])) if r.get(mint) else None
        except (HTTPError, KeyError, ValueError):
            return None

    def symbol(self, mint):
        try:
            r = request_json(f"{self.base}/tokens/v2/search?query={urllib.parse.quote(mint)}", retries=2)
            for t in r or []:
                if t.get("id") == mint and t.get("symbol"):
                    return t["symbol"]
        except (HTTPError, KeyError, ValueError, TypeError):
            pass
        return None


class SolanaWatcher:
    def __init__(self, chain_cfg, client=None, prices=None):
        self.cfg = chain_cfg
        self.name = chain_cfg["name"]
        urls = []
        env_url = os.environ.get(chain_cfg.get("rpc_env", "SOLANA_RPC_URL"), "")
        if env_url:
            urls.append(env_url)
        key = read_helius_key()
        if key:
            urls.append(f"https://mainnet.helius-rpc.com/?api-key={key}")
        urls += chain_cfg.get("rpc_urls", [])
        self.client = client or SolanaClient(urls, float(chain_cfg.get("request_delay", 0.12)))
        self.prices = prices or JupiterPrices()
        self.allow = {m["mint"]: m for m in chain_cfg["allowlist"]}
        self.usd_threshold = Decimal(str(chain_cfg.get("usd_threshold", 1000)))
        self._watch = None

    def watch_addresses(self):
        if self._watch is None:
            addrs = set(self.cfg.get("extra_watch_addresses", []))
            for mint in self.allow:
                try:
                    fa = self.client.mint_info(mint).get("freezeAuthority")
                    if fa:
                        addrs.add(fa)
                except SolanaRPCError as e:
                    log.warning("could not read freeze authority of %s: %s", mint, e)
            self._watch = sorted(addrs)
            log.info("solana: watching %d freeze authorities", len(self._watch))
        return self._watch

    def head(self):
        return self.client.slot()

    def default_start(self, head):
        return max(0, head - int(self.cfg.get("start_lookback_slots", 216000)))

    def signatures_in_range(self, address, from_slot, to_slot):
        out, before = [], None
        while True:
            page = self.client.signatures(address, before)
            if not page:
                return out
            for s in page:
                if s["slot"] < from_slot:
                    return out
                if s["slot"] <= to_slot and s.get("err") is None:
                    out.append(s)
            before = page[-1]["signature"]
            if len(page) < 1000:
                return out

    def scan(self, from_slot, to_slot):
        sigs = {}
        for addr in self.watch_addresses():
            for s in self.signatures_in_range(addr, from_slot, to_slot):
                sigs[s["signature"]] = s["slot"]
        events = []
        for sig, slot in sorted(sigs.items(), key=lambda kv: kv[1]):
            tx = self.client.transaction(sig)
            if tx:
                events.extend(self.events_from_tx(sig, tx))
        yield events, to_slot

    def events_from_tx(self, sig, tx):
        events = []
        for pos, info in extract_freezes(tx):
            account, mint = info["account"], info["mint"]
            amount, decimals, owner = balance_from_meta(tx, account)
            source = "tx-token-balance"
            if amount is None:
                try:
                    amount, source = self.client.token_account_balance(account), "balance@latest"
                except SolanaRPCError:
                    amount, source = None, "unknown"
            allowed = self.allow.get(mint)
            if decimals is None:
                decimals = allowed["decimals"] if allowed else 0
            ev = FreezeEvent(
                chain=self.name, symbol=allowed["symbol"] if allowed else None, decimals=decimals,
                event="freezeAccount", kind="freeze", post=True, holder=account, owner=owner,
                amount_raw=amount, amount_source=source, block=tx["slot"], tx=sig, log_index=pos,
                explorer_url=self.cfg["explorer_tx"].format(tx=sig), timestamp=tx.get("blockTime"),
            )
            if not allowed:
                self._apply_threshold(ev, mint)
            events.append(ev)
        return events

    def _apply_threshold(self, ev, mint):
        ev.symbol = self.prices.symbol(mint) or f"{mint[:4]}…{mint[-4:]}"
        price = self.prices.usd_price(mint) if ev.amount else None
        if price is None or ev.amount * price < self.usd_threshold:
            ev.post = False
            ev.skip_reason = "not-allowlisted" if price is not None or not ev.amount else "not-allowlisted-no-price"
