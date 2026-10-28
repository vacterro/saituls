// A stand-in for sai-accounts.exe, used by tests/saituls_accounts_flow_test.cs.
//
// It speaks the real engine's one-line JSON envelope and is driven by a
// scenario file so a test can say exactly what the control plane will do:
// stay offline until N status probes have happened, hang forever, exit
// nonzero, write to stderr, and so on. Every invocation is appended to a call
// log, which is how the tests assert the ORDER and the IDENTITY of the calls
// -- the two things the "never fall back to another account" and "never fall
// back to another workdir" rules are actually made of.
//
// Scenario file (FAKE_ENGINE_SCENARIO), one key=value per line:
//   offline_status_calls=<n>  status returns OFFLINE for the first n probes
//   initial_auth=<word>       auth_state while offline (default UNKNOWN)
//   online_auth=<word>        auth_state once online (default AUTHENTICATED)
//   hang=<verb>               that verb writes a lot to stderr and never exits
//   fail=<verb>:<code>        that verb exits with that code and no stdout
//   fail_envelope=<verb>      that verb exits 1 but still prints a JSON envelope
//   stderr=<verb>             that verb writes to stderr and succeeds
//   launch_access_denied=<n>   the first n launches answer WORKDIR_UNAVAILABLE
//                              with reason ACCESS_DENIED (a valid folder this
//                              managed SID cannot read), then they succeed
//   launch_not_found=<n>       the first n launches answer reason NOT_FOUND
//   auth_required_until=<n>    auth_state stays AUTH_REQUIRED until n auth
//                              operations have been issued, so the GUI's
//                              bounded post-auth poll actually observes READY
//   grant_status=<word>        grant-workdir answers that status (default
//                              granted); anything else exercises the refusal path
//   auth_status=<word>         auth answers that status (default launched) and
//                              exits nonzero for anything else, exactly like the
//                              real engine's launch verdict
//   identity_fp=<hex>          every status reply carries
//                              credential.provider_identity.fingerprint = <hex>
//   identity_fp_<SEL>=<hex>    overrides it for one selector (AG1/AG2/AG3)
//   identity_label=<text>      every status reply carries
//                              provider_identity.account_label = <text>
//   identity_label_<SEL>=<text>  the same, for one selector
//   identity_fp_after=<hex>    once an auth operation has been issued, this
//                              becomes the fingerprint: a completed switch
//   identity_fp_after_<SEL>=<hex>  the same, for one selector
//   identity_label_after=<text>  the label that arrives with it
//   saipen_verdict=<word>      saipen-readiness answers that verdict (T-288);
//                              absent = the verb is unknown, like an engine that
//                              predates it
//   logout_sticks=1            logout reports success but the credential stays,
//                              so the GUI has to notice before opening OAuth
//   no identity_* key at all   the reply carries NO credential object: an
//                              UNPROVEN identity, which the GUI must render as
//                              unknown rather than as a shared account
//
// The default is no credential object, which is what every pre-identity
// scenario describes. That matters: a fixture that always emitted a
// fingerprint would let "the GUI reads the fingerprint" look true in tests
// that never meant to exercise it.
using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;

class FakeAccountsEngine
{
    static Dictionary<string, string> Scene = new Dictionary<string, string>();
    static readonly object Gate = new object();
    static int _statusCalls;

    /// Cross-process lock over the state file.
    ///
    /// Gate is per-process, and the GUI spawns a fresh fake for every call, so
    /// every counter bump and every event note is a read-modify-write against a
    /// file several of those processes hold at once. One of them wins and the
    /// other's write is gone: "last=logout" vanishes under a concurrent
    /// "count:status", the fake goes on reporting a live credential, and the
    /// product prints a sign-out failure for a logout that did happen. The name
    /// carries the file name so two fixtures in different roots do not queue
    /// behind each other; the atomically-swapped write below covers a reader,
    /// this covers a writer.
    sealed class StateLock : IDisposable
    {
        readonly Mutex _m;
        public StateLock(string path)
        {
            _m = new Mutex(false, @"Local\saiFakeState." + Path.GetFileName(path));
            try { _m.WaitOne(5000); } catch (AbandonedMutexException) { }
        }
        public void Dispose()
        {
            try { _m.ReleaseMutex(); } catch { }
            _m.Dispose();
        }
    }

    const string Secondary = "antigravity_b";

    static int Main(string[] argv)
    {
        try { return Run(argv); }
        catch (Exception ex)
        {
            // A crashed fixture must look like a crashed engine, not like a
            // refusal the product is supposed to explain. Silently returning 0
            // here would make a broken fixture look like a working one.
            try { Console.Error.WriteLine("fake engine crashed: " + ex.Message); } catch { }
            return 2;
        }
    }

