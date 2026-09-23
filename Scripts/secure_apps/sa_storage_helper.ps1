<#
.SYNOPSIS
    Privileged storage helper for SAITULS Secure Apps (bitlocker-vhdx backend).

.DESCRIPTION
    Attaching a VHDX and unlocking a BitLocker volume need administrator
    rights. Launching the protected application must NOT be elevated. So the
    broker stays at medium integrity and delegates exactly these operations
    to this script, which runs elevated and serves the broker over a named
    pipe (Scripts/secure_apps/sa_privhelper.py).

    This script does not run from the repository. An elevated Install/Repair
    transaction copies it, together with the frozen FIDO worker
    (sa_fido_worker.exe) and a runtime bundle manifest, into a protected
    directory %ProgramData%\SAITULS\secure-apps\privileged whose ACL lets a
    medium-integrity account execute but never modify it. The fixed task
    \SAITULS\SecureAppsPrivilegedHelper names THAT installed copy. After
    registration, ordinary unlock, lock, mount and relock start this helper
    WITHOUT a consent dialog -- and may start only this one action.

    Because a fixed task action carries no arguments, the pipe name and the
    worker path are read from the installation pin in
    %ProgramData%\SAITULS\secure-apps, which only SYSTEM and Administrators
    may write. Before serving anything the helper self-checks: its own path
    and SHA-256 must be the pinned ones, the frozen worker must sit inside the
    protected runtime root with the pinned digest, and the runtime bundle
    fingerprint must match the pin. The only executable it launches is that
    installed sa_fido_worker.exe, by absolute path -- there is no interpreter
    on the elevated runtime path, so no python.exe on PATH and no rewritable
    import tree can decide what this elevated process runs.

    This helper owns the pipe; the broker connects to it. It accepts one
    client at a time, refuses any client that is not this same account at
    medium integrity, and exits by itself once nobody has been connected for
    the pinned idle timeout -- so an elevated command channel does not
    outlive the work it was started for. The next unlock starts it again.

    The volume unlock secret arrives base64-encoded INSIDE the pipe message,
    is turned into a SecureString and is handed to the BitLocker cmdlets as a
    parameter. It never appears on a command line, in an environment
    variable, in a temp file, in a window title or in any log line. The only
    values this script writes to stdout are protocol responses, and the only
    secret any response ever carries is the BitLocker recovery password, once,
    at container creation, for the one-time display the enrollment flow
    requires.

    Protocol: newline-delimited UTF-8 JSON, one response per request.
      ->  {"id":N,"op":"<op>","args":{...}}
      <-  {"id":N,"ok":true,"result":{...}}
      <-  {"id":N,"ok":false,"error":"<category>","message":"<text>"}

    Operations: ping, state, helper_info, create, unlock_mount, unmount,
    shutdown.

.PARAMETER ScheduledHelper
    The production mode: validate the installation pin, serve the per-user
    helper pipe elevated, and exit on idle. This is the only switch the
    registered scheduled task passes.

.PARAMETER SelfTest
    Non-interactive structural probe. Reports which prerequisites are present
    and exits. Touches no disk image, needs no elevation, prints no secret.

.PARAMETER PinPath
    Diagnostics only, and honoured ONLY together with -SelfTest. The
    production path is a constant: an elevated process that let a caller
    choose its pin file would be letting the caller choose what it executes.
#>
[CmdletBinding()]
param(
    [switch]$ScheduledHelper,
    [switch]$SelfTest,
    [string]$PinPath
)

$ErrorActionPreference = 'Stop'

$ERR_BUSY = 'storage_busy'
$ERR_CREATE = 'storage_create_failed'
$ERR_UNLOCK = 'storage_unlock_failed'
$ERR_MOUNT = 'storage_mount_failed'
$ERR_UNMOUNT = 'storage_unmount_failed'
$ERR_INTERNAL = 'internal'

# ═══════════════════════════════ trusted privileged runtime environment
# This script runs at HIGH integrity, so nothing it loads or launches may be
# FOUND for it -- it has to be CHOSEN by it.
#
#   * PowerShell auto-loads a module for an unqualified command from
#     $env:PSModulePath, whose first entry is normally the CurrentUser
#     location under %USERPROFILE%\Documents. A medium-integrity account owns
#     that directory, so an elevated Get-Disk could be somebody else's
#     Get-Disk. Before any Storage or BitLocker command can auto-load, the
#     search path is rebuilt from machine locations only and the two modules
#     are imported from absolute system paths; the commands are then written
#     module-qualified (Storage\Get-Disk, BitLocker\Unlock-BitLocker) so the
#     resolution never depends on a same-named user module losing a race.
#   * every machine location is resolved through the known-folder API, never
#     through the environment block: %SystemRoot% and %ProgramFiles% are
#     ordinary environment variables and a user-scope variable of the same
#     name wins in the merged block a scheduled task inherits.
#   * every external executable is launched by absolute, verified path -- no
#     privileged child is ever selected through PATH.
$STORAGE_MODULE = 'Storage'
$BITLOCKER_MODULE = 'BitLocker'
$BUNDLE_FINGERPRINT_SCHEMA = 'saituls.secure-apps.fido-worker-bundle/1'
$FIDO_BUNDLE_DIR_NAME = 'fido-worker'
$FIDO_WORKER_EXE_NAME = 'sa_fido_worker.exe'

#: SIDs that are SUPPOSED to control the protected runtime: Administrators,
#: SYSTEM, CREATOR OWNER. Any other SID holding a write/replace right is the
#: boundary not being enforced.
$PRIVILEGED_SIDS = @('S-1-5-32-544', 'S-1-5-18', 'S-1-3-0')

#: WriteData | AppendData | WriteExtendedAttributes | WriteAttributes |
#: Delete | ChangePermissions | TakeOwnership. FullControl is a superset.
$MEDIUM_WRITE_MASK = 0xD0116

function Get-WindowsRoot {
    $root = ''
    try { $root = [Environment]::GetFolderPath('Windows') } catch { }
    if (-not $root) { $root = 'C:\Windows' }
    return $root
}

function Get-SystemModuleRoot {
    return (Join-Path (Get-WindowsRoot) 'System32\WindowsPowerShell\v1.0\Modules')
}

function Test-PathUnderUserProfile([string]$Path) {
    $profileRoot = ''
    try { $profileRoot = [Environment]::GetFolderPath('UserProfile') } catch { }
    if (-not $profileRoot) { return $true }      # unprovable is not clean
    $full = $null
    try { $full = [System.IO.Path]::GetFullPath($Path) } catch { return $true }
    $prefix = ($profileRoot.TrimEnd('\') + '\').ToLowerInvariant()
    return $full.ToLowerInvariant().StartsWith($prefix)
}

function Get-TrustedModulePath {
    $paths = @((Get-SystemModuleRoot))
    $programFiles = ''
    try { $programFiles = [Environment]::GetFolderPath('ProgramFiles') } catch { }
    if ($programFiles) { $paths += (Join-Path $programFiles 'WindowsPowerShell\Modules') }
    return @($paths | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Container) -and
        (-not (Test-PathUnderUserProfile $_))
    })
}

