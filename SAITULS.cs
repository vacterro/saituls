using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;

// SAITULS — unified toolkit manager.
// Tabs: Menus | Monitor | Tools | Settings
// UI: saipen UI.md Golden Default. Text is NON-antialiased (pixel text).
// v1 C#. Combines context-menu management, problip monitor, and mini-tools.

namespace Saituls
{
    static class Native
    {
        [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateFont(int nHeight, int nWidth, int nEscapement, int nOrientation,
            int fnWeight, uint fdwItalic, uint fdwUnderline, uint fdwStrikeOut, uint fdwCharSet,
            uint fdwOutputPrecision, uint fdwClipPrecision, uint fdwQuality, uint fdwPitchAndFamily, string lpszFace);
        public const int NONANTIALIASED_QUALITY = 3;
    }

    static class Palette
    {
        public static readonly Color BG = C(0x1A1810);
        public static readonly Color BG_SOFT = C(0x232018);
        public static readonly Color SURFACE = C(0x332E22);
        public static readonly Color RAISED = C(0x3D372A);
        public static readonly Color ALT = C(0x453D30);
        public static readonly Color BDARK = C(0x100E08);
        public static readonly Color BHL = C(0xF0D060);
        public static readonly Color BEVEL = C(0x75663D);
        public static readonly Color BMUTED = C(0x5A5040);
        public static readonly Color TEXT = C(0xD4C89A);
        public static readonly Color TEXT2 = C(0x9C9371);
        public static readonly Color MUTED = C(0x6E674E);
        public static readonly Color TEAL = C(0x008080);
        public static readonly Color TEAL_DEEP = C(0x004C4C);
        public static readonly Color SUCCESS = C(0x4A7A20);
        public static readonly Color WARNING = C(0x7A7A20);
        public static readonly Color DANGER = C(0x7A2020);
        public static readonly Color DANGERTXT = C(0xD66464);
        public static readonly Color SELECTION = C(0x3D372A);
        public static readonly Color COMPARE = C(0x14120C);
        public static readonly Color LINK = C(0xF0D060);
        static Color C(int rgb)
        {
            return Color.FromArgb((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);
        }
    }

    class SaitulsSettings
    {
        public string Dir;
        public string IniPath;
        public string RootPath;
        public string WavPath;
        public string IcoPath;
        public double Volume = 0.05;
        public int MinMs = 4000;
        public int MaxMs = 7000;
        public bool AutoStart = true;
        public bool StartMinimized = false;
        public string LastTab = "Menus";

        public SaitulsSettings(string dir)
        {
            Dir = dir;
            IniPath = Path.Combine(dir, "SAITULS.ini");
            WavPath = Path.Combine(dir, "blip01.wav");
            IcoPath = Path.Combine(dir, "SAITULS.ico");
            RootPath = dir;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern int GetPrivateProfileString(string app, string key, string def, System.Text.StringBuilder buf, int size, string file);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern bool WritePrivateProfileString(string app, string key, string val, string file);

        string Read(string key, string def)
        {
            var sb = new System.Text.StringBuilder(260);
            GetPrivateProfileString("saituls", key, def, sb, sb.Capacity, IniPath);
            return sb.ToString();
        }
        void Write(string key, string val) { WritePrivateProfileString("saituls", key, val, IniPath); }

        public void Load()
        {
            string v;
            v = Read("Volume", "0.05");
            double.TryParse(v, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out Volume);
            if (Volume < 0 || Volume > 1) Volume = 0.05;
            v = Read("MinMs", "4000");
            int.TryParse(v, out MinMs);
            v = Read("MaxMs", "7000");
            int.TryParse(v, out MaxMs);
            if (MaxMs < MinMs) MaxMs = MinMs;
            if (MinMs < 1000) MinMs = 1000;
            if (MaxMs > 60000) MaxMs = 60000;
            AutoStart = Read("AutoStart", "1") == "1";
            StartMinimized = Read("StartMinimized", "0") == "1";
            LastTab = Read("LastTab", "Menus");
            v = Read("RootPath", Dir);
            if (Directory.Exists(v)) RootPath = v;
            if (!File.Exists(IniPath)) Save();
        }

        public void Save()
        {
            var ci = System.Globalization.CultureInfo.InvariantCulture;
            Write("Volume", Volume.ToString("0.00", ci));
            Write("MinMs", MinMs.ToString());
            Write("MaxMs", MaxMs.ToString());
            Write("AutoStart", AutoStart ? "1" : "0");
            Write("StartMinimized", StartMinimized ? "1" : "0");
            Write("LastTab", LastTab);
            Write("RootPath", RootPath);
        }
    }

    class BlipEngine
    {
        SaitulsSettings S;
        System.Media.SoundPlayer Player;
        string CachePath;
        Random Rng = new Random();
        Timer Timer;
        long LastPlayMs = -100000;
        Stopwatch Clock = Stopwatch.StartNew();
        public bool Enabled = false;
        public int MinMs, MaxMs;

