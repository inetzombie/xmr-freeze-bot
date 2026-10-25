"""Per-chain threads, Tron overlap re-scan, thread-safe store (offline)."""
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal

from freezebot import __main__ as m
from freezebot.models import FreezeEvent
from freezebot.store import Store
from freezebot.tron import TronWatcher


def ev(tx, chain="tron", block=1):
    return FreezeEvent(chain=chain, symbol="USDT", decimals=6, event="AddedBlackList", kind="freeze",
                       post=True, holder="T", amount_raw=5_000_000, amount_source="t", block=block,
                       tx=tx, log_index=0, explorer_url="x", timestamp=None)


class Out:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, msg, e):
        self.sent.append(e.uid)


class FakeTronClient:
    def __init__(self):
        self.calls, self.now = [], 1_000_000

    def now_block(self):
        return 1, self.now

    def events(self, contract, name, lo, hi):
        self.calls.append((lo, hi))
        return []


class TestTron(unittest.TestCase):
    def cfg(self, **kw):
        c = {"name": "tron", "type": "tron", "explorer_tx": "{tx}", "tokens": [
            {"symbol": "USDT", "address": "T", "decimals": 6, "events": [{"name": "AddedBlackList", "kind": "freeze"}]}]}
        c.update(kw)
        return c

    def test_head_near_tip_and_overlap(self):
        w = TronWatcher(self.cfg(), client=FakeTronClient())
        self.assertEqual(w.head(), 1_000_000 - 3_000)
        self.assertEqual(w.rescan_overlap, 600_000)

    def test_run_chain_rescans_behind_cursor_without_moving_it_back(self):
        client = FakeTronClient()
        w = TronWatcher(self.cfg(rescan_overlap_ms=10_000), client=client)
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "s.db"))
            st.set_cursor("tron", 990_000)
            m.run_chain(w, st, [], 1)
            self.assertEqual(client.calls[0], (980_001, 997_000))
            self.assertEqual(st.get_cursor("tron"), 997_000)

    def test_late_indexed_event_still_posted_once(self):
        with tempfile.TemporaryDirectory() as d:
            st, out = Store(os.path.join(d, "s.db")), Out()
            self.assertEqual(m.handle(ev("a"), st, [out], 1), "emitted")
            self.assertEqual(m.handle(ev("a"), st, [out], 1), "dup")
            self.assertEqual(len(out.sent), 1)


class TestThreads(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(m.chain_interval({"name": "tron"}, {"poll_interval_seconds": 60}), 12)
        self.assertEqual(m.chain_interval({"name": "base"}, {"poll_interval_seconds": 60}), 60)
        self.assertEqual(m.chain_interval({"name": "tron", "poll_interval_seconds": 5}, {}), 5)

    def test_concurrent_handle_posts_each_uid_once(self):
        with tempfile.TemporaryDirectory() as d:
            st, out = Store(os.path.join(d, "s.db")), Out()
            ts = [threading.Thread(target=lambda: [m.handle(ev(f"t{i}"), st, [out], 1) for i in range(30)])
                  for _ in range(6)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(sorted(out.sent), sorted(set(out.sent)))
            self.assertEqual(len(out.sent), 30)

    def test_slow_chain_does_not_block_fast_chain(self):
        class W:
            def __init__(self, name, delay):
                self.name, self.delay, self.polls = name, delay, 0

            def head(self):
                self.polls += 1
                time.sleep(self.delay)
                return 0

            def default_start(self, h):
                return 1  # > head -> nothing to scan

        slow, fast = W("slow", 5), W("fast", 0)
        with tempfile.TemporaryDirectory() as d:
            st, stop = Store(os.path.join(d, "s.db")), threading.Event()
            for w, iv in ((slow, 10), (fast, 0.05)):
                threading.Thread(target=m.poll_forever, args=(w, iv, st, [], 1, {}, stop), daemon=True).start()
            time.sleep(2.5)
            stop.set()
        self.assertEqual(slow.polls, 1)
        self.assertGreaterEqual(fast.polls, 2)


if __name__ == "__main__":
    unittest.main()
