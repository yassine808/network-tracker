#!/usr/bin/env python3
"""
DataGuard - keeps a laptop inside a phone's data plan (built for a 60 GB 4G hotspot).

  * Counts what your laptop sends/receives, but only while it is on your phone's
    hotspot (matched by Wi-Fi name), and totals it per billing cycle.
  * Dashboard at http://127.0.0.1:8787 : used / left, a safe daily allowance,
    projected run-out date, live speed, 30-day history, and (Windows) "who is using data now".
  * Desktop notifications at 50/75/90/100 % of the budget, when your pace will
    run you out early, and when something starts pulling data fast.

Cheap by design: one process, one dependency (psutil), wakes every 5 s (1 s while
the dashboard is open), writes to disk at most once a minute, opens no network
connections of its own, and only looks at per-app activity when you ask.

  pip install psutil
  python dataguard.py run --open        start it (and open the dashboard)
  python dataguard.py status            summary in the terminal
  python dataguard.py calibrate 12.4    "my carrier says I've used 12.4 GB"
  python dataguard.py ifaces            list network interfaces
  python dataguard.py startup install   Windows: start hidden at every login

Your carrier also counts your PHONE's own traffic, which the laptop can't see.
Calibrate every few days to stay exact.
"""

import argparse
import base64
import calendar
import collections
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if sys.version_info < (3, 8):
    sys.exit("DataGuard needs Python 3.8 or newer.")
try:
    import psutil
except ImportError:
    sys.exit("DataGuard needs one package:  pip install psutil")

APP = "DataGuard"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # stops helper commands flashing a console window

DEFAULTS = {
    "plan_gb": 60,            # size of your data plan
    "reset_day": 1,           # day of the month your plan renews
    "reserve_pct": 5,         # % of the plan kept untouched (phone overhead, rounding)
    "ssid": "",               # your phone's hotspot name; only that network is counted ("" = always count)
    "iface": "auto",          # network interface to watch, or "auto"
    "alert_pcts": [50, 75, 90, 100],
    "burst_mb_per_min": 150,  # warn when a download runs faster than this (0 = off)
    "binary_gb": False,       # True if your carrier counts 1 GB = 1024 MB
    "notify": True,           # desktop notifications
    "port": 8787,
}

VIRTUAL = re.compile(
    r"loopback|^lo$|vethernet|vmware|virtualbox|vbox|hyper-v|docker|veth|virbr|br-|^tun|^tap|"
    r"^wg\d|utun|awdl|llw|bridge|bluetooth|isatap|teredo|pseudo|npcap|tailscale|zerotier", re.I)
WIFI = re.compile(r"wi-?fi|wlan|wlp\d|wlx|wireless|^wl\d|^ath\d|^en0$", re.I)


# --------------------------------------------------------------------------- helpers

def default_home():
    if os.environ.get("DATAGUARD_HOME"):
        return Path(os.environ["DATAGUARD_HOME"])
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP


def cycle_bounds(today, reset_day):
    """(start, end) of the billing cycle containing `today`; `end` is the next renewal day."""
    def on(y, m):
        return date(y, m, min(reset_day, calendar.monthrange(y, m)[1]))
    start = on(today.year, today.month)
    if start > today:
        start = on(today.year - 1, 12) if today.month == 1 else on(today.year, today.month - 1)
    end = on(start.year + 1, 1) if start.month == 12 else on(start.year, start.month + 1)
    return start, end


def clean_cfg(new, base):
    """Validate settings (from the file or the dashboard); raises ValueError with a readable message."""
    out = dict(base)

    def num(key, lo, hi, cast=float):
        if key in new:
            try:
                v = cast(new[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            out[key] = v

    num("plan_gb", 1, 100000)
    num("reset_day", 1, 31, int)
    num("reserve_pct", 0, 50)
    num("burst_mb_per_min", 0, 100000, int)
    num("port", 1024, 65535, int)
    if "ssid" in new:
        out["ssid"] = str(new["ssid"]).strip()[:64]
    if "iface" in new:
        out["iface"] = str(new["iface"]).strip() or "auto"
    if "alert_pcts" in new:
        try:
            pcts = sorted({int(x) for x in new["alert_pcts"]})
        except (TypeError, ValueError):
            raise ValueError("alert_pcts must be a list of whole numbers")
        if len(pcts) > 8 or not all(1 <= p <= 300 for p in pcts):
            raise ValueError("alert_pcts: up to 8 values between 1 and 300")
        out["alert_pcts"] = pcts
    for key in ("binary_gb", "notify"):
        if key in new:
            out[key] = bool(new[key])
    return out


def run_quiet(cmd, timeout=4):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None


def get_ssid():
    """Wi-Fi network name: str; "" when not on Wi-Fi; None when it can't be determined."""
    if IS_WIN:
        out = run_quiet(["netsh", "wlan", "show", "interfaces"])
        if out is None:
            return None
        m = re.search(r"^[ \t]*SSID[ \t]*:[ \t]*(.+?)[ \t]*$", out, re.M)  # BSSID lines don't match; stays on one line
        return m.group(1) if m else ""
    if IS_MAC:
        out = run_quiet(["networksetup", "-getairportnetwork", "en0"])
        m = re.search(r"Network:\s*(.+?)\s*$", out or "", re.M)
        return m.group(1) if m else None  # recent macOS hides the name; treat as unknown
    out = run_quiet(["iwgetid", "-r"])
    if out is not None:
        return out.strip()
    out = run_quiet(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    if out is None:
        return None
    for line in out.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":")
    return ""


def auto_iface(names, stats, counters):
    """The interface that is probably the hotspot: the Wi-Fi adapter, else the busiest real one."""
    real = [n for n in names if not VIRTUAL.search(n)]
    wifi = [n for n in real if WIFI.search(n)]
    if wifi:
        return sorted(wifi, key=lambda n: not (n in stats and stats[n].isup))[0]
    up = [n for n in real if n in stats and stats[n].isup]
    pool = up or real or list(names)
    return max(pool, key=lambda n: counters[n].bytes_recv + counters[n].bytes_sent) if pool else None


PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:DG_TITLE)
$m = [Security.SecurityElement]::Escape($env:DG_MSG)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>")
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def notify(title, msg):
    """Fire-and-forget desktop notification; quietly does nothing if the OS can't show one."""
    try:
        kw = dict(env=dict(os.environ, DG_TITLE=title, DG_MSG=msg), stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        if IS_WIN:
            enc = base64.b64encode(PS_TOAST.encode("utf-16-le")).decode()
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                              "-EncodedCommand", enc], **kw)
        elif IS_MAC:
            subprocess.Popen(["osascript", "-e",
                              'display notification (system attribute "DG_MSG") '
                              'with title (system attribute "DG_TITLE")'], **kw)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", APP, title, msg], **kw)
    except Exception:
        pass


