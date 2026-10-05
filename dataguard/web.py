"""The local dashboard server: static page + JSON API on 127.0.0.1."""

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as e:
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
            else:
                return self._json({"error": "not found"}, 404)
            self._json({"ok": True})
        except (BrokenPipeError, ConnectionError):
            pass
        except (ValueError, TypeError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
