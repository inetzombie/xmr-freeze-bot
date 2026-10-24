# xmr-freeze-bot

A bot that watches stablecoin issuers freezing people's funds on-chain and announces each freeze
in the format requested by [Monero bounty #99](https://bounties.monero.social/posts/99/3-600m-freeze-interactions-bot-notifier):

```
219,408.69 $USDT should have gone through #Monero $XMR. Learn more at getmonero.org
https://etherscan.io/tx/0xa8dafd10f768b389e069306cdea66b84243253f8ce6b129f5d9170a9e1243c2f
```

Status: **v0.4**. Detection on **24 chains** (20 EVM + Tron + Solana + Stellar + XRP Ledger) across 16 issuers' tokens, amounts, dedupe, and stdout,
**Telegram**, **Nostr** and **Mastodon** outputs (Nostr/Mastodon need credentials to go live).

## What it watches

All contract addresses and event signatures below were checked against real on-chain events.

Every address below comes from the issuer's own docs (Circle, Tether/USDT0, Paxos, …). Every freeze event was
checked against a real on-chain freeze, except where marked † (no freeze has happened there yet; the event's topic is
verified to be compiled into the live implementation bytecode instead). `tests/test_live*.py` re-check all of it.

Event families (topic0 in `config.json`):
- **Circle** (USDC, EURC): `Blacklisted(address)`
- **Tether** multichain / USDT0 / XAUT: `BlockPlaced(address)`, burn `DestroyedBlockedFunds` (recorded)
- **Tether** legacy Ethereum/Tron USDT: `AddedBlackList`, burn `DestroyedBlackFunds` (recorded)
- **Paxos** (PYUSD, USDP, PAXG, USDG, BUSD): `FreezeAddress(address)` (current contracts) and `AddressFrozen(address)`
  (older), wipe `FrozenAddressWiped` (recorded). Ethereum PYUSD switched from the old to the new event at block
  21,166,732, so both are watched.
- **TrueUSD**: `Blacklisted(address,bool)`, only `true` is posted (the same event with `false` is an un-blacklist)
- **Ripple RLUSD**: `AccountPaused(address)` · **Agora AUSD**: `AccountFrozen(address)`
- **First Digital FDUSD**, **World Liberty USD1**: `Freeze(address caller, address account)`

| Chain | Tokens watched |
|---|---|
| Ethereum | USDT `0xdAC1…1ec7`, USDC `0xA0b8…eB48`, EURC `0x1aBa…C33c`, PYUSD `0x6c3e…A0e8`, USDP `0x8E87…89E1`, PAXG `0x4580…af78`, USDG `0xe343…491D`, BUSD `0x4Fab…7C53`, XAUT `0x6874…2F38`, TUSD `0x0000…b376`, RLUSD `0x8292…17eD`, AUSD `0x0000…012a`, FDUSD† `0xc5f0…6409`, USD1† `0x8d0D…0B0d` |
| Arbitrum | USDT0 `0xFd08…Cbb9`, USDC `0xaf88…5831`, PYUSD `0x4685…6984`, USDG `0x004B…9bbC` |
| Base | USDC `0x8335…2913`, EURC `0x60a3…db42` |
| Polygon | USDT0 `0xc213…8e8F`, USDC `0x3c49…3359`, PYUSD `0x99aF…0750` |
| Avalanche | USDT `0x9702…A8c7`, USDC `0xB97E…8a6E`, EURC `0xC891…2ACD` |
| Optimism | USDC `0x0b2C…Ff85`, USDT0† `0x01bF…1071` |
| Unichain | USDC `0x078D…7AD6`, USDT0† `0x9151…EcC5` |
| Ink | USDC `0x2D27…EAEd`, USDG `0xe343…491D`, USDT0† `0x0200…70c1` |
| ZKsync Era | USDC `0x1d17…38D4` |
| Linea | USDC `0x1762…E1ff` |
| Celo | USDC `0xcebA…118C`, USDT `0x4806…3D5e` (one freeze, pre-L2-migration) |
| Sonic | USDC `0x2921…8894` |
| World Chain | USDC `0x79A0…24D1`, EURC `0x1C60…380B` |
| Mantle | USDG `0x063C…C086`, USDT0† `0x779D…3736` |
| Berachain | USDT0† `0x779D…3736` |
| HyperEVM | USDC `0xb883…630f`, USDT0† `0xB8CE…5ebb` |
| Plasma | USDC `0x2d66…F67F`, EURC `0x3EE1…FD2C`, USDT0† `0xB8CE…5ebb` |
| Monad | USDC `0x7547…b603`, USDT0† `0xe7cd…C82D` |
| Sei | USDC (bytecode-verified; public RPCs are pruned past its freezes) `0xe15f…2392`, USDT0† `0x9151…EcC5` |
| BNB Chain | FDUSD† `0xc5f0…6409`, USD1† `0x8d0D…0B0d` |
| Tron | USDT `TR7N…Lj6t` (`AddedBlackList`), USDC `TEkx…rdz8` (`Blacklisted`), TUSD `TUpM…C1F4` (`Blacklisted`, true only) |
| Solana | any SPL / Token-2022 `freezeAccount` by the allow-listed mints' freeze authorities (see below) |
| Stellar | USDC `GA5Z…K4KZVN`, EURC `GDHU…ITNPP2` (Circle), PYUSD `GDQE…ZTU2V5` (Paxos): trustline deauthorization (see below) |
| XRP Ledger | RLUSD `rMxC…8m5De` (Ripple), USDC `rGm7…XuWhE` (Circle): trust-line freeze / deep freeze / global freeze (see below) |

Full addresses are in `config.json`.

Notes:
- Ethereum mainnet USDT uses the legacy Tether contract (`AddedBlackList`, address in event data). The newer
  multichain Tether contracts (USDT0 on Arbitrum/Polygon, USDT on Avalanche) use `BlockPlaced` /
  `DestroyedBlockedFunds` with the address indexed.
- **BNB Chain** only has FDUSD and USD1: Binance-Peg USDT and USDC (`0x55d3…7955`, `0x8AC7…580d`) have no
  blacklist or freeze function, so there is nothing to watch. Base has no Tether-issued USDT.
- Circle pre-blacklists a shared list of addresses when it deploys USDC/EURC on a new chain. Those holders
  usually have a zero balance, so they fall under `min_amount` and are recorded but not posted.

EVM chains are config entries in `config.json`. Adding another one means adding a block with its RPC
URLs, explorer link and token contracts; no code changes. Public RPCs cap `eth_getLogs` ranges differently,
so the watcher halves its block range automatically when an RPC rejects a request.

### Solana

Scanning every Solana block is too heavy for free RPC tiers. Instead, the bot reads each allow-listed
mint's current `freezeAuthority` at startup and watches those addresses with `getSignaturesForAddress`.
It then parses every `freezeAccount` instruction (SPL Token `Tokenkeg…` and Token-2022 `Tokenz…`,
including inner instructions issued through multisig programs) in those transactions.

- **Allow-list** (posted): USDC `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`,
  USDT `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB`, PYUSD `2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo`,
  USDG `2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH`, EURC `HzwqbKZw8HxMN6bF2yFZNrht3c2iXXzpKcFu7uBEDKtr`,
  USDP `HVbpJAQGNpkgBaYBZQBR1t7yFdvaYVp2vCQQfKKEN4tM`, PAXG `5GgRAEmv8ZxF2PR5hY72Qs5x1bnQ6UK2RbTPoqJ3wSwW`,
  USD1 `USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB`, FDUSD `9zNQRsGLjNKwCUU5Gq5LR8beUCPzQMVMqKAi3SSZh54u`,
  AUSD `AUSD1jCcCyPLybk1YnvPWsHQSrZ46dxwoMniN4N2UEB9`.
- **Other mints** frozen by the same authorities (Paxos also freezes USDP and PAXG, for example) are
  stored in the DB. They're only posted if the frozen value is at least `usd_threshold` (default $1,000),
  priced with Jupiter's free, keyless price API.
- Add more authorities to watch with `extra_watch_addresses`.
- **Amount** is the frozen token account's balance, taken from the freeze transaction's own token
  balances. It falls back to the current balance.
- RPC: `SOLANA_RPC_URL` if set, then Helius if `HELIUS_API_KEY` (or `HELIUS_API_KEY_FILE`) is set, then
  the public `api.mainnet-beta.solana.com`.
- `--from-block` on Solana takes a slot.

### Stellar

Classic Stellar assets are frozen by their issuer clearing the `authorized` flag on a holder's trustline
(`set_trust_line_flags`, or the older `allow_trust` with `authorize=false`; downgrading to
"authorized to maintain liabilities" counts too, since the holder can then neither send nor receive). Only the issuer
can do that, so the bot pages each issuer's `/accounts/{issuer}/operations` on Horizon (`horizon.stellar.org`, then
`horizon.stellar.lobstr.co`). Issuer `clawback` operations are recorded as `destroy` and not posted.