def io_proxy(p):
    """Windows books socket traffic as 'other' I/O, so that per-process counter doubles as a network gauge."""
    return p.io_counters().other_bytes


def top_processes(window=3.0, limit=8):
    """Windows only: rank apps by recent 'other' I/O among processes that own network sockets. An estimate,
    not exact bytes. Linux and macOS have no comparable per-process counter, so the view reports itself unavailable."""
    if not IS_WIN:
        return {"supported": False, "items": [], "blind": [], "window": window}

    def snap():
        try:
            pids = {c.pid for c in psutil.net_connections(kind="inet") if c.pid}
        except (psutil.AccessDenied, OSError):
            pids = None
        vals, blind = {}, {}
        for p in psutil.process_iter(["name"]):
            if pids is not None and p.pid not in pids:
                continue
            name = p.info.get("name") or f"pid {p.pid}"
            try:
                vals[p.pid] = (name, io_proxy(p))
            except psutil.AccessDenied:
                blind[p.pid] = name
            except (psutil.Error, AttributeError):
                pass
        return vals, blind

    before, _ = snap()
    time.sleep(window)
    after, blind = snap()
    rows = {}
    for pid, (name, v1) in after.items():
        if pid in before and v1 >= before[pid][1]:
            r = rows.setdefault(name, {"name": name, "pids": 0, "rate": 0.0})
            r["pids"] += 1
            r["rate"] += (v1 - before[pid][1]) / window
    items = sorted((r for r in rows.values() if r["rate"] >= 512), key=lambda r: -r["rate"])[:limit]
    total = sum(r["rate"] for r in rows.values()) or 1.0
    for r in items:
        r["share"] = r["rate"] / total
    return {"supported": True, "items": items, "blind": sorted(set(blind.values()))[:8], "window": window}


# --------------------------------------------------------------------------- storage

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


# --------------------------------------------------------------------------- monitor

