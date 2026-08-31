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

namespace Limisaw
{
    static class Native
    {
        [DllImport("gdi32.dll")] public static extern IntPtr CreateFont(int nHeight, int nWidth, int nEscapement, int nOrientation,
            int fnWeight, uint fdwItalic, uint fdwUnderline, uint fdwStrikeOut, uint fdwCharSet,
            uint fdwOutputPrecision, uint fdwClipPrecision, uint fdwQuality, uint fdwPitchAndFamily, string lpszFace);
        public const int NONANTIALIASED_QUALITY = 3;
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

    class AccountData
    {
        public int Index;
        public string Name;
        public bool Ok;
        public string Error;
        public bool FiveHourAvail, WeeklyAvail;
        public int FiveHourRem, WeeklyRem;
        public string FiveHourReset, WeeklyReset;
    }

    class LimisawForm : Form
    {
        string RootPath;
        AccountData[] Accounts = new AccountData[2];
        string LastFetch = "";
        bool Stale = false;
        int ConsecutiveFails = 0;
        Timer RefreshTimer;
        bool Refreshing = false;
        List<Rectangle> Buttons = new List<Rectangle>();
        List<Action> ButtonActions = new List<Action>();

        static Font MakePixelFont(int pt)
        {
            IntPtr hf = Native.CreateFont(-(int)(pt * 96 / 72), 0, 0, 0, 400,
                0, 0, 0, 1, 0, 0, Native.NONANTIALIASED_QUALITY, 0, "Verdana");
            return Font.FromHfont(hf);
        }
        static Font F(int pt)
        {
            return MakePixelFont(pt);
        }

        public LimisawForm(string root)
        {
            RootPath = root;
            Text = "LIMISAW";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(380, 260);
            BackColor = Palette.BG;
            DoubleBuffered = true;
            TopMost = false;
            RefreshTimer = new Timer { Interval = 180000 };
            RefreshTimer.Tick += (o, e) => RefreshData();
            RefreshTimer.Start();
            RefreshData();
        }

        protected override void WndProc(ref Message m)
        {
            const int WM_NCHITTEST = 0x84;
            const int HTCAPTION = 2;
            base.WndProc(ref m);
            if (m.Msg == WM_NCHITTEST)
            {
                int raw = m.LParam.ToInt32();
                int x = raw & 0xFFFF; if (x > 0x7FFF) x -= 0x10000;
                int y = (raw >> 16) & 0xFFFF; if (y > 0x7FFF) y -= 0x10000;
                Point p = PointToClient(new Point(x, y));
                if (p.Y < 22 && p.X < Width - 20) m.Result = (IntPtr)HTCAPTION;
            }
        }

