"""Offline tests for the Nostr and Mastodon adapters (no network, nothing posted)."""
import hashlib
import json
import os
import unittest
from unittest import mock

from freezebot import nostr
from freezebot.outputs import MastodonOutput, NostrOutput


class Ev:
    uid = "ethereum:0xabc:1"


class TestBip340(unittest.TestCase):
    def test_official_vectors(self):
        sk = bytes.fromhex("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF")
        msg = bytes.fromhex("243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89")
        sig = nostr.schnorr_sign(msg, sk, bytes(31) + b"\x01")
        self.assertEqual(sig.hex().upper(),
                         "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE3341"
                         "8906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A")
        self.assertTrue(nostr.schnorr_verify(msg, nostr.pubkey(sk), sig))
        self.assertFalse(nostr.schnorr_verify(bytes(32), nostr.pubkey(sk), sig))

    def test_nip19_nsec(self):
        self.assertEqual(
            nostr.parse_seckey("nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5").hex(),
            "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa")

    def test_event_id_and_sig(self):
        sk = bytes.fromhex("67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa")
        ev = nostr.make_event("5 $USDT should have gone through #Monero $XMR.", sk, [["t", "monero"]], 1700000000)
        ser = json.dumps([0, ev["pubkey"], 1700000000, 1, [["t", "monero"]], ev["content"]],
                         separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(ev["id"], hashlib.sha256(ser.encode()).hexdigest())
        self.assertTrue(nostr.schnorr_verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]),
                                             bytes.fromhex(ev["sig"])))


ENV_N = {"NOSTR_NSEC": "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa",
         "NOSTR_RELAYS": "wss://a.example, wss://b.example"}


class TestNostrOutput(unittest.TestCase):
    def test_publishes_to_all_relays_and_tolerates_one_failure(self):
        calls = []

        def pub(relay, ev):
            calls.append(relay)
            if relay.startswith("wss://a"):
                raise ConnectionError("down")
            return True, ""
        with mock.patch.dict(os.environ, ENV_N):
            out = NostrOutput(live=True, publisher=pub)
            ev = out.send("hello", Ev())
        self.assertEqual(calls, ["wss://a.example", "wss://b.example"])
        self.assertEqual(ev["kind"], 1)
        self.assertIn(["t", "monero"], ev["tags"])

    def test_raises_when_no_relay_accepts(self):
        with mock.patch.dict(os.environ, ENV_N):
            out = NostrOutput(live=True, publisher=lambda r, e: (False, "blocked"))
            with self.assertRaises(RuntimeError):
                out.send("hello", Ev())

    def test_dry_run_never_publishes(self):
        with mock.patch.dict(os.environ, ENV_N):
            NostrOutput(live=False, publisher=lambda r, e: self.fail("published in dry-run")).send("x", Ev())


class TestMastodonOutput(unittest.TestCase):
    ENV = {"MASTODON_BASE_URL": "https://mastodon.example/", "MASTODON_ACCESS_TOKEN": "tok"}

    def test_posts_status_with_idempotency_key(self):
        seen = {}

        def transport(url, data, headers, timeout):
            seen.update(url=url, body=json.loads(data), headers=headers)
            return 200, {"id": "1"}
        with mock.patch.dict(os.environ, self.ENV):
            MastodonOutput(live=True, transport=transport).send("msg\nhttps://x", Ev())
        self.assertEqual(seen["url"], "https://mastodon.example/api/v1/statuses")
        self.assertEqual(seen["body"], {"status": "msg\nhttps://x", "visibility": "public"})
        self.assertEqual(seen["headers"]["Idempotency-Key"], Ev.uid)
        self.assertEqual(seen["headers"]["Authorization"], "Bearer tok")

    def test_error_does_not_leak_token(self):
        with mock.patch.dict(os.environ, self.ENV):
            out = MastodonOutput(live=True, transport=lambda *a: (401, {"error": "The access token is invalid"}))
            with self.assertRaises(RuntimeError) as cm:
                out.send("m", Ev())
        self.assertNotIn("tok\"", str(cm.exception))
        self.assertNotIn("Bearer", str(cm.exception))


@unittest.skipIf(os.environ.get("FREEZEBOT_SKIP_LIVE") == "1", "live tests disabled")
class TestLiveWebsocketReadOnly(unittest.TestCase):
    def test_relay_handshake_and_read(self):
        # Read-only: opens a subscription for one kind-1 note and closes. Nothing is published.
        for relay in ("wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net"):
            try:
                ws = nostr.WebSocket(relay)
            except OSError:
                continue
            try:
                ws.send_text(json.dumps(["REQ", "fb-test", {"kinds": [1], "limit": 1}]))
                msg = json.loads(ws.recv_text())
                self.assertIn(msg[0], ("EVENT", "EOSE", "NOTICE"))
                return
            finally:
                ws.close()
        self.skipTest("no relay reachable")


if __name__ == "__main__":
    unittest.main()
