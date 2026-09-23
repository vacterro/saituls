<#
    Architectural contract for the taskbar edge reveal.

    Two halves, both source-shape assertions on code (comments stripped first,
    so a historical note never passes or fails a check):

      1. SAITULS.cs owns lifecycle only -- start, stop, status, toggle. No
         cursor polling, no taskbar Win32, no shell-window manipulation.
      2. Scripts\taskbar_edge stays inside the stated non-goals -- no focus
         theft, no StuckRects3, no Explorer restart, no auto-hide toggling, no
         global mouse hook, no elevation, no moving or resizing the bar.

    Behavioural proof lives in test_taskbar_edge_logic.ps1 (pure logic) and
    test_taskbar_edge_integration.ps1 (real shell).
#>
$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $testsDir
$edgeDir = Join-Path $root 'Scripts\taskbar_edge'

$script:failures = 0
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

function Get-Code([string]$Path) {
    $text = Get-Content -LiteralPath $Path -Raw
    $text = [regex]::Replace($text, '/\*.*?\*/', '', 'Singleline')
    $text = [regex]::Replace($text, '(?m)//.*$', '')
    return $text
}

# Brace-counted body of a type or method, so a token later in the file can
# never be attributed to this block.
function Get-Block([string]$Code, [string]$Header) {
    $start = $Code.IndexOf($Header)
    if ($start -lt 0) { return '' }
    $open = $Code.IndexOf('{', $start)
    if ($open -lt 0) { return '' }
    $depth = 0
    for ($i = $open; $i -lt $Code.Length; $i++) {
        if ($Code[$i] -eq '{') { $depth++ }
        elseif ($Code[$i] -eq '}') {
            $depth--
            if ($depth -eq 0) { return $Code.Substring($start, $i - $start + 1) }
        }
    }
    return ''
}

# Case-sensitive, not preceded by an identifier character: SWP_SHOWWINDOW is
# not a ShowWindow call, and MyGetCursorPos would not be GetCursorPos.
function Has-Token([string]$Code, [string]$Token) {
    return $Code -cmatch ('(?<![A-Za-z0-9_])' + [regex]::Escape($Token))
}

$saitulsPath = Join-Path $root 'SAITULS.cs'
$logicPath = Join-Path $edgeDir 'EdgeLogic.cs'
$helperPath = Join-Path $edgeDir 'TaskbarEdge.cs'
$buildPath = Join-Path $edgeDir 'build.ps1'

foreach ($p in @($saitulsPath, $logicPath, $helperPath, $buildPath)) {
    Check "source present: $(Split-Path -Leaf $p)" (Test-Path -LiteralPath $p) ''
}
if ($script:failures -gt 0) { exit 1 }

$saituls = Get-Code $saitulsPath
$logic = Get-Code $logicPath
$helper = Get-Code $helperPath
$both = $logic + "`n" + $helper

# --- 1. SAITULS.cs owns lifecycle only ---------------------------------------

$forbiddenInLauncher = @(
    'SHAppBarMessage', 'ABM_', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
    'GetCursorPos', 'SetCursorPos', 'MonitorFromPoint', 'MonitorFromWindow',
    'EnumDisplayMonitors', 'GetMonitorInfo', 'SetWindowPos', 'SetForegroundWindow',
    'SetWindowsHookEx', 'WH_MOUSE_LL', 'StuckRects', 'edge_pixels', 'poll_ms', 'dwell_ms'
)
foreach ($token in $forbiddenInLauncher) {
    Check "SAITULS.cs contains no $token" (-not (Has-Token $saituls $token)) ''
}
$edgeHelper = [regex]::Match($saituls, '(?s)static class TaskbarEdgeHelper\b.*?(?=\n\s*static class Toolchain\b)').Value
Check 'TaskbarEdgeHelper source is located' ($edgeHelper.Length -gt 0) ''
Check 'SAITULS edge helper runs no timer-driven loop' (-not ($edgeHelper -match 'System\.Windows\.Forms\.Timer|Timers\.Timer')) ''

