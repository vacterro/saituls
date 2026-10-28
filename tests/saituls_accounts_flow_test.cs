// Accounts runtime UX tests (SAITULS).
//
// These drive the REAL Accounts tab in SAITULS.cs against a stand-in engine
// (tests/fake_sai_accounts/FakeAccountsEngine.cs). Not source regexes: the
// form is constructed, the real actions are invoked, and the engine call log
// is asserted for ORDER and IDENTITY. "Never fall back to another account"
// and "never fall back to another workdir" are claims about what actually
// reaches the engine, so they are tested as exactly that.
//
// Compiled together with SAITULS.cs; -main: picks this entry point.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

static class AccountsFlowTest
{
    const BindingFlags Any = BindingFlags.Instance | BindingFlags.Static
        | BindingFlags.NonPublic | BindingFlags.Public;

    static string Engine, Scenario, CallLog, Counter, WorkDir;
    static int _passed, _failed;

    static void Check(bool ok, string what, string detail)
    {
        if (ok) { Console.WriteLine("PASS  " + what); _passed++; }
        else { Console.WriteLine("FAIL  " + what + "  -- " + detail); _failed++; }
    }

    // ---- reflection into the app, so SAITULS.cs needs no test seams --------
    static object F(object form, string name)
    {
        return form.GetType().GetField(name, Any).GetValue(form);
    }
    static void SF(object form, string name, object value)
    {
        form.GetType().GetField(name, Any).SetValue(form, value);
    }
    static object M(object form, string name, params object[] argv)
    {
        var mi = form.GetType().GetMethod(name, Any);
        if (mi == null) throw new Exception("no method " + name);
        return mi.Invoke(form, argv);
    }
    static object SM(Type t, string name, params object[] argv)
    {
        var mi = t.GetMethod(name, Any);
        if (mi == null) throw new Exception("no static method " + name);
        return mi.Invoke(null, argv);
    }
    static T Field<T>(object o, string name)
    {
        object v = F(o, name);
        return v == null ? default(T) : (T)v;
    }
    static object GetOf(object row, string name)
    {
        return row.GetType().GetField(name, Any).GetValue(row);
    }
    static void SetOf(object row, string name, object value)
    {
        row.GetType().GetField(name, Any).SetValue(row, value);
    }
    static string CompactOf(object row) { return (string)GetOf(row, "Compact"); }

    static object MakeRow(string compact, string state, bool hidden, string backend, string ctx)
    {
        Type t = typeof(Saituls.SaitulsForm).GetNestedType("AccountRow", Any);
        object row = Activator.CreateInstance(t);
        SetOf(row, "Id", "id-" + compact);
        SetOf(row, "Compact", compact);
        SetOf(row, "State", state);
        SetOf(row, "Hidden", hidden);
        SetOf(row, "Backend", backend);
        SetOf(row, "Provider", "antigravity");
        SetOf(row, "Context", ctx);
        return row;
    }

