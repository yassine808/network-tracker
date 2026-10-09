"""Network interfaces: which one to count, and the Wi-Fi (hotspot) name."""

import logging
import re
import subprocess

from .common import IS_MAC, IS_WIN, NO_WINDOW, psutil

VIRTUAL = re.compile(
    r"loopback|^lo$|vethernet|vmware|virtualbox|vbox|hyper-v|docker|veth|virbr|br-|^tun|^tap|"
    r"^wg\d|utun|awdl|llw|bridge|bluetooth|isatap|teredo|pseudo|npcap|tailscale|zerotier", re.I)
# 802.11 covers USB dongles Windows names after their chipset ("802.11n USB Wireless LAN
# Adapter") - a real Wi-Fi adapter the old pattern missed, leaving its users unable to pin
# their hotspot
WIFI = re.compile(r"wi-?fi|wlan|wlp\d|wlx|wireless|802\.11|^wl\d|^ath\d|^en0$", re.I)


def run_quiet(cmd, timeout=4, enc=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", encoding=enc,
                           timeout=timeout, creationflags=NO_WINDOW)
        if r.returncode and not (r.stdout or "").strip():
            logging.debug("interfaces: %s exited %d", cmd[0], r.returncode)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug("interfaces: %s failed: %s", cmd[0], e)
        return None


def get_ssid():
    """Wi-Fi network name: str; "" when not on Wi-Fi; None when it can't be determined."""
    if IS_WIN:
        out = run_quiet(["netsh", "wlan", "show", "interfaces"])
        if out:
            # several adapters: a DISCONNECTED one still prints its old SSID, so take the
            # name of an interface whose State is "connected"; fall back to the first seen
            first, found = None, None
            for blk in re.split(r"(?m)^(?=Interface name)", out):
                sm = re.search(r"^[ \t]*SSID[ \t]*:[ \t]*(.+?)[ \t]*$", blk, re.M)  # BSSID lines don't match
                if not sm or not sm.group(1):
                    continue
                if first is None:
                    first = sm.group(1)
                if re.search(r"^[ \t]*State[ \t]*:[ \t]*connected[ \t]*$", blk, re.M | re.I):
                    found = sm.group(1)
                    break
            name = found or first
            if name:
                logging.debug("get_ssid: netsh -> %s", name)
                return name
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
            logging.debug("get_ssid: profile name -> %s", ps.strip())
            return ps.strip()
        ps = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                        "Get-NetConnectionProfile | Where-Object { $_.InterfaceAlias -match "
                        "'Wi-?Fi|WLAN|Wireless|802.11' } | Select-Object -First 1 -ExpandProperty Name"],
                       timeout=10, enc="utf-8")
        if ps and ps.strip():
            logging.debug("get_ssid: profile alias -> %s", ps.strip())
            return ps.strip()
        # still nothing: only say "not on Wi-Fi" if no Wi-Fi adapter is up; otherwise unknown
        try:
            stats = psutil.net_if_stats()
            up = [n for n, s in stats.items() if WIFI.search(n) and s.isup]
            if up:
                logging.debug("get_ssid: Wi-Fi adapter %s up but name unreadable", up)
                return None
        except Exception:
            return None
        logging.debug("get_ssid: no Wi-Fi adapter up (netsh=%s)", "ran" if out is not None else "failed")
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