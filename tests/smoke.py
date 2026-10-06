"""Fast checks that must pass before any release: pricing math, strict-SSID gating and
dashboard syntax. Run: python tests/smoke.py (exit 0 = all good)."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataguard import __version__
from dataguard import live as live_mod
from dataguard.live import Monitor
from dataguard.settings import DEFAULTS, clean_cfg, cycle_bounds
from dataguard.usage import Store

FAILS = []


def check(ok, name, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  <- " + detail))
    if not ok:
        FAILS.append(name)


def test_imports():
    """Every module the app needs at startup imports with just psutil installed."""
    import importlib

    for mod in ("dataguard.settings", "dataguard.usage", "dataguard.live",
                "dataguard.web", "dataguard.cli"):
        try:
            importlib.import_module(mod)
            check(True, "import " + mod)
        except Exception as e:
            check(False, "import " + mod, f"{type(e).__name__}: {e}")
    check(bool(re.match(r"^\d+\.\d+\.\d+$", __version__)), "package: a release version is set",
          __version__)


def test_settings():
    """Validation keeps bad numbers out and the billing cycle follows reset_day."""
    out = clean_cfg({"plan_gb": 120, "ssid": "  Home 5G  ", "alert_pcts": [90, 50, 90]}, DEFAULTS)
    check(out["plan_gb"] == 120, "settings: plan accepted", str(out["plan_gb"]))
    check(out["ssid"] == "Home 5G", "settings: ssid trimmed", repr(out["ssid"]))
    check(out["alert_pcts"] == [50, 90], "settings: alert levels sorted, no dupes", str(out["alert_pcts"]))
    try:
        clean_cfg({"plan_gb": 0}, DEFAULTS)
        check(False, "settings: plan below 1 GB rejected")
    except ValueError:
        check(True, "settings: plan below 1 GB rejected")
    start, end = cycle_bounds(date(2026, 10, 6), 1)
    check((start.isoformat(), end.isoformat()) == ("2026-10-01", "2026-11-01"),
          "settings: the cycle holds reset_day", f"{start}..{end}")


def test_summary(tmp):
    """The reserve is a share of the plan counted in the ring, never subtracted from it."""
    st = Store(tmp)
    try:
        st.set_config({"plan_gb": 60, "reserve_pct": 5})
        gb = st.gb
        s = st.summary()
        check(abs(s["plan"] - 60 * gb) <= 1, "summary: plan stays whole", str(s["plan"]))
        check(abs(s["budget"] - 57 * gb) <= 1, "summary: budget = plan minus 5%", str(s["budget"]))
        check(abs(s["reserve"] - 3 * gb) <= 1, "summary: reserve = the 5% held back", str(s["reserve"]))
        st.add(date.today().isoformat(), 10 * gb, 0)
        s = st.summary()
        check(abs(s["used"] - 10 * gb) <= 1, "summary: usage recorded", str(s["used"]))
        check(abs(s["pct"] - 13 / 60 * 100) < 0.05, "summary: ring counts used + reserve",
              str(round(s["pct"], 3)))
        check(s["remaining"] >= 49.999 * gb, "summary: remaining ignores the reserve",
              str(s["remaining"]))
    finally:
        st.conn.close()


def test_monitor(tmp):
    """Only the configured hotspot is counted: everything else records nothing and alerts nothing."""
    st = Store(tmp)
    orig_ssid = live_mod.get_ssid
    try:
        st.set_config({"ssid": "HOTSPOT-TEST", "iface": "nic1", "burst_mb_per_min": 0})
        mon = Monitor(st)
        mon.check_kill = lambda *a, **k: None  # the firewall never runs during a test
        evaluated = []
        mon.check_alerts = lambda now: evaluated.append(now)

        def counters(rx, tx):
            return {"nic1": SimpleNamespace(bytes_recv=rx, bytes_sent=tx),
                    "nic2": SimpleNamespace(bytes_recv=0, bytes_sent=0)}

        t = time.time()
        live_mod.get_ssid = lambda: "OTHER-NET"
        mon.sample(now=t, counters=counters(0, 0), stats={})
        mon.sample(now=t + 11, counters=counters(5_000_000, 1_000_000), stats={})
        check(mon.counting is False, "monitor: a different network is not counted", str(mon.counting))
        check(not st.days, "monitor: nothing recorded off the hotspot", str(st.days))
        check(not evaluated, "monitor: no alerts while off the hotspot", str(len(evaluated)))

        live_mod.get_ssid = lambda: "HOTSPOT-TEST"
        mon.sample(now=t + 22, counters=counters(11_000_000, 1_700_000), stats={})
        today = date.today().isoformat()
        check(mon.counting is True, "monitor: the configured hotspot is counted", str(mon.counting))
        check(st.days.get(today) == [6_000_000, 700_000],
              "monitor: only on-network bytes are recorded", str(st.days))
        check(len(evaluated) == 1, "monitor: alerts run on the hotspot", str(len(evaluated)))
    finally:
        live_mod.get_ssid = orig_ssid
        st.conn.close()


def test_calibrate_cli(tmp):
    """The CLI rejects the same out-of-range figures the dashboard API rejects, before any call."""
    from dataguard import cli as cli_mod

    st = Store(tmp)
    orig_api = cli_mod.api
    try:
        for bad in (-1, 100001):
            try:
                cli_mod.cmd_calibrate(st, bad)
                check(False, "cli: out-of-range calibrate rejected", str(bad))
            except SystemExit as e:
                check(bool(str(e)), "cli: out-of-range calibrate rejected", str(bad))
        cli_mod.api = lambda *a, **k: None  # offline: the local path must still work
        cli_mod.cmd_calibrate(st, 12.5)
        check(abs(st.summary()["used"] - 12.5 * st.gb) <= 1, "cli: a valid figure still calibrates",
              str(st.summary()["used"]))
    finally:
        cli_mod.api = orig_api
        st.conn.close()


def test_load_repairs(tmp):
    """One invalid stored setting falls back alone; every other setting survives and the row is fixed."""
    st = Store(tmp)
    try:
        st.set_config({"plan_gb": 120, "ssid": "HOTSPOT", "notify": False})
        with st.conn:  # simulate an out-of-range value reaching the database (a downgrade, a hand edit)
            st.conn.execute("UPDATE settings SET value = '999999' WHERE key = 'plan_gb'")
    finally:
        st.conn.close()
    st2 = Store(tmp)
    try:
        check(st2.cfg["ssid"] == "HOTSPOT", "load: valid settings survive a bad neighbour",
              repr(st2.cfg["ssid"]))
        check(st2.cfg["plan_gb"] == 60, "load: only the invalid key falls back to default",
              str(st2.cfg["plan_gb"]))
        check(st2.cfg["notify"] is False, "load: other types survive too", str(st2.cfg["notify"]))
    finally:
        st2.conn.close()
    st3 = Store(tmp)
    try:
        row = st3.conn.execute("SELECT value FROM settings WHERE key = 'plan_gb'").fetchone()
        check(row is not None and json.loads(row[0]) == 60, "load: the repaired value is written back",
              str(row))
    finally:
        st3.conn.close()


def test_dashboard_js(tmp):
    """The dashboard's inline script parses, so a typo can never ship."""
    html = (ROOT / "dataguard" / "dashboard.html").read_text("utf-8")
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    check(bool(js.strip()), "dashboard: inline script found", "no <script> block")
    node = shutil.which("node")
    if not node:
        print("  skip  dashboard syntax (node not installed)")
        return
    f = Path(tmp) / "dashboard.js"
    f.write_text(js, "utf-8")
    p = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    check(p.returncode == 0, "dashboard: inline script parses", (p.stderr or p.stdout)[:400])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dg-smoke-"))
    try:
        print("imports")
        test_imports()
        print("settings")
        test_settings()
        print("usage summary")
        test_summary(tmp / "home")
        print("monitor")
        test_monitor(tmp / "mon")
        print("cli calibrate")
        test_calibrate_cli(tmp / "cli")
        print("settings repair")
        test_load_repairs(tmp / "repair")
        print("dashboard")
        test_dashboard_js(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\n{len(FAILS)} check(s) failed:")
        for name in FAILS:
            print(" - " + name)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
