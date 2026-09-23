using System;
using System.Collections.Generic;
using Saituls.TaskbarEdge;

// Deterministic unit tests for the pure TaskbarEdge geometry and state logic.
// Compiled together with Scripts\taskbar_edge\EdgeLogic.cs by
// tests\test_taskbar_edge_logic.ps1. No desktop, no shell, no clock.

static class TaskbarEdgeLogicTests
{
    static int failures;
    static int checks;

    static void Check(string name, bool ok, string detail)
    {
        checks++;
        if (ok) Console.WriteLine("PASS  " + name + (detail.Length > 0 ? "  " + detail : ""));
        else { failures++; Console.WriteLine("FAIL  " + name + (detail.Length > 0 ? "  " + detail : "")); }
    }

    static void Check(string name, bool ok) { Check(name, ok, ""); }

    // ---- sample helpers -------------------------------------------------

    static EdgeSample Sample(long t, long monitor, bool inStrip)
    {
        EdgeSample s = new EdgeSample();
        s.NowMs = t;
        s.HaveMonitor = true;
        s.MonitorId = monitor;
        s.InStrip = inStrip;
        s.AutoHide = true;
        s.TaskbarValid = true;
        s.TaskbarHidden = true;
        s.Generation = 1;
        return s;
    }

    static EdgeStateMachine Machine()
    {
        return new EdgeStateMachine(EdgeConfig.Defaults());
    }

    // ---- geometry -------------------------------------------------------

    static void BottomEdgeDetection()
    {
        EdgeRect m = new EdgeRect(0, 0, 1920, 1080);
        Check("bottom edge: last row is inside", EdgeGeometry.InBottomEdgeStrip(m, 960, 1079, 2));
        Check("bottom edge: second-to-last row is inside", EdgeGeometry.InBottomEdgeStrip(m, 960, 1078, 2));
        Check("bottom edge: third-to-last row is outside", !EdgeGeometry.InBottomEdgeStrip(m, 960, 1077, 2));
        Check("bottom edge: the bottom coordinate itself is off-monitor", !EdgeGeometry.InBottomEdgeStrip(m, 960, 1080, 2));
        Check("bottom edge: top row is outside", !EdgeGeometry.InBottomEdgeStrip(m, 960, 0, 2));
        Check("bottom edge: left column is inside", EdgeGeometry.InBottomEdgeStrip(m, 0, 1079, 2));
        Check("bottom edge: last column is inside", EdgeGeometry.InBottomEdgeStrip(m, 1919, 1079, 2));
        Check("bottom edge: one past the right column is outside", !EdgeGeometry.InBottomEdgeStrip(m, 1920, 1079, 2));
        Check("bottom edge: one before the left column is outside", !EdgeGeometry.InBottomEdgeStrip(m, -1, 1079, 2));
        Check("bottom edge: degenerate monitor never matches", !EdgeGeometry.InBottomEdgeStrip(new EdgeRect(0, 0, 0, 0), 0, 0, 2));
    }

    static void TwoPixelStrip()
    {
        EdgeRect m = new EdgeRect(0, 0, 1920, 1080);
        int inside = 0;
        for (int y = 1070; y < 1080; y++) if (EdgeGeometry.InBottomEdgeStrip(m, 10, y, 2)) inside++;
        Check("2px strip: exactly two rows activate", inside == 2, "rows=" + inside);

        Check("1px strip: only the final row", EdgeGeometry.InBottomEdgeStrip(m, 10, 1079, 1)
            && !EdgeGeometry.InBottomEdgeStrip(m, 10, 1078, 1));

        int wide = 0;
        for (int y = 1070; y < 1080; y++) if (EdgeGeometry.InBottomEdgeStrip(m, 10, y, 5)) wide++;
        Check("5px strip: exactly five rows activate", wide == 5, "rows=" + wide);

        Check("edge_pixels below 1 is clamped to the final row", EdgeGeometry.InBottomEdgeStrip(m, 10, 1079, 0)
            && !EdgeGeometry.InBottomEdgeStrip(m, 10, 1078, 0));
    }

