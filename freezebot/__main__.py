"""CLI entrypoint: python -m freezebot [--once] [--chain NAME --from-block N [--to-block M]] [--live]"""
import argparse
import json
import logging
import sys
import threading
import time
from decimal import Decimal

from .evm import EvmWatcher
from .fmt import format_message
from .outputs import build_outputs
from .store import Store
from .solana import SolanaWatcher
from .stellar import StellarWatcher, ledger_cursor
from .tron import TronWatcher
from .xrpl import XrplWatcher

log = logging.getLogger("freezebot")
WATCHERS = {"evm": EvmWatcher, "tron": TronWatcher, "solana": SolanaWatcher,
            "stellar": StellarWatcher, "xrpl": XrplWatcher}


# Per-chain poll cadence (seconds) when a chain sets no "poll_interval_seconds" of its own.
# Fast chains with the most freeze traffic poll often; the rest use the global default.
DEFAULT_CHAIN_INTERVALS = {"tron": 12, "ethereum": 12}
_output_lock = threading.Lock()  # outputs (rate limiters, sockets) are shared by all chain threads


def chain_interval(chain_cfg, cfg):
    if "poll_interval_seconds" in chain_cfg:
        return float(chain_cfg["poll_interval_seconds"])
    return float(DEFAULT_CHAIN_INTERVALS.get(chain_cfg["name"], cfg.get("poll_interval_seconds", 60)))


def make_watcher(chain_cfg):
    return WATCHERS[chain_cfg["type"]](chain_cfg)


def handle(ev, store, outputs, min_amount):
    if store.seen(ev.uid):
        return "dup"
    if not ev.post:
        store.record(ev, "skipped:" + (ev.skip_reason or "not-posted-kind"))
        return "skipped"
    if ev.amount is None:
        store.record(ev, "skipped:unknown-amount")
        log.warning("unknown amount, not posting: %s %s", ev.chain, ev.tx)
        return "skipped"
    if ev.amount < Decimal(str(min_amount)):
        store.record(ev, "skipped:below-min")
        log.info("below min (%s %s): %s", ev.amount, ev.symbol, ev.tx)
        return "skipped"
    msg = format_message(ev)
    with _output_lock:
        if store.seen(ev.uid):  # another thread got here first
            return "dup"
        for out in outputs:
            try:
                out.send(msg, ev)
            except Exception as e:  # keep other outputs going
                log.error("%s failed: %s", out.name, e)
        store.record(ev, "emitted", msg)
    return "emitted"


def run_chain(watcher, store, outputs, min_amount, from_cursor=None, to_cursor=None):
    head = watcher.head()
    end = min(to_cursor, head) if to_cursor is not None else head
    if from_cursor is None:
        last = store.get_cursor(watcher.name)
        from_cursor = last + 1 if last is not None else watcher.default_start(head)
        if last is not None:  # re-scan a window the source may have indexed late (dedupe by uid)
            from_cursor = max(0, from_cursor - int(getattr(watcher, "rescan_overlap", 0) or 0))
    if from_cursor > end:
        return {}
    counts = {}
    for events, chunk_end in watcher.scan(from_cursor, end):
        for ev in events:
            r = handle(ev, store, outputs, min_amount)
            counts[r] = counts.get(r, 0) + 1
        if to_cursor is None:  # backfills don't move the live cursor
            store.set_cursor(watcher.name, max(chunk_end, store.get_cursor(watcher.name) or 0))
    return counts


def poll_forever(w, interval, store, outputs, min_amount, failures, stop):
    """One thread per chain: a slow, failing, or catching-up chain never delays the others."""
    while not stop.is_set():
        t0 = time.monotonic()
        before = store.get_cursor(w.name)
        log.info("poll %s start (cursor %s)", w.name, before)
        try:
            counts = run_chain(w, store, outputs, min_amount)
            log.info("poll %s done in %.1fs, cursor %s -> %s%s", w.name, time.monotonic() - t0,
                     before, store.get_cursor(w.name), f", {counts}" if counts else "")
        except Exception as e:
            failures[w.name] = failures.get(w.name, 0) + 1
            log.error("%s poll failed after %.1fs: %s", w.name, time.monotonic() - t0, e)
        stop.wait(max(1.0, interval - (time.monotonic() - t0)))


