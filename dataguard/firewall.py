"""Windows Firewall: block one program, or cut this PC's internet. Changes need one Windows
UAC "yes" when DataGuard is not running as administrator; reading state never does.

Per-program blocking is layered, so one broken layer never leaves the program unblocked:
  1. WFP filter (wfp module)                  - primary: enforced by the filtering platform itself,
                                                even if a third-party firewall owns the engine or
                                                the Windows Firewall profile is off
  2. Windows Firewall rule (out + in)         - fallback + keeps the Windows Firewall UI informed
       netsh -> New-NetFirewallRule cmdlet -> HNetCfg.FwPolicy2 COM API (each tried only if the
       one before it failed)
The block counts as successful when at least one layer is in place; if only the firewall rule
took, the message says it may not be enforced."""

import base64
import ctypes
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading

from . import wfp
from .common import NO_WINDOW

CUT_RULE = "DataGuard-Internet-Cutoff"
ALLOW_RULE = "DataGuard-Allow-Local"
BLOCK_PREFIX = "DataGuard-Block-"
SEAL_RULE = "DataGuard-Self-Block-"
SEAL_ALLOW = "DataGuard-Self-Allow-"

# Blocking these would break Windows itself (DNS, logon, update...) - refuse politely.
SYSTEM = {"svchost.exe", "lsass.exe", "services.exe", "wininit.exe", "csrss.exe", "smss.exe",
          "winlogon.exe", "System", "Registry", "Memory Compression", "System Idle Process", "Idle"}
_SYSTEM_LOWER = {s.lower() for s in SYSTEM}  # the original compared lower-case names to a mixed-case set

_SAFE = re.compile(r"[A-Za-z0-9_.:=/,+-]+$")
_LOCK = threading.Lock()  # one UAC prompt at a time