        void RefreshData()
        {
            if (Refreshing) return;
            Refreshing = true;
            System.Threading.ThreadPool.QueueUserWorkItem(_ =>
            {
                try
                {
                    string probe = Path.Combine(RootPath, "Scripts", "limisaw_probe.py");
                    string python = "python.exe";
                    string args = "\"" + probe + "\" --json";

                    var psi = new ProcessStartInfo(python, args)
                    {
                        RedirectStandardOutput = true,
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        WorkingDirectory = RootPath,
                    };
                    var proc = Process.Start(psi);
                    if (proc == null) { SetError("could not start python"); return; }
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
            ConsecutiveFails++;
            Stale = true;
            Refresh();
        }

        void ParseAndUpdate(string json)
        {
            try
            {
                var ser = new JavaScriptSerializer();
                var root = ser.Deserialize<Dictionary<string, object>>(json);
                var accounts = root["accounts"] as ArrayList;
                int i = 0;
                foreach (var aObj in accounts)
                {
                    var a = aObj as Dictionary<string, object>;
                    var ad = new AccountData();
                    ad.Index = (int)a["index"];
                    ad.Name = (string)a["name"];
                    ad.Ok = (bool)a["ok"];
                    ad.Error = a.ContainsKey("error") ? a["error"] as string : null;

                    var fh = a["five_hour"] as Dictionary<string, object>;
                    if (fh != null)
                    {
                        ad.FiveHourAvail = (bool)fh["available"];
                        if (fh.ContainsKey("remaining_percent") && fh["remaining_percent"] != null)
                            ad.FiveHourRem = (int)Math.Round(Convert.ToDouble(fh["remaining_percent"]));
                        if (fh.ContainsKey("resets_at") && fh["resets_at"] != null)
                            ad.FiveHourReset = (string)fh["resets_at"];
                    }
                    var wk = a["weekly"] as Dictionary<string, object>;
                    if (wk != null)
                    {
                        ad.WeeklyAvail = (bool)wk["available"];
                        if (wk.ContainsKey("remaining_percent") && wk["remaining_percent"] != null)
                            ad.WeeklyRem = (int)Math.Round(Convert.ToDouble(wk["remaining_percent"]));
                        if (wk.ContainsKey("resets_at") && wk["resets_at"] != null)
                            ad.WeeklyReset = (string)wk["resets_at"];
                    }
                    if (i < Accounts.Length) Accounts[i] = ad;
                    i++;
                }
                ConsecutiveFails = 0;
                Stale = false;
                LastFetch = DateTime.Now.ToString("HH:mm:ss");
            }
            catch (Exception ex)
            {
                ConsecutiveFails++;
                Stale = true;
                var _ = ex;
            }
            finally
            {
                Refreshing = false;
                try { BeginInvoke((Action)Refresh); } catch { }
            }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
            g.SmoothingMode = SmoothingMode.None;
            g.InterpolationMode = InterpolationMode.NearestNeighbor;
            g.Clear(Palette.BG);
            Buttons.Clear(); ButtonActions.Clear();

            int w = Width, h = Height;
            using (var b = new SolidBrush(Palette.SURFACE)) g.FillRectangle(b, 0, 0, w, 22);
            DrawText(g, "LIMISAW", 6, 3, Palette.TEXT, 12, true);
            string st = Stale ? "STALE" : "OK";
            Color stCol = Stale ? Palette.DANGERTXT : Palette.SUCCESS;
            DrawText(g, st, w - 60, 3, stCol, 12);
            var xr = new Rectangle(w - 22, 1, 20, 20);
            Buttons.Add(xr); ButtonActions.Add(() => { Hide(); });
            DrawText(g, "X", w - 18, 3, Palette.TEXT2, 12);

            int y = 30;
            DrawText(g, "Codex rate limits", 8, y, Palette.TEXT2, 11);
            DrawText(g, LastFetch, 140, y, Palette.MUTED, 10);

            for (int i = 0; i < 2; i++)
            {
                var a = Accounts[i] ?? new AccountData();
                int cy = y + 20 + i * 90;
                int cw = w - 16;
                using (var bg = new SolidBrush(Palette.RAISED))
                    g.FillRectangle(bg, 8, cy, cw, 80);
                DrawBevel(g, 8, cy, cw, 80, false);

                DrawText(g, a.Name ?? "Account " + (i + 1), 14, cy + 6, Palette.LINK, 11, true);

                if (a.Error != null)
                {
                    DrawText(g, "ERROR: " + a.Error, 14, cy + 30, Palette.DANGERTXT, 10);
                    DrawText(g, "stale/error", 14, cy + 50, Palette.MUTED, 10);
                    continue;
                }

                int x1 = 14, x2 = 140;
                int ly = cy + 28;
                DrawText(g, "5h:", x1, ly, Palette.TEXT2, 11);
                DrawText(g, FormatPct(a.FiveHourAvail, a.FiveHourRem), x1 + 28, ly, PctColor(a.FiveHourRem), 11, true);
                DrawText(g, a.FiveHourReset ?? "--", x1 + 28, ly + 16, a.FiveHourAvail ? Palette.MUTED : Palette.MUTED, 10);

                DrawText(g, "Weekly:", x2, ly, Palette.TEXT2, 11);
                DrawText(g, FormatPct(a.WeeklyAvail, a.WeeklyRem), x2 + 55, ly, PctColor(a.WeeklyRem), 11, true);
                DrawText(g, a.WeeklyReset ?? "--", x2 + 55, ly + 16, Palette.MUTED, 10);

                int by = cy + 56;
                var br = new Rectangle(14, by, 60, 22);
                Buttons.Add(br); ButtonActions.Add(() => RefreshData());
                DrawButton(g, br, "Refresh", false);
            }

            DrawText(g, "Auto-refresh: 3 min", 8, h - 20, Palette.MUTED, 10);
        }

        string FormatPct(bool avail, int pct)
        {
            return avail ? (pct + "%") : "--";
        }
        Color PctColor(int pct)
        {
            if (pct < 10) return Palette.DANGERTXT;
            if (pct < 30) return Palette.WARNING;
            return Palette.SUCCESS;
        }

        void DrawText(Graphics g, string s, int x, int y, Color c, int pt, bool bold = false)
        {
            using (Font f = F(pt)) using (var br = new SolidBrush(c)) g.DrawString(s, f, br, (float)x, (float)y);
        }

        void DrawBevel(Graphics g, int x, int y, int w, int h, bool raised)
        {
            Color hi = raised ? Palette.BEVEL : Palette.BDARK;
            Color lo = raised ? Palette.BDARK : Palette.BEVEL;
            using (var p1 = new Pen(hi)) g.DrawRectangle(p1, x, y, w - 1, h - 1);
            using (var p2 = new Pen(lo)) g.DrawRectangle(p2, x + 1, y + 1, w - 3, h - 3);
        }

        void DrawButton(Graphics g, Rectangle r, string label, bool selected)
        {
            using (var bg = new SolidBrush(selected ? Palette.ALT : Palette.RAISED))
                g.FillRectangle(bg, r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4);
            DrawBevel(g, r.X, r.Y, r.Width, r.Height, !selected);
            var fmt = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
            using (Font f = F(10)) using (var br = new SolidBrush(Palette.TEXT))
                g.DrawString(label, f, br, new RectangleF(r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4), fmt);
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            for (int i = 0; i < Buttons.Count; i++)
            {
                if (Buttons[i].Contains(e.Location))
                { ButtonActions[i](); return; }
            }
        }
    }

    static class Program
    {
        [STAThread]
        static void Main()
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory;
            if (!File.Exists(Path.Combine(dir, "Scripts", "limisaw_probe.py")))
            {
                string parent = Path.GetDirectoryName(dir);
                if (File.Exists(Path.Combine(parent, "Scripts", "limisaw_probe.py")))
                    dir = parent;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            var form = new LimisawForm(dir);
            Application.Run(form);
        }
    }
}