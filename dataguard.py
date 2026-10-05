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

The code lives in the dataguard/ package: settings, usage (storage), live
(monitoring), frontend (dashboard page), web (server), interfaces, notify,
processes, common and cli.
"""

import sys

from dataguard.cli import main

if __name__ == "__main__":
    sys.exit(main())