    static void NegativeMonitorCoordinates()
    {
        // Monitor to the LEFT of the primary display.
        EdgeRect left = new EdgeRect(-1920, 0, 0, 1080);
        Check("negative X: left column inside", EdgeGeometry.InBottomEdgeStrip(left, -1920, 1079, 2));
        Check("negative X: last column inside", EdgeGeometry.InBottomEdgeStrip(left, -1, 1079, 2));
        Check("negative X: x=0 belongs to the next monitor", !EdgeGeometry.InBottomEdgeStrip(left, 0, 1079, 2));
        Check("negative X: row above the strip is outside", !EdgeGeometry.InBottomEdgeStrip(left, -960, 1077, 2));

        // Monitor ABOVE the primary display: its bottom edge is y = 0.
        EdgeRect above = new EdgeRect(-1920, -1080, 0, 0);
        Check("negative Y: y=-1 inside", EdgeGeometry.InBottomEdgeStrip(above, -960, -1, 2));
        Check("negative Y: y=-2 inside", EdgeGeometry.InBottomEdgeStrip(above, -960, -2, 2));
        Check("negative Y: y=-3 outside", !EdgeGeometry.InBottomEdgeStrip(above, -960, -3, 2));
        Check("negative Y: y=0 is already the monitor below", !EdgeGeometry.InBottomEdgeStrip(above, -960, 0, 2));
    }

    static void NonPrimaryMonitor()
    {
        EdgeRect right = new EdgeRect(1920, 0, 3840, 1080);
        Check("non-primary: own left column inside", EdgeGeometry.InBottomEdgeStrip(right, 1920, 1079, 2));
        Check("non-primary: own right column inside", EdgeGeometry.InBottomEdgeStrip(right, 3839, 1079, 2));
        Check("non-primary: primary's last column is outside", !EdgeGeometry.InBottomEdgeStrip(right, 1919, 1079, 2));

        // A monitor that does not start at (0,0) and is taller than the primary.
        EdgeRect tall = new EdgeRect(1920, -420, 3480, 1080);
        Check("non-primary offset origin: bottom strip still resolves", EdgeGeometry.InBottomEdgeStrip(tall, 2000, 1079, 2));
        Check("non-primary offset origin: top is not the bottom", !EdgeGeometry.InBottomEdgeStrip(tall, 2000, -420, 2));
    }

    static void HiddenSliverDetection()
    {
        EdgeRect mon = new EdgeRect(-1920, 0, 0, 1080);
        Check("parked bar is reported hidden", EdgeGeometry.IsBottomTaskbarHidden(mon, new EdgeRect(-1920, 1078, 0, 1108)));
        Check("slid-in bar is reported revealed", !EdgeGeometry.IsBottomTaskbarHidden(mon, new EdgeRect(-1920, 1050, 0, 1080)));

        Check("bottom bar shape accepted", EdgeGeometry.LooksLikeBottomBar(mon, new EdgeRect(-1920, 1078, 0, 1108)));
        Check("left-docked bar shape rejected", !EdgeGeometry.LooksLikeBottomBar(mon, new EdgeRect(-1920, 0, -1880, 1080)));
        Check("top-docked bar shape rejected", !EdgeGeometry.LooksLikeBottomBar(mon, new EdgeRect(-1920, 0, 0, 40)));
        Check("empty bar shape rejected", !EdgeGeometry.LooksLikeBottomBar(mon, new EdgeRect(0, 0, 0, 0)));
    }

    // ---- state machine --------------------------------------------------

    static void Dwell()
    {
        EdgeStateMachine m = Machine();
        EdgeDecision d1 = m.Observe(Sample(0, 1, true));
        Check("dwell: first in-strip sample enters but does not reveal", !d1.Reveal && d1.Has(EdgeEvent.EdgeEnter));

        EdgeDecision d2 = m.Observe(Sample(30, 1, true));
        Check("dwell: second sample at the dwell window reveals", d2.Reveal && d2.Has(EdgeEvent.RevealRequest));

        // Same two samples, but too close together for the dwell window.
        EdgeStateMachine fast = Machine();
        fast.Observe(Sample(0, 1, true));
        EdgeDecision early = fast.Observe(Sample(5, 1, true));
        Check("dwell: two samples inside 5ms do not reveal yet", !early.Reveal);
        EdgeDecision later = fast.Observe(Sample(31, 1, true));
        Check("dwell: reveal lands once the dwell window is met", later.Reveal);

        // A single sample can never reveal, however long the gap.
        EdgeStateMachine one = Machine();
        EdgeDecision only = one.Observe(Sample(5000, 1, true));
        Check("dwell: one sample alone never reveals", !only.Reveal);
    }

    static void CrossingStackedMonitorsDoesNotFire()
    {
        // A cursor travelling down through a monitor that is stacked above
        // another clips the strip for a single sample only.
        EdgeStateMachine m = Machine();
        m.Observe(Sample(0, 1, false));
        EdgeDecision clip = m.Observe(Sample(30, 1, true));
        EdgeDecision gone = m.Observe(Sample(60, 2, false));
        Check("monitor seam: a single clipped sample does not reveal", !clip.Reveal);
        Check("monitor seam: leaving emits EDGE_EXIT", gone.Has(EdgeEvent.EdgeExit));
    }

