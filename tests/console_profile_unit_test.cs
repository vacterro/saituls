// T-265 regression: the managed console contract and the input-language mirror.
//
// Compiled together with Scripts\accounts\SaiAccountsHelper.cs and driven by
// reflection, so the oracle is the helper's own code, not a re-statement of it.
//
//  1. CtrlKeyShortcutsDisabled must be written as 0. Writing 1 -- what every
//     legacy all-ones profile did -- turns OFF Ctrl+C, Ctrl+V and Ctrl+A in every
//     managed console; the other eight console switches are 1.
//  2. The operator's input languages (Preload, Toggle, User Profile) are copied
//     into a stand-in "managed" hive key: EN+RU and the Ctrl+Shift switch
//     arrive, a stale extra slot is removed, a second run changes nothing, and
//     invalid operator identities are refused. The companion managed-context
//     suite checks the strict patterns and fixed registry-write boundaries.
//
// The "operator" is the current user (HKU\<own SID> is HKCU), and the managed
// hive is a scratch key under HKCU\Software, deleted afterwards. Nothing else
// in either hive is touched.
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Security.Principal;
using Microsoft.Win32;

static class ConsoleProfileUnitTest
{
    static int failures, passes;

    static void Check(bool ok, string name, string detail)
    {
        if (ok) { passes++; Console.WriteLine("PASS  " + name); }
        else { failures++; Console.WriteLine("FAIL  " + name + "  -- " + detail); }
    }

    static object Call(string method, params object[] args)
    {
        MethodInfo m = typeof(SaiAccountsHelper).GetMethod(method, BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public);
        if (m == null) throw new Exception("no method " + method);
        return m.Invoke(null, args);
    }

