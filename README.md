# xmr-freeze-bot

A bot that watches stablecoin issuers freezing people's funds on-chain and announces each freeze
in the format requested by [Monero bounty #99](https://bounties.monero.social/posts/99/3-600m-freeze-interactions-bot-notifier):

```
219,408.69 $USDT should have gone through #Monero $XMR. Learn more at getmonero.org
https://etherscan.io/tx/0xa8dafd10f768b389e069306cdea66b84243253f8ce6b129f5d9170a9e1243c2f
```

Status: **v0.2**. Detection on 7 chains, amounts, dedupe and stdout output work. Social posting adapters are stubs.

## What it watches

All contract addresses and event signatures below were checked against real on-chain events.

| Chain | Token | Contract / mint | Freeze event (posted) | Burn event (recorded) |
|---|---|---|---|---|
| Ethereum | USDT | `0xdAC17F958D2ee523a2206206994597C13D831ec7` | `AddedBlackList` | `DestroyedBlackFunds` |
| Ethereum | USDC | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | `Blacklisted` | |
| Ethereum | PYUSD | `0x6c3ea9036406852006290770BEdFcAbA0e23A0e8` | `AddressFrozen` | `FrozenAddressWiped` |
| Arbitrum | USDT0 | `0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9` | `BlockPlaced` | `DestroyedBlockedFunds` |
| Arbitrum | USDC | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` | `Blacklisted` | |
| Base | USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | `Blacklisted` | |
| Polygon | USDT0 | `0xc2132D05D31c914a87C6611C10748AEb04B58e8F` | `BlockPlaced` | `DestroyedBlockedFunds` |
| Polygon | USDC | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` | `Blacklisted` | |
| Avalanche | USDT | `0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7` | `BlockPlaced` | `DestroyedBlockedFunds` |
| Avalanche | USDC | `0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E` | `Blacklisted` | |
| Tron | USDT | `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` | `AddedBlackList` | `DestroyedBlackFunds` |
| Solana | any SPL / Token-2022 mint | see below | `freezeAccount` (top-level and inner/CPI) | |

Notes:
- Ethereum mainnet USDT uses the legacy Tether contract (`AddedBlackList`, address in event data). The newer
  multichain Tether contracts (USDT0 on Arbitrum/Polygon, USDT on Avalanche) use `BlockPlaced` /
  `DestroyedBlockedFunds` with the address indexed.
- **BNB Chain is not included**: Binance-Peg USDT and USDC (`0x55d3…7955`, `0x8AC7…580d`) have no
  blacklist or freeze function (every `isBlackListed`/`isBlacklisted` style call reverts), so there is
  nothing to watch. Base has no Tether-issued USDT.

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
  USDG `2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH`.
- **Other mints** frozen by the same authorities (Paxos also freezes USDP and PAXG, for example) are
  stored in the DB. They're only posted if the frozen value is at least `usd_threshold` (default $1,000),
  priced with Jupiter's free, keyless price API.
- Add more authorities to watch with `extra_watch_addresses`.
- **Amount** is the frozen token account's balance, taken from the freeze transaction's own token
  balances. It falls back to the current balance.
- RPC: `SOLANA_RPC_URL` if set, then Helius if `HELIUS_API_KEY` (or `HELIUS_API_KEY_FILE`) is set, then
  the public `api.mainnet-beta.solana.com`.
- `--from-block` on Solana takes a slot.

## Run

Python 3.10+, no third-party dependencies.

```sh
python -m freezebot --once                     # one pass over all chains (dry-run, stdout)
python -m freezebot                            # poll forever (every poll_interval_seconds)
python -m freezebot --chain ethereum --from-block 26136686 --to-block 26151692   # backfill
python -m freezebot --chain tron --from-block 87311680 --to-block 87312845
python -m freezebot --chain solana --from-block 459806300 --to-block 459806400   # slots
```

On first start each chain begins `start_lookback_blocks` (EVM) or `start_lookback_hours` (Tron) back. After that the
last processed block (Tron: block timestamp) is stored per chain in SQLite, and every event is deduplicated by
`chain:tx:logIndex`. Backfills dedupe too but don't move the live cursor.

Docker:

```sh
docker build -t xmr-freeze-bot .
docker run -v freezebot-data:/data xmr-freeze-bot
```

## Configuration and secrets

Everything is in `config.json`. Optional environment variables (see `.env.example`):

- `ETH_RPC_URL`: preferred Ethereum RPC, tried before the public ones
- `TRONGRID_API_KEY`: optional; TronGrid works without one at low rates
- `ARBITRUM_RPC_URL`, `BASE_RPC_URL`, `POLYGON_RPC_URL`, `AVALANCHE_RPC_URL`: preferred RPC per chain
- `SOLANA_RPC_URL`, `HELIUS_API_KEY` or `HELIUS_API_KEY_FILE` (path to a file holding the key): optional Solana RPC
- Output adapter credentials: `NOSTR_NSEC`, `NOSTR_RELAYS`, `MASTODON_BASE_URL`, `MASTODON_ACCESS_TOKEN`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

Credentials are only ever read from the environment. Nothing secret belongs in the repo or the image.

## Outputs

Only `stdout` is enabled by default. Nostr, Mastodon and Telegram adapters exist as stubs that load their
credentials from the environment. Without `--live` they only log what they would post; with `--live` they currently
raise "not implemented".

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

The Solana test runs without a key on the public RPC.

## Roadmap

1. Implement the Nostr (NIP-01 kind-1), Mastodon and Telegram channel posters.
2. Solana: optional whole-network freeze discovery (block subscription or a Helius webhook) for non-allow-listed tokens.
3. More issuers and chains (FDUSD, EURC, Optimism, ...) as config entries.
4. Daily digest, public JSON/CSV archive and a `/status` page.

## License

MIT, see [LICENSE](LICENSE).
