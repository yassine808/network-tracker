"""Per-app activity: which programs are pulling data right now (Windows only)."""

import time

from .common import IS_WIN, psutil


def io_proxy(p):
    """Windows books socket traffic as 'other' I/O, so that per-process counter doubles as a network gauge."""
    return p.io_counters().other_bytes


def top_processes(window=3.0, limit=8):
    """Windows only: rank apps by recent 'other' I/O among processes that own network sockets. An estimate,
    not exact bytes. Linux and macOS have no comparable per-process counter, so the view reports itself unavailable."""
    if not IS_WIN:
        return {"supported": False, "items": [], "blind": [], "window": window}

    def snap():
        try:
            pids = {c.pid for c in psutil.net_connections(kind="inet") if c.pid}
        except (psutil.AccessDenied, OSError):
            pids = None
        vals, blind = {}, {}
        for p in psutil.process_iter(["name"]):
            if pids is not None and p.pid not in pids:
                continue
            name = p.info.get("name") or f"pid {p.pid}"
            try:
                vals[p.pid] = (name, io_proxy(p))
            except psutil.AccessDenied:
                blind[p.pid] = name
            except (psutil.Error, AttributeError):
                pass
        return vals, blind

    before, _ = snap()
    time.sleep(window)
    after, blind = snap()
    rows = {}
    for pid, (name, v1) in after.items():
        if pid in before and v1 >= before[pid][1]:
            r = rows.setdefault(name, {"name": name, "pids": 0, "rate": 0.0})
            r["pids"] += 1
            r["rate"] += (v1 - before[pid][1]) / window
    items = sorted((r for r in rows.values() if r["rate"] >= 512), key=lambda r: -r["rate"])[:limit]
    total = sum(r["rate"] for r in rows.values()) or 1.0
    for r in items:
        r["share"] = r["rate"] / total
    return {"supported": True, "items": items, "blind": sorted(set(blind.values()))[:8], "window": window}
