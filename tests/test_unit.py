import json
import os
import tempfile
import unittest
from decimal import Decimal

from freezebot.__main__ import handle, run_chain
from freezebot.evm import EvmWatcher, decode_log
from freezebot.fmt import format_amount, format_message
from freezebot.models import FreezeEvent
from freezebot.outputs import MastodonOutput, NostrOutput, TelegramOutput, build_outputs
from freezebot.store import Store
from freezebot.tron import hex_to_base58

ROOT = os.path.dirname(os.path.dirname(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
ETH = next(c for c in CFG["chains"] if c["name"] == "ethereum")
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
ADDED = "0x42e160154868087d6bfdc0ca23d96a1c1cfa32f1b72ba9ba27b69b98a0d819dc"
DESTROYED = "0x61e6e66b0d6339b2980aecc6ccc0039736791f0ccde9ed512e789a7fbdd698c6"
BLACKLISTED = "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855"
HOLDER = "6c1aea2787cf3e78b519aa3e2f2d87e83b553c65"


def ev(**kw):
    base = dict(chain="ethereum", symbol="USDT", decimals=6, event="AddedBlackList", kind="freeze", post=True,
                holder="0x" + HOLDER, amount_raw=219408685448, amount_source="balanceOf@1", block=1,
                tx="0xabc", log_index=0, explorer_url="https://etherscan.io/tx/0xabc")
    base.update(kw)
    return FreezeEvent(**base)


class FakeOut:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, msg, e):
        self.sent.append(msg)


class FakeClient:
    """Mimics EvmClient; balance is 0 at block N and 5 USDT at N-1 for one holder."""

    def __init__(self, logs, balances):
        self.logs, self.balances = logs, balances

    def block_number(self):
        return 1000

    def get_logs(self, addresses, topics0, f, t):
        return [l for l in self.logs if f <= int(l["blockNumber"], 16) <= t]

    def balance_of(self, token, holder, block):
        return self.balances.get((holder.lower(), block), 0)


class TestFormat(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(format_amount(Decimal("12500")), "12,500")
        self.assertEqual(format_amount(Decimal("219408.685448")), "219,408.69")
        self.assertEqual(format_amount(Decimal("0.5")), "0.50")

    def test_message_matches_op_style(self):
        self.assertEqual(
            format_message(ev()),
            "219,408.69 $USDT should have gone through #Monero $XMR. Learn more at getmonero.org\n"
            "https://etherscan.io/tx/0xabc")


class TestDecode(unittest.TestCase):
    def test_usdt_added_blacklist_address_in_data(self):
        log = {"topics": [ADDED], "data": "0x" + HOLDER.rjust(64, "0")}
        self.assertEqual(decode_log(log, {"address_in": "data"}), ("0x" + HOLDER, None))

    def test_usdt_destroyed_carries_amount(self):
        log = {"topics": [DESTROYED], "data": "0x" + HOLDER.rjust(64, "0") + hex(40010317298)[2:].rjust(64, "0")}
        self.assertEqual(decode_log(log, {"address_in": "data", "amount_in": "data"}), ("0x" + HOLDER, 40010317298))

    def test_usdc_blacklisted_address_in_topic(self):
        log = {"topics": [BLACKLISTED, "0x" + HOLDER.rjust(64, "0")], "data": "0x"}
        self.assertEqual(decode_log(log, {"address_in": "topic1"}), ("0x" + HOLDER, None))

    def test_tron_base58(self):
        # USDT-TRC20 contract: 41a614f803b6fd780986a42c78ec9c7f77e6ded13c
        self.assertEqual(hex_to_base58("0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"), "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"))

    def test_dedupe_and_filters(self):
        out = FakeOut()
        self.assertEqual(handle(ev(), self.store, [out], 1), "emitted")
        self.assertEqual(handle(ev(), self.store, [out], 1), "dup")
        self.assertEqual(handle(ev(tx="0x2", amount_raw=0), self.store, [out], 1), "skipped")
        self.assertEqual(handle(ev(tx="0x3", amount_raw=None), self.store, [out], 1), "skipped")
        self.assertEqual(handle(ev(tx="0x4", kind="destroy", post=False), self.store, [out], 1), "skipped")
        self.assertEqual(len(out.sent), 1)

    def test_watcher_cursor_and_balance_fallback(self):
        logs = [
            {"address": USDT, "topics": [ADDED], "data": "0x" + HOLDER.rjust(64, "0"),
             "blockNumber": hex(990), "logIndex": "0x1", "transactionHash": "0xaa"},
            {"address": USDT, "topics": [DESTROYED],
             "data": "0x" + HOLDER.rjust(64, "0") + hex(5_000_000)[2:].rjust(64, "0"),
             "blockNumber": hex(990), "logIndex": "0x2", "transactionHash": "0xaa"},
        ]
        w = EvmWatcher(dict(ETH, max_block_range=7), client=FakeClient(logs, {("0x" + HOLDER, 989): 5_000_000}))
        out = FakeOut()
        counts = run_chain(w, self.store, [out], 1)
        self.assertEqual(counts, {"emitted": 1, "skipped": 1})
        self.assertTrue(out.sent[0].startswith("5 $USDT should have gone through #Monero $XMR."))
        self.assertEqual(self.store.get_cursor("ethereum"), 1000 - ETH["confirmations"])
        self.assertEqual(run_chain(w, self.store, [out], 1), {})  # nothing new


class TestOutputs(unittest.TestCase):
    def test_default_config_is_stdout_only(self):
        self.assertEqual([o.name for o in build_outputs(CFG["outputs"])], ["stdout"])

    def test_stubs_never_post(self):
        for cls in (NostrOutput, MastodonOutput, TelegramOutput):
            cls(live=False).send("x", ev())  # dry-run: logs only
            with self.assertRaises((RuntimeError, NotImplementedError)):
                cls(live=True).send("x", ev())


if __name__ == "__main__":
    unittest.main()
