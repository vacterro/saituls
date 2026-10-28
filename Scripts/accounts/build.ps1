# Compiles the SAI Accounts control plane binary (sai-accounts.exe).
# Authority for this command line: Scripts/accounts/build.ps1 (cited by
# .github/workflows/test.yml). Requires the Windows .NET Framework csc.
param(
    [string]$OutDir = "$PSScriptRoot\bin",
    # Off by default so a CI build never writes to the machine's install.
    [switch]$Deploy
)

$ErrorActionPreference = 'Stop'
$OutDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutDir)
. (Join-Path $PSScriptRoot 'InstallImage.ps1')
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path $csc)) { Write-Error "csc.exe not found: compiler missing means FAIL, never SKIP" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# A running broker holds sai-accounts.exe open, and csc cannot write over a
# locked image. Only brokers using the exact output image can hold this build
# open. A test or source-tree build must leave the installed plane and other
# isolated builds running. An unreadable process path does not grant ownership.
$outputEnginePath = [System.IO.Path]::GetFullPath((Join-Path $OutDir 'sai-accounts.exe'))
Get-CimInstance Win32_Process -Filter "Name='sai-accounts.exe'" |
    Where-Object {
        $_.CommandLine -like '*--broker*' -and $_.ProcessId -ne $PID -and
        $_.ExecutablePath -and
        [string]::Equals([System.IO.Path]::GetFullPath($_.ExecutablePath),
            $outputEnginePath, [System.StringComparison]::OrdinalIgnoreCase)
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 300

$sources = @("$PSScriptRoot\SaiAccounts.cs")

& $csc -nologo "-out:$OutDir\sai-accounts.exe" -optimize+ `
    -r:System.dll -r:System.Core.dll -r:System.Web.Extensions.dll `
    $sources
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $csc -nologo "-out:$OutDir\sai-antigravity.exe" -optimize+ `
    -r:System.dll -r:System.Core.dll -r:System.Web.Extensions.dll `
    "$PSScriptRoot\SaiAntigravityShim.cs"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $csc -nologo "-out:$OutDir\sai-accounts-helper.exe" -optimize+ `
    -r:System.dll -r:System.Core.dll -r:System.Security.dll -r:System.ServiceProcess.dll -r:System.Web.Extensions.dll `
    "$PSScriptRoot\SaiAccountsHelper.cs"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& "$OutDir\sai-accounts.exe" list | Out-Null
if ($LASTEXITCODE -gt 1) { Write-Error "sai-accounts.exe failed its smoke 'list' run" }
Write-Host "built $OutDir\sai-accounts.exe, $OutDir\sai-antigravity.exe, $OutDir\sai-accounts-helper.exe"

# Building is not deploying. Every consumer -- SAITULS included, which resolves
# C:\ProgramData\SAI\Accounts\bin first -- runs the INSTALLED copy, so a fix can
# sit in this repo looking perfect while the machine keeps the old binary. That
# is not hypothetical: it is exactly how a broker-loop fix shipped and the
# operator kept seeing the old error. Say so on every build.
$engineInstall = "$env:ProgramData\SAI\Accounts\bin\sai-accounts.exe"
if ($Deploy) {
    $installed = Test-Path $engineInstall
    if ($installed) {
        $busy = @(Get-Process sai-accounts -ErrorAction SilentlyContinue)
        if ($busy.Count -gt 0) {
            Write-Warning "a broker is still running (pid $($busy.Id -join ', ')); it keeps the old binary until it is restarted"
        }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $engineInstall) | Out-Null

    # The helper runs as a Windows service. A running service holds its image open,
    # so copying over it either fails on a sharing violation or, worse, leaves SCM
    # running the previous binary while the new one sits on disk looking deployed.
    # The only honest sequence is: stop, copy, prove the bytes match, start, prove
    # the service answers. "Deployed" is a claim these steps have to support.
    $helperInstall = "$env:ProgramData\SAI\Accounts\bin\sai-accounts-helper.exe"
    $serviceName = 'SaiAccountsHelper'
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    # Keyed on EXISTENCE, not on "was it running". A deploy that finds the service
    # already stopped still has to leave it running when it finishes, and a broker
    # holds sai-accounts.exe just as firmly whether or not SCM is up.
    $hasService = $null -ne $service

    # Stop and start are separate steps on purpose. Restarting here would re-lock
    # the image before the copy, which is the sharing violation this whole
    # sequence exists to avoid.
    $stopScript = @"
`$ErrorActionPreference = 'Stop'
Stop-Service -Name '$serviceName' -Force -ErrorAction SilentlyContinue
`$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt `$deadline) {
    `$s = Get-Service -Name '$serviceName' -ErrorAction SilentlyContinue
    if (-not `$s -or `$s.Status -eq 'Stopped') { exit 0 }
    Start-Sleep -Milliseconds 500
}
Write-Error "service $serviceName did not reach Stopped within 30s; the image is still held"
exit 1
"@
    $stopScriptPath = Join-Path $env:TEMP 'sai-accounts-stop-helper.ps1'
    Set-Content -Path $stopScriptPath -Value $stopScript -Encoding UTF8

    if ($hasService) {
        Write-Host "stopping service $serviceName so SCM releases the old image"
        if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            # SCM control needs elevation. Re-run this exact script elevated and
            # wait for it: a silent UAC is already configured on this host, and a
            # half-restarted helper is worse than none.
            Write-Host "elevating to stop/start $serviceName"
            $log = Join-Path $env:TEMP 'sai-accounts-deploy.log'
            # -Verb RunAs cannot be combined with output redirection, so the
            # elevated run transcribes itself. Without this a failed deploy is
            # just an exit code and nobody can tell what actually went wrong.
            $quotedLog = $log.Replace("'", "''")
            $quotedScript = $PSCommandPath.Replace("'", "''")
            $quotedOutput = $OutDir.Replace("'", "''")
            $inner = "Start-Transcript -Path '$quotedLog' -Force | Out-Null; " +
                     "& '$quotedScript' -Deploy -OutDir '$quotedOutput'; " +
                     "`$code = `$LASTEXITCODE; Stop-Transcript | Out-Null; exit `$code"
            $p = Start-Process -FilePath 'powershell' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $inner) `
                -Verb RunAs -Wait -PassThru -WindowStyle Hidden
            if (Test-Path $log) { Get-Content $log | ForEach-Object { Write-Host "  [elevated] $_" } }
            if ($p.ExitCode -ne 0) { Write-Error "elevated deploy failed with exit code $($p.ExitCode) (see $log)" }
            exit $p.ExitCode
        }
        & $stopScriptPath
        if ($LASTEXITCODE -ne 0) { Write-Error "could not stop $serviceName; refusing to copy over a running image" }
    }

    # Brokers hold sai-accounts.exe open, and the GUI's background poll restarts one
    # within seconds of it exiting -- so with the service down (the helper cannot
    # satisfy managed_context_start) the window to copy in is short and real.
    # Only the exact installed image belongs to this deployment. Keep other
    # source/test brokers and operator console title guards running.
    $installedEnginePath = [System.IO.Path]::GetFullPath($engineInstall)
    Get-CimInstance Win32_Process -Filter "Name='sai-accounts.exe'" |
        Where-Object {
            $_.CommandLine -like '*--broker*' -and $_.ProcessId -ne $PID -and
            $_.ExecutablePath -and
            [string]::Equals([System.IO.Path]::GetFullPath($_.ExecutablePath),
                $installedEnginePath, [System.StringComparison]::OrdinalIgnoreCase)
        } |
        ForEach-Object { Write-Host "  stopping broker pid $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 300

    # Title guards stay alive with their vendors. Rename their old mapped image,
    # retain it, and publish the new bytes at the canonical path; no guard or
    # vendor needs to exit so a deployment can replace its executable.
    foreach ($pair in @(@("$OutDir\sai-accounts.exe", $engineInstall), @("$OutDir\sai-accounts-helper.exe", $helperInstall))) {
        $replacement = Install-SaiExecutableImage -Source $pair[0] -Target $pair[1] -AllowedDirectory (Split-Path -Parent $pair[1])
        Write-Host "image $($replacement.Status): $($pair[1]); retained=$($replacement.RetainedImage)"
    }

    # Prove the bytes on disk are the bytes that were just built. A deploy that
    # silently did nothing is the failure this whole check exists to catch.
    foreach ($pair in @(@($helperInstall, "$OutDir\sai-accounts-helper.exe"), @($engineInstall, "$OutDir\sai-accounts.exe"))) {
        $built = (Get-FileHash $pair[1] -Algorithm SHA256).Hash
        $live = (Get-FileHash $pair[0] -Algorithm SHA256).Hash
        Write-Host "sha256 built=$built"
        Write-Host "sha256 live =$live  $($pair[0])"
        if ($built -ne $live) { Write-Error "deployed hash does not match the build hash for $($pair[0])" }
    }

    if ($hasService) {
        # Only now does the service get the new image: bytes first, SCM second.
        Start-Service -Name $serviceName
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            if ((Get-Service -Name $serviceName).Status -eq 'Running') { break }
            Start-Sleep -Milliseconds 500
        }
        if ((Get-Service -Name $serviceName).Status -ne 'Running') { Write-Error "service $serviceName did not come back after the deploy" }
        $status = & "$engineInstall" helper status 2>&1
        Write-Host "helper status: $status"
        if ($LASTEXITCODE -ne 0) { Write-Error "helper did not come back after restart" }
    }

    $shimInstall = "$env:ProgramData\SAI\Antigravity\bin\sai-antigravity.exe"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $shimInstall) | Out-Null
    $replacement = Install-SaiExecutableImage -Source "$OutDir\sai-antigravity.exe" -Target $shimInstall -AllowedDirectory (Split-Path -Parent $shimInstall)
    Write-Host "deployed $engineInstall"
    Write-Host "deployed $helperInstall"
    Write-Host "deployed $shimInstall"
} else {
    Write-Host "NOT deployed. Consumers run the installed copy, not this one."
    Write-Host "  deploy with: powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Deploy"
}
exit 0