class Monitor:
    SSID_EVERY = 30  # seconds between Wi-Fi name checks (extra check during big transfers)

    def __init__(self, store):
        self.store = store
        self.base = {}                    # nic -> (rx, tx) at the previous reading
        self.t_last = None
        self.ring = collections.deque()   # (t0, t1, rx, tx, counted) for the last 15 minutes
        self.iface = None
        self.ssid = None
        self.ssid_t = 0.0
        self.counting = None
        self.last_ui = 0.0
        self.last_burst = 0.0
        self.t_alert = 0.0
        self.t_flush = time.time()
        self.err = ""
        self.stop = threading.Event()
        self.wake = threading.Event()     # lets a freshly opened dashboard end the slow idle sleep

    # -- sampling
    def sample(self, now=None, counters=None, stats=None):
        now = time.time() if now is None else now
        counters = psutil.net_io_counters(pernic=True) if counters is None else counters
        stats = psutil.net_if_stats() if stats is None else stats
        cfg = self.store.cfg
        want = cfg["iface"]
        self.iface = want if want != "auto" and want in counters else auto_iface(list(counters), stats, counters)

        delta = {}
        for nic, c in counters.items():
            cur = (c.bytes_recv, c.bytes_sent)
            prev = self.base.get(nic)
            if prev is not None:  # a counter that went backwards was reset (adapter restart): count from zero
                delta[nic] = tuple(a - b if a >= b else a for a, b in zip(cur, prev))
            self.base[nic] = cur
        t0, self.t_last = self.t_last, now
        if t0 is None or now <= t0 or self.iface not in delta:
            return
        drx, dtx = delta[self.iface]

        if cfg["ssid"]:
            big = drx + dtx >= 5_000_000
            if now - self.ssid_t >= self.SSID_EVERY or (big and now - self.ssid_t >= 3):
                self.ssid, self.ssid_t = get_ssid(), now
            counted = not self.ssid or self.ssid == cfg["ssid"]  # name unknown or unreadable: count rather than miss data
        else:
            counted = True
        self.counting = counted

        with self.store.lock:
            self.ring.append((t0, now, drx, dtx, counted))
            while self.ring and self.ring[0][1] < now - 900:
                self.ring.popleft()
        if counted and (drx or dtx):
            self.store.add(date.fromtimestamp(now).isoformat(), drx, dtx)
            self.check_burst(now)
        if now - self.t_alert >= 10:
            self.t_alert = now
            self.check_alerts(datetime.fromtimestamp(now))
        if now - self.t_flush >= 60:
            self.t_flush = now
            self.store.flush()

    def window(self, ref, span, nbins, counted_only=False):
        """Spread recorded traffic over [ref-span, ref] in equal bins -> (rx_bins, tx_bins) in bytes."""
        w, t_start = span / nbins, ref - span
        rx, tx = [0.0] * nbins, [0.0] * nbins
        with self.store.lock:
            ring = list(self.ring)
        for t0, t1, drx, dtx, counted in ring:
            if t1 <= t_start or t1 <= t0 or (counted_only and not counted):
                continue
            dur, cur = t1 - t0, max(t0, t_start)
            i = min(int((cur - t_start) / w), nbins - 1)
            while cur < t1 and i < nbins:
                seg_end = min(t1, t_start + (i + 1) * w)
                f = (seg_end - cur) / dur
                rx[i] += drx * f
                tx[i] += dtx * f
                cur, i = seg_end, i + 1
        return rx, tx

    # -- alerts
    def alert(self, kind, title, msg):
        st = self.store
        with st.lock:
            st.log.append({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "title": title, "msg": msg})
            st.log = st.log[-40:]
            st.dirty = True
        print(f"[{time.strftime('%H:%M:%S')}] {title} - {msg}", flush=True)
        if st.cfg["notify"]:
            notify(title, msg)

    def check_alerts(self, now):
        st = self.store
        s = st.summary(now)
        g = lambda b: f"{b / st.gb:.1f} GB"
        tkey = now.date().isoformat()
        out = []
        with st.lock:
            f = st.fired
            if f.get("cycle") != s["cycle_start"]:
                f.clear()
                f.update(cycle=s["cycle_start"], pcts=[])
                st.dirty = True
            f["pcts"] = [p for p in f.get("pcts", []) if s["pct"] >= p]  # forget levels we've dropped below
            due = [p for p in st.cfg["alert_pcts"] if s["pct"] >= p and p not in f["pcts"]]
            if due:
                top = max(due)
                f["pcts"] = sorted(set(f["pcts"]) | set(due))
                st.dirty = True
                if top >= 100:
                    out.append(("limit", "Data budget used up",
                                f"{g(s['used'])} of {g(s['budget'])} used, {s['days_left']} days to renewal. "
                                "Pause big downloads and updates."))
                else:
                    out.append(("level", f"{top}% of your data used",
                                f"{g(s['used'])} of {g(s['budget'])}. {s['days_left']} days to renewal - "
                                f"about {g(s['allowance'])} a day keeps you on track."))
            if (f.get("daily") != tkey and s["allowance"] > 0 and s["today"] >= s["allowance"] and s["pct"] < 100):
                f["daily"] = tkey
                st.dirty = True
                out.append(("daily", "Today's allowance reached",
                            f"{g(s['today'])} used today (allowance {g(s['allowance'])}). Going over eats into later days."))
            if f.get("pace") != tkey and s["pace_reliable"] and s["run_out"] and s["pct"] < 100:
                f["pace"] = tkey
                st.dirty = True
                out.append(("pace", "On course to run out early",
                            f"At your recent pace the data runs out around {s['run_out']}, "
                            f"{s['days_early']} days before renewal. Aim for {g(s['allowance'])} a day."))
        for kind, title, msg in out:
            self.alert(kind, title, msg)

    def check_burst(self, ref):
        limit = self.store.cfg["burst_mb_per_min"] * self.store.mb
        if not limit or ref - self.last_burst < 600:
            return
        rx, tx = self.window(ref, 60, 1, counted_only=True)
        if rx[0] + tx[0] >= limit:
            self.last_burst = ref
            threading.Thread(target=self._burst_report, args=(rx[0] + tx[0],), daemon=True).start()

    def _burst_report(self, got):
        try:
            names = ", ".join(i["name"] for i in top_processes(window=2.0, limit=3)["items"])
        except Exception:
            names = ""
        who = f" Busiest: {names}." if names else ""
        self.alert("burst", "Heavy download in progress",
                   f"{got / self.store.mb:.0f} MB in the last minute.{who} Open the dashboard for details.")

    # -- loop + status
    def run(self):
        while not self.stop.is_set():
            try:
                self.sample()
                self.err = ""
            except Exception as e:  # keep the guard alive whatever happens
                self.err = f"{type(e).__name__}: {e}"
            self.wake.wait(1.0 if time.time() - self.last_ui < 10 else 5.0)
            self.wake.clear()

    def status(self):
        now = time.time()
        was_idle = now - self.last_ui >= 10
        self.last_ui = now  # dashboard is open: sample every second for a while
        if was_idle:
            self.wake.set()  # ...and take a reading now instead of waiting out the 5 s idle sleep
        s = self.store.summary(datetime.fromtimestamp(now))
        ref = self.ring[-1][1] if self.ring else now
        rx, tx = self.window(ref, 4, 1)  # 4 s average: reacts quickly, still smooth
        srx, stx = self.window(ref, 300, 60)
        today = date.today()
        with self.store.lock:
            hist = [{"d": (today - timedelta(days=i)).isoformat(),
                     "b": sum(self.store.days.get((today - timedelta(days=i)).isoformat(), [0, 0]))}
                    for i in range(30, -1, -1)]
            s.update(cfg=dict(self.store.cfg), alerts=list(reversed(self.store.log[-10:])))
        s.update(iface=self.iface, ssid=self.ssid, counting=self.counting, err=self.err,
                 ifaces=sorted(psutil.net_io_counters(pernic=True)),
                 down=rx[0] / 4, up=tx[0] / 4,
                 series_down=[b / 5 for b in srx], series_up=[b / 5 for b in stx], history=hist)
        return s