    static int Run(string[] argv)
    {
        Load(Environment.GetEnvironmentVariable("FAKE_ENGINE_SCENARIO"), Scene);
        SyncStateGeneration();
        string log = Environment.GetEnvironmentVariable("FAKE_ENGINE_LOG");
        if (!string.IsNullOrEmpty(log))
        {
            // Tab-separated: the point of the log is to prove the ENGINE
            // received exactly these arguments, so a workdir with a space
            // in it must still be one argument.
            //
            // Retried, and never fatal. Several of these processes run at once --
            // a poll sweep plus a switch's own polls -- and AppendAllText takes
            // an exclusive handle, so the loser of that race used to die with a
            // sharing violation and hand the GUI a nonzero exit that reads, in
            // the product, as "logout failed or was refused". A fixture must not
            // manufacture product failures.
            for (int attempt = 0; attempt < 20; attempt++)
            {
                try { File.AppendAllText(log, string.Join("\t", argv) + Environment.NewLine); break; }
                catch (IOException) { System.Threading.Thread.Sleep(10); }
                catch (UnauthorizedAccessException) { System.Threading.Thread.Sleep(10); }
            }
        }
        string verb = argv.Length > 0 ? argv[0].ToLowerInvariant() : "";

        // A hang is the whole point of the deadlock tests: flood stderr past
        // the pipe buffer, then block. A parent that reads stdout to the end
        // before waiting will never come back from this.
        if (Scene.ContainsKey("hang") && Scene["hang"] == verb)
        {
            string filler = new string('x', 4096);
            for (int i = 0; i < 64; i++)
            {
                Console.Error.Write(filler);
                Console.Error.Flush();
            }
            Thread.Sleep(Timeout.Infinite);
            return 0;
        }
        if (Scene.ContainsKey("fail") && Scene["fail"].StartsWith(verb + ":"))
        {
            Console.Error.WriteLine("fake engine refused " + verb);
            int code;
            return int.TryParse(Scene["fail"].Substring(verb.Length + 1), out code) ? code : 1;
        }
        // A nonzero exit that still carries a real envelope. This is the case the
        // operator actually hit: the CLI fails, the JSON is on stdout, and a GUI
        // that only renders stdout on success shows the operator nothing useful.
        if (Scene.ContainsKey("fail_envelope") && Scene["fail_envelope"] == verb)
        {
            var bad = Env("sai.accounts/context/1");
            Put(bad, "account_id", "AG2");
            Put(bad, "status", verb + "_failed");
            Put(bad, "error", "broker_not_listening_within_5000ms");
            Console.WriteLine(Json.Serialize(bad));
            return 1;
        }
        if (Scene.ContainsKey("stderr") && Scene["stderr"] == verb)
            Console.Error.WriteLine("fake engine warning on " + verb);

        if (verb == "list") return List();
        if (verb == "capabilities") return Capabilities();
        if (verb == "status") return Status(argv);
        if (verb == "context") return Context(argv);
        if (verb == "launch") return Launch(argv);
        if (verb == "auth") return Auth(argv);
        if (verb == "logout") return Logout(argv);
        if (verb == "grant-workdir") return GrantWorkdir(argv);
        if (verb == "set-hidden") return SetHidden(argv);
        if (verb == "add") return Add(argv);
        if (verb == "move") return Move(argv);
        if (verb == "bind-identity") return BindIdentity(argv);
        if (verb == "saipen-readiness" && Scene.ContainsKey("saipen_verdict")) return SaipenReadiness(argv);
        Console.Error.WriteLine("fake engine: unknown verb " + verb);
        return 2;
    }

    static void Load(string path, Dictionary<string, string> into)
    {
        if (string.IsNullOrEmpty(path)) return;
        // Retried for the same reason as the log append: the harness rewrites the
        // scenario between tests while a poll from the previous one may still be
        // reading it. A reader that loses that race used to die, and a dead
        // fixture reads as a broken product.
        string[] lines = null;
        for (int attempt = 0; attempt < 20 && lines == null; attempt++)
        {
            try { if (File.Exists(path)) lines = File.ReadAllLines(path); }
            catch (IOException) { System.Threading.Thread.Sleep(10); }
            catch (UnauthorizedAccessException) { System.Threading.Thread.Sleep(10); }
        }
        if (lines == null) return;
        foreach (string raw in lines)
        {
            string line = raw.Trim();
            if (line.Length == 0 || line.StartsWith("#")) continue;
            int eq = line.IndexOf('=');
            if (eq <= 0) continue;
            into[line.Substring(0, eq).Trim()] = line.Substring(eq + 1).Trim();
        }
    }

    // The engine is one process per call, so "offline for the first N probes"
    // has to survive across invocations. The counter lives in a file for exactly
    // that reason: a static field would reset every time and the context would
    // never come online. The same counter, per channel, is what makes "the
    // first N launches are denied" and "auth stays required for N calls"
    // expressible at all.
    static int StatusCalls() { return Count("status"); }

    static int Count(string channel)
    {
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (string.IsNullOrEmpty(path)) { lock (Gate) { return ++_statusCalls; } }
        string key = "count:" + channel;
        using (new StateLock(path))
        lock (Gate)
        {
            var st = ReadState(path);
            int n;
            if (!int.TryParse(StateOf(st, key), out n)) n = 0;
            st[key] = (++n).ToString();
            WriteState(path, st);
            return n;
        }
    }

    // Which of the two happened LAST. Not a comparison of the two counts: a
    // completed switch is one logout and one auth, so the counts come out equal
    // and no arithmetic on them can tell "signed out, waiting" from "signed in
    // again". The order of events can, and that is exactly what decides whether
    // a credential exists.
    static void NoteEvent(string channel)
    {
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (string.IsNullOrEmpty(path)) return;
        using (new StateLock(path))
        lock (Gate)
        {
            var st = ReadState(path);
            st["last"] = channel;
            WriteState(path, st);
        }
    }