        public BlipEngine(SaitulsSettings s)
        {
            S = s;
            MinMs = s.MinMs;
            MaxMs = s.MaxMs;
            Timer = new Timer();
            Timer.Interval = 500;
            Timer.Tick += Tick;
            BuildCache();
        }

        void BuildCache()
        {
            // Every volume change calls Reload -> BuildCache. Dispose the old
            // player and delete its temp wav, else each click leaks a file in
            // %TEMP% and the handle keeps it locked for the whole session.
            try
            {
                if (Player != null) { Player.Dispose(); Player = null; }
                if (CachePath != null && File.Exists(CachePath)) File.Delete(CachePath);
            }
            catch { }
            try
            {
                byte[] src = File.ReadAllBytes(S.WavPath);
                byte[] scaled = ScaleWav(src, S.Volume);
                CachePath = Path.Combine(Path.GetTempPath(), "saituls_" + Guid.NewGuid().ToString("N") + ".wav");
                File.WriteAllBytes(CachePath, scaled);
                Player = new System.Media.SoundPlayer(CachePath);
                Player.Load();
            }
            catch { Player = null; }
        }

        byte[] ScaleWav(byte[] b, double gain)
        {
            if (gain >= 0.999999) return b;
            byte[] outb = (byte[])b.Clone();
            int dataStart = -1, dataLen = 0, fmtBits = 0, fmtPos = 12;
            while (fmtPos + 8 <= b.Length)
            {
                string id = System.Text.Encoding.ASCII.GetString(b, fmtPos, 4);
                int len = BitConverter.ToInt32(b, fmtPos + 4);
                if (len < 0 || fmtPos + 8 + len > b.Length) break;
                if (id == "fmt " && len >= 16) fmtBits = BitConverter.ToUInt16(b, fmtPos + 22);
                else if (id == "data") { dataStart = fmtPos + 8; dataLen = len; break; }
                fmtPos += 8 + len + (len % 2);
            }
            if (dataStart < 0) return b;
            int bps = fmtBits / 8;
            int end = dataStart + dataLen;
            for (int i = dataStart; i + bps <= end; i += bps)
            {
                if (fmtBits == 8)
                {
                    int v = (int)Math.Round((outb[i] - 128) * gain) + 128;
                    outb[i] = (byte)Math.Max(0, Math.Min(255, v));
                }
                else if (fmtBits == 16)
                {
                    short v = BitConverter.ToInt16(outb, i);
                    int n = (int)Math.Round(v * gain);
                    if (n > 32767) n = 32767; else if (n < -32768) n = -32768;
                    outb[i] = (byte)(n & 0xFF); outb[i + 1] = (byte)((n >> 8) & 0xFF);
                }
                else if (fmtBits == 24)
                {
                    int raw = outb[i] | (outb[i + 1] << 8) | (outb[i + 2] << 16);
                    int v = (raw & 0x800000) != 0 ? raw - 0x1000000 : raw;
                    int n = (int)Math.Round(v * gain);
                    if (n > 8388607) n = 8388607; else if (n < -8388608) n = -8388608;
                    n &= 0xFFFFFF;
                    outb[i] = (byte)(n & 0xFF); outb[i + 1] = (byte)((n >> 8) & 0xFF); outb[i + 2] = (byte)((n >> 16) & 0xFF);
                }
                else if (fmtBits == 32)
                {
                    int v = BitConverter.ToInt32(outb, i);
                    long n = (long)Math.Round((double)v * gain);
                    if (n > int.MaxValue) n = int.MaxValue; else if (n < int.MinValue) n = int.MinValue;
                    byte[] tmp = BitConverter.GetBytes((int)n);
                    outb[i] = tmp[0]; outb[i + 1] = tmp[1]; outb[i + 2] = tmp[2]; outb[i + 3] = tmp[3];
                }
            }
            return outb;
        }

        int NextDelay() { if (MinMs >= MaxMs) return MinMs; return Rng.Next(MinMs, MaxMs + 1); }

        void Tick(object sender, EventArgs e)
        {
            if (!Enabled || Player == null) return;
            long now = Clock.ElapsedMilliseconds;
            if (now - LastPlayMs >= 300)
            {
                try { Player.Play(); } catch { }
                LastPlayMs = now;
            }
            Timer.Interval = NextDelay();
        }

        public void Start() { Enabled = true; Timer.Interval = 500; Timer.Start(); }
        public void Stop() { Enabled = false; Timer.Stop(); }
        public void Reload() { Stop(); BuildCache(); Start(); }
        public bool IsOn
        {
            get { return Enabled; }
        }

