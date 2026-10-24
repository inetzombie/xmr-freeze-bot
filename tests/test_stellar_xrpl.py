"""Offline tests for the Stellar and XRPL watchers, using recorded RPC/Horizon data in tests/fixtures."""
import json
import os
import tempfile
import unittest
from decimal import Decimal

from freezebot.__main__ import handle, run_chain
from freezebot.fmt import format_message
from freezebot.stellar import StellarWatcher, is_freeze, ledger_cursor
from freezebot.store import Store
from freezebot.xrpl import XrplClient, XrplWatcher, balance_from_meta, freeze_kind, normalize

ROOT = os.path.dirname(os.path.dirname(__file__))
FIX = os.path.join(ROOT, "tests", "fixtures")
CFG = json.load(open(os.path.join(ROOT, "config.json")))
CHAINS = {c["name"]: c for c in CFG["chains"]}
USDC_XLM = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
EURC_XLM = "GDHU6WRG4IEQXM5NZ4BMPKOXHW76MZM4Y2IEMFDVXBSDP6SJY4ITNPP2"
RLUSD = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
USDC_XRPL = "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE"
FROZEN = "rnXyVQzgxZe7TR1EPzTkGj2jxH4LMJYh66"
HOLDER = "GBHOLDERHOLDERHOLDERHOLDERHOLDERHOLDERHOLDERHOLDERHOLDER"


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class FakeOut:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, msg, e):
        self.sent.append(msg)


def op(type_, ledger, tx_order=1, idx=1, source=USDC_XLM, **kw):
    pt = (ledger << 32) | (tx_order << 12) | idx
    d = {"id": str(pt), "paging_token": str(pt), "transaction_successful": True, "source_account": source,
         "type": type_, "created_at": "2026-10-24T12:00:00Z", "transaction_hash": "AB" * 32,
         "asset_type": "credit_alphanum4", "asset_code": "USDC", "asset_issuer": source}
    d.update(kw)
    return d


class FakeHorizon:
    def __init__(self, ops_by_issuer, balances=None, ledger=100):
        self.ops, self.balances, self.ledger, self.calls = ops_by_issuer, balances or {}, ledger, []

    def latest_ledger(self):
        return self.ledger

    def operations(self, account, cursor, limit=200):
        self.calls.append((account, int(cursor)))
        recs = [o for o in self.ops.get(account, []) if int(o["paging_token"]) > int(cursor)]
        return recs[:limit]

    def balance(self, account, code, issuer):
        return self.balances.get((account, code))