def main(argv=None):
    p = argparse.ArgumentParser(prog="freezebot", description=__doc__)
    p.add_argument("--config", default="config.json")
    p.add_argument("--db", help="override db_path")
    p.add_argument("--chain", action="append", help="only these chains (repeatable)")
    p.add_argument("--from-block", type=int, help="backfill start block (slot on Solana, ledger on Stellar/XRPL; requires one --chain)")
    p.add_argument("--to-block", type=int, help="backfill end block (default: head)")
    p.add_argument("--once", action="store_true", help="one pass then exit")
    p.add_argument("--live", action="store_true", help="allow non-stdout adapters to post (default: dry-run)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = json.load(open(a.config))
    chains = [c for c in cfg["chains"] if not a.chain or c["name"] in a.chain]
    if a.from_block is not None and len(chains) != 1:
        p.error("--from-block needs exactly one --chain")
    store = Store(a.db or cfg["db_path"])
    outputs = build_outputs(cfg["outputs"], live=a.live)
    if a.live:
        unconfigured = [o for o in outputs if not o.configured]
        if unconfigured:
            for o in unconfigured:
                need = o._missing() if hasattr(o, "_missing") else list(o.env_vars)
                log.error("--live set but '%s' output is not configured; set: %s",
                          o.name, ", ".join(need))
            return 2
    min_amount = cfg.get("min_amount", 1)
    watchers = [make_watcher(c) for c in chains]

    if a.from_block is not None:
        w = watchers[0]
        to_b = a.to_block
        fc, tc = a.from_block, to_b
        if isinstance(w, TronWatcher):  # Tron cursor is a timestamp
            fc = w.block_to_cursor(a.from_block)
            tc = w.block_to_cursor(to_b) + 2999 if to_b is not None else None
        elif isinstance(w, StellarWatcher):  # Stellar cursor is a Horizon paging token
            fc = ledger_cursor(a.from_block)
            tc = ledger_cursor(to_b + 1) - 1 if to_b is not None else None
        counts = run_chain(w, store, outputs, min_amount, fc, tc if tc is not None else w.head())
        log.info("backfill %s done: %s", w.name, counts)
        return 0

    log.info("starting: %d chains (%s), live=%s", len(watchers), ", ".join(w.name for w in watchers), a.live)
    heartbeat = float(cfg.get("heartbeat_minutes", 60)) * 60
    if a.once:
        for w in watchers:
            try:
                counts = run_chain(w, store, outputs, min_amount)
                if counts:
                    log.info("%s: %s", w.name, counts)
            except Exception as e:
                log.error("%s poll failed: %s", w.name, e)
        return 0
    failures, stop = {}, threading.Event()
    by_name = {c["name"]: c for c in cfg["chains"]}
    for w in watchers:
        iv = chain_interval(by_name.get(w.name, {"name": w.name}), cfg)
        log.info("chain %s polls every %.0fs", w.name, iv)
        threading.Thread(target=poll_forever, name=f"poll-{w.name}", daemon=True,
                         args=(w, iv, store, outputs, min_amount, failures, stop)).start()
    try:
        while True:
            time.sleep(heartbeat if heartbeat > 0 else 3600)
            if heartbeat > 0:
                # One line per hour so a quiet log still shows every chain is advancing.
                snap = dict(failures)
                failures.clear()
                log.info("heartbeat: %d chains, cursors %s, poll failures since last heartbeat %s",
                         len(watchers), {w.name: store.get_cursor(w.name) for w in watchers}, snap or "none")
    finally:
        stop.set()

if __name__ == "__main__":
    sys.exit(main())
