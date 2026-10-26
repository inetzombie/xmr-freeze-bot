"""Output adapters.

stdout, Telegram, Nostr (NIP-01, stdlib BIP-340 signing) and Mastodon are
implemented. Nothing is posted anywhere unless --live is passed AND the adapter
is enabled and configured. Credentials are only ever read from the environment
(or a file named by a *_FILE env var); nothing secret is stored in the repo.
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request

from .http import USER_AGENT

log = logging.getLogger(__name__)


def read_secret(env_name, file_env=None):
    """Resolve a secret from $<env_name>, or from the file named by $<file_env>.

    The file may hold a bare value on its own line, or KEY=VALUE lines (the line
    whose key matches env_name wins). Lines starting with # and blanks are
    ignored. Returns None when nothing is found. The value itself is never
    logged.
    """
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    path = os.environ.get(file_env, "").strip() if file_env else ""
    if path and os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, sep, v = line.partition("=")
                if not sep:
                    return k.strip() or None
                if k.strip() == env_name:
                    return v.strip().strip('"').strip("'") or None
    return None


class Output:
    name = "base"
    env_vars = ()

    def __init__(self, live=False):
        self.live = live
        self.creds = {k: os.environ.get(k) for k in self.env_vars}

    @property
    def configured(self):
        return all(self.creds.values())

    def send(self, message, event):
        raise NotImplementedError


class StdoutOutput(Output):
    name = "stdout"

    def send(self, message, event):
        print(message, flush=True)
        print(flush=True)


class NostrOutput(Output):
    """Publish each message as a NIP-01 kind-1 note, signed with $NOSTR_NSEC (nsec1… or hex; or a file named by
    $NOSTR_NSEC_FILE), to every relay in $NOSTR_RELAYS (comma-separated wss:// URLs). Succeeds if at least one
    relay accepts the note. Tags it #monero/#xmr so it shows up in hashtag feeds."""
    name = "nostr"
    env_vars = ("NOSTR_NSEC", "NOSTR_RELAYS")

    def __init__(self, live=False, publisher=None):
        self.live = live
        secret = read_secret("NOSTR_NSEC", "NOSTR_NSEC_FILE")
        self.relays = [r.strip() for r in (os.environ.get("NOSTR_RELAYS") or "").split(",") if r.strip()]
        self._seckey = None
        if secret:
            from .nostr import parse_seckey
            self._seckey = parse_seckey(secret)
        self._publish = publisher

    @property
    def configured(self):
        return bool(self._seckey and self.relays)

    def _missing(self):
        return [k for k, ok in (("NOSTR_NSEC (or NOSTR_NSEC_FILE)", self._seckey), ("NOSTR_RELAYS", self.relays))
                if not ok]

    def send(self, message, event):
        if not self.live:
            log.info("[dry-run] nostr would post: %s", message.splitlines()[0])
            return
        if not self.configured:
            raise RuntimeError(f"nostr: missing {self._missing()}")
        from . import nostr
        ev = nostr.make_event(message, self._seckey, tags=[["t", "monero"], ["t", "xmr"]])
        publish = self._publish or nostr.publish
        results = {}
        for relay in self.relays:
            try:
                results[relay] = publish(relay, ev)
            except Exception as e:  # one bad relay must not stop the others
                results[relay] = (False, str(e)[:120])
        if not any(ok for ok, _ in results.values()):
            raise RuntimeError(f"nostr: no relay accepted the note: {results}")
        return ev


class MastodonOutput(Output):
    """POST {MASTODON_BASE_URL}/api/v1/statuses with Bearer $MASTODON_ACCESS_TOKEN (or $MASTODON_ACCESS_TOKEN_FILE).
    Uses an Idempotency-Key derived from the event uid so a retry never double-posts."""
    name = "mastodon"
    env_vars = ("MASTODON_BASE_URL", "MASTODON_ACCESS_TOKEN")

    def __init__(self, live=False, transport=None, visibility="unlisted", sleep=time.sleep):
        self.live = live
        self.base = (os.environ.get("MASTODON_BASE_URL") or "").strip().rstrip("/") or None
        self.token = read_secret("MASTODON_ACCESS_TOKEN", "MASTODON_ACCESS_TOKEN_FILE")
        self.visibility = visibility
        self._transport = transport or _urllib_post_auth
        self._sleep = sleep

    @property
    def configured(self):
        return bool(self.base and self.token)

    def _missing(self):
        return [k for k, ok in (("MASTODON_BASE_URL", self.base),
                                ("MASTODON_ACCESS_TOKEN (or MASTODON_ACCESS_TOKEN_FILE)", self.token)) if not ok]

    def send(self, message, event):
        if not self.live:
            log.info("[dry-run] mastodon would post: %s", message.splitlines()[0])
            return
        if not self.configured:
            raise RuntimeError(f"mastodon: missing {self._missing()}")
        data = json.dumps({"status": message, "visibility": self.visibility}).encode()
        headers = {"Authorization": f"Bearer {self.token}",
                   "Idempotency-Key": getattr(event, "uid", None) or str(hash(message))}
        from .batching import RateLimited
        for attempt in range(3):
            status, body = self._transport(f"{self.base}/api/v1/statuses", data, headers, 30)
            if status == 200:
                return body
            if status == 429:
                # Never hammer a 429: hand back to the batcher, which re-queues and backs off.
                ra = body.get("_retry_after") if isinstance(body, dict) else None
                raise RateLimited(ra, "mastodon rate-limited (HTTP 429)")
            if status in (0, 500, 502, 503, 504) and attempt < 2:
                self._sleep(5 * (attempt + 1))
                continue
            desc = body.get("error") if isinstance(body, dict) else body
            raise RuntimeError(f"mastodon post failed (HTTP {status}): {desc}")


def _retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except Exception:
        return None


def _urllib_post_auth(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            if not isinstance(body, dict):
                body = {"error": str(body)[:200]}
        except Exception:
            body = {"error": f"HTTP {e.code}"}
        # Retry-After may be seconds or an HTTP date; Mastodon also sends X-RateLimit-Reset (ISO time).
        ra = _retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
        if ra is None and e.headers and e.headers.get("X-RateLimit-Reset"):
            try:
                from datetime import datetime
                reset = datetime.fromisoformat(e.headers["X-RateLimit-Reset"].replace("Z", "+00:00"))
                ra = max(0.0, reset.timestamp() - time.time())
            except Exception:
                ra = None
        body["_retry_after"] = ra
        return e.code, body
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"error": f"network error: {getattr(e, 'reason', e)}"}


def _urllib_post(url, data, timeout):
    """POST raw JSON bytes. Returns (status_code, parsed_body_or_desc).

    Captures the body of error responses too (Telegram returns a JSON error with
    parameters.retry_after on HTTP 429). Never raises on HTTP status; network
    failures come back as status 0. The URL (which carries the bot token) is
    never put into the returned data or any log line.
    """
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"description": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError) as e:
        # str(e) is the reason (e.g. "timed out"); it does not contain the URL.
        return 0, {"description": f"network error: {e.reason if hasattr(e, 'reason') else e}"}


