"""Tests for the Telegram output adapter. All network I/O is mocked; nothing is
ever posted to the real Telegram API here."""
import json
import os
import tempfile
import unittest

from freezebot.outputs import TelegramOutput, build_outputs

MSG = ("219,408.69 $USDT should have gone through #Monero $XMR. Learn more at getmonero.org\n"
       "https://etherscan.io/tx/0xabc")
FAKE_TOKEN = "123456789:TEST-fake-bot-token-not-real"


class FakeTransport:
    """Stands in for _urllib_post: returns queued (status, body) responses and
    records every (url, payload) it was handed. No network."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, data, timeout):
        payload = json.loads(data.decode())
        self.calls.append((url, payload))
        return self._responses.pop(0)


class EnvGuard(unittest.TestCase):
    TG_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_FILE", "TELEGRAM_CHAT_ID")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.TG_VARS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestDryRun(EnvGuard):
    def test_dry_run_never_calls_transport(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
        os.environ["TELEGRAM_CHAT_ID"] = "@FreezeWatchXMR"
        t = FakeTransport([])  # would IndexError if called
        out = TelegramOutput(live=False, transport=t)
        out.send(MSG, None)
        self.assertEqual(t.calls, [])

    def test_dry_run_works_without_credentials(self):
        t = FakeTransport([])
        out = TelegramOutput(live=False, transport=t)
        out.send(MSG, None)  # no creds, but dry-run must not raise
        self.assertEqual(t.calls, [])


class TestLivePosting(EnvGuard):
    def _out(self, responses, **kw):
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
        os.environ.setdefault("TELEGRAM_CHAT_ID", "@FreezeWatchXMR")
        self.slept = []
        return TelegramOutput(live=True, transport=FakeTransport(responses),
                              sleep=lambda s: self.slept.append(s),
                              min_interval=0, **kw)

    def test_success_sends_exact_message(self):
        out = self._out([(200, {"ok": True, "result": {"message_id": 7}})])
        out.send(MSG, None)
        url, payload = out._transport.calls[0]
        self.assertIn("/bot" + FAKE_TOKEN + "/sendMessage", url)
        self.assertEqual(payload["text"], MSG)          # message unchanged
        self.assertEqual(payload["chat_id"], "@FreezeWatchXMR")
        self.assertTrue(payload["disable_web_page_preview"])

    def test_single_call_on_success(self):
        out = self._out([(200, {"ok": True, "result": {}})])
        out.send(MSG, None)
        self.assertEqual(len(out._transport.calls), 1)

    def test_429_retry_after_then_success(self):
        out = self._out([
            (429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                   "parameters": {"retry_after": 3}}),
            (200, {"ok": True, "result": {}}),
        ])
        out.send(MSG, None)
        self.assertEqual(len(out._transport.calls), 2)      # retried once
        self.assertTrue(any(s >= 3 for s in self.slept))    # honoured retry_after

    def test_transient_5xx_retries(self):
        out = self._out([
            (502, {"description": "Bad Gateway"}),
            (200, {"ok": True, "result": {}}),
        ])
        out.send(MSG, None)
        self.assertEqual(len(out._transport.calls), 2)

    def test_hard_error_raises_without_leaking_token(self):
        out = self._out([(400, {"ok": False, "error_code": 400,
                                 "description": "Bad Request: chat not found"})])
        with self.assertRaises(RuntimeError) as cm:
            out.send(MSG, None)
        msg = str(cm.exception)
        self.assertIn("chat not found", msg)
        self.assertNotIn(FAKE_TOKEN, msg)   # token must never appear in errors

    def test_rate_limit_spaces_messages(self):
        # With a positive interval and a controllable clock, the second send waits.
        clock = {"t": 100.0}
        os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
        os.environ["TELEGRAM_CHAT_ID"] = "@FreezeWatchXMR"
        slept = []
        out = TelegramOutput(
            live=True,
            transport=FakeTransport([(200, {"ok": True, "result": {}}),
                                     (200, {"ok": True, "result": {}})]),
            sleep=lambda s: slept.append(s),
            monotonic=lambda: clock["t"],
            min_interval=1.05,
        )
        out.send(MSG, None)          # first send: no wait (last_send is None)
        out.send(MSG, None)          # second send immediately: must wait ~1.05s
        self.assertTrue(slept and abs(slept[0] - 1.05) < 1e-6)


class TestMissingCreds(EnvGuard):
    def test_live_missing_token_raises(self):
        os.environ["TELEGRAM_CHAT_ID"] = "@FreezeWatchXMR"
        out = TelegramOutput(live=True, transport=FakeTransport([]))
        self.assertFalse(out.configured)
        with self.assertRaises(RuntimeError) as cm:
            out.send(MSG, None)
        self.assertIn("TELEGRAM_BOT_TOKEN", str(cm.exception))

    def test_live_missing_chat_id_raises(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
        out = TelegramOutput(live=True, transport=FakeTransport([]))
        self.assertFalse(out.configured)
        with self.assertRaises(RuntimeError) as cm:
            out.send(MSG, None)
        self.assertIn("TELEGRAM_CHAT_ID", str(cm.exception))


class TestTokenFromFile(EnvGuard):
    def test_token_read_from_file_env(self):
        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as f:
            f.write(FAKE_TOKEN + "\n")
            path = f.name
        try:
            os.environ["TELEGRAM_BOT_TOKEN_FILE"] = path
            os.environ["TELEGRAM_CHAT_ID"] = "@FreezeWatchXMR"
            out = TelegramOutput(
                live=True, min_interval=0, sleep=lambda s: None,
                transport=FakeTransport([(200, {"ok": True, "result": {}})]))
            self.assertTrue(out.configured)
            out.send(MSG, None)
            url, _ = out._transport.calls[0]
            self.assertIn(FAKE_TOKEN, url)
        finally:
            os.unlink(path)


class TestBuildOutputs(EnvGuard):
    def test_telegram_enabled_builds_with_options(self):
        cfg = {"stdout": {"enabled": True},
               "telegram": {"enabled": True, "min_interval": 0.5,
                            "disable_web_page_preview": False}}
        outs = build_outputs(cfg, live=False)
        tg = [o for o in outs if o.name == "telegram"][0]
        self.assertEqual(tg.min_interval, 0.5)
        self.assertFalse(tg.disable_preview)

    def test_telegram_disabled_not_built(self):
        cfg = {"stdout": {"enabled": True}, "telegram": {"enabled": False}}
        outs = build_outputs(cfg, live=False)
        self.assertEqual([o.name for o in outs], ["stdout"])


if __name__ == "__main__":
    unittest.main()


class TestMainLivePreflight(EnvGuard):
    """main() must exit non-zero with a clear error when --live is set but an
    enabled adapter lacks credentials, before doing any scanning/network."""
    def _write_cfg(self):
        cfg = {
            "db_path": tempfile.mkstemp(suffix=".sqlite3")[1],
            "poll_interval_seconds": 1,
            "min_amount": 1,
            "chains": [{"name": "ethereum", "type": "evm", "rpc_urls": [],
                        "tokens": []}],
            "outputs": {"stdout": {"enabled": True},
                        "telegram": {"enabled": True}},
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        return path

    def test_live_without_telegram_creds_exits_2(self):
        from freezebot.__main__ import main
        cfg_path = self._write_cfg()
        try:
            # --chain nonexistent -> no watchers built -> no network; preflight
            # runs first and should return 2 because telegram is unconfigured.
            rc = main(["--config", cfg_path, "--once", "--chain", "nope", "--live"])
            self.assertEqual(rc, 2)
        finally:
            os.unlink(cfg_path)

    def test_dry_run_without_creds_is_fine(self):
        from freezebot.__main__ import main
        cfg_path = self._write_cfg()
        try:
            rc = main(["--config", cfg_path, "--once", "--chain", "nope"])
            self.assertEqual(rc, 0)
        finally:
            os.unlink(cfg_path)
