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

# 4. runtime deps straight into site-packages: psutil (usage) + tray/window extras (shell.py)
$sp = Join-Path $stage "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sp | Out-Null
& $Python -m pip install --quiet --target $sp psutil pystray pillow pywebview

# 5. the app itself
Copy-Item (Join-Path $root "dataguard.py") $stage
Copy-Item (Join-Path $root "dataguard") $stage -Recurse
$pyc = @(Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__")
if ($pyc) { $pyc | Remove-Item -Recurse -Force }

# 6. the staged app must actually run before we ship it
& (Join-Path $stage "python.exe") -c "import psutil, pystray, PIL, webview, dataguard; print('payload ok', psutil.__version__, dataguard.__version__)"
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
