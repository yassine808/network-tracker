# DataGuard setup.exe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a standard Windows install wizard (`DataGuard-Setup-<version>.exe`) with a pre-checked "start at sign-in" checkbox, publish it to GitHub releases by extending the existing tag-triggered release job.

**Architecture:** Inno Setup wizard installs a portable payload (embeddable CPython 3.9 + psutil + the app) to `{localappdata}\Programs\DataGuard` with no admin rights. The autostart checkbox reuses the app's own `dataguard.py startup install` command (Startup-folder VBS → `pythonw.exe` hidden), so installer and CLI share one code path. `installer/build.ps1` stages the payload and runs ISCC; CI's release job calls the same script on every `v*` tag and attaches the exe next to the existing zip.

**Tech Stack:** Inno Setup 6 (ISCC), embeddable CPython 3.9.13, PowerShell 5.1, GitHub Actions windows-latest.

**Decisions (user-confirmed):** standard install wizard (not custom) / bundled python payload (not PyInstaller) / CI builds+attaches on tag push (not manual upload).

**Explicit non-goals:** no new smoke tests (the release job's build IS the gate; installing Inno into the checks job would tax every push), no custom app icon, no auto-update changes (separate TO-DO item), no kill-running-app-on-uninstall (known limitation: uninstalling while DataGuard runs may prompt to finish after reboot).

**Version rule:** CI enforces tag == `dataguard.__version__`; first release is `v1.0.0` (matches current `__version__ = "1.0.0"`), so no bump needed.

---

### Task 1: installer/setup.iss — the wizard

**Files:**
- Create: `installer/setup.iss`

- [ ] **Step 1: Write the Inno script**

```innosetup
; DataGuard installer. Compiled by installer/build.ps1, which stages build\payload first.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7B2E6D18-9C4A-4F3E-8A51-2D6F0E9B3C47}
AppName=DataGuard
AppVersion={#AppVersion}
AppPublisher=yassine808
DefaultDirName={localappdata}\Programs\DataGuard
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=DataGuard-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=DataGuard

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start DataGuard automatically when I sign in to Windows"; GroupDescription: "When Windows starts:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\build\payload\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\DataGuard"; Filename: "{app}\pythonw.exe"; Parameters: """{app}\dataguard.py"" run --open"; WorkingDir: "{app}"; Comment: "Open the DataGuard dashboard"
Name: "{userdesktop}\DataGuard"; Filename: "{app}\pythonw.exe"; Parameters: """{app}\dataguard.py"" run --open"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\python.exe"; Parameters: """{app}\dataguard.py"" startup install"; WorkingDir: "{app}"; Flags: runhidden selectedbytasks; StatusMsg: "Setting up auto-start..."
Filename: "{app}\pythonw.exe"; Parameters: """{app}\dataguard.py"" run --open"; WorkingDir: "{app}"; Description: "Start DataGuard now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\python.exe"; Parameters: """{app}\dataguard.py"" startup remove"; WorkingDir: "{app}"; RunOnceId: "RemoveStartup"; Flags: runhidden
```

Notes for the reviewer: `checkedonce` = pre-checked on first install, remembered on upgrade. `selectedbytasks` ties `startup install` to the checkbox. Uninstall runs `startup remove`, deleting the same VBS the CLI wrote.

- [ ] **Step 2:** No compile yet (needs Task 3's ISCC). Expected later: ISCC exits 0, `dist\DataGuard-Setup-1.0.0.exe` exists.

### Task 2: installer/build.ps1 — stage payload + compile

**Files:**
- Create: `installer/build.ps1`

- [ ] **Step 1: Write the build script**

```powershell
# Stages the portable payload (embeddable Python + psutil + app) and compiles installer/setup.iss.
param(
    [string]$Version = "",
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$root  = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $root "build\payload"
$dist  = Join-Path $root "dist"
$pyver = "3.9.13"

if (-not $Version) {
    $init = Get-Content (Join-Path $root "dataguard\__init__.py") -Raw
    $Version = [regex]::Match($init, '__version__\s*=\s*"([^"]+)"').Groups[1].Value
}
"Building DataGuard-Setup-$Version"

# 1. embeddable Python, cached between builds
$zip = Join-Path $root "build\python-$pyver-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $zip) | Out-Null
    "downloading embeddable python $pyver..."
    Invoke-WebRequest "https://www.python.org/ftp/python/$pyver/python-$pyver-embed-amd64.zip" -OutFile $zip
}

# 2. fresh payload folder
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Expand-Archive $zip -DestinationPath $stage

# 3. make Lib\site-packages importable (embeddable python ships with `import site` commented out)
if (-not (Select-String -Path (Join-Path $stage "python39._pth") -Pattern 'site-packages' -Quiet)) {
    Add-Content (Join-Path $stage "python39._pth") "Lib\site-packages`r`n"
}

# 4. psutil (the one runtime dependency) straight into site-packages
$sp = Join-Path $stage "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sp | Out-Null
& $Python -m pip install --quiet --target $sp psutil

# 5. the app itself
Copy-Item (Join-Path $root "dataguard.py") $stage
Copy-Item (Join-Path $root "dataguard") $stage -Recurse
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# 6. the staged app must actually run before we ship it
& (Join-Path $stage "python.exe") -c "import psutil, dataguard; print('payload ok', psutil.__version__, dataguard.__version__)"
if ($LASTEXITCODE -ne 0) { throw "payload smoke test failed" }

# 7. compile the installer
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "ISCC.exe not found - install Inno Setup 6 first (winget install JRSoftware.InnoSetup)" }
New-Item -ItemType Directory -Force -Path $dist | Out-Null
& $iscc "/DAppVersion=$Version" (Join-Path $root "installer\setup.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

$exe = Join-Path $dist "DataGuard-Setup-$Version.exe"
if (-not (Test-Path $exe)) { throw "expected output missing: $exe" }
"built: $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
Get-FileHash $exe -Algorithm SHA256
```

- [ ] **Step 2:** Syntax check: `powershell -NoProfile -Command "& { $null = [System.Management.Automation.Language.Parser]::ParseFile('installer/build.ps1', [ref]$null, [ref]$err); if ($err) { $err; exit 1 } else { 'parse ok' } }"` → expected `parse ok`.

### Task 3: Install Inno Setup locally (one-time, no UAC) + first build

**Files:** none (toolchain)

- [ ] **Step 1: Get the official installer URL**

```powershell
winget show JRSoftware.InnoSetup --accept-source-agreements | Select-String "Installer Url"
```
Expected: a `https://files.jrsoftware.org/is/6/innosetup-6.x.x.exe` URL.

- [ ] **Step 2: Download + silent per-user install (no UAC elevation needed)**

```powershell
$url = "<url from step 1>"
Invoke-WebRequest $url -OutFile "$env:TEMP\innosetup.exe"
Start-Process "$env:TEMP\innosetup.exe" -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER','/SP-' -Wait
Test-Path "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"   # expect True
```
If the CURRENTUSER default dir differs, check `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` too (build.ps1 probes all three).

- [ ] **Step 3: Build**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Projects\network-tracker\installer\build.ps1 -Python "C:\Users\ysmag\AppData\Local\Programs\Python\Python39\python.exe"
```
Expected: prints `payload ok <psutil> 1.0.0` then `built: ...\dist\DataGuard-Setup-1.0.0.exe (1x.x MB)`.

### Task 4: Local end-to-end install test

**Files:** none (verification)

- [ ] **Step 1: silent install with the autostart task ON**

```powershell
$setup = "D:\Projects\network-tracker\dist\DataGuard-Setup-1.0.0.exe"
$app   = "$env:LOCALAPPDATA\Programs\DataGuard"
$vbs   = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\DataGuard.vbs"
Start-Process $setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/TASKS="autostart"' -Wait
@(
  (Test-Path "$app\python.exe"),
  (Test-Path "$app\dataguard\dashboard.html"),
  (Test-Path $vbs)
) -join " "    # expect True True True
Get-Content $vbs   # expect one line: Run ""<app>\pythonw.exe"" ""<app>\dataguard.py"" run", 0, False
& "$app\python.exe" -c "import psutil; print('psutil', psutil.__version__)"    # expect psutil 7.x.x
& "$app\python.exe" "$app\dataguard.py" status   # expect the usual summary (reads shared app-data)
```

- [ ] **Step 2: silent uninstall + cleanup check**

```powershell
Start-Process "$app\unins000.exe" -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait
Start-Sleep 3
"vbs gone: $(-not (Test-Path $vbs))"    # expect True
"app gone: $(-not (Test-Path $app))"    # expect True (or empty shell; report actual)
```

- [ ] **Step 3: negative check — no autostart when the box is off**

```powershell
Start-Process $setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/TASKS=""' -Wait
Test-Path $vbs    # expect False
Start-Process "$app\unins000.exe" -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait
```

### Task 5: Wire the exe into the CI release job

**Files:**
- Modify: `.github/workflows/ci.yml` (release job, after the "Build the release file" step)

- [ ] **Step 1: insert the setup.exe build step**

```yaml
      - name: Build the setup.exe
        shell: pwsh
        run: |
          $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
          if (-not (Test-Path $iscc)) { choco install innosetup -y --no-progress }
          ./installer/build.ps1 -Version $env:GITHUB_REF_NAME -Python python
```

- [ ] **Step 2: extend the publish step to attach both assets**

```yaml
      - name: Publish the release
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
        run: >
          gh release create "$GITHUB_REF_NAME"
          "dist/dataguard-${GITHUB_REF_NAME}.zip"
          "dist/DataGuard-Setup-${GITHUB_REF_NAME}.exe"
          --title "$GITHUB_REF_NAME"
          --generate-notes
```

Note the version carries the `v` (`v1.0.0`) from `GITHUB_REF_NAME` through both filename and `AppVersion`; build.ps1 and the publish line use the same convention.

### Task 6: Repo hygiene, commit, push, checks green

- [ ] **Step 1: check `.gitignore`** for `build/` and `dist/`; add `/build/` and `/dist/` if missing.
- [ ] **Step 2: stage individually and commit**

```powershell
git -C D:\Projects\network-tracker add installer/setup.iss installer/build.ps1 .github/workflows/ci.yml
git -C D:\Projects\network-tracker add .gitignore    # only if changed
git -C D:\Projects\network-tracker commit -m "Add setup.exe installer built on release tags"
git -C D:\Projects\network-tracker push
```
Expected: `git status -sb` shows main synced after push; `gh run watch` (checks job) ends `success`.

### Task 7: Cut v1.0.0 and verify the published release

- [ ] **Step 1: tag + push (this triggers checks → release)**

```powershell
git -C D:\Projects\network-tracker tag -a v1.0.0 -m "DataGuard 1.0.0"
git -C D:\Projects\network-tracker push origin v1.0.0
gh run watch --repo yassine808/network-tracker (pick the tag run)   # expect success
```
- [ ] **Step 2: verify both assets**

```powershell
gh release view v1.0.0 --repo yassine808/network-tracker
```
Expected: assets `dataguard-v1.0.0.zip` AND `DataGuard-Setup-v1.0.0.exe`.
- [ ] **Step 3: download the published exe and confirm it matches the local build's SHA256** (proves CI produced the same artifact chain): `gh release download v1.0.0 --pattern "*.exe"` then `Get-FileHash` compare with local `dist\DataGuard-Setup-1.0.0.exe`... expect DIFFERENT hashes (timestamps) — instead just verify size is within ~1 MB of the local build and the downloaded exe's `/VERYSILENT` install smoke re-runs successfully. Report actual values.

### Task 8: Close out

- [ ] **Step 1:** `TO-DO.md`: change `- [] add setup.exe that auto starts when pc turns on.` → `- [x] add setup.exe that auto starts when pc turns on.` (commit with any fixups, push).
- [ ] **Step 2: Report:** what shipped, release URL, sizes, hashes, test results; surface the second open TO-DO item (remove auto-update; app must not use internet) as the likely next task — do NOT start it unasked.

---

## Self-Review
1. **Spec coverage:** wizard style ✓ (T1), autostart check mark ✓ (T1 tasks+T4 tests), bundled python ✓ (T2), publish to releases ✓ (T5+T7). Gap check: desktop shortcut included (bonus, unchecked by default); app opens dashboard post-install ✓.
2. **Placeholders:** one dynamic value (Inno download URL, T3 Step 1) is fetched at runtime by design, not a placeholder.
3. **Type consistency:** version flows `GITHUB_REF_NAME` → `-Version` → `/DAppVersion` → `OutputBaseFilename` → publish asset path, all in the same `v1.0.0` form ✓.
