"""Command line: run, status, calibrate, ifaces, startup."""

import argparse
import json
import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .apps import AppStore, AppTracker
from .common import IS_WIN, psutil
from .interfaces import WIFI, VIRTUAL, auto_iface
from .live import Monitor
from .settings import default_home
from .usage import Store
from .web import Handler, Server

ENTRY_SCRIPT = Path(__file__).resolve().parents[1] / "dataguard.py"  # what startup must launch


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
    tracker = AppTracker(AppStore(home))
    Handler.tracker = tracker
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tracker.start()
    mon_t = threading.Thread(target=mon.run, daemon=True)
    mon_t.start()
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):
        pass
    print(f"DataGuard running. Dashboard: {url}   (Ctrl+C to stop)", flush=True)
    from . import shell
    try:
        shell.run(url, open_now=args.open)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        mon.stop.set()
        mon.wake.set()
        mon_t.join(2)
        tracker.stop()
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
              f"systemd user service:  {sys.executable} {ENTRY_SCRIPT} run")
        return
    path = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "DataGuard.vbs")
    if action == "remove":
        path.unlink(missing_ok=True)
        print("Removed from startup.")
        return
    exe = Path(sys.executable)
    exe = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    script = ENTRY_SCRIPT
    path.write_text(f'CreateObject("WScript.Shell").Run """{exe}"" ""{script}"" run", 0, False\r\n')
    print(f"DataGuard will start hidden at every login.\nStart it now by double-clicking: {path}")


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

