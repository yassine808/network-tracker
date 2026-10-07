#!/usr/bin/env python3
"""
DataGuard - keeps a laptop inside a phone's data plan (built for a 60 GB 4G hotspot).

  * Counts what your laptop sends/receives, but only while it is on your phone's
    hotspot (matched by Wi-Fi name), and totals it per billing cycle.
  * Dashboard at http://127.0.0.1:8787 : used / left, a safe daily allowance,
    projected run-out date, live speed, 30-day history, and (Windows) "who is using data now".
  * Desktop notifications at 50/75/90/100 % of your budget, when your pace will
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

The code lives in the dataguard/ package: settings, usage (storage), live
(monitoring), frontend (dashboard page), web (server), interfaces, notify,
processes, common and cli.
"""

import sys
import tempfile
import time
import traceback
from pathlib import Path


def _report():
    """The shortcut starts this through pythonw, which has no console: a failing launch must
    still leave the traceback somewhere readable, or the app simply looks like it never ran."""
    tb = traceback.format_exc()
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(Path(tempfile.gettempdir()) / "dataguard-startup.log", "a", encoding="utf-8") as fh:
            fh.write(f"--- {stamp}\n{tb}\n")
    except OSError:
        pass
    windowed = True  # no console of its own (pythonw / a hidden run): say it on screen too
    try:
        import ctypes
        windowed = not ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        pass
    if windowed:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "DataGuard could not start:\n\n" + tb[-1500:],
                                             "DataGuard", 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    sys.stderr.write(tb)


if __name__ == "__main__":
    try:
        from dataguard.cli import main
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        _report()
        sys.exit(1)