- Issuers (from Circle's [USDC](https://developers.circle.com/stablecoins/usdc-contract-addresses) /
  [EURC](https://developers.circle.com/stablecoins/eurc-contract-addresses) address pages and Paxos' API docs and
  PYUSD-on-Stellar whitepaper; all three accounts have `auth_revocable` set on-chain, home domains `circle.com` /
  `token-metadata.paxos.com`):
  USDC `GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN`,
  EURC `GDHU6WRG4IEQXM5NZ4BMPKOXHW76MZM4Y2IEMFDVXBSDP6SJY4ITNPP2`,
  PYUSD `GDQE7IXJ4HUHV6RQHIUPRJSEZE4DRS5WY577O2FY6YQ5LVWZ7JZTU2V5`.
- **Amount**: the holder's current trustline balance (a deauthorized trustline can't send, so it equals the frozen
  amount unless clawed back).
- The cursor is the Horizon operation paging token; `--from-block` takes a ledger sequence. First start looks back
  `start_lookback_ledgers` (720, about an hour). SDF's Horizon keeps about a year of history: no issuer freeze by
  these three accounts exists in that window, so the unit tests use recorded real issuer operations plus
  freeze/clawback records in Horizon's format.
- Caveat: PYUSD also has a Soroban admin contract. A freeze done through the Stellar Asset Contract's
  `set_authorized` from that contract would not be an operation from the issuer account and is not detected yet.