def is_admin():
    if not sys.platform.startswith("win"):
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _q(s):
    """PowerShell single-quoted literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _powershell():
    """64-bit Windows PowerShell. The WFP structs are laid out for 64-bit; a 32-bit Python would
    otherwise start the 32-bit PowerShell (WOW64) and the filter structs would be corrupt."""
    sysnative = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Sysnative", "powershell.exe")
    if struct.calcsize("P") == 4 and os.path.isfile(sysnative):
        return sysnative
    return "powershell"


def _ps_line(argv):
    """One command as PowerShell source: bare when harmless, single-quoted otherwise, so a path
    with spaces reaches netsh as a single token (PowerShell quotes it for the native call)."""
    out = []
    for a in argv:
        a = a.replace("'", "''")
        out.append(a if _SAFE.match(a) else "'" + a + "'")
    return " ".join(out)


def _run_ps(path):
    """(exit code, output). Admin: run the script directly. Otherwise: one elevated launch (UAC;
    exit code 3 = the prompt was declined)."""
    if is_admin():
        try:
            r = subprocess.run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                               capture_output=True, text=True, errors="replace",
                               timeout=300, creationflags=NO_WINDOW)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            return 1, ""
    # FIX: Start-Process -ArgumentList does NOT quote array items, so a temp path containing a
    # space (e.g. a user name with a space) used to be split and the elevated run silently
    # failed. The script path is now wrapped in literal double quotes.
    ps = ("$ErrorActionPreference='Stop'; try { $p = Start-Process -FilePath 'powershell.exe' "
          "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%s\"' "
          "-Verb RunAs -Wait -WindowStyle Hidden -PassThru; exit $p.ExitCode } "
          "catch { Write-Output $_.Exception.Message; exit 3 }") % path.replace("'", "''")
    try:
        r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, errors="replace",
                           timeout=240, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 4, "timed out waiting for the Windows prompt"
    except OSError:
        return 1, ""


def _verdict(code, out):
    if code == 0:
        return True, ""
    if code == 3:
        return False, "Windows permission was declined"
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    return False, lines[-1] if lines else "Windows Firewall refused the change"


def _apply(argvs):
    """Run netsh command(s), elevating once when needed. add-rules must succeed; deletes are
    best-effort (a missing rule is fine). An entry ["PS", source] drops raw PowerShell into
    the script instead; that source may use $out (log file) and must exit by itself.
    Returns (ok, message)."""
    with _LOCK:
        fd, path = tempfile.mkstemp(prefix="dataguard-fw-", suffix=".ps1")
        log = path + ".log"
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                f.write("$ErrorActionPreference='Stop'\ntry {\n  $out = '%s'\n" % log.replace("'", "''"))
                for argv in argvs:
                    if argv and argv[0] == "PS":
                        f.write(argv[1])
                        if not argv[1].endswith("\n"):
                            f.write("\n")
                        continue
                    f.write("  " + _ps_line(argv) + " *> $out\n")
                    if len(argv) > 3 and argv[3] == "add":  # only a new rule may abort the sequence
                        f.write("  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n")
                if any(len(a) > 3 and a[3] == "add" for a in argvs):
                    f.write("  exit $LASTEXITCODE\n")
                else:
                    f.write("  exit 0\n")
                f.write("} catch {\n  Set-Content -LiteralPath $out -Value $_.Exception.Message\n"
                        "  exit 4\n}\n")
            code, out = _run_ps(path)
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as f:
                    out = out + "\n" + f.read()
            except OSError:
                pass
        finally:
            for p in (path, log):
                try:
                    os.remove(p)
                except OSError:
                    pass
    if code != 0:
        logging.warning("firewall: change failed (exit %s): %s", code, (out or "").strip()[-2000:])
    return _verdict(code, out)


def _rule_name(name):
    return BLOCK_PREFIX + "".join(c if c.isalnum() or c in ".-_" else "_" for c in name)[:80]


def _rule_program(rule):
    """(exists, program path) for a named rule. Read-only, works without admin. The path
    comes from the "Program:" line; on a non-English Windows that label won't match and
    the path reads as "" (meaning: it exists, but whether it is stale stays unknown)."""
    if not sys.platform.startswith("win"):
        return False, ""
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=" + rule],
                           capture_output=True, text=True, errors="replace",
                           timeout=6, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if r.returncode != 0:  # 1 = "No rules match" (in any language)
        return False, ""
    m = re.search(r"(?i)program:\s*(.+)", r.stdout or "")
    program = (m.group(1).strip() if m else "")
    if program.lower() == "any":
        program = ""
    return True, program


def _wfp_child(src):
    """PowerShell lines that run the WFP script in its OWN powershell process. The WFP source
    calls `exit` itself on failure; run inline it would end the whole script and skip every
    other layer. Leaves the child's exit code in $wfpCode."""
    blob = base64.b64encode(("\ufeff" + src).encode("utf-8")).decode("ascii")
    return ("  $w = Join-Path $env:TEMP ('dg-wfp-' + [guid]::NewGuid().ToString('N') + '.ps1')\n"
            "  [IO.File]::WriteAllBytes($w, [Convert]::FromBase64String('%s'))\n"
            "  & powershell -NoProfile -ExecutionPolicy Bypass -File $w *>> $out\n"
            "  $wfpCode = $LASTEXITCODE\n"
            "  Remove-Item -LiteralPath $w -Force -ErrorAction SilentlyContinue\n" % blob)


_RESET_TCP = r"""  try {
    if (-not ('DgTcp' -as [type])) {
      Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class DgTcp { [StructLayout(LayoutKind.Sequential)] public struct ROW { public uint state; public uint la; public uint lp; public uint ra; public uint rp; } [DllImport("iphlpapi.dll")] public static extern int SetTcpEntry(ref ROW r); }'
    }
    $ids = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -eq $exe } | ForEach-Object { $_.ProcessId })
    if ($ids.Count) {
      foreach ($c in @(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Where-Object { ($ids -contains $_.OwningProcess) -and ($_.LocalAddress -notmatch ':') })) {
        $r = New-Object DgTcp+ROW
        $r.state = 12
        $r.la = [BitConverter]::ToUInt32(([IPAddress]$c.LocalAddress).GetAddressBytes(), 0)
        $r.ra = [BitConverter]::ToUInt32(([IPAddress]$c.RemoteAddress).GetAddressBytes(), 0)
        $lp = [int]$c.LocalPort; $rp = [int]$c.RemotePort
        $r.lp = [uint32]((($lp -shl 8) -band 0xFF00) -bor (($lp -shr 8) -band 0xFF))
        $r.rp = [uint32]((($rp -shl 8) -band 0xFF00) -bor (($rp -shr 8) -band 0xFF))
        [void][DgTcp]::SetTcpEntry([ref]$r)
      }
    }
  } catch { Add-Content -LiteralPath $out -Value $_.Exception.Message }
"""
# A block filter only judges NEW connections. A long-lived connection that was already open
# (Avast keeps several to its cloud) would keep flowing, so the elevated block script also
# resets the program's existing IPv4 connections right after the filters are in place.


