"""Plan settings: defaults, validation, the billing-cycle calendar, and where the data lives."""

import calendar
import os
from datetime import date
from pathlib import Path

from .common import APP, IS_MAC, IS_WIN

DEFAULTS = {
    "plan_gb": 60,            # size of your data plan
    "reset_day": 1,           # day of the month your plan renews
    "reserve_pct": 5,         # % of the plan kept untouched (phone overhead, rounding)
    "ssid": "",               # your phone's hotspot name; only that network is counted ("" = always count)
    "iface": "auto",          # network interface to watch, or "auto"
    "count_mode": "auto",     # "auto" = count as usual; "off" = count nothing at all
    "alert_pcts": [50, 75, 90, 100],
    "burst_mb_per_min": 150,  # warn when a download runs faster than this (0 = off)
    "binary_gb": False,       # True if your carrier counts 1 GB = 1024 MB
    "notify": True,           # desktop notifications
    "port": 8787,
}


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
    if "count_mode" in new:
        mode = str(new["count_mode"]).strip().lower()
        if mode not in ("auto", "off"):
            raise ValueError("count_mode must be 'auto' or 'off'")
        out["count_mode"] = mode
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