class TelegramOutput(Output):
    """Post each composed freeze message to a Telegram channel via the Bot API.

    Token comes from $TELEGRAM_BOT_TOKEN or the file named by
    $TELEGRAM_BOT_TOKEN_FILE; the channel from $TELEGRAM_CHAT_ID (e.g.
    '@FreezeWatchXMR' or a numeric -100... id). The message text is sent exactly
    as composed by fmt.format_message.
    """
    name = "telegram"
    env_vars = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    API_BASE = "https://api.telegram.org"
    # Telegram allows ~1 message/second to a given chat; stay just under that.
    DEFAULT_MIN_INTERVAL = 1.05

    def __init__(self, live=False, min_interval=None, max_retries=3,
                 disable_preview=True, transport=None, sleep=time.sleep,
                 monotonic=time.monotonic):
        self.live = live
        self.token = read_secret("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_FILE")
        self.chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip() or None
        self.min_interval = self.DEFAULT_MIN_INTERVAL if min_interval is None else min_interval
        self.max_retries = max_retries
        self.disable_preview = disable_preview
        self._transport = transport or _urllib_post
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_send = None

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def _missing(self):
        missing = []
        if not self.token:
            missing.append("TELEGRAM_BOT_TOKEN (or TELEGRAM_BOT_TOKEN_FILE)")
        if not self.chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        return missing

    def _rate_limit(self):
        if self.min_interval <= 0 or self._last_send is None:
            return
        wait = self.min_interval - (self._monotonic() - self._last_send)
        if wait > 0:
            self._sleep(wait)

    def send(self, message, event):
        if not self.live:
            log.info("[dry-run] telegram would post to %s: %s",
                     self.chat_id or "<no chat id>", message.splitlines()[0])
            return
        if not self.configured:
            raise RuntimeError(f"telegram: missing {self._missing()}")
        self._rate_limit()
        url = f"{self.API_BASE}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": self.disable_preview,
        }
        data = json.dumps(payload).encode()
        for attempt in range(self.max_retries + 1):
            status, body = self._transport(url, data, 30)
            if status == 200 and isinstance(body, dict) and body.get("ok"):
                self._last_send = self._monotonic()
                return body
            retry_after = None
            desc = None
            if isinstance(body, dict):
                retry_after = (body.get("parameters") or {}).get("retry_after")
                desc = body.get("description")
            if status == 429 and retry_after is not None and attempt < self.max_retries:
                log.warning("telegram rate-limited (429), retry_after=%ss", retry_after)
                self._sleep(float(retry_after) + 0.5)
                continue
            if status in (0, 500, 502, 503, 504) and attempt < self.max_retries:
                log.warning("telegram transient failure (status %s), retrying", status)
                self._sleep(1.5 * (attempt + 1))
                continue
            # Error message deliberately excludes the URL so the token never leaks.
            raise RuntimeError(f"telegram sendMessage failed (HTTP {status}): {desc or body}")
        raise RuntimeError("telegram sendMessage failed after retries")