        // Called on shutdown: the temp wav must not outlive the process.
        public void Dispose()
        {
            try { Stop(); } catch { }
            try { if (Player != null) { Player.Dispose(); Player = null; } } catch { }
            try { if (CachePath != null && File.Exists(CachePath)) File.Delete(CachePath); } catch { }
        }

        // Sweep wavs left behind by earlier runs that were killed before Dispose.
        public static void SweepStaleCaches()
        {
            try
            {
                foreach (string f in Directory.GetFiles(Path.GetTempPath(), "saituls_*.wav"))
                {
                    try { File.Delete(f); } catch { }
                }
            }
            catch { }
        }
    }

    class SaitulsForm : Form
    {
        SaitulsSettings S;
        BlipEngine Engine;
        NotifyIcon Tray;
        string CurrentTab = "Menus";
        List<Rectangle> TabRects = new List<Rectangle>();
        string[] TabNames = { "Menus", "Monitor", "Tools", "Settings" };
        List<ButtonDef> Buttons = new List<ButtonDef>();

        // Registry feature list
        string[] RegFeatures = {
            "COPY_PATH", "DEL_DUP", "DEL_EMPTY", "DEL_JUNK", "DEL_SAME", "DL_YT",
            "FFMPEG_MENU", "MERGE_AUD", "MKV_FIX", "NEW_PROJ", "PACK",
            "PS_ADMIN", "TAKE_OWN", "TOGGLE_HID"
        };
        string[] RegLabels = {
            "Copy Path", "Del Dup", "Del Empty", "Del Junk", "Del Same", "YouTube DL",
            "FFmpeg Menu", "Merge Audio", "MKV Fix", "New Project", "Pack",
            "PS Admin", "Take Own", "Toggle Hidden"
        };
        bool[] FeatureChecked;
        List<Rectangle> MenuCheckRects = new List<Rectangle>();

        // Monitor state
        double[] VolPresets = { 0.01, 0.05, 0.10, 0.33, 0.50, 0.75, 1.00 };
        int[] MinPresets = { 4000, 5000, 10000, 15000, 20000, 30000 };
        int[] MaxPresets = { 7000, 5000, 10000, 15000, 20000, 30000 };
        string[] IntervalLabels = { "4-7s", "5s", "10s", "15s", "20s", "30s" };

        class ButtonDef { public Rectangle R; public Action A; public string Label; public bool Sel; }

        public SaitulsForm(SaitulsSettings s, BlipEngine engine, NotifyIcon tray)
        {
            S = s; Engine = engine; Tray = tray;
            Text = "SAITULS";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(480, 420);
            BackColor = Palette.BG;
            DoubleBuffered = true;
            TopMost = false;
            FeatureChecked = new bool[RegFeatures.Length];
            for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = true;
            try { Icon = File.Exists(s.IcoPath) ? new Icon(s.IcoPath) : SystemIcons.Application; } catch { }
        }

        protected override void WndProc(ref Message m)
        {
            const int WM_NCHITTEST = 0x84;
            const int HTCAPTION = 2;
            base.WndProc(ref m);
            if (m.Msg == WM_NCHITTEST)
            {
                // LParam is a packed POINT in screen coords; on x64 truncating to
                // int is correct because only the low 32 bits carry the point.
                int raw = m.LParam.ToInt32();
                int x = raw & 0xFFFF; if (x > 0x7FFF) x -= 0x10000;
                int y = (raw >> 16) & 0xFFFF; if (y > 0x7FFF) y -= 0x10000;
                Point p = PointToClient(new Point(x, y));
                if (p.Y < 24 && p.X < Width - 20) m.Result = (IntPtr)HTCAPTION;
            }
        }

