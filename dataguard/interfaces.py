"""Network interfaces: which one to count, and the Wi-Fi (hotspot) name."""

import re
import subprocess

from .common import IS_MAC, IS_WIN, NO_WINDOW

VIRTUAL = re.compile(
    r"loopback|^lo$|vethernet|vmware|virtualbox|vbox|hyper-v|docker|veth|virbr|br-|^tun|^tap|"
    r"^wg\d|utun|awdl|llw|bridge|bluetooth|isatap|teredo|pseudo|npcap|tailscale|zerotier", re.I)
WIFI = re.compile(r"wi-?fi|wlan|wlp\d|wlx|wireless|^wl\d|^ath\d|^en0$", re.I)


def run_quiet(cmd, timeout=4):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None


def get_ssid():
    """Wi-Fi network name: str; "" when not on Wi-Fi; None when it can't be determined."""
    if IS_WIN:
        out = run_quiet(["netsh", "wlan", "show", "interfaces"])
        if out is None:
            return None
        m = re.search(r"^[ \t]*SSID[ \t]*:[ \t]*(.+?)[ \t]*$", out, re.M)  # BSSID lines don't match; stays on one line
        return m.group(1) if m else ""
    if IS_MAC:
        out = run_quiet(["networksetup", "-getairportnetwork", "en0"])
        m = re.search(r"Network:\s*(.+?)\s*$", out or "", re.M)
        return m.group(1) if m else None  # recent macOS hides the name; treat as unknown
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


def auto_iface(names, stats, counters):
    """The interface that is probably the hotspot: the Wi-Fi adapter, else the busiest real one."""
    real = [n for n in names if not VIRTUAL.search(n)]
    wifi = [n for n in real if WIFI.search(n)]
    if wifi:
        return sorted(wifi, key=lambda n: not (n in stats and stats[n].isup))[0]
    up = [n for n in real if n in stats and stats[n].isup]
    pool = up or real or list(names)
    return max(pool, key=lambda n: counters[n].bytes_recv + counters[n].bytes_sent) if pool else None
