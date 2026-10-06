"""Usage history: settings, daily byte counts, calibration, fired alerts and the alert list,
all in one small SQLite database. Thread-safe (one guarded connection)."""

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import DB_NAME
from .settings import DEFAULTS, clean_cfg, cycle_bounds

_SCHEMA = """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, rx INTEGER NOT NULL, tx INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cal (cycle TEXT PRIMARY KEY, offset INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fired (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (seq INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT NOT NULL,
                                   kind TEXT NOT NULL, title TEXT NOT NULL, msg TEXT NOT NULL);"""


class Store:
    """Settings + usage history in dataguard.db. Thread-safe."""

    KEEP_DAYS = 400

    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.home / DB_NAME), check_same_thread=False)
        self.cfg = dict(DEFAULTS)
        self.days = {}     # "YYYY-MM-DD" -> [bytes received, bytes sent] counted against the plan
        self.cal = {}      # {"cycle": "YYYY-MM-DD", "offset": bytes}, set by calibrate
        self.fired = {}    # which alerts were already shown
        self.log = []      # recent alerts, newest last (display); the table keeps the full history
        self._pending = []  # alert rows not yet written to the database
        self.dirty = False
        with self.lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self._migrate()
        self._load()

    @staticmethod
    def _read_json(path):
        try:
            return json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            Store._quarantine(path)
            return None

    @staticmethod
    def _quarantine(path):
        try:
            path.replace(path.with_name(path.name + ".bad"))  # keep the broken file, start fresh
        except OSError:
            pass

    @staticmethod
    def _park(path):
        """Old JSON file imported: keep it as a backup, out of the way."""
        try:
            path.replace(path.with_name(path.name + ".migrated"))
        except OSError:
            pass

    def _put_settings(self, cfg):
        self.conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in cfg.items()])

    def _migrate(self):
        """One-time import of the old config.json + usage.json (the files are renamed afterwards)."""
        with self.lock, self.conn:
            if not self.conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
                raw = self._read_json(self.home / "config.json")
                if isinstance(raw, dict):
                    try:
                        cfg = clean_cfg(raw, DEFAULTS)
                    except ValueError as e:
                        logging.warning("config.json ignored (%s); using defaults", e)
                        cfg = dict(DEFAULTS)
                    self._put_settings(cfg)
                    self._park(self.home / "config.json")
            if not self.conn.execute("SELECT 1 FROM days LIMIT 1").fetchone():
                use = self._read_json(self.home / "usage.json")
                if isinstance(use, dict):
                    try:
                        days = {k: [int(v[0]), int(v[1])] for k, v in (use.get("days") or {}).items()}
                        cal = dict(use.get("cal") or {})
                        fired = dict(use.get("fired") or {})
                        log = [a for a in (use.get("log") or []) if isinstance(a, dict)]
                        if not isinstance(cal.get("cycle"), str):
                            cal = {}
                        if cal:
                            cal = {"cycle": cal["cycle"], "offset": int(cal.get("offset", 0))}
                    except (TypeError, ValueError, IndexError, AttributeError):
                        self._quarantine(self.home / "usage.json")  # malformed: keep the file, start fresh
                        days, cal, fired, log = {}, {}, {}, []
                    self.conn.executemany("INSERT OR REPLACE INTO days VALUES (?, ?, ?)",
                                          [(k, v[0], v[1]) for k, v in days.items()])
                    if cal:
                        self.conn.execute("INSERT OR REPLACE INTO cal VALUES (?, ?)",
                                          (cal["cycle"], cal["offset"]))
                    self.conn.executemany("INSERT OR REPLACE INTO fired VALUES (?, ?)",
                                          [(k, json.dumps(v)) for k, v in fired.items()])
                    self.conn.executemany("INSERT INTO alerts (t, kind, title, msg) VALUES (?, ?, ?, ?)",
                                          [(str(a.get("t", "")), str(a.get("kind", "info")),
                                            str(a.get("title", "")), str(a.get("msg", ""))) for a in log])
                    self._park(self.home / "usage.json")

    def _load(self):
        with self.lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
            if rows:
                try:
                    self.cfg = clean_cfg({k: json.loads(v) for k, v in rows}, DEFAULTS)
                except ValueError as e:
                    logging.warning("stored settings ignored (%s); using defaults", e)
                    self.cfg = dict(DEFAULTS)
            else:
                self.cfg = dict(DEFAULTS)
                with self.conn:  # fresh install: persist the defaults so they are inspectable
                    self._put_settings(self.cfg)
            self.days = {d: [rx, tx] for d, rx, tx in self.conn.execute("SELECT day, rx, tx FROM days")}
            row = self.conn.execute("SELECT cycle, offset FROM cal LIMIT 1").fetchone()
            self.cal = {"cycle": row[0], "offset": row[1]} if row else {}
            try:
                self.fired = {k: json.loads(v) for k, v in self.conn.execute("SELECT key, value FROM fired")}
            except ValueError:
                logging.warning("fired-alert state unreadable; starting fresh")
                self.fired = {}
            self.log = [{"t": t, "kind": k, "title": ti, "msg": m} for t, k, ti, m in self.conn.execute(
                "SELECT t, kind, title, msg FROM alerts ORDER BY seq DESC LIMIT 40")]
            self.log.reverse()

    def flush(self, force=False):
        with self.lock:
            if not (self.dirty or force):
                return
            cutoff = (date.today() - timedelta(days=self.KEEP_DAYS)).isoformat()
            self.days = {k: v for k, v in self.days.items() if k >= cutoff}
            try:
                with self.conn:
                    self.conn.executemany(
                        "INSERT INTO days (day, rx, tx) VALUES (?, ?, ?) "
                        "ON CONFLICT (day) DO UPDATE SET rx = excluded.rx, tx = excluded.tx",
                        [(k, v[0], v[1]) for k, v in self.days.items()])
                    self.conn.execute("DELETE FROM days WHERE day < ?", (cutoff,))
                    self.conn.execute("DELETE FROM cal")
                    if self.cal:
                        self.conn.execute("INSERT INTO cal (cycle, offset) VALUES (?, ?)",
                                          (self.cal.get("cycle"), int(self.cal.get("offset", 0))))
                    self.conn.execute("DELETE FROM fired")
                    self.conn.executemany("INSERT INTO fired (key, value) VALUES (?, ?)",
                                          [(k, json.dumps(v)) for k, v in self.fired.items()])
                    if self._pending:
                        self.conn.executemany("INSERT INTO alerts (t, kind, title, msg) VALUES (?, ?, ?, ?)",
                                              self._pending)
                self._pending = []
                self.dirty = False
            except sqlite3.Error:
                pass  # disk full or locked: try again next time

    @property
    def gb(self):
        return 2 ** 30 if self.cfg["binary_gb"] else 10 ** 9

    @property
    def mb(self):
        return 2 ** 20 if self.cfg["binary_gb"] else 10 ** 6

    def add(self, day, rx, tx):
        with self.lock:
            d = self.days.setdefault(day, [0, 0])
            d[0] += rx
            d[1] += tx
            self.dirty = True

    def add_alert(self, kind, title, msg):
        with self.lock:
            e = {"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "title": title, "msg": msg}
            self.log.append(e)
            self.log = self.log[-40:]
            self._pending.append((e["t"], kind, title, msg))
            self.dirty = True

    def set_config(self, new):
        with self.lock:
            self.cfg = clean_cfg(new, self.cfg)
            with self.conn:
                self._put_settings(self.cfg)

    def reset_cycle(self, now=None):
        """Zero this cycle's counters and calibration; settings, past cycles and the alert list stay."""
        now = now or datetime.now()
        with self.lock:
            start, _ = cycle_bounds(now.date(), self.cfg["reset_day"])
            skey = start.isoformat()
            self.days = {k: v for k, v in self.days.items() if k < skey}
            self.cal = {}
            self.fired = {"cycle": skey, "pcts": []}  # thresholds re-arm from zero, like a fresh cycle
            with self.conn:  # flush only upserts memory rows: the cycle's rows must be deleted outright
                self.conn.execute("DELETE FROM days WHERE day >= ?", (skey,))
            self.dirty = True
            self.flush(force=True)
        return skey

    def calibrate(self, gb_used, now=None):
        """Make the app's total for this cycle equal what the carrier reports."""
        s = self.summary(now)
        with self.lock:
            self.cal = {"cycle": s["cycle_start"], "offset": int(float(gb_used) * self.gb - s["counted"])}
            self.dirty = True
        return self.summary(now)

    def summary(self, now=None):
        now = now or datetime.now()
        with self.lock:
            cfg, gb = self.cfg, self.gb
            today = now.date()
            tkey = today.isoformat()
            start, end = cycle_bounds(today, cfg["reset_day"])
            skey = start.isoformat()
            plan = cfg["plan_gb"] * gb
            budget = plan * (1 - cfg["reserve_pct"] / 100.0)
            reserve = plan - budget  # shown as part of the plan; the ring counts it as used from day one
            cyc = {k: v[0] + v[1] for k, v in self.days.items() if skey <= k <= tkey}
            counted = sum(cyc.values())
            offset = self.cal.get("offset", 0) if self.cal.get("cycle") == skey else 0
            used = max(0, counted + offset)
            today_used = cyc.get(tkey, 0)
            days_left = (end - today).days
            allowance = max(0.0, (budget - max(0, used - today_used)) / days_left)

            # Recent daily average over whole calendar days (days with no traffic count as zero).
            reliable = False
            if cyc and min(cyc) < tkey:
                yesterday = today - timedelta(days=1)
                w0 = max(date.fromisoformat(min(cyc)), yesterday - timedelta(days=6))
                n = (yesterday - w0).days + 1
                avg = sum(cyc.get((w0 + timedelta(days=i)).isoformat(), 0) for i in range(n)) / n
                reliable = n >= 2
            else:
                frac = max((now - datetime.combine(today, datetime.min.time())).total_seconds() / 86400, 0.1)
                avg = today_used / frac

            time_left = max((datetime.combine(end, datetime.min.time()) - now).total_seconds() / 86400, 0.0)
            projected = used + avg * time_left
            left_budget = budget - used  # the reserve is not spendable: the cycle ends when this hits zero
            remaining = plan - used      # what the user has left of the plan as displayed
            run_out = days_early = None
            if avg > 0 and left_budget > 0 and projected > budget:
                t = now + timedelta(days=left_budget / avg)
                run_out = t.date().isoformat()
                days_early = max(1, (end - t.date()).days)
            return {
                "now": now.isoformat(timespec="seconds"),
                "cycle_start": skey, "cycle_end": end.isoformat(), "days_left": days_left,
                "plan": plan, "budget": budget, "reserve": reserve,
                "reserve_pct": cfg["reserve_pct"],
                "counted": counted, "offset": offset, "used": used, "remaining": remaining,
                "pct": (used + reserve) / plan * 100 if plan else 0.0,
                "today": today_used, "allowance": allowance,
                "avg_daily": avg, "pace_reliable": reliable, "projected": projected,
                "run_out": run_out, "days_early": days_early,
                "unit_gb": gb, "unit_mb": self.mb,
            }
