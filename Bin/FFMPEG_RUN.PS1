#Requires -Version 5.1
# T-167 shared media runner. One contract for every Registry\FFMPEG_MENU.REG /
# MKV_FIX.REG leaf: the registry is a launcher, this script owns safety.
#
# Usage (from FFMPEG_RUN.BAT):
#   powershell -NoProfile -ExecutionPolicy Bypass -File FFMPEG_RUN.PS1
#       -InputPath <file> -Args <ffmpeg args string> -Suffix <output suffix>
#
# Output naming (unchanged legacy contract):
#   single file : <input dir>\<input name><suffix>          e.g. song.wav -> song.mp3
#   frame mode  : suffix starting with "\frame_" writes the frame sequence
#                 into <input dir>\<input name>\frame_NNNNNN.<ext>
#
# Status protocol: prints SAIFMPEG_STATUS=<code>:<detail> and exits nonzero on
# any refusal. Exit codes:
#   0 ok            2 input missing      3 ffmpeg missing     4 ffprobe missing
#   5 encoder unavailable (never a silent CPU fallback)   6 collision
#   7 ffmpeg failed 8 output validation failed                9 publish failed
#  10 bad usage
# Everything the runner writes lives in a GUID scratch name first; the source
# is never the output of the run that reads it. ffmpeg runs with -nostdin so
# an overwrite prompt can never hang a visible action.

param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [string]$ArgSpec = '',
    [string]$Suffix = '',
    [string]$FFmpegExe = '',
    [string]$FFprobeExe = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not $ArgSpec -and $env:SAIFMPEG_ARGS) {
    $ArgSpec = $env:SAIFMPEG_ARGS
    Remove-Item Env:SAIFMPEG_ARGS -ErrorAction SilentlyContinue
}

Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
public static class SaifmpegArgs {
    [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string lpCmdLine, out int pNumArgs);
    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr hMem);
    public static string[] Parse(string cmdLine) {
        if (String.IsNullOrWhiteSpace(cmdLine)) return new string[0];
        int argc;
        IntPtr argv = CommandLineToArgvW("x " + cmdLine, out argc);
        if (argv == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        try {
            string[] result = new string[argc - 1];
            for (int i = 1; i < argc; i++) {
                result[i - 1] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv, i * IntPtr.Size));
            }
            return result;
        } finally {
            LocalFree(argv);
        }
    }
    private static string Quote(string arg) {
        if (arg.Length > 0 && arg.IndexOf('"') < 0) {
            bool bare = true;
            foreach (char c in arg) if (Char.IsWhiteSpace(c)) { bare = false; break; }
            if (bare) return arg;
        }
        StringBuilder quoted = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in arg) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') quoted.Append('\\', slashes * 2 + 1);
            else quoted.Append('\\', slashes);
            quoted.Append(c);
            slashes = 0;
        }
        quoted.Append('\\', slashes * 2).Append('"');
        return quoted.ToString();
    }
    private static string Join(string[] args) {
        string[] quoted = new string[args.Length];
        for (int i = 0; i < args.Length; i++) quoted[i] = Quote(args[i]);
        return String.Join(" ", quoted);
    }
    public static int Run(string exe, string[] args, string stdoutPath, string stderrPath) {
        ProcessStartInfo start = new ProcessStartInfo(exe, Join(args));
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;
        using (Process process = Process.Start(start)) {
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            process.WaitForExit();
            System.Threading.Tasks.Task.WaitAll(stdout, stderr);
            File.WriteAllText(stdoutPath, stdout.Result, new UTF8Encoding(false));
            File.WriteAllText(stderrPath, stderr.Result, new UTF8Encoding(false));
            return process.ExitCode;
        }
    }
}
'@

function Stop-Run([int]$Code, [string]$Detail, [object]$Cleanup) {
    if ($Cleanup) { & $Cleanup }
    Write-Output ("SAIFMPEG_STATUS={0}:{1}" -f $Code, $Detail)
    exit $Code
}

$binDir = $PSScriptRoot
if (-not $FFmpegExe)  { $FFmpegExe  = Join-Path $binDir 'FFMPEG.EXE' }
if (-not $FFprobeExe) { $FFprobeExe = Join-Path $binDir 'FFPROBE.EXE' }

