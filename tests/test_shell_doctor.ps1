<#
    Harness for SHELL DOCTOR.

    Part 1 proves every verdict in Scripts\shell_doctor\shell_doctor_logic.ps1
    from fixtures (no machine state). The first fixture is the incident the
    tool was written for: a USB NVMe enclosure reset by UASPStor every 285 s,
    1356 times in five days, freezing Explorer and Start.

    Part 2 runs the real collector read-only (-Json -NoPause) and checks the
    output contract. It never passes -ResetViews.
#>
$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $testsDir
$logic = Join-Path $root 'Scripts\shell_doctor\shell_doctor_logic.ps1'
$tool = Join-Path $root 'Scripts\shell_doctor\SHELL_DOCTOR.ps1'
foreach ($f in @($logic, $tool)) {
    if (-not (Test-Path -LiteralPath $f)) { Write-Host "FAIL  missing $f"; exit 1 }
}
. $logic

$script:fail = 0
$script:pass = 0
function Check([string]$Name, [bool]$Ok, $Info = '') {
    if ($Ok) { $script:pass++; Write-Host "PASS  $Name" }
    else { $script:fail++; Write-Host "FAIL  $Name  $Info" }
}
function Codes($f) { @($f | ForEach-Object { $_.Code }) }

$t0 = [datetime]'2026-09-17T15:40:32'
$disks = @(
    [pscustomobject]@{ Index = 4; SCSIPort = 1; Model = 'KINGSTON SFYRD2000G'; Bus = 'NVMe'; Letters = @('C', 'V') }
    [pscustomobject]@{ Index = 7; SCSIPort = 0; Model = 'Microsoft Storage Space Device'; Bus = 'Spaces'; Letters = @('N') }
    [pscustomobject]@{ Index = 8; SCSIPort = 4; Model = 'Samsung SSD 970 EVO SCSI Disk Device'; Bus = 'USB'; Letters = @('U') }
    [pscustomobject]@{ Index = 0; SCSIPort = 0; Model = 'WDC WD40PURX'; Bus = 'SATA'; Letters = @('R') }
)

# ---------------------------------------------------------------- cadence --
$periodic = @(0..1355 | ForEach-Object { $t0.AddSeconds(285 * $_ + $(if ($_ % 17 -eq 0) { -10 } else { 0 })) })
$c = Get-SdResetCadence -Times $periodic -WindowDays 7
Check 'cadence: incident fixture counted' ($c.Count -eq 1356) $c.Count
Check 'cadence: median 285 s' ($c.MedianSeconds -eq 285) $c.MedianSeconds
Check 'cadence: incident is PERIODIC' ($c.Periodic)
Check 'cadence: per-day rate ~ 303 over 4.5 days' ($c.PerDay -gt 250 -and $c.PerDay -lt 320) $c.PerDay

$rnd = New-Object System.Random 7
$acc = 0; $scatter = @(1..40 | ForEach-Object { $acc += $rnd.Next(30, 20000); $t0.AddSeconds($acc) })
Check 'cadence: random scatter is NOT periodic' (-not (Get-SdResetCadence -Times $scatter).Periodic)
$e = Get-SdResetCadence -Times @()
Check 'cadence: empty input is zero, no throw' ($e.Count -eq 0 -and -not $e.Periodic)
$few = Get-SdResetCadence -Times @($t0, $t0.AddSeconds(60))
Check 'cadence: < 4 events never claims a period' ($few.Count -eq 2 -and -not $few.Periodic -and $null -eq $few.MedianSeconds)

# ----------------------------------------------------------- disk mapping --
$m = @(Resolve-SdPortDisk -Device '\Device\RaidPort4' -Disks $disks)
Check 'map: RaidPort4 -> Samsung (SCSIPort 4)' ($m.Count -eq 1 -and $m[0].Model -like 'Samsung*')
$m = @(Resolve-SdPortDisk -Device '\Device\RaidPort0' -Disks $disks)
Check 'map: Storage Spaces virtual disk never blamed' ($m.Count -eq 1 -and $m[0].Model -like 'WDC*') ($m | Out-String)
$m = @(Resolve-SdPortDisk -Device 'Disk 4' -Disks $disks)
Check 'map: "Disk N" uses Index, not SCSIPort' ($m.Count -eq 1 -and $m[0].Model -like 'KINGSTON*')
$m = @(Resolve-SdPortDisk -Device '\Device\Harddisk8\DR8' -Disks $disks)
Check 'map: \Device\HarddiskN uses Index' ($m.Count -eq 1 -and $m[0].Model -like 'Samsung*')
Check 'map: unknown device string -> empty' (@(Resolve-SdPortDisk -Device 'whatever' -Disks $disks).Count -eq 0)

