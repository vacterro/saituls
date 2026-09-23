param(
    [string]$Source = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'SAITULS.cs'),
    [int]$Paints = 10000,
    [int]$MaxBytesPerPaint = 160000,
    [double]$MaxMsPerPaint = 10
)

$ErrorActionPreference = 'Stop'
$failures = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { Write-Host "FAIL  csc.exe not found at $csc"; exit 1 }

$work = Join-Path ([IO.Path]::GetTempPath()) ("saituls_repaint_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$dll = Join-Path $work 'SaitulsSubject.dll'

& $csc -nologo -target:library -out:$dll -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll $Source | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL  subject does not compile: $Source"; exit 1 }

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SaitulsGuiResources
{
    [DllImport("user32.dll")] static extern uint GetGuiResources(IntPtr hProcess, uint uiFlags);
    public static uint Gdi() { return GetGuiResources(System.Diagnostics.Process.GetCurrentProcess().Handle, 0); }
    public static uint User() { return GetGuiResources(System.Diagnostics.Process.GetCurrentProcess().Handle, 1); }
}
'@

$asm = [Reflection.Assembly]::LoadFrom($dll)
$settingsType = $asm.GetType('Saituls.SaitulsSettings', $true)
$formType = $asm.GetType('Saituls.SaitulsForm', $true)
$buttonType = $asm.GetType('Saituls.SaitulsForm+ButtonDef', $true)

$nonPublic = [Reflection.BindingFlags]'Instance,NonPublic'
$anyCtor = [Reflection.BindingFlags]'Instance,Public,NonPublic'
$anyMethod = [Reflection.BindingFlags]'Instance,Static,Public,NonPublic'

# RootPath points at the scratch dir, so the Home tab's presence probes stay
# inside the sandbox instead of reading the real toolkit.
$settings = $settingsType.GetConstructors($anyCtor)[0].Invoke([object[]]@([string]$work))
$tray = New-Object System.Windows.Forms.NotifyIcon
$form = $formType.GetConstructors($anyCtor)[0].Invoke([object[]]@($settings, [System.Windows.Forms.NotifyIcon]$tray))

$onPaint = $formType.GetMethod('OnPaint', $anyMethod)
$currentTabField = $formType.GetField('CurrentTab', $nonPublic)
$tabRectsField = $formType.GetField('TabRects', $nonPublic)
$buttonsField = $formType.GetField('Buttons', $nonPublic)
$fontsField = $formType.GetField('Fonts', $nonPublic)
$centeredField = $formType.GetField('Centered', $nonPublic)
$btnR = $buttonType.GetField('R')
$btnLabel = $buttonType.GetField('Label')
$tabs = @('Home', 'Explorer menus', 'Tools', 'Settings')
$first = $null

function Paint([object]$bmp, [string]$tab) {
    $currentTabField.SetValue($form, $tab)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try {
        $pe = New-Object System.Windows.Forms.PaintEventArgs $g, (New-Object System.Drawing.Rectangle 0, 0, $bmp.Width, $bmp.Height)
        $onPaint.Invoke($form, [object[]]@([System.Windows.Forms.PaintEventArgs]$pe))
        $pe.Dispose()
    } finally { $g.Dispose() }
}

function Geometry() {
    $parts = @()
    foreach ($r in $tabRectsField.GetValue($form)) { $parts += "T $($r.X),$($r.Y),$($r.Width),$($r.Height)" }
    foreach ($b in $buttonsField.GetValue($form)) {
        $r = $btnR.GetValue($b)
        $parts += "B $($btnLabel.GetValue($b))@$($r.X),$($r.Y),$($r.Width),$($r.Height)"
    }
    return ($parts -join '|')
}

try {
    # One Font per point size, handed out for the form's whole life.
    $fMethod = $formType.GetMethod('F', $anyMethod)
    if ($null -eq $fMethod) {
        Check 'repeated font requests reuse one cached Font' $false 'no F(int) member'
    } else {
        $fTarget = if ($fMethod.IsStatic) { $null } else { $form }
        $first = $fMethod.Invoke($fTarget, [object[]]@([int]12))
        $same = $true
        foreach ($pt in 10, 12, 14) {
            $a = $fMethod.Invoke($fTarget, [object[]]@([int]$pt))
            $b = $fMethod.Invoke($fTarget, [object[]]@([int]$pt))
            if (-not [Object]::ReferenceEquals($a, $b)) { $same = $false }
        }
        Check 'repeated font requests reuse one cached Font' $same
        Check 'distinct point sizes stay distinct fonts' `
            (-not [Object]::ReferenceEquals($fMethod.Invoke($fTarget, [object[]]@([int]12)), $fMethod.Invoke($fTarget, [object[]]@([int]14))))
    }

    # Appearance guardrail: the cached font is the very font the old per-call
    # path produced, so nothing on screen can shift.
    $makeFont = $formType.GetMethod('MakePixelFont', $anyMethod)
    $fresh = $makeFont.Invoke($null, [object[]]@('Verdana', [int]12))
    try {
        $ok = $null -ne $first -and $fresh.Name -eq $first.Name -and $fresh.Size -eq $first.Size -and `
            $fresh.Style -eq $first.Style -and $fresh.Height -eq $first.Height
        Check 'cached font matches a freshly built pixel font' $ok "$($fresh.Name) $($fresh.Size) h=$($fresh.Height)"
    } finally { $fresh.Dispose() }

    if ($null -ne $centeredField) {
        $sf = $centeredField.GetValue($form)
        Check 'the shared StringFormat centers exactly like the per-call one did' `
            ($sf.Alignment -eq 'Center' -and $sf.LineAlignment -eq 'Center')
    } else {
        Check 'the shared StringFormat centers exactly like the per-call one did' $false 'no Centered field'
    }

    $bmp = New-Object System.Drawing.Bitmap 560, 440
    try {
        foreach ($tab in $tabs) { for ($i = 0; $i -lt 5; $i++) { Paint $bmp $tab } }
        [GC]::Collect(); [GC]::WaitForPendingFinalizers(); [GC]::Collect()

        $baseline = @{}
        foreach ($tab in $tabs) { Paint $bmp $tab; $baseline[$tab] = Geometry }

        $beforeGdi = [SaitulsGuiResources]::Gdi()
        $beforeUser = [SaitulsGuiResources]::User()
        $fontsBefore = if ($null -ne $fontsField) { @($fontsField.GetValue($form).Values) } else { @() }
        $beforeBytes = [GC]::GetAllocatedBytesForCurrentThread()
        $sw = [Diagnostics.Stopwatch]::StartNew()
        for ($i = 0; $i -lt $Paints; $i++) { Paint $bmp $tabs[$i % $tabs.Length] }
        $sw.Stop()
        $gdiGrowth = [int][SaitulsGuiResources]::Gdi() - [int]$beforeGdi
        $userGrowth = [int][SaitulsGuiResources]::User() - [int]$beforeUser
        $perPaint = [int](([GC]::GetAllocatedBytesForCurrentThread() - $beforeBytes) / $Paints)
        $msPerPaint = [Math]::Round($sw.Elapsed.TotalMilliseconds / $Paints, 3)

        Check 'GDI objects plateau across repaints' ($gdiGrowth -lt 50) "$Paints paints, growth=$gdiGrowth"
        Check 'USER handles plateau across repaints' ($userGrowth -lt 50) "growth=$userGrowth"
        Check 'managed allocation per repaint stays bounded' ($perPaint -lt $MaxBytesPerPaint) `
            "$perPaint bytes/paint (limit $MaxBytesPerPaint), $msPerPaint ms/paint"
        Check 'a repaint costs less than the per-paint budget' ($msPerPaint -lt $MaxMsPerPaint) `
            "$msPerPaint ms/paint (limit $MaxMsPerPaint)"

        if ($null -ne $fontsField) {
            $after = @($fontsField.GetValue($form).Values)
            $cached = $after.Count
            Check 'the font cache plateaus at one entry per point size' ($cached -gt 0 -and $cached -lt 12) "entries=$cached after $Paints paints"
            $kept = $fontsBefore.Count -gt 0
            foreach ($f in $fontsBefore) {
                if (-not ($after | Where-Object { [Object]::ReferenceEquals($_, $f) })) { $kept = $false }
            }
            Check 'the storm reused the very Font instances it started with' $kept
        } else {
            Check 'the font cache plateaus at one entry per point size' $false 'no Fonts cache field'
            Check 'the storm reused the very Font instances it started with' $false 'no Fonts cache field'
        }

        $stable = $true
        $drift = ''
        foreach ($tab in $tabs) {
            Paint $bmp $tab
            $now = Geometry
            if ($now -ne $baseline[$tab]) { $stable = $false; $drift = $tab }
        }
        Check 'tab and button geometry is identical after the repaint storm' $stable $drift
        Check 'every tab really laid out tabs and buttons' `
            (($tabs | Where-Object { $baseline[$_] -match 'T 4,28' -and $baseline[$_] -match '\|B ' }).Count -eq $tabs.Length)

        $painted = $false
        for ($x = 0; $x -lt 560 -and -not $painted; $x += 11) {
            for ($y = 0; $y -lt 440 -and -not $painted; $y += 11) {
                if ($bmp.GetPixel($x, $y).ToArgb() -ne 0) { $painted = $true }
            }
        }
        Check 'the measured paint path really rendered the window' $painted
    } finally { $bmp.Dispose() }

    # Disposal releases the cache exactly once and survives a second call.
    $disposeDecl = $formType.GetMethod('Dispose', $nonPublic, $null, @([bool]), $null)
    Check 'the form owns a Dispose override for its cached GDI objects' `
        ($null -ne $disposeDecl -and $disposeDecl.DeclaringType.Name -eq 'SaitulsForm')
    $form.Dispose()
    if ($null -ne $fontsField) { Check 'Dispose empties the font cache' ($fontsField.GetValue($form).Count -eq 0) }
    if ($null -ne $first) {
        # Font.Height answers from managed state even after Dispose; GetHeight
        # touches the native handle, so only it proves the release.
        $released = $false
        try { $null = $first.GetHeight(96.0) } catch { $released = $true }
        Check 'Dispose really released the cached font' $released
    }
    $doubleOk = $true
    try { $form.Dispose() } catch { $doubleOk = $false }
    Check 'a second Dispose is harmless' $doubleOk
} finally {
    try { $form.Dispose() } catch { }
    $tray.Dispose()
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

# Source contracts the audit clause names explicitly.
$text = Get-Content -LiteralPath $Source -Raw
Check 'F hands out a cached font instead of building one per call' `
    ($text -match 'Font F\(int pt\)' -and $text -match 'Fonts\.TryGetValue' -and $text -notmatch 'static Font F\(int pt\)')
Check 'no draw helper disposes a font it does not own' ($text -notmatch 'using \(Font f = F\(')
Check 'DrawButton and DrawTextCenter no longer build a StringFormat per call' `
    ($text -notmatch 'var fmt = new StringFormat' -and $text -notmatch 'var sf = new StringFormat')
Check 'the T-88 HFONT release stays in place' ($text -match 'Font\.FromHfont' -and $text -match 'Native\.DeleteObject')

Write-Host '---'
if ($failures) { Write-Host "FAILED ($failures failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
