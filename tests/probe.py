"""Accuracy probe: drive one app for a fixed window with a countable payload, then compare
what the Apps panel recorded, what the Overview counted and the bytes that really moved.

The payload is the ground truth: curl reports it, .NET returns the array length, certutil
leaves the file on disk, Brave leaves the download behind. No estimate is used as truth.

Run:  python tests/probe.py curl --minutes 15
      python tests/probe.py all  --minutes 15     (every scenario in turn)

The app must be running (it owns 127.0.0.1:8787) and must have the ETW session (admin),
otherwise the Apps page is the estimate fallback and the numbers are not comparable.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

API = "http://127.0.0.1:8787"
# a fixed-size body from Cloudflare's speed-test endpoint: the payload is exactly `bytes`
URL = "https://speed.cloudflare.com/__down?bytes=10000000"
CHUNK = 10_000_000
PROXY = "socks5h://127.0.0.1:18119"   # usque's local proxy, when it is running


def api(path):
    req = Request(API + path, headers={"Host": "127.0.0.1:8787"})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def nic(iface):
    """(rx, tx) of one adapter straight from psutil - independent of the app's own maths."""
    import psutil
    c = psutil.net_io_counters(pernic=True).get(iface)
    return (c.bytes_recv, c.bytes_sent) if c else (0, 0)


def snap():
    st = api("/api/status")
    ap = api("/api/apps")
    rx, tx = nic(st["iface"])
    return {"t": time.time(), "counted": st["counted"], "today": st["today"],
            "iface": st["iface"], "nic": rx + tx,
            "apps": {r["app"]: r["cycle"] for r in ap["apps"]},
            "diag": ap.get("diag", "")}


def _curl(args):
    r = subprocess.run(["curl.exe", "-sL", "--max-time", "180", "-o", "NUL",
                        "-w", "%{size_download}"] + args + [URL],
                       capture_output=True, text=True)
    try:
        return int((r.stdout or "0").strip() or 0)
    except ValueError:
        print("    curl said: %r %r" % (r.stdout[:80], r.stderr[:120]))
        return 0


def _powershell():
    ps = ("$ErrorActionPreference='Stop';"
          "$c=New-Object Net.WebClient;"
          "$c.Headers.Add('User-Agent','dg-probe');"
          "$b=$c.DownloadData('%s');"
          "Write-Output $b.Length") % URL
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    try:
        return int((r.stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        print("    powershell said: %r %r" % (r.stdout[:80], r.stderr[:160]))
        return 0


def _certutil():
    tmp = Path(tempfile.gettempdir()) / "dg_probe.bin"
    if tmp.exists():
        tmp.unlink()
    subprocess.run(["certutil.exe", "-urlcache", "-split", "-f", URL, str(tmp)],
                   capture_output=True, text=True)
    n = tmp.stat().st_size if tmp.exists() else 0
    if tmp.exists():
        tmp.unlink()
    return n


def _brave():
    """Open the fixed-size URL in Brave: Chromium downloads it and leaves the file behind."""
    exe = _brave_exe()
    if not exe:
        print("    brave.exe not found")
        return 0
    before = _downloads()
    subprocess.run([exe, "--new-window", URL], capture_output=True, text=True)
    for _ in range(60):  # a 10 MB file is done in seconds; wait for it to appear
        time.sleep(2)
        new = _downloads() - before
        if new:
            return sum(new.values())
    return 0


def _brave_exe():
    for p in (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"):
        if os.path.isfile(p):
            return p
    return ""


def _downloads():
    d = Path(os.path.expanduser("~")) / "Downloads"
    if not d.is_dir():
        return {}
    return {p.name: p.stat().st_size for p in d.iterdir()
            if p.is_file() and not p.name.endswith((".crdownload", ".tmp"))}


SCENARIOS = {
    # name -> (app row to compare, one burst -> bytes it really moved)
    "curl":     ("curl.exe",       lambda: _curl([])),
    "proxy":    ("curl.exe",       lambda: _curl(["--proxy", PROXY])),
    "powershell": ("powershell.exe", _powershell),
    "certutil": ("certutil.exe",   _certutil),
    "brave":    ("brave.exe",      _brave),
}


def run(name, minutes, bursts):
    app, burst = SCENARIOS[name]
    gap = max(1.0, minutes * 60.0 / bursts)
    print("\n=== %s: %s, %d x %d MB over %.0f min (app row: %s)"
          % (name, "proxy via usque" if name == "proxy" else "direct", bursts,
             CHUNK // 1_000_000, minutes, app))
    a = snap()
    print("    before: used=%.3f GB, iface=%s, diag=%s"
          % (a["counted"] / 1e9, a["iface"], a["diag"][:60]))
    truth = 0
    t0 = time.time()
    for i in range(bursts):
        n = burst()
        truth += n
        print("    burst %2d/%d: %d bytes (running %d)  [%.1f min]"
              % (i + 1, bursts, n, truth, (time.time() - t0) / 60.0))
        left = (i + 1) * gap - (time.time() - t0)
        if i + 1 < bursts and left > 0:
            time.sleep(left)
    time.sleep(12)  # let the last tick land before the closing snapshot
    b = snap()
    delta = {k: b["apps"].get(k, 0) - a["apps"].get(k, 0) for k in set(a["apps"]) | set(b["apps"])}
    wired = b["nic"] - a["nic"]
    metered = b["counted"] - a["counted"]
    row = delta.get(app, 0)
    same = sorted(((v, k) for k, v in delta.items() if k != app and v > 100_000), reverse=True)[:6]
    print("    truth=%d  app row %s=%d  err=%+.1f%%" % (truth, app, row, pct(row, truth)))
    print("    NIC moved=%d (err %+.1f%%)   Overview counted=%d (err %+.1f%%)"
          % (wired, pct(wired, truth), metered, pct(metered, truth)))
    for v, k in same:
        print("    also moved: %-28s %d" % (k, v))
    return {"scenario": name, "app": app, "truth": truth, "row": row,
            "row_err": pct(row, truth), "nic": wired, "nic_err": pct(wired, truth),
            "overview": metered, "overview_err": pct(metered, truth),
            "others": {k: v for v, k in same}, "diag": b["diag"],
            "minutes": minutes, "bursts": bursts}


def pct(got, truth):
    return (got - truth) * 100.0 / truth if truth else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="all",
                    help="one of %s, or all" % ", ".join(SCENARIOS))
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--bursts", type=int, default=15)
    args = ap.parse_args()
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    out = []
    for n in names:
        out.append(run(n, args.minutes, args.bursts))
    print("\n=== summary ===")
    print("  %-11s %-16s %12s %12s %8s %12s %8s" %
          ("scenario", "app", "truth", "app row", "err%", "overview", "err%"))
    for r in out:
        print("  %-11s %-16s %12d %12d %+7.1f%% %12d %+7.1f%%"
              % (r["scenario"], r["app"], r["truth"], r["row"], r["row_err"],
                 r["overview"], r["overview_err"]))
    dest = Path(__file__).resolve().parent / "probe.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("  written to %s" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