    static int Main()
    {
        // ---- 1. console value contract -------------------------------------
        var dwords = (string[])typeof(SaiAccountsHelper).GetField("ManagedConsoleDwords",
            BindingFlags.Static | BindingFlags.NonPublic).GetValue(null);
        foreach (string name in dwords)
        {
            int want = name == "CtrlKeyShortcutsDisabled" ? 0 : 1;
            int got = (int)Call("ManagedConsoleValue", name);
            Check(got == want, "console value " + name + " = " + want, "got " + got);
        }

        string imageKey = (string)Call("ConsoleSubkeyFor", @"C:\ProgramData\SAI\Accounts\bin\agy.exe");
        Check(imageKey == @"Console\C:_ProgramData_SAI_Accounts_bin_agy.exe",
            "image override belongs below Console with the drive colon preserved", imageKey);

        // ---- 2. allowlisted presentation and executable overrides ------------
        string presentationScratch = @"Software\SaitulsConsolePresentation_" + Guid.NewGuid().ToString("N");
        try
        {
            using (RegistryKey scratchRoot = Registry.CurrentUser.CreateSubKey(presentationScratch))
            using (RegistryKey operatorProfile = scratchRoot.CreateSubKey("operator"))
            using (RegistryKey managedProfile = scratchRoot.CreateSubKey("managed"))
            {
                string vendorKey = (string)Call("ConsoleSubkeyFor", @"C:\ProgramData\SAI\Accounts\bin\agy.exe");
                string guardKey = (string)Call("ConsoleSubkeyFor", @"C:\ProgramData\SAI\Accounts\bin\sai-accounts.exe");
                var keys = new[] { "Console", vendorKey, guardKey, guardKey };
                using (RegistryKey source = operatorProfile.CreateSubKey("Console"))
                using (RegistryKey actual = Registry.CurrentUser.OpenSubKey("Console"))
                {
                    var names = new List<string>((string[])typeof(SaiAccountsHelper).GetField("ConsolePresentationDwords", BindingFlags.Static | BindingFlags.NonPublic).GetValue(null));
                    names.AddRange((string[])typeof(SaiAccountsHelper).GetField("ConsolePresentationStrings", BindingFlags.Static | BindingFlags.NonPublic).GetValue(null));
                    foreach (string name in names)
                    {
                        object value = actual == null ? null : actual.GetValue(name);
                        if (value != null) source.SetValue(name, value, actual.GetValueKind(name));
                    }
                    source.SetValue("ScreenColors", 15, RegistryValueKind.DWord);
                    source.SetValue("PopupColors", 240, RegistryValueKind.DWord);
                    source.SetValue("FaceName", "Terminus (TTF) for Windows", RegistryValueKind.String);
                    source.SetValue("Unrelated", "keep-secret-out", RegistryValueKind.String);
                }
                using (RegistryKey overrideKey = operatorProfile.CreateSubKey(guardKey))
                    overrideKey.SetValue("ScreenColors", 14, RegistryValueKind.DWord);
                using (RegistryKey stock = managedProfile.CreateSubKey("Console"))
                {
                    stock.SetValue("ScreenColors", 7, RegistryValueKind.DWord);
                    stock.SetValue("CtrlKeyShortcutsDisabled", 0, RegistryValueKind.DWord);
                    stock.SetValue("Unrelated", "retain", RegistryValueKind.String);
                }
                var first = (Dictionary<string, object>)Call("ReconcileConsolePresentation", operatorProfile, managedProfile, keys);
                Check((string)first["status"] == "applied", "golden operator presentation replaces stock managed presentation", (string)first["status"]);
                foreach (string key in new[] { "Console", vendorKey, guardKey })
                using (RegistryKey target = managedProfile.OpenSubKey(key))
                {
                    Check(target != null && object.Equals(target.GetValue("ScreenColors"), key == guardKey ? 14 : 15)
                        && target.GetValueKind("ScreenColors") == RegistryValueKind.DWord,
                        "effective presentation and exact DWORD type for " + key, "wrong value or type");
                    Check(target != null && object.Equals(target.GetValue("FaceName"), "Terminus (TTF) for Windows")
                        && target.GetValueKind("FaceName") == RegistryValueKind.String,
                        "font falls back to generic operator profile with exact string type", "font lost");
                }
                using (RegistryKey stock = managedProfile.OpenSubKey("Console"))
                    Check(object.Equals(stock.GetValue("Unrelated"), "retain")
                        && object.Equals(stock.GetValue("CtrlKeyShortcutsDisabled"), 0),
                        "presentation preserves unrelated preferences and input policy", "changed");
                using (RegistryKey target = managedProfile.OpenSubKey(guardKey))
                    Check(target.GetValue("Unrelated") == null, "unallowlisted operator value is never copied", "copied");
                var second = (Dictionary<string, object>)Call("ReconcileConsolePresentation", operatorProfile, managedProfile, keys);
                Check((string)second["status"] == "already_correct" && (int)second["values_written"] == 0,
                    "repeat reconciliation performs zero writes including duplicate keys", (string)second["status"]);
                using (RegistryKey emptyOperator = scratchRoot.CreateSubKey("empty-operator"))
                {
                    var unavailable = (Dictionary<string, object>)Call("ReconcileConsolePresentation", emptyOperator, managedProfile, keys);
                    Check((string)unavailable["status"] == "unavailable" && (int)unavailable["values_written"] == 0,
                        "operator without custom settings preserves safe target fallback", (string)unavailable["status"]);
                }
                using (RegistryKey readOnly = scratchRoot.OpenSubKey("managed"))
                {
                    using (RegistryKey source = operatorProfile.CreateSubKey("Console")) source.SetValue("PopupColors", 1, RegistryValueKind.DWord);
                    var failed = (Dictionary<string, object>)Call("ReconcileConsolePresentation", operatorProfile, readOnly, keys);
                    Check((string)failed["status"] == "failed", "registry write failure differs from unavailable", (string)failed["status"]);
                }
                var badOperator = (Dictionary<string, object>)Call("MirrorOperatorConsolePresentation", "S-1-5-18", managedProfile, keys);
                Check((string)badOperator["status"] == "unavailable", "SYSTEM is not a presentation operator", (string)badOperator["status"]);
                var wrongOperator = (Dictionary<string, object>)Call("MirrorOperatorConsolePresentation", "S-1-5-21-1-2-3-999", managedProfile, keys);
                Check((string)wrongOperator["status"] == "unavailable", "unbound human SID is not a presentation operator", (string)wrongOperator["status"]);
                using (RegistryKey source = operatorProfile.CreateSubKey("Console"))
                    source.SetValue("FontFace", new byte[] { 1, 2, 3 }, RegistryValueKind.Binary);
                using (RegistryKey target = managedProfile.CreateSubKey("Console"))
                    target.SetValue("FontFace", "safe existing font", RegistryValueKind.String);
                Call("ReconcileConsolePresentation", operatorProfile, managedProfile, keys);
                using (RegistryKey target = managedProfile.OpenSubKey("Console"))
                    Check(object.Equals(target.GetValue("FontFace"), "safe existing font")
                        && target.GetValueKind("FontFace") == RegistryValueKind.String,
                        "unsupported operator value type preserves the safe target value", "copied or removed");
            }
        }
        finally { Registry.CurrentUser.DeleteSubKeyTree(presentationScratch, false); }

        // ---- 3. input-language mirror ---------------------------------------
        string sid = WindowsIdentity.GetCurrent().User.Value;
        string scratch = @"Software\SaitulsT265Test_" + Guid.NewGuid().ToString("N");
        string opPreload = null;
        try
        {
            using (RegistryKey op = Registry.CurrentUser.OpenSubKey(@"Keyboard Layout\Preload"))
                if (op != null) opPreload = op.GetValue("1") as string;
            if (opPreload == null)
            {
                Check(false, "operator Preload is readable", "HKCU\\Keyboard Layout\\Preload\\1 missing");
                return Done();
            }

            using (RegistryKey managed = Registry.CurrentUser.CreateSubKey(scratch))
            {
                // A stock profile: one layout, plus a stale slot 7 that the
                // operator does not have, and a junk value that must survive
                // untouched because it is not ours to write.
                using (RegistryKey k = managed.CreateSubKey(@"Keyboard Layout\Preload"))
                {
                    k.SetValue("1", "00000409");
                    k.SetValue("7", "0000040c");
                }
                using (RegistryKey k = managed.CreateSubKey(@"Keyboard Layout"))
                    k.SetValue("Unrelated", "keep");

                string first = (string)Call("MirrorOperatorInputLanguages", sid, managed);
                Check(first == "applied" || first == "already_correct", "mirror reports applied", first);

                var opSlots = new List<string>();
                using (RegistryKey op = Registry.CurrentUser.OpenSubKey(@"Keyboard Layout\Preload"))
                    for (int i = 1; i <= 9; i++) { string v = op.GetValue(i.ToString()) as string; if (v != null) opSlots.Add(v); }
                var gotSlots = new List<string>();
                using (RegistryKey k = managed.OpenSubKey(@"Keyboard Layout\Preload"))
                    for (int i = 1; i <= 9; i++) { string v = k.GetValue(i.ToString()) as string; if (v != null) gotSlots.Add(v); }
                Check(string.Join(",", gotSlots.ToArray()) == string.Join(",", opSlots.ToArray()),
                    "managed Preload equals the operator's (" + string.Join(",", opSlots.ToArray()) + ")",
                    string.Join(",", gotSlots.ToArray()));
                Check(opSlots.Count >= 7 || gotSlots.Count == opSlots.Count,
                    "stale slot the operator does not have is removed", string.Join(",", gotSlots.ToArray()));

                using (RegistryKey op = Registry.CurrentUser.OpenSubKey(@"Keyboard Layout\Toggle"))
                using (RegistryKey k = managed.OpenSubKey(@"Keyboard Layout\Toggle"))
                {
                    string want = op != null ? op.GetValue("Hotkey") as string : null;
                    string got = k != null ? k.GetValue("Hotkey") as string : null;
                    Check(want == null || want == got, "language switch hotkey mirrored (" + want + ")", got ?? "absent");
                }

                using (RegistryKey op = Registry.CurrentUser.OpenSubKey(@"Control Panel\International\User Profile"))
                using (RegistryKey k = managed.OpenSubKey(@"Control Panel\International\User Profile"))
                {
                    string[] want = op != null ? op.GetValue("Languages") as string[] : null;
                    string[] got = k != null ? k.GetValue("Languages") as string[] : null;
                    Check(want == null || (got != null && string.Join("|", got) == string.Join("|", want)),
                        "User Profile languages mirrored", got == null ? "absent" : string.Join("|", got));
                }

                using (RegistryKey k = managed.OpenSubKey(@"Keyboard Layout"))
                    Check(object.Equals(k.GetValue("Unrelated"), "keep"), "values outside the contract are untouched", "changed");

                string second = (string)Call("MirrorOperatorInputLanguages", sid, managed);
                Check(second == "already_correct", "second run is idempotent", second);

                // Refusals: an unknown operator mirrors nothing.
                Check((string)Call("MirrorOperatorInputLanguages", "not-a-sid", managed) == "operator_unknown",
                    "non-SID operator refused", "accepted");
                Check((string)Call("MirrorOperatorInputLanguages", "S-1-5-18", managed) == "operator_unknown",
                    "SYSTEM is never the operator", "accepted");
            }
        }
        finally
        {
            try { Registry.CurrentUser.DeleteSubKeyTree(scratch, false); } catch { }
        }
        return Done();
    }

    static int Done()
    {
        Console.WriteLine("passed " + passes + ", failed " + failures);
        return failures == 0 ? 0 : 1;
    }
}
