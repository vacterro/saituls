param(
    [string]$Source = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'SAITULS.cs')
)

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

$work = Join-Path ([IO.Path]::GetTempPath()) ("saituls_fa_accept_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$screenDir = Join-Path $root 'tmp\screenshots'
New-Item -ItemType Directory -Path $screenDir -Force | Out-Null

$dll = Join-Path $work 'SaitulsFaSubject.dll'
$module = @(Get-ChildItem -LiteralPath $ModuleDir -Filter '*.cs' -File |
    Sort-Object Name | ForEach-Object { $_.FullName })
& $csc -nologo -target:library -out:$dll -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll $Source @module | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL  subject does not compile: $Source + FolderAutomation"; exit 1 }

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$asm = [Reflection.Assembly]::LoadFrom($dll)

$engineType = $asm.GetType('Saituls.FolderAutomation.FolderAccessManagerEngine', $true)
$probeMethod = $engineType.GetMethod('ProbeTrustedMode', [Reflection.BindingFlags]'Static,Public')
$runBoundedMethod = $engineType.GetMethod('RunProcessBounded', [Reflection.BindingFlags]'Static,Public')
$dlgType = $asm.GetType('Saituls.FolderAutomation.FolderAccessManagerDialog', $true)

Write-Host "=== 1. STRUCTURED BACKEND ERROR HANDLING & NEGATIVE TESTS ==="

# Helper to create mock scripts
function NewMock([string]$name, [string]$content) {
    $p = Join-Path $work $name
    Set-Content -LiteralPath $p -Value $content -Encoding Ascii
    return $p
}

function Probe([string]$sel, [string]$id, [string]$exe) {
    return $probeMethod.Invoke($null, [object[]]@($sel, $id, $exe))
}

# 1.1 unknown_action response
$mockUnknown = NewMock 'mock_unknown.bat' @'
@echo off
echo {"schema":"sai.accounts/error/1","status":"unknown_action","detail":"trusted-mode-status"}
exit /b 1
'@
$res = Probe 'AG1' 'acc-ag1' $mockUnknown
Check 'negative: unknown_action reports BACKEND_OUTDATED' ($res.Status -eq 'BACKEND_OUTDATED' -and $res.IsDiagnosticError -eq $true) "status=$($res.Status)"

# 1.2 generic error schema
$mockGeneric = NewMock 'mock_generic.bat' @'
@echo off
echo {"schema":"sai.accounts/error/1","status":"HELPER_UNAVAILABLE","detail":"Helper service offline"}
exit /b 1
'@
$res = Probe 'AG1' 'acc-ag1' $mockGeneric
Check 'negative: generic error envelope parsed' ($res.Status -eq 'HELPER_UNAVAILABLE' -and $res.IsDiagnosticError -eq $true) "status=$($res.Status)"

# 1.3 nonzero process exit with non-json stderr
$mockNonzero = NewMock 'mock_nonzero.bat' @'
@echo off
echo Fatal error occurred 1>&2
exit /b 2
'@
$res = Probe 'AG1' 'acc-ag1' $mockNonzero
Check 'negative: nonzero exit reports ERROR' ($res.Status -eq 'ERROR' -and $res.IsDiagnosticError -eq $true) "status=$($res.Status)"

# 1.4 malformed JSON
$mockMalformed = NewMock 'mock_malformed.bat' @'
@echo off
echo {not valid json at all
exit /b 0
'@
$res = Probe 'AG1' 'acc-ag1' $mockMalformed
Check 'negative: malformed JSON reports INVALID_RESPONSE' ($res.Status -eq 'INVALID_RESPONSE' -and $res.IsDiagnosticError -eq $true) "status=$($res.Status)"

# 1.5 missing status fields
$mockMissingFields = NewMock 'mock_missing.bat' @'
@echo off
echo {"schema":"sai.accounts/trusted-mode-status/1","account_id":"acc-ag1","selector":"AG1"}
exit /b 0
'@
$res = Probe 'AG1' 'acc-ag1' $mockMissingFields
Check 'negative: missing status/preference reports INVALID_RESPONSE' ($res.Status -eq 'INVALID_RESPONSE' -and $res.IsDiagnosticError -eq $true) "status=$($res.Status)"

# 1.6 mismatched account identity
$mockMismatched = NewMock 'mock_mismatched.bat' @'
@echo off
echo {"schema":"sai.accounts/trusted-mode-status/1","account_id":"acc-ag2","selector":"AG2","preference":"OFF","status":"OFF_VERIFIED"}
exit /b 0
'@
$res = Probe 'AG1' 'acc-ag1' $mockMismatched
Check 'negative: mismatched account identity reports INVALID_RESPONSE' ($res.Status -eq 'INVALID_RESPONSE' -and $res.IsDiagnosticError -eq $true) "status=$($res.Status)"

# 1.7 backend timeout
$mockTimeout = NewMock 'mock_timeout.bat' @'
@echo off
powershell -NoProfile -Command "Start-Sleep -Seconds 30"
'@
$sw = [Diagnostics.Stopwatch]::StartNew()
$res = Probe 'AG1' 'acc-ag1' $mockTimeout
$sw.Stop()
Check 'negative: backend timeout detected boundedly' ($res.Status -eq 'TIMEOUT' -and $sw.ElapsedMilliseconds -lt 10000) "status=$($res.Status) time=$($sw.ElapsedMilliseconds)ms"

# 1.8 broker/backend unavailable (missing file)
$res = Probe 'AG1' 'acc-ag1' 'C:\nonexistent\sai-accounts.exe'
Check 'negative: missing binary reports BACKEND_UNAVAILABLE' ($res.Status -eq 'BACKEND_UNAVAILABLE' -and $res.IsDiagnosticError -eq $true) "status=$($res.Status)"

# 1.9 real installed binary probe (now deployed with trusted-mode-status support)
$installedExe = "$env:ProgramData\SAI\Accounts\bin\sai-accounts.exe"
if (Test-Path $installedExe) {
    $res = Probe 'AG1' 'antigravity:windows-user:1f17624fa8051f44' $installedExe
    Check 'real installed binary supports trusted-mode-status' ($res.Status -ne 'BACKEND_OUTDATED' -and $res.Status -ne 'BACKEND_UNAVAILABLE') "status=$($res.Status) detail=$($res.ErrorDetail)"
}

# 1.10 real source/staged binary probe
$repoExe = Join-Path $root 'Scripts\accounts\bin\sai-accounts.exe'
if (Test-Path $repoExe) {
    $res = Probe 'AG1' 'antigravity:windows-user:1f17624fa8051f44' $repoExe
    Check 'repo binary reports valid status (not unknown_action)' ($res.Status -in @('OFF_VERIFIED', 'ON_VERIFIED', 'ERROR', 'EMPTY_SCOPE')) "status=$($res.Status)"
}

Write-Host "=== 2. VISUAL ACCEPTANCE, GEOMETRY AUDIT & SCREENSHOTS ==="

# Helper to inspect control hierarchy and check for overlaps
function AssertNoOverlaps([System.Windows.Forms.Control]$container, [string]$scopeName) {
    $controls = @()
    foreach ($c in $container.Controls) {
        if ($c.Visible -and $c.Width -gt 0 -and $c.Height -gt 0) {
            $controls += $c
        }
    }
    for ($i = 0; $i -lt $controls.Count; $i++) {
        for ($j = $i + 1; $j -lt $controls.Count; $j++) {
            $c1 = $controls[$i]
            $c2 = $controls[$j]
            # Controls inside FlowLayoutPanel or TableLayoutPanel cells are managed by layout
            if ($c1.Bounds.IntersectsWith($c2.Bounds)) {
                return "Overlap in $scopeName between '$($c1.GetType().Name):$($c1.Text)' at $($c1.Bounds) and '$($c2.GetType().Name):$($c2.Text)' at $($c2.Bounds)"
            }
        }
    }
    return ''
}

function RenderAndCapture([object]$dialog, [string]$filename) {
    $dialog.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $dialog.Location = New-Object System.Drawing.Point -32000, -32000
    $dialog.Show()
    try {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 200
        [System.Windows.Forms.Application]::DoEvents()

        $bmp = New-Object System.Drawing.Bitmap $dialog.Width, $dialog.Height
        try {
            $dialog.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle 0, 0, $bmp.Width, $bmp.Height))
            $outPath = Join-Path $screenDir $filename
            $bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)

            # Check client area zero-white
            $insetX = [int](($dialog.Width - $dialog.ClientSize.Width) / 2)
            $insetY = $dialog.Height - $dialog.ClientSize.Height - $insetX
            $client = New-Object System.Drawing.Rectangle $insetX, $insetY, $dialog.ClientSize.Width, $dialog.ClientSize.Height
            $light = 0
            for ($y = $client.Top; $y -lt $client.Bottom; $y += 2) {
                for ($x = $client.Left; $x -lt $client.Right; $x += 2) {
                    $c = $bmp.GetPixel($x, $y)
                    if ($c.R -ge 230 -and $c.G -ge 230 -and $c.B -ge 230) { $light++ }
                }
            }
            return @{ OutPath = $outPath; Light = $light }
        } finally { $bmp.Dispose() }
    } finally { $dialog.Hide() }
}