# ----------------------------------------------------------- product root --
Check 'root: Program Files vendor' ((Get-SdProductRoot 'C:\Program Files\Google\Drive File Stream\131\x.dll') -eq 'C:\Program Files\Google')
Check 'root: AppData Roaming vendor' ((Get-SdProductRoot 'C:\Users\u\AppData\Roaming\Yandex\YandexDisk2\3.2\y.dll') -eq 'C:\Users\u\AppData\Roaming\Yandex')
Check 'root: x86 vendor' ((Get-SdProductRoot 'C:\Program Files (x86)\SageThumbs\64\SageThumbs.dll') -eq 'C:\Program Files (x86)\SageThumbs')
Check 'windows path: system32 excluded' (Test-SdWindowsPath '%SystemRoot%\system32\shell32.dll')
Check 'windows path: C:\Windows excluded' (Test-SdWindowsPath 'C:\Windows\System32\DriverStore\x\nvshext.dll')
Check 'windows path: vendor DLL kept' (-not (Test-SdWindowsPath 'C:\Program Files\WinRAR\rarext.dll'))

# ------------------------------------------------------- handler overlap --
$pairs = @()
$pairs += @(1..211 | ForEach-Object { @{ Ext = ".s$_"; Dll = 'C:\Program Files (x86)\SageThumbs\64\SageThumbs.dll' } })
$pairs += @(1..58 | ForEach-Object { @{ Ext = ".s$_"; Dll = 'C:\Program Files (x86)\Icaros\IcarosThumbnailProvider.dll' } })
$pairs += @{ Ext = '.txt'; Dll = '%SystemRoot%\system32\windows.storage.dll' }
$pairs += @{ Ext = '.S1'; Dll = 'C:\Program Files (x86)\Icaros\IcarosThumbnailProvider.dll' }   # case dup
$rows = @(Get-SdHandlerOverlap -Pairs $pairs)
Check 'handlers: Windows DLLs ignored' (-not ($rows | Where-Object { $_.Dll -like '*system32*' }))
Check 'handlers: Sage owns 211, sorted first' ($rows[0].Count -eq 211)
Check 'handlers: Icaros 58 distinct (case-folded), all shared' ($rows[1].Count -eq 58 -and $rows[1].Shared -eq 58) ($rows[1] | Out-String)