class TestStellar(unittest.TestCase):
    def watcher(self, client):
        return StellarWatcher(CHAINS["stellar"], client=client)

    def test_config_issuers(self):
        iss = {i["issuer"]: [a["code"] for a in i["assets"]] for i in CHAINS["stellar"]["issuers"]}
        self.assertEqual(iss[USDC_XLM], ["USDC"])
        self.assertEqual(iss[EURC_XLM], ["EURC"])
        self.assertIn("GDQE7IXJ4HUHV6RQHIUPRJSEZE4DRS5WY577O2FY6YQ5LVWZ7JZTU2V5", iss)

    def test_freeze_detection_rules(self):
        self.assertTrue(is_freeze({"type": "set_trust_line_flags", "clear_flags": [1], "clear_flags_s": ["authorized"]}))
        # authorized -> maintain-liabilities only is still a freeze (holder can't send or receive)
        self.assertTrue(is_freeze({"type": "set_trust_line_flags", "clear_flags": [1], "set_flags": [2]}))
        self.assertFalse(is_freeze({"type": "set_trust_line_flags", "set_flags": [1]}))  # re-authorize
        self.assertFalse(is_freeze({"type": "set_trust_line_flags", "clear_flags": [4]}))  # clawback flag only
        self.assertTrue(is_freeze({"type": "allow_trust", "authorize": False}))
        self.assertFalse(is_freeze({"type": "allow_trust", "authorize": True}))

    def test_real_issuer_ops_have_no_freezes(self):
        recs = fixture("stellar_usdc_issuer_ops.json")["records"]
        w = self.watcher(FakeHorizon({}))
        self.assertEqual([w.event_from_op(USDC_XLM, r) for r in recs], [None] * len(recs))

    def test_freeze_posts_with_balance_and_explorer_link(self):
        f = op("set_trust_line_flags", 60000000, trustor=HOLDER, clear_flags=[1], clear_flags_s=["authorized"])
        client = FakeHorizon({USDC_XLM: [f]}, {(HOLDER, "USDC"): "12345.6700000"})
        w = self.watcher(client)
        ev = w.event_from_op(USDC_XLM, f)
        self.assertEqual((ev.kind, ev.post, ev.symbol, ev.holder, ev.block), ("freeze", True, "USDC", HOLDER, 60000000))
        self.assertEqual(ev.amount, Decimal("12345.67"))
        self.assertEqual(ev.amount_source, "balance@latest")
        self.assertEqual(format_message(ev), "12,345.67 $USDC should have gone through #Monero $XMR. Learn more at "
                         "getmonero.org\nhttps://stellar.expert/explorer/public/tx/" + "AB" * 32)

    def test_clawback_is_recorded_not_posted(self):
        c = op("clawback", 60000000, **{"from": HOLDER, "amount": "5.0000000"})
        ev = self.watcher(FakeHorizon({})).event_from_op(USDC_XLM, c)
        self.assertEqual((ev.kind, ev.post, ev.amount_raw), ("destroy", False, 50000000))

    def test_ignores_failed_foreign_and_unknown_asset(self):
        w = self.watcher(FakeHorizon({}))
        f = dict(trustor=HOLDER, clear_flags=[1])
        self.assertIsNone(w.event_from_op(USDC_XLM, op("set_trust_line_flags", 1, transaction_successful=False, **f)))
        self.assertIsNone(w.event_from_op(USDC_XLM, op("set_trust_line_flags", 1, source=HOLDER, **f)))
        ev = w.event_from_op(USDC_XLM, op("set_trust_line_flags", 1, asset_code="XYZ", **f))
        self.assertFalse(ev.post)
        self.assertEqual(ev.skip_reason, "not-allowlisted")

    def test_cursor_paging_and_dedupe_through_run_chain(self):
        f1 = op("set_trust_line_flags", 95, trustor=HOLDER, clear_flags=[1])
        f2 = op("allow_trust", 99, tx_order=2, trustor=HOLDER, authorize=False, transaction_hash="CD" * 32)
        late = op("set_trust_line_flags", 101, trustor=HOLDER, clear_flags=[1], transaction_hash="EF" * 32)
        client = FakeHorizon({USDC_XLM: [f1, f2, late]}, {(HOLDER, "USDC"): "10"}, ledger=100)
        w, out = self.watcher(client), FakeOut()
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "s.db"))
            store.set_cursor("stellar", ledger_cursor(90) - 1)
            self.assertEqual(run_chain(w, store, [out], 1), {"emitted": 2})
            self.assertEqual(store.get_cursor("stellar"), ledger_cursor(101) - 1)
            # Horizon was asked with an exclusive cursor = stored cursor
            self.assertIn((USDC_XLM, ledger_cursor(90) - 1), client.calls)
            client.ledger = 101
            self.assertEqual(run_chain(w, store, [out], 1), {"emitted": 1})
            self.assertEqual(run_chain(w, store, [out], 1), {})
        self.assertEqual(len(out.sent), 3)

    def test_default_start_lookback(self):
        w = self.watcher(FakeHorizon({}, ledger=10000))
        head = w.head()
        self.assertEqual(head >> 32, 10000)
        self.assertEqual(w.default_start(head), ledger_cursor(10000 - CHAINS["stellar"]["start_lookback_ledgers"]))


class FakeXrpl:
    def __init__(self, txs_by_issuer, head=106382500, lines=None):
        self.txs, self.h, self.lines = txs_by_issuer, head, lines or {}

    def validated_ledger(self):
        return self.h

    def issuer_txs(self, account, lo, hi, types=None):
        return [t for t in self.txs.get(account, []) if lo <= t["ledger_index"] <= hi]

    def trust_line_balance(self, holder, issuer, currency):
        return self.lines.get(holder)


