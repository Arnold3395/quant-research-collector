# Running this file is an explicit opt-in. Nothing is installed by run.ps1.
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('Daily','Weekly')][string]$Frequency = 'Daily',
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$At = '08:30',
    [ValidateSet('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')][string]$Day = 'Monday',
    [string]$TaskName = 'QuantResearchCollector',
    [string]$Python = '',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
if ($Remove) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    return
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task '$TaskName' already exists. Choose another name or explicitly remove the old task."
}
if (-not $Python) {
    $localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $localPython) { $Python = $localPython }
    elseif (Test-Path -LiteralPath $bundledPython) { $Python = $bundledPython }
    else { $Python = (Get-Command python -ErrorAction Stop).Source }
}
$Python = (Resolve-Path -LiteralPath $Python).Path
$version = & $Python -c 'import sys; print(sys.version_info >= (3,11))'
if ($LASTEXITCODE -ne 0 -or $version -ne 'True') { throw 'Python 3.11+ is required.' }
$scriptPath = Join-Path $PSScriptRoot 'collector.py'
$configPath = Join-Path $PSScriptRoot 'config.toml'
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$scriptPath`" run --config `"$configPath`"" -WorkingDirectory $PSScriptRoot
# Windows Task Scheduler interprets -At in the current Windows local time zone.
$runAt = [datetime]::Today.Add([timespan]::Parse($At))
if ($Frequency -eq 'Daily') { $trigger = New-ScheduledTaskTrigger -Daily -At $runAt }
else { $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $runAt }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$description = "Rule-based free research collection; no AI. Runs only when this user is logged in. Local Windows time zone: $([System.TimeZoneInfo]::Local.Id)."
if ($PSCmdlet.ShouldProcess("$TaskName : $Frequency at $At ($([System.TimeZoneInfo]::Local.Id))", 'Register scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description | Out-Null
    Write-Output "Registered $TaskName. Time zone: $([System.TimeZoneInfo]::Local.Id). User must be logged in; missed runs start when available."
}
