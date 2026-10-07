# build_exe.ps1 -- build the onedir exe with PyInstaller
#
# Output: dist\WeChatFerryApp\WeChatFerryApp.exe  (ship the whole folder)
#
# Why onedir: fast start, lower AV false-positive rate than onefile.
# Why not sqlcipher3/zstandard: only used by scripts\db_backfill.py, not at runtime.
#
# NOTE (encoding): this file is intentionally pure ASCII. Windows PowerShell 5.1
# reads a BOM-less .ps1 as GBK, so non-ASCII comments/strings can break parsing.
# Keep it ASCII, or re-save with a UTF-8 BOM if you must add Chinese text.
#
# Usage:
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1
#     powershell ... -File .\build_exe.ps1 -Console
#     powershell ... -File .\build_exe.ps1 -Clean
#
[CmdletBinding()]
param(
    [string]$Name = 'WeChatFerryApp',
    [switch]$Console,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv python not found: $py" }

$dist = Join-Path $root 'dist'
$work = Join-Path $root 'build\pyinstaller'
if ($Clean) {
    Remove-Item $dist, $work -Recurse -Force -ErrorAction SilentlyContinue
}

$pyiArgs = @(
    '-m', 'PyInstaller'
    '--noconfirm'
    '--clean'
    '--onedir'
    '--name', $Name
    '--distpath', $dist
    '--workpath', $work
    '--specpath', $work
    '--add-data', "$root\web;web"
    '--collect-all', 'pystray'
    # Offline WeChat DB reading (contact names / group members / history backfill):
    # sqlcipher3 ~5.9 MB, zstandard ~1.6 MB -> cheap, and it makes the
    # "import names from WeChat DB" button work in the packaged app too.
    '--collect-all', 'sqlcipher3'
    '--collect-all', 'zstandard'
    '--hidden-import', 'pysilk'
    '--hidden-import', 'watchdog.observers.winapi'
    '--hidden-import', 'watchdog.observers.read_directory_changes'
    '--exclude-module', 'matplotlib'
    '--exclude-module', 'numpy'
    '--exclude-module', 'pandas'
    '--exclude-module', 'tkinter'
    '--exclude-module', 'PyQt5'
)

if (-not $Console) { $pyiArgs += '--windowed' }

$icon = Join-Path $root 'assets\app.ico'
if (Test-Path $icon) {
    $pyiArgs += @('--icon', $icon)
    Write-Host "[build] exe icon: $icon" -ForegroundColor Cyan
} else {
    Write-Warning "missing assets\app.ico - exe will use the default icon"
}

$pyiArgs += (Join-Path $root 'app_exe.py')

Write-Host "[build] PyInstaller ..." -ForegroundColor Cyan
& $py @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed, exit=$LASTEXITCODE" }

$exe = Join-Path $dist "$Name\$Name.exe"
if (-not (Test-Path $exe)) { throw "build finished but exe not found: $exe" }

# vendor\ is data, not a python package: keep only the copy next to the exe.
# If PyInstaller happened to collect it into _internal\vendor, drop that copy
# (it would double the package size by ~229 MB).
$internalVendor = Join-Path (Join-Path $dist $Name) '_internal\vendor'
if (Test-Path $internalVendor) {
    Remove-Item $internalVendor -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $internalVendor) {
        Write-Warning "_internal\vendor exists and could not be removed (locked?)"
    } else {
        Write-Host "[ok] removed _internal\vendor (avoid duplicating vendor)" -ForegroundColor Green
    }
}

$vendorSrc = Join-Path $root 'vendor'
$vendorDst = Join-Path (Join-Path $dist $Name) 'vendor'
if (Test-Path $vendorSrc) {
    Copy-Item $vendorSrc $vendorDst -Recurse -Force
    Write-Host "[ok] vendor copied next to exe" -ForegroundColor Green
} else {
    Write-Warning "vendor dir missing - the package will report no_vendor on setup"
}

$size = (Get-ChildItem (Join-Path $dist $Name) -Recurse -File |
         Measure-Object Length -Sum).Sum
Write-Host ""
Write-Host "[ok] $exe" -ForegroundColor Green
Write-Host ("     folder size: {0:N1} MB" -f ($size / 1MB))
Write-Host ""
Write-Host "Ship the whole $(Join-Path $dist $Name) folder." -ForegroundColor Cyan