# --------------------------------------------------------------------------- web

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DataGuard</title>
<link rel="icon" href="data:,">
<style>
:root{color-scheme:light dark;--bg:#f4f6f9;--card:#fff;--ink:#101828;--mute:#667085;--line:#e4e7ec;--acc:#2563eb;--accink:#fff;--up:#9333ea;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--track:#e8ecf2}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#141a22;--ink:#e8edf3;--mute:#8b96a5;--line:#242d38;--acc:#5aa2ff;--accink:#06101f;--up:#c4a1ff;--ok:#3fb950;--warn:#e3a008;--bad:#f85149;--track:#243040}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:22px 16px 56px;display:grid;gap:14px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{margin:0;font-size:20px;letter-spacing:-.01em}
h2{margin:0 0 10px;font-size:11.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--mute)}
.chip{font-size:12.5px;padding:4px 11px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--mute)}
.chip.ok{color:var(--ok);border-color:var(--ok)}.chip.warn{color:var(--warn);border-color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(270px,1fr))}
.hero{display:flex;gap:26px;align-items:center;flex-wrap:wrap}
#ring{width:170px;height:170px;flex:none}
.trk,.arc{fill:none;stroke-width:10}.trk{stroke:var(--track)}
.arc{stroke:var(--ok);stroke-linecap:round;stroke-dasharray:0 400;transition:stroke-dasharray .6s,stroke .3s}
.big{font-size:24px;font-weight:700;fill:var(--ink)}
.facts{flex:1;min-width:260px;display:grid;grid-template-columns:repeat(2,minmax(120px,1fr));gap:16px 24px}
.facts span{display:block;font-size:12.5px;color:var(--mute)}
.facts b{font-size:22px;letter-spacing:-.02em}
.facts small{display:block;color:var(--mute);font-size:12.5px}
.num{font-size:28px;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--mute);font-size:13.5px}
.meter{height:8px;background:var(--track);border-radius:99px;overflow:hidden;margin:10px 0 6px}
.meter i{display:block;height:100%;width:0;background:var(--ok);border-radius:99px;transition:width .5s,background .3s}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
svg text{font-family:inherit}
#spark{width:100%;height:58px;display:block;margin-top:8px}
#spark polyline{fill:none;stroke-width:1.8;stroke-linejoin:round;vector-effect:non-scaling-stroke}
#sd{stroke:var(--acc)}#su{stroke:var(--up)}
#bars{width:100%;height:auto;display:block}
.col{fill:var(--acc);opacity:.5}.col.today{opacity:1}
.allow{stroke:var(--warn);stroke-width:1;stroke-dasharray:4 3}
.axis{font-size:10px;fill:var(--mute)}
button{font:inherit;padding:8px 14px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--acc)}button:disabled{opacity:.6;cursor:default}
button.pri{background:var(--acc);color:var(--accink);border-color:transparent;font-weight:600}
ul{list-style:none;margin:0;padding:0;display:grid;gap:10px}
li .t{font-weight:600}li small{color:var(--mute);display:block}
.row{display:grid;grid-template-columns:minmax(90px,170px) 1fr auto;gap:10px;align-items:center;font-size:14px}
.row .meter{margin:0}
details summary{cursor:pointer;font-weight:600}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:14px 0}
label{display:grid;gap:4px;font-size:13px;color:var(--mute)}
label.chk{display:flex;gap:8px;align-items:center}
input,select{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);min-width:0}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
#msg{font-size:13px;color:var(--mute)}
.hint{margin:0;font-size:13px;color:var(--mute)}
footer{font-size:12.5px;color:var(--mute)}
</style></head>
<body><main>
<header><h1>DataGuard</h1><span id="chip" class="chip">connecting&hellip;</span></header>

<section class="card hero">
  <svg id="ring" viewBox="0 0 120 120" role="img" aria-label="Share of data budget used">
    <circle class="trk" cx="60" cy="60" r="52"/>
    <circle id="arc" class="arc" cx="60" cy="60" r="52" transform="rotate(-90 60 60)"/>
    <text id="pct" class="big" x="60" y="68" text-anchor="middle">-</text>
  </svg>
  <div class="facts">
    <div><span>Used this cycle</span><b id="used">-</b><small id="plan"></small></div>
    <div><span id="remk">Left in budget</span><b id="rem">-</b><small id="remn"></small></div>
    <div><span>Days to renewal</span><b id="days">-</b><small id="renew"></small></div>
    <div><span>Safe daily allowance</span><b id="allow">-</b><small>to last until renewal</small></div>
  </div>