class TestXrpl(unittest.TestCase):
    def setUp(self):
        self.rlusd = normalize(fixture("xrpl_rlusd_freeze_2026-08-18.json"))
        self.usdc = normalize(fixture("xrpl_usdc_freeze_2025-05-13.json"))

    def iss(self, addr):
        return next(i for i in CHAINS["xrpl"]["issuers"] if i["issuer"] == addr)

    def test_real_rlusd_deep_freeze_2026_08_18(self):
        self.assertEqual(freeze_kind(self.rlusd, RLUSD), ("TrustSet:tfSetDeepFreeze", "freeze"))
        ev = XrplWatcher(CHAINS["xrpl"], client=FakeXrpl({})).event_from_tx(self.iss(RLUSD), self.rlusd)
        self.assertEqual((ev.symbol, ev.holder, ev.block, ev.post), ("RLUSD", FROZEN, 106382493, True))
        self.assertEqual(ev.tx, "037B0D8FDF1FFA645CFBE6CA7FE804E8D35F03E36267DA9771D26D7EF5997682")
        self.assertEqual(ev.amount_raw, 0)  # pre-emptive freeze of an empty line: recorded, below min_amount
        self.assertEqual(ev.amount_source, "tx-meta-balance")
        self.assertEqual(ev.timestamp, 1787073200)  # 2026-08-18T17:13:20Z
        self.assertTrue(ev.explorer_url.startswith("https://livenet.xrpl.org/transactions/037B0D8F"))

    def test_real_circle_usdc_freeze_2025_05_13(self):
        self.assertEqual(freeze_kind(self.usdc, USDC_XRPL), ("TrustSet:tfSetDeepFreeze", "freeze"))
        ev = XrplWatcher(CHAINS["xrpl"], client=FakeXrpl({})).event_from_tx(self.iss(USDC_XRPL), self.usdc)
        self.assertEqual((ev.symbol, ev.holder, ev.block), ("USDC", FROZEN, 96102710))
        self.assertEqual(freeze_kind(self.usdc, RLUSD), None)  # not sent by that issuer

    def test_flag_rules(self):
        base = {"Account": RLUSD, "meta": {"TransactionResult": "tesSUCCESS"}}
        self.assertEqual(freeze_kind({**base, "TransactionType": "TrustSet", "Flags": 0x00100000}, RLUSD),
                         ("TrustSet:tfSetFreeze", "freeze"))
        self.assertIsNone(freeze_kind({**base, "TransactionType": "TrustSet", "Flags": 0x00200000}, RLUSD))  # clear
        self.assertIsNone(freeze_kind({**base, "TransactionType": "TrustSet", "Flags": 0x00020000}, RLUSD))  # noRipple
        self.assertEqual(freeze_kind({**base, "TransactionType": "AccountSet", "SetFlag": 7}, RLUSD),
                         ("AccountSet:asfGlobalFreeze", "freeze"))
        self.assertIsNone(freeze_kind({**base, "TransactionType": "AccountSet", "ClearFlag": 7}, RLUSD))
        self.assertEqual(freeze_kind({**base, "TransactionType": "Clawback"}, RLUSD), ("Clawback", "destroy"))
        failed = {**base, "meta": {"TransactionResult": "tecNO_PERMISSION"}, "TransactionType": "TrustSet",
                  "Flags": 0x00100000}
        self.assertIsNone(freeze_kind(failed, RLUSD))

    def test_balance_from_meta_and_fallback(self):
        tx = json.loads(json.dumps(self.rlusd))
        for n in tx["meta"]["AffectedNodes"]:
            m = n.get("ModifiedNode") or n.get("CreatedNode")
            if m and m["LedgerEntryType"] == "RippleState":
                (m.get("FinalFields") or m["NewFields"])["Balance"]["value"] = "-2500.5"
        cur = "524C555344000000000000000000000000000000"
        self.assertEqual(balance_from_meta(tx, FROZEN, RLUSD, cur), "2500.5")
        ev = XrplWatcher(CHAINS["xrpl"], client=FakeXrpl({})).event_from_tx(self.iss(RLUSD), tx)
        self.assertEqual(format_message(ev).splitlines()[0],
                         "2,500.50 $RLUSD should have gone through #Monero $XMR. Learn more at getmonero.org")
        tx["meta"]["AffectedNodes"] = []
        ev = XrplWatcher(CHAINS["xrpl"], client=FakeXrpl({}, lines={FROZEN: "-7"})).event_from_tx(self.iss(RLUSD), tx)
        self.assertEqual((ev.amount, ev.amount_source), (Decimal(7), "balance@latest"))

    def test_global_freeze_unknown_amount_not_posted(self):
        tx = {"Account": RLUSD, "TransactionType": "AccountSet", "SetFlag": 7, "hash": "AA" * 32,
              "ledger_index": 5, "meta": {"TransactionResult": "tesSUCCESS"}}
        ev = XrplWatcher(CHAINS["xrpl"], client=FakeXrpl({})).event_from_tx(self.iss(RLUSD), tx)
        self.assertEqual((ev.holder, ev.amount, ev.symbol), (RLUSD, None, "RLUSD"))
        with tempfile.TemporaryDirectory() as d:
            store, out = Store(os.path.join(d, "s.db")), FakeOut()
            self.assertEqual(handle(ev, store, [out], 1), "skipped")
            self.assertEqual(out.sent, [])

    def test_clawback_recorded(self):
        tx = {"Account": RLUSD, "TransactionType": "Clawback", "hash": "BB" * 32, "ledger_index": 5,
              "Amount": {"currency": "524C555344000000000000000000000000000000", "issuer": FROZEN, "value": "10"},
              "meta": {"TransactionResult": "tesSUCCESS"}}
        ev = XrplWatcher(CHAINS["xrpl"], client=FakeXrpl({})).event_from_tx(self.iss(RLUSD), tx)
        self.assertEqual((ev.kind, ev.post, ev.holder, ev.amount), ("destroy", False, FROZEN, Decimal(10)))

    def test_run_chain_chunks_and_dedupes(self):
        client = FakeXrpl({RLUSD: [self.rlusd]}, head=106382600)
        w, out = XrplWatcher(dict(CHAINS["xrpl"], max_ledger_range=50), client=client), FakeOut()
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "s.db"))
            store.set_cursor("xrpl", 106382400)
            self.assertEqual(run_chain(w, store, [out], 0), {"emitted": 1})
            self.assertEqual(store.get_cursor("xrpl"), 106382600)
            store.set_cursor("xrpl", 106382400)
            self.assertEqual(run_chain(w, store, [out], 0), {"dup": 1})
        self.assertEqual(len(out.sent), 1)

    def test_client_falls_back_when_server_ignores_tx_type(self):
        payment = {"tx_json": {"TransactionType": "Payment", "Account": "rX"}, "hash": "P1", "ledger_index": 10}
        freeze = {"tx_json": self.rlusd["meta"] and {k: v for k, v in self.rlusd.items() if k != "meta"},
                  "meta": self.rlusd["meta"], "hash": self.rlusd["hash"], "ledger_index": 10}
        calls = []

        class C(XrplClient):
            def _post(self, url, method, params):
                calls.append(dict(params))
                return {"transactions": [payment, freeze]}  # same answer whatever tx_type says

        txs = C(["u"]).issuer_txs(RLUSD, 1, 20)
        self.assertEqual([t["hash"] for t in txs], [self.rlusd["hash"]])
        self.assertEqual(len(calls), 2)  # one filtered try, then a single unfiltered pass
        self.assertNotIn("tx_type", calls[-1])


