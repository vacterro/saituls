// Central native-control and locale regression. Uses real native HWNDs and
// PrintWindow, plus disposable copies of the canonical locale registry.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Globalization;
using System.Threading;
using System.Windows.Forms;
using Saituls;
using Saituls.FolderAutomation;

static class ProductNormalizationTest
{
    static int pass, fail;
    static string output;
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr hwnd, uint msg, IntPtr wp, IntPtr lp);
    [DllImport("user32.dll")] static extern uint GetGuiResources(IntPtr process, uint flags);
    [DllImport("user32.dll")] static extern bool GetComboBoxInfo(IntPtr combo, ref ComboInfo info);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hwnd, out Rect rect);
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct ComboInfo { public int Size; public Rect Item, Button; public int State; public IntPtr Combo, Edit, List; }
    static void Check(bool ok, string name) { Console.WriteLine((ok ? "PASS  " : "FAIL  ") + name); if (ok) pass++; else fail++; }
    static Bitmap Capture(Control c)
    {
        c.Refresh(); Application.DoEvents();
        var bmp = new Bitmap(c.ClientSize.Width, c.ClientSize.Height);
        using (Graphics g = Graphics.FromImage(bmp))
        {
            IntPtr dc = g.GetHdc();
            // A native ComboBox does not implement PrintWindow's return
            // contract. WM_PRINT requests the same real client paint directly.
            try { SendMessage(c.Handle, 0x317, dc, new IntPtr(0x14)); }
            finally { g.ReleaseHdc(dc); }
        }
        return bmp;
    }
    static int Light(Bitmap bmp, Rectangle area)
    {
        int count = 0;
        area.Intersect(new Rectangle(Point.Empty, bmp.Size));
        for (int y = area.Top; y < area.Bottom; y++) for (int x = area.Left; x < area.Right; x++)
        { Color p = bmp.GetPixel(x, y); if (p.R > 180 && p.G > 180 && p.B > 180) count++; }
        return count;
    }
    static void Dark(Control c, string name, Rectangle area)
    {
        using (Bitmap bmp = Capture(c))
        {
            bmp.Save(Path.Combine(output, name + ".png"));
            Check(Light(bmp, area) == 0, name + " contains no native light pixels");
            int ink = 0;
            for (int y = 0; y < bmp.Height; y++) for (int x = 0; x < bmp.Width; x++)
                if (bmp.GetPixel(x, y).ToArgb() == Palette.TEXT.ToArgb() || bmp.GetPixel(x, y).ToArgb() == Palette.TEXT2.ToArgb()) ink++;
            Check(ink > 12, name + " retains readable palette text instead of a blank dark capture");
        }
    }
    [STAThread] static int Main(string[] args)
    {
        output = args[0]; Directory.CreateDirectory(output);
        Application.EnableVisualStyles();
        string[] choices = AccountsLoc.Choices;
        Check(choices.Length == 34 && choices[0] == "auto" && choices[1] == "en", "33 canonical bundles plus Auto, English fallback first");
        Check(Array.IndexOf(choices, "et") >= 0 && Array.IndexOf(choices, "ja") >= 0, "choices include non-EN/RU locales");
        foreach (string lang in new[] { "en", "ru", "et" })
        {
            AccountsLoc.Apply(lang);
            Check(AccountsLoc.CurrentLang == lang && Environment.GetEnvironmentVariable("SAITULS_LANG") == lang, lang + " resolves and propagates to children");
            foreach (ProviderEntry e in ProviderCatalog.All)
            {
                Check(e.Description != e.DescriptionKey && e.OperatorNote().Length > 0 && e.AvailabilityTag != "Provider" + e.Availability,
                    lang + " provider " + e.ProviderId + " has help and availability text");
                Check(e.SupportsAddNow == (e.Availability == ProviderAvailability.Available) && ProviderCatalog.ById(e.ProviderId) == e,
                    lang + " leaves provider identity and engine admission semantic");
                if (lang == "ru") Check(!e.OperatorNote().Contains("Planned") && !e.OperatorNote().Contains("Uses your") && !e.OperatorNote().Contains("sign-in") && !e.AvailabilityTag.Contains("Available"),
                    "RU " + e.ProviderId + " has no English provider prose");
            }
        }
        AccountsLoc.Apply("et");
        Check(AccountsLoc.T("ProviderAntigravityHelp").StartsWith("Google sign-in"), "untranslated new provider keys use canonical English fallback");
        var oldCulture = Thread.CurrentThread.CurrentUICulture;
        Thread.CurrentThread.CurrentUICulture = CultureInfo.GetCultureInfo("et-EE");
        AccountsLoc.Apply("auto");
        Check(AccountsLoc.CurrentLang == "et", "Auto resolves a real supported Windows UI culture beyond EN/RU");
        Thread.CurrentThread.CurrentUICulture = CultureInfo.GetCultureInfo("be-BY");
        AccountsLoc.Apply("auto");
        Check(AccountsLoc.CurrentLang == "en", "Auto uses English for an unsupported culture instead of guessing Russian");
        Thread.CurrentThread.CurrentUICulture = oldCulture;
        AccountsLoc.Apply("../outside"); Check(AccountsLoc.CurrentLang == "en", "invalid locale cannot escape bundle directory");
        string bundles = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "i18n", "strings");
        File.WriteAllText(Path.Combine(bundles, "strings.xxm.json"), "{bad");
        typeof(AccountsLoc).GetField("_choices", BindingFlags.Static | BindingFlags.NonPublic).SetValue(null, null);
        Check(Array.IndexOf(AccountsLoc.Choices, "xxm") < 0, "malformed locale is not offered by registry");
        AccountsLoc.Apply("xxm"); Check(AccountsLoc.CurrentLang == "en" && AccountsLoc.T("TabHome") == "Home", "malformed bundle does not prevent startup");
        var settings = new SaitulsSettings(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "preferences"));
        Directory.CreateDirectory(settings.Dir); settings.Language = "et"; settings.Save();
        var restored = new SaitulsSettings(settings.Dir); restored.Load();
        Check(restored.Language == "et", "non-EN/RU language persists in the existing single setting");
        AccountsLoc.Apply("ru");
        using (var form = new Form())
        {
            form.BackColor = Palette.BG; form.ClientSize = new Size(500, 420);
            var tabs = new TabControl { Location = new Point(10, 10), Size = new Size(480, 180) };
            tabs.TabPages.Add(new TabPage("Один") { BackColor = Palette.BG });
            tabs.TabPages.Add(new TabPage("Два") { BackColor = Palette.BG });
            FaUi.StyleTab(tabs); form.Controls.Add(tabs);
            var combo = FaUi.Combo(10, 205, 280, "Первый", "Второй"); form.Controls.Add(combo);
            var code = FaUi.CodeCombo(10, 240, 280, "FaOp_", "is", "matches"); form.Controls.Add(code);
            var list = new ListView { Location = new Point(10, 280), Size = new Size(480, 125) };
            FaUi.Style(list); list.Columns.Add("Название", 240); list.Columns.Add("Состояние", 120);
            list.Items.Add(new ListViewItem(new[] { "Пример", "Готово" })); form.Controls.Add(list);
            form.Show(); Application.DoEvents();
            Dark(tabs, "tabs", new Rectangle(0, 0, 480, 180));
            tabs.Focus(); SendMessage(tabs.Handle, 0x100, new IntPtr(0x27), IntPtr.Zero);
            Check(tabs.SelectedIndex == 1, "native tab keyboard navigation remains functional");
            Dark(tabs, "tabs-focused-selected", new Rectangle(0, 0, 480, 180));
            Dark(combo, "combo", combo.ClientRectangle);
            combo.Focus(); SendMessage(combo.Handle, 0x100, new IntPtr(0x28), IntPtr.Zero);
            Check(combo.SelectedIndex == 1, "ordinary native combo keyboard navigation remains functional");
            Dark(combo, "combo-focused-selected", combo.ClientRectangle);
            combo.DroppedDown = true; Application.DoEvents();
            ComboInfo popup = new ComboInfo { Size = Marshal.SizeOf(typeof(ComboInfo)) };
            Rect popupArea;
            bool foundPopup = GetComboBoxInfo(combo.Handle, ref popup) && GetClientRect(popup.List, out popupArea);
            Check(foundPopup, "ordinary combo exposes its native accessible dropdown");
            if (foundPopup)
            {
                GetClientRect(popup.List, out popupArea);
                using (var bmp = new Bitmap(popupArea.Right, popupArea.Bottom))
                {
                    using (Graphics g = Graphics.FromImage(bmp))
                    { IntPtr dc = g.GetHdc(); try { SendMessage(popup.List, 0x317, dc, new IntPtr(0x14)); } finally { g.ReleaseHdc(dc); } }
                    bmp.Save(Path.Combine(output, "combo-dropdown-selected.png"));
                    Check(Light(bmp, new Rectangle(1, 1, bmp.Width - 2, bmp.Height - 2)) == 0, "native dropdown selected and unselected rows remain dark");
                }
            }
            combo.DroppedDown = false;
            combo.Enabled = false; Dark(combo, "combo-disabled", combo.ClientRectangle); combo.Enabled = true;
            Check(code.DrawMode == DrawMode.OwnerDrawFixed && code.SelectedItem.ToString() == "is", "CodeCombo shares painter while preserving canonical SelectedItem");
            Dark(code, "code-combo", code.ClientRectangle);
            Dark(list, "list-header-remainder-and-body", list.ClientRectangle);
            list.Items[0].Selected = true; list.Items[0].Focused = true; list.Focus();
            Dark(list, "list-selected-focused", list.ClientRectangle);
            using (var plain = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, BackColor = Palette.COMPARE, ForeColor = Palette.TEXT, Size = new Size(280, 24), Location = new Point(305, 205) })
            {
                plain.Items.Add("Native control"); plain.SelectedIndex = 0; form.Controls.Add(plain);
                using (Bitmap bmp = Capture(plain)) Check(Light(bmp, plain.ClientRectangle) > 100, "same pixel oracle rejects the native-light baseline");
            }
            uint gdi = GetGuiResources(System.Diagnostics.Process.GetCurrentProcess().Handle, 0);
            uint user = GetGuiResources(System.Diagnostics.Process.GetCurrentProcess().Handle, 1);
            for (int i = 0; i < 1000; i++)
            { tabs.SelectedIndex = i % 2; combo.SelectedIndex = i % 2; using (Bitmap b = Capture(tabs)) { } using (Bitmap b = Capture(combo)) { } }
            Check(GetGuiResources(System.Diagnostics.Process.GetCurrentProcess().Handle, 0) <= gdi + 2 && GetGuiResources(System.Diagnostics.Process.GetCurrentProcess().Handle, 1) <= user + 2,
                "central native painting reaches a GDI/USER plateau across 1000 selections");
        }
        File.WriteAllText(Path.Combine(bundles, "strings.en.json"), "{bad");
        ((Dictionary<string, Dictionary<string, string>>)typeof(AccountsLoc).GetField("Bundles", BindingFlags.Static | BindingFlags.NonPublic).GetValue(null)).Clear();
        AccountsLoc.Apply("en");
        Check(AccountsLoc.T("ProviderAntigravityHelp").StartsWith("Google sign-in") && AccountsLoc.T("TabHome") == "Home",
            "damaged English bundle still has complete compatibility help and startup labels");
        Console.WriteLine(pass + " PASS / " + fail + " FAIL"); return fail == 0 ? 0 : 1;
    }
}
