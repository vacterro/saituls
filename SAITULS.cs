using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;
using System.Text.RegularExpressions;
using Microsoft.Win32;

// SAITULS — unified toolkit manager.
// Tabs: Home | Explorer menus | Tools | Settings
// UI: saipen UI.md Golden Default. Text is NON-antialiased (pixel text).
// v1 C#. Launcher hub: context-menu install + tool launcher + settings.

namespace Saituls
{
    static class Native
    {
        // The user32 window-hunting imports (FindWindow/ShowWindow/
        // SetForegroundWindow/SendMessage) are gone: single-instance
        // activation is delivered through the named Local\SaitulsShow event,
        // so no code searched for a window by title any more.
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
        // Reliable taskbar edge reveal. Off until the user turns it on once --
        // upgrading SAITULS never enables it by itself.
        public bool TaskbarEdge = false;
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
            TaskbarEdge = Read("TaskbarEdge", "0") == "1";
            LastTab = Read("LastTab", "Home");
            v = Read("RootPath", Dir);
            if (Directory.Exists(v)) RootPath = v;
            if (!File.Exists(IniPath)) Save();
        }

        public void Save()
        {
            var ci = System.Globalization.CultureInfo.InvariantCulture;
            Write("AutoStart", AutoStart ? "1" : "0");
            Write("TaskbarEdge", TaskbarEdge ? "1" : "0");
            Write("LastTab", LastTab);
            Write("RootPath", RootPath);
        }
    }


    // TaskbarEdge lifecycle ONLY.
    //
    // The reliable bottom-edge reveal lives entirely in
    // Scripts\taskbar_edge\TaskbarEdge.exe: cursor sampling, monitor geometry,
    // appbar discovery, dwell, debounce and the reveal call are all its
    // business. SAITULS is allowed four verbs and nothing else -- start, stop,
    // report running state, and toggle the autostart/enabled setting. No edge
    // detection, no taskbar Win32 and no shell-window manipulation belongs in
    // this file, and none of it is here.
    static class TaskbarEdgeHelper
    {
        public const string RunKeyName = "SaitulsTaskbarEdge";
        // Mirrors Program.MutexName / Program.StopEventName in
        // Scripts\taskbar_edge\TaskbarEdge.cs. Per-session, per-user, never
        // Global\ -- the helper is an interactive-desktop primitive.
        const string HelperMutex = "Local\\SaitulsTaskbarEdge";
        const string HelperStopEvent = "Local\\SaitulsTaskbarEdgeStop";

        public static string ExePath(string root)
        {
            return Path.Combine(root, "Scripts", "taskbar_edge", "TaskbarEdge.exe");
        }

        public static bool Installed(string root) { return File.Exists(ExePath(root)); }

        public static bool Running()
        {
            Mutex m = null;
            try { m = Mutex.OpenExisting(HelperMutex); return true; }
            catch (WaitHandleCannotBeOpenedException) { return false; }
            catch (UnauthorizedAccessException) { return true; }
            catch { return false; }
            finally { if (m != null) m.Close(); }
        }

        // Never elevated, never with a window. A second start is harmless: the
        // helper's own mutex makes it exit successfully.
        public static bool Start(string root)
        {
            if (Running()) return true;
            string exe = ExePath(root);
            if (!File.Exists(exe)) return false;
            try
            {
                Process.Start(new ProcessStartInfo(exe)
                {
                    WorkingDirectory = Path.GetDirectoryName(exe),
                    UseShellExecute = false,
                    CreateNoWindow = true
                });
                return true;
            }
            catch { return false; }
        }

        public static void Stop()
        {
            try
            {
                using (EventWaitHandle stop = EventWaitHandle.OpenExisting(HelperStopEvent))
                    stop.Set();
            }
            catch { }
        }
    }


    // Toolchain lookups and real readiness probes shared by every section.
    // Existence is NOT readiness: a probe runs the tool and reports the reason.
    static class Toolchain
    {
        public static string FindOnPath(string fileName)
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

        // null = ready. Otherwise a human reason ("missing", "too small",
        // "did not answer --version in 10s", "exited 1", "not runnable (...)").
        // This mirrors Bin/media_payload.ps1's rule set (PE size floor + real
        // --version probe) rather than inventing a second definition of a valid
        // executable: the size floors below are the same ones that script uses.
        public static string Probe(string exePath, string probeArgs, long minBytes, int timeoutMs)
        {
            if (string.IsNullOrEmpty(exePath) || !File.Exists(exePath)) return "missing";
            try
            {
                var info = new FileInfo(exePath);
                if (minBytes > 0 && info.Length < minBytes) return "too small (" + info.Length + " bytes)";
                var psi = new ProcessStartInfo(exePath)
                {
                    Arguments = probeArgs,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (Process p = Process.Start(psi))
                {
                    if (p == null) return "did not start";
                    bool exited = p.WaitForExit(timeoutMs);
                    if (!exited)
                    {
                        try { p.Kill(); } catch { }
                        return "did not answer " + probeArgs + " within " + (timeoutMs / 1000) + "s";
                    }
                    if (p.ExitCode != 0) return probeArgs + " exited " + p.ExitCode;
                }
                return null;
            }
            catch (Exception ex) { return "not runnable (" + ex.Message + ")"; }
        }
    }

    // Clipboard+ lifecycle and status ONLY (T-164). The resident owns every
    // clipboard rule; this shell starts it with pythonw (no console flash),
    // stops it explicitly, and reads the subsystem's own NON-SECRET status and
    // settings files. The shell never reads clipboard content, never stores a
    // sanitized secret and never keeps Safe Copy history.
    static class ClipboardPlusHelper
    {
        public const string MutexName = @"Local\SaitulsClipboardPlus";
        const string StopEventName = @"Local\SaitulsClipboardPlusStop";
        const string StateDirEnv = "SAITULS_CLIPBOARD_PLUS_STATE_DIR";

        public static string ScriptPath(string root)
        {
            return Path.Combine(root, "Scripts", "saipatch", "clipboard+.pyw");
        }

        public static bool Installed(string root) { return File.Exists(ScriptPath(root)); }

        public static string StateDir()
        {
            string over = Environment.GetEnvironmentVariable(StateDirEnv);
            if (!string.IsNullOrEmpty(over)) return over;
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "SAITULS", "clipboard+");
        }

        public static string SettingsPath() { return Path.Combine(StateDir(), "clipboard-plus.ini"); }
        public static string StatusPath() { return Path.Combine(StateDir(), "status.ini"); }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern int GetPrivateProfileString(string app, string key, string def,
            System.Text.StringBuilder buf, int size, string file);

        static string Ini(string file, string section, string key, string def)
        {
            try
            {
                var sb = new System.Text.StringBuilder(600);
                GetPrivateProfileString(section, key, def, sb, sb.Capacity, file);
                return sb.ToString();
            }
            catch { return def; }
        }

        public static string Setting(string key, string def) { return Ini(SettingsPath(), "clipboard+", key, def); }
        public static string Status(string key, string def) { return Ini(StatusPath(), "status", key, def); }
        public static bool StatusFlag(string key) { return Status(key, "0") == "1"; }

        public static string Pythonw()
        {
            string exe = Toolchain.FindOnPath("pythonw.exe");
            if (exe == null) exe = Toolchain.FindOnPath("python.exe");
            return exe;
        }

        public static bool Running()
        {
            Mutex m = null;
            try { m = Mutex.OpenExisting(MutexName); return true; }
            catch (WaitHandleCannotBeOpenedException) { return false; }
            catch (UnauthorizedAccessException) { return true; }
            catch { return false; }
            finally { if (m != null) m.Close(); }
        }

        // Explicit stop. Never a kill, never "start a replacement".
        public static bool Stop()
        {
            try
            {
                using (EventWaitHandle stop = EventWaitHandle.OpenExisting(StopEventName))
                {
                    stop.Set();
                    return true;
                }
            }
            catch { return false; }
        }

        // Idempotent: the resident itself owns the named mutex, so a second
        // start returns ALREADY_RUNNING inside that process and changes nothing.
        public static bool Start(string root)
        {
            string exe = Pythonw();
            if (exe == null) return false;
            string script = ScriptPath(root);
            if (!File.Exists(script)) return false;
            try
            {
                Process.Start(new ProcessStartInfo(exe, "\"" + script + "\" --start")
                {
                    WorkingDirectory = Path.GetDirectoryName(script),
                    UseShellExecute = false,
                    CreateNoWindow = true
                });
                return true;
            }
            catch { return false; }
        }

        // Real dependency readiness as the subsystem reports it (imports plus a
        // real API call). null = ready, otherwise the missing pieces.
        public static string CheckDependencies(string root, int timeoutMs)
        {
            string exe = Pythonw();
            if (exe == null) return "Python (pythonw.exe) not found on PATH";
            string script = ScriptPath(root);
            if (!File.Exists(script)) return "Clipboard+ script is missing";
            try
            {
                var psi = new ProcessStartInfo(exe, "\"" + script + "\" --check-deps")
                {
                    WorkingDirectory = Path.GetDirectoryName(script),
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (Process p = Process.Start(psi))
                {
                    if (p == null) return "could not start the readiness probe";
                    if (!p.WaitForExit(timeoutMs))
                    {
                        try { p.Kill(); } catch { }
                        return "the readiness probe did not answer within " + (timeoutMs / 1000) + "s";
                    }
                    string text = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
                    if (p.ExitCode == 0) return null;
                    foreach (string line in text.Split('\n'))
                        if (line.TrimStart().StartsWith("MISSING="))
                            return line.TrimStart().Substring(8).Trim();
                    string trimmed = text.Trim();
                    return trimmed.Length > 0 ? trimmed : ("readiness probe exited " + p.ExitCode);
                }
            }
            catch (Exception ex) { return ex.Message; }
        }

        // Runs one one-shot command (--sanitize-text-once / --restore-last-safe-copy)
        // and returns what the worker printed, so the reason is never lost in a
        // window that vanished.
        public static string RunOneShot(string root, string argument, int timeoutMs)
        {
            string exe = Pythonw();
            if (exe == null) return "Python (pythonw.exe) not found on PATH";
            string script = ScriptPath(root);
            if (!File.Exists(script)) return "Clipboard+ script is missing: " + script;
            try
            {
                var psi = new ProcessStartInfo(exe, "\"" + script + "\" " + argument)
                {
                    WorkingDirectory = Path.GetDirectoryName(script),
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                using (Process p = Process.Start(psi))
                {
                    if (p == null) return "Clipboard+ could not be started";
                    if (!p.WaitForExit(timeoutMs))
                    {
                        try { p.Kill(); } catch { }
                        return "Clipboard+ did not answer within " + (timeoutMs / 1000) + "s";
                    }
                    string text = (p.StandardOutput.ReadToEnd() + " " + p.StandardError.ReadToEnd()).Trim();
                    return text.Length > 0 ? text : ("Clipboard+ exited " + p.ExitCode);
                }
            }
            catch (Exception ex) { return ex.Message; }
        }

        public static bool LaunchSettings(string root)
        {
            string exe = Pythonw();
            if (exe == null) return false;
            string script = ScriptPath(root);
            if (!File.Exists(script)) return false;
            try
            {
                Process.Start(new ProcessStartInfo(exe, "\"" + script + "\" --settings")
                {
                    WorkingDirectory = Path.GetDirectoryName(script),
                    UseShellExecute = false,
                    CreateNoWindow = true
                });
                return true;
            }
            catch { return false; }
        }
    }

    // The clipboard is the downloader's INPUT, so it is inspected before a job
    // is launched (B2). Explorer file copies, prose and non-http input are
    // refused with a reason instead of being handed to yt-dlp.
    static class ClipboardInput
    {
        public static List<string> ExtractUrls(out string problem)
        {
            problem = "";
            var urls = new List<string>();
            try
            {
                if (Clipboard.ContainsFileDropList())
                {
                    problem = "The clipboard holds copied files, not links. Copy the video link as text first.";
                    return urls;
                }
                string text = Clipboard.ContainsText() ? Clipboard.GetText() : "";
                if (text == null) text = "";
                bool sawNonHttpScheme = false;
                foreach (string rawLine in text.Split('\n'))
                {
                    string line = rawLine.Trim();
                    if (line.Length == 0) continue;
                    Match m = Regex.Match(line, "https?://\\S+", RegexOptions.IgnoreCase);
                    if (m.Success)
                    {
                        string url = m.Value.TrimEnd('.', ',', ';', ':', ')', ']', '>', '"', '\'');
                        if (url.Length > 0 && !urls.Contains(url)) urls.Add(url);
                        continue;
                    }
                    if (Regex.IsMatch(line, "^[A-Za-z][A-Za-z0-9+.\\-]*://")) sawNonHttpScheme = true;
                }
                if (urls.Count == 0)
                {
                    problem = sawNonHttpScheme
                        ? "Only http:// and https:// links are accepted; the clipboard has a different scheme."
                        : (text.Trim().Length == 0
                            ? "The clipboard is empty."
                            : "The clipboard text has no http/https link.");
                }
            }
            catch (Exception ex) { problem = "The clipboard could not be read: " + ex.Message; }
            return urls;
        }
    }

    // Compact downloader options (B5): mode, playlist/date, destination and the
    // clipboard input that will actually be used. SAITULS owns this UI only --
    // Scripts\DL_YT.PS1 owns the download.
    class DownloadDialog : Form
    {
        public string Mode = "audio";
        public bool Playlist;
        public bool Dated;
        public string OutDir = "";

        RadioButton _audio = new RadioButton();
        RadioButton _video = new RadioButton();
        CheckBox _playlist = new CheckBox();
        CheckBox _dated = new CheckBox();
        TextBox _folder = new TextBox();

        public DownloadDialog(string mode, string folder, int urlCount, string urlProblem, string readinessNote)
        {
            Mode = mode == "video" ? "video" : "audio";
            OutDir = folder;

            Text = "SAITULS - download";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(452, 226);
            BackColor = Palette.BG_SOFT;
            ForeColor = Palette.TEXT;
            Font = new Font("Verdana", 8f);

            var modeLabel = new Label();
            modeLabel.Text = "Mode";
            modeLabel.Location = new Point(12, 10);
            modeLabel.AutoSize = true;
            modeLabel.ForeColor = Palette.TEXT2;

            _audio.Text = "Audio (mp3)";
            _audio.Location = new Point(12, 30);
            _audio.AutoSize = true;
            _audio.ForeColor = Palette.TEXT;
            _audio.Checked = Mode == "audio";

            _video.Text = "Video (mkv)";
            _video.Location = new Point(12, 54);
            _video.AutoSize = true;
            _video.ForeColor = Palette.TEXT;
            _video.Checked = Mode == "video";

            _playlist.Text = "Playlist";
            _playlist.Location = new Point(160, 30);
            _playlist.AutoSize = true;
            _playlist.ForeColor = Palette.TEXT;

            _dated.Text = "Prefix upload date";
            _dated.Location = new Point(160, 54);
            _dated.AutoSize = true;
            _dated.ForeColor = Palette.TEXT;

            var folderLabel = new Label();
            folderLabel.Text = "Output folder";
            folderLabel.Location = new Point(12, 84);
            folderLabel.AutoSize = true;
            folderLabel.ForeColor = Palette.TEXT2;

            _folder.Text = folder;
            _folder.Location = new Point(12, 104);
            _folder.Size = new Size(328, 22);
            _folder.BackColor = Palette.COMPARE;
            _folder.ForeColor = Palette.TEXT;
            _folder.BorderStyle = BorderStyle.FixedSingle;

            var browse = new Button();
            browse.Text = "Browse...";
            browse.Location = new Point(348, 103);
            browse.Size = new Size(92, 24);
            browse.FlatStyle = FlatStyle.Flat;
            browse.BackColor = Palette.RAISED;
            browse.ForeColor = Palette.TEXT;
            browse.Click += delegate
            {
                using (var fb = new FolderBrowserDialog())
                {
                    fb.Description = "Pick the download folder";
                    fb.SelectedPath = Directory.Exists(_folder.Text) ? _folder.Text : folder;
                    if (fb.ShowDialog(this) == DialogResult.OK) _folder.Text = fb.SelectedPath;
                }
            };

            var sourceLabel = new Label();
            sourceLabel.Location = new Point(12, 136);
            sourceLabel.Size = new Size(428, 30);
            sourceLabel.ForeColor = urlCount > 0 ? Palette.TEXT2 : Palette.DANGERTXT;
            sourceLabel.Text = urlCount > 0
                ? "Source: the link(s) in your clipboard - " + urlCount + " found. Destination: the folder above."
                : "No downloadable URL found in the clipboard. " + urlProblem;

            var readiness = new Label();
            readiness.Location = new Point(12, 166);
            readiness.Size = new Size(428, 16);
            readiness.ForeColor = Palette.DANGERTXT;
            readiness.Text = readinessNote;

            var download = new Button();
            download.Text = "Download";
            download.Location = new Point(252, 190);
            download.Size = new Size(96, 26);
            download.FlatStyle = FlatStyle.Flat;
            download.BackColor = Palette.RAISED;
            download.ForeColor = urlCount > 0 ? Palette.TEXT : Palette.MUTED;
            download.Enabled = urlCount > 0;
            download.Click += delegate
            {
                Mode = _video.Checked ? "video" : "audio";
                Playlist = _playlist.Checked;
                Dated = _dated.Checked;
                OutDir = _folder.Text.Trim();
                if (OutDir.Length == 0)
                {
                    MessageBox.Show("Pick an output folder first.", "SAITULS - download");
                    return;
                }
                DialogResult = DialogResult.OK;
                Close();
            };

            var cancel = new Button();
            cancel.Text = "Cancel";
            cancel.Location = new Point(354, 190);
            cancel.Size = new Size(86, 26);
            cancel.FlatStyle = FlatStyle.Flat;
            cancel.BackColor = Palette.RAISED;
            cancel.ForeColor = Palette.TEXT;
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };

            Controls.Add(modeLabel);
            Controls.Add(_audio);
            Controls.Add(_video);
            Controls.Add(_playlist);
            Controls.Add(_dated);
            Controls.Add(folderLabel);
            Controls.Add(_folder);
            Controls.Add(browse);
            Controls.Add(sourceLabel);
            Controls.Add(readiness);
            Controls.Add(download);
            Controls.Add(cancel);
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
        // Rapid-Enter / hold-repeat guard. Holding Enter/Space fires KeyDown
        // auto-repeat (~30/s); double-click fires two MouseDowns. Both used
        // to invoke the focused button's Action N times (N UAC prompts,
        // N tool processes). _enterSpaceHeld kills hold-repeat (one press =
        // one activation, like a standard Button); the TickCount window eats
        // rapid distinct presses; _activating blocks re-entrancy while a
        // modal (MessageBox/FileDialog) is up.
        int _lastActivateTick = 0;
        bool _activating = false;
        bool _enterSpaceHeld = false;
        const int ActivateDebounceMs = 400;

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
        // One Font per point size and one centered StringFormat for the whole
        // form lifetime. Every paint used to build a pixel font per DrawText,
        // DrawTextCenter, DrawButton and TextW call plus a StringFormat per
        // tab and button, all of it churn in a process that stays resident
        // for days.
        Dictionary<int, Font> Fonts = new Dictionary<int, Font>();
        StringFormat Centered = new StringFormat();

        class ButtonDef { public Rectangle R; public Action A; public string Label; public bool Sel; }

        // One tracked tool process (download or merge). The record survives the
        // window being hidden, so a finished job's result is still visible.
        class ToolJob
        {
            public string Label = "";
            public string Line = "";
            public string LogPath = "";
            public string OutDir = "";
            public bool Finished;
            public bool Ok;
            public int Exit = -1;
        }

        class MediaProbeResult
        {
            public bool Ready;
            public string Line = "Media tools: checking...";
            public string DownloadBlocker = "";
            public string MergeBlocker = "";
            public string Ffmpeg = "";
            public string Ffprobe = "";
            public string YtDlp = "";
            public string JsRuntime = "";
        }

        // ---- MEDIA section state (T-167) ------------------------------------
        bool _mediaProbeStarted, _mediaProbeDone;
        MediaProbeResult _lastProbe;
        ToolJob DownloadJob, MergeJob;

        // ---- ITEM LIST / ITEM TREE state (T-165) -----------------------------
        // One engine, two modes. The result is kept so "Open output" and
        // "Copy output path" survive until the next run.
        ToolJob ItemJob;
        string _itemOutPath = "";
        string _itemMsg = "";

        // ---- CLIPBOARD+ section state (T-164) --------------------------------
        bool _clipDepsStarted, _clipDepsDone;
        string _clipDeps;                  // null == dependencies ready
        string _clipMsg = "";
        bool _clipRunning, _clipMonitorOn, _clipHotkeyOn, _clipPathOk, _clipFallbackActive, _clipStatusStale;
        string _clipSavePath = "", _clipHotkey = "", _clipEffectivePath = "", _clipHotkeyError = "", _clipSaveError = "", _clipSavedCount = "0";
        DateTime _clipStatusRead = DateTime.MinValue;
        System.Windows.Forms.Timer _clipWait;

        public SaitulsForm(SaitulsSettings s, NotifyIcon tray)
        {
            S = s; Tray = tray;
            Centered.Alignment = StringAlignment.Center;
            Centered.LineAlignment = StringAlignment.Center;
            Text = "SAITULS";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(560, 520);
            BackColor = Palette.BG;
            DoubleBuffered = true;
            TopMost = false;
            KeyPreview = true;
            FeatureChecked = new bool[RegFeatures.Length];
            for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = true;
            if (Array.IndexOf(TabNames, S.LastTab) >= 0) CurrentTab = S.LastTab;
            try { Icon = Program.LoadIcon(s.IcoPath, SystemInformation.IconSize.Width); } catch { }
            Activated += (o, e) => Refresh();
        }

        protected override void WndProc(ref Message m)
        {
            const int WM_NCHITTEST = 0x84;
            const int HTCAPTION = 2;
            base.WndProc(ref m);
            if (m.Msg == WM_NCHITTEST)
            {
                // LParam packs two SIGNED 16-bit screen coords. On a monitor
                // above the primary the y half sets the high bit, so ToInt32()
                // overflows and the title strip stops dragging there. Read the
                // whole native value and truncate unchecked.
                int raw = unchecked((int)m.LParam.ToInt64());
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
        // Cached for the form's lifetime: the caller never owns the Font and
        // must not dispose it. Released in Dispose.
        Font F(int pt)
        {
            Font f;
            if (!Fonts.TryGetValue(pt, out f))
            {
                f = MakePixelFont("Verdana", pt);
                Fonts[pt] = f;
            }
            return f;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                foreach (Font f in Fonts.Values) f.Dispose();
                Fonts.Clear();
                Centered.Dispose();
            }
            base.Dispose(disposing);
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
            bool python = Toolchain.FindOnPath("pythonw.exe") != null || Toolchain.FindOnPath("python.exe") != null;
            // The same real probes the Tools > MEDIA section uses: a payload that
            // merely exists but does not run must never read as READY.
            EnsureMediaProbe();
            MediaProbeResult probe = _lastProbe;
            bool probed = _mediaProbeDone && probe != null;
            bool aria = File.Exists(Path.Combine(S.RootPath, "Bin", "ARIA2C.EXE"));
            bool ready = python && probed && probe.Ready && aria;

            DrawText(g, ready ? "Ready to use" : (probed ? "Setup needs attention" : "Checking the toolchain..."),
                x, y, ready ? Palette.LINK : (probed ? Palette.DANGERTXT : Palette.TEXT2), 14, true);
            DrawText(g, "Install or repair fills only missing parts and refreshes Explorer menus.", x, y + 22, Palette.TEXT2, 10);
            DrawStatus(g, x, y + 48, "Main app", true, "ready");
            DrawStatus(g, x, y + 68, "Python tools", python, python ? "ready" : "missing");
            DrawStatus(g, x, y + 88, "FFmpeg", probed && probe.Ffmpeg.Length == 0,
                probed ? (probe.Ffmpeg.Length == 0 ? "ready" : probe.Ffmpeg) : "checking...");
            DrawStatus(g, x, y + 108, "yt-dlp", probed && probe.YtDlp.Length == 0,
                probed ? (probe.YtDlp.Length == 0 ? "ready" : probe.YtDlp) : "checking...");
            DrawStatus(g, x, y + 128, "aria2", aria, aria ? "ready" : "missing");
            DrawStatus(g, x, y + 148, "Deno for YouTube", probed && probe.JsRuntime.Length == 0,
                probed ? (probe.JsRuntime.Length == 0 ? "ready" : probe.JsRuntime) : "checking...");

            int by = y + 184;
            var setup = new Rectangle(x, by, 176, 28);
            Buttons.Add(new ButtonDef { R = setup, A = () => StartSetup() });
            DrawButton(g, setup, "Install / repair", false, 11);

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

        // MEDIA section grid: label | worker | mode. This is the canonical list
        // the section draws AND the media matrix discovers, so there is no
        // second hardcoded list to drift. The workers own every rule; the shell
        // only picks the input and reports the real outcome.
        string[,] MediaDefs = {
            { "Download Audio", "DL_YT.PS1",     "audio" },
            { "Download Video", "DL_YT.PS1",     "video" },
            { "Merge Audio",    "MERGE_AUD.PS1", "file"  },
        };

        // label | script | argument mode
        //   "none"   = no target needed
        //   "folder" = ask for a folder, pass it as %1
        //   "file"   = ask for a file, pass it as %1
        string[,] ToolDefs = {
            { "New Project",  "NEW_PROJ.CMD",     "folder" },
            { "Pack File",    "PACK.PYW",         "file"   },
            { "Pack Folder",  "PACK.PYW",         "folder" },
            { "Del Empty",    "DEL_EMPTY.PYW",    "folder" },
            { "Del Dup",      "DEL_DUP.PYW",      "folder" },
            { "Del Same",     "DEL_SAME.PYW",     "folder" },
            { "Del Junk",     "DEL_JUNK.PYW",     "folder" },
            { "PS Admin",     "powershell.exe",   "none"   },
            // Item List / Item Tree: the shared first-party engine
            // (Scripts\item_tools.py) in its two modes. The shell only picks
            // the target and the output path, runs the worker hidden and
            // reports the real result -- no traversal logic lives here.
            { "Item List",    "item_tools.py",    "itemlist" },
            { "Item Tree",    "item_tools.py",    "itemtree" },
            // OpenCode Patcher: the ONLY SAIPATCH surface in SAITULS. The button
            // launches the standalone patcher (Scripts\saipatch\patcher.ps1);
            // all patch logic lives there, never here.
            { "OpenCode Patcher", "saipatch",     "saipatch" },
            // OpenCode Settings: the SAIPATCH completion-sound settings GUI
            // (Scripts\saipatch\settings.ps1). Same boundary as the patcher.
            { "OpenCode Settings", "saipatch",    "saipatch-settings" },
            // OpenCode Queue Viewer: the SAIPATCH read-only queue cockpit
            // (Scripts\saipatch\queue.ps1 -> queue_viewer.pyw). Same SAIPATCH
            // boundary; no patch Apply/Restore controls live in the Viewer.
            { "OpenCode Queue Viewer", "saipatch", "saipatch-queue-viewer" },
            // Scenarios: the ONLY surface for the migrated _SCENARIOS collection.
            // One button launches the dedicated data-driven launcher
            // (Scripts\scenarios\SCENARIOS.ps1); no scenario logic lives here.
            { "Scenarios", "scenarios",           "scenarios" },
            // Secure Apps: the ONLY surface for the FIDO2-gated protected
            // application subsystem (Scripts\secure_apps\SECURE_APPS.ps1).
            // No FIDO2 protocol code, no key handling, no BitLocker lifecycle,
            // no idle monitoring and no protected-process management lives in
            // this shell -- the subsystem owns all of it, exactly as SAIPATCH
            // and Scenarios own theirs. This button launches it and nothing else.
            { "Secure Apps", "secureapps",        "secureapps" },
            // AI Consoles: the data-driven, NON-elevated console launcher
            // (Scripts\consoles\CONSOLES.ps1). Separate from Secure Apps:
            // these profiles are not behind the encrypted vault.
            { "AI Consoles", "consoles",          "consoles" },
            // Shell Doctor: read-only Explorer/Start lag diagnosis
            // (Scripts\shell_doctor\SHELL_DOCTOR.ps1) -- storage resets, idle
            // shell extensions, handler overlap. Its only mutation is an
            // opt-in per-user view/cache reset it asks for itself.
            { "Shell Doctor", "shelldoctor",      "shelldoctor" },
            // SAISPIN Settings: per-user watchdog policy and transactional task
            // registration. The editor stays non-elevated.
            { "SAISPIN Settings", "saispin_settings.ps1", "saispin-settings" },
        };

        void DrawSectionHeader(Graphics g, int x, int y, int w, string title)
        {
            DrawText(g, title, x, y, Palette.TEXT, 12, true);
            int tw = TextW(g, title, 12);
            using (var p = new Pen(Palette.BMUTED)) g.DrawLine(p, x + tw + 8, y + 8, x + w, y + 8);
        }

        void DrawToolsTab(Graphics g, int x, int y, int w, int h)
        {
            int cy = y;
            EnsureMediaProbe();
            RefreshClipboardStatus();

            // ------------------------------------------------------------------
            // MEDIA - grouped, understandable, with ONE compact readiness line so
            // nobody has to press a button and watch a console disappear (B1).
            // ------------------------------------------------------------------
            DrawSectionHeader(g, x, cy, w, "MEDIA");
            cy += 20;
            MediaProbeResult probe = _lastProbe;
            bool mediaKnown = _mediaProbeDone && probe != null;
            DrawText(g, mediaKnown ? probe.Line : "Media tools: checking (FFmpeg, FFprobe, yt-dlp, JavaScript runtime)...",
                x, cy, mediaKnown ? (probe.Ready ? Palette.LINK : Palette.DANGERTXT) : Palette.TEXT2, 10);
            cy += 16;
            int btnW = 126, btnH = 26, gap = 8;
            int bx = x;
            for (int i = 0; i < MediaDefs.GetLength(0); i++)
            {
                string label = MediaDefs[i, 0];
                string mode = MediaDefs[i, 2];
                if (mode == "file") label += " *";
                var r = new Rectangle(bx, cy, btnW, btnH);
                string m = mode;
                Buttons.Add(new ButtonDef { R = r, A = () => MediaAction(m) });
                DrawButton(g, r, label, false, 10);
                bx += btnW + gap;
            }
            var recheck = new Rectangle(bx, cy, btnW, btnH);
            Buttons.Add(new ButtonDef { R = recheck, A = () => { ResetMediaProbe(); EnsureMediaProbe(); Refresh(); } });
            DrawButton(g, recheck, "Recheck", false, 10);
            cy += btnH + 4;
            cy = DrawJobLine(g, x, cy, w, DownloadJob, "Download");
            cy = DrawJobLine(g, x, cy, w, MergeJob, "Merge Audio");

            // ------------------------------------------------------------------
            // CLIPBOARD+ - start, stop, Safe Copy, Restore, Settings and the
            // real runtime state. The subsystem owns every clipboard rule.
            // ------------------------------------------------------------------
            cy += 6;
            DrawSectionHeader(g, x, cy, w, "CLIPBOARD+");
            cy += 20;

            bool clipInstalled = ClipboardPlusHelper.Installed(S.RootPath);
            string state;
            Color stateColor;
            if (!clipInstalled) { state = "Clipboard+: NOT INSTALLED"; stateColor = Palette.DANGERTXT; }
            else if (_clipDepsDone && _clipDeps != null)
            { state = "Clipboard+: DEPENDENCY MISSING (" + _clipDeps + ")"; stateColor = Palette.DANGERTXT; }
            else if (_clipRunning) { state = "Clipboard+: RUNNING"; stateColor = Palette.LINK; }
            else { state = "Clipboard+: STOPPED"; stateColor = Palette.TEXT2; }
            DrawText(g, state, x, cy, stateColor, 11, true);
            if (_clipRunning)
            {
                DrawText(g, _clipMonitorOn ? "monitor active" : "monitor inactive", x + 200, cy + 1,
                    _clipMonitorOn ? Palette.TEXT2 : Palette.DANGERTXT, 10);
                string stale = _clipStatusStale ? "status file is stale" : null;
                if (stale != null) DrawText(g, stale, x + 292, cy + 1, Palette.DANGERTXT, 10);
            }
            cy += 18;

            string hotkeyText = _clipHotkey.Length > 0 ? _clipHotkey : "Ctrl+Alt+Shift+C";
            if (_clipRunning && !_clipHotkeyOn)
                DrawText(g, "Safe Copy hotkey NOT registered: " + hotkeyText +
                    (_clipHotkeyError.Length > 0 ? " (" + _clipHotkeyError + ")" : "") +
                    " - the image monitor still runs.", x, cy, Palette.DANGERTXT, 10);
            else
                DrawText(g, "Safe Copy hotkey: " + hotkeyText, x, cy, Palette.TEXT2, 10);
            cy += 15;

            if (_clipRunning)
            {
                if (_clipPathOk)
                    DrawText(g, "Images -> " + _clipEffectivePath + (_clipFallbackActive ? "  (fallback in use)" : "") +
                        "    saved: " + _clipSavedCount, x, cy, Palette.TEXT2, 10);
                else
                    DrawText(g, "SAVE PATH UNAVAILABLE -> " + _clipSavePath +
                        (_clipSaveError.Length > 0 ? "  (" + _clipSaveError + ")" : "") +
                        "    use Settings to repair it", x, cy, Palette.DANGERTXT, 10);
            }
            else
            {
                string configured = _clipSavePath.Length > 0 ? _clipSavePath : "(default)";
                DrawText(g, "Image save folder: " + configured + (_clipPathOk ? "" : "  (not usable right now)"),
                    x, cy, _clipPathOk ? Palette.TEXT2 : Palette.DANGERTXT, 10);
            }
            cy += 16;

            int cbW = 100, cbH = 24, cbGap = 6;
            int cbx = x;
            var startR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = startR, A = ClipboardStartAction });
            DrawButton(g, startR, "Start", false, 10);
            cbx += cbW + cbGap;
            var stopR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = stopR, A = ClipboardStopAction });
            DrawButton(g, stopR, "Stop", false, 10);
            cbx += cbW + cbGap;
            var safeR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = safeR, A = ClipboardSafeCopyAction });
            DrawButton(g, safeR, "Safe Copy", false, 10);
            cbx += cbW + cbGap;
            var restoreR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = restoreR, A = ClipboardRestoreAction });
            DrawButton(g, restoreR, "Restore", false, 10);
            cbx += cbW + cbGap;
            var settingsR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = settingsR, A = ClipboardSettingsAction });
            DrawButton(g, settingsR, "Settings", false, 10);
            cy += cbH + 2;

            if (_clipMsg.Length > 0)
            {
                DrawText(g, _clipMsg, x, cy, Palette.TEXT2, 10);
                cy += 15;
            }

            // ------------------------------------------------------------------
            // OTHER TOOLS
            // ------------------------------------------------------------------
            cy += 6;
            DrawSectionHeader(g, x, cy, w, "OTHER TOOLS");
            cy += 20;
            int cols = 3;
            int cx = x;
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
                if ((i + 1) % cols == 0) { cx = x; cy += btnH + 4; }
            }
            if (cx != x) cy += btnH + 4;
            // ------------------------------------------------------------------
            // ITEM TOOLS - the Item List / Item Tree job line, with the two
            // actions a finished output can offer: open it, or copy its path.
            // ------------------------------------------------------------------
            if (ItemJob != null)
            {
                cy += 6;
                cy = DrawItemJobLine(g, x, cy, w);
            }

            // Basic Secure Apps status only. The shell reads the subsystem's
            // own durable NON-SECRET state file and prints what it says; it
            // never asks a backend, never holds a secret and never decides a
            // lifecycle. An unreadable or absent file reads as "not set up",
            // which is the honest answer from out here.
            DrawText(g, "Secure Apps: " + SecureAppsStatusLine(), x, cy + 2, Palette.TEXT2, 10);
        }

        // One job line + the two actions a finished job can offer. Nothing is
        // hidden: the shell shows running/completed/failed, the destination and
        // the captured log instead of letting a console vanish with the reason.
        int DrawJobLine(Graphics g, int x, int y, int w, ToolJob job, string title)
        {
            if (job == null) return y;
            Color color = job.Finished ? (job.Ok ? Palette.LINK : Palette.DANGERTXT) : Palette.TEXT;
            DrawText(g, title + ": " + job.Line, x, y, color, 10);
            int bx = x + 336;
            if (job.Finished && job.Ok && !string.IsNullOrEmpty(job.OutDir) && Directory.Exists(job.OutDir))
            {
                var r = new Rectangle(bx, y - 4, 108, 20);
                string dir = job.OutDir;
                Buttons.Add(new ButtonDef { R = r, A = () => OpenPath(dir) });
                DrawButton(g, r, "Open folder", false, 9);
                bx += 114;
            }
            if (job.Finished && !string.IsNullOrEmpty(job.LogPath) && File.Exists(job.LogPath))
            {
                var r = new Rectangle(bx, y - 4, 90, 20);
                string log = job.LogPath;
                Buttons.Add(new ButtonDef { R = r, A = () => OpenPath(log) });
                DrawButton(g, r, "Show log", false, 9);
            }
            return y + 18;
        }

        // One job line + the actions a finished item output can offer: open it
        // or copy its path. The result stays visible until the next run.
        int DrawItemJobLine(Graphics g, int x, int y, int w)
        {
            Color color = ItemJob.Finished ? (ItemJob.Ok ? Palette.LINK : Palette.DANGERTXT) : Palette.TEXT;
            DrawText(g, ItemJob.Line, x, y, color, 10);
            int bx = x + 336;
            if (ItemJob.Finished && ItemJob.Ok && _itemOutPath.Length > 0 && File.Exists(_itemOutPath))
            {
                var ro = new Rectangle(bx, y - 4, 108, 20);
                string outPath = _itemOutPath;
                Buttons.Add(new ButtonDef { R = ro, A = () => OpenPath(outPath) });
                DrawButton(g, ro, "Open output", false, 9);
                bx += 114;
                var rc = new Rectangle(bx, y - 4, 122, 20);
                Buttons.Add(new ButtonDef { R = rc, A = () =>
                {
                    try { Clipboard.SetText(_itemOutPath); _itemMsg = "Output path copied."; }
                    catch { _itemMsg = "Could not copy the output path."; }
                    Refresh();
                } });
                DrawButton(g, rc, "Copy output path", false, 9);
                bx += 128;
            }
            if (ItemJob.Finished && !string.IsNullOrEmpty(ItemJob.LogPath) && File.Exists(ItemJob.LogPath))
            {
                var rl = new Rectangle(bx, y - 4, 90, 20);
                string log = ItemJob.LogPath;
                Buttons.Add(new ButtonDef { R = rl, A = () => OpenPath(log) });
                DrawButton(g, rl, "Show log", false, 9);
            }
            y += 18;
            if (_itemMsg.Length > 0)
            {
                DrawText(g, _itemMsg, x, y, Palette.TEXT2, 10);
                y += 15;
            }
            return y;
        }

        // Minimal reader for the Secure Apps state file
        // (<managed-root>\state\secure-apps.json). Deliberately a scanner,
        // not a parser: the shell must not grow a dependency on the
        // subsystem's schema beyond two public strings, and it must never
        // read the credential store, which is a different file entirely.
        string SecureAppsStatusLine()
        {
            try
            {
                string root = Environment.GetEnvironmentVariable("SAITULS_SECURE_APPS_ROOT");
                if (string.IsNullOrEmpty(root))
                    root = Path.Combine(Environment.GetFolderPath(
                        Environment.SpecialFolder.LocalApplicationData), "SAITULS", "secure-apps");
                string statePath = Path.Combine(root, "state", "secure-apps.json");
                if (!File.Exists(statePath)) return "not set up";
                string text = File.ReadAllText(statePath);
                var parts = new List<string>();
                int index = 0;
                while (parts.Count < 4)
                {
                    int idAt = text.IndexOf("\"profile_id\"", index, StringComparison.Ordinal);
                    if (idAt < 0) break;
                    string id = ScanJsonString(text, idAt + 13);
                    int stateAt = text.IndexOf("\"expected_state\"", idAt, StringComparison.Ordinal);
                    string state = stateAt < 0 ? null : ScanJsonString(text, stateAt + 17);
                    if (id != null) parts.Add(id + "=" + (state ?? "?"));
                    index = idAt + 13;
                }
                return parts.Count == 0 ? "no profiles recorded" : string.Join("  ", parts.ToArray());
            }
            catch { return "status unavailable"; }
        }

        static string ScanJsonString(string text, int from)
        {
            int open = text.IndexOf('"', from);
            if (open < 0) return null;
            int close = text.IndexOf('"', open + 1);
            if (close < 0) return null;
            return text.Substring(open + 1, close - open - 1);
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

            DrawTaskbarEdgeSection(g, x, y + 76, w);

            int by = y + 216;
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

        // Two switches and a status line. Polling cadence, dwell, cooldown and
        // strategy are deliberately NOT here -- they live in
        // Scripts\taskbar_edge\taskbar_edge.ini for the rare person who needs
        // them.
        void DrawTaskbarEdgeSection(Graphics g, int x, int y, int w)
        {
            DrawBevel(g, new Rectangle(x, y - 8, w, 2), false);
            DrawText(g, "Taskbar", x, y, Palette.TEXT, 12, true);

            bool installed = TaskbarEdgeHelper.Installed(S.RootPath);
            bool enabled = S.TaskbarEdge;
            bool running = TaskbarEdgeHelper.Running();

            var er = new Rectangle(x, y + 20, 236, 24);
            Buttons.Add(new ButtonDef { R = er, A = () => ToggleTaskbarEdge() });
            DrawButton(g, er, (enabled ? "[X] " : "[ ] ") + "Reliable taskbar edge reveal", enabled, 10);

            string state;
            Color stateColor;
            if (!installed) { state = "helper missing"; stateColor = Palette.DANGERTXT; }
            else if (running) { state = "running"; stateColor = Palette.LINK; }
            else if (enabled) { state = "enabled, not running"; stateColor = Palette.DANGERTXT; }
            else { state = "stopped"; stateColor = Palette.TEXT2; }
            DrawText(g, state, x + 246, y + 26, stateColor, 10);

            bool autoOn = AutoStartEnabled(TaskbarEdgeHelper.RunKeyName);
            var ar = new Rectangle(x, y + 50, 236, 24);
            Buttons.Add(new ButtonDef { R = ar, A = () => ToggleTaskbarEdgeAutostart() });
            DrawButton(g, ar, (autoOn ? "[X] " : "[ ] ") + "Start with SAITULS / Windows", autoOn, 10);
            DrawText(g, enabled ? "Survives a SAITULS restart." : "Turn the reveal on first.",
                x + 246, y + 56, Palette.MUTED, 10);

            DrawText(g, "Push the pointer into the bottom edge of a screen and that screen's", x, y + 82, Palette.MUTED, 10);
            DrawText(g, "auto-hidden taskbar appears, even under a maximized window.", x, y + 96, Palette.MUTED, 10);
            DrawText(g, "Keeps running when SAITULS exits; switch it off here to stop it.", x, y + 110, Palette.MUTED, 10);
        }

        void ToggleTaskbarEdge()
        {
            if (!TaskbarEdgeHelper.Installed(S.RootPath))
            {
                MessageBox.Show("TaskbarEdge is missing:\n" + TaskbarEdgeHelper.ExePath(S.RootPath)
                    + "\n\nBuild it with Scripts\\taskbar_edge\\build.ps1.", "SAITULS");
                return;
            }

            if (S.TaskbarEdge)
            {
                S.TaskbarEdge = false;
                S.Save();
                TaskbarEdgeHelper.Stop();
                // Disabling the feature also retires its Windows autostart:
                // leaving the Run entry behind would resurrect it next logon.
                if (AutoStartEnabled(TaskbarEdgeHelper.RunKeyName))
                    ToggleAutostart(TaskbarEdgeHelper.RunKeyName, "TaskbarEdge", TaskbarEdgeHelper.ExePath(S.RootPath));
            }
            else
            {
                S.TaskbarEdge = true;
                S.Save();
                if (!TaskbarEdgeHelper.Start(S.RootPath))
                    MessageBox.Show("Could not start TaskbarEdge:\n" + TaskbarEdgeHelper.ExePath(S.RootPath), "SAITULS");
            }
            Refresh();
        }

        void ToggleTaskbarEdgeAutostart()
        {
            if (!TaskbarEdgeHelper.Installed(S.RootPath))
            {
                MessageBox.Show("TaskbarEdge is missing:\n" + TaskbarEdgeHelper.ExePath(S.RootPath), "SAITULS");
                return;
            }
            if (!S.TaskbarEdge && !AutoStartEnabled(TaskbarEdgeHelper.RunKeyName))
            {
                MessageBox.Show("Turn on \"Reliable taskbar edge reveal\" first.", "SAITULS");
                return;
            }
            ToggleAutostart(TaskbarEdgeHelper.RunKeyName, "TaskbarEdge", TaskbarEdgeHelper.ExePath(S.RootPath));
        }

        void StartSetup()
        {
            string setup = Path.Combine(S.RootPath, "setup.ps1");
            if (!File.Exists(setup)) { MessageBox.Show("Setup script not found:\n" + setup, "SAITULS"); return; }
            // Install / repair can change every payload, so the cached readiness
            // is discarded and measured again: never a stale READY.
            ResetMediaProbe();
            _clipDepsStarted = false;
            _clipDepsDone = false;
            _clipDeps = null;
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
            // Preflight. A selected feature whose .REG is missing used to be
            // silently skipped: the user confirmed a install that never ran and
            // the summary said nothing. Nothing is imported at all in that case,
            // because half an install the user did not confirm is worse than
            // none of it.
            List<string> missing = new List<string>();
            foreach (string f in selected)
                if (!File.Exists(Path.Combine(S.RootPath, "Registry", f + ".REG"))) missing.Add(f + ".REG");
            if (missing.Count > 0)
            {
                MessageBox.Show("Missing registry file(s) - nothing was imported:\n\n" +
                    string.Join("\n", missing.ToArray()), "SAITULS");
                return;
            }
            // One elevated powershell that imports every selected feature. Each
            // import is guarded so one failure cannot hide the rest, and the
            // script exits with the number of failures for the parent to report.
            var sb = new System.Text.StringBuilder("$__failed = 0\r\n");
            foreach (string f in selected)
            {
                string reg = Path.Combine(S.RootPath, "Registry", f + ".REG");
                sb.Append("try { & '").Append(safe.Replace("'", "''")).Append("' -File '")
                  .Append(reg.Replace("'", "''")).AppendLine("' }")
                  .AppendLine("catch { $__failed++; Write-Warning '" + f + ": ' + $_.Exception.Message }");
            }
            RunElevatedScript(sb.ToString(), selected.Count);
        }

        void UninstallSelected()
        {
            List<string> selected = new List<string>();
            for (int i = 0; i < RegFeatures.Length; i++)
                if (FeatureChecked[i]) selected.Add(RegFeatures[i]);
            if (selected.Count == 0) { MessageBox.Show("Select at least one feature.", "SAITULS"); return; }
            string safe = ImportSafePath();
            if (!File.Exists(safe)) { MessageBox.Show("IMPORT_SAFE.PS1 not found:\n" + safe, "SAITULS"); return; }
            List<string> missing = new List<string>();
            foreach (string f in selected)
                if (!File.Exists(Path.Combine(S.RootPath, "Registry", f + "_REM.REG"))) missing.Add(f + "_REM.REG");
            if (missing.Count > 0)
            {
                MessageBox.Show("Missing removal file(s) - nothing was removed:\n\n" +
                    string.Join("\n", missing.ToArray()), "SAITULS");
                return;
            }
            var sb = new System.Text.StringBuilder("$__failed = 0\r\n");
            foreach (string f in selected)
            {
                string reg = Path.Combine(S.RootPath, "Registry", f + "_REM.REG");
                sb.Append("try { & '").Append(safe.Replace("'", "''")).Append("' -File '")
                  .Append(reg.Replace("'", "''")).AppendLine("' }")
                  .AppendLine("catch { $__failed++; Write-Warning '" + f + ": ' + $_.Exception.Message }");
            }
            RunElevatedScript(sb.ToString(), selected.Count);
        }

        // Runs one elevated PowerShell script and reports the real outcome.
        // The child's exit code -- not the fact that it started -- is the
        // result: IMPORT_SAFE throws on a failed reg import, so a nonzero exit
        // means at least one requested mutation did not happen, and the UI must
        // say so instead of letting a vanished console window be the only
        // witness.
        void RunElevatedScript(string scriptBody, int requested)
        {
            string tmp = null;
            try
            {
                // Write statements to a temp .ps1, then elevate ONE powershell via
                // ShellExecute Verb=runas — no nested quoting hops at all.
                // The failure count becomes the exit code; the pause keeps the
                // result readable because a window that vanishes makes silent
                // success indistinguishable from failure.
                tmp = Path.Combine(Path.GetTempPath(), "saituls_" + Guid.NewGuid().ToString("N") + ".ps1");
                File.WriteAllText(tmp,
                    scriptBody +
                    "\r\nWrite-Host ''\r\nWrite-Host 'Done. Press Enter to close.'\r\n[void][System.Console]::ReadLine()\r\n" +
                    "exit $__failed\r\n",
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
                    int count = requested;
                    proc.EnableRaisingEvents = true;
                    proc.Exited += delegate(object o, EventArgs e)
                    {
                        int code = 0;
                        try { code = proc.ExitCode; } catch { }
                        try { if (File.Exists(cleanup)) File.Delete(cleanup); } catch { }
                        // The Exited handler runs on a threadpool thread; the
                        // MessageBox belongs on the UI thread, and even without
                        // one this marshals so two rapid operations cannot
                        // interleave their reports.
                        try
                        {
                            if (InvokeRequired) BeginInvoke((MethodInvoker)delegate
                            {
                                ReportElevatedResult(code, count);
                            });
                            else ReportElevatedResult(code, count);
                        }
                        catch { }
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

        void ReportElevatedResult(int exitCode, int requested)
        {
            if (exitCode == 0)
            {
                MessageBox.Show(requested + " feature(s) installed/removed successfully.",
                    "SAITULS", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            else
            {
                MessageBox.Show(
                    exitCode + " of " + requested + " operation(s) FAILED.\n" +
                    "The console window listed which; the failing features were NOT applied.",
                    "SAITULS", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        void LaunchTool(string script, string mode)
        {
            string root = S.RootPath;
            string binDir = Path.Combine(root, "Bin");

            if (mode == "saispin-settings")
            {
                string settingsScript = Path.Combine(root, "Scripts", "saispin_settings.ps1");
                if (!File.Exists(settingsScript))
                {
                    MessageBox.Show("SAISPIN Settings is missing:\n" + settingsScript, "SAITULS");
                    return;
                }
                try
                {
                    Process.Start(new ProcessStartInfo("powershell.exe")
                    {
                        Arguments = "-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"" + settingsScript + "\"",
                        WorkingDirectory = Path.GetDirectoryName(settingsScript),
                        CreateNoWindow = true,
                        UseShellExecute = false
                    });
                }
                catch (Exception ex)
                {
                    MessageBox.Show("Could not start SAISPIN Settings:\n" + ex.Message, "SAITULS");
                }
                return;
            }

            // SAIPATCH: launch the standalone patcher and exit. SAITULS owns no
            // patch logic -- a missing patcher is a visible error, not silence.
            if (mode == "saipatch" || mode == "saipatch-settings" || mode == "saipatch-queue-viewer")
            {
                string saipatchScript;
                if (mode == "saipatch-settings")
                    saipatchScript = Path.Combine(root, "Scripts", "saipatch", "settings.ps1");
                else if (mode == "saipatch-queue-viewer")
                    saipatchScript = Path.Combine(root, "Scripts", "saipatch", "queue.ps1");
                else
                    saipatchScript = Path.Combine(root, "Scripts", "saipatch", "patcher.ps1");
                if (!File.Exists(saipatchScript))
                {
                    MessageBox.Show("SAIPATCH is missing:\n" + saipatchScript, "SAITULS");
                    return;
                }
                try
                {
                    Process.Start(new ProcessStartInfo("powershell.exe")
                    {
                        Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + saipatchScript + "\"",
                        WorkingDirectory = Path.GetDirectoryName(saipatchScript),
                        UseShellExecute = false
                    });
                }
                catch (Exception ex)
                {
                    MessageBox.Show("Could not start SAIPATCH:\n" + ex.Message, "SAITULS");
                }
                return;
            }

            // SECURE APPS / AI CONSOLES: launch the dedicated subsystem and
            // return. SAITULS owns no authentication, key, storage, policy or
            // console logic -- a missing launcher is a visible error.
            if (mode == "secureapps" || mode == "consoles" || mode == "shelldoctor")
            {
                string subsystemScript = mode == "secureapps"
                    ? Path.Combine(root, "Scripts", "secure_apps", "SECURE_APPS.ps1")
                    : mode == "consoles"
                    ? Path.Combine(root, "Scripts", "consoles", "CONSOLES.ps1")
                    : Path.Combine(root, "Scripts", "shell_doctor", "SHELL_DOCTOR.ps1");
                string subsystemName = mode == "secureapps" ? "Secure Apps"
                    : mode == "consoles" ? "AI Consoles" : "Shell Doctor";
                if (!File.Exists(subsystemScript))
                {
                    MessageBox.Show(subsystemName + " is missing:\n" + subsystemScript, "SAITULS");
                    return;
                }
                try
                {
                    // Never elevated from here. Secure Apps raises its own UAC
                    // prompt for the privileged storage helper only; the console
                    // launcher raises none at all.
                    Process.Start(new ProcessStartInfo("powershell.exe")
                    {
                        Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + subsystemScript + "\"",
                        WorkingDirectory = Path.GetDirectoryName(subsystemScript),
                        UseShellExecute = false
                    });
                }
                catch (Exception ex)
                {
                    MessageBox.Show("Could not start " + subsystemName + ":\n" + ex.Message, "SAITULS");
                }
                return;
            }

            // SCENARIOS: launch the dedicated scenario launcher and return.            // The launcher owns the registry, dependency gates, destructive
            // confirmation and per-action admin; SAITULS owns no scenario logic.
            if (mode == "scenarios")
            {
                string scenariosScript = Path.Combine(root, "Scripts", "scenarios", "SCENARIOS.ps1");
                if (!File.Exists(scenariosScript))
                {
                    MessageBox.Show("Scenarios launcher is missing:\n" + scenariosScript, "SAITULS");
                    return;
                }
                try
                {
                    Process.Start(new ProcessStartInfo("powershell.exe")
                    {
                        Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + scenariosScript + "\"",
                        WorkingDirectory = Path.GetDirectoryName(scenariosScript),
                        UseShellExecute = false
                    });
                }
                catch (Exception ex)
                {
                    MessageBox.Show("Could not start Scenarios:\n" + ex.Message, "SAITULS");
                }
                return;
            }

            // ITEM LIST / ITEM TREE: pick a target folder and an output path,
            // run the shared engine (Scripts\item_tools.py) hidden, then offer
            // Open output / Copy output path. All traversal, ordering, atomic
            // publication and error reporting live in the engine.
            if (mode == "itemlist" || mode == "itemtree")
            {
                RunItemTool(mode == "itemtree");
                return;
            }

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
                    string exe = Toolchain.FindOnPath("pythonw.exe") ?? Toolchain.FindOnPath("python.exe");
                    if (exe == null) { MessageBox.Show("Python is missing. Open Home and run Install / repair first.", "SAITULS"); return; }
                    string args = "\"" + fullPath + "\"" + (target != null ? " \"" + target + "\"" : "");
                    Process.Start(new ProcessStartInfo(exe, args) { WorkingDirectory = binDir, UseShellExecute = false });
                }
                else
                {
                    // PATH must carry Bin\ and Bin\App\ so bare tools resolve.
                    string args = "/c set \"PATH=" + binDir + ";" + Path.Combine(binDir, "App") + ";%PATH%\" && call \"" + fullPath + "\"" +
                                  (target != null ? " \"" + target + "\"" : "");
                    Process.Start(new ProcessStartInfo("cmd.exe", args) { WorkingDirectory = binDir, UseShellExecute = false });
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not launch " + script + ":\n" + ex.Message, "SAITULS");
            }
        }

        // ======================================================================
        // MEDIA: readiness, download options, tracked jobs
        // ======================================================================

        // The ONE readiness definition for the media tools. Existence is not
        // readiness: each tool is run with its own --version probe and the same
        // size floors Bin\media_payload.ps1 uses.
        static MediaProbeResult ProbeMedia(string root)
        {
            var result = new MediaProbeResult();
            string bin = Path.Combine(root, "Bin");
            var problems = new List<string>();
            var downloadBlockers = new List<string>();
            var mergeBlockers = new List<string>();

            result.Ffmpeg = Toolchain.Probe(Path.Combine(bin, "FFMPEG.EXE"), "-version", 30L * 1024 * 1024, 10000) ?? "";
            if (result.Ffmpeg.Length > 0)
            {
                problems.Add("FFmpeg " + result.Ffmpeg);
                downloadBlockers.Add("FFmpeg " + result.Ffmpeg);
                mergeBlockers.Add("FFmpeg " + result.Ffmpeg);
            }
            result.Ffprobe = Toolchain.Probe(Path.Combine(bin, "FFPROBE.EXE"), "-version", 10L * 1024 * 1024, 10000) ?? "";
            if (result.Ffprobe.Length > 0)
            {
                problems.Add("FFprobe " + result.Ffprobe);
                mergeBlockers.Add("FFprobe " + result.Ffprobe);
            }
            result.YtDlp = Toolchain.Probe(Path.Combine(bin, "yt-dlp.exe"), "--version", 5L * 1024 * 1024, 15000) ?? "";
            if (result.YtDlp.Length > 0)
            {
                problems.Add("yt-dlp " + result.YtDlp);
                downloadBlockers.Add("yt-dlp " + result.YtDlp);
            }

            // A JavaScript runtime is required by the production download path.
            // Deno ships bundled; Node on PATH is accepted. Reported, but it does
            // not block a download for non-YouTube sources.
            string deno = Path.Combine(bin, "deno.exe");
            if (File.Exists(deno)) result.JsRuntime = Toolchain.Probe(deno, "-V", 10L * 1024 * 1024, 10000) ?? "";
            else
            {
                string node = Toolchain.FindOnPath("node.exe");
                result.JsRuntime = node != null ? (Toolchain.Probe(node, "-v", 0, 10000) ?? "") : "missing";
            }
            if (result.JsRuntime.Length > 0) problems.Add("JavaScript runtime " + result.JsRuntime);

            result.Ready = problems.Count == 0;
            result.Line = result.Ready
                ? "Media tools: READY"
                : "Media tools: " + string.Join("; ", problems.ToArray());
            result.DownloadBlocker = string.Join("\n", downloadBlockers.ToArray());
            result.MergeBlocker = string.Join("\n", mergeBlockers.ToArray());
            return result;
        }

        void EnsureMediaProbe()
        {
            if (_mediaProbeStarted) return;
            _mediaProbeStarted = true;
            string root = S.RootPath;
            ThreadPool.QueueUserWorkItem(delegate
            {
                MediaProbeResult result = ProbeMedia(root);
                TryBeginInvoke(delegate
                {
                    _lastProbe = result;
                    _mediaProbeDone = true;
                    Refresh();
                });
            });
        }

        void ResetMediaProbe()
        {
            _mediaProbeStarted = false;
            _mediaProbeDone = false;
            _lastProbe = null;
        }

        // An explicit action must never act on a stale/missing probe.
        MediaProbeResult CurrentMediaProbe()
        {
            if (_mediaProbeDone && _lastProbe != null) return _lastProbe;
            _lastProbe = ProbeMedia(S.RootPath);
            _mediaProbeStarted = true;
            _mediaProbeDone = true;
            return _lastProbe;
        }

        void MediaAction(string mode)
        {
            if (mode == "file") { PickAndMergeAudio(); return; }
            OpenDownloadDialog(mode);
        }

        void OpenDownloadDialog(string mode)
        {
            string blocker = DownloadBlocker();
            if (blocker != null)
            {
                MessageBox.Show(blocker, "SAITULS - download", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string problem;
            List<string> urls = ClipboardInput.ExtractUrls(out problem);
            MediaProbeResult probe = CurrentMediaProbe();
            using (var dialog = new DownloadDialog(mode, S.RootPath, urls.Count, problem,
                                                   probe.Ready ? "" : probe.Line))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                // The clipboard is inspected again at launch time: it may have
                // changed while the dialog was open, and yt-dlp must never be
                // started with an empty or prose input.
                string lateProblem;
                List<string> current = ClipboardInput.ExtractUrls(out lateProblem);
                if (current.Count == 0)
                {
                    MessageBox.Show("No downloadable URL found in the clipboard.\n\n" + lateProblem,
                        "SAITULS - download", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                StartDownloadJob(dialog.Mode, dialog.Playlist, dialog.Dated, dialog.OutDir, current.Count);
            }
        }

        void StartDownloadJob(string mode, bool playlist, bool dated, string outDir, int urlCount)
        {
            if (DownloadJob != null && !DownloadJob.Finished)
            {
                MessageBox.Show("A download is already running:\n" + DownloadJob.Line +
                    "\n\nWait for it to finish before starting another one.",
                    "SAITULS - download", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string worker = Path.Combine(S.RootPath, "Scripts", "DL_YT.PS1");
            if (!File.Exists(worker))
            {
                MessageBox.Show("The download worker is missing:\n" + worker, "SAITULS - download",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            string args = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + worker + "\" -Mode " + mode +
                          " -OutPath \"" + outDir + "\"";
            if (playlist) args += " -Playlist";
            if (dated) args += " -Dated";
            string log = Path.Combine(Path.GetTempPath(), "saituls-download-" + Guid.NewGuid().ToString("N") + ".log");
            var psi = new ProcessStartInfo("powershell.exe", args)
            {
                WorkingDirectory = S.RootPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            try
            {
                Process p = Process.Start(psi);
                var job = new ToolJob
                {
                    Label = "download (" + mode + ")",
                    Line = mode + (playlist ? " playlist" : "") + (dated ? " dated" : "") +
                           ": RUNNING - " + urlCount + " link(s) from the clipboard -> " + outDir,
                    OutDir = outDir,
                    LogPath = log
                };
                DownloadJob = job;
                CaptureJobOutput(p, job, "Download", false);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not start the download:\n" + ex.Message,
                    "SAITULS - download", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            Refresh();
        }

        string DownloadBlocker()
        {
            string yt = Path.Combine(S.RootPath, "Bin", "yt-dlp.exe");
            if (!File.Exists(yt))
                return "yt-dlp is missing:\n" + yt + "\n\nOpen Home and run Install / repair first.";
            MediaProbeResult probe = CurrentMediaProbe();
            if (probe.DownloadBlocker.Length > 0)
                return "The download cannot run:\n\n" + probe.DownloadBlocker +
                       "\n\nOpen Home and run Install / repair first.";
            return null;
        }

        string MergeBlocker()
        {
            string bin = Path.Combine(S.RootPath, "Bin");
            string ff = Path.Combine(bin, "FFMPEG.EXE");
            string fp = Path.Combine(bin, "FFPROBE.EXE");
            if (!File.Exists(ff))
                return "FFmpeg is missing:\n" + ff + "\n\nOpen Home and run Install / repair first.";
            if (!File.Exists(fp))
                return "FFprobe is missing:\n" + fp +
                       "\n\nMerge Audio validates the audio tracks with ffprobe before it touches the file; " +
                       "open Home and run Install / repair first.";
            MediaProbeResult probe = CurrentMediaProbe();
            if (probe.MergeBlocker.Length > 0)
                return "Merge Audio cannot run:\n\n" + probe.MergeBlocker +
                       "\n\nOpen Home and run Install / repair first.";
            return null;
        }

        void PickAndMergeAudio()
        {
            string blocker = MergeBlocker();
            if (blocker != null)
            {
                MessageBox.Show(blocker, "SAITULS - Merge Audio", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string file;
            using (var of = new OpenFileDialog())
            {
                of.Title = "Merge Audio - pick the video file with exactly two audio tracks";
                of.InitialDirectory = S.RootPath;
                of.Filter = "Media files|*.mkv;*.mp4;*.mov;*.m4v;*.avi;*.webm;*.ts;*.m2ts;*.wmv;*.flv;*.mpg;*.mpeg;*.3gp;*.ogv;*.ogg|All files|*.*";
                of.CheckFileExists = true;
                if (of.ShowDialog(this) != DialogResult.OK) return;
                file = of.FileName;
            }
            StartMergeJob(file);
        }

        void StartMergeJob(string file)
        {
            if (MergeJob != null && !MergeJob.Finished)
            {
                MessageBox.Show("A merge is already running:\n" + MergeJob.Line,
                    "SAITULS - Merge Audio", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string worker = Path.Combine(S.RootPath, "Scripts", "MERGE_AUD.PS1");
            if (!File.Exists(worker))
            {
                MessageBox.Show("The Merge Audio worker is missing:\n" + worker,
                    "SAITULS - Merge Audio", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            var psi = new ProcessStartInfo("powershell.exe",
                "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + worker + "\"")
            {
                WorkingDirectory = Path.GetDirectoryName(worker),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            psi.EnvironmentVariables["MERGE_IN"] = file;
            string log = Path.Combine(Path.GetTempPath(), "saituls-merge-" + Guid.NewGuid().ToString("N") + ".log");
            try
            {
                Process p = Process.Start(psi);
                var job = new ToolJob
                {
                    Label = "merge",
                    Line = Path.GetFileName(file) + ": RUNNING (two audio tracks -> one stereo AAC)",
                    OutDir = Path.GetDirectoryName(file),
                    LogPath = log
                };
                MergeJob = job;
                CaptureJobOutput(p, job, "Merge Audio", true);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not start Merge Audio:\n" + ex.Message,
                    "SAITULS - Merge Audio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            Refresh();
        }

        // ----------------------------------------------------------------------
        // ITEM LIST / ITEM TREE (T-165). The engine (Scripts\item_tools.py) owns
        // traversal, ordering, atomic publication and error reporting. This
        // shell only picks the target and output path and runs the worker.
        // ----------------------------------------------------------------------
        void RunItemTool(bool tree)
        {
            string engine = Path.Combine(S.RootPath, "Scripts", "item_tools.py");
            if (!File.Exists(engine))
            {
                MessageBox.Show("The item tool engine is missing:\n" + engine, "SAITULS", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            string python = Toolchain.FindOnPath("python.exe") ?? Toolchain.FindOnPath("pythonw.exe");
            if (python == null)
            {
                MessageBox.Show("Python is missing. Open Home and run Install / repair first.", "SAITULS");
                return;
            }

            string mode = tree ? "tree" : "flat";
            string target;
            using (var fb = new FolderBrowserDialog())
            {
                fb.Description = "Item " + (tree ? "Tree" : "List") + " - pick the target folder";
                fb.SelectedPath = S.RootPath;
                if (fb.ShowDialog(this) != DialogResult.OK) return;
                target = fb.SelectedPath;
            }

            string suggested = Path.Combine(target, tree ? "item_tree.txt" : "item_list.txt");
            string outPath;
            using (var sf = new SaveFileDialog())
            {
                sf.Title = "Item " + (tree ? "Tree" : "List") + " - save the output";
                sf.InitialDirectory = target;
                sf.FileName = Path.GetFileName(suggested);
                sf.Filter = "Text files|*.txt|All files|*.*";
                sf.OverwritePrompt = false;
                if (sf.ShowDialog(this) != DialogResult.OK) return;
                outPath = sf.FileName;
            }

            var psi = new ProcessStartInfo(python,
                "\"" + engine + "\" --mode " + mode + " --target \"" + target + "\" --out \"" + outPath + "\"")
            {
                WorkingDirectory = Path.GetDirectoryName(engine),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            string log = Path.Combine(Path.GetTempPath(), "saituls-item-" + Guid.NewGuid().ToString("N") + ".log");
            try
            {
                Process p = Process.Start(psi);
                var job = new ToolJob
                {
                    Label = "Item " + (tree ? "Tree" : "List"),
                    Line = (tree ? "Item Tree" : "Item List") + ": RUNNING - " + target,
                    OutDir = Path.GetDirectoryName(outPath),
                    LogPath = log
                };
                ItemJob = job;
                _itemOutPath = outPath;
                _itemMsg = "";
                CaptureJobOutput(p, job, job.Label, false, delegate
                {
                    // Exit 0 = OK, 2 = completed with reported child errors:
                    // both publish a usable output, so both count as produced.
                    if (job.Exit == 0 || job.Exit == 2) job.Ok = true;
                    if (job.Ok && File.Exists(outPath))
                        _itemMsg = "Output written: " + outPath + (job.Exit == 2 ? "  (with reported errors)" : "");
                    else
                        _itemMsg = "No output was published; the previous file (if any) is untouched.";
                });
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not start the item tool:\n" + ex.Message, "SAITULS");
            }
            Refresh();
        }

        // Watches a tool process to its real end and keeps the outcome: exit        // status, destination and the captured log. Never a vanished console
        // with the only error message in it.
        void CaptureJobOutput(Process p, ToolJob job, string title, bool modalOnSuccess)
        {
            CaptureJobOutput(p, job, title, modalOnSuccess, null);
        }

        // Optional onComplete runs inside the completion block AFTER the job
        // record is filled in, so a caller can reclassify the result (for
        // example: exit 2 means "produced, with reported child errors" for the
        // item tools) before the failure/success reporting decides.
        void CaptureJobOutput(Process p, ToolJob job, string title, bool modalOnSuccess, Action onComplete)
        {
            var stdout = new System.Text.StringBuilder();
            var stderr = new System.Text.StringBuilder();
            try
            {
                p.OutputDataReceived += delegate(object o, DataReceivedEventArgs e)
                { if (e.Data != null) lock (stdout) stdout.AppendLine(e.Data); };
                p.ErrorDataReceived += delegate(object o, DataReceivedEventArgs e)
                { if (e.Data != null) lock (stderr) stderr.AppendLine(e.Data); };
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
            }
            catch { }
            ThreadPool.QueueUserWorkItem(delegate
            {
                try { p.WaitForExit(); } catch { }
                int code = -1;
                try { code = p.ExitCode; } catch { }
                string text;
                lock (stdout) text = stdout.ToString();
                lock (stderr) text += "\n" + stderr.ToString();
                try { File.WriteAllText(job.LogPath, text, new System.Text.UTF8Encoding(false)); } catch { }
                job.Exit = code;
                job.Finished = true;
                job.Ok = code == 0;
                if (onComplete != null) { try { onComplete(); } catch { } }
                job.Line = job.Label + ": " + (job.Ok ? "COMPLETED" : "FAILED (exit " + code + ")") +
                           " -> " + job.OutDir;
                TryBeginInvoke(delegate
                {
                    Refresh();
                    if (!job.Ok) ReportJobFailure(job, title);
                    else if (modalOnSuccess) ReportJobSuccess(job, title);
                });
            });
        }

        void ReportJobFailure(ToolJob job, string title)
        {
            string reason = "";
            try
            {
                if (File.Exists(job.LogPath))
                {
                    string text = File.ReadAllText(job.LogPath);
                    foreach (string raw in text.Split('\n'))
                    {
                        string line = raw.Trim();
                        if (line.StartsWith("SAITULS_DL_ERROR="))
                        {
                            reason = line.Substring("SAITULS_DL_ERROR=".Length);
                            break;
                        }
                    }
                    if (reason.Length == 0)
                    {
                        var lines = new List<string>();
                        foreach (string raw in text.Split('\n'))
                        { string line = raw.Trim(); if (line.Length > 0) lines.Add(line); }
                        for (int i = Math.Max(0, lines.Count - 3); i < lines.Count; i++) reason += lines[i] + "\n";
                        reason = reason.Trim();
                    }
                }
            }
            catch { }
            MessageBox.Show(title + " FAILED (exit " + job.Exit + ").\n\n" +
                (reason.Length > 0 ? reason : "See the log for the full output.") +
                "\n\nDestination: " + job.OutDir + "\nLog: " + job.LogPath,
                "SAITULS - " + title, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }

        void ReportJobSuccess(ToolJob job, string title)
        {
            string message = "";
            try
            {
                if (File.Exists(job.LogPath))
                    foreach (string raw in File.ReadAllLines(job.LogPath))
                    { string line = raw.Trim(); if (line.Length > 0) message = line; }
            }
            catch { }
            MessageBox.Show(title + " finished.\n\n" + (message.Length > 0 ? message : "Done.") +
                "\n\nFile: " + job.OutDir, "SAITULS - " + title,
                MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        void TryBeginInvoke(Action action)
        {
            try
            {
                if (IsDisposed || !IsHandleCreated) return;
                if (InvokeRequired) BeginInvoke((MethodInvoker)delegate { action(); });
                else action();
            }
            catch { }
        }

        // ======================================================================
        // CLIPBOARD+ actions and status
        // ======================================================================

        void RefreshClipboardStatus()
        {
            EnsureClipboardDepsProbe();
            if ((DateTime.Now - _clipStatusRead).TotalSeconds < 2.0) return;
            _clipStatusRead = DateTime.Now;
            _clipRunning = ClipboardPlusHelper.Installed(S.RootPath) && ClipboardPlusHelper.Running();
            _clipSavePath = ClipboardPlusHelper.Setting("save_path", "");
            if (_clipSavePath.Length == 0) _clipSavePath = ClipboardPlusHelper.Status("save_path", "");
            if (_clipSavePath.Length == 0) _clipSavePath = @"%USERPROFILE%\Pictures\Clipboard+";
            _clipHotkey = ClipboardPlusHelper.Setting("hotkey", "");
            if (_clipHotkey.Length == 0) _clipHotkey = ClipboardPlusHelper.Status("hotkey", "Ctrl+Alt+Shift+C");
            _clipEffectivePath = ClipboardPlusHelper.Status("effective_save_path", "");
            _clipSaveError = ClipboardPlusHelper.Status("save_error", "");
            _clipHotkeyError = ClipboardPlusHelper.Status("hotkey_error", "");
            _clipSavedCount = ClipboardPlusHelper.Status("saved_count", "0");
            _clipMonitorOn = ClipboardPlusHelper.StatusFlag("monitor");
            _clipHotkeyOn = ClipboardPlusHelper.StatusFlag("hotkey_registered");
            _clipFallbackActive = ClipboardPlusHelper.StatusFlag("fallback_active");
            _clipStatusStale = false;
            if (_clipRunning)
            {
                _clipPathOk = ClipboardPlusHelper.StatusFlag("save_path_ok");
                if (_clipEffectivePath.Length == 0 && _clipPathOk) _clipEffectivePath = _clipSavePath;
                DateTime stamp;
                string updated = ClipboardPlusHelper.Status("updated", "");
                if (updated.Length > 0 && DateTime.TryParse(updated, out stamp))
                    _clipStatusStale = (DateTime.Now - stamp).TotalSeconds > 90;
            }
            else
            {
                // Stopped: report the CONFIGURED folder and make no claim about
                // its usability, because nothing is measuring it right now.
                _clipPathOk = true;
                _clipMonitorOn = false;
                _clipHotkeyOn = false;
            }
        }

        void EnsureClipboardDepsProbe()
        {
            if (_clipDepsStarted) return;
            _clipDepsStarted = true;
            string root = S.RootPath;
            ThreadPool.QueueUserWorkItem(delegate
            {
                string problem = ClipboardPlusHelper.CheckDependencies(root, 20000);
                TryBeginInvoke(delegate
                {
                    _clipDeps = problem;
                    _clipDepsDone = true;
                    Refresh();
                });
            });
        }

        void ClipboardStartAction()
        {
            if (!ClipboardPlusHelper.Installed(S.RootPath))
            {
                MessageBox.Show("Clipboard+ is missing:\n" + ClipboardPlusHelper.ScriptPath(S.RootPath),
                    "SAITULS - Clipboard+", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            if (ClipboardPlusHelper.Pythonw() == null)
            {
                _clipMsg = "DEPENDENCY MISSING: Python (pythonw.exe) was not found on PATH. Use Install / repair on Home.";
                Refresh();
                return;
            }
            string deps = ClipboardPlusHelper.CheckDependencies(S.RootPath, 20000);
            _clipDeps = deps;
            _clipDepsDone = true;
            if (deps != null)
            {
                _clipMsg = "DEPENDENCY MISSING: " + deps;
                MessageBox.Show("Clipboard+ cannot start yet.\n\n" + deps +
                    "\n\nA resident that would die on import is never started. Use Install / repair on Home.",
                    "SAITULS - Clipboard+", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                Refresh();
                return;
            }
            if (ClipboardPlusHelper.Running())
            {
                // Idempotent start: the healthy resident and its RAM-only Safe
                // Copy history are left exactly as they are.
                _clipMsg = "Clipboard+ is already running - the existing process was left untouched.";
                RefreshClipboardStatus();
                Refresh();
                return;
            }
            if (!ClipboardPlusHelper.Start(S.RootPath))
            {
                _clipMsg = "Clipboard+ could not be started (pythonw.exe or the script is unavailable).";
                Refresh();
                return;
            }
            _clipMsg = "Clipboard+ start requested...";
            ArmClipboardWait(true);
        }

        void ClipboardStopAction()
        {
            if (!ClipboardPlusHelper.Running())
            {
                _clipMsg = "Clipboard+ is already stopped.";
                RefreshClipboardStatus();
                Refresh();
                return;
            }
            if (!ClipboardPlusHelper.Stop())
            {
                _clipMsg = "Clipboard+ stop channel is unavailable (no resident stop event). Nothing was killed.";
                Refresh();
                return;
            }
            _clipMsg = "Clipboard+ stop requested...";
            ArmClipboardWait(false);
        }

        void ClipboardSafeCopyAction()
        {
            string text = ClipboardPlusHelper.RunOneShot(S.RootPath, "--sanitize-text-once", 20000);
            _clipMsg = "Safe Copy: " + text;
            RefreshClipboardStatus();
            Refresh();
            MessageBox.Show(text, "SAITULS - Safe Copy", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        void ClipboardRestoreAction()
        {
            string text = ClipboardPlusHelper.RunOneShot(S.RootPath, "--restore-last-safe-copy", 20000);
            _clipMsg = "Restore: " + text;
            RefreshClipboardStatus();
            Refresh();
            MessageBox.Show(text, "SAITULS - Restore original", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        void ClipboardSettingsAction()
        {
            if (!ClipboardPlusHelper.Installed(S.RootPath))
            {
                MessageBox.Show("Clipboard+ is missing:\n" + ClipboardPlusHelper.ScriptPath(S.RootPath),
                    "SAITULS - Clipboard+", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            if (!ClipboardPlusHelper.LaunchSettings(S.RootPath))
            {
                _clipMsg = "Clipboard+ settings could not open (pythonw.exe/tkinter unavailable).";
            }
            else _clipMsg = "Clipboard+ settings opened - Save there, the running resident reloads it.";
            Refresh();
        }

        void ArmClipboardWait(bool wantRunning)
        {
            if (_clipWait != null) { _clipWait.Stop(); _clipWait.Dispose(); _clipWait = null; }
            int ticks = 0;
            _clipWait = new System.Windows.Forms.Timer();
            _clipWait.Interval = 500;
            _clipWait.Tick += delegate
            {
                ticks++;
                bool running = ClipboardPlusHelper.Running();
                _clipStatusRead = DateTime.MinValue;
                RefreshClipboardStatus();
                if (running == wantRunning || ticks >= 12 || _clipWait == null)
                {
                    if (_clipWait != null) { _clipWait.Stop(); _clipWait.Dispose(); _clipWait = null; }
                    _clipMsg = wantRunning
                        ? (running ? "Clipboard+ is RUNNING." : "Clipboard+ did not report RUNNING within 6s - press Stop and check the log/status file.")
                        : (running ? "Clipboard+ is still running after 6s (stop request not acknowledged)." : "Clipboard+ STOPPED.");
                }
                Refresh();
            };
            _clipWait.Start();
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
            ToggleAutostart(keyName, displayName, null);
        }

        // command == null means "this executable, started minimized"; a helper
        // passes its own path instead.
        void ToggleAutostart(string keyName, string displayName, string command)
        {
            try
            {
                bool nowOn;
                // T-133 6.3: the Run key is project-owned registry state, so this
                // write serializes through the same cross-process contract every
                // SAITULS installer/importer uses. A blocked or busy lock skips
                // the write instead of mutating concurrently.
                using (var mutationLock = new Mutex(false, @"Global\SAITULS_REGISTRY_MUTATION"))
                {
                    bool owned = false;
                    try
                    {
                        try { owned = mutationLock.WaitOne(5000); }
                        catch (AbandonedMutexException) { owned = true; }
                        if (!owned)
                        {
                            MessageBox.Show("Another SAITULS registry mutation is in progress. Try again in a moment.", "SAITULS");
                            return;
                        }
                        using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                        {
                            if (rk == null) return;
                            if (AutoStartEnabled(keyName)) { rk.DeleteValue(keyName, false); nowOn = false; }
                            else
                            {
                                rk.SetValue(keyName, command == null
                                    ? "\"" + Application.ExecutablePath + "\" --minimized"
                                    : "\"" + command + "\"");
                                nowOn = true;
                            }
                        }
                    }
                    finally
                    {
                        if (owned) { try { mutationLock.ReleaseMutex(); } catch { } }
                    }
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
            using (var br = new SolidBrush(c)) g.DrawString(s, F(pt), br, (float)x, (float)y);
        }

        void DrawTextCenter(Graphics g, string s, Rectangle r, Color c, int pt, bool bold = false)
        {
            using (var br = new SolidBrush(c))
                g.DrawString(s, F(pt), br, new RectangleF(r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4), Centered);
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
            using (var br = new SolidBrush(selected ? Palette.LINK : Palette.TEXT))
                g.DrawString(label, F(pt), br, new RectangleF(r.X + 2, r.Y + 2, r.Width - 4, r.Height - 4), Centered);
        }

        int TextW(Graphics g, string s, int pt)
        {
            return (int)Math.Ceiling(g.MeasureString(s, F(pt)).Width);
        }

        // Single gate for every button activation with a side effect.
        // Returns false when the press was eaten (repeat/rapid/re-entrant).
        bool TryActivate(Action a)
        {
            if (a == null) return false;
            if (_activating) return false;
            int now = Environment.TickCount;
            if (unchecked(now - _lastActivateTick) < ActivateDebounceMs) return false;
            _lastActivateTick = now;
            _activating = true;
            try { a(); }
            finally { _activating = false; }
            return true;
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
                    if (b.R.Contains(e.Location) && b.A != null) { FocusedButton = i; Action act = b.A; TryActivate(act); return; }
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
                // Hold-repeat: WinForms resends KeyDown while the key is held.
                // One physical press = one activation; the release (KeyUp)
                // re-arms. Without this, holding Enter spawns N processes.
                if (_enterSpaceHeld) { e.Handled = true; e.SuppressKeyPress = true; return; }
                _enterSpaceHeld = true;
                Action action = Buttons[FocusedButton].A;
                TryActivate(action);
                e.Handled = true; e.SuppressKeyPress = true; return;
            }
            base.OnKeyDown(e);
        }

        protected override void OnKeyUp(KeyEventArgs e)
        {
            if (e.KeyCode == Keys.Enter || e.KeyCode == Keys.Space)
            {
                _enterSpaceHeld = false;
                e.Handled = true;
            }
            base.OnKeyUp(e);
        }
    }

    static class Program
    {
        static SaitulsForm _form;

        // Load the frame the shell is about to ask for. Plain new Icon(path)
        // returns 32x32 whatever the caller needs, so the tray and the taskbar
        // end up rescaling it (blur). Falls back to the stock icon.
        public static Icon LoadIcon(string path, int size)
        {
            try
            {
                if (path != null && File.Exists(path)) return new Icon(path, size, size);
            }
            catch { }
            using (Icon fallback = SystemIcons.Application) return (Icon)fallback.Clone();
        }

        [STAThread]
        static void Main(string[] args)
        {
            // Publish the activation channel before ownership. A secondary can
            // now always signal it, even while the primary is still building UI.
            using (var showEvent = new System.Threading.EventWaitHandle(false,
                System.Threading.EventResetMode.AutoReset, "Local\\SaitulsShow"))
            {
                bool createdNew;
                using (var mutex = new System.Threading.Mutex(true, "Local\\SaitulsApp", out createdNew))
                {
                    if (!createdNew)
                    {
                        showEvent.Set();
                        return;
                    }
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
                // Ask for the SMALL icon size: the default new Icon(path) hands
                // back the 32x32 frame, which the shell then downscales into the
                // 16x16 tray slot - exactly the blur UI.md forbids.
                tray.Icon = LoadIcon(s.IcoPath, SystemInformation.SmallIconSize.Width);
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
                    // T-133 6.3: startup reapplies the recorded autostart setting
                    // through the shared cross-process registry contract. Best
                    // effort: a busy lock leaves the Run key untouched this start.
                    using (var mutationLock = new Mutex(false, @"Global\SAITULS_REGISTRY_MUTATION"))
                    {
                        bool owned = false;
                        try
                        {
                            try { owned = mutationLock.WaitOne(5000); }
                            catch (AbandonedMutexException) { owned = true; }
                            if (owned)
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
                        }
                        finally
                        {
                            if (owned) { try { mutationLock.ReleaseMutex(); } catch { } }
                        }
                    }
                }
                catch { }

                // The taskbar edge helper is a desktop primitive, not a SAITULS
                // window: start it when the user has enabled it and it is not
                // already up. Its lifetime is deliberately NOT tied to this
                // process -- hiding to the tray or exiting leaves it running,
                // and the Settings switch is the one explicit stop.
                if (s.TaskbarEdge) { try { TaskbarEdgeHelper.Start(s.RootPath); } catch { } }

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
                    tray.Visible = false;
                    tray.Dispose();
                }
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