</section>
<p class="hint" id="hint" hidden></p>

<div class="grid">
  <section class="card"><h2>Today</h2>
    <div class="num" id="today">-</div>
    <div class="meter"><i id="tbar"></i></div>
    <div class="sub" id="tnote"></div></section>
  <section class="card"><h2>Pace</h2>
    <span class="tag" id="ptag">-</span>
    <p class="sub" id="pnote" style="margin:10px 0 0"></p></section>
  <section class="card"><h2>Live</h2>
    <div><span class="num" id="down">-</span> <span class="sub">down</span></div>
    <div class="sub"><span id="up">-</span> up<span id="nc"></span></div>
    <svg id="spark" viewBox="0 0 300 56" preserveAspectRatio="none"><polyline id="sd"/><polyline id="su"/></svg></section>
</div>

<section class="card"><h2>Last 30 days</h2><svg id="bars" viewBox="0 0 600 160"></svg></section>

<section class="card"><h2>Who's using data right now?</h2>
  <div class="actions"><button id="whoBtn" class="pri">Measure (3 s)</button>
  <span class="sub" id="whoNote">Estimated from per-app activity on this computer.</span></div>
  <div id="who" style="margin-top:12px;display:grid;gap:8px"></div></section>

<section class="card"><h2>Recent alerts</h2><ul id="alerts"></ul></section>

<section class="card"><details><summary>Settings &amp; calibration</summary>
  <div class="form">
    <label>Plan size (GB)<input id="f_plan" type="number" min="1" step="1"></label>
    <label>Renewal day of month<input id="f_day" type="number" min="1" max="31"></label>
    <label>Reserve (% kept untouched)<input id="f_res" type="number" min="0" max="50" step="1"></label>
    <label>Phone hotspot Wi-Fi name<input id="f_ssid" placeholder="empty = count the whole interface"></label>
    <label>Network interface<select id="f_iface"></select></label>
    <label>Alert at (% of budget, comma separated)<input id="f_alerts"></label>
    <label>Warn above (MB per minute, 0 = off)<input id="f_burst" type="number" min="0"></label>
    <label class="chk"><input id="f_bin" type="checkbox"> My carrier counts 1 GB = 1024 MB</label>
    <label class="chk"><input id="f_notify" type="checkbox"> Desktop notifications</label>
  </div>
  <div class="actions"><button class="pri" id="save">Save</button>
    <button id="useSsid">Use the Wi-Fi I'm on now</button>
    <button id="test">Test notification</button><span id="msg"></span></div>
  <div class="actions" style="margin-top:18px">
    <label>Carrier says I've used (GB) this cycle<input id="f_cal" type="number" min="0" step="0.1"></label>
    <button id="calBtn">Calibrate</button></div>
  <p class="sub">The laptop only sees its own traffic; your carrier also counts your phone's. Calibrate every few days
  (check your carrier's app) and the totals stay exact.</p>
</details></section>
<footer>Everything stays on this computer. DataGuard makes no internet connections of its own.</footer>
</main>
<script>
"use strict";
const $ = s => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
let U = {gb: 1e9, mb: 1e6}, filled = false, timer = 0, msgTimer = 0;

const gb = b => { const v = b / U.gb; return (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)) + " GB"; };
const sz = b => {
  const a = Math.abs(b);
  if (a >= U.gb) return (b / U.gb).toFixed(2) + " GB";
  if (a >= U.mb) return (b / U.mb).toFixed(a >= 10 * U.mb ? 0 : 1) + " MB";
  return Math.round(b / (U.mb / 1000)) + " KB";
};
const dstr = iso => new Date(iso + "T12:00:00").toLocaleDateString(undefined, {month: "short", day: "numeric"});
const txt = (sel, t) => { $(sel).textContent = t; };
const tone = p => p >= 90 ? "var(--bad)" : p >= 75 ? "var(--warn)" : "var(--ok)";
const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const msg = t => { txt("#msg", t); clearTimeout(msgTimer); msgTimer = setTimeout(() => txt("#msg", ""), 6000); };

function spark(down, up) {
  const W = 300, H = 56, max = Math.max(50000, ...down, ...up);
  const pts = a => a.map((v, i) => (i * W / (a.length - 1)).toFixed(1) + "," + (H - 2 - v / max * (H - 6)).toFixed(1)).join(" ");
  $("#sd").setAttribute("points", pts(down));
  $("#su").setAttribute("points", pts(up));
}

function bars(hist, allow) {
  const el = $("#bars"); el.replaceChildren();
  const W = 600, H = 160, pad = 16, n = hist.length, y0 = H - pad, bw = W / n;
  const top = Math.max(allow || 0, ...hist.map(h => h.b), 1) * 1.12;
  hist.forEach((h, i) => {
    const bh = h.b / top * (y0 - 4);
    const r = svg("rect", {class: i === n - 1 ? "col today" : "col", x: (i * bw + 1.5).toFixed(1), y: (y0 - bh).toFixed(1),
      width: Math.max(bw - 3, 1).toFixed(1), height: Math.max(bh, h.b > 0 ? 1 : 0).toFixed(1), rx: 2});
    const t = svg("title", {}); t.textContent = dstr(h.d) + ": " + sz(h.b); r.appendChild(t); el.appendChild(r);
  });
  if (allow > 0) {
    const y = (y0 - allow / top * (y0 - 4)).toFixed(1);
    el.appendChild(svg("line", {class: "allow", x1: 0, x2: W, y1: y, y2: y}));
  }
  [[0, "start", 0], [Math.floor(n / 2), "middle", W / 2], [n - 1, "end", W]].forEach(([i, a, x]) => {
    const t = svg("text", {class: "axis", x: x, y: H - 3, "text-anchor": a});
    t.textContent = dstr(hist[i].d); el.appendChild(t);
  });
}