$labels = New-Object 'System.Collections.Generic.Dictionary[string,string]'
$labels['AG1'] = 'AG1 (Development)'
$labels['AG2'] = 'AG2 (Isolated Test)'
$labels['AG_LONG'] = 'antigravity-very-long-account-identifier-that-exceeds-standard-lengths-1234567890'

$backends = New-Object 'System.Collections.Generic.Dictionary[string,string]'
$backends['AG1'] = 'managed_windows_user'
$backends['AG2'] = 'managed_windows_user'
$backends['AG_LONG'] = 'managed_windows_user'

$sids = New-Object 'System.Collections.Generic.Dictionary[string,string]'
$sids['AG1'] = 'S-1-5-21-9999999999-9999999999-9999999999-1002'
$sids['AG2'] = 'S-1-5-21-9999999999-9999999999-9999999999-1003'
$sids['AG_LONG'] = 'S-1-5-21-9999999999-9999999999-9999999999-1099'

$dummyFolders = [Func[string, System.Collections.Generic.List[string]]]{
    param($id)
    $l = New-Object 'System.Collections.Generic.List[string]'
    $l.Add('C:\Projects\SAITULS')
    $l.Add('C:\Users\testuser\AppData\Local\Temp')
    $l.Add('D:\Projects\VeryLongNestedProjectDirectoryPathWithManySubdirectories\Source')
    return $l
}
$dummySave = [Action[string, System.Collections.Generic.List[string]]]{ param($id, $flds) }

