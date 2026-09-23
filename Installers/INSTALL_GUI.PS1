# Self-elevate: reg import into HKLM/HKCR silently fails without admin rights
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    # -Wait -PassThru -ErrorAction Stop and the child's own exit code: without
    # them the parent returned 0 the moment the elevated child STARTED, so a
    # dismissed UAC prompt or a failed import was indistinguishable from a
    # completed install to anything calling this script.
    try {
        $elevated = Start-Process powershell.exe -ArgumentList @('-ExecutionPolicy','Bypass','-NoProfile','-WindowStyle','Hidden','-File',"`"$($MyInvocation.MyCommand.Definition)`"") -Verb RunAs -Wait -PassThru -ErrorAction Stop
    } catch {
        Write-Host "Elevation was cancelled. Nothing was changed." -ForegroundColor Yellow
        exit 1
    }
    # A process object with no ExitCode is counted with a missing one: `exit
    # $null` is `exit 0`, which is the very "success" this guard exists to stop.
    if (-not $elevated -or $null -eq $elevated.ExitCode) {
        Write-Host "Elevation did not complete. Nothing was changed." -ForegroundColor Yellow
        exit 1
    }
    exit $elevated.ExitCode
}

# Shared cross-process writer contract (T-133 6.1): the GUI holds NO mutation
# lock while idle. Each Install/Uninstall click takes the one named lock for
# exactly one bounded transaction and releases it in finally, so a user staring
# at the form blocks no other registry writer. BUSY -> clear message, zero mutation.
# The action returns its failure list; the wrapper returns @{Busy;Failures} so a
# BUSY result is distinguishable from a clean run that collected zero failures.
function Invoke-RegistryTransaction {
    param([scriptblock]$Action)
    $m = New-Object Threading.Mutex($false, 'Global\SAITULS_REGISTRY_MUTATION')
    $owned = $false
    try {
        try { $owned = $m.WaitOne(5000) }
        catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) {
            [System.Windows.Forms.MessageBox]::Show(
                "SAITULS registry mutation is already in progress in another process. Try again in a moment.",
                "Busy", 0, [System.Windows.Forms.MessageBoxIcon]::Warning)
            return @{ Busy = $true; Failures = @() }
        }
        return @{ Busy = $false; Failures = @(& $Action) }
    } finally {
        if ($owned) { $m.ReleaseMutex() }
        $m.Dispose()
    }
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$rootDir = (Get-Item $scriptDir).Parent.FullName
$registryDir = Join-Path $rootDir "Registry"

# T-034: same %%ROOT%% substitution as INSTALL_ALL.PS1 -- regs are
# relocation-proof, real root is filled in here at import time.
function Import-RegWithRoot([string]$path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $text = [System.Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
        $encoding = 'utf16'
    } else {
        $text = [System.Text.Encoding]::Default.GetString($bytes)
        $encoding = 'ansi'
    }
    $text = $text.Replace('%%ROOT%%', $rootDir.Replace('\', '\\'))
    $tmp = Join-Path $env:TEMP ("ctxmenu_" + [System.IO.Path]::GetRandomFileName() + ".reg")
    if ($encoding -eq 'utf16') {
        [System.IO.File]::WriteAllBytes($tmp, [System.Text.Encoding]::Unicode.GetPreamble() + [System.Text.Encoding]::Unicode.GetBytes($text))
    } else {
        [System.IO.File]::WriteAllBytes($tmp, [System.Text.Encoding]::Default.GetBytes($text))
    }
    try {
        # reg.exe reports a rejected import only through its exit code, so
        # without -PassThru and this check a denied import was reported to the
        # user as "Successfully installed".
        $proc = Start-Process "reg.exe" -ArgumentList "import `"$tmp`"" -Wait -PassThru -WindowStyle Hidden
        if ($proc.ExitCode -ne 0) {
            throw "reg import failed with exit code $($proc.ExitCode) for $(Split-Path -Leaf $path)"
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

# One result dialog for a batch of imports: success only when every requested
# mutation actually completed, and the failures named so they can be retried.
# The count is also this script's exit code, so the elevated child reports the
# real result to whoever waited for it instead of a bare 0.
$script:failed = 0
function Show-BatchResult([string]$verb, [string[]]$failures, [int]$total) {
    if ($failures.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show(
            "Successfully $verb $total selected feature(s)!", "Success", 0,
            [System.Windows.Forms.MessageBoxIcon]::Information)
        return
    }
    $script:failed += $failures.Count
    $detail = ($failures | ForEach-Object { "  - $_" }) -join "`r`n"
    [System.Windows.Forms.MessageBox]::Show(
        "$($failures.Count) of $total operation(s) FAILED:`r`n`r`n$detail", "Failed", 0,
        [System.Windows.Forms.MessageBoxIcon]::Error)
}

# Get all base features (exclude _Remove files and the Legacy folder)
$features = Get-ChildItem -Path $registryDir -Filter "*.reg" -File | Where-Object { $_.Name -notmatch "_REM" -and $_.Name -notmatch "_UTF16" -and $_.Name -notmatch "_REWRITTEN" -and $_.Name -ne "FFMPEG.REG" }

$form = New-Object System.Windows.Forms.Form
$form.Text = "SAITULS Manager"
$form.Size = New-Object System.Drawing.Size(400,500)
$form.StartPosition = "CenterScreen"
$form.BackColor = [System.Drawing.Color]::FromArgb(32,32,32)
$form.ForeColor = [System.Drawing.Color]::White
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false

$title = New-Object System.Windows.Forms.Label
$title.Text = "Select features to install or remove:"
$title.Location = New-Object System.Drawing.Point(15,15)
$title.AutoSize = $true
$title.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($title)

$checkedListBox = New-Object System.Windows.Forms.CheckedListBox
$checkedListBox.Location = New-Object System.Drawing.Point(15, 45)
$checkedListBox.Size = New-Object System.Drawing.Size(355, 330)
$checkedListBox.BackColor = [System.Drawing.Color]::FromArgb(45,45,48)
$checkedListBox.ForeColor = [System.Drawing.Color]::White
$checkedListBox.CheckOnClick = $true
$checkedListBox.Font = New-Object System.Drawing.Font("Segoe UI", 10)

foreach ($f in $features) {
    [void]$checkedListBox.Items.Add($f.BaseName)
}
$form.Controls.Add($checkedListBox)

$btnInstall = New-Object System.Windows.Forms.Button
$btnInstall.Text = "Install Selected"
$btnInstall.Location = New-Object System.Drawing.Point(15, 390)
$btnInstall.Size = New-Object System.Drawing.Size(170, 40)
$btnInstall.BackColor = [System.Drawing.Color]::FromArgb(0, 122, 204)
$btnInstall.FlatStyle = 'Flat'
$btnInstall.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$btnInstall.Add_Click({
    $selected = $checkedListBox.CheckedItems
    if ($selected.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show("Please select at least one feature.", "Notice", 0, [System.Windows.Forms.MessageBoxIcon]::Information)
        return
    }
    # Preflight: a selected feature whose .reg is missing used to be a silent
    # skip, so the dialog claimed success for something that never happened.
    $missing = @()
    foreach ($item in $selected) {
        if (-not (Test-Path -LiteralPath (Join-Path $registryDir "$item.reg"))) { $missing += $item }
    }
    if ($missing.Count -gt 0) {
        $script:failed += $missing.Count
        [System.Windows.Forms.MessageBox]::Show(
            "Missing registry file(s); nothing was installed:`r`n`r`n" + (($missing | ForEach-Object { "  - $_.reg" }) -join "`r`n"),
            "Failed", 0, [System.Windows.Forms.MessageBoxIcon]::Error)
        return
    }
    $btnInstall.Enabled = $false
    $btnUninstall.Enabled = $false
    try {
        $result = Invoke-RegistryTransaction {
            $f = @()
            foreach ($item in $selected) {
                $regPath = Join-Path $registryDir "$item.reg"
                try { Import-RegWithRoot $regPath } catch { $f += "$item : $($_.Exception.Message)" }
            }
            $f
        }
    } finally {
        $btnInstall.Enabled = $true
        $btnUninstall.Enabled = $true
    }
    if ($result.Busy) { return }
    Show-BatchResult "installed" $result.Failures $selected.Count
})
$form.Controls.Add($btnInstall)

$btnUninstall = New-Object System.Windows.Forms.Button
$btnUninstall.Text = "Uninstall Selected"
$btnUninstall.Location = New-Object System.Drawing.Point(200, 390)
$btnUninstall.Size = New-Object System.Drawing.Size(170, 40)
$btnUninstall.BackColor = [System.Drawing.Color]::FromArgb(204, 0, 0)
$btnUninstall.FlatStyle = 'Flat'
$btnUninstall.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$btnUninstall.Add_Click({
    $selected = $checkedListBox.CheckedItems
    if ($selected.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show("Please select at least one feature.", "Notice", 0, [System.Windows.Forms.MessageBoxIcon]::Information)
        return
    }
    $missing = @()
    foreach ($item in $selected) {
        if (-not (Test-Path -LiteralPath (Join-Path $registryDir "${item}_REM.reg"))) { $missing += $item }
    }
    if ($missing.Count -gt 0) {
        $script:failed += $missing.Count
        [System.Windows.Forms.MessageBox]::Show(
            "Missing removal file(s); nothing was removed:`r`n`r`n" + (($missing | ForEach-Object { "  - ${_}_REM.reg" }) -join "`r`n"),
            "Failed", 0, [System.Windows.Forms.MessageBoxIcon]::Error)
        return
    }
    $btnInstall.Enabled = $false
    $btnUninstall.Enabled = $false
    try {
        $result = Invoke-RegistryTransaction {
            $f = @()
            foreach ($item in $selected) {
                $removeReg = Join-Path $registryDir "${item}_REM.reg"
                try { Import-RegWithRoot $removeReg } catch { $f += "$item : $($_.Exception.Message)" }
            }
            $f
        }
    } finally {
        $btnInstall.Enabled = $true
        $btnUninstall.Enabled = $true
    }
    if ($result.Busy) { return }
    Show-BatchResult "removed" $result.Failures $selected.Count
})
$form.Controls.Add($btnUninstall)

$form.ShowDialog() | Out-Null

# Terminal result, printed and returned by the elevated child only -- the
# unelevated parent above exits with this code, never with its own 0.
if ($script:failed -gt 0) {
    Write-Host "FAILED: $($script:failed) operation(s) did not complete." -ForegroundColor Red
    exit $script:failed
}
Write-Host "Done!" -ForegroundColor Green
exit 0
