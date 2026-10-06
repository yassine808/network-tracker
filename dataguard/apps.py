"""Per-app usage history: a background sampler that records each program's bytes into SQLite."""

import logging
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from .common import DB_NAME, IS_WIN, psutil
from .processes import io_proxy

KEEP_DAYS = 400


def _iso(day):
    return day.isoformat() if isinstance(day, date) else day


class AppStore:
    """Daily per-app byte totals in one SQLite file. Thread-safe (one guarded connection)."""

    def __init__(self, home):
        folder = Path(home)
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / DB_NAME
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS app_usage ("
                "day TEXT NOT NULL, app TEXT NOT NULL, bytes INTEGER NOT NULL, "
                "PRIMARY KEY (day, app)) WITHOUT ROWID")
            self.conn.commit()
        self.pending = {}  # (day, app) -> bytes not yet written to disk
        self._migrate()

    def _migrate(self):
        """One-time import of the legacy app_usage.db into dataguard.db, then park the old file."""
        old = self.path.with_name("app_usage.db")
        with self.lock, self.conn:
            if self.conn.execute("SELECT 1 FROM app_usage LIMIT 1").fetchone() or not old.exists():
                return
            try:
                src = sqlite3.connect(str(old))
                try:
                    rows = src.execute("SELECT day, app, bytes FROM app_usage").fetchall()
                finally:
                    src.close()
                self.conn.executemany("INSERT OR REPLACE INTO app_usage VALUES (?, ?, ?)", rows)
            except sqlite3.Error as e:
                logging.warning("app_usage.db import failed (%s); keeping it", e)
                return
        try:
            old.replace(old.with_name(old.name + ".migrated"))
        except OSError:
            pass

    def add(self, day, app, n):
        with self.lock:
            self.pending[(day, app)] = self.pending.get((day, app), 0) + n

    def clear_since(self, day):
        """Drop this cycle's per-app rows (reset button): day = first day to remove."""
        with self.lock:
            self.pending = {(d, a): n for (d, a), n in self.pending.items() if d < day}
            with self.conn:
                self.conn.execute("DELETE FROM app_usage WHERE day >= ?", (day,))

    def flush(self, force=False):
        """Write buffered bytes to disk, then drop history older than KEEP_DAYS."""
        with self.lock:
            if not self.pending and not force:
                return
            try:
                if self.pending:
                    self.conn.executemany(
                        "INSERT INTO app_usage (day, app, bytes) VALUES (?, ?, ?) "
                        "ON CONFLICT (day, app) DO UPDATE SET bytes = app_usage.bytes + excluded.bytes",
                        [(d, a, n) for (d, a), n in self.pending.items()])
                    self.pending.clear()
                cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
                self.conn.execute("DELETE FROM app_usage WHERE day < ?", (cutoff,))
                self.conn.commit()
            except sqlite3.Error:
                pass  # disk full or locked: retry on the next flush, tracker keeps running

    def snapshot(self, cycle_start, today):
        """Per-app totals for the cycle, today and the last 30 days, plus the 30-day day list."""
        with self.lock:
            t, cs = _iso(today), _iso(cycle_start)
            d30 = (today - timedelta(days=29)).isoformat()
            window = min(cs, d30)
            agg = {}
            rows = self.conn.execute(
                "SELECT day, app, bytes FROM app_usage WHERE day >= ? AND day <= ?", (window, t))
            for day, app, n in rows:
                self._fold(agg, day, app, n, t, cs, d30)
            for (day, app), n in self.pending.items():  # show bytes before they hit the disk
                if window <= day <= t:
                    self._fold(agg, day, app, n, t, cs, d30)
            apps = sorted(agg.values(), key=lambda r: (-r["cycle"], -r["d30"], r["app"]))
            return {"apps": apps, "count": len(apps),
                    "total_today": sum(r["today"] for r in apps),
                    "total_cycle": sum(r["cycle"] for r in apps),
                    "days": [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]}

    @staticmethod
    def _fold(agg, day, app, n, t, cs, d30):
        r = agg.setdefault(app, {"app": app, "today": 0, "cycle": 0, "d30": 0, "days": {}})
        if day == t:
            r["today"] += n
        if cs <= day:
            r["cycle"] += n
        if day >= d30:
            r["d30"] += n
            r["days"][day] = r["days"].get(day, 0) + n


class AppTracker(threading.Thread):
    """Sums each program's I/O delta every INTERVAL seconds while DataGuard runs.
    `allowed` says whether bytes are recorded - the dashboard passes the monitor's
    "are we on the configured network" flag, so the Apps page follows the meter's rule."""

    INTERVAL = 15
    FLUSH_EVERY = 60

    def __init__(self, store, allowed=None):
        super().__init__(name="dataguard-apps", daemon=True)
        self.store = store
        self.allowed = allowed or (lambda: True)
        self.supported = IS_WIN
        self.err = ""
        self.base = {}      # pid -> (name, other_bytes) at the previous tick
        self.paths = {}     # app name -> its .exe, for the Apps page (icon + block button)
        self.blocked = set()  # apps the user switched to "block internet" this session
        self.t_flush = time.time()
        self.stop_event = threading.Event()

    def exe(self, name):
        """Last known file path for an app, or "" when it never showed one."""
        return self.paths.get(name) or ""

    def run(self):
        if not self.supported:
            return
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.err = ""
            except Exception as e:
                self.err = f"{type(e).__name__}: {e}"
            self.stop_event.wait(self.INTERVAL)
        self.store.flush(force=True)

    def stop(self):
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)
        self.store.flush(force=True)

    def tick(self):
        record = self.allowed()  # off the configured network: track state, but record nothing
        try:
            pids = {c.pid for c in psutil.net_connections(kind="inet") if c.pid}
        except (psutil.AccessDenied, OSError):
            pids = None
        day = date.today().isoformat()
        cur, gained = {}, 0
        for p in psutil.process_iter(["name", "exe"]):
            name = p.info.get("name") or f"pid {p.pid}"
            if p.info.get("exe"):
                self.paths.setdefault(name, p.info["exe"])  # record for every process, connected or not
            if pids is not None and p.pid not in pids:
                continue
            try:
                v = io_proxy(p)
            except (psutil.AccessDenied, psutil.Error, AttributeError):
                continue
            cur[p.pid] = (name, v)
            prev = self.base.get(p.pid)
            if prev is not None and prev[0] == name and v >= prev[1]:
                d = v - prev[1]
                if d and record:
                    self.store.add(day, name, d)
                    gained += d
        self.base = cur
        if gained and time.time() - self.t_flush >= self.FLUSH_EVERY:
            self.t_flush = time.time()
            self.store.flush()

    def snapshot(self, cycle_start, today):
        data = self.store.snapshot(cycle_start, today)
        for r in data["apps"]:
            r["blocked"] = r["app"] in self.blocked
        data.update(supported=self.supported, interval=self.INTERVAL, err=self.err)
        return data
