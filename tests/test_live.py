"""Live checks against real historical freezes. Skip with FREEZEBOT_SKIP_LIVE=1."""
import json
import os
import unittest

from freezebot.evm import EvmWatcher
from freezebot.fmt import format_message
from freezebot.tron import TronWatcher

ROOT = os.path.dirname(os.path.dirname(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
CHAINS = {c["name"]: c for c in CFG["chains"]}
SKIP = os.environ.get("FREEZEBOT_SKIP_LIVE") == "1"


@unittest.skipIf(SKIP, "live tests disabled")
class TestLiveEthereum(unittest.TestCase):
    def test_real_usdt_added_blacklist(self):
        # Tether blacklisted 0x6c1a...3c65 in block 26,136,686 (tx 0xa8da...2c2f).
        w = EvmWatcher(CHAINS["ethereum"])
        events = [e for batch, _ in w.scan(26136686, 26136686) for e in batch]
        usdt = [e for e in events if e.event == "AddedBlackList"]
        self.assertEqual(len(usdt), 1)
        e = usdt[0]
        self.assertEqual(e.holder, "0x6c1aea2787cf3e78b519aa3e2f2d87e83b553c65")
        self.assertEqual(e.tx, "0xa8dafd10f768b389e069306cdea66b84243253f8ce6b129f5d9170a9e1243c2f")
        self.assertEqual(e.amount_raw, 219408685448)
        self.assertEqual(
            format_message(e),
            "219,408.69 $USDT should have gone through #Monero $XMR. Learn more at getmonero.org\n"
            "https://etherscan.io/tx/0xa8dafd10f768b389e069306cdea66b84243253f8ce6b129f5d9170a9e1243c2f")

    def test_real_usdc_blacklisted(self):
        w = EvmWatcher(CHAINS["ethereum"])
        events = [e for batch, _ in w.scan(0x18e1d9c, 0x18e1d9c) for e in batch]
        self.assertTrue(any(e.symbol == "USDC" and e.holder == "0x601d77306917263f0ea2a8fa8a7516b3aa0a0a1f"
                            for e in events))


@unittest.skipIf(SKIP, "live tests disabled")
class TestLiveTron(unittest.TestCase):
    def test_real_tron_usdt_added_blacklist(self):
        w = TronWatcher(CHAINS["tron"])
        ts = w.block_to_cursor(87312845)
        events = [e for batch, _ in w.scan(ts, ts + 2999) for e in batch]
        hit = [e for e in events if e.tx == "db97d2ba41788412974fd7ace030343a2a0b6ff002793ffe3d54e4be5fa7465a"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].event, "AddedBlackList")
        self.assertIsNotNone(hit[0].amount_raw)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(SKIP, "live tests disabled")
class TestLiveSolanaAndL2(unittest.TestCase):
    def test_real_solana_usdc_freeze(self):
        # Circle froze USDC token account V4acAD... (owner 2djfJ6...) holding 53,617.075656 USDC.
        from freezebot.solana import SolanaWatcher
        w = SolanaWatcher(CHAINS["solana"])
        sig = "2YrBNgK5j3BB8cfCCiDwLNXNPSLGoodSRs7tSq3tXYPuXpQkX75o5eJbrbdPD8t9nJ75zigMPb4ravSEaD7VQTyQ"
        [e] = w.events_from_tx(sig, w.client.transaction(sig))
        self.assertEqual((e.symbol, e.holder, e.owner, e.amount_raw, e.post),
                         ("USDC", "V4acADRAk22JWgLuq4tqKzmNM7swysfibGmADzjYo5q",
                          "2djfJ6oAEYkvTgsx5mhzs3inyhEoJb7Ey3E4EbDqmhVT", 53617075656, True))
        self.assertEqual(format_message(e),
                         "53,617.08 $USDC should have gone through #Monero $XMR. Learn more at getmonero.org\n"
                         "https://solscan.io/tx/" + sig)

    def test_real_arbitrum_usdt0_block_placed(self):
        w = EvmWatcher(CHAINS["arbitrum"])
        [e] = [e for b, _ in w.scan(514843699, 514843699) for e in b]
        self.assertEqual((e.symbol, e.event, e.amount_raw), ("USDT", "BlockPlaced", 23000063904))

    def test_real_avalanche_usdt_block_placed(self):
        w = EvmWatcher(CHAINS["avalanche"])
        [e] = [e for b, _ in w.scan(88179450, 88179450) for e in b]
        self.assertEqual((e.holder, e.amount_raw), ("0x4ad0aaaee18a66b12caff45df515c0047a8dac0c", 43587000000))

    def test_real_pyusd_address_frozen(self):
        w = EvmWatcher(CHAINS["ethereum"])
        evs = [e for b, _ in w.scan(20112292, 20112292) for e in b if e.symbol == "PYUSD"]
        self.assertEqual([e.event for e in evs], ["AddressFrozen"])
