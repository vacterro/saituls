using System;
using System.Collections.Generic;
using System.Globalization;

// TaskbarEdge pure logic.
//
// Nothing in this file touches Win32, the clock, the disk or the shell: every
// decision is a function of values handed in by the caller. That is what makes
// bottom-edge detection, dwell, debounce, monitor switching and rediscovery
// testable off a real desktop -- tests/taskbar_edge_logic_tests.cs compiles
// against this exact source.

namespace Saituls.TaskbarEdge
{
    // Signed rectangle. Monitors left of or above the primary display have
    // negative coordinates, so every field here is int, never uint.
    public struct EdgeRect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;

        public EdgeRect(int left, int top, int right, int bottom)
        {
            Left = left; Top = top; Right = right; Bottom = bottom;
        }

        public int Width { get { return Right - Left; } }
        public int Height { get { return Bottom - Top; } }

        public override string ToString()
        {
            return Left.ToString(CultureInfo.InvariantCulture) + "," + Top.ToString(CultureInfo.InvariantCulture)
                + "," + Right.ToString(CultureInfo.InvariantCulture) + "," + Bottom.ToString(CultureInfo.InvariantCulture);
        }
    }

    public static class EdgeGeometry
    {
        // An auto-hidden bottom taskbar keeps a sliver of itself on screen
        // (2px on Windows 10). Any real taskbar is far taller than this, so the
        // slack cleanly separates "parked" from "slid in".
        public const int HiddenSliverSlackPx = 8;

        // The activation strip is the last edge_pixels rows of the monitor's
        // FULL rectangle -- never the work area, which an auto-hidden taskbar
        // does not shrink anyway and a docked appbar would.
        public static bool InBottomEdgeStrip(EdgeRect monitor, int x, int y, int edgePixels)
        {
            if (edgePixels < 1) edgePixels = 1;
            if (monitor.Width <= 0 || monitor.Height <= 0) return false;
            if (x < monitor.Left || x >= monitor.Right) return false;
            if (edgePixels > monitor.Height) edgePixels = monitor.Height;
            return y >= monitor.Bottom - edgePixels && y < monitor.Bottom;
        }

        public static bool ContainsPoint(EdgeRect r, int x, int y)
        {
            return x >= r.Left && x < r.Right && y >= r.Top && y < r.Bottom;
        }

        // True while the bar sits in its parked position under the edge.
        public static bool IsBottomTaskbarHidden(EdgeRect monitor, EdgeRect taskbar)
        {
            return taskbar.Top >= monitor.Bottom - HiddenSliverSlackPx;
        }

        // Fallback-discovery shape test: a bottom-docked bar spans the monitor
        // horizontally and ends at (or just under) the monitor's bottom edge.
        public static bool LooksLikeBottomBar(EdgeRect monitor, EdgeRect taskbar)
        {
            if (taskbar.Width <= 0 || taskbar.Height <= 0) return false;
            if (taskbar.Width < taskbar.Height) return false;
            if (taskbar.Bottom < monitor.Bottom - HiddenSliverSlackPx) return false;
            return taskbar.Right > monitor.Left && taskbar.Left < monitor.Right;
        }
    }

    // Advanced values. Every field is range-checked; a bad value falls back to
    // its own default and is reported as a warning -- never terminates.
    public sealed class EdgeConfig
    {
        public const int DefaultEdgePixels = 2;
        public const int DefaultPollMs = 30;
        public const int DefaultDwellMs = 30;
        public const int DefaultCooldownMs = 150;

        public const int MinEdgePixels = 1, MaxEdgePixels = 32;
        public const int MinPollMs = 10, MaxPollMs = 250;
        public const int MinDwellMs = 0, MaxDwellMs = 500;
        public const int MinCooldownMs = 0, MaxCooldownMs = 5000;

        public int EdgePixels = DefaultEdgePixels;
        public int PollMs = DefaultPollMs;
        public int DwellMs = DefaultDwellMs;
        public int CooldownMs = DefaultCooldownMs;
        public string RevealStrategy = RevealStrategies.Topmost;
        public bool Debug = false;

        public static EdgeConfig Defaults() { return new EdgeConfig(); }

        static int Clamp(IDictionary<string, string> values, string key, int def, int min, int max, IList<string> warnings)
        {
            string raw;
            if (values == null || !values.TryGetValue(key, out raw) || raw == null) return def;
            raw = raw.Trim();
            if (raw.Length == 0) return def;
            int parsed;
            if (!int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed))
            {
                if (warnings != null) warnings.Add(key + "=" + raw + " is not an integer, using " + def);
                return def;
            }
            if (parsed < min || parsed > max)
            {
                if (warnings != null) warnings.Add(key + "=" + parsed + " outside " + min + ".." + max + ", using " + def);
                return def;
            }
            return parsed;
        }

        public static EdgeConfig Parse(IDictionary<string, string> values, IList<string> warnings)
        {
            EdgeConfig c = new EdgeConfig();
            c.EdgePixels = Clamp(values, "edge_pixels", DefaultEdgePixels, MinEdgePixels, MaxEdgePixels, warnings);
            c.PollMs = Clamp(values, "poll_ms", DefaultPollMs, MinPollMs, MaxPollMs, warnings);
            c.DwellMs = Clamp(values, "dwell_ms", DefaultDwellMs, MinDwellMs, MaxDwellMs, warnings);
            c.CooldownMs = Clamp(values, "cooldown_ms", DefaultCooldownMs, MinCooldownMs, MaxCooldownMs, warnings);

            string strategy;
            if (values != null && values.TryGetValue("reveal_strategy", out strategy) && strategy != null && strategy.Trim().Length > 0)
            {
                strategy = strategy.Trim().ToLowerInvariant();
                if (RevealStrategies.IsKnown(strategy)) c.RevealStrategy = strategy;
                else if (warnings != null) warnings.Add("reveal_strategy=" + strategy + " unknown, using " + RevealStrategies.Topmost);
            }

            string debug;
            if (values != null && values.TryGetValue("debug", out debug) && debug != null)
            {
                debug = debug.Trim();
                c.Debug = debug == "1" || string.Equals(debug, "true", StringComparison.OrdinalIgnoreCase);
            }
            return c;
        }

        // Strips a trailing " ; comment" or " # comment" from a value. The
        // marker only counts after whitespace, so "a;b" stays "a;b". The
        // shipped default file annotates every value with its range, and
        // without this the annotation would be part of the number.
        static string StripInlineComment(string value)
        {
            for (int i = 1; i < value.Length; i++)
            {
                if ((value[i] == ';' || value[i] == '#') && (value[i - 1] == ' ' || value[i - 1] == '\t'))
                    return value.Substring(0, i);
            }
            return value;
        }

        // Minimal INI reader: "key=value", ";" and "#" comments, [section]
        // headers ignored. Deliberately tolerant -- a malformed line is skipped,
        // never fatal.
        public static IDictionary<string, string> ParseIni(string text)
        {
            Dictionary<string, string> map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (text == null) return map;
            string[] lines = text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                if (line.Length == 0 || line[0] == ';' || line[0] == '#' || line[0] == '[') continue;
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string key = line.Substring(0, eq).Trim();
                string val = StripInlineComment(line.Substring(eq + 1)).Trim();
                if (key.Length == 0) continue;
                map[key] = val;
            }
            return map;
        }
    }

    public static class RevealStrategies
    {
        // Proven on the target Windows 10 shell: raise the bar's topmost
        // z-order without moving, resizing or activating it.
        public const string Topmost = "topmost";
        // Narrow secondary: the same SetWindowPos plus one shell-directed
        // WM_MOUSEMOVE posted to the bar. No cursor movement, no input
        // injection. Opt-in only.
        public const string TopmostMouseMove = "topmost_mousemove";

        public static bool IsKnown(string s)
        {
            return s == Topmost || s == TopmostMouseMove;
        }
    }

    public enum EdgeEvent
    {
        None = 0,
        EdgeEnter,
        RevealRequest,
        EdgeExit,
        TaskbarRediscovered
    }

    // One observation of the desktop, already resolved by the Win32 adapter.
    public struct EdgeSample
    {
        public long NowMs;
        public bool HaveMonitor;    // cursor resolved to a monitor at all
        public long MonitorId;      // HMONITOR as a signed value
        public bool InStrip;        // cursor inside the bottom activation strip
        public bool AutoHide;       // shell auto-hide currently active
        public bool TaskbarValid;   // a bottom taskbar is resolved for MonitorId
        public bool TaskbarHidden;  // that taskbar sits parked under the edge
        public long Generation;     // bumps whenever taskbar identity changes
    }

    // Up to two transitions can fall out of one sample (leaving monitor A's
    // strip and entering monitor B's). Two fixed slots keep the hot path
    // allocation-free.
    public struct EdgeDecision
    {
        public bool Reveal;
        public EdgeEvent First;
        public EdgeEvent Second;

        public bool Has(EdgeEvent e)
        {
            return First == e || Second == e;
        }
    }

    public sealed class EdgeStateMachine
    {
        readonly EdgeConfig Cfg;

        bool inStrip;
        long currentMonitor;
        long stripEnterMs;
        int stripSamples;
        bool revealArmed = true;
        long lastRevealMs;
        long lastGeneration;
        bool sawGeneration;

        public EdgeStateMachine(EdgeConfig cfg)
        {
            if (cfg == null) cfg = EdgeConfig.Defaults();
            Cfg = cfg;
        }

        public bool InStrip { get { return inStrip; } }
        public bool RevealArmed { get { return revealArmed; } }
        public long CurrentMonitor { get { return currentMonitor; } }

        static void Emit(ref EdgeDecision d, EdgeEvent e)
        {
            if (d.First == EdgeEvent.None) d.First = e;
            else if (d.Second == EdgeEvent.None) d.Second = e;
        }

        public EdgeDecision Observe(EdgeSample s)
        {
            EdgeDecision d = new EdgeDecision();

            // Explorer restarted or the bar was recreated: whatever debounce
            // state referred to the old window is meaningless now.
            if (sawGeneration && s.Generation != lastGeneration)
            {
                Emit(ref d, EdgeEvent.TaskbarRediscovered);
                revealArmed = true;
            }
            lastGeneration = s.Generation;
            sawGeneration = true;

            bool sameMonitor = inStrip && s.HaveMonitor && s.MonitorId == currentMonitor;

            if (inStrip && (!s.HaveMonitor || !s.InStrip || !sameMonitor))
            {
                // Left the strip we were tracking -- including sliding along the
                // bottom into the next monitor, which must re-arm rather than
                // reveal monitor A's bar for monitor B's cursor.
                Emit(ref d, EdgeEvent.EdgeExit);
                inStrip = false;
                stripSamples = 0;
                revealArmed = true;
            }

            if (!s.HaveMonitor || !s.InStrip) return d;

            if (!inStrip)
            {
                inStrip = true;
                currentMonitor = s.MonitorId;
                stripEnterMs = s.NowMs;
                stripSamples = 0;
                Emit(ref d, EdgeEvent.EdgeEnter);
            }

            if (stripSamples < int.MaxValue) stripSamples++;

            // The bar hid itself again after a bounded cooldown: a fresh reveal
            // is legitimate without the cursor ever leaving the edge.
            if (!revealArmed && s.TaskbarHidden && (s.NowMs - lastRevealMs) >= Cfg.CooldownMs)
                revealArmed = true;

            // Two consecutive in-strip samples AND the dwell window. At the
            // 30ms/30ms defaults that is the second sample -- immediate to a
            // human, but enough to refuse a cursor merely crossing the seam
            // between vertically stacked monitors.
            bool dwellSatisfied = stripSamples >= 2 && (s.NowMs - stripEnterMs) >= Cfg.DwellMs;

            if (dwellSatisfied && revealArmed && s.AutoHide && s.TaskbarValid)
            {
                d.Reveal = true;
                Emit(ref d, EdgeEvent.RevealRequest);
                revealArmed = false;
                lastRevealMs = s.NowMs;
            }

            return d;
        }
    }
}