    static void Debounce()
    {
        EdgeStateMachine m = Machine();
        int reveals = 0;
        for (int i = 0; i < 100; i++)
        {
            EdgeSample s = Sample(i * 30, 1, true);
            // After the first reveal the bar is up, so it is no longer hidden.
            s.TaskbarHidden = reveals == 0;
            if (m.Observe(s).Reveal) reveals++;
        }
        Check("debounce: 100 pinned samples produce exactly one reveal", reveals == 1, "reveals=" + reveals);
        Check("debounce: state machine is disarmed while pinned", !m.RevealArmed);
    }

    static void CooldownReArm()
    {
        EdgeConfig cfg = EdgeConfig.Defaults();
        EdgeStateMachine m = new EdgeStateMachine(cfg);
        m.Observe(Sample(0, 1, true));
        Check("cooldown: initial reveal", m.Observe(Sample(30, 1, true)).Reveal);

        // Bar is up: no re-arm regardless of elapsed time.
        EdgeSample up = Sample(30 + cfg.CooldownMs + 500, 1, true);
        up.TaskbarHidden = false;
        Check("cooldown: a visible bar never re-arms", !m.Observe(up).Reveal);

        // Bar hid itself again, but inside the cooldown window.
        EdgeSample earlyHidden = Sample(30 + cfg.CooldownMs - 10, 1, true);
        Check("cooldown: hidden again too early does not re-arm", !m.Observe(earlyHidden).Reveal);

        // Bar hidden again after the cooldown: a fresh reveal is legitimate
        // without the cursor ever leaving the edge.
        EdgeSample lateHidden = Sample(30 + cfg.CooldownMs, 1, true);
        Check("cooldown: hidden again after the cooldown re-arms", m.Observe(lateHidden).Reveal);
    }

    static void LeaveAndReenter()
    {
        EdgeStateMachine m = Machine();
        m.Observe(Sample(0, 1, true));
        Check("leave/re-enter: first reveal", m.Observe(Sample(30, 1, true)).Reveal);

        EdgeDecision left = m.Observe(Sample(60, 1, false));
        Check("leave/re-enter: leaving emits EDGE_EXIT", left.Has(EdgeEvent.EdgeExit) && !left.Reveal);
        Check("leave/re-enter: leaving re-arms", m.RevealArmed);

        EdgeDecision back = m.Observe(Sample(90, 1, true));
        Check("leave/re-enter: returning enters without revealing yet", back.Has(EdgeEvent.EdgeEnter) && !back.Reveal);
        EdgeSample stillUp = Sample(120, 1, true);
        stillUp.TaskbarHidden = false;
        Check("leave/re-enter: second reveal fires after the dwell", m.Observe(stillUp).Reveal);
    }

    static void MonitorSwitch()
    {
        EdgeStateMachine m = Machine();
        m.Observe(Sample(0, 101, true));
        Check("monitor switch: reveal on the first monitor", m.Observe(Sample(30, 101, true)).Reveal);
        Check("monitor switch: current monitor tracked", m.CurrentMonitor == 101, "mon=" + m.CurrentMonitor);

        EdgeDecision hop = m.Observe(Sample(60, 202, true));
        Check("monitor switch: hop emits EDGE_EXIT then EDGE_ENTER",
            hop.Has(EdgeEvent.EdgeExit) && hop.Has(EdgeEvent.EdgeEnter));
        Check("monitor switch: hop does not reveal on the same sample", !hop.Reveal);
        Check("monitor switch: current monitor follows the cursor", m.CurrentMonitor == 202, "mon=" + m.CurrentMonitor);

        EdgeDecision second = m.Observe(Sample(90, 202, true));
        Check("monitor switch: the new monitor reveals after its own dwell", second.Reveal);
    }

    static void NoTaskbarOnSelectedMonitor()
    {
        EdgeStateMachine m = Machine();
        for (int i = 0; i < 10; i++)
        {
            EdgeSample s = Sample(i * 30, 7, true);
            s.TaskbarValid = false;
            Check("no taskbar: sample " + i + " does not reveal", !m.Observe(s).Reveal);
        }
        Check("no taskbar: the machine is still armed", m.RevealArmed);

        // Moving to a monitor that does have one still works.
        m.Observe(Sample(300, 8, true));
        Check("no taskbar: a monitor that has one still reveals", m.Observe(Sample(330, 8, true)).Reveal);
    }

