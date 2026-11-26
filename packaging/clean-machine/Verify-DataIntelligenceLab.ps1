<#
.SYNOPSIS
  Data Intelligence Lab v1.0 - clean Windows 10/11 verification (Milestone D).

.DESCRIPTION
  Run on a FRESH, disposable Windows 10 22H2 or Windows 11 VM (snapshot it first), from the
  unpacked verification kit, in a normal (non-elevated) PowerShell window:

      powershell -ExecutionPolicy Bypass -File .\Verify-DataIntelligenceLab.ps1

  Phase 1 (machine without the Visual C++ runtime, the normal state of a fresh install):
    confirms setup refuses to install and names the prerequisite, then stops and asks you
    to install the runtime from Microsoft. Run the script again afterwards.
  Phase 2 (runtime present): installs to the default per-user location, runs the automated
    smoke test against the installed copy with the app's own python.exe, guides you through
    the interactive checks (Start menu, browser, upload, closing the window stops the app),
    uninstalls, and confirms your datasets were preserved.

  Evidence is written to your Desktop as dil-verification-<build>-<time>.json. Send that
  file back; it contains no dataset contents, only checks, versions and paths.
  The script never downloads or installs anything except the Data Intelligence Lab setup
  you provide.
#>
[CmdletBinding()]
param(
    [string]$Setup = (Join-Path $PSScriptRoot "DataIntelligenceLab-Setup-1.0.0.exe"),
    [switch]$SkipInteractive
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$AppName = "Data Intelligence Lab"
$AppId = "{D1A7B1CE-1A8B-4C9D-9E5A-3A6D47C3B8E1}"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\${AppId}_is1"
$VcKey = "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
$VcUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$DefaultAppDir = Join-Path $env:LOCALAPPDATA "Programs\Data Intelligence Lab"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Data Intelligence Lab"
$UserStorage = Join-Path $env:USERPROFILE ".data-intelligence-lab\storage"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Work = Join-Path $env:TEMP "dil-verify-$Stamp"
New-Item -ItemType Directory -Force -Path $Work | Out-Null

$Evidence = [ordered]@{ kit = "clean-machine-v1"; started = (Get-Date).ToString("o"); checks = @() }
$Failed = $false

function Add-Check([string]$Name, [bool]$Ok, $Detail = "") {
    $script:Evidence.checks += [ordered]@{ name = $Name; result = $(if ($Ok) { "PASS" } else { "FAIL" }); detail = "$Detail" }
    if ($Ok) { Write-Host "CHECK ${Name}: PASS $Detail" -ForegroundColor Green }
    else { Write-Host "CHECK ${Name}: FAIL $Detail" -ForegroundColor Red; $script:Failed = $true }
}

function Save-Evidence {
    $script:Evidence.finished = (Get-Date).ToString("o")
    $script:Evidence.result = $(if ($script:Failed) { "FAIL" } else { "PASS" })
    $build = $script:Evidence.os.CurrentBuild
    $path = Join-Path ([Environment]::GetFolderPath("Desktop")) "dil-verification-$build-$Stamp.json"
    $json = $script:Evidence | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding($false)))
    Write-Host ""
    Write-Host "Evidence written to $path" -ForegroundColor Cyan
}

function Ask-YesNo([string]$Question) {
    if ($SkipInteractive) { return $null }
    while ($true) {
        $a = Read-Host "$Question [y/n]"
        if ($a -match '^(y|yes)$') { return $true }
        if ($a -match '^(n|no)$') { return $false }
    }
}

function Get-VcRuntime {
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, $view)
        $k = $base.OpenSubKey($VcKey)
        if ($k -and $k.GetValue("Installed") -eq 1) {
            return [ordered]@{ Major = $k.GetValue("Major"); Minor = $k.GetValue("Minor"); Bld = $k.GetValue("Bld") }
        }
    }
    return $null
}

