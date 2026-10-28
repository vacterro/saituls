// Disposable reflection probe: execute the compiled helper's real private
// journal routines, without copying their implementation into the oracle.
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Security.Principal;
using System.Web.Script.Serialization;

class AccountsJournalProbe
{
    static Type Helper;
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static object Call(string name, params object[] args)
    {
        foreach (var method in Helper.GetMethods(BindingFlags.Static | BindingFlags.NonPublic))
            if (method.Name == name && method.GetParameters().Length == args.Length)
                return method.Invoke(null, args);
        throw new MissingMethodException(name);
    }
    static string S(Dictionary<string, object> d, string k)
    { object v; return d.TryGetValue(k, out v) ? v as string : null; }
    static object V(Dictionary<string, object> d, string k)
    { object v; return d.TryGetValue(k, out v) ? v : null; }
    static Dictionary<string, object> Run(Dictionary<string, object> d)
    {
        var r = new Dictionary<string, object>();
        string method = S(d, "method");
        if (method == "update")
        {
            List<string> targets = null;
            var a = V(d, "targets") as object[];
            if (a != null) { targets = new List<string>(); foreach (object item in a) targets.Add((string)item); }
            object error = Call("UpdateTxnJournal", S(d, "state"), S(d, "error"),
                S(d, "op"), S(d, "sid"), S(d, "workdir"), V(d, "created"),
                targets, S(d, "pre"), S(d, "post"), S(d, "write"), object.Equals(V(d, "begin"), true));
            r["ok"] = error == null; r["error"] = error;
        }
        else if (method == "state")
            r["ok"] = Call("MarkTxnIntentState", S(d, "state"), S(d, "error"));
        else if (method == "failed")
        {
            object result = Call("MarkTxnIntentFailed", S(d, "op"), S(d, "sid"),
                S(d, "workdir"), S(d, "state"), S(d, "error"));
            r["ok"] = result;
        }
        else if (method == "remove" || method == "reconcile")
        {
            object[] args = { null };
            r["ok"] = Call(method == "remove" ? "RemoveTxnIntent" : "ReconcileInterruptedTxn", args);
            r["error"] = args[0];
        }
        else if (method == "publish")
        {
            string path = System.IO.Path.Combine(Environment.GetEnvironmentVariable("SAI_ACCOUNTS_ROOT"), "grants.txn.json");
            object[] args = { path, V(d, "journal"), null,
                System.IO.File.Exists(path) ? System.IO.File.ReadAllText(path) : null, !System.IO.File.Exists(path) };
            r["ok"] = Call("PublishTxnJournal", args); r["error"] = args[2];
        }
        else if (method == "fingerprint")
            r["value"] = Call("CurrentAclFingerprint", S(d, "workdir"));
        else if (method == "failure")
            Call("HandleGrantFailure", r, S(d, "workdir"), S(d, "sid"),
                new SecurityIdentifier(S(d, "sid")), "probe", null, "ACL_MUTATION_FAILED");
        else if (method == "fault")
        {
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_JOURNAL_FAULT", S(d, "value")); r["ok"] = true;
        }
        else if (method == "checkpoint")
        {
            Environment.SetEnvironmentVariable("SAI_ACCOUNTS_JOURNAL_CHECKPOINT", S(d, "value")); r["ok"] = true;
        }
        else throw new ArgumentException("unknown probe method");
        return r;
    }
    static int Main(string[] args)
    {
        string root = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_ROOT");
        if (args.Length != 1 || Environment.GetEnvironmentVariable("SAI_HELPER_TEST_MODE") != "1"
            || string.IsNullOrEmpty(root) || !System.IO.Path.GetFullPath(root).StartsWith(
                System.IO.Path.GetFullPath(System.IO.Path.GetTempPath()), StringComparison.OrdinalIgnoreCase))
            return 2;
        Helper = Assembly.LoadFrom(args[0]).GetType("SaiAccountsHelper", true);
        string line;
        while ((line = Console.ReadLine()) != null)
        {
            Dictionary<string, object> r;
            try { r = Run(Json.DeserializeObject(line) as Dictionary<string, object>); }
            catch (Exception ex) { r = new Dictionary<string, object>(); r["ok"] = false; r["exception"] = ex.ToString(); }
            Console.WriteLine(Json.Serialize(r)); Console.Out.Flush();
        }
        return 0;
    }
}
