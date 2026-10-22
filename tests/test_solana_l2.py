import json
import os
import tempfile
import unittest
from decimal import Decimal

from freezebot.evm import EvmWatcher, RPCError, decode_log
from freezebot.solana import SolanaWatcher, balance_from_meta, extract_freezes, read_helius_key

ROOT = os.path.dirname(os.path.dirname(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
CHAINS = {c["name"]: c for c in CFG["chains"]}
SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PYUSD = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
MEME = "MemeMint1111111111111111111111111111111111"


def freeze_ix(program, account, mint):
    return {"programId": program, "parsed": {"type": "freezeAccount", "info": {"account": account, "mint": mint}}}


def fake_tx(outer, inner=(), balances=()):
    keys = ["payer", "acctA", "acctB", "acctC"]
    return {"slot": 123, "blockTime": 1, "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys],
                                                                      "instructions": list(outer)}},
            "meta": {"innerInstructions": list(inner), "preTokenBalances": [],
                     "postTokenBalances": [{"accountIndex": keys.index(a), "mint": m, "owner": "wallet",
                                            "uiTokenAmount": {"amount": str(amt), "decimals": 6}}
                                           for a, m, amt in balances]}}


class FakePrices:
    def __init__(self, price):
        self.price = price

    def usd_price(self, mint):
        return self.price

    def symbol(self, mint):
        return "MEME"


class FakeSolClient:
    def token_account_balance(self, a):
        return 7


class TestSolanaParsing(unittest.TestCase):
    def test_top_level_and_inner_freezes_both_programs(self):
        tx = fake_tx(
            [{"programId": "11111111111111111111111111111111", "parsed": {"type": "advanceNonce", "info": {}}},
             freeze_ix(SPL, "acctA", USDC)],
            inner=[{"index": 0, "instructions": [
                {"programId": T22, "parsed": {"type": "thawAccount", "info": {}}},
                freeze_ix(T22, "acctB", PYUSD)]}])
        got = extract_freezes(tx)
        self.assertEqual([(p, i["account"]) for p, i in got], [(1000, "acctA"), (2, "acctB")])

    def test_ignores_non_token_programs(self):
        tx = fake_tx([freeze_ix("FakeProgram111", "acctA", USDC)])
        self.assertEqual(extract_freezes(tx), [])

    def test_balance_from_tx_meta(self):
        tx = fake_tx([], balances=[("acctA", USDC, 53617075656)])
        self.assertEqual(balance_from_meta(tx, "acctA"), (53617075656, 6, "wallet"))
        self.assertEqual(balance_from_meta(tx, "acctC"), (None, None, None))

    def _watcher(self, price):
        cfg = dict(CHAINS["solana"], usd_threshold=1000)
        return SolanaWatcher(cfg, client=FakeSolClient(), prices=FakePrices(price))

    def test_allowlisted_posts_with_symbol_and_solscan(self):
        w = self._watcher(None)
        [ev] = w.events_from_tx("SIG", fake_tx([freeze_ix(SPL, "acctA", USDC)], balances=[("acctA", USDC, 5_000_000)]))
        self.assertTrue(ev.post)
        self.assertEqual((ev.symbol, ev.amount, ev.explorer_url), ("USDC", Decimal(5), "https://solscan.io/tx/SIG"))

    def test_non_allowlisted_below_threshold_is_db_only(self):
        w = self._watcher(Decimal("0.0001"))
        [ev] = w.events_from_tx("SIG", fake_tx([freeze_ix(SPL, "acctA", MEME)], balances=[("acctA", MEME, 10**12)]))
        self.assertFalse(ev.post)
        self.assertEqual(ev.skip_reason, "not-allowlisted")

    def test_non_allowlisted_above_threshold_posts(self):
        w = self._watcher(Decimal("2"))
        [ev] = w.events_from_tx("SIG", fake_tx([freeze_ix(SPL, "acctA", MEME)], balances=[("acctA", MEME, 10**12)]))
        self.assertTrue(ev.post)
        self.assertEqual(ev.symbol, "MEME")

    def test_missing_meta_balance_falls_back_to_rpc(self):
        w = self._watcher(None)
        [ev] = w.events_from_tx("SIG", fake_tx([freeze_ix(SPL, "acctC", USDC)]))
        self.assertEqual((ev.amount_raw, ev.amount_source), (7, "balance@latest"))

    def test_helius_key_from_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# comment\nHELIUS_API_KEY=dummy-not-a-real-key\n")
        old = {k: os.environ.pop(k, None) for k in ("HELIUS_API_KEY", "HELIUS_API_KEY_FILE")}
        try:
            os.environ["HELIUS_API_KEY_FILE"] = f.name
            self.assertEqual(read_helius_key(), "dummy-not-a-real-key")
        finally:
            os.unlink(f.name)
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


class TestEvmAdditions(unittest.TestCase):
    def test_tether_v2_destroyed_blocked_funds(self):
        # Polygon USDT0 DestroyedBlockedFunds: holder indexed (topic1), amount in data word 0
        log = {"topics": ["0x6a28", "0x" + "aa2fda50ca3731970d363d750adf676f6783490c".rjust(64, "0")],
               "data": "0x" + "12c0d35b7e6".rjust(64, "0")}
        self.assertEqual(decode_log(log, {"address_in": "topic1", "amount_in": "data"}),
                         ("0xaa2fda50ca3731970d363d750adf676f6783490c", 1288711813094))

    def test_range_shrinks_when_rpc_rejects(self):
        class Client:
            calls = []

            def get_logs(self, a, t, f, to):
                self.calls.append(to - f + 1)
                if to - f + 1 > 250:
                    raise RPCError("range too large")
                return []
        c = Client()
        w = EvmWatcher(dict(CHAINS["base"], max_block_range=1000), client=c)
        list(w.scan(1, 1000))
        self.assertEqual(c.calls[:3], [1000, 500, 250])

    def test_l2_configs_are_complete(self):
        for name in ("arbitrum", "base", "polygon", "avalanche"):
            c = CHAINS[name]
            self.assertTrue(c["rpc_urls"] and "{tx}" in c["explorer_tx"])
            for t in c["tokens"]:
                self.assertTrue(any(e["kind"] == "freeze" and e.get("post") for e in t["events"]))


if __name__ == "__main__":
    unittest.main()
