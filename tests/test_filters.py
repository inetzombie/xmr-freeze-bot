"""Offline tests for per-event filters and the HTTP error-body passthrough added with the coverage expansion."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from freezebot import http
from freezebot.evm import EvmWatcher, matches_filter
from freezebot.tron import _matches

TUSD_EV = {"name": "Blacklisted", "kind": "freeze", "address_in": "topic1", "require_data_word": [0, 1],
           "topic0": "0xcf3473b85df1594d47b6958f29a32bea0abff9dd68296f7bf33443646793cfd8"}
HOLDER = "0x" + "0" * 24 + "9d5765ae1c95c21d4cc3b1d5bba71bad3b012b68"


def log(flag):
    return {"address": "0x0000000000085d4780b73119b644ae5ecd22b376", "topics": [TUSD_EV["topic0"], HOLDER],
            "data": "0x" + hex(flag)[2:].rjust(64, "0"), "blockNumber": "0x10", "logIndex": "0x0",
            "transactionHash": "0xaa"}


class FakeClient:
    def __init__(self, logs):
        self.logs = logs

    def block_number(self):
        return 100

    def get_logs(self, *a):
        return self.logs

    def balance_of(self, *a):
        return 5 * 10 ** 18


class TestEventFilters(unittest.TestCase):
    def test_tusd_unblacklist_is_dropped(self):
        self.assertTrue(matches_filter(log(1), TUSD_EV))
        self.assertFalse(matches_filter(log(0), TUSD_EV))
        cfg = {"name": "ethereum", "rpc_urls": [], "explorer_tx": "{tx}", "max_block_range": 10,
               "tokens": [{"symbol": "TUSD", "address": "0x0000000000085d4780B73119b644AE5ecd22b376",
                           "decimals": 18, "events": [TUSD_EV]}]}
        evs = [e for b, _ in EvmWatcher(cfg, client=FakeClient([log(1), log(0)])).scan(16, 16) for e in b]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].holder, "0x9d5765ae1c95c21d4cc3b1d5bba71bad3b012b68")

    def test_no_filter_keeps_everything(self):
        self.assertTrue(matches_filter(log(0), {"name": "x"}))

    def test_tron_require_result(self):
        ev = {"require_result": {"1": "true"}}
        self.assertTrue(_matches({"result": {"0": "0xab", "1": "true"}}, ev))
        self.assertFalse(_matches({"result": {"0": "0xab", "1": "false"}}, ev))
        self.assertTrue(_matches({"result": {}}, {}))


class TestHttpErrorBody(unittest.TestCase):
    def _raise(self, code, body):
        def opener(*a, **k):
            raise urllib.error.HTTPError("https://rpc.example", code, "x", {}, io.BytesIO(body))
        return opener

    def test_json_rpc_error_body_returned_without_retry(self):
        body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32602, "message": "range too large"}}).encode()
        with mock.patch("urllib.request.urlopen", side_effect=self._raise(413, body)) as m, \
                mock.patch("time.sleep") as s:
            r = http.request_json("https://rpc.example", {"x": 1})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(m.call_count, 1)
        s.assert_not_called()

    def test_rate_limit_is_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._raise(429, b"slow down")) as m, \
                mock.patch("time.sleep"):
            with self.assertRaises(http.HTTPError):
                http.request_json("https://rpc.example", {"x": 1})
        self.assertEqual(m.call_count, 3)


if __name__ == "__main__":
    unittest.main()