$testCases = @(
    @{ Name = '960x560_100DPI'; Width = 960; Height = 560; Scale = 1.0; Account = 'AG1'; File = '01_960x560_100dpi.png' },
    @{ Name = '820x460_100DPI'; Width = 820; Height = 460; Scale = 1.0; Account = 'AG1'; File = '02_820x460_100dpi.png' },
    @{ Name = '125DPI_Simulation'; Width = 1000; Height = 600; Scale = 1.25; Account = 'AG1'; File = '03_125dpi.png' },
    @{ Name = '150DPI_Simulation'; Width = 1100; Height = 650; Scale = 1.5; Account = 'AG1'; File = '04_150dpi.png' },
    @{ Name = '200DPI_Simulation'; Width = 1280; Height = 750; Scale = 2.0; Account = 'AG1'; File = '05_200dpi.png' },
    @{ Name = 'Long_Account_Identifier'; Width = 960; Height = 560; Scale = 1.0; Account = 'AG_LONG'; File = '06_long_account.png' },
    @{ Name = 'Narrow_Window_820'; Width = 820; Height = 520; Scale = 1.0; Account = 'AG1'; File = '07_narrow_window.png' },
    @{ Name = 'Expanded_Window_1200'; Width = 1200; Height = 700; Scale = 1.0; Account = 'AG1'; File = '08_expanded_window.png' }
)