    // ---- form under test ---------------------------------------------------
    static Saituls.SaitulsForm MakeForm(bool withWorkDir)
    {
        string dir = Path.Combine(Path.GetTempPath(), "saituls-acc-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        var s = new Saituls.SaitulsSettings(dir);
        s.Load();
        s.LastWorkDir = withWorkDir ? WorkDir : "";
        var f = new Saituls.SaitulsForm(s, null);
        // Create the handle so InvokeRequired/BeginInvoke behave as they do in
        // the running app. The form itself is never shown.
        IntPtr h = f.Handle;
        M(f, "LoadAccounts");
        // The background status tick is a real production path, and the one
        // test that is ABOUT it drives it through Sweep(), which rewinds this
        // clock. Every other test wants the scenario it armed to be the only
        // status probe in flight: an incidental probe spends the fake engine's
        // shared "first N probes offline" budget and appends to the call log,
        // so "an OFFLINE AG2 starts its context first" and the Switch-offered
        // cases would pass or fail on timing. Park its throttle in the future
        // and stop the background timer so nothing runs until a test asks.
        SetOf(f, "_accountsPollAt", DateTime.Now.AddYears(1));
        var tick = GetOf(f, "_tick") as System.Windows.Forms.Timer;
        if (tick != null) tick.Stop();
        return f;
    }

    static void Pump(Func<bool> done, int budgetMs)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < budgetMs)
        {
            Application.DoEvents();
            if (done()) return;
            Thread.Sleep(10);
        }
    }

    static string Note(Saituls.SaitulsForm f) { return (string)F(f, "_accountsNote"); }
    static bool Busy(Saituls.SaitulsForm f) { return (bool)F(f, "_accountsBusy"); }

    static bool Select(Saituls.SaitulsForm f, string compact)
    {
        var list = (IList)F(f, "_accounts");
        foreach (object row in list)
            if (CompactOf(row) == compact) { SF(f, "_selectedAccount", GetOf(row, "Id")); return true; }
        return false;
    }

    static object Row(Saituls.SaitulsForm f, string compact)
    {
        var list = (IList)F(f, "_accounts");
        foreach (object row in list) if (CompactOf(row) == compact) return row;
        throw new Exception("no row " + compact);
    }

    static int _sceneGen = 0;
    static void Scene(params string[] lines)
    {
        var list = new List<string>(lines);
        list.Add("generation=" + Interlocked.Increment(ref _sceneGen));
        File.WriteAllLines(Scenario, list.ToArray());
    }
    // Arm AFTER the form exists. LoadAccounts runs its own status probes while
    // the form is built, and those spend the scenario's "offline for the first
    // N probes" budget before the test has done anything -- so a scenario meant
    // to read OFFLINE silently reads ONLINE and the test passes for the wrong
    // reason. Clearing after construction also scopes the call log to the
    // action under test, which is what "never launches anything" needs.
    static void Arm(params string[] lines) { ResetLog(); Scene(lines); }

    static string[][] Calls()
    {
        if (!File.Exists(CallLog)) return new string[0][];
        // Opened with FileShare.ReadWrite, NOT File.ReadAllLines. The log is an
        // append target and a fake engine holds a write handle on it for the
        // whole of a call; File.ReadAllLines asks for FileShare.Read, which
        // DENIES that writer, so every read raced every write. Sharing the
        // writer's access instead of fighting it is the whole fix -- the retry
        // below is only there for the delete-and-recreate window ResetLog makes.
        string[] raw = null;
        for (int attempt = 0; attempt < 6 && raw == null; attempt++)
        {
            try
            {
                using (var fs = new FileStream(CallLog, FileMode.Open, FileAccess.Read,
                                                FileShare.ReadWrite | FileShare.Delete))
                using (var sr = new StreamReader(fs))
                    raw = sr.ReadToEnd().Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
            }
            catch (IOException) { Thread.Sleep(25); }
            catch (UnauthorizedAccessException) { Thread.Sleep(25); }
        }
        if (raw == null) throw new IOException("call log stayed locked: " + CallLog);
        var outp = new string[raw.Length][];
        for (int i = 0; i < raw.Length; i++) outp[i] = raw[i].Split('\t');
        return outp;
    }
    // Let a status probe the app started on its own (LoadAccounts, an action, a
    // repaint) finish and write before the files below are cleared. Otherwise it
    // lands in the NEXT test's log, scenario or counter -- one stray "status"
    // can spend the next test's offline budget, and the failures then depend on
    // how loaded the machine is. Nothing is asserted here; the wait only orders
    // the harness against the background work the form really does.
    static void Drain(int budgetMs)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < budgetMs) { Application.DoEvents(); Thread.Sleep(10); }
        // A fixed wait only orders the harness against work that happens to be
        // fast. A probe whose fake engine was spawned before ResetLog but
        // reaches its counter after it spends the NEXT scene's offline budget
        // (T20a/T20b read ONLINE and skipped `context start`, 3 runs in 8).
        // Quiescence is the real condition: no fake engine is alive, so no
        // earlier call can still write the counter, the log or the scenario.
        var quiet = Stopwatch.StartNew();
        while (quiet.ElapsedMilliseconds < 15000)
        {
            Process[] alive = Process.GetProcessesByName("fake-sai-accounts");
            int n = alive.Length;
            foreach (Process p in alive) p.Dispose();
            if (n == 0) break;
            Application.DoEvents();
            Thread.Sleep(15);
        }
    }
    static void ResetLog()
    {
        Drain(400);
        if (File.Exists(CallLog)) File.Delete(CallLog);
        if (File.Exists(Scenario)) File.Delete(Scenario);
        // The probe counter has to reset too, or "offline for the first N"
        // carries over from the previous test.
        if (File.Exists(Counter)) File.Delete(Counter);
    }
    static int Where(string verb, string selector)
    {
        string[][] c = Calls();
        for (int i = 0; i < c.Length; i++)
            if (c[i].Length > 1 && c[i][0] == verb && c[i][1] == selector) return i;
        return -1;
    }
    // The exact argv the engine received for a launch of this selector.
    static string[] LaunchCall(string selector)
    {
        foreach (string[] a in Calls())
            if (a.Length > 1 && a[0] == "launch" && a[1] == selector) return a;
        return null;
    }
    static bool HasCall(string verb, string selector) { return Where(verb, selector) >= 0; }
    // Some verbs are issued with the account_id rather than the compact label.
    // Matching on either is what makes "the switch named AG2" a claim about the
    // account rather than a claim about the spelling the GUI happened to use.
    static bool HasCallFor(string verb, string compact, Saituls.SaitulsForm f)
    {
        if (HasCall(verb, compact)) return true;
        try { return HasCall(verb, (string)GetOf(Row(f, compact), "Id")); }
        catch { return false; }
    }
    static bool AnyLaunch() { return Where("launch", "AG1") >= 0 || Where("launch", "AG2") >= 0
        || Where("launch", "AG3") >= 0; }
    static string Show()
    {
        var sb = new System.Text.StringBuilder();
        foreach (string[] a in Calls()) sb.Append(string.Join(" ", a)).Append(" | ");
        return sb.ToString();
    }

    [STAThread]
    static int Main(string[] args)
    {
        bool focused = args.Length == 1 && args[0] == "--offline-start-and-switch";
        bool identityFocused = args.Length == 1 && args[0] == "--identity-status-and-switch";
        bool catalogFocused = args.Length == 1 && args[0] == "--provider-catalog-and-layout";
        if (args.Length > 0 && !focused && !identityFocused && !catalogFocused)
        {
            Console.WriteLine("FAIL  unknown runtime test selection");
            return 1;
        }
        string bin = AppDomain.CurrentDomain.BaseDirectory;
        Engine = Path.Combine(bin, "fake-sai-accounts.exe");
        Scenario = Path.Combine(bin, "fake-scenario.txt");
        CallLog = Path.Combine(bin, "fake-calls.log");
        Counter = Path.Combine(bin, "fake-probes.txt");
        WorkDir = Path.Combine(bin, "project-a");
        Directory.CreateDirectory(WorkDir);
        Environment.SetEnvironmentVariable("SAITULS_LIMITS_DIR", Path.Combine(WorkDir, "isolated-limits"));
        Environment.SetEnvironmentVariable("SAI_ACCOUNTS_EXE", Engine);
        Environment.SetEnvironmentVariable("FAKE_ENGINE_SCENARIO", Scenario);
        Environment.SetEnvironmentVariable("FAKE_ENGINE_LOG", CallLog);
        Environment.SetEnvironmentVariable("FAKE_ENGINE_STATE", Counter);

        if (!File.Exists(Engine))
        {
            Console.WriteLine("FAIL  fake engine missing: " + Engine);
            return 1;
        }

        try
        {
            if (catalogFocused)
            {
                T45ProviderCatalogIsTheDialogsSingleSource();
                T46PlannedProvidersCannotNameABackend();
                T47ExistingProvidersStillCreateAccounts();
                T48CatalogNeverPromotesAProviderTheEngineCannotCreate();
            }
            else if (identityFocused)
            {
                Test29IdentityFingerprintReachesTheRow();
                T37SwitchIntoTheSameAccountIsReportedAsNoSwitch();
                T38SwitchStopsWhenTheCredentialIsStillThere();
            }
            else if (focused)
            {
                Test20SignInStartsAnOfflineContextFirst();
                T43SwitchingThePrimaryTouchesOnlyThePrimary();
            }
            else
            {
            Test1OfflineStartsContext();
            Test2LaunchesSelectedAccount();
            Test3NoFallbackToAnotherAccount();
            Test3cLifecycleAndHiddenRefuse();
            Test4ExplicitWorkdir();
            Test5MissingWorkdirAsks();
            Test6InaccessibleWorkdirFailsClosed();
            Test7StartTimeout();
            Test8AuthRequired();
            Test9PrimaryUserNoContext();
            Test10StopContext();
            Test11SetHiddenTrueFalse();
            Test12HiddenStaysListed();
            Test13HiddenSuppressesLaunch();
            Test14HungChildBounded();
            Test15UiThreadNotBlocked();
            Test16FailedConnectRendersTheRealError();
            Test17ConnectSuccessShowsOnline();
            Test18SignInIsNotAConsoleLaunch();
            Test19SignInNeverTouchesAWorkdir();
            Test20SignInStartsAnOfflineContextFirst();
            Test21SignInOnAnOnlineAccountSkipsTheContextStart();
            Test22SignInNeverFallsBackToAnotherAccount();
            Test23AccessDeniedProvisionsThenRetriesTheSamePath();
            Test24NotFoundNeverProvisions();
            Test25ProvisioningHappensAtMostOnce();
            Test26EveryGrantFailureNamesItself();
            Test288SaipenReadinessGatesTheConsole();
            Test27AcceptedGrantThatStillDoesNotWorkSaysSo();
            Test28ConnectAndSignInNeverBlockTheUiThread();
            Test29IdentityFingerprintReachesTheRow();
            Test29dDistinctIdentitiesStayDistinct();
            Test29fUnprovenIdentityIsNotASharedOne();
            Test29hDiagnosticsCarryTheIdentity();
            Test29kUnprovenIdentityIsLabelledInDiagnostics();
            T30IdentityLabelReachesTheRow();
            T31GroupingKeysOnFingerprintNotLabel();
            T32DuplicateFingerprintsWarnOnBothRows();
            T33DistinctFingerprintsDoNotWarn();
            T34SwitchIsOfferedOnlyWhereItMeansSomething();
            T35SwitchTargetsOnlyTheSelectedContext();
            T36SwitchCompletesToANewIdentity();
            T37SwitchIntoTheSameAccountIsReportedAsNoSwitch();
            T38SwitchStopsWhenTheCredentialIsStillThere();
            T39ConnectNeverAuthenticates();
            T40NoTokenMaterialInTheDiagnostics();
            T41SwitchPollDoesNotTouchTheUiThread();
            T42CredentialTimestampIsNotAnIdentity();
            T43SwitchingThePrimaryTouchesOnlyThePrimary();
            T44LogoutLeavesTheAccountConfigured();
            T45ProviderCatalogIsTheDialogsSingleSource();
            T46PlannedProvidersCannotNameABackend();
            T47ExistingProvidersStillCreateAccounts();
            T48CatalogNeverPromotesAProviderTheEngineCannotCreate();
            T49AddAccountSignsInThroughTheAccountPlane();
            T50AccountReorderContract();
            T51CanonicalVsRuntimeIdentityIsolation();
            TestStateMatrix();
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine("FAIL  harness threw -- " + ex);
            _failed++;
        }
        finally { KillStragglers(); }

        Console.WriteLine("");
        Console.WriteLine("passed " + _passed + ", failed " + _failed);
        return _failed > 0 ? 1 : 0;
    }
// The argv the engine actually received for `context <verb> <selector>`.
    static string[] ContextCall(string verb, string selector)
    {
        foreach (string[] a in Calls())
            if (a.Length > 2 && a[0] == "context" && a[1] == verb && a[2] == selector) return a;
        return null;
    }

    static string Join(string[] a)
    {
        return a == null ? "none" : string.Join(" ", a);
    }

    // -------------------------------------------------------------------- T1
    static void Test1OfflineStartsContext()
    {
        ResetLog();
        Scene("offline_status_calls=1");   // probe 1 offline, probe 2 online
        Saituls.SaitulsForm f = MakeForm(true);
        Check(Select(f, "AG2"), "T0  the fake registry exposes a secondary account AG2", "AG2 missing");

        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("Launched"), 40000);

        Check(ContextCall("start", "AG2") != null,
            "T1  an OFFLINE AG2 + Open console requests context start", Show());
        f.Dispose();
    }

    // ------------------------------------------------------------------ T288
    // A SAIPEN-managed folder: OpenConsole asks saipen-readiness first. No
    // in-console cure (a stale machine runtime) -> no launch and the class is
    // named; READY or a lifecycle refusal -> the launch happens and the note
    // carries the verdict; an engine without the verb or a non-SAIPEN folder
    // launches exactly as before.
    static void Test288SaipenReadinessGatesTheConsole()
    {
        string project = Path.Combine(Path.GetDirectoryName(WorkDir), "project-saipen");
        Directory.CreateDirectory(Path.Combine(project, ".saipen"));
        File.WriteAllText(Path.Combine(project, ".saipen", "STATE.md"), "---\nphase: BUILD\n---\n");

        Arm("offline_status_calls=0", "saipen_verdict=RUNTIME_STALE");
        Saituls.SaitulsForm f = MakeForm(true);
        SetOf(f, "_accountsWorkdir", project);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("SAIPEN"), 30000);
        Check(LaunchCall("AG2") == null, "T288a a stale machine runtime launches no console", Show());
        Check(HasCall("saipen-readiness", "AG2"), "T288a2 readiness is asked for the SELECTED account", Show());
        Check(Note(f).Contains("RUNTIME_STALE") && Note(f).Contains("saipen host machine install"),
            "T288a3 the operator is told the class and the one step that changes it", Note(f));
        Check((string)GetOf(Row(f, "AG2"), "SaipenVerdict") == "RUNTIME_STALE",
            "T288a4 the verdict is kept on the row for the detail pane",
            (string)GetOf(Row(f, "AG2"), "SaipenVerdict"));
        f.Dispose();

        Arm("offline_status_calls=0", "saipen_verdict=READY");
        f = MakeForm(true);
        SetOf(f, "_accountsWorkdir", project);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("Launched"), 30000);
        string[] launch = LaunchCall("AG2");
        Check(launch != null && launch[3] == project, "T288b a READY account opens on that project", Show());
        Check(Note(f).StartsWith("Launched AG2 PID") && Note(f).Contains("SAIPEN READY"),
            "T288b2 the launch note carries the verdict", Note(f));
        Check(Where("saipen-readiness", "AG2") < Where("launch", "AG2"),
            "T288b3 readiness is asked before the launch", Show());
        f.Dispose();

        Arm("offline_status_calls=0", "saipen_verdict=PROJECT_LIFECYCLE_DENIED");
        f = MakeForm(true);
        SetOf(f, "_accountsWorkdir", project);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("Launched"), 30000);
        Check(LaunchCall("AG2") != null, "T288c a lifecycle refusal still opens the console that repairs it", Show());
        Check(Note(f).Contains("PROJECT_LIFECYCLE_DENIED") && Note(f).Contains("saipen continue"),
            "T288c2 and names the canonical route", Note(f));
        f.Dispose();

        Arm("offline_status_calls=0");
        f = MakeForm(true);
        SetOf(f, "_accountsWorkdir", project);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("Launched"), 30000);
        Check(LaunchCall("AG2") != null && !Note(f).Contains("SAIPEN"),
            "T288d an engine without the verb launches exactly as before", Note(f) + " :: " + Show());
        f.Dispose();

        Arm("offline_status_calls=0", "saipen_verdict=RUNTIME_STALE");
        f = MakeForm(true);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("Launched"), 30000);
        Check(LaunchCall("AG2") != null && !HasCall("saipen-readiness", "AG2"),
            "T288e a folder without .saipen is never asked and still launches", Show());
        f.Dispose();
    }

    // -------------------------------------------------------------------- T2
    static void Test2LaunchesSelectedAccount()
    {
        ResetLog();
        Scene("offline_status_calls=1");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("Launched"), 40000);

        string[] launch = LaunchCall("AG2");
        Check(launch != null, "T2  once ONLINE the SELECTED account AG2 is launched", Show());
        Check(launch != null && launch.Length == 4 && launch[2] == "--workdir",
            "T2b the launch names exactly one workdir argument", Join(launch));

        int iStart = Where("context", "start");
        int iLaunch = Where("launch", "AG2");
        Check(iStart >= 0 && iLaunch >= 0 && iStart < iLaunch,
            "T2c the context starts before the launch, never after", iStart + "/" + iLaunch);
        Check(Note(f).StartsWith("Launched AG2 PID"), "T2d the launch result reaches the operator", Note(f));
        f.Dispose();
    }

    // -------------------------------------------------------------------- T3
    static void Test3NoFallbackToAnotherAccount()
    {
        ResetLog();
        Scene("offline_status_calls=1");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);
        Check(LaunchCall("AG1") == null, "T3  no fallback launch of another account", Show());

        // The refusal path too: a context that never comes online must not end
        // in "launch something that does work".
        ResetLog();
        Scene("offline_status_calls=99999");
        SetBudget(4000);
        Saituls.SaitulsForm g = MakeForm(true);
        Select(g, "AG2");
        M(g, "OpenConsole");
        Pump(() => !Busy(g) && Note(g).Length > 0, 40000);
        Check(!AnyLaunch(), "T3b a timed-out launch launches nothing at all", Show());
        SetBudget(180000);
        g.Dispose();
        f.Dispose();
    }

    // ------------------------------------------------------------------- T3c
    static void Test3cLifecycleAndHiddenRefuse()
    {
        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG3"); // FROZEN in the fake registry
        M(f, "OpenConsole");
        Pump(() => !Busy(f), 3000);
        Check(Note(f).StartsWith("LIFECYCLE_FORBIDDEN"), "T3c a FROZEN account is refused by lifecycle", Note(f));
        Check(!AnyLaunch() && !HasCall("context", "start"),
            "T3d a refused launch starts no context and no console", Show());
        f.Dispose();
    }

    // -------------------------------------------------------------------- T4
    static void Test4ExplicitWorkdir()
    {
        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).Length > 0, 20000);
        string[] launch = LaunchCall("AG2");
        Check(launch != null && launch.Length == 4 && launch[3] == WorkDir,
            "T4  the engine receives the chosen folder as one intact argument", Join(launch));
        f.Dispose();
    }

    // -------------------------------------------------------------------- T5
    static void Test5MissingWorkdirAsks()
    {
        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(false);
        Select(f, "AG2");
        Check((string)M(f, "CurrentWorkdir") == null,
            "T5a with nothing chosen, no workdir is resolved", "resolved something");

        // Open console on a worker: it will reach the folder picker, which this
        // harness cannot answer. What matters is the note it writes first, and
        // that no engine call happened on the way there.
        var worker = new Thread((ThreadStart)delegate { try { M(f, "OpenConsole"); } catch { } });
        worker.IsBackground = true;
        worker.Start();
        Pump(() => Note(f).Contains("project folder"), 8000);

        Check(Note(f).Contains("Choose a project folder"),
            "T5b a missing workdir asks for one instead of guessing", Note(f));
        Check(!AnyLaunch(), "T5c no launch was issued anywhere else", Show());
        Check((string)M(f, "CurrentWorkdir") == null,
            "T5d never System32, the profile, or the toolkit root", "");
        f.Dispose();
    }

    // -------------------------------------------------------------------- T6
    static void Test6InaccessibleWorkdirFailsClosed()
    {
        Check(!(bool)SM(typeof(Saituls.SaitulsForm), "WorkdirUsable", Path.Combine(WorkDir, "nope")),
            "T6a a missing folder is not a usable workdir", "");
        Check(!(bool)SM(typeof(Saituls.SaitulsForm), "WorkdirUsable", Engine),
            "T6b a file is not a usable workdir", "");
        Check((bool)SM(typeof(Saituls.SaitulsForm), "WorkdirUsable", WorkDir), "T6c a real folder is", "");

        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(true);
        M(f, "OpenConsoleFlow", "AG2", Path.Combine(WorkDir, "nope"), "windows_user", "antigravity", "antigravity_b");
        Pump(() => !Busy(f) && Note(f).Length > 0, 10000);
        Check(Note(f).StartsWith("WORKDIR_UNAVAILABLE"),
            "T6d an inaccessible workdir fails closed with a visible reason", Note(f));
        Check(!AnyLaunch(), "T6e nothing was launched against the bad workdir", Show());
        f.Dispose();
    }

    // -------------------------------------------------------------------- T7
    static void Test7StartTimeout()
    {
        ResetLog();
        Scene("offline_status_calls=999999");   // never comes online
        SetBudget(4000);
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => Note(f).StartsWith("Waiting for Windows login"), 20000);
        Check(Note(f).StartsWith("Waiting for Windows login"),
            "T7a the wait tells the operator what is happening", Note(f));
        Pump(() => !Busy(f), 40000);
        Check(Note(f).StartsWith("CONTEXT_START_TIMEOUT"),
            "T7b a context that never comes online ends in a visible timeout", Note(f));
        Check(!AnyLaunch(), "T7c the timeout does not launch anything", Show());
        SetBudget(180000);
        f.Dispose();
    }

    // -------------------------------------------------------------------- T8
    static void Test8AuthRequired()
    {
        ResetLog();
        Scene("offline_status_calls=0", "online_auth=AUTH_REQUIRED");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).Length > 0, 20000);
        Check(Note(f).StartsWith("AUTH_REQUIRED"),
            "T8  AUTH_REQUIRED is surfaced to the operator", Note(f));
        Check(!AnyLaunch(), "T8b nothing is launched while auth is required", Show());

        // And the affordance appears for a row that needs auth, and not for
        // one that does not.
        SetOf(Row(f, "AG2"), "AuthState", "AUTH_REQUIRED");
        // The affordance itself, not the button COUNT. Counting was a proxy that
        // stops working the moment the row offers one control in one auth state
        // and a different one in the other -- which is exactly what Switch Google
        // account is for a signed-in slot. Advanced is expanded because that is
        // where re-authenticate has always lived.
        SF(f, "_acctAdvancedExpanded", true);
        Check(ButtonWithLabel(f, Loc("Reauth")) != null,
            "T8c a re-authenticate action is offered for that row",
            "auth=" + GetOf(Row(f, "AG2"), "AuthState") + " have=[" + string.Join(",", Labels(f)) + "]");
        f.Dispose();
    }

    // -------------------------------------------------------------------- T9
    static void Test9PrimaryUserNoContext()
    {
        ResetLog();
        Scene("offline_status_calls=99999");
        SetBudget(4000);
        Saituls.SaitulsForm f = MakeForm(true);
        object row = Row(f, "AG1");
        SetOf(row, "Context", Environment.UserName);
        Select(f, "AG1");
        Check(!(bool)M(f, "ContextSecondary", row),
            "T9a the operator's own Windows account is not a secondary context", "");

        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).Length > 0, 30000);
        Check(!HasCall("context", "start"),
            "T9b no secondary context is started for the current user", Show());
        SetBudget(180000);
        f.Dispose();
    }

    // ------------------------------------------------------------------- T10
    static void Test10StopContext()
    {
        ResetLog();
        // The engine reports the context gone once the stop lands, which is what
        // the operator should then be shown.
        Scene("offline_status_calls=999999");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "AccountsContext", "disconnect");
        Pump(() => !Busy(f) && Note(f).Length > 0, 20000);
        Check(ContextCall("disconnect", "AG2") != null,
            "T10 Disconnect speaks intent, not a bare process stop", Show());
        Check(Note(f).Contains("OFFLINE"),
            "T10b the row is re-probed and reads OFFLINE, rather than echoing the CLI's string", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T11
    static void Test11SetHiddenTrueFalse()
    {
        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "AccountsRun", "set-hidden", "true");
        Pump(() => !Busy(f) && Note(f).Length > 0, 20000);
        string[] call = null;
        foreach (string[] a in Calls()) if (a[0] == "set-hidden") call = a;
        Check(call != null && call.Length == 3 && call[2] == "true",
            "T11 set-hidden sends true, which is what bool.TryParse accepts", Join(call));
        Check(Note(f).Contains("account_hidden"),
            "T11b the engine accepted it and said so", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T12
    static void Test12HiddenStaysListed()
    {
        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(true);
        int before = RowButtons(f);
        SetOf(Row(f, "AG2"), "Hidden", true);
        int after = RowButtons(f);
        Check(before == 3 && after == before,
            "T12  a hidden account is still listed and still clickable",
            "rows before " + before + ", after " + after);
        f.Dispose();
    }

    // ------------------------------------------------------------------- T13
    static void Test13HiddenSuppressesLaunch()
    {
        ResetLog();
        Scene("offline_status_calls=0");
        string block = (string)SM(typeof(Saituls.SaitulsForm), "BlockedBy",
            MakeRow("AG9", "ENABLED", true, "windows_user", "someone_else"));
        Check(block.Contains("hidden"), "T13 a hidden account is refused a launch", block);

        Saituls.SaitulsForm f = MakeForm(true);
        SetOf(Row(f, "AG2"), "Hidden", true);
        Select(f, "AG2");
        M(f, "OpenConsole");
        Pump(() => !Busy(f), 4000);
        Check(!AnyLaunch(), "T13b no launch for a hidden account", Show());
        Check(Note(f).Contains("hidden"), "T13c and it says why", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T14
    static void Test14HungChildBounded()
    {
        ResetLog();
        Scene("hang=list");
        var sw = Stopwatch.StartNew();
        object res = SM(typeof(Saituls.SaitulsForm), "RunBounded", Engine, "list --all", 3000);
        sw.Stop();
        Check(sw.ElapsedMilliseconds < 20000,
            "T14a an engine that floods stderr and never exits still returns",
            sw.ElapsedMilliseconds + "ms");
        Check((bool)GetOf(res, "TimedOut"), "T14b the run is reported as timed out, not as empty success", "");
        Check(!(bool)Prop(res, "Ok"), "T14c a timed-out run is never Ok", "");

        ResetLog();
        Scene("fail=list:7");
        object res2 = SM(typeof(Saituls.SaitulsForm), "RunBounded", Engine, "list --all", 5000);
        Check((int)GetOf(res2, "ExitCode") == 7, "T14d a nonzero exit is reported, not swallowed", "");
        Check(((string)Prop(res2, "Problem")).Contains("fake engine refused"),
            "T14e stderr reaches the operator", "");

        ResetLog();
        Scene("stderr=list");
        object res3 = SM(typeof(Saituls.SaitulsForm), "RunBounded", Engine, "list --all", 5000);
        Check(((string)GetOf(res3, "Stderr")).Contains("fake engine warning"),
            "T14f stderr is captured even on success", "");
        KillStragglers();
    }

    // ------------------------------------------------------------------- T15
    static void Test15UiThreadNotBlocked()
    {
        ResetLog();
        Scene("offline_status_calls=1");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        var sw = Stopwatch.StartNew();
        M(f, "OpenConsole");
        sw.Stop();
        Check(sw.ElapsedMilliseconds < 1500,
            "T15 Open console returns immediately instead of waiting on the engine",
            sw.ElapsedMilliseconds + "ms");
        Check(Busy(f), "T15b the work is running off the UI thread", "busy=" + Busy(f));
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);
        Check(Note(f).StartsWith("Launched"), "T15c and it does finish the launch", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T16
    // A Connect that fails still has to tell the operator what failed. The engine
    // exits nonzero AND prints a valid envelope; a GUI that only reads stdout on
    // success leaves the row stuck on OFFLINE with no explanation at all, which is
    // exactly the "pressing Connect does nothing" report.
    static void Test16FailedConnectRendersTheRealError()
    {
        ResetLog();
        Scene("offline_status_calls=999999\nfail_envelope=context");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "AccountsContext", "connect");
        Pump(() => !Busy(f) && Note(f).Contains("failed"), 40000);

        string note = Note(f);
        Check(note.Contains("start_context_failed") || note.Contains("context_failed"),
            "T16a a failed Connect renders the parsed status", note);
        Check(note.Contains("broker_not_listening_within_5000ms"),
            "T16b a failed Connect renders the actionable error, not a generic FAIL", note);
        Check(!note.Contains("{") && !note.Contains("\"schema\""),
            "T16c the operator strip never shows a raw JSON envelope", note);
        Check(note.Length < 120,
            "T16d the failure fits the one-line operator strip", "len=" + note.Length + " " + note);
        f.Dispose();
    }

    // ------------------------------------------------------------------- T17
    // The same Connect, succeeding: the row must read ONLINE from a fresh probe,
    // not from the CLI's optimistic exit code.
    static void Test17ConnectSuccessShowsOnline()
    {
        ResetLog();
        Scene("offline_status_calls=0");   // the re-probe sees the context come up
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        M(f, "AccountsContext", "connect");
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);
        Check(Note(f).Contains("ONLINE"),
            "T17a a successful Connect shows ONLINE without a restart", Note(f));
        Check(ContextCall("connect", "AG2") != null,
            "T17b the connect went to the selected account only", Show());
        f.Dispose();

        // The opposite case, and the one that used to lie: the CLI accepted the
        // connect but the context has not come up yet. Reporting ONLINE here
        // would be a state the operator can see is false two seconds later, so
        // the CLI's own answer is what gets shown.
        ResetLog();
        Scene("offline_status_calls=999999");
        Saituls.SaitulsForm g = MakeForm(true);
        Select(g, "AG2");
        M(g, "AccountsContext", "connect");
        Pump(() => !Busy(g) && Note(g).Length > 0, 40000);
        Check(!Note(g).Contains("ONLINE") && Note(g).Contains("launch_requested"),
            "T17c a connect whose context is not up yet never claims ONLINE", Note(g));
        g.Dispose();
    }

    // ------------------------------------------------------------------- T18
    // The production defect, stated as an executable claim: SIGN IN used to be
    // OpenConsole(true), which is why clicking it demanded a project folder and
    // then failed with WORKDIR_UNAVAILABLE for a managed account that cannot
    // read the operator's project tree. Authentication is its own operation now.
    static void Test18SignInIsNotAConsoleLaunch()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=0", "online_auth=AUTH_REQUIRED");
        M(f, "SignInSelectedAccount");
        Pump(() => !Busy(f) && Note(f).Contains("Antigravity is open"), 40000);

        Check(HasCall("auth", "AG2"), "T18a SIGN IN issues the dedicated auth operation", Show());
        Check(LaunchCall("AG2") == null, "T18b SIGN IN never issues a console launch", Show());
        Check(!Note(f).Contains("WORKDIR_UNAVAILABLE"),
            "T18c SIGN IN never reports WORKDIR_UNAVAILABLE", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T19
    // The whole point of PART 1: no workdir anywhere on the sign-in path. Not
    // on the command line, not in the note, and the form must not even need a
    // usable workdir -- so this one runs with the saved workdir cleared.
    static void Test19SignInNeverTouchesAWorkdir()
    {
        Saituls.SaitulsForm f = MakeForm(false);   // no remembered project folder
        Select(f, "AG2");
        Arm("offline_status_calls=0", "online_auth=AUTH_REQUIRED");
        M(f, "SignInSelectedAccount");
        Pump(() => !Busy(f) && Note(f).Contains("Antigravity is open"), 40000);

        bool anyFlag = false;
        foreach (string[] a in Calls())
            foreach (string arg in a) if (arg == "--workdir" || arg == WorkDir) anyFlag = true;
        Check(!anyFlag, "T19a SIGN IN sends no --workdir and no project path", Show());
        Check(!Note(f).Contains(WorkDir), "T19b SIGN IN's note names no project folder", Note(f));

        string[] auth = null;
        foreach (string[] a in Calls())
            if (a.Length > 1 && a[0] == "auth") auth = a;
        Check(auth != null && auth.Length == 2,
            "T19c the auth operation carries the selector and nothing else", Join(auth));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T20
    // An offline managed account cannot open its own sign-in window, so the
    // EXACT selected context is started and awaited first.
    static void Test20SignInStartsAnOfflineContextFirst()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=1", "online_auth=AUTH_REQUIRED");
        M(f, "SignInSelectedAccount");
        Pump(() => !Busy(f) && Note(f).Contains("Antigravity is open"), 40000);

        Check(ContextCall("start", "AG2") != null,
            "T20a an OFFLINE AG2 starts its own context before signing in", Show());
        int iStart = Where("context", "start");
        int iAuth = Where("auth", "AG2");
        Check(iStart >= 0 && iAuth >= 0 && iStart < iAuth,
            "T20b the context is online before the auth operation is issued", iStart + "/" + iAuth);
        f.Dispose();
    }

    // ------------------------------------------------------------------- T21
    // Already online: go straight to the auth operation, no pointless restart.
    static void Test21SignInOnAnOnlineAccountSkipsTheContextStart()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=0", "online_auth=AUTH_REQUIRED");
        M(f, "SignInSelectedAccount");
        Pump(() => !Busy(f) && Note(f).Contains("Antigravity is open"), 40000);

        Check(ContextCall("start", "AG2") == null,
            "T21a an ONLINE AG2 + SIGN IN goes straight to auth", Show());
        Check(HasCall("auth", "AG2"), "T21b the auth operation is issued", Show());
        f.Dispose();
    }

    // ------------------------------------------------------------------- T22
    // Identity fails closed. Signing in as, or for, a different account is the
    // one failure the managed-account model exists to make impossible -- a
    // Google credential landing in the wrong Windows profile is unrecoverable
    // by any amount of retrying.
    static void Test22SignInNeverFallsBackToAnotherAccount()
    {
        SetBudget(4000);
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=99999");
        M(f, "SignInSelectedAccount");
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);

        Check(!HasCall("auth", "AG1"), "T22a a timed-out sign-in never authenticates AG1", Show());
        Check(LaunchCall("AG1") == null && LaunchCall("AG2") == null,
            "T22b a timed-out sign-in launches no console for anyone", Show());
        Check(ContextCall("start", "AG1") == null,
            "T22c a timed-out sign-in never starts another account's context", Show());
        SetBudget(180000);
        f.Dispose();
    }

    // ------------------------------------------------------------------- T23
    // The second half of the production defect. A valid project the managed
    // account cannot read comes back as ACCESS_DENIED, and the GUI's job is to
    // grant the minimum rights once and retry THE SAME PATH -- never to send
    // the operator off to pick a different folder.
    static void Test23AccessDeniedProvisionsThenRetriesTheSamePath()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=0", "launch_access_denied=1");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).StartsWith("Launched"), 40000);

        string[] grant = null;
        foreach (string[] a in Calls())
            if (a.Length > 1 && a[0] == "grant-workdir") grant = a;
        Check(grant != null, "T23a ACCESS_DENIED provisions the managed workdir", Show());
        Check(grant != null && grant.Length == 4 && grant[3] == WorkDir,
            "T23b the grant names the EXACT selected project path, nothing else", Join(grant));

        var launches = new List<string[]>();
        foreach (string[] a in Calls()) if (a.Length > 1 && a[0] == "launch") launches.Add(a);
        Check(launches.Count == 2, "T23c the exact same workdir is retried once", launches.Count + " launches");
        bool samePath = launches.Count == 2
            && launches[0].Length == 4 && launches[1].Length == 4
            && launches[0][3] == WorkDir && launches[1][3] == WorkDir;
        Check(samePath, "T23d both launches name the identical path, no substitution",
            Join(launches.Count > 0 ? launches[0] : null) + " / " + Join(launches.Count > 1 ? launches[1] : null));
        Check(Note(f).StartsWith("Launched AG2 PID"), "T23e the launch completes after the grant", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T24
    // A folder that is simply not there is the operator's to fix, and touching
    // an ACL because of it would be both useless and an untracked security
    // change. NOT_FOUND must never reach the provisioning path.
    static void Test24NotFoundNeverProvisions()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=0", "launch_not_found=1");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);

        bool granted = false;
        foreach (string[] a in Calls()) if (a.Length > 1 && a[0] == "grant-workdir") granted = true;
        Check(!granted, "T24a NOT_FOUND never mutates an ACL", Show());
        Check(Note(f).Contains("WORKDIR_UNAVAILABLE"),
            "T24b NOT_FOUND is reported as an unavailable workdir", Note(f));
        Check(!Note(f).Contains("ACCESS_DENIED"),
            "T24c a missing folder is never mislabelled a permissions problem", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T25
    // One attempt, one retry. If the grant does not take, the operator sees the
    // real structured failure -- no loop, no fallback directory, no second try
    // that quietly succeeds somewhere else.
    static void Test25ProvisioningHappensAtMostOnce()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=0", "launch_access_denied=99", "grant_status=ACL_MUTATION_FAILED");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);

        int grants = 0, launches = 0;
        foreach (string[] a in Calls())
        {
            if (a.Length > 1 && a[0] == "grant-workdir") grants++;
            if (a.Length > 1 && a[0] == "launch") launches++;
        }
        Check(grants == 1, "T25a a refused grant is attempted exactly once", grants + " grants");
        Check(launches == 1, "T25b a refused grant is not retried blindly", launches + " launches");
        Check(Note(f).StartsWith("WORKDIR ACL update failed"),
            "T25c the operator sees the real structured failure", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T26
    // One generic sentence used to stand for every provisioning failure, so the
    // operator could not tell a broken ACL from a link from a deny rule. Each
    // closed helper status now names itself, matched by value -- never by
    // matching text inside an English exception.
    static void Test26EveryGrantFailureNamesItself()
    {
        string[,] cases = {
            {"ACL_READ_FAILED",         "WORKDIR ACL read failed"},
            {"ACL_MUTATION_FAILED",     "WORKDIR ACL update failed"},
            {"EFFECTIVE_ACCESS_DENIED", "Existing ACL policy denies"},
            {"WORKDIR_REPARSE_REFUSED", "was not modified"},
            {"WORKDIR_TARGET_REFUSED",  "not an allowed grant target"},
            {"WORKDIR_NOT_FOUND",       "Project folder not found"},
            {"WORKDIR_NOT_READABLE",    "could not be inspected"},
            {"HELPER_UNAVAILABLE",      "helper service did not answer"},
        };
        for (int i = 0; i < cases.GetLength(0); i++)
        {
            string status = cases[i, 0];
            string expected = cases[i, 1];
            Saituls.SaitulsForm f = MakeForm(true);
            Select(f, "AG2");
            Arm("offline_status_calls=0", "launch_access_denied=1", "grant_status=" + status);
            M(f, "OpenConsole");
            Pump(() => !Busy(f) && Note(f).Length > 0, 40000);
            Check(Note(f).Contains(expected),
                "T26 " + status + " is reported as itself, not as a generic denial", Note(f));
            f.Dispose();
        }
    }

    // ------------------------------------------------------------------- T27
    // The grant was accepted and the retry STILL came back ACCESS_DENIED. That
    // is a different problem from "no rights yet" -- the ACE is there and is not
    // effective -- and it must not be dressed up as a missing permission or sent
    // through a second blind grant.
    static void Test27AcceptedGrantThatStillDoesNotWorkSaysSo()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=0", "launch_access_denied=2", "grant_status=granted");
        M(f, "OpenConsole");
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);

        int grants = 0, launches = 0;
        foreach (string[] a in Calls())
        {
            if (a.Length > 1 && a[0] == "grant-workdir") grants++;
            if (a.Length > 1 && a[0] == "launch") launches++;
        }
        Check(grants == 1, "T27a an ineffective grant is not repeated", grants + " grants");
        Check(launches == 2, "T27b the one retry still happens", launches + " launches");
        Check(Note(f).Contains("still cannot enter"),
            "T27c the operator is told the ACE was written and did not help", Note(f));
        f.Dispose();
    }

    // ------------------------------------------------------------------- T28
    // The operator's own words for T-232: "the connect button does not answer
    // for 3+ minutes". A handler that waits on the engine freezes the whole
    // form, and a frozen form is indistinguishable from a dead button: no note,
    // no progress, no error, and the window stops repainting. T15 proved that
    // for OpenConsole only -- the two buttons the report is actually about had
    // no bound at all, so a regression here would be silent.
    static void Test28ConnectAndSignInNeverBlockTheUiThread()
    {
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        Arm("offline_status_calls=0");
        var sw = Stopwatch.StartNew();
        M(f, "AccountsContext", "connect");
        sw.Stop();
        Check(sw.ElapsedMilliseconds < 1500,
            "T28a Connect returns immediately instead of waiting on the engine",
            sw.ElapsedMilliseconds + "ms");
        Check(Busy(f), "T28b Connect runs its work off the UI thread", "busy=" + Busy(f));
        Pump(() => !Busy(f) && Note(f).Length > 0, 40000);
        Check(Note(f).Contains("ONLINE"), "T28c and the connect does finish", Note(f));
        f.Dispose();

        Saituls.SaitulsForm g = MakeForm(true);
        Select(g, "AG2");
        Arm("offline_status_calls=0", "online_auth=AUTH_REQUIRED");
        var sw2 = Stopwatch.StartNew();
        M(g, "SignInSelectedAccount");
        sw2.Stop();
        Check(sw2.ElapsedMilliseconds < 1500,
            "T28d SIGN IN returns immediately instead of waiting on the engine",
            sw2.ElapsedMilliseconds + "ms");
        Check(Busy(g), "T28e SIGN IN runs its work off the UI thread", "busy=" + Busy(g));
        Pump(() => !Busy(g) && Note(g).Length > 0, 40000);
        Check(HasCall("auth", "AG2"), "T28f and the sign-in does finish", Show());
        g.Dispose();
    }

    // ------------------------------------------------------------------ T29
    // Drive one status reply through the REAL form and read what lands.
    //
    // The source-regex checks in test_accounts_tab.ps1 prove the tab contains
    // the right string; they cannot prove a fingerprint ever reaches the row.
    // That gap mattered once already: this tab rendered two Antigravity slots
    // both AUTHENTICATED with nothing on screen to say they were one human,
    // while the engine was emitting exactly the field that settles it. So the
    // claim under test is the data flow, and it is tested as the data flow.
    const string SharedFp = "ce93fa79b66ad0c7";

    // One status poll for this slot.
    //
    // NOT ResetLog(): that helper deletes the scenario file on purpose, and the
    // scenario is exactly what says which fingerprint the engine answers with.
    // Arming is the form-creation job; from here the log only has to be counted
    // forward from where it already is.
    static int CountCalls(string verb, string selector)
    {
        int n = 0;
        foreach (string[] a in Calls())
            if (a.Length > 1 && a[0] == verb && a[1] == selector) n++;
        return n;
    }

    static string PollIdentity(Saituls.SaitulsForm f, string compact, bool expectValue)
    {
        int before = CountCalls("status", compact);
        M(f, "PollStatus", Row(f, compact));
        if (expectValue)
            // The field itself is the signal, so a GUI that never fills it fails
            // on the clock instead of on an assumption about the plumbing.
            Pump(() => ((string)GetOf(Row(f, compact), "IdentityFp")).Length > 0, 20000);
        else
        {
            // Nothing is expected to change, so there is no field to wait on.
            // Wait for the engine to have answered instead, then settle for the
            // marshalled apply that will land -- and land as "" regardless,
            // because a reply with no credential object has nothing to set.
            Pump(() => CountCalls("status", compact) > before, 20000);
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < 300) { Application.DoEvents(); Thread.Sleep(10); }
        }
        return (string)GetOf(Row(f, compact), "IdentityFp");
    }

    static void Test29IdentityFingerprintReachesTheRow()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);

        Check(PollIdentity(f, "AG1", true) == SharedFp,
            "T29  a proven fingerprint reaches the row from a real status reply",
            "AG1 IdentityFp=" + (string)GetOf(Row(f, "AG1"), "IdentityFp"));
        PollLabel(f, "AG1");
        Check(IdeFpOf(Row(f, "AG1")) == SharedFp,
            "T29a the same reply fills the IDE plane as well",
            "AG1 RuntimeIdentityFp=" + IdeFpOf(Row(f, "AG1")));
        Check(PollIdentity(f, "AG2", true) == SharedFp,
            "T29b two slots on ONE Google account report the SAME fingerprint",
            "AG2 IdentityFp=" + (string)GetOf(Row(f, "AG2"), "IdentityFp"));
        Check(PollIdentity(f, "AG3", true) == SharedFp,
            "T29c a third slot proves it too -- the sharing is not a two-row accident",
            "AG3 IdentityFp=" + (string)GetOf(Row(f, "AG3"), "IdentityFp"));
        f.Dispose();
    }

    // The other half of the claim: a per-slot override has to be able to make
    // them DIFFER, or "they match" is just a constant and proves nothing about
    // attribution. One slot proved separately is the case an operator needs to
    // see: it is how a second Google login would be recognised as real capacity.
    static void Test29dDistinctIdentitiesStayDistinct()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "configured_fp_AG3=a1b2c3d4e5f60718", "configured_label_AG3=" + Carol,
              "identity_fp=" + SharedFp, "identity_label=" + Alice,
              "identity_fp_AG3=a1b2c3d4e5f60718", "identity_label_AG3=" + Carol);
        Saituls.SaitulsForm f = MakeForm(true);
        Check(PollIdentity(f, "AG3", true) == "a1b2c3d4e5f60718",
            "T29d a slot with its own Google login keeps ITS OWN fingerprint",
            "AG3 IdentityFp=" + (string)GetOf(Row(f, "AG3"), "IdentityFp"));
        Check(PollIdentity(f, "AG1", true) == SharedFp,
            "T29e the override does not leak onto the shared slots",
            "AG1 IdentityFp=" + (string)GetOf(Row(f, "AG1"), "IdentityFp"));
        f.Dispose();
    }

    // An identity that was never proven must stay distinguishable from a
    // shared one. A GUI that defaulted the field to "unknown" as a literal
    // string would make unproven and shared look alike at exactly the moment
    // they matter, and the fingerprint is worthless if it cannot be trusted
    // when absent.
    static void Test29fUnprovenIdentityIsNotASharedOne()
    {
        ResetLog();
        Scene("offline_status_calls=0");   // no identity_* key: no credential object
        Saituls.SaitulsForm f = MakeForm(true);
        string fp = PollIdentity(f, "AG2", false);
        Check(fp.Length == 0, "T29f a reply with no credential object leaves the identity unproven",
            "AG2 IdentityFp=" + fp);
        Check(IdeFpOf(Row(f, "AG2")).Length == 0,
            "T29f2 and the IDE plane stays unproven with it", IdeFpOf(Row(f, "AG2")));
        Check(fp != SharedFp, "T29g unproven never renders as somebody else's account", fp);
        f.Dispose();
    }

    // And the fingerprint has to leave the process. Copy Diagnostics is the
    // text an operator pastes into a bug report, so an identity the tab holds
    // but never surfaces is an identity the support thread never sees.
    static void Test29hDiagnosticsCarryTheIdentity()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        PollIdentity(f, "AG2", true);
        // PollIdentity can return on the list reply alone; the status poll's
        // marshalled apply carries the IDE observation, so give it a turn.
        PollLabel(f, "AG2");
        Pump(() => IdeFpOf(Row(f, "AG2")).Length > 0, 20000);

        // Paint for real: DrawAccountsTab is what registers the button, and the
        // button is the only route to the text. Advanced has to be expanded --
        // the diagnostics block lives under it -- and the copy button is the
        // 116x18 box, which nothing else in the file is.
        SF(f, "_acctAdvancedExpanded", true);
        var bmp = new Bitmap(900, 700);
        using (var g = Graphics.FromImage(bmp))
            M(f, "DrawAccountsTab", g, 0, 0, 900, 700);

        var buttons = (IList)F(f, "Buttons");
        object copy = null;
        foreach (object b in buttons)
        {
            var r = (Rectangle)b.GetType().GetField("R", Any).GetValue(b);
            if (r.Width == 116 && r.Height == 18) { copy = b; break; }
        }
        Check(copy != null, "T29h the accounts tab registers a Copy Diagnostics button", "none of " + buttons.Count);

        string diag = "";
        if (copy != null)
        {
            Clipboard.Clear();
            ((Action)copy.GetType().GetField("A", Any).GetValue(copy))();
            diag = Clipboard.ContainsText() ? Clipboard.GetText() : "";
        }
        Check(diag.Contains("ConsoleIdentity: " + SharedFp),
            "T29i the diagnostics an operator copies carry the canonical console fingerprint",
            diag.Replace("\n", " | "));
        Check(diag.Contains("IdeIdentity: " + SharedFp),
            "T29i2 and the IDE identity as its own independent field", diag.Replace("\n", " | "));
        Check(diag.Contains("IdentityMismatch") == false,
            "T29i3 and no mismatch verdict anywhere in it", diag.Replace("\n", " | "));
        Check(diag.Contains("ConsoleIdentity: (unproven)") == false,
            "T29j a proven identity is never labelled unproven", diag.Replace("\n", " | "));
        bmp.Dispose();
        f.Dispose();
    }

    // ------------------------------------------------------- T-243 identity UX
    //
    // The label is the human half of the same verified token. Everything below
    // is about the boundary between the two halves: the fingerprint is the
    // identity and everything groups on it, the label is a name for people and
    // nothing groups on it.
    const string OtherFp = "62b01cb67a9a27af";
    const string Alice = "alice@example.com";
    const string Bob = "bob@example.net";
    const string Carol = "carol@example.org";
    const string CarolFp = "87ce6817ca3b4b55";
    const string BobFp = OtherFp;
    const string AliceFp = SharedFp;
    const string id1 = "antigravity:windows-user:0000000000000001";
    const string id2 = "antigravity:windows-user:0000000000000002";
    const string id3 = "antigravity:windows-user:0000000000000003";

    // ---- the two identity planes, read straight off the row ---------------
    //
    // PLANE A, the console/quota account, is the canonical provider identity
    // the slot owns. It comes from `configured_identity` in the registry and
    // can only be established by an explicit bind. Quota and duplicate
    // reasoning is done here and nowhere else.
    //
    // PLANE B, the IDE account, is what the running Antigravity context is
    // signed into right now, read from `credential.provider_identity`. It is an
    // observation: it may equal Plane A, differ from it, equal another slot's
    // console account, or be unproven. None of those four is a fault.
    static string ConsoleFpOf(object row) { return (string)GetOf(row, "ConfiguredIdentityFp"); }
    static string ConsoleLabelOf(object row) { return (string)GetOf(row, "ConfiguredIdentityLabel"); }
    static string IdeFpOf(object row) { return (string)GetOf(row, "RuntimeIdentityFp"); }
    static string IdeLabelOf(object row) { return (string)GetOf(row, "RuntimeIdentityLabel"); }

    // The engine verbs that change canonical state. A passive status poll, a
    // repaint and a Refresh may not issue any of them.
    static readonly string[] MutatingVerbs = {
        "bind-identity", "rename", "move", "set-state", "set-hidden",
        "add", "logout", "auth" };

    static bool AnyMutation()
    {
        foreach (string[] a in Calls())
            if (a.Length > 0 && Array.IndexOf(MutatingVerbs, a[0]) >= 0) return true;
        return false;
    }

    // Read the legacy mismatch contract WITHOUT naming its fields. A build that
    // still carries `IdentityMismatch` answers true here, and the assertions
    // below then fail against it -- which is exactly what makes them
    // discriminating rather than retro-fitted to the fix.
    static object LegacyField(object row, string name)
    {
        FieldInfo fi = row.GetType().GetField(name, Any);
        return fi == null ? null : fi.GetValue(row);
    }
    static bool LegacyMismatch(object row)
    {
        object v = LegacyField(row, "IdentityMismatch");
        return v is bool && (bool)v;
    }
    static string LegacyMismatchText(object row)
    {
        if (!LegacyMismatch(row)) return "";
        object c = LegacyField(row, "RuntimeCollisionWith");
        string coll = c as string;
        return (coll != null && coll.Length > 0)
            ? SwitchText("IdeSessionMatches", coll)
            : Loc("IdeSessionMismatch");
    }

    static string LabelOf(Saituls.SaitulsForm f, string compact)
    {
        return (string)GetOf(Row(f, compact), "IdentityLabel");
    }
    static string SharedOf(Saituls.SaitulsForm f, string compact)
    {
        return (string)GetOf(Row(f, compact), "IdentitySharedWith");
    }
    // The whole identity table, one row per line. Grouping is a property of the
    // set, not of one row, so a failure anywhere in it is only diagnosable with
    // every row next to it.
    static string Table(Saituls.SaitulsForm f)
    {
        var sb = new System.Text.StringBuilder();
        foreach (object row in (IList)F(f, "_accounts"))
            sb.Append(CompactOf(row)).Append(" fp=").Append(GetOf(row, "IdentityFp"))
              .Append(" label=").Append(GetOf(row, "IdentityLabel"))
              .Append(" shared=[").Append(GetOf(row, "IdentitySharedWith")).Append("]; ");
        return sb.ToString();
    }

    // Wait for the observation to be applied, including an unproven reply.
    // The fake writes its call log BEFORE producing a reply; a 400ms pause
    // after that log can read the old row and miss the Switch control.
    static void PollLabel(Saituls.SaitulsForm f, string compact)
    {
        object row = Row(f, compact);
        SetOf(row, "IdentitySeen", false);
        M(f, "PollStatus", row);
        Pump(() => (bool)GetOf(Row(f, compact), "IdentitySeen") || Note(f).StartsWith("status failed:"), 20000);
    }

    // Drive the background status tick itself, ticks and all: the throttle clock is
    // rewound each time so one call is one probe, and the wait is on the number
    // of rows that have an identity rather than on a fixed sleep. Polling rows by
    // hand here would test a path the real UI only reaches through the sweep.
    static void Sweep(Saituls.SaitulsForm f, int ticks)
    {
        for (int i = 0; i < ticks; i++)
        {
            SetOf(f, "_accountsPollAt", DateTime.MinValue);
            SetOf(f, "_accountsPolling", false);
            int before = CountAll("status");
            M(f, "PollStatusTick");
            Pump(() => CountAll("status") > before, 20000);
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < 400) { Application.DoEvents(); Thread.Sleep(10); }
        }
    }
    static int CountAll(string verb)
    {
        int n = 0;
        foreach (string[] a in Calls()) if (a.Length > 0 && a[0] == verb) n++;
        return n;
    }

    // (1, 9) The label has to travel the same path the fingerprint does. A GUI
    // that renders the fingerprint but drops the address answers "which account"
    // with sixteen hex characters, which is the archaeology this exists to end.
    static void T30IdentityLabelReachesTheRow()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);

        Check(PollIdentity(f, "AG2", true) == SharedFp,
            "T30  the fingerprint still reaches the row beside its label",
            "AG2 fp=" + (string)GetOf(Row(f, "AG2"), "IdentityFp"));
        PollLabel(f, "AG2");
        Check(LabelOf(f, "AG2") == Alice,
            "T30b the human-readable label reaches the row from a real status reply",
            "AG2 label=" + LabelOf(f, "AG2"));
        Check(ConsoleLabelOf(Row(f, "AG2")) == Alice && ConsoleFpOf(Row(f, "AG2")) == SharedFp,
            "T30c and it lands on the canonical console plane",
            "AG2 console=" + ConsoleFpOf(Row(f, "AG2")) + "/" + ConsoleLabelOf(Row(f, "AG2")));
        f.Dispose();
    }

    // (19) The single most important claim in the set: the grouping keys on the
    // fingerprint and NOT on the label. Two rows with one fingerprint and
    // DIFFERENT addresses must still be reported as one human -- if they were not,
    // a Google account used under two addresses would read as two quota pools,
    // which is the exact false capacity this feature must not invent.
    static void T31GroupingKeysOnFingerprintNotLabel()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "configured_label_AG3=" + Bob,
              "identity_fp=" + SharedFp,
              "identity_label=" + Alice, "identity_label_AG3=" + Bob);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG1");
        PollLabel(f, "AG3");

        Check(LabelOf(f, "AG3") == Bob,
            "T31  the fixture really does hold two different addresses on one fingerprint",
            "AG3 label=" + LabelOf(f, "AG3"));
        Check(SharedOf(f, "AG3").Length > 0,
            "T31b different labels do NOT make the same fingerprint look like two humans",
            "AG3 shared='" + SharedOf(f, "AG3") + "' table=" + Table(f));
        Check(SharedOf(f, "AG1").Length > 0,
            "T31c and the warning lands on the OTHER slot too, not just the odd one out",
            "AG1 shared='" + SharedOf(f, "AG1") + "'");
        f.Dispose();
    }

    // (10) The warning has to be visible to the operator, not merely computed.
    // Painting the real panel and reading the pixels is the only way to know a
    // warning colour was actually used: a row that computed the answer and then
    // drew it in the ordinary text colour would pass every data-flow test here.
    static void T32DuplicateFingerprintsWarnOnBothRows()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        // The sweep, not a hand-probe of AG1: the warning is required on BOTH
        // rows, so the panel has to look at a slot the operator never opens.
        Sweep(f, 4);
        Check(LabelOf(f, "AG1") == Alice,
            "T32z the panel probes a slot nobody opened", "AG1 label=" + LabelOf(f, "AG1"));

        // Paint AG1 and AG2 in turn and confirm the rendered panel names the
        // other slot in warning text on each.
        string p1 = PanelText(f, "AG1");
        string p2 = PanelText(f, "AG2");
        Check(p1.Contains("AG2"),
            "T32  AG1's panel names the slot sharing its Google account", p1.Replace("\n", " | "));
        Check(p2.Contains("AG1"),
            "T32b AG2's panel names the slot sharing its Google account", p2.Replace("\n", " | "));
        Check(p2.Contains(Alice),
            "T32c the label is on the panel, not one disclosure level down", p2.Replace("\n", " | "));
        f.Dispose();
    }

    // (11) The other half: no warning when the identities really are different.
    // A warning that fires unconditionally is as useless as none at all.
    static void T33DistinctFingerprintsDoNotWarn()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
              "configured_fp_AG2=" + OtherFp, "configured_label_AG2=" + Bob,
              "identity_fp_AG1=" + SharedFp, "identity_label_AG1=" + Alice,
              "identity_fp_AG2=" + OtherFp, "identity_label_AG2=" + Bob);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG1");
        PollLabel(f, "AG2");

        Check(SharedOf(f, "AG1").Length == 0 && SharedOf(f, "AG2").Length == 0,
            "T33  two different fingerprints raise no duplicate warning",
            "AG1='" + SharedOf(f, "AG1") + "' AG2='" + SharedOf(f, "AG2") + "'");
        Check(PanelText(f, "AG2").Contains("AG1") == false,
            "T33b and no slot is named on the other's panel", PanelText(f, "AG2").Replace("\n", " | "));
        f.Dispose();
    }

    // (12) The action is offered only where it means something: a provider with
    // a credential, signed in, still enabled. Offering it everywhere would be a
    // second button competing with SIGN IN for the same job.
    static void T34SwitchIsOfferedOnlyWhereItMeansSomething()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Check(HasButtonLabel(PaintAccounts(f), Loc("SwitchAccount")),
            "T34  an authenticated Antigravity slot offers Switch Google account", "no such button");

        // Not authenticated: SIGN IN is the right verb there, so the switch is
        // removed rather than left as a button that would log out of nothing.
        ResetLog();
        Scene("offline_status_calls=0", "initial_auth=AUTH_REQUIRED", "online_auth=AUTH_REQUIRED");
        Saituls.SaitulsForm g = MakeForm(true);
        Select(g, "AG2");
        Check(!HasButtonLabel(PaintAccounts(g), Loc("SwitchAccount")),
            "T34b a slot with no credential offers no switch", "the button is there anyway");
        g.Dispose();

        // A frozen slot is still a live slot and is still switchable: freezing is
        // about the account, not about who is signed into Google.
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm h = MakeForm(true);
        PollLabel(h, "AG3");
        Select(h, "AG3");
        Check(HasButtonLabel(PaintAccounts(h), Loc("SwitchAccount")),
            "T34c a frozen but signed-in slot can still switch its Google account", "no such button");
        h.Dispose();
        f.Dispose();
    }

    // (12, 13, 14) The isolation claim, which is the one that matters most: a
    // switch on AG2 must issue logout for AG2 and nothing else. The engine call
    // log is the proof -- it is literally what the engine was handed.
    static void T35SwitchTargetsOnlyTheSelectedContext()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Form = f;
        object sw = SwitchButton(PaintAccounts(f));
        Check(sw != null, "T35  the switch action is present on the panel", "no such button");
        if (sw != null)
        {
            Press(sw);
            Pump(() => HasCallFor("logout", "AG2", f) || HasCallFor("auth", "AG2", f), 40000);
        }
        Check(HasCallFor("logout", "AG2", f),
            "T35b switching AG2 signs AG2 out", Show());
        Check(!HasCallFor("logout", "AG1", f) && !HasCallFor("logout", "AG3", f),
            "T35c and signs NOBODY else out", Show());
        Check(!HasCallFor("auth", "AG1", f) && !HasCallFor("auth", "AG3", f),
            "T35d no other slot is sent through a sign-in either", Show());
        Check(!AnyLaunch(),
            "T35e a switch never launches a console", Show());
        f.Dispose();
    }

    // (17) The end of the happy path: after the operator completes the OAuth, a
    // DIFFERENT verified fingerprint appears and the panel says so.
    static void T36SwitchCompletesToANewIdentity()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice,
              "identity_fp_after=" + OtherFp, "identity_label_after=" + Bob);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Form = f;
        object sw = SwitchButton(PaintAccounts(f));
        if (sw != null)
        {
            Press(sw);
            // The post-auth poll sleeps before its first read, so the wait here
            // is deliberately generous: this is the one test that has to outlast
            // a real sleep in the code under test.
            Pump(() => Note(f).Contains(Bob), 90000);
        }
        Check(Note(f).Contains(Bob),
            "T36  a completed switch names the Google account that came back", Note(f));
        PollLabel(f, "AG2");
        Check(LabelOf(f, "AG2") == Bob,
            "T36b and the row carries the new label", "AG2 label=" + LabelOf(f, "AG2"));
        Check(!Note(f).Contains(Loc("SwitchUnchanged")),
            "T36c a real identity change is never reported as no change", Note(f));
        // (16) The transition has to be REAL, not merely narrated: the sign-in
        // is opened only after the slot was observed with no credential at all.
        // The call order is the proof -- logout, then at least one status read
        // while it was gone, then auth. A flow that issued auth straight after
        // logout would open a browser over a credential that never went, and
        // would still produce the same final note.
        int iOut = Where("logout", "AG2");
        int iIn = Where("auth", "AG2");
        if (iOut < 0) iOut = Where("logout", (string)GetOf(Row(f, "AG2"), "Id"));
        if (iIn < 0) iIn = Where("auth", (string)GetOf(Row(f, "AG2"), "Id"));
        bool waited = false;
        for (int i = 0; i < iIn; i++)
        {
            if (Calls()[i].Length > 1 && Calls()[i][0] == "status"
                && (Calls()[i][1] == "AG2" || Calls()[i][1] == (string)GetOf(Row(f, "AG2"), "Id")))
            { waited = true; break; }
        }
        Check(iOut >= 0 && iIn > iOut && waited,
            "T36d the slot was observed AUTH_REQUIRED between the sign-out and the sign-in",
            "logout@" + iOut + " auth@" + iIn + " waited=" + waited + " log=" + Show());
        f.Dispose();
    }

    // (18) The failure mode that must never be papered over: the operator signs
    // back into the SAME account, nothing changed, and the UI says the switch
    // did not change identity. Claiming success here is how a slot ends up
    // believed to hold a second quota pool while holding the first one again.
    static void T37SwitchIntoTheSameAccountIsReportedAsNoSwitch()
    {
        ResetLog();
        // No *_after keys: the identity after the sign-in is the identity before,
        // which is exactly "the operator picked the same account again".
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Form = f;
        object sw = SwitchButton(PaintAccounts(f));
        if (sw != null)
        {
            Press(sw);
            // Ten minutes of polling is the real deadline; the test does not sit
            // through it. The helper takes the deadline as an argument precisely
            // so the unchanged case can be proven in seconds.
            M(f, "PollForIdentityChange", (string)GetOf(Row(f, "AG2"), "Id"), "AG2",
              SharedFp, Alice, DateTime.UtcNow.AddSeconds(9));
            Pump(() => Note(f).Length > 0 && !Note(f).Contains("выход") && !Note(f).Contains("signing out"), 40000);
        }
        // The template is rendered with the label the slot had BEFORE the
        // attempt, so the comparison is against the formatted sentence, not the
        // raw "{0}" pattern.
        Check(Note(f).Contains(SwitchText("SwitchUnchanged", Alice)),
            "T37  signing back into the SAME account is reported as exactly that", Note(f));
        f.Dispose();
    }

    // The step-E guard. A logout that answers success over a store it did not own
    // must not be taken at its word: the GUI confirms the credential is gone
    // before opening a sign-in meant to replace it, and stops when it is not.
    static void T38SwitchStopsWhenTheCredentialIsStillThere()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice,
              "logout_sticks=1");
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Form = f;
        object sw = SwitchButton(PaintAccounts(f));
        if (sw != null)
        {
            Press(sw);
            Pump(() => !Busy(f) && Note(f).Length > 0, 90000);
        }
        Check(!HasCall("auth", "AG2"),
            "T38  a logout that did not take effect never opens a sign-in on top of it", Show());
        Check(Note(f).Contains("still signed in"),
            "T38b and the operator is told the account is still signed in", Note(f));
        f.Dispose();
    }

    // (8) The semantic boundary. Connect starts the managed context; it is not
    // OAuth, it does not replace a credential, and it must not issue a sign-in.
    // The fake's credential survives a connect untouched, which is the second
    // half: a connect that quietly rotated the login would change who the
    // account is without anyone asking.
    static void T39ConnectNeverAuthenticates()
    {
        ResetLog();
        Scene("offline_status_calls=1",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Form = f;
        IList buttons = PaintAccounts(f);
        object conn = ConnectionButton(buttons);
        Check(conn != null, "T39  the panel offers a connection control", "none of " + buttons.Count);
        if (conn != null) Press(conn);

        Check(ContextCall("start", "AG2") != null || ContextCall("connect", "AG2") != null,
            "T39b Connect starts the managed context",
            "AG2 ctx=" + GetOf(Row(f, "AG2"), "ContextState") + " auth=" + GetOf(Row(f, "AG2"), "AuthState")
            + " note=" + Note(f) + " log=" + Show());
        Check(!HasCall("auth", "AG2"),
            "T39c and Connect never starts a sign-in", Show());
        Check(!HasCall("logout", "AG2"),
            "T39d Connect never signs the account out either", Show());

        // The credential is still the same one afterwards: same fingerprint, so
        // a connect that replaced the login would show up as an identity change.
        PollIdentity(f, "AG2", true);
        Check((string)GetOf(Row(f, "AG2"), "IdentityFp") == SharedFp,
            "T39e the Google identity is unchanged by a reconnect", Show());
        f.Dispose();
    }

    // (20) Nothing that could authenticate anybody travels the text an operator
    // pastes into a support thread. The label is allowed -- it is the point of
    // the feature -- but a JWT, an access token or a credential blob is not.
    static void T40NoTokenMaterialInTheDiagnostics()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        PollIdentity(f, "AG2", true);
        PollLabel(f, "AG2");
        SF(f, "_acctAdvancedExpanded", true);
        var bmp = new Bitmap(900, 700);
        using (var g = Graphics.FromImage(bmp))
            M(f, "DrawAccountsTab", g, 0, 0, 900, 700);
        object copy = null;
        foreach (object b in (IList)F(f, "Buttons"))
        {
            var r = (Rectangle)b.GetType().GetField("R", Any).GetValue(b);
            if (r.Width == 116 && r.Height == 18) { copy = b; break; }
        }
        string diag = "";
        if (copy != null)
        {
            Clipboard.Clear();
            ((Action)copy.GetType().GetField("A", Any).GetValue(copy))();
            diag = Clipboard.ContainsText() ? Clipboard.GetText() : "";
        }
        Check(!diag.Contains("eyJ") && !diag.Contains("id_token") && !diag.Contains("Bearer"),
            "T40  no JWT, token name or bearer header reaches the copied diagnostics",
            diag.Replace("\n", " | "));
        Check(diag.Contains("ConsoleIdentity: " + SharedFp),
            "T40b the identity itself is still in there -- the guard is not over-broad", diag.Replace("\n", " | "));
        bmp.Dispose();
        f.Dispose();
    }

    // The switch is minutes long and the operator is clicking around while it
    // runs. Its polling must stay off the UI thread or the window stops
    // repainting exactly when the operator needs to see it change.
    static void T41SwitchPollDoesNotTouchTheUiThread()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice,
              "identity_fp_after=" + OtherFp, "identity_label_after=" + Bob);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Form = f;
        object sw = SwitchButton(PaintAccounts(f));
        if (sw != null)
        {
            Press(sw);
            Pump(() => HasCall("auth", "AG2"), 40000);
        }
        // Still polling for the operator's browser answer. The UI has to be
        // responsive throughout, which is observable by painting again.
        int seen = Buttons(f);
        Check(seen > 0,
            "T41  the tab keeps painting while the post-auth poll is still waiting", "no buttons");
        Check(!Busy(f),
            "T41b and the tab is not left stuck on the working caption", "busy=" + Busy(f));
        f.Dispose();
    }

    // (7) The timestamp trap. `credential_last_written` is a fact about a
    // FILE: it moves on every token refresh, on every reconnect, and it moves
    // independently on every machine. Two slots holding one Google account are
    // refreshed at different moments and therefore carry different timestamps
    // forever. Any grouping that reads it invents a second identity -- the same
    // false capacity the fingerprint exists to prevent.
    static void T42CredentialTimestampIsNotAnIdentity()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp=" + SharedFp, "configured_label=" + Alice,
              "identity_fp=" + SharedFp, "identity_label=" + Alice,
              "credential_written_AG1=2024-01-01T00:00:00Z",
              "credential_written_AG3=2026-09-30T04:15:00Z");
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG1");
        PollLabel(f, "AG3");

        Check(SharedOf(f, "AG3").Length > 0,
            "T42  two years of credential age do NOT split one Google account into two",
            "AG3 shared='" + SharedOf(f, "AG3") + "' table=" + Table(f));
        Check(SharedOf(f, "AG1").Length > 0,
            "T42b and the warning still lands on the older slot", "AG1 shared='" + SharedOf(f, "AG1") + "'");
        f.Dispose();

        // The mirror image: an identical timestamp on two DIFFERENT identities
        // must not merge them. "Same time" is not "same human" either.
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
              "configured_fp_AG2=" + OtherFp, "configured_label_AG2=" + Bob,
              "identity_fp_AG1=" + SharedFp, "identity_label_AG1=" + Alice,
              "identity_fp_AG2=" + OtherFp, "identity_label_AG2=" + Bob,
              "credential_written_AG1=2026-09-30T04:15:00Z",
              "credential_written_AG2=2026-09-30T04:15:00Z");
        Saituls.SaitulsForm g = MakeForm(true);
        PollLabel(g, "AG1");
        PollLabel(g, "AG2");
        Check(SharedOf(g, "AG1").Length == 0 && SharedOf(g, "AG2").Length == 0,
            "T42c and one timestamp on two humans does not merge them either",
            "AG1='" + SharedOf(g, "AG1") + "' AG2='" + SharedOf(g, "AG2") + "'");
        g.Dispose();
    }

    // (13 mirrored) T35 proves a switch on AG2 signs nobody else out. The other
    // direction is the one an operator actually runs when AG1 is the wrong
    // account, so it gets its own proof rather than being assumed by symmetry.
    static void T43SwitchingThePrimaryTouchesOnlyThePrimary()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG1");
        Select(f, "AG1");
        Form = f;
        object sw = SwitchButton(PaintAccounts(f));
        Check(sw != null, "T43  the primary slot offers the switch too", "no such button");
        if (sw != null)
        {
            Press(sw);
            Pump(() => HasCallFor("logout", "AG1", f) || HasCallFor("auth", "AG1", f), 40000);
        }
        Check(HasCallFor("logout", "AG1", f),
            "T43b switching AG1 signs AG1 out", Show());
        Check(!HasCallFor("logout", "AG2", f) && !HasCallFor("logout", "AG3", f),
            "T43c AG1's logout cannot reach AG2's credential store, or AG3's", Show());
        Check(!HasCallFor("auth", "AG2", f) && !HasCallFor("auth", "AG3", f),
            "T43d and neither managed slot is sent through a sign-in", Show());
        f.Dispose();
    }

    // (15) Signing out of Google is not removing the account. The slot stays
    // configured, listed and connected; only its provider authentication is
    // gone. Anything that hid, archived or removed an account as a side effect
    // of a provider logout would take the operator's Windows identity with it.
    static void T44LogoutLeavesTheAccountConfigured()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp, "identity_label=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        PollLabel(f, "AG2");
        Select(f, "AG2");
        Form = f;
        object sw = SwitchButton(PaintAccounts(f));
        if (sw != null)
        {
            Press(sw);
            Pump(() => HasCallFor("logout", "AG2", f), 40000);
        }
        foreach (string verb in new[] { "set-hidden", "archive", "remove", "add" })
            Check(!HasCall(verb, "AG2") && !HasCallFor(verb, "AG2", f),
                "T44  a provider logout never calls " + verb, Show());

        Check(Row(f, "AG2") != null,
            "T44b the account is still listed after its Google sign-out", "the row is gone");
        Check((string)GetOf(Row(f, "AG2"), "ContextState") == "ONLINE",
            "T44c and its managed context is still up -- only the provider login went",
            "AG2 ctx=" + (string)GetOf(Row(f, "AG2"), "ContextState")
            + " auth=" + (string)GetOf(Row(f, "AG2"), "AuthState"));
        f.Dispose();
    }

    static object SwitchButton(IList buttons)
    {
        return ButtonWithLabel(buttons, Loc("SwitchAccount"));
    }
    // Any painted button carrying this caption. Null when the panel does not
    // offer it, which is the only honest way to ask "is this action there?".
    static object ButtonWithLabel(IList buttons, string label)
    {
        foreach (object b in buttons)
            if ((string)b.GetType().GetField("Label", Any).GetValue(b) == label) return b;
        return null;
    }
    // Paint the panel and search it in one step.
    static object ButtonWithLabel(Saituls.SaitulsForm f, string label)
    {
        return ButtonWithLabel(PaintAccounts(f), label);
    }

    // What the panel shows for one slot, as text.
    //
    // It paints for real first -- a draw that throws fails the test rather than
    // quietly returning "" -- and then renders the same facts the panel renders,
    // through the same localisation table, so the assertions below are about the
    // words the operator reads rather than about words this test invented.
    //
    // The IDE line and the console line are printed unconditionally and
    // neutrally: a difference between the two planes is a fact, not a state.
    // LegacyMismatchText() appends whatever the OLD contract would have drawn,
    // and is empty on a build that no longer has one -- which is what lets this
    // same assertion fail before the fix and pass after it.
    static string PanelText(Saituls.SaitulsForm f, string compact)
    {
        if (!Select(f, compact)) return "";
        var bmp = new Bitmap(900, 700);
        try
        {
            using (var g = Graphics.FromImage(bmp))
                M(f, "DrawAccountsTab", g, 0, 0, 900, 700);
        }
        finally { bmp.Dispose(); }

        object row = Row(f, compact);
        var sb = new System.Text.StringBuilder();
        string shared = (string)GetOf(row, "IdentitySharedWith");
        string console = ConsoleLabelOf(row).Length > 0 ? ConsoleLabelOf(row) : ConsoleFpOf(row);
        string ide = IdeLabelOf(row).Length > 0 ? IdeLabelOf(row) : IdeFpOf(row);
        sb.Append(Loc("ConsoleAccount")).Append(": ")
          .Append(console.Length > 0 ? console : "(unproven)");
        sb.Append(" | ").Append(Loc("IdeAccount")).Append(": ")
          .Append(ide.Length > 0 ? ide : "(unproven)");
        if (shared.Length > 0) sb.Append(" | ").Append(SharedText(shared));
        sb.Append(" | ").Append(LegacyMismatchText(row));
        return sb.ToString();
    }

    // The rendered duplicate line: the localisation template filled with the
    // other slots' compact labels.
    static string SharedText(string shared)
    {
        MethodInfo mi = typeof(Saituls.AccountsLoc).GetMethod("T", Any, null,
            new[] { typeof(string), typeof(string) }, null);
        return (string)mi.Invoke(null, new object[] { "QuotaShared", shared });
    }
    // A switch sentence with its {0} filled in, the way the UI fills it.
    static string SwitchText(string key, string arg)
    {
        MethodInfo mi = typeof(Saituls.AccountsLoc).GetMethod("T", Any, null,
            new[] { typeof(string), typeof(string) }, null);
        return (string)mi.Invoke(null, new object[] { key, arg });
    }

    // The whole panel turns on one question the operator has to be able to
    // answer without learning "windows_user" from "managed_windows_user": does
    // this account need anything done to it? AG1 does not. AG2 does, and only
    // AG2.
    static IList PaintAccounts(Saituls.SaitulsForm f)
    {
        var bmp = new Bitmap(900, 700);
        var buttons = new List<object>();
        // OnPaint owns the clear, not DrawAccountsTab. A harness that calls the
        // draw method directly therefore accumulates every button every panel
        // has ever registered, and "this account has no Connect button" starts
        // failing for a reason that has nothing to do with the account.
        ((IList)F(f, "Buttons")).Clear();
        using (var g = Graphics.FromImage(bmp))
            M(f, "DrawAccountsTab", g, 0, 0, 900, 700);
        foreach (object b in (IList)F(f, "Buttons")) buttons.Add(b);
        bmp.Dispose();
        return buttons;
    }

    static bool HasButtonLabel(IList buttons, string label)
    {
        foreach (object b in buttons)
            if ((string)b.GetType().GetField("Label", Any).GetValue(b) == label) return true;
        return false;
    }

    // The Connect/Disconnect control is the only 74x18 box on the panel (SIGN IN
    // is 72). Identifying it by geometry and then PRESSING it proves which verb
    // it carries, which is stronger than reading a caption off it.
    // Every caption the last paint offered, so a "the control is missing"
