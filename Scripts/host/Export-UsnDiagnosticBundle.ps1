# USN diagnostic bundle exporter (read-only; no elevation needed).
#
# Builds a scrubbed, self-describing bundle so a future USN/Everything
# investigation carries the same members every time:
#   1. the canonical repair implementation (Scripts/host/UsnJournalGuard.ps1)
#   2. the regression suite (tests/test_usn_journal_guard.ps1)
#   3. scrubbed diagnostic evidence (guard -Json snapshot, log tail)
#   4. the scheduled-task plan (guard -InstallPlan + live task query)
#   5. an accurate host-state snapshot (boot time, clamp state, manifest.json)
#
# Privacy contract:
#   * Raw guard/task output is collected into a controlled temporary directory
#     only, sanitized before any deliverable is written, and the temporary
#     directory is removed on both success and failure. No raw
#     guard_snapshot.json / .err or task_plan raw/err file ever remains in the
#     deliverable directory.
#   * The scrubber removes profile paths, repository/drive paths, UNC paths,
#     SIDs, emails, account identities and sensitive error text while keeping
#     technical meaning (volume tokens like C:, exit codes, sizes stay).
#   * JSON property NAMES are share-safe too: canonical schema keys are kept
#     verbatim; any other key is scrubbed, and keys that collide after
#     scrubbing are disambiguated deterministically (key, key~2, key~3 ...)
#     so two different source keys are never silently merged.
#   * Every manifest warning (including copy/read exception text) passes the
#     same scrubber before serialization; the final verification refuses a
#     manifest whose warnings are not scrub-stable.
#   * The deliverable directory holds ONLY allowlisted files. Unexpected files
#     or sub-directories fail the export. Every JSON member is validated before
#     acceptance, every manifest member hash is verified against actual file
#     bytes, and the export exits non-zero when a mandatory member is missing
#     or unreadable.
#   * The two shipped SOURCE members (UsnJournalGuard.ps1,
#     test_usn_journal_guard.ps1) are mandatory troubleshooting evidence and are
#     copied byte-identically -- never scrubbed in place or in the deliverable
#     (rewriting executable source would corrupt it). Their safety is instead
#     proven by an independent read-only verdict on the delivered bytes
#     (Test-SourceMemberShareSafe): a share-safe export is REFUSED and the
#     member is not shipped when those bytes carry a detectable sensitive local
#     identifier (profile/drive/UNC paths, SIDs, emails, SAIAG account ids, this
#     host/user identity). Registry provider paths, hex literals, volume letters
#     and code/regex text are technical meaning and are accepted. This privacy
#     verdict is distinct from SHA-256 integrity: a recomputed hash never
#     bypasses it.
#
# Completeness contract (exit codes):
#   0  complete: every mandatory member present, readable, valid and
#      hash-verified against the final deliverable bytes (manifest.complete=true)
#   1  partial: at least one mandatory member missing/unreadable/invalid, or the
#      final verification failed (manifest.complete=false, reason in warnings)
#   2  refused: guard source unavailable (partial manifest still written with
#      complete=false), or the output directory is not empty (nothing written)
#   Mandatory: UsnJournalGuard.ps1, test_usn_journal_guard.ps1,
#              guard_snapshot.scrubbed.json, task_plan.json,
#              installed_tasks.scrubbed.json, bundle_manifest.json
#   Optional:  guard_log_tail.scrubbed.log (absence is reported, not fatal)
#   A process exit code alone is not proof: -VerifyBundle re-checks an existing
#   bundle directory from its bytes (exit 0 only for a verified complete bundle).
#
# Truthfulness contract (state models, not only exit codes):
#   * installed_tasks.scrubbed.json is an OBJECT, never a bare array, so a
#     failed scheduler query can never read as an empty task list:
#       queryState   QUERY_OK_EMPTY | QUERY_OK_WITH_TASKS | QUERY_FAILED
#       taskPresence NONE | PRESENT | UNKNOWN (UNKNOWN only when the query failed)
#       taskCount    0 | N | null (null = UNKNOWN; never 0 for a failed query)
#       infoState    TASK_INFO_COMPLETE | TASK_INFO_PARTIAL; a per-task
#                    infoState marks the rows whose metadata could not be read.
#                    The task identity itself is always preserved -- a failed
#                    Get-ScheduledTaskInfo never drops the task.
#     A QUERY_FAILED observation makes the bundle partial (exit 1): scheduled
#     task presence is mandatory evidence, and 'could not ask' is not 'none'.
#     TASK_INFO_PARTIAL does not fail the bundle; it is warned and marked.
#   * host observation is reported separately from structural completeness:
#       clampState PRESENT | ABSENT | UNREADABLE (a missing OR unreadable
#                  registry key is UNREADABLE, matching the canonical
#                  UsnJournalGuard.ps1 readFailed model; UNKNOWN is never
#                  promoted to healthy or absent)
#       bootTimeState OK | UNREADABLE
#       hostObservationComplete false when either state is UNREADABLE
#     bundle.complete (structural) stays independent of hostObservationComplete:
#     a bundle can be structurally complete while host state is UNKNOWN.
#   * -VerifyBundle independently re-checks scrub stability of every diagnostic
#     member (JSON keys, JSON values, log tail, manifest warnings) from its
#     bytes, and runs the same read-only source-member privacy verdict on the
#     delivered UsnJournalGuard.ps1 / test_usn_journal_guard.ps1 bytes. SHA-256
#     membership proves integrity only, never safety, so unsafe content with a
#     recomputed hash is still rejected. Canonical schema keys are preserved
#     verbatim. Coverage is exactly: profile/repo/drive/UNC paths, SIDs, emails,
#     SAIAG account ids and the current host/user identity tokens -- not an
#     unbounded promise against unknown secret formats. A diagnostic member
#     nested deeper than the supported verification depth is REFUSED (its
#     privacy verdict cannot be completed), never silently passed.
#
# Explicitly NEVER collected: credentials, SAITULS.ini, Secure Apps
# vaults/state/apps, user profile contents, Bin binaries, .saipen memory,
# Security event logs, or anything outside the member list below.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\host\Export-UsnDiagnosticBundle.ps1
#   powershell ... -SelfTest                          # hermetic check (scrubber + task/host state models); touches nothing live
#   powershell ... -FixtureDir <dir> -OutDir <dir>    # hermetic bundle build from adversarial fixtures (tests)
#   powershell ... -VerifyBundle <dir>                # re-verify an existing bundle (read-only)
param(
    [string]$OutDir = '',
    [string]$GuardSource = '',
    [string]$SuiteSource = '',
    [string]$FixtureDir = '',
    [string]$VerifyBundle = '',
    [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'
if (-not $GuardSource) {
    $GuardSource = Join-Path (Split-Path -Parent $PSCommandPath) 'UsnJournalGuard.ps1'
}

$ManifestSchema = 'saituls.usn-diagnostic-bundle.v1'
$AllowList = @('UsnJournalGuard.ps1', 'test_usn_journal_guard.ps1', 'guard_snapshot.scrubbed.json', 'guard_log_tail.scrubbed.log', 'task_plan.json', 'installed_tasks.scrubbed.json', 'bundle_manifest.json')
$Mandatory = @('UsnJournalGuard.ps1', 'test_usn_journal_guard.ps1', 'guard_snapshot.scrubbed.json', 'task_plan.json', 'installed_tasks.scrubbed.json', 'bundle_manifest.json')
$Optional = @('guard_log_tail.scrubbed.log')
$JsonMembers = @('guard_snapshot.scrubbed.json', 'task_plan.json', 'installed_tasks.scrubbed.json', 'bundle_manifest.json')
# Canonical schema keys emitted by UsnJournalGuard.ps1 (-Json report and
# -InstallPlan task plan), the live installed-task query below, and this
# manifest. These are fixed literals of SAITULS code, never user data, so they
# are preserved verbatim (scrubbing them could corrupt the schema, e.g. when a
# host/user name happens to equal a key word).
$CanonicalKeys = @(
    'schema', 'mode', 'exitCode', 'elevated', 'psVersion', 'fsutil', 'rebootRequired', 'targetUnmet',
    'registry', 'path', 'name', 'present', 'value', 'readFailed',
    'volumes', 'volume', 'state', 'maxBytes', 'maxMB', 'healthy', 'detail', 'targetMB', 'targetMet',
    'problems', 'actions',
    'taskPath', 'taskName', 'description', 'action', 'execute', 'arguments', 'triggers', 'type', 'at',
    'principal', 'userId', 'logonType', 'runLevel', 'settings', 'startWhenAvailable', 'multipleInstances',
    'executionTimeLimitMinutes', 'lastRun', 'lastResult'
)
$script:KeyRedactions = 0
# Maximum JSON nesting the privacy walk covers. Members deeper than this are
# REFUSED by Test-BundleIntegrity, never silently truncated into a pass (the
# canonical members serialize at Depth 6/10, well inside this limit). The bound
# keeps traversal resource use bounded.
$MaxJsonVerifyDepth = 12
$script:WalkDepthTruncated = $false

function Invoke-Scrub([string]$text, [switch]$SkipUnc) {
    # -SkipUnc exists for the SelfTest mutation control only: it removes the
    # UNC rule so the suite can prove its UNC assertion is load-bearing.
    if (-not $text) { return '' }
    $out = $text
    if ($env:COMPUTERNAME) { $out = $out -replace [regex]::Escape($env:COMPUTERNAME), '<HOST>' }
    if ($env:USERNAME) { $out = $out -replace [regex]::Escape($env:USERNAME), '<USER>' }
    $out = $out -replace 'S-1-5-21(-\d+)+', '<SID>'
    $out = $out -replace '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '<EMAIL>'
    $out = $out -replace 'SAIAG_[0-9a-fA-F]+', '<ACCOUNT>'
    # Quoted absolute paths first (exception messages quote paths, and quoted
    # paths may contain spaces that the unquoted rules below stop at, which
    # would leak the tail of a profile or folder name). The whole quoted span
    # is replaced, keeping the quotes so the sentence stays readable.
    $out = $out -replace '(?<q>[''"])[A-Za-z]:\\+Users\\+[^''"\r\n]*\k<q>', '${q}<USERPROFILE>${q}'
    $out = $out -replace '(?<q>[''"])\\\\[A-Za-z0-9._-]+\\[^''"\r\n]*\k<q>', '${q}<UNC>${q}'
    $out = $out -replace '(?<q>[''"])[A-Za-z]:\\[^''"\r\n]*\k<q>', '${q}<PATH>${q}'
    # Windows profile paths before generic drive paths. Note: inside [...]
    # \s must stay a whitespace class (single backslash); [\\s] would
    # exclude the letter 's' and truncate names like fx_user.
    $out = $out -replace '[A-Za-z]:\\Users\\[^\s\"''<>|]+(?:\\[^\s\"''<>|]*)*', '<USERPROFILE>'
    # UNC paths, plain and JSON-escaped forms: server + share required,
    # segments exclude backslashes (so a trailing JSON escape is never
    # consumed and the JSON stays valid), and never preceded by ':' (so the
    # '\\server' fragment of a JSON-escaped 'V:\\server\\...' drive path
    # is left for the drive rule). JSON-escaped '\\SAITULS\\' task paths
    # have no share and are never mistaken for a UNC share.
    if (-not $SkipUnc) {
        $out = $out -replace '(?<!:)(?:\\\\[A-Za-z0-9._-]+\\[^\s\\\\"''<>|]+(?:\\[^\s\\\\"''<>|]+)*|\\\\\\\\[A-Za-z0-9._-]+\\\\[^\s\\\\"''<>|]+(?:\\\\[^\s\\\\"''<>|]+)*)', '<UNC>'
    }
    # Any remaining absolute drive path (repo paths, non-system drives,
    # command-line errors containing absolute paths). Registry provider
    # paths (HKLM:\, HKCU:\, HKCR:\, HKU:\, HKCC:\) are technical meaning,
    # not file paths, and are left intact (SIDs inside them are already gone).
    $out = $out -replace '(?<!HKL)(?<!HKC)(?<!HK)[A-Za-z]:\\[^\s\"''<>|]*', '<PATH>'
    return $out
}

function Test-ValidJson([string]$text) {
    if ([string]::IsNullOrWhiteSpace($text)) { return $false }
    try { $text | ConvertFrom-Json | Out-Null; return $true } catch { return $false }
}

function Get-ScrubbedKey([string]$key, [System.Collections.IDictionary]$target) {
    # Canonical schema keys stay verbatim. Any other key is scrubbed with the
    # same text scrubber as values; a changed key is counted as a redaction.
    # Collisions (after scrubbing, or against an existing key under the
    # case-insensitive dictionary) get a deterministic ~N suffix in input
    # order, so different source keys are never merged into one.
    if ($CanonicalKeys -ccontains $key) { $candidate = $key }
    else {
        $candidate = Invoke-Scrub $key
        if ($candidate -cne $key) { $script:KeyRedactions++ }
    }
    if (-not $target.Contains($candidate)) { return $candidate }
    $n = 2
    while ($target.Contains("$candidate~$n")) { $n++ }
    return "$candidate~$n"
}

function Scrub-Object([object]$node) {
    # Recursively scrubs every string VALUE and every property NAME in parsed
    # JSON. String values hold plain single-backslash paths, so the plain-text
    # scrubber applies exactly and re-serialization keeps the JSON valid by
    # construction. Raw-text scrubbing of serialized JSON is never used for
    # these members: JSON escapes make backslash runs asymmetric and corrupt
    # the structure.
    # Arrays are returned with the unary comma so PowerShell never unrolls
    # them: an empty array stays [] (not null) and a one-element array stays
    # [x] (not x), keeping the schema stable.
    if ($null -eq $node) { return $null }
    if ($node -is [string]) { return (Invoke-Scrub $node) }
    if ($node -is [System.Collections.IDictionary]) {
        $o = [ordered]@{}
        foreach ($k in @($node.Keys)) { $o[(Get-ScrubbedKey ([string]$k) $o)] = Scrub-Object $node[$k] }
        return $o
    }
    if ($node -is [System.Management.Automation.PSCustomObject]) {
        $o = [ordered]@{}
        foreach ($p in $node.PSObject.Properties) { $o[(Get-ScrubbedKey $p.Name $o)] = Scrub-Object $p.Value }
        return $o
    }
    if ($node -is [System.Collections.IEnumerable]) {
        $list = New-Object System.Collections.Generic.List[object]
        foreach ($item in $node) { $list.Add((Scrub-Object $item)) }
        return , ($list.ToArray())
    }
    return $node
}

function Convert-ScrubbedJson([string]$rawText) {
    # Returns scrubbed JSON text, or $null when the raw text is not JSON.
    # PowerShell 7 enumerates a top-level JSON array (a one-element array
    # would collapse to a scalar); -NoEnumerate keeps it intact. 5.1 never
    # enumerates and has no such switch.
    try {
        if ($PSVersionTable.PSVersion.Major -ge 7) { $parsed = ConvertFrom-Json -InputObject $rawText -NoEnumerate -ErrorAction Stop }
        else { $parsed = ConvertFrom-Json -InputObject $rawText -ErrorAction Stop }
    } catch { return $null }
    $scrubbed = Scrub-Object $parsed
    try { $text = ConvertTo-Json -InputObject $scrubbed -Depth 10 -ErrorAction Stop } catch { return $null }
    if (-not (Test-ValidJson $text)) { return $null }
    return $text
}

function Get-FileHashHex([string]$path) {
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Format-ShareSafe([string]$text, [int]$max = 500) {
    $s = Invoke-Scrub $text
    $s = ($s -replace '[\r\n]+', ' ').Trim()
    if ($s.Length -gt $max) { $s = $s.Substring(0, $max) }
    return $s
}

function Test-ShareSafeText([string]$text) {
    # True when the text carries no detectable sensitive token. Scrubber
    # markers are removed first so '<PATH>' etc. never count as findings.
    if (-not $text) { return $true }
    $t = $text -replace '<(USERPROFILE|PATH|UNC|EMAIL|SID|ACCOUNT|HOST|USER)>', ' '
    if ($t -match '(?<!HKL)(?<!HKC)(?<!HK)[A-Za-z]:\\') { return $false }
    if ($t -match '\\\\[A-Za-z0-9._-]+\\') { return $false }
    if ($t -match 'S-1-5-21(-\d+)+') { return $false }
    if ($t -match '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}') { return $false }
    if ($t -match 'SAIAG_[0-9a-fA-F]+') { return $false }
    foreach ($id in @($env:COMPUTERNAME, $env:USERNAME)) {
        if ($id -and $id.Length -ge 3 -and $t -match [regex]::Escape($id)) { return $false }
    }
    return $true
}

function Test-SourceMemberShareSafe([string]$path) {
    # Read-only privacy verdict for a SHIPPED source member's delivered bytes
    # (UsnJournalGuard.ps1 / test_usn_journal_guard.ps1). True when the member
    # carries no detectable sensitive local identifier: profile/drive paths, UNC
    # paths, SIDs, emails, SAIAG account ids, this host/user identity. Registry
    # provider paths (HKLM:\, HKCU:\, ...), volume letters, hex literals and
    # code/regex text are technical meaning and are accepted; scrubber markers
    # in code are never counted as findings. This is the same detectable set as
    # Test-ShareSafeText -- not a claim about unknown secret formats.
    # The member itself is NEVER rewritten or scrubbed: integrity (byte-identical
    # copy verified by SHA-256) and this privacy verdict are independent gates.
    try { $t = Get-Content -Raw -LiteralPath $path -ErrorAction Stop } catch { return $false }
    if ($null -eq $t) { return $false }
    return (Test-ShareSafeText $t)
}

function Get-UnstableDiagnosticToken([object]$node) {
    # Walks parsed JSON and returns up to 3 tokens the scrubber would still
    # change on a second pass (a scrub-stable member is a fixed point of
    # Invoke-Scrub). Canonical schema keys are exempt by construction: they are
    # fixed literals of SAITULS code and are preserved verbatim on purpose.
    # Bounded in depth and result count; read-only, never rewrites the member.
    # A node deeper than $MaxJsonVerifyDepth is NOT a clean node: the walk sets
    # $script:WalkDepthTruncated so the caller refuses the member instead of
    # reading "traversal stopped" as "scrub-stable".
    $found = New-Object System.Collections.Generic.List[string]
    $walk = {
        param($n, $d)
        if ($null -eq $n) { return }
        if ($found.Count -ge 3) { return }
        if ($d -gt $MaxJsonVerifyDepth) { $script:WalkDepthTruncated = $true; return }
        if ($n -is [string]) {
            if ($n -and (Invoke-Scrub $n) -cne $n) { $found.Add($n) | Out-Null }
            return
        }
        if ($n -is [System.Collections.IDictionary]) {
            foreach ($k in @($n.Keys)) {
                $ks = [string]$k
                if ($CanonicalKeys -cnotcontains $ks -and (Invoke-Scrub $ks) -cne $ks) { $found.Add($ks) | Out-Null }
                & $walk $n[$k] ($d + 1)
            }
            return
        }
        if ($n -is [System.Management.Automation.PSCustomObject]) {
            foreach ($p in $n.PSObject.Properties) {
                $ks = [string]$p.Name
                if ($CanonicalKeys -cnotcontains $ks -and (Invoke-Scrub $ks) -cne $ks) { $found.Add($ks) | Out-Null }
                & $walk $p.Value ($d + 1)
            }
            return
        }
        if ($n -is [System.Collections.IEnumerable]) { foreach ($i in $n) { & $walk $i ($d + 1) } }
    }
    & $walk $node 0
    if ($found.Count -eq 0) { return }
    return , $found.ToArray()
}

function Test-BundleIntegrity([string]$dir) {
    # Verifies a bundle directory from its bytes alone. Returns a list of
    # share-safe problem strings; an empty list means the bundle is verified.
    # The mandatory set comes from THIS script, never from the manifest, so a
    # tampered manifest cannot drop a requirement.
    $problems = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { $problems.Add('bundle directory missing') | Out-Null; return , $problems }
    $root = (Get-Item -LiteralPath $dir -Force).FullName.TrimEnd('\')
    $items = @(Get-ChildItem -LiteralPath $dir -Force -Recurse -ErrorAction SilentlyContinue)
    $files = New-Object System.Collections.Generic.List[string]
    foreach ($it in $items) {
        $isTop = (Split-Path -Parent $it.FullName).TrimEnd('\') -eq $root
        if ($it.PSIsContainer) { $problems.Add("unexpected deliverable directory rejected: $(Format-ShareSafe $it.Name 120)") | Out-Null; continue }
        if (-not $isTop -or ($AllowList -cnotcontains $it.Name)) { $problems.Add("unexpected deliverable file rejected: $(Format-ShareSafe $it.Name 120)") | Out-Null; continue }
        $files.Add($it.Name) | Out-Null
    }
    foreach ($m in $Mandatory) {
        if ($files -cnotcontains $m) { $problems.Add("mandatory member missing: $m") | Out-Null }
    }
    foreach ($jf in $JsonMembers) {
        if ($files -ccontains $jf) {
            $t = try { Get-Content -Raw -LiteralPath (Join-Path $dir $jf) -ErrorAction Stop } catch { $null }
            if (-not (Test-ValidJson $t)) { $problems.Add("deliverable is not valid JSON: $jf") | Out-Null }
        }
    }
    $manifest = $null
    if ($files -ccontains 'bundle_manifest.json') {
        try { $manifest = Get-Content -Raw -LiteralPath (Join-Path $dir 'bundle_manifest.json') -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { $manifest = $null }
    }
    if ($null -eq $manifest) { $problems.Add('manifest unreadable') | Out-Null; return , $problems }
    if ([string]$manifest.schema -ne $ManifestSchema) { $problems.Add('manifest schema mismatch') | Out-Null }
    $declared = @($manifest.members | ForEach-Object { [string]$_ })
    foreach ($m in $declared) { if ($files -cnotcontains $m) { $problems.Add("manifest member not on disk: $(Format-ShareSafe $m 120)") | Out-Null } }
    foreach ($f in $files) { if ($declared -cnotcontains $f) { $problems.Add("file on disk not declared in manifest: $f") | Out-Null } }
    $hashNames = @()
    if ($manifest.hashes) { $hashNames = @($manifest.hashes.PSObject.Properties | ForEach-Object { $_.Name }) }
    foreach ($f in $files) {
        if ($f -eq 'bundle_manifest.json') { continue }
        if ($hashNames -cnotcontains $f) { $problems.Add("member has no manifest hash: $f") | Out-Null; continue }
        $actual = try { Get-FileHashHex (Join-Path $dir $f) } catch { 'unreadable' }
        if ($actual -ne [string]$manifest.hashes.$f) { $problems.Add("manifest hash disagrees with deliverable bytes: $f") | Out-Null }
    }
    foreach ($h in $hashNames) { if ($files -cnotcontains $h) { $problems.Add("manifest hash for absent member: $(Format-ShareSafe $h 120)") | Out-Null } }
    # ---- scrub stability of the diagnostic members -------------------------
    # Independent of the hashes above: SHA-256 membership proves the bytes are
    # the ones the manifest declares, never that those bytes are safe, so a
    # member whose hash was recomputed over unsafe content is rejected here on
    # content, not on membership. Canonical schema keys are exempt (see
    # Get-UnstableDiagnosticToken); a missing/invalid member is reported by the
    # membership and JSON checks above and skipped here.
    foreach ($jf in @('guard_snapshot.scrubbed.json', 'installed_tasks.scrubbed.json', 'task_plan.json')) {
        if ($files -cnotcontains $jf) { continue }
        $t = try { Get-Content -Raw -LiteralPath (Join-Path $dir $jf) -ErrorAction Stop } catch { $null }
        if (-not $t) { continue }
        $parsed = try { ConvertFrom-Json -InputObject $t -ErrorAction Stop } catch { $null }
        if ($null -eq $parsed) { continue }
        $script:WalkDepthTruncated = $false
        $unstable = @(Get-UnstableDiagnosticToken $parsed | Where-Object { $null -ne $_ -and "$_" -ne '' })
        # No silent truncation: JSON deeper than the supported verification
        # depth is refused, because the walk could not have seen everything.
        if ($script:WalkDepthTruncated) {
            $problems.Add("diagnostic member exceeds supported verification depth (> $MaxJsonVerifyDepth nesting levels), privacy verdict refused: $jf") | Out-Null
            continue
        }
        if ($unstable.Count -gt 0) {
            $first = Format-ShareSafe ([string]$unstable[0]) 80
            $problems.Add("diagnostic member is not scrub-stable: $jf ($($unstable.Count) token(s), first: $first)") | Out-Null
        }
    }
    if ($files -ccontains 'guard_log_tail.scrubbed.log') {
        $t = try { Get-Content -Raw -LiteralPath (Join-Path $dir 'guard_log_tail.scrubbed.log') -ErrorAction Stop } catch { $null }
        if ($t -and (Invoke-Scrub $t) -cne $t) { $problems.Add('log member is not scrub-stable: guard_log_tail.scrubbed.log') | Out-Null }
    }
    # ---- source-member privacy -----------------------------------------------
    # The two shipped source members are NEVER scrubbed (scrubbing executable
    # source would corrupt it), so their safety is proven by an independent
    # read-only verdict on the delivered bytes instead of by a rewrite. This is
    # separate from SHA-256 membership: a recomputed hash never bypasses it, and
    # a member removed for copy/read failure never reaches this gate as "safe"
    # (it is reported missing by the mandatory-member check above).
    foreach ($sm in @('UsnJournalGuard.ps1', 'test_usn_journal_guard.ps1')) {
        if ($files -ccontains $sm) {
            $p = Join-Path $dir $sm
            if (Test-Path -LiteralPath $p) {
                if (-not (Test-SourceMemberShareSafe $p)) {
                    $problems.Add("source member is not share-safe: $sm (delivered bytes carry a sensitive local identifier)") | Out-Null
                }
            }
        }
    }
    foreach ($w in @($manifest.warnings)) {
        if ($null -ne $w -and -not (Test-ShareSafeText ([string]$w))) { $problems.Add('manifest warning is not share-safe') | Out-Null; break }
    }
    foreach ($w in @($manifest.warnings)) {
        if ($null -eq $w) { continue }
        $ws = [string]$w
        if ((Invoke-Scrub $ws) -cne $ws) { $problems.Add('manifest warning is not scrub-stable') | Out-Null; break }
    }
    return , $problems
}

function Get-InstalledTaskObservation {
    # Read-only scheduled-task observation with injectable seams ($QueryTasks /
    # $QueryInfo default to the real cmdlets with -ErrorAction Stop, so a
    # scheduler failure is a terminating error, never a silent empty result).
    # A failed query yields queryState=QUERY_FAILED and taskCount=$null: it is
    # never collapsed into an empty task list. A failed per-task info read keeps
    # the task's identity and only marks its metadata TASK_INFO_PARTIAL.
    param(
        [scriptblock]$QueryTasks = $null,
        [scriptblock]$QueryInfo = $null
    )
    if (-not $QueryTasks) { $QueryTasks = { Get-ScheduledTask -ErrorAction Stop } }
    if (-not $QueryInfo) { $QueryInfo = { param($p, $n) Get-ScheduledTaskInfo -TaskPath $p -TaskName $n -ErrorAction Stop } }

    $tasks = New-Object System.Collections.Generic.List[object]
    $queryState = 'QUERY_FAILED'
    $queryError = $null
    try {
        foreach ($t in @(& $QueryTasks)) {
            if ($null -eq $t) { continue }
            if ($t.TaskPath -notlike '*SAITULS*' -and $t.TaskName -notlike '*USN*' -and $t.TaskName -notlike '*SAISPIN*') { continue }
            $tasks.Add([ordered]@{
                taskPath  = [string]$t.TaskPath
                taskName  = [string]$t.TaskName
                state     = [string]$t.State
                lastRun   = $null
                lastResult = $null
                infoState = 'OK'
            }) | Out-Null
        }
        $queryState = if ($tasks.Count -gt 0) { 'QUERY_OK_WITH_TASKS' } else { 'QUERY_OK_EMPTY' }
    } catch {
        $queryError = $_.Exception.Message
    }

    $infoState = 'TASK_INFO_COMPLETE'
    $infoError = $null
    if ($queryState -ne 'QUERY_FAILED') {
        foreach ($row in $tasks) {
            try {
                $info = & $QueryInfo $row.taskPath $row.taskName
                $row.lastRun = [string]$info.LastRunTime
                $row.lastResult = $info.LastTaskResult
            } catch {
                $row.infoState = 'TASK_INFO_PARTIAL'
                $infoState = 'TASK_INFO_PARTIAL'
                if (-not $infoError) { $infoError = $_.Exception.Message }
            }
        }
    }

    $presence = if ($queryState -eq 'QUERY_OK_EMPTY') { 'NONE' } elseif ($queryState -eq 'QUERY_OK_WITH_TASKS') { 'PRESENT' } else { 'UNKNOWN' }
    $count = if ($queryState -eq 'QUERY_FAILED') { $null } else { $tasks.Count }
    return [ordered]@{
        schema       = 'saituls.usn-installed-tasks.v1'
        queryState   = $queryState
        queryError   = $queryError
        taskPresence = $presence
        taskCount    = $count
        infoState    = $infoState
        infoError    = $infoError
        tasks        = @($tasks.ToArray())
    }
}

function Get-ClampObservation {
    # PRESENT | ABSENT | UNREADABLE. A missing OR unreadable registry key is
    # UNREADABLE, matching the canonical UsnJournalGuard.ps1 readFailed model
    # (a policy key that cannot be read is never 'the clamp is gone').
    param([scriptblock]$Read = $null)
    if (-not $Read) {
        $Read = {
            if (-not (Test-Path -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -ErrorAction Stop)) {
                throw 'registry key not readable: HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
            }
            $item = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -ErrorAction Stop
            if ($item.PSObject.Properties.Match('NtfsAllowUsnMinSize1Mb').Count -eq 0) {
                return [ordered]@{ present = $false; value = $null }
            }
            return [ordered]@{ present = $true; value = [string]$item.NtfsAllowUsnMinSize1Mb }
        }
    }
    try {
        $r = & $Read
        if ($r.present) { return [ordered]@{ state = 'PRESENT'; value = [string]$r.value } }
        return [ordered]@{ state = 'ABSENT'; value = $null }
    } catch {
        return [ordered]@{ state = 'UNREADABLE'; value = $null }
    }
}

function Get-BootObservation {
    # OK | UNREADABLE. An unavailable boot time stays UNKNOWN (null), never a
    # fabricated timestamp and never an implicit 'system healthy'.
    param([scriptblock]$Read = $null)
    if (-not $Read) { $Read = { (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToString('o') } }
    try { return [ordered]@{ state = 'OK'; value = [string](& $Read) } }
    catch { return [ordered]@{ state = 'UNREADABLE'; value = $null } }
}

if ($SelfTest) {
    $fails = 0
    function TCheck([string]$name, [bool]$ok) {
        if ($ok) { Write-Host "PASS  $name" } else { Write-Host "FAIL  $name"; $script:fails++ }
    }
    # Entirely synthetic fixture data: no real host, user, email, SID or path.
    $dirty = 'profile C:\Users\synth_user\Documents\x repo V:\REPO\Scripts\host\UsnJournalGuard.ps1 extrad D:\Projects\secret\plan.txt unc \\SYNTH-HOST-7F3A\share\secret.txt mail synth.user@example.invalid sid S-1-5-21-111-222-333-1001 acct SAIAG_abcdef12 err Access denied: C:\Users\synth_user\file.txt'
    $clean = Invoke-Scrub $dirty
    TCheck 'synth_host_scrubbed' ($clean -notmatch 'SYNTH-HOST-7F3A')
    TCheck 'synth_user_scrubbed' ($clean -notmatch 'synth_user')
    TCheck 'synth_email_scrubbed' ($clean -notmatch 'example\.invalid')
    TCheck 'synth_sid_scrubbed' ($clean -notmatch 'S-1-5-21')
    TCheck 'synth_account_scrubbed' ($clean -notmatch 'SAIAG_')
    TCheck 'synth_profile_scrubbed' ($clean -notmatch 'C:\\Users\\synth_user')
    TCheck 'synth_repo_path_scrubbed' ($clean -notmatch 'V:\\REPO')
    TCheck 'synth_drive_path_scrubbed' ($clean -notmatch 'D:\\Projects')
    # The input carries \\SYNTH-HOST-7F3A\share\secret.txt. Asserting on
    # 'SYNTH-SRV' (a token that never appears) passed vacuously and never
    # exercised the UNC rule; assert the token that is actually in the input.
    TCheck 'synth_unc_host_scrubbed' ($clean -notmatch 'SYNTH-HOST-7F3A')
    # Negative mutation control: with the UNC rule removed the same input keeps
    # the token, which proves the assertion above is load-bearing rather than
    # true by accident.
    $mutated = Invoke-Scrub $dirty -SkipUnc
    TCheck 'mutation_control_unc_rule_required' ($mutated -match 'SYNTH-HOST-7F3A')
    TCheck 'markers_present' ($clean -match '<USERPROFILE>' -and $clean -match '<PATH>' -and $clean -match '<UNC>' -and $clean -match '<EMAIL>' -and $clean -match '<SID>' -and $clean -match '<ACCOUNT>')
    TCheck 'volume_token_preserved' (('volume C: size 32' | ForEach-Object { Invoke-Scrub $_ }) -match 'C:')
    TCheck 'registry_path_preserved' ((Invoke-Scrub 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem') -ceq 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem')
    $quoted = Invoke-Scrub "Could not find file 'D:\Synth Secret Dir\inner name\plan.txt' nor ""C:\Users\synth user\a b.txt"""
    TCheck 'quoted_spaced_paths_scrubbed' (($quoted -notmatch 'Synth Secret|inner name|synth user|a b\.txt') -and ($quoted -match "'<PATH>'") -and ($quoted -match '"<USERPROFILE>"'))
    $keyObj = ConvertFrom-Json '{"schema":"s","D:\\SynthA\\k":1,"D:\\SynthB\\k":2,"synth.user@example.invalid":{"inner":[["a"],[]]},"single":["only"]}'
    $script:KeyRedactions = 0
    $ko = Scrub-Object $keyObj
    $kj = ConvertTo-Json -InputObject $ko -Depth 10 -Compress
    TCheck 'json_keys_scrubbed' (($kj -notmatch 'Synth[AB]') -and ($kj -notmatch 'example\.invalid'))
    TCheck 'json_key_collision_not_merged' (($ko.Contains('<PATH>')) -and ($ko.Contains('<PATH>~2')) -and $ko['<PATH>'] -eq 1 -and $ko['<PATH>~2'] -eq 2)
    TCheck 'json_canonical_key_kept' ($ko.Contains('schema') -and $script:KeyRedactions -eq 3)
    TCheck 'json_arrays_stable' ($kj -match '"single":\["only"\]' -and $kj -match '"inner":\[\["a"\],\[\]\]')

    # ---- scheduled-task observation state model (hermetic, injected) -------
    $empty = Get-InstalledTaskObservation -QueryTasks { @() } -QueryInfo { param($p, $n) throw 'unused' }
    TCheck 'task_query_ok_empty' ($empty.queryState -ceq 'QUERY_OK_EMPTY' -and $empty.taskPresence -ceq 'NONE' -and $empty.taskCount -eq 0)
    $multi = Get-InstalledTaskObservation -QueryTasks {
        [pscustomobject]@{ TaskPath = '\SAITULS\'; TaskName = 'USN Journal Guard'; State = 'Ready' }
        [pscustomobject]@{ TaskPath = '\SAITULS\'; TaskName = 'SAISPIN Watchdog'; State = 'Running' }
    } -QueryInfo { param($p, $n) [pscustomobject]@{ LastRunTime = '2026-10-10T00:00:00'; LastTaskResult = 0 } }
    TCheck 'task_query_ok_with_tasks' ($multi.queryState -ceq 'QUERY_OK_WITH_TASKS' -and $multi.taskPresence -ceq 'PRESENT' -and $multi.taskCount -eq 2 -and $multi.infoState -ceq 'TASK_INFO_COMPLETE')
    $svcfail = Get-InstalledTaskObservation -QueryTasks { throw 'The Task Scheduler service is not available (0x800706BA)' }
    TCheck 'task_query_failed_not_absent' ($svcfail.queryState -ceq 'QUERY_FAILED' -and $svcfail.taskPresence -ceq 'UNKNOWN' -and $null -eq $svcfail.taskCount -and @($svcfail.tasks).Count -eq 0)
    TCheck 'task_query_failed_message_kept' ($svcfail.queryError -match 'Task Scheduler service is not available')
    $denied = Get-InstalledTaskObservation -QueryTasks { throw 'Access is denied.' }
    TCheck 'task_query_access_denied_visible' ($denied.queryState -ceq 'QUERY_FAILED' -and $denied.queryError -match 'Access is denied')
    $cmdfail = Get-InstalledTaskObservation -QueryTasks { Get-ScheduledTaskNoSuchCmdlet -ErrorAction Stop }
    TCheck 'task_query_command_failure_visible' ($cmdfail.queryState -ceq 'QUERY_FAILED' -and $cmdfail.queryError -match 'Get-ScheduledTaskNoSuchCmdlet')
    $partial = Get-InstalledTaskObservation -QueryTasks {
        [pscustomobject]@{ TaskPath = '\SAITULS\'; TaskName = 'USN Journal Guard'; State = 'Ready' }
        [pscustomobject]@{ TaskPath = '\SAITULS\'; TaskName = 'SAISPIN Watchdog'; State = 'Running' }
    } -QueryInfo { param($p, $n) if ($n -eq 'USN Journal Guard') { throw 'Access is denied.' }; [pscustomobject]@{ LastRunTime = '2026-10-10T00:00:00'; LastTaskResult = 7 } }
    TCheck 'task_info_partial_marked' ($partial.infoState -ceq 'TASK_INFO_PARTIAL' -and $partial.queryState -ceq 'QUERY_OK_WITH_TASKS')
    TCheck 'task_info_partial_keeps_identities' (@($partial.tasks).Count -eq 2 -and $partial.tasks[0].infoState -ceq 'TASK_INFO_PARTIAL' -and $partial.tasks[1].infoState -ceq 'OK' -and $partial.tasks[1].lastResult -eq 7)
    TCheck 'task_info_partial_no_fabricated_absence' ($partial.taskPresence -ceq 'PRESENT' -and $partial.taskCount -eq 2)

    # ---- host observation state model (hermetic, injected) ----------------
    $cPresent = Get-ClampObservation -Read { [ordered]@{ present = $true; value = '1' } }
    TCheck 'clamp_present' ($cPresent.state -ceq 'PRESENT' -and $cPresent.value -ceq '1')
    $cAbsent = Get-ClampObservation -Read { [ordered]@{ present = $false; value = $null } }
    TCheck 'clamp_absent' ($cAbsent.state -ceq 'ABSENT' -and $null -eq $cAbsent.value)
    $cUnread = Get-ClampObservation -Read { throw 'Access is denied.' }
    TCheck 'clamp_unreadable_not_absent' ($cUnread.state -ceq 'UNREADABLE' -and $null -eq $cUnread.value)
    $bOk = Get-BootObservation -Read { '2026-10-10T00:00:00.0000000+03:00' }
    TCheck 'boot_time_confirmed' ($bOk.state -ceq 'OK' -and $bOk.value -ceq '2026-10-10T00:00:00.0000000+03:00')
    $bUnknown = Get-BootObservation -Read { throw 'CIM not available' }
    TCheck 'boot_time_unavailable_not_ok' ($bUnknown.state -ceq 'UNREADABLE' -and $null -eq $bUnknown.value)

    # ---- source-member privacy verdict (hermetic temp files) ----------------
    # Ordinary technical constants (registry provider paths, hex literals,
    # volume letters, JSON-escaped task paths) are accepted; a synthetic local
    # profile path is refused. The verdict only reads, never rewrites.
    $privDir = Join-Path ([IO.Path]::GetTempPath()) ('usn_priv_' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $privDir -Force | Out-Null
    try {
        $cleanSrc = Join-Path $privDir 'clean.ps1'
        Set-Content -LiteralPath $cleanSrc -Value "param() # HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem 0x0000000000100000 volume C: '\SAITULS\' settings Depth 10" -Encoding UTF8
        TCheck 'source_privacy_accepts_technical_constants' (Test-SourceMemberShareSafe $cleanSrc)
        $poisonSrc = Join-Path $privDir 'poison.ps1'
        Set-Content -LiteralPath $poisonSrc -Value 'param() # leak C:\Users\synth_user\private\notes.txt' -Encoding UTF8
        TCheck 'source_privacy_rejects_profile_path' (-not (Test-SourceMemberShareSafe $poisonSrc))
    } finally {
        Remove-Item -LiteralPath $privDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if ($fails -eq 0) { Write-Host 'bundle self-test: all checks PASS'; exit 0 }
    Write-Host "bundle self-test: $fails check(s) FAILED"; exit 1
}

if ($VerifyBundle) {
    $vp = Test-BundleIntegrity $VerifyBundle
    $vm = $null
    try { $vm = Get-Content -Raw -LiteralPath (Join-Path $VerifyBundle 'bundle_manifest.json') -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { }
    # Structural verification and host-observation certainty are separate
    # facts: a verified complete bundle can still carry an UNKNOWN host state,
    # which is reported but never upgraded to healthy and never a verify
    # failure (the bundle does not claim to have observed it).
    $hostNote = ''
    if ($vm -and $vm.PSObject.Properties.Match('hostObservationComplete').Count -gt 0 -and -not $vm.hostObservationComplete) {
        $hostNote = "; host observation incomplete: boot=$($vm.bootTimeState) clamp=$($vm.clampState)"
    }
    if ($vp.Count -eq 0 -and $vm -and $vm.complete -eq $true) { Write-Host "bundle verified: complete$hostNote"; exit 0 }
    if ($vp.Count -eq 0) { Write-Host "bundle verified: consistent but declared partial (complete=false)$hostNote"; exit 1 }
    Write-Host ('bundle verification FAILED: ' + ($vp -join ' | '))
    exit 1
}

$guardAvailable = Test-Path -LiteralPath $GuardSource -PathType Leaf
if (-not $SuiteSource) {
    $SuiteSource = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSCommandPath))) 'tests\test_usn_journal_guard.ps1'
}
if (-not $OutDir) {
    $OutDir = Join-Path ([IO.Path]::GetTempPath()) ('usn_bundle_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
}
# A reused output directory could carry stale members from an earlier run
# (e.g. an old regression-suite copy) that would satisfy membership falsely.
# Refuse instead of deleting anything the caller owns.
if ((Test-Path -LiteralPath $OutDir) -and @(Get-ChildItem -LiteralPath $OutDir -Force -ErrorAction SilentlyContinue).Count -gt 0) {
    Write-Host 'ERROR: output directory is not empty; refusing to mix with existing files'
    exit 2
}

$rawDir = Join-Path ([IO.Path]::GetTempPath()) ('usn_raw_' + [Guid]::NewGuid().ToString('N'))
$warnings = New-Object System.Collections.Generic.List[string]
function Add-Warn([string]$text) {
    $w = Format-ShareSafe $text
    if ($warnings -cnotcontains $w) { $warnings.Add($w) | Out-Null }
}
$complete = $true
$snapCode = -1
try {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    New-Item -ItemType Directory -Path $rawDir -Force | Out-Null

    # 1+2. canonical implementation + regression suite (both mandatory).
    # Each copy is verified byte-for-byte against its source, and the DELIVERED
    # bytes then pass an independent read-only privacy verdict: a share-safe
    # export is refused when a source member carries a detectable sensitive
    # local identifier, and unsafe bytes are never shipped. The comment at the
    # top of Test-SourceMemberShareSafe names the exact accepted set.
    $sourceMembers = [ordered]@{ 'UsnJournalGuard.ps1' = $GuardSource; 'test_usn_journal_guard.ps1' = $SuiteSource }
    foreach ($name in $sourceMembers.Keys) {
        $src = $sourceMembers[$name]
        $label = if ($name -eq 'UsnJournalGuard.ps1') { 'guard source' } else { 'regression suite source' }
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
            Add-Warn "mandatory member missing: $name ($label not found); bundle partial"
            $complete = $false
            continue
        }
        $dest = Join-Path $OutDir $name
        try {
            Copy-Item -LiteralPath $src -Destination $dest -Force -ErrorAction Stop
            if ((Get-FileHashHex $src) -ne (Get-FileHashHex $dest)) {
                Add-Warn "mandatory member copy mismatch: $name; bundle partial"
                $complete = $false
            }
        } catch {
            Add-Warn "mandatory member unreadable: $name ($label copy failed: $($_.Exception.Message)); bundle partial"
            $complete = $false
            if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue }
        }
        # Privacy gate on the final delivered bytes: distinct from the copy
        # check above (integrity), and never a rewrite of the member.
        if (Test-Path -LiteralPath $dest) {
            if (-not (Test-SourceMemberShareSafe $dest)) {
                Add-Warn "mandatory member not share-safe: $name (delivered source bytes carry a sensitive local identifier); bundle partial"
                $complete = $false
                Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
            }
        }
    }

    $useFixture = (-not [string]::IsNullOrWhiteSpace($FixtureDir)) -and (Test-Path -LiteralPath $FixtureDir)
    # Live collection runs the guard itself; without it there is nothing
    # truthful to collect, so sections 3-4 are skipped and reported missing.
    $collect = $guardAvailable -or $useFixture

    # 3. guard JSON snapshot: raw -> $rawDir only, scrubbed -> deliverable.
    $rawSnap = Join-Path $rawDir 'guard_snapshot.json'
    $rawSnapErr = Join-Path $rawDir 'guard_snapshot.err'
    if ($collect -and $useFixture) {
        $fx = Join-Path $FixtureDir 'guard_snapshot.raw.json'
        if (Test-Path -LiteralPath $fx) { Copy-Item -LiteralPath $fx -Destination $rawSnap -Force }
        $fxe = Join-Path $FixtureDir 'guard_snapshot.raw.err'
        if (Test-Path -LiteralPath $fxe) { Copy-Item -LiteralPath $fxe -Destination $rawSnapErr -Force } else { Set-Content -LiteralPath $rawSnapErr -Value '' -Encoding UTF8 }
        $fxc = Join-Path $FixtureDir 'snapshotExit.txt'
        if (Test-Path -LiteralPath $fxc) { $snapCode = [int]((Get-Content -Raw -LiteralPath $fxc).Trim()) } else { $snapCode = 0 }
    } elseif ($collect) {
        $hostExe = (Get-Command 'powershell.exe' -ErrorAction SilentlyContinue).Source
        if (-not $hostExe) { $hostExe = 'powershell.exe' }
        # -Quiet: the guard's human log echo goes to stdout too (Write-Host), and
        # the JSON report is then a second document after it, unparseable as one
        # JSON text. -Quiet keeps stdout machine-only while the log file is
        # still written (-Json already implies it; passed for both paths).
        & $hostExe -NoProfile -ExecutionPolicy Bypass -File $GuardSource -Json -Quiet 1> $rawSnap 2> $rawSnapErr
        $snapCode = $LASTEXITCODE
    }
    $snapRawText = try { Get-Content -Raw -LiteralPath $rawSnap -ErrorAction Stop } catch { $null }
    if ($null -eq $snapRawText) {
        Add-Warn 'guard snapshot missing or unreadable; bundle partial'
        $complete = $false
    } else {
        $script:KeyRedactions = 0
        $snapJson = Convert-ScrubbedJson $snapRawText
        if ($null -eq $snapJson) {
            Add-Warn 'guard snapshot is not valid JSON; bundle partial'
            $complete = $false
            Set-Content -LiteralPath (Join-Path $OutDir 'guard_snapshot.scrubbed.json') -Value (Invoke-Scrub $snapRawText) -Encoding UTF8
        } else {
            Set-Content -LiteralPath (Join-Path $OutDir 'guard_snapshot.scrubbed.json') -Value $snapJson -Encoding UTF8
            if ($script:KeyRedactions -gt 0) { Add-Warn "json property names redacted in guard_snapshot.scrubbed.json: $($script:KeyRedactions)" }
        }
        # Raw stderr is never a deliverable; a non-empty scrubbed stderr is
        # recorded as a warning (paths scrubbed) without writing the raw text.
        $snapErrText = try { Get-Content -Raw -LiteralPath $rawSnapErr -ErrorAction SilentlyContinue } catch { '' }
        if (-not [string]::IsNullOrWhiteSpace($snapErrText)) {
            Add-Warn ('guard snapshot stderr (scrubbed): ' + $snapErrText)
        }
    }

    # 3b. log tail (optional source, scrubbed deliverable only).
    $logTailText = $null
    if ($useFixture) {
        $fx = Join-Path $FixtureDir 'guard_log_tail.raw.log'
        if (Test-Path -LiteralPath $fx) { $logTailText = Get-Content -Raw -LiteralPath $fx -ErrorAction SilentlyContinue }
    } else {
        $logPath = Join-Path $env:ProgramData 'SAI\usn-journal-guard.log'
        if (Test-Path -LiteralPath $logPath) {
            $tail = Get-Content -LiteralPath $logPath -Tail 100 -ErrorAction SilentlyContinue
            if ($tail) { $logTailText = ($tail -join "`n") }
        }
    }
    if ($null -ne $logTailText) {
        Set-Content -LiteralPath (Join-Path $OutDir 'guard_log_tail.scrubbed.log') -Value (Invoke-Scrub $logTailText) -Encoding UTF8
    } else {
        Add-Warn 'optional member absent: guard_log_tail.scrubbed.log (no guard log available)'
    }

    # 4. task plan + installed-task state (both read-only, raw -> $rawDir only).
    $rawPlanOut = Join-Path $rawDir 'task_plan.raw.txt'
    $rawPlanErr = Join-Path $rawDir 'task_plan.err'
    if ($collect -and $useFixture) {
        $fx = Join-Path $FixtureDir 'task_plan.raw.txt'
        if (Test-Path -LiteralPath $fx) { Copy-Item -LiteralPath $fx -Destination $rawPlanOut -Force }
        $fxe = Join-Path $FixtureDir 'task_plan.raw.err'
        if (Test-Path -LiteralPath $fxe) { Copy-Item -LiteralPath $fxe -Destination $rawPlanErr -Force } else { Set-Content -LiteralPath $rawPlanErr -Value '' -Encoding UTF8 }
    } elseif ($collect) {
        $hostExe2 = (Get-Command 'powershell.exe' -ErrorAction SilentlyContinue).Source
        if (-not $hostExe2) { $hostExe2 = 'powershell.exe' }
        # -Quiet: without it the guard echoes 'task plan: {compact}' to stdout
        # (Write-Host) before the real -InstallPlan document, and the raw text is
        # two concatenated JSON documents, which is not valid JSON.
        & $hostExe2 -NoProfile -ExecutionPolicy Bypass -File $GuardSource -InstallPlan -Quiet 1> $rawPlanOut 2> $rawPlanErr
    }
    $rawPlan = try { Get-Content -Raw -LiteralPath $rawPlanOut -ErrorAction Stop } catch { $null }
    if ($null -eq $rawPlan) {
        Add-Warn 'task plan missing or unreadable; bundle partial'
        $complete = $false
    } else {
        $jsonFrom = $rawPlan.IndexOf('{')
        if ($jsonFrom -ge 0) { $rawPlan = $rawPlan.Substring($jsonFrom) }
        $script:KeyRedactions = 0
        $planJson = Convert-ScrubbedJson $rawPlan
        if ($null -eq $planJson) {
            Add-Warn 'task plan is not valid JSON; bundle partial'
            $complete = $false
            Set-Content -LiteralPath (Join-Path $OutDir 'task_plan.json') -Value (Invoke-Scrub $rawPlan) -Encoding UTF8
        } else {
            Set-Content -LiteralPath (Join-Path $OutDir 'task_plan.json') -Value $planJson -Encoding UTF8
            if ($script:KeyRedactions -gt 0) { Add-Warn "json property names redacted in task_plan.json: $($script:KeyRedactions)" }
        }
        $planErrText = try { Get-Content -Raw -LiteralPath $rawPlanErr -ErrorAction SilentlyContinue } catch { '' }
        if (-not [string]::IsNullOrWhiteSpace($planErrText)) {
            Add-Warn ('task plan stderr (scrubbed): ' + $planErrText)
        }
    }

    # 4b. installed scheduled-task state (mandatory evidence; read-only).
    # The observation always goes through Get-InstalledTaskObservation, so the
    # live path and the hermetic fixture path share one state model.
    $tasksRawText = $null
    $obs = $null
    if ($collect -and $useFixture) {
        # Hermetic simulated query outcome. installed_tasks.query.json:
        #   {"query":"OK|FAIL","queryError":"...","info":"OK|FAIL",
        #    "infoError":"...","tasks":[{"taskPath","taskName","state",
        #    "lastRun","lastResult"}]}
        # installed_tasks.raw.json (legacy fixture shape: a task-row array) is
        # accepted as a successful query with those rows.
        $fxq = Join-Path $FixtureDir 'installed_tasks.query.json'
        $fxr = Join-Path $FixtureDir 'installed_tasks.raw.json'
        $spec = $null
        if (Test-Path -LiteralPath $fxq) {
            $spec = try { Get-Content -Raw -LiteralPath $fxq -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { $null }
        } elseif (Test-Path -LiteralPath $fxr) {
            $rows = try { Get-Content -Raw -LiteralPath $fxr -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { $null }
            if ($null -ne $rows) { $spec = [ordered]@{ query = 'OK'; queryError = ''; info = 'OK'; infoError = ''; tasks = @($rows) } }
        }
        if ($null -ne $spec) {
            $querySb = {
                if ([string]$spec.query -eq 'FAIL') { throw ('scheduled-task query unavailable: ' + [string]$spec.queryError) }
                foreach ($t in @($spec.tasks)) { [pscustomobject]@{ TaskPath = [string]$t.taskPath; TaskName = [string]$t.taskName; State = [string]$t.state } }
            }
            $infoSb = {
                param($p, $n)
                if ([string]$spec.info -eq 'FAIL') { throw ('scheduled-task metadata unavailable: ' + [string]$spec.infoError) }
                $row = @($spec.tasks) | Where-Object { [string]$_.taskPath -eq $p -and [string]$_.taskName -eq $n } | Select-Object -First 1
                [pscustomobject]@{ LastRunTime = [string]$row.lastRun; LastTaskResult = $row.lastResult }
            }
            $obs = Get-InstalledTaskObservation -QueryTasks $querySb -QueryInfo $infoSb
            $tasksRawText = ConvertTo-Json -InputObject $obs -Depth 6
        }
    } elseif ($collect) {
        $obs = Get-InstalledTaskObservation
        $tasksRawText = ConvertTo-Json -InputObject $obs -Depth 6
    }
    if ($null -eq $tasksRawText) {
        Add-Warn 'installed-task state missing or unreadable; bundle partial'
        $complete = $false
    } else {
        $script:KeyRedactions = 0
        $tasksJson = Convert-ScrubbedJson $tasksRawText
        if ($null -eq $tasksJson) {
            Add-Warn 'installed-task state is not valid JSON; bundle partial'
            $complete = $false
            Set-Content -LiteralPath (Join-Path $OutDir 'installed_tasks.scrubbed.json') -Value (Invoke-Scrub $tasksRawText) -Encoding UTF8
        } else {
            Set-Content -LiteralPath (Join-Path $OutDir 'installed_tasks.scrubbed.json') -Value $tasksJson -Encoding UTF8
            if ($script:KeyRedactions -gt 0) { Add-Warn "json property names redacted in installed_tasks.scrubbed.json: $($script:KeyRedactions)" }
        }
        # A failed query is UNKNOWN task presence, never 'no tasks'; it is
        # mandatory evidence, so it makes the bundle partial. Partial metadata
        # keeps every task identity and only marks the metadata incomplete.
        if ($obs.queryState -eq 'QUERY_FAILED') {
            Add-Warn ('scheduled-task query failed -> task presence UNKNOWN (not absent); bundle partial: ' + $obs.queryError)
            $complete = $false
        } elseif ($obs.infoState -eq 'TASK_INFO_PARTIAL') {
            Add-Warn ('scheduled-task metadata partially unavailable; task identities preserved, lastRun/lastResult UNKNOWN: ' + $obs.infoError)
        }
    }

    # 5. host-state observation (reads only). Fixture mode may supply
    #    host_state.json: {"bootState","bootTime","clampState","clampValue"};
    #    the default fixture state is a confirmed-absent clamp and a synthetic
    #    boot time. Structural bundle completeness stays independent of
    #    hostObservationComplete: an UNREADABLE host state is UNKNOWN, never
    #    promoted to healthy or absent, and never silently reported as a
    #    structurally incomplete bundle.
    $bootObs = [ordered]@{ state = 'OK'; value = 'synthetic' }
    $clampObs = [ordered]@{ state = 'ABSENT'; value = $null }
    if ($useFixture) {
        $fxh = Join-Path $FixtureDir 'host_state.json'
        if (Test-Path -LiteralPath $fxh) {
            $hs = try { Get-Content -Raw -LiteralPath $fxh -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { $null }
            if ($null -ne $hs) {
                $bootObs = [ordered]@{ state = [string]$hs.bootState; value = $hs.bootTime }
                $clampObs = [ordered]@{ state = [string]$hs.clampState; value = $hs.clampValue }
            }
        }
    } else {
        $bootObs = Get-BootObservation
        $clampObs = Get-ClampObservation
    }
    if ($bootObs.state -eq 'UNREADABLE') { Add-Warn 'host boot time unavailable (UNKNOWN, not absent)' }
    if ($clampObs.state -eq 'UNREADABLE') { Add-Warn 'registry clamp state unreadable (UNKNOWN, not absent)' }
    $hostComplete = (($bootObs.state -eq 'OK') -and ($clampObs.state -ne 'UNREADABLE'))

    # ---- acceptance: allowlist, JSON validity, membership ------------------
    $actualFiles = @(Get-ChildItem -LiteralPath $OutDir -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.Name } | Where-Object { $AllowList -ccontains $_ })
    foreach ($m in $Mandatory) {
        if ($m -eq 'bundle_manifest.json') { continue }
        if ($actualFiles -cnotcontains $m) { Add-Warn "mandatory member missing: $m"; $complete = $false }
    }
    foreach ($jf in $JsonMembers) {
        if ($jf -eq 'bundle_manifest.json' -or $actualFiles -cnotcontains $jf) { continue }
        $t = try { Get-Content -Raw -LiteralPath (Join-Path $OutDir $jf) -ErrorAction Stop } catch { $null }
        if (-not (Test-ValidJson $t)) { Add-Warn "deliverable is not valid JSON: $jf"; $complete = $false }
    }
    # Hashes cover every member EXCEPT the manifest itself: a stored self-hash
    # would be stale by construction (writing it changes the hashed bytes).
    # The manifest is verified by re-reading it, like every other member.
    $hashes = [ordered]@{}
    foreach ($f in $actualFiles) {
        try { $hashes[$f] = Get-FileHashHex (Join-Path $OutDir $f) } catch { $hashes[$f] = 'unreadable'; Add-Warn "member unreadable for hashing: $f"; $complete = $false }
    }
    $manifest = [ordered]@{
        schema       = $ManifestSchema
        builtAt      = (Get-Date).ToString('o')
        bootTime     = $bootObs.value
        bootTimeState = $bootObs.state
        clampValue   = $clampObs.value
        clampState   = $clampObs.state
        hostObservationComplete = [bool]$hostComplete
        snapshotExit = $snapCode
        complete     = [bool]$complete
        members      = @($actualFiles + 'bundle_manifest.json')
        mandatory    = @($Mandatory)
        hashes       = $hashes
        warnings     = @($warnings | ForEach-Object { Format-ShareSafe $_ })
        excluded     = @('credentials', 'SAITULS.ini', 'secure-apps/*', 'user profile contents', 'Bin/*', '.saipen/*', 'Security event logs')
    }
    $manifestPath = Join-Path $OutDir 'bundle_manifest.json'
    Set-Content -LiteralPath $manifestPath -Value (ConvertTo-Json -InputObject $manifest -Depth 6) -Encoding UTF8

    # ---- final verification against the delivered bytes --------------------
    # Success is never claimed from this process's own bookkeeping: the
    # finished directory is re-read and verified exactly as -VerifyBundle does.
    $problems = Test-BundleIntegrity $OutDir
    if ($problems.Count -gt 0) {
        foreach ($p in $problems) { Add-Warn $p }
        $complete = $false
        $manifest.complete = $false
        $manifest.warnings = @($warnings | ForEach-Object { Format-ShareSafe $_ })
        Set-Content -LiteralPath $manifestPath -Value (ConvertTo-Json -InputObject $manifest -Depth 6) -Encoding UTF8
    }

    if (-not $guardAvailable) {
        Write-Host ('ERROR: guard source not found; partial bundle: ' + ($warnings -join ' | '))
        exit 2
    }
    if (-not $complete) {
        Write-Host ('bundle partial: ' + ($warnings -join ' | '))
        exit 1
    }
    Write-Host "bundle written to $OutDir"
    exit 0
}
finally {
    if ($rawDir -and (Test-Path -LiteralPath $rawDir)) {
        Remove-Item -LiteralPath $rawDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
