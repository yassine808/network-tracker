"""Program icons as small PNGs. Pillow is optional: without it, or when extraction fails,
png() logs why and returns None so the server can answer with a generic glyph instead of a hole."""

import base64
import ctypes
import io
import logging
import os
import sys
import threading
import time

_cache = {}      # path -> [png bytes or None, when it was tried]
_lock = threading.Lock()
_work = threading.Lock()  # one extraction at a time: the shell and GDI fail under concurrent callers
FAIL_TTL = 60.0  # seconds a failed path is cached (never latch "no icon" for good)

# 64x64 neutral tile with a window glyph, rendered once and embedded so the fallback
# needs no Pillow at request time.
GENERIC = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAABZUlEQVR4nO3bMU7DMBjF8ee/MiIhLsKKxA04AEt7hq"
    "49BCtiY6VLD8ANkFi5EAikSFZqksYJiuPHT2qluv4cP9tVsyRohIfHl0+twH63Def2DbWEzl0Mag5/ToaQU1TTacAl"
    "/G/ZGOpQm25GZA6n3U9lpdvgos2MzCFzwfH4x5A5ZA6ZQ+aa3MLN/Z1Kcji+ZtUhc/zFTvTtxtv7x8+rygU4RMFTixA"
    "HL2URmGugVOC4LRW4hEVA5phroNS/Qtx2e3N98n2qbdUnYBMFTi1IHLiE8JPuA3LvD0oJ3kLmmtzCq8sL1QCZQ+aQO"
    "WQOmUPmkDlkDplD5pA5ZA6ZQ+aQOWQOmUPmyC18ej6qFFPmwlIXnsvUObD0BJa+dlPKRJaCzCFzyBwyx5gHjGqz323D"
    "/wmQOb7fHH8GbWa6DQ7irMgc8QeHU9DNyFCHmqSyhb6CWp4m6dtUcgvXYihDGDPYWk7EmI37AhBHdFslgcpPAAAAAElF"
    "TkSuQmCC"
)


def _image_cls():
    """PIL's Image, imported per call: a missing or broken Pillow install gets retried
    instead of being latched forever."""
    try:
        from PIL import Image
        return Image
    except Exception as e:
        logging.debug("icon: Pillow unavailable: %s", e)
        return None


def png(path):
    """PNG bytes for an .exe (or a .lnk carrying its icon), or None (logged, briefly cached)
    so callers can fall back. Normalized first: the shell rejects a path with forward
    slashes - Steam's registry records exactly that form."""
    if not path or not sys.platform.startswith("win"):
        return None
    path = os.path.normpath(path)
    now = time.time()
    with _lock:
        hit = _cache.get(path)
        if hit and (hit[0] is not None or now - hit[1] < FAIL_TTL):
            return hit[0]
    Image = _image_cls()
    if Image is None:
        data, why = None, "Pillow is not installed"
        logging.warning("icon: no icon for %s: %s (retry in %ds)", path, why, int(FAIL_TTL))
        with _lock:
            _cache[path] = [data, time.time()]
        return None
    with _work:
        with _lock:  # a request that waited in line may already have filled this entry
            hit = _cache.get(path)
            if hit and (hit[0] is not None or time.time() - hit[1] < FAIL_TTL):
                return hit[0]
        data, why = _extract(path, Image)
        if data is None and "Pillow" not in why:
            data, why = _extract(path, Image)  # shell/GDI hiccups are transient: try once more
        if data is None:
            logging.warning("icon: no icon for %s: %s (retry in %ds)", path, why, int(FAIL_TTL))
        with _lock:
            _cache[path] = [data, time.time()]
        return data


def _extract(path, Image):
    """(PNG bytes, "") or (None, why it failed)."""
    from ctypes import wintypes

    class SHFI(ctypes.Structure):
        _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                    ("dwAttributes", ctypes.c_uint), ("szDisplayName", ctypes.c_wchar * 260),
                    ("szTypeName", ctypes.c_wchar * 80)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                    ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", ctypes.c_uint),
                    ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint),
                    ("biClrImportant", ctypes.c_uint)]

    # local DLL handles: typing functions on the shared windll would leak into pywebview
    s32, u32, g32 = (ctypes.WinDLL("shell32"), ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32"))
    s32.SHGetFileInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_uint)
    s32.SHGetFileInfoW.restype = ctypes.c_void_p
    u32.GetIconInfo.argtypes = (wintypes.HICON, ctypes.POINTER(ICONINFO))
    u32.GetIconInfo.restype = wintypes.BOOL
    u32.DestroyIcon.argtypes = (wintypes.HICON,)
    u32.DestroyIcon.restype = wintypes.BOOL
    u32.GetDC.argtypes = (wintypes.HWND,)
    u32.GetDC.restype = wintypes.HDC
    u32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    u32.ReleaseDC.restype = ctypes.c_int
    g32.GetObjectW.argtypes = (wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p)
    g32.GetObjectW.restype = ctypes.c_int
    g32.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
    g32.GetDIBits.restype = ctypes.c_int
    g32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    g32.DeleteObject.restype = wintypes.BOOL

    fi = SHFI()
    SHGFI_ICON, SHGFI_LARGEICON = 0x100, 0
    try:
        if not s32.SHGetFileInfoW(path, 0, ctypes.byref(fi), ctypes.sizeof(fi),
                                  SHGFI_ICON | SHGFI_LARGEICON) or not fi.hIcon:
            return None, "the shell could not read that file"
        ii = ICONINFO()
        if not u32.GetIconInfo(fi.hIcon, ctypes.byref(ii)):
            return None, "the icon had no bitmap"
        try:
            if not ii.hbmColor:
                return None, "monochrome icon"  # 1-bit icon (mask only): rare, give up quietly
            bm = BITMAP()
            g32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
            w, h = int(bm.bmWidth), abs(int(bm.bmHeight))
            if not 0 < w <= 512 or not 0 < h <= 512:
                return None, f"odd icon size {w}x{h}"
            bi = BMIH()
            bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(BMIH), w, -h  # top-down
            bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
            buf = (ctypes.c_ubyte * (w * h * 4))()
            hdc = u32.GetDC(None)
            try:
                if g32.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(bi), 0) <= 0:
                    return None, "the icon's bitmap could not be read"
            finally:
                u32.ReleaseDC(None, hdc)
            img = Image.frombytes("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
            if not img.getextrema()[3][1]:  # alpha channel all zero: the icon carried no mask
                img.putalpha(255)
            out = io.BytesIO()
            img.save(out, "PNG")
            return out.getvalue(), ""
        finally:
            if ii.hbmColor:
                g32.DeleteObject(ii.hbmColor)
            if ii.hbmMask:
                g32.DeleteObject(ii.hbmMask)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        if fi.hIcon:
            u32.DestroyIcon(fi.hIcon)