function fillForm(s) {
  const c = s.cfg;
  $("#f_plan").value = c.plan_gb; $("#f_day").value = c.reset_day; $("#f_res").value = c.reserve_pct;
  $("#f_ssid").value = c.ssid; $("#f_alerts").value = c.alert_pcts.join(", "); $("#f_burst").value = c.burst_mb_per_min;
  $("#f_bin").checked = c.binary_gb; $("#f_notify").checked = c.notify;
  const sel = $("#f_iface"); sel.replaceChildren();
  ["auto"].concat(s.ifaces).forEach(n => sel.appendChild(mk("option", "", n === "auto" ? "auto (" + (s.iface || "?") + ")" : n)));
  Array.from(sel.options).forEach(o => { o.value = o.textContent.startsWith("auto (") ? "auto" : o.textContent; });
  sel.value = c.iface; filled = true;
}

function render(s) {
  U = {gb: s.unit_gb, mb: s.unit_mb};
  const C = 2 * Math.PI * 52, p = Math.max(0, Math.min(s.pct, 100));
  const arc = $("#arc");
  arc.style.strokeDasharray = (C * p / 100) + " " + C;
  arc.style.stroke = tone(s.pct);
  txt("#pct", Math.round(s.pct) + "%");
  txt("#used", sz(s.used));
  txt("#plan", "of " + gb(s.budget) + " budget (plan " + gb(s.plan) + ")");
  const left = s.budget - s.used;
  txt("#remk", left >= 0 ? "Left in budget" : "Over budget by");
  txt("#rem", sz(Math.abs(left)));
  txt("#remn", s.reserve_pct ? gb(s.plan - s.budget) + " reserve kept on top" : "");
  txt("#days", String(s.days_left));
  txt("#renew", "renews " + dstr(s.cycle_end));
  txt("#allow", gb(s.allowance));

  const chip = $("#chip");
  if (s.err) { chip.className = "chip warn"; chip.textContent = "Error: " + s.err; }
  else if (s.counting === null) { chip.className = "chip"; chip.textContent = "Starting\u2026"; }
  else if (s.counting) { chip.className = "chip ok"; chip.textContent = "Counting \u00b7 " + (s.cfg.ssid || s.iface || "?"); }
  else { chip.className = "chip"; chip.textContent = "Not counted \u00b7 on " + (s.ssid ? "\u201c" + s.ssid + "\u201d" : "another network"); }

  txt("#today", sz(s.today));
  const r = s.allowance > 0 ? s.today / s.allowance : (s.today > 0 ? 2 : 0);
  const tb = $("#tbar"); tb.style.width = Math.min(100, r * 100) + "%"; tb.style.background = tone(r * 100);
  txt("#tnote", s.allowance <= 0 ? "No allowance left this cycle"
    : s.today <= s.allowance ? sz(s.allowance - s.today) + " left of today's " + gb(s.allowance)
    : "Over today's " + gb(s.allowance) + " allowance by " + sz(s.today - s.allowance));

  const tag = $("#ptag"), note = $("#pnote");
  if (s.pct >= 100) { tag.className = "tag bad"; tag.textContent = "Over budget"; note.textContent = "The " + gb(s.budget) + " budget is used up with " + s.days_left + " days to go. Keep to essentials."; }
  else if (s.run_out) { tag.className = "tag bad"; tag.textContent = "Over pace"; note.textContent = "At ~" + gb(s.avg_daily) + "/day you'd run out around " + dstr(s.run_out) + ", " + s.days_early + " days early. Aim for " + gb(s.allowance) + "/day."; }
  else if (s.pace_reliable) { tag.className = "tag ok"; tag.textContent = "On track"; note.textContent = "At ~" + gb(s.avg_daily) + "/day you'll finish the cycle near " + gb(s.projected) + " (" + Math.round(100 * s.projected / s.budget) + "% of budget)."; }
  else { tag.className = "tag"; tag.textContent = "Learning"; note.textContent = "Needs a couple of full days of data before it can predict your pace."; }

  txt("#down", sz(s.down) + "/s"); txt("#up", sz(s.up) + "/s");
  txt("#nc", s.counting === false ? "  \u00b7  not counted" : "");
  spark(s.series_down, s.series_up);
  bars(s.history, s.allowance);

  const ul = $("#alerts"); ul.replaceChildren();
  if (!s.alerts.length) ul.appendChild(mk("li", "sub", "Nothing yet."));
  s.alerts.forEach(a => {
    const li = mk("li"); li.append(mk("span", "t", a.title), mk("small", "", a.msg + " \u00b7 " + new Date(a.t).toLocaleString()));
    ul.appendChild(li);
  });

  const tips = [];
  if (!s.cfg.ssid) tips.push("Tip: open Settings and enter your phone's hotspot name so only that traffic is counted.");
  if (!s.offset) tips.push("Tip: your phone's own traffic isn't visible here. Calibrate with your carrier's figure (Settings) to keep totals exact.");
  const hint = $("#hint"); hint.hidden = !tips.length; hint.textContent = tips[0] || "";
  if (!filled) fillForm(s);
}

