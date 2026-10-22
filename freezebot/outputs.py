"""Output adapters.

stdout and Telegram are implemented; Nostr and Mastodon remain credential-loading
stubs. Nothing is posted anywhere unless --live is passed AND the adapter is
implemented and configured. Credentials are only ever read from the environment
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


class _StubOutput(Output):
    def send(self, message, event):
        if not self.live:
            log.info("[dry-run] %s would post: %s", self.name, message.splitlines()[0])
            return
        if not self.configured:
            missing = [k for k, v in self.creds.items() if not v]
            raise RuntimeError(f"{self.name}: missing env vars {missing}")
        raise NotImplementedError(f"{self.name} posting is not implemented yet (MVP stub)")


class NostrOutput(_StubOutput):
    # TODO: sign kind-1 note (NIP-01) with NOSTR_NSEC and publish to NOSTR_RELAYS (comma-separated).
    name = "nostr"
    env_vars = ("NOSTR_NSEC", "NOSTR_RELAYS")


class MastodonOutput(_StubOutput):
    # TODO: POST {MASTODON_BASE_URL}/api/v1/statuses with Bearer MASTODON_ACCESS_TOKEN.
    name = "mastodon"
    env_vars = ("MASTODON_BASE_URL", "MASTODON_ACCESS_TOKEN")


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


def build_outputs(cfg, live=False):
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
            outs.append(cls(live=live, **kwargs))
        else:
            outs.append(cls(live=live))
    return outs