    static void AutoHideDisabled()
    {
        EdgeStateMachine m = Machine();
        for (int i = 0; i < 10; i++)
        {
            EdgeSample s = Sample(i * 30, 1, true);
            s.AutoHide = false;
            Check("autohide off: sample " + i + " does not reveal", !m.Observe(s).Reveal);
        }
        // Auto-hide switched on while the cursor is still pinned.
        Check("autohide off: enabling it mid-dwell reveals", m.Observe(Sample(300, 1, true)).Reveal);
    }

    static void StaleHandleRediscovery()
    {
        EdgeStateMachine m = Machine();
        m.Observe(Sample(0, 1, true));
        Check("rediscovery: first reveal", m.Observe(Sample(30, 1, true)).Reveal);

        EdgeSample pinned = Sample(60, 1, true);
        pinned.TaskbarHidden = false;
        Check("rediscovery: still debounced before the restart", !m.Observe(pinned).Reveal);

        // Explorer restarted: every taskbar HWND is new.
        EdgeSample restarted = Sample(90, 1, true);
        restarted.Generation = 2;
        EdgeDecision d = m.Observe(restarted);
        Check("rediscovery: generation change is reported", d.Has(EdgeEvent.TaskbarRediscovered));
        Check("rediscovery: the new bar is revealed without leaving the edge", d.Reveal);
    }

    static void MissingMonitor()
    {
        EdgeStateMachine m = Machine();
        m.Observe(Sample(0, 1, true));
        m.Observe(Sample(30, 1, true));
        EdgeSample lost = new EdgeSample();
        lost.NowMs = 60;
        lost.HaveMonitor = false;
        lost.Generation = 1;
        EdgeDecision d = m.Observe(lost);
        Check("unresolved monitor: emits EDGE_EXIT and never reveals", d.Has(EdgeEvent.EdgeExit) && !d.Reveal);
        Check("unresolved monitor: leaves the machine armed", m.RevealArmed);
    }

    // ---- configuration --------------------------------------------------

    static void ConfigDefaultsAndValidation()
    {
        EdgeConfig def = EdgeConfig.Defaults();
        Check("config defaults", def.EdgePixels == 2 && def.PollMs == 30 && def.DwellMs == 30
            && def.CooldownMs == 150 && def.RevealStrategy == RevealStrategies.Topmost && !def.Debug);

        Dictionary<string, string> good = new Dictionary<string, string>();
        good["edge_pixels"] = "4";
        good["poll_ms"] = "50";
        good["dwell_ms"] = "40";
        good["cooldown_ms"] = "400";
        good["reveal_strategy"] = "topmost_mousemove";
        good["debug"] = "1";
        List<string> w = new List<string>();
        EdgeConfig parsed = EdgeConfig.Parse(good, w);
        Check("config: valid values are taken", parsed.EdgePixels == 4 && parsed.PollMs == 50
            && parsed.DwellMs == 40 && parsed.CooldownMs == 400
            && parsed.RevealStrategy == RevealStrategies.TopmostMouseMove && parsed.Debug && w.Count == 0);

        string[] keys = { "edge_pixels", "poll_ms", "dwell_ms", "cooldown_ms" };
        string[] tooBig = { "999", "9999", "9999", "999999" };
        for (int i = 0; i < keys.Length; i++)
        {
            Dictionary<string, string> bad = new Dictionary<string, string>();
            bad[keys[i]] = tooBig[i];
            List<string> warn = new List<string>();
            EdgeConfig c = EdgeConfig.Parse(bad, warn);
            bool fellBack = c.EdgePixels == 2 && c.PollMs == 30 && c.DwellMs == 30 && c.CooldownMs == 150;
            Check("config: out-of-range " + keys[i] + " falls back and warns", fellBack && warn.Count == 1);
        }

        Dictionary<string, string> zero = new Dictionary<string, string>();
        zero["edge_pixels"] = "0";
        List<string> zw = new List<string>();
        Check("config: edge_pixels=0 is below the minimum", EdgeConfig.Parse(zero, zw).EdgePixels == 2 && zw.Count == 1);

        Dictionary<string, string> junk = new Dictionary<string, string>();
        junk["poll_ms"] = "not-a-number";
        junk["reveal_strategy"] = "restart_explorer";
        List<string> jw = new List<string>();
        EdgeConfig jc = EdgeConfig.Parse(junk, jw);
        Check("config: junk values fall back and warn", jc.PollMs == 30
            && jc.RevealStrategy == RevealStrategies.Topmost && jw.Count == 2);

        Check("config: null map yields defaults", EdgeConfig.Parse(null, null).PollMs == 30);
        Check("config: negative poll_ms rejected", EdgeConfig.Parse(One("poll_ms", "-5"), null).PollMs == 30);
        Check("config: dwell_ms=0 is legal", EdgeConfig.Parse(One("dwell_ms", "0"), null).DwellMs == 0);
        Check("config: cooldown_ms=0 is legal", EdgeConfig.Parse(One("cooldown_ms", "0"), null).CooldownMs == 0);
    }