if (-not ($InputPath -and (Test-Path -LiteralPath $InputPath -PathType Leaf))) {
    Stop-Run 2 "input not found: $InputPath"
}
if (-not (Test-Path -LiteralPath $FFmpegExe -PathType Leaf)) {
    Stop-Run 3 "ffmpeg missing: $FFmpegExe"
}
if (-not (Test-Path -LiteralPath $FFprobeExe -PathType Leaf)) {
    Stop-Run 4 "ffprobe missing: $FFprobeExe"
}
$InputPath = (Resolve-Path -LiteralPath $InputPath).ProviderPath
$inDir  = Split-Path -Parent $InputPath
$inName = [IO.Path]::GetFileNameWithoutExtension($InputPath)

# ---- encoder capability: refuse before conversion, never fall back --------
function Get-RequestedEncoders([string]$ArgSpec) {
    $found = @()
    $toks = [SaifmpegArgs]::Parse($ArgSpec)
    for ($i = 0; $i -lt $toks.Count; $i++) {
        if ($toks[$i] -in @('-c:v', '-c:a', '-c:s', '-vcodec', '-acodec', '-scodec') -and $i + 1 -lt $toks.Count) {
            $found += $toks[$i + 1]
        }
    }
    return $found | Select-Object -Unique
}

function Get-EncoderCodecKind([string]$Encoder, [string]$FFmpegExe, [string]$FFprobeExe) {
    $encList = & $FFmpegExe -hide_banner -encoders 2>$null
    $line = @($encList | Where-Object { $_ -match ('^\s*\S+\s+' + [regex]::Escape($Encoder) + '\s') })[0]
    if (-not $line) { return 'unknown' }
    if ($line -match '^\s*(\S)\S*\s+\S') { return $Matches[1] }
    return 'unknown'
}

$encoders = Get-RequestedEncoders $ArgSpec
foreach ($enc in $encoders) {
    if ($enc -in @('copy')) { continue }
    $kind = Get-EncoderCodecKind $enc $FFmpegExe $FFprobeExe
    if ($kind -eq 'unknown') { continue }   # e.g. "aac" name inside -c:a aac
    $probeArgs = @('-hide_banner', '-loglevel', 'error', '-y')
    if ($kind -eq 'V') {
        $probeArgs += @('-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=5:duration=0.2')
    } elseif ($kind -eq 'A') {
        $probeArgs += @('-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.1')
    } else {
        continue
    }
    $probeArgs += @('-c', $enc, '-f', 'null', 'NUL')
    $probeLog = Join-Path $env:TEMP ("saifmpeg_cap_{0}.log" -f [guid]::NewGuid().ToString('N'))
    try {
        $probeExit = [SaifmpegArgs]::Run($FFmpegExe, $probeArgs, $probeLog, "$probeLog.err")
    } catch {
        Remove-Item $probeLog, "$probeLog.err" -Force -ErrorAction SilentlyContinue
        Stop-Run 5 "encoder probe could not start: $($_.Exception.Message)"
    }
    Remove-Item $probeLog, "$probeLog.err" -Force -ErrorAction SilentlyContinue
    if ($probeExit -ne 0) {
        Stop-Run 5 "encoder '$enc' is not usable on this machine - action refused, no fallback"
    }
}

# ---- output planning -------------------------------------------------------
$frameMode = $Suffix -match '^\\?frame_.*%0?\d*d\.' -or $Suffix -match '^\\?frame_'
if ($frameMode) {
    $ext = [IO.Path]::GetExtension($Suffix)
    $outDir = Join-Path $inDir $inName
    $pattern = 'frame_%06d' + $ext
} else {
    $ext = [IO.Path]::GetExtension($Suffix)
    $outPath = Join-Path $inDir ($inName + $Suffix)
    if ($outPath -ieq $InputPath) {
        Stop-Run 6 "output would overwrite the input: $outPath"
    }
}

$guid = [guid]::NewGuid().ToString('N')
$cleanupPaths = New-Object System.Collections.Generic.List[string]
$cleanup = {
    foreach ($p in $cleanupPaths) {
        try {
            if (Test-Path -LiteralPath $p -PathType Container) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }
            elseif (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
        } catch { }
    }
}.GetNewClosure()

