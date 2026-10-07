"""Per-app usage history: a background sampler that records each program's bytes into SQLite."""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from .common import DB_NAME, IS_WIN, psutil
from .processes import io_proxy

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
            except sqlite3.Error:
                pass  # disk full or locked: retry on the next flush, tracker keeps running

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
    "are we on the configured network" flag, so the Apps page follows the meter's rule."""

    INTERVAL = 15
    FLUSH_EVERY = 60
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
        self.t_flush = time.time()
        self.stop_event = threading.Event()
        self._index = {}          # lowercase exe name -> path, built once in the background
        self._index_evt = threading.Event()
        self._index_lock = threading.Lock()
        self._index_started = False
        self._lnk = {}            # program stem -> the shortcut that carries its icon
        self._no_path = {}        # app name -> when the file last could not be found

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
        p = self._find_live(name) or _guess_path(name) or self._indexed(name)
        if p:
            self.paths[name] = p
            self._no_path.pop(name, None)
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

    def set_blocked(self, name, on):
        """Remember the user's choice on disk (the rules themselves live in Windows)."""
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

    def _reapply_blocks(self):
        """Make sure every remembered block still exists in the firewall. Rules normally
        survive reboots (read-only check, no prompt); a missing or stale one - an app
        update moved the file, or the firewall was reset - is rebuilt."""
        if not IS_WIN or not self.blocked:
            return
        from . import firewall
        for name in sorted(self.blocked):
            exe = self.exe(name)
            try:
                ok, err = firewall.ensure_block(name, exe)
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            logging.info("firewall: re-applied block for %s: %s",
                         name, "ok" if ok else f"failed ({err})")

    def run(self):
        if not self.supported:
            return
        self._start_index()  # so the first page load finds icons, not a disk walk
        try:
            self._reapply_blocks()
        except Exception as e:  # never let firewall upkeep stop the tracker
            logging.warning("apps: re-applying blocks failed: %s", e)
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.err = ""
            except Exception as e:
                self.err = f"{type(e).__name__}: {e}"
            self.stop_event.wait(self.INTERVAL)
        self.store.flush(force=True)

    def stop(self):
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)
        self.store.flush(force=True)

    def tick(self):
        record = self.allowed()  # off the configured network: track state, but record nothing
        try:
            pids = {c.pid for c in psutil.net_connections(kind="inet") if c.pid}
        except (psutil.AccessDenied, OSError):
            pids = None
        day = date.today().isoformat()
        cur, gained = {}, 0
        for p in psutil.process_iter(["name", "exe"]):
            name = p.info.get("name") or f"pid {p.pid}"
            if p.info.get("exe"):
                self.paths.setdefault(name, p.info["exe"])  # record for every process, connected or not
            if pids is not None and p.pid not in pids:
                continue
            try:
                v = io_proxy(p)
            except (psutil.AccessDenied, psutil.Error, AttributeError):
                continue
            cur[p.pid] = (name, v)
            prev = self.base.get(p.pid)
            if prev is not None and prev[0] == name and v >= prev[1]:
                d = v - prev[1]
                if d and record:
                    self.store.add(day, name, d)
                    gained += d
        self.base = cur
        if gained and time.time() - self.t_flush >= self.FLUSH_EVERY:
            self.t_flush = time.time()
            self.store.flush()

    def snapshot(self, cycle_start, today):
        data = self.store.snapshot(cycle_start, today)
        for r in data["apps"]:
            r["blocked"] = r["app"] in self.blocked
        data.update(supported=self.supported, interval=self.INTERVAL, err=self.err)
        return data
