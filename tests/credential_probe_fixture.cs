using System;
using System.Collections.Generic;

// Compiled only by the native launch harness, never by the production build.
static class CredentialProbeFixture
{
    static int Main(string[] args)
    {
        SaiAccounts.CredentialProbe = delegate {
            return new Dictionary<string, object> {
                { "credential_present", Environment.GetEnvironmentVariable("SAI_FIXTURE_CREDENTIAL_PRESENT") == "1" }
            };
        };
        return SaiAccounts.Main(args);
    }
}
