"""Per-app activity: which programs are pulling data right now (Windows only)."""

import time

from .common import IS_WIN, psutil

# Security suites keep a constant stream of 'other' I/O against their own kernel drivers
# (webshield, traffic inspection). That is local driver chatter, not internet, but Windows
# books it on the same 'other' counter a real download uses - so without this the suite
# shows up as "busy downloading" while sending zero bytes online. We only discount that
# chatter for these known suites, and only while they hold no remote socket, so a genuine
# download by any other app is never touched.
_SUITE_STEMS = ("avast", "avg", "asw", "afw", "alwil", "avira", "norton", "symantec",
                "mcafee", "kaspersky", "bitdefender", "eset", "nod32", "sophos", "f-secure",
                "windows defender", "msmpeng", "mpdefendercoreservice")


def _is_suite(name):
    n = (name or "").lower()
    return any(n.startswith(s) or s in n for s in _SUITE_STEMS)


def _has_remote_socket(pid, remote_pids):
    """True when the pid owns at least one non-loopback remote socket - i.e. real internet."""
    return remote_pids is not None and pid in remote_pids


def _remote_pids():
    """{pid} for processes holding a non-loopback remote socket, or None when unreadable."""
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return None
    remote = set()
    for c in conns:
        if not c.pid or not c.raddr:
            continue
        ip = c.raddr.ip
        if (ip.startswith("127.") or ip.startswith("169.254.") or ip.startswith("10.")
                or ip.startswith("192.168.") or ip in ("0.0.0.0", "::", "::1")):
            continue
        remote.add(c.pid)
    return remote


def io_proxy(p, remote_pids=None):
    """Windows books socket traffic as 'other' I/O, so that per-process counter doubles as a network gauge.

    For a known security suite holding no remote socket the counter is local driver chatter,
    not internet, so it reports 0 to keep the suite off the busy list."""
    if remote_pids is not None and _is_suite(p.info.get("name")) and not _has_remote_socket(p.pid, remote_pids):
        return 0
    return p.io_counters().other_bytes


def top_processes(window=3.0, limit=8):
    """Windows only: rank apps by recent 'other' I/O among processes that own network sockets. An estimate,
    not exact bytes. Linux and macOS have no comparable per-process counter, so the view reports itself unavailable."""
    if not IS_WIN:
        return {"supported": False, "items": [], "blind": [], "window": window}

    remote = _remote_pids()

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
                vals[p.pid] = (name, io_proxy(p, remote))
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
