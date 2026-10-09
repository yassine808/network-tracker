"""Per-app usage history: a background sampler that records each program's bytes into SQLite."""

import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from .common import DB_NAME, IS_WIN, psutil
from .processes import io_proxy, _remote_pids, loopback_pids

KEEP_DAYS = 400


def _guess_path(name):
    """Exact filename match in PATH, then the folders apps usually live in, plus the App
    Paths registry. The last resort when no process will reveal its path (protected/elevated
    programs): only exact matches count, so a wrong file is never picked."""
    if not name or not IS_WIN:
        return ""
    win = os.environ.get("SystemRoot") or r"C:\Windows"
    dirs = [d for d in (os.environ.get("PATH") or "").split(os.pathsep) if d]
    dirs += [os.path.join(win, "System32"), os.path.join(win, "SysWOW64"),
             os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
             os.environ.get("APPDATA", ""), os.environ.get("PROGRAMDATA", "")]
    for d in dirs:
        cand = os.path.join(d, name)
        if d and os.path.isfile(cand):
            return cand
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                        hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\\" + name) as k:
                    v, _ = winreg.QueryValueEx(k, "")
            except OSError:
                continue
            v = (v or "").strip('"')
            if v and os.path.isfile(v):
                return v
    except (OSError, ImportError):
        pass
    return ""


LAUNCHER_DIRS = ("Riot Games", "HoYoPlay", "HoYoverse", "Epic Games", "Origin Games",
                 "EA Games", "Rockstar Games", "Steam", "GOG Games", "Ubisoft",
                 "Microsoft Studios", "Blizzard Entertainment")
MAX_DEPTH = 6  # VALORANT's shipping exe sits 5 folders under C:\Riot Games


def _steam_libraries():
    """Steam's own folder plus every library it records - games usually live on another drive."""
    roots = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (r"Software\Valve\Steam", r"SOFTWARE\WOW6432Node\Valve\Steam"):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        v, _ = winreg.QueryValueEx(k, "SteamPath")
                except OSError:
                    continue
                if v and v not in roots:
                    roots.append(v)
    except (OSError, ImportError):
        return roots
    libs = [r for r in roots if os.path.isdir(r)]
    for vdf in [os.path.join(r, "steamapps", "libraryfolders.vdf") for r in roots]:
        try:
            text = Path(vdf).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for raw in re.findall(r'"path"\s*"([^"]+)"', text):
            p = raw.replace("\\\\", "\\")
            if p not in libs:
                libs.append(p)
    return [os.path.normpath(os.path.join(p, "steamapps", "common")) for p in libs
            if os.path.isdir(os.path.join(p, "steamapps"))]


def _stem_key(name):
    """'GenshinImpact.exe' and 'Genshin Impact.lnk' both -> 'genshinimpact'."""
    base = os.path.basename(name or "")
    return re.sub(r"[^a-z0-9]", "", os.path.splitext(base)[0].lower())


def _shortcut_roots():
    """Start Menu and desktop: a program's icon lives there even when its exe hides
    from psutil and from every folder the installer index walks."""
    if not IS_WIN:
        return []
    roots = [os.path.join(os.environ.get("APPDATA", ""),
                          r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.environ.get("PROGRAMDATA", ""),
                          r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.path.expanduser("~"), "Desktop"),
             os.path.join(os.environ.get("PUBLIC", ""), "Desktop")]
    return [r for r in dict.fromkeys(roots) if r and os.path.isdir(r)]


def _library_roots():
    """Folders where installers put the programs that hide their path from psutil
    (anti-cheat, elevated): game launchers on every drive, Program Files, Steam libraries."""
    if not IS_WIN:
        return []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    drive = os.environ.get("SystemDrive", "C:")
    roots = [os.path.join(pf, d) for d in LAUNCHER_DIRS]
    roots += [os.path.join(pf86, d) for d in LAUNCHER_DIRS]
    roots += [os.path.join(drive + "\\", d) for d in LAUNCHER_DIRS]
    roots += _steam_libraries()
    roots += [pf, pf86]  # the whole Program Files trees: everything not made by a launcher
    return [r for r in dict.fromkeys(roots) if os.path.isdir(r)]