# Imported by absolute manifest path, then CONFIRMED to have come from there:
# Import-Module succeeding is not by itself evidence about which file answered.
function Import-TrustedModule([string]$Name) {
    $manifest = Join-Path (Get-SystemModuleRoot) ('{0}\{0}.psd1' -f $Name)
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { return $false }
    if (Test-PathUnderUserProfile $manifest) { return $false }
    try {
        Import-Module -Name $manifest -Force -DisableNameChecking -ErrorAction Stop
    } catch {
        return $false
    }
    $loaded = @(Get-Module -Name $Name)
    if ($loaded.Count -ne 1) { return $false }
    # A manifest reports the file that really answered -- for BitLocker that
    # is BitLocker.psm1, not the .psd1 that was asked for -- so the question
    # is whether the loaded module lives in the trusted system directory for
    # this module name, not whether one exact filename came back.
    $expectedBase = $null
    $actualBase = $null
    try { $expectedBase = ([System.IO.Path]::GetFullPath((Join-Path (Get-SystemModuleRoot) $Name))).TrimEnd('\') } catch { }
    try { $actualBase = ([System.IO.Path]::GetFullPath([string]$loaded[0].ModuleBase)).TrimEnd('\') } catch { }
    if (-not $expectedBase -or -not $actualBase) { return $false }
    if ($actualBase -ine $expectedBase) { return $false }
    $file = $null
    try { $file = [System.IO.Path]::GetFullPath([string]$loaded[0].Path) } catch { }
    if (-not $file) { return $false }
    return $file.ToLowerInvariant().StartsWith(($expectedBase + '\').ToLowerInvariant())
}

function Initialize-TrustedModuleEnvironment {
    $trusted = @(Get-TrustedModulePath)
    if ($trusted.Count -lt 1) {
        $script:TrustedModulePathApplied = $false
    } else {
        $script:TrustedModulePath = ($trusted -join ';')
        $env:PSModulePath = $script:TrustedModulePath
        $script:TrustedModulePathApplied = $true
        foreach ($entry in $trusted) {
            if (Test-PathUnderUserProfile $entry) { $script:TrustedModulePathApplied = $false }
        }
    }
    $script:StorageModuleTrusted = $false
    $script:BitLockerModuleTrusted = $false
    if ($script:TrustedModulePathApplied) {
        $script:StorageModuleTrusted = Import-TrustedModule $STORAGE_MODULE
        $script:BitLockerModuleTrusted = Import-TrustedModule $BITLOCKER_MODULE
    }
    return [pscustomobject]@{
        trusted_module_path      = [bool]$script:TrustedModulePathApplied
        storage_module_trusted   = [bool]$script:StorageModuleTrusted
        bitlocker_module_trusted = [bool]$script:BitLockerModuleTrusted
        module_path              = [string]$script:TrustedModulePath
    }
}

$script:TrustedExecutables = @{}

# %SystemRoot%\System32\<name>, verified to exist. Never an unqualified name:
# a bare 'diskpart.exe' from an elevated process is a PATH lookup, and PATH is
# assembled from variables a medium account can set.
function Get-TrustedSystemExecutable([string]$Name) {
    if ($script:TrustedExecutables.ContainsKey($Name)) { return $script:TrustedExecutables[$Name] }
    $candidate = Join-Path (Get-WindowsRoot) ('System32\' + $Name)
    $full = $null
    try { $full = [System.IO.Path]::GetFullPath($candidate) } catch { }
    if (-not $full -or -not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "the trusted Windows executable $Name is not present at $candidate"
    }
    $script:TrustedExecutables[$Name] = $full
    return $full
}

function Test-MediumWritable([string]$Path) {
    try {
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    } catch {
        return $true                              # unreadable is never "safe"
    }
    if (-not $acl) { return $true }
    foreach ($rule in @($acl.Access)) {
        if ("$($rule.AccessControlType)" -ne 'Allow') { continue }
        $sid = $null
        try {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]).Value
        } catch { return $true }
        if ($PRIVILEGED_SIDS -contains $sid) { continue }
        if ((([int]$rule.FileSystemRights) -band $MEDIUM_WRITE_MASK) -ne 0) { return $true }
    }
    return $false
}

# ═══════════════════════════════════════════════ protected privileged scratch
# An elevated DiskPart executes whatever its script file says. That file holds
# no secret, but its INTEGRITY decides what an elevated process does, so it
# may never live in the medium user's TEMP. It goes in a per-transaction
# directory under %ProgramData%\SAITULS\secure-apps\privileged-tmp, which the
# elevated installer created with SYSTEM + Administrators full control and no
# medium-integrity write at all.
function Get-PrivilegedTmpRoot {
    $root = ''
    try { $root = [Environment]::GetFolderPath('CommonApplicationData') } catch { }
    if (-not $root) { $root = 'C:\ProgramData' }
    return (Join-Path (Join-Path $root 'SAITULS\secure-apps') 'privileged-tmp')
}

function New-PrivilegedScratchDir([string]$Prefix) {
    $root = Get-PrivilegedTmpRoot
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "the privileged scratch root is missing: $root"
    }
    if (Test-MediumWritable $root) {
        throw 'the privileged scratch root is writable by a medium-integrity account'
    }
    $dir = Join-Path $root ($Prefix + '-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir -ErrorAction Stop | Out-Null
    $icacls = Get-TrustedSystemExecutable 'icacls.exe'
    & $icacls $dir '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' `
        '/grant:r' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        throw 'the privileged scratch directory could not be hardened'
    }
    return $dir
}

# An elevated process creates objects owned by the ACCOUNT, not by
# Administrators, unless the machine's default-owner policy says otherwise --
# and an owner holds WRITE_DAC implicitly. So a scratch file left owned by the
# interactive user could be re-permissioned by that same user at medium
# integrity, however tight the inherited DACL is. Ownership moves to
# Administrators as part of writing it.
function Set-PrivilegedOwner([string]$Path) {
    $icacls = Get-TrustedSystemExecutable 'icacls.exe'
    & $icacls $Path '/setowner' '*S-1-5-32-544' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'the privileged command file could not be re-owned'
    }
    return $true
}

# Asked immediately before an elevated consumer opens the file, never earlier:
# still inside the protected root, not redirected there by a reparse point,
# and not writable by a medium-integrity account.
function Assert-PrivilegedScratchFile([string]$Path) {
    $root = ([System.IO.Path]::GetFullPath((Get-PrivilegedTmpRoot))).TrimEnd('\')
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.ToLowerInvariant().StartsWith(($root + '\').ToLowerInvariant())) {
        throw 'the privileged command file is not inside the protected scratch root'
    }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw 'the privileged command file disappeared before it was used'
    }
    $cursor = $full
    while ($cursor) {
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        if ($item -and (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw 'the privileged command file is reached through a reparse point'
        }
        if ($cursor -ieq $root) { break }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or ($parent -ieq $cursor)) { break }
        $cursor = $parent
    }
    if (Test-MediumWritable $full) {
        throw 'the privileged command file is writable by a medium-integrity account'
    }
    $owner = $null
    try { $owner = (Get-Acl -LiteralPath $full).GetOwner([Security.Principal.SecurityIdentifier]).Value } catch { }
    if (-not $owner -or @('S-1-5-32-544', 'S-1-5-18') -notcontains $owner) {
        throw 'the privileged command file is not owned by Administrators or SYSTEM'
    }
    return $true
}

# ═══════════════════════════════════ recursive FIDO worker bundle fingerprint
# sa_privtask.py computes exactly this value in Python; the two implementations
# agree byte for byte because the canonical rules are fixed: relative paths
# from the bundle root, '/' separators, lowercased, printable ASCII only,
# sorted ORDINALLY, one {"path","sha256","size"} object each, wrapped in
# {"entries":[...],"file_count":N,"schema":"..."} with sorted keys and no
# insignificant whitespace. No timestamp and no absolute path is bound, so the
# value is reproducible from the installed tree alone.
function ConvertTo-CanonicalJsonString([string]$Value) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '"') { [void]$sb.Append('\"') }
        elseif ($ch -eq '\') { [void]$sb.Append('\\') }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Get-BundleFingerprint([string]$BundleRoot) {
    if (-not (Test-Path -LiteralPath $BundleRoot -PathType Container)) {
        throw 'the FIDO worker bundle directory is missing'
    }
    $rootFull = ([System.IO.Path]::GetFullPath($BundleRoot)).TrimEnd('\')
    $map = @{}
    $stack = New-Object System.Collections.Stack
    $stack.Push($rootFull)
    while ($stack.Count -gt 0) {
        $current = [string]$stack.Pop()
        $children = @()
        try { $children = @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop) }
        catch { throw 'the FIDO worker bundle directory could not be read' }
        foreach ($item in $children) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'the FIDO worker bundle contains a reparse point'
            }
            if ($item.PSIsContainer) { $stack.Push($item.FullName); continue }
            $rel = $item.FullName.Substring($rootFull.Length + 1).Replace('\', '/').ToLowerInvariant()
            foreach ($ch in $rel.ToCharArray()) {
                if (([int]$ch) -lt 0x20 -or ([int]$ch) -gt 0x7E) {
                    throw 'a FIDO worker bundle path is not printable ASCII'
                }
            }
            if ($map.ContainsKey($rel)) {
                throw 'two FIDO worker bundle entries share one path'
            }
            $digest = Get-FileSha256 $item.FullName
            if (-not $digest) { throw 'a FIDO worker bundle file could not be hashed' }
            $map[$rel] = [pscustomobject]@{ sha256 = $digest; size = [int64]$item.Length }
        }
    }
    if ($map.Count -eq 0) { throw 'the FIDO worker bundle is empty' }
    if (-not $map.ContainsKey($FIDO_WORKER_EXE_NAME.ToLowerInvariant())) {
        throw 'the FIDO worker bundle does not contain the worker executable'
    }
    $keys = [string[]]@($map.Keys)
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('{"entries":[')
    for ($i = 0; $i -lt $keys.Length; $i++) {
        if ($i -gt 0) { [void]$sb.Append(',') }
        $entry = $map[$keys[$i]]
        [void]$sb.Append('{"path":')
        [void]$sb.Append((ConvertTo-CanonicalJsonString $keys[$i]))
        [void]$sb.Append(',"sha256":')
        [void]$sb.Append((ConvertTo-CanonicalJsonString $entry.sha256))
        [void]$sb.Append(',"size":')
        [void]$sb.Append(([string]$entry.size))
        [void]$sb.Append('}')
    }
    [void]$sb.Append('],"file_count":')
    [void]$sb.Append([string]$keys.Length)
    [void]$sb.Append(',"schema":')
    [void]$sb.Append((ConvertTo-CanonicalJsonString $BUNDLE_FINGERPRINT_SCHEMA))
    [void]$sb.Append('}')
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Category only. An exception's own text is the classic place a secret leaks
# from, so it is mapped to a fixed string and dropped.
function Get-ErrorCategory([string]$Fallback, $Exception) {
    $text = ''
    try { $text = "$($Exception.GetType().Name) $($Exception.Message)".ToLowerInvariant() } catch { }
    if ($text -match 'in use|being used|denied access|sharing violation|because it is open') { return $ERR_BUSY }
    if ($text -match 'password is not correct|invalid password|0x80310027|not authenticated') { return $ERR_UNLOCK }
    if ($text -match 'auth_capability') { return 'auth_capability' }
    if ($text -match 'auth_cancelled') { return 'auth_cancelled' }
    if ($text -match 'auth_wrong_credential') { return 'auth_wrong_credential' }
    if ($text -match 'auth_unavailable') { return 'auth_unavailable' }
    if ($text -match 'auth_failed') { return 'auth_failed' }
    return $Fallback
}

function Resolve-ContainerDisk([string]$Container) {
    $image = Storage\Get-DiskImage -ImagePath $Container -ErrorAction Stop
    if (-not $image.Attached) { return $null }
    return Storage\Get-Disk -Number $image.Number -ErrorAction Stop
}

function Resolve-DataPartition($Disk) {
    if ($null -eq $Disk) { return $null }
    $parts = @(Storage\Get-Partition -DiskNumber $Disk.Number -ErrorAction SilentlyContinue |
        Where-Object { $_.Type -ne 'Reserved' -and $_.Size -gt 16MB })
    if ($parts.Count -eq 0) { return $null }
    return $parts[0]
}

# The volume GUID path, e.g. \\?\Volume{...}\. Present on the partition even
# while the volume is BitLocker-locked, which a drive letter is not -- so the
# vault never has to be exposed at a letter just to be unlocked.
function Resolve-VolumePath($Partition) {
    if ($null -eq $Partition) { return $null }
    $guid = @($Partition.AccessPaths | Where-Object { $_ -like '\\?\Volume{*' }) | Select-Object -First 1
    return $guid
}

function Get-ContainerState([string]$Container, [string]$MountPath) {
    if (-not (Test-Path -LiteralPath $Container -PathType Leaf)) {
        return @{ state = 'missing'; mounted_at = $null }
    }
    $disk = Resolve-ContainerDisk $Container
    if ($null -eq $disk) { return @{ state = 'detached'; mounted_at = $null } }
    $part = Resolve-DataPartition $disk
    $volume = Resolve-VolumePath $part
    if ($null -eq $volume) { return @{ state = 'attached_locked'; mounted_at = $null } }
    $lock = 'Unknown'
    try { $lock = (BitLocker\Get-BitLockerVolume -MountPoint $volume -ErrorAction Stop).LockStatus } catch { $lock = 'Unknown' }
    if ("$lock" -eq 'Locked') { return @{ state = 'attached_locked'; mounted_at = $null } }
    $paths = @($part.AccessPaths | Where-Object { $_ -notlike '\\?\Volume{*' })
    $at = $null
    if ($MountPath) {
        $wanted = $MountPath.TrimEnd('\')
        $at = @($paths | Where-Object { $_.TrimEnd('\') -ieq $wanted }) | Select-Object -First 1
    }
    if (-not $at -and $paths.Count -gt 0) { $at = $paths[0] }
    return @{ state = 'mounted'; mounted_at = $at }
}

# Windows attaches a volume only to an EMPTY directory. Asking here, before
# any mutation, turns "failed halfway through building your vault" into a
# refusal that leaves the user's data exactly where it was. Never call
# Storage\Add-PartitionAccessPath without this.
function Assert-EmptyMountTarget([string]$Path, [string]$What = 'mount target') {
    if (-not $Path) { return }
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$What exists and is not a directory: $Path"
    }
    $entry = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop |
        Select-Object -First 1)
    if ($entry.Count -gt 0) {
        throw "$What is not empty: $Path"
    }
}

function ConvertTo-Secure([string]$Base64) {
    if ([string]::IsNullOrEmpty($Base64)) { throw 'no unlock secret supplied' }
    $bytes = [System.Convert]::FromBase64String($Base64)
    $chars = [System.Text.Encoding]::ASCII.GetChars($bytes)
    $secure = New-Object System.Security.SecureString
    try {
        foreach ($ch in $chars) { $secure.AppendChar($ch) }
        $secure.MakeReadOnly()
        return $secure
    } finally {
        for ($i = 0; $i -lt $chars.Length; $i++) { $chars[$i] = [char]0 }
        for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = 0 }
    }
}

function New-VhdxContainer([string]$Container, [int]$SizeGb) {
    $dir = Split-Path -Parent $Container
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $Container) {
        throw "container already exists: $Container"
    }
    # New-VHD lives in the Hyper-V module, which a Pro workstation need not
    # have. diskpart is always present and its script carries no secret --
    # only a path and a size.
    $mb = [int]($SizeGb * 1024)
    # 'create vdisk' does NOT attach, so the 'detach vdisk' this script used
    # to send always failed with "the virtual disk is already detached" --
    # and diskpart's non-zero exit turned a perfectly good image into a
    # refused enrollment on every first run. Create, then stop.
    $script = @(
        "create vdisk file=`"$Container`" maximum=$mb type=expandable",
        "exit"
    ) -join "`r`n"
    # The script file is privileged command input: DiskPart executes whatever
    # it says, elevated. It therefore lives in the protected scratch root, in
    # a directory of its own for this one transaction, and its path, its
    # reparse-point freedom and its ACL are re-checked in the instant before
    # DiskPart opens it -- not when it was written.
    $scratch = New-PrivilegedScratchDir 'vhdx'
    try {
        $tmp = Join-Path $scratch ('create-' + [guid]::NewGuid().ToString('N') + '.dpt')
        Set-Content -LiteralPath $tmp -Value $script -Encoding ASCII
        [void](Set-PrivilegedOwner $tmp)
        [void](Assert-PrivilegedScratchFile $tmp)
        $diskpart = Get-TrustedSystemExecutable 'diskpart.exe'
        $out = & $diskpart /s $tmp 2>&1
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Container)) {
            throw "diskpart could not create the container (exit $LASTEXITCODE)"
        }
    } finally {
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Wait-Encryption([string]$Volume, [int]$TimeoutSeconds = 900) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $bl = BitLocker\Get-BitLockerVolume -MountPoint $Volume -ErrorAction Stop
        if ("$($bl.VolumeStatus)" -eq 'FullyEncrypted' -or $bl.EncryptionPercentage -ge 100) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Invoke-Create($Arguments) {
    $container = [string]$Arguments.container
    $mountPath = [string]$Arguments.mount_path
    $sizeGb = [int]$Arguments.size_gb
    $label = [string]$Arguments.label
    $secure = ConvertTo-Secure ([string]$Arguments.secret_b64)

    # BEFORE the image exists: a non-empty mount target is a configuration
    # problem, and finding it here costs nothing to unwind.
    Assert-EmptyMountTarget -Path $mountPath -What 'container mount target'

    New-VhdxContainer -Container $container -SizeGb $sizeGb
    $image = Storage\Mount-DiskImage -ImagePath $container -StorageType VHDX -NoDriveLetter -PassThru -ErrorAction Stop
    $disk = Storage\Get-Disk -Number $image.Number -ErrorAction Stop
    if ("$($disk.PartitionStyle)" -eq 'RAW') {
        Storage\Initialize-Disk -Number $disk.Number -PartitionStyle GPT -Confirm:$false -ErrorAction Stop | Out-Null
    }
    $part = Storage\New-Partition -DiskNumber $disk.Number -UseMaximumSize -ErrorAction Stop
    Storage\Format-Volume -Partition $part -FileSystem NTFS -NewFileSystemLabel $label -Force -Confirm:$false -ErrorAction Stop | Out-Null
    $part = Storage\Get-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber
    $volume = Resolve-VolumePath $part
    if (-not $volume) { throw 'the new volume exposed no volume GUID path' }

    # -WarningAction SilentlyContinue is a secret-hygiene control, not noise
    # suppression: BitLocker\Add-BitLockerKeyProtector prints the recovery password in
    # full on the warning stream, and the one place it is allowed to appear
    # is this operation's single response.
    BitLocker\Enable-BitLocker -MountPoint $volume -PasswordProtector -Password $secure `
        -EncryptionMethod XtsAes256 -UsedSpaceOnly -SkipHardwareTest `
        -WarningAction SilentlyContinue -ErrorAction Stop | Out-Null
    $recovery = BitLocker\Add-BitLockerKeyProtector -MountPoint $volume -RecoveryPasswordProtector `
        -WarningAction SilentlyContinue -ErrorAction Stop
    $recoveryPassword = @($recovery.KeyProtector |
        Where-Object { "$($_.KeyProtectorType)" -eq 'RecoveryPassword' } |
        Select-Object -Last 1).RecoveryPassword
    [void](Wait-Encryption -Volume $volume)

    if ($mountPath) {
        Assert-EmptyMountTarget -Path $mountPath -What 'container mount target'
        if (-not (Test-Path -LiteralPath $mountPath)) {
            New-Item -ItemType Directory -Path $mountPath -Force | Out-Null
        }
        Storage\Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber `
            -AccessPath $mountPath -ErrorAction Stop
    }
    return @{
        container    = $container
        mount_path   = $mountPath
        volume       = $volume
        recovery_password = $recoveryPassword
    }
}

function Invoke-UnlockMount($Arguments) {
    $container = [string]$Arguments.container
    $mountPath = [string]$Arguments.mount_path
    $secure = ConvertTo-Secure ([string]$Arguments.secret_b64)

    if (-not (Test-Path -LiteralPath $container -PathType Leaf)) {
        throw "container not found: $container"
    }
    $image = Storage\Get-DiskImage -ImagePath $container -ErrorAction Stop
    if (-not $image.Attached) {
        $image = Storage\Mount-DiskImage -ImagePath $container -StorageType VHDX -NoDriveLetter -PassThru -ErrorAction Stop
    }
    $disk = Storage\Get-Disk -Number $image.Number -ErrorAction Stop
    $part = Resolve-DataPartition $disk
    $volume = Resolve-VolumePath $part
    if (-not $volume) { throw 'the attached container exposed no volume GUID path' }

    $bl = BitLocker\Get-BitLockerVolume -MountPoint $volume -ErrorAction Stop
    if ("$($bl.LockStatus)" -eq 'Locked') {
        BitLocker\Unlock-BitLocker -MountPoint $volume -Password $secure -ErrorAction Stop | Out-Null
    }
    $already = @($part.AccessPaths | Where-Object { $_.TrimEnd('\') -ieq $mountPath.TrimEnd('\') })
    if ($already.Count -eq 0) {
        # Only a NEW access path needs an empty target; a path this partition
        # already owns is showing the vault, which is the whole point.
        Assert-EmptyMountTarget -Path $mountPath -What 'volume mount target'
        if (-not (Test-Path -LiteralPath $mountPath)) {
            New-Item -ItemType Directory -Path $mountPath -Force | Out-Null
        }
        Storage\Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber `
            -AccessPath $mountPath -ErrorAction Stop
    }
    return @{ container = $container; mount_path = $mountPath; volume = $volume; state = 'mounted' }
}

function Invoke-Unmount($Arguments) {
    $container = [string]$Arguments.container
    $mountPath = [string]$Arguments.mount_path
    if (-not (Test-Path -LiteralPath $container -PathType Leaf)) {
        return @{ container = $container; state = 'missing' }
    }
    $image = Storage\Get-DiskImage -ImagePath $container -ErrorAction Stop
    if (-not $image.Attached) { return @{ container = $container; state = 'detached' } }
    $disk = Storage\Get-Disk -Number $image.Number -ErrorAction Stop
    $part = Resolve-DataPartition $disk
    $volume = Resolve-VolumePath $part
    if ($part -and $mountPath) {
        $present = @($part.AccessPaths | Where-Object { $_.TrimEnd('\') -ieq $mountPath.TrimEnd('\') })
        if ($present.Count -gt 0) {
            Storage\Remove-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber `
                -AccessPath $mountPath -ErrorAction Stop
        }
    }
    if ($volume) {
        try { BitLocker\Lock-BitLocker -MountPoint $volume -ErrorAction Stop | Out-Null } catch { }
    }
    Storage\Dismount-DiskImage -ImagePath $container -ErrorAction Stop | Out-Null
    return @{ container = $container; state = 'detached' }
}

# A fully qualified local path (C:\...) and nothing else: not relative, not
# drive-relative (C:x), not rooted without a drive (\x), not UNC, and already
# canonical, so no '.' or '..' segment can redirect it.
function Test-AbsoluteLocalPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if ($Path -notmatch '^[A-Za-z]:\\') { return $false }
    try { $full = [System.IO.Path]::GetFullPath($Path) } catch { return $false }
    return ($full -ieq $Path)
}

function New-WorkerLaunchRefusal([string]$Code) {
    # Category first, fixed reason code second: Get-ErrorCategory maps the
    # text, and no path or caller-supplied value ever rides along.
    return "auth_unavailable: fido worker launch refused ($Code)"
}

# This process is elevated. It launches ONE thing: the frozen
# sa_fido_worker.exe that the elevated installer copied into this protected
# runtime directory, by absolute path. There is no interpreter on this path
# any more -- no python.exe to find on PATH, no import tree a medium account
# could rewrite. The worker must be the exact file the pin names, it must sit
# inside the protected runtime root, and it must exist; anything else is
# refused before a process is created.
function Resolve-FidoWorkerLaunch([string]$WorkerExe, [string]$RuntimeRoot) {
    if ([string]::IsNullOrWhiteSpace($WorkerExe)) { throw (New-WorkerLaunchRefusal 'fido_worker_missing') }
    if (-not (Test-AbsoluteLocalPath $WorkerExe)) { throw (New-WorkerLaunchRefusal 'fido_worker_not_absolute') }
    if ([System.IO.Path]::GetExtension($WorkerExe) -ine '.exe') { throw (New-WorkerLaunchRefusal 'fido_worker_not_executable') }
    if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) { throw (New-WorkerLaunchRefusal 'fido_worker_unexpected') }
    $expected = $null
    try {
        $expected = [System.IO.Path]::GetFullPath(
            (Join-Path (Join-Path $RuntimeRoot $FIDO_BUNDLE_DIR_NAME) $FIDO_WORKER_EXE_NAME))
    } catch { }
    if (-not $expected -or $WorkerExe -ine $expected) { throw (New-WorkerLaunchRefusal 'fido_worker_unexpected') }
    if (-not (Test-Path -LiteralPath $WorkerExe -PathType Leaf)) { throw (New-WorkerLaunchRefusal 'fido_worker_not_found') }
    return [pscustomobject]@{ Worker = $WorkerExe }
}

function Invoke-FidoWorker($WorkerPayload) {
    $launch = Resolve-FidoWorkerLaunch -WorkerExe $FidoWorkerExe -RuntimeRoot $RuntimeRoot

    $json = $WorkerPayload | ConvertTo-Json -Compress -Depth 6
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $launch.Worker
    $psi.Arguments = ''
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $inWriter = $proc.StandardInput
    $inWriter.WriteLine($json)
    $inWriter.Flush()
    $inWriter.Close()

    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ([string]::IsNullOrEmpty($stdout)) {
        throw "FIDO worker produced no output (exit $($proc.ExitCode)): $stderr"
    }
    $resp = $stdout | ConvertFrom-Json
    if (-not $resp.ok) {
        $cat = if ($resp.error) { [string]$resp.error } else { $ERR_INTERNAL }
        throw "fido_error: $cat"
    }
    return $resp.result
}

# NOTE: the payload parameter is deliberately NOT named after PowerShell's
# automatic unbound-argument variable. A parameter with that name is shadowed
# by it, so every payload field read from an empty Object[] and every storage
# operation silently received nothing at all.
function Invoke-Operation([string]$Op, $Arguments) {
    switch ($Op) {
        'ping'              { return @{ pong = $true } }
        'state'             { return (Get-ContainerState ([string]$Arguments.container) ([string]$Arguments.mount_path)) }
        'create'            { return (Invoke-Create $Arguments) }
        'unlock_mount'      { return (Invoke-UnlockMount $Arguments) }
        'unmount'           { return (Invoke-Unmount $Arguments) }
        'fido_capabilities' { return (Invoke-FidoWorker @{ op = 'capabilities'; args = $Arguments }) }
        'fido_create'       {
            try {
                return (Invoke-FidoWorker @{ op = 'create'; args = $Arguments })
            } finally {
                if ($Arguments -and $Arguments.pin) { $Arguments.pin = $null }
            }
        }
        'fido_hmac'         {
            try {
                return (Invoke-FidoWorker @{ op = 'hmac'; args = $Arguments })
            } finally {
                if ($Arguments -and $Arguments.pin) { $Arguments.pin = $null }
            }
        }
        default             { throw "unknown operation: $Op" }
    }
}

# ═══════════════════════════════════════════ installation pin and identity
# The pin is the elevated half of the fixed task action. sa_privtask.py
# writes it from an elevated installer and hardens its ACL to
# Administrators/SYSTEM; everything below treats a pin it cannot fully
# verify as a refusal to start, never as a default to fall back on.
$PIN_SCHEMA = 'saituls.secure-apps.privileged-helper-pin/3'
$RUNTIME_MANIFEST_SCHEMA = 'saituls.secure-apps.privileged-runtime/2'
$GREETING_SCHEMA = 'saituls.secure-apps.privileged-helper-greeting/1'
$INTEGRITY_HIGH_RID = 12288

# .NET exposes no mandatory label: WindowsIdentity.Groups omits the S-1-16-*
# SID even for the process's own token, so "is the client elevated?" cannot be
# answered from the managed API at all. It is answered here, from the token
# itself, through the one Win32 call that knows -- and a host where this
# cannot be compiled reports it in -SelfTest instead of quietly accepting
# every client.
$INTEGRITY_PROBE_SOURCE = @'
using System;
using System.Runtime.InteropServices;
public static class SaitulsTokenIntegrity
{
    const int TokenIntegrityLevel = 25;
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool GetTokenInformation(IntPtr TokenHandle, int TokenInformationClass,
        IntPtr TokenInformation, int TokenInformationLength, out int ReturnLength);
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    public static int Level(IntPtr token)
    {
        int size = 0;
        GetTokenInformation(token, TokenIntegrityLevel, IntPtr.Zero, 0, out size);
        if (size <= 0) { return -1; }
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try
        {
            if (!GetTokenInformation(token, TokenIntegrityLevel, buffer, size, out size)) { return -1; }
            IntPtr sid = Marshal.ReadIntPtr(buffer);
            IntPtr countPtr = GetSidSubAuthorityCount(sid);
            int count = Marshal.ReadByte(countPtr);
            if (count <= 0) { return -1; }
            IntPtr ridPtr = GetSidSubAuthority(sid, (uint)(count - 1));
            return Marshal.ReadInt32(ridPtr);
        }
        finally { Marshal.FreeHGlobal(buffer); }
    }
}
'@

function Initialize-IntegrityProbe {
    if ($script:IntegrityProbeReady) { return $true }
    try {
        if (-not ('SaitulsTokenIntegrity' -as [type])) {
            Add-Type -TypeDefinition $INTEGRITY_PROBE_SOURCE -Language CSharp -ErrorAction Stop
        }
        $script:IntegrityProbeReady = $true
    } catch {
        $script:IntegrityProbeReady = $false
    }
    return $script:IntegrityProbeReady
}

function Get-DefaultPinPath {
    # The known-folder API, never the environment block: an elevated process
    # that trusts %ProgramData% lets whoever set that variable choose where it
    # reads the file that names what it executes.
    $root = ''
    try { $root = [Environment]::GetFolderPath('CommonApplicationData') } catch { }
    if (-not $root) { $root = 'C:\ProgramData' }
    return (Join-Path (Join-Path $root 'SAITULS\secure-apps') 'privileged-helper.json')
}

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try { return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
        finally { $stream.Dispose() }
    } finally { $sha.Dispose() }
}

function Get-CurrentUserSid {
    return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

# Both ends derive this from the account SID rather than exchanging it,
# because a fixed task action has no command line to carry it on. It is not
# a secret and authenticates nothing: it only keeps two users apart.
function Get-HelperPipeName([string]$Sid) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes("saituls.secure-apps.privileged-helper/1|$Sid")
        $hex = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    return ('\\.\pipe\SAITULS_SECAPP_PRIV_' + $hex.Substring(0, 32))
}

# The runtime manifest lives beside this helper in the protected runtime
# directory ($PSScriptRoot is the runtime root when the installed helper runs).
function Get-RuntimeManifestPath([string]$RuntimeRoot) {
    return (Join-Path $RuntimeRoot 'privileged-runtime.json')
}

function Read-PinRecord([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "the privileged helper pin record is missing: $Path"
    }
    $pin = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($pin.schema -ne $PIN_SCHEMA) { throw 'the pin record uses an unexpected schema' }
    $sid = Get-CurrentUserSid
    if ($pin.user_sid -ne $sid) { throw 'the pin record belongs to a different account' }
    $expectedPipe = Get-HelperPipeName $sid
    if ($pin.pipe_name -ne $expectedPipe) { throw 'the pinned pipe name does not belong to this account' }
    return $pin
}

# Defense in depth on top of the filesystem ACL: even if the ACL were wrong,
# the helper refuses to run unless its OWN resolved path and SHA-256 are the
# pinned ones, the frozen worker sits inside the protected runtime root with
# the pinned digest, and the runtime bundle manifest's fingerprint matches the
# pin. Any mismatch is refused before a worker process could ever be created.
function Assert-PinnedRuntime($Pin, [string]$RuntimeRoot, [bool]$CheckSelf = $true) {
    $self = $PSCommandPath
    # Own identity: resolved path and digest must be exactly the pinned ones.
    # Skipped under -SelfTest, where the running script is the repository copy
    # and the point is to validate the PIN, not this process.
    if (-not $Pin.helper_path) { throw 'the pin record names no privileged helper' }
    if ($CheckSelf) {
        $selfFull = $null
        try { $selfFull = [System.IO.Path]::GetFullPath($self) } catch { }
        $pinnedHelper = $null
        try { $pinnedHelper = [System.IO.Path]::GetFullPath([string]$Pin.helper_path) } catch { }
        if (-not $selfFull -or -not $pinnedHelper -or ($selfFull -ine $pinnedHelper)) {
            throw "this helper is not the pinned one: $self"
        }
        $selfDigest = Get-FileSha256 $self
        if (-not $selfDigest) { throw 'the running helper could not be hashed' }
        if ($selfDigest -ne $Pin.helper_sha256) {
            throw 'the running helper does not match its recorded fingerprint'
        }
    }
    # The frozen worker is a PyInstaller --onedir BUNDLE inside the protected
    # runtime root. Binding only sa_fido_worker.exe would leave every DLL and
    # every Python extension module this elevated process loads unbound, so
    # the WHOLE tree is re-measured here and compared with the recursive
    # fingerprint the installer pinned.
    $rootFull = $null
    try { $rootFull = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') } catch { }
    if (-not $rootFull) { throw 'the protected runtime root is unreadable' }
    $expectedBundle = [System.IO.Path]::GetFullPath((Join-Path $rootFull $FIDO_BUNDLE_DIR_NAME))
    $pinnedBundle = $null
    try { $pinnedBundle = [System.IO.Path]::GetFullPath([string]$Pin.fido_worker_bundle_dir) } catch { }
    if (-not $pinnedBundle -or ($pinnedBundle -ine $expectedBundle)) {
        throw 'the pinned FIDO worker bundle directory is unexpected'
    }
    $worker = [string]$Pin.fido_worker_exe_path
    if (-not $worker) { throw 'the pin record names no frozen FIDO worker' }
    $workerFull = $null
    try { $workerFull = [System.IO.Path]::GetFullPath($worker) } catch { }
    if (-not $workerFull -or
        -not $workerFull.ToLowerInvariant().StartsWith(($expectedBundle + '\').ToLowerInvariant())) {
        throw 'the pinned FIDO worker is not inside the protected worker bundle'
    }
    $expectedWorker = [System.IO.Path]::GetFullPath((Join-Path $expectedBundle $FIDO_WORKER_EXE_NAME))
    if ($workerFull -ine $expectedWorker) { throw 'the pinned FIDO worker path is unexpected' }
    $workerDigest = Get-FileSha256 $worker
    if (-not $workerDigest) { throw 'the pinned frozen FIDO worker is missing' }
    if ($workerDigest -ne $Pin.fido_worker_exe_sha256) {
        throw 'the pinned frozen FIDO worker does not match its recorded fingerprint'
    }
    $bundleDigest = Get-BundleFingerprint $expectedBundle
    if (-not $Pin.fido_worker_bundle_fingerprint) {
        throw 'the pin record carries no FIDO worker bundle fingerprint'
    }
    if ($bundleDigest -ne $Pin.fido_worker_bundle_fingerprint) {
        throw 'the FIDO worker bundle does not match its recorded recursive fingerprint'
    }
    # The runtime bundle manifest must be present, self-consistent and pinned.
    $manifestPath = Get-RuntimeManifestPath $RuntimeRoot
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'the protected runtime manifest is missing'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne $RUNTIME_MANIFEST_SCHEMA) {
        throw 'the runtime manifest uses an unexpected schema'
    }
    if ($manifest.bundle_fingerprint -ne $Pin.runtime_bundle_fingerprint) {
        throw 'the runtime bundle fingerprint does not match the pin'
    }
    if ($manifest.helper_sha256 -ne $Pin.helper_sha256 -or
        $manifest.fido_worker_sha256 -ne $Pin.fido_worker_exe_sha256) {
        throw 'the runtime manifest disagrees with the pin about a fingerprint'
    }
    if ($manifest.fido_worker_bundle_fingerprint -ne $Pin.fido_worker_bundle_fingerprint) {
        throw 'the runtime manifest disagrees with the pin about the FIDO worker bundle'
    }
    return $true
}

# This user, Administrators, SYSTEM. Nobody else, and no inherited default:
# the pipe carries the volume unlock secret.
function New-HelperPipeSecurity([string]$Sid) {
    $security = New-Object System.IO.Pipes.PipeSecurity
    $owner = New-Object System.Security.Principal.SecurityIdentifier($Sid)
    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    foreach ($sid in @($owner, $admins, $system)) {
        $security.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule(
            $sid, [System.IO.Pipes.PipeAccessRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow)))
    }
    $security.SetOwner($owner)
    return $security
}

# A pipe that already exists was created by somebody else, and creating a
# second instance of it would mean serving on their terms. Refuse instead.
function Test-PipeNameTaken([string]$PipeName) {
    $leaf = $PipeName -replace '^\\\\\.\\pipe\\', ''
    try {
        foreach ($existing in [System.IO.Directory]::GetFiles('\\.\pipe\')) {
            if ((Split-Path -Leaf $existing) -ieq $leaf) { return $true }
        }
    } catch { return $false }
    return $false
}

# Who is on the other end: asked of Windows by impersonating the caller for
# IDENTIFICATION only. The broker must be this same account at medium
# integrity -- an elevated client is refused, because an elevated broker is
# precisely the architecture Secure Apps exists to avoid.
function Test-ClientAcceptable($Server, [string]$ExpectedSid) {
    $script:ClientSid = $null
    $script:ClientIntegrity = $null
    if (-not (Initialize-IntegrityProbe)) {
        return @{ ok = $false; reason = 'integrity_probe_unavailable' }
    }
    try {
        $Server.RunAsClient([System.IO.Pipes.PipeStreamImpersonationWorker] {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $script:ClientSid = $identity.User.Value
            $script:ClientIntegrity = [SaitulsTokenIntegrity]::Level($identity.Token)
        })
    } catch {
        return @{ ok = $false; reason = 'client_identity_unavailable' }
    }
    if (-not $script:ClientSid) { return @{ ok = $false; reason = 'client_identity_unavailable' } }
    if ($script:ClientSid -ne $ExpectedSid) { return @{ ok = $false; reason = 'client_wrong_account' } }
    if ($null -eq $script:ClientIntegrity -or [int]$script:ClientIntegrity -lt 0) {
        return @{ ok = $false; reason = 'client_integrity_unknown' }
    }
    if ($script:ClientIntegrity -ge $INTEGRITY_HIGH_RID) {
        return @{ ok = $false; reason = 'client_elevated' }
    }
    return @{ ok = $true; reason = 'ok'; sid = $script:ClientSid; integrity = $script:ClientIntegrity }
}

function New-Greeting($Pin) {
    return @{
        schema                     = $GREETING_SCHEMA
        pid                        = $PID
        elevated                   = (Test-Elevated)
        user_sid                   = (Get-CurrentUserSid)
        launch_mode                = $Pin.launch_mode
        task_path                  = $Pin.task_path
        definition_fingerprint     = $Pin.definition_fingerprint
        runtime_bundle_fingerprint = $Pin.runtime_bundle_fingerprint
        idle_timeout_seconds       = $Pin.idle_timeout_seconds
        helper_sha256              = $Pin.helper_sha256
    }
}

# One client at a time, one response per request, and an idle clock that
# starts the moment nobody is connected.
function Invoke-ScheduledHelper {
    if (-not (Test-Elevated)) {
        Write-Error 'the scheduled privileged helper must run elevated'
        exit 4
    }
    # BEFORE any Storage or BitLocker command can auto-load from a
    # CurrentUser-controlled module directory. Fail closed: an elevated helper
    # that cannot prove where its storage cmdlets came from does not serve.
    $modules = Initialize-TrustedModuleEnvironment
    if (-not ($modules.trusted_module_path -and $modules.storage_module_trusted -and
              $modules.bitlocker_module_trusted)) {
        Write-Error 'the trusted Storage/BitLocker module environment could not be established'
        exit 6
    }
    # The protected scratch root must exist and deny medium writes before any
    # operation that needs privileged command material can be accepted.
    $scratchRoot = Get-PrivilegedTmpRoot
    if (-not (Test-Path -LiteralPath $scratchRoot -PathType Container) -or
        (Test-MediumWritable $scratchRoot)) {
        Write-Error 'the privileged scratch root is missing or not protected'
        exit 7
    }
    $pin = Read-PinRecord (Get-DefaultPinPath)
    # The installed helper runs from the protected runtime directory, so its
    # own location IS the runtime root. Bind everything the worker launch needs
    # from there, and self-check before serving anything.
    $script:RuntimeRoot = Split-Path -Parent $PSCommandPath
    [void](Assert-PinnedRuntime $pin $script:RuntimeRoot)
    $script:FidoWorkerExe = [string]$pin.fido_worker_exe_path
    $idleSeconds = [int]$pin.idle_timeout_seconds
    if ($idleSeconds -lt 30) { $idleSeconds = 30 }
    if ($idleSeconds -gt 3600) { $idleSeconds = 3600 }
    $sid = Get-CurrentUserSid
    $pipeName = [string]$pin.pipe_name
    if (Test-PipeNameTaken $pipeName) {
        Write-Error 'another process already owns the privileged helper pipe'
        exit 5
    }
    $leaf = $pipeName -replace '^\\\\\.\\pipe\\', ''
    $server = New-Object System.IO.Pipes.NamedPipeServerStream(
        $leaf, [System.IO.Pipes.PipeDirection]::InOut, 1,
        [System.IO.Pipes.PipeTransmissionMode]::Byte,
        [System.IO.Pipes.PipeOptions]::Asynchronous,
        65536, 65536, (New-HelperPipeSecurity $sid))
    try {
        while ($true) {
            $waiting = $server.WaitForConnectionAsync()
            if (-not $waiting.Wait([int]($idleSeconds * 1000))) {
                # Nobody came back. Exit rather than sit here elevated; the
                # next unlock starts this task again, silently.
                return 0
            }
            $verdict = Test-ClientAcceptable $server $sid
            if (-not $verdict.ok) {
                try { $server.Disconnect() } catch { }
                continue
            }
            $stop = Serve-Client $server $pin
            try { $server.Disconnect() } catch { }
            if ($stop) { return 0 }
        }
    } finally {
        try { $server.Dispose() } catch { }
    }
}

# Returns $true when the broker asked this helper to exit now.
function Serve-Client($Server, $Pin) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $reader = New-Object System.IO.StreamReader($Server, $encoding, $false, 4096, $true)
    $writer = New-Object System.IO.StreamWriter($Server, $encoding, 4096, $true)
    $writer.AutoFlush = $true
    $writer.NewLine = "`n"
    try {
        $writer.WriteLine(((New-Greeting $Pin) | ConvertTo-Json -Compress))
    } catch {
        return $false
    }
    while ($true) {
        $line = $null
        try { $line = $reader.ReadLine() } catch { return $false }
        if ($null -eq $line) { return $false }        # broker disconnected
        if ($line.Trim().Length -eq 0) { continue }
        $id = 0
        try {
            $request = $line | ConvertFrom-Json
            $id = [int]$request.id
            $op = [string]$request.op
            if ($op -eq 'shutdown') { return $true }
            if ($op -eq 'helper_info') {
                $result = New-Greeting $Pin
            } else {
                $result = Invoke-Operation $op $request.args
            }
            $writer.WriteLine((@{ id = $id; ok = $true; result = $result } | ConvertTo-Json -Compress -Depth 6))
        } catch {
            $fallback = switch -Regex ($line) {
                '"op"\s*:\s*"create"'       { $ERR_CREATE; break }
                '"op"\s*:\s*"unlock_mount"' { $ERR_MOUNT; break }
                '"op"\s*:\s*"unmount"'      { $ERR_UNMOUNT; break }
                '"op"\s*:\s*"fido_create"'  { 'auth_failed'; break }
                '"op"\s*:\s*"fido_hmac"'    { 'auth_failed'; break }
                default                     { $ERR_INTERNAL }
            }
            $category = Get-ErrorCategory $fallback $_.Exception
            try {
                $writer.WriteLine((@{ id = $id; ok = $false; error = $category
                    message = "storage helper operation failed ($category)" } | ConvertTo-Json -Compress))
            } catch { return $false }
        }
    }
}

# ------------------------------------------------------------------ selftest
if ($SelfTest) {
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    # The same trusted-module construction production uses, reported rather
    # than enforced: -SelfTest is a diagnostic and must be able to say WHY a
    # host would be refused, not merely refuse.
    $modules = Initialize-TrustedModuleEnvironment
    $required = @('Get-DiskImage', 'Mount-DiskImage', 'Dismount-DiskImage', 'Get-Disk',
        'Get-Partition', 'Initialize-Disk', 'New-Partition', 'Format-Volume',
        'Add-PartitionAccessPath', 'Remove-PartitionAccessPath', 'Enable-BitLocker',
        'Unlock-BitLocker', 'Lock-BitLocker', 'Get-BitLockerVolume',
        'Add-BitLockerKeyProtector')
    $missing = @($required | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) })
    $diskpartPath = ''
    try { $diskpartPath = Get-TrustedSystemExecutable 'diskpart.exe' } catch { }
    $scratchRoot = Get-PrivilegedTmpRoot
    $scratchPresent = Test-Path -LiteralPath $scratchRoot -PathType Container
    $scratchProtected = $false
    if ($scratchPresent) { $scratchProtected = -not (Test-MediumWritable $scratchRoot) }
    # -PinPath is honoured here and nowhere else: the production mode reads a
    # constant path, because an elevated process that lets a caller choose its
    # pin file lets the caller choose what it executes.
    $pinFile = if ($PinPath) { $PinPath } else { Get-DefaultPinPath }
    $pinPresent = Test-Path -LiteralPath $pinFile -PathType Leaf
    $pinValid = $false
    $pinProblem = ''
    $pipeExpected = ''
    try { $pipeExpected = Get-HelperPipeName (Get-CurrentUserSid) } catch { }
    if ($pinPresent) {
        try {
            $probe = Read-PinRecord $pinFile
            # In -SelfTest the running script may be the repository copy, not
            # the installed one, so verify the pinned runtime against its own
            # recorded runtime_root rather than against $PSScriptRoot.
            $probeRoot = [string]$probe.runtime_root
            if (-not $probeRoot) { $probeRoot = Split-Path -Parent ([string]$probe.helper_path) }
            [void](Assert-PinnedRuntime $probe $probeRoot $false)
            $pinValid = $true
        } catch {
            $pinProblem = "$($_.Exception.Message)"
        }
    } else {
        $pinProblem = 'the privileged helper is not installed'
    }
    [pscustomobject]@{
        ok            = ($missing.Count -eq 0 -and $modules.trusted_module_path -and
                         $modules.storage_module_trusted -and $modules.bitlocker_module_trusted -and
                         [bool]$diskpartPath)
        elevated      = (Test-Elevated)
        diskpart      = [bool]$diskpartPath
        diskpart_path = $diskpartPath
        trusted_module_path      = [bool]$modules.trusted_module_path
        storage_module_trusted   = [bool]$modules.storage_module_trusted
        bitlocker_module_trusted = [bool]$modules.bitlocker_module_trusted
        module_path              = [string]$modules.module_path
        privileged_tmp_root      = $scratchRoot
        privileged_tmp_present   = [bool]$scratchPresent
        privileged_tmp_protected = [bool]$scratchProtected
        missing       = $missing
        operations    = @('ping', 'state', 'helper_info', 'create', 'unlock_mount', 'unmount', 'shutdown',
                          'fido_capabilities', 'fido_create', 'fido_hmac')
        error_categories = @($ERR_BUSY, $ERR_CREATE, $ERR_UNLOCK, $ERR_MOUNT, $ERR_UNMOUNT, $ERR_INTERNAL,
                             'auth_capability', 'auth_cancelled', 'auth_wrong_credential', 'auth_unavailable', 'auth_failed')
        launch_mode   = 'scheduled-task-v1'
        integrity_probe = (Initialize-IntegrityProbe)
        pin_path      = $pinFile
        pin_present   = $pinPresent
        pin_valid     = $pinValid
        pin_problem   = $pinProblem
        pipe_name     = $pipeExpected
        runtime_root  = if ($pinPresent -and $probe) { [string]$probe.runtime_root } else { '' }
        frozen_worker = $FIDO_WORKER_EXE_NAME
        fido_worker_bundle_dir = if ($pinPresent -and $probe) { [string]$probe.fido_worker_bundle_dir } else { '' }
        fido_worker_bundle_fingerprint = if ($pinPresent -and $probe) { [string]$probe.fido_worker_bundle_fingerprint } else { '' }
    } | ConvertTo-Json -Compress
    exit 0
}

# ══════════════════════════════════════════════════════════════════ entry
if ($ScheduledHelper) {
    exit (Invoke-ScheduledHelper)
}

Write-Error 'sa_storage_helper.ps1 runs as -ScheduledHelper (started by the registered SAITULS task) or -SelfTest.'
exit 2
