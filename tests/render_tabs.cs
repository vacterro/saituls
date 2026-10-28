// Offscreen render of every SAITULS tab at given client sizes, against the
// operator's REAL configuration (a copy of SAITULS.ini in a scratch folder, so
// nothing the form saves touches the real file) and the real installed
// engines. Read-only: the form is never shown, never activated, and no button
// is pressed. Writes <out>\<tab>_<w>x<h>.png and a layout report that flags
// buttons overlapping each other or leaving the window.
//
//   render-tabs.exe <saituls-root> <out-dir> [WxH ...]
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

static class RenderTabs
{
    const BindingFlags Any = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

    static object Get(object o, string name)
    {
        Type t = o.GetType();
        FieldInfo f = t.GetField(name, Any);
        if (f != null) return f.GetValue(o);
        PropertyInfo p = t.GetProperty(name, Any);
        return p != null ? p.GetValue(o, null) : null;
    }
    static void Set(object o, string name, object v) { o.GetType().GetField(name, Any).SetValue(o, v); }
    static object Call(object o, string name, params object[] args)
    {
        foreach (MethodInfo m in o.GetType().GetMethods(Any))
            if (m.Name == name && m.GetParameters().Length == args.Length) return m.Invoke(o, args);
        throw new Exception("no method " + name);
    }

    static void Pump(Func<bool> done, int ms)
    {
        var sw = Stopwatch.StartNew();
        while (!done() && sw.ElapsedMilliseconds < ms) { Application.DoEvents(); Thread.Sleep(20); }
    }

    [STAThread]
    static int Main(string[] args)
    {
        string root = args[0], outDir = args[1];
        Directory.CreateDirectory(outDir);
        var sizes = new List<Size>();
        for (int i = 2; i < args.Length; i++)
            foreach (string one in args[i].Split(new[] { ',', ' ' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string[] wh = one.Split('x');
                sizes.Add(new Size(int.Parse(wh[0]), int.Parse(wh[1])));
            }
        if (sizes.Count == 0) { sizes.Add(new Size(600, 460)); sizes.Add(new Size(720, 580)); sizes.Add(new Size(1000, 700)); }

        string scratch = Path.Combine(outDir, "ini");
        Directory.CreateDirectory(scratch);
        File.Copy(Path.Combine(root, "SAITULS.ini"), Path.Combine(scratch, "SAITULS.ini"), true);
        string ico = Path.Combine(root, "SAITULS.ico");
        if (File.Exists(ico)) File.Copy(ico, Path.Combine(scratch, "SAITULS.ico"), true);
        var s = new Saituls.SaitulsSettings(scratch);
        s.Load();
        s.RootPath = root;
        string limisaw = Environment.GetEnvironmentVariable("RENDER_LIMISAW");
        if (!string.IsNullOrEmpty(limisaw)) s.LimisawPath = limisaw;
        string lang = Environment.GetEnvironmentVariable("RENDER_LANG");
        if (!string.IsNullOrEmpty(lang)) s.Language = lang;
        Saituls.AccountsLoc.Apply(s.Language);

        var tray = new NotifyIcon();
        var form = new Saituls.SaitulsForm(s, tray);
        IntPtr h = form.Handle;
        // Stop the timer so nothing runs except what this harness asks for.
        var tick = (System.Windows.Forms.Timer)Get(form, "_tick");
        if (tick != null) tick.Stop();

        Call(form, "LoadAccounts");
        Call(form, "RefreshLimits", true);
        Pump(() => !(bool)Get(form, "_limitsBusy"), 180000);
        Call(form, "EnsureMediaProbe");
        Pump(() => (bool)Get(form, "_mediaProbeDone"), 60000);
        // One status pass so the rows carry runtime state.
        Set(form, "_accountsPollAt", DateTime.MinValue);
        Call(form, "PollStatusTick");
        Pump(() => !(bool)Get(form, "_accountsPolling"), 30000);
        Pump(() => false, 600);

        var report = new List<string>();
        string[] tabs = { "Home", "Accounts", "Settings", "Explorer menus", "Tools", "Organizer" };
        foreach (Size size in sizes)
        {
            form.ClientSize = size;
            foreach (string tab in tabs)
            {
                Set(form, "CurrentTab", tab);
                if (tab == "Accounts") Set(form, "_acctAdvancedExpanded", false);
                using (var bmp = new Bitmap(form.ClientSize.Width, form.ClientSize.Height))
                {
                    using (Graphics g = Graphics.FromImage(bmp))
                        Call(form, "OnPaint", new PaintEventArgs(g, new Rectangle(Point.Empty, bmp.Size)));
                    string file = Path.Combine(outDir, tab.Replace(" ", "_") + "_" + size.Width + "x" + size.Height + ".png");
                    bmp.Save(file, ImageFormat.Png);
                }
                // Layout audit on the hit targets the paint registered.
                var buttons = (IList)Get(form, "Buttons");
                var rects = new List<Rectangle>();
                var labels = new List<string>();
                foreach (object b in buttons) { rects.Add((Rectangle)Get(b, "R")); labels.Add((string)Get(b, "Label") ?? ""); }
                for (int i = 0; i < rects.Count; i++)
                {
                    Rectangle r = rects[i];
                    if (r.IsEmpty) continue;
                    if (r.Right > size.Width || r.Bottom > size.Height || r.X < 0 || r.Y < 0)
                        report.Add(size.Width + "x" + size.Height + " " + tab + ": button '" + labels[i] + "' outside window " + r);
                    for (int j = i + 1; j < rects.Count; j++)
                    {
                        if (rects[j].IsEmpty) continue;
                        Rectangle o = Rectangle.Intersect(r, rects[j]);
                        // Account list rows are drawn under their own hit target.
                        if (o.Width > 2 && o.Height > 2 && !(tab == "Accounts" && (r.Width >= 160 || rects[j].Width >= 160) && (labels[i] == "" || labels[j] == "")))
                            report.Add(size.Width + "x" + size.Height + " " + tab + ": '" + labels[i] + "' overlaps '" + labels[j] + "'");
                    }
                }
            }
        }
        File.WriteAllLines(Path.Combine(outDir, "layout-report.txt"), report.ToArray());
        Console.WriteLine("children started by the form: " + Get(form, "ChildLaunches"));
        Console.WriteLine("layout findings: " + report.Count);
        foreach (string line in report) Console.WriteLine("  " + line);
        Call(form, "StopBackground");
        tray.Dispose();
        return report.Count == 0 ? 0 : 1;
    }
}