@unittest.skipIf(os.environ.get("FREEZEBOT_SKIP_LIVE"), "live network test")
class TestLiveStellarXrpl(unittest.TestCase):
    def test_live_xrpl_finds_real_rlusd_freeze(self):
        w = XrplWatcher(CHAINS["xrpl"])
        evs = [e for batch, _ in w.scan(106382490, 106382495) for e in batch]
        self.assertIn("037B0D8FDF1FFA645CFBE6CA7FE804E8D35F03E36267DA9771D26D7EF5997682", [e.tx for e in evs])

    def test_live_issuers_can_freeze(self):
        from freezebot.http import request_json
        for i in CHAINS["xrpl"]["issuers"]:
            r = request_json("https://xrplcluster.com", {"method": "account_info", "params": [
                {"account": i["issuer"], "ledger_index": "validated"}]})["result"]
            self.assertFalse(r["account_flags"]["noFreeze"], i["issuer"])
        for i in CHAINS["stellar"]["issuers"]:
            acct = request_json("https://horizon.stellar.org/accounts/" + i["issuer"])
            self.assertTrue(acct["flags"]["auth_revocable"], i["issuer"])

    def test_live_stellar_head_and_scan(self):
        w = StellarWatcher(CHAINS["stellar"])
        head = w.head()
        batches = list(w.scan(ledger_cursor((head >> 32) - 20), head))
        self.assertEqual(len(batches), 1)


if __name__ == "__main__":
    unittest.main()
