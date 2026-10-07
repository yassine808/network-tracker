"""Network interfaces: which one to count, and the Wi-Fi (hotspot) name."""

import re
import subprocess

from .common import IS_MAC, IS_WIN, NO_WINDOW, psutil

VIRTUAL = re.compile(
    r"loopback|^lo$|vethernet|vmware|virtualbox|vbox|hyper-v|docker|veth|virbr|br-|^tun|^tap|"
    r"^wg\d|utun|awdl|llw|bridge|bluetooth|isatap|teredo|pseudo|npcap|tailscale|zerotier", re.I)
WIFI = re.compile(r"wi-?fi|wlan|wlp\d|wlx|wireless|^wl\d|^ath\d|^en0$", re.I)


def run_quiet(cmd, timeout=4, enc=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", encoding=enc,
                           timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None


def get_ssid():
    """Wi-Fi network name: str; "" when not on Wi-Fi; None when it can't be determined."""
    if IS_WIN:
        out = run_quiet(["netsh", "wlan", "show", "interfaces"])
        m = re.search(r"^[ \t]*SSID[ \t]*:[ \t]*(.+?)[ \t]*$", out or "", re.M)  # BSSID lines don't match
        if m:
            return m.group(1)
        # netsh gave no name: on a fresh PC Windows 11 24H2 hides it until location access is
        # allowed. The connection profile name equals the SSID and needs no permission.
        ps = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                        "$i=@(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' -and "
                        "$_.PhysicalMediaType -match '802.11|Native' } | "
                        "ForEach-Object { $_.ifIndex });"
                        "Get-NetConnectionProfile | Where-Object { $i -contains $_.InterfaceIndex } | "
                        "Select-Object -First 1 -ExpandProperty Name"],
                       timeout=10, enc="utf-8")
        if ps and ps.strip():
            return ps.strip()
        ps = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                        "Get-NetConnectionProfile | Where-Object { $_.InterfaceAlias -match "
                        "'Wi-?Fi|WLAN|Wireless' } | Select-Object -First 1 -ExpandProperty Name"],
                       timeout=10, enc="utf-8")
        if ps and ps.strip():
            return ps.strip()
        # still nothing: only say "not on Wi-Fi" if no Wi-Fi adapter is up; otherwise unknown
        try:
            stats = psutil.net_if_stats()
            if any(WIFI.search(n) and s.isup for n, s in stats.items()):
                return None
        except Exception:
            return None
        return "" if out is not None else None
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