# ------------------------------------------------------------ findings: A --
# The incident, end to end.
$S = @{
    WindowDays = 7
    Disks = $disks
    StorageEvents = @($periodic | ForEach-Object { @{ Id = 129; Provider = 'UASPStor'; Device = '\Device\RaidPort4'; Time = $_ } }) +
                    @(@{ Id = 129; Provider = 'UASPStor'; Device = '\Device\RaidPort7'; Time = $t0 })
    ShellCrashes = @(@{ Id = 1002; Time = $t0.AddDays(5) })
    ExplorerModules = @(
        @{ Path = 'C:\Users\u\AppData\Local\MEGAsync\ShellExtX64.dll'; Company = ''; Running = $false }
        @{ Path = 'C:\Users\u\AppData\Roaming\Yandex\YandexDisk2\3.2\YandexDisk3ShellExt.dll'; Company = 'Yandex'; Running = $false }
        @{ Path = 'C:\Program Files\Google\Drive File Stream\131\drivefsext.dll'; Company = 'Google LLC.'; Running = $false }
        @{ Path = 'C:\Program Files\Listary\Hooks\ListaryHook64.dll'; Company = 'Bopsoft'; Running = $true }
    )
    Overlays = @(1..18 | ForEach-Object { @{ Name = "ov$_"; Dll = 'C:\x\ov.dll'; DllExists = $true } })
    ContextHandlers = @(@{ Key = '*'; Name = 'Old'; Dll = 'C:\gone\old.dll'; DllExists = $false })
    Handlers = $pairs
    BagsCount = 1744
    ThumbCacheMB = 175
}
$F = @(Get-SdFindings -S $S)
$codes = Codes $F
Check 'A: first finding is the HIGH storage stall' ($F[0].Severity -eq 'HIGH' -and $F[0].Code -eq 'STORAGE_STALL') ($F[0] | Out-String)
Check 'A: stall names the disk and its letter' (($F[0].Detail -join ' ') -match 'Samsung SSD 970 EVO.*\[USB\] U:')
Check 'A: stall says PERIODIC' (($F[0].Detail -join ' ') -match 'PERIODIC')
Check 'A: USB advice (rear port / firmware / remove to prove)' ($F[0].Fix -match 'rear motherboard USB' -and $F[0].Fix -match 'safely remove')
Check 'A: single stray reset on another port is LOW, separate' (@($F | Where-Object { $_.Code -eq 'STORAGE_STALL' -and $_.Severity -eq 'LOW' }).Count -eq 1)
Check 'A: 3 idle products -> IDLE_SHELL_EXT MEDIUM' (@($F | Where-Object { $_.Code -eq 'IDLE_SHELL_EXT' -and $_.Severity -eq 'MEDIUM' }).Count -eq 1)
Check 'A: running hook is INFO, not blamed' (@($F | Where-Object { $_.Code -eq 'SHELL_EXT' -and $_.Severity -eq 'INFO' }).Count -eq 1)
Check 'A: 18 overlays -> OVERLAY_LIMIT lists 3 ignored' (@($F | Where-Object { $_.Code -eq 'OVERLAY_LIMIT' })[0].Detail.Count -eq 3)
Check 'A: missing DLL -> DANGLING_SHELL_EXT' ($codes -contains 'DANGLING_SHELL_EXT')
Check 'A: 211-type thumbnailer + overlap -> HANDLER_HOG' ($codes -contains 'HANDLER_HOG')
Check 'A: 1744 bags -> VIEW_BLOAT' ($codes -contains 'VIEW_BLOAT')
Check 'A: shell crash reported' ($codes -contains 'SHELL_CRASH')
$rank = @{ HIGH = 0; MEDIUM = 1; LOW = 2; INFO = 3 }
$ordered = $true
for ($i = 1; $i -lt $F.Count; $i++) { if ($rank[$F[$i].Severity] -lt $rank[$F[$i - 1].Severity]) { $ordered = $false } }
Check 'A: findings ordered HIGH > MEDIUM > LOW > INFO' $ordered

# ------------------------------------------------------------ findings: B --
# A healthy machine produces no real finding.
$B = @(Get-SdFindings -S @{ WindowDays = 7; Disks = $disks; StorageEvents = @(); ShellCrashes = @();
    ExplorerModules = @(); Overlays = @(1..12 | ForEach-Object { @{ Name = "ov$_"; Dll = 'C:\x.dll'; DllExists = $true } });
    ContextHandlers = @(); Handlers = @(@{ Ext = '.blend'; Dll = 'C:\Program Files\Blender\BlendThumb.dll' }); BagsCount = 300; ThumbCacheMB = 80 })
Check 'B: healthy snapshot -> zero findings' ($B.Count -eq 0) (Codes $B)
$Empty = @(Get-SdFindings -S @{})
Check 'B: empty snapshot -> zero findings, no throw' ($Empty.Count -eq 0)

# ------------------------------------------------------------ findings: C --
# Irregular non-USB retries: MEDIUM, cable/SMART advice, not the USB text.
$irr = @($scatter | Select-Object -First 5 | ForEach-Object { @{ Id = 153; Provider = 'disk'; Device = 'Disk 0'; Time = $_ } })
$C = @(Get-SdFindings -S @{ Disks = $disks; StorageEvents = $irr })
Check 'C: 5 irregular SATA retries -> MEDIUM' ($C.Count -eq 1 -and $C[0].Severity -eq 'MEDIUM') ($C | Out-String)
Check 'C: SATA advice mentions SMART, not USB port' ($C[0].Fix -match 'SMART' -and $C[0].Fix -notmatch 'USB')
Check 'C: irregular wording' (($C[0].Detail -join ' ') -match 'irregular')

