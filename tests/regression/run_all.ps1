# run_all.ps1 -- run every regression suite in this folder and print a summary.
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads a BOM-less .ps1 as
# GBK, so non-ASCII text here can break parsing.
#
# Usage:
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\regression\run_all.ps1
#     powershell ... -File .\tests\regression\run_all.ps1 -Filter pergroup
#     powershell ... -File .\tests\regression\run_all.ps1 -IncludeEnvDependent
#
# Suites skipped by default (they need things a dev sandbox does not have):
#   wft_tray_check      - creating a tray icon is denied by the DSH sandbox (WinError 5)
#   wft_live_check      - probes a LIVE running app / real account data
#   wft_discover_check  - OBSOLETE, superseded by wft_discover_check2 (old data-root rule)
#
[CmdletBinding()]
param(
    [string]$Filter = '',
    [switch]$IncludeEnvDependent
)

$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$root = Split-Path (Split-Path $here -Parent) -Parent      # <repo> = parent of tests\
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv python not found: $py" }

$envDependent = @('wft_tray_check', 'wft_live_check', 'wft_discover_check')

$suites = Get-ChildItem $here -Filter 'wft_*_check.py' -File | Sort-Object Name
if ($Filter) { $suites = $suites | Where-Object { $_.Name -like "*$Filter*" } }

$skipped = @()
if (-not $IncludeEnvDependent) {
    $skipped = $suites | Where-Object { $envDependent -contains $_.BaseName }
    $suites = $suites | Where-Object { $envDependent -notcontains $_.BaseName }
}

$outDir = Join-Path $env:TEMP 'wft-regression-logs'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$failed = @()
try {
    foreach ($s in $suites) {
        $log = Join-Path $outDir ($s.BaseName + '.log')
        & $py -B $s.FullName *> $log
        $code = $LASTEXITCODE
        $tail = (Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue |
                 Select-String -Pattern 'FAILED|ALL PASSED|REG RESSION|PASSED' |
                 Select-Object -Last 1)
        $status = if ($code -eq 0) { 'OK  ' } else { 'FAIL' }
        Write-Output ("[{0}] {1}  {2}" -f $s.BaseName, $status, $tail)
        if ($code -ne 0) {
            $failed += $s.BaseName
            Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue |
                Select-String -Pattern '  FAIL' | Select-Object -First 3 |
                ForEach-Object { Write-Output ("        " + $_.Line.Trim()) }
        }
    }
}
finally {
    # Sweep the scratch dirs every suite creates ('.wft-*' next to the repo).
    # Suites clean up after themselves, but a FAILING suite never reaches its
    # rmtree, and a few suites (_quit/_reload/_discover/_e6/_frozen_root) have no
    # cleanup at all -- they used to pile up (~34 MB) in D:\projects.
    # Runs after the last suite, so nothing is still using these dirs.
    $scratchRoot = Split-Path $root -Parent
    $swept = 0
    Get-ChildItem $scratchRoot -Filter '.wft-*' -Directory -Force -ErrorAction SilentlyContinue |
        ForEach-Object {
            try { Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop; $swept++ } catch { }
        }
    if ($swept) { Write-Output ("swept {0} scratch dir(s) under {1}" -f $swept, $scratchRoot) }
}

Write-Output ""
Write-Output ("logs: " + $outDir)
if ($skipped.Count) {
    Write-Output ("skipped (env dependent, use -IncludeEnvDependent): " + (($skipped | ForEach-Object { $_.BaseName }) -join ', '))
}
if ($failed.Count) {
    Write-Output ("FAILED SUITES (" + $failed.Count + "): " + ($failed -join ', '))
    exit 1
}
Write-Output ("ALL " + $suites.Count + " SUITES PASSED")
