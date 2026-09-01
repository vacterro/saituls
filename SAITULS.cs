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
// Tabs: Home | Explorer menus | Tools | Settings
// UI: saipen UI.md Golden Default. Text is NON-antialiased (pixel text).
// v1 C#. Launcher hub: context-menu install + tool launcher + settings.

namespace Saituls
{
    static class Native
    {
        [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string className, string windowName);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateFont(int nHeight, int nWidth, int nEscapement, int nOrientation,
            int fnWeight, uint fdwItalic, uint fdwUnderline, uint fdwStrikeOut, uint fdwCharSet,
            uint fdwOutputPrecision, uint fdwClipPrecision, uint fdwQuality, uint fdwPitchAndFamily, string lpszFace);
        [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr handle);
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
        public string IcoPath;
        public bool AutoStart = false;
        public string LastTab = "Home";

        public SaitulsSettings(string dir)
        {
            Dir = dir;
            IniPath = Path.Combine(dir, "SAITULS.ini");
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
            AutoStart = Read("AutoStart", "0") == "1";
            LastTab = Read("LastTab", "Home");
            v = Read("RootPath", Dir);
            if (Directory.Exists(v)) RootPath = v;
            if (!File.Exists(IniPath)) Save();
        }

        public void Save()
        {
            var ci = System.Globalization.CultureInfo.InvariantCulture;
            Write("AutoStart", AutoStart ? "1" : "0");
            Write("LastTab", LastTab);
            Write("RootPath", RootPath);
        }
    }


    class SaitulsForm : Form
    {
        SaitulsSettings S;
        NotifyIcon Tray;
        string CurrentTab = "Home";
        List<Rectangle> TabRects = new List<Rectangle>();
        string[] TabNames = { "Home", "Explorer menus", "Tools", "Settings" };
        List<ButtonDef> Buttons = new List<ButtonDef>();
        int FocusedButton = -1;

        // Registry feature list
        string[] RegFeatures = {
            "COPY_PATH", "DEL_DUP", "DEL_EMPTY", "DEL_JUNK", "DEL_SAME", "DL_YT",
            "FFMPEG_MENU", "MERGE_AUD", "MKV_FIX", "NEW_PROJ", "PACK",
            "PS_ADMIN", "TAKE_OWN", "TOGGLE_HID"
        };
        string[] RegLabels = {
            "Copy file path", "Delete duplicates", "Delete empty folders", "Delete junk", "Delete same-name items", "Download YouTube",
            "FFmpeg actions", "Merge audio tracks", "Repair MKV", "Create project folders", "Pack file",
            "PowerShell as admin", "Take ownership", "Show / hide files"
        };
        bool[] FeatureChecked;
        List<Rectangle> MenuCheckRects = new List<Rectangle>();

        class ButtonDef { public Rectangle R; public Action A; public string Label; public bool Sel; }

        public SaitulsForm(SaitulsSettings s, NotifyIcon tray)
        {
            S = s; Tray = tray;
            Text = "SAITULS";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(560, 440);
            BackColor = Palette.BG;
            DoubleBuffered = true;
            TopMost = false;
            KeyPreview = true;
            FeatureChecked = new bool[RegFeatures.Length];
            for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = true;
            if (Array.IndexOf(TabNames, S.LastTab) >= 0) CurrentTab = S.LastTab;
            try { Icon = File.Exists(s.IcoPath) ? new Icon(s.IcoPath) : SystemIcons.Application; } catch { }
            Activated += (o, e) => Refresh();
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
                if (p.X >= 0 && p.Y >= 0 && p.Y < 24 && p.X < Width - 20) m.Result = (IntPtr)HTCAPTION;
            }
        }

        static Font MakePixelFont(string name, int pt)
        {
            IntPtr hf = Native.CreateFont(-(int)(pt * 96 / 72), 0, 0, 0, 400,
                0, 0, 0, 1, 0, 0, Native.NONANTIALIASED_QUALITY, 0, name);
            try { using (Font wrapped = Font.FromHfont(hf)) return (Font)wrapped.Clone(); }
            finally { if (hf != IntPtr.Zero) Native.DeleteObject(hf); }
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
            DrawText(g, "desktop toolkit", 88, 7, Palette.TEXT2, 10);
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
            if (FocusedButton >= 0 && FocusedButton < Buttons.Count)
            {
                Rectangle focus = Buttons[FocusedButton].R; focus.Inflate(-3, -3);
                using (var pen = new Pen(Palette.TEXT)) { pen.DashStyle = DashStyle.Dot; g.DrawRectangle(pen, focus); }
            }
        }

