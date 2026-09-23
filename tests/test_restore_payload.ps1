$ErrorActionPreference = 'Stop'
$root = Join-Path $env:TEMP ('saituls-restore-root-' + [Guid]::NewGuid().ToString('N'))
$backup = Join-Path $env:TEMP ('saituls-restore-backup-' + [Guid]::NewGuid().ToString('N'))
$script = Join-Path $PSScriptRoot '..\Scripts\restore_payload.ps1'
$failures = 0

function Check([string]$Name, [bool]$Ok) {
    if ($Ok) { Write-Host "PASS  $Name" } else { Write-Host "FAIL  $Name"; $script:failures++ }
}

try {
    New-Item -ItemType Directory -Path (Join-Path $root 'Bin\App') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $backup 'Bin\App') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $root 'PAYLOAD_MANIFEST.txt') -Value @('# fixture', 'Bin/tool.exe', 'Bin/App/data.dll') -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $backup 'SAITULS.exe') -Value 'app-backup' -NoNewline -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $backup 'Bin/tool.exe') -Value 'tool-backup' -NoNewline -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $backup 'Bin/App/data.dll') -Value 'data-backup' -NoNewline -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $root 'Bin/tool.exe') -Value 'corrupt' -NoNewline -Encoding Ascii

    & $script -Root $root -BackupRoot $backup
    Check 'missing app restored' ((Get-FileHash (Join-Path $root 'SAITULS.exe')).Hash -eq (Get-FileHash (Join-Path $backup 'SAITULS.exe')).Hash)
    Check 'corrupt payload repaired' ((Get-FileHash (Join-Path $root 'Bin/tool.exe')).Hash -eq (Get-FileHash (Join-Path $backup 'Bin/tool.exe')).Hash)
    Check 'nested payload restored' ((Get-FileHash (Join-Path $root 'Bin/App/data.dll')).Hash -eq (Get-FileHash (Join-Path $backup 'Bin/App/data.dll')).Hash)

    & $script -Root $root -BackupRoot $backup -VerifyOnly
    Check 'verify-only accepts exact payload' ($LASTEXITCODE -eq 0)

    Set-Content -LiteralPath (Join-Path $root 'PAYLOAD_MANIFEST.txt') -Value '..\escape.exe' -Encoding Ascii
    $message = ''
    try { & $script -Root $root -BackupRoot $backup; $ok = $false } catch { $ok = $true; $message = $_.Exception.Message }
    Check 'path traversal rejected' ($ok -and $message -match 'escapes root')
} finally {
    Remove-Item -LiteralPath $root, $backup -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '---'
if ($failures) { Write-Host "FAILED ($failures failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
