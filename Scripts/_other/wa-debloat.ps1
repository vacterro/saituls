<#
  wa-debloat.ps1 - trim WhatsApp Desktop (MSIX) background behaviour, reversibly.

  Every knob here is keyed by package FAMILY name or by EXE name, never by
  version and never by touching files under C:\Program Files\WindowsApps.
  That is what makes it survive MSIX updates and re-registration.

  ITEMS
    Startup   HKCU ...\AppModel\SystemAppData\<family>\<TaskId>\State
              2 = Enabled -> 1 = DisabledByUser. Kills "start with Windows".
              Cost: no messages while the app is closed.

    WebView2  HKCU\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments
              value name = WhatsApp.Root.exe -> "--renderer-process-limit=1"
              Caps RENDERER processes only; GPU/network/utility children stay.
              Expect a modest RAM win, not a miracle.

    Hosts     A marked block in %SystemRoot%\System32\drivers\etc\hosts with
              WhatsApp telemetry/crash endpoints only. Core hosts
              (g / v / mmg / pps / static / media-*.whatsapp.net) are NOT touched -
              blocking those breaks login, media and avatars.

  USAGE
    .\wa-debloat.ps1 -Action Status
    .\wa-debloat.ps1 -Action Apply
    .\wa-debloat.ps1 -Action Revert
    .\wa-debloat.ps1 -Action Apply -Items Startup,WebView2

  Hosts needs elevation; Startup and WebView2 do not.
#>
[CmdletBinding()]
param(
    [ValidateSet('Apply','Revert','Status')][string]$Action = 'Status',
    [ValidateSet('Startup','WebView2','Hosts')][string[]]$Items = @('Startup','WebView2','Hosts')
)

$ErrorActionPreference = 'Stop'

$Fam     = '5319275A.WhatsAppDesktop_cv1g1gvanyjgm'
$TaskId  = '2defd21c-0b9e-4e4e-873a-2a68c47d7da5'   # uap5:StartupTask, DisplayName "WhatsApp"
$ExeName = 'WhatsApp.Root.exe'
$WvArgs  = '--renderer-process-limit=1'

$StartupKey = "HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData\$Fam\$TaskId"
$WvKey      = 'HKCU:\Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments'
$HostsFile  = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$Begin      = '# === WA-DEBLOAT BEGIN (wa-debloat.ps1) ==='
$End        = '# === WA-DEBLOAT END ==='

# telemetry / crash reporting only - nothing load-bearing
$BlockHosts = @(
    'crashlogs.whatsapp.net'
    'dit.whatsapp.net'
    'privatestats.whatsapp.net'
)

function Is-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------------- Startup ----------------
function Startup-Status {
    if (-not (Test-Path $StartupKey)) { return 'key absent (app not registered for this user?)' }
    $s = (Get-ItemProperty $StartupKey -Name State -EA SilentlyContinue).State
    $n = switch ($s) { 0 {'Disabled'} 1 {'DisabledByUser'} 2 {'Enabled'} 3 {'DisabledByPolicy'} 4 {'EnabledByPolicy'} default {'?'} }
    "State=$s ($n)"
}
function Startup-Apply {
    if (-not (Test-Path $StartupKey)) { '  Startup: key absent, skipped'; return }
    Set-ItemProperty $StartupKey -Name State -Value 1 -Type DWord
    '  Startup: State -> 1 (DisabledByUser)'
}
function Startup-Revert {
    if (-not (Test-Path $StartupKey)) { '  Startup: key absent, skipped'; return }
    Set-ItemProperty $StartupKey -Name State -Value 2 -Type DWord
    '  Startup: State -> 2 (Enabled)'
}

# ---------------- WebView2 ----------------
function WebView2-Status {
    if (-not (Test-Path $WvKey)) { return 'policy key absent' }
    $v = (Get-ItemProperty $WvKey -Name $ExeName -EA SilentlyContinue).$ExeName
    if ($v) { "$ExeName = $v" } else { 'no entry for this exe' }
}
function WebView2-Apply {
    if (-not (Test-Path $WvKey)) { New-Item $WvKey -Force | Out-Null }
    New-ItemProperty $WvKey -Name $ExeName -Value $WvArgs -PropertyType String -Force | Out-Null
    "  WebView2: $ExeName = $WvArgs  (restart WhatsApp to take effect)"
}
function WebView2-Revert {
    if ((Test-Path $WvKey) -and (Get-ItemProperty $WvKey -Name $ExeName -EA SilentlyContinue)) {
        Remove-ItemProperty $WvKey -Name $ExeName -Force
        '  WebView2: entry removed'
    } else { '  WebView2: nothing to remove' }
}

# ---------------- Hosts ----------------
function Hosts-Status {
    $t = Get-Content $HostsFile -EA SilentlyContinue
    if ($t -contains $Begin) { "managed block present ($($BlockHosts.Count) hosts)" }
    else {
        $already = $BlockHosts | Where-Object { $t -match [regex]::Escape($_) }
        "managed block absent (already covered elsewhere in hosts: $($already.Count)/$($BlockHosts.Count))"
    }
}
function Hosts-Strip { param($Lines)
    $out = @(); $skip = $false
    foreach ($l in $Lines) {
        if ($l -eq $Begin) { $skip = $true; continue }
        if ($l -eq $End)   { $skip = $false; continue }
        if (-not $skip) { $out += $l }
    }
    ,$out
}
function Hosts-Apply {
    if (-not (Is-Admin)) { '  Hosts: SKIPPED - needs elevation'; return }
    $lines = @(Get-Content $HostsFile)
    if (-not (Test-Path "$HostsFile.wa-debloat.bak")) { Copy-Item $HostsFile "$HostsFile.wa-debloat.bak" }
    $lines = Hosts-Strip $lines
    $block = @($Begin) + ($BlockHosts | ForEach-Object { "0.0.0.0 $_" }) + @($End)
    Set-Content $HostsFile -Value ($lines + $block) -Encoding ASCII
    "  Hosts: block written ($($BlockHosts.Count) entries), backup -> $HostsFile.wa-debloat.bak"
}
function Hosts-Revert {
    if (-not (Is-Admin)) { '  Hosts: SKIPPED - needs elevation'; return }
    $lines = @(Get-Content $HostsFile)
    Set-Content $HostsFile -Value (Hosts-Strip $lines) -Encoding ASCII
    '  Hosts: managed block removed'
}

# ---------------- run ----------------
switch ($Action) {
 'Status' {
    "package family : $Fam"
    "Startup        : $(Startup-Status)"
    "WebView2       : $(WebView2-Status)"
    "Hosts          : $(Hosts-Status)"
    $p = Get-Process | Where-Object { $_.ProcessName -match 'WhatsApp|msedgewebview2' }
    if ($p) {
        "processes      : $($p.Count), RAM $([int](($p | Measure-Object WorkingSet64 -Sum).Sum/1MB)) MB"
        $p | Group-Object ProcessName | ForEach-Object { "                 {0} x{1}" -f $_.Name,$_.Count }
    } else { 'processes      : none running' }
 }
 'Apply'  { "APPLY  [$($Items -join ',')]"; foreach ($i in $Items) { & "$i-Apply" };  'done' }
 'Revert' { "REVERT [$($Items -join ',')]"; foreach ($i in $Items) { & "$i-Revert" }; 'done' }
}
