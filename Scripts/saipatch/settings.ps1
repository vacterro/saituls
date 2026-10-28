<#
.SYNOPSIS
    SAIPATCH completion-sound settings (T-162).

.DESCRIPTION
    Small compact WinForms window following the UI.md Golden Default:
    Verdana, flat fills, square corners, two-pixel bevels, no animation.

    Editing writes %LOCALAPPDATA%\SAITULS\SAIPATCH\opencode-queue-mode.settings.json
    atomically (temp + rename). The running OpenCode plugin picks the new
    settings up on its next TUI start; no patch reapply is needed.

    Mandatory DONE safety conditions (successful native completion, queue
    drained, no pending human turn) are NOT configurable -- they are shown
    as locked informational checks.

    Test Sound plays the selected WAV at the CURRENT UNSAVED volume and does
    not consume a completion episode.

    Exit codes: 0 = saved or cancelled, nonzero = launch failure.
#>
param(
    [string]$OpenCodeRoot
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$SettingsDir = Join-Path $env:LOCALAPPDATA 'SAITULS\SAIPATCH'
$SettingsPath = Join-Path $SettingsDir 'opencode-queue-mode.settings.json'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
# Canonical WAV sources live directly in the SAIPATCH root (settings.ps1 sits
# next to them), NOT under patches\...\sounds.
$SoundDir = $ScriptRoot

$SoundNames = @('PICKUP01.wav','PICKUP02.wav','PICKUP03.wav','PICKUP04.wav','PICKUP05.wav','PICKUP06.wav','PICKUP07.wav')

# Palette: UI.md Golden Default
$BG        = [System.Drawing.Color]::FromArgb(212, 208, 200)
$BG_DARK   = [System.Drawing.Color]::FromArgb(160, 156, 148)
$TEXT      = [System.Drawing.Color]::FromArgb(0, 0, 0)
$TEXT2     = [System.Drawing.Color]::FromArgb(64, 64, 64)
$ACCENT    = [System.Drawing.Color]::FromArgb(179, 90, 31)
$FACE      = [System.Drawing.Color]::FromArgb(192, 192, 192)

function Read-Settings {
    try {
        $raw = Get-Content -Raw -LiteralPath $SettingsPath | ConvertFrom-Json
    } catch {
        return @{ enabled = $false; volume = 70; mode = 'random'; disabledSounds = @(); minimumRunSeconds = 0; attention = @{ visualPulseEnabled = $true; taskbarFlashEnabled = $true; pulseCount = 4; taskbarFlashCount = 5; triggerOnDone = $true; triggerOnNeedsHuman = $true; onlyWhenUnfocused = $false }; autoContinue = @{ enabled = $true; maxAttempts = 3 } }
    }
    $att = $raw.attention
    if ($null -eq $att) { $att = @{} }
    $acc = $raw.autoContinue
    if ($null -eq $acc) { $acc = @{} }
    $clamp = { param($v,$lo,$hi,$d); if ($null -eq $v) { return $d }; $n = [int]$v; if ($n -lt $lo) { $lo } elseif ($n -gt $hi) { $hi } else { $n } }
    return @{
        enabled           = ($raw.enabled -eq $true)
        volume            = [Math]::Min(100, [Math]::Max(0, [int]$raw.volume))
        mode              = if ($raw.mode -in @('random','ordered')) { [string]$raw.mode } else { 'random' }
        disabledSounds    = @($raw.disabledSounds | Where-Object { $_ -is [string] })
        minimumRunSeconds = [Math]::Max(0, [int]($raw.minimumRunSeconds | ForEach-Object { if ($null -ne $_) { $_ } else { 0 } }))
        attention         = @{
            visualPulseEnabled = ($att.visualPulseEnabled -ne $false)
            taskbarFlashEnabled = ($att.taskbarFlashEnabled -ne $false)
            pulseCount         = (& $clamp $att.pulseCount 1 10 4)
            taskbarFlashCount  = (& $clamp $att.taskbarFlashCount 1 20 5)
            triggerOnDone      = ($att.triggerOnDone -ne $false)
            triggerOnNeedsHuman= ($att.triggerOnNeedsHuman -ne $false)
            onlyWhenUnfocused  = ($att.onlyWhenUnfocused -eq $true)
        }
        autoContinue      = @{
            enabled     = ($acc.enabled -ne $false)
            maxAttempts = (& $clamp $acc.maxAttempts 1 10 3)
        }
    }
}

function Write-Settings([hashtable]$S) {
    [void][IO.Directory]::CreateDirectory($SettingsDir)
    $obj = [ordered]@{
        enabled           = [bool]$S.enabled
        volume            = [int]$S.volume
        mode              = [string]$S.mode
        disabledSounds    = @($S.disabledSounds)
        minimumRunSeconds = [int]$S.minimumRunSeconds
        attention         = [ordered]@{
            visualPulseEnabled = [bool]$S.attention.visualPulseEnabled
            taskbarFlashEnabled = [bool]$S.attention.taskbarFlashEnabled
            pulseCount         = [int]$S.attention.pulseCount
            taskbarFlashCount  = [int]$S.attention.taskbarFlashCount
            triggerOnDone      = [bool]$S.attention.triggerOnDone
            triggerOnNeedsHuman= [bool]$S.attention.triggerOnNeedsHuman
            onlyWhenUnfocused  = [bool]$S.attention.onlyWhenUnfocused
        }
        autoContinue      = [ordered]@{
            enabled     = [bool]$S.autoContinue.enabled
            maxAttempts = [int]$S.autoContinue.maxAttempts
        }
    }
    $json = $obj | ConvertTo-Json -Depth 4
    $temp = "$SettingsPath.$PID.tmp"
    [IO.File]::WriteAllText($temp, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $SettingsPath -Force
}

$script:testPlayer = $null
function Invoke-TestSound([string]$Name, [int]$Volume) {
    # Preview: plays at the CURRENT UNSAVED volume, never mutates DONE state.
    # Nonblocking WMPlayer.OCX, the same mechanism as the plugin runtime
    # (saipatch-sound-runtime.js buildPlayCommand). The COM object is kept in
    # script scope so playback outlives the click handler and the form stays
    # responsive; a later preview replaces it.
    $file = Join-Path $SoundDir $Name
    if (-not (Test-Path -LiteralPath $file)) {
        [System.Windows.Forms.MessageBox]::Show("Sound asset missing:`n$file", 'SAIPATCH') | Out-Null
        return
    }
    try {
        $player = New-Object -ComObject WMPlayer.OCX
        $player.settings.volume = [Math]::Min(100, [Math]::Max(0, $Volume))
        $player.URL = $file
        $player.controls.play()
        $script:testPlayer = $player
    } catch {
        [System.Windows.Forms.MessageBox]::Show("Playback failed: $($_.Exception.Message)", 'SAIPATCH') | Out-Null
    }
}

$S = Read-Settings

$form                     = New-Object System.Windows.Forms.Form
$form.Text                = 'OpenCode Settings - SAIPATCH'
$form.ClientSize          = New-Object System.Drawing.Size(420, 356)
$form.BackColor           = $BG
$form.FormBorderStyle     = 'FixedDialog'
$form.MaximizeBox         = $false
$form.MinimizeBox         = $false
$form.StartPosition       = 'CenterScreen'
$form.Font                = New-Object System.Drawing.Font('Verdana', 8.25)

$y = 12

$lblEnable = New-Object System.Windows.Forms.Label
$lblEnable.Text = 'Completion sound'
$lblEnable.SetBounds(12, ($y + 3), 130, 18)
$lblEnable.BackColor = $BG
$lblEnable.ForeColor = $TEXT
$form.Controls.Add($lblEnable)

$chkEnable = New-Object System.Windows.Forms.CheckBox
$chkEnable.Text = 'Enabled'
$chkEnable.SetBounds(150, $y, 90, 22)
$chkEnable.BackColor = $BG
$chkEnable.Checked = $S.enabled
$chkEnable.FlatStyle = 'Flat'
$form.Controls.Add($chkEnable)

$y += 30

$lblMode = New-Object System.Windows.Forms.Label
$lblMode.Text = 'Playback'
$lblMode.SetBounds(12, ($y + 3), 60, 18)
$lblMode.BackColor = $BG
$form.Controls.Add($lblMode)

$radRandom = New-Object System.Windows.Forms.RadioButton
$radRandom.Text = 'Random'
$radRandom.SetBounds(80, $y, 80, 22)
$radRandom.BackColor = $BG
$radRandom.Checked = ($S.mode -eq 'random')
$radRandom.FlatStyle = 'Flat'
$form.Controls.Add($radRandom)

$radOrdered = New-Object System.Windows.Forms.RadioButton
$radOrdered.Text = 'In order'
$radOrdered.SetBounds(165, $y, 80, 22)
$radOrdered.BackColor = $BG
$radOrdered.Checked = ($S.mode -eq 'ordered')
$radOrdered.FlatStyle = 'Flat'
$form.Controls.Add($radOrdered)

$y += 32

$lblVol = New-Object System.Windows.Forms.Label
$lblVol.Text = 'Volume'
$lblVol.SetBounds(12, ($y + 4), 60, 18)
$lblVol.BackColor = $BG
$form.Controls.Add($lblVol)

$numVol = New-Object System.Windows.Forms.NumericUpDown
$numVol.SetBounds(80, $y, 60, 22)
$numVol.Minimum = 0
$numVol.Maximum = 100
$numVol.Value = $S.volume
$form.Controls.Add($numVol)

$lblVolNote = New-Object System.Windows.Forms.Label
$lblVolNote.Text = '0..100'
$lblVolNote.SetBounds(148, ($y + 4), 60, 18)
$lblVolNote.BackColor = $BG
$lblVolNote.ForeColor = $TEXT2
$form.Controls.Add($lblVolNote)

$y += 32

$lblRun = New-Object System.Windows.Forms.Label
$lblRun.Text = 'Minimum runtime seconds'
$lblRun.SetBounds(12, ($y + 4), 160, 18)
$lblRun.BackColor = $BG
$form.Controls.Add($lblRun)

$numRun = New-Object System.Windows.Forms.NumericUpDown
$numRun.SetBounds(180, $y, 60, 22)
$numRun.Minimum = 0
$numRun.Maximum = 3600
$numRun.Value = $S.minimumRunSeconds
$form.Controls.Add($numRun)

$y += 34

$lblSounds = New-Object System.Windows.Forms.Label
$lblSounds.Text = 'Sounds'
$lblSounds.SetBounds(12, $y, 80, 18)
$lblSounds.BackColor = $BG
$form.Controls.Add($lblSounds)

$y += 22
$soundChecks = @()
foreach ($name in $SoundNames) {
    $chk = New-Object System.Windows.Forms.CheckBox
    $chk.Text = $name
    $chk.SetBounds(24, $y, 160, 20)
    $chk.BackColor = $BG
    $chk.Checked = ($S.disabledSounds -notcontains $name)
    $chk.FlatStyle = 'Flat'
    $form.Controls.Add($chk)
    $soundChecks += ,@($name, $chk)
    $y += 22
}

$y += 4
$lblLocked = New-Object System.Windows.Forms.Label
$lblLocked.Text = "Locked conditions: successful native completion; queue drained; no human request pending."
$lblLocked.SetBounds(12, $y, 396, 30)
$lblLocked.BackColor = $BG
$lblLocked.ForeColor = $TEXT2
$form.Controls.Add($lblLocked)

$y += 34
$grpAtt = New-Object System.Windows.Forms.GroupBox
$grpAtt.Text = 'Attention'
$grpAtt.SetBounds(8, $y, 404, 132)
$grpAtt.BackColor = $BG
$form.Controls.Add($grpAtt)
$chkPulseVis = New-Object System.Windows.Forms.CheckBox
$chkPulseVis.Text = 'Visual pulse'
$chkPulseVis.SetBounds(12, 20, 130, 20)
$chkPulseVis.BackColor = $BG
$chkPulseVis.Checked = $S.attention.visualPulseEnabled
$grpAtt.Controls.Add($chkPulseVis)
$chkFlashTask = New-Object System.Windows.Forms.CheckBox
$chkFlashTask.Text = 'Flash taskbar when unfocused'
$chkFlashTask.SetBounds(160, 20, 180, 20)
$chkFlashTask.BackColor = $BG
$chkFlashTask.Checked = $S.attention.taskbarFlashEnabled
$grpAtt.Controls.Add($chkFlashTask)
$lblPC = New-Object System.Windows.Forms.Label
$lblPC.Text = 'Pulse count'
$lblPC.SetBounds(12, 48, 80, 18)
$lblPC.BackColor = $BG
$grpAtt.Controls.Add($lblPC)
$numPC = New-Object System.Windows.Forms.NumericUpDown
$numPC.SetBounds(100, 46, 50, 20)
$numPC.Minimum = 1; $numPC.Maximum = 10
$numPC.Value = $S.attention.pulseCount
$grpAtt.Controls.Add($numPC)
$lblFC = New-Object System.Windows.Forms.Label
$lblFC.Text = 'Taskbar flashes'
$lblFC.SetBounds(160, 48, 110, 18)
$lblFC.BackColor = $BG
$grpAtt.Controls.Add($lblFC)
$numFC = New-Object System.Windows.Forms.NumericUpDown
$numFC.SetBounds(276, 46, 50, 20)
$numFC.Minimum = 1; $numFC.Maximum = 20
$numFC.Value = $S.attention.taskbarFlashCount
$grpAtt.Controls.Add($numFC)
$chkUndone = New-Object System.Windows.Forms.CheckBox
$chkUndone.Text = 'Trigger: Task completed (DONE)'
$chkUndone.SetBounds(12, 76, 200, 20)
$chkUndone.BackColor = $BG
$chkUndone.Checked = $S.attention.triggerOnDone
$grpAtt.Controls.Add($chkUndone)
$chkNeed = New-Object System.Windows.Forms.CheckBox
$chkNeed.Text = 'Trigger: Needs human'
$chkNeed.SetBounds(12, 100, 180, 20)
$chkNeed.BackColor = $BG
$chkNeed.Checked = $S.attention.triggerOnNeedsHuman
$grpAtt.Controls.Add($chkNeed)
$chkOnlyUnfocused = New-Object System.Windows.Forms.CheckBox
$chkOnlyUnfocused.Text = 'Only when unfocused'
$chkOnlyUnfocused.SetBounds(210, 100, 150, 20)
$chkOnlyUnfocused.BackColor = $BG
$chkOnlyUnfocused.Checked = $S.attention.onlyWhenUnfocused
$grpAtt.Controls.Add($chkOnlyUnfocused)

$y += 140

# T-144 auto-cc recovery: minimal queue settings (extend the SAME settings
# schema; deliberately narrow -- no rules editor).
$grpAcc = New-Object System.Windows.Forms.GroupBox
$grpAcc.Text = 'Auto-continue transient stalls'
$grpAcc.SetBounds(8, $y, 404, 56)
$grpAcc.BackColor = $BG
$form.Controls.Add($grpAcc)
$chkAcc = New-Object System.Windows.Forms.CheckBox
$chkAcc.Text = 'Enabled (auto submit cc after recoverable timeout)'
$chkAcc.SetBounds(12, 18, 300, 20)
$chkAcc.BackColor = $BG
$chkAcc.Checked = $S.autoContinue.enabled
$grpAcc.Controls.Add($chkAcc)
$lblAccMax = New-Object System.Windows.Forms.Label
$lblAccMax.Text = 'Max automatic cc attempts'
$lblAccMax.SetBounds(12, 38, 160, 18)
$lblAccMax.BackColor = $BG
$grpAcc.Controls.Add($lblAccMax)
$numAccMax = New-Object System.Windows.Forms.NumericUpDown
$numAccMax.SetBounds(180, 36, 50, 20)
$numAccMax.Minimum = 1; $numAccMax.Maximum = 10
$numAccMax.Value = $S.autoContinue.maxAttempts
$grpAcc.Controls.Add($numAccMax)

$y += 64

# Resize form to fit new section
$form.ClientSize = New-Object System.Drawing.Size(420, ($y + 44))

$btnTest = New-Object System.Windows.Forms.Button
$btnTest.Text = 'Test Sound'
$btnTest.SetBounds(12, $y, 110, 28)
$btnTest.BackColor = $FACE
$btnTest.FlatStyle = 'Flat'
$btnTest.Add_Click({
    $sel = $soundChecks | Where-Object { $_[1].Checked } | Select-Object -First 1
    if (-not $sel) { return }
    Invoke-TestSound -Name $sel[0] -Volume ([int]$numVol.Value)
})
$form.Controls.Add($btnTest)

$btnSave = New-Object System.Windows.Forms.Button
$btnSave.Text = 'Save'
$btnSave.SetBounds(216, $y, 90, 28)
$btnSave.BackColor = $FACE
$btnSave.FlatStyle = 'Flat'
$btnSave.Add_Click({
    $disabled = @()
    foreach ($entry in $soundChecks) {
        if (-not $entry[1].Checked) { $disabled += $entry[0] }
    }
    Write-Settings @{
        enabled           = $chkEnable.Checked
        volume            = [int]$numVol.Value
        mode              = if ($radOrdered.Checked) { 'ordered' } else { 'random' }
        disabledSounds    = $disabled
        minimumRunSeconds = [int]$numRun.Value
        attention         = @{
            visualPulseEnabled = $chkPulseVis.Checked
            taskbarFlashEnabled = $chkFlashTask.Checked
            pulseCount         = [int]$numPC.Value
            taskbarFlashCount  = [int]$numFC.Value
            triggerOnDone      = $chkUndone.Checked
            triggerOnNeedsHuman= $chkNeed.Checked
            onlyWhenUnfocused  = $chkOnlyUnfocused.Checked
        }
        autoContinue      = @{
            enabled     = $chkAcc.Checked
            maxAttempts = [int]$numAccMax.Value
        }
    }
    $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Close()
})
$form.Controls.Add($btnSave)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = 'Cancel'
$btnCancel.SetBounds(314, $y, 90, 28)
$btnCancel.BackColor = $FACE
$btnCancel.FlatStyle = 'Flat'
$btnCancel.Add_Click({ $form.Close() })
$form.Controls.Add($btnCancel)

$form.AcceptButton = $btnSave
$form.CancelButton = $btnCancel

[void]$form.ShowDialog()
exit 0
