"""Хранилище на SQLite и журнал событий."""
import json
import os
import sqlite3
import threading
import time

from .config import CFG


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o700)  # в базе история входов и хеши сессий
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS hosts(name TEXT PRIMARY KEY, last_ts REAL, latest TEXT);
        CREATE TABLE IF NOT EXISTS raw(host TEXT, metric TEXT, ts INTEGER, v REAL);
        CREATE INDEX IF NOT EXISTS raw_idx ON raw(host, metric, ts);
        CREATE TABLE IF NOT EXISTS roll(host TEXT, metric TEXT, ts INTEGER, v REAL, vmax REAL,
                                        PRIMARY KEY(host, metric, ts)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS cluster(kind TEXT PRIMARY KEY, ts REAL, src TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS events(ts REAL, level TEXT, msg TEXT);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS alert_log(id INTEGER PRIMARY KEY, key TEXT, level TEXT, msg TEXT,
                                             start REAL, end REAL, muted INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS alert_log_start ON alert_log(start);
        CREATE TABLE IF NOT EXISTS silence_log(id TEXT PRIMARY KEY, scope TEXT, reason TEXT, by TEXT,
                                               start REAL, end REAL);
        CREATE TABLE IF NOT EXISTS logins(host TEXT, ts REAL, user TEXT, ip TEXT, method TEXT, known INTEGER);
        CREATE INDEX IF NOT EXISTS logins_ts ON logins(ts);
        """)
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(path + ext):
                os.chmod(path + ext, 0o600)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(logins)")}
        for col, typ in (("port", "INTEGER"), ("end", "REAL"), ("key", "TEXT"), ("fail", "INTEGER DEFAULT 0")):
            if col not in cols:  # база от прежней версии: входы без выходов и неудачных попыток
                self.db.execute(f"ALTER TABLE logins ADD COLUMN {col} {typ}")

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def tx(self, fn):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                fn(self.db)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def meta_get(self, k, default=None):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r[0][0]) if r else default

    def meta_set(self, k, v):
        self.q("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v)))


S = Store(CFG["db"])


def event(level, msg):
    S.q("INSERT INTO events VALUES(?,?,?)", (time.time(), level, msg))
