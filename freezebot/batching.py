"""Polite posting: batch alerts over a short window and cap posts per hour.

BatchingOutput wraps another output. send() only enqueues (so chain threads and
other outputs such as Telegram are never delayed); a background worker waits
`window` seconds after the first queued alert, collects anything else that
arrived, and posts either the single original message or ONE combined summary.
At most `max_per_hour` posts go out per rolling hour; excess alerts stay queued
and roll into the next combined post.
"""
import collections
import logging
import threading
import time

from .fmt import format_amount

log = logging.getLogger(__name__)

HOUR = 3600.0


class RateLimited(Exception):
    """Raised by an inner output on HTTP 429; retry_after is seconds (may be None)."""

    def __init__(self, retry_after=None, msg="rate limited"):
        super().__init__(msg)
        self.retry_after = retry_after


def _line(ev):
    try:
        return f"{format_amount(ev.amount)} ${ev.symbol} frozen on {ev.chain}"
    except Exception:
        return None


def combine(items, limit=500, link=None):
    """Build one post (<= limit chars) summarising [(message, event), ...]."""
    if len(items) == 1:
        return items[0][0][:limit]
    head = f"{len(items)} freezes that should have gone through #Monero $XMR:"
    tail_base = "Learn more at getmonero.org"
    lines = [_line(ev) or msg.splitlines()[0][:80] for msg, ev in items]
    for shown in range(len(lines), -1, -1):
        rest = len(lines) - shown
        parts = [head] + ["- " + ln for ln in lines[:shown]]
        if rest:
            parts.append(f"+{rest} more" + (f": {link}" if link else ""))
        parts.append(tail_base)
        text = "\n".join(parts)
        if len(text) <= limit:
            return text
    return (head + f" +{len(items)} more")[:limit]


class BatchingOutput:
    def __init__(self, inner, window=120.0, max_per_hour=4, char_limit=500, summary_link=None,
                 clock=time.monotonic, sleep=time.sleep, start_thread=True):
        self.inner = inner
        self.name = inner.name
        self.window = float(window)
        self.max_per_hour = int(max_per_hour)
        self.char_limit = char_limit
        self.summary_link = summary_link
        self._clock = clock
        self._sleep = sleep
        self._queue = []
        self._first_at = None
        self._sent = collections.deque()
        self._blocked_until = 0.0
        self._cv = threading.Condition()
        self._thread = None
        if start_thread and getattr(inner, "live", False):
            self._thread = threading.Thread(target=self._run, name=f"batch-{self.name}", daemon=True)
            self._thread.start()

    @property
    def configured(self):
        return self.inner.configured

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def send(self, message, event):
        with self._cv:
            if not self._queue:
                self._first_at = self._clock()
            self._queue.append((message, event))
            self._cv.notify()
        if self._thread is None and not getattr(self.inner, "live", False):
            self.flush(force=True)  # dry-run: just log immediately via inner

    def _slot_free_at(self, now):
        while self._sent and now - self._sent[0] >= HOUR:
            self._sent.popleft()
        t = max(now, self._blocked_until)
        if self.max_per_hour > 0 and len(self._sent) >= self.max_per_hour:
            t = max(t, self._sent[0] + HOUR)
        return t

    def next_due(self):
        """Clock time at which a post may go out, or None if nothing is queued."""
        with self._cv:
            if not self._queue:
                return None
            return max(self._first_at + self.window, self._slot_free_at(self._clock()))

    def flush(self, force=False):
        """Post one (possibly combined) message if due. Returns the text posted, or None."""
        now = self._clock()
        with self._cv:
            if not self._queue:
                return None
            if not force and (now < self._first_at + self.window or now < self._slot_free_at(now)):
                return None
            items, self._queue, self._first_at = self._queue, [], None
        text = combine(items, self.char_limit, self.summary_link)
        event = items[0][1] if len(items) == 1 else _Batch(items)
        try:
            self.inner.send(text, event)
        except RateLimited as e:
            backoff = float(e.retry_after) if e.retry_after else 300.0
            log.warning("%s rate-limited; backing off %.0fs, %d alert(s) re-queued", self.name, backoff, len(items))
            self._requeue(items, now + backoff)
            return None
        except Exception as e:
            log.error("%s batch post failed (%d alert(s) dropped): %s", self.name, len(items), e)
            return None
        with self._cv:
            self._sent.append(self._clock())
        if len(items) > 1:
            log.info("%s posted combined summary of %d alerts", self.name, len(items))
        return text

    def _requeue(self, items, blocked_until):
        with self._cv:
            self._queue = items + self._queue
            self._first_at = self._clock() - self.window
            self._blocked_until = blocked_until

    def _run(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
            due = self.next_due()
            if due is not None:
                wait = due - self._clock()
                if wait > 0:
                    self._sleep(min(wait, 30.0))
                    continue
                self.flush()


class _Batch:
    """Stand-in event for a combined post (stable idempotency key from member uids)."""

    def __init__(self, items):
        import hashlib
        uids = ",".join(getattr(ev, "uid", "") for _, ev in items)
        self.uid = "batch:" + hashlib.sha256(uids.encode()).hexdigest()[:32]
        self.events = [ev for _, ev in items]
