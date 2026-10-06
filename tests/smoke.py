"""Fast checks that must pass before any release: pricing math, update packaging safety,
strict-SSID gating and dashboard syntax. Run: python tests/smoke.py (exit 0 = all good)."""

import io
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataguard import __version__, update
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
                "dataguard.update", "dataguard.web", "dataguard.cli"):
        try:
            importlib.import_module(mod)
            check(True, "import " + mod)
        except Exception as e:
            check(False, "import " + mod, f"{type(e).__name__}: {e}")


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


def test_update_versions():
    """Version tuples compare numerically and the status flag only flips on something newer."""
    check(update.ver("v1.10.0") > update.ver("v1.9.0"), "update: 1.10.0 beats 1.9.0")
    check(update.ver("v9.9.9") > update.ver("1.0.0"), "update: v9.9.9 beats 1.0.0")
    check(update.ver(None) < update.ver("1.0.0"), "update: a missing tag never wins")
    check(bool(re.match(r"^\d+\.\d+\.\d+$", __version__)), "package: a release version is set",
          __version__)
    saved = update._state["latest"]
    try:
        update._state["latest"] = "v99.0.0"
        check(update.status()["available"] is True, "update: a newer release is offered")
        update._state["latest"] = "v0.0.1"
        check(update.status()["available"] is False, "update: an older release stays quiet")
        update._state["latest"] = None
        check(update.status()["available"] is False, "update: no releases = nothing to offer")
    finally:
        update._state["latest"] = saved


def test_unpack(tmp):
    """A release zip is unpacked safely: one wrapping folder peeled, escape attempts refused."""
    def zipped(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return zipfile.ZipFile(io.BytesIO(buf.getvalue()))

    case = 0

    def dest():
        nonlocal case
        case += 1
        d = Path(tmp) / f"case{case}"
        d.mkdir(parents=True)
        return d

    d = dest()
    update.unpack(zipped({"pkg/a.txt": "A", "pkg/b.txt": "B"}), d)
    check((d / "a.txt").read_text() == "A" and (d / "b.txt").read_text() == "B",
          "unpack: one wrapping folder is peeled off")

    d = dest()
    update.unpack(zipped({"dataguard/x.py": "X", "dataguard.py": "Y"}), d)
    check((d / "dataguard" / "x.py").read_text() == "X" and (d / "dataguard.py").read_text() == "Y",
          "unpack: the real release layout lands at the root")

    for entries, name in [
        ({"pkg/a.txt": "A", "../../evil.txt": "boom"}, "unpack: .. paths refused"),
        ({"/etc/evil.txt": "boom"}, "unpack: absolute paths refused"),
        ({}, "unpack: an empty release refused"),
    ]:
        d = dest()
        try:
            update.unpack(zipped(entries), d)
            check(False, name, "no error raised")
        except ValueError:
            check(True, name)
        except Exception as e:
            check(False, name, f"{type(e).__name__}: {e}")


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
        print("updater")
        test_update_versions()
        test_unpack(tmp / "zip")
        print("monitor")
        test_monitor(tmp / "mon")
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