if ($frameMode) {
    $scratchDir = Join-Path $inDir ("_run_{0}" -f $guid)
    New-Item -ItemType Directory -Force -Path $scratchDir | Out-Null
    $cleanupPaths.Add($scratchDir)
    $outArg = Join-Path $scratchDir $pattern
} else {
    $scratchPath = Join-Path $inDir ("_run_{0}{1}" -f $guid, $ext)
    $cleanupPaths.Add($scratchPath)
    $outArg = $scratchPath
}

# ---- run ffmpeg ------------------------------------------------------------
$ffArgs = @([SaifmpegArgs]::Parse($ArgSpec))
$inputExt = [IO.Path]::GetExtension($InputPath).ToLower()
$imageExts = @('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tga', '.tiff', '.webp')
if ($inputExt -in $imageExts -and -not $frameMode) {
    $argList = @('-hide_banner', '-nostdin', '-y', '-i', $InputPath) + $ffArgs + @('-frames:v', '1', $outArg)
} elseif ($frameMode) {
    $argList = @('-hide_banner', '-nostdin', '-y', '-i', $InputPath) + $ffArgs + @('-f', 'image2', $outArg)
} else {
    $argList = @('-hide_banner', '-nostdin', '-y', '-i', $InputPath) + $ffArgs + @($outArg)
}
$logPath = Join-Path $inDir ("_run_{0}.log" -f $guid)
$cleanupPaths.Add($logPath)
$cleanupPaths.Add("$logPath.err")
try {
    $ffrc = [SaifmpegArgs]::Run($FFmpegExe, $argList, $logPath, "$logPath.err")
} catch {
    Stop-Run 7 "ffmpeg could not start: $($_.Exception.Message)" $cleanup
}
if ($frameMode) {
    $produced = @(Get-ChildItem -LiteralPath $scratchDir -Filter 'frame_*' -File -ErrorAction SilentlyContinue)
    $frameOk = ($ffrc -eq 0 -and $produced.Count -gt 0)
    if (-not $frameOk) {
        Get-Content "$logPath.err" -ErrorAction SilentlyContinue | Select-Object -Last 5 | ForEach-Object { Write-Output $_ }
        Get-Content $logPath -ErrorAction SilentlyContinue | Select-Object -Last 5 | ForEach-Object { Write-Output $_ }
        Remove-Item $logPath, "$logPath.err" -Force -ErrorAction SilentlyContinue
        Stop-Run 7 "ffmpeg frame run failed (exit $ffrc)" $cleanup
    }
    Remove-Item $logPath, "$logPath.err" -Force -ErrorAction SilentlyContinue
} elseif ($ffrc -ne 0 -or -not (Test-Path -LiteralPath $outArg)) {
    Get-Content "$logPath.err" -ErrorAction SilentlyContinue | Select-Object -Last 5 | ForEach-Object { Write-Output $_ }
    Get-Content $logPath -ErrorAction SilentlyContinue | Select-Object -Last 5 | ForEach-Object { Write-Output $_ }
    Stop-Run 7 "ffmpeg failed (exit $ffrc), source untouched" $cleanup
} else {
    Remove-Item $logPath, "$logPath.err" -Force -ErrorAction SilentlyContinue
}

# ---- validate the scratch output before it may replace anything ------------
if ($frameMode) {
    # frames were already counted; skip per-file ffprobe (image2 pattern is
    # not a probe target) and go straight to publish
    $produced = @(Get-ChildItem -LiteralPath $scratchDir -Filter 'frame_*' -File -ErrorAction SilentlyContinue)
    if ($produced.Count -eq 0) { Stop-Run 8 "frame run produced no frames" $cleanup }
    if (Test-Path -LiteralPath $outDir) {
        $existing = @(Get-ChildItem -LiteralPath $outDir -Filter 'frame_*' -File -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0) {
            Stop-Run 9 "frame directory already contains frames: $outDir - run refused, existing frames preserved" $cleanup
        }
    }
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    try {
        foreach ($frame in $produced) {
            $destination = Join-Path $outDir $frame.Name
            Copy-Item -LiteralPath $frame.FullName -Destination $destination -ErrorAction Stop
            $cleanupPaths.Add($destination)
        }
    } catch {
        Stop-Run 9 "frame publish failed: $($_.Exception.Message)" $cleanup
    }
    Remove-Item -LiteralPath $scratchDir -Recurse -Force -ErrorAction SilentlyContinue
    foreach ($frame in $produced) { $cleanupPaths.Remove((Join-Path $outDir $frame.Name)) }
    Write-Output ("SAIFMPEG_STATUS=0:{0} frames -> {1}" -f $produced.Count, $outDir)
    exit 0
}