foreach ($tc in $testCases) {
    $dlg = New-Object Saituls.FolderAutomation.FolderAccessManagerDialog(
        $labels, $backends, $sids, $tc.Account, $repoExe, '', (Join-Path $root 'Scripts\accounts'),
        $dummyFolders, $dummySave
    )
    try {
        $dlg.Size = New-Object System.Drawing.Size $tc.Width, $tc.Height
        if ($tc.Scale -ne 1.0) {
            $sf = New-Object System.Drawing.SizeF ([float]$tc.Scale), ([float]$tc.Scale)
            $dlg.Scale($sf)
        }

        # Check for control overlaps in all panels
        $topPanel = $dlg.Controls | Where-Object { $_.Dock -eq [System.Windows.Forms.DockStyle]::Top }
        $bottomPanel = $dlg.Controls | Where-Object { $_.Dock -eq [System.Windows.Forms.DockStyle]::Bottom }

        $errTop = ''
        if ($topPanel -ne $null) {
            foreach ($sub in $topPanel.Controls) {
                $err = AssertNoOverlaps $sub "$($tc.Name):topPanel:$($sub.GetType().Name)"
                if ($err) { $errTop = $err; break }
            }
        }
        Check "layout: no control overlap in topPanel ($($tc.Name))" ($errTop -eq '') $errTop

        $errBot = ''
        if ($bottomPanel -ne $null) {
            foreach ($sub in $bottomPanel.Controls) {
                $err = AssertNoOverlaps $sub "$($tc.Name):bottomPanel:$($sub.GetType().Name)"
                if ($err) { $errBot = $err; break }
            }
        }
        Check "layout: no control overlap in bottomPanel ($($tc.Name))" ($errBot -eq '') $errBot

        $render = RenderAndCapture $dlg $tc.File
        Check "rendered: zero light pixels in client area ($($tc.Name))" ($render.Light -eq 0) "light_pixels=$($render.Light) file=$($tc.File)"
    } finally {
        $dlg.Dispose()
    }
}

# 2.2 Special states: Backend Outdated rendering (using mock unknown)
$dlgOutdated = New-Object Saituls.FolderAutomation.FolderAccessManagerDialog(
    $labels, $backends, $sids, 'AG1', $mockUnknown, '', (Join-Path $root 'Scripts\accounts'),
    $dummyFolders, $dummySave
)
try {
    $dlgOutdated.Size = New-Object System.Drawing.Size 960, 560
    $dlgOutdated.LoadTrustedModeState('AG1')
    $renderOut = RenderAndCapture $dlgOutdated '09_backend_outdated_state.png'
    Check 'special state: BACKEND_OUTDATED rendered and captured' ($renderOut.Light -eq 0) "file=09_backend_outdated_state.png"
} finally { $dlgOutdated.Dispose() }

# 2.3 Special states: Backend Unavailable rendering
$dlgUnavail = New-Object Saituls.FolderAutomation.FolderAccessManagerDialog(
    $labels, $backends, $sids, 'AG1', 'C:\missing\sai-accounts.exe', '', (Join-Path $root 'Scripts\accounts'),
    $dummyFolders, $dummySave
)
try {
    $dlgUnavail.Size = New-Object System.Drawing.Size 960, 560
    $dlgUnavail.LoadTrustedModeState('AG1')
    $renderUnavail = RenderAndCapture $dlgUnavail '10_backend_unavailable_state.png'
    Check 'special state: BACKEND_UNAVAILABLE rendered and captured' ($renderUnavail.Light -eq 0) "file=10_backend_unavailable_state.png"
} finally { $dlgUnavail.Dispose() }

Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "---"
if ($failures -gt 0) {
    Write-Host "FAILED ($failures failure(s))" -ForegroundColor Red
    exit 1
} else {
    Write-Host "PASS (0 failures) - All visual & regression checks passed!" -ForegroundColor Green
    exit 0
}
