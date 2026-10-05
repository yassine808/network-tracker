"""Usage history: settings + daily byte counts in two small JSON files. Thread-safe."""

import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .settings import DEFAULTS, clean_cfg, cycle_bounds


class Store:
    """Settings + usage history in two small JSON files. Thread-safe."""

    KEEP_DAYS = 400

    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.home / "config.json"
        self.use_path = self.home / "usage.json"
        self.lock = threading.RLock()
        self.cfg = dict(DEFAULTS)
        self.days = {}     # "YYYY-MM-DD" -> [bytes received, bytes sent] counted against the plan
        self.cal = {}      # {"cycle": "YYYY-MM-DD", "offset": bytes}, set by calibrate
        self.fired = {}    # which alerts were already shown
        self.log = []      # recent alerts, newest last
        self.dirty = False
        self._load()

    def _read(self, path):
        try:
            return json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self._quarantine(path)
            return None

    @staticmethod
    def _quarantine(path):
        try:
            path.replace(path.with_name(path.name + ".bad"))  # keep the broken file, start fresh
        except OSError:
            pass

    def _write(self, path, obj, pretty=False):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2 if pretty else None), "utf-8")
        os.replace(tmp, path)

    def _load(self):
        raw = self._read(self.cfg_path)
        if isinstance(raw, dict):
            try:
                self.cfg = clean_cfg(raw, DEFAULTS)
            except ValueError as e:
                print(f"config.json ignored ({e}); using defaults")
        elif raw is None:
            try:
                self._write(self.cfg_path, self.cfg, pretty=True)  # create it so it can be edited by hand
            except OSError:
                pass
        use = self._read(self.use_path)
        if isinstance(use, dict):
            try:
                self.days = {k: [int(v[0]), int(v[1])] for k, v in (use.get("days") or {}).items()}
                self.cal = dict(use.get("cal") or {})
                self.fired = dict(use.get("fired") or {})
                self.log = list(use.get("log") or [])
            except (TypeError, ValueError, IndexError, AttributeError):
                self._quarantine(self.use_path)
                self.days, self.cal, self.fired, self.log = {}, {}, {}, []

    def flush(self, force=False):
        with self.lock:
            if not (self.dirty or force):
                return
            cutoff = (date.today() - timedelta(days=self.KEEP_DAYS)).isoformat()
            self.days = {k: v for k, v in self.days.items() if k >= cutoff}
            try:
                self._write(self.use_path, {"days": self.days, "cal": self.cal,
                                            "fired": self.fired, "log": self.log[-40:]})
                self.dirty = False
            except OSError:
                pass  # try again next time

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

    def set_config(self, new):
        with self.lock:
            self.cfg = clean_cfg(new, self.cfg)
            self._write(self.cfg_path, self.cfg, pretty=True)

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
            remaining = budget - used
            run_out = days_early = None
            if avg > 0 and remaining > 0 and projected > budget:
                t = now + timedelta(days=remaining / avg)
                run_out = t.date().isoformat()
                days_early = max(1, (end - t.date()).days)
            return {
                "now": now.isoformat(timespec="seconds"),
                "cycle_start": skey, "cycle_end": end.isoformat(), "days_left": days_left,
                "plan": plan, "budget": budget, "reserve_pct": cfg["reserve_pct"],
                "counted": counted, "offset": offset, "used": used, "remaining": remaining,
                "pct": used / budget * 100 if budget else 0.0,
                "today": today_used, "allowance": allowance,
                "avg_daily": avg, "pace_reliable": reliable, "projected": projected,
                "run_out": run_out, "days_early": days_early,
                "unit_gb": gb, "unit_mb": self.mb,
            }
