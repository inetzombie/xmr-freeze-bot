"""EVM watcher: eth_getLogs over configured token contracts/events."""
import logging
import os

from .http import HTTPError, request_json
from .models import FreezeEvent

log = logging.getLogger(__name__)
BALANCE_OF = "0x70a08231"


class RPCError(Exception):
    pass


class EvmClient:
    def __init__(self, urls):
        self.urls = list(urls)

    def call(self, method, params):
        errors = []
        for url in self.urls:
            try:
                r = request_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except HTTPError as e:
                errors.append(str(e))
                continue
            if "error" in r:
                errors.append(f"{url}: {r['error']}")
                continue
            return r["result"]
        raise RPCError(f"{method} failed on all RPCs: {errors}")

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def get_logs(self, addresses, topics0, from_block, to_block):
        return self.call("eth_getLogs", [{
            "address": addresses,
            "topics": [topics0],
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }])

    def balance_of(self, token, holder, block):
        data = BALANCE_OF + holder.lower().replace("0x", "").rjust(64, "0")
        res = self.call("eth_call", [{"to": token, "data": data}, hex(block)])
        return int(res, 16) if res and res != "0x" else 0


def _word(data_hex, i):
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return d[i * 64:(i + 1) * 64]


def _addr_from_word(word):
    return "0x" + word[-40:]


def matches_filter(log_entry, event_cfg):
    """Optional per-event filter, e.g. TrueUSD's Blacklisted(address indexed, bool) is emitted for both
    blacklisting (true) and un-blacklisting (false): {"require_data_word": [0, 1]} keeps only data word 0 == 1."""
    req = event_cfg.get("require_data_word")
    if not req:
        return True
    idx, want = req
    word = _word(log_entry.get("data", "0x"), int(idx))
    return bool(word) and int(word, 16) == int(want)


def decode_log(log_entry, event_cfg):
    """Return (holder_address, amount_raw_or_None) for a log matching event_cfg."""
    where = event_cfg.get("address_in", "topic1")
    if where == "data":
        holder = _addr_from_word(_word(log_entry["data"], 0))
        amount_word = 1
    else:
        holder = _addr_from_word(log_entry["topics"][int(where.replace("topic", ""))])
        amount_word = 0
    amount = None
    if event_cfg.get("amount_in") == "data":
        amount = int(_word(log_entry["data"], amount_word), 16)
    return holder, amount


class EvmWatcher:
    def __init__(self, chain_cfg, client=None):
        self.cfg = chain_cfg
        self.name = chain_cfg["name"]
        urls = list(chain_cfg["rpc_urls"])
        env_url = os.environ.get(chain_cfg.get("rpc_env", ""), "")
        if env_url:
            urls.insert(0, env_url)
        self.client = client or EvmClient(urls)
        self.by_key = {}
        for tok in chain_cfg["tokens"]:
            for ev in tok["events"]:
                self.by_key[(tok["address"].lower(), ev["topic0"].lower())] = (tok, ev)

    def head(self):
        return self.client.block_number() - int(self.cfg.get("confirmations", 6))

    def default_start(self, head):
        return max(0, head - int(self.cfg.get("start_lookback_blocks", 7200)))

    def scan(self, from_block, to_block):
        """Yield (events, chunk_end) per chunk so the caller can checkpoint."""
        step = int(self.cfg.get("max_block_range", 5000))
        addresses = sorted({t["address"] for t in self.cfg["tokens"]})
        topics = sorted({k[1] for k in self.by_key})
        start = from_block
        while start <= to_block:
            end = min(start + step - 1, to_block)
            try:
                logs = self.client.get_logs(addresses, topics, start, end)
            except RPCError:
                if step <= 100:
                    raise
                step //= 2  # public RPCs cap log ranges differently; shrink and retry
                log.info("%s: shrinking getLogs range to %d blocks", self.name, step)
                continue
            logs.sort(key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
            yield [self._to_event(l) for l in logs if not l.get("removed") and self._wanted(l)], end
            start = end + 1

    def _wanted(self, l):
        key = (l["address"].lower(), l["topics"][0].lower())
        return key in self.by_key and matches_filter(l, self.by_key[key][1])

    def _to_event(self, l):
        tok, ev = self.by_key[(l["address"].lower(), l["topics"][0].lower())]
        holder, amount = decode_log(l, ev)
        block = int(l["blockNumber"], 16)
        source = "event"
        if amount is None:
            amount, source = self.amount_at_freeze(tok["address"], holder, block)
        return FreezeEvent(
            chain=self.name, symbol=tok["symbol"], decimals=tok["decimals"],
            event=ev["name"], kind=ev["kind"], post=ev.get("post", True),
            holder=holder, amount_raw=amount, amount_source=source,
            block=block, tx=l["transactionHash"], log_index=int(l["logIndex"], 16),
            explorer_url=self.cfg["explorer_tx"].format(tx=l["transactionHash"]),
        )

    def amount_at_freeze(self, token, holder, block):
        """Balance at end of freeze block N (frozen funds can't move after).
        If that's 0 (e.g. funds destroyed in the same block/tx), fall back to N-1."""
        try:
            bal = self.client.balance_of(token, holder, block)
            if bal == 0 and block > 0:
                prev = self.client.balance_of(token, holder, block - 1)
                if prev:
                    return prev, f"balanceOf@{block - 1}"
            return bal, f"balanceOf@{block}"
        except RPCError as e:
            log.warning("balance lookup failed for %s: %s", holder, e)
            return None, "unknown"