        void DrawContent(Graphics g, Rectangle r)
        {
            int x = r.X + 8, y = r.Y + 8, w = r.Width - 16;
            switch (CurrentTab)
            {
                case "Home": DrawHomeTab(g, x, y, w, r.Height - 16); break;
                case "Explorer menus": DrawMenusTab(g, x, y, w, r.Height - 16); break;
                case "Tools": DrawToolsTab(g, x, y, w, r.Height - 16); break;
                case "Settings": DrawSettingsTab(g, x, y, w, r.Height - 16); break;
            }
        }

        void DrawHomeTab(Graphics g, int x, int y, int w, int h)
        {
            bool python = FindOnPath("pythonw.exe") != null || FindOnPath("python.exe") != null;
            bool ffmpeg = File.Exists(Path.Combine(S.RootPath, "Bin", "FFMPEG.EXE"));
            bool ytdlp = File.Exists(Path.Combine(S.RootPath, "Bin", "yt-dlp.exe"));
            bool aria = File.Exists(Path.Combine(S.RootPath, "Bin", "ARIA2C.EXE"));
            bool deno = File.Exists(Path.Combine(S.RootPath, "Bin", "deno.exe"));
            bool limisaw = File.Exists(Path.Combine(S.RootPath, "LIMISAW.exe"));
            bool ready = python && ffmpeg && ytdlp && aria && deno && limisaw;

            DrawText(g, ready ? "Ready to use" : "Setup needs attention", x, y, ready ? Palette.LINK : Palette.DANGERTXT, 14, true);
            DrawText(g, "Install or repair fills only missing parts and refreshes Explorer menus.", x, y + 22, Palette.TEXT2, 10);
            DrawStatus(g, x, y + 48, "Main app", true, "ready");
            DrawStatus(g, x, y + 68, "Python tools", python, python ? "ready" : "missing");
            DrawStatus(g, x, y + 88, "FFmpeg", ffmpeg, ffmpeg ? "ready" : "missing");
            DrawStatus(g, x, y + 108, "yt-dlp", ytdlp, ytdlp ? "ready" : "missing");
            DrawStatus(g, x, y + 128, "aria2", aria, aria ? "ready" : "missing");
            DrawStatus(g, x, y + 148, "Deno for YouTube", deno, deno ? "ready" : "missing");
            DrawStatus(g, x, y + 168, "Codex limits", limisaw, limisaw ? "ready" : "missing");

            int by = y + 204;
            var setup = new Rectangle(x, by, 176, 28);
            Buttons.Add(new ButtonDef { R = setup, A = () => StartSetup() });
            DrawButton(g, setup, "Install / repair", false, 11);

            var limits = new Rectangle(x + 184, by, 150, 28);
            Buttons.Add(new ButtonDef { R = limits, A = () => LaunchTool("LIMISAW.EXE", "none") });
            DrawButton(g, limits, "Open Codex limits", false, 10);

            int by2 = by + 36;
            var readme = new Rectangle(x, by2, 176, 26);
            Buttons.Add(new ButtonDef { R = readme, A = () => OpenPath(Path.Combine(S.RootPath, "README.md")) });
            DrawButton(g, readme, "Open README", false, 10);

            var folder = new Rectangle(x + 184, by2, 150, 26);
            Buttons.Add(new ButtonDef { R = folder, A = () => OpenPath(S.RootPath) });
            DrawButton(g, folder, "Open toolkit folder", false, 10);

            DrawText(g, "Nothing is added to Windows startup unless you enable it in Settings.", x, y + h - 18, Palette.MUTED, 10);
        }

