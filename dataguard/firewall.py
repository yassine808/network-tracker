"""Windows Firewall: block one program, or cut this PC's internet. Changes need one Windows
UAC "yes" when DataGuard is not running as administrator; reading state never does."""

import ctypes
import logging
import os
import re
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

_SAFE = re.compile(r"[A-Za-z0-9_.:=/,+-]+$")
_LOCK = threading.Lock()  # one UAC prompt at a time


def is_admin():
    if not sys.platform.startswith("win"):
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                               capture_output=True, text=True, errors="replace",
                               timeout=60, creationflags=NO_WINDOW)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            return 1, ""
    ps = ("$ErrorActionPreference='Stop'; try { $p = Start-Process -FilePath 'powershell.exe' "
          "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%s' "
          "-Verb RunAs -Wait -WindowStyle Hidden -PassThru; exit $p.ExitCode } "
          "catch { Write-Output $_.Exception.Message; exit 3 }") % path.replace("'", "''")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
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
    the script instead (the WFP steps; the source exits non-zero itself on failure).
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


def block_app(name, exe):
    """Stop one program from reaching the internet. (ok, message). The netsh rule keeps the
    Windows Firewall UI informed; the WFP filter is what actually cuts the traffic."""
    if not exe:
        return False, "the program's file could not be found"
    if name.lower() in SYSTEM or exe.replace("\\", "/").rsplit("/", 1)[-1].lower() in SYSTEM:
        return False, "that is a Windows system service - blocking it could break the PC"
    rule = _rule_name(name)
    ok, msg = _apply([
        ["PS", wfp.add_script(exe, rule)],
        ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + rule],
        ["netsh", "advfirewall", "firewall", "add", "rule", "name=" + rule,
         "dir=out", "action=block", "program=" + exe, "enable=yes"]])
    if not ok:
        return ok, msg
    exists, _ = _rule_program(rule)
    if not exists:
        return False, "Windows Firewall did not keep the rule"
    if wfp.present(rule) is False:  # None = present but only the elevated writer could read it
        return False, "Windows did not keep the block filter"
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
    return _apply([
        ["PS", wfp.remove_script(rule)],
        ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + rule]])


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
