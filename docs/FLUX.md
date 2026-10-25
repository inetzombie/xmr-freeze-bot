# Running on Flux (runonflux.com)

Flux runs every app as several identical containers (3 by default) on independent nodes with **no shared
disk**. Without coordination, each instance would post every freeze. Flux mode (`FREEZEBOT_FLUX=1` or
`--flux`) makes exactly one of them post. It is **off by default**; with it off the bot behaves exactly as before.

## How the single-poster guard works

No central server is needed. The coordination medium is the Nostr relays the bot already uses.

1. Every instance polls every chain independently and keeps its own SQLite DB on its Flux volume.
2. Each instance has a deterministic **rank**:
   - `FREEZEBOT_INSTANCE_RANK` if set (0 = leader). Flux runs the same env on every instance, so in practice
     this is only useful outside Flux.
   - Otherwise `sha256(FLUX_INSTANCE | FREEZEBOT_INSTANCE_ID | hostname) mod FREEZEBOT_FLUX_INSTANCES` (default 3).
3. **Rank 0 posts immediately**, then publishes a *claim marker* to every relay: a NIP-78 (kind 30078)
   addressable event, signed by the bot's Nostr key, `d` tag = `freezebot:<sha256(uid)[:32]>`. Normal Nostr
   clients don't show kind 30078 in feeds.
4. **Rank k waits `k × FREEZEBOT_FLUX_STEP_SECONDS`** (default 90 s), then queries the relays for that marker
   (`authors=[bot pubkey]`, `#d=[tag]`). Found: record `peer-posted`, send nothing. Not found: post and publish
   its own marker. So a dead leader is covered by rank 1 after ~90 s, and rank 2 covers rank 1 after ~180 s.
5. Belt and braces: in flux mode the public Nostr note uses the chain timestamp as `created_at`, so every
   instance builds the **same event id** and relays store it once. Mastodon posts already carry an
   `Idempotency-Key` derived from the uid (the server dedupes for about an hour).
6. **Fresh instances don't backfill.** When a chain has no cursor yet, flux mode sets it to the chain head
   and posts nothing from history. Normal mode keeps its lookback window.

### Why this design

- **Telegram can't be read back.** Bots can't read their own channel history (`getUpdates` only gets
  updates sent to the bot, and only one consumer can poll it). Telegram can't be the source of truth, and
  `sendMessage` has no idempotency key.
- **Nostr is a public, replicated, signed log** the bot already writes to. Only the holder of `NOSTR_NSEC`
  can forge a marker, and several relays give redundancy.
- Alternatives I rejected: Flux has no built-in leader election or shared storage for this. A lock on
  an external DB or Redis needs a server we run. Picking the leader from the Flux API's instance-IP list is
  possible, but it adds an API dependency and has the same split-brain problems.

### Failure modes (be honest)

| Situation | Result |
|---|---|
| Leader healthy | One post, about one poll interval after the chain event. |
| Leader dead or stuck | A standby posts about 90 s (rank 1) or 180 s (rank 2) late. |
| Two instances hash to the same rank (about 1/3 chance per pair with 3 instances) | Both may post. If both are rank 0, a duplicate is likely on Telegram (Nostr and Mastodon dedupe). If all three share a nonzero rank, all post after the delay. Set `FREEZEBOT_FLUX_INSTANCES` larger (for example 10) to spread ranks; delays grow accordingly. |
| No instance hashes to rank 0 | Posts are delayed by the lowest rank × step. Nothing is lost. |
| Leader posts but its marker publish fails, or relays lag past the step | The standby posts again, which is a **duplicate** on Telegram. |
| Relays unreachable from a standby | Default `FREEZEBOT_FLUX_ON_CHECK_ERROR=post` (fail-open: you get a duplicate, never a miss). Set it to `skip` to fail closed, which risks a miss if the leader is also down. |
| Leader sees the event much later than a standby (slow RPC) | The standby may post first, then the leader posts too. The leader never checks; this trades duplicates for latency. Raise the step if this happens. |
| Nostr relays drop old kind-30078 events | Only matters within the step window, so it is harmless. |
| Clock skew | Doesn't matter. Delays are local and markers are matched by tag, not time. |

The guard is *best effort*: it is tuned so you never miss a post, and duplicates are rare but possible.
If even a rare duplicate on Telegram is unacceptable, run a single instance instead of using Flux.

