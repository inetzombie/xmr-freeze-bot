"""Tron watcher using TronGrid's public event API (API key optional)."""
import hashlib
import logging
import os
import urllib.parse

from .http import HTTPError, request_json
from .models import FreezeEvent

log = logging.getLogger(__name__)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def hex_to_base58(addr_hex):
    """0x-prefixed 20-byte (or 41-prefixed 21-byte) hex -> Tron base58check."""
    h = addr_hex.lower().replace("0x", "")
    if len(h) == 40:
        h = "41" + h
    raw = bytes.fromhex(h)
    chk = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    n = int.from_bytes(raw + chk, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return out


class TronClient:
    def __init__(self, api_url, api_key=None):
        self.api = api_url.rstrip("/")
        self.headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}

    def get(self, path, params=None):
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return request_json(url, headers=self.headers)

    def post(self, path, body):
        return request_json(f"{self.api}{path}", body, headers=self.headers)

    def now_block(self):
        b = self.post("/wallet/getnowblock", {})
        h = b["block_header"]["raw_data"]
        return h["number"], h["timestamp"]

    def block_timestamp(self, num):
        b = self.post("/wallet/getblockbynum", {"num": int(num)})
        return b["block_header"]["raw_data"]["timestamp"]

    def events(self, contract, event_name, min_ts, max_ts):
        params = {"event_name": event_name, "only_confirmed": "true", "order_by": "block_timestamp,asc",
                  "min_block_timestamp": min_ts, "max_block_timestamp": max_ts, "limit": 200}
        out = []
        url_path = f"/v1/contracts/{contract}/events"
        while True:
            r = self.get(url_path, params)
            if not r.get("success", True):
                raise HTTPError(f"TronGrid error: {r}")
            out.extend(r.get("data", []))
            fp = r.get("meta", {}).get("fingerprint")
            if not fp or not r.get("data"):
                return out
            params["fingerprint"] = fp

    def balance_of(self, contract, holder_hex):
        param = holder_hex.lower().replace("0x", "")[-40:].rjust(64, "0")
        r = self.post("/wallet/triggerconstantcontract", {
            "owner_address": contract, "contract_address": contract,
            "function_selector": "balanceOf(address)", "parameter": param, "visible": True})
        res = (r.get("constant_result") or ["0"])[0]
        return int(res or "0", 16)


class TronWatcher:
    """Cursor for Tron is a block timestamp in ms (TronGrid filters by time)."""

    def __init__(self, chain_cfg, client=None):
        self.cfg = chain_cfg
        self.name = chain_cfg["name"]
        key = os.environ.get(chain_cfg.get("api_key_env", ""), "") or None
        self.client = client or TronClient(chain_cfg["api_url"], key)

    def head(self):
        _, ts = self.client.now_block()
        return ts - 60_000  # ~20 blocks behind tip for confirmation

    def default_start(self, head):
        return head - int(self.cfg.get("start_lookback_hours", 24)) * 3_600_000

    def block_to_cursor(self, block):
        return self.client.block_timestamp(block)

    def scan(self, from_ts, to_ts):
        events = []
        for tok in self.cfg["tokens"]:
            for ev in tok["events"]:
                for e in self.client.events(tok["address"], ev["name"], from_ts, to_ts):
                    events.append(self._to_event(tok, ev, e))
        events.sort(key=lambda e: (e.block, e.log_index))
        yield events, to_ts

    def _to_event(self, tok, ev, e):
        res = e["result"]
        holder_hex = res.get("_user") or res.get("_blackListedUser") or res.get("0")
        amount, source = None, "unknown"
        if ev["kind"] == "destroy":
            raw = res.get("_balance") or res.get("1")
            amount, source = (int(raw), "event") if raw is not None else (None, "unknown")
        else:
            try:
                # TronGrid has no historical eth_call; blacklisted funds can't move,
                # so the current balance equals the frozen amount unless later destroyed.
                amount, source = self.client.balance_of(tok["address"], holder_hex), "balanceOf@latest"
            except HTTPError as err:
                log.warning("tron balance lookup failed: %s", err)
        return FreezeEvent(
            chain=self.name, symbol=tok["symbol"], decimals=tok["decimals"],
            event=ev["name"], kind=ev["kind"], post=ev.get("post", True),
            holder=hex_to_base58(holder_hex), amount_raw=amount, amount_source=source,
            block=e["block_number"], tx=e["transaction_id"], log_index=e.get("event_index", 0),
            explorer_url=self.cfg["explorer_tx"].format(tx=e["transaction_id"]),
            timestamp=e.get("block_timestamp"),
        )
