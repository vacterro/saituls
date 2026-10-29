# Wait on the exact hidden recorder process owned by the shell test. A fixed
# startup sleep races a cold Python/launcher; a completed process also makes
# it safe to remove the disposable script and result files.
# A cold system launcher on hosted Windows can take more than ten seconds.
# Keep a finite completion bound; timeout still fails and retains the sandbox.
function Wait-SaiShellProbeProcess([IntPtr]$Handle, [uint32]$TimeoutMilliseconds = 60000) {
    if ($Handle -eq [IntPtr]::Zero) {
        return @{ completed = $false; wait = 'NO_PROCESS_HANDLE'; exit = $null }
    }
    try {
        $waitCode = [SaiShellExec]::WaitForSingleObject($Handle, $TimeoutMilliseconds)
        $exitCode = [uint32]0
        $gotExit = [SaiShellExec]::GetExitCodeProcess($Handle, [ref]$exitCode)
        return @{ completed = ($waitCode -eq 0); wait = $waitCode
                  exit = $(if ($gotExit) { $exitCode } else { $null }) }
    } finally {
        [void][SaiShellExec]::CloseHandle($Handle)
    }
}
