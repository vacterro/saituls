// Executes the actual LaunchAccount against disposable helper/broker pipes.
// No Windows account, credential, live registry, or provider process is used.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using System.Threading;
using System.Web.Script.Serialization;

static class ManagedConsoleLaunchTest
{
    static int passed, failed, sequence;
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    static object Call(string name, params object[] args)
    {
        return typeof(SaiAccounts).GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public).Invoke(null, args);
    }
    static void Check(bool ok, string name)
    {
        if (ok) { passed++; Console.WriteLine("PASS  " + name); }
        else { failed++; Console.WriteLine("FAIL  " + name); }
    }

    sealed class ReplyPipe : IDisposable
    {
        readonly NamedPipeServerStream server;
        readonly Thread thread;
        readonly string reply;
        public Dictionary<string, object> Request;
        public int Order;
        public Exception Error;
        public ReplyPipe(string name, string response)
        {
            reply = response;
            server = new NamedPipeServerStream(name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
            thread = new Thread(Run) { IsBackground = true };
            thread.Start();
        }
        void Run()
        {
            try
            {
                while (Request == null)
                {
                    server.WaitForConnection();
                    var reader = new StreamReader(server);
                    string line = reader.ReadLine();
                    // File.Exists probes the broker pipe before the actual
                    // connection; it closes without sending a JSON request.
                    if (line == null) { server.Disconnect(); continue; }
                    var writer = new StreamWriter(server) { AutoFlush = true };
                    Request = Json.Deserialize<Dictionary<string, object>>(line);
                    Order = Interlocked.Increment(ref sequence);
                    writer.WriteLine(reply);
                }
            }
            catch (ObjectDisposedException) { }
            catch (IOException) { }
            catch (Exception ex) { Error = ex; }
        }
        public void Dispose()
        {
            server.Dispose();
            if (!thread.Join(3000)) throw new Exception("owned pipe did not stop");
            if (Error != null) throw new Exception("owned pipe failed", Error);
        }
    }

    static void Scenario(string backend, bool legacyManaged, string profileStatus, bool expectsRepair, string workdir)
    {
        string scope = "console-" + Guid.NewGuid().ToString("N");
        string helperName = "console-helper-" + Guid.NewGuid().ToString("N");
        string sid = "S-1-5-21-9-9-9-1234";
        Environment.SetEnvironmentVariable("SAI_ACCOUNTS_SCOPE", scope);
        Environment.SetEnvironmentVariable("SAI_HELPER_PIPE", helperName);
        string fp = (string)Call("Fingerprint", sid);
        string pipe = (string)Call("PipeNameFor", scope, fp);
        var meta = new Dictionary<string, object> { { "windows_sid", sid }, { "managed", legacyManaged } };
        var account = new Dictionary<string, object> {
            { "account_id", "antigravity:windows-user:" + fp }, { "provider_id", "antigravity" },
            { "execution_backend", backend }, { "operational_state", "ENABLED" }, { "provider_metadata", meta }
        };
        using (var helper = new ReplyPipe(helperName, Json.Serialize(new { status = profileStatus, input_languages = "already_correct" })))
        using (var broker = new ReplyPipe(pipe, "{\"status\":\"launched\",\"pid\":7}"))
        {
            // LaunchAccount takes the optional auth token as a fourth
            // argument. Reflection does not apply the C# default, so the
            // no-token launch path is passed explicitly; every assertion
            // below is unchanged.
            var response = (Dictionary<string, object>)Call("LaunchAccount", account, new List<Dictionary<string, object>> { account }, workdir, null);
            string label = backend + (legacyManaged ? " legacy managed" : "");
            Check((string)response["status"] == "launched", label + " online broker launch succeeds");
            Check(broker.Request != null && (string)broker.Request["cmd"] == "launch"
                && (string)broker.Request["workdir"] == workdir, label + " intended workdir reaches broker");
            Check((string)broker.Request["account_id"] == (string)account["account_id"]
                && (string)broker.Request["compact_label"] == "AG1",
                label + " canonical account and derived label reach the managed broker");
            if (expectsRepair)
            {
                Check(helper.Request != null && helper.Request.Count == 2
                    && (string)helper.Request["op"] == "managed_console_profile"
                    && (string)helper.Request["target_sid"] == sid, label + " repairs exactly the intended SID");
                Check(helper.Order > 0 && helper.Order < broker.Order, label + " repair precedes visible console launch");
                Check(response.ContainsKey("console_profile") && (string)response["console_profile"] == profileStatus,
                    label + " exposes the actual profile outcome " + profileStatus);
            }
            else
                Check(helper.Request == null && !response.ContainsKey("console_profile"), label + " external user preferences stay untouched");
        }
    }

    static int Main()
    {
        string scratch = Path.Combine(Path.GetTempPath(), "sai-console-contract-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(scratch);
        string oldRoot = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_ROOT");
        string oldScope = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_SCOPE");
        string oldHelper = Environment.GetEnvironmentVariable("SAI_HELPER_PIPE");
        try
        {
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_ROOT", scratch);
            Scenario("managed_windows_user", false, "applied", true, scratch);
            Scenario("windows_user", true, "already_correct", true, scratch);
            Scenario("windows_user", false, "applied", false, scratch);
            Scenario("managed_windows_user", false, "HIVE_OPEN_FAILED", true, scratch);
        }
        finally
        {
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_ROOT", oldRoot);
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_SCOPE", oldScope);
            Environment.SetEnvironmentVariable("SAI_HELPER_PIPE", oldHelper);
            Directory.Delete(scratch, true); // exact directory allocated above
        }
        Console.WriteLine("passed " + passed + ", failed " + failed);
        return failed == 0 ? 0 : 1;
    }
}
