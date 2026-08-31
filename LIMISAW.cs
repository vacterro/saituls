using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace Limisaw
{
    static class Native
    {
        [DllImport("gdi32.dll")] public static extern IntPtr CreateFont(int nHeight, int nWidth, int nEscapement, int nOrientation,
            int fnWeight, uint fdwItalic, uint fdwUnderline, uint fdwStrikeOut, uint fdwCharSet,
            uint fdwOutputPrecision, uint fdwClipPrecision, uint fdwQuality, uint fdwPitchAndFamily, string lpszFace);
        public const int NONANTIALIASED_QUALITY = 3;
        [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
    }

    static class Palette
    {
        public static readonly Color BG = C(0x1A1810);
        public static readonly Color SURFACE = C(0x332E22);
        public static readonly Color RAISED = C(0x3D372A);
        public static readonly Color ALT = C(0x453D30);
        public static readonly Color BDARK = C(0x100E08);
        public static readonly Color BEVEL = C(0x75663D);
        public static readonly Color TEXT = C(0xD4C89A);
        public static readonly Color TEXT2 = C(0x9C9371);
        public static readonly Color MUTED = C(0x6E674E);
        public static readonly Color SUCCESS = C(0x4A7A20);
        public static readonly Color SUCCESS_BRIGHT = C(0x7CCB4A);
        public static readonly Color WARNING = C(0x7A7A20);
        public static readonly Color WARNING_BRIGHT = C(0xC8C83A);
        public static readonly Color DANGER = C(0x7A2020);
        public static readonly Color DANGERTXT = C(0xE06666);
        public static readonly Color DANGER_BRIGHT = C(0xF07070);
        public static readonly Color LINK = C(0xF0D060);
        static Color C(int rgb) { return Color.FromArgb((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF); }
    }

    class LimisawSettings
    {
        public string Dir;
        public string IniPath;
        public int RefreshSeconds = 180;
        public bool TrayC1_5h = true, TrayC1_Wk = true, TrayC2_5h = true, TrayC2_Wk = true;
        public bool NotifyOnReset = true;
        public bool NotifyBeforeReset = true;
        public int WarningMinutes = 10;
        public bool AutoStart = false;

        public LimisawSettings(string dir) { Dir = dir; IniPath = Path.Combine(dir, "LIMISAW.ini"); }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern int GetPrivateProfileString(string app, string key, string def, System.Text.StringBuilder buf, int size, string file);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern bool WritePrivateProfileString(string app, string key, string val, string file);

        string Read(string key, string def) { var sb = new System.Text.StringBuilder(260); GetPrivateProfileString("limisaw", key, def, sb, sb.Capacity, IniPath); return sb.ToString(); }
        void Write(string key, string val) { WritePrivateProfileString("limisaw", key, val, IniPath); }

        public void Load()
        {
            int.TryParse(Read("RefreshSeconds", "180"), out RefreshSeconds); if (RefreshSeconds < 30) RefreshSeconds = 30;
            TrayC1_5h = Read("TrayC1_5h", "1") == "1"; TrayC1_Wk = Read("TrayC1_Wk", "1") == "1";
            TrayC2_5h = Read("TrayC2_5h", "1") == "1"; TrayC2_Wk = Read("TrayC2_Wk", "1") == "1";
            NotifyOnReset = Read("NotifyOnReset", "1") == "1"; NotifyBeforeReset = Read("NotifyBeforeReset", "1") == "1";
            int.TryParse(Read("WarningMinutes", "10"), out WarningMinutes);
            AutoStart = Read("AutoStart", "0") == "1";
        }
        public void Save()
        {
            Write("RefreshSeconds", RefreshSeconds.ToString()); Write("TrayC1_5h", TrayC1_5h ? "1" : "0");
            Write("TrayC1_Wk", TrayC1_Wk ? "1" : "0"); Write("TrayC2_5h", TrayC2_5h ? "1" : "0");
            Write("TrayC2_Wk", TrayC2_Wk ? "1" : "0"); Write("NotifyOnReset", NotifyOnReset ? "1" : "0");
            Write("NotifyBeforeReset", NotifyBeforeReset ? "1" : "0"); Write("WarningMinutes", WarningMinutes.ToString());
            Write("AutoStart", AutoStart ? "1" : "0");
        }
    }

    class AccountData
    {
        public int Index; public string Name; public bool Ok; public string Error;
        public bool FiveHourAvail, WeeklyAvail;
        public int FiveHourRem, WeeklyRem;
        public string FiveHourReset, WeeklyReset;
    }

    class ResetEvent
    {
        public int AccountIndex; public string LimitType; public int NewRemaining; public string ResetAt;
    }

    class LimisawForm : Form
    {
        string RootPath; LimisawSettings Settings;
        AccountData[] Accounts = new AccountData[2];
        AccountData[] PrevAccounts = new AccountData[2];
        List<string> NotifiedResetKeys = new List<string>();
        string LastFetch = ""; bool Stale = false; bool Refreshing = false;
        Timer RefreshTimer; NotifyIcon Tray;
        ToolStripMenuItem[] TrayCheckItems = new ToolStripMenuItem[4];
        List<Rectangle> Buttons = new List<Rectangle>(); List<Action> ButtonActions = new List<Action>();

        static Font MakePixelFont(int pt) { IntPtr hf = Native.CreateFont(-(int)(pt * 96 / 72), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, Native.NONANTIALIASED_QUALITY, 0, "Verdana"); return Font.FromHfont(hf); }
        static Font F(int pt) { return MakePixelFont(pt); }

        public LimisawForm(string root, LimisawSettings s, NotifyIcon tray)
        {
            RootPath = root; Settings = s; Tray = tray;
            Text = "LIMISAW"; FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen; ClientSize = new Size(380, 260);
            BackColor = Palette.BG; DoubleBuffered = true; TopMost = false;
            RefreshTimer = new Timer { Interval = Settings.RefreshSeconds * 1000 };
            RefreshTimer.Tick += (o, e) => RefreshData();
            RefreshTimer.Start(); RefreshData();
        }

        protected override void WndProc(ref Message m)
        {
            const int WM_NCHITTEST = 0x84, HTCAPTION = 2;
            base.WndProc(ref m);
            if (m.Msg == WM_NCHITTEST)
            {
                int raw = m.LParam.ToInt32(); int x = raw & 0xFFFF; if (x > 0x7FFF) x -= 0x10000;
                int y = (raw >> 16) & 0xFFFF; if (y > 0x7FFF) y -= 0x10000;
                if (PointToClient(new Point(x, y)).Y < 22 && x < Width - 20) m.Result = (IntPtr)HTCAPTION;
            }
        }

        public void RefreshData()
        {
            if (Refreshing) return; Refreshing = true;
            System.Threading.ThreadPool.QueueUserWorkItem(_ =>
            {
                try
                {
                    string probe = Path.Combine(RootPath, "Scripts", "limisaw_probe.py");
                    var psi = new ProcessStartInfo("python.exe", "\"" + probe + "\" --json")
                    { RedirectStandardOutput = true, UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = RootPath };
                    var proc = Process.Start(psi); if (proc == null) { SetError("python not found"); return; }
                    string json = proc.StandardOutput.ReadToEnd();
                    if (!proc.WaitForExit(20000)) { proc.Kill(); SetError("timeout"); return; }
                    if (proc.ExitCode != 0) { SetError("probe exit " + proc.ExitCode); return; }
                    ParseAndUpdate(json);
                }
                catch (Exception ex) { SetError(ex.Message); }
            });
        }

        void SetError(string msg) { Stale = true; try { BeginInvoke((Action)(() => { Refresh(); UpdateTray(); })); } catch { } }

        void ParseAndUpdate(string json)
        {
            try
            {
                var ser = new JavaScriptSerializer();
                var root = ser.Deserialize<Dictionary<string, object>>(json);
                var accounts = root["accounts"] as ArrayList; int i = 0;
                var newAccounts = new AccountData[2];
                foreach (var aObj in accounts)
                {
                    var a = aObj as Dictionary<string, object>; var ad = new AccountData();
                    ad.Index = (int)a["index"]; ad.Name = (string)a["name"]; ad.Ok = (bool)a["ok"];
                    ad.Error = a.ContainsKey("error") ? a["error"] as string : null;
                    var fh = a["five_hour"] as Dictionary<string, object>;
                    if (fh != null) { ad.FiveHourAvail = (bool)fh["available"];
                        if (fh.ContainsKey("remaining_percent") && fh["remaining_percent"] != null) ad.FiveHourRem = (int)Math.Round(Convert.ToDouble(fh["remaining_percent"]));
                        if (fh.ContainsKey("resets_at") && fh["resets_at"] != null) ad.FiveHourReset = (string)fh["resets_at"]; }
                    var wk = a["weekly"] as Dictionary<string, object>;
                    if (wk != null) { ad.WeeklyAvail = (bool)wk["available"];
                        if (wk.ContainsKey("remaining_percent") && wk["remaining_percent"] != null) ad.WeeklyRem = (int)Math.Round(Convert.ToDouble(wk["remaining_percent"]));
                        if (wk.ContainsKey("resets_at") && wk["resets_at"] != null) ad.WeeklyReset = (string)wk["resets_at"]; }
                    if (i < newAccounts.Length) newAccounts[i] = ad; i++;
                }
                PrevAccounts = Accounts; Accounts = newAccounts;
                Stale = false; LastFetch = DateTime.Now.ToString("HH:mm:ss");
                DetectResets();
            }
            catch { Stale = true; }
            finally
            {
                Refreshing = false;
                try { BeginInvoke((Action)(() => { Refresh(); UpdateTray(); })); } catch { }
            }
        }

        void DetectResets()
        {
            if (!Settings.NotifyOnReset) { return; }
            for (int i = 0; i < 2 && i < Accounts.Length; i++)
            {
                var cur = Accounts[i]; var prev = (i < PrevAccounts.Length) ? PrevAccounts[i] : null;
                if (cur == null) continue;
                if (cur.Ok && prev != null && prev.Ok)
                {
                    CheckReset(i, "5h", cur.FiveHourAvail, cur.FiveHourRem, cur.FiveHourReset, prev.FiveHourAvail, prev.FiveHourRem, prev.FiveHourReset);
                    CheckReset(i, "weekly", cur.WeeklyAvail, cur.WeeklyRem, cur.WeeklyReset, prev.WeeklyAvail, prev.WeeklyRem, prev.WeeklyReset);
                }
            }
        }

        void CheckReset(int accIdx, string limit, bool curAvail, int curRem, string curReset, bool prevAvail, int prevRem, string prevReset)
        {
            if (!curAvail || !prevAvail || string.IsNullOrEmpty(curReset) || string.IsNullOrEmpty(prevReset)) return;
            if (curReset == prevReset && curRem <= prevRem + 25) return; // no meaningful change
            if (curRem <= prevRem + 25) return; // dropped or negligible
            string key = accIdx + "_" + limit + "_" + curReset;
            if (NotifiedResetKeys.Contains(key)) return;
            NotifiedResetKeys.Add(key);
            if (NotifiedResetKeys.Count > 100) NotifiedResetKeys.RemoveRange(0, 50);
            ResetEvent ev = new ResetEvent { AccountIndex = accIdx, LimitType = limit, NewRemaining = curRem, ResetAt = curReset };
            Notify(ev);
        }

        void Notify(ResetEvent ev)
        {
            try
            {
                string accName = (ev.AccountIndex < Accounts.Length && Accounts[ev.AccountIndex] != null) ? Accounts[ev.AccountIndex].Name : "Account " + (ev.AccountIndex + 1);
                string title = "LIMISAW — " + accName;
                string text = ev.LimitType + " limit reset — " + ev.NewRemaining + "% remaining";
                Tray.BalloonTipTitle = title;
                Tray.BalloonTipText = text;
                Tray.BalloonTipIcon = ToolTipIcon.Info;
                Tray.ShowBalloonTip(5000);
            }
            catch { }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
            g.SmoothingMode = SmoothingMode.None; g.InterpolationMode = InterpolationMode.NearestNeighbor;
            g.Clear(Palette.BG); Buttons.Clear(); ButtonActions.Clear();
            int w = Width, h = Height;
            using (var b = new SolidBrush(Palette.SURFACE)) g.FillRectangle(b, 0, 0, w, 22);
            DrawText(g, "LIMISAW", 6, 3, Palette.TEXT, 12, true);
            string st = Stale ? "STALE" : "OK";
            DrawText(g, st, w - 60, 3, Stale ? Palette.DANGER_BRIGHT : (Accounts[0] != null ? Palette.SUCCESS_BRIGHT : Palette.MUTED), 12, true);
            var xr = new Rectangle(w - 22, 1, 20, 20); Buttons.Add(xr); ButtonActions.Add(() => { Hide(); });
            DrawText(g, "X", w - 18, 3, Palette.TEXT2, 12);
            int y = 30;
            DrawText(g, "Codex rate limits", 8, y, Palette.TEXT2, 11);
            DrawText(g, LastFetch, 140, y, Palette.MUTED, 10);
            for (int i = 0; i < 2; i++)
            {
                var a = Accounts[i] ?? new AccountData();
                int cy = y + 20 + i * 90; int cw = w - 16;
                using (var bg = new SolidBrush(Palette.RAISED)) g.FillRectangle(bg, 8, cy, cw, 80);
                DrawBevel(g, 8, cy, cw, 80, false);
                DrawText(g, a.Name ?? "Account " + (i + 1), 14, cy + 6, Palette.LINK, 11, true);
                if (a.Error != null)
                {
                    DrawText(g, "ERROR: " + a.Error, 14, cy + 30, Palette.DANGER_BRIGHT, 10);
                    DrawText(g, "stale/error", 14, cy + 50, Palette.MUTED, 10); continue;
                }
                int x1 = 14, x2 = 140, ly = cy + 28;
                DrawText(g, "5h:", x1, ly, Palette.TEXT2, 11);
                DrawText(g, FormatPct(a.FiveHourAvail, a.FiveHourRem), x1 + 28, ly, PctColor(a.FiveHourRem), 11, true);
                DrawText(g, FriendlyTime(a.FiveHourReset), x1 + 28, ly + 16, Palette.MUTED, 10);
                DrawText(g, "Weekly:", x2, ly, Palette.TEXT2, 11);
                DrawText(g, FormatPct(a.WeeklyAvail, a.WeeklyRem), x2 + 55, ly, PctColor(a.WeeklyRem), 11, true);
                DrawText(g, FriendlyTime(a.WeeklyReset), x2 + 55, ly + 16, Palette.MUTED, 10);
                int by = cy + 56;
                var br = new Rectangle(14, by, 60, 22); Buttons.Add(br); ButtonActions.Add(() => RefreshData());
                DrawButton(g, br, "Refresh", false);
            }
            DrawText(g, "Auto-refresh: " + (Settings.RefreshSeconds / 60) + " min", 8, h - 20, Palette.MUTED, 10);
        }

        string FormatPct(bool avail, int pct) { return avail ? (pct + "%") : "--"; }
        Color PctColor(int pct)
        {
            if (pct < 10) return Palette.DANGER_BRIGHT; if (pct < 30) return Palette.WARNING_BRIGHT; return Palette.SUCCESS_BRIGHT;
        }

        static string FriendlyTime(string iso)
        {
            if (string.IsNullOrEmpty(iso)) return "--";
            DateTime t;
            if (!DateTime.TryParse(iso, out t)) return iso;
            if (t.Kind == DateTimeKind.Unspecified) t = DateTime.SpecifyKind(t, DateTimeKind.Local);
            TimeSpan delta = t - DateTime.Now;
            if (delta.TotalSeconds < 0) return "now";
            if (delta.TotalHours < 1) return "in " + (int)delta.TotalMinutes + "m";
            if (delta.TotalDays < 1) return "in " + (int)delta.TotalHours + "h " + delta.Minutes + "m";
            return "in " + (int)delta.TotalDays + "d " + t.ToString("HH:mm");
        }

        void DrawText(Graphics g, string s, int x, int y, Color c, int pt, bool bold = false)
        { using (Font f = F(pt)) using (var br = new SolidBrush(c)) g.DrawString(s, f, br, (float)x, (float)y); }

        void DrawBevel(Graphics g, int x, int y, int w, int h, bool raised)
        {
            Color hi = raised ? Palette.BEVEL : Palette.BDARK, lo = raised ? Palette.BDARK : Palette.BEVEL;
            using (var p1 = new Pen(hi)) g.DrawRectangle(p1, x, y, w - 1, h - 1);
            using (var p2 = new Pen(lo)) g.DrawRectangle(p2, x + 1, y + 1, w - 3, h - 3);
        }

        void DrawButton(Graphics g, Rectangle r, string label, bool selected)
        {
            using (var bg = new SolidBrush(selected ? Palette.ALT : Palette.RAISED)) g.FillRectangle(bg, r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4);
            DrawBevel(g, r.X, r.Y, r.Width, r.Height, !selected);
            var fmt = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
            using (Font f = F(10)) using (var br = new SolidBrush(Palette.TEXT)) g.DrawString(label, f, br, new RectangleF(r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4), fmt);
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            for (int i = 0; i < Buttons.Count; i++) { if (Buttons[i].Contains(e.Location)) { ButtonActions[i](); return; } }
        }

        public void UpdateTray()
        {
            try
            {
                // Build tooltip text (compact, 63 chars limit)
                string tip = "LIMISAW";
                for (int i = 0; i < 2 && i < Accounts.Length; i++)
                {
                    var a = Accounts[i]; if (a == null || !a.Ok) continue;
                    string suffix = (i == 0 && a.Ok && Accounts[1] != null && Accounts[1].Ok) ? (" " + Accounts[1].Abbrev()) : a.Abbrev();
                    tip += " " + suffix;
                }
                if (tip.Length > 63) tip = tip.Substring(0, 60) + "...";
                Tray.Text = tip;

                // Draw icon with numbers
                using (Bitmap bmp = new Bitmap(32, 32))
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
                    g.SmoothingMode = SmoothingMode.None; g.InterpolationMode = InterpolationMode.NearestNeighbor;
                    g.Clear(Palette.BG);
                    // 2x2 grid: (0,0)=C1-5h, (1,0)=C1-wk, (0,1)=C2-5h, (1,1)=C2-wk
                    bool[] show = { Settings.TrayC1_5h, Settings.TrayC1_Wk, Settings.TrayC2_5h, Settings.TrayC2_Wk };
                    int[] vals = new int[4];
                    bool[] avails = new bool[4];
                    if (Accounts[0] != null) { vals[0] = Accounts[0].FiveHourRem; avails[0] = Accounts[0].FiveHourAvail; vals[1] = Accounts[0].WeeklyRem; avails[1] = Accounts[0].WeeklyAvail; }
                    if (Accounts[1] != null) { vals[2] = Accounts[1].FiveHourRem; avails[2] = Accounts[1].FiveHourAvail; vals[3] = Accounts[1].WeeklyRem; avails[3] = Accounts[1].WeeklyAvail; }
                    for (int ci = 0; ci < 4; ci++)
                    {
                        if (!show[ci]) continue;
                        int cellX = (ci % 2) * 16, cellY = (ci / 2) * 16;
                        string text = avails[ci] ? vals[ci].ToString() : "--";
                        Color col = Stale ? Palette.MUTED : (avails[ci] ? (vals[ci] < 10 ? Palette.DANGER_BRIGHT : (vals[ci] < 30 ? Palette.WARNING_BRIGHT : Palette.SUCCESS_BRIGHT)) : Palette.MUTED);
                        using (Font f = MakePixelFont(10)) using (var br = new SolidBrush(col)) { g.DrawString(text, f, br, (float)cellX + 1, (float)cellY + 3); }
                    }
                    Icon old = (Icon)Tray.Icon; Tray.Icon = Icon.FromHandle(bmp.GetHicon());
                    if (old != null) { try { old.Dispose(); } catch { } }
                }
            }
            catch { }
        }
    }

    static class AccountExt
    {
        public static string Abbrev(this AccountData a)
        {
            if (a == null || !a.Ok) return "";
            string fh = a.FiveHourAvail ? a.FiveHourRem.ToString() : "--";
            string wk = a.WeeklyAvail ? a.WeeklyRem.ToString() : "--";
            return fh + "/" + wk;
        }
    }

    static class Program
    {
        [STAThread]
        static void Main()
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            if (!File.Exists(Path.Combine(dir, "Scripts", "limisaw_probe.py")))
            { string p = Path.GetDirectoryName(dir); if (File.Exists(Path.Combine(p, "Scripts", "limisaw_probe.py"))) dir = p; }
            var s = new LimisawSettings(dir); s.Load();
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            NotifyIcon tray = new NotifyIcon(); tray.Visible = true;
            LimisawForm form = null; form = new LimisawForm(dir, s, tray);
            tray.Text = "LIMISAW";
            tray.ContextMenuStrip = BuildMenu(s, tray, () => form, () => { form.RefreshData(); });
            form.FormClosing += (o, e) => { if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; form.Hide(); } };
            form.UpdateTray();
            try
            {
                using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                {
                    if (rk != null) { if (s.AutoStart) rk.SetValue("LimisawApp", "\"" + Application.ExecutablePath + "\""); else if (rk.GetValue("LimisawApp") != null) rk.DeleteValue("LimisawApp", false); }
                }
            }
            catch { }
            form.Show(); Application.Run(); tray.Dispose();
        }

        static ContextMenuStrip BuildMenu(LimisawSettings s, NotifyIcon tray, Func<LimisawForm> getForm, Action refresh)
        {
            var m = new ContextMenuStrip();
            m.Items.Add("Open LIMISAW", null, (o, e) => { var f = getForm(); f.Show(); f.Activate(); });
            m.Items.Add("Refresh now", null, (o, e) => refresh());
            m.Items.Add(new ToolStripSeparator());
            var showMenu = new ToolStripMenuItem("Show in tray");
            string[] labels = { "C1 5h", "C1 weekly", "C2 5h", "C2 weekly" };
            bool[] vals = { s.TrayC1_5h, s.TrayC1_Wk, s.TrayC2_5h, s.TrayC2_Wk };
            string[] keys = { "TrayC1_5h", "TrayC1_Wk", "TrayC2_5h", "TrayC2_Wk" };
            for (int i = 0; i < 4; i++)
            {
                int idx = i;
                var item = new ToolStripMenuItem(labels[i], null, (sender, eArgs) =>
                {
                    var it = (ToolStripMenuItem)sender; it.Checked = !it.Checked;
                    typeof(LimisawSettings).GetField(keys[idx]).SetValue(s, it.Checked); s.Save();
                    var f = getForm(); f.UpdateTray();
                });
                item.Checked = vals[i]; showMenu.DropDownItems.Add(item);
            }
            m.Items.Add(showMenu);
            m.Items.Add(new ToolStripSeparator());
            var notifyItem = new ToolStripMenuItem("Notify on reset") { Checked = s.NotifyOnReset };
            notifyItem.Click += (o, e) => { notifyItem.Checked = !notifyItem.Checked; s.NotifyOnReset = notifyItem.Checked; s.Save(); };
            m.Items.Add(notifyItem);
            m.Items.Add(new ToolStripSeparator());
            m.Items.Add("Exit", null, (o, e) => { var f = getForm(); f.Close(); try { tray.Visible = false; } catch { } Application.Exit(); });
            return m;
        }
    }
}