"""Cross-platform constants and the guarded psutil import (every module pulls psutil from here)."""

import sys

if sys.version_info < (3, 8):
    sys.exit("DataGuard needs Python 3.8 or newer.")
try:
    import psutil  # noqa: F401 - re-exported: every module pulls psutil from here
except ImportError:
    sys.exit("DataGuard needs one package:  pip install psutil")

APP = "DataGuard"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # stops helper commands flashing a console window
DB_NAME = "dataguard.db"  # one database: settings, daily usage, per-app totals, alert history