        static Font MakePixelFont(string name, int pt)
        {
            IntPtr hf = Native.CreateFont(-(int)(pt * 96 / 72), 0, 0, 0, 400,
                0, 0, 0, 1, 0, 0, Native.NONANTIALIASED_QUALITY, 0, name);
            return Font.FromHfont(hf);
        }
        static Font F(int pt)
        {
            return MakePixelFont("Verdana", pt);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
            g.SmoothingMode = SmoothingMode.None;
            g.InterpolationMode = InterpolationMode.NearestNeighbor;
            g.CompositingQuality = CompositingQuality.HighSpeed;
            g.Clear(Palette.BG);
            Buttons.Clear();
            TabRects.Clear();

            // title bar
            using (var b = new SolidBrush(Palette.SURFACE)) g.FillRectangle(b, 0, 0, Width, 24);
            DrawText(g, "SAITULS", 8, 5, Palette.TEXT, 14, true);
            DrawText(g, "v1", 92, 7, Palette.TEXT2, 11);
            string st = Engine.IsOn ? "ON" : "OFF";
            int sw = (int)g.MeasureString(st, F(12)).Width;
            DrawText(g, st, Width - 44 - sw, 5, Engine.IsOn ? Palette.SUCCESS : Palette.MUTED, 12, true);
            var xr = new Rectangle(Width - 22, 2, 20, 20);
            Buttons.Add(new ButtonDef { R = xr, A = () => { Hide(); } });
            DrawText(g, "X", Width - 18, 5, Palette.TEXT2, 12, true);

            // tabs
            int tabY = 28;
            int tabH = 22;
            int tabX = 4;
            for (int i = 0; i < TabNames.Length; i++)
            {
                int tw = (int)g.MeasureString(TabNames[i], F(12)).Width + 20;
                var r = new Rectangle(tabX, tabY, tw, tabH);
                bool sel = CurrentTab == TabNames[i];
                TabRects.Add(r);
                // C# 5 captures the for-variable BY REFERENCE: a lambda closing
                // over i would see i == TabNames.Length after the loop and throw
                // IndexOutOfRange on click. Copy to a per-iteration local.
                string tabName = TabNames[i];
                Buttons.Add(new ButtonDef { R = r, A = () => SwitchTab(tabName), Label = tabName, Sel = sel });
                DrawBevel(g, r, !sel);
                using (var bg = new SolidBrush(sel ? Palette.COMPARE : Palette.RAISED))
                    g.FillRectangle(bg, r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4);
                DrawTextCenter(g, TabNames[i], r, sel ? Palette.LINK : Palette.TEXT, 12, true);
                tabX += tw + 2;
            }

            // content area
            int cy = tabY + tabH + 4;
            var contentRect = new Rectangle(4, cy, Width - 8, Height - cy - 6);
            using (var bg = new SolidBrush(Palette.RAISED))
                g.FillRectangle(bg, contentRect.X, contentRect.Y, contentRect.Width, contentRect.Height);
            DrawBevel(g, contentRect, false);

            using (var clip = new SolidBrush(Palette.RAISED))
            {
                g.SetClip(contentRect);
                DrawContent(g, contentRect);
                g.ResetClip();
            }
        }

        void DrawContent(Graphics g, Rectangle r)
        {
            int x = r.X + 8, y = r.Y + 8, w = r.Width - 16;
            switch (CurrentTab)
            {
                case "Menus": DrawMenusTab(g, x, y, w, r.Height - 16); break;
                case "Monitor": DrawMonitorTab(g, x, y, w, r.Height - 16); break;
                case "Tools": DrawToolsTab(g, x, y, w, r.Height - 16); break;
                case "Settings": DrawSettingsTab(g, x, y, w, r.Height - 16); break;
            }
        }

        void DrawMenusTab(Graphics g, int x, int y, int w, int h)
        {
            MenuCheckRects.Clear();
            DrawText(g, "Context menu features to install:", x, y, Palette.TEXT, 12, true);
            int chkY = y + 20;
            int colW = w / 2 - 10;
            for (int i = 0; i < RegFeatures.Length; i++)
            {
                int cx = x + (i < 7 ? 0 : colW + 20);
                int cy = chkY + (i < 7 ? i : i - 7) * 20;
                var cr = new Rectangle(cx, cy, 14, 14);
                MenuCheckRects.Add(cr);
                DrawBevel(g, cr, false);
                if (FeatureChecked[i])
                {
                    using (var p = new Pen(Palette.LINK, 2))
                    {
                        g.DrawLine(p, cx + 2, cy + 7, cx + 5, cy + 10);
                        g.DrawLine(p, cx + 5, cy + 10, cx + 11, cy + 3);
                    }
                }
                DrawText(g, RegLabels[i], cx + 18, cy + 1, Palette.TEXT, 11);
            }

            int btnY = chkY + 8 * 20 + 12;
            int btnW = 120;
            int btnH = 26;
            // Check All / Uncheck All
            var caR = new Rectangle(x, btnY, btnW, btnH);
            Buttons.Add(new ButtonDef { R = caR, A = () => { for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = true; Refresh(); } });
            DrawButton(g, caR, "Check All", false);

            var uaR = new Rectangle(x + btnW + 8, btnY, btnW, btnH);
            Buttons.Add(new ButtonDef { R = uaR, A = () => { for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = false; Refresh(); } });
            DrawButton(g, uaR, "Uncheck All", false);

            // Install / Remove
            int btnY2 = btnY + btnH + 8;
            var inR = new Rectangle(x, btnY2, btnW, btnH);
            Buttons.Add(new ButtonDef { R = inR, A = () => InstallSelected() });
            DrawButton(g, inR, "Install", false);

            var rmR = new Rectangle(x + btnW + 8, btnY2, btnW, btnH);
            Buttons.Add(new ButtonDef { R = rmR, A = () => UninstallSelected() });
            DrawButton(g, rmR, "Remove", false);
        }