    static Dictionary<string, string> One(string k, string v)
    {
        Dictionary<string, string> d = new Dictionary<string, string>();
        d[k] = v;
        return d;
    }

    static void IniParsing()
    {
        string text = "; comment\r\n# another\r\n[taskbar_edge]\r\nedge_pixels=3   ; trailing\r\n\r\npoll_ms = 40\r\nbroken-line\r\n=novalue\r\nDEBUG=1\r\n";
        IDictionary<string, string> map = EdgeConfig.ParseIni(text);
        Check("ini: section and comment lines skipped", !map.ContainsKey("[taskbar_edge]"));
        Check("ini: malformed lines skipped", !map.ContainsKey("broken-line") && !map.ContainsKey(""));
        Check("ini: keys are case-insensitive", map.ContainsKey("debug"));
        Check("ini: value whitespace trimmed", map["poll_ms"] == "40");
        Check("ini: null text yields an empty map", EdgeConfig.ParseIni(null).Count == 0);
        Check("ini: trailing comment stripped from the value", map["edge_pixels"] == "3", "got=" + map["edge_pixels"]);

        List<string> w = new List<string>();
        EdgeConfig c = EdgeConfig.Parse(map, w);
        Check("ini: an annotated value still parses", c.EdgePixels == 3 && w.Count == 0, "warn=" + w.Count);
        Check("ini: clean value on the same file is taken", c.PollMs == 40);

        // A marker that is not preceded by whitespace is part of the value.
        IDictionary<string, string> tight = EdgeConfig.ParseIni("reveal_strategy=a;b\r\nedge_pixels=4#5\r\n");
        Check("ini: ';' without leading whitespace is kept", tight["reveal_strategy"] == "a;b", "got=" + tight["reveal_strategy"]);
        Check("ini: '#' without leading whitespace is kept", tight["edge_pixels"] == "4#5", "got=" + tight["edge_pixels"]);

        // The exact shape TaskbarEdge writes on first run must round-trip to
        // the documented defaults with no warnings at all.
        string shipped =
            "; TaskbarEdge advanced settings. Delete this file to restore defaults.\r\n" +
            "[taskbar_edge]\r\n" +
            "edge_pixels=2      ; 1..32\r\n" +
            "poll_ms=30          ; 10..250\r\n" +
            "dwell_ms=30         ; 0..500\r\n" +
            "cooldown_ms=150     ; 0..5000\r\n" +
            "reveal_strategy=topmost  ; topmost | topmost_mousemove\r\n" +
            "debug=0            ; 1 records transitions only\r\n";
        List<string> sw = new List<string>();
        EdgeConfig sc = EdgeConfig.Parse(EdgeConfig.ParseIni(shipped), sw);
        Check("ini: the shipped default file parses with no warnings", sw.Count == 0, "warn=" + sw.Count);
        Check("ini: the shipped default file yields the documented defaults",
            sc.EdgePixels == 2 && sc.PollMs == 30 && sc.DwellMs == 30 && sc.CooldownMs == 150
            && sc.RevealStrategy == RevealStrategies.Topmost && !sc.Debug);
    }

    static void RevealStrategyNames()
    {
        Check("strategy: topmost known", RevealStrategies.IsKnown(RevealStrategies.Topmost));
        Check("strategy: topmost_mousemove known", RevealStrategies.IsKnown(RevealStrategies.TopmostMouseMove));
        Check("strategy: anything else unknown", !RevealStrategies.IsKnown("setforegroundwindow"));
    }

    static int Main()
    {
        BottomEdgeDetection();
        TwoPixelStrip();
        NegativeMonitorCoordinates();
        NonPrimaryMonitor();
        HiddenSliverDetection();
        Dwell();
        CrossingStackedMonitorsDoesNotFire();
        Debounce();
        CooldownReArm();
        LeaveAndReenter();
        MonitorSwitch();
        NoTaskbarOnSelectedMonitor();
        AutoHideDisabled();
        StaleHandleRediscovery();
        MissingMonitor();
        ConfigDefaultsAndValidation();
        IniParsing();
        RevealStrategyNames();

        Console.WriteLine("TASKBAR_EDGE_LOGIC checks=" + checks + " failures=" + failures);
        return failures == 0 ? 0 : 1;
    }
}