def _block_script(exe, rule):
    """Elevated script: every layer is tried independently, success if any layer holds."""
    return (
        "  $ErrorActionPreference = 'Continue'\n"
        "  $rule = %s\n  $exe = %s\n  $fw = $false\n"
        # ---- layer 1: netsh ------------------------------------------------------------
        "  netsh advfirewall firewall delete rule \"name=$rule\" *>> $out\n"
        "  netsh advfirewall firewall add rule \"name=$rule\" dir=out action=block "
        "\"program=$exe\" enable=yes profile=any *>> $out\n"
        "  if ($LASTEXITCODE -eq 0) {\n"
        "    $fw = $true\n"
        "    netsh advfirewall firewall add rule \"name=$rule\" dir=in action=block "
        "\"program=$exe\" enable=yes profile=any *>> $out\n"
        "  }\n"
        # ---- fallback A: NetSecurity cmdlets ------------------------------------------
        "  if (-not $fw) {\n"
        "    try {\n"
        "      Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | "
        "Remove-NetFirewallRule -ErrorAction SilentlyContinue\n"
        "      New-NetFirewallRule -DisplayName $rule -Direction Outbound -Program $exe "
        "-Action Block -Profile Any -Enabled True -ErrorAction Stop | Out-Null\n"
        "      $fw = $true\n"
        "      New-NetFirewallRule -DisplayName $rule -Direction Inbound -Program $exe "
        "-Action Block -Profile Any -Enabled True -ErrorAction SilentlyContinue | Out-Null\n"
        "    } catch { Add-Content -LiteralPath $out -Value $_.Exception.Message }\n"
        "  }\n"
        # ---- fallback B: COM firewall API ----------------------------------------------
        "  if (-not $fw) {\n"
        "    try {\n"
        "      $pol = New-Object -ComObject HNetCfg.FwPolicy2\n"
        "      foreach ($d in 2, 1) {\n"
        "        $r = New-Object -ComObject HNetCfg.FWRule\n"
        "        $r.Name = $rule; $r.ApplicationName = $exe; $r.Action = 0\n"
        "        $r.Direction = $d; $r.Profiles = 0x7FFFFFFF; $r.Enabled = $true\n"
        "        $pol.Rules.Add($r)\n"
        "        $fw = $true\n"
        "      }\n"
        "    } catch { Add-Content -LiteralPath $out -Value $_.Exception.Message }\n"
        "  }\n"
        # ---- layer 2: WFP filter (independent of the firewall profile state) -----------
        % (_q(rule), _q(exe))
    ) + _wfp_child(wfp.add_script(exe, rule)) + _RESET_TCP + (
        "  if ($fw -or $wfpCode -eq 0) { exit 0 }\n"
        "  Add-Content -LiteralPath $out -Value 'no blocking layer could be installed'\n"
        "  exit 5\n")


def _unblock_script(rule):
    return (
        "  $ErrorActionPreference = 'Continue'\n"
        "  $rule = %s\n"
        "  netsh advfirewall firewall delete rule \"name=$rule\" *>> $out\n"
        "  try { Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | "
        "Remove-NetFirewallRule -ErrorAction SilentlyContinue } catch {}\n"
        % _q(rule)
    ) + _wfp_child(wfp.remove_script(rule)) + "  exit $wfpCode\n"


