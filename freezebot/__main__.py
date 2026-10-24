"""CLI entrypoint: python -m freezebot [--once] [--chain NAME --from-block N [--to-block M]] [--live]"""
import argparse
import json
import logging
import sys
import time
from decimal import Decimal

from .evm import EvmWatcher
from .fmt import format_message
from .outputs import build_outputs
from .store import Store
from .solana import SolanaWatcher
from .tron import TronWatcher

log = logging.getLogger("freezebot")
WATCHERS = {"evm": EvmWatcher, "tron": TronWatcher, "solana": SolanaWatcher}


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
    if from_cursor > end:
        return {}
    counts = {}
    for events, chunk_end in watcher.scan(from_cursor, end):
        for ev in events:
            r = handle(ev, store, outputs, min_amount)
            counts[r] = counts.get(r, 0) + 1
        if to_cursor is None:  # backfills don't move the live cursor
            store.set_cursor(watcher.name, chunk_end)
    return counts


def main(argv=None):
    p = argparse.ArgumentParser(prog="freezebot", description=__doc__)
    p.add_argument("--config", default="config.json")
    p.add_argument("--db", help="override db_path")
    p.add_argument("--chain", action="append", help="only these chains (repeatable)")
    p.add_argument("--from-block", type=int, help="backfill start block, or slot on Solana (requires one --chain)")
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
        counts = run_chain(w, store, outputs, min_amount, fc, tc if tc is not None else w.head())
        log.info("backfill %s done: %s", w.name, counts)
        return 0

    heartbeat = float(cfg.get("heartbeat_minutes", 60)) * 60
    last_beat, failures = time.monotonic(), {}
    while True:
        for w in watchers:
            try:
                counts = run_chain(w, store, outputs, min_amount)
                if counts:
                    log.info("%s: %s", w.name, counts)
            except Exception as e:
                failures[w.name] = failures.get(w.name, 0) + 1
                log.error("%s poll failed: %s", w.name, e)
        if a.once:
            return 0
        if heartbeat > 0 and time.monotonic() - last_beat >= heartbeat:
            # One line per hour so a quiet log still shows every chain is advancing.
            log.info("heartbeat: %d chains, cursors %s, poll failures since last heartbeat %s",
                     len(watchers), {w.name: store.get_cursor(w.name) for w in watchers}, failures or "none")
            last_beat, failures = time.monotonic(), {}
        time.sleep(cfg.get("poll_interval_seconds", 60))


if __name__ == "__main__":
    sys.exit(main())
