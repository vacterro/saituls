# Reusable native-TUI compatibility probe for an OpenCode build.
#
# Read-only. It never mutates the inspected root and never silently inspects an
# arbitrary PATH installation: the root under test is an explicit argument.
#
# What it answers: does this build's visible TUI (submit, message rendering,
# busy status, interrupt, session lifecycle) run on ONE native execution domain,
# or is the TUI split between the legacy /session/* contract and the native
# /api/session/* scheduler? The verdict rests on source signatures extracted
# from the executable, never on the version string alone.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass `
#           -File Scripts\saipatch\tools\probe_native_tui.ps1 `
#           -OpenCodeRoot <install-root-or-exe> [-OutJson <file>]
# Exit:   0 = classified (verdict UNIFIED_NATIVE_TUI | SPLIT_LEGACY_NATIVE |
#               UNKNOWN_BUILD)
#         2 = UNSUPPORTED_LAYOUT (root/exe unusable or no known signature found)
#         1 = unexpected probe error
param(
    [Parameter(Mandatory = $true)][string]$OpenCodeRoot,
    [string]$OutJson
)
$ErrorActionPreference = 'Stop'

# Signature table, calibrated byte-for-byte against OpenCode 1.18.29 Windows
# x64 (SHA256 88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5).
# Call-site shapes carry the ({sessionID: argument literal; SDK route
# annotations such as identifier:"v2.session.prompt" do not match them.
$signatures = [ordered]@{
    legacy_submit_call    = 'client.session.prompt({sessionID:'
    legacy_status_call    = 'client.session.status({'
    legacy_messages_call  = 'client.session.messages({'
    legacy_abort_call     = 'client.session.abort({sessionID:'
    v2_submit_call        = 'v2.session.prompt({sessionID:'
    v2_messages_call      = 'v2.session.messages({'
    v2_status_call        = 'v2.session.status({'
    v2_active_call        = 'v2.session.active('
    v2_abort_call         = 'v2.session.interrupt('
    legacy_message_path   = '/session/{sessionID}/message'
    legacy_status_path    = '/session/status'
    legacy_abort_path     = '/session/{sessionID}/abort'
    v2_prompt_path        = '/api/session/{sessionID}/prompt'
    v2_active_path        = '/api/session/active'
    v2_messages_path      = '/api/session/{sessionID}/message'
    v2_abort_path         = '/api/session/{sessionID}/interrupt'
    native_scheduler      = 'promoteNextQueued'
    native_runner         = 'SessionRunner.run'
    native_steer_engine   = 'promoteSteers'
    tui_plugin_seam       = 'tui.json'
}

function Resolve-Exe([string]$root) {
    if (Test-Path -LiteralPath $root -PathType Leaf) { return $root }
    foreach ($rel in @('bin\opencode.exe', 'opencode.exe')) {
        $p = Join-Path $root $rel
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
    return $null
}

function Get-BuildVersion([string]$exe) {
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        $psi.Arguments = '--version'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $out = $proc.StandardOutput.ReadToEnd()
        if (-not $proc.WaitForExit(15000)) { $proc.Kill(); return 'unknown' }
        $text = ($out.Trim() -split '\r?\n' | Select-Object -First 1)
        return $(if ($text) { $text } else { 'unknown' })
    } catch { return 'unknown' }
}

function Count-Signature([string]$text, [string]$needle) {
    $count = 0; $first = -1; $idx = 0
    while (($idx = $text.IndexOf($needle, $idx, [StringComparison]::Ordinal)) -ge 0) {
        if ($first -lt 0) { $first = $idx }
        $count++
        if ($count -ge 6) { break }
        $idx++
    }
    [pscustomobject]@{ count = $count; first = $first }
}

$report = [ordered]@{
    executable_path = $null
    version = 'unknown'
    sha256 = $null
    legacy_prompt_present = $false
    legacy_message_domain_present = $false
    legacy_status_domain_present = $false
    legacy_abort_domain_present = $false
    native_v2_prompt_present = $false
    native_v2_active_status_present = $false
    native_v2_messages_present = $false
    native_v2_abort_present = $false
    tui_submit_domain = 'none'
    tui_render_domain = 'none'
    tui_status_domain = 'none'
    tui_abort_domain = 'none'
    tui_plugin_seam_present = $false
    queue_delivery_supported = $false
    unified_native_tui_candidate = $false
    verdict = 'UNSUPPORTED_LAYOUT'
    evidence = [ordered]@{}
}