    static void SyncStateGeneration()
    {
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (string.IsNullOrEmpty(path)) return;
        string gen;
        if (!Scene.TryGetValue("generation", out gen) || string.IsNullOrEmpty(gen)) return;
        using (new StateLock(path))
        lock (Gate)
        {
            var st = ReadState(path);
            if (StateOf(st, "generation") != gen)
            {
                st.Clear();
                st["generation"] = gen;
                WriteState(path, st);
            }
        }
    }

    static Dictionary<string, string> ReadState(string path)
    {
        var st = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        string[] lines = null;
        for (int attempt = 0; attempt < 20 && lines == null; attempt++)
        {
            try { if (File.Exists(path)) lines = File.ReadAllLines(path); }
            catch (IOException) { Thread.Sleep(10); }
            catch (UnauthorizedAccessException) { Thread.Sleep(10); }
        }
        if (lines == null) return st;
        foreach (string raw in lines)
        {
            int eq = raw.IndexOf('=');
            if (eq <= 0) continue;
            st[raw.Substring(0, eq).Trim()] = raw.Substring(eq + 1).Trim();
        }
        return st;
    }

    static string StateOf(Dictionary<string, string> st, string key)
    {
        string v;
        return st.TryGetValue(key, out v) ? v : "";
    }

    static void WriteState(string path, Dictionary<string, string> st)
    {
        var lines = new List<string>();
        foreach (var kv in st) lines.Add(kv.Key + "=" + kv.Value);
        // Written through a temporary file and swapped in atomically.
        // WriteAllLines truncates first, so a reader in another process -- and
        // there are always several, one per poll -- can catch the file empty and
        // conclude the account never signed out. That read as "sign-out did not
        // take effect", in the product, for a fixture that had simply been
        // caught mid-write.
        string tmp = path + "." + System.Diagnostics.Process.GetCurrentProcess().Id + ".tmp";
        for (int attempt = 0; attempt < 20; attempt++)
        {
            try
            {
                File.WriteAllLines(tmp, lines.ToArray());
                if (File.Exists(path)) File.Replace(tmp, path, null);
                else File.Move(tmp, path);
                return;
            }
            catch (IOException) { Thread.Sleep(10); }
            catch (UnauthorizedAccessException) { Thread.Sleep(10); }
        }
        try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
    }

    static string ArgOf(string[] argv, string flag)
    {
        for (int i = 0; i < argv.Length - 1; i++)
            if (argv[i] == flag) return argv[i + 1];
        return null;
    }

