<#
    SHELL DOCTOR - why is Explorer / Start lagging?

    Read-only by default. Collects the evidence that actually explains shell
    freezes (storage resets, shell crashes, shell extensions from apps that
    are not running, dangling registrations, thumbnail-handler overlap, view
    state bloat), hands it to shell_doctor_logic.ps1, prints ranked findings.

    -ResetViews is the only mutation and it is per-user (HKCU + %LOCALAPPDATA%):
    folder views (Bags/BagMRU) and thumbnail/icon caches, backed up first,
    Explorer restarted. Nothing machine-wide is ever written: no services, no
    HKLM, no power plan, no device settings.

    Usage:
      SHELL_DOCTOR.ps1                 report, then interactive menu
      SHELL_DOCTOR.ps1 -NoPause        report only
      SHELL_DOCTOR.ps1 -Json           findings + snapshot summary as JSON
      SHELL_DOCTOR.ps1 -Days 14        look back 14 days (default 7)
      SHELL_DOCTOR.ps1 -ResetViews     reset views/caches (asks; -Yes skips)
#>
param(
    [int]$Days = 7,
    [switch]$Json,
    [switch]$NoPause,
    [switch]$ResetViews,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
. (Join-Path $here 'shell_doctor_logic.ps1')

$BagsPath   = 'Software\Classes\Local Settings\Software\Microsoft\Windows\Shell'
$CacheDir   = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Explorer'
$ThumbGuid  = '{e357fccd-a995-4576-b01f-234630154e96}'   # IThumbnailProvider
$CtxRoots   = @('*', 'Directory', 'Directory\Background', 'Folder', 'Drive', 'AllFilesystemObjects')

function Get-ClsidDll {
    param([string]$Clsid)
    if (-not $Clsid) { return $null }
    foreach ($view in @('CLSID', 'WOW6432Node\CLSID')) {
        $k = [Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey("$view\$Clsid\InprocServer32")
        if ($k) {
            try { $v = [string]$k.GetValue('') } finally { $k.Close() }
            if ($v) { return $v.Trim('"') }
        }
    }
    return $null
}

function Test-DllPresent {
    param([string]$Dll)
    if (-not $Dll) { return $true }
    $p = [Environment]::ExpandEnvironmentVariables($Dll)
    if (-not [IO.Path]::IsPathRooted($p)) { return $true }   # bare name: resolved via PATH/System32
    return (Test-Path -LiteralPath $p)
}

function Get-SdSnapshot {
    param([int]$Days)
    $since = (Get-Date).AddDays(-$Days)
    $S = @{ WindowDays = [double]$Days }

    # ---- disks ------------------------------------------------------------
    $bus = @{}
    try { Get-PhysicalDisk | ForEach-Object { $bus[[string]$_.DeviceId] = [string]$_.BusType } } catch { }
    $S.Disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object {
        $idx = [int]$_.Index
        $letters = @()
        try { $letters = @(Get-Partition -DiskNumber $idx -ErrorAction Stop | Where-Object { $_.DriveLetter } | ForEach-Object { [string]$_.DriveLetter }) } catch { }
        [pscustomobject]@{
            Index = $idx; SCSIPort = [int]$_.SCSIPort; Model = [string]$_.Model
            Bus = $(if ($bus.ContainsKey([string]$idx)) { $bus[[string]$idx] } else { [string]$_.InterfaceType })
            Letters = $letters
        }
    })

    # ---- storage stalls ---------------------------------------------------
    $S.StorageEvents = @()
    try {
        $S.StorageEvents = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 129, 153, 7, 51, 157; StartTime = $since } -ErrorAction Stop |
            Where-Object { $_.ProviderName -notmatch 'Service Control|Kernel-General' } |
            ForEach-Object {
                $dev = [string]$_.Properties[0].Value
                if ($_.Id -ne 129) {
                    $m = [regex]::Match([string]$_.Message, '(?i)(\bDisk\s+\d+|\\Device\\Harddisk\d+)')
                    if ($m.Success) { $dev = $m.Value }
                }
                [pscustomobject]@{ Id = $_.Id; Provider = $_.ProviderName; Device = $dev; Time = $_.TimeCreated }
            })
    } catch { }   # "No events were found" is the good case

    # ---- shell crashes ----------------------------------------------------
    $S.ShellCrashes = @()
    try {
        $S.ShellCrashes = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000, 1002; StartTime = $since } -ErrorAction Stop |
            Where-Object { $_.Message -match '(?i)explorer\.exe' } |
            ForEach-Object { [pscustomobject]@{ Id = $_.Id; Time = $_.TimeCreated } })
    } catch { }

    # ---- third-party DLLs inside explorer.exe -----------------------------
    $running = @(Get-Process | Where-Object { $_.Name -ne 'explorer' } | ForEach-Object { try { $_.Path } catch { } } | Where-Object { $_ })
    $seen = @{}
    $S.ExplorerModules = @(Get-Process explorer -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.Modules } catch { }
    } | Where-Object { $_.FileName -and -not (Test-SdWindowsPath $_.FileName) } | ForEach-Object {
        $f = $_.FileName
        if ($seen.ContainsKey($f)) { return }
        $seen[$f] = $true
        $company = ''
        try { $company = [string]$_.FileVersionInfo.CompanyName } catch { }
        if ($company -match '^Microsoft') { return }
        $root = Get-SdProductRoot $f
        $alive = $false
        if ($root) { $alive = @($running | Where-Object { $_.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0 }
        [pscustomobject]@{ Path = $f; Company = $company; Running = $alive }
    })

    # ---- overlays + context-menu handlers ---------------------------------
    $S.Overlays = @()
    $ok = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\ShellIconOverlayIdentifiers')
    if ($ok) {
        try {
            # Explorer takes them in name order, which is why vendors pad with spaces.
            $S.Overlays = @($ok.GetSubKeyNames() | Sort-Object | ForEach-Object {
                $sk = $ok.OpenSubKey($_); $c = $null
                if ($sk) { try { $c = [string]$sk.GetValue('') } finally { $sk.Close() } }
                $dll = Get-ClsidDll $c
                [pscustomobject]@{ Name = $_.Trim(); Dll = $dll; DllExists = (Test-DllPresent $dll) }
            })
        } finally { $ok.Close() }
    }
    $S.ContextHandlers = @(foreach ($r in $CtxRoots) {
        $k = [Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey("$r\shellex\ContextMenuHandlers")
        if (-not $k) { continue }
        try {
            foreach ($n in $k.GetSubKeyNames()) {
                $sk = $k.OpenSubKey($n); $c = $null
                if ($sk) { try { $c = [string]$sk.GetValue('') } finally { $sk.Close() } }
                if ($c -notmatch '^\{') { $c = $n }
                if ($c -notmatch '^\{') { continue }
                $dll = Get-ClsidDll $c
                [pscustomobject]@{ Key = $r; Name = $n; Dll = $dll; DllExists = (Test-DllPresent $dll) }
            }
        } finally { $k.Close() }
    })

    # ---- property + thumbnail handlers ------------------------------------
    $pairs = New-Object System.Collections.Generic.List[object]
    $ph = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\PropertySystem\PropertyHandlers')
    if ($ph) {
        try {
            foreach ($ext in $ph.GetSubKeyNames()) {
                $sk = $ph.OpenSubKey($ext); $c = $null
                if ($sk) { try { $c = [string]$sk.GetValue('') } finally { $sk.Close() } }
                $dll = Get-ClsidDll $c
                if ($dll) { $pairs.Add([pscustomobject]@{ Ext = $ext; Dll = $dll }) }
            }
        } finally { $ph.Close() }
    }
    foreach ($ext in ([Microsoft.Win32.Registry]::ClassesRoot.GetSubKeyNames() | Where-Object { $_.StartsWith('.') })) {
        $tk = [Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey("$ext\ShellEx\$ThumbGuid")
        if (-not $tk) { continue }
        try { $c = [string]$tk.GetValue('') } finally { $tk.Close() }
        $dll = Get-ClsidDll $c
        if ($dll) { $pairs.Add([pscustomobject]@{ Ext = $ext; Dll = $dll }) }
    }
    $S.Handlers = $pairs.ToArray()

    # ---- view state -------------------------------------------------------
    $bk = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$BagsPath\Bags")
    $S.BagsCount = if ($bk) { try { $bk.SubKeyCount } finally { $bk.Close() } } else { 0 }
    $S.ThumbCacheMB = 0.0
    if (Test-Path -LiteralPath $CacheDir) {
        $sum = (Get-ChildItem -LiteralPath $CacheDir -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        if ($sum) { $S.ThumbCacheMB = [math]::Round($sum / 1MB, 1) }
    }
    return $S
}

function Write-SdReport {
    param([object[]]$Findings, [hashtable]$S)
    $col = @{ HIGH = 'Red'; MEDIUM = 'Yellow'; LOW = 'Cyan'; INFO = 'Gray' }
    Write-Host ''
    Write-Host "SHELL DOCTOR  -  last $($S.WindowDays) days  -  $(Get-Date -Format 'yyyy-MM-dd HH:mm')" -ForegroundColor White
    Write-Host ('-' * 72)
    $real = @($Findings | Where-Object { $_.Severity -ne 'INFO' })
    if (-not $real.Count) {
        Write-Host 'No shell-lag cause found in the evidence.' -ForegroundColor Green
    }
    foreach ($f in $Findings) {
        Write-Host ''
        Write-Host ("[{0}] {1}  ({2})" -f $f.Severity, $f.Title, $f.Code) -ForegroundColor $col[$f.Severity]
        foreach ($d in $f.Detail) { Write-Host "    $d" }
        if ($f.Fix) { Write-Host "  -> $($f.Fix)" -ForegroundColor DarkGreen }
    }
    Write-Host ''
}

function Invoke-SdResetViews {
    param([switch]$Yes)
    if (-not $Yes) {
        Write-Host 'Reset folder views (all remembered view modes/sizes) and thumbnail/icon caches for THIS user?'
        Write-Host 'Explorer restarts (taskbar blinks). A registry backup is written first.'
        $a = Read-Host 'Type YES to continue'
        if ($a -ne 'YES') { Write-Host 'Cancelled.'; return $false }
    }
    $bkDir = Join-Path $env:LOCALAPPDATA ('SAITULS\shell_doctor\backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $bkDir -Force | Out-Null
    & reg.exe export "HKCU\$BagsPath" (Join-Path $bkDir 'shell-bags.reg') | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "Backup failed (reg export exit $LASTEXITCODE) - nothing changed." -ForegroundColor Red; return $false }
    Write-Host "Backup: $bkDir"

    $removed = 0; $locked = 0
    try {
        Get-Process explorer -ErrorAction SilentlyContinue | Stop-Process -Force
        # Winlogon restarts the shell by itself; retry briefly for the files it re-opens.
        $deadline = (Get-Date).AddSeconds(4)
        do {
            $left = @(Get-ChildItem -LiteralPath $CacheDir -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'thumbcache_*.db' -or $_.Name -like 'iconcache_*.db' })
            foreach ($f in $left) { try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop; $removed++ } catch { } }
            if (-not @(Get-ChildItem -LiteralPath $CacheDir -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'thumbcache_*.db' -or $_.Name -like 'iconcache_*.db' }).Count) { break }
            Start-Sleep -Milliseconds 300
        } while ((Get-Date) -lt $deadline)
        $locked = @(Get-ChildItem -LiteralPath $CacheDir -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'thumbcache_*.db' -or $_.Name -like 'iconcache_*.db' }).Count
        $legacy = Join-Path $env:LOCALAPPDATA 'IconCache.db'
        if (Test-Path -LiteralPath $legacy) { Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue }
        foreach ($sub in 'Bags', 'BagMRU') {
            $p = "HKCU:\$BagsPath\$sub"
            if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
        }
    } finally {
        Start-Sleep -Milliseconds 500
        if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) { Start-Process explorer.exe }
    }
    Write-Host ("Views reset. Cache files removed: {0}, still locked (rebuilt by Windows): {1}." -f $removed, $locked) -ForegroundColor Green
    Write-Host "Undo views: double-click $bkDir\shell-bags.reg"
    return $true
}