function Invoke-Setup([string]$LogName, [string[]]$Extra = @()) {
    $log = Join-Path $Work $LogName
    $setupArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$log`"") + $Extra
    $p = Start-Process -FilePath $Setup -ArgumentList $setupArgs -Wait -PassThru
    return @{ ExitCode = $p.ExitCode; Log = $(if (Test-Path $log) { Get-Content $log -Raw } else { "" }) }
}

function Get-AppProcesses([string]$Dir) {
    $prefix = ($Dir.TrimEnd('\') + '\').ToUpperInvariant()
    Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.ToUpperInvariant().StartsWith($prefix) } catch { $false }
    }
}

# --- Machine facts ----------------------------------------------------------------
$cv = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$Evidence.os = [ordered]@{
    ProductName = $cv.ProductName; DisplayVersion = $cv.DisplayVersion; EditionID = $cv.EditionID
    CurrentBuild = $cv.CurrentBuild; UBR = $cv.UBR
    Family = $(if ([int]$cv.CurrentBuild -ge 22000) { "Windows 11" } else { "Windows 10" })
    Is64Bit = [Environment]::Is64BitOperatingSystem
    Culture = (Get-Culture).Name
}
$Evidence.account = [ordered]@{
    User = $env:USERNAME
    IsElevated = $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
$Evidence.powershell = $PSVersionTable.PSVersion.ToString()
Write-Host "$($Evidence.os.Family) build $($cv.CurrentBuild).$($cv.UBR) ($($cv.DisplayVersion)), user $env:USERNAME, elevated: $($Evidence.account.IsElevated)"

try { & {
    # --- Kit integrity ------------------------------------------------------------
    Add-Check "setup-present" (Test-Path $Setup) $Setup
    $sums = Join-Path $PSScriptRoot "SHA256SUMS"
    $actual = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLowerInvariant()
    $expected = $null
    if (Test-Path $sums) {
        $line = Get-Content $sums | Where-Object { $_ -match [regex]::Escape((Split-Path $Setup -Leaf)) } | Select-Object -First 1
        if ($line) { $expected = ($line -split '\s+')[0].ToLowerInvariant() }
    }
    $Evidence.setup_sha256 = $actual
    Add-Check "setup-hash-matches-kit" ($expected -eq $actual) "sha256 $actual"
    Add-Check "not-elevated" (-not $Evidence.account.IsElevated) "run from a normal PowerShell window"
    Add-Check "no-previous-install" (-not (Test-Path $UninstallKey) -and -not (Test-Path $DefaultAppDir)) $DefaultAppDir
    Get-ChildItem $PSScriptRoot -File | Unblock-File

    # --- Prerequisite --------------------------------------------------------------
    $vc = Get-VcRuntime
    $Evidence.vc_runtime = $vc
    $vcOk = $vc -and (($vc.Major -gt 14) -or ($vc.Major -eq 14 -and $vc.Minor -ge 44))
    if (-not $vcOk) {
        Write-Host ""
        Write-Host "Phase 1: the VC++ runtime is missing or older than 14.44 (found: $(if ($vc) { "$($vc.Major).$($vc.Minor).$($vc.Bld)" } else { 'none' }))." -ForegroundColor Yellow
        $r = Invoke-Setup "setup-without-vc-runtime.log"
        $gateLine = ($r.Log -split "`r?`n" | Where-Object { $_ -match "VC\+\+ runtime prerequisite not met" } | Select-Object -First 1)
        $Evidence.phase1 = [ordered]@{ exit_code = $r.ExitCode; log = "$gateLine" }
        Add-Check "setup-refuses-without-vc-runtime" (($r.ExitCode -ne 0) -and $gateLine -and -not (Test-Path $DefaultAppDir)) "exit $($r.ExitCode): $gateLine"
        if (-not $SkipInteractive) {
            Write-Host ""
            Write-Host "Now run the setup by double-clicking it once, to see the message a user gets." -ForegroundColor Yellow
            $Evidence.phase1.dialog_names_prerequisite = Ask-YesNo "Did setup show a message naming the Microsoft Visual C++ Redistributable and a Microsoft link, then close without installing?"
            if ($Evidence.phase1.dialog_names_prerequisite -eq $false) { $script:Failed = $true }
        }
        Write-Host ""
        Write-Host "Next: install the Microsoft Visual C++ Redistributable (x64) from $VcUrl" -ForegroundColor Cyan
        Write-Host "      then run this script again for phase 2." -ForegroundColor Cyan
        $Evidence.phase = 1
        return
    }
    $Evidence.phase = 2
    Add-Check "vc-runtime-meets-14.44" $true "$($vc.Major).$($vc.Minor).$($vc.Bld)"

    # --- Install (default per-user location) --------------------------------------
    $r = Invoke-Setup "install.log"
    Add-Check "install-exit-code" ($r.ExitCode -eq 0) $r.ExitCode
    Add-Check "installed-to-default-location" (Test-Path (Join-Path $DefaultAppDir "launcher.py")) $DefaultAppDir
    Add-Check "start-menu-shortcut" (Test-Path (Join-Path $StartMenuDir "$AppName.lnk"))
    Add-Check "uninstall-entry" (Test-Path $UninstallKey)

    # --- Automated smoke test with the installed runtime --------------------------
    $py = Join-Path $DefaultAppDir "runtime\python\python.exe"
    $smokeEvidence = Join-Path $Work "smoke-evidence.json"
    $smokeOut = Join-Path $Work "smoke-output.txt"
    $p = Start-Process -FilePath $py -ArgumentList @("-B", "`"$(Join-Path $PSScriptRoot 'smoke_test_bundle.py')`"",
            "--installed", "`"$DefaultAppDir`"", "--work-dir", "`"$(Join-Path $Work 'smoke')`"", "--evidence", "`"$smokeEvidence`"") `
         -Wait -PassThru -NoNewWindow -RedirectStandardOutput $smokeOut -RedirectStandardError (Join-Path $Work "smoke-error.txt")
    Get-Content $smokeOut | ForEach-Object { Write-Host "  $_" }
    if (Test-Path $smokeEvidence) {
        $Evidence.smoke = Get-Content $smokeEvidence -Raw | ConvertFrom-Json
        $Evidence.smoke.PSObject.Properties.Remove("launcher_output")
    }
    Add-Check "installed-smoke-test" ($p.ExitCode -eq 0) "exit $($p.ExitCode)"

    # --- Interactive checks: the real user path -----------------------------------
    if (-not $SkipInteractive) {
        Write-Host ""
        Write-Host "Interactive checks. Use the app as a user would; answer each question honestly." -ForegroundColor Cyan
        Write-Host "1. Open the Start menu and start '$AppName'." -ForegroundColor Cyan
        $Evidence.ui = [ordered]@{}
        $Evidence.ui.console_window_shows_url = Ask-YesNo "Did a console window open showing 'Frontend: http://127.0.0.1:<port>'?"
        $Evidence.ui.browser_opened_app = Ask-YesNo "Did your default browser open the Data Intelligence Lab page by itself?"
        Write-Host "2. In the browser, upload any small CSV file and open its profile." -ForegroundColor Cyan
        $Evidence.ui.upload_and_profile_work = Ask-YesNo "Did the upload succeed and the dataset profile page display?"
        $running = @(Get-AppProcesses $DefaultAppDir)
        Add-Check "app-processes-running-from-install" ($running.Count -ge 3) "$($running.Count) process(es)"
        Write-Host "3. Close the console window with its X button now, then press Enter here." -ForegroundColor Cyan
        [void](Read-Host "Press Enter after closing the console window")
        $deadline = (Get-Date).AddSeconds(20)
        do { Start-Sleep -Milliseconds 500; $left = @(Get-AppProcesses $DefaultAppDir) } while ($left.Count -gt 0 -and (Get-Date) -lt $deadline)
        Add-Check "closing-window-stops-app" ($left.Count -eq 0) "$($left.Count) process(es) left: $(($left | ForEach-Object { $_.ProcessName }) -join ', ')"
        foreach ($k in @($Evidence.ui.Keys)) { if ($Evidence.ui[$k] -eq $false) { $script:Failed = $true } }
    }

    # --- Uninstall preserves datasets --------------------------------------------------
    $sentinel = Join-Path $UserStorage "verification-sentinel.txt"
    New-Item -ItemType Directory -Force -Path $UserStorage | Out-Null
    Set-Content -Path $sentinel -Value "created by Verify-DataIntelligenceLab.ps1 $Stamp" -Encoding ASCII
    $before = @(Get-ChildItem $UserStorage -Recurse -File | ForEach-Object { "$($_.FullName)|$((Get-FileHash $_.FullName).Hash)" })
    $Evidence.storage_files_before_uninstall = $before.Count
    $u = Start-Process -FilePath (Join-Path $DefaultAppDir "unins000.exe") -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$(Join-Path $Work 'uninstall.log')`"") -Wait -PassThru
    $deadline = (Get-Date).AddSeconds(180)
    while (((Test-Path $DefaultAppDir) -or (Test-Path $UninstallKey)) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
    Add-Check "uninstall-exit-code" ($u.ExitCode -eq 0) $u.ExitCode
    Add-Check "install-folder-removed" (-not (Test-Path $DefaultAppDir))
    Add-Check "shortcuts-removed" (-not (Test-Path $StartMenuDir))
    Add-Check "uninstall-entry-removed" (-not (Test-Path $UninstallKey))
    $after = @(Get-ChildItem $UserStorage -Recurse -File | ForEach-Object { "$($_.FullName)|$((Get-FileHash $_.FullName).Hash)" })
    Add-Check "datasets-preserved" ((Compare-Object $before $after) -eq $null) "$($after.Count) file(s) unchanged"
    Remove-Item $sentinel -Force
} }
catch {
    Add-Check "script-error" $false $_.Exception.Message
}
finally {
    $Evidence.work_dir = $Work
    Save-Evidence
    if ($Failed) { Write-Host "RESULT: FAIL" -ForegroundColor Red } else { Write-Host "RESULT: PASS (phase $($Evidence.phase))" -ForegroundColor Green }
}
if ($Failed) { exit 1 }
exit 0
