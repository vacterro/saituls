param([switch]$Python, [switch]$Node, [switch]$OpenCode)
$ErrorActionPreference = 'Stop'
$token = 'SAITULS_TOOLCHAIN_STDOUT_OK'
$probe = Join-Path ([IO.Path]::GetTempPath()) ('saituls-health-' + [Guid]::NewGuid().ToString('N'))
try {
    $out = & powershell -NoProfile -Command "[Console]::WriteLine('$token')" 2>&1
    if ($LASTEXITCODE -ne 0 -or ($out -join "`n").Trim() -cne $token) { throw 'shell stdout mismatch' }
    if (-not [IO.File]::ReadAllText($PSCommandPath).Contains($token)) { throw 'file read failed' }
    [IO.File]::WriteAllText($probe, $token)
    if ([IO.File]::ReadAllText($probe) -cne $token) { throw 'temporary write/read mismatch' }
    [IO.File]::Delete($probe)
    if ([IO.File]::Exists($probe)) { throw 'temporary delete failed' }
    $out = & git status --porcelain 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'git status failed' }
    if ($Python) {
        $out = & python -c "print('$token')" 2>&1
        if ($LASTEXITCODE -ne 0 -or ($out -join "`n").Trim() -cne $token) { throw 'Python stdout mismatch' }
    }
    if ($Node) {
        $out = & node -e "console.log('$token')" 2>&1
        if ($LASTEXITCODE -ne 0 -or ($out -join "`n").Trim() -cne $token) { throw 'Node stdout mismatch' }
    }
    if ($OpenCode) {
        $out = & opencode --version 2>&1
        if ($LASTEXITCODE -ne 0 -or ($out -join "`n").Trim() -notmatch '^\d+\.\d+\.\d+$') {
            throw 'OpenCode startup/stdout failed'
        }
    }
    Write-Output "$token PASS"
} catch {
    Write-Output "TOOLCHAIN_UNHEALTHY: $($_.Exception.Message)"
    exit 1
} finally {
    if ([IO.File]::Exists($probe)) { [IO.File]::Delete($probe) }
}