        void DrawMonitorTab(Graphics g, int x, int y, int w, int h)
        {
            DrawText(g, "Volume %:", x, y, Palette.TEXT2, 11);
            int xv = x + 70;
            for (int i = 0; i < VolPresets.Length; i++)
            {
                double v = VolPresets[i];
                bool sel = Math.Abs(v - S.Volume) < 0.0001;
                string lbl = (v * 100).ToString("0");
                int bw = TextW(g, lbl, 10) + 12;
                var r = new Rectangle(xv, y, bw, 22);
                double vc = v;
                Buttons.Add(new ButtonDef { R = r, A = () => SetVolume(vc), Sel = sel });
                DrawButton(g, r, lbl, sel, 10);
                xv += bw + 3;
            }

            int yi = y + 30;
            DrawText(g, "Interval:", x, yi, Palette.TEXT2, 11);
            int xi = x + 70;
            for (int i = 0; i < MinPresets.Length; i++)
            {
                int mn = MinPresets[i], mx = MaxPresets[i];
                bool sel = S.MinMs == mn && S.MaxMs == mx;
                var r = new Rectangle(xi, yi, 48, 22);
                int cmn = mn, cmx = mx;
                Buttons.Add(new ButtonDef { R = r, A = () => ApplyRange(cmn, cmx), Sel = sel });
                DrawButton(g, r, IntervalLabels[i], sel, 10);
                xi += 52;
            }

            int yb = yi + 34;
            bool ao = AutoStartEnabled("SaitulsMonitor");
            var ar = new Rectangle(x, yb, 130, 22);
            Buttons.Add(new ButtonDef { R = ar, A = () => ToggleAutostart("SaitulsMonitor", "SAITULS Monitor") });
            DrawButton(g, ar, ao ? "[X] autostart" : "[ ] autostart", ao);

            var sr = new Rectangle(x + 138, yb, 60, 22);
            Buttons.Add(new ButtonDef { R = sr, A = () => { Engine.Start(); Refresh(); } });
            DrawButton(g, sr, "ON", Engine.IsOn);

            var pr = new Rectangle(x + 204, yb, 60, 22);
            Buttons.Add(new ButtonDef { R = pr, A = () => { Engine.Stop(); Refresh(); } });
            DrawButton(g, pr, "OFF", !Engine.IsOn);

            int yb2 = yb + 30;
            DrawText(g, "Status: " + (Engine.IsOn ? "blip active" : "stopped"), x, yb2, Engine.IsOn ? Palette.SUCCESS : Palette.MUTED, 11);
        }

        // label | script | argument mode
        //   "none"   = no target needed
        //   "folder" = ask for a folder, pass it as %1
        //   "file"   = ask for a file, pass it as %1
        string[,] ToolDefs = {
            { "DL_YT audio",  "DL_YT.CMD",        "audio"  },
            { "DL_YT video",  "DL_YT.CMD",        "video"  },
            { "Merge Audio",  "MERGE_AUD.CMD",    "file"   },
            { "New Project",  "NEW_PROJ.CMD",     "folder" },
            { "Pack File",    "PACK.PYW",         "file"   },
            { "Del Empty",    "DEL_EMPTY.PYW",    "folder" },
            { "Del Dup",      "DEL_DUP.PYW",      "folder" },
            { "Del Same",     "DEL_SAME.PYW",     "folder" },
            { "Del Junk",     "DEL_JUNK.PYW",     "folder" },
            { "PS Admin",     "powershell.exe",   "none"   },
        };

        void DrawToolsTab(Graphics g, int x, int y, int w, int h)
        {
            DrawText(g, "Launch a bundled tool. * asks for a target first.", x, y, Palette.TEXT2, 11);
            int btnW = 110, btnH = 26, gap = 4, cols = 3;
            int cx = x, cy = y + 18;
            for (int i = 0; i < ToolDefs.GetLength(0); i++)
            {
                string lbl = ToolDefs[i, 0];
                string script = ToolDefs[i, 1];
                string mode = ToolDefs[i, 2];
                if (mode == "folder" || mode == "file") lbl += " *";
                var r = new Rectangle(cx, cy, btnW, btnH);
                string sCopy = script, mCopy = mode;
                Buttons.Add(new ButtonDef { R = r, A = () => LaunchTool(sCopy, mCopy) });
                DrawButton(g, r, lbl, false, 10);
                cx += btnW + gap;
                if ((i + 1) % cols == 0) { cx = x; cy += btnH + gap; }
            }
        }

