"""Watch the Apps rows against the Overview while one known download runs.

    python tests/watch.py            # 2 x 10 MB, direct (no proxy)

Prints one line every 5 s: the gap between the sum of the rows and the meter, the row of
the app that does the work, and System. Then it prints what the download really moved.
"""

import json
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import psutil

API = "http://127.0.0.1:8787"
URL = "https://speed.cloudflare.com/__down?bytes=10000000"


def api(path):
    req = Request(API + path, headers={"Host": "127.0.0.1:8787"})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def snap():
    st = api("/api/status")
    ap = api("/api/apps")
    c = psutil.net_io_counters(pernic=True).get(st["iface"])
    rows = {r["app"]: r for r in ap["apps"]}
    return {"overview": st["counted"], "sum": sum(r["cycle"] for r in ap["apps"]),
            "nic": (c.bytes_recv + c.bytes_sent) if c else 0,
            "rows": rows, "diag": ap.get("diag", "")}


def line(tag, a, b):
    def row(name):
        return b["rows"].get(name, {}).get("cycle", 0) - a["rows"].get(name, {}).get("cycle", 0)
    print("%-12s gap=%+9d (ov %+10d) nic %+11d curl %+11d System %+9d"
          % (tag, (b["sum"] - b["overview"]) - (a["sum"] - a["overview"]),
             b["overview"] - a["overview"], b["nic"] - a["nic"], row("curl.exe"), row("System")))


def main():
    a = snap()
    print("before:", a["diag"])
    truth = 0
    for i in range(2):
        r = subprocess.run(["curl.exe", "-sL", "--noproxy", "*", "--max-time", "120",
                            "-o", "NUL", "-w", "%{size_download}", URL],
                           capture_output=True, text=True)
        try:
            truth += int((r.stdout or "0").strip() or 0)
        except ValueError:
            print("  curl said:", (r.stdout or "")[:60], (r.stderr or "")[:80])
        time.sleep(8)
        b = snap()
        line("burst %d" % (i + 1), a, b)
        a = b
    for i in range(6):
        time.sleep(10)
        b = snap()
        line("idle +%ds" % ((i + 1) * 10), a, b)
        a = b
    print("download moved %d bytes; final diag: %s" % (truth, a["diag"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
