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
        [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr handle);
        public const int NONANTIALIASED_QUALITY = 3;
        [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr hIcon);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string className, string windowName);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
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
        public static readonly Color WARNING = C(0x7A7A20);
        public static readonly Color DANGER = C(0x7A2020);
        public static readonly Color DANGERTXT = C(0xD66464);
        public static readonly Color LINK = C(0xF0D060);
        static Color C(int rgb) { return Color.FromArgb((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF); }
    }

    class LimisawSettings
    {
        public string Dir;
        public string IniPath;
        public int RefreshSeconds = 180;
        public string TrayMetric = "lowest";
        public bool NotifyOnReset = true;
        public bool AutoStart = false;
        public int WindowX = int.MinValue, WindowY = int.MinValue;

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
            if (RefreshSeconds > 3600) RefreshSeconds = 3600;
            TrayMetric = Read("TrayMetric", "lowest");
            if (Array.IndexOf(new[] { "lowest", "c1_5h", "c1_week", "c2_5h", "c2_week" }, TrayMetric) < 0) TrayMetric = "lowest";
            NotifyOnReset = Read("NotifyOnReset", "1") == "1";
            AutoStart = Read("AutoStart", "0") == "1";
            int.TryParse(Read("WindowX", int.MinValue.ToString()), out WindowX);
            int.TryParse(Read("WindowY", int.MinValue.ToString()), out WindowY);
        }
        public void Save()
        {
            Write("RefreshSeconds", RefreshSeconds.ToString()); Write("TrayMetric", TrayMetric);
            Write("NotifyOnReset", NotifyOnReset ? "1" : "0");
            Write("AutoStart", AutoStart ? "1" : "0");
            Write("WindowX", WindowX.ToString()); Write("WindowY", WindowY.ToString());
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
        string LastFetch = ""; string LastError = ""; bool Stale = false; bool Refreshing = false;
        Timer RefreshTimer; NotifyIcon Tray;
        List<Rectangle> Buttons = new List<Rectangle>(); List<Action> ButtonActions = new List<Action>();

        static Font MakePixelFont(int pt)
        {
            IntPtr hf = Native.CreateFont(-(int)(pt * 96 / 72), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, Native.NONANTIALIASED_QUALITY, 0, "Verdana");
            try { using (Font wrapped = Font.FromHfont(hf)) return (Font)wrapped.Clone(); }
            finally { if (hf != IntPtr.Zero) Native.DeleteObject(hf); }
        }
        static Font F(int pt) { return MakePixelFont(pt); }

        public LimisawForm(string root, LimisawSettings s, NotifyIcon tray)
        {
            RootPath = root; Settings = s; Tray = tray;
            Text = "LIMISAW"; FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen; ClientSize = new Size(380, 260);
            BackColor = Palette.BG; DoubleBuffered = true; TopMost = false;
            KeyPreview = true;
            try { string ico = Path.Combine(root, "heh.ico"); if (File.Exists(ico)) Icon = new Icon(ico); } catch { }
            if (Settings.WindowX != int.MinValue && Settings.WindowY != int.MinValue)
            {
                var saved = new Rectangle(Settings.WindowX, Settings.WindowY, Width, Height);
                foreach (Screen screen in Screen.AllScreens)
                    if (screen.WorkingArea.IntersectsWith(saved)) { StartPosition = FormStartPosition.Manual; Location = saved.Location; break; }
            }
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
                Point p = PointToClient(new Point(x, y));
                if (p.X >= 0 && p.Y >= 0 && p.Y < 22 && p.X < Width - 22) m.Result = (IntPtr)HTCAPTION;
            }
        }

        public void HideToTray()
        {
            Settings.WindowX = Location.X; Settings.WindowY = Location.Y; Settings.Save();
            Hide();
        }

        public void RefreshData()
        {
            if (Refreshing) return; Refreshing = true;
            LastError = ""; Refresh();
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

        void SetError(string msg)
        {
            Stale = true; LastError = msg; Refreshing = false;
            try { BeginInvoke((Action)(() => { Refresh(); UpdateTray(); })); } catch { }
        }

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
                Stale = false; LastError = ""; LastFetch = DateTime.Now.ToString("HH:mm:ss");
                DetectResets();
            }
            catch (Exception ex) { Stale = true; LastError = ex.Message; }
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
            string st = Refreshing ? "..." : (Stale ? "STALE" : "OK");
            DrawText(g, st, w - 60, 3, Stale ? Palette.DANGERTXT : (Accounts[0] != null ? Palette.LINK : Palette.MUTED), 12, true);
            var xr = new Rectangle(w - 22, 1, 20, 20); Buttons.Add(xr); ButtonActions.Add(() => HideToTray());
            DrawText(g, "X", w - 18, 3, Palette.TEXT2, 12);
            int y = 30;
            DrawText(g, "Codex limits", 8, y, Palette.TEXT2, 11);
            string update = Refreshing ? "Checking..." : (Stale ? "Update failed" : (LastFetch.Length > 0 ? "Updated " + LastFetch : "Not checked"));
            DrawText(g, update, 112, y, Stale ? Palette.DANGERTXT : Palette.MUTED, 10);
            var rr = new Rectangle(w - 102, 26, 94, 22); Buttons.Add(rr); ButtonActions.Add(() => RefreshData());
            DrawButton(g, rr, "Refresh now", false);
            for (int i = 0; i < 2; i++)
            {
                var a = Accounts[i] ?? new AccountData();
                int cy = y + 24 + i * 84; int cw = w - 16;
                using (var bg = new SolidBrush(Palette.RAISED)) g.FillRectangle(bg, 8, cy, cw, 76);
                DrawBevel(g, 8, cy, cw, 76, false);
                DrawText(g, a.Name ?? "Account " + (i + 1), 14, cy + 6, Palette.LINK, 11, true);
                if (a.Error != null)
                {
                    DrawText(g, "ERROR: " + a.Error, 14, cy + 30, Palette.DANGERTXT, 10);
                    DrawText(g, "stale/error", 14, cy + 50, Palette.MUTED, 10); continue;
                }
                int x1 = 14, x2 = 140, ly = cy + 28;
                DrawText(g, "5h:", x1, ly, Palette.TEXT2, 11);
                DrawText(g, FormatPct(a.FiveHourAvail, a.FiveHourRem), x1 + 28, ly, PctColor(a.FiveHourRem), 11, true);
                DrawText(g, FriendlyTime(a.FiveHourReset), x1 + 28, ly + 16, Palette.MUTED, 10);
                DrawText(g, "Weekly:", x2, ly, Palette.TEXT2, 11);
                DrawText(g, FormatPct(a.WeeklyAvail, a.WeeklyRem), x2 + 55, ly, PctColor(a.WeeklyRem), 11, true);
                DrawText(g, FriendlyTime(a.WeeklyReset), x2 + 55, ly + 16, Palette.MUTED, 10);
            }
            string footer = LastError.Length > 0 ? "Error: " + ShortText(LastError, 52) : "Auto-refresh every " + (Settings.RefreshSeconds / 60) + " min · F5 refresh · Esc hide";
            DrawText(g, footer, 8, h - 20, LastError.Length > 0 ? Palette.DANGERTXT : Palette.MUTED, 10);
        }

        static string ShortText(string value, int max)
        {
            if (string.IsNullOrEmpty(value) || value.Length <= max) return value;
            return value.Substring(0, max - 3) + "...";
        }

        string FormatPct(bool avail, int pct) { return avail ? (pct + "%") : "--"; }
        Color PctColor(int pct)
        {
            if (pct < 10) return Palette.DANGERTXT; if (pct < 30) return Palette.TEXT; return Palette.LINK;
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

        protected override void OnKeyDown(KeyEventArgs e)
        {
            if (e.KeyCode == Keys.F5) { RefreshData(); e.Handled = true; return; }
            if (e.KeyCode == Keys.Escape) { HideToTray(); e.Handled = true; return; }
            base.OnKeyDown(e);
        }

        public void UpdateTray()
        {
            try
            {
                int value; bool available; string metricLabel;
                GetTrayMetric(out value, out available, out metricLabel);
                string tip = "LIMISAW | " + metricLabel + " " + (available ? value + "%" : "--");
                if (Accounts[0] != null && Accounts[0].Ok) tip += " | C1 " + Accounts[0].Abbrev();
                if (Accounts[1] != null && Accounts[1].Ok) tip += " | C2 " + Accounts[1].Abbrev();
                if (Stale) tip += " | stale";
                if (tip.Length > 63) tip = tip.Substring(0, 60) + "...";
                Tray.Text = tip;

                using (Bitmap bmp = new Bitmap(16, 16))
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
                    g.SmoothingMode = SmoothingMode.None; g.InterpolationMode = InterpolationMode.NearestNeighbor;
                    g.PixelOffsetMode = PixelOffsetMode.None;
                    g.Clear(Palette.BG);
                    using (var edge = new Pen(Palette.BEVEL)) g.DrawRectangle(edge, 0, 0, 15, 15);
                    string text = available ? value.ToString() : "--";
                    Color col = Stale ? Palette.MUTED : (available ? (value < 10 ? Palette.DANGERTXT : (value < 30 ? Palette.TEXT : Palette.LINK)) : Palette.MUTED);
                    int size = text.Length >= 3 ? 6 : 8;
                    using (Font f = MakePixelFont(size))
                    using (var br = new SolidBrush(col))
                    {
                        var fmt = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                        g.DrawString(text, f, br, new RectangleF(1, 1, 14, 14), fmt);
                    }
                    IntPtr handle = bmp.GetHicon();
                    try
                    {
                        Icon next;
                        using (Icon borrowed = Icon.FromHandle(handle)) next = (Icon)borrowed.Clone();
                        Icon old = Tray.Icon; Tray.Icon = next;
                        if (old != null) { try { old.Dispose(); } catch { } }
                    }
                    finally { Native.DestroyIcon(handle); }
                }
            }
            catch { }
        }

        void GetTrayMetric(out int value, out bool available, out string label)
        {
            value = 0; available = false; label = Settings.TrayMetric;
            if (Settings.TrayMetric == "lowest")
            {
                string[] ids = { "c1_5h", "c1_week", "c2_5h", "c2_week" };
                int minAny = int.MaxValue; int minPos = int.MaxValue;
                string labelAny = ""; string labelPos = "";
                bool any = false, pos = false;
                foreach (string id in ids)
                {
                    int candidate; bool candidateAvailable; string candidateLabel;
                    GetMetric(id, out candidate, out candidateAvailable, out candidateLabel);
                    if (!candidateAvailable) continue;
                    if (candidate < minAny) { minAny = candidate; labelAny = candidateLabel; any = true; }
                    if (candidate > 0 && candidate < minPos) { minPos = candidate; labelPos = candidateLabel; pos = true; }
                }
                if (pos) { value = minPos; label = labelPos; }
                else if (any) { value = minAny; label = labelAny; }
                available = any;
                return;
            }
            GetMetric(Settings.TrayMetric, out value, out available, out label);
        }

        void GetMetric(string id, out int value, out bool available, out string label)
        {
            value = 0; available = false; label = id;
            int account = id.StartsWith("c2_") ? 1 : 0;
            AccountData a = account < Accounts.Length ? Accounts[account] : null;
            bool weekly = id.EndsWith("week");
            label = "C" + (account + 1) + (weekly ? " weekly" : " 5h");
            if (a == null || !a.Ok) return;
            available = weekly ? a.WeeklyAvail : a.FiveHourAvail;
            value = weekly ? a.WeeklyRem : a.FiveHourRem;
        }

        public string AccountSummary(int index)
        {
            if (index >= Accounts.Length || Accounts[index] == null) return "Account " + (index + 1) + ": not checked";
            AccountData a = Accounts[index];
            if (!a.Ok) return "Account " + (index + 1) + ": unavailable";
            return "Account " + (index + 1) + ": " + a.Abbrev();
        }
    }

    static class AccountExt
    {
        public static string Abbrev(this AccountData a)
        {
            if (a == null || !a.Ok) return "";
            string fh = a.FiveHourAvail ? a.FiveHourRem.ToString() : "--";
            string wk = a.WeeklyAvail ? a.WeeklyRem.ToString() : "--";
            return "5h " + fh + "% · week " + wk + "%";
        }
    }

    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            bool createdNew;
            using (var mutex = new System.Threading.Mutex(true, "Local\\LimisawApp", out createdNew))
            {
                if (!createdNew)
                {
                    try
                    {
                        using (var signal = System.Threading.EventWaitHandle.OpenExisting("Local\\LimisawShow"))
                            signal.Set();
                    }
                    catch
                    {
                        IntPtr existing = Native.FindWindow(null, "LIMISAW");
                        if (existing != IntPtr.Zero) { Native.ShowWindow(existing, 5); Native.SetForegroundWindow(existing); }
                    }
                    return;
                }
                var showEvent = new System.Threading.EventWaitHandle(false,
                    System.Threading.EventResetMode.AutoReset, "Local\\LimisawShow");

                string dir = AppDomain.CurrentDomain.BaseDirectory;
                if (!File.Exists(Path.Combine(dir, "Scripts", "limisaw_probe.py")))
                { string p = Path.GetDirectoryName(dir); if (File.Exists(Path.Combine(p, "Scripts", "limisaw_probe.py"))) dir = p; }
                var s = new LimisawSettings(dir); s.Load();
                Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                NotifyIcon tray = new NotifyIcon(); tray.Visible = true;
                LimisawForm form = null; form = new LimisawForm(dir, s, tray);
                // BeginInvoke from the single-instance signal needs a handle even
                // when Windows starts the application hidden in the tray.
                IntPtr hiddenHandle = form.Handle;
                tray.Text = "LIMISAW";
                Action show = () => { form.Show(); form.Activate(); form.RefreshData(); };
                var showWait = System.Threading.ThreadPool.RegisterWaitForSingleObject(showEvent, (state, timedOut) =>
                {
                    if (form.IsDisposed || !form.IsHandleCreated) return;
                    try { form.BeginInvoke(show); } catch { }
                }, null, System.Threading.Timeout.Infinite, false);
                tray.ContextMenuStrip = BuildMenu(s, tray, () => form, show, () => { form.RefreshData(); });
                tray.DoubleClick += (o, e) => show();
                form.FormClosing += (o, e) => { if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; form.HideToTray(); } };
                form.UpdateTray();
                ApplyAutostart(s);
                bool startHidden = Array.Exists(args, a => string.Equals(a, "--minimized", StringComparison.OrdinalIgnoreCase));
                if (!startHidden) form.Show();
                Application.Run();
                showWait.Unregister(null); showEvent.Dispose(); tray.Dispose();
            }
        }

        static void ApplyAutostart(LimisawSettings s)
        {
            try
            {
                using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                {
                    if (rk == null) return;
                    if (s.AutoStart) rk.SetValue("LimisawApp", "\"" + Application.ExecutablePath + "\" --minimized");
                    else if (rk.GetValue("LimisawApp") != null) rk.DeleteValue("LimisawApp", false);
                }
            }
            catch { }
        }

        static ContextMenuStrip BuildMenu(LimisawSettings s, NotifyIcon tray, Func<LimisawForm> getForm, Action show, Action refresh)
        {
            var m = new ContextMenuStrip();
            m.Items.Add("Open LIMISAW", null, (o, e) => show());
            m.Items.Add("Refresh now", null, (o, e) => refresh());
            m.Items.Add(new ToolStripSeparator());
            var account1 = new ToolStripMenuItem("Account 1: not checked") { Enabled = false };
            var account2 = new ToolStripMenuItem("Account 2: not checked") { Enabled = false };
            m.Items.Add(account1); m.Items.Add(account2);
            m.Items.Add(new ToolStripSeparator());
            var showMenu = new ToolStripMenuItem("Tray number");
            string[] labels = { "Lowest remaining (recommended)", "Account 1 · 5h", "Account 1 · weekly", "Account 2 · 5h", "Account 2 · weekly" };
            string[] values = { "lowest", "c1_5h", "c1_week", "c2_5h", "c2_week" };
            for (int i = 0; i < values.Length; i++)
            {
                string value = values[i];
                var item = new ToolStripMenuItem(labels[i], null, (sender, eArgs) =>
                {
                    s.TrayMetric = value; s.Save();
                    foreach (ToolStripItem child in showMenu.DropDownItems)
                    {
                        var choice = child as ToolStripMenuItem;
                        if (choice != null) choice.Checked = string.Equals((string)choice.Tag, s.TrayMetric, StringComparison.OrdinalIgnoreCase);
                    }
                    getForm().UpdateTray();
                });
                item.Tag = value; item.Checked = s.TrayMetric == value; showMenu.DropDownItems.Add(item);
            }
            m.Items.Add(showMenu);
            m.Items.Add(new ToolStripSeparator());
            var notifyItem = new ToolStripMenuItem("Notify on reset") { Checked = s.NotifyOnReset };
            notifyItem.Click += (o, e) => { notifyItem.Checked = !notifyItem.Checked; s.NotifyOnReset = notifyItem.Checked; s.Save(); };
            m.Items.Add(notifyItem);
            var autoItem = new ToolStripMenuItem("Start with Windows") { Checked = s.AutoStart };
            autoItem.Click += (o, e) => { autoItem.Checked = !autoItem.Checked; s.AutoStart = autoItem.Checked; s.Save(); ApplyAutostart(s); };
            m.Items.Add(autoItem);
            m.Items.Add(new ToolStripSeparator());
            m.Items.Add("Exit", null, (o, e) => { var f = getForm(); f.Close(); try { tray.Visible = false; } catch { } Application.Exit(); });
            m.Opening += (o, e) => { account1.Text = getForm().AccountSummary(0); account2.Text = getForm().AccountSummary(1); };
            return m;
        }
    }
}
