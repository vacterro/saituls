<#
  wa-fix-wns.ps1 - Windows Push Notification platform (WNS) on/off.

  WHY: a debloater had disabled WpnService/WpnUserService and set the
  PushNotifications policies. WhatsApp Desktop (WinUI3) then dies at startup:
      COMException[-2143420155] = 0x803E0105 WPN_E_PLATFORM_UNAVAILABLE
      at WhatsApp.App.EnsureMainWindowInitialized()

  Apply  = restore WNS (what WhatsApp needs).
  Revert = put back the debloated state (services disabled + policies).

  Needs elevation. Reboot (or `net start WpnService`) after Apply.
#>
[CmdletBinding()]
param([ValidateSet('Apply','Revert','Status')][string]$Action = 'Status')

$ErrorActionPreference = 'Stop'

$HklmPol = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\PushNotifications'
$HkcuPol = 'HKCU:\Software\Policies\Microsoft\Windows\CurrentVersion\PushNotifications'
$HkcuPn  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications'
$HkcuExp = 'HKCU:\Software\Policies\Microsoft\Windows\Explorer'

function Get-WpnKeys {
    $k = @('HKLM:\SYSTEM\CurrentControlSet\Services\WpnService',
           'HKLM:\SYSTEM\CurrentControlSet\Services\WpnUserService')
    $k += (Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Services' |
           Where-Object { $_.PSChildName -like 'WpnUserService_*' } |
           ForEach-Object { $_.PSPath })
    $k | Where-Object { Test-Path $_ }
}

function Set-Val { param($Key,$Name,$Value)
    if (-not (Test-Path $Key)) { New-Item $Key -Force | Out-Null }
    New-ItemProperty $Key -Name $Name -Value $Value -PropertyType DWord -Force | Out-Null
}
function Del-Val { param($Key,$Name)
    if ((Test-Path $Key) -and (Get-ItemProperty $Key -Name $Name -ErrorAction SilentlyContinue)) {
        Remove-ItemProperty $Key -Name $Name -Force
    }
}

switch ($Action) {

 'Status' {
    'services:'
    Get-WpnKeys | ForEach-Object {
        '  {0,-24} Start={1}' -f (Split-Path $_ -Leaf), (Get-ItemProperty $_).Start
    }
    Get-Service WpnService,WpnUserService* -ErrorAction SilentlyContinue |
        ForEach-Object { '  {0,-24} {1} / {2}' -f $_.Name,$_.Status,$_.StartType }
    'policies:'
    foreach ($k in $HklmPol,$HkcuPol) {
        $p = if (Test-Path $k) { (Get-ItemProperty $k).PSObject.Properties |
              Where-Object { $_.Name -notlike 'PS*' } |
              ForEach-Object { "$($_.Name)=$($_.Value)" } } else { $null }
        '  {0} -> {1}' -f $k, $(if ($p) { $p -join ', ' } else { '(clean)' })
    }
    '  ToastEnabled={0}' -f (Get-ItemProperty $HkcuPn -Name ToastEnabled -EA SilentlyContinue).ToastEnabled
    '  DisableNotificationCenter={0}' -f (Get-ItemProperty $HkcuExp -Name DisableNotificationCenter -EA SilentlyContinue).DisableNotificationCenter
 }

 'Apply' {
    Get-WpnKeys | ForEach-Object {
        Set-ItemProperty $_ -Name Start -Value 2 -Type DWord
        '  {0} Start -> 2 (Automatic)' -f (Split-Path $_ -Leaf)
    }
    foreach ($k in $HklmPol,$HkcuPol) {
        foreach ($v in 'NoCloudApplicationNotification','NoTileApplicationNotification',
                       'NoToastApplicationNotification','DisallowNotificationMirroring') {
            Del-Val $k $v
        }
    }
    '  policies cleared'
    Set-Val $HkcuPn 'ToastEnabled' 1;                 '  ToastEnabled -> 1'
    Del-Val $HkcuExp 'DisableNotificationCenter';     '  DisableNotificationCenter removed'
    try { Start-Service WpnService -EA Stop; '  WpnService started' }
    catch { '  WpnService not started yet - reboot or: net start WpnService' }
    'APPLIED'
 }

 'Revert' {
    Get-WpnKeys | ForEach-Object {
        try { Stop-Service (Split-Path $_ -Leaf) -Force -EA SilentlyContinue } catch {}
        Set-ItemProperty $_ -Name Start -Value 4 -Type DWord
        '  {0} Start -> 4 (Disabled)' -f (Split-Path $_ -Leaf)
    }
    Set-Val $HklmPol 'NoTileApplicationNotification'  1
    Set-Val $HklmPol 'NoCloudApplicationNotification' 1
    Set-Val $HkcuPol 'NoTileApplicationNotification'  1
    Set-Val $HkcuPol 'NoCloudApplicationNotification' 1
    Set-Val $HkcuPol 'DisallowNotificationMirroring'  1
    Set-Val $HkcuPn  'ToastEnabled' 0
    Set-Val $HkcuExp 'DisableNotificationCenter' 1
    'REVERTED to debloated state (WhatsApp will crash again - that is the point of the test). Reboot.'
 }
}