## Environment variables

| Var | Purpose |
|---|---|
| `FREEZEBOT_FLUX=1` | enable flux mode |
| `FREEZEBOT_LIVE=1` | actually post (same as `--live`); without it, dry-run and no markers |
| `FREEZEBOT_OUTPUTS` | e.g. `telegram,nostr`; overrides `enabled` flags in config.json |
| `FREEZEBOT_DATA_DIR` | data dir (image default `/data`); DB is `freezebot.sqlite3` inside it. `FREEZEBOT_DB` overrides the full path |
| `FREEZEBOT_FLUX_STEP_SECONDS` | standby delay per rank (default 90). Must exceed the slowest chain poll interval plus post time |
| `FREEZEBOT_FLUX_INSTANCES` | rank modulus (default 3) |
| `FREEZEBOT_FLUX_RELAYS` | relays for markers (default `NOSTR_RELAYS`) |
| `FREEZEBOT_FLUX_ON_CHECK_ERROR` | `post` (default) or `skip` |
| `FREEZEBOT_HEALTH_FILE` | heartbeat file (flux default `$DATA_DIR/health`) |
| `NOSTR_NSEC`, `NOSTR_RELAYS` | **required in flux mode**, even if the Nostr output is disabled (markers need them) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MASTODON_BASE_URL`, `MASTODON_ACCESS_TOKEN` | as before |

## Deploy step by step

1. **Build and push the image** (Flux pulls from a public registry such as Docker Hub; amd64):
   `docker buildx build --platform linux/amd64 -t <you>/xmr-freeze-bot:flux --push .`
   The image contains no secrets. config.json is baked in, so use `FREEZEBOT_OUTPUTS` to choose outputs.
2. **Flux account:** sign in at home.runonflux.io (Zelcore or SSP wallet login, or the email-based options
   they offer).
3. **Register the app** (Applications → Register New App), Docker app, one component:
   - Image: `<you>/xmr-freeze-bot:flux`, run command empty (entrypoint is built in).
   - **Ports: none needed.** The bot only makes outbound connections. If the form requires a port, give it
     an unused high port; nothing listens on it.
   - **Container data / persistent volume:** `/data` (so each instance keeps its cursors across restarts).
   - **Instances:** 3 (the Flux default/minimum). `FREEZEBOT_FLUX_INSTANCES=3`.
   - CPU 0.5, RAM 512 MB, SSD 1–2 GB is plenty (estimate).
   - **Secrets:** use the **Enterprise / private** environment option so `NOSTR_NSEC`, `TELEGRAM_BOT_TOKEN`
     and so on are encrypted and visible only to the nodes. Plain env on a non-Enterprise app is **public on
     the Flux network**, so never put tokens there.
   - Env: `FREEZEBOT_FLUX=1`, `FREEZEBOT_LIVE=1`, `FREEZEBOT_OUTPUTS=telegram,nostr`, `NOSTR_RELAYS=wss://...,wss://...`
     (use at least 3 relays), and the secrets above.
4. **Pay** the registration and subscription fee (FLUX token, or the fiat/card checkout they offer).
5. **Check logs** for each instance: you should see `flux mode: rank N`, `fresh state for <chain>, starting
   at head`, then normal poll lines. Two instances with the same rank is the collision case above. Raise
   `FREEZEBOT_FLUX_INSTANCES` if you want to avoid it.
6. **Before going live,** run with `FREEZEBOT_LIVE` unset for a day and confirm that only rank 0 logs
   `[dry-run] … would post` immediately while the others log `waiting`. Note that in dry-run no markers are
   published, so standbys will also "post" to stdout after their delay. That is expected.
7. **Stop the old single-instance bot** only after Flux is live. Otherwise both will post.

## Costs

Unknown and not verified. Flux pricing depends on resources, instance count, Enterprise, subscription length
and the FLUX price; use the price shown in the registration form. Enterprise apps cost more than standard
ones (amount unknown).

## Not verified

Nothing here has been deployed to Flux. The exact Flux form field names, Enterprise availability and price,
whether `FLUX_INSTANCE` or another instance-id env var is injected (the hostname fallback covers its
absence), and whether Flux acts on Docker `HEALTHCHECK` are all unverified. Relay support for `#d` filters on
kind 30078 is standard NIP-01/NIP-78 behavior, but it has only been tested against fakes here.