$probeJson = Join-Path $inDir ("_run_{0}.probe.json" -f $guid)
$cleanupPaths.Add($probeJson)
$cleanupPaths.Add("$probeJson.err")
$probeArgs = @('-hide_banner', '-loglevel', 'error',
    '-show_entries', 'format=format_name,duration',
    '-show_entries', 'stream=codec_type,codec_name',
    '-of', 'json', $outArg)
try {
    $probeExit = [SaifmpegArgs]::Run($FFprobeExe, $probeArgs, $probeJson, "$probeJson.err")
} catch {
    Stop-Run 8 "ffprobe could not start: $($_.Exception.Message)" $cleanup
}
if ($probeExit -ne 0) {
    Stop-Run 8 "ffprobe could not open the produced output" $cleanup
}
$probe = @(Get-Content $probeJson -ErrorAction SilentlyContinue)
Remove-Item $probeJson -Force -ErrorAction SilentlyContinue
Remove-Item "$probeJson.err" -Force -ErrorAction SilentlyContinue
try { $meta = ($probe -join "`n") | ConvertFrom-Json } catch {
    Stop-Run 8 "ffprobe output was not valid JSON" $cleanup
}
if (-not $meta.streams -or $meta.streams.Count -eq 0) {
    Stop-Run 8 "output has no decodable streams" $cleanup
}
if ($meta.format.duration -and [double]$meta.format.duration -le 0) {
    Stop-Run 8 "output duration is not positive" $cleanup
}

# ---- publish ---------------------------------------------------------------
# Publication is ATOMIC for an existing destination: one native Windows
# replacement operation ([IO.File]::Replace / ReplaceFile semantics) swaps the
# validated scratch into the canonical destination. The old destination stays
# visible until the replacement commits; there is no two-step move-aside
# window in which a crash could leave the canonical destination missing.
#   - failure before commit  -> old destination byte-identical, exit 9
#   - success                -> new destination committed, scratch consumed
#   - backup removal happens ONLY after commit and never reverts it
# A destination that does not yet exist is created with a same-volume Move-Item.
$pubCommitted = $false
$pubBackup = $null
if (Test-Path -LiteralPath $outPath) {
    # a real backup name (PowerShell 5.1 coerces $null string args to '' and
    # ReplaceFile rejects an empty backup path)
    $pubBackup = Join-Path $inDir ("_run_{0}.bak{1}" -f $guid, $ext)
    try {
        # same volume: scratch lives in the input directory by construction
        [IO.File]::Replace($scratchPath, $outPath, $pubBackup, $true)
        $pubCommitted = $true
    } catch {
        Stop-Run 9 "atomic replacement refused or failed: $($_.Exception.Message)" $cleanup
    }
} else {
    try {
        Move-Item -LiteralPath $scratchPath -Destination $outPath -ErrorAction Stop
        $pubCommitted = $true
    } catch {
        Stop-Run 9 "publish failed: $($_.Exception.Message)" $cleanup
    }
}
[void]$cleanupPaths.Remove($scratchPath)
if (-not $pubCommitted) {
    # unreachable by construction; kept as an explicit fail-closed guard
    Stop-Run 9 "publication did not commit" $cleanup
}
if ($pubBackup) {
    # post-commit cleanup only: the new destination is already canonical and
    # a failure here must not revert or corrupt it
    try {
        Remove-Item -LiteralPath $pubBackup -Force -ErrorAction Stop
    } catch {
        Write-Output ("SAIFMPEG_STATUS=0:{0} (backup of previous output left at {1})" -f $outPath, $pubBackup)
        exit 0
    }
}
Write-Output ("SAIFMPEG_STATUS=0:{0}" -f $outPath)
exit 0