SUITE = re.compile(r"[\\/](avast software|alwil software|avg)[\\/]", re.I)  # security suites: one product, many .exe

# Folders where a block must stay local: their exes belong to Windows itself, never to one app
SYSTEM_DIRS = ("program files", "program files (x86)", "windows", "system32", "syswow64")


def _service_path(name):
    """Path of a Windows service whose program is `name` (AvastSvc.exe runs as a protected
    service: psutil can't read its path and it lives outside the folders we index first)."""
    if not IS_WIN or not name:
        return ""
    try:
        for svc in psutil.win_service_iter():
            try:
                b = (svc.binpath() or "").strip()
            except (psutil.Error, OSError):
                continue
            m = re.match(r'"([^"]+)"', b) or re.match(r"(.+?\.exe)", b, re.I)
            p = m.group(1) if m else ""
            if p and os.path.basename(p).lower() == name.lower() and os.path.isfile(p):
                return p
    except (psutil.Error, OSError, AttributeError):
        pass
    return ""


def _block_group(exe):
    """[(name, path)] that blocking `exe` must also cover. A security suite (Avast/AVG) keeps
    one product across many exes in the vendor's whole tree - blocking a few leaves the rest
    online. Every other program lives with its helpers (updaters, crash handlers) in its own
    install folder, so the folder tree is walked too: blocking one member must take the whole
    app with it, without naming any product. A Program Files/Windows ROOT is never walked -
    that would block unrelated apps. Members are keyed by full path - a copy of the same file
    name in another folder is a DIFFERENT binary; only the name gets suffixed on collision."""
    if not exe:
        return []
    m = SUITE.search(exe)
    if m:
        roots = [exe[:m.end(1)]]
        pd = os.environ.get("PROGRAMDATA")
        if pd:
            roots.append(os.path.join(pd, m.group(1)))
        depth, cap = 6, 150
    else:
        folder = os.path.dirname(exe)
        if not folder or os.path.basename(folder).lower() in SYSTEM_DIRS:
            return []  # the exe sits directly in a shared root: block it alone
        roots = [folder]
        depth, cap = 3, 40
    seen_paths, named, out = set(), {}, []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for dp, dn, fn in os.walk(root):
                if dp.count(os.sep) - root.count(os.sep) >= depth:
                    dn[:] = []
                for f in fn:
                    if not f.lower().endswith(".exe") or "unins" in f.lower():
                        continue
                    p = os.path.normcase(os.path.join(dp, f))
                    if p in seen_paths:
                        continue
                    seen_paths.add(p)
                    n = f
                    if n.lower() in named:  # same file name in two folders: keep both, name apart
                        n = "%s (%s)" % (f, hashlib.md5(p.encode(), usedforsecurity=False).hexdigest()[:6])
                    named[n.lower()] = p
                    out.append((n, p))
        except OSError:
            pass
    if len(out) > cap:
        logging.warning("firewall: block group scan found %d programs, covering only the first %d",
                        len(out), cap)
        out = out[:cap]
    return out


def _leak_check(delay=25):
    """Log, a bit after a block, which suite/System programs still hold connections.
    Matching is broad on purpose: AVG and Alwil-branded trees share Avast's binaries."""
    def run():
        time.sleep(delay)
        try:
            rows = {}
            for c in psutil.net_connections(kind="inet"):
                if not c.pid or not c.raddr:
                    continue
                # UDP has no status (never "ESTABLISHED"), so only filter TCP; a suite
                # talking DNS/NTP over UDP would otherwise be invisible to this check
                if c.type == socket.SOCK_STREAM and c.status != "ESTABLISHED":
                    continue
                if c.raddr.ip.startswith("127.") or c.raddr.ip == "::1":
                    continue
                try:
                    p = psutil.Process(c.pid)
                    n, ex = p.name(), p.exe()
                except psutil.Error:
                    n, ex = "pid %d" % c.pid, ""
                low_n, low_e = n.lower(), (ex or "").lower()
                if (c.pid == 4 or "avast" in low_e or "avg" in low_e or "alwil" in low_e
                        or low_n.startswith(("avast", "asw", "afw", "avg"))):
                    rows.setdefault("%s [%s]" % (n, ex or "?"), []).append("%s:%s" % (c.raddr.ip, c.raddr.port))
            logging.info("firewall: %ds after the block, open suite/System connections: %s", delay,
                         {k: v[:4] for k, v in rows.items()} or "none")
        except Exception as e:
            logging.info("firewall: leak check failed: %s", e)
    threading.Thread(target=run, daemon=True, name="dataguard-leakcheck").start()


