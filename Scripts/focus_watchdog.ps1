param(
    [int]$DurationMinutes = 20,
    [string]$LogPath = "C:\ProgramData\SAI\Accounts\logs\focus-watchdog.log"
)
$ErrorActionPreference = 'SilentlyContinue'
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class FW {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder t, int n);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr hWnd, StringBuilder t, int n);
}
"@
$end = (Get-Date).AddMinutes($DurationMinutes)
"=== focus watchdog start $(Get-Date) duration ${DurationMinutes}m ===" | Out-File -FilePath $LogPath -Encoding UTF8
$seen = @{}
# WMI process creation watcher
$watcher = New-Object System.Management.ManagementEventWatcher "SELECT * FROM Win32_ProcessStartTrace"
$watcher.Start()
while ((Get-Date) -lt $end) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    $fg = [FW]::GetForegroundWindow()
    $pid = 0
    [FW]::GetWindowThreadProcessId($fg, [ref]$pid) | Out-Null
    $title = New-Object System.Text.StringBuilder 256
    $cls = New-Object System.Text.StringBuilder 128
    [FW]::GetWindowText($fg, $title, 256) | Out-Null
    [FW]::GetClassName($fg, $cls, 128) | Out-Null
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    $pname = if ($proc) { $proc.ProcessName } else { "?" }
    # log foreground if changed from last
    $key = "$pid|$($title.ToString())"
    if ($lastKey -ne $key) {
        "$ts FG pid=$pid name=$pname title='$($title.ToString())' class='$($cls.ToString())'" | Out-File -FilePath $LogPath -Append -Encoding UTF8
        $script:lastKey = $key
    }
    # drain process start events
    $evt = $watcher.WaitForNextEvent(200)
    if ($evt) {
        $newPid = $evt.Properties['ProcessID'].Value
        $newName = $evt.Properties['ProcessName'].Value
        "$ts PROCSTART pid=$newPid name=$newName" | Out-File -FilePath $LogPath -Append -Encoding UTF8
    }
    Start-Sleep -Milliseconds 200
}
$watcher.Stop()
"=== focus watchdog end $(Get-Date) ===" | Out-File -FilePath $LogPath -Append -Encoding UTF8
