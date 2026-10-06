"""The local dashboard server: static page + JSON API on 127.0.0.1."""

import json
import logging
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .common import APP, IS_WIN
from .frontend import PAGE
from .interfaces import get_ssid
from .notify import notify
from .processes import top_processes
from .settings import cycle_bounds

TOP_LOCK = threading.Lock()


class Server(ThreadingHTTPServer):
    allow_reuse_address = not IS_WIN  # on Windows this flag would let a second copy hijack the port
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    mon = None     # set before serving
    tracker = None  # set before serving

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host in ("127.0.0.1", "localhost"):
            return True
        self._json({"error": "bad host"}, 403)  # blocks DNS-rebinding tricks from web pages
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                data = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/status":
                self._json(self.mon.status())
            elif path == "/api/top":
                if not TOP_LOCK.acquire(blocking=False):
                    return self._json({"error": "already measuring"}, 429)
                try:
                    self._json(top_processes(3.0, 8))
                finally:
                    TOP_LOCK.release()
            elif path == "/api/ssid":
                self._json({"ssid": get_ssid()})
            elif path == "/api/apps":
                if self.tracker is None:
                    return self._json({"supported": False, "apps": [], "count": 0,
                                       "total_today": 0, "total_cycle": 0, "days": [],
                                       "interval": 0, "err": "tracker not running"})
                start, _ = cycle_bounds(date.today(), self.mon.store.cfg["reset_day"])
                self._json(self.tracker.snapshot(start, date.today()))
            elif path == "/api/app_icon":
                if self.tracker is None:
                    return self._json({"error": "tracker not running"}, 404)
                from . import icons  # Pillow may be missing: png() then just says "no icon"
                name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
                data = icons.png(self.tracker.exe(name))
                if not data:
                    return self._json({"error": "no icon"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")  # an exe's icon never changes
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as e:
            logging.exception("GET %s failed", self.path)
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._host_ok():
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json({"error": "JSON only"}, 415)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                return self._json({"error": "request too large"}, 413)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            path, st = self.path.split("?", 1)[0], self.mon.store
            if path == "/api/config":
                st.set_config(body)
                self.mon.ssid_t = 0.0  # re-read the Wi-Fi name right away
            elif path == "/api/calibrate":
                gb_used = float(body.get("gb", -1))
                if not 0 <= gb_used <= 100000:
                    raise ValueError("gb must be a number from 0 up")
                st.calibrate(gb_used)
            elif path == "/api/notify-test":
                notify(APP, "Notifications are working.")
            elif path == "/api/reset":
                start = st.reset_cycle()
                if self.tracker is not None:
                    self.tracker.store.clear_since(start)
            elif path == "/api/block_app":
                from . import firewall  # Windows-only helper; the routes below fail politely elsewhere
                if self.tracker is None:
                    raise ValueError("the per-app tracker is not running")
                name = str(body.get("app") or "")
                if not name:
                    raise ValueError("no app given")
                if body.get("on"):
                    exe = self.tracker.exe(name)
                    ok, err = firewall.block_app(name, exe)
                    if not ok:
                        raise ValueError(err)
                    self.tracker.blocked.add(name)
                else:
                    ok, err = firewall.unblock_app(name)
                    if not ok:
                        raise ValueError(err)
                    self.tracker.blocked.discard(name)
            elif path == "/api/kill":
                on = bool(body.get("on"))
                ok, err = self.mon.kill_now(on)
                if not ok:
                    raise ValueError(err)
            else:
                return self._json({"error": "not found"}, 404)
            self._json({"ok": True})
        except (BrokenPipeError, ConnectionError):
            pass
        except (ValueError, TypeError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            logging.exception("POST %s failed", self.path)
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
