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
from . import __version__

TOP_LOCK = threading.Lock()


class Server(ThreadingHTTPServer):
    allow_reuse_address = not IS_WIN  # on Windows this flag would let a second copy hijack the port
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    mon = None     # set before serving
    tracker = None  # set before serving
    opener = None  # set by shell.run: shows the app window (a second launch asks for it)

    def log_message(self, fmt, *args):
        logging.debug("http: %s - %s", self.address_string(), fmt % args)

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
        logging.warning("http: rejected request with Host=%r (DNS-rebinding guard)",
                        self.headers.get("Host"))
        self._json({"error": "bad host"}, 403)  # blocks DNS-rebinding tricks from web pages
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        logging.debug("http: GET %s", self.path)
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
                s = self.mon.status()
                s["version"] = __version__
                self._json(s)
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
                                       "days": [], "err": "tracker not running"})
                start, _ = cycle_bounds(date.today(), self.mon.store.cfg["reset_day"])
                data = self.tracker.snapshot(start, date.today())
                self._json(data)
            elif path == "/api/app_icon":
                if self.tracker is None:
                    return self._json({"error": "tracker not running"}, 404)
                from . import icons  # Pillow may be missing: then, or on any failure, a generic glyph
                name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
                src = self.tracker.icon_src(name)  # the exe, or its shortcut when the exe hides
                data = icons.png(src)
                fallback = not data
                if fallback:
                    if not src:
                        logging.info("icon: no file known for %s, serving the generic icon", name)
                    data = icons.GENERIC
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                # an exe's icon never changes, but a generic glyph must not be stuck in the
                # browser for a day: it would outlive the short retry window on the server
                self.send_header("Cache-Control", "no-store" if fallback else "max-age=86400")
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
            path = self.path.split("?", 1)[0]
            logging.debug("http: POST %s body=%s", path, json.dumps(body)[:200])
            if path == "/api/open":  # a second `run --open`: raise this copy's window, not a browser tab
                if self.opener is None:
                    raise ValueError("this copy has no window to show")
                type(self).opener()  # via the class: a plain function stored here would bind to self
                return self._json({"ok": True})
            st = self.mon.store
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
                if self.tracker is None:
                    raise ValueError("the per-app tracker is not running")
                name = str(body.get("app") or "")
                if not name:
                    raise ValueError("no app given")
                on = bool(body.get("on"))
                ok, err = self.tracker.set_blocked(name, on)
                if not ok:
                    raise ValueError(err)
                # `active`: Windows enforces the block RIGHT NOW - false when the choice was
                # only remembered because this isn't the configured Wi-Fi (it applies there)
                return self._json({"ok": True, "active": on and bool(self.tracker.allowed())})
            elif path == "/api/kill":
                if body.get("skip"):  # the "about to be cut" warning was ignored: hands off until tomorrow
                    self.mon.skip_kill_today()
                else:
                    ok, err = self.mon.kill_now(bool(body.get("on")))
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