// failure can say what WAS there.
    static List<string> Labels(Saituls.SaitulsForm f)
    {
        var names = new List<string>();
        foreach (object b in PaintAccounts(f))
        {
            string l = (string)b.GetType().GetField("Label", Any).GetValue(b);
            names.Add(l == null ? "(none)" : l);
        }
        return names;
    }
    static object ConnectionButton(IList buttons)
    {
        foreach (object b in buttons)
        {
            var r = (Rectangle)b.GetType().GetField("R", Any).GetValue(b);
            if (r.Width == 74 && r.Height == 18) return b;
        }
        return null;
    }

    static void Press(object button)
    {
        ((Action)button.GetType().GetField("A", Any).GetValue(button))();
        SetBudget(30000);
        Pump(() => !Busy(Form), 20000);
    }

    static Saituls.SaitulsForm Form;

    static string Loc(string key)
    {
        return (string)typeof(Saituls.AccountsLoc)
            .GetMethod("T", Any, null, new[] { typeof(string) }, null)
            .Invoke(null, new object[] { key });
    }

    static string Eval(Saituls.SaitulsForm f, string method, object row)
    {
        return (string)f.GetType().GetMethod(method, Any, null,
            new[] { f.GetType().GetNestedType("AccountRow", Any) }, null)
            .Invoke(f, new[] { row });
    }

    static void TestStateMatrix()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + SharedFp);
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;

        // AG1: the operator's own Windows user. Nothing to connect, so no
        // Connect, no Disconnect, and nothing pretending an intent exists.
        Select(f, "AG1");
        object ag1 = Row(f, "AG1");
        IList b1 = PaintAccounts(f);
        Check(ConnectionButton(b1) == null,
            "SM1 the current-user account never offers Connect or Disconnect",
            string.Join(",", Labels(b1)));
        string cur = Eval(f, "ExecutionLabel", ag1);
        Check(cur == Loc("ExecCurrentUser"),
            "SM2 the current-user account says it runs in the current Windows user", cur);
        Check(cur != Loc("Shared") && cur != Loc("Local"),
            "SM3 'Shared' and 'Local' never stand in for how the account executes", cur);

        // AG2 offline with the operator's intent off: this is the ONE ordinary
        // state where Connect is the way back.
        Arm("offline_status_calls=999999");
        Select(f, "AG2");
        object ag2 = Row(f, "AG2");
        SetOf(ag2, "ContextState", "OFFLINE");
        SetOf(ag2, "AutoConnect", false);
        SetOf(ag2, "AutoConnectReason", "operator_disconnect");
        IList b2 = PaintAccounts(f);
        object connect = ConnectionButton(b2);
        Check(connect != null,
            "SM4 a managed account that is offline and unwanted offers Connect",
            string.Join(",", Labels(b2)));
        if (connect != null)
        {
            Press(connect);
            Check(ContextCall("connect", "AG2") != null,
                "SM4b the offline control is a Connect", Show());
        }
        Check(Eval(f, "ExecutionLabel", ag2) == Loc("ExecManaged"),
            "SM5 the managed account says it runs in a managed Windows user",
            Eval(f, "ExecutionLabel", ag2));
        Check(Eval(f, "AutoConnectNote", ag2) == Loc("OffByChoice"),
            "SM6 Auto-connect OFF names the hand that turned it off",
            Eval(f, "AutoConnectNote", ag2));

        // Same account ONLINE with the intent still ON: Disconnect, and the OFF
        // note is gone because there is no longer an OFF to explain. The row
        // is re-fetched after the press above, which reloads the list.
        Arm("offline_status_calls=0");
        Select(f, "AG2");
        ag2 = Row(f, "AG2");
        SetOf(ag2, "ContextState", "ONLINE");
        SetOf(ag2, "AutoConnect", true);
        SetOf(ag2, "AutoConnectReason", "operator_connect");
        IList b3 = PaintAccounts(f);
        object disconnect = ConnectionButton(b3);
        Check(disconnect != null,
            "SM7 an online managed account offers Disconnect", string.Join(",", Labels(b3)));
        if (disconnect != null)
        {
            Press(disconnect);
            Check(ContextCall("disconnect", "AG2") != null,
                "SM7b the online control is a Disconnect", Show());
        }
        Check(Eval(f, "AutoConnectNote", ag2) == "",
            "SM8 an account that wants connection carries no OFF explanation", "note not empty");

        // A broker that died is not a decision. The row says OFFLINE, the
        // intent stays ON, and the note stays empty -- nothing invents a hand.
        SetOf(ag2, "ContextState", "OFFLINE");
        SetOf(ag2, "AutoConnect", true);
        Check(Eval(f, "AutoConnectNote", ag2) == "",
            "SM9 an offline broker never produces an 'you turned this off' note",
            Eval(f, "AutoConnectNote", ag2));

        // An origin we cannot prove is never dressed up as a choice.
        SetOf(ag2, "AutoConnect", false);
        SetOf(ag2, "AutoConnectReason", "unknown");
        Check(Eval(f, "AutoConnectNote", ag2) == Loc("OffUnknown"),
            "SM10 an unprovable OFF does not claim a decision",
            Eval(f, "AutoConnectNote", ag2));

        // The matrix has to survive a repaint, not just the first paint.
        Select(f, "AG1");
        IList b5 = PaintAccounts(f);
        Check(ConnectionButton(b5) == null,
            "SM11 the current-user panel is still button-free after a repaint",
            string.Join(",", Labels(b5)));
        f.Dispose();
        Form = null;
    }

    static string[] Labels(IList buttons)
    {
        var outp = new List<string>();
        foreach (object b in buttons)
            outp.Add((string)b.GetType().GetField("Label", Any).GetValue(b));
        return outp.ToArray();
    }

    // An unproven identity must still reach the diagnostics, labelled. Dropping
    // the line entirely would leave the operator with no way to tell "not
    // signed in" from "old build that never looked".
    static void Test29kUnprovenIdentityIsLabelledInDiagnostics()
    {
        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(true);
        Select(f, "AG2");
        PollIdentity(f, "AG2", false);

        SF(f, "_acctAdvancedExpanded", true);
        var bmp = new Bitmap(900, 700);
        using (var g = Graphics.FromImage(bmp))
            M(f, "DrawAccountsTab", g, 0, 0, 900, 700);

        var buttons = (IList)F(f, "Buttons");
        string diag = "";
        foreach (object b in buttons)
        {
            var r = (Rectangle)b.GetType().GetField("R", Any).GetValue(b);
            if (r.Width != 116 || r.Height != 18) continue;
            Clipboard.Clear();
            ((Action)b.GetType().GetField("A", Any).GetValue(b))();
            diag = Clipboard.ContainsText() ? Clipboard.GetText() : "";
        }
        Check(diag.Contains("ConsoleIdentity: (unproven)"),
            "T29k an unproven canonical identity is labelled, not left blank", diag.Replace("\n", " | "));
        Check(diag.Contains("IdeIdentity: (unproven)"),
            "T29k2 and so is an unproven IDE identity", diag.Replace("\n", " | "));
        bmp.Dispose();
        f.Dispose();
    }

    // ---- paint helpers -----------------------------------------------------
    static void SetBudget(int ms)
    {
        typeof(Saituls.SaitulsForm).GetField("ContextStartBudgetMs", Any).SetValue(null, ms);
    }
    static object Prop(object o, string name)
    {
        return o.GetType().GetProperty(name, Any).GetValue(o, null);
    }
    // Draw the Accounts tab once and return how many clickable targets it made.
    static int Buttons(Saituls.SaitulsForm f)
    {
        ((IList)F(f, "Buttons")).Clear();
        using (var bmp = new Bitmap(600, 500))
        using (Graphics g = Graphics.FromImage(bmp))
            M(f, "DrawAccountsTab", g, 20, 20, 520, 460);
        return ((IList)F(f, "Buttons")).Count;
    }
    // Rows alone add full-width hit targets; the action buttons are narrower.
    static int RowButtons(Saituls.SaitulsForm f)
    {
        ((IList)F(f, "Buttons")).Clear();
        using (var bmp = new Bitmap(600, 500))
        using (Graphics g = Graphics.FromImage(bmp))
            M(f, "DrawAccountsTab", g, 20, 20, 520, 460);
        int n = 0;
        foreach (object b in (IList)F(f, "Buttons"))
            if (((Rectangle)b.GetType().GetField("R", Any).GetValue(b)).Width >= 160) n++;
        return n;
    }

    // ====================================================== T-244 provider catalog
    //
    // The dialog used to carry a hardcoded three-item ComboBox and a
    // `pIdx == 1 ? "claude" : ...` ternary, and it leaked a white rectangle from
    // a system-drawn closed state and popup list. Both are tested here against
    // the REAL widgets and the REAL paint routine: the dialog is constructed
    // through the same builder the operator's button uses, and colours are read
    // off rendered pixels rather than off the constants that produced them.

    // Relative luminance, the WCAG definition. Lightness is the question the
    // white leak is actually about, so it is measured rather than asserted by
    // reading back the same constant the paint used.
    static double Luminance(Color c)
    {
        Func<int, double> ch = delegate (int v)
        {
            double s = v / 255.0;
            return s <= 0.03928 ? s / 12.92 : Math.Pow((s + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * ch(c.R) + 0.7152 * ch(c.G) + 0.0722 * ch(c.B);
    }

    static double Contrast(Color a, Color b)
    {
        double la = Luminance(a), lb = Luminance(b);
        if (la < lb) { double t = la; la = lb; lb = t; }
        return (la + 0.05) / (lb + 0.05);
    }

    // Render one provider row exactly as the dropdown would, and hand back the
    // pixels. The caller owns the bitmap.
    static Bitmap RenderProviderRow(Saituls.ProviderEntry e, bool selected)
    {
        var bmp = new Bitmap(360, 22);
        using (Graphics g = Graphics.FromImage(bmp))
        using (Font f = new Font("Verdana", 8.25f))
            Saituls.SaitulsForm.PaintProviderRow(g,
                new Rectangle(0, 0, 360, 22), e.DisplayName, e.AvailabilityTag,
                selected, false, f);
        return bmp;
    }

    static void T45ProviderCatalogIsTheDialogsSingleSource()
    {
        ComboBox combo; TextBox alias; Label note; Button ok;
        using (Form dlg = Saituls.SaitulsForm.BuildAddAccountDialog(out combo, out alias, out note, out ok))
        {
            bool bound = combo.Items.Count == Saituls.ProviderCatalog.Count;
            var mismatch = new System.Text.StringBuilder();
            if (bound)
                for (int i = 0; i < combo.Items.Count; i++)
                    if ((string)combo.Items[i] != Saituls.ProviderCatalog.At(i).ListText)
                        mismatch.Append(i).Append(':').Append(combo.Items[i]).Append(' ');
            Check(bound && mismatch.Length == 0,
                "T45.1 the Add Account dropdown is bound to the provider catalog, one row per entry",
                "items=" + combo.Items.Count + " catalog=" + Saituls.ProviderCatalog.Count
                    + " mismatch=" + mismatch);

            // (2) ZCode is the entry the operator asked for by name.
            Saituls.ProviderEntry zcode = Saituls.ProviderCatalog.ById("zcode");
            bool zcodeVisible = zcode != null;
            for (int i = 0; zcodeVisible && i < combo.Items.Count; i++)
                if (((string)combo.Items[i]).IndexOf("ZCode", StringComparison.Ordinal) >= 0) zcodeVisible = true;
            Check(zcodeVisible,
                "T45.2 ZCode is in the catalog and visible in the Add Account list",
                "catalog=" + (zcode == null ? "absent" : zcode.DisplayName)
                    + " items=" + string.Join(",", ToStrings(combo)));

            // (3) A provider that cannot be created says so, in words that admit
            // it, and never shows the "this will happen next" help text.
            bool honest = true;
            var why = new System.Text.StringBuilder();
            for (int i = 0; i < Saituls.ProviderCatalog.Count; i++)
            {
                Saituls.ProviderEntry e = Saituls.ProviderCatalog.At(i);
                if (e.SupportsAddNow) continue;
                string n = e.OperatorNote();
                string low = n.ToLowerInvariant();
                bool saysWhy = n.Length > 0 && n != e.Description
                    && (low.Contains("not") || low.Contains("pending") || low.Contains("yet")
                        || low.Contains("planned") || low.Contains("unavailable"));
                if (!saysWhy) { honest = false; why.Append(e.ProviderId).Append("->'").Append(n).Append("' "); }
            }
            Check(honest,
                "T45.3 every planned provider shows an honest note instead of help text",
                why.ToString());

            // (4) One truth source for the button, walked across every row.
            bool stateOk = true;
            var seen = new System.Text.StringBuilder();
            for (int i = 0; i < combo.Items.Count; i++)
            {
                combo.SelectedIndex = i;
                Saituls.ProviderEntry e = Saituls.ProviderCatalog.At(i);
                if (ok.Enabled != e.SupportsAddNow || alias.Enabled != e.SupportsAddNow)
                {
                    stateOk = false;
                    seen.Append(e.ProviderId).Append("(btn=").Append(ok.Enabled)
                        .Append(",alias=").Append(alias.Enabled).Append(") ");
                }
            }
            Check(stateOk,
                "T45.4 the Add button and the alias field are enabled exactly for providers that can be created",
                seen.ToString());

            // (5) The note tracks the selection instead of being one fixed
            // paragraph describing whichever provider happens to be first.
            var notes = new System.Collections.Generic.HashSet<string>();
            bool matched = true;
            for (int i = 0; i < combo.Items.Count; i++)
            {
                combo.SelectedIndex = i;
                Saituls.ProviderEntry e = Saituls.ProviderCatalog.At(i);
                if (note.Text != e.OperatorNote()) matched = false;
                notes.Add(note.Text);
            }
            combo.SelectedIndex = 0;
            Check(matched && notes.Count == combo.Items.Count,
                "T45.5 the note under the dropdown updates to describe the selected provider",
                "distinct=" + notes.Count + " of " + combo.Items.Count + " matched=" + matched);

            // (6) The closed state. OwnerDraw + Flat is the recipe; the colours
            // are checked too, so a later edit cannot quietly drop back to the
            // system look without failing here.
            Check(combo.DrawMode == DrawMode.OwnerDrawFixed
                    && combo.FlatStyle == FlatStyle.Flat
                    && Luminance(combo.BackColor) < 0.20
                    && Contrast(combo.ForeColor, combo.BackColor) >= 4.5,
                "T45.6 the closed provider field is owner-drawn and dark, with legible text",
                "draw=" + combo.DrawMode + " flat=" + combo.FlatStyle
                    + " back=" + combo.BackColor + " lum=" + Luminance(combo.BackColor).ToString("0.00")
                    + " contrast=" + Contrast(combo.ForeColor, combo.BackColor).ToString("0.0"));

            // (7)+(8) The popup list, painted for real. The white rectangle lived
            // here, so the pixels are read off the rendered rows.
            Saituls.ProviderEntry probe = Saituls.ProviderCatalog.ById("zcode");
            using (Bitmap plain = RenderProviderRow(probe, false))
            using (Bitmap sel = RenderProviderRow(probe, true))
            {
                Color pb = plain.GetPixel(3, 3), sb = sel.GetPixel(3, 3);
                Check(Luminance(pb) < 0.20 && Luminance(sb) < 0.20 && pb != sb,
                    "T45.7 the provider list paints dark rows, and the selected row is visibly different",
                    "plain=" + pb + " selected=" + sb
                        + " plainLum=" + Luminance(pb).ToString("0.00")
                        + " selLum=" + Luminance(sb).ToString("0.00"));
                Check(Contrast(Saituls.Palette.TEXT, sb) >= 4.5,
                    "T45.8 the selected row stays readable -- no white, but no invisible text either",
                    "contrast=" + Contrast(Saituls.Palette.TEXT, sb).ToString("0.0")
                        + " text=" + Saituls.Palette.TEXT + " row=" + sb);
            }

            // (9)+(10) The rest of the dialog. A TextBox and two buttons leak
            // exactly as much as a ComboBox does, so they are measured too.
            var light = new System.Text.StringBuilder();
            foreach (Control c in AllControls(dlg))
            {
                // Transparent is not a colour: a transparent label inherits its
                // parent's dark background and cannot leak.
                if (c.BackColor.A != 0 && Luminance(c.BackColor) >= 0.20)
                    light.Append(c.GetType().Name).Append('=').Append(c.BackColor).Append(' ');
            }
            Check(light.Length == 0,
                "T45.9 no control in the Add Account dialog paints a light background",
                light.ToString());

            Check(Contrast(alias.ForeColor, alias.BackColor) >= 4.5
                    && Contrast(ok.ForeColor, ok.BackColor) >= 4.5,
                "T45.10 the alias field and the buttons are themed and their text still readable",
                "alias=" + Contrast(alias.ForeColor, alias.BackColor).ToString("0.0")
                    + " ok=" + Contrast(ok.ForeColor, ok.BackColor).ToString("0.0"));
        }
    }

    static string[] ToStrings(ComboBox c)
    {
        var s = new string[c.Items.Count];
        for (int i = 0; i < s.Length; i++) s[i] = (string)c.Items[i];
        return s;
    }

    static IEnumerable<Control> AllControls(Control root)
    {
        foreach (Control c in root.Controls)
        {
            yield return c;
            foreach (Control g in AllControls(c)) yield return g;
        }
    }

    // (11)+(12) A planned provider cannot even name a backend, and the flow
    // refuses it anyway. Together these are what make the disabled button safe
    // rather than merely polite: the engine is where a broken account would be
    // born, so the assertion is that it is never called at all.
    static void T46PlannedProvidersCannotNameABackend()
    {
        bool ok = true;
        var why = new System.Text.StringBuilder();
        for (int i = 0; i < Saituls.ProviderCatalog.Count; i++)
        {
            string id = Saituls.ProviderCatalog.EngineProviderId(i);
            Saituls.ProviderEntry e = Saituls.ProviderCatalog.At(i);
            string want = e.SupportsAddNow ? e.ProviderId : "";
            if (id != want) { ok = false; why.Append(e.ProviderId).Append("->'").Append(id).Append("' "); }
        }
        Check(ok,
            "T46 a provider that cannot be created resolves to no engine provider id at all",
            why.ToString());

        // The log is armed AFTER the form exists: constructing it runs LoadAccounts,
        // whose own `list --all` would otherwise be counted as engine contact
        // caused by the flow under test.
        Saituls.SaitulsForm f = MakeForm(true);
        Arm("offline_status_calls=0");
        M(f, "AddAccountFlow", "zcode", null);
        Thread.Sleep(300);
        Check(Calls().Length == 0,
            "T46b AddAccountFlow with a planned provider contacts the engine zero times", Show());
        Check(Note(f).StartsWith("ADD_REFUSED"),
            "T46c and says plainly that the provider is not available",
            "note='" + Note(f) + "'");
        f.Dispose();
    }

    // (13) The three providers that actually work still work. A wider catalog is
    // worthless if it loses the entries it already had.
    static void T47ExistingProvidersStillCreateAccounts()
    {
        var missing = new System.Text.StringBuilder();
        foreach (string prov in new[] { "antigravity", "claude", "codex" })
        {
            Saituls.SaitulsForm f = MakeForm(true);
            Arm("offline_status_calls=0");
            M(f, "AddAccountFlow", prov, "alias-" + prov);
            // The engine's argv is tab-separated and space-split into words, so
            // "add --provider claude" is THREE log fields. Matching on the
            // joined line is what asserts the command the operator chose.
            bool sent = false;
            foreach (string[] a in Calls())
                if (string.Join(" ", a).StartsWith("add --provider " + prov + " ")) sent = true;
            if (!sent) missing.Append(prov).Append(' ');
            f.Dispose();
        }
        Check(missing.Length == 0,
            "T47 antigravity, claude and codex still create accounts through the engine",
            "missing=" + missing + " last=" + Show());
    }

    // (14) The section-7 honesty invariant, checked against the real engine
    // source rather than against the catalog's own opinion of itself: every
    // provider the catalog calls Available must have a row in the engine's
    // provider table, and no provider outside that table may be called
    // Available. Either half failing is a lie, in opposite directions.
    static void T48CatalogNeverPromotesAProviderTheEngineCannotCreate()
    {
        string root = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".."));
        string engineSrc = Path.Combine(root, "Scripts", "accounts", "SaiAccounts.cs");

        var known = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (File.Exists(engineSrc))
        {
            foreach (string line in File.ReadAllLines(engineSrc))
            {
                int at = line.IndexOf("new ProviderDef", StringComparison.Ordinal);
                if (at < 0) continue;
                int q1 = line.IndexOf('"', at);
                if (q1 < 0) continue;
                int q2 = line.IndexOf('"', q1 + 1);
                if (q2 > q1) known.Add(line.Substring(q1 + 1, q2 - q1 - 1));
            }
        }

        var bad = new System.Text.StringBuilder();
        if (known.Count == 0) bad.Append("engine provider table unreadable at ").Append(engineSrc).Append(' ');
        for (int i = 0; i < Saituls.ProviderCatalog.Count && known.Count > 0; i++)
        {
            Saituls.ProviderEntry e = Saituls.ProviderCatalog.At(i);
            bool backend = known.Contains(e.ProviderId);
            if (e.SupportsAddNow && !backend) bad.Append(e.ProviderId).Append("=available-without-backend; ");
            if (!e.SupportsAddNow && backend) bad.Append(e.ProviderId).Append("=hidden-despite-backend; ");
        }
        Check(bad.Length == 0,
            "T48 Available matches the engine's provider table exactly -- no ghost providers, no hidden ones",
            bad.ToString());
    }

    // (15) Adding an account signs in through the ACCOUNT PLANE, not through a
    // project launch. The add path used to drive Google auth with
    // `launch --workdir` and DISCARD the CliRun, so every refusal it could
    // produce -- launch_failed, WORKDIR_UNAVAILABLE, CONTEXT_OFFLINE -- showed up
    // as the same 90-second "waiting for Google sign-in verification" while a
    // browser that was never a browser answered with a "Windows cannot find"
    // popup. Re-authenticate always did this right; add now uses the same
    // routine, so this test pins the ORDER (status, context start, auth) and the
    // ABSENCE of any project launch.
    static void T49AddAccountSignsInThroughTheAccountPlane()
    {
        // The account the fixture's `add` creates.
        const string newAcc = "antigravity:windows-user:1122334455667788";

        // (a) the working path.
        Saituls.SaitulsForm f = MakeForm(true);
        Arm("offline_status_calls=1", "auth_required_until=1");
        M(f, "AddAccountFlow", "antigravity", "alias-ag");
        Pump(() => Note(f).Contains("signed in"), 60000);

        var launched = new System.Text.StringBuilder();
        bool authNew = false, startNew = false, statusNew = false;
        foreach (string[] a in Calls())
        {
            if (a.Length > 0 && a[0] == "launch") launched.Append(string.Join(" ", a)).Append(" | ");
            // `context start <selector>` carries its selector in the THIRD
            // field; every other verb here carries it in the second.
            if (a.Length > 2 && a[0] == "context" && a[1] == "start" && a[2] == newAcc) startNew = true;
            if (a.Length > 1 && a[1] == newAcc)
            {
                if (a[0] == "auth") authNew = true;
                if (a[0] == "status") statusNew = true;
            }
        }
        Check(launched.Length == 0,
            "T49a adding an account never launches a project console for its sign-in",
            launched.ToString());
        Check(statusNew && startNew && authNew,
            "T49b and signs in the account it just created, through the account plane",
            "status=" + statusNew + " start=" + startNew + " auth=" + authNew + " last=" + Show());
        Check(Note(f).Contains("signed in"),
            "T49c a completed sign-in is reported as signed in", "note='" + Note(f) + "'");
        f.Dispose();

        // (b) the refusal path: the engine's own status, at once, and never the
        // disappearing wait the operator could not read.
        f = MakeForm(true);
        Arm("offline_status_calls=1", "auth_required_until=9", "auth_status=auth_launch_failed");
        M(f, "AddAccountFlow", "antigravity", "alias-ag2");
        Pump(() => Note(f).StartsWith("SIGN_IN_FAILED"), 60000);
        Check(Note(f).StartsWith("SIGN_IN_FAILED") && Note(f).Contains("auth_launch_failed"),
            "T49d a refused sign-in window is reported at once, naming the engine's own status",
            "note='" + Note(f) + "'");
        Check(!Note(f).Contains("Waiting for Google"),
            "T49e and the bogus 90-second wait is gone with it", "note='" + Note(f) + "'");
        f.Dispose();

        // (c) a sign-in is an account-plane operation: it needs no project
        // folder, and with none configured it still reaches auth instead of
        // demanding one or substituting the install root.
        f = MakeForm(false);
        Arm("offline_status_calls=1", "auth_required_until=1");
        M(f, "AddAccountFlow", "antigravity", null);
        Pump(() => Note(f).Contains("signed in"), 60000);
        bool anyLaunch = false;
        foreach (string[] a in Calls()) if (a.Length > 0 && a[0] == "launch") anyLaunch = true;
        Check(HasCall("auth", newAcc) && !anyLaunch,
            "T49f no project folder is needed, demanded or substituted for a sign-in",
            Show());
        f.Dispose();
    }

    static string[] AccountIds(Saituls.SaitulsForm f)
    {
        var list = (IList)F(f, "_accounts");
        var ids = new List<string>();
        if (list != null)
            foreach (object row in list) ids.Add((string)GetOf(row, "Id"));
        return ids.ToArray();
    }

    static string[] MoveCall()
    {
        foreach (string[] a in Calls())
            if (a.Length > 0 && a[0] == "move") return a;
        return null;
    }

    static int MoveCallCount()
    {
        int n = 0;
        foreach (string[] a in Calls())
            if (a.Length > 0 && a[0] == "move") n++;
        return n;
    }

    static void MouseDownLeft(Saituls.SaitulsForm f, int x, int y)
    {
        M(f, "OnMouseDown", new MouseEventArgs(MouseButtons.Left, 1, x, y, 0));
    }

    static void MouseMoveLeft(Saituls.SaitulsForm f, int x, int y)
    {
        M(f, "OnMouseMove", new MouseEventArgs(MouseButtons.Left, 0, x, y, 0));
    }

    static void MouseUpLeft(Saituls.SaitulsForm f, int x, int y)
    {
        M(f, "OnMouseUp", new MouseEventArgs(MouseButtons.Left, 1, x, y, 0));
    }

    static void T50AccountReorderContract()
    {
        const string id1 = "antigravity:windows-user:0000000000000001";
        const string id2 = "antigravity:windows-user:0000000000000002";
        const string id3 = "antigravity:windows-user:0000000000000003";

        // (1) Simple click selects without reorder; drag below threshold selects only.
        ResetLog();
        Scene("offline_status_calls=0");
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        SF(f, "CurrentTab", "Accounts");
        PaintAccounts(f);
        Arm("offline_status_calls=0");

        // Row 0 = AG1 (y=37..69), Row 1 = AG2 (y=71..103), Row 2 = AG3 (y=105..137)
        MouseDownLeft(f, 60, 87);
        MouseUpLeft(f, 60, 87);
        Check((string)F(f, "_selectedAccount") == id2 && MoveCallCount() == 0,
            "T50.1 a simple click on an account row selects it without reordering",
            "sel=" + F(f, "_selectedAccount") + " calls=" + Show());

        // Sub-threshold drag (3px vertical movement < threshold)
        MouseDownLeft(f, 60, 121);
        MouseMoveLeft(f, 60, 124);
        MouseUpLeft(f, 60, 124);
        Check((string)F(f, "_selectedAccount") == id3 && MoveCallCount() == 0,
            "T50.2 a drag below the movement threshold selects only and does not reorder",
            "sel=" + F(f, "_selectedAccount") + " calls=" + Show());

        // (2) Dragging outside the list cancels safely; dropping onto current position is a no-op.
        MouseDownLeft(f, 60, 121);
        MouseMoveLeft(f, 60, 44);
        MouseMoveLeft(f, 450, 44); // outside listRect (width=260)
        MouseUpLeft(f, 450, 44);
        Check(MoveCallCount() == 0,
            "T50.3 dragging outside the account list cancels safely without mutating",
            Show());

        MouseDownLeft(f, 60, 87); // AG2
        MouseMoveLeft(f, 60, 44); // drag active
        MouseMoveLeft(f, 60, 82); // back onto AG2 itself
        MouseUpLeft(f, 60, 82);
        Check(MoveCallCount() == 0,
            "T50.4 dropping a row back onto its current position is a no-op",
            Show());

        // (3) Drag over threshold reorders the exact account_id, reloads canonical order,
        // and preserves selection by account_id.
        MouseDownLeft(f, 60, 121); // AG3 (row 2)
        MouseMoveLeft(f, 60, 42);  // upper half of AG1 (row 0)
        MouseUpLeft(f, 60, 42);
        Pump(() => !Busy(f), 20000);
        string[] mv = MoveCall();
        Check(mv != null && mv.Length == 4 && mv[1] == id3 && mv[2] == "before" && mv[3] == id1,
            "T50.5 dragging AG3 above AG1 issues one canonical move by immutable account_id",
            Join(mv) + " | all=" + Show());
        Check(string.Join(",", AccountIds(f)) == id3 + "," + id1 + "," + id2,
            "T50.6 canonical reload after drag reflects the new order [AG3, AG1, AG2]",
            string.Join(",", AccountIds(f)));
        Check((string)F(f, "_selectedAccount") == id3,
            "T50.7 selection stays on the moved account_id after reload",
            (string)F(f, "_selectedAccount"));

        // (4) Fallback Move Up / Move Down controls and first/last boundary behavior.
        // Currently selected is AG3 at index 0 -> Move Up at index 0 must be a boundary no-op.
        if (File.Exists(CallLog)) File.Delete(CallLog);
        object upBtn = ButtonWithLabel(PaintAccounts(f), Loc("MoveUp"));
        object downBtn = ButtonWithLabel(PaintAccounts(f), Loc("MoveDown"));
        Check(upBtn != null && downBtn != null,
            "T50.8 Move Up and Move Down controls are present in the Manage surface",
            string.Join(",", Labels(f)));
        if (upBtn != null) Press(upBtn);
        Check(MoveCallCount() == 0 && string.Join(",", AccountIds(f)) == id3 + "," + id1 + "," + id2,
            "T50.9 Move Up at the first position is a boundary no-op and makes no engine call",
            Show());

        // Move Down on AG3 (index 0 -> index 1): [AG1, AG3, AG2]
        if (downBtn != null) Press(downBtn);
        mv = MoveCall();
        Check(mv != null && mv.Length == 3 && mv[1] == id3 && mv[2] == "down",
            "T50.10 Move Down operates on the selected immutable account_id",
            Join(mv));
        Check(string.Join(",", AccountIds(f)) == id1 + "," + id3 + "," + id2 && (string)F(f, "_selectedAccount") == id3,
            "T50.11 Move Down updates order to [AG1, AG3, AG2] and keeps AG3 selected",
            string.Join(",", AccountIds(f)) + " sel=" + F(f, "_selectedAccount"));

        // Select AG2 (now at last position, index 2) and press Move Down -> boundary no-op.
        if (File.Exists(CallLog)) File.Delete(CallLog);
        Select(f, "AG2");
        downBtn = ButtonWithLabel(PaintAccounts(f), Loc("MoveDown"));
        if (downBtn != null) Press(downBtn);
        Check(MoveCallCount() == 0,
            "T50.12 Move Down at the last position is a boundary no-op",
            Show());

        // Press Move Up on AG2 (index 2 -> index 1): [AG1, AG2, AG3]
        upBtn = ButtonWithLabel(PaintAccounts(f), Loc("MoveUp"));
        if (upBtn != null) Press(upBtn);
        mv = MoveCall();
        Check(mv != null && mv.Length == 3 && mv[1] == id2 && mv[2] == "up"
                && string.Join(",", AccountIds(f)) == id1 + "," + id2 + "," + id3
                && (string)F(f, "_selectedAccount") == id2,
            "T50.13 Move Up moves AG2 by account_id back to [AG1, AG2, AG3] and keeps AG2 selected",
            Join(mv) + " order=" + string.Join(",", AccountIds(f)));

        // (5) Keyboard navigation vs modifier reorder: bare Up/Down only change selection.
        if (File.Exists(CallLog)) File.Delete(CallLog);
        M(f, "OnKeyDown", new KeyEventArgs(Keys.Up));
        Check((string)F(f, "_selectedAccount") == id1 && MoveCallCount() == 0,
            "T50.14 bare Up arrow changes selection only and never reorders",
            "sel=" + F(f, "_selectedAccount") + " calls=" + Show());
        M(f, "OnKeyDown", new KeyEventArgs(Keys.Down));
        Check((string)F(f, "_selectedAccount") == id2 && MoveCallCount() == 0,
            "T50.15 bare Down arrow changes selection only and never reorders",
            "sel=" + F(f, "_selectedAccount") + " calls=" + Show());

        // (6) Explorer sync success vs failure handling on reorder.
        // Without INSTALL_CONSOLES_MENU.PS1 in the temp root, SyncConsolesMenu reports failure
        // while keeping the canonical reorder committed.
        Check(Note(f).Contains("EXPLORER MENU UPDATE FAILED") && (bool)F(f, "_menuSyncFailed"),
            "T50.16 when Explorer menu sync fails, the failure is reported while the canonical reorder stands",
            "note='" + Note(f) + "'");

        // Now install a working INSTALL_CONSOLES_MENU.PS1 in f's RootPath and verify sync succeeds.
        object settings = F(f, "S");
        string rootPath = (string)settings.GetType().GetField("RootPath", Any).GetValue(settings);
        string installersDir = Path.Combine(rootPath, "Installers");
        Directory.CreateDirectory(installersDir);
        string syncMarker = Path.Combine(rootPath, "menu-synced.txt");
        File.WriteAllText(Path.Combine(installersDir, "INSTALL_CONSOLES_MENU.PS1"),
            "Set-Content -LiteralPath '" + syncMarker.Replace("'", "''") + "' -Value 'synced'\r\nexit 0\r\n");

        if (File.Exists(CallLog)) File.Delete(CallLog);
        Select(f, "AG3");
        upBtn = ButtonWithLabel(PaintAccounts(f), Loc("MoveUp"));
        if (upBtn != null) Press(upBtn);
        Check(File.Exists(syncMarker) && Note(f).Contains("menu synced") && !(bool)F(f, "_menuSyncFailed"),
            "T50.17 a successful reorder triggers SyncConsolesMenu and clears _menuSyncFailed",
            "exists=" + File.Exists(syncMarker) + " note='" + Note(f) + "'");
        f.Dispose();

        // (7) Scrolled account list drag maps visible row to scrolled canonical account_id, not visible index.
        ResetLog();
        Scene("offline_status_calls=0", "extra_accounts=20"); // AG1..AG23 (> maxVisibleRows=18)
        f = MakeForm(true);
        Form = f;
        SF(f, "CurrentTab", "Accounts");
        SF(f, "_acctListScrollY", 2); // visible row 0 is AG3 (index 2), visible row 2 is AG5 (index 4)
        PaintAccounts(f);
        if (File.Exists(CallLog)) File.Delete(CallLog);

        const string id5 = "antigravity:windows-user:0000000000000005";
        MouseDownLeft(f, 60, 121); // visible row 2 -> canonical index 4 (AG5)
        MouseMoveLeft(f, 60, 42);  // upper half of visible row 0 -> canonical index 2 (AG3)
        MouseUpLeft(f, 60, 42);
        Pump(() => !Busy(f), 20000);
        mv = MoveCall();
        Check(mv != null && mv.Length == 4 && mv[1] == id5 && mv[2] == "before" && mv[3] == id3,
            "T50.18 dragging in a scrolled list reorders the scrolled canonical account_id (AG5 before AG3), not visible row 2 before 0",
            Join(mv));
        f.Dispose();
        Form = null;
    }

    // ---------------------------------------------------------------- T51
    //
    // The console/quota account and the Antigravity IDE session account are two
    // independent facts. A difference between them is NORMAL. The only thing
    // that legitimately alarms anyone is TWO CANONICAL console/quota identities
    // proving the same provider fingerprint -- that is a shared quota pool.
    //
    // Each case below is one the old conflation got wrong, or could get wrong.
    static void T51CanonicalVsRuntimeIdentityIsolation()
    {
        T51Case1DifferentPlanesAreNormal();
        T51Case2CanonicalDuplicate();
        T51Case3RuntimeOnlyCanonicalUnproven();
        T51Case4ExplicitBind();
        T51Case5RuntimeMatchesAnotherSlot();
        T51Case6DiagnosticsAndLocalization();
        T51Case7SignInBindFailure();
        T51Case8SwitchBindFailure();
        T51Case9CanonicalClearClearsDuplicateWarning();
        Form = null;
    }

    // CASE 1 -- a console/IDE difference is a fact, not a fault.
    //
    // AG1 is canonically Alice; the Antigravity context running under AG1 is
    // signed into Carol (AG3's console account). Nothing about that is wrong,
    // and nothing about it makes AG1 share a quota pool with AG3.
    static void T51Case1DifferentPlanesAreNormal()
    {
        ResetLog();
        Scene(
            "offline_status_calls=0",
            "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
            "configured_fp_AG2=" + OtherFp, "configured_label_AG2=" + Bob,
            "configured_fp_AG3=" + CarolFp, "configured_label_AG3=" + Carol,
            "identity_fp_AG1=" + CarolFp, "identity_label_AG1=" + Carol,
            "identity_fp_AG2=" + OtherFp, "identity_label_AG2=" + Bob,
            "identity_fp_AG3=" + CarolFp, "identity_label_AG3=" + Carol
        );
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        PollLabel(f, "AG1");
        PollLabel(f, "AG2");
        PollLabel(f, "AG3");

        object ag1 = Row(f, "AG1");
        Check(ConsoleFpOf(ag1) == SharedFp && ConsoleLabelOf(ag1) == Alice,
            "T51.1a AG1's console/quota account stays Alice -- a runtime IDE session never becomes canonical",
            "console=" + ConsoleFpOf(ag1) + "/" + ConsoleLabelOf(ag1) + " table=" + Table(f));
        Check(IdeFpOf(ag1) == CarolFp && IdeLabelOf(ag1) == Carol,
            "T51.1b AG1's IDE account is Carol, recorded as an independent observation",
            "ide=" + IdeFpOf(ag1) + "/" + IdeLabelOf(ag1));
        Check((string)GetOf(ag1, "IdentityFp") == SharedFp && (string)GetOf(ag1, "IdentityLabel") == Alice,
            "T51.1c the compatibility Identity* field carries ONLY the canonical console identity",
            "IdentityFp=" + GetOf(ag1, "IdentityFp"));
        Check(LabelOf(f, "AG2") == Bob && LabelOf(f, "AG3") == Carol,
            "T51.1d AG2 and AG3 keep their own canonical accounts untouched", Table(f));
        Check(SharedOf(f, "AG1").Length == 0 && SharedOf(f, "AG3").Length == 0,
            "T51.1e an IDE account equal to another slot's console account produces NO quota-shared warning",
            "AG1='" + SharedOf(f, "AG1") + "' AG3='" + SharedOf(f, "AG3") + "' table=" + Table(f));

        string p1 = PanelText(f, "AG1");
        Check(p1.Contains(Alice) && p1.Contains(Carol),
            "T51.1f AG1's panel states the console account and the IDE account as two separate facts", p1);
        Check(!p1.Contains(Loc("IdeSessionMismatch")) && !p1.Contains(Loc("IdeSessionMatches"))
                && !p1.Contains("IdentityMismatch") && !p1.Contains("differs"),
            "T51.1g and never as a mismatch, error or account-integrity problem", p1);
        Check(!p1.Contains(SharedText("AG3")),
            "T51.1h and never as a quota warning", p1);

        // A full registry reload is not a licence to adopt the runtime value.
        M(f, "LoadAccounts");
        Check(ConsoleFpOf(Row(f, "AG1")) == SharedFp && IdeFpOf(Row(f, "AG1")) == CarolFp,
            "T51.1i Refresh preserves both planes exactly as they were",
            "console=" + ConsoleFpOf(Row(f, "AG1")) + " ide=" + IdeFpOf(Row(f, "AG1")));
        Check(SharedOf(f, "AG1").Length == 0,
            "T51.1j and Refresh does not invent the quota warning either", SharedOf(f, "AG1"));

        Check(!AnyMutation(),
            "T51.1k passive polling and Refresh issue zero bind-identity / rename / move / auth / logout mutations",
            Show());
        Check(string.Join(",", AccountIds(f)) == id1 + "," + id2 + "," + id3,
            "T51.1l account_id sequence and persisted order are unchanged",
            string.Join(",", AccountIds(f)));

        f.Dispose();
    }

    // CASE 2 -- the canonical duplicate, which IS a problem: two slots whose
    // CONSOLE accounts are one provider identity share one quota pool. The
    // runtime IDE accounts are irrelevant to that, so the answer has to be the
    // same whatever they are.
    static void T51Case2CanonicalDuplicate()
    {
        ResetLog();
        Scene(
            "offline_status_calls=0",
            "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
            "configured_fp_AG3=" + SharedFp, "configured_label_AG3=" + Alice,
            // deliberately different IDE sessions
            "identity_fp_AG1=" + BobFp, "identity_label_AG1=" + Bob,
            "identity_fp_AG3=" + CarolFp, "identity_label_AG3=" + Carol
        );
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        PollLabel(f, "AG1");
        PollLabel(f, "AG3");

        string s1 = SharedOf(f, "AG1");
        string s3 = SharedOf(f, "AG3");
        Check(s1.Length > 0 && s1.Contains("AG3"),
            "T51.2a AG1 is marked as sharing its canonical quota account with the other compact slot",
            "AG1='" + s1 + "' table=" + Table(f));
        Check(s3.Length > 0 && s3.Contains("AG1"),
            "T51.2b and the warning lands on AG3 too", "AG3='" + s3 + "'");
        string p1 = PanelText(f, "AG1");
        Check(p1.Contains(SharedText("AG3")),
            "T51.2c the panel names the other slot through the quota-account sentence", p1);
        Check(SharedText("AG3").Contains("AG3") && SharedText("AG3").Contains("{0}") == false,
            "T51.2d the sentence is the quota template with the other slot substituted",
            SharedText("AG3"));

        // Same canonical pair, now with EQUAL runtime IDE accounts. The answer
        // may not move: it is derived from the canonical plane alone.
        Scene(
            "offline_status_calls=0",
            "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
            "configured_fp_AG3=" + SharedFp, "configured_label_AG3=" + Alice,
            "identity_fp=" + SharedFp, "identity_label=" + Alice
        );
        PollLabel(f, "AG1");
        PollLabel(f, "AG3");
        Check(SharedOf(f, "AG1") == s1 && SharedOf(f, "AG3") == s3,
            "T51.2e the result does not depend on RuntimeIdentityFp",
            "before AG1='" + s1 + "' AG3='" + s3 + "' after AG1='" + SharedOf(f, "AG1")
            + "' AG3='" + SharedOf(f, "AG3") + "'");

        // And when the canonical accounts are genuinely different, nothing fires
        // even though the IDE sessions now agree.
        Scene(
            "offline_status_calls=0",
            "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
            "configured_fp_AG3=" + CarolFp, "configured_label_AG3=" + Carol,
            "identity_fp=" + SharedFp, "identity_label=" + Alice
        );
        PollLabel(f, "AG1");
        PollLabel(f, "AG3");
        Check(SharedOf(f, "AG1").Length == 0 && SharedOf(f, "AG3").Length == 0,
            "T51.2f two different canonical accounts raise no warning even when the IDE sessions are one account",
            "AG1='" + SharedOf(f, "AG1") + "' AG3='" + SharedOf(f, "AG3") + "'");
        f.Dispose();
    }

    // CASE 3 -- runtime only, canonical unproven. This is the case the old code
    // got wrong: it promoted the observation into the canonical binding.
    static void T51Case3RuntimeOnlyCanonicalUnproven()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + BobFp, "identity_label=" + Bob);
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        PollLabel(f, "AG2");

        object ag2 = Row(f, "AG2");
        Check(IdeFpOf(ag2) == BobFp && IdeLabelOf(ag2) == Bob,
            "T51.3a the IDE account is observed as Bob",
            "ide=" + IdeFpOf(ag2) + "/" + IdeLabelOf(ag2));
        Check(ConsoleFpOf(ag2).Length == 0 && ConsoleLabelOf(ag2).Length == 0,
            "T51.3b a passive runtime observation NEVER populates the canonical console identity",
            "console=" + ConsoleFpOf(ag2) + "/" + ConsoleLabelOf(ag2));
        Check(((string)GetOf(ag2, "IdentityFp")).Length == 0,
            "T51.3c and the compatibility canonical field stays empty with it",
            "IdentityFp=" + GetOf(ag2, "IdentityFp"));
        Check(SharedOf(f, "AG2").Length == 0,
            "T51.3d an unproven canonical account shares quota with nobody", SharedOf(f, "AG2"));

        // A full registry reload must not resurrect it from the cached runtime value.
        M(f, "LoadAccounts");
        ag2 = Row(f, "AG2");
        Check(ConsoleFpOf(ag2).Length == 0 && ((string)GetOf(ag2, "IdentityFp")).Length == 0,
            "T51.3e Refresh does not promote the cached runtime identity into the canonical one",
            "console=" + ConsoleFpOf(ag2) + " IdentityFp=" + GetOf(ag2, "IdentityFp"));
        Check(IdeFpOf(ag2) == BobFp,
            "T51.3f while the IDE observation itself is still there", IdeFpOf(ag2));
        Check(!AnyMutation(),
            "T51.3g no persistent mutation is issued for a slot with an unproven canonical account", Show());
        f.Dispose();

        // "Restart": a brand new form reading the same engine registry.
        Saituls.SaitulsForm g = MakeForm(true);
        Check(ConsoleFpOf(Row(g, "AG2")).Length == 0,
            "T51.3h and a restart still finds the canonical account unproven",
            "console=" + ConsoleFpOf(Row(g, "AG2")));
        g.Dispose();
    }

    // CASE 4 -- the explicit workflow is still allowed to bind. Only passive
    // observation is barred; an operator-driven switch must keep working.
    static void T51Case4ExplicitBind()
    {
        ResetLog();
        Scene("offline_status_calls=0", "identity_fp=" + BobFp, "identity_label=" + Bob);
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        PollLabel(f, "AG2");
        Check(ConsoleFpOf(Row(f, "AG2")).Length == 0,
            "T51.4a the canonical account starts unproven", ConsoleFpOf(Row(f, "AG2")));

        // The real sign-in/switch path, driven the way T37 drives it.
        M(f, "PollForIdentityChange", (string)GetOf(Row(f, "AG2"), "Id"), "AG2",
          "", "", DateTime.UtcNow.AddSeconds(60));
        Pump(() => ConsoleFpOf(Row(f, "AG2")).Length > 0, 60000);
        Check(HasCallFor("bind-identity", "AG2", f),
            "T51.4b completing an explicit sign-in issues bind-identity", Show());
        Check(ConsoleFpOf(Row(f, "AG2")) == BobFp,
            "T51.4c the canonical console account becomes Bob only after that explicit bind",
            "console=" + ConsoleFpOf(Row(f, "AG2")));
        Check(SharedOf(f, "AG2").Length == 0,
            "T51.4d and a bound account that is unique still shares quota with nobody", SharedOf(f, "AG2"));
        f.Dispose();

        Saituls.SaitulsForm g = MakeForm(true);
        Check(ConsoleFpOf(Row(g, "AG2")) == BobFp,
            "T51.4e restart/reload preserves the canonical binding",
            "console=" + ConsoleFpOf(Row(g, "AG2")));
        g.Dispose();
    }

    // CASE 5 -- a runtime IDE account equal to another slot's console account is
    // an observation about a login, not a statement about a quota pool.
    static void T51Case5RuntimeMatchesAnotherSlot()
    {
        ResetLog();
        Scene(
            "offline_status_calls=0",
            "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
            "configured_fp_AG3=" + CarolFp, "configured_label_AG3=" + Carol,
            "identity_fp=" + CarolFp, "identity_label=" + Carol
        );
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        PollLabel(f, "AG1");
        PollLabel(f, "AG3");

        Check(ConsoleFpOf(Row(f, "AG1")) == SharedFp && ConsoleFpOf(Row(f, "AG3")) == CarolFp,
            "T51.5a the two canonical quota accounts stay distinct",
            "AG1=" + ConsoleFpOf(Row(f, "AG1")) + " AG3=" + ConsoleFpOf(Row(f, "AG3")));
        Check(SharedOf(f, "AG1").Length == 0 && SharedOf(f, "AG3").Length == 0,
            "T51.5b AG1 is still not sharing a quota pool with AG3",
            "AG1='" + SharedOf(f, "AG1") + "' AG3='" + SharedOf(f, "AG3") + "'");
        string p1 = PanelText(f, "AG1");
        Check(!p1.Contains(SharedText("AG3")),
            "T51.5c runtime equality with another slot never becomes a quota/account warning", p1);
        Check(p1.Contains(Carol),
            "T51.5d AG1 may neutrally display the IDE account it is running under", p1);
        f.Dispose();
    }

    // CASE 6 -- the words. An ordinary difference may not be spelled as a fault,
    // and the duplicate warning must say which plane it is about.
    static void T51Case6DiagnosticsAndLocalization()
    {
        Check(Loc("IdeSessionMismatch") == "IdeSessionMismatch"
                && Loc("IdeSessionMatches") == "IdeSessionMatches",
            "T51.6a no localisation entry still describes a plane difference as a mismatch",
            Loc("IdeSessionMismatch") + " / " + Loc("IdeSessionMatches"));
        Check(Loc("IdeSession") == "IdeSession",
            "T51.6b the old ambiguous 'IDE session' label is gone too", Loc("IdeSession"));
        Check(Loc("ConsoleAccount") != "ConsoleAccount" && Loc("IdeAccount") != "IdeAccount",
            "T51.6c both planes have an operator-facing label",
            Loc("ConsoleAccount") + " / " + Loc("IdeAccount"));

        string quota = SharedText("AG3");
        Check(quota != "QuotaShared" && quota.Contains("AG3"),
            "T51.6d the duplicate warning renders as a sentence naming the other slot", quota);
        Check(quota.Contains("Quota") || quota.Contains("quota")
                || quota.Contains("Квот") || quota.Contains("квот"),
            "T51.6e and names the QUOTA plane rather than 'same Google account'", quota);
        Check(quota.Contains("Google account") == false,
            "T51.6f the ambiguous 'same Google account' wording is not used", quota);

        // The copied diagnostics carry the two planes as independent fields and
        // no mismatch verdict.
        ResetLog();
        Scene(
            "offline_status_calls=0",
            "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
            "identity_fp_AG1=" + CarolFp, "identity_label_AG1=" + Carol
        );
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        Select(f, "AG1");
        PollLabel(f, "AG1");
        string diag = CopyDiagnostics(f);
        Check(diag.Contains("ConsoleIdentity: " + Alice) || diag.Contains("ConsoleIdentity: " + SharedFp),
            "T51.6g Copy Diagnostics carries the canonical console identity", diag.Replace("\n", " | "));
        Check(diag.Contains("IdeIdentity: " + Carol) || diag.Contains("IdeIdentity: " + CarolFp),
            "T51.6h and the IDE identity as its own field", diag.Replace("\n", " | "));
        Check(diag.Contains("IdentityMismatch") == false,
            "T51.6i with no mismatch verdict in it", diag.Replace("\n", " | "));
        f.Dispose();
    }

    // CASE 7 -- sign-in succeeds at vendor OAuth but bind-identity fails.
    // The panel must NOT claim "signed in and ready", must report BIND_FAILED,
    // and must not mark the canonical console account bound.
    static void T51Case7SignInBindFailure()
    {
        ResetLog();
        Scene("offline_status_calls=1", "auth_required_until=1", "bind_identity_fails=1");
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        Select(f, "AG2");
        M(f, "SignInSelectedAccount");
        Pump(() => !Busy(f) && Note(f).Length > 0 && Note(f).Contains("BIND_FAILED"), 40000);
        Check(Note(f).Contains("BIND_FAILED"),
            "T51.7a sign-in with failing bind reports BIND_FAILED", Note(f));
        Check(!Note(f).Contains("signed in and ready"),
            "T51.7b and does not claim to be signed in and ready", Note(f));
        Check(ConsoleFpOf(Row(f, "AG2")).Length == 0,
            "T51.7c and canonical console identity remains unproven", ConsoleFpOf(Row(f, "AG2")));
        f.Dispose();
    }

    // CASE 8 -- switch completes OAuth but bind-identity fails.
    // Must NOT report SwitchedTo, must report BIND_FAILED, and canonical
    // console identity is not updated to the new identity.
    static void T51Case8SwitchBindFailure()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp_AG2=" + SharedFp, "configured_label_AG2=" + Alice,
              "identity_fp_AG2=" + OtherFp, "identity_label_AG2=" + Bob,
              "bind_identity_fails=1");
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        Select(f, "AG2");
        M(f, "PollForIdentityChange", (string)GetOf(Row(f, "AG2"), "Id"), "AG2",
          SharedFp, Alice, DateTime.UtcNow.AddSeconds(9));
        Pump(() => Note(f).Contains("BIND_FAILED"), 30000);
        Check(Note(f).Contains("BIND_FAILED"),
            "T51.8a switch with failing bind reports BIND_FAILED", Note(f));
        Check(!Note(f).Contains(SwitchText("SwitchedTo", Bob)),
            "T51.8b switch with failing bind does not claim SwitchedTo", Note(f));
        Check(ConsoleFpOf(Row(f, "AG2")) != OtherFp,
            "T51.8c canonical console identity does not adopt the unbound identity", ConsoleFpOf(Row(f, "AG2")));
        f.Dispose();
    }

    // CASE 9 -- canonical clear reloads authoritative engine state and clears duplicate warnings.
    static void T51Case9CanonicalClearClearsDuplicateWarning()
    {
        ResetLog();
        Scene("offline_status_calls=0",
              "configured_fp_AG1=" + SharedFp, "configured_label_AG1=" + Alice,
              "configured_fp_AG3=" + SharedFp, "configured_label_AG3=" + Alice,
              "identity_fp_AG1=" + SharedFp, "identity_label_AG1=" + Alice,
              "identity_fp_AG3=" + SharedFp, "identity_label_AG3=" + Alice);
        Saituls.SaitulsForm f = MakeForm(true);
        Form = f;
        PollLabel(f, "AG1");
        PollLabel(f, "AG3");
        Check(SharedOf(f, "AG1").Contains("AG3") && SharedOf(f, "AG3").Contains("AG1"),
            "T51.9a initial duplicate quota warning is present on both AG1 and AG3",
            "AG1='" + SharedOf(f, "AG1") + "' AG3='" + SharedOf(f, "AG3") + "'");

        Process.Start(new ProcessStartInfo
        {
            FileName = Engine,
            Arguments = "bind-identity AG3 --clear",
            UseShellExecute = false,
            CreateNoWindow = true
        }).WaitForExit();

        M(f, "LoadAccounts");
        Check(SharedOf(f, "AG1").Length == 0,
            "T51.9b after canonical clear of AG3, AG1 duplicate warning is cleared",
            "AG1='" + SharedOf(f, "AG1") + "'");
        Check(SharedOf(f, "AG3").Length == 0,
            "T51.9c after canonical clear of AG3, AG3 duplicate warning is cleared",
            "AG3='" + SharedOf(f, "AG3") + "'");
        Check(ConsoleFpOf(Row(f, "AG3")).Length == 0,
            "T51.9d AG3 canonical identity is now empty", ConsoleFpOf(Row(f, "AG3")));
        f.Dispose();
    }

    // Paint, find the 116x18 Copy Diagnostics button and press it. That box is
    // the only one in the file with that geometry.
    static string CopyDiagnostics(Saituls.SaitulsForm f)
    {
        SF(f, "_acctAdvancedExpanded", true);
        var bmp = new Bitmap(900, 700);
        try
        {
            ((IList)F(f, "Buttons")).Clear();
            using (var g = Graphics.FromImage(bmp))
                M(f, "DrawAccountsTab", g, 0, 0, 900, 700);
        }
        finally { bmp.Dispose(); }

        foreach (object b in (IList)F(f, "Buttons"))
        {
            var r = (Rectangle)b.GetType().GetField("R", Any).GetValue(b);
            if (r.Width != 116 || r.Height != 18) continue;
            Clipboard.Clear();
            ((Action)b.GetType().GetField("A", Any).GetValue(b))();
            return Clipboard.ContainsText() ? Clipboard.GetText() : "";
        }
        return "";
    }

    static void KillStragglers()
    {
        foreach (Process p in Process.GetProcessesByName("fake-sai-accounts"))
            try { p.Kill(); } catch { }
    }
}