HELPERS = {"msedgewebview2.exe"}  # their network traffic belongs to the host app (WhatsApp, Teams...)


def _owner_name(p, name):
    """Walk up from a WebView2 helper to the app that launched it; the helper itself does the
    downloading, so without this the app (e.g. WhatsApp) never shows up."""
    if name.lower() not in HELPERS:
        return name
    try:
        q = p
        for _ in range(8):
            q = q.parent()
            if q is None:
                break
            n = q.name()
            if n.lower() not in HELPERS:
                return n if n.lower() not in ("explorer.exe", "svchost.exe") else name
    except (psutil.Error, OSError):
        pass
    return name


def _wmi_other():
    """{pid: cumulative 'other' I/O bytes} from the performance counters. Unlike psutil this
    also reads protected and packaged (Store) apps such as WhatsApp. {} on any failure."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_PerfRawData_PerfProc_Process | "
             "Select-Object IDProcess,IOOtherBytesPersec | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20, creationflags=0x08000000)
        data = json.loads(r.stdout)
        if isinstance(data, dict):
            data = [data]
        return {int(d["IDProcess"]): int(d["IOOtherBytesPersec"]) for d in data}
    except Exception as e:
        logging.warning("apps: WMI fallback failed: %s", e)
        return {}


def _iso(day):
    return day.isoformat() if isinstance(day, date) else day


class AppStore:
    """Daily per-app byte totals in one SQLite file. Thread-safe (one guarded connection)."""

    def __init__(self, home):
        folder = Path(home)
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / DB_NAME
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS app_usage ("
                "day TEXT NOT NULL, app TEXT NOT NULL, bytes INTEGER NOT NULL, "
                "PRIMARY KEY (day, app)) WITHOUT ROWID")
            self.conn.commit()
        self.pending = {}  # (day, app) -> bytes not yet written to disk
        logging.info("apps: database ready at %s", self.path)
        self._migrate()

    def _migrate(self):
        """One-time import of the legacy app_usage.db into dataguard.db, then park the old file."""
        old = self.path.with_name("app_usage.db")
        with self.lock, self.conn:
            if self.conn.execute("SELECT 1 FROM app_usage LIMIT 1").fetchone() or not old.exists():
                return
            try:
                src = sqlite3.connect(str(old))
                try:
                    rows = src.execute("SELECT day, app, bytes FROM app_usage").fetchall()
                finally:
                    src.close()
                self.conn.executemany("INSERT OR REPLACE INTO app_usage VALUES (?, ?, ?)", rows)
                logging.info("apps: imported %d row(s) from the legacy app_usage.db", len(rows))
            except sqlite3.Error as e:
                logging.warning("app_usage.db import failed (%s); keeping it", e)
                return
        try:
            old.replace(old.with_name(old.name + ".migrated"))
        except OSError:
            pass

    def add(self, day, app, n):
        with self.lock:
            self.pending[(day, app)] = self.pending.get((day, app), 0) + n

    def rows_since(self, day):
        """[(day, app, bytes)] for every row on/after `day`, pending bytes folded in."""
        with self.lock:
            agg = {}
            for d, a, n in self.conn.execute(
                    "SELECT day, app, bytes FROM app_usage WHERE day >= ?", (day,)):
                agg[(d, a)] = agg.get((d, a), 0) + n
            for (d, a), n in self.pending.items():
                if d >= day:
                    agg[(d, a)] = agg.get((d, a), 0) + n
            return [(d, a, n) for (d, a), n in agg.items()]

    def clear_since(self, day):
        """Drop this cycle's per-app rows (reset button): day = first day to remove."""
        with self.lock:
            self.pending = {(d, a): n for (d, a), n in self.pending.items() if d < day}
            with self.conn:
                self.conn.execute("DELETE FROM app_usage WHERE day >= ?", (day,))

    def flush(self, force=False):
        """Write buffered bytes to disk, then drop history older than KEEP_DAYS."""
        with self.lock:
            if not self.pending and not force:
                return
            try:
                if self.pending:
                    self.conn.executemany(
                        "INSERT INTO app_usage (day, app, bytes) VALUES (?, ?, ?) "
                        "ON CONFLICT (day, app) DO UPDATE SET bytes = app_usage.bytes + excluded.bytes",
                        [(d, a, n) for (d, a), n in self.pending.items()])
                    self.pending.clear()
                cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
                self.conn.execute("DELETE FROM app_usage WHERE day < ?", (cutoff,))
                self.conn.commit()
            except sqlite3.Error as e:
                # disk full or locked: retry on the next flush, tracker keeps running
                logging.warning("apps: flush failed (will retry): %s", e)

    def snapshot(self, cycle_start, today):
        """Per-app totals for the cycle, today and the last 30 days, plus the 30-day day list."""
        with self.lock:
            t, cs = _iso(today), _iso(cycle_start)
            d30 = (today - timedelta(days=29)).isoformat()
            window = min(cs, d30)
            agg = {}
            rows = self.conn.execute(
                "SELECT day, app, bytes FROM app_usage WHERE day >= ? AND day <= ?", (window, t))
            for day, app, n in rows:
                self._fold(agg, day, app, n, t, cs, d30)
            for (day, app), n in self.pending.items():  # show bytes before they hit the disk
                if window <= day <= t:
                    self._fold(agg, day, app, n, t, cs, d30)
            apps = sorted(agg.values(), key=lambda r: (-r["cycle"], -r["d30"], r["app"]))
            return {"apps": apps, "count": len(apps),
                    "days": [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]}

    @staticmethod
    def _fold(agg, day, app, n, t, cs, d30):
        r = agg.setdefault(app, {"app": app, "today": 0, "cycle": 0, "d30": 0, "days": {}})
        if day == t:
            r["today"] += n
        if cs <= day:
            r["cycle"] += n
        if day >= d30:
            r["d30"] += n
            r["days"][day] = r["days"].get(day, 0) + n


