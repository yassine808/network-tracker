"""Network interfaces: which one to count, and the Wi-Fi (hotspot) name."""

import ctypes
import logging
import re
import subprocess
import sys
import time

from .common import IS_MAC, IS_WIN, NO_WINDOW, psutil

VIRTUAL = re.compile(
    r"loopback|^lo$|vethernet|vmware|virtualbox|vbox|hyper-v|docker|veth|virbr|br-|^tun|^tap|"
    r"^wg\d|utun|awdl|llw|bridge|bluetooth|isatap|teredo|pseudo|npcap|tailscale|zerotier", re.I)
# 802.11 covers USB dongles Windows names after their chipset ("802.11n USB Wireless LAN
# Adapter") - a real Wi-Fi adapter the old pattern missed, leaving its users unable to pin
# their hotspot
WIFI = re.compile(r"wi-?fi|wlan|wlp\d|wlx|wireless|802\.11|^wl\d|^ath\d|^en0$", re.I)


def _oem_enc():
    """cp + this machine's OEM code page, or None: netsh writes OEM bytes when its output is
    redirected (Python would decode them as ANSI, turning a non-ASCII SSID like "Café 5G"
    into mojibake that never matches)."""
    try:
        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return None


def run_quiet(cmd, timeout=4, enc=None):
    if sys.platform.startswith("win"):
        enc = enc or _oem_enc()  # default decoding: netsh writes OEM, not ANSI, when redirected
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", encoding=enc,
                           timeout=timeout, creationflags=NO_WINDOW)
        if r.returncode and not (r.stdout or "").strip():
            logging.debug("interfaces: %s exited %d", cmd[0], r.returncode)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug("interfaces: %s failed: %s", cmd[0], e)
        return None


def _netsh_ssid(blk):
    """The SSID line of one netsh interface block, trimmed exactly as the old lazy
    regex did (trailing spaces/tabs off; a whitespace-only value keeps its last char)."""
    for m in re.finditer(r"^[ \t]*SSID[ \t]*:", blk, re.M):  # BSSID lines don't match
        nl = blk.find("\n", m.end())
        tail = blk[m.end():] if nl < 0 else blk[m.end():nl]
        body = tail.lstrip(" \t")
        if body:
            return body.rstrip(" \t")
        if tail:
            return tail[-1]
    return None


def _netsh_name(out):
    """SSID across netsh adapters: prefer one whose State is "connected" (a disconnected
    adapter still prints its old SSID), fall back to the first one seen."""
    first, found = None, None
    for blk in re.split(r"(?m)^(?=Interface name)", out):
        name = _netsh_ssid(blk)
        if not name:
            continue
        if first is None:
            first = name
        if re.search(r"^[ \t]*State[ \t]*:[ \t]*connected[ \t]*$", blk, re.M | re.I):
            found = name
            break
    return found or first


def _profile_ssid(cmd, what):
    """Connection-profile name (equals the SSID); None when Windows won't show it."""
    out = run_quiet(cmd, timeout=10, enc="utf-8")
    if out and out.strip():
        logging.debug("get_ssid: %s -> %s", what, out.strip())
        return out.strip()
    return None


def _wifi_adapters_up():
    """Wi-Fi adapters that are up: the list, [] for none, None when psutil fails."""
    try:
        stats = psutil.net_if_stats()
        return [n for n, s in stats.items() if WIFI.search(n) and s.isup]
    except Exception:
        return None


def _ssid_win():
    """Windows: netsh first, then the connection-profile name, then an honest unknown."""
    out = run_quiet(["netsh", "wlan", "show", "interfaces"])
    if out:
        name = _netsh_name(out)
        if name:
            logging.debug("get_ssid: netsh -> %s", name)
            return name
    # netsh gave no name: on a fresh PC Windows 11 24H2 hides it until location access is
    # allowed. The connection profile name equals the SSID and needs no permission.
    name = _profile_ssid(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                          "$i=@(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' -and "
                          "$_.PhysicalMediaType -match '802.11|Native' } | "
                          "ForEach-Object { $_.ifIndex });"
                          "Get-NetConnectionProfile | Where-Object { $i -contains $_.InterfaceIndex } | "
                          "Select-Object -First 1 -ExpandProperty Name"],
                         "profile name")
    if name:
        return name
    name = _profile_ssid(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                          "Get-NetConnectionProfile | Where-Object { $_.InterfaceAlias -match "
                          "'Wi-?Fi|WLAN|Wireless|802.11' } | Select-Object -First 1 -ExpandProperty Name"],
                         "profile alias")
    if name:
        return name
    # still nothing: only say "not on Wi-Fi" if no Wi-Fi adapter is up; otherwise unknown
    up = _wifi_adapters_up()
    if up:
        logging.debug("get_ssid: Wi-Fi adapter %s up but name unreadable", up)
        return None
    if up is None:  # psutil failed: we learned nothing
        return None
    logging.debug("get_ssid: no Wi-Fi adapter up (netsh=%s)", "ran" if out is not None else "failed")
    return "" if out is not None else None


def _ssid_mac():
    out = run_quiet(["networksetup", "-getairportnetwork", "en0"])
    text = out or ""
    at = text.find("Network:")  # str ops instead of a regex: same leftmost match, no backtracking
    line = text[at + len("Network:"):].split("\n", 1)[0].lstrip(" \t") if at >= 0 else ""
    name = line.rstrip(" \t")
    return name if name else None  # recent macOS hides the name; treat as unknown


def _ssid_linux():
    out = run_quiet(["iwgetid", "-r"])
    if out is not None:
        return out.strip()
    out = run_quiet(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    if out is None:
        return None
    for line in out.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":")
    return ""


def get_ssid():
    """Wi-Fi network name: str; "" when not on Wi-Fi; None when it can't be determined."""
    t0 = time.monotonic()
    if IS_WIN:
        name, src = _ssid_win(), "win"
    elif IS_MAC:
        name, src = _ssid_mac(), "mac"
    else:
        name, src = _ssid_linux(), "linux"
    # one end-to-end line per read: the Settings "Use current Wi-Fi" button lands here last
    logging.debug("get_ssid: end-to-end %s -> %r in %.2fs", src, name, time.monotonic() - t0)
    return name


def auto_iface(names, stats, counters):
    """The interface that is probably the hotspot: the Wi-Fi adapter, else the busiest real one."""
    real = [n for n in names if not VIRTUAL.search(n)]
    wifi = [n for n in real if WIFI.search(n)]
    if wifi:
        return min(wifi, key=lambda n: not (n in stats and stats[n].isup))
    up = [n for n in real if n in stats and stats[n].isup]
    pool = up or real or list(names)
    return max(pool, key=lambda n: counters[n].bytes_recv + counters[n].bytes_sent) if pool else None
