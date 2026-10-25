"""Offline tests for Flux mode: rank derivation, the claim-marker guard, and no-backfill fresh start."""
import os
import unittest
from decimal import Decimal
from unittest import mock

from freezebot import __main__ as m
from freezebot import nostr
from freezebot.flux import FluxGuard, instance_rank, marker_tag, MARKER_KIND
from freezebot.models import FreezeEvent
from freezebot.store import Store

SK = bytes.fromhex("11" * 32)


def ev(i=0):
    return FreezeEvent(chain="ethereum", symbol="USDT", decimals=6, event="AddedBlackList", kind="freeze",
                       post=True, holder="0xabc", amount_raw=5_000_000, amount_source="test", block=100 + i,
                       tx=f"0x{i:064x}", log_index=0, explorer_url="https://example.invalid/tx", timestamp=1700000000)


class FakeRelays:
    """In-memory relay set: publish stores events, query matches kind/author/#d."""
    def __init__(self, fail=False):
        self.events, self.fail, self.queries = [], fail, 0

    def publish(self, relay, e):
        if self.fail:
            raise ConnectionError("down")
        self.events.append(e)
        return True, ""

    def query(self, relay, f):
        self.queries += 1
        if self.fail:
            raise ConnectionError("down")
        return [e for e in self.events if e["kind"] in f["kinds"] and e["pubkey"] in f["authors"]
                and any(t[0] == "d" and t[1] in f["#d"] for t in e["tags"])]


class Rec:
    name = "rec"
    def __init__(self):
        self.sent = []
    def send(self, msg, e):
        self.sent.append(e.uid)


def guard(rank, relays, **kw):
    return FluxGuard(SK, ["wss://a.invalid", "wss://b.invalid"], rank, step_seconds=30,
                     querier=relays.query, publisher=relays.publish, sleep=kw.pop("sleep", lambda s: None), **kw)


class RankTests(unittest.TestCase):
    def test_explicit_rank(self):
        self.assertEqual(instance_rank({"FREEZEBOT_INSTANCE_RANK": "2"}), 2)

    def test_hash_rank_deterministic_and_bounded(self):
        r = instance_rank({"FLUX_INSTANCE": "node-x"})
        self.assertEqual(r, instance_rank({"FLUX_INSTANCE": "node-x"}))
        self.assertIn(r, range(3))
        self.assertIn(instance_rank({}, hostname="h1"), range(3))

    def test_marker_tag_stable(self):
        self.assertEqual(marker_tag("a:b:0"), marker_tag("a:b:0"))
        self.assertNotEqual(marker_tag("a:b:0"), marker_tag("a:b:1"))


class GuardTests(unittest.TestCase):
    def test_leader_posts_without_waiting_or_querying(self):
        r, slept = FakeRelays(), []
        g = guard(0, r, sleep=slept.append)
        self.assertTrue(g.should_post("u"))
        self.assertEqual((slept, r.queries), ([], 0))

    def test_standby_skips_when_leader_marked(self):
        r, slept = FakeRelays(), []
        guard(0, r).mark("u")
        g = guard(2, r, sleep=slept.append)
        self.assertFalse(g.should_post("u"))
        self.assertEqual(slept, [60])
        self.assertEqual(r.events[0]["kind"], MARKER_KIND)
        self.assertTrue(nostr.schnorr_verify(bytes.fromhex(r.events[0]["id"]), bytes.fromhex(r.events[0]["pubkey"]),
                                             bytes.fromhex(r.events[0]["sig"])))

    def test_standby_posts_when_leader_silent(self):
        self.assertTrue(guard(1, FakeRelays()).should_post("u"))

    def test_marker_for_other_uid_ignored(self):
        r = FakeRelays()
        guard(0, r).mark("other")
        self.assertTrue(guard(1, r).should_post("u"))

    def test_relays_down_fail_open_by_default(self):
        self.assertIsNone(guard(1, FakeRelays(fail=True)).already_posted("u"))
        self.assertTrue(guard(1, FakeRelays(fail=True)).should_post("u"))

    def test_relays_down_fail_closed_option(self):
        self.assertFalse(guard(1, FakeRelays(fail=True), on_check_error="skip").should_post("u"))

    def test_mark_failure_does_not_raise(self):
        self.assertFalse(guard(0, FakeRelays(fail=True)).mark("u"))

    def test_from_env_requires_nostr(self):
        with mock.patch.dict(os.environ, {"NOSTR_NSEC": "", "NOSTR_RELAYS": "", "NOSTR_NSEC_FILE": ""}):
            with self.assertRaises(RuntimeError):
                FluxGuard.from_env()


class HandleTests(unittest.TestCase):
    def test_three_instances_post_once(self):
        relays, posted = FakeRelays(), []
        for rank in (0, 1, 2):  # each instance has its own store (no shared disk)
            out = Rec()
            r = m.handle(ev(), Store(":memory:"), [out], 1, guard(rank, relays))
            posted += out.sent
            self.assertEqual(r, "emitted" if rank == 0 else "peer-posted")
        self.assertEqual(len(posted), 1)

    def test_dead_leader_standby_takes_over(self):
        relays, out = FakeRelays(), Rec()
        self.assertEqual(m.handle(ev(), Store(":memory:"), [out], 1, guard(1, relays)), "emitted")
        out2 = Rec()
        self.assertEqual(m.handle(ev(), Store(":memory:"), [out2], 1, guard(2, relays)), "peer-posted")
        self.assertEqual((len(out.sent), len(out2.sent)), (1, 0))

    def test_no_guard_unchanged(self):
        out = Rec()
        self.assertEqual(m.handle(ev(), Store(":memory:"), [out], 1), "emitted")
        self.assertEqual(len(out.sent), 1)


class FakeWatcher:
    name, rescan_overlap = "fake", 0
    def __init__(self):
        self.scanned = []
    def head(self):
        return 1000
    def default_start(self, head):
        return head - 500
    def scan(self, a, b):
        self.scanned.append((a, b))
        yield [ev(1)], b


class FreshStartTests(unittest.TestCase):
    def test_flux_fresh_start_no_backfill(self):
        w, s, out = FakeWatcher(), Store(":memory:"), Rec()
        self.assertEqual(m.run_chain(w, s, [out], 1, fresh_start_at_head=True), {})
        self.assertEqual((w.scanned, out.sent, s.get_cursor("fake")), ([], [], 1000))

    def test_default_still_backfills_lookback(self):
        w, s, out = FakeWatcher(), Store(":memory:"), Rec()
        m.run_chain(w, s, [out], 1)
        self.assertEqual(w.scanned, [(500, 1000)])


class DeterministicNostrTests(unittest.TestCase):
    def test_same_note_id_across_instances(self):
        from freezebot.outputs import NostrOutput
        ids = []
        with mock.patch.dict(os.environ, {"NOSTR_NSEC": SK.hex(), "NOSTR_RELAYS": "wss://x.invalid"}):
            for _ in range(2):
                o = NostrOutput(live=True, publisher=lambda r, e: (True, ""))
                o.deterministic = True
                ids.append(o.send("msg", ev())["id"])
        self.assertEqual(ids[0], ids[1])


if __name__ == "__main__":
    unittest.main()
