"""System tray icon + native dashboard window (pystray + pywebview). Optional: without them the browser still works."""

import ctypes
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
WM_SETICON, ICON_SMALL, ICON_BIG = 0x80, 0, 1
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
        small = u32.LoadImageW(None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        big = u32.LoadImageW(None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if small:
            _icon_handles.append(small)  # the window keeps pointing at it
            u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
            u32.SetClassLongPtrW(hwnd, GCLP_HICONSM, small)
        if big:
            _icon_handles.append(big)
            u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            u32.SetClassLongPtrW(hwnd, GCLP_HICON, big)
    except Exception:
        pass  # cosmetic: the default icon stays


_win_act = None  # run() installs this: the HTML titlebar buttons act on the open window


def win_action(act):
    """"min"/"close" from the HTML titlebar (via /api/win). No window open: no-op."""
    if _win_act is None:
        return False
    return bool(_win_act(act))


def _focus():
    if windll is None:
        return
    try:
        u32 = _user32()
        hwnd = u32.FindWindowW(None, TITLE)
        if hwnd:
            u32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def run(url, open_now=False):
    """Tray icon + dashboard window; blocks until Exit. Without the extras: browser + console, as before."""
    global _win_act
    if not HAVE_SHELL:
        if pystray is None or webview is None:
            print("Tip: pip install pystray pillow pywebview  (tray icon + app window)", flush=True)
        if open_now:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            return

    im = _icon_image()
    ico = _icon_file(im)
    opened, quitting = threading.Event(), threading.Event()
    holder = {"win": None, "broken": False}

    def _do_act(act):
        w = holder["win"]
        if w is None:
            return False
        if act == "min":
            w.minimize()
        elif act == "close":
            w.hide()  # parks in the tray; Exit really quits
        else:
            return False
        return True

    _win_act = _do_act

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

    def exit_app(icon, item):
        quitting.set()  # first: the closing handler checks this before parking in the tray
        opened.set()
        w = holder["win"]
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        try:
            icon.stop()
        except Exception:
            pass

    icon = pystray.Icon("dataguard", im, TITLE,
                        pystray.Menu(pystray.MenuItem("Open DataGuard", open_win, default=True),
                                     pystray.MenuItem("Exit", exit_app)))

    def run_tray():
        try:
            icon.run()
        except Exception as e:
            print(f"Tray icon failed: {e}", flush=True)

    threading.Thread(target=run_tray, daemon=True).start()
    if open_now:
        opened.set()

    try:
        while True:
            opened.wait(1.0)  # 1 s tick so console Ctrl+C stays responsive while idle
            if quitting.is_set():
                break
            if not opened.is_set():
                continue
            opened.clear()
            if holder["win"] is not None:
                continue
            try:
                w = webview.create_window(TITLE, url, width=1100, height=740, resizable=False,
                                          frameless=True, easy_drag=False)  # fixed size; the HTML draws its own titlebar

                def on_shown():
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
                print(f"App window unavailable: {e} - opening the dashboard in the browser.", flush=True)
                webbrowser.open(url)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _win_act = None
        try:
            icon.stop()
        except Exception:
            pass
