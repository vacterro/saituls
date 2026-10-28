param(
    [string]$Source = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'SAITULS.cs')
)

# T-257 / Wave D: the Golden Default ZERO-WHITE contract for application-owned
# message surfaces.
#
# The old surface was System.Windows.Forms.MessageBox -- a light client area on
# a dark product, with a light flash before it painted. This audit holds two
# ends of the fix:
#
#   1. SOURCE: every application-owned call site now routes through the one
#      shared dialog, and the dialog maps exactly the button sets the shell
#      actually uses.
#   2. RENDERED: the dialog is really drawn, and its pixels contain NO light
#      surface at all (min(R,G,B) >= 200 anywhere is a failure) while still
#      showing its own dark chrome and buttons. A stock MessageBox renders
#      #F0F0F0, which this catches.
#
# Operating-system and vendor dialogs are NOT routed through the shared dialog
# and are not audited here: this verifies the surfaces SAITULS owns.

$ErrorActionPreference = 'Stop'
$failures = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

$root = Split-Path -Parent $Source
$ModuleDir = Join-Path $root 'FolderAutomation'
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { $csc = 'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $csc)) { Write-Host 'FAIL  csc.exe not found'; exit 1 }

$work = Join-Path ([IO.Path]::GetTempPath()) ("saituls_dialogs_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$dll = Join-Path $work 'SaitulsDialogsSubject.dll'

$module = @(Get-ChildItem -LiteralPath $ModuleDir -Filter '*.cs' -File |
    Sort-Object Name | ForEach-Object { $_.FullName })
& $csc -nologo -target:library -out:$dll -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll $Source @module | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL  subject does not compile: $Source + FolderAutomation"; exit 1 }

Add-Type -AssemblyName System.Drawing, System.Windows.Forms

# ---------------------------------------------------------------- source level
$text = [IO.File]::ReadAllText($Source)
$migrated = ([regex]::Matches($text, 'SaitulsDialog\.Show\(')).Count
$leftover = ([regex]::Matches($text, 'MessageBox\.Show\(')).Count

Check 'no application-owned MessageBox.Show call site is left' ($leftover -eq 0) "left=$leftover"
Check 'every app-owned call site routes through the shared dialog' ($migrated -ge 50) "migrated=$migrated"
Check 'the shared dialog is the one owned message surface' `
    ($text -cmatch 'internal static class SaitulsDialog' -and $text -cmatch 'internal sealed class SaitulsMessageDialog')

# The tray ContextMenuStrip was the last surface a stock WinForms renderer painted
# light. It must be built by the one themed factory, never hand-coloured inline.
$menuSites = ([regex]::Matches($text, 'new ContextMenuStrip\(')).Count
Check 'every owned popup menu is built by the themed factory' ($menuSites -eq 1) "inline=$menuSites"
Check 'the tray menu is that themed menu' ($text -cmatch 'ContextMenuStrip menu = SaitulsMenu\.Themed\(\);')
Check 'no popup menu delegates its colours to the system' (-not ($text -cmatch 'ToolStripRenderMode\.System'))
Check 'the themed renderer pins the drop-down surface and border' `
    ($text -cmatch 'class SaitulsMenuColors' -and $text -cmatch 'override Color ToolStripDropDownBackground' -and `
     $text -cmatch 'override Color ImageMarginGradientBegin' -and $text -cmatch 'override Color MenuBorder')

$sets = @([regex]::Matches($text, 'MessageBoxButtons\.(\w+)') |
    ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
$unmapped = @($sets | Where-Object { $_ -notin @('OK', 'OKCancel', 'YesNo') })
Check 'only OK / OKCancel / YesNo button sets are used' ($unmapped.Count -eq 0) ($sets -join ',')

# ------------------------------------------------------------- rendered level
$asm = [Reflection.Assembly]::LoadFrom($dll)
$dlgType = $asm.GetType('Saituls.SaitulsMessageDialog', $true)
$anyCtor = [Reflection.BindingFlags]'Instance,Public,NonPublic'
$ctor = $dlgType.GetConstructors($anyCtor)[0]
$acceptField = $dlgType.GetField('AcceptResult', $anyCtor)
$declineField = $dlgType.GetField('DeclineResult', $anyCtor)
$messageProp = $dlgType.GetProperty('MessageText', $anyCtor)
$hasDeclineProp = $dlgType.GetProperty('HasDeclineButton', $anyCtor)

$MB = [System.Windows.Forms.MessageBoxButtons]
$MI = [System.Windows.Forms.MessageBoxIcon]

function NewDialog([string]$body, [string]$caption, $buttons, $icon) {
    return $ctor.Invoke([object[]]@($body, $caption, $buttons, $icon))
}

function RenderStats([object]$dialog) {
    # A Form that has never been shown has no child handles, so DrawToBitmap
    # paints only its own background -- the dialog would be audited as if it had
    # no text and no buttons. It is therefore shown at an off-screen coordinate
    # (ShowInTaskbar is false, so it never appears anywhere the operator can see),
    # rendered, and hidden again: the same "paint without being seen" contract the
    # other rendered audits in this repo keep.
    $dialog.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $dialog.Location = New-Object System.Drawing.Point -32000, -32000
    $dialog.Show()
    try {
        [System.Windows.Forms.Application]::DoEvents()
        return (RenderShown $dialog)
    } finally { $dialog.Hide() }
}

function RenderShown([object]$dialog) {
    $bmp = New-Object System.Drawing.Bitmap $dialog.Width, $dialog.Height
    try {
        $dialog.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle 0, 0, $bmp.Width, $bmp.Height))
        # DrawToBitmap renders a Form's NON-CLIENT area as well: the title bar and
        # frame are painted by the operating system, not by SAITULS, so the
        # zero-white claim is made about the CLIENT rectangle only. The frame is
        # measured too and reported, so the distinction is visible in the output
        # rather than assumed.
        $insetX = [int](($dialog.Width - $dialog.ClientSize.Width) / 2)
        $insetY = $dialog.Height - $dialog.ClientSize.Height - $insetX
        $client = New-Object System.Drawing.Rectangle $insetX, $insetY, $dialog.ClientSize.Width, $dialog.ClientSize.Height
        $light = 0
        $frameLight = 0
        $raised = 0
        $counts = @{}
        for ($y = 0; $y -lt $bmp.Height; $y++) {
            for ($x = 0; $x -lt $bmp.Width; $x++) {
                $c = $bmp.GetPixel($x, $y)
                # "light surface" = a pixel a stock MessageBox would paint
                if ($c.R -ge 200 -and $c.G -ge 200 -and $c.B -ge 200) {
                    if ($client.Contains($x, $y)) { $light++ } else { $frameLight++ }
                }
                $key = '{0},{1},{2}' -f $c.R, $c.G, $c.B
                if ($counts.ContainsKey($key)) { $counts[$key]++ } else { $counts[$key] = 1 }
            }
        }
        $ranked = @($counts.GetEnumerator() | Sort-Object Value -Descending)
        $top = $ranked[0]
        if ($counts.ContainsKey($TOKEN_RAISED)) { $raised = $counts[$TOKEN_RAISED] }
        return @{
            Light = $light
            FrameLight = $frameLight
            Raised = $raised
            Top = $top.Key
            TopCount = $top.Value
            Distinct = $counts.Count
            TopColors = (($ranked | Select-Object -First 4 | ForEach-Object { "$($_.Name)x$($_.Value)" }) -join ' ')
        }
    } finally { $bmp.Dispose() }
}

$TOKEN_BG_SOFT = '35,32,24'      # Palette.BG_SOFT  #232018
$TOKEN_RAISED = '61,55,42'       # Palette.RAISED   #3D372A

$cases = @(
    @{ Name = 'OK / Information'; Buttons = $MB::OK; Icon = $MI::Information },
    @{ Name = 'OK / Error'; Buttons = $MB::OK; Icon = $MI::Error },
    @{ Name = 'OK / Warning'; Buttons = $MB::OK; Icon = $MI::Warning },
    @{ Name = 'OKCancel / Warning'; Buttons = $MB::OKCancel; Icon = $MI::Warning },
    @{ Name = 'YesNo / Warning'; Buttons = $MB::YesNo; Icon = $MI::Warning }
)

# A body with a Win32 path, an ampersand and newlines: everything a tool-missing
# message actually contains, and everything a Label or a mnemonic could mangle.
$body = "The item tool engine is missing:`nC:\Tools & Stuff\item_tools.py`n`nBuild it first."
$caption = 'SAITULS'
$oneButtonRaised = -1

foreach ($case in $cases) {
    $dialog = NewDialog $body $caption $case.Buttons $case.Icon
    try {
        $stats = RenderStats $dialog
        Check ("rendered: the client area has zero light pixels ({0})" -f $case.Name) `
            ($stats.Light -eq 0) "client_light=$($stats.Light) os_frame_light=$($stats.FrameLight)"
        Check ("rendered: client area is the Golden Default background ({0})" -f $case.Name) `
            ($stats.Top -eq $TOKEN_BG_SOFT) "top=$($stats.Top) x$($stats.TopCount)"
        Check ("rendered: the dialog really drew its own controls ({0})" -f $case.Name) `
            ($stats.Distinct -gt 2) "distinct=$($stats.Distinct)"
        Check ("rendered: the buttons are drawn from the raised token ({0})" -f $case.Name) `
            ($stats.Raised -gt 0) "raised=$($stats.Raised) [$($stats.TopColors)]"
        if ($case.Buttons -eq $MB::OK) { $oneButtonRaised = $stats.Raised }
        elseif ($oneButtonRaised -gt 0) {
            # two buttons must occupy measurably more raised pixels than one
            Check ("rendered: the second button really drew ({0})" -f $case.Name) `
                ($stats.Raised -gt $oneButtonRaised) "raised=$($stats.Raised) vs one=$oneButtonRaised"
        }
    } finally { $dialog.Dispose() }
}

# ------------------------------------------------------- semantics, per button set
$okDialog = NewDialog $body $caption $MB::OK $MI::None
try {
    Check 'OK maps to DialogResult.OK' ($acceptField.GetValue($okDialog) -eq [System.Windows.Forms.DialogResult]::OK)
    Check 'OK has no decline button' ($hasDeclineProp.GetValue($okDialog) -eq $false)
    Check 'OK is the accept button and there is no cancel button' `
        ($okDialog.AcceptButton -ne $null -and $okDialog.CancelButton -eq $null)
    Check 'the dialog cannot be dismissed from a close box' ($okDialog.ControlBox -eq $false)
    Check 'the dialog is a fixed, non-resizable window' ($okDialog.FormBorderStyle -eq 'FixedDialog')
    Check 'the dialog starts over its owner' ($okDialog.StartPosition -eq 'CenterParent')
    Check 'the message survives verbatim (path, ampersand, newlines)' `
        ($messageProp.GetValue($okDialog) -eq $body)
} finally { $okDialog.Dispose() }

$okCancel = NewDialog 'text' $caption $MB::OKCancel $MI::Warning
try {
    Check 'OKCancel maps OK and Cancel to the right DialogResults' `
        ($acceptField.GetValue($okCancel) -eq [System.Windows.Forms.DialogResult]::OK -and `
         $declineField.GetValue($okCancel) -eq [System.Windows.Forms.DialogResult]::Cancel)
    Check 'OKCancel wires Enter to accept and Esc to cancel' `
        ($okCancel.AcceptButton.Text -eq 'OK' -and $okCancel.CancelButton -ne $null -and `
         $okCancel.CancelButton.DialogResult -eq [System.Windows.Forms.DialogResult]::Cancel)
} finally { $okCancel.Dispose() }

$yesNo = NewDialog 'text' $caption $MB::YesNo $MI::Question
try {
    Check 'YesNo maps Yes and No to the right DialogResults' `
        ($acceptField.GetValue($yesNo) -eq [System.Windows.Forms.DialogResult]::Yes -and `
         $declineField.GetValue($yesNo) -eq [System.Windows.Forms.DialogResult]::No)
    Check 'YesNo wires Enter to Yes and Esc to No' `
        ($yesNo.AcceptButton.Text -eq 'Yes' -and $yesNo.CancelButton.Text -eq 'No')
} finally { $yesNo.Dispose() }

# ------------------------------------------------------- modality / facade shape
$facade = $asm.GetType('Saituls.SaitulsDialog', $true)
# static methods need the Static flag in the binding attribute
$staticFlags = [Reflection.BindingFlags]'Static,Public,NonPublic'
foreach ($shape in @(
    @{ Args = @([string]); Name = 'Show(string)' },
    @{ Args = @([string], [string]); Name = 'Show(string, string)' },
    @{ Args = @([string], [string], $MB, $MI); Name = 'Show(string, string, buttons, icon)' },
    @{ Args = @([System.Windows.Forms.IWin32Window], [string], [string]); Name = 'Show(owner, string, string)' },
    @{ Args = @([System.Windows.Forms.IWin32Window], [string], [string], $MB, $MI); Name = 'Show(owner, string, string, buttons, icon)' }
)) {
    $m = $facade.GetMethod('Show', $staticFlags, $null, $shape.Args, $null)
    Check ("facade keeps the MessageBox shape {0}" -f $shape.Name) ($m -ne $null -and $m.IsStatic)
}

# ------------------------------------------------- popup menus (tray / context)
# Same method as the dialogs: build the real control through the product's own
# factory, render it, then require every ink to be a Golden Default token.
$anyMem = [Reflection.BindingFlags]'Instance,Static,Public,NonPublic'
$menuType = $asm.GetType('Saituls.SaitulsMenu', $true)
Check 'the popup-menu factory exists' ($menuType -ne $null)

$bgSoft = [System.Drawing.Color]::FromArgb(0x23, 0x20, 0x18)   # Palette.BG_SOFT
$bdark = [System.Drawing.Color]::FromArgb(0x10, 0x0E, 0x08)    # Palette.BDARK
$bevel = [System.Drawing.Color]::FromArgb(0x75, 0x66, 0x3D)    # Palette.BEVEL
$textInk = [System.Drawing.Color]::FromArgb(0xD4, 0xC8, 0x9A)  # Palette.TEXT
$muted = [System.Drawing.Color]::FromArgb(0x6E, 0x67, 0x4E)    # Palette.MUTED
$tokens = @(("{0},{1},{2}" -f $bgSoft.R, $bgSoft.G, $bgSoft.B),
            ("{0},{1},{2}" -f $bdark.R, $bdark.G, $bdark.B),
            ("{0},{1},{2}" -f $bevel.R, $bevel.G, $bevel.B),
            ("{0},{1},{2}" -f $textInk.R, $textInk.G, $textInk.B),
            ("{0},{1},{2}" -f $muted.R, $muted.G, $muted.B))

function NewMenuItems([System.Windows.Forms.ContextMenuStrip]$menu) {
    $menu.Items.Add('Open SAITULS') | Out-Null
    $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
    $menu.Items.Add('Folder Automation') | Out-Null
    $off = $menu.Items.Add('Unavailable')
    $off.Enabled = $false
    $menu.Items.Add('Exit') | Out-Null
    return $off
}

function RenderMenu([System.Windows.Forms.ContextMenuStrip]$menu) {
    $menu.PerformLayout()
    $pref = $menu.GetPreferredSize((New-Object System.Drawing.Size 0, 0))
    $bmp = New-Object System.Drawing.Bitmap ([Math]::Max(1, $pref.Width)), ([Math]::Max(1, $pref.Height))
    try {
        $menu.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle 0, 0, $bmp.Width, $bmp.Height))
        $light = 0
        $offToken = 0
        $offGray = 0
        $counts = @{}
        for ($y = 0; $y -lt $bmp.Height; $y++) {
            for ($x = 0; $x -lt $bmp.Width; $x++) {
                $c = $bmp.GetPixel($x, $y)
                if ($c.R -ge 200 -and $c.G -ge 200 -and $c.B -ge 200) { $light++ }
                $key = '{0},{1},{2}' -f $c.R, $c.G, $c.B
                if ($counts.ContainsKey($key)) { $counts[$key]++ } else { $counts[$key] = 1 }
            }
        }
        return @{ Light = $light; Counts = $counts; Width = $bmp.Width; Height = $bmp.Height }
    } finally { $bmp.Dispose() }
}

$themedMenu = $menuType.GetMethod('Themed', $anyMem).Invoke($null, [object[]]@())
try {
    Check 'themed menu: Golden Default surface, ink and pixel font' `
        ($themedMenu.BackColor -eq $bgSoft -and $themedMenu.ForeColor -eq $textInk -and $themedMenu.Font.Name -eq 'Verdana') `
        "bg=$($themedMenu.BackColor) font=$($themedMenu.Font.Name)"
    Check 'themed menu: the product renderer is the one installed' `
        ($themedMenu.Renderer.GetType().Name -eq 'SaitulsMenuRenderer') $themedMenu.Renderer.GetType().Name

    $off = NewMenuItems $themedMenu
    $offBox = $off.Bounds
    $themedStats = RenderMenu $themedMenu
    $alien = @($themedStats.Counts.Keys | Where-Object { $_ -notin $tokens })
    Check 'rendered: the themed menu has no light pixel at all' `
        ($themedStats.Light -eq 0) "light=$($themedStats.Light)"
    Check 'rendered: every menu pixel comes from a Golden Default token' `
        ($alien.Count -eq 0) ($alien -join ' | ')
    Check 'rendered: the menu really drew its own chrome and items' `
        ($themedStats.Counts.Count -ge 4) "distinct=$($themedStats.Counts.Count)"

    # The disabled item is the one a stock renderer paints SystemColors.GrayText
    # (109,109,109): neither a palette token nor legible on the dark surface.
    $mutedPx = 0
    $grayPx = 0
    $probe = New-Object System.Drawing.Bitmap ([Math]::Max(1, $themedStats.Width)), ([Math]::Max(1, $themedStats.Height))
    try {
        $themedMenu.DrawToBitmap($probe, (New-Object System.Drawing.Rectangle 0, 0, $probe.Width, $probe.Height))
        for ($y = $offBox.Top; $y -lt $offBox.Bottom; $y++) {
            for ($x = $offBox.Left; $x -lt $offBox.Right; $x++) {
                $c = $probe.GetPixel($x, $y)
                $k = '{0},{1},{2}' -f $c.R, $c.G, $c.B
                if ($k -eq ('{0},{1},{2}' -f $muted.R, $muted.G, $muted.B)) { $mutedPx++ }
                if ($k -eq '109,109,109') { $grayPx++ }
            }
        }
    } finally { $probe.Dispose() }
    Check 'rendered: a disabled item is drawn in the muted token' ($mutedPx -gt 0) "muted=$mutedPx"
    Check 'rendered: no disabled item falls back to the OS grey text' ($grayPx -eq 0) "graytext=$grayPx"

    # Negative control: the same render of an untuned stock menu is lighter, so the
    # measurement above is known to discriminate rather than to always pass.
    $stock = New-Object System.Windows.Forms.ContextMenuStrip
    try {
        NewMenuItems $stock | Out-Null
        $stockStats = RenderMenu $stock
        Check 'control: an untuned stock menu is measurably lighter than the themed one' `
            ($themedStats.Light -lt $stockStats.Light) `
            "themed=$($themedStats.Light) stock=$($stockStats.Light)"
    } finally { $stock.Dispose() }
} finally { $themedMenu.Dispose() }

Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host '---'
if ($failures -gt 0) { Write-Host "SAITULS_DIALOGS: FAIL ($failures)"; exit 1 }
Write-Host 'SAITULS_DIALOGS: PASS (0 failures)'
exit 0
