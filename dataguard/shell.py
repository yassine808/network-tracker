"""System tray icon + native dashboard window (pystray + pywebview). Optional: without them the browser still works."""

import ctypes
import logging
import os
import sys
import tempfile
import threading
import time
import webbrowser

try:
    from ctypes import windll
except ImportError:  # not Windows
    windll = None

try:
    import pystray
    from PIL import Image, ImageDraw
    import webview
    HAVE_SHELL = sys.platform != "darwin"  # macOS wants the tray on the main thread; this app is Windows-first
except ImportError:
    pystray = webview = None
    HAVE_SHELL = False

TITLE = "DataGuard"
WM_SETICON, WM_GETICON, ICON_SMALL, ICON_BIG = 0x80, 0x7F, 0, 1
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
GCLP_HICON, GCLP_HICONSM = -14, -34  # window-class icons: what the taskbar/Alt-Tab fall back to
_icon_handles = []  # SendMessage handed these to the window: they must outlive the call
_typed = False


def _user32():
    """user32 with real signatures - handles are 64-bit, bare ctypes calls would truncate them."""
    global _typed
    u32 = windll.user32
    if not _typed:
        from ctypes import wintypes
        u32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        u32.FindWindowW.restype = wintypes.HWND
        u32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        u32.SetForegroundWindow.restype = wintypes.BOOL
        u32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint)
        u32.LoadImageW.restype = wintypes.HANDLE
        u32.SendMessageW.argtypes = (wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t)
        u32.SendMessageW.restype = ctypes.c_size_t
        u32.SetClassLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_size_t)
        u32.SetClassLongPtrW.restype = ctypes.c_size_t
        _typed = True
    return u32


def _icon_image():
    """The dashboard's shield mark, 64 px RGBA."""
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(32, 5), (57, 14), (57, 33), (32, 59), (7, 33), (7, 14)], fill=(36, 86, 214, 255))
    d.line([(21, 32), (29, 40), (44, 23)], fill=(255, 255, 255, 255), width=8, joint="curve")
    return im