        void DrawStatus(Graphics g, int x, int y, string label, bool ok, string state)
        {
            DrawText(g, ok ? "[OK]" : "[--]", x, y, ok ? Palette.LINK : Palette.DANGERTXT, 10, true);
            DrawText(g, label, x + 42, y, Palette.TEXT, 11);
            DrawText(g, state, x + 190, y, ok ? Palette.TEXT2 : Palette.DANGERTXT, 10);
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
                var box = new Rectangle(cx, cy, 14, 14);
                var hit = new Rectangle(cx, cy, colW, 16);
                MenuCheckRects.Add(hit);
                int featureIndex = i;
                Buttons.Add(new ButtonDef { R = hit, A = () => { FeatureChecked[featureIndex] = !FeatureChecked[featureIndex]; Refresh(); } });
                DrawBevel(g, box, false);
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

        // label | script | argument mode
        //   "none"   = no target needed
        //   "folder" = ask for a folder, pass it as %1
        //   "file"   = ask for a file, pass it as %1
        string[,] ToolDefs = {
            { "Download audio", "DL_YT.CMD",      "audio"  },
            { "Download video", "DL_YT.CMD",      "video"  },
            { "Merge Audio",  "MERGE_AUD.CMD",    "file"   },
            { "New Project",  "NEW_PROJ.CMD",     "folder" },
            { "Pack File",    "PACK.PYW",         "file"   },
            { "Pack Folder",  "PACK.PYW",         "folder" },
            { "Del Empty",    "DEL_EMPTY.PYW",    "folder" },
            { "Del Dup",      "DEL_DUP.PYW",      "folder" },
            { "Del Same",     "DEL_SAME.PYW",     "folder" },
            { "Del Junk",     "DEL_JUNK.PYW",     "folder" },
            { "PS Admin",     "powershell.exe",   "none"   },
            { "Codex Limits", "LIMISAW.EXE",      "none"   },
        };

        void DrawToolsTab(Graphics g, int x, int y, int w, int h)
        {
            DrawText(g, "Launch a tool. Items marked * ask what file or folder to use.", x, y, Palette.TEXT2, 11);
            int btnW = 126, btnH = 26, gap = 4, cols = 3;
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
            var ar = new Rectangle(x, y + 40, 170, 24);
            Buttons.Add(new ButtonDef { R = ar, A = () => ToggleAutostart("SaitulsApp", "SAITULS") });
            DrawButton(g, ar, ao ? "[X] Start with Windows" : "[ ] Start with Windows", ao, 10);
            DrawText(g, "Starts quietly in the tray. Disabled by default.", x + 180, y + 45, Palette.MUTED, 10);

            int by = y + 70;
            int bw = 140;
            var br = new Rectangle(x, by, bw, 26);
            Buttons.Add(new ButtonDef { R = br, A = () => { Application.Exit(); } });
            DrawButton(g, br, "Exit", false);

            var ber = new Rectangle(x + bw + 8, by, bw, 26);
            Buttons.Add(new ButtonDef { R = ber, A = () => {
                Process.Start("explorer.exe", S.RootPath);
            } });
            DrawButton(g, ber, "Open folder", false);

            DrawText(g, "SAITULS - " + RegFeatures.Length + " Explorer menu features", x, by + 34, Palette.MUTED, 10);
            DrawText(g, "Golden Default per saipen UI.md", x, by + 48, Palette.MUTED, 10);
        }

        void StartSetup()
        {
            string setup = Path.Combine(S.RootPath, "setup.ps1");
            if (!File.Exists(setup)) { MessageBox.Show("Setup script not found:\n" + setup, "SAITULS"); return; }
            try
            {
                Process.Start(new ProcessStartInfo("powershell.exe")
                {
                    Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + setup + "\" -NoLaunch -WaitAtEnd",
                    WorkingDirectory = S.RootPath,
                    Verb = "runas",
                    UseShellExecute = true,
                    WindowStyle = ProcessWindowStyle.Normal
                });
            }
            catch (Exception ex) { MessageBox.Show("Could not start setup:\n" + ex.Message, "SAITULS"); }
        }

        void OpenPath(string path)
        {
            if (!File.Exists(path) && !Directory.Exists(path)) { MessageBox.Show("Not found:\n" + path, "SAITULS"); return; }
            try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
            catch (Exception ex) { MessageBox.Show("Could not open:\n" + ex.Message, "SAITULS"); }
        }

        static string FindOnPath(string fileName)
        {
            string path = (Environment.GetEnvironmentVariable("PATH") ?? "") + Path.PathSeparator +
                (Environment.GetEnvironmentVariable("PATH", EnvironmentVariableTarget.Machine) ?? "") + Path.PathSeparator +
                (Environment.GetEnvironmentVariable("PATH", EnvironmentVariableTarget.User) ?? "");
            foreach (string raw in path.Split(Path.PathSeparator))
            {
                string dir = raw.Trim().Trim('"');
                if (dir.Length == 0) continue;
                try { string candidate = Path.Combine(dir, fileName); if (File.Exists(candidate)) return candidate; }
                catch { }
            }
            return null;
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

            if (script == "LIMISAW.EXE")
            {
                string limi = Path.Combine(root, "LIMISAW.exe");
                if (FindOnPath("python.exe") == null)
                {
                    MessageBox.Show("Python is missing. Open Home and run Install / repair first.", "SAITULS");
                }
                else if (File.Exists(limi))
                {
                    Process.Start(new ProcessStartInfo(limi) { WorkingDirectory = root, UseShellExecute = true });
                }
                else
                {
                    MessageBox.Show("LIMISAW.exe not found:\n" + limi, "SAITULS");
                }
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
            if (mode == "folder" || mode == "audio" || mode == "video")
            {
                using (var fb = new FolderBrowserDialog())
                {
                    fb.Description = (mode == "audio" || mode == "video") ? "Pick the download folder" : script + " — pick the target folder";
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
                    string exe = FindOnPath("pythonw.exe") ?? FindOnPath("python.exe");
                    if (exe == null) { MessageBox.Show("Python is missing. Open Home and run Install / repair first.", "SAITULS"); return; }
                    string args = "\"" + fullPath + "\"" + (target != null ? " \"" + target + "\"" : "");
                    Process.Start(new ProcessStartInfo(exe, args) { WorkingDirectory = binDir, UseShellExecute = false });
                }
                else
                {
                    if ((script == "DL_YT.CMD" || script == "MERGE_AUD.CMD") && !File.Exists(Path.Combine(binDir, "FFMPEG.EXE")))
                    { MessageBox.Show("FFmpeg is missing. Open Home and run Install / repair first.", "SAITULS"); return; }
                    if (script == "DL_YT.CMD" && !File.Exists(Path.Combine(binDir, "yt-dlp.exe")))
                    { MessageBox.Show("yt-dlp is missing. Open Home and run Install / repair first.", "SAITULS"); return; }
                    if (script == "DL_YT.CMD" && !File.Exists(Path.Combine(binDir, "deno.exe")) && FindOnPath("node.exe") == null)
                    { MessageBox.Show("The YouTube JavaScript runtime is missing. Open Home and run Install / repair first.", "SAITULS"); return; }
                    // PATH must carry Bin\ and Bin\App\ so bare yt-dlp/ffmpeg resolve.
                    string args = "/c set \"PATH=" + binDir + ";" + Path.Combine(binDir, "App") + ";%PATH%\" && call \"" + fullPath + "\"" +
                                  (mode == "audio" || mode == "video" ? " " + mode + " \"" + target + "\"" : (target != null ? " \"" + target + "\"" : ""));
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
            FocusedButton = -1;
            S.LastTab = name;
            S.Save();
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
                    else { rk.SetValue(keyName, "\"" + Application.ExecutablePath + "\" --minimized"); nowOn = true; }
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
                if (CurrentTab == "Explorer menus")
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
                for (int i = 0; i < Buttons.Count; i++)
                {
                    var b = Buttons[i];
                    if (b.R.Contains(e.Location) && b.A != null) { FocusedButton = i; b.A(); return; }
                }
                // Tab click
                for (int i = 0; i < TabRects.Count; i++)
                {
                    if (TabRects[i].Contains(e.Location))
                    {
                        SwitchTab(TabNames[i]);
                        return;
                    }
                }
            }
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            if (e.KeyCode == Keys.Escape) { Hide(); e.Handled = true; return; }
            if (e.Control && e.KeyCode >= Keys.D1 && e.KeyCode <= Keys.D4)
            {
                int index = (int)e.KeyCode - (int)Keys.D1;
                if (index < TabNames.Length) SwitchTab(TabNames[index]);
                e.Handled = true; return;
            }
            if (e.KeyCode == Keys.Tab && Buttons.Count > 0)
            {
                int delta = e.Shift ? -1 : 1;
                FocusedButton = (FocusedButton + delta + Buttons.Count) % Buttons.Count;
                Refresh(); e.Handled = true; return;
            }
            if ((e.KeyCode == Keys.Enter || e.KeyCode == Keys.Space) && FocusedButton >= 0 && FocusedButton < Buttons.Count)
            {
                Action action = Buttons[FocusedButton].A;
                if (action != null) action();
                e.Handled = true; return;
            }
            base.OnKeyDown(e);
        }
    }

    static class Program
    {
        static SaitulsForm _form;

        [STAThread]
        static void Main(string[] args)
        {
            bool createdNew;
            using (var mutex = new System.Threading.Mutex(true, "Local\\SaitulsApp", out createdNew))
            {
                if (!createdNew)
                {
                    try
                    {
                        using (var signal = System.Threading.EventWaitHandle.OpenExisting("Local\\SaitulsShow"))
                            signal.Set();
                    }
                    catch
                    {
                        IntPtr existing = Native.FindWindow(null, "SAITULS");
                        if (existing != IntPtr.Zero) { Native.ShowWindow(existing, 5); Native.SetForegroundWindow(existing); }
                    }
                    return;
                }
                var showEvent = new System.Threading.EventWaitHandle(false,
                    System.Threading.EventResetMode.AutoReset, "Local\\SaitulsShow");
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

                NotifyIcon tray = new NotifyIcon();
                tray.Icon = File.Exists(s.IcoPath) ? new Icon(s.IcoPath) : SystemIcons.Application;
                tray.Text = "SAITULS";
                ContextMenuStrip menu = new ContextMenuStrip();
                menu.Items.Add("Open SAITULS", null, (o, e) => ShowForm(s, tray));
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add("Exit", null, (o, e) =>
                {
                                        tray.Visible = false;
                    Application.Exit();
                });
                tray.ContextMenuStrip = menu;
                tray.Visible = true;
                tray.DoubleClick += (o, e) => ShowForm(s, tray);

                try
                {
                    using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                    {
                        if (rk != null)
                        {
                            if (s.AutoStart)
                                rk.SetValue("SaitulsApp", "\"" + Application.ExecutablePath + "\" --minimized");
                            else if (rk.GetValue("SaitulsApp") != null)
                                rk.DeleteValue("SaitulsApp", false);
                        }
                    }
                }
                catch { }

                bool startHidden = Array.Exists(args, a => string.Equals(a, "--minimized", StringComparison.OrdinalIgnoreCase));
                if (startHidden) EnsureForm(s, tray); else ShowForm(s, tray);
                var showWait = System.Threading.ThreadPool.RegisterWaitForSingleObject(showEvent, (state, timedOut) =>
                {
                    SaitulsForm form = _form;
                    if (form == null || form.IsDisposed || !form.IsHandleCreated) return;
                    try { form.BeginInvoke((Action)(() => ShowForm(s, tray))); } catch { }
                }, null, System.Threading.Timeout.Infinite, false);
                Application.Run();
                showWait.Unregister(null);
                showEvent.Dispose();
                tray.Visible = false;
                tray.Dispose();
            }
        }

        static void ShowForm(SaitulsSettings s, NotifyIcon tray)
        {
            EnsureForm(s, tray);
            _form.Show();
            _form.Activate();
        }

        static void EnsureForm(SaitulsSettings s, NotifyIcon tray)
        {
            if (_form == null || _form.IsDisposed)
            {
                _form = new SaitulsForm(s, tray);
                _form.FormClosing += (o, e) =>
                {
                    if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; _form.Hide(); }
                };
                // BeginInvoke from the single-instance signal needs a handle even
                // when Windows starts the application hidden in the tray.
                IntPtr hiddenHandle = _form.Handle;
            }
        }
    }
}
