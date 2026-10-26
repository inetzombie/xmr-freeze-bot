"""Offline tests for Mastodon batching / rate cap (fake clock, nothing posted)."""
import os
import unittest
from decimal import Decimal
from unittest import mock

from freezebot.batching import BatchingOutput, RateLimited, combine
from freezebot.outputs import MastodonOutput, TelegramOutput, build_outputs


class Ev:
    def __init__(self, i, amount="1000", symbol="USDT", chain="ethereum"):
        self.uid = f"{chain}:0x{i}:0"
        self.amount, self.symbol, self.chain = Decimal(amount), symbol, chain


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Inner:
    name, live, configured = "mastodon", True, True

    def __init__(self):
        self.posts, self.fail = [], []

    def send(self, msg, ev):
        if self.fail:
            raise self.fail.pop(0)
        self.posts.append((msg, ev))


def mk(**kw):
    c, inner = Clock(), Inner()
    return BatchingOutput(inner, clock=c, start_thread=False, **kw), inner, c


class TestBatching(unittest.TestCase):
    def test_single_alert_posted_verbatim_after_window(self):
        b, inner, c = mk(window=120)
        b.send("orig msg\nhttps://e/1", Ev(1))
        self.assertIsNone(b.flush())
        c.t += 119
        self.assertIsNone(b.flush())
        c.t += 1
        self.assertEqual(b.flush(), "orig msg\nhttps://e/1")
        self.assertEqual(len(inner.posts), 1)

    def test_multiple_alerts_combined_into_one_post(self):
        b, inner, c = mk(window=120)
        for i in range(3):
            b.send(f"m{i}", Ev(i))
            c.t += 10
        c.t += 120
        text = b.flush()
        self.assertEqual(len(inner.posts), 1)
        self.assertIn("3 freezes", text)
        self.assertTrue(inner.posts[0][1].uid.startswith("batch:"))
        self.assertLessEqual(len(text), 500)

    def test_overflow_summarised_with_link(self):
        items = [(f"m{i}", Ev(i, amount="123456789.12")) for i in range(40)]
        text = combine(items, 500, "https://example.org/feed")
        self.assertLessEqual(len(text), 500)
        self.assertRegex(text, r"\+\d+ more: https://example.org/feed")

    def test_rate_cap_rolls_excess_into_next_post(self):
        b, inner, c = mk(window=0, max_per_hour=4)
        for i in range(4):
            b.send(f"m{i}", Ev(i)); b.flush(); c.t += 60
        self.assertEqual(len(inner.posts), 4)
        for i in range(4, 9):
            b.send(f"m{i}", Ev(i)); self.assertIsNone(b.flush()); c.t += 60
        self.assertEqual(len(inner.posts), 4)
        c.t = 1000.0 + 3600  # first post ages out
        text = b.flush()
        self.assertEqual(len(inner.posts), 5)
        self.assertIn("5 freezes", text)

    def test_429_requeues_and_backs_off_retry_after(self):
        b, inner, c = mk(window=0)
        inner.fail = [RateLimited(600)]
        b.send("m", Ev(1))
        self.assertIsNone(b.flush())
        self.assertEqual(inner.posts, [])
        c.t += 599
        self.assertIsNone(b.flush())
        c.t += 1
        self.assertEqual(b.flush(), "m")


class TestMastodonPolite(unittest.TestCase):
    ENV = {"MASTODON_BASE_URL": "https://m.example", "MASTODON_ACCESS_TOKEN": "x"}

    def test_429_raises_rate_limited_without_retrying(self):
        calls = []

        def t(*a):
            calls.append(1)
            return 429, {"error": "Too many", "_retry_after": 42.0}
        with mock.patch.dict(os.environ, self.ENV):
            with self.assertRaises(RateLimited) as cm:
                MastodonOutput(live=True, transport=t).send("m", Ev(1))
        self.assertEqual(cm.exception.retry_after, 42.0)
        self.assertEqual(len(calls), 1)

    def test_build_outputs_defaults(self):
        with mock.patch.dict(os.environ, self.ENV):
            outs = build_outputs({"mastodon": {"enabled": True}, "telegram": {"enabled": True}}, live=False)
        m, t = outs
        self.assertIsInstance(m, BatchingOutput)
        self.assertEqual((m.window, m.max_per_hour, m.inner.visibility), (120.0, 4, "unlisted"))
        self.assertIsInstance(t, TelegramOutput)  # Telegram unwrapped by default: instant

    def test_build_outputs_config_overrides(self):
        cfg = {"mastodon": {"enabled": True, "visibility": "public",
                            "batching": {"window_seconds": 30, "max_posts_per_hour": 2}}}
        with mock.patch.dict(os.environ, self.ENV):
            m, = build_outputs(cfg)
        self.assertEqual((m.window, m.max_per_hour, m.inner.visibility), (30.0, 2, "public"))
        with mock.patch.dict(os.environ, self.ENV):
            m, = build_outputs({"mastodon": {"enabled": True, "batching": {"enabled": False}}})
        self.assertIsInstance(m, MastodonOutput)


if __name__ == "__main__":
    unittest.main()
