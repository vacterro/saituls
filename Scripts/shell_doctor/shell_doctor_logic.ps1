<#
    SHELL DOCTOR - pure decision logic.

    No registry, no event log, no process, no filesystem: every function takes
    plain data and returns plain data, so tests\test_shell_doctor.ps1 can prove
    each verdict from fixtures. SHELL_DOCTOR.ps1 owns collection and output.

    The defect class this exists for: Explorer/Start freezes blamed on "Windows
    being slow" when the real cause is a storage device being reset by its
    driver on a timer, or shell extensions from apps that are not even running.
    Both are invisible in Task Manager and obvious in the data below.
#>

Set-StrictMode -Version 2.0

# Event IDs that stall any I/O touching the device while they happen.
$script:SdStorageStallIds = @{
    129 = 'reset issued by the storage driver (I/O timed out)'
    153 = 'I/O retried by the disk driver'
    7   = 'bad block reported'
    51  = 'paging-operation error'
    157 = 'disk surprise-removed'
}

function Get-SdResetCadence {
    <#
        Timestamps -> cadence verdict. A reset storm on a fixed interval means
        something (the device firmware, its bridge, or a poller) trips on a
        timer; a random scatter means a flaky cable/port/power.
        Returns Count, PerDay, MedianSeconds, Periodic.
    #>
    param([datetime[]]$Times, [double]$WindowDays = 7)

    $r = [ordered]@{ Count = 0; PerDay = 0.0; MedianSeconds = $null; Periodic = $false }
    if (-not $Times -or $Times.Count -eq 0) { return [pscustomobject]$r }
    $sorted = @($Times | Sort-Object)
    $r.Count = $sorted.Count
    $span = ($sorted[-1] - $sorted[0]).TotalDays
    $days = [math]::Max([math]::Min($WindowDays, [math]::Max($span, 1.0)), 1.0)
    $r.PerDay = [math]::Round($sorted.Count / $days, 1)
    if ($sorted.Count -lt 4) { return [pscustomobject]$r }

    $gaps = New-Object System.Collections.Generic.List[double]
    for ($i = 1; $i -lt $sorted.Count; $i++) {
        $gaps.Add(($sorted[$i] - $sorted[$i - 1]).TotalSeconds)
    }
    $g = @($gaps | Sort-Object)
    $median = $g[[int][math]::Floor($g.Count / 2)]
    $r.MedianSeconds = [math]::Round($median, 0)
    if ($median -gt 0) {
        $tol = [math]::Max($median * 0.1, 5)
        $near = @($g | Where-Object { [math]::Abs($_ - $median) -le $tol }).Count
        $r.Periodic = ($near / $g.Count) -ge 0.6
    }
    return [pscustomobject]$r
}

function Resolve-SdPortDisk {
    <#
        Map an event's device string to the physical disk it names.
        storport logs "\Device\RaidPort<N>"; Win32_DiskDrive.SCSIPort carries
        the same N. The disk driver logs "Disk <N>" / "\Device\Harddisk<N>",
        where N is Win32_DiskDrive.Index.
        Disks: objects with Index, SCSIPort, Model, Letters, Bus.
        Returns the matching disk objects (may be several on one port).
    #>
    param([string]$Device, [object[]]$Disks)

    if (-not $Device -or -not $Disks) { return @() }
    $m = [regex]::Match($Device, '(?i)(?:RaidPort|ScsiPort)(\d+)')
    if ($m.Success) {
        $port = [int]$m.Groups[1].Value
        return @($Disks | Where-Object { $_.SCSIPort -eq $port -and $_.Model -notmatch 'Storage Space' })
    }
    $m = [regex]::Match($Device, '(?i)(?:\bDisk\s+|\\Harddisk)(\d+)')
    if ($m.Success) {
        $idx = [int]$m.Groups[1].Value
        return @($Disks | Where-Object { $_.Index -eq $idx })
    }
    return @()
}

function Get-SdHandlerOverlap {
    <#
        Property/thumbnail handler registrations (@{ Ext; Dll } pairs; one
        extension may appear under several DLLs) -> per third-party DLL the
        number of distinct extensions it owns, and how many of those another
        third-party DLL also owns. Two thumbnailers claiming the same
        extensions is double the work for no gain.
    #>
    param([object[]]$Pairs)

    $byDll = @{}
    $byExt = @{}
    foreach ($p in @($Pairs)) {
        if (-not $p) { continue }
        $dll = [string]$p.Dll; $ext = ([string]$p.Ext).ToLowerInvariant()
        if (-not $dll -or -not $ext -or (Test-SdWindowsPath $dll)) { continue }
        if (-not $byDll.ContainsKey($dll)) { $byDll[$dll] = @{} }
        $byDll[$dll][$ext] = $true
        if (-not $byExt.ContainsKey($ext)) { $byExt[$ext] = @{} }
        $byExt[$ext][$dll] = $true
    }
    $rows = foreach ($k in $byDll.Keys) {
        $shared = @($byDll[$k].Keys | Where-Object { $byExt[$_].Count -gt 1 }).Count
        [pscustomobject]@{ Dll = $k; Count = $byDll[$k].Count; Shared = $shared }
    }
    return @($rows | Sort-Object Count -Descending)
}