async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

async function who() {
  const b = $("#whoBtn"), box = $("#who"); b.disabled = true; b.textContent = "Measuring\u2026";
  try {
    const j = await (await fetch("/api/top")).json();
    if (j.error) throw new Error(j.error);
    box.replaceChildren();
    txt("#whoNote", !j.supported ? "The per-app view only works on Windows."
      : !j.items.length ? "Nothing noticeable is using the network right now."
      : "Estimated from per-app activity over " + j.window + " s \u2014 relative, not exact bytes.");
    j.items.forEach(it => {
      const row = mk("div", "row"), m = mk("div", "meter"), i = mk("i");
      i.style.width = Math.max(3, it.share * 100) + "%"; m.appendChild(i);
      row.append(mk("span", "", it.name + (it.pids > 1 ? " \u00d7" + it.pids : "")), m, mk("span", "sub", "\u2248 " + sz(it.rate) + "/s"));
      box.appendChild(row);
    });
    if (j.blind && j.blind.length) box.appendChild(mk("div", "sub", "Couldn't read: " + j.blind.join(", ") + ". Run DataGuard as administrator to include system services such as Windows Update."));
  } catch (e) { txt("#whoNote", "Couldn't measure: " + e.message); }
  b.disabled = false; b.textContent = "Measure again";
}

$("#whoBtn").onclick = who;
$("#save").onclick = async () => {
  try {
    await post("/api/config", {
      plan_gb: +$("#f_plan").value, reset_day: +$("#f_day").value, reserve_pct: +$("#f_res").value,
      ssid: $("#f_ssid").value, iface: $("#f_iface").value,
      alert_pcts: $("#f_alerts").value.split(",").map(x => parseInt(x, 10)).filter(x => x > 0),
      burst_mb_per_min: +$("#f_burst").value, binary_gb: $("#f_bin").checked, notify: $("#f_notify").checked});
    filled = false; msg("Saved."); tick();
  } catch (e) { msg(e.message); }
};
$("#useSsid").onclick = async () => {
  try {
    const j = await (await fetch("/api/ssid")).json();
    if (j.ssid) { $("#f_ssid").value = j.ssid; msg("Filled in \u201c" + j.ssid + "\u201d \u2014 press Save."); }
    else msg(j.ssid === "" ? "You're not on Wi-Fi right now." : "Couldn't read the Wi-Fi name on this system.");
  } catch (e) { msg(e.message); }
};
$("#test").onclick = async () => { try { await post("/api/notify-test", {}); msg("Sent \u2014 look for a notification."); } catch (e) { msg(e.message); } };
$("#calBtn").onclick = async () => {
  const v = parseFloat($("#f_cal").value);
  if (!(v >= 0)) return msg("Enter the GB your carrier reports.");
  try { await post("/api/calibrate", {gb: v}); msg("Calibrated."); tick(); } catch (e) { msg(e.message); }
};

