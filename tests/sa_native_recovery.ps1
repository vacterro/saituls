# encoding: utf-8
<#
    Elevated native BitLocker recovery worker for LIVE D / LIVE E.

    Deliberately independent of SAITULS. Nothing here imports Scripts/secure_apps,
    sa_storage, sa_broker, sa_crypto or the privileged storage helper: the only
    things it touches are Windows' own storage and BitLocker cmdlets. That is the
    whole point of the gate -- a pass proves Windows alone, plus a copied
    .svault and the human secret, can recover the data when SAITULS is gone.

    Started once per live run by tests/sa_live_elevation.py through the UAC
    "runas" verb, so the acceptance runner itself stays at medium integrity and
    the elevated surface is exactly this one disposable recovery worker.

    Secrets never ride on the command line: the pipe name, the vault path and
    the canary digest are not secret, and the two door secrets arrive only over
    a per-run named pipe whose DACL admits this Windows user alone.

    Usage:
        powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
            -File sa_native_recovery.ps1 -PipeName <name> -VaultPath <copy.svault> `
            -ExpectedCanarySha256 <hex> [-EvidencePath <sanitized.json>]
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PipeName,
    [Parameter(Mandatory = $true)][string]$VaultPath,
    [Parameter(Mandatory = $true)][string]$ExpectedCanarySha256,
    [string]$EvidencePath = "",
    [int]$IdleTimeoutSeconds = 900
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ─────────────────────────────────────────────────────────── elevation proof
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
$elevated = $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) {
    [Console]::Error.WriteLine("NOT_ELEVATED: native recovery worker refuses to run at medium integrity")
    exit 87
}

# ─────────────────────────────────────────────────────────── framing helpers
# 4-byte big-endian length + UTF-8 JSON. Line framing would be ambiguous for
# secrets containing newlines, and a JSON string body is already escaped.
function Read-Frame {
    param([System.IO.Stream]$Stream)
    $header = New-Object byte[] 4
    $read = 0
    while ($read -lt 4) {
        $n = $Stream.Read($header, $read, 4 - $read)
        if ($n -le 0) { return $null }
        $read += $n
    }
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($header) }
    $length = [BitConverter]::ToInt32($header, 0)
    if ($length -le 0 -or $length -gt 1048576) { throw "invalid frame length $length" }
    $body = New-Object byte[] $length
    $read = 0
    while ($read -lt $length) {
        $n = $Stream.Read($body, $read, $length - $read)
        if ($n -le 0) { return $null }
        $read += $n
    }
    return [Text.Encoding]::UTF8.GetString($body)
}

function Write-Frame {
    param([System.IO.Stream]$Stream, [string]$Text)
    $body = [Text.Encoding]::UTF8.GetBytes($Text)
    $header = [BitConverter]::GetBytes([int]$body.Length)
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($header) }
    $Stream.Write($header, 0, 4)
    $Stream.Write($body, 0, $body.Length)
    $Stream.Flush()
}

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($Bytes)
        return (-join ($hash | ForEach-Object { "{0:x2}" -f $_ }))
    } finally {
        $sha.Dispose()
    }
}

<#
    The volume as Windows sees it, in non-secret terms. The protector list is
    what explains a wrong-key rejection: a volume carrying no Password
    protector, or a lock state that was never Locked, fails Door 2 for a reason
    that has nothing to do with the secret. No secret and no key is echoed.
#>
function Get-VolumeFacts {
    param([string]$MountPoint)
    try {
        $bl = Get-BitLockerVolume -MountPoint $MountPoint -ErrorAction Stop
        $types = @($bl.KeyProtector | ForEach-Object { "$($_.KeyProtectorType)" } | Sort-Object -Unique)
        return ("lock_status=$($bl.LockStatus); volume_status=$($bl.VolumeStatus); " +
                "protectors=[$($types -join ',')]; protector_count=$(@($bl.KeyProtector).Count)")
    } catch {
        return "volume inquiry failed: $($_.Exception.Message)"
    }
}

# ─────────────────────────────────────────────────────────── the native oracle
function Get-CanarySha256 {
    param([string]$MountPoint)
    $canaryFile = Join-Path $MountPoint "canary.txt"
    if (-not (Test-Path -LiteralPath $canaryFile)) {
        throw "canary file not found on the unlocked volume: $canaryFile"
    }
    $bytes = [System.IO.File]::ReadAllBytes($canaryFile)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $hash = $sha.ComputeHash($bytes) } finally { $sha.Dispose() }
        return (-join ($hash | ForEach-Object { "{0:x2}" -f $_ }))
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

<#
    One complete native recovery cycle: attach the copy, unlock with the given
    method, prove the canary, then lock and detach -- and prove the detach. No
    volume stays mounted between the two doors.

    Never throws: a failure is a value, so the parent receives the same volume
    facts for a rejection as for a pass, and can tell a wrong key apart from a
    door that never really ran.
#>
function Invoke-NativeRecovery {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("Password", "RecoveryPassword")][string]$Method,
        [Parameter(Mandatory = $true)][string]$Secret
    )

    $result = @{
        status                 = "FAIL"
        method                 = $Method
        mount                  = ""
        canary_sha256          = ""
        detach_confirmed       = $false
        pre_unlock_lock_status = ""
        pre_unlock_protectors  = ""
        detail                 = ""
    }

    $fullPath = [System.IO.Path]::GetFullPath($VaultPath)
    if (-not (Test-Path -LiteralPath $fullPath)) {
        $result.detail = "vault file not found: $fullPath"
        return $result
    }

    # A .svault is the container image under another name; Mount-DiskImage only
    # takes accepted VHD/VHDX extensions, so an extension-less copy is staged.
    $attachPath = $fullPath
    $tempCopy = $null
    if ($fullPath -notmatch '\.(vhdx|vhd)$') {
        $tempDir = Join-Path $env:TEMP ("svault_native_live_" + [System.Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
        $tempCopy = Join-Path $tempDir "vault.vhdx"
        Copy-Item -LiteralPath $fullPath -Destination $tempCopy -Force
        $attachPath = $tempCopy
    }

    $attached = $false
    try {
        $img = Mount-DiskImage -ImagePath $attachPath -NoDriveLetter -PassThru -ErrorAction Stop
        $attached = $true

        $disk = Get-Disk -Number $img.Number -ErrorAction Stop
        $part = Get-Partition -DiskNumber $disk.Number -ErrorAction Stop |
            Where-Object { $_.Type -ne 'Reserved' -and $_.Size -gt 16MB } |
            Select-Object -First 1
        if (-not $part) { throw "no data partition found in $attachPath" }

        # Unlock and file reads are both driven from the volume GUID path -- the
        # same handle the production storage helper resolves. Nothing here
        # assigns a drive letter: a letter is nothing but another access path,
        # and adding one is what asks Windows to mount the filesystem of a
        # still-locked volume. A native recovery needs no such thing.
        $volumePath = @($part.AccessPaths | Where-Object { $_ -like '\\?\Volume{*' }) | Select-Object -First 1
        if (-not $volumePath) { throw "the attached volume exposed no volume GUID path" }
        $result.mount = $volumePath

        # What the volume looked like before the door ran. A door that "passes"
        # on a volume Windows had already unlocked would prove nothing, so the
        # pre-unlock state is recorded rather than assumed -- and a volume that
        # was never Locked is a refusal, not a pass.
        $preBl = Get-BitLockerVolume -MountPoint $volumePath -ErrorAction Stop
        $result.pre_unlock_lock_status = "$($preBl.LockStatus)"
        $result.pre_unlock_protectors = (@($preBl.KeyProtector |
            ForEach-Object { "$($_.KeyProtectorType)" } | Sort-Object -Unique) -join ",")
        if ($result.pre_unlock_lock_status -ne 'Locked') {
            throw "the volume was not Locked when the door ran: lock_status=$($result.pre_unlock_lock_status)"
        }

        if ($Method -eq "Password") {
            $secure = ConvertTo-SecureString -String $Secret -AsPlainText -Force
            try {
                Unlock-BitLocker -MountPoint $volumePath -Password $secure -ErrorAction Stop | Out-Null
            } finally {
                $secure.Dispose()
            }
        } else {
            Unlock-BitLocker -MountPoint $volumePath -RecoveryPassword $Secret -ErrorAction Stop | Out-Null
        }

        $bl = Get-BitLockerVolume -MountPoint $volumePath -ErrorAction Stop
        if ("$($bl.LockStatus)" -ne "Unlocked") {
            throw "BitLocker LockStatus is not Unlocked: $($bl.LockStatus)"
        }

        $result.canary_sha256 = Get-CanarySha256 -MountPoint $volumePath
        if ($ExpectedCanarySha256 -and ($result.canary_sha256.ToLower() -ne $ExpectedCanarySha256.ToLower())) {
            throw "canary hash mismatch: $($result.canary_sha256)"
        }

        Lock-BitLocker -MountPoint $volumePath -ForceDismount -ErrorAction SilentlyContinue | Out-Null
        Dismount-DiskImage -ImagePath $attachPath -ErrorAction Stop | Out-Null
        $attached = $false

        $di = Get-DiskImage -ImagePath $attachPath -ErrorAction Stop
        if ($di.Attached) { throw "disk image is still Attached after Dismount-DiskImage" }

        $result.detach_confirmed = $true
        $result.status = "PASS"
    } catch {
        $result.detail = ($_.Exception.GetType().Name + ": " + $_.Exception.Message)
        if ($result.mount) {
            $result.detail += ("; " + (Get-VolumeFacts -MountPoint $result.mount))
        }
        try {
            if ($attached) { Dismount-DiskImage -ImagePath $attachPath -ErrorAction SilentlyContinue | Out-Null }
        } catch { }
        try {
            $di = Get-DiskImage -ImagePath $attachPath -ErrorAction SilentlyContinue
            $result.detach_confirmed = -not [bool]$di.Attached
        } catch { }
    } finally {
        if ($tempCopy) {
            $staging = Split-Path $tempCopy
            if (Test-Path -LiteralPath $staging) {
                Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue | Out-Null
            }
        }
    }
    return $result
}

<#
    One door. A single attempt is the rule; the Password door may try again from
    a clean detach, because Windows can refuse a password unlock while it is
    still settling a volume that was attached a moment ago. Every attempt is
    recorded, so a pass on attempt 2 or 3 is visible in the evidence instead of
    being silently normalised into a first-try success.
#>
function Invoke-Door {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Secret
    )

    $maxAttempts = 1
    if ($Method -eq "Password") { $maxAttempts = 3 }

    $attempts = @()
    $result = $null
    for ($i = 1; $i -le $maxAttempts; $i++) {
        $result = Invoke-NativeRecovery -Method $Method -Secret $Secret
        $attempts += @{
            n                      = $i
            status                 = $result.status
            pre_unlock_lock_status = $result.pre_unlock_lock_status
            detach_confirmed       = [bool]$result.detach_confirmed
            detail                 = $result.detail
        }
        if ($result.status -eq "PASS") { break }
        if ($i -lt $maxAttempts) { Start-Sleep -Seconds 2 }
    }
    return @{ result = $result; attempts = $attempts }
}

# ─────────────────────────────────────────────────────────── pipe + protocol
# The elevated worker owns the pipe server. A DACL carrying one Allow ACE for
# this Windows user is the whole access policy: no Administrators, no SYSTEM,
# no Everyone, no anonymous access, so only this user can connect. The name is
# not a secret, which is exactly why it may be the only thing on the command
# line; both door secrets arrive over this pipe and nowhere else.
$pipeSecurity = New-Object System.IO.Pipes.PipeSecurity
$pipeSecurity.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule(
    $identity.User,
    [System.IO.Pipes.PipeAccessRights]::ReadWrite,
    [System.Security.AccessControl.AccessControlType]::Allow)))
$pipeSecurity.SetAccessRuleProtection($true, $false)

try {
    $server = [System.IO.Pipes.NamedPipeServerStream]::new(
        $PipeName,
        [System.IO.Pipes.PipeDirection]::InOut,
        1,
        [System.IO.Pipes.PipeTransmissionMode]::Byte,
        [System.IO.Pipes.PipeOptions]::None,
        4096,
        4096,
        $pipeSecurity)
} catch {
    [Console]::Error.WriteLine("PIPE_CREATE_FAILED: " + $_.Exception.Message)
    exit 88
}

$client = $server
try {
    # Async form so the wait can be bounded: a blocking WaitForConnection()
    # cannot be interrupted, and this worker must never outlive the run.
    if (-not $server.WaitForConnectionAsync().Wait($IdleTimeoutSeconds * 1000)) {
        [Console]::Error.WriteLine("PIPE_CONNECT_TIMEOUT: no medium-integrity parent connected")
        $server.Dispose()
        exit 88
    }
} catch {
    [Console]::Error.WriteLine("PIPE_CONNECT_FAILED: " + $_.Exception.Message)
    try { $server.Dispose() } catch { }
    exit 88
}

# The DACL is the access control; this is the same claim stated twice. It fails
# closed when the client is identified and differs from us, and reports
# "unknown" rather than pretending when it cannot be identified at all.
#
# GetImpersonationUserName() is unreliable on a pipe this worker created with
# PipeOptions.None: it throws Win32Exception on some hosts and then a working
# boundary would be recorded as unverified. The fallback impersonates the
# connected client and reads the identity the token actually carries, which is
# the same question asked a way the token has to answer.
$clientUser = "unknown"
try {
    $clientUser = $server.GetImpersonationUserName()
} catch {
    $clientUser = "unknown"
}
if ($clientUser -eq "unknown") {
    try {
        $clientUser = $server.RunAsClient({ [System.Security.Principal.WindowsIdentity]::GetCurrent().Name })
    } catch {
        $clientUser = "unknown"
    }
}
if ($clientUser -ne "unknown" -and $clientUser -ne $identity.Name) {
    [Console]::Error.WriteLine("PIPE_CLIENT_REJECTED: connected by $clientUser, expected $($identity.Name)")
    try { $server.Dispose() } catch { }
    exit 89
}

$evidence = @{}
$exitCode = 0
try {
    Write-Frame -Stream $client -Text ([pscustomobject]@{
        type        = "hello"
        pid         = $PID
        elevated    = $elevated
        user        = $identity.Name
        user_sid    = $identity.User.Value
        client_user = $clientUser
        protocol    = 1
    } | ConvertTo-Json -Compress)

    while ($true) {
        $raw = Read-Frame -Stream $client
        if (-not $raw) { break }
        $request = $raw | ConvertFrom-Json
        if ($request.type -eq "close") { break }
        if ($request.type -ne "door") { continue }

        $method = [string]$request.door
        $secretBytes = [Convert]::FromBase64String([string]$request.secret_b64)
        # The digest of the bytes actually handed to the unlocker, so the parent
        # can prove the child used the intended secret -- and can tell a
        # transport fault apart from a genuine wrong-key rejection. A digest,
        # never the secret.
        $secretDigest = Get-Sha256Hex -Bytes $secretBytes
        $secret = $null
        try {
            $secret = [Text.Encoding]::UTF8.GetString($secretBytes)
            $outcome = Invoke-Door -Method $method -Secret $secret
            $result = $outcome.result
            $evidence[$method] = @{
                status                 = $result.status
                canary_sha256          = $result.canary_sha256
                detach_confirmed       = [bool]$result.detach_confirmed
                mount                  = $result.mount
                pre_unlock_lock_status = $result.pre_unlock_lock_status
                pre_unlock_protectors  = $result.pre_unlock_protectors
                attempts               = $outcome.attempts
            }
            $reply = [pscustomobject]@{
                type                   = "result"
                door                   = $method
                status                 = $result.status
                canary_sha256          = $result.canary_sha256
                detach_confirmed       = [bool]$result.detach_confirmed
                mount                  = $result.mount
                pre_unlock_lock_status = $result.pre_unlock_lock_status
                pre_unlock_protectors  = $result.pre_unlock_protectors
                attempts               = $outcome.attempts
                secret_sha256          = $secretDigest
                secret_length          = $secretBytes.Length
                # Category + message only: never an echo of the secret.
                detail                 = $result.detail
            }
            if ($result.status -ne "PASS") { $exitCode = 1 }
        } catch {
            $evidence[$method] = @{ status = "FAIL"; secret_sha256 = $secretDigest }
            $reply = [pscustomobject]@{
                type             = "result"
                door             = $method
                status           = "FAIL"
                canary_sha256    = ""
                detach_confirmed = $false
                secret_sha256    = $secretDigest
                secret_length    = $secretBytes.Length
                detail           = ($_.Exception.GetType().Name + ": " + $_.Exception.Message)
            }
            $exitCode = 1
        } finally {
            # Best effort, and honest about the ceiling: a .NET string is
            # immutable, so the decoded copy this worker handed the cmdlet dies
            # with the generation, not with this statement. The byte buffer is
            # cleared deterministically.
            if ($secretBytes) { [Array]::Clear($secretBytes, 0, $secretBytes.Length) }
            $secret = $null
            $request.secret_b64 = $null
            $raw = $null
            [GC]::Collect()
        }
        Write-Frame -Stream $client -Text ($reply | ConvertTo-Json -Compress -Depth 6)
    }
} finally {
    try { $client.Dispose() } catch { }
    if ($EvidencePath) {
        try {
            $record = [pscustomobject]@{
                kind                 = "native_recovery_worker"
                pid                  = $PID
                elevated             = $elevated
                user                 = $identity.Name
                client_user          = $clientUser
                vault_path           = $VaultPath
                expected_canary_sha256 = $ExpectedCanarySha256
                doors                = $evidence
            }
            $dir = Split-Path -Parent $EvidencePath
            if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            ($record | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
        } catch { }
    }
}
exit $exitCode