class AppTracker(threading.Thread):
    """Sums each program's I/O delta every INTERVAL seconds while DataGuard runs.
    `allowed` says whether bytes are recorded - the dashboard passes the monitor's
    "are we on the configured network" flag, so the Apps page follows the meter's rule.
    The same flag gates the Windows block rules: a remembered block lives only on that
    network (every network when no Wi-Fi is configured), and elsewhere the choice is
    kept on disk until the network is back. `allowed` returning None means the meter
    has not learned which network we're on - then Windows is left alone."""

    INTERVAL = 10
    FLUSH_EVERY = 10
    INDEX_WAIT = 2.0   # first icon of the session may wait this long for the folder index
    MISS_TTL = 60.0    # remember "no file found" so a page load stops re-walking every process

    def __init__(self, store, allowed=None):
        super().__init__(name="dataguard-apps", daemon=True)
        self.store = store
        self.allowed = allowed or (lambda: True)
        self.supported = IS_WIN
        self.err = ""
        self.base = {}      # pid -> (name, other_bytes) at the previous tick
        self.paths = {}     # app name -> its .exe, for the Apps page (icon + block button)
        self.blocked_file = Path(store.path).parent / "blocked.json"
        self.blocked = self._load_blocked()  # apps switched to "block internet", kept across restarts
        for legacy in ("limits.json", "suspended.json"):  # leftovers of the removed bandwidth limiter
            try:
                (Path(store.path).parent / legacy).unlink()
            except OSError:
                pass
        self._block_lock = threading.Lock()  # a user toggle and the periodic resync must not interleave
        self.t_flush = time.time()
        self.stop_event = threading.Event()
        self._index = {}          # lowercase exe name -> path, built once in the background
        self._index_evt = threading.Event()
        self._index_lock = threading.Lock()
        self._index_started = False
        self._lnk = {}            # program stem -> the shortcut that carries its icon
        self._no_path = {}        # app name -> when the file last could not be found
        self.nic_fn = None        # set by the dashboard: () -> bytes the counted NIC moved this tick
        self.totals_fn = None     # set by the dashboard: () -> Overview's bytes for today (ground truth)
        self.t_recon = 0.0        # last time the today total was reconciled against the Overview

    def exe(self, name):
        """File path for an app: the recorded one when it still exists, else looked up live
        (a protected or elevated process hides its path from the sampler), else guessed from
        PATH and the folders apps usually live in, else found in the install-folder index.
        "" only when nothing could find the file - and then only for a minute, so a new
        install or a relaunched game is retried instead of staying a generic glyph."""
        p = self.paths.get(name) or ""
        if p and os.path.isfile(p):
            return p
        miss = self._no_path.get(name)
        if miss is not None and time.time() - miss < self.MISS_TTL:
            return ""
        p = self._find_live(name) or _guess_path(name) or _service_path(name) or self._indexed(name)
        if p:
            self.paths[name] = p
            self._no_path.pop(name, None)
            logging.debug("apps: found file for %s -> %s", name, p)
            return p
        if self._index_evt.is_set():  # every source really was tried: remember it for a minute
            self._no_path[name] = time.time()
        return ""

    def _indexed(self, name):
        """Look the file up in the folder index. The walk runs in the background from the
        moment tracking starts, so this only ever waits out a build already under way -
        a request must never scan the disk itself."""
        self._start_index()
        self._index_evt.wait(self.INDEX_WAIT)
        return self._index.get(name.lower(), "")

    def icon_src(self, name):
        """File to take an app's icon from: its exe when that is known, else the Start Menu
        or desktop shortcut carrying the same program's icon - a program can hide its path
        (or live outside every folder the index walks) while its shortcut stays put. Exact
        stem matches only: a near-miss would put someone else's logo on the row."""
        exe = self.exe(name)
        if exe:
            return exe
        self._start_index()
        self._index_evt.wait(self.INDEX_WAIT)
        return self._lnk.get(_stem_key(name), "")

    def _start_index(self):
        if not IS_WIN:
            return
        with self._index_lock:
            if self._index_started:
                return
            self._index_started = True
            threading.Thread(target=self._build_index, daemon=True,
                             name="dataguard-exe-index").start()

    def _build_index(self):
        """Map every .exe under the launcher folders to its path, and every shortcut to the
        program it points at, once."""
        try:
            idx = {}
            for root in _library_roots():
                for dirpath, dirnames, filenames in os.walk(root):
                    if dirpath.count(os.sep) - root.count(os.sep) >= MAX_DEPTH:
                        dirnames[:] = []
                    for f in filenames:
                        if f.lower().endswith(".exe"):
                            idx.setdefault(f.lower(), os.path.join(dirpath, f))
            self._index = idx
            logging.info("apps: indexed %d program files from the launcher folders", len(idx))
        except Exception as e:  # an odd folder must not stop tracking
            logging.warning("apps: could not index the launcher folders: %s", e)
        try:
            lnk = {}
            for root in _shortcut_roots():
                for dirpath, _, files in os.walk(root):
                    for f in files:
                        if f.lower().endswith(".lnk"):
                            lnk.setdefault(_stem_key(f), os.path.join(dirpath, f))
            self._lnk = lnk
        except Exception as e:
            logging.warning("apps: could not index the shortcuts: %s", e)
        finally:
            self._index_evt.set()


    def _find_live(self, name):
        """Ask psutil again right now: the sampler may have missed a path that reads fine later."""
        if not IS_WIN:
            return ""
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                if (proc.info.get("name") or "").lower() == name.lower() and proc.info.get("exe"):
                    return proc.info["exe"]
        except (psutil.Error, OSError):
            pass
        return ""

    def _load_blocked(self):
        try:
            data = json.loads(self.blocked_file.read_text(encoding="utf-8"))
            return {n for n in data if isinstance(n, str)} if isinstance(data, list) else set()
        except (OSError, ValueError):
            return set()

    def group(self, name):
        """[(name, exe)] that blocking `name` must cover: the app itself plus every sibling
        program shipped in the same install folder (helpers, updaters) or - for security
        suites - the vendor's whole tree."""
        exe = self.exe(name)
        members = {name: exe}
        for n, p in _block_group(exe):
            members.setdefault(n, p)
        return list(members.items())

    def set_blocked(self, name, on):
        """(ok, message): remember the user's choice on disk; the Windows rule changes only
        while the network gate is open (`allowed`). Off that network the choice is recorded
        and sync_blocks applies it when the configured Wi-Fi returns; validation (a real
        file, never a system service) always runs, and the set is only updated after
        Windows has accepted the change - a rejected block is never remembered."""
        if not self.supported:
            return False, "per-app blocking runs on Windows only"
        from . import firewall
        members = self.group(name)
        with self._block_lock:
            if on:
                exe = dict(members).get(name)
                ok, err = firewall.can_block(name, exe)
                if not ok:
                    return False, err
                if self.allowed():
                    if len(members) == 1:
                        ok, err = firewall.block_app(name, exe)
                        if not ok:
                            return False, err
                    else:  # a suite: all its programs in ONE change (one Windows prompt)
                        items = [(n, p, True) for n, p in members if p]
                        res = {n: (ok, msg) for n, ok, msg in firewall.sync_app_blocks(items)}
                        for n, (ok, msg) in res.items():
                            if not ok:
                                logging.warning("firewall: %s: %s", n, msg)
                        _leak_check()
                        if not res.get(name, (False, ""))[0]:
                            return False, res.get(name, (False, "could not block"))[1] or "could not block"
            else:
                if len(members) == 1:
                    if firewall.has_block(name):
                        ok, err = firewall.unblock_app(name)
                        if not ok:
                            return False, err
                else:  # one change here too: member-by-member lets the periodic resync
                    # re-add a suite member the loop already removed (stray blocks)
                    items = [(n, p, False) for n, p in members]
                    res = {n: (ok, msg) for n, ok, msg in firewall.sync_app_blocks(items)}
                    for n, (ok, msg) in res.items():
                        if not ok:
                            logging.warning("firewall: %s: %s", n, msg)
                    if not res.get(name, (False, ""))[0]:
                        return False, res.get(name, (False, "could not unblock"))[1] or "could not unblock"
            if on:
                self.blocked.add(name)
            else:
                self.blocked.discard(name)
            try:
                tmp = self.blocked_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(self.blocked)), encoding="utf-8")
                tmp.replace(self.blocked_file)
            except OSError as e:
                logging.warning("apps: could not save the blocked list: %s", e)
        logging.info("apps: %s %s", name, "blocked" if on else "unblocked")
        return True, ""

    def sync_blocks(self):
        """Bring Windows in line with the remembered blocks and the current network gate:
        on the configured Wi-Fi every remembered block must exist (rebuild it if an app
        moved or the firewall was reset); anywhere else none of them may - the choice
        stays on disk and comes back with the hotspot. The gate is read fresh here;
        None (the meter has not learned the network yet) leaves Windows alone."""
        if not self.supported or not self.blocked:
            return
        if not self._block_lock.acquire(blocking=False):
            return  # a user toggle is mid-flight; it leaves a consistent state, skip this round
        try:
            gate = self.allowed()
            if gate is None:
                return
            from . import firewall
            items = {}
            for name in sorted(self.blocked):
                for n, p in self.group(name):
                    items.setdefault(n, (n, p, bool(gate)))
            logging.debug("firewall: syncing %d program(s) for %d blocked app(s)", len(items), len(self.blocked))
            failed = 0
            for name, ok, err in firewall.sync_app_blocks(list(items.values())):
                if not ok:
                    failed += 1
                    logging.warning("firewall: %s: %s", name, err)
            if failed:
                logging.warning("firewall: %d of %d program(s) could not be %s", failed, len(items),
                                "blocked" if gate else "unblocked")
            if gate and len(items) > 1:
                _leak_check()
        finally:
            self._block_lock.release()

    def run(self):
        if not self.supported:
            return
        self._start_index()  # so the first page load finds icons, not a disk walk
        gate = None  # None = the network is still unknown; judged on the first ticks
        last_resync = 0.0
        while not self.stop_event.is_set():
            try:
                self.tick()
                now = self.allowed()
                if now is not None and now != gate:
                    gate = now
                    self.sync_blocks()
                    logging.info("firewall: network gate %s - remembered blocks %s",
                                 "opened" if gate else "closed",
                                 "are in place" if gate else "are removed until the Wi-Fi returns")
                # Avast updates itself while blocked and drops new binaries: re-scan and
                # re-apply the suite blocks every few minutes (read-only when nothing changed)
                if gate and self.blocked and time.monotonic() - last_resync > 300:
                    last_resync = time.monotonic()
                    self.sync_blocks()
                self.err = ""
            except Exception as e:  # never let a hiccup stop the tracker
                self.err = f"{type(e).__name__}: {e}"
            # while the network is unknown, judge again quickly instead of waiting a full tick
            self.stop_event.wait(self.INTERVAL if gate is not None else 1.0)
        self.store.flush(force=True)

    def tick(self):
        record = self.allowed()  # off the configured network: track state, but record nothing
        try:
            pids = {c.pid for c in psutil.net_connections(kind="inet") if c.pid}
        except (psutil.AccessDenied, OSError):
            pids = None
        day = date.today().isoformat()
        cur, blind = {}, {}
        remote = _remote_pids()   # who holds a real internet socket, for the suite-driver filter
        loop = loopback_pids()    # who holds nothing but loopback: camera/mic chatter, not internet

        # Pass 1 - read every process's raw counter into cur{}, naming/blinding as we go, but
        # record nothing yet (the scale factor needs the whole population first).
        loopback_skipped = 0
        for p in psutil.process_iter(["name", "exe"]):
            name = p.info.get("name") or f"pid {p.pid}"
            if p.info.get("exe"):
                self.paths.setdefault(name, p.info["exe"])  # record for every process, connected or not
            name = _owner_name(p, name)  # WebView2/Electron helpers: bill the app that owns them
            try:
                if p.pid in loop and p.pid not in remote:
                    v = 0  # loopback-only process (e.g. NVIDIA Broadcast on 127.0.0.1): not internet
                    loopback_skipped += 1
                else:
                    v = io_proxy(p, remote)
            except (psutil.AccessDenied, psutil.Error, AttributeError):
                if pids is None or p.pid in pids:
                    blind[p.pid] = name  # protected / packaged app: read it from WMI below
                    logging.debug("tick: pid %s (%s) blind (AccessDenied/WMI)", p.pid, name)
                continue
            cur[p.pid] = (name, v)  # baseline for EVERY process, so a new app counts from its first socket
        if blind:
            raw = _wmi_other()
            for pid, name in blind.items():
                cur[pid] = (name, raw.get(pid, 0))  # unreadable counter: hold at 0 so the app still lists

        # Pass 2 - a process's raw 'other' bytes include device/driver I/O (NVIDIA Broadcast's
        # camera work) and loopback, which the NIC never sees, so the raw deltas sum larger than
        # the Overview's counter. Scale each app's share so the recorded total tracks the bytes
        # the counted NIC actually moved: Apps then adds up to the Overview instead of over-counting
        # it. scale is capped at 1.0 so a sample-boundary mismatch never inflates or shrinks real
        # bytes downward; when the NIC moved more than the apps reported (blind app), we record raw.
        gained = 0
        skipped = 0
        blocked_skipped = 0
        deltas = []
        for pid, (name, v) in cur.items():
            if pids is not None and pid not in pids:
                skipped += 1
                continue
            if record and name in self.blocked:
                blocked_skipped += 1  # firewalled: its bytes never reach the counted NIC
                continue
            prev = self.base.get(pid)
            d = v - prev[1] if prev is not None and prev[0] == name and v >= prev[1] else 0
            if d < 0:
                d = 0  # a counter reset must never subtract from the app's total
            if d:
                deltas.append((name, d))
        # Deltas only - both sides must be bytes moved in THIS tick's window, or the scale
        # (NIC delta / absolute counter) collapses to ~0 and the apps stop counting at all.
        raw_delta = sum(d for _, d in deltas)
        nic_tot = self.nic_fn() if self.nic_fn else None
        scale = (nic_tot / raw_delta) if (record and raw_delta > 0 and nic_tot is not None) else 1.0
        scale = min(scale, 1.0)
        if record:
            for name, d in deltas:
                d = int(d * scale)
                self.store.add(day, name, d)
                gained += d
        self.base = cur
        self.diag = "v1.3.0 · %s processes with connections, %d read via WMI, %d loopback-only, %d blocked%s" % (
            "?" if pids is None else len({q for q in pids}), len(blind),
            len(loop - (remote or set())), blocked_skipped,
            "" if scale in (0.0, 1.0) else ", scaled %.0f%% to NIC" % (scale * 100))
        logging.debug(
            "tick: record=%s pids=%s raw_delta=%d nic_delta=%s scale=%.3f gained=%d "
            "procs=%d blind=%d skipped_no_conn=%d loopback_skipped=%d blocked_skipped=%d",
            record, "?" if pids is None else len(pids), raw_delta, nic_tot, scale, gained,
            len(cur), len(blind), skipped, loopback_skipped, blocked_skipped)
        if record and self.totals_fn and time.time() - self.t_recon >= 60:
            self.t_recon = time.time()
            self._reconcile(day, deltas)
        if time.time() - self.t_flush >= self.FLUSH_EVERY:
            self.t_flush = time.time()
            self.store.flush()

    def _reconcile(self, day, deltas):
        """Make each cycle day's Apps total equal the Overview's (ground truth).

        The meter counts every byte the counted NIC moved, per day; the apps are summed
        from per-process counters, which miss some bytes (no readable counter, a window
        the sampler was down) and invent others (device/driver I/O). tick()'s scale
        removes the excess each tick, but a missed window leaves a lasting hole - and a
        past bug can leave a lasting overcount. Once a minute, walk the cycle's days and
        close each gap: hand the difference to that day's apps in proportion to what they
        already recorded (or, with nothing recorded yet, to whatever just moved bytes)."""
        try:
            truth = self.totals_fn()
        except Exception as e:
            logging.warning("reconcile: Overview totals unavailable: %s", e)
            return
        if not truth:
            return
        start, ov_days = truth["start"], truth["days"]
        by_day = {}
        for d, app, n in self.store.rows_since(start):
            if n:
                by_day.setdefault(d, []).append((app, n))
        for d in sorted(set(ov_days) | set(by_day)):
            if d < start:
                continue
            mine = sum(n for _, n in by_day.get(d, ()))
            diff = int(ov_days.get(d, 0)) - mine
            if abs(diff) < 256 * 1024:  # sub-quarter-MB jitter is window alignment, not a real gap
                continue
            rows = by_day.get(d) or []
            total = sum(n for _, n in rows)
            if not rows and d == day and deltas:  # nothing on record: share it by what just moved
                rows, total = [(n, b) for n, b in deltas if b > 0], sum(b for _, b in deltas if b > 0)
            if not rows or total <= 0:
                continue
            if diff > 0:
                add = {a: int(diff * n / total) for a, n in rows}
                top = max(rows, key=lambda kv: kv[1])[0]  # rounding remainder to the biggest share
                add[top] += diff - sum(add.values())
                for a, b in add.items():
                    if b:
                        self.store.add(d, a, b)
            else:  # an old overcount: shrink proportionally, never below zero
                real = 0
                for a, n in rows:
                    s = min(n, int(-diff * n / total))
                    if s:
                        self.store.add(d, a, -s)
                        real += s
                diff = -real
            logging.info("reconcile: %s apps=%d vs Overview=%d -> %+d across %d app(s)",
                         d, mine, int(ov_days.get(d, 0)), diff, len(rows))

    def stop(self):
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)
        self.store.flush(force=True)

    def snapshot(self, cycle_start, today):
        data = self.store.snapshot(cycle_start, today)
        known = {r["app"] for r in data["apps"]}
        for name in sorted(self.blocked - known):  # a blocked app must stay listed after a reset
            data["apps"].append({"app": name, "today": 0, "cycle": 0, "d30": 0, "days": {}})
        data["apps"].sort(key=lambda r: (-r["cycle"], -r["d30"], r["app"]))
        for r in data["apps"]:
            r["blocked"] = r["app"] in self.blocked
            r["blocked_here"] = r["blocked"] and bool(self.allowed())  # enforced on this network right now
        data["count"] = len(data["apps"])
        data.update(supported=self.supported, interval=self.INTERVAL, err=self.err,
                    diag=getattr(self, "diag", ""), blocked=sorted(self.blocked))
        logging.debug("apps: snapshot %d app(s), %d blocked",
                      data["count"], len(self.blocked))
        return data
