<#
  wa-logon-task.ps1 - install/remove a logon Scheduled Task that re-applies
  wa-debloat.ps1 settings.

  WHY: an MSIX update or a package re-registration can reset the per-user
  StartupTask state. The task re-asserts it at every logon, so the debloat
  survives updates without you remembering anything.

  It runs ONLY the elevation-free items (Startup, WebView2) as the current
  user, so no admin prompt at logon. Hosts needs no re-applying - a package
  update never touches the hosts file.

  Install/Remove need elevation (Register-ScheduledTask).
#>
[CmdletBinding()]
param([ValidateSet('Install','Remove','Status','Run')][string]$Action = 'Status')

$ErrorActionPreference = 'Stop'

$TaskName = 'WhatsApp debloat (logon)'
$TaskPath = '\'
$Script   = Join-Path $PSScriptRoot 'wa-debloat.ps1'

if (-not (Test-Path $Script)) { throw "wa-debloat.ps1 not found next to this script: $Script" }

switch ($Action) {

 'Status' {
    $t = Get-ScheduledTask -TaskName $TaskName -EA SilentlyContinue
    if ($t) {
        "task    : $($t.TaskName)  state=$($t.State)"
        "action  : $($t.Actions.Execute) $($t.Actions.Arguments)"
        $i = Get-ScheduledTaskInfo -TaskName $TaskName -EA SilentlyContinue
        if ($i) { "lastrun : $($i.LastRunTime)  result=$($i.LastTaskResult)" }
    } else { 'task    : NOT INSTALLED' }
 }

 'Install' {
    $act = New-ScheduledTaskAction -Execute 'powershell.exe' `
           -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Action Apply -Items Startup,WebView2' -f $Script)
    $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
           -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $prn = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $act -Trigger $trg `
        -Settings $set -Principal $prn -Description 'Re-applies WhatsApp Desktop debloat settings after MSIX updates / re-registration.' -Force | Out-Null
    "INSTALLED: $TaskName"
 }

 'Remove' {
    if (Get-ScheduledTask -TaskName $TaskName -EA SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "REMOVED: $TaskName"
    } else { 'nothing to remove' }
 }

 'Run' {
    Start-ScheduledTask -TaskName $TaskName
    'triggered'
 }
}
