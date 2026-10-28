<#
  wa-fix-acl.ps1 - SYSTEM/Administrators ACEs on the WhatsApp AppX container folder.

  WHY: %LOCALAPPDATA%\Packages\<family> had only inherited user ACEs. AppXSvc
  runs as SYSTEM and could not build the Desktop AppX container:
      Microsoft-Windows-AppModel-Runtime/Admin
      id=214 0x80070005: Cannot create the Desktop AppX container ... creating the description
      id=208 0x80070005: Cannot create the process for package ... [LaunchProcess]
  Shell surfaced it as "Windows cannot access the specified device, path, or file."

  A healthy package (compare NVIDIACorp.NVIDIAControlPanel) has:
      NT AUTHORITY\SYSTEM:(OI)(CI)(F)   BUILTIN\Administrators:(OI)(CI)(F)

  Needs elevation. No reboot.
#>
[CmdletBinding()]
param([ValidateSet('Apply','Revert','Status')][string]$Action = 'Status')

$ErrorActionPreference = 'Stop'

$Fam = '5319275A.WhatsAppDesktop_cv1g1gvanyjgm'
$Dir = Join-Path $env:LOCALAPPDATA "Packages\$Fam"
$Bak = Join-Path $PSScriptRoot 'wa-acl-backup.txt'

if (-not (Test-Path $Dir)) { throw "not found: $Dir" }

# SIDs, not names - the box runs a non-English Windows.
$SidSystem = '*S-1-5-18'
$SidAdmins = '*S-1-5-32-544'

switch ($Action) {

 'Status' {
    icacls $Dir
    $acl = (icacls $Dir) -join "`n"
    $ok  = ($acl -match 'S-1-5-18|SYSTEM') -and ($acl -match 'S-1-5-32-544|Administrators|Администраторы')
    'verdict: {0}' -f $(if ($ok) { 'OK (SYSTEM + Administrators present)' } else { 'BROKEN (missing SYSTEM/Administrators)' })
 }

 'Apply' {
    icacls $Dir /save $Bak /t /c | Out-Null
    "ACL backup -> $Bak"
    icacls $Dir /grant "${SidSystem}:(OI)(CI)F" "${SidAdmins}:(OI)(CI)F"
    'APPLIED - launch WhatsApp from Start'
 }

 'Revert' {
    icacls $Dir /remove $SidSystem $SidAdmins
    'REVERTED (WhatsApp should fail with 0x80070005 again - that is the test)'
 }
}