def can_block(name, exe):
    """(ok, message): whether this program may be blocked at all - its file must be found
    and it must never be a Windows system service (blocking one could break the PC)."""
    if not exe:
        return False, "the program's file could not be found"
    base = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.lower() in _SYSTEM_LOWER or base in _SYSTEM_LOWER:
        return False, "that is a Windows system service - blocking it could break the PC"
    return True, ""


def block_app(name, exe):
    """Stop one program from reaching the internet. (ok, message)."""
    ok, msg = can_block(name, exe)
    if not ok:
        return False, msg
    rule = _rule_name(name)
    ok, msg = _apply([["PS", _block_script(exe, rule)]])
    if not ok:
        return ok, msg
    rule_ok = _rule_program(rule)[0]
    wfp_ok = wfp.present(rule) is not False  # None = present but only the elevated writer can read it
    if not rule_ok and not wfp_ok:
        return False, "Windows did not keep the block"
    if wfp.present(rule) is False:
        # The WFP filter is what the platform enforces; a netsh rule alone can be ignored when a
        # third-party firewall controls the filter engine, so say so instead of claiming success.
        logging.warning("firewall: %s has only the Windows Firewall rule (WFP filter missing)", name)
        return True, "blocked by a Windows Firewall rule only - a third-party firewall may ignore it"
    if not rule_ok:
        logging.warning("firewall: %s blocked by the WFP filter only (no firewall rule)", name)
    return True, ""


def ensure_block(name, exe):
    """(ok, message): make sure the block rule and its filter are present and point at this
    file. Read-only when all is well, so re-applying at startup costs no Windows prompt; a
    missing or stale rule (app updated, firewall reset) is rebuilt through block_app."""
    if not exe:
        return False, "the program's file could not be found"
    rule = _rule_name(name)
    exists, program = _rule_program(rule)
    if (exists and (not program or os.path.normcase(program) == os.path.normcase(exe))
            and wfp.present(rule) is not False):
        return True, ""
    return block_app(name, exe)


def unblock_app(name):
    """Let a program reach the internet again. (ok, message)."""
    rule = _rule_name(name)
    ok, msg = _apply([["PS", _unblock_script(rule)]])
    if _rule_program(rule)[0]:
        return False, msg or "Windows Firewall still has the block rule"
    return ok, msg


def has_block(name):
    """True when Windows still enforces a block for this app (firewall rule or WFP filter).
    Read-only, never prompts; a filter state that cannot be read counts as present, so
    cleanup never skips a block that might be there."""
    if not sys.platform.startswith("win"):
        return False
    rule = _rule_name(name)
    return _rule_program(rule)[0] or wfp.present(rule) is not False


def _child(src):
    """PowerShell lines that run `src` in its OWN powershell child process - the sources
    built for block/unblock `exit` themselves and would end the whole script otherwise
    (the same trick as _wfp_child)."""
    blob = base64.b64encode(("\ufeff" + src).encode("utf-8")).decode("ascii")
    return ("  $c = Join-Path $env:TEMP ('dg-run-' + [guid]::NewGuid().ToString('N') + '.ps1')\n"
            "  [IO.File]::WriteAllBytes($c, [Convert]::FromBase64String('%s'))\n"
            "  & powershell -NoProfile -ExecutionPolicy Bypass -File $c *>> $out\n"
            "  Remove-Item -LiteralPath $c -Force -ErrorAction SilentlyContinue\n" % blob)


