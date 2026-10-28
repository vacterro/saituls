# T-166 SCENARIOS launcher. Data-driven from scenarios.json; owns no scenario
# logic. The launcher process itself is NEVER elevated - elevation is per
# action, requested only when a scenario declares requires_admin. Test mode
# (-TestResolve) resolves, validates and prints a plan without spawning any
# process; it is the only path automated tests may use.
param(
    [switch]$TestResolve,
    [string]$Scenario = "",
    [string]$Target = "",
    [string]$CheckScriptPath = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$registryPath = Join-Path $here "scenarios.json"
if (-not (Test-Path -LiteralPath $registryPath)) { throw "scenarios.json not found: $registryPath" }
$registry = Get-Content -LiteralPath $registryPath -Raw -Encoding UTF8 | ConvertFrom-Json

$interpreterMap = @{
    "python"     = @{ Exe = "python.exe";        Prefix = @() }
    "powershell" = @{ Exe = "powershell.exe";    Prefix = @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File") }
    "cmd"        = @{ Exe = "cmd.exe";           Prefix = @("/d", "/c") }
    "bash"       = @{ Exe = "bash.exe";          Prefix = @() }
}

function Get-ScenarioRoot { return $here }

# Win32 argv quoting: double backslashes preceding a quote, escape the quote
# itself, and double any trailing backslashes (CommandLineToArgvW rules).
function ConvertTo-Win32CommandLine {
    param([string[]]$Argv)
    return ($Argv | ForEach-Object {
        $a = $_ -replace '(\\+)"', '$1$1\"'
        $a = $a -replace '(\\+)$', '$1$1'
        '"' + $a + '"'
    }) -join " "
}

# Resolve a scenario's script to a full path, refusing any escape from the
# scenarios root (path traversal refusal is fail-closed, not cosmetic).
function Resolve-ScenarioScript {
    param([object]$Sc)
    if ($Sc.interpreter -eq "bash") {
        # Git Bash consumes /c/-style paths; still validate against the root.
    }
    $full = [System.IO.Path]::GetFullPath((Join-Path $here $Sc.script))
    $rootWithSep = $here.TrimEnd('\') + '\'
    if (-not $full.StartsWith($rootWithSep, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escape refused for scenario '$($Sc.id)': $($Sc.script)"
    }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "Script missing for scenario '$($Sc.id)': $full"
    }
    return $full
}

# Dependency validation. Returns $null when ready, or an exact blocker string.
function Get-DependencyBlocker {
    param([object]$Sc)
    foreach ($dep in @($Sc.dependencies)) {
        if ([string]::IsNullOrWhiteSpace($dep)) { continue }
        if ($dep.StartsWith("blocked:", [System.StringComparison]::OrdinalIgnoreCase)) {
            return $dep.Substring(8)
        }
        elseif ($dep.StartsWith("bundled:", [System.StringComparison]::OrdinalIgnoreCase)) {
            $name = $dep.Substring(8)
            if (-not (Test-Path -LiteralPath (Join-Path $here $name)) -and -not (Get-Command $name -ErrorAction SilentlyContinue)) {
                return "bundled dependency missing: $name"
            }
        }
        elseif ($dep.StartsWith("python:", [System.StringComparison]::OrdinalIgnoreCase)) {
            $mod = $dep.Substring(7)
            # EAP is Stop in this script; a native 2> redirect would turn the
            # probe's stderr into a terminating error. Relax locally.
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                & python.exe -c "import importlib,sys; importlib.import_module(sys.argv[1])" $mod 2>$null | Out-Null
                $probeRc = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $prevEap
            }
            if ($probeRc -ne 0) { return "python module missing: $mod" }
        }
        elseif ($dep.StartsWith("external:", [System.StringComparison]::OrdinalIgnoreCase)) {
            $name = $dep.Substring(9)
            if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
                return "external dependency missing: $name"
            }
        }
        else {
            return "unknown dependency spec: $dep"
        }
    }
    return $null
}

function Get-EnabledScenarios {
    param([object]$Reg)
    foreach ($sc in $Reg.scenarios) {
        if (-not $sc.PSObject.Properties["enabled"] -or $sc.enabled) { $sc }
    }
}

function Build-LaunchPlan {
    param([object]$Sc, [string]$TargetPath)
    $scriptFull = Resolve-ScenarioScript -Sc $Sc
    $interp = $interpreterMap[$Sc.interpreter]
    if ($null -eq $interp) { throw "Unknown interpreter '$($Sc.interpreter)' for scenario '$($Sc.id)'" }
    $argv = @()
    $argv += $interp.Prefix
    if ($Sc.interpreter -eq "python") { $argv += $scriptFull }
    elseif ($Sc.interpreter -eq "powershell") { $argv += $scriptFull }
    elseif ($Sc.interpreter -eq "cmd") { $argv += $scriptFull }
    elseif ($Sc.interpreter -eq "bash") { $argv += $scriptFull }
    if ($Sc.target_mode -eq "folder" -or $Sc.target_mode -eq "file") {
        if ([string]::IsNullOrWhiteSpace($TargetPath)) { throw "Scenario '$($Sc.id)' requires a target" }
        $argv += $TargetPath   # arrives exactly once, as one argv element
    }
    return [pscustomobject]@{
        Id            = $Sc.id
        Exe           = $interp.Exe
        Argv          = $argv
        RequiresAdmin = [bool]$Sc.requires_admin
        Destructive   = [bool]$Sc.destructive
    }
}

# ---------------------------------------------------------------- test mode
if ($TestResolve) {
    # PS 5.1 console codepage mangles non-ASCII JSON on stdout; tests read UTF-8.
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    if ($CheckScriptPath) {
        # Read-only resolver probe for regression tests: proves path traversal
        # refusal with the REAL product resolver, spawning nothing.
        try {
            $probe = [pscustomobject]@{ id = "_probe"; script = $CheckScriptPath; interpreter = "python" }
            $resolved = Resolve-ScenarioScript -Sc $probe
            [pscustomobject]@{ ok = $true; resolved = $resolved } | ConvertTo-Json -Compress
            exit 0
        } catch {
            [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
            exit 1
        }
    }
    try {
        $sc = $registry.scenarios | Where-Object { $_.id -eq $Scenario } | Select-Object -First 1
        if ($null -eq $sc) { throw "scenario not found: $Scenario" }
        if ($sc.PSObject.Properties["enabled"] -and -not $sc.enabled) {
            throw "scenario disabled: $Scenario ($($sc.blocked_reason))"
        }
        $blocker = Get-DependencyBlocker -Sc $sc
        if ($blocker) { throw "dependency blocker: $blocker" }
        $plan = Build-LaunchPlan -Sc $sc -TargetPath $Target
        [pscustomobject]@{
            ok = $true; id = $plan.Id; exe = $plan.Exe; argv = $plan.Argv
            cmdline = ConvertTo-Win32CommandLine -Argv $plan.Argv
            requires_admin = $plan.RequiresAdmin; destructive = $plan.Destructive
            would_spawn_process = $false
        } | ConvertTo-Json -Depth 4 -Compress
        exit 0
    } catch {
        [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
        exit 1
    }
}

# ---------------------------------------------------------------- GUI
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# Golden Default palette (bound saipen UI.md; mirrors SAITULS.cs Palette).
$C_BG      = [System.Drawing.Color]::FromArgb(0x1A,0x18,0x10)
$C_SURFACE = [System.Drawing.Color]::FromArgb(0x33,0x2E,0x22)
$C_RAISED  = [System.Drawing.Color]::FromArgb(0x3D,0x37,0x2A)
$C_BDARK   = [System.Drawing.Color]::FromArgb(0x10,0x0E,0x08)
$C_TEXT    = [System.Drawing.Color]::FromArgb(0xD4,0xC8,0x9A)
$C_TEXT2   = [System.Drawing.Color]::FromArgb(0x9C,0x93,0x71)
$C_MUTED   = [System.Drawing.Color]::FromArgb(0x6E,0x67,0x4E)
$C_DANGER  = [System.Drawing.Color]::FromArgb(0xD6,0x64,0x64)
$C_WARN    = [System.Drawing.Color]::FromArgb(0x7A,0x7A,0x20)

$form = New-Object System.Windows.Forms.Form
$form.Text = "SCENARIOS"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.StartPosition = "CenterScreen"
$form.ClientSize = New-Object System.Drawing.Size(520, 430)
$form.BackColor = $C_BG
$form.Font = New-Object System.Drawing.Font("Verdana", 8)
$form.KeyPreview = $true

function New-Label {
    param([string]$Text, [int]$X, [int]$Y, [System.Drawing.Color]$Color, [int]$Size = 8)
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $Text; $l.Left = $X; $l.Top = $Y; $l.AutoSize = $true
    $l.ForeColor = $Color; $l.BackColor = $C_BG
    $l.Font = New-Object System.Drawing.Font("Verdana", $Size)
    $form.Controls.Add($l); return $l
}

New-Label "SCENARIOS" 12 10 $C_TEXT 10

New-Label "Category:" 12 36 $C_TEXT2
$cats = @("all") + @($registry.categories | Where-Object { $_ -ne "all" })
$cmbCat = New-Object System.Windows.Forms.ComboBox
$cmbCat.DropDownStyle = "DropDownList"
$cmbCat.Left = 80; $cmbCat.Top = 33; $cmbCat.Width = 110
foreach ($c in $cats) { [void]$cmbCat.Items.Add($c) }
$cmbCat.SelectedIndex = 0
$form.Controls.Add($cmbCat)

New-Label "Search:" 210 36 $C_TEXT2
$txtSearch = New-Object System.Windows.Forms.TextBox
$txtSearch.Left = 268; $txtSearch.Top = 33; $txtSearch.Width = 236
$form.Controls.Add($txtSearch)

$lvlList = New-Object System.Windows.Forms.ListBox
$lvlList.Left = 12; $lvlList.Top = 60; $lvlList.Width = 492; $lvlList.Height = 170
$lvlList.BackColor = $C_SURFACE; $lvlList.ForeColor = $C_TEXT
$lvlList.BorderStyle = "FixedSingle"
$form.Controls.Add($lvlList)

$lblDetail = New-Object System.Windows.Forms.Label
$lblDetail.Left = 12; $lblDetail.Top = 240; $lblDetail.Width = 492; $lblDetail.Height = 96
$lblDetail.ForeColor = $C_TEXT2; $lblDetail.BackColor = $C_BG
$lblDetail.Font = New-Object System.Drawing.Font("Verdana", 8)
$form.Controls.Add($lblDetail)

$lblBadges = New-Object System.Windows.Forms.Label
$lblBadges.Left = 12; $lblBadges.Top = 336; $lblBadges.Width = 492; $lblBadges.Height = 18
$lblBadges.ForeColor = $C_WARN; $lblBadges.BackColor = $C_BG
$form.Controls.Add($lblBadges)

function New-Button {
    param([string]$Text, [int]$X, [int]$Y, [int]$W, [System.Windows.Forms.Keys]$Acc = 0)
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $Text; $b.Left = $X; $b.Top = $Y; $b.Width = $W; $b.Height = 26
    $b.BackColor = $C_RAISED; $b.ForeColor = $C_TEXT
    $b.FlatStyle = "Flat"; $b.FlatAppearance.BorderColor = $C_BDARK
    $form.Controls.Add($b); return $b
}

$btnRun   = New-Button "Run"   12 368 100
$btnOpen  = New-Button "Open folder" 120 368 120
$btnClose = New-Button "Close" 248 368 90

$script:current = $null

function Get-BadgeText {
    param([object]$Sc)
    $parts = @()
    if (-not ($Sc.PSObject.Properties["enabled"] -and -not $Sc.enabled)) {
        foreach ($b in @($Sc.badges)) { $parts += "[$b]" }
    } else {
        $parts += "[DISABLED]"
    }
    return ($parts -join " ")
}

function Show-Selected {
    $sc = $script:current
    if ($null -eq $sc) { $lblDetail.Text = ""; $lblBadges.Text = ""; return }
    $blocked = ($sc.PSObject.Properties["enabled"] -and -not $sc.enabled)
    $deps = (@($sc.dependencies) | Where-Object { $_ -notmatch '^blocked:' }) -join ", "
    if (-not $deps) { $deps = "none" }
    $lines = @()
    $lines += $sc.description
    $lines += "Target: $($sc.target_mode)   Dependencies: $deps"
    if ($blocked) { $lines += "BLOCKED: $($sc.blocked_reason)" }
    $lblDetail.Text = ($lines -join "`n")
    $lblBadges.Text = Get-BadgeText -Sc $sc
    if ($sc.destructive) { $lblBadges.ForeColor = $C_DANGER } else { $lblBadges.ForeColor = $C_WARN }
}

function Refresh-List {
    $lvlList.BeginUpdate()
    $lvlList.Items.Clear()
    $script:visible = @()
    $cat = $cmbCat.SelectedItem
    $q = $txtSearch.Text.Trim().ToLowerInvariant()
    foreach ($sc in $registry.scenarios) {
        if ($cat -ne "all" -and $sc.category -ne $cat) { continue }
        if ($q -and -not (($sc.label + " " + $sc.description + " " + $sc.category).ToLowerInvariant().Contains($q))) { continue }
        $flag = ""; if ($sc.PSObject.Properties["enabled"] -and -not $sc.enabled) { $flag = "  [disabled]" }
        $badges = (@($sc.badges) | Select-Object -First 1)
        [void]$lvlList.Items.Add("$($sc.label)$flag  $badges")
        $script:visible += $sc
    }
    $lvlList.EndUpdate()
    if ($lvlList.Items.Count -gt 0) { $lvlList.SelectedIndex = 0 } else { $script:current = $null }
    Show-Selected
}

$cmbCat.Add_SelectedIndexChanged({ Refresh-List })
$txtSearch.Add_TextChanged({ Refresh-List })
$lvlList.Add_SelectedIndexChanged({
    if ($lvlList.SelectedIndex -ge 0) { $script:current = $script:visible[$lvlList.SelectedIndex] }
    Show-Selected
})

function Invoke-Scenario {
    param([object]$Sc)
    if ($null -eq $Sc) { return }
    $blocked = ($Sc.PSObject.Properties["enabled"] -and -not $Sc.enabled)
    if ($blocked) {
        [System.Windows.Forms.MessageBox]::Show("This scenario is disabled.`n`nReason: $($Sc.blocked_reason)", "SCENARIOS",
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
        return
    }
    $blocker = Get-DependencyBlocker -Sc $Sc
    if ($blocker) {
        [System.Windows.Forms.MessageBox]::Show("This scenario cannot run.`n`n$blocker", "SCENARIOS",
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
        return
    }
    $target = ""
    if ($Sc.target_mode -eq "folder") {
        $fb = New-Object System.Windows.Forms.FolderBrowserDialog
        $fb.Description = "$($Sc.label) - choose the folder to operate on"
        if ($fb.ShowDialog($form) -ne [System.Windows.Forms.DialogResult]::OK) { return }
        $target = $fb.SelectedPath
    }
    elseif ($Sc.target_mode -eq "file") {
        $of = New-Object System.Windows.Forms.OpenFileDialog
        $of.Title = "$($Sc.label) - choose the file"
        if ($of.ShowDialog($form) -ne [System.Windows.Forms.DialogResult]::OK) { return }
        $target = $of.FileName
    }
    # Destructive confirmation: explicit, never auto-confirmed (append K).
    if ($Sc.destructive) {
        $msg = "Scenario: $($Sc.label)`n" +
               "Action category: DESTRUCTIVE`n" +
               "Admin required: $(if ($Sc.requires_admin) { 'YES' } else { 'no' })`n" +
               "Scope: $(if ($target) { $target } else { 'as configured inside the script' })`n`n" +
               "This action modifies or deletes files. Continue?"
        $r = [System.Windows.Forms.MessageBox]::Show($msg, "Confirm destructive action",
            [System.Windows.Forms.MessageBoxButtons]::OKCancel, [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($r -ne [System.Windows.Forms.DialogResult]::OK) { return }
    }
    try {
        $plan = Build-LaunchPlan -Sc $Sc -TargetPath $target
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "SCENARIOS",
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
        return
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $plan.Exe
    $psi.Arguments = ConvertTo-Win32CommandLine -Argv $plan.Argv
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $false
    if ($plan.RequiresAdmin) {
        $psi.UseShellExecute = $true
        $psi.Verb = "runas"    # per-action elevation only; the launcher stays non-elevated
    }
    try {
        [System.Diagnostics.Process]::Start($psi) | Out-Null
    } catch [System.ComponentModel.Win32Exception] {
        # UAC declined or spawn refused: visible, not fatal.
    } catch {
        [System.Windows.Forms.MessageBox]::Show("Could not start scenario: $($_.Exception.Message)", "SCENARIOS",
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    }
}

$btnRun.Add_Click({ Invoke-Scenario -Sc $script:current })
$btnClose.Add_Click({ $form.Close() })
$btnOpen.Add_Click({
    if ($script:current) {
        $dir = Split-Path -Parent (Join-Path $here $script:current.script)
        if (Test-Path -LiteralPath $dir) { Start-Process explorer.exe -ArgumentList "`"$dir`"" }
    }
})
$form.Add_KeyDown({
    if ($_.KeyCode -eq "Escape") { $form.Close() }
})

Refresh-List
[void]$form.ShowDialog()