Check 'SAITULS.cs has a lifecycle-only TaskbarEdgeHelper' ($saituls -match 'static class TaskbarEdgeHelper') ''
foreach ($verb in @('Start', 'Stop', 'Running', 'Installed')) {
    Check "TaskbarEdgeHelper exposes $verb" ($saituls -match ("(?s)static class TaskbarEdgeHelper.*?public static .*?\b$verb\s*\(")) ''
}
Check 'TaskbarEdgeHelper points at Scripts\taskbar_edge\TaskbarEdge.exe' ($saituls -match '"Scripts",\s*"taskbar_edge",\s*"TaskbarEdge\.exe"') ''

$helperClass = Get-Block $saituls 'static class TaskbarEdgeHelper'
Check 'TaskbarEdgeHelper body located' ($helperClass.Length -gt 0) ''
Check 'helper is started non-elevated' ($helperClass -match 'UseShellExecute = false') ''
Check 'helper is started without a window' ($helperClass -match 'CreateNoWindow = true') ''
Check 'helper start never requests runas' (-not ($helperClass -match 'runas')) ''
Check 'helper uses a per-session Local\ mutex' ($saituls -match 'Local\\\\SaitulsTaskbarEdge"') ''
Check 'helper is never addressed through a Global\ primitive' (-not ($saituls -match 'Global\\\\SaitulsTaskbarEdge')) ''

Check 'TaskbarEdge setting is persisted in SAITULS.ini' ($saituls -match 'Read\("TaskbarEdge", "0"\)' -and $saituls -match 'Write\("TaskbarEdge"') ''
Check 'TaskbarEdge is off by default' ($saituls -match 'public bool TaskbarEdge = false') ''
Check 'Settings exposes the reveal switch' ($saituls -match 'Reliable taskbar edge reveal') ''
Check 'Settings exposes the autostart switch' ($saituls -match 'Start with SAITULS / Windows') ''
Check 'Settings shows helper status' ($saituls -match '(?s)DrawTaskbarEdgeSection.*?TaskbarEdgeHelper\.Running\(\)') ''
Check 'startup starts the helper only when enabled' ($saituls -match 'if \(s\.TaskbarEdge\) \{ try \{ TaskbarEdgeHelper\.Start') ''
Check 'closing or exiting SAITULS never stops the helper' (-not ($saituls -match '(?s)(FormClosing|Application\.Exit)[^;]*TaskbarEdgeHelper\.Stop')) ''

# --- 2. the subsystem stays inside the non-goals ------------------------------

$forbiddenInHelper = @(
    'SetForegroundWindow', 'StuckRects', 'SetWindowsHookEx', 'WH_MOUSE_LL',
    'ABM_SETSTATE', 'ABM_SETAUTOHIDEBAR', 'ABM_NEW', 'ABM_REMOVE',
    'SetCursorPos', 'mouse_event', 'SendInput', 'taskkill', 'ShowWindow',
    'MoveWindow', 'SetWindowPlacement'
)
foreach ($token in $forbiddenInHelper) {
    Check "subsystem contains no $token" (-not (Has-Token $both $token)) ''
}
Check 'subsystem never restarts Explorer' (-not ($both -match 'explorer\.exe')) ''
Check 'subsystem never asks for Administrator' (-not ($both -match 'runas|requestedExecutionLevel|requireAdministrator')) ''
Check 'subsystem uses a per-session Local\ mutex' ($helper -match '"Local\\\\SaitulsTaskbarEdge"') ''
Check 'subsystem uses no Global\ primitive' (-not ($helper -match 'Global\\\\')) ''

