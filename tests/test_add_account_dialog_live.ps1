# Live acceptance for the Add Account dialog (T-244).
#
# This loads the SHIPPED SAITULS.exe, not the source tree, and asks it for its
# own dialog. That distinction is the whole point: the hermetic harness compiles
# SAITULS.cs and proves the logic, while this proves the binary the operator
# actually double-clicks carries the catalog and no light control. A source
# check could stay green while the built exe stayed stale.
#
# Read-only. It constructs the dialog, reads it, and disposes it: no account is
# created, no engine is contacted, no registry is touched.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$exe = Join-Path $root 'SAITULS.exe'
if (-not (Test-Path $exe)) { Write-Host 'FAIL  SAITULS.exe not built'; exit 1 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$asm = [System.Reflection.Assembly]::LoadFrom($exe)
$form = $asm.GetType('Saituls.SaitulsForm')
if (-not $form) { Write-Host 'FAIL  Saituls.SaitulsForm not found in the built exe'; exit 1 }

$builder = $form.GetMethod('BuildAddAccountDialog',
    [System.Reflection.BindingFlags]'Public,Static')
if (-not $builder) { Write-Host 'FAIL  the built exe has no BuildAddAccountDialog -- stale binary'; exit 1 }

# The four widgets come back through out parameters, and PowerShell wraps a
# [ref] in PSReference, which reflection will not convert to ComboBox&. One
# three-line C# shim does the invoke; everything after this stays in PowerShell.
Add-Type -ReferencedAssemblies 'System.Windows.Forms.dll', 'System.Drawing.dll' -TypeDefinition @'
using System;
using System.Drawing;
using System.Reflection;
using System.Windows.Forms;
public static class DialogProbe {
    public static Form Build(MethodInfo m,
        out ComboBox combo, out TextBox alias, out Label note, out Button ok)
    {
        var args = new object[] { null, null, null, null };
        Form f = (Form)m.Invoke(null, args);
        combo = (ComboBox)args[0]; alias = (TextBox)args[1];
        note = (Label)args[2]; ok = (Button)args[3];
        return f;
    }
    // Same reason: a Graphics handed across the PowerShell boundary arrives
    // wrapped, and reflection cannot unwrap it. Render here, return a colour.
    public static Color PaintRowPixel(MethodInfo m, int w, int h)
    {
        using (var bmp = new Bitmap(w, h))
        using (var g = Graphics.FromImage(bmp))
        using (var font = new Font("Verdana", 8.25f))
        {
            m.Invoke(null, new object[] { g, new Rectangle(0, 0, w, h),
                "Antigravity (Google)", "Available", true, false, font });
            return bmp.GetPixel(3, 3);
        }
    }
}
'@

[System.Windows.Forms.ComboBox] $combo = $null
[System.Windows.Forms.TextBox] $alias = $null
[System.Windows.Forms.Label] $note = $null
[System.Windows.Forms.Button] $ok = $null
$dlg = [DialogProbe]::Build($builder, [ref]$combo, [ref]$alias, [ref]$note, [ref]$ok)

$pass = 0; $fail = 0
function Check($ok, $what, $detail) {
    if ($ok) { Write-Host "PASS  $what"; $script:pass++ }
    else { Write-Host "FAIL  $what  -- $detail"; $script:fail++ }
}

function Lum([System.Drawing.Color]$c) {
    function Ch([int]$v) { $s = $v / 255.0
        if ($s -le 0.03928) { return $s / 12.92 } else { return [Math]::Pow(($s + 0.055) / 1.055, 2.4) } }
    return 0.2126 * (Ch $c.R) + 0.7152 * (Ch $c.G) + 0.0722 * (Ch $c.B)
}

$items = @($combo.Items | ForEach-Object { [string]$_ })

# A -- the catalog is there, in the built binary, with ZCode in it.
Check ($items.Count -ge 7) "A1 the built dialog lists the full provider catalog ($($items.Count) entries)" ($items -join ' | ')
Check (($items -join ' ') -match 'ZCode') "A2 ZCode is visible in the built dialog" ($items -join ' | ')

# B -- a planned provider is honest and the button is off.
$z = [Array]::IndexOf($items, ($items | Where-Object { $_ -match '^ZCode' } | Select-Object -First 1))
if ($z -ge 0) {
    $combo.SelectedIndex = $z
    Check ($note.Text -match 'pending|not|planned') "B1 selecting ZCode explains why it cannot be created yet" "note='$($note.Text)'"
    Check (-not $ok.Enabled) "B2 and the Add button is disabled for it" "enabled=$($ok.Enabled)"
    Check (-not $alias.Enabled) "B3 and the alias field is disabled too" "enabled=$($alias.Enabled)"
} else {
    Check $false "B1 ZCode row present" 'not found'
}

# C -- an available provider shows help text and can be created.
$a = [Array]::IndexOf($items, ($items | Where-Object { $_ -match '^Antigravity' } | Select-Object -First 1))
if ($a -ge 0) {
    $combo.SelectedIndex = $a
    Check ($note.Text -match 'Google') "C1 selecting Antigravity describes the browser sign-in" "note='$($note.Text)'"
    Check ($ok.Enabled) "C2 and its Add button is enabled" "enabled=$($ok.Enabled)"
} else {
    Check $false "C1 Antigravity row present" 'not found'
}

# D -- the closed state is themed, not system-drawn.
Check ($combo.DrawMode -eq 'OwnerDrawFixed' -and $combo.FlatStyle -eq 'Flat') `
    "D1 the closed provider field is owner-drawn and flat" "draw=$($combo.DrawMode) flat=$($combo.FlatStyle)"
Check ((Lum $combo.BackColor) -lt 0.20) "D2 its background is dark" "back=$($combo.BackColor)"

# E -- nothing else in the dialog leaks a light rectangle.
$light = @()
foreach ($c in $dlg.Controls) {
    if ($c.BackColor.A -ne 0 -and (Lum $c.BackColor) -ge 0.20) { $light += ("$($c.GetType().Name)=$($c.BackColor)") }
}
Check ($light.Count -eq 0) "E1 no control in the dialog paints a light background" ($light -join ' ')

# F -- the painted list row is dark too, read off real pixels.
$row = $form.GetMethod('PaintProviderRow', [System.Reflection.BindingFlags]'Public,Static')
if ($row) {
    $px = [DialogProbe]::PaintRowPixel($row, 360, 22)
    Check ((Lum $px) -lt 0.20) "F1 the painted provider row is dark, selected state included" "pixel=$px"
} else {
    Check $false "F1 PaintProviderRow exists in the built exe" 'method missing -- stale binary'
}

$dlg.Dispose()
Write-Host ''
Write-Host "passed $pass, failed $fail"
if ($fail -gt 0) { exit 1 }
exit 0