# Idle cost of a running SAITULS: child-process launches, CPU time, working
# set, handle count. Starts the given exe with the given arguments (default
# --minimized, i.e. tray-hidden, which is how SAITULS runs at logon), waits a
# warm-up, then samples for -Seconds. Children are detected by polling the
# process table for ParentProcessId == SAITULS every 250 ms, so a child that
# lives under ~250 ms can be missed; the count is a lower bound.
param(
    [string]$Exe = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'SAITULS.exe'),
    [string]$Arguments = '--minimized',
    [int]$WarmupSeconds = 20,
    [int]$Seconds = 360,
    [string]$Out = ''
)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MeasuredSaitulsWindow {
    public delegate bool EnumProc(IntPtr hwnd,IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb,IntPtr data);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
    [DllImport("user32.dll")] static extern bool PostThreadMessage(uint thread,uint msg,IntPtr wp,IntPtr lp);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    public static bool QuitOwned(int owner) {
        bool sent=false;
        EnumWindows((hwnd,data)=>{uint pid;uint thread=GetWindowThreadProcessId(hwnd,out pid);
            if(pid!=(uint)owner)return true;
            sent=PostThreadMessage(thread,0x12,IntPtr.Zero,IntPtr.Zero);return !sent;
        },IntPtr.Zero);
        return sent;
    }
}
'@
$existing = Get-Process -Name 'SAITULS' -ErrorAction SilentlyContinue
if ($existing) { throw "SAITULS is already running (pid $($existing.Id -join ',')); measure a fresh instance" }
$foregroundBefore = [MeasuredSaitulsWindow]::GetForegroundWindow()
$systemProcessesBefore = @(Get-Process).Count
$sw = [Diagnostics.Stopwatch]::StartNew()
$start = @{FilePath=$Exe;WindowStyle='Hidden';PassThru=$true}
if ($Arguments) { $start.ArgumentList = $Arguments }
$p = Start-Process @start
try {
$p.WaitForInputIdle(15000) | Out-Null
if ($foregroundBefore -ne [IntPtr]::Zero) { [MeasuredSaitulsWindow]::SetForegroundWindow($foregroundBefore) | Out-Null }
$firstIdleMs = $sw.ElapsedMilliseconds
$seen = @{}
$children = New-Object System.Collections.Generic.List[object]
function Poll-Children {
    foreach ($c in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.Id)" -ErrorAction SilentlyContinue)) {
        $key = "$($c.ProcessId)/$($c.CreationDate)"
        if (-not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            $children.Add([pscustomobject]@{ t = [math]::Round($sw.Elapsed.TotalSeconds, 1); name = $c.Name; cmd = ($c.CommandLine -replace '\s+', ' ') })
        }
    }
}
$warmEnd = $sw.Elapsed.TotalSeconds + $WarmupSeconds
while ($sw.Elapsed.TotalSeconds -lt $warmEnd) { Poll-Children; Start-Sleep -Milliseconds 250 }
$warmCount = $children.Count
$p.Refresh(); $cpu0 = $p.TotalProcessorTime.TotalMilliseconds
$t0 = $sw.Elapsed.TotalSeconds
while ($sw.Elapsed.TotalSeconds -lt $t0 + $Seconds) { Poll-Children; Start-Sleep -Milliseconds 250 }
$p.Refresh()
$cpu1 = $p.TotalProcessorTime.TotalMilliseconds
$idleChildren = @($children | Select-Object -Skip $warmCount)
$result = [pscustomobject]@{
    exe = $Exe; arguments = $Arguments
    first_input_idle_ms = $firstIdleMs
    warmup_children = $warmCount
    idle_seconds = $Seconds
    idle_children = $idleChildren.Count
    idle_children_per_minute = [math]::Round($idleChildren.Count * 60.0 / $Seconds, 2)
    idle_cpu_ms = [math]::Round($cpu1 - $cpu0, 0)
    idle_cpu_percent_of_one_core = [math]::Round(($cpu1 - $cpu0) / ($Seconds * 10.0), 3)
    working_set_mb = [math]::Round($p.WorkingSet64 / 1MB, 1)
    private_mb = [math]::Round($p.PrivateMemorySize64 / 1MB, 1)
    handles = $p.HandleCount
    threads = $p.Threads.Count
    system_processes_before = $systemProcessesBefore
    system_processes_after = @(Get-Process).Count
    sha256 = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash
    children = $children
}
[MeasuredSaitulsWindow]::QuitOwned($p.Id) | Out-Null
$result | Add-Member -NotePropertyName normal_exit -NotePropertyValue ($p.WaitForExit(5000))
$json = $result | ConvertTo-Json -Depth 4
if ($Out) { Set-Content -LiteralPath $Out -Value $json -Encoding UTF8 }
$json
} finally {
    if ($p -and -not $p.HasExited) {
        [MeasuredSaitulsWindow]::QuitOwned($p.Id) | Out-Null
        $p.WaitForExit(5000) | Out-Null
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    }
    if ($foregroundBefore -ne [IntPtr]::Zero) { [MeasuredSaitulsWindow]::SetForegroundWindow($foregroundBefore) | Out-Null }
}
