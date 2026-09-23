param(
    [string]$ScriptSource = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)) 'SAITULS.cs')
)

$ErrorActionPreference = 'Stop'
$failures = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host "PASS  $Name $Detail" }
    else { Write-Host "FAIL  $Name $Detail"; $script:failures++ }
}

$source = Get-Content -LiteralPath $ScriptSource -Raw
$mainAt = $source.IndexOf('static void Main(string[] args)')
$eventAt = $source.IndexOf('new System.Threading.EventWaitHandle(false', $mainAt)
$mutexAt = $source.IndexOf('new System.Threading.Mutex(true, "Local\\SaitulsApp"', $mainAt)
$setAt = $source.IndexOf('showEvent.Set();', $mainAt)
$runAt = $source.IndexOf('Application.Run();', $mainAt)

Check 'activation event is published before mutex ownership' ($mainAt -ge 0 -and $eventAt -gt $mainAt -and $eventAt -lt $mutexAt) "event=$eventAt mutex=$mutexAt"
Check 'secondary signals the already-open shared event' ($setAt -gt $mutexAt -and $setAt -lt $runAt)
Check 'secondary no longer relies on OpenExisting during publication gap' ($source.IndexOf('EventWaitHandle.OpenExisting("Local\\SaitulsShow")', $mainAt) -lt 0)

$probe = @'
using System;
using System.Threading;

public static class SaitulsActivationRaceProbe
{
    public static int Run(int iterations)
    {
        int failures = 0;
        for (int i = 0; i < iterations; i++)
        {
            string suffix = Guid.NewGuid().ToString("N");
            string eventName = "Local\\SaitulsShow_Test_" + suffix;
            string mutexName = "Local\\SaitulsApp_Test_" + suffix;
            using (var showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, eventName))
            {
                bool primary;
                using (var owner = new Mutex(true, mutexName, out primary))
                {
                    if (!primary) { failures++; continue; }
                    bool secondaryUnexpectedlyOwned = false;
                    var secondary = new Thread(() =>
                    {
                        using (var sameEvent = new EventWaitHandle(false, EventResetMode.AutoReset, eventName))
                        {
                            bool createdNew;
                            using (var sameMutex = new Mutex(true, mutexName, out createdNew))
                            {
                                secondaryUnexpectedlyOwned = createdNew;
                                if (!createdNew) sameEvent.Set();
                            }
                        }
                    });
                    secondary.Start();
                    if (!secondary.Join(2000) || secondaryUnexpectedlyOwned) { failures++; continue; }

                    // Models startup work before RegisterWaitForSingleObject.
                    Thread.Sleep(10);
                    if (!showEvent.WaitOne(1000)) failures++;
                }
            }
        }
        return failures;
    }
}
'@

Add-Type -TypeDefinition $probe -Language CSharp
$iterations = 100
$raceFailures = [SaitulsActivationRaceProbe]::Run($iterations)
Check 'pre-UI activation stays latched across repeated races' ($raceFailures -eq 0) "$iterations iterations, failures=$raceFailures"

Write-Host '---'
if ($failures) { Write-Host "FAILED ($failures failure(s))"; exit 1 }
Write-Host 'PASS (0 failures)'
exit 0
