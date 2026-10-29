using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Text;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

// Drive production identity projection and compare actual rendered pixels.
// Display labels never serve as an identity oracle or reconstruct panel text.
static class AccountIdentityContractTest
{
    const BindingFlags Any = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
    const string Alice = "aaaabbbbccccdddd", Bob = "1111222233334444";
    static int passed, failed;
    static Type App = typeof(Saituls.SaitulsForm);
    static Type RowType = App.GetNestedType("AccountRow", Any);
    static void Set(object target, string name, object value) { target.GetType().GetField(name, Any).SetValue(target, value); }
    static string Text(object target, string name) { return (string)target.GetType().GetField(name, Any).GetValue(target); }
    static object Call(string name, params object[] args) { return App.GetMethod(name, Any).Invoke(null, args); }
    static void Check(bool ok, string name)
    {
        Console.WriteLine((ok ? "PASS  " : "FAIL  ") + name);
        if (ok) passed++; else failed++;
    }
    static Dictionary<string, object> Identity(string fingerprint, string label, bool verified)
    {
        return new Dictionary<string, object> { { "fingerprint", fingerprint }, { "account_label", label }, { "verified", verified } };
    }
    static object Row(string compact, string provider, string fingerprint)
    {
        object row = Activator.CreateInstance(RowType);
        Set(row, "Id", "contract-" + compact); Set(row, "Compact", compact);
        Set(row, "Name", "Identity contract"); Set(row, "Provider", provider);
        Set(row, "Backend", "windows_user"); Set(row, "Context", Environment.UserName);
        Set(row, "State", "ENABLED"); Set(row, "ContextState", "ONLINE");
        Set(row, "AuthState", "AUTHENTICATED");
        Set(row, "ConfiguredIdentityFp", fingerprint); Set(row, "ConfiguredIdentityLabel", "same@example.com");
        Set(row, "RuntimeIdentityFp", fingerprint); Set(row, "RuntimeIdentityLabel", "same@example.com");
        return row;
    }
    static IList Rows(params object[] rows)
    {
        var list = (IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(RowType));
        foreach (object row in rows) list.Add(row);
        return list;
    }
    static Bitmap Render(Saituls.SaitulsForm form, object row, int width, int height)
    {
        var image = new Bitmap(width, height);
        using (var g = Graphics.FromImage(image))
        {
            g.Clear(Color.FromArgb(26, 24, 16));
            g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
            ((IList)form.GetType().GetField("Buttons", Any).GetValue(form)).Clear();
            App.GetMethod("DrawAccountDetailPane", Any).Invoke(form, new object[] { g, 0, 0, width, height, row });
        }
        return image;
    }
    static bool Equal(Bitmap left, Bitmap right)
    {
        for (int y = 0; y < left.Height; y++)
            for (int x = 0; x < left.Width; x++)
                if (left.GetPixel(x, y) != right.GetPixel(x, y))
                {
                    Console.WriteLine("PIXEL_DIFF x=" + x + " y=" + y);
                    return false;
                }
        return true;
    }
    [STAThread]
    static int Main()
    {
        string fixture = Path.Combine(Path.GetTempPath(), "sai-identity-contract-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(fixture);
        try
        {
            var settings = new Saituls.SaitulsSettings(fixture);
            using (var form = new Saituls.SaitulsForm(settings, null))
            {
                IntPtr handle = form.Handle;
                Set(form, "_accountsPollAt", DateTime.Now.AddYears(1));
                var timer = (System.Windows.Forms.Timer)App.GetField("_tick", Any).GetValue(form);
                timer.Stop();
                object row = Row("AG1", "antigravity", Alice);
                Set(form, "_accounts", Rows(row)); Set(form, "_accountsRuntime", Rows(row));
                Set(form, "_selectedAccount", "contract-AG1");
                // The invisible fixture has not had a first paint. Real GDI
                // font initialization changes even the unchanged subtitle on
                // hosted Windows; initialize it before comparing identities.
                using (var warmup = Render(form, row, 640, 540)) { }
                using (var same = Render(form, row, 640, 540))
                {
                    using (var unchanged = Render(form, row, 640, 540))
                        Check(Equal(same, unchanged), "unchanged identity rendering is stable before comparisons");
                    Set(row, "RuntimeIdentityFp", Bob);
                    using (var distinct = Render(form, row, 640, 540))
                    {
                        Check(!Equal(same, distinct), "equal email labels cannot collapse two different fingerprints in rendered pixels");
                        string imageDir = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_TEST_EVIDENCE");
                        if (!string.IsNullOrEmpty(imageDir)) distinct.Save(Path.Combine(imageDir, "identity-distinct.png"));
                    }
                    Set(row, "RuntimeIdentityFp", Alice.ToUpperInvariant());
                    using (var caseOnly = Render(form, row, 640, 540))
                        Check(Equal(same, caseOnly), "fingerprint casing does not invent a second account line");
                    Set(row, "RuntimeIdentityFp", Alice); Set(row, "RuntimeIdentityLabel", "renamed@example.com");
                    using (var renamed = Render(form, row, 640, 540))
                        Check(Equal(same, renamed), "renaming a label does not split the same provider identity");
                }
                var complete = new Dictionary<string, object> {
                    { "schema", "sai.accounts/status/1" },
                    { "configured_identity", Identity(Alice, "bound@example.com", true) },
                    { "credential", new Dictionary<string, object> { { "provider_identity", Identity(Bob, "unverified@example.com", false) } } }
                };
                Call("ApplyRuntimeIdentity", row, complete);
                Check(Text(row, "ConfiguredIdentityFp") == Alice, "an unverified observation leaves the explicit binding intact");
                Check(Text(row, "RuntimeIdentityFp") == "" && Text(row, "RuntimeIdentityLabel") == "", "an unverified claim never becomes a displayed identity");
                complete.Remove("configured_identity");
                Call("ApplyRuntimeIdentity", row, complete);
                Check(Text(row, "ConfiguredIdentityFp") == "" && Text(row, "IdentityFp") == "", "a complete status descriptor acknowledges a cleared binding");
                Check(Text(row, "ConfiguredIdentityLabel") == "", "clearing the fingerprint clears its old label too");
                Set(row, "ConfiguredIdentityFp", Alice);
                var partial = new Dictionary<string, object> { { "credential", new Dictionary<string, object> {
                    { "provider_identity", Identity(Bob, "observed@example.com", true) } } } };
                Call("ApplyRuntimeIdentity", row, partial);
                Check(Text(row, "ConfiguredIdentityFp") == Alice && Text(row, "RuntimeIdentityFp") == Bob, "a legacy partial observation preserves the binding and its independent observation");
                object other = Row("CL1", "claude", Alice);
                var pair = Rows(row, other);
                Call("MarkSharedIdentities", pair);
                Check(Text(row, "IdentitySharedWith") == "" && Text(other, "IdentitySharedWith") == "", "different providers never share a quota identity because hashes coincide");
                Set(other, "Provider", "antigravity");
                Call("MarkSharedIdentities", pair);
                Check(Text(row, "IdentitySharedWith") == "CL1" && Text(other, "IdentitySharedWith") == "AG1", "same-provider canonical duplicates still name both slots");
                Set(other, "Provider", ""); Call("MarkSharedIdentities", pair);
                Check(Text(row, "IdentitySharedWith") == "", "a missing provider proves no shared quota scope");
                Set(row, "ConfiguredIdentityFp", ""); Set(row, "ConfiguredIdentityLabel", "");
                Set(row, "RuntimeIdentityFp", ""); Set(row, "RuntimeIdentityLabel", "");
                Set(row, "IdentitySeen", false);
                using (var unseen = Render(form, row, 640, 540))
                {
                    Set(row, "IdentitySeen", true);
                    using (var unproven = Render(form, row, 640, 540))
                        Check(!Equal(unseen, unproven), "already checked but unproven never renders as not checked yet");
                }
            }
        }
        catch (Exception ex) { Console.WriteLine("FAIL  identity harness: " + ex); failed++; }
        finally { Directory.Delete(fixture, true); }
        Console.WriteLine("passed " + passed + ", failed " + failed);
        return failed == 0 ? 0 : 1;
    }
}