# ------------------------------------------------------------ findings: D --
# A slow but clock-regular USB reset (every 4 h, 6/day) on a port that maps to
# no disk: periodic Event 129 alone makes it HIGH, and the UASPStor provider
# alone must still select the USB advice.
$slow = @(0..19 | ForEach-Object { @{ Id = 129; Provider = 'UASPStor'; Device = '\Device\RaidPort9'; Time = $t0.AddHours(4 * $_) } })
$D = @(Get-SdFindings -S @{ WindowDays = 7; Disks = $disks; StorageEvents = $slow })
Check 'D: periodic 129 under 10/day is still HIGH' ($D.Count -eq 1 -and $D[0].Severity -eq 'HIGH') ($D | Out-String)
Check 'D: unmapped UASPStor port still gets USB advice' ($D[0].Fix -match 'rear motherboard USB') $D[0].Fix
Check 'D: unmapped device is said, not guessed' (($D[0].Detail -join ' ') -match 'device not mapped to a disk')
$Dr = @(Get-SdFindings -S @{ WindowDays = 7; Disks = $disks; StorageEvents = @($slow | ForEach-Object { @{ Id = 153; Provider = 'disk'; Device = 'Disk 0'; Time = $_.Time } }) })
Check 'D: same cadence as 153 retries is MEDIUM, not HIGH' ($Dr.Count -eq 1 -and $Dr[0].Severity -eq 'MEDIUM') ($Dr | Out-String)

# ---------------------------------------------- part 2: real collector smoke --
$json = & powershell -NoProfile -ExecutionPolicy Bypass -File $tool -Json -NoPause -Days 2 2>&1 | Out-String
$code = $LASTEXITCODE
Check 'collector: -Json exits 0' ($code -eq 0) $code
$obj = $null
try { $obj = $json | ConvertFrom-Json } catch { }
Check 'collector: output is JSON' ($null -ne $obj) ($json.Substring(0, [math]::Min(400, $json.Length)))
if ($obj) {
    Check 'collector: window_days honoured' ($obj.window_days -eq 2)
    Check 'collector: summary has every section' ($null -ne $obj.summary.PSObject.Properties['storage_events'] -and
        $null -ne $obj.summary.PSObject.Properties['handler_registrations'] -and $null -ne $obj.summary.PSObject.Properties['bags'])
    $bad = @($obj.findings | Where-Object { $_.Severity -notin 'HIGH', 'MEDIUM', 'LOW', 'INFO' -or -not $_.Code })
    Check 'collector: every finding has severity + code' ($bad.Count -eq 0)
}
$src = Get-Content -LiteralPath $tool -Raw
Check 'safety: collector never writes HKLM' ($src -notmatch 'HKLM:\\|LocalMachine\.(CreateSubKey|DeleteSubKey|SetValue)|OpenSubKey\([^)]*,\s*\$true')
Check 'safety: no service / power / device mutation' ($src -notmatch '(?i)Set-Service|Stop-Service|powercfg\s+/(set|change)|Disable-PnpDevice|bcdedit')
# The export must be live code whose failure aborts, and it must come first.
$exp = [regex]::Match($src, '(?m)^[ \t]*&[ \t]*reg\.exe[ \t]+export\b.*\r?\n[ \t]*if \(\$LASTEXITCODE -ne 0\) \{[^}\r\n]*return \$false')
$del = $src.IndexOf("Remove-Item -LiteralPath `$p -Recurse")
Check 'safety: reset is backed up before deletion' ($exp.Success -and $del -gt 0 -and $exp.Index -lt $del)

Write-Host ''
Write-Host "shell_doctor: $script:pass passed, $script:fail failed"
if ($script:fail) { exit 1 }
Write-Host 'SHELL_DOCTOR_GREEN=TRUE'
exit 0
