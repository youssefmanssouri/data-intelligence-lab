<#
  CI helper (ephemeral runners only): runs a PowerShell script as a non-administrator
  local user, with that user's own profile and environment.

  It uses a Task Scheduler task with a stored password rather than
  Start-Process -Credential, which passes the caller's environment
  (USERPROFILE, APPDATA, TEMP) to the child.

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

$task = "dil-$Name"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`""
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeoutMinutes) -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $task -Action $action -Settings $settings -User "$env:COMPUTERNAME\$User" -Password $pw -RunLevel Limited -Force | Out-Null
Start-ScheduledTask -TaskName $task

$deadline = (Get-Date).AddMinutes($TimeoutMinutes + 1)
do {
    Start-Sleep -Seconds 5
    $state = (Get-ScheduledTask -TaskName $task).State
} while ((-not (Test-Path $exitFile)) -and $state -eq "Running" -and (Get-Date) -lt $deadline)
Start-Sleep -Seconds 2

if (Test-Path $log) { Get-Content $log | ForEach-Object { Write-Host $_ } }
$info = Get-ScheduledTaskInfo -TaskName $task
Unregister-ScheduledTask -TaskName $task -Confirm:$false
if (-not (Test-Path $exitFile)) { throw "Task '$task' produced no exit code (state $state, last result $($info.LastTaskResult))" }
$code = [int](Get-Content $exitFile -Raw).Trim()
Write-Host "Step '$Name' ran as $User and exited $code"
exit $code