def sync_app_blocks(items):
    """(name, ok, message) for every (name, exe, block) entry of items. States are compared
    read-only first and only the differences are written - all of them in ONE change, so a
    network flip costs at most one Windows prompt when DataGuard is not elevated, and none
    when every state already matches. The read-only re-check after the change is the
    verdict: the batch's single exit code cannot say which layer took on which app."""
    if not sys.platform.startswith("win"):
        return [(n, False, "per-app blocking runs on Windows only") for n, _, _ in items]
    verdicts, todo = {}, []
    for name, exe, block in items:
        rule = _rule_name(name)
        try:
            if block:
                exists, program = _rule_program(rule)
                if (exists and (not program or os.path.normcase(program) == os.path.normcase(exe))
                        and wfp.present(rule) is not False):
                    verdicts[name] = (True, "")  # already in place: nothing to ask Windows for
                    continue
                ok, msg = can_block(name, exe)
                if not ok:
                    verdicts[name] = (False, msg)
                    continue
            elif not has_block(name):
                verdicts[name] = (True, "")
                continue
            todo.append((name, exe, block, rule))
        except Exception as e:
            verdicts[name] = (False, f"{type(e).__name__}: {e}")
    if todo:
        logging.info("firewall: syncing %d block state(s) in one change (%d on, %d off)",
                     len(todo), sum(1 for t in todo if t[2]), sum(1 for t in todo if not t[2]))
        src = "".join(_child(_block_script(exe, rule) if block else _unblock_script(rule))
                      for _, exe, block, rule in todo)
        _apply([["PS", src]])  # the read-only re-check below decides, not this exit code
        for name, exe, block, rule in todo:
            try:
                if block:
                    ok = _rule_program(rule)[0] or wfp.present(rule) is not False
                    verdicts[name] = (True, "") if ok else (False, "Windows did not keep the block")
                else:
                    ok = not has_block(name)
                    verdicts[name] = (True, "") if ok else (False, "Windows still has the block")
            except Exception as e:
                verdicts[name] = (False, f"{type(e).__name__}: {e}")
    return [(n, *verdicts.get(n, (False, "state was not checked"))) for n, _, _ in items]


def cut_active():
    """True when the whole-PC cutoff is in place. Read-only; works without admin."""
    return _rule_program(CUT_RULE)[0]


def cut_internet():
    """Block all outbound internet, keeping loopback and the local network usable (the dashboard
    and the printer keep working). (ok, message)."""
    return _apply([
        ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + ALLOW_RULE],
        ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + CUT_RULE],
        ["netsh", "advfirewall", "firewall", "add", "rule", "name=" + ALLOW_RULE,
         "dir=out", "action=allow", "remoteip=127.0.0.1,localsubnet"],
        ["netsh", "advfirewall", "firewall", "add", "rule", "name=" + CUT_RULE,
         "dir=out", "action=block", "remoteip=any"]])


def restore_internet():
    """Remove the cutoff rules. Deletes are best-effort: already gone counts as success."""
    logging.info("firewall: restoring internet access")
    return _apply([
        ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + CUT_RULE],
        ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + ALLOW_RULE]])


def _seal_names(exe):
    base = exe.replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in base)[:60]
    return SEAL_RULE + safe, SEAL_ALLOW + safe


def seal_app(exes):
    """(ok, message): keep DataGuard's own interpreter(s) off the internet while loopback
    stays allowed (the dashboard and the CLI still work). Already sealed = read-only, no
    Windows prompt; only a missing or stale rule is rebuilt."""
    if not sys.platform.startswith("win"):
        return False, "only Windows has this firewall"
    argvs = []
    for exe in exes:
        if not exe or not os.path.isfile(exe):
            continue
        block, allow = _seal_names(exe)
        for rule, act, extra in ((allow, "allow", ["remoteip=127.0.0.1,localsubnet"]),
                                 (block, "block", [])):
            exists, program = _rule_program(rule)
            if exists and (not program or os.path.normcase(program) == os.path.normcase(exe)):
                continue
            argvs += [["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + rule],
                      ["netsh", "advfirewall", "firewall", "add", "rule", "name=" + rule,
                       "dir=out", "action=" + act, "program=" + exe] + extra]
    if not argvs:
        return True, ""  # already sealed: nothing to ask Windows for
    ok, msg = _apply(argvs)
    if not ok:
        return ok, msg
    for exe in exes:
        if exe and os.path.isfile(exe) and not _rule_program(_seal_names(exe)[0])[0]:
            return False, "Windows Firewall did not keep the self-block rule"
    logging.info("firewall: sealed DataGuard from the internet (%s)", ", ".join(exes))
    return True, ""