        void DrawSettingsTab(Graphics g, int x, int y, int w, int h)
        {
            DrawText(g, "Toolkit root:", x, y, Palette.TEXT2, 11);
            DrawText(g, S.RootPath, x + 90, y, Palette.TEXT, 11);

            DrawText(g, "INI:", x, y + 18, Palette.TEXT2, 11);
            DrawText(g, S.IniPath, x + 35, y + 18, Palette.MUTED, 10);

            bool ao = AutoStartEnabled("SaitulsApp");
            var ar = new Rectangle(x, y + 40, 130, 22);
            Buttons.Add(new ButtonDef { R = ar, A = () => ToggleAutostart("SaitulsApp", "SAITULS") });
            DrawButton(g, ar, ao ? "[X] autostart app" : "[ ] autostart app", ao);

            int by = y + 70;
            int bw = 140;
            var br = new Rectangle(x, by, bw, 26);
            Buttons.Add(new ButtonDef { R = br, A = () => { Engine.Stop(); Application.Exit(); } });
            DrawButton(g, br, "Exit", false);

            var ber = new Rectangle(x + bw + 8, by, bw, 26);
            Buttons.Add(new ButtonDef { R = ber, A = () => {
                Process.Start("explorer.exe", S.RootPath);
            } });
            DrawButton(g, ber, "Open folder", false);

            DrawText(g, "SAITULS v1 - " + RegFeatures.Length + " context features", x, by + 34, Palette.MUTED, 10);
            DrawText(g, "Golden Default per saipen UI.md", x, by + 48, Palette.MUTED, 10);
        }

        string ImportSafePath()
        {
            return Path.Combine(S.RootPath, "Registry", "IMPORT_SAFE.PS1");
        }

        void InstallSelected()
        {
            List<string> selected = new List<string>();
            for (int i = 0; i < RegFeatures.Length; i++)
                if (FeatureChecked[i]) selected.Add(RegFeatures[i]);
            if (selected.Count == 0) { MessageBox.Show("Select at least one feature.", "SAITULS"); return; }
            string safe = ImportSafePath();
            if (!File.Exists(safe)) { MessageBox.Show("IMPORT_SAFE.PS1 not found:\n" + safe, "SAITULS"); return; }
            // One elevated powershell that imports every selected feature.
            var sb = new System.Text.StringBuilder();
            foreach (string f in selected)
            {
                string reg = Path.Combine(S.RootPath, "Registry", f + ".REG");
                if (File.Exists(reg))
                    sb.Append("& '").Append(safe.Replace("'", "''")).Append("' -File '").Append(reg.Replace("'", "''")).AppendLine("'");
            }
            if (sb.Length > 0) RunElevatedScript(sb.ToString());
        }

        void UninstallSelected()
        {
            List<string> selected = new List<string>();
            for (int i = 0; i < RegFeatures.Length; i++)
                if (FeatureChecked[i]) selected.Add(RegFeatures[i]);
            if (selected.Count == 0) { MessageBox.Show("Select at least one feature.", "SAITULS"); return; }
            string safe = ImportSafePath();
            if (!File.Exists(safe)) { MessageBox.Show("IMPORT_SAFE.PS1 not found:\n" + safe, "SAITULS"); return; }
            var sb = new System.Text.StringBuilder();
            foreach (string f in selected)
            {
                string reg = Path.Combine(S.RootPath, "Registry", f + "_REM.REG");
                if (File.Exists(reg))
                    sb.Append("& '").Append(safe.Replace("'", "''")).Append("' -File '").Append(reg.Replace("'", "''")).AppendLine("'");
            }
            if (sb.Length > 0) RunElevatedScript(sb.ToString());
        }

        void RunElevatedScript(string scriptBody)
        {
            string tmp = null;
            try
            {
                // Write statements to a temp .ps1, then elevate ONE powershell via
                // ShellExecute Verb=runas — no nested quoting hops at all.
                // The trailing pause keeps the result readable: a window that
                // vanishes makes silent success indistinguishable from failure.
                tmp = Path.Combine(Path.GetTempPath(), "saituls_" + Guid.NewGuid().ToString("N") + ".ps1");
                File.WriteAllText(tmp,
                    scriptBody +
                    "\r\nWrite-Host ''\r\nWrite-Host 'Done. Press Enter to close.'\r\n[void][System.Console]::ReadLine()\r\n",
                    new System.Text.UTF8Encoding(false));
                var proc = Process.Start(new ProcessStartInfo("powershell.exe")
                {
                    Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + tmp + "\"",
                    Verb = "runas",
                    UseShellExecute = true,
                    WindowStyle = ProcessWindowStyle.Normal
                });
                if (proc != null)
                {
                    string cleanup = tmp;
                    proc.EnableRaisingEvents = true;
                    proc.Exited += delegate(object o, EventArgs e)
                    {
                        try { if (File.Exists(cleanup)) File.Delete(cleanup); } catch { }
                    };
                }
            }
            catch (System.ComponentModel.Win32Exception)
            {
                // ERROR_CANCELLED (1223) = user dismissed the UAC prompt.
                try { if (tmp != null && File.Exists(tmp)) File.Delete(tmp); } catch { }
                MessageBox.Show("Elevation was cancelled. Nothing was changed.", "SAITULS");
            }
            catch (Exception ex)
            {
                try { if (tmp != null && File.Exists(tmp)) File.Delete(tmp); } catch { }
                MessageBox.Show("Could not start the elevated import:\n" + ex.Message, "SAITULS");
            }
        }

