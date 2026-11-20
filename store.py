"""SQLite log of every check, plus saved settings. Survives restarts."""
from __future__ import annotations

import json
import sqlite3
import threading
import time


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS results(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, platform TEXT NOT NULL,
                name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_pn ON results(platform, name)")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_st ON results(status, id)")
            self.db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.commit()

    def add(self, platform, name, status, detail=""):
        with self.lock:
            self.db.execute("INSERT INTO results(ts,platform,name,status,detail) VALUES(?,?,?,?,?)",
                            (time.time(), platform, name, status, detail))
            self.db.commit()

    def known(self, platform, names, hours):
        """Names with a conclusive (non-unknown) result newer than `hours`."""
        cutoff = time.time() - hours * 3600
        out, names = set(), list(names)
        with self.lock:
            for i in range(0, len(names), 500):
                chunk = names[i:i + 500]
                q = ("SELECT DISTINCT name FROM results WHERE platform=? AND status!='unknown' "
                     "AND ts>=? AND name IN (%s)" % ",".join("?" * len(chunk)))
                out.update(r[0] for r in self.db.execute(q, [platform, cutoff, *chunk]))
        return out

    def counts(self):
        with self.lock:
            rows = self.db.execute("SELECT platform,status,COUNT(*) FROM results GROUP BY platform,status").fetchall()
        total, avail = {}, {}
        for platform, status, n in rows:
            total[status] = total.get(status, 0) + n
            if status == "available":
                avail[platform] = n
        return {"total": total, "available_by_platform": avail}

    @staticmethod
    def _where(status, platform):
        w, a = [], []
        if status:
            w.append("status=?"); a.append(status)
        if platform:
            w.append("platform=?"); a.append(platform)
        return (" WHERE " + " AND ".join(w)) if w else "", a

    def query(self, status=None, platform=None, limit=100, offset=0):
        where, args = self._where(status, platform)
        limit, offset = max(1, min(limit, 500)), max(0, offset)
        with self.lock:
            rows = self.db.execute(f"SELECT ts,platform,name,status,detail FROM results{where} "
                                   "ORDER BY id DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            n = self.db.execute(f"SELECT COUNT(*) FROM results{where}", args).fetchone()[0]
        return rows, n

    def export(self, status=None, platform=None):
        where, args = self._where(status, platform)
        db = sqlite3.connect(self.path)          # separate connection: does not block writers (WAL)
        try:
            cur = db.execute(f"SELECT ts,platform,name,status,detail FROM results{where} ORDER BY id", args)
            while True:
                rows = cur.fetchmany(1000)
                if not rows:
                    return
                yield from rows
        finally:
            db.close()

    def get_setting(self, key, default=None):
        with self.lock:
            r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value)))
            self.db.commit()
