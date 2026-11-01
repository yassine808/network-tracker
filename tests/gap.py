"""Show, right now, how the Apps rows compare with the Overview and the raw DB.

    python tests/gap.py            # today
    python tests/gap.py 2026-11-01

Reads the running app's API and the same SQLite file it writes.
"""

import json
import os
import sqlite3
import sys
from urllib.request import Request, urlopen

DB = os.path.join(os.environ["APPDATA"], "DataGuard", "dataguard.db")


def api(path):
    req = Request("http://127.0.0.1:8787" + path, headers={"Host": "127.0.0.1:8787"})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    st = api("/api/status")
    ap = api("/api/apps")
    rows = {r["app"]: r for r in ap["apps"]}
    total = sum(r["cycle"] for r in ap["apps"])
    print("overview: counted=%d used=%d offset=%d today=%d"
          % (st["counted"], st["used"], st["offset"], st["today"]))
    print("apps:     cycle=%d  diff vs counted=%+d (%.4f%%)"
          % (total, total - st["counted"], 100.0 * (total - st["counted"]) / max(st["counted"], 1)))
    print("diag:     %s" % ap.get("diag", ""))
    for name in ("System", "curl.exe", "WmiPrvSE.exe"):
        if name in rows:
            r = rows[name]
            print("  %-16s cycle=%d" % (name, r["cycle"]))
    con = sqlite3.connect(DB)
    days = {d: rx + tx for d, rx, tx in con.execute("SELECT day, rx, tx FROM days")}
    apps = {}
    for d, n in con.execute("SELECT day, SUM(bytes) FROM app_usage GROUP BY day"):
        apps[d] = n
    print("\nper day (db):")
    for d in sorted(set(days) | set(apps)):
        ov, ap_ = days.get(d, 0), apps.get(d, 0)
        print("  %s overview=%12d apps=%12d diff=%+9d" % (d, ov, ap_, ap_ - ov))
    return 0


if __name__ == "__main__":
    sys.exit(main())