        void LaunchTool(string script, string mode)
        {
            string root = S.RootPath;
            string binDir = Path.Combine(root, "Bin");

            if (script == "powershell.exe")
            {
                try
                {
                    Process.Start(new ProcessStartInfo("powershell.exe", "-NoExit -Command \"Set-Location -LiteralPath '" + root.Replace("'", "''") + "'\"")
                    {
                        Verb = "runas", UseShellExecute = true
                    });
                }
                catch (System.ComponentModel.Win32Exception) { }
                return;
            }

            string fullPath = Path.Combine(root, "Scripts", script);
            if (!File.Exists(fullPath))
            {
                MessageBox.Show("Tool not found:\n" + fullPath, "SAITULS");
                return;
            }

            // The context-menu versions receive %1 from Explorer. Launched from
            // here there is no selection, so ask — a tool silently operating on
            // Bin\ would be a surprise, and DEL_* delete things.
            string target = null;
            if (mode == "folder")
            {
                using (var fb = new FolderBrowserDialog())
                {
                    fb.Description = script + " — pick the target folder";
                    fb.SelectedPath = root;
                    if (fb.ShowDialog() != DialogResult.OK) return;
                    target = fb.SelectedPath;
                }
            }
            else if (mode == "file")
            {
                using (var of = new OpenFileDialog())
                {
                    of.Title = script + " — pick the target file";
                    of.InitialDirectory = root;
                    if (of.ShowDialog() != DialogResult.OK) return;
                    target = of.FileName;
                }
            }

            try
            {
                if (script.EndsWith(".PYW", StringComparison.OrdinalIgnoreCase))
                {
                    // Same interpreter the registry commands use.
                    string pyw = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "pyw.exe");
                    string exe = File.Exists(pyw) ? pyw : "pythonw.exe";
                    string args = "\"" + fullPath + "\"" + (target != null ? " \"" + target + "\"" : "");
                    Process.Start(new ProcessStartInfo(exe, args) { WorkingDirectory = binDir, UseShellExecute = false });
                }
                else
                {
                    // PATH must carry Bin\ and Bin\App\ so bare yt-dlp/ffmpeg resolve.
                    string args = "/c set \"PATH=" + binDir + ";" + Path.Combine(binDir, "App") + ";%PATH%\" && call \"" + fullPath + "\"" +
                                  (mode == "audio" || mode == "video" ? " " + mode : (target != null ? " \"" + target + "\"" : ""));
                    Process.Start(new ProcessStartInfo("cmd.exe", args) { WorkingDirectory = binDir, UseShellExecute = false });
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not launch " + script + ":\n" + ex.Message, "SAITULS");
            }
        }

        void SwitchTab(string name)
        {
            CurrentTab = name;
            S.LastTab = name;
            S.Save();
            Refresh();
        }

        void SetVolume(double v)
        {
            S.Volume = v;
            S.Save();
            Engine.Reload();
            Refresh();
        }

        void ApplyRange(int mn, int mx)
        {
            S.MinMs = mn; S.MaxMs = mx;
            S.Save();
            Engine.MinMs = mn; Engine.MaxMs = mx;
            Refresh();
        }

        bool AutoStartEnabled(string keyName)
        {
            try
            {
                using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
                    return rk != null && rk.GetValue(keyName) != null;
            }
            catch { return false; }
        }

        void ToggleAutostart(string keyName, string displayName)
        {
            try
            {
                bool nowOn;
                using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                {
                    if (rk == null) return;
                    if (AutoStartEnabled(keyName)) { rk.DeleteValue(keyName, false); nowOn = false; }
                    else { rk.SetValue(keyName, "\"" + Application.ExecutablePath + "\""); nowOn = true; }
                }
                // The app-level toggle MUST persist to the ini: Main() re-applies
                // s.AutoStart on every start, so a registry-only change would be
                // silently reverted on the next launch.
                if (keyName == "SaitulsApp")
                {
                    S.AutoStart = nowOn;
                    S.Save();
                }
                Refresh();
            }
            catch { }
        }

        void DrawText(Graphics g, string s, int x, int y, Color c, int pt, bool bold = false)
        {
            using (Font f = F(pt)) using (var br = new SolidBrush(c)) g.DrawString(s, f, br, (float)x, (float)y);
        }

        void DrawTextCenter(Graphics g, string s, Rectangle r, Color c, int pt, bool bold = false)
        {
            using (Font f = F(pt))
            using (var br = new SolidBrush(c))
            {
                var sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                g.DrawString(s, f, br, new RectangleF(r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4), sf);
            }
        }

