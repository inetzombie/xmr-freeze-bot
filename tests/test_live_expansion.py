"""Live checks for the chains/tokens added in v0.4 (expand-coverage). Skip with FREEZEBOT_SKIP_LIVE=1.

Each EVM case scans exactly one block that contains a real, historical freeze for that token, and checks the
watcher decodes it (token symbol, event name, tx hash). Where an issuer has never frozen anything on a chain,
the test instead checks that the freeze event's topic0 is compiled into the token's current implementation
bytecode, so the config would decode it the day it happens.
"""
import json
import os
import unittest

from freezebot.evm import EvmClient, EvmWatcher
from freezebot.tron import TronWatcher

ROOT = os.path.dirname(os.path.dirname(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
CHAINS = {c["name"]: c for c in CFG["chains"]}
SKIP = os.environ.get("FREEZEBOT_SKIP_LIVE") == "1"

# (chain, block, symbol, event, tx)
REAL_FREEZES = [
    ("ethereum", 26127169, "PYUSD", "FreezeAddress", "0x2a2174e4f0f142905b5c4d96c1e68fafd9c0186fa910bf8444a49723281cc805"),
    ("ethereum", 15037656, "EURC", "Blacklisted", "0xe703501f4521d8594ccd301a386ab696dafe0686e4b8eb8451d6d206b8e2cf51"),
    ("ethereum", 25949234, "USDP", "FreezeAddress", "0x927ccd6e11d712d649d91b692739d8d32590e60238647e33c25190d11b69442b"),
    ("ethereum", 25949233, "PAXG", "FreezeAddress", "0x0b85d67b2539e377d510f680730db5584d68f4a3cb2994e4eccb317c5fb9de76"),
    ("ethereum", 25949233, "USDG", "FreezeAddress", "0x3cf2f98dcce3e2164cebaf959729947eb88d63129ea4dcf19833a86877f132c6"),
    ("ethereum", 9815715, "BUSD", "AddressFrozen", "0x0d5f399a82bad13a594a91f0a90f1ec261f44b948e70cba4ccec9bc4d8d60f13"),
    ("ethereum", 22755232, "XAUT", "BlockPlaced", "0x02951d341c9f471de4ce5304495deafa9baec00d4529b84511b6b85668574c0e"),
    ("ethereum", 17640051, "TUSD", "Blacklisted", "0x4c51ec227dbba2152ff15ef073008f5712e3ce60dd4a3ab4d4e53b308375ca99"),
    ("ethereum", 26035502, "RLUSD", "AccountPaused", "0xb7ee0a48b2822ab138bba239dbe5124670d4557bbce7f283415070cb79db39f5"),
    ("ethereum", 23012605, "AUSD", "AccountFrozen", "0xc364614d213968a2c443385bdec1ec53a46dd9c1274a3c2f8d4c91ab34dbb48e"),
    ("arbitrum", 511968128, "PYUSD", "FreezeAddress", "0xfc68ed31d2e5291d343c9e2875b620321edf5a586a66f1d7f6e3c074163a6c8a"),
    ("arbitrum", 503477934, "USDG", "FreezeAddress", "0xdc809b0e898c014c75bcb6c1985a67adbefc74f3e69b8a5817fc5aa9b97f761a"),
    ("polygon", 95006854, "PYUSD", "FreezeAddress", "0x2abf1b6e8b22a6d3eae320df1c4b7015db69ed3f77cbdb9bdeb341bd018d09e8"),
    ("avalanche", 31066492, "EURC", "Blacklisted", "0xb932e6e763c8932f7498fcea122d8112530d3d3c4d10b6af641f61887a6d34c2"),
    ("base", 51995071, "EURC", "Blacklisted", "0x4bde9516992d0d77d980e0e234178ed87e06ea6184ad7e6f8e572e45e778bd5c"),
    ("optimism", 157590350, "USDC", "Blacklisted", "0x9453015f12cb26d16ecd9407dc518315e55e3edf28570522822c77d51ddf4f18"),
    ("unichain", 174021, "USDC", "Blacklisted", "0x9a455b916484775fa7790a00a6512691b9b33b269cf1466ffd92e46df7d7e7fb"),
    ("ink", 22300765, "USDC", "Blacklisted", "0xb63cf28d4863ba8f36a686e355a8e3acbf8e28f43005350610f7d550c07f6524"),
    ("ink", 18682493, "USDG", "FreezeAddress", "0xa2c65c588e46033c882c7499f54d52fb78f437d7565dec058173d3f048615e1d"),
    ("zksync", 72279013, "USDC", "Blacklisted", "0x26cfdec04bb4787c8cc9b019ad6534d7cede0b2cfc556fe3573982063125cfdc"),
    ("linea", 32192129, "USDC", "Blacklisted", "0x9ccb47bafa53539daec0dd6436ac2f3e65c93179ad86f5919e75a6bec2803266"),
    ("celo", 78878725, "USDC", "Blacklisted", "0x51c16133391d07d05a7fca4945ea62f4ae7661c5e6afe26d63ff966f39b7deae"),
    ("sonic", 80139592, "USDC", "Blacklisted", "0x29232ca82846b7847b0b5718a61aaf920ccd141223e018f21b172ca901f54a7f"),
    ("worldchain", 14604254, "USDC", "Blacklisted", "0x17619a9e5892894f85de10fcf3e6480d3a7e9a14a1379971430cc7e57cd7718f"),
    ("worldchain", 35721917, "EURC", "Blacklisted", "0xc3cc9d24431f8505c6e7626e9a34466b99b115960cd39c708816b8b5e12cbfb5"),
    ("mantle", 100427478, "USDG", "FreezeAddress", "0x70fcf0a63762201dd81e13338cfd85c27b1ffe16d75cf9e85fdcefb17bbf430b"),
    ("hyperevm", 47298180, "USDC", "Blacklisted", "0x41a462abc48f06c3be32c858951654bd007d9058a78c97ab3b023af17302b5a6"),
    ("plasma", 33838560, "USDC", "Blacklisted", "0xa6cc0e7d5a043101eff108ce0d55e9fd14b4cdcdb921fb7a9a83f68c23559041"),
    ("plasma", 33838587, "EURC", "Blacklisted", "0x1544cb06ef3bb0006efcfcdd4ed5fcca5e37e5e855d28287df211fae065daa5d"),
    ("monad", 22909616, "USDC", "Blacklisted", "0xb0bb672d909372afa3ebdf6a7d2afc042558df55040b83422edca0ec90ec4b5f"),
]

# (chain, symbol, topic0) for tokens with no freeze we can fetch from a free RPC (as of 2026-10-23): the issuer
# has not frozen anything there yet, or (Sei USDC) the public RPCs are pruned past the freezes.
NO_FREEZE_YET = [
    ("optimism", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("unichain", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("ink", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("mantle", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("berachain", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("hyperevm", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("plasma", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("monad", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("sei", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("ethereum", "FDUSD", "0x51d18786e9cb144f87d46e7b796309ea84c7c687d91e09c97f051eacf59bc528"),
    ("ethereum", "USD1", "0x51d18786e9cb144f87d46e7b796309ea84c7c687d91e09c97f051eacf59bc528"),
    ("bsc", "FDUSD", "0x51d18786e9cb144f87d46e7b796309ea84c7c687d91e09c97f051eacf59bc528"),
    ("bsc", "USD1", "0x51d18786e9cb144f87d46e7b796309ea84c7c687d91e09c97f051eacf59bc528"),
    # Tether froze/burned on Celo once, before Celo's L2 migration (block 24,776,727, tx 0x5776383a…); today's
    # Celo L2 RPCs no longer serve those pre-migration logs, so this falls back to the bytecode check.
    ("celo", "USDT", "0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7"),
    ("sei", "USDC", "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855"),
]
EIP1967 = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
FIATTOKEN_PROXY = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"


def _token(chain, symbol):
    return next(t for t in CHAINS[chain]["tokens"] if t["symbol"] == symbol)


def implementation_code(client, address):
    for slot in (EIP1967, FIATTOKEN_PROXY):
        impl = "0x" + client.call("eth_getStorageAt", [address, slot, "latest"])[-40:]
        if int(impl, 16):
            return client.call("eth_getCode", [impl, "latest"])
    return client.call("eth_getCode", [address, "latest"])


@unittest.skipIf(SKIP, "live tests disabled")
class TestLiveExpansion(unittest.TestCase):
    def test_real_freezes_decode(self):
        for chain, block, symbol, event, tx in REAL_FREEZES:
            with self.subTest(chain=chain, symbol=symbol):
                w = EvmWatcher(CHAINS[chain])
                evs = [e for b, _ in w.scan(block, block) for e in b if e.tx == tx and e.symbol == symbol]
                self.assertTrue(evs, f"{chain} {symbol}: no {event} decoded in block {block}")
                self.assertEqual(evs[0].event, event)
                self.assertEqual(evs[0].kind, "freeze")
                self.assertTrue(evs[0].holder.startswith("0x") and len(evs[0].holder) == 42)

    def test_freeze_event_compiled_into_contracts_without_freezes_yet(self):
        for chain, symbol, topic in NO_FREEZE_YET:
            with self.subTest(chain=chain, symbol=symbol):
                tok = _token(chain, symbol)
                self.assertIn(topic, [e["topic0"] for e in tok["events"]])
                code = implementation_code(EvmClient(CHAINS[chain]["rpc_urls"]), tok["address"])
                self.assertIn(topic[2:], code.lower())

    def test_tron_usdc_blacklisted(self):
        w = TronWatcher(CHAINS["tron"])
        ts = w.block_to_cursor(86705854)
        hit = [e for b, _ in w.scan(ts, ts + 2999) for e in b
               if e.tx.startswith("add804566ff3d0931c4a36172837c8878f4786a737e01b9a4704a")]
        self.assertEqual(len(hit), 1)
        self.assertEqual((hit[0].symbol, hit[0].event), ("USDC", "Blacklisted"))

    def test_tron_tusd_blacklisted(self):
        w = TronWatcher(CHAINS["tron"])
        ts = w.block_to_cursor(29146296)
        hit = [e for b, _ in w.scan(ts, ts + 2999) for e in b
               if e.tx == "81b66a6b335e27f14065fc8b827144ebd6c061d6dc5c47c7e6d006112d593d91"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].symbol, "TUSD")

    def test_solana_eurc_and_ausd_freezes(self):
        from freezebot.solana import SolanaWatcher
        w = SolanaWatcher(CHAINS["solana"])
        for sig, symbol in [
            ("44CT7cNMNCL3CT2v3NPDVNPRcrCHYdSzVZrvjsJAMz6Nfpyffsw4rUB48Xxeh1NyNhwEHesCyd38oZGBgPtzYjfw", "EURC"),
            ("3XJHTCNSJ3obUaS1vfqTNiF7Nv1XGBa68pm7e6nCSCbNwrtzGo4vnZqHGQeXCBPzPCuc7DJKD3SdWdrrLC4Eer2z", "AUSD"),
        ]:
            with self.subTest(symbol=symbol):
                evs = w.events_from_tx(sig, w.client.transaction(sig))
                self.assertEqual([e.symbol for e in evs], [symbol])


if __name__ == "__main__":
    unittest.main()