# ------------------------------------------------------------------ main --
if ($ResetViews) {
    $null = Invoke-SdResetViews -Yes:$Yes
    exit 0
}

$snap = Get-SdSnapshot -Days $Days
$findings = @(Get-SdFindings -S $snap)

if ($Json) {
    [pscustomobject]@{
        generated = (Get-Date).ToString('o')
        window_days = $Days
        findings = $findings
        summary = [pscustomobject]@{
            storage_events = @($snap.StorageEvents).Count
            shell_crashes = @($snap.ShellCrashes).Count
            explorer_third_party_dlls = @($snap.ExplorerModules).Count
            overlays = @($snap.Overlays).Count
            context_handlers = @($snap.ContextHandlers).Count
            handler_registrations = @($snap.Handlers).Count
            bags = $snap.BagsCount
            thumbcache_mb = $snap.ThumbCacheMB
        }
    } | ConvertTo-Json -Depth 6
    exit 0
}

Write-SdReport -Findings $findings -S $snap
if ($NoPause -or -not [Environment]::UserInteractive) { exit 0 }

Write-Host '[R] reset folder views + thumbnail/icon caches (per-user, backed up)'
Write-Host '[Enter] exit'
$k = Read-Host 'Choice'
if ($k -match '^[Rr]$') { $null = Invoke-SdResetViews; Read-Host 'Press Enter to exit' | Out-Null }
exit 0