function Test-SdWindowsPath {
    param([string]$Path)
    if (-not $Path) { return $false }
    return ($Path -match '(?i)(^|\\|%)(system32|syswow64|SystemRoot%)(\\|$)' -or
            $Path -match '(?i)^[a-z]:\\windows\\' -or
            $Path -match '(?i)^%windir%')
}

function Get-SdProductRoot {
    <#
        The folder that identifies the product a DLL belongs to:
        "C:\Program Files\Vendor", "...\AppData\Local\Vendor" etc.
        Used to decide whether any process of that product is running.
    #>
    param([string]$Path)
    if (-not $Path) { return $null }
    $p = $Path.TrimEnd('\')
    $m = [regex]::Match($p, '(?i)^(.*?\\(?:Program Files(?: \(x86\))?|ProgramData|AppData\\(?:Local|Roaming|LocalLow))\\[^\\]+)')
    if ($m.Success) { return $m.Groups[1].Value }
    $parts = $p.Split('\')
    if ($parts.Count -ge 3) { return ($parts[0..1] -join '\') }
    return $null
}

function New-SdFinding {
    param([string]$Severity, [string]$Code, [string]$Title, [string[]]$Detail, [string]$Fix)
    [pscustomobject]@{ Severity = $Severity; Code = $Code; Title = $Title; Detail = @($Detail); Fix = $Fix }
}

function Get-SdFindings {
    <#
        Snapshot -> ordered findings. The snapshot keys are produced by
        SHELL_DOCTOR.ps1 (Get-SdSnapshot) and by the test fixtures:

          WindowDays      [double]
          StorageEvents   @( @{ Id; Provider; Device; Time } )
          Disks           @( @{ SCSIPort; Model; Letters; Bus } )
          ShellCrashes    @( @{ Id; Time } )
          ExplorerModules @( @{ Path; Company; Running } )   third-party only
          Overlays        @( @{ Name; Dll; DllExists } )
          ContextHandlers @( @{ Key; Name; Dll; DllExists } )
          Handlers        @( @{ Ext; Dll } )   property + thumbnail handlers
          BagsCount       [int]
          ThumbCacheMB    [double]
    #>
    param([hashtable]$S)

    $out = New-Object System.Collections.Generic.List[object]
    $win = if ($S.ContainsKey('WindowDays') -and $S.WindowDays) { [double]$S.WindowDays } else { 7.0 }

    # ---- storage stalls, grouped per device and event id -------------------
    $ev = @(if ($S.ContainsKey('StorageEvents')) { $S.StorageEvents })
    foreach ($grp in ($ev | Group-Object { "{0}|{1}|{2}" -f $_.Id, $_.Provider, $_.Device })) {
        $first = $grp.Group[0]
        $cad = Get-SdResetCadence -Times @($grp.Group | ForEach-Object { [datetime]$_.Time }) -WindowDays $win
        $disks = @(Resolve-SdPortDisk -Device $first.Device -Disks @($S.Disks))
        $who = if ($disks.Count) {
            ($disks | ForEach-Object { "{0} [{1}] {2}" -f $_.Model, $_.Bus, (($_.Letters | ForEach-Object { "$_`:" }) -join ' ') }) -join '; '
        } else { 'device not mapped to a disk' }
        $what = $script:SdStorageStallIds[[int]$first.Id]
        $sev = if ($cad.PerDay -ge 10 -or ([int]$first.Id -eq 129 -and $cad.Periodic)) { 'HIGH' }
               elseif ($cad.Count -ge 3) { 'MEDIUM' } else { 'LOW' }
        $detail = @(
            "Event $($first.Id) $($first.Provider) $($first.Device): $what",
            "$($cad.Count) events, $($cad.PerDay)/day, disk: $who"
        )
        if ($cad.MedianSeconds) {
            $detail += ("median interval {0}s{1}" -f $cad.MedianSeconds, $(if ($cad.Periodic) { ' - PERIODIC (timer-driven: bridge/firmware power state or a poller)' } else { ' - irregular (cable/port/power)' }))
        }
        $usb = @($disks | Where-Object { $_.Bus -match 'USB' }).Count -gt 0 -or $first.Provider -match 'UASP|USBSTOR'
        $fix = if ($usb) {
            'Every reset freezes Explorer, Start and anything enumerating drives. Plug the enclosure straight into a rear motherboard USB port (no hub/extension), update the bridge firmware, or replace the enclosure. To prove it: safely remove this drive for an hour and re-run SHELL DOCTOR.'
        } else {
            'Check the cable/port and the drive health (SMART); a storage reset stalls every process touching the disk, Explorer first.'
        }
        $out.Add((New-SdFinding $sev 'STORAGE_STALL' "Storage device keeps stalling ($($cad.Count)x)" $detail $fix))
    }

    # ---- shell crashes ------------------------------------------------------
    $cr = @(if ($S.ContainsKey('ShellCrashes')) { $S.ShellCrashes })
    if ($cr.Count) {
        $last = ($cr | Sort-Object { [datetime]$_.Time } | Select-Object -Last 1).Time
        $sev = if ($cr.Count -ge 3) { 'HIGH' } else { 'MEDIUM' }
        $out.Add((New-SdFinding $sev 'SHELL_CRASH' "explorer.exe crashed/hung $($cr.Count)x" @("last: $last") 'Usually a symptom: fix STORAGE_STALL / shell-extension findings first.'))
    }

    # ---- shell extensions loaded while their app is not running ------------
    $mods = @(if ($S.ContainsKey('ExplorerModules')) { $S.ExplorerModules })
    $idle = @($mods | Where-Object { -not $_.Running })
    if ($idle.Count) {
        $roots = @($idle | ForEach-Object { Get-SdProductRoot $_.Path } | Where-Object { $_ } | Sort-Object -Unique)
        $sev = if ($roots.Count -ge 3) { 'MEDIUM' } else { 'LOW' }
        $out.Add((New-SdFinding $sev 'IDLE_SHELL_EXT' "$($roots.Count) product(s) inject DLLs into Explorer with no process of their own running" `
            @($idle | ForEach-Object { "{0}  ({1})" -f $_.Path, $(if ($_.Company) { $_.Company } else { 'unknown vendor' }) }) `
            'Each one runs inside Explorer on every folder/right-click; a sync-client extension whose app is closed can block waiting for it. Uninstall the ones you do not use (Settings > Apps). Start-menu/taskbar mods live only inside Explorer by design - they belong here only if you no longer want them.'))
    }
    $live = @($mods | Where-Object { $_.Running })
    if ($live.Count) {
        $out.Add((New-SdFinding 'INFO' 'SHELL_EXT' "$($live.Count) third-party DLL(s) loaded in Explorer (app running)" `
            @($live | ForEach-Object { $_.Path }) 'Suspects if lag remains after the findings above are fixed.'))
    }

    # ---- overlay slots ------------------------------------------------------
    $ov = @(if ($S.ContainsKey('Overlays')) { $S.Overlays })
    if ($ov.Count -gt 15) {
        $out.Add((New-SdFinding 'LOW' 'OVERLAY_LIMIT' "$($ov.Count) icon overlay handlers registered (Windows uses 15)" `
            @($ov | Select-Object -Skip 15 | ForEach-Object { "ignored: $($_.Name)" }) `
            'The surplus handlers still load but never draw. Remove sync clients you do not use.'))
    }

    # ---- dangling registrations ---------------------------------------------
    $dang = @(@($ov) + @(if ($S.ContainsKey('ContextHandlers')) { $S.ContextHandlers }) | Where-Object { $_.Dll -and -not $_.DllExists })
    if ($dang.Count) {
        $out.Add((New-SdFinding 'MEDIUM' 'DANGLING_SHELL_EXT' "$($dang.Count) shell extension(s) point at a missing DLL" `
            @($dang | ForEach-Object { "{0} -> {1}" -f $_.Name, $_.Dll }) `
            'Explorer probes these on every use. Reinstall or cleanly uninstall the owning app.'))
    }

    # ---- property/thumbnail handlers ---------------------------------------
    if ($S.ContainsKey('Handlers') -and $S.Handlers) {
        $rows = @(Get-SdHandlerOverlap -Pairs @($S.Handlers))
        $big = @($rows | Where-Object { $_.Count -ge 50 })
        $dup = @($rows | Where-Object { $_.Shared -ge 10 })
        if ($big.Count -or $dup.Count -ge 2) {
            $sev = if ($big.Count -and $dup.Count -ge 2) { 'MEDIUM' } else { 'LOW' }
            $out.Add((New-SdFinding $sev 'HANDLER_HOG' 'Third-party property/thumbnail handlers own many file types' `
                @($rows | ForEach-Object { "{0,4} types ({1} also claimed by another)  {2}" -f $_.Count, $_.Shared, $_.Dll }) `
                'Explorer calls these for every file shown in Details/thumbnail views. Keep one thumbnailer (e.g. Icaros) and uninstall the rest.'))
        }
    }

    # ---- view-state bloat ----------------------------------------------------
    $bags = if ($S.ContainsKey('BagsCount')) { [int]$S.BagsCount } else { 0 }
    $tc = if ($S.ContainsKey('ThumbCacheMB')) { [double]$S.ThumbCacheMB } else { 0 }
    if ($bags -ge 1500 -or $tc -ge 1024) {
        $out.Add((New-SdFinding 'LOW' 'VIEW_BLOAT' 'Explorer view state / thumbnail cache is large' `
            @("folder view records (Bags): $bags", ("thumbnail+icon cache: {0:N0} MB" -f $tc)) `
            'Run SHELL DOCTOR -ResetViews (per-user, backed up) to reset folder views and caches.'))
    }

    $rank = @{ HIGH = 0; MEDIUM = 1; LOW = 2; INFO = 3 }
    return @($out | Sort-Object { $rank[$_.Severity] })
}
