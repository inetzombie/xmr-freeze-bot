"""Output adapters. Only stdout is implemented; the rest are credential-loading stubs.

Nothing is posted anywhere unless --live is passed AND an adapter is fully implemented.
"""
import logging
import os

log = logging.getLogger(__name__)


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


class TelegramOutput(_StubOutput):
    # TODO: POST https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage chat_id=TELEGRAM_CHAT_ID.
    name = "telegram"
    env_vars = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


REGISTRY = {c.name: c for c in (StdoutOutput, NostrOutput, MastodonOutput, TelegramOutput)}


def build_outputs(cfg, live=False):
    return [REGISTRY[name](live=live) for name, opts in cfg.items() if opts.get("enabled") and name in REGISTRY]
