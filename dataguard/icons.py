"""Program icons as small PNGs (optional: needs Pillow; any failure just means no icon)."""

import ctypes
import io
import logging
import sys
import threading

try:
    from PIL import Image
except ImportError:
    Image = None

_cache = {}
_lock = threading.Lock()


def png(path):
    """PNG bytes for an .exe, or None. Cached (failures too, so a dead path is only tried once)."""
    if not path or Image is None or not sys.platform.startswith("win"):
        return None
    with _lock:
        if path in _cache:
            return _cache[path]
    data = _extract(path)
    with _lock:
        _cache[path] = data
    return data


def _extract(path):
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
            return None
        ii = ICONINFO()
        if not u32.GetIconInfo(fi.hIcon, ctypes.byref(ii)):
            return None
        try:
            if not ii.hbmColor:
                return None  # 1-bit icon (mask only): rare, give up quietly
            bm = BITMAP()
            g32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
            w, h = int(bm.bmWidth), abs(int(bm.bmHeight))
            if not 0 < w <= 512 or not 0 < h <= 512:
                return None
            bi = BMIH()
            bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(BMIH), w, -h  # top-down
            bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
            buf = (ctypes.c_ubyte * (w * h * 4))()
            hdc = u32.GetDC(None)
            try:
                if g32.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(bi), 0) <= 0:
                    return None
            finally:
                u32.ReleaseDC(None, hdc)
            img = Image.frombytes("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
            if not img.getextrema()[3][1]:  # alpha channel all zero: the icon carried no mask
                img.putalpha(255)
            out = io.BytesIO()
            img.save(out, "PNG")
            return out.getvalue()
        finally:
            if ii.hbmColor:
                g32.DeleteObject(ii.hbmColor)
            if ii.hbmMask:
                g32.DeleteObject(ii.hbmMask)
    except Exception as e:
        logging.debug("icon for %s failed: %s", path, e)
        return None
    finally:
        if fi.hIcon:
            u32.DestroyIcon(fi.hIcon)