try {
    $exe = Resolve-Exe $OpenCodeRoot
    if (-not $exe) {
        Write-Output 'UNSUPPORTED_LAYOUT: no opencode executable under the given root'
        if ($OutJson) { $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutJson -Encoding UTF8 }
        exit 2
    }
    $report.executable_path = $exe
    $item = Get-Item -LiteralPath $exe
    $report.sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLower()
    if ($item.Length -le 0) {
        Write-Output 'UNSUPPORTED_LAYOUT: executable is empty'
        if ($OutJson) { $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutJson -Encoding UTF8 }
        exit 2
    }
    $report.version = Get-BuildVersion $exe

    # Bun single-file executables embed the bundled JS source; ISO-8859-1 maps
    # every byte to one char so offsets stay byte offsets.
    $text = [Text.Encoding]::GetEncoding(28591).GetString([IO.File]::ReadAllBytes($exe))
    $counts = [ordered]@{}
    foreach ($name in $signatures.Keys) {
        $counts[$name] = Count-Signature $text $signatures[$name]
        $report.evidence[$name] = "$($counts[$name].count)@$($counts[$name].first)"
    }

    $any = @($counts.Values | Where-Object { $_.count -gt 0 })
    if ($any.Count -eq 0) {
        Write-Output 'UNSUPPORTED_LAYOUT: no known OpenCode signature in this binary'
        if ($OutJson) { $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutJson -Encoding UTF8 }
        exit 2
    }

    $c = @{ }
    foreach ($name in $signatures.Keys) { $c[$name] = $counts[$name].count }

    $report.legacy_prompt_present = ($c.legacy_submit_call -gt 0)
    $report.legacy_message_domain_present = ($c.legacy_message_path -gt 0)
    $report.legacy_status_domain_present = ($c.legacy_status_path -gt 0)
    $report.legacy_abort_domain_present = ($c.legacy_abort_path -gt 0)
    $report.native_v2_prompt_present = ($c.v2_prompt_path -gt 0)
    $report.native_v2_active_status_present = ($c.v2_active_path -gt 0)
    $report.native_v2_messages_present = ($c.v2_messages_path -gt 0)
    $report.native_v2_abort_present = ($c.v2_abort_path -gt 0)
    $report.tui_plugin_seam_present = ($c.tui_plugin_seam -gt 0)
    $report.queue_delivery_supported = (
        $c.v2_prompt_path -gt 0 -and $c.native_scheduler -gt 0 -and
        $c.native_runner -gt 0 -and $c.native_steer_engine -gt 0)

    # One candidate boundary per domain, else fail closed to unknown.
    # Render is deliberately conservative: current builds also hydrate a
    # non-authoritative Data context through v2 session.messages (data.tsx
    # refresh), while the rendered session history is produced by the legacy
    # fetch sites (proven by the mixed-runner evidence, where native messages
    # stayed invisible to the legacy renderer). So ANY surviving legacy render
    # fetch makes the render domain legacy; it can only be native when every
    # legacy fetch site is gone. Submit/status/abort keep strict ambiguity:
    # two authority candidates in one domain mean the build is incoherent.
    function Get-Domain([int]$legacy, [int]$native) {
        if ($legacy -gt 0 -and $native -gt 0) { return 'ambiguous' }
        if ($legacy -gt 0) { return 'legacy' }
        if ($native -gt 0) { return 'native' }
        return 'none'
    }
    $report.tui_submit_domain = Get-Domain $c.legacy_submit_call $c.v2_submit_call
    $report.tui_render_domain = if ($c.legacy_messages_call -gt 0) { 'legacy' }
        elseif ($c.v2_messages_call -gt 0) { 'native' } else { 'none' }
    $report.tui_status_domain = Get-Domain $c.legacy_status_call ($c.v2_status_call + $c.v2_active_call)
    $report.tui_abort_domain = Get-Domain $c.legacy_abort_call $c.v2_abort_call

    $ambiguous = @($report.tui_submit_domain, $report.tui_render_domain,
        $report.tui_status_domain, $report.tui_abort_domain) -contains 'ambiguous'
    $unifiedExpected = @(
        $report.legacy_message_domain_present, $report.legacy_status_domain_present,
        $report.legacy_abort_domain_present, $report.native_v2_prompt_present,
        $report.native_v2_active_status_present, $report.native_v2_messages_present,
        $report.native_v2_abort_present) -notcontains $false
    $report.unified_native_tui_candidate = (
        -not $ambiguous -and $unifiedExpected -and
        $report.tui_submit_domain -eq 'native' -and $report.tui_render_domain -eq 'native' -and
        $report.tui_status_domain -eq 'native' -and $report.tui_abort_domain -eq 'native')

    if ($c.legacy_submit_call -gt 1 -or $ambiguous) {
        $report.verdict = 'UNKNOWN_BUILD'
    } elseif ($report.unified_native_tui_candidate) {
        $report.verdict = 'UNIFIED_NATIVE_TUI'
    } elseif ($report.legacy_prompt_present -and $unifiedExpected) {
        $report.verdict = 'SPLIT_LEGACY_NATIVE'
    } else {
        $report.verdict = 'UNKNOWN_BUILD'
    }

    $json = $report | ConvertTo-Json -Depth 5
    if ($OutJson) { $json | Set-Content -LiteralPath $OutJson -Encoding UTF8 }
    Write-Output "verdict: $($report.verdict)"
    Write-Output "version: $($report.version)  sha256: $($report.sha256)"
    Write-Output ("tui domains: submit={0} render={1} status={2} abort={3}" -f `
        $report.tui_submit_domain, $report.tui_render_domain,
        $report.tui_status_domain, $report.tui_abort_domain)
    Write-Output ("native v2: prompt={0} active={1} messages={2} abort={3} queue_delivery={4}" -f `
        $report.native_v2_prompt_present, $report.native_v2_active_status_present,
        $report.native_v2_messages_present, $report.native_v2_abort_present,
        $report.queue_delivery_supported)
    Write-Output $json
    exit 0
} catch {
    Write-Output "UNSUPPORTED_LAYOUT: probe error: $($_.Exception.Message)"
    if ($OutJson) { $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutJson -Encoding UTF8 }
    exit 2
}