Check 'documented appbar lookup is present' ($helper -match 'ABM_GETAUTOHIDEBAREX') ''
Check 'the bottom edge is requested explicitly' ($helper -match 'ABE_BOTTOM') ''
Check 'auto-hide is read through ABM_GETSTATE' ($helper -match 'ABM_GETSTATE') ''
Check 'fallback enumerates both shell tray classes' ($helper -match 'Shell_TrayWnd' -and $helper -match 'Shell_SecondaryTrayWnd') ''
Check 'fallback maps a bar back to its monitor' ($helper -match 'MonitorFromWindow') ''
Check 'Per-Monitor DPI Awareness V2 is requested first' ($helper -match 'SetProcessDpiAwarenessContext\(new IntPtr\(-4\)\)') ''
Check 'cursor position comes from the global GetCursorPos' ($helper -match 'GetCursorPos') ''
Check 'the monitor is resolved from the cursor point' ($helper -match 'MonitorFromPoint') ''
Check 'geometry is taken from the FULL monitor rectangle' ($helper -match 'rcMonitor') ''
Check 'handles are revalidated, never trusted' ($helper -match 'IsWindow') ''

$reveal = Get-Block $helper 'static class Reveal'
Check 'reveal block located' ($reveal.Length -gt 0) ''
foreach ($flag in @('SWP_NOMOVE', 'SWP_NOSIZE', 'SWP_NOACTIVATE', 'SWP_SHOWWINDOW', 'SWP_FRAMECHANGED')) {
    Check "reveal passes $flag" ($reveal -match $flag) ''
}
Check 'reveal targets HWND_TOPMOST' ($reveal -match 'HWND_TOPMOST') ''
Check 'reveal passes zero geometry arguments' ($reveal -match 'SetWindowPos\(bar, Win32\.HWND_TOPMOST, 0, 0, 0, 0,') ''

Check 'EdgeLogic.cs is pure: no P/Invoke' (-not ($logic -match 'DllImport')) ''
Check 'EdgeLogic.cs is pure: no I/O' (-not ($logic -match 'System\.IO|File\.|Directory\.')) ''
Check 'EdgeLogic.cs is pure: no clock' (-not ($logic -match 'DateTime\.Now|DateTime\.UtcNow|Environment\.TickCount|Stopwatch')) ''

Check 'every advanced value is range-checked' ($logic -match 'MinEdgePixels' -and $logic -match 'MaxCooldownMs') ''
Check 'defaults match the specified values' (
    ($logic -match 'DefaultEdgePixels = 2') -and ($logic -match 'DefaultPollMs = 30') -and
    ($logic -match 'DefaultDwellMs = 30') -and ($logic -match 'DefaultCooldownMs = 150')) ''
Check 'dwell requires two consecutive samples' ($logic -match 'stripSamples >= 2') ''
Check 'debounce is an entered/exited edge state' ($logic -match 'revealArmed') ''

Check 'diagnostics expose --status' ($helper -match '"--status"') ''
Check 'diagnostics expose --self-test' ($helper -match '"--self-test"') ''
Check 'an explicit stop command exists' ($helper -match '"--stop"') ''
Check 'debug mode records transitions only' (
    ($helper -match 'EDGE_ENTER') -and ($helper -match 'REVEAL_REQUEST') -and ($helper -match 'REVEAL_OK') -and
    ($helper -match 'EDGE_EXIT') -and ($helper -match 'TASKBAR_REDISCOVERED')) ''
Check 'the debug log is bounded' ($helper -match 'MaxBytes') ''

$build = Get-Content -LiteralPath $buildPath -Raw
Check 'build produces a winexe (no console flash)' ($build -match '-target:winexe') ''
Check 'build compiles both subsystem sources' ($build -match 'EdgeLogic\.cs' -and $build -match 'TaskbarEdge\.cs') ''

Write-Host "TASKBAR_EDGE_CONTRACT failures=$script:failures"
if ($script:failures -eq 0) { Write-Host 'TASKBAR_EDGE_CONTRACT_GREEN=TRUE'; exit 0 }
exit 1
