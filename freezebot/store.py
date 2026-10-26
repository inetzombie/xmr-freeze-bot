"""SQLite state: per-chain cursor + event dedupe."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (chain TEXT PRIMARY KEY, cursor INTEGER NOT NULL, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS events (
  uid TEXT PRIMARY KEY, chain TEXT, symbol TEXT, event TEXT, holder TEXT,
  amount_raw TEXT, block INTEGER, tx TEXT, status TEXT, message TEXT, payload TEXT, created_at INTEGER
);
"""


class Store:
    def __init__(self, path):
        # One connection shared by all chain threads; every access goes through self.lock.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def get_cursor(self, chain):
        with self.lock:
            row = self.db.execute("SELECT cursor FROM cursors WHERE chain=?", (chain,)).fetchone()
            return row[0] if row else None

    def set_cursor(self, chain, cursor):
        with self.lock:
            self.db.execute("INSERT INTO cursors(chain,cursor,updated_at) VALUES(?,?,?) "
                            "ON CONFLICT(chain) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
                            (chain, int(cursor), int(time.time())))
            self.db.commit()

    def seen(self, uid):
        with self.lock:
            return self.db.execute("SELECT 1 FROM events WHERE uid=?", (uid,)).fetchone() is not None

    def record(self, ev, status, message=None):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                ev.uid, ev.chain, ev.symbol, ev.event, ev.holder,
                None if ev.amount_raw is None else str(ev.amount_raw),
                ev.block, ev.tx, status, message, json.dumps(ev.to_dict()), int(time.time())))
            self.db.commit()
