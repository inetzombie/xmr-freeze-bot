"""Flux mode: run N copies of the bot (Flux runs 3 by default) with no shared disk, and have
only one of them post each freeze.

Mechanism ("post, then mark; standbys wait, then check"):
  * Every instance polls the chains independently.
  * Each instance has a deterministic rank. Rank 0 (the leader) posts as soon as it sees an event.
    Rank k waits k * FREEZEBOT_FLUX_STEP_SECONDS before deciding.
  * After posting, the poster publishes a *claim marker* to Nostr relays: a NIP-78 (kind 30078)
    addressable event, signed by the bot's Nostr key, with d-tag "freezebot:<sha256(uid)[:32]>".
    It is invisible in normal feeds and idempotent (same author+kind+d replaces itself).
  * A standby, after its delay, queries the relays for that marker. Found -> record the event as
    "peer-posted" and skip all outputs. Not found -> post (and publish its own marker), so a dead
    leader is covered by rank 1, a dead rank 1 by rank 2, and so on.
The Nostr notes themselves are also made deterministic (created_at = chain time, fixed content), so
even if two instances post the note, relays see the same event id and store it once. Mastodon posts
carry an Idempotency-Key per uid. Telegram has no read-back or idempotency, which is why the marker
exists. See docs/FLUX.md for failure modes.
"""
import hashlib
import logging
import os
import socket
import time

log = logging.getLogger(__name__)
MARKER_KIND = 30078


def env_flag(name, default=False):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def marker_tag(uid):
    return "freezebot:" + hashlib.sha256(uid.encode()).hexdigest()[:32]


def instance_rank(env=None, hostname=None):
    """FREEZEBOT_INSTANCE_RANK (explicit, 0 = leader) wins; else FLUX_INSTANCE / FREEZEBOT_INSTANCE_ID /
    hostname, hashed into 0..FREEZEBOT_FLUX_INSTANCES-1. Hash ranks can collide (two instances with the
    same rank); collisions only cost a possible duplicate post, never a missed one."""
    env = os.environ if env is None else env
    n = max(1, int(env.get("FREEZEBOT_FLUX_INSTANCES") or 3))
    explicit = (env.get("FREEZEBOT_INSTANCE_RANK") or "").strip()
    if explicit:
        return int(explicit)
    ident = ((env.get("FLUX_INSTANCE") or env.get("FREEZEBOT_INSTANCE_ID") or "").strip()
             or hostname or socket.gethostname())
    return int(hashlib.sha256(ident.encode()).hexdigest(), 16) % n


class FluxGuard:
    """Decides whether this instance should post an event. Network calls are injectable for tests."""

    def __init__(self, seckey, relays, rank, step_seconds=90.0, on_check_error="post",
                 querier=None, publisher=None, sleep=time.sleep):
        self.seckey, self.relays, self.rank = seckey, list(relays), int(rank)
        self.step = float(step_seconds)
        self.on_check_error = on_check_error
        self._query, self._publish, self._sleep = querier, publisher, sleep
        from .nostr import pubkey
        self.pub = pubkey(seckey).hex()

    @classmethod
    def from_env(cls, env=None, **kw):
        from .nostr import parse_seckey
        from .outputs import read_secret
        env = os.environ if env is None else env
        secret = read_secret("NOSTR_NSEC", "NOSTR_NSEC_FILE")
        relays = [r.strip() for r in (env.get("FREEZEBOT_FLUX_RELAYS") or env.get("NOSTR_RELAYS") or "").split(",")
                  if r.strip()]
        if not secret or not relays:
            raise RuntimeError("flux mode needs NOSTR_NSEC (or NOSTR_NSEC_FILE) and NOSTR_RELAYS/FREEZEBOT_FLUX_RELAYS "
                               "for the cross-instance claim marker")
        return cls(parse_seckey(secret), relays, instance_rank(env),
                   step_seconds=float(env.get("FREEZEBOT_FLUX_STEP_SECONDS") or 90),
                   on_check_error=(env.get("FREEZEBOT_FLUX_ON_CHECK_ERROR") or "post").strip().lower(), **kw)

    def delay(self):
        return self.rank * self.step

    def already_posted(self, uid):
        """True/False, or None when no relay could be queried."""
        from . import nostr
        q = self._query or nostr.query
        filt = {"kinds": [MARKER_KIND], "authors": [self.pub], "#d": [marker_tag(uid)], "limit": 1}
        answered = False
        for relay in self.relays:
            try:
                evs = q(relay, filt)
            except Exception as e:
                log.warning("flux: marker query failed on %s: %s", relay, str(e)[:120])
                continue
            answered = True
            if any(e.get("pubkey") == self.pub and ["d", marker_tag(uid)] in [t[:2] for t in e.get("tags", [])]
                   for e in evs):
                return True
        return False if answered else None

    def should_post(self, uid):
        """Leader: always True. Standby: wait rank*step, then True only if no marker exists."""
        if self.rank <= 0:
            return True
        d = self.delay()
        log.info("flux: standby rank %d waiting %.0fs before checking %s", self.rank, d, uid)
        self._sleep(d)
        seen = self.already_posted(uid)
        if seen is None:
            log.warning("flux: no relay reachable to check %s; on_check_error=%s", uid, self.on_check_error)
            return self.on_check_error != "skip"
        return not seen

    def mark(self, uid):
        from . import nostr
        pub = self._publish or nostr.publish
        ev = nostr.make_event("", self.seckey, kind=MARKER_KIND,
                              tags=[["d", marker_tag(uid)], ["rank", str(self.rank)]])
        ok_any = False
        for relay in self.relays:
            try:
                ok, _ = pub(relay, ev)
                ok_any = ok_any or ok
            except Exception as e:
                log.warning("flux: marker publish failed on %s: %s", relay, str(e)[:120])
        if not ok_any:
            log.error("flux: no relay accepted the claim marker for %s (standbys may re-post)", uid)
        return ok_any
