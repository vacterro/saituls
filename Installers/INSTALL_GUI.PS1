# Self-elevate: reg import into HKLM/HKCR silently fails without admin rights
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell.exe -ArgumentList @('-ExecutionPolicy','Bypass','-NoProfile','-WindowStyle','Hidden','-File',"`"$($MyInvocation.MyCommand.Definition)`"") -Verb RunAs
    exit
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
        Start-Process "reg.exe" -ArgumentList "import `"$tmp`"" -Wait -WindowStyle Hidden
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
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
    foreach ($item in $selected) {
        $regPath = Join-Path $registryDir "$item.reg"
        Import-RegWithRoot $regPath
    }
    [System.Windows.Forms.MessageBox]::Show("Successfully installed selected features!", "Success", 0, [System.Windows.Forms.MessageBoxIcon]::Information)
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
    foreach ($item in $selected) {
        $removeReg = Join-Path $registryDir "${item}_REM.reg"
        if (Test-Path $removeReg) {
            Import-RegWithRoot $removeReg
        } else {
            Write-Warning "Removal file not found for $item"
        }
    }
    [System.Windows.Forms.MessageBox]::Show("Successfully removed selected features!", "Success", 0, [System.Windows.Forms.MessageBoxIcon]::Information)
})
$form.Controls.Add($btnUninstall)

$form.ShowDialog() | Out-Null
