<#
  CI helper (ephemeral runners only): runs a PowerShell script as a non-administrator
  local user, with that user's own profile and environment.

  It uses CreateProcessWithLogonW (create_process_with_logon.py) with the
  user's profile, rather than Start-Process -Credential, which passes the
  caller's environment (USERPROFILE, APPDATA, TEMP) to the child.

  Usage: Invoke-AsStandardUser.ps1 -ScriptPath C:\dil-shared\step.ps1 -Name step [-TimeoutMinutes 40]
  The script's output goes to C:\dil-shared\<Name>.log and is printed; this exits with its exit code.
#>
param(
    [Parameter(Mandatory)] [string]$ScriptPath,
    [Parameter(Mandatory)] [string]$Name,
    [int]$TimeoutMinutes = 40
)
$ErrorActionPreference = "Stop"
$User = "dilstd"
$Shared = "C:\dil-shared"
$PwFile = Join-Path $env:RUNNER_TEMP "dilstd.pw"

if (-not (Get-LocalUser -Name $User -ErrorAction SilentlyContinue)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = [Convert]::ToBase64String($bytes) + "aA1!"
    Write-Host "::add-mask::$pw"
    Set-Content -Path $PwFile -Value $pw -NoNewline
    $secure = ConvertTo-SecureString $pw -AsPlainText -Force
    New-LocalUser -Name $User -Password $secure -PasswordNeverExpires -AccountNeverExpires -Description "Data Intelligence Lab standard-user CI account" | Out-Null
    $groups = (Get-LocalGroupMember -Group "Administrators" | Where-Object { $_.Name -like "*\$User" })
    if ($groups) { throw "$User must not be an administrator" }
}
$pw = Get-Content -Path $PwFile -Raw
New-Item -ItemType Directory -Force -Path $Shared | Out-Null
& icacls $Shared /grant "${User}:(OI)(CI)M" | Out-Null

$log = Join-Path $Shared "$Name.log"
$exitFile = Join-Path $Shared "$Name.exit"
Remove-Item $log, $exitFile -ErrorAction SilentlyContinue
$wrapper = Join-Path $Shared "$Name.wrapper.ps1"
@"
`$ErrorActionPreference = 'Continue'
& '$ScriptPath' *> '$log'
Set-Content -Path '$exitFile' -Value `$LASTEXITCODE
"@ | Set-Content -Path $wrapper -Encoding UTF8

# CreateProcessWithLogonW (LOGON_WITH_PROFILE, NULL environment): the child gets the user's own
# profile environment. (A Task Scheduler task never started for a fresh local user on the runners,
# and Start-Process -Credential would pass this admin session's environment.)
$python = Join-Path $env:pythonLocation "python.exe"
$helper = Join-Path $PSScriptRoot "create_process_with_logon.py"
$launcher = Start-Process -FilePath $python -PassThru -NoNewWindow -ArgumentList @(
    "`"$helper`"", "--user", $User, "--password-file", "`"$PwFile`"", "--cwd", "`"$Shared`"",
    "--timeout-minutes", $TimeoutMinutes,
    "--command", "`"powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper`"")  # $Shared has no spaces
# Stream the step's log while it runs, so a hang shows where it stopped.
$shown = 0
while (-not $launcher.HasExited) {
    Start-Sleep -Seconds 10
    if (Test-Path $log) {
        $lines = @(Get-Content $log -ErrorAction SilentlyContinue)
        for ($i = $shown; $i -lt $lines.Count; $i++) { Write-Host $lines[$i] }
        $shown = $lines.Count
    }
}
$launcher.WaitForExit()
$launchCode = $launcher.ExitCode
if (Test-Path $log) {
    $lines = @(Get-Content $log -ErrorAction SilentlyContinue)
    for ($i = $shown; $i -lt $lines.Count; $i++) { Write-Host $lines[$i] }
}
if (-not (Test-Path $exitFile)) { throw "Step '$Name' produced no exit code (launcher exit $launchCode)" }
$code = [int](Get-Content $exitFile -Raw).Trim()
Write-Host "Step '$Name' ran as $User and exited $code"
exit $code
