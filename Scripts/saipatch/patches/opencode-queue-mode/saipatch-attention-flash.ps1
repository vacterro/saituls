<#
.SYNOPSIS
    Patch-owned Windows taskbar/window flash helper for OpenCode attention.

.DESCRIPTION
    Calls user32!FlashWindowEx on the resolved OpenCode HWND with bounded counts.
    NEVER calls SetForegroundWindow / ShowWindow / AltTab. Bounded by default.
    On failure, logs and exits nonzero with WINDOW_HANDLE_UNAVAILABLE.

.PARAMETER Hwnd
    The HWND to flash (decimal string or hex).

.PARAMETER Count
    Bounded flash count (1..20, default 5).

.PARAMETER ForegroundProbe
    TEST-ONLY: if set, the script probes whether the target is already
    foreground (GetForegroundWindow) and suppresses flashing when true, reporting
    FOREGROUND already. Production attention always passes the check in the JS
    caller instead.

.PARAMETER DryRun
    TEST-ONLY: record the would-flash call without invoking Win32.

.PARAMETER LogSink
    TEST-ONLY: file to append one JSON record of the request.
#>
param(
    [Parameter(Mandatory=$true)][string]$Hwnd,
    [int]$Count = 5,
    [switch]$ForegroundProbe,
    [switch]$DryRun,
    [string]$LogSink
)

$ErrorActionPreference = 'Stop'

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class FlashUtil {
  [StructLayout(LayoutKind.Sequential)]
  public struct FLASHWINFO {
    public uint cbSize;
    public IntPtr hwnd;
    public uint dwFlags;
    public uint uCount;
    public uint dwTimeout;
  }
  public const uint FLASHW_ALL = 0x00000003;
  public const uint FLASHW_TRAY = 0x00000002;
  public const uint FLASHW_TIMERNOFG = 0x0000000C;
  public const uint FLASHW_STOP = 0;
  [DllImport("user32.dll")] public static extern bool FlashWindowEx(ref FLASHWINFO pfwi);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern IntPtr GetConsoleWindow();
}
"@ | Out-Null

$hwndVal = [Int64]0
if ($Hwnd -match '^0x') { $hwndVal = [Convert]::ToInt64($Hwnd, 16) } else { $hwndVal = [Int64]$Hwnd }
$hwndPtr = [IntPtr]::new($hwndVal)
if ($hwndPtr -eq [IntPtr]::Zero) {
    if ($LogSink) { @{ts=(Get-Date -Format o); hwnd=$Hwnd; count=$Count; result="WINDOW_HANDLE_UNAVAILABLE"} | ConvertTo-Json -Compress | Out-File -Append -Encoding utf8 $LogSink }
    Write-Error "WINDOW_HANDLE_UNAVAILABLE"
    exit 2
}

$count = [Math]::Min(20, [Math]::Max(1, $Count))

if ($ForegroundProbe) {
    $fg = [FlashUtil]::GetForegroundWindow()
    if ($fg -eq $hwndPtr) {
        if ($LogSink) { @{ts=(Get-Date -Format o); hwnd=$Hwnd; count=$count; result="FOREGROUND"; setForegroundCalls=0} | ConvertTo-Json -Compress | Out-File -Append -Encoding utf8 $LogSink }
        exit 0
    }
}

if ($DryRun) {
    $rec = @{ts=(Get-Date -Format o); hwnd=$Hwnd; count=$count; api="FlashWindowEx"; flags="FLASHW_TRAY|FLASHW_TIMERNOFG"; setForegroundCalls=0; result="DRYRUN"}
    if ($LogSink) { $rec | ConvertTo-Json -Compress | Out-File -Append -Encoding utf8 $LogSink }
    else { $rec | ConvertTo-Json -Compress }
    exit 0
}

$info = New-Object FlashUtil+FLASHWINFO
$info.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($info)
$info.hwnd = $hwndPtr
$info.dwFlags = ([FlashUtil]::FLASHW_TRAY -bor [FlashUtil]::FLASHW_TIMERNOFG)
$info.uCount = [uint32]$count
$info.dwTimeout = 0
$ok = [FlashUtil]::FlashWindowEx([ref]$info)
$rec = @{ts=(Get-Date -Format o); hwnd=$Hwnd; count=$count; api="FlashWindowEx"; flags="FLASHW_TRAY|FLASHW_TIMERNOFG"; setForegroundCalls=0; ok=$ok}
if ($LogSink) { $rec | ConvertTo-Json -Compress | Out-File -Append -Encoding utf8 $LogSink } else { $rec | ConvertTo-Json -Compress }
if (-not $ok) { Write-Warning "FlashWindowEx returned false for $Hwnd"; exit 0 }
exit 0
