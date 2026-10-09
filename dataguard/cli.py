"""Command line: run, status, calibrate, ifaces, startup."""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .apps import AppStore, AppTracker
from .common import IS_WIN, psutil
from .interfaces import WIFI, VIRTUAL, auto_iface
from .live import Monitor
from .settings import default_home
from .usage import Store
from .web import Handler, Server
from . import __version__

ENTRY_SCRIPT = Path(__file__).resolve().parents[1] / "dataguard.py"  # what startup must launch
LOG_CAP = 16 * 1024 * 1024  # dataguard.log never grows past this
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # where Windows keeps logon apps
RUN_NAME = "DataGuard"
PYTHONW = "pythonw.exe"  # the console-less interpreter: hidden launches must use it


class SmallLog(RotatingFileHandler):
    """Rotates by truncating the same file: no .1 backup, total stays under maxBytes."""

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            open(self.baseFilename, "w").close()  # restart fresh instead of keeping a copy
        except OSError:
            pass
        if not self.delay:
            self.stream = self._open()


def _safe_home(home):
    """--home arrives straight from argv: NUL/newline characters can smuggle a path past
    mkdir/open, so reject them, then resolve. The resolved folder is the base the app owns;
    the log path is a fixed literal joined to it, so it provably stays inside."""
    text = os.fspath(home)
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError(f"invalid home path: {text!r}")
    return Path(text).resolve()


def setup_logging(home):
    """Everything worth keeping goes to dataguard.log in the home folder, plus the console."""
    home = _safe_home(home)
    # the home folder does not exist until something makes it, and logging is the first thing
    # that runs: a first launch would otherwise die here, silently (pythonw has no stderr)
    home.mkdir(parents=True, exist_ok=True)
    file_h = SmallLog(str(home / "dataguard.log"), maxBytes=LOG_CAP, backupCount=0, encoding="utf-8")
    file_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    con_h = logging.StreamHandler(sys.stdout)
    con_h.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # per-tick sampler detail goes to dataguard.log
    root.addHandler(file_h)
    root.addHandler(con_h)


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
    except (OSError, ValueError):  # URLError/HTTPError derive from OSError
        return None


