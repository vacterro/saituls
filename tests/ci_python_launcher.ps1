# CI-only launcher: pythonw.exe depends on DLLs beside its installed executable.
# Copying that executable to Windows makes ShellExecute succeed while Python
# fails to load. Keep the real interpreter in place and forward the raw Windows
# argument tail, including the intentionally awkward drive-root quoting.
function New-SaiCiPythonLauncher([string]$Pythonw, [string]$Destination) {
    $compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) { throw 'pythonw.exe not found' }
    if (Test-Path -LiteralPath $Destination) { throw 'Refusing to replace an existing launcher' }
    $encodedPath = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes([IO.Path]::GetFullPath($Pythonw)))
    $source = [IO.Path]::GetTempFileName() + '.cs'
    try {
        @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
internal static class SaiCiPythonLauncher {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
    private static extern IntPtr GetCommandLineW();
    [STAThread]
    private static int Main() {
        try {
            string line = Marshal.PtrToStringUni(GetCommandLineW()).TrimStart();
            int end = line.StartsWith("\"") ? line.IndexOf('"', 1) + 1 : line.IndexOf(' ');
            string tail = end < 0 ? "" : line.Substring(end).TrimStart();
            var start = new ProcessStartInfo {
                FileName = Encoding.Unicode.GetString(Convert.FromBase64String("$encodedPath")),
                Arguments = tail, UseShellExecute = false, CreateNoWindow = true
            };
            using (var child = Process.Start(start)) {
                child.WaitForExit();
                return child.ExitCode;
            }
        } catch { return 1; }
    }
}
"@ | Set-Content -LiteralPath $source -Encoding UTF8
        & $compiler /nologo /target:winexe "/out:$Destination" $source
        if ($LASTEXITCODE -ne 0) { throw 'CI Python launcher compilation failed' }
    } finally {
        Remove-Item -LiteralPath $source -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath ($source.Substring(0, $source.Length - 3)) -Force -ErrorAction SilentlyContinue
    }
}