### XRP Ledger

Detected, for each configured issuer: `TrustSet` sent by the issuer with `tfSetFreeze` or `tfSetDeepFreeze` (posted),
`AccountSet` with `asfGlobalFreeze` (recorded; one freeze of every holder, no single amount, so it falls under the
unknown-amount rule), and issuer `Clawback` (recorded as `destroy`).

- Issuers (from [Ripple's RLUSD docs](https://docs.ripple.com/products/stablecoin/overview/token-addresses) and
  [Circle's USDC address page](https://developers.circle.com/stablecoins/usdc-contract-addresses); both have
  `noFreeze` unset on-chain, domains `ripple.com` / `circle.com`):
  RLUSD `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De` (currency `524C5553…`), USDC `rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE`
  (currency `55534443…`).
- `account_tx` on a stablecoin issuer returns every payment rippling through it (hundreds per minute), so the bot asks
  Clio servers (`s1`/`s2.ripple.com`) for one `tx_type` at a time and falls back to `xrplcluster.com` / `xrpl.ws`,
  where it does one unfiltered pass if the server ignores `tx_type`.
- **Amount**: the holder's trust-line balance from the freeze transaction's own metadata, else the current
  `account_lines` balance.
- Cursor: validated ledger index; first start looks back `start_lookback_ledgers` (300, about 20 minutes).
- Real freezes in `tests/fixtures`: RLUSD deep freeze of `rnXyVQzgxZe7TR1EPzTkGj2jxH4LMJYh66` (ledger 106,382,493,
  2026-08-18) and Circle USDC freeze + deep freeze of the same address (ledger 96,102,710, 2025-05-13). Both lines were
  empty, so they're recorded but stay under `min_amount`.

## Run

Python 3.10+, no third-party dependencies.

```sh
python -m freezebot --once                     # one pass over all chains (dry-run, stdout)
python -m freezebot                            # poll forever (every poll_interval_seconds)
python -m freezebot --chain ethereum --from-block 26136686 --to-block 26151692   # backfill
python -m freezebot --chain tron --from-block 87311680 --to-block 87312845
python -m freezebot --chain solana --from-block 459806300 --to-block 459806400   # slots
python -m freezebot --chain xrpl --from-block 106382490 --to-block 106382495      # ledgers
```

On first start each chain begins `start_lookback_blocks` (EVM) or `start_lookback_hours` (Tron) back. After that the
last processed block (Tron: block timestamp) is stored per chain in SQLite, and every event is deduplicated by
`chain:tx:logIndex`. Backfills dedupe too but don't move the live cursor.

The bot logs one `heartbeat:` line per hour (`heartbeat_minutes` in `config.json`, `0` to disable) with every
chain's cursor and any poll failures, so a quiet log still proves each chain is advancing.

Docker:

```sh
docker build -t xmr-freeze-bot .
docker run -v freezebot-data:/data xmr-freeze-bot
```

## Configuration and secrets

Everything is in `config.json`. Optional environment variables (see `.env.example`):

- `ETH_RPC_URL`: preferred Ethereum RPC, tried before the public ones
- `TRONGRID_API_KEY`: optional; TronGrid works without one at low rates
- `<CHAIN>_RPC_URL` (e.g. `ARBITRUM_RPC_URL`, `OPTIMISM_RPC_URL`, `BSC_RPC_URL`; see `rpc_env` in `config.json`):
  preferred RPC per chain, tried before the free public ones
- `SOLANA_RPC_URL`, `HELIUS_API_KEY` or `HELIUS_API_KEY_FILE` (path to a file holding the key): optional Solana RPC
- Output adapter credentials: `NOSTR_NSEC` (nsec1… or hex; or `NOSTR_NSEC_FILE`), `NOSTR_RELAYS` (comma-separated wss:// URLs), `MASTODON_BASE_URL`, `MASTODON_ACCESS_TOKEN`,
  `TELEGRAM_BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN_FILE`, a path to a file holding the token), `TELEGRAM_CHAT_ID`

Credentials are only ever read from the environment. Nothing secret belongs in the repo or the image.

## Outputs

Only `stdout` is enabled by default. **Telegram**, **Nostr** and **Mastodon** are implemented; enable each in
`config.json` and give it credentials through the environment. Without `--live` every non-stdout adapter only logs
what it would post (dry-run).

- **Nostr**: a NIP-01 kind-1 note per freeze, tagged `#monero`/`#xmr`, signed with BIP-340 Schnorr in pure Python
  (no dependencies) and sent to every relay in `NOSTR_RELAYS` over a small built-in websocket client. A post counts
  as done when at least one relay answers `OK true`.
- **Mastodon**: `POST /api/v1/statuses` with an `Idempotency-Key` derived from the event id, so a retry never
  double-posts. Needs an access token with `write:statuses` scope.
If `--live` is set but an enabled adapter is missing its credentials, the bot exits immediately with a clear error
(it never posts a partial run).

### Running the Telegram feed

The Telegram adapter posts each composed message to a channel via the Bot API `sendMessage`. The message text is
sent exactly as shown above (the `… should have gone through #Monero $XMR …` line plus the explorer link), with the
link preview disabled to keep the channel clean. It self-limits to about one message per second and honours a `429`
`retry_after` from Telegram.

1. Create a bot with [@BotFather](https://t.me/BotFather) and get its token. Add the bot to your channel as an admin.
2. Provide the token **without committing it** — either set `TELEGRAM_BOT_TOKEN`, or put the token in a file and point
   `TELEGRAM_BOT_TOKEN_FILE` at it:

   ```sh
   export TELEGRAM_BOT_TOKEN_FILE=/path/to/secret/telegram_token   # file contains only the token
   export TELEGRAM_CHAT_ID=@YourChannel                            # or a numeric -100... id
   ```
3. Enable the adapter in `config.json` (`"telegram": { "enabled": true }`). Optional per-adapter settings:
   `"min_interval"` (seconds between messages, default `1.05`) and `"disable_web_page_preview"` (default `true`).
4. Dry-run first (no `--live`) to see exactly what would be posted, then go live:

   ```sh
   python -m freezebot --once --chain ethereum                     # dry-run: logs "[dry-run] telegram would post: …"
   python -m freezebot --once --chain ethereum --live              # actually posts to the channel
   ```

The token is only ever read from the environment or the `*_FILE` path at runtime, and is never written to logs,
config, or the repo.

## Tests

```sh
python -m unittest -v                        # unit + live checks against real historical freezes
FREEZEBOT_SKIP_LIVE=1 python -m unittest -v  # offline only
```

The live tests check real historical freezes:
- Tether on Ethereum (block 26,136,686, 219,408.685448 USDT)
- a Circle USDC blacklist and a PYUSD `AddressFrozen` on Ethereum
- a Tron USDT freeze
- USDT0 `BlockPlaced` on Arbitrum (23,000.06 USDT)
- USDT `BlockPlaced` on Avalanche (43,587 USDT)
- a Circle USDC `freezeAccount` on Solana (53,617.08 USDC)

- `tests/test_stellar_xrpl.py`: offline fixture tests for both watchers, plus live checks that the XRPL watcher finds
  the real RLUSD freeze and that every Stellar/XRPL issuer can still freeze
- `tests/test_live_expansion.py`: one real freeze per added chain/token (30 cases over 14 chains plus Tron USDC/TUSD
  and Solana EURC/AUSD), and the bytecode check for the † tokens that have never frozen anything

The Solana test runs without a key on the public RPC. The live suite takes about 10 minutes on free RPCs.

## Roadmap

1. Solana: optional whole-network freeze discovery (block subscription or a Helius webhook) for non-allow-listed tokens.
2. Non-EVM issuer freezes: Sui/Aptos; Stellar Soroban (SAC `set_authorized`) freezes
   (USDC deny lists), TON (USDT jetton). Each needs its own watcher.
3. X Layer, Stable, Robinhood Chain and other new USDT0/Paxos/Circle chains once they have usable free RPCs.
4. Daily digest, public JSON/CSV archive and a `/status` page.

## License

MIT, see [LICENSE](LICENSE).
