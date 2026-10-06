"""Checks GitHub once per start for a newer release, then installs it: unzip over the code, restart."""

import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import __version__

REPO = "yassine808/network-tracker"
API = os.environ.get("DG_UPDATE_API") or f"https://api.github.com/repos/{REPO}/releases/latest"
ROOT = Path(__file__).resolve().parents[1]  # the checkout: dataguard.py and dataguard/ sit here
MAX_ZIP = 50 * 1024 * 1024
HEADERS = {"User-Agent": f"DataGuard/{__version__}", "Accept": "application/vnd.github+json"}

_lock = threading.Lock()
_state = {"version": __version__, "latest": None, "url": "", "notes": "", "checked": False,
          "err": "", "busy": False}
_shutdown = None  # what cmd_run wants done before this process dies


def ver(tag):
    """(9, 9, 9) from 'v9.9.9' - so '1.10.0' beats '1.9.0' and a missing tag never wins."""
    return tuple(int(n) for n in re.findall(r"\d+", str(tag))[:4]) or (0,)


def status():
    """What the dashboard's About box shows."""
    with _lock:
        out = dict(_state)
    out["available"] = bool(out["latest"]) and ver(out["latest"]) > ver(out["version"])
    return out


def set_shutdown(fn):
    """Register the clean-stop routine to run right before a restart kills this process."""
    global _shutdown
    _shutdown = fn


def start_check(delay=6.0):
    """One request per app start: is there a newer release? Answers in a background thread."""
    def go():
        if delay:
            time.sleep(delay)
        try:
            with urlopen(Request(API, headers=HEADERS), timeout=15) as r:
                rel = json.loads(r.read())
            assets = rel.get("assets") or []
            url = next((a.get("browser_download_url") or "" for a in assets
                        if str(a.get("name") or "").lower().endswith(".zip")), "")
            with _lock:
                _state.update(latest=str(rel.get("tag_name") or "") or None, url=url,
                              notes=str(rel.get("body") or "")[:400], checked=True, err="")
            logging.info("update: latest release is %s (running %s)", _state["latest"], __version__)
        except HTTPError as e:
            with _lock:  # no releases published yet simply means there is nothing newer
                _state.update(checked=True, err="" if e.code == 404 else str(e))
            if e.code != 404:
                logging.info("update: check failed (%s)", e)
        except Exception as e:  # offline is normal, not an error worth waking anyone up for
            with _lock:
                _state.update(checked=True, err=str(e))
            logging.info("update: check failed (%s)", e)
    threading.Thread(target=go, daemon=True, name="update-check").start()


def install():
    """Download the release file and unpack it over this install. Returns (ok, why-not)."""
    with _lock:
        url = _state["url"]
        if _state["busy"]:
            return False, "an update is already installing"
        _state["busy"] = True
    try:
        if not url:
            raise ValueError("the release has no file to download")
        with urlopen(Request(url, headers=HEADERS), timeout=120) as r:
            blob = r.read(MAX_ZIP + 1)
        if len(blob) > MAX_ZIP:
            raise ValueError("the release file is bigger than it should be")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            unpack(zf, ROOT)
        logging.info("update: installed the files from %s", url)
        return True, ""
    except Exception as e:
        logging.warning("update: install failed: %s", e)
        return False, str(e)
    finally:
        with _lock:
            _state["busy"] = False


def unpack(zf, dest):
    """Unzip into dest: a single wrapping folder is peeled off, anything outside dest is refused."""
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise ValueError("the release file is empty")
    tops = {n.split("/", 1)[0] for n in names}
    strip = tops.pop() + "/" if len(tops) == 1 and all("/" in n for n in names) else ""
    root = Path(dest).resolve()
    for n in names:
        rel = n[len(strip):] if strip else n
        if not rel or ".." in Path(rel).parts or n.startswith(("/", "\\")) or ":" in rel.split("/", 1)[0]:
            raise ValueError(f"unsafe path in the release file: {n}")
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"outside the install folder: {n}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(n) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def restart():
    """Stop cleanly, start a fresh copy with the same arguments, and leave this one behind."""
    if _shutdown:
        try:
            _shutdown()
        except Exception:
            logging.exception("update: shutdown hook failed")
    cmd = [sys.executable, *sys.argv]
    try:
        subprocess.Popen(cmd, cwd=str(ROOT), env=dict(os.environ, DG_RESTART="1"))
    except Exception:
        logging.exception("update: could not start the new copy")
        return
    logging.info("update: restarting with: %s", " ".join(cmd))
    os._exit(0)