REGISTRY = {c.name: c for c in (StdoutOutput, NostrOutput, MastodonOutput, TelegramOutput)}


def _batch_opts(opts, defaults):
    """Batching options for an output; `defaults` = (enabled, window_s, max_per_hour)."""
    b = opts.get("batching") or {}
    return (b.get("enabled", defaults[0]), b.get("window_seconds", defaults[1]),
            b.get("max_posts_per_hour", defaults[2]))


# Mastodon batches by default (politeness; mastodon.social suspends spammy bots).
# Nostr/Telegram can opt in via "batching": {"enabled": true}; off by default so Telegram stays instant.
BATCH_DEFAULTS = {"mastodon": (True, 120, 4), "nostr": (False, 120, 0), "telegram": (False, 120, 0)}


def build_outputs(cfg, live=False):
    from .batching import BatchingOutput
    outs = []
    for name, opts in cfg.items():
        if not opts.get("enabled") or name not in REGISTRY:
            continue
        cls = REGISTRY[name]
        if name == "telegram":
            kwargs = {}
            if "min_interval" in opts:
                kwargs["min_interval"] = opts["min_interval"]
            if "disable_web_page_preview" in opts:
                kwargs["disable_preview"] = opts["disable_web_page_preview"]
            out = cls(live=live, **kwargs)
        elif name == "mastodon":
            out = cls(live=live, visibility=opts.get("visibility", "unlisted"))
        else:
            out = cls(live=live)
        if name in BATCH_DEFAULTS:
            enabled, window, cap = _batch_opts(opts, BATCH_DEFAULTS[name])
            if enabled:
                out = BatchingOutput(out, window=window, max_per_hour=cap,
                                     char_limit=opts.get("char_limit", 500 if name == "mastodon" else 4000),
                                     summary_link=opts.get("summary_link"))
        outs.append(out)
    return outs