        void DrawBevel(Graphics g, Rectangle r, bool raised)
        {
            Color hi = raised ? Palette.BEVEL : Palette.BDARK;
            Color lo = raised ? Palette.BDARK : Palette.BEVEL;
            using (var p1 = new Pen(hi)) g.DrawRectangle(p1, r.X, r.Y, r.Width - 1, r.Height - 1);
            using (var p2 = new Pen(lo)) g.DrawRectangle(p2, r.X + 1, r.Y + 1, r.Width - 3, r.Height - 3);
        }

        void DrawButton(Graphics g, Rectangle r, string label, bool selected, int pt = 12)
        {
            using (var bg = new SolidBrush(selected ? Palette.COMPARE : Palette.RAISED))
                g.FillRectangle(bg, r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4);
            DrawBevel(g, r, !selected);
            var fmt = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
            using (Font f = F(pt))
            using (var br = new SolidBrush(selected ? Palette.LINK : Palette.TEXT))
                g.DrawString(label, f, br, new RectangleF(r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4), fmt);
        }

        int TextW(Graphics g, string s, int pt)
        {
            using (Font f = F(pt)) return (int)Math.Ceiling(g.MeasureString(s, f).Width);
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button == MouseButtons.Left)
            {
                // Check menuitem checkboxes (hit-test against rects recorded at draw time)
                if (CurrentTab == "Menus")
                {
                    for (int i = 0; i < MenuCheckRects.Count && i < RegFeatures.Length; i++)
                    {
                        if (MenuCheckRects[i].Contains(e.Location))
                        {
                            FeatureChecked[i] = !FeatureChecked[i];
                            Refresh();
                            return;
                        }
                    }
                }
                foreach (var b in Buttons)
                {
                    if (b.R.Contains(e.Location) && b.A != null) { b.A(); return; }
                }
                // Tab click
                for (int i = 0; i < TabRects.Count; i++)
                {
                    if (TabRects[i].Contains(e.Location))
                    {
                        CurrentTab = TabNames[i];
                        S.LastTab = TabNames[i];
                        S.Save();
                        Refresh();
                        return;
                    }
                }
            }
        }
    }

    static class Program
    {
        static SaitulsForm _form;

        [STAThread]
        static void Main()
        {
            bool createdNew;
            using (var mutex = new System.Threading.Mutex(true, "Local\\SaitulsApp", out createdNew))
            {
                if (!createdNew) return;
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);

                string dir = AppDomain.CurrentDomain.BaseDirectory;
                // Check if running from SAITULS root (not Bin\)
                if (!File.Exists(Path.Combine(dir, "SAITULS.ini")))
                {
                    string parent = Path.GetDirectoryName(dir);
                    if (File.Exists(Path.Combine(parent, "SAITULS.ini")))
                        dir = parent;
                }

                SaitulsSettings s = new SaitulsSettings(dir);
                s.Load();

                // Clear wavs left by runs that were killed before shutdown.
                BlipEngine.SweepStaleCaches();

                BlipEngine engine = new BlipEngine(s);
                engine.Start();

                NotifyIcon tray = new NotifyIcon();
                tray.Icon = File.Exists(s.IcoPath) ? new Icon(s.IcoPath) : SystemIcons.Application;
                tray.Text = "SAITULS";
                ContextMenuStrip menu = new ContextMenuStrip();
                menu.Items.Add("Open SAITULS", null, (o, e) => ShowForm(s, engine, tray));
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add("Monitor ON", null, (o, e) => { engine.Start(); RefreshForm(); });
                menu.Items.Add("Monitor OFF", null, (o, e) => { engine.Stop(); RefreshForm(); });
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add("Exit", null, (o, e) =>
                {
                    engine.Dispose();
                    tray.Visible = false;
                    Application.Exit();
                });
                tray.ContextMenuStrip = menu;
                tray.Visible = true;
                tray.DoubleClick += (o, e) => ShowForm(s, engine, tray);

                try
                {
                    using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                    {
                        if (rk != null)
                        {
                            if (s.AutoStart)
                                rk.SetValue("SaitulsApp", "\"" + Application.ExecutablePath + "\"");
                            else if (rk.GetValue("SaitulsApp") != null)
                                rk.DeleteValue("SaitulsApp", false);
                        }
                    }
                }
                catch { }

                ShowForm(s, engine, tray);
                Application.Run();
                engine.Dispose();
                tray.Visible = false;
                tray.Dispose();
            }
        }

        // Tray Monitor ON/OFF changes engine state; the open panel must redraw
        // or its ON/OFF badge lies until the next click.
        static void RefreshForm()
        {
            try { if (_form != null && !_form.IsDisposed && _form.Visible) _form.Refresh(); }
            catch { }
        }

        static void ShowForm(SaitulsSettings s, BlipEngine engine, NotifyIcon tray)
        {
            if (_form == null || _form.IsDisposed)
            {
                _form = new SaitulsForm(s, engine, tray);
                _form.FormClosing += (o, e) =>
                {
                    if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; _form.Hide(); }
                };
            }
            _form.Show();
            _form.Activate();
        }
    }
}