def _icon_file(im):
    """One .ico in %TEMP%, swapped atomically. Cosmetic: a failure just keeps the default icon."""
    path = os.path.join(tempfile.gettempdir(), "dataguard.ico")
    tmp = f"{path}.{os.getpid()}"
    try:
        im.save(tmp, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def _window_icon(path):
    """WM_SETICON + class icons: the shield on the taskbar, in Alt-Tab and in the titlebar."""
    if not path or windll is None:
        return
    try:
        u32 = _user32()
        hwnd = u32.FindWindowW(None, TITLE)
        if not hwnd:
            return
        # cx/cy pick the frame: 0 loads the file's first (16 px) image for both sizes
        small = u32.LoadImageW(None, path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        big = u32.LoadImageW(None, path, IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        if small:
            _icon_handles.append(small)  # the window keeps pointing at it
            u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
            u32.SetClassLongPtrW(hwnd, GCLP_HICONSM, small)
        if big:
            _icon_handles.append(big)
            u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            u32.SetClassLongPtrW(hwnd, GCLP_HICON, big)
        got = u32.SendMessageW(hwnd, WM_GETICON, ICON_BIG, 0)  # what the shell reads back
        if not (small and big and got):
            logging.warning("window icon partial: small=%s big=%s readback=%s",
                            bool(small), bool(big), bool(got))
    except Exception as e:
        logging.warning("window icon failed: %s", e)  # cosmetic, but never silent


def _dark_titlebar():
    """Let Windows draw the titlebar dark when the system is in dark mode (Win10 20H1+).
    Cosmetic: any failure just keeps the normal light titlebar."""
    if windll is None:
        return
    try:
        from ctypes import wintypes

        dark = 1  # the dashboard is always navy-dark now, so keep the chrome dark
        u32 = _user32()
        hwnd = u32.FindWindowW(None, TITLE)
        if not hwnd:
            return
        dwm = ctypes.WinDLL("dwmapi")  # private loader: windll stays untyped for pywebview
        dwm.DwmSetWindowAttribute.argtypes = (wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint)
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long  # HRESULT
        for attr in (20, 19):  # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (20H1+), 19 = the first build's value
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(dark)),
                                         ctypes.sizeof(ctypes.c_int)) == 0:
                break
    except Exception as e:
        logging.debug("shell: dark titlebar not applied (%s) - cosmetic", e)


def _focus():
    if windll is None:
        return
    try:
        u32 = _user32()
        hwnd = u32.FindWindowW(None, TITLE)
        if hwnd:
            u32.SetForegroundWindow(hwnd)
    except Exception as e:
        logging.debug("shell: could not focus window (%s)", e)


def _fallback(url, open_now):
    """No tray/window extras: browser + console, running until the process is asked to stop.
    Ctrl+C and SIGTERM propagate to cmd_run, which logs and shuts the app down cleanly."""
    if pystray is None or webview is None:
        print("Tip: pip install pystray pillow pywebview  (tray icon + app window)", flush=True)
    if open_now:
        webbrowser.open(url)
    while True:
        time.sleep(3600)


def _opener(url, holder, opened):
    """Tray and API handler that shows (or restores) the dashboard window."""
    def open_win(icon=None, item=None):
        if holder["broken"]:
            webbrowser.open(url)
            return
        w = holder["win"]
        if w is None:
            opened.set()
            return
        try:
            w.restore()
            w.show()
        except Exception:
            holder["broken"] = True
            webbrowser.open(url)
            return
        _focus()
    return open_win


def _exitter(quitting, opened, holder):
    """Tray Exit: stop the loop first, then tear the window and the icon down."""
    def exit_app(icon, item):
        quitting.set()  # first: the closing handler checks this before parking in the tray
        opened.set()
        w = holder["win"]
        if w is not None:
            try:
                w.destroy()
            except Exception as e:
                logging.debug("shell: window destroy failed on exit (%s)", e)
        try:
            icon.stop()
        except Exception as e:
            logging.debug("shell: tray icon stop failed on exit (%s)", e)
    return exit_app


def _tray_runner(icon):
    def run_tray():
        try:
            icon.run()
        except Exception as e:
            logging.exception("Tray icon failed: %s", e)
    return run_tray


def _dashboard(url, holder, quitting, ico):
    """One native window; any failure falls back to the browser, never to silence."""
    try:
        w = webview.create_window(TITLE, url, width=1100, height=740, resizable=False)

        def on_shown():
            _dark_titlebar()
            _window_icon(ico)
            threading.Timer(1.0, _window_icon, (ico,)).start()  # beat Windows' cached taskbar icon

        w.events.shown += on_shown

        def on_closing(ww=w, quit=quitting):
            if quit.is_set():
                return  # Exit asked for it: let the window really close
            threading.Thread(target=ww.hide, daemon=True).start()  # hides after this handler releases
            return False  # X parks in the tray instead of quitting

        w.events.closing += on_closing
        holder["win"] = w
        webview.start()  # main thread; returns when the window is destroyed
        holder["win"] = None
    except Exception as e:  # no WebView2, window failed to load, ...: browser still works
        holder.update(win=None, broken=True)
        logging.exception("App window unavailable: %s - opening the dashboard in the browser.", e)
        webbrowser.open(url)


def _await_window(opened, quitting, holder):
    """Block until Exit is asked (False) or the window should be created (True)."""
    while True:
        opened.wait(1.0)  # 1 s tick so console Ctrl+C stays responsive while idle
        if quitting.is_set():
            return False
        if opened.is_set():
            opened.clear()
            if holder["win"] is None:
                return True


def _stop_tray(icon):
    try:
        icon.stop()
    except Exception as e:
        logging.debug("shell: tray icon stop failed at teardown (%s)", e)


def _event_loop(url, holder, opened, quitting, ico, icon):
    try:
        while _await_window(opened, quitting, holder):
            _dashboard(url, holder, quitting, ico)
    finally:
        _stop_tray(icon)


def run(url, open_now=False):
    """Tray icon + dashboard window; blocks until Exit. Without the extras: browser + console, as before."""
    if not HAVE_SHELL:
        return _fallback(url, open_now)

    im = _icon_image()
    ico = _icon_file(im)
    opened, quitting = threading.Event(), threading.Event()
    holder = {"win": None, "broken": False}

    open_win = _opener(url, holder, opened)
    from .web import Handler
    Handler.opener = open_win  # a second `run --open` POSTs /api/open instead of opening a browser

    exit_app = _exitter(quitting, opened, holder)
    icon = pystray.Icon("dataguard", im, TITLE,
                        pystray.Menu(pystray.MenuItem("Open DataGuard", open_win, default=True),
                                     pystray.MenuItem("Exit", exit_app)))

    threading.Thread(target=_tray_runner(icon), daemon=True).start()
    if open_now:
        opened.set()

    _event_loop(url, holder, opened, quitting, ico, icon)