    // Envelopes are built as dictionaries and serialized, exactly the way the
    // real engine does it. Hand-concatenated JSON is how this fixture shipped a
    // bug once already: a Windows path is full of backslashes and "\V" is not
    // a legal JSON escape, so SAITULS' serializer threw and every answer that
    // carried a workdir was silently lost.
    static readonly System.Web.Script.Serialization.JavaScriptSerializer Json =
        new System.Web.Script.Serialization.JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 };

    static Dictionary<string, object> Env(string schema)
    {
        return new Dictionary<string, object> { { "schema", schema } };
    }
    static void Put(Dictionary<string, object> d, string k, object v) { d[k] = v; }
    static int Emit(Dictionary<string, object> d)
    {
        return Emit(d, 0);
    }
    static int Emit(Dictionary<string, object> d, int exitCode)
    {
        Console.WriteLine(Json.Serialize(d));
        return exitCode;
    }

    static List<Dictionary<string, object>> CurrentAccounts()
    {
        string me = Environment.UserName;
        var baseList = new List<Dictionary<string, object>>
        {
            Account("antigravity:windows-user:0000000000000001", me, "AG1", "ENABLED"),
            Account("antigravity:windows-user:0000000000000002", Secondary, "AG2", "ENABLED"),
            Account("antigravity:windows-user:0000000000000003", Secondary, "AG3", "FROZEN")
        };
        int extra = 0;
        if (Scene.ContainsKey("extra_accounts")) int.TryParse(Scene["extra_accounts"], out extra);
        for (int i = 1; i <= extra; i++)
        {
            int idx = 3 + i;
            baseList.Add(Account(
                string.Format("antigravity:windows-user:{0:x16}", idx),
                Secondary,
                "AG" + idx,
                "ENABLED"));
        }
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (!string.IsNullOrEmpty(path))
        {
            using (new StateLock(path))
            lock (Gate)
            {
                string rawOrder = StateOf(ReadState(path), "order");
                if (!string.IsNullOrEmpty(rawOrder))
                {
                    var byId = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
                    foreach (var a in baseList) byId[(string)a["account_id"]] = a;
                    var ordered = new List<Dictionary<string, object>>();
                    var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    foreach (string id in rawOrder.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
                    {
                        Dictionary<string, object> hit;
                        if (byId.TryGetValue(id, out hit) && seen.Add(id)) ordered.Add(hit);
                    }
                    foreach (var a in baseList)
                    {
                        string id = (string)a["account_id"];
                        if (seen.Add(id)) ordered.Add(a);
                    }
                    return ordered;
                }
            }
        }
        return baseList;
    }

    static int List()
    {
        var accounts = new List<object>();
        foreach (var a in CurrentAccounts()) accounts.Add(a);
        var d = Env("sai.accounts/list/1");
        Put(d, "accounts", accounts);
        return Emit(d);
    }

    static int Move(string[] argv)
    {
        if (argv.Length < 3)
        {
            var bad = Env("sai.accounts/error/1");
            Put(bad, "status", "move_args");
            Console.WriteLine(Json.Serialize(bad));
            return 1;
        }
        string sel = argv[1];
        string dir = argv[2].ToLowerInvariant();
        var list = CurrentAccounts();
        int fromIdx = list.FindIndex(a =>
            string.Equals((string)a["account_id"], sel, StringComparison.OrdinalIgnoreCase) ||
            string.Equals((string)a["compact_label"], sel, StringComparison.OrdinalIgnoreCase));
        if (fromIdx < 0)
        {
            var nf = Env("sai.accounts/error/1");
            Put(nf, "status", "ACCOUNT_NOT_FOUND");
            Put(nf, "error", sel);
            Console.WriteLine(Json.Serialize(nf));
            return 1;
        }
        var item = list[fromIdx];
        string itemId = (string)item["account_id"];
        int toIdx = fromIdx;
        if (dir == "up")
        {
            if (fromIdx > 0) toIdx = fromIdx - 1;
        }
        else if (dir == "down")
        {
            if (fromIdx < list.Count - 1) toIdx = fromIdx + 1;
        }
        else if (dir == "before" || dir == "after")
        {
            if (argv.Length < 4)
            {
                var bad = Env("sai.accounts/error/1");
                Put(bad, "status", "move_args");
                Console.WriteLine(Json.Serialize(bad));
                return 1;
            }
            string targetSel = argv[3];
            int tIdx = list.FindIndex(a =>
                string.Equals((string)a["account_id"], targetSel, StringComparison.OrdinalIgnoreCase) ||
                string.Equals((string)a["compact_label"], targetSel, StringComparison.OrdinalIgnoreCase));
            if (tIdx < 0)
            {
                var nf = Env("sai.accounts/error/1");
                Put(nf, "status", "ACCOUNT_NOT_FOUND");
                Put(nf, "error", targetSel);
                Console.WriteLine(Json.Serialize(nf));
                return 1;
            }
            string targetId = (string)list[tIdx]["account_id"];
            if (!string.Equals(itemId, targetId, StringComparison.OrdinalIgnoreCase))
            {
                var copy = new List<Dictionary<string, object>>(list);
                copy.RemoveAt(fromIdx);
                int adj = copy.FindIndex(a => string.Equals((string)a["account_id"], targetId, StringComparison.OrdinalIgnoreCase));
                toIdx = dir == "before" ? adj : adj + 1;
            }
        }
        else
        {
            var bad = Env("sai.accounts/error/1");
            Put(bad, "status", "move_args");
            Console.WriteLine(Json.Serialize(bad));
            return 1;
        }

        if (toIdx == fromIdx)
        {
            var noop = Env("sai.accounts/mutate/1");
            Put(noop, "status", "noop");
            Put(noop, "account_id", itemId);
            Put(noop, "result", "unchanged");
            return Emit(noop);
        }

        list.RemoveAt(fromIdx);
        list.Insert(toIdx, item);
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (!string.IsNullOrEmpty(path))
        {
            using (new StateLock(path))
            lock (Gate)
            {
                var st = ReadState(path);
                var ids = new List<string>();
                foreach (var a in list) ids.Add((string)a["account_id"]);
                st["order"] = string.Join(",", ids.ToArray());
                WriteState(path, st);
            }
        }
        var ok = Env("sai.accounts/mutate/1");
        Put(ok, "status", "ok");
        Put(ok, "account_id", itemId);
        Put(ok, "from_index", fromIdx);
        Put(ok, "to_index", toIdx);
        Put(ok, "result", "moved");
        return Emit(ok);
    }

    static string NormalizeSelectorKey(string sel)
    {
        if (string.IsNullOrEmpty(sel)) return "";
        if (string.Equals(sel, "antigravity:windows-user:0000000000000001", StringComparison.OrdinalIgnoreCase)) return "AG1";
        if (string.Equals(sel, "antigravity:windows-user:0000000000000002", StringComparison.OrdinalIgnoreCase)) return "AG2";
        if (string.Equals(sel, "antigravity:windows-user:0000000000000003", StringComparison.OrdinalIgnoreCase)) return "AG3";
        return sel;
    }

    static string ConfiguredIdentityOf(string sel)
    {
        string key = NormalizeSelectorKey(sel);
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (!string.IsNullOrEmpty(path))
        {
            using (new StateLock(path))
            lock (Gate)
            {
                var st = ReadState(path);
                if (StateOf(st, "cleared:" + key) == "1") return "";
                string bound = StateOf(st, "bound_fp:" + key);
                if (!string.IsNullOrEmpty(bound)) return bound;
            }
        }
        string v;
        if (!string.IsNullOrEmpty(key) && Scene.TryGetValue("configured_fp_" + key, out v)) return v;
        if (!string.IsNullOrEmpty(sel) && Scene.TryGetValue("configured_fp_" + sel, out v)) return v;
        return Value("configured_fp", "");
    }

    static string ConfiguredLabelOf(string sel)
    {
        string key = NormalizeSelectorKey(sel);
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (!string.IsNullOrEmpty(path))
        {
            using (new StateLock(path))
            lock (Gate)
            {
                var st = ReadState(path);
                if (StateOf(st, "cleared:" + key) == "1") return "";
                string bound = StateOf(st, "bound_label:" + key);
                if (!string.IsNullOrEmpty(bound)) return bound;
            }
        }
        string v;
        if (!string.IsNullOrEmpty(key) && Scene.TryGetValue("configured_label_" + key, out v)) return v;
        if (!string.IsNullOrEmpty(sel) && Scene.TryGetValue("configured_label_" + sel, out v)) return v;
        return Value("configured_label", "");
    }

    static int BindIdentity(string[] argv)
    {
        string sel = argv.Length > 1 ? argv[1] : "";
        string key = NormalizeSelectorKey(sel);
        if (Value("bind_identity_fails", "0") == "1" || (!string.IsNullOrEmpty(key) && Value("bind_identity_fails_" + key, "0") == "1"))
        {
            var err = Env("sai.accounts/error/1");
            Put(err, "status", "error");
            Put(err, "error", "BIND_FAILED");
            Put(err, "message", "Simulated bind-identity failure");
            return Emit(err, 1);
        }
        bool clear = false;
        foreach (string a in argv) if (a == "--clear") clear = true;
        if (clear)
        {
            string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
            if (!string.IsNullOrEmpty(path))
            {
                using (new StateLock(path))
                lock (Gate)
                {
                    var st = ReadState(path);
                    st["bound_fp:" + key] = "";
                    st["bound_label:" + key] = "";
                    st["cleared:" + key] = "1";
                    WriteState(path, st);
                }
            }
            var d = Env("sai.accounts/mutate/1");
            Put(d, "status", "account_identity_cleared");
            Put(d, "account_id", sel);
            Put(d, "result", "cleared");
            return Emit(d);
        }
        string fp = ArgOf(argv, "--fingerprint");
        string lbl = ArgOf(argv, "--label");
        if (string.IsNullOrEmpty(fp)) fp = IdentityOf(key);
        if (string.IsNullOrEmpty(lbl)) lbl = LabelOf(key);
        string path2 = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (!string.IsNullOrEmpty(path2) && !string.IsNullOrEmpty(fp))
        {
            using (new StateLock(path2))
            lock (Gate)
            {
                var st = ReadState(path2);
                st["bound_fp:" + key] = fp;
                if (!string.IsNullOrEmpty(lbl)) st["bound_label:" + key] = lbl;
                st["cleared:" + key] = "0";
                WriteState(path2, st);
            }
        }
        var d2 = Env("sai.accounts/mutate/1");
        Put(d2, "status", "ok");
        Put(d2, "account_id", sel);
        var res = new Dictionary<string, object> { { "fingerprint", fp } };
        if (!string.IsNullOrEmpty(lbl)) res["account_label"] = lbl;
        Put(d2, "result", res);
        return Emit(d2);
    }

    static Dictionary<string, object> Account(string id, string windowsUser, string label, string state)
    {
        var acc = new Dictionary<string, object>
        {
            { "account_id", id },
            { "display_name", label },
            { "compact_label", label },
            { "provider_id", "antigravity" },
            { "context_label", windowsUser },
            { "operational_state", state },
            { "execution_backend", "windows_user" },
            { "hidden", false },
            { "provider_metadata", new Dictionary<string, object> { { "windows_user", windowsUser } } }
        };
        string cfgFp = ConfiguredIdentityOf(label);
        if (cfgFp.Length > 0)
        {
            var ci = new Dictionary<string, object> { { "fingerprint", cfgFp } };
            string cfgLbl = ConfiguredLabelOf(label);
            if (cfgLbl.Length > 0) ci["account_label"] = cfgLbl;
            acc["configured_identity"] = ci;
        }
        return acc;
    }

    static int Capabilities()
    {
        var d = Env("sai.accounts/provider/1");
        Put(d, "provider_id", "antigravity");
        Put(d, "supports_multi_account", true);
        Put(d, "supports_interactive_login", true);
        Put(d, "supports_quota", true);
        Put(d, "supports_launch", true);
        Put(d, "supports_logout", true);
        Put(d, "supports_context_creation", true);
        return Emit(d);
    }

    static int Status(string[] argv)
    {
        string sel = argv.Length > 1 ? argv[1] : "";
        int n = StatusCalls();
        int offline = 0;
        if (Scene.ContainsKey("offline_status_calls"))
            int.TryParse(Scene["offline_status_calls"], out offline);
        bool online = n > offline;
        if (offline > 0 && offline < 99999)
        {
            bool started = ReadCount("context:start:" + sel) > 0 || ReadCount("context:connect:" + sel) > 0;
            online = started;
        }
        string auth = online ? Value("online_auth", "AUTHENTICATED") : Value("initial_auth", "UNKNOWN");
        // Signing in is only observable through the credential probe, so the
        // fixture models that directly: the account stays AUTH_REQUIRED until
        // the auth operation has actually been issued N times. Without this the
        // GUI's post-auth poll would have nothing to observe and a broken poll
        // would still look green.
        int requiredUntil = 0;
        if (online && Scene.ContainsKey("auth_required_until"))
            int.TryParse(Scene["auth_required_until"], out requiredUntil);
        if (requiredUntil > 0 && ReadCount("auth") < requiredUntil) auth = "AUTH_REQUIRED";
        // A completed logout is AUTH_REQUIRED with no credential at all -- the
        // two things the real engine reports for an account whose credential was
        // dropped. It stays that way only until the next SIGN-IN: the state is
        // the LAST thing that happened, so a completed switch (logout, then auth)
        // reads as signed in and an abandoned one (logout, then nothing) reads
        // as signed out. `logout_sticks` models the opposite: a logout that
        // answered success over a store it did not really own, which is exactly
        // the case the GUI is required to catch instead of opening a sign-in on
        // top of a credential that never went.
        bool signedOut = LastEvent() == "logout" && !StickyLogout();
        if (signedOut)
        {
            auth = "AUTH_REQUIRED";
            var empty = Env("sai.accounts/status/1");
            Put(empty, "account_id", sel);
            Put(empty, "context_state", online ? "ONLINE" : "OFFLINE");
            Put(empty, "auth_state", auth);
            Put(empty, "quota_state", "UNKNOWN");
            string cfgFpEmpty = ConfiguredIdentityOf(sel);
            if (cfgFpEmpty.Length > 0)
            {
                var ciEmpty = new Dictionary<string, object> { { "fingerprint", cfgFpEmpty } };
                string cfgLblEmpty = ConfiguredLabelOf(sel);
                if (cfgLblEmpty.Length > 0) ciEmpty["account_label"] = cfgLblEmpty;
                Put(empty, "configured_identity", ciEmpty);
            }
            Put(empty, "credential", new Dictionary<string, object>
            {
                { "target", "gemini:antigravity" },
                { "credential_present", false },
                { "provider_identity", new Dictionary<string, object> { { "verified", false } } }
            });
            return Emit(empty);
        }
        var d = Env("sai.accounts/status/1");
        Put(d, "account_id", sel);
        Put(d, "context_state", online ? "ONLINE" : "OFFLINE");
        Put(d, "auth_state", auth);
        Put(d, "quota_state", "UNKNOWN");
        string cfgFp = ConfiguredIdentityOf(sel);
        if (cfgFp.Length > 0)
        {
            var ci = new Dictionary<string, object> { { "fingerprint", cfgFp } };
            string cfgLbl = ConfiguredLabelOf(sel);
            if (cfgLbl.Length > 0) ci["account_label"] = cfgLbl;
            Put(d, "configured_identity", ci);
        }
        // The provider identity, nested exactly as the real engine nests it.
        // Two slots signed into ONE Google account answer with the SAME
        // fingerprint here, which is the condition T-239 is about: nothing in
        // context_state, auth_state or quota_state distinguishes them, and the
        // fingerprint is the only evidence that exists. Built as dictionaries
        // rather than concatenated, so a nested reply stays as legal JSON.
        //
        // The label is the human half of the same verified token. It is modelled
        // as an ordinary string a test can point at two slots with the SAME
        // fingerprint and DIFFERENT labels, which is the case that proves the
        // grouping keys on the fingerprint and not on the address.
        string fp = IdentityOf(sel);
        if (fp.Length > 0)
        {
            var pi = new Dictionary<string, object>
            {
                { "fingerprint", fp },
                { "source", "google_id_token_sub" },
                { "verified", true }
            };
            string label = LabelOf(sel);
            if (label.Length > 0) pi["account_label"] = label;
            Put(d, "credential", new Dictionary<string, object>
            {
                { "target", "gemini:antigravity" },
                { "credential_present", true },
                // The store's own timestamp, per selector. It is carried because
                // the real engine reports it and because a consumer must be able
                // to be shown that it proves nothing: two slots with one identity
                // refresh at completely different times and are still one human.
                { "credential_last_written", WrittenAtOf(sel) },
                { "provider_id", "antigravity" },
                { "provider_identity", pi }
            });
        }
        // Matches the real engine: `status` answers 0 whatever it finds. An
        // offline context is a fact, not a failure.
        return Emit(d);
    }

    static bool StickyLogout()
    {
        string v;
        return Scene.TryGetValue("logout_sticks", out v) && v == "1";
    }

    // When the credential store was written, which is a fact about a FILE and
    // not about a person. It exists here so a test can hand the GUI two slots
    // with wildly different timestamps and see whether it treats that as a
    // second identity.
    static string WrittenAtOf(string sel)
    {
        string v;
        if (!string.IsNullOrEmpty(sel) && Scene.TryGetValue("credential_written_" + sel, out v) && v.Length > 0) return v;
        return Value("credential_written", "2026-01-01T00:00:00Z");
    }

    // Per-selector override first, then the shared default, then nothing.
    // "Nothing" is the load-bearing default: it is what an unproven identity
    // looks like, and the GUI has to keep it distinguishable from a shared one.
    static string IdentityOf(string sel)
    {
        string key = NormalizeSelectorKey(sel);
        int auths = ReadCount("auth");
        if (auths > 0)
        {
            string after;
            if (!string.IsNullOrEmpty(key) && Scene.TryGetValue("identity_fp_after_" + key, out after) && after.Length > 0) return after;
            if (!string.IsNullOrEmpty(sel) && Scene.TryGetValue("identity_fp_after_" + sel, out after) && after.Length > 0) return after;
            string shared = Value("identity_fp_after", "");
            if (shared.Length > 0) return shared;
        }
        string v;
        if (!string.IsNullOrEmpty(key) && Scene.TryGetValue("identity_fp_" + key, out v)) return v;
        if (!string.IsNullOrEmpty(sel) && Scene.TryGetValue("identity_fp_" + sel, out v)) return v;
        return Value("identity_fp", "");
    }

    // The label follows whichever fingerprint is current, so a switch moves both
    // together and a stalled switch moves neither.
    static string LabelOf(string sel)
    {
        string key = NormalizeSelectorKey(sel);
        int auths = ReadCount("auth");
        if (auths > 0)
        {
            string after;
            if (!string.IsNullOrEmpty(key) && Scene.TryGetValue("identity_label_after_" + key, out after) && after.Length > 0) return after;
            if (!string.IsNullOrEmpty(sel) && Scene.TryGetValue("identity_label_after_" + sel, out after) && after.Length > 0) return after;
            string shared = Value("identity_label_after", "");
            if (shared.Length > 0) return shared;
        }
        string v;
        if (!string.IsNullOrEmpty(key) && Scene.TryGetValue("identity_label_" + key, out v)) return v;
        if (!string.IsNullOrEmpty(sel) && Scene.TryGetValue("identity_label_" + sel, out v)) return v;
        return Value("identity_label", "");
    }

    static int ReadCount(string channel)
    {
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (string.IsNullOrEmpty(path)) return 0;
        using (new StateLock(path))
        lock (Gate)
        {
            int v;
            int.TryParse(StateOf(ReadState(path), "count:" + channel), out v);
            return v;
        }
    }

    static string LastEvent()
    {
        string path = Environment.GetEnvironmentVariable("FAKE_ENGINE_STATE");
        if (string.IsNullOrEmpty(path)) return "";
        using (new StateLock(path))
        lock (Gate) { return StateOf(ReadState(path), "last"); }
    }

    static int Context(string[] argv)
    {
        string what = argv.Length > 1 ? argv[1] : "";
        string sel = argv.Length > 2 ? argv[2] : "";
        if (sel == "AG1")
        {
            var already = Env("sai.accounts/context/1");
            Put(already, "status", "already_current_user");
            return Emit(already);
        }
        var d = Env("sai.accounts/context/1");
        Put(d, "account_id", sel);
        // connect/disconnect are the intent verbs the Connect and Disconnect
        // buttons speak; start/stop are the bare process moves the launch path
        // reaches for on its own. The work underneath is the same, and the
        // fake has to answer both or the GUI tests would be measuring a
        // stand-in that no longer matches the engine contract.
        if (what == "start" || what == "connect")
        {
            Count("context:" + what + ":" + sel);
            Count("context:active:" + sel);
            Put(d, "status", "launch_requested");
            Put(d, "windows_user", Secondary);
            return Emit(d);
        }
        if (what == "delete")
        {
            Put(d, "status", "removed");
            return Emit(d);
        }
        if (what == "migrate")
        {
            Put(d, "status", "migrated");
            return Emit(d);
        }
        Put(d, "status", "shutting_down");
        return Emit(d);
    }

    static int Add(string[] argv)
    {
        var d = Env("sai.accounts/add/1");
        Put(d, "status", "created");
        Put(d, "account_id", "antigravity:windows-user:1122334455667788");
        Put(d, "execution_backend", "managed_windows_user");
        Put(d, "display_name", "AG4");
        return Emit(d);
    }

    // T-288: the facts the GUI reads back from the real engine's envelope.
    static int SaipenReadiness(string[] argv)
    {
        string verdict = Scene["saipen_verdict"];
        var d = Env("sai.accounts/saipen-readiness/1");
        Put(d, "account_id", argv.Length > 1 ? argv[1] : "");
        Put(d, "workdir", ArgOf(argv, "--workdir"));
        var admission = new Dictionary<string, object>();
        admission["next_action"] = "PHASE BUILD T-7";
        admission["write_admission"] = verdict == "PROJECT_LIFECYCLE_DENIED" ? "CROSS_USER_RUNTIME_UNAVAILABLE" : "ADMITTED";
        var runtime = new Dictionary<string, object>();
        runtime["code"] = verdict == "RUNTIME_STALE" ? "MACHINE_RUNTIME_STALE" : "MACHINE_RUNTIME_READY";
        var facts = new Dictionary<string, object>();
        facts["project_admission"] = admission;
        facts["saipen_runtime_detail"] = runtime;
        Put(d, "facts", facts);
        Put(d, "verdict", verdict);
        Put(d, "status", verdict == "READY" ? "ok" : verdict);
        Console.WriteLine(Json.Serialize(d));
        return verdict == "READY" ? 0 : 1;
    }

    static int Launch(string[] argv)
    {
        string sel = argv.Length > 1 ? argv[1] : "";
        string workdir = ArgOf(argv, "--workdir");
        var d = Env("sai.accounts/launch/1");
        Put(d, "account_id", sel);
        if (string.IsNullOrEmpty(workdir) || !Directory.Exists(workdir))
        {
            Put(d, "status", "WORKDIR_UNAVAILABLE");
            Put(d, "workdir", workdir);
            Put(d, "reason", "NOT_FOUND");
            Console.WriteLine(Json.Serialize(d));
            return 1;
        }
        // A folder that exists but that this identity cannot open. The real
        // engine answers this from the broker's own access check, so the
        // envelope has to carry the reason field or the GUI has nothing but an
        // exception string to go on.
        int denied = 0;
        if (Scene.ContainsKey("launch_access_denied")) int.TryParse(Scene["launch_access_denied"], out denied);
        // Counted ONCE. Count() increments, so calling it twice would spend two
        // probes on a single launch and put "the first N launches" off by one
        // -- which reads as a test that cannot fail.
        int nth = Count("launch");
        if (nth <= denied)
        {
            Put(d, "status", "WORKDIR_UNAVAILABLE");
            Put(d, "workdir", workdir);
            Put(d, "reason", "ACCESS_DENIED");
            Console.WriteLine(Json.Serialize(d));
            return 1;
        }
        int missing = 0;
        if (Scene.ContainsKey("launch_not_found")) int.TryParse(Scene["launch_not_found"], out missing);
        if (nth <= missing)
        {
            Put(d, "status", "WORKDIR_UNAVAILABLE");
            Put(d, "workdir", workdir);
            Put(d, "reason", "NOT_FOUND");
            Console.WriteLine(Json.Serialize(d));
            return 1;
        }
        Put(d, "workdir", workdir);
        Put(d, "status", "launched");
        Put(d, "pid", 4242);
        Put(d, "execution", "broker");
        return Emit(d);
    }

    // The account-plane sign-in operation. It takes a selector and NOTHING
    // else, so a caller that tries to couple authentication to a project
    // folder is refused by the shape of the call rather than by a later check.
    static int Auth(string[] argv)
    {
        var d = Env("sai.accounts/auth/1");
        Put(d, "account_id", argv.Length > 1 ? argv[1] : "");
        if (argv.Length != 2)
        {
            Put(d, "status", "OPERATION_NOT_ALLOWED");
            Put(d, "rejected_field", "extra_argument");
            Console.WriteLine(Json.Serialize(d));
            return 1;
        }
        Count("auth");
        NoteEvent("auth");
        // The real engine's auth answers a structured status and a nonzero exit
        // code when the sign-in window cannot be opened. Both halves have to be
        // modelled: a fixture that always said `launched` would let a caller
        // that ignores the envelope look correct.
        string authStatus = Value("auth_status", "launched");
        Put(d, "status", authStatus);
        if (authStatus != "launched")
        {
            Put(d, "error", "sign-in window could not be started");
            Console.WriteLine(Json.Serialize(d));
            return 1;
        }
        Put(d, "pid", 5151);
        Put(d, "auth_directory", @"C:\Users\antigravity_b");
        Put(d, "sid", "S-1-5-21-999999999-888888888-777777777-1002");
        Put(d, "execution", "broker");
        return Emit(d);
    }

    // The provider sign-out. It takes a selector and NOTHING else, exactly like
    // auth: the engine's whole input surface is the account, so there is no
    // credential target, no path and no program for a caller to point somewhere
    // else. Any extra argument is refused the same way auth refuses one, which
    // is the fixture half of "logout accepts no arbitrary target".
    static int Logout(string[] argv)
    {
        var d = Env("sai.accounts/logout/1");
        Put(d, "account_id", argv.Length > 1 ? argv[1] : "");
        if (argv.Length != 2)
        {
            Put(d, "status", "OPERATION_NOT_ALLOWED");
            Put(d, "rejected_field", "extra_argument");
            Console.WriteLine(Json.Serialize(d));
            return 1;
        }
        Count("logout");
        NoteEvent("logout");
        Put(d, "provider", "antigravity");
        Put(d, "was_authenticated", true);
        Put(d, "execution", "broker");
        // The identity that WAS there, so a caller could in principle prove the
        // slot changed rather than merely lost something. A digest and an
        // address: no token byte, ever.
        Put(d, "identity_before", new Dictionary<string, object>
        {
            { "fingerprint", IdentityOf(argv[1]) },
            { "verified", true }
        });
        Put(d, "status", StickyLogout() ? "LOGOUT_INCOMPLETE" : "logged_out");
        Put(d, "credential_present", !StickyLogout());
        return Emit(d);
    }

    // Narrow ACL provisioning. Answers a status, never touches a real ACL: the
    // mutation itself is covered against the real helper binary in
    // tests/test_sai_accounts.py, where a real directory and a real SDDL exist.
    static int GrantWorkdir(string[] argv)
    {
        var d = Env("sai.accounts/grant-workdir/1");
        Put(d, "account_id", argv.Length > 1 ? argv[1] : "");
        Put(d, "workdir", ArgOf(argv, "--workdir"));
        if (argv.Length != 4 || argv[2] != "--workdir")
        {
            Put(d, "status", "grant_workdir_args");
            Console.WriteLine(Json.Serialize(d));
            return 1;
        }
        Put(d, "status", Value("grant_status", "granted"));
        return Emit(d);
    }

    static int SetHidden(string[] argv)
    {
        string raw = argv.Length > 2 ? argv[2] : "";
        bool hidden;
        if (!bool.TryParse(raw, out hidden))
        {
            var bad = Env("sai.accounts/error/1");
            Put(bad, "status", "set_hidden_args");
            Put(bad, "error", "true|false");
            Console.WriteLine(Json.Serialize(bad));
            return 1;
        }
        var d = Env("sai.accounts/set-hidden/1");
        Put(d, "account_id", argv.Length > 1 ? argv[1] : "");
        Put(d, "status", hidden ? "account_hidden" : "account_shown");
        return Emit(d);
    }

    static string Value(string key, string fallback)
    {
        string v;
        return Scene.TryGetValue(key, out v) ? v : fallback;
    }
}