async function tick() {
  try { render(await (await fetch("/api/status", {cache: "no-store"})).json()); }
  catch (e) { const c = $("#chip"); c.className = "chip warn"; c.textContent = "Can't reach DataGuard"; console.error(e); }
  clearTimeout(timer);
  if (!document.hidden) timer = setTimeout(tick, 2000);   // no polling while the tab is hidden
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); tick(); } });
tick();
</script></body></html>
"""

TOP_LOCK = threading.Lock()


class Server(ThreadingHTTPServer):
    allow_reuse_address = not IS_WIN  # on Windows this flag would let a second copy hijack the port
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    mon = None  # set before serving

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host in ("127.0.0.1", "localhost"):
            return True
        self._json({"error": "bad host"}, 403)  # blocks DNS-rebinding tricks from web pages
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                data = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/status":
                self._json(self.mon.status())
            elif path == "/api/top":
                if not TOP_LOCK.acquire(blocking=False):
                    return self._json({"error": "already measuring"}, 429)
                try:
                    self._json(top_processes(3.0, 8))
                finally:
                    TOP_LOCK.release()
            elif path == "/api/ssid":
                self._json({"ssid": get_ssid()})
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._host_ok():
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json({"error": "JSON only"}, 415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                return self._json({"error": "request too large"}, 413)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            path, st = self.path.split("?", 1)[0], self.mon.store
            if path == "/api/config":
                st.set_config(body)
                self.mon.ssid_t = 0.0  # re-read the Wi-Fi name right away
            elif path == "/api/calibrate":
                gb_used = float(body.get("gb", -1))
                if not 0 <= gb_used <= 100000:
                    raise ValueError("gb must be a number from 0 up")
                st.calibrate(gb_used)
            elif path == "/api/notify-test":
                notify(APP, "Notifications are working.")
            else:
                return self._json({"error": "not found"}, 404)
            self._json({"ok": True})
        except (BrokenPipeError, ConnectionError):
            pass
        except (ValueError, TypeError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


# --------------------------------------------------------------------------- command line

def api(port, path, payload=None):
    """Talk to a running DataGuard. Returns parsed JSON, or None if nothing is listening."""
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except HTTPError as e:
        try:
            reason = json.loads(e.read()).get("error", e.reason)
        except Exception:
            reason = e.reason
        sys.exit(f"DataGuard says: {reason}")
    except (URLError, OSError, ValueError):
        return None


def print_status(s, running):
    g = lambda b: f"{b / s['unit_gb']:.1f} GB"
    print(f"DataGuard - {'running' if running else 'not running (showing saved data)'}")
    print(f"  Cycle     {s['cycle_start']} to {s['cycle_end']}  ({s['days_left']} days left)")
    print(f"  Used      {g(s['used'])} of {g(s['budget'])} budget ({s['pct']:.0f}%), plan {g(s['plan'])}")
    left = s["budget"] - s["used"]
    print(f"  Left      {g(left) if left >= 0 else 'over budget by ' + g(-left)}")
    print(f"  Today     {g(s['today'])} of a {g(s['allowance'])} daily allowance")
    if s["run_out"]:
        pace = f"~{g(s['avg_daily'])}/day -> runs out around {s['run_out']} ({s['days_early']} days early)"
    elif s["pace_reliable"]:
        pace = f"~{g(s['avg_daily'])}/day -> about {g(s['projected'])} by renewal, on track"
    else:
        pace = "still learning (needs a couple of full days)"
    print(f"  Pace      {pace}")
    if running:
        print(f"  Network   {s.get('iface')} - {'counting' if s.get('counting') else 'NOT counting (different Wi-Fi)'}")


def cmd_run(args, home):
    for name in ("stdout", "stderr"):  # pythonw has no console streams
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    store = Store(home)
    port = args.port or store.cfg["port"]
    url = f"http://127.0.0.1:{port}"
    mon = Monitor(store)
    Handler.mon = mon
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError:
        print(f"Port {port} is busy - DataGuard is probably already running: {url}")
        if args.open:
            webbrowser.open(url)
        return 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):
        pass
    print(f"DataGuard running. Dashboard: {url}   (Ctrl+C to stop)", flush=True)
    if args.open:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        mon.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        store.flush(force=True)
    return 0


def cmd_status(store):
    s = api(store.cfg["port"], "/api/status")
    print_status(s if s else store.summary(), running=bool(s))


def cmd_calibrate(store, gb_used):
    if api(store.cfg["port"], "/api/calibrate", {"gb": gb_used}) is None:  # not running: edit the files directly
        store.calibrate(gb_used)
        store.flush(force=True)
    print(f"Calibrated: this cycle now reads {gb_used:g} GB.")


def cmd_ifaces(store):
    counters, stats = psutil.net_io_counters(pernic=True), psutil.net_if_stats()
    chosen = store.cfg["iface"]
    if chosen == "auto" or chosen not in counters:
        chosen = auto_iface(list(counters), stats, counters)
    for name, c in sorted(counters.items()):
        up = "up" if name in stats and stats[name].isup else "down"
        kind = "wifi" if WIFI.search(name) else ("virtual" if VIRTUAL.search(name) else "")
        print(f"{'*' if name == chosen else ' '} {name:<30} {up:<5} {kind:<8} "
              f"down {c.bytes_recv / 1e9:8.2f} GB   up {c.bytes_sent / 1e9:8.2f} GB")
    print("\n* = the interface DataGuard is counting. Change it in the dashboard settings.")


def cmd_startup(action):
    if not IS_WIN:
        print("Auto-start is built in for Windows only. On macOS add this to Login Items; on Linux use a "
              f"systemd user service:  {sys.executable} {Path(__file__).resolve()} run")
        return
    path = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "DataGuard.vbs")
    if action == "remove":
        path.unlink(missing_ok=True)
        print("Removed from startup.")
        return
    exe = Path(sys.executable)
    exe = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    script = Path(__file__).resolve()
    path.write_text(f'CreateObject("WScript.Shell").Run """{exe}"" ""{script}"" run", 0, False\r\n')
    print(f"DataGuard will start hidden at every login.\nStart it now by double-clicking: {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dataguard", description="Keep your laptop inside your phone's data plan.")
    ap.add_argument("--home", help="folder for settings and history (default: your app-data folder)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="start monitoring + dashboard (default)")
    p.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    p.add_argument("--port", type=int, help="dashboard port (default 8787)")
    sub.add_parser("status", help="print a summary")
    p = sub.add_parser("calibrate", help="sync with the figure your carrier reports")
    p.add_argument("gb", type=float, help="GB your carrier says you've used this cycle")
    sub.add_parser("ifaces", help="list network interfaces")
    p = sub.add_parser("startup", help="Windows: start at every login")
    p.add_argument("action", choices=["install", "remove"])
    args = ap.parse_args(argv)

    home = Path(args.home) if args.home else default_home()
    cmd = args.cmd or "run"
    if cmd == "run":
        args.open, args.port = getattr(args, "open", False), getattr(args, "port", None)
        return cmd_run(args, home)
    if cmd == "startup":
        return cmd_startup(args.action)
    store = Store(home)
    if cmd == "status":
        cmd_status(store)
    elif cmd == "calibrate":
        cmd_calibrate(store, args.gb)
    elif cmd == "ifaces":
        cmd_ifaces(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