def _ask_open(port):
    """A second `run --open`: ask the copy that owns the port to raise its window.
    True = it did. False = fall back to the browser (old build, browser-mode copy,
    or some other app owns the port)."""
    req = Request(f"http://127.0.0.1:{port}/api/open", data=b"{}",
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as r:
            return bool(json.loads(r.read()).get("ok"))
    except (OSError, ValueError):  # HTTPError/URLError derive from OSError
        return False


def print_status(s, running):
    def g(b):
        return f"{b / s['unit_gb']:.1f} GB"
    print(f"DataGuard - {'running' if running else 'not running (showing saved data)'}")
    print(f"  Cycle     {s['cycle_start']} to {s['cycle_end']}  ({s['days_left']} days left)")
    print(f"  Used      {g(s['used'])} of {g(s['plan'])} plan ({s['pct']:.0f}%)")
    left = s["plan"] - s["used"]
    print(f"  Left      {g(left) if left >= 0 else 'over plan by ' + g(-left)}")
    print(f"  Today     {g(s['today'])} of a {g(s['allowance'])} daily allowance")
    if s["run_out"]:
        pace = f"~{g(s['avg_daily'])}/day -> runs out around {s['run_out']} ({s['days_early']} days early)"
    elif s["pace_reliable"]:
        pace = f"~{g(s['avg_daily'])}/day -> about {g(s['projected'])} by renewal, on track"
    else:
        pace = "still learning (needs a couple of full days)"
    print(f"  Pace      {pace}")
    if running:
        mode = s.get("cfg", {}).get("count_mode", "auto")
        if mode == "off":
            note = "counting off"
        elif s.get("counting"):
            note = "counting"
        else:
            note = "NOT counting (different Wi-Fi)"
        print(f"  Network   {s.get('iface')} - {note}")


def _seal_targets():
    """The installed layout's own interpreter(s), or [] in a dev run (that Python is shared
    with every other tool on this machine and must never be sealed)."""
    if not IS_WIN:
        return []
    here = Path(__file__).resolve().parent.parent  # the folder holding dataguard.py
    exe_dir = Path(sys.executable).resolve().parent
    if exe_dir != here:
        return []
    return [str(p) for p in (exe_dir / "python.exe", exe_dir / PYTHONW) if p.is_file()]


def _bind_server(port):
    """Claim the dashboard port, retrying while a copy that is still shutting down holds it.
    Returns the server, or None if something else owns the port."""
    tries = 8 if os.environ.get("DG_RESTART") else 1
    for attempt in range(tries):
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError:
            if attempt < tries - 1:
                time.sleep(0.75)
    return None


def _announce_busy(port, url, open_window):
    """The port is taken: say so, and give the running copy a chance to show its window."""
    print(f"Port {port} is busy - DataGuard is probably already running: {url}")
    if open_window and not _ask_open(port):
        webbrowser.open(url)  # the running copy could not show a window


def _nic_bytes_fn(mon):
    """Build the tracker's nic callback: bytes the counted NIC moved since this tracker's
    previous tick (Overview = ground truth). Differencing the cumulative counter keeps both
    sides on the tracker's own window; a reset/jump (total went backwards) reports None so
    apps record raw."""
    nic_prev = [None]

    def nic_bytes():
        tot = mon.nic_total()
        prev, nic_prev[0] = nic_prev[0], tot
        if tot is None or prev is None or tot < prev:
            return None
        return tot - prev

    return nic_bytes


def _totals_today_fn(mon):
    """Build the tracker's totals callback: the meter's per-day byte totals for this cycle -
    ground truth for the Apps' reconciliation."""
    def totals_today():
        from .settings import cycle_bounds
        start, _ = cycle_bounds(date.today(), mon.store.cfg["reset_day"])
        with mon.store.lock:
            days = {k: sum(v) for k, v in mon.store.days.items() if k >= start.isoformat()}
        return {"start": start.isoformat(), "days": days}

    return totals_today


def _heartbeat_loop(mon, tracker):
    """A one-line state summary every 5 minutes: the log should answer 'is it alive
    and what is it seeing' without a debugger."""
    while True:
        time.sleep(300)
        try:
            s = mon.store.summary()
            logging.info("heartbeat: used=%.2f GB today=%.2f MB counting=%s iface=%s "
                         "ssid=%s blocked=%d tracker_err=%r mon_err=%r",
                         s["used"] / mon.store.gb, s["today"] / mon.store.mb,
                         mon.counting, mon.iface, mon.ssid,
                         len(tracker.blocked),
                         tracker.err, mon.err)
        except Exception as e:
            logging.warning("heartbeat: %s", e)


def _seal_firewall():  # installed app only: keep DataGuard itself off the internet (loopback kept)
    from . import firewall
    ok, why = firewall.seal_app(_seal_targets())
    if not ok and why:
        logging.warning("firewall: DataGuard is not sealed off the internet: %s", why)


def cmd_run(args, home):
    for name in ("stdout", "stderr"):  # pythonw has no console streams
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    setup_logging(home)  # before Store(): migration warnings belong in the log too
    store = Store(home)
    port = args.port or store.cfg["port"]
    url = f"http://127.0.0.1:{port}"
    logging.info("cli: DataGuard %s starting (home=%s, python=%s, port=%d)",
                 __version__, home, sys.version.split()[0], port)
    logging.info("cli: config reset_day=%s iface=%s ssid=%s count_mode=%s notify=%s "
                 "alert_pcts=%s burst_mb_per_min=%s plan_gb=%s",
                 store.cfg.get("reset_day"), store.cfg.get("iface"), store.cfg.get("ssid"),
                 store.cfg.get("count_mode"), store.cfg.get("notify"),
                 store.cfg.get("alert_pcts"), store.cfg.get("burst_mb_per_min"),
                 store.cfg.get("plan_gb"))
    mon = Monitor(store)
    Handler.mon = mon
    server = _bind_server(port)
    if server is None:
        _announce_busy(port, url, args.open)
        return
    # allowed: the meter's network flag - None until known, True only on the configured Wi-Fi
    tracker = AppTracker(AppStore(home), allowed=lambda: mon.counting)
    Handler.tracker = tracker
    tracker.nic_fn = _nic_bytes_fn(mon)
    tracker.totals_fn = _totals_today_fn(mon)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tracker.start()
    mon_t = threading.Thread(target=mon.run, daemon=True)
    mon_t.start()
    logging.info("cli: web server, app tracker (interval %ds) and monitor threads started",
                 tracker.INTERVAL)
    threading.Thread(target=_heartbeat_loop, args=(mon, tracker), daemon=True,
                     name="dataguard-heartbeat").start()
    if _seal_targets():
        threading.Thread(target=_seal_firewall, daemon=True, name="dataguard-seal").start()
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):
        pass
    logging.info("DataGuard running. Dashboard: %s   (Ctrl+C to stop)", url)
    from . import shell
    try:
        shell.run(url, open_now=args.open)
    finally:
        logging.info("cli: shutting down")
        mon.stop.set()
        mon.wake.set()
        mon_t.join(2)
        tracker.stop()
        store.flush(force=True)
        logging.info("cli: stopped cleanly")


def cmd_status(store):
    s = api(store.cfg["port"], "/api/status")
    print_status(s if s else store.summary(), running=bool(s))


def cmd_calibrate(store, gb_used):
    if not 0 <= gb_used <= 100000:
        sys.exit("calibrate: gb must be a number from 0 up")
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
        if WIFI.search(name):
            kind = "wifi"
        elif VIRTUAL.search(name):
            kind = "virtual"
        else:
            kind = ""
        print(f"{'*' if name == chosen else ' '} {name:<30} {up:<5} {kind:<8} "
              f"down {c.bytes_recv / 1e9:8.2f} GB   up {c.bytes_sent / 1e9:8.2f} GB")
    print("\n* = the interface DataGuard is counting. Change it in the dashboard settings.")


def cmd_startup(action):
    if not IS_WIN:
        print("Auto-start is built in for Windows only. On macOS add this to Login Items; on Linux use a "
              f"systemd user service:  {sys.executable} {ENTRY_SCRIPT} run")
        return
    import winreg
    # every Windows app (Steam, Discord...) autostarts from HKCU's Run key; pre-1.2 builds
    # used a Startup-folder .vbs instead — clear it so an upgrade never launches twice
    legacy = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
              / "Startup" / "DataGuard.vbs")
    legacy.unlink(missing_ok=True)
    if action == "remove":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_NAME)
            print("Removed from startup.")
        except FileNotFoundError:
            print("DataGuard is not in startup.")
        return
    exe = Path(sys.executable)
    exe = exe.with_name(PYTHONW) if exe.with_name(PYTHONW).exists() else exe
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, f'"{exe}" "{ENTRY_SCRIPT}" run')
    print("DataGuard will start hidden at every login.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dataguard", description="Keep your laptop inside your phone's data plan.")
    ap.add_argument("--home", help="folder for settings and history (default: your app-data folder)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="start monitoring + dashboard (default)")
    p.add_argument("--open", action="store_true", help="open the dashboard window")
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
        cmd_run(args, home)
        return 0
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
