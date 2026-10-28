using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Saituls;

// Folder Automation — editor windows. The main window keeps only a compact
// status view; anything with real editing gets its own form with room to work.

namespace Saituls.FolderAutomation
{
    internal static class FaUi
    {
        public static Font Face { get { return new Font("Verdana", 8.25f); } }

        public static Label Label(string text, int x, int y, int w = 0)
        {
            Label l = new Label();
            l.Text = text;
            l.Location = new Point(x, y);
            l.Size = new Size(w == 0 ? 400 : w, 18);
            l.AutoSize = w == 0;
            if (w == 0) l.MaximumSize = new Size(600, 0);
            else l.AutoEllipsis = true;
            l.ForeColor = Palette.TEXT2;
            return l;
        }

        public static TextBox Text(string value, int x, int y, int w, int h = 22)
        {
            TextBox t = new TextBox();
            t.Text = value ?? "";
            t.Location = new Point(x, y);
            t.Size = new Size(w, h);
            t.BackColor = Palette.COMPARE;
            t.ForeColor = Palette.TEXT;
            t.BorderStyle = BorderStyle.FixedSingle;
            return t;
        }

        public static Button Btn(string text, int x, int y, int w = 96, int h = 26)
        {
            Button b = new Button();
            b.Text = text;
            b.Location = new Point(x, y);
            b.Size = new Size(w, h);
            b.FlatStyle = FlatStyle.Flat;
            b.BackColor = Palette.RAISED;
            b.ForeColor = Palette.TEXT;
            return b;
        }

        public static CheckBox Check(string text, bool value, int x, int y, int w = 240)
        {
            CheckBox c = new CheckBox();
            c.Text = text;
            c.Checked = value;
            c.Location = new Point(x, y);
            c.Size = new Size(w, 22);
            c.ForeColor = Palette.TEXT;
            c.AutoSize = false;
            return c;
        }

        public static ComboBox Combo(int x, int y, int w, params string[] items)
        {
            ComboBox c = new FaComboBox();
            c.Location = new Point(x, y);
            c.Size = new Size(w, 22);
            c.DropDownStyle = ComboBoxStyle.DropDownList;
            c.BackColor = Palette.COMPARE;
            c.ForeColor = Palette.TEXT;
            c.Items.AddRange(items);
            if (c.Items.Count > 0) c.SelectedIndex = 0;
            return c;
        }

        // Display translated labels while SelectedItem remains the canonical
        // condition code used by the evaluator and persisted configuration.
        public static ComboBox CodeCombo(int x, int y, int w, string prefix, params string[] items)
        {
            ComboBox c = Combo(x, y, w, items);
            ((FaComboBox)c).DisplayText = delegate(string code) { return CodeText(prefix, code); };
            return c;
        }

        public static string CodeText(string prefix, string code)
        {
            string key = prefix + code;
            string text = AccountsLoc.T(key);
            return text == key ? code : text;
        }

        public static string RunSummary(FaRunReport report)
        {
            string text = string.Format(AccountsLoc.T("FaRunSummary"), report.ProfileName, report.Actions, report.Skipped, report.Errors);
            if (report.Bytes > 0) text += AccountsLoc.T("FaRunBytes", report.Bytes.ToString());
            if (!string.IsNullOrEmpty(report.PausedReason)) text += AccountsLoc.T("FaRunPaused", report.PausedReason);
            return text;
        }

        public static void Style(ListView v)
        {
            v.BackColor = Palette.COMPARE;
            v.ForeColor = Palette.TEXT;
            v.BorderStyle = BorderStyle.FixedSingle;
            v.FullRowSelect = true;
            v.View = View.Details;
            v.GridLines = false;
            v.LabelEdit = false;
            if (v.OwnerDraw) return;
            v.OwnerDraw = true;
            v.DrawColumnHeader += delegate(object sender, DrawListViewColumnHeaderEventArgs e)
            {
                PaintCell(e.Graphics, e.Bounds, e.Header.Text, v.Font, false, false, v.Enabled, true);
            };
            v.DrawItem += delegate(object sender, DrawListViewItemEventArgs e)
            {
                if (v.View != View.Details)
                    PaintCell(e.Graphics, e.Bounds, e.Item.Text, v.Font, e.Item.Selected, v.Focused && e.Item.Focused, v.Enabled, false);
            };
            v.DrawSubItem += delegate(object sender, DrawListViewSubItemEventArgs e)
            {
                PaintCell(e.Graphics, e.Bounds, e.SubItem.Text, v.Font, e.Item.Selected, v.Focused && e.Item.Focused, v.Enabled, false);
            };
            FaNativeFrame frame = new FaNativeFrame(v, false);
            frame.Install();
        }

        public static void StyleTab(TabControl t)
        {
            t.BackColor = Palette.BG;
            t.ForeColor = Palette.TEXT;
            if (t.DrawMode == TabDrawMode.OwnerDrawFixed) return;
            t.DrawMode = TabDrawMode.OwnerDrawFixed;
            t.Padding = new Point(14, 4);
            t.DrawItem += delegate(object sender, DrawItemEventArgs e) { PaintTab(t, e.Graphics, e.Index); };
            FaNativeFrame frame = new FaNativeFrame(t, true);
            frame.Install();
        }

        // All native surfaces use this palette, including selected, disabled
        // and keyboard-focus states. A sunken outline carries selection even
        // in grayscale; a separate inner outline carries focus.
        internal static void PaintCell(Graphics g, Rectangle r, string text, Font font,
            bool selected, bool focused, bool enabled, bool raised)
        {
            using (var brush = new SolidBrush(raised || selected ? Palette.RAISED : Palette.COMPARE)) g.FillRectangle(brush, r);
            if (raised || selected)
                ControlPaint.DrawBorder(g, r, selected ? Palette.BDARK : Palette.BEVEL, 1, ButtonBorderStyle.Solid,
                    selected ? Palette.BDARK : Palette.BEVEL, 1, ButtonBorderStyle.Solid,
                    selected ? Palette.BEVEL : Palette.BDARK, 1, ButtonBorderStyle.Solid,
                    selected ? Palette.BEVEL : Palette.BDARK, 1, ButtonBorderStyle.Solid);
            Rectangle label = Rectangle.Inflate(r, -4, -2);
            TextRenderer.DrawText(g, text ?? "", font, label, enabled ? Palette.TEXT : Palette.TEXT2,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding);
            if (focused && r.Width > 6 && r.Height > 6)
                using (var pen = new Pen(Palette.BHL)) g.DrawRectangle(pen, r.X + 2, r.Y + 2, r.Width - 5, r.Height - 5);
        }

        static void PaintTab(TabControl tabs, Graphics g, int index)
        {
            if (index < 0 || index >= tabs.TabCount) return;
            Rectangle r = tabs.GetTabRect(index);
            bool selected = index == tabs.SelectedIndex;
            PaintCell(g, r, tabs.TabPages[index].Text, tabs.Font, selected, selected && tabs.Focused, tabs.Enabled, true);
            if (selected)
                using (var pen = new Pen(Palette.TEXT, 2)) g.DrawLine(pen, r.Left + 4, r.Bottom - 3, r.Right - 4, r.Bottom - 3);
        }

        // OwnerDraw alone leaves the native tab frame/header remainder white.
        // Overlay only those bounded native surfaces after their normal paint;
        // messages, tab navigation and native accessibility remain WinForms'.
        sealed class FaNativeFrame : NativeWindow
        {
            readonly Control control;
            readonly bool tabs;
            [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr wp, IntPtr lp);
            [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hwnd, out NativeRect rect);
            [DllImport("uxtheme.dll", CharSet = CharSet.Unicode)] static extern int SetWindowTheme(IntPtr hwnd, string subApp, string subId);
            [StructLayout(LayoutKind.Sequential)] struct NativeRect { public int Left, Top, Right, Bottom; }
            public FaNativeFrame(Control c, bool isTabs) { control = c; tabs = isTabs; }
            public void Install()
            {
                control.HandleCreated += delegate { Attach(); };
                control.HandleDestroyed += delegate { if (Handle != IntPtr.Zero) ReleaseHandle(); };
                if (control.IsHandleCreated) Attach();
            }
            void Attach()
            {
                IntPtr target = tabs ? control.Handle : SendMessage(control.Handle, 0x101f, IntPtr.Zero, IntPtr.Zero); // LVM_GETHEADER
                if (target == IntPtr.Zero) return;
                SetWindowTheme(target, "", "");
                AssignHandle(target);
            }
            protected override void WndProc(ref Message m)
            {
                base.WndProc(ref m);
                if (m.Msg != 0x0f && m.Msg != 0x318 && m.Msg != 0x317) return; // WM_PAINT / WM_PRINTCLIENT / WM_PRINT
                using (Graphics g = m.Msg != 0x0f && m.WParam != IntPtr.Zero ? Graphics.FromHdc(m.WParam) : Graphics.FromHwnd(Handle))
                {
                    if (tabs)
                    {
                        TabControl t = (TabControl)control;
                        g.ExcludeClip(t.DisplayRectangle);
                        using (var brush = new SolidBrush(Palette.BG)) g.FillRectangle(brush, t.ClientRectangle);
                        Rectangle frame = Rectangle.Inflate(t.DisplayRectangle, 1, 1);
                        using (var pen = new Pen(Palette.BEVEL)) g.DrawRectangle(pen, frame);
                        for (int i = 0; i < t.TabCount; i++) PaintTab(t, g, i);
                    }
                    else
                    {
                        ListView list = (ListView)control;
                        NativeRect area;
                        if (!GetClientRect(Handle, out area)) return;
                        using (var brush = new SolidBrush(Palette.RAISED)) g.FillRectangle(brush, 0, 0, area.Right, area.Bottom);
                        int x = 0;
                        foreach (ColumnHeader column in list.Columns)
                        {
                            PaintCell(g, new Rectangle(x, 0, column.Width, area.Bottom), column.Text, list.Font, false, false, list.Enabled, true);
                            x += column.Width;
                        }
                    }
                }
            }
        }

        internal sealed class FaComboBox : ComboBox
        {
            public Func<string, string> DisplayText = delegate(string item) { return item; };
            public FaComboBox()
            {
                DrawMode = DrawMode.OwnerDrawFixed;
                FlatStyle = FlatStyle.Flat;
            }
            protected override void OnDrawItem(DrawItemEventArgs e)
            {
                string text = e.Index >= 0 && e.Index < Items.Count ? DisplayText(Items[e.Index].ToString()) : "";
                PaintCell(e.Graphics, e.Bounds, text, Font, (e.State & DrawItemState.Selected) != 0,
                    (e.State & DrawItemState.Focus) != 0, Enabled, false);
                base.OnDrawItem(e);
            }
            protected override void WndProc(ref Message m)
            {
                base.WndProc(ref m);
                if (m.Msg != 0x0f && m.Msg != 0x318 && m.Msg != 0x317) return;
                using (Graphics g = m.Msg != 0x0f && m.WParam != IntPtr.Zero ? Graphics.FromHdc(m.WParam) : Graphics.FromHwnd(Handle))
                {
                    Rectangle r = ClientRectangle;
                    PaintCell(g, r, "", Font, false, Focused, Enabled, false);
                    Rectangle label = new Rectangle(0, 0, Math.Max(0, r.Width - 22), r.Height);
                    string text = SelectedItem == null ? "" : DisplayText(SelectedItem.ToString());
                    TextRenderer.DrawText(g, text, Font, Rectangle.Inflate(label, -4, -2), Enabled ? Palette.TEXT : Palette.TEXT2,
                        TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPrefix);
                    Rectangle button = new Rectangle(r.Right - 20, 1, 19, r.Height - 2);
                    PaintCell(g, button, "", Font, DroppedDown, false, Enabled, true);
                    int cx = button.Left + button.Width / 2, cy = button.Top + button.Height / 2;
                    using (var brush = new SolidBrush(Enabled ? Palette.TEXT : Palette.TEXT2))
                        g.FillPolygon(brush, new[] { new Point(cx - 4, cy - 2), new Point(cx + 4, cy - 2), new Point(cx, cy + 2) });
                    using (var pen = new Pen(Focused ? Palette.BHL : Palette.BEVEL)) g.DrawRectangle(pen, 0, 0, r.Width - 1, r.Height - 1);
                }
            }
        }
    }

    // One condition. The common kinds are plain dropdowns so no regex is ever
    // required; the advanced kinds are reachable but never in the way.
    internal sealed class FaConditionForm : Form
    {
        public FaCondition Condition;

        public FaConditionForm(FaCondition existing)
        {
            Condition = existing == null ? new FaCondition() : existing.Clone();
            Text = existing == null ? AccountsLoc.T("FaNewCondition") : AccountsLoc.T("FaEditCondition");
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            MaximizeBox = false;
            MinimizeBox = false;
            ClientSize = new Size(460, 210);
            BackColor = Palette.BG;
            ForeColor = Palette.TEXT;
            Font = FaUi.Face;

            string[] simple =
            {
                "extension", "filename", "filename_contains", "filename_startswith", "filename_endswith",
                "extension_group", "min_size", "max_size", "min_age", "max_age",
                "modified_after", "created_after", "weekday", "time_window"
            };
            string[] advanced = { "glob", "regex", "path", "is_directory", "hidden", "created_before", "modified_before" };

            Controls.Add(FaUi.Label(AccountsLoc.T("FaWhen"), 12, 12, 80));
            ComboBox kinds = FaUi.CodeCombo(12, 30, 240, "FaKind_", simple);
            kinds.Items.AddRange(advanced);
            for (int i = 0; i < kinds.Items.Count; i++)
                if (string.Equals(kinds.Items[i].ToString(), Condition.Kind, StringComparison.OrdinalIgnoreCase))
                    kinds.SelectedIndex = i;
            Controls.Add(kinds);

            Controls.Add(FaUi.Label(AccountsLoc.T("FaTest"), 264, 12, 80));
            ComboBox op = FaUi.CodeCombo(264, 30, 180, "FaOp_", "is_one_of", "is", "contains", "starts", "ends", "matches", "is_inside");
            for (int i = 0; i < op.Items.Count; i++)
                if (string.Equals(op.Items[i].ToString(), Condition.Op, StringComparison.OrdinalIgnoreCase))
                    op.SelectedIndex = i;
            Controls.Add(op);

            Controls.Add(FaUi.Label(AccountsLoc.T("FaValuesHint"), 12, 62, 300));
            TextBox value = FaUi.Text(
                Condition.Values != null && Condition.Values.Length > 0
                    ? string.Join(";", Condition.Values)
                    : Condition.Value, 12, 80, 432);
            Controls.Add(value);

            Label hint = FaUi.Label(AccountsLoc.T("FaConditionExample"), 12, 108, 430);
            hint.ForeColor = Palette.MUTED;
            Controls.Add(hint);

            Button ok = FaUi.Btn("OK", 244, 150);
            ok.Click += delegate
            {
                Condition.Kind = kinds.SelectedItem.ToString();
                Condition.Op = op.SelectedItem.ToString();
                string raw = value.Text.Trim();
                Condition.Value = raw;
                Condition.Values = raw.IndexOf(';') >= 0
                    ? raw.Split(new char[] { ';' }, StringSplitOptions.RemoveEmptyEntries)
                    : new string[0];
                for (int i = 0; i < Condition.Values.Length; i++) Condition.Values[i] = Condition.Values[i].Trim();
                DialogResult = DialogResult.OK;
                Close();
            };
            Controls.Add(ok);

            Button cancel = FaUi.Btn(AccountsLoc.T("Cancel"), 348, 150);
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            Controls.Add(cancel);

            AcceptButton = ok;
            CancelButton = cancel;
        }
    }

    internal sealed class FaRuleEditorForm : Form
    {
        public FaRule Rule;
        readonly ListView _conditions;

        public FaRuleEditorForm(FaRule existing)
        {
            Rule = existing == null ? new FaRule() : existing.Clone();
            Text = existing == null ? AccountsLoc.T("FaNewRule") : AccountsLoc.T("FaEditRule");
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(560, 470);
            BackColor = Palette.BG;
            ForeColor = Palette.TEXT;
            Font = FaUi.Face;

            Controls.Add(FaUi.Label(AccountsLoc.T("FaRuleName"), 12, 12));
            TextBox name = FaUi.Text(Rule.Name, 12, 30, 240);
            Controls.Add(name);

            Controls.Add(FaUi.Label(AccountsLoc.T("FaMatch"), 264, 12));
            ComboBox match = FaUi.Combo(264, 30, 130, AccountsLoc.T("FaMatchAll"), AccountsLoc.T("FaMatchAny"), AccountsLoc.T("FaMatchExclude"));
            match.SelectedIndex = (int)Rule.Match;
            Controls.Add(match);

            CheckBox enabled = FaUi.Check(AccountsLoc.T("FaEditorEnabled"), Rule.Enabled, 406, 32, 140);
            Controls.Add(enabled);

            Controls.Add(FaUi.Label(AccountsLoc.T("FaAction"), 12, 62));
            string[] actions = { AccountsLoc.T("FaActionMove"), AccountsLoc.T("FaActionCopy"), AccountsLoc.T("FaActionRename"), AccountsLoc.T("FaActionIgnore"), AccountsLoc.T("FaActionStop"), AccountsLoc.T("FaActionQuarantine"), AccountsLoc.T("FaActionRecycle"), AccountsLoc.T("FaActionDelete") };
            ComboBox action = FaUi.Combo(12, 80, 180, actions);
            action.SelectedIndex = (int)Rule.Action;
            Controls.Add(action);

            Controls.Add(FaUi.Label(AccountsLoc.T("FaTo"), 204, 62));
            TextBox dest = FaUi.Text(Rule.Destination, 204, 80, 344);
            Controls.Add(dest);

            Controls.Add(FaUi.Label(AccountsLoc.T("FaRenameTemplate"), 12, 110));
            TextBox rename = FaUi.Text(Rule.RenameTemplate, 12, 128, 300);
            Controls.Add(rename);

            Label vars = FaUi.Label("{year} {month} {day} {hour} {minute} {name} {ext} {dot_ext} {profile} {rule}", 12, 152, 520);
            vars.ForeColor = Palette.MUTED;
            Controls.Add(vars);

            CheckBox createDir = FaUi.Check(AccountsLoc.T("FaCreateDestination"), Rule.CreateDir, 320, 118, 240);
            Controls.Add(createDir);

            CheckBox danger = FaUi.Check(AccountsLoc.T("FaAcknowledgeDelete"), Rule.DangerousAck, 320, 140, 240);
            danger.ForeColor = Palette.DANGERTXT;
            Controls.Add(danger);

            Controls.Add(FaUi.Label(AccountsLoc.T("FaAllConditions"), 12, 180));
            _conditions = new ListView();
            _conditions.Location = new Point(12, 198);
            _conditions.Size = new Size(536, 180);
            FaUi.Style(_conditions);
            _conditions.Columns.Add(AccountsLoc.T("FaWhen"), 150);
            _conditions.Columns.Add(AccountsLoc.T("FaTest"), 90);
            _conditions.Columns.Add(AccountsLoc.T("FaValue"), 290);
            Controls.Add(_conditions);
            foreach (FaCondition c in Rule.Conditions) AddConditionRow(c);

            Button add = FaUi.Btn(AccountsLoc.T("FaAdd"), 12, 388, 80);
            add.Click += delegate
            {
                using (FaConditionForm f = new FaConditionForm(null))
                {
                    if (f.ShowDialog(this) != DialogResult.OK) return;
                    Rule.Conditions.Add(f.Condition);
                    AddConditionRow(f.Condition);
                }
            };
            Controls.Add(add);

            Button edit = FaUi.Btn(AccountsLoc.T("FaEdit"), 100, 388, 80);
            edit.Click += delegate
            {
                if (_conditions.SelectedIndices.Count == 0) return;
                int i = _conditions.SelectedIndices[0];
                using (FaConditionForm f = new FaConditionForm(Rule.Conditions[i]))
                {
                    if (f.ShowDialog(this) != DialogResult.OK) return;
                    Rule.Conditions[i] = f.Condition;
                    _conditions.Items.RemoveAt(i);
                    AddConditionRow(f.Condition);
                }
            };
            Controls.Add(edit);

            Button remove = FaUi.Btn(AccountsLoc.T("FaRemove"), 188, 388, 80);
            remove.Click += delegate
            {
                if (_conditions.SelectedIndices.Count == 0) return;
                int i = _conditions.SelectedIndices[0];
                Rule.Conditions.RemoveAt(i);
                _conditions.Items.RemoveAt(i);
            };
            Controls.Add(remove);

            Button ok = FaUi.Btn(AccountsLoc.T("FaSaveRule"), 312, 430, 140);
            Button cancel = FaUi.Btn(AccountsLoc.T("Cancel"), 460, 430, 88);
            Controls.Add(ok);
            Controls.Add(cancel);

            ok.Click += delegate
            {
                Rule.Name = name.Text.Trim();
                Rule.Match = (FaMatch)match.SelectedIndex;
                Rule.Action = (FaAction)action.SelectedIndex;
                Rule.Destination = dest.Text.Trim();
                Rule.RenameTemplate = rename.Text.Trim();
                Rule.CreateDir = createDir.Checked;
                Rule.DangerousAck = danger.Checked;
                Rule.Enabled = enabled.Checked;
                if (Rule.Conditions.Count == 0)
                {
                    MessageBox.Show(this, AccountsLoc.T("FaRuleNeedsCondition"),
                        AccountsLoc.T("FaEditorTitle"), MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                DialogResult = DialogResult.OK;
                Close();
            };
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            AcceptButton = ok;
            CancelButton = cancel;
        }

        void AddConditionRow(FaCondition c)
        {
            ListViewItem it = new ListViewItem(FaUi.CodeText("FaKind_", c.Kind));
            it.SubItems.Add(FaUi.CodeText("FaOp_", c.Op));
            it.SubItems.Add(c.Values != null && c.Values.Length > 0 ? string.Join(";", c.Values) : c.Value);
            _conditions.Items.Add(it);
        }
    }

    internal sealed class FaProfileEditorForm : Form
    {
        public FaProfile Profile;
        bool _newProfile;

        public FaProfileEditorForm(FaProfile existing, FaConfig config)
        {
            _newProfile = existing == null;
            Profile = existing == null ? new FaProfile() : existing.Clone();
            if (_newProfile && string.IsNullOrEmpty(Profile.Id)) Profile.Id = FaStore.NewId();

            Text = _newProfile ? AccountsLoc.T("FaNewProfile") : AccountsLoc.T("FaEditProfile") + Profile.Name;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(640, 520);
            BackColor = Palette.BG;
            ForeColor = Palette.TEXT;
            Font = FaUi.Face;

            TabControl tabs = new TabControl();
            tabs.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            tabs.Location = new Point(8, 8);
            tabs.Size = new Size(624, 466);
            FaUi.StyleTab(tabs);
            Controls.Add(tabs);

            TabPage sources = NewTab(tabs, AccountsLoc.T("FaSourceTab"));
            TabPage rules = NewTab(tabs, AccountsLoc.T("FaRulesTab"));
            TabPage general = NewTab(tabs, AccountsLoc.T("FaGeneralTab"));
            TabPage schedule = NewTab(tabs, AccountsLoc.T("FaScheduleTab"));
            TabPage safety = NewTab(tabs, AccountsLoc.T("FaSafetyTab"));
            TabPage advanced = NewTab(tabs, AccountsLoc.T("FaAdvancedTab"));

            // ---- General
            general.Controls.Add(FaUi.Label(AccountsLoc.T("FaName"), 12, 12));
            TextBox name = FaUi.Text(Profile.Name, 12, 30, 280);
            general.Controls.Add(name);
            CheckBox enabled = FaUi.Check(AccountsLoc.T("FaEditorEnabled"), Profile.Enabled, 320, 32, 200);
            general.Controls.Add(enabled);
            general.Controls.Add(FaUi.Label(AccountsLoc.T("FaDestinationHint"), 12, 62));
            TextBox root = FaUi.Text(Profile.DestinationRoot, 12, 80, 420);
            general.Controls.Add(root);
            Button browseRoot = FaUi.Btn(AccountsLoc.T("Browse"), 440, 79, 90);
            general.Controls.Add(browseRoot);
            browseRoot.Click += delegate
            {
                using (FolderBrowserDialog fb = new FolderBrowserDialog())
                {
                    if (Directory.Exists(root.Text)) fb.SelectedPath = root.Text;
                    if (fb.ShowDialog(this) == DialogResult.OK) root.Text = fb.SelectedPath;
                }
            };

            // ---- Sources
            ListView src = new ListView();
            src.Location = new Point(12, 12);
            src.Size = new Size(600, 300);
            FaUi.Style(src);
            src.Columns.Add(AccountsLoc.T("FaSourceFolder"), 460);
            src.Columns.Add(AccountsLoc.T("FaState"), 130);
            sources.Controls.Add(src);
            CheckBox recursive = FaUi.Check(AccountsLoc.T("FaIncludeSubfolders"), Profile.Recursive, 12, 320, 220);
            sources.Controls.Add(recursive);
            foreach (string s in Profile.Sources)
            {
                ListViewItem it = new ListViewItem(s);
                it.SubItems.Add(Directory.Exists(FaPaths.Normalize(s)) ? AccountsLoc.T("FaReachable") : AccountsLoc.T("FaUnreachable"));
                src.Items.Add(it);
            }
            Button addSrc = FaUi.Btn(AccountsLoc.T("FaAddSource"), 12, 352, 150);
            addSrc.Click += delegate
            {
                using (FolderBrowserDialog fb = new FolderBrowserDialog())
                    if (fb.ShowDialog(this) == DialogResult.OK)
                    {
                        ListViewItem it = new ListViewItem(fb.SelectedPath);
                        it.SubItems.Add(Directory.Exists(fb.SelectedPath) ? AccountsLoc.T("FaReachable") : AccountsLoc.T("FaUnreachable"));
                        src.Items.Add(it);
                    }
            };
            sources.Controls.Add(addSrc);
            Button delSrc = FaUi.Btn(AccountsLoc.T("FaRemove"), 170, 352, 96);
            delSrc.Click += delegate
            {
                foreach (ListViewItem it in src.SelectedItems) src.Items.Remove(it);
            };
            sources.Controls.Add(delSrc);

            // ---- Rules
            ListView rulesView = new ListView();
            rulesView.Location = new Point(12, 12);
            rulesView.Size = new Size(600, 330);
            FaUi.Style(rulesView);
            rulesView.Columns.Add("#", 30);
            rulesView.Columns.Add(AccountsLoc.T("FaRule"), 160);
            rulesView.Columns.Add(AccountsLoc.T("FaWhen"), 240);
            rulesView.Columns.Add(AccountsLoc.T("FaAction"), 80);
            rulesView.Columns.Add(AccountsLoc.T("FaTo"), 180);
            rules.Controls.Add(rulesView);
            RefreshRules(rulesView);

            Button addRule = FaUi.Btn(AccountsLoc.T("FaAddRule"), 12, 352, 96);
            addRule.Click += delegate
            {
                using (FaRuleEditorForm f = new FaRuleEditorForm(null))
                {
                    if (f.ShowDialog(this) != DialogResult.OK) return;
                    Profile.Rules.Add(f.Rule);
                    RefreshRules(rulesView);
                }
            };
            rules.Controls.Add(addRule);
            Button editRule = FaUi.Btn(AccountsLoc.T("FaEdit"), 116, 352, 96);
            editRule.Click += delegate
            {
                int i = rulesView.SelectedIndices.Count == 0 ? -1 : rulesView.SelectedIndices[0];
                if (i < 0) return;
                using (FaRuleEditorForm f = new FaRuleEditorForm(Profile.Rules[i]))
                {
                    if (f.ShowDialog(this) != DialogResult.OK) return;
                    Profile.Rules[i] = f.Rule;
                    RefreshRules(rulesView);
                }
            };
            rules.Controls.Add(editRule);
            Button delRule = FaUi.Btn(AccountsLoc.T("FaRemove"), 220, 352, 96);
            delRule.Click += delegate
            {
                int i = rulesView.SelectedIndices.Count == 0 ? -1 : rulesView.SelectedIndices[0];
                if (i < 0) return;
                Profile.Rules.RemoveAt(i);
                RefreshRules(rulesView);
            };
            rules.Controls.Add(delRule);
            Button upRule = FaUi.Btn(AccountsLoc.T("FaUp"), 324, 352, 70);
            upRule.Click += delegate
            {
                int i = rulesView.SelectedIndices.Count == 0 ? -1 : rulesView.SelectedIndices[0];
                if (i <= 0) return;
                FaRule r = Profile.Rules[i]; Profile.Rules[i] = Profile.Rules[i - 1]; Profile.Rules[i - 1] = r;
                RefreshRules(rulesView, i - 1);
            };
            rules.Controls.Add(upRule);
            Button downRule = FaUi.Btn(AccountsLoc.T("FaDown"), 400, 352, 70);
            downRule.Click += delegate
            {
                int i = rulesView.SelectedIndices.Count == 0 ? -1 : rulesView.SelectedIndices[0];
                if (i < 0 || i >= Profile.Rules.Count - 1) return;
                FaRule r = Profile.Rules[i]; Profile.Rules[i] = Profile.Rules[i + 1]; Profile.Rules[i + 1] = r;
                RefreshRules(rulesView, i + 1);
            };
            rules.Controls.Add(downRule);

            // ---- Schedule
            // FaTrigger.Manual is zero. Keep display order identical to the
            // persisted enum so choosing "Manual only" stays manual.
            string[] triggers =
            {
                AccountsLoc.T("FaTriggerManual"),
                AccountsLoc.T("FaTriggerRealtime"), AccountsLoc.T("FaTriggerEveryMinutes"), AccountsLoc.T("FaTriggerHourly"), AccountsLoc.T("FaTriggerDaily"),
                AccountsLoc.T("FaTriggerWeekly"), AccountsLoc.T("FaTriggerStartup"), AccountsLoc.T("FaTriggerResume"), AccountsLoc.T("FaTriggerPathAvailable"),
                AccountsLoc.T("FaTriggerOnce")
            };
            schedule.Controls.Add(FaUi.Label(AccountsLoc.T("FaSchedulePrompt"), 12, 12));
            ComboBox trig = FaUi.Combo(12, 30, 300, triggers);
            trig.SelectedIndex = (int)Profile.Schedule.Trigger;
            schedule.Controls.Add(trig);
            schedule.Controls.Add(FaUi.Label(AccountsLoc.T("FaEveryMinutes"), 12, 62));
            TextBox every = FaUi.Text(Profile.Schedule.EveryMinutes.ToString(), 12, 80, 90);
            schedule.Controls.Add(every);
            schedule.Controls.Add(FaUi.Label(AccountsLoc.T("FaAtTime"), 120, 62));
            TextBox at = FaUi.Text(Profile.Schedule.At, 120, 80, 90);
            schedule.Controls.Add(at);
            schedule.Controls.Add(FaUi.Label(AccountsLoc.T("FaWeekday"), 228, 62));
            TextBox weekday = FaUi.Text(Profile.Schedule.Weekday.ToString(), 228, 80, 60);
            schedule.Controls.Add(weekday);
            schedule.Controls.Add(FaUi.Label(AccountsLoc.T("FaReconcileMinutes"), 368, 62));
            TextBox reconcile = FaUi.Text(Profile.ReconcileMinutes.ToString(), 368, 80, 90);
            schedule.Controls.Add(reconcile);
            schedule.Controls.Add(FaUi.Label(AccountsLoc.T("FaMissedPrompt"), 12, 112));
            ComboBox missed = FaUi.Combo(12, 130, 300, AccountsLoc.T("FaMissedRunAsap"), AccountsLoc.T("FaMissedSkip"), AccountsLoc.T("FaMissedRunOnce"));
            missed.SelectedIndex = (int)Profile.Schedule.Missed;
            schedule.Controls.Add(missed);
            Label missedNote = FaUi.Label(AccountsLoc.T("FaMissedHint"), 12, 156, 560);
            missedNote.ForeColor = Palette.MUTED;
            schedule.Controls.Add(missedNote);

            // ---- Safety
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaQuietSeconds"), 12, 12));
            TextBox quiet = FaUi.Text(Profile.QuietSeconds.ToString(), 12, 30, 90);
            safety.Controls.Add(quiet);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaMaxOperations"), 120, 12));
            TextBox maxOps = FaUi.Text(Profile.Limits.MaxOps.ToString(), 120, 30, 90);
            safety.Controls.Add(maxOps);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaMaxBytes"), 228, 12));
            TextBox maxBytes = FaUi.Text(Profile.Limits.MaxBytes.ToString(), 228, 30, 120);
            safety.Controls.Add(maxBytes);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaMaxErrors"), 366, 12));
            TextBox maxErr = FaUi.Text(Profile.Limits.MaxErrors.ToString(), 366, 30, 80);
            safety.Controls.Add(maxErr);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaWarnOperations"), 120, 62));
            TextBox warnOps = FaUi.Text(Profile.Limits.WarnOps.ToString(), 120, 80, 90);
            safety.Controls.Add(warnOps);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaCollisionPolicy"), 228, 62));
            ComboBox coll = FaUi.Combo(228, 80, 200,
                AccountsLoc.T("FaCollisionSkip"), AccountsLoc.T("FaCollisionCounter"), AccountsLoc.T("FaCollisionTimestamp"), AccountsLoc.T("FaCollisionReplace"), AccountsLoc.T("FaCollisionKeepNewest"),
                AccountsLoc.T("FaCollisionKeepOldest"), AccountsLoc.T("FaCollisionKeepLarger"), AccountsLoc.T("FaCollisionKeepSmaller"));
            coll.SelectedIndex = (int)Profile.Collision;
            safety.Controls.Add(coll);
            Label collNote = FaUi.Label(AccountsLoc.T("FaCollisionHint"), 12, 108, 560);
            collNote.ForeColor = Palette.MUTED;
            safety.Controls.Add(collNote);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaVerifyCopy"), 12, 134));
            ComboBox verify = FaUi.Combo(12, 152, 200, AccountsLoc.T("FaVerifyNone"), AccountsLoc.T("FaVerifySize"), AccountsLoc.T("FaVerifyHash"));
            verify.SelectedIndex = (int)Profile.Verify;
            safety.Controls.Add(verify);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaRetryAttempts"), 228, 134));
            TextBox attempts = FaUi.Text(Profile.Retry.Attempts.ToString(), 228, 152, 60);
            safety.Controls.Add(attempts);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaRetryBackoff"), 452, 134));
            TextBox backoff = FaUi.Text(Profile.Retry.BackoffMs.ToString(), 452, 152, 70);
            safety.Controls.Add(backoff);
            safety.Controls.Add(FaUi.Label(AccountsLoc.T("FaQuarantineFolder"), 12, 184));
            TextBox quarantine = FaUi.Text(Profile.Quarantine, 12, 202, 420);
            safety.Controls.Add(quarantine);

            // ---- Advanced
            advanced.Controls.Add(FaUi.Label(AccountsLoc.T("FaTempPatterns"), 12, 12));
            TextBox temp = FaUi.Text(string.Join(";", Profile.TempPatterns.ToArray()), 12, 30, 480);
            advanced.Controls.Add(temp);
            CheckBox overrideTemp = FaUi.Check(AccountsLoc.T("FaOverridePatterns"), Profile.OverrideTempPatterns, 12, 58, 380);
            advanced.Controls.Add(overrideTemp);
            advanced.Controls.Add(FaUi.Label(AccountsLoc.T("FaExcludedPatterns"), 12, 88));
            TextBox excl = FaUi.Text(string.Join(";", Profile.Exclusions.ToArray()), 12, 106, 480);
            advanced.Controls.Add(excl);
            CheckBox reparse = FaUi.Check(AccountsLoc.T("FaFollowLinks"), Profile.FollowReparse, 12, 136, 480);
            advanced.Controls.Add(reparse);
            CheckBox watchWrite = FaUi.Check(AccountsLoc.T("FaWatchChanges"), Profile.WatchLastWrite, 12, 162, 480);
            advanced.Controls.Add(watchWrite);
            Label advNote = FaUi.Label(AccountsLoc.T("FaConfigurationPath") + FaPaths.ConfigPath(), 12, 196, 600);
            advNote.ForeColor = Palette.MUTED;
            advanced.Controls.Add(advNote);

            Button ok = FaUi.Btn(AccountsLoc.T("FaSaveProfile"), 356, 482, 164);
            Button cancel = FaUi.Btn(AccountsLoc.T("Cancel"), 528, 482, 100);
            Controls.Add(ok);
            Controls.Add(cancel);
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; Close(); };
            ok.Click += delegate
            {
                Profile.Name = name.Text.Trim();
                Profile.DestinationRoot = root.Text.Trim();
                Profile.Recursive = recursive.Checked;
                Profile.Schedule.Trigger = (FaTrigger)trig.SelectedIndex;
                Profile.Trigger = Profile.Schedule.Trigger;
                Profile.Schedule.EveryMinutes = Int(every.Text, 15);
                Profile.Schedule.At = at.Text.Trim();
                Profile.Schedule.Weekday = Int(weekday.Text, 1);
                Profile.Schedule.Missed = (FaMissed)missed.SelectedIndex;
                Profile.ReconcileMinutes = Int(reconcile.Text, 30);
                Profile.QuietSeconds = Int(quiet.Text, 10);
                Profile.Limits.MaxOps = Int(maxOps.Text, 500);
                Profile.Limits.MaxBytes = Long(maxBytes.Text, 4294967296L);
                Profile.Limits.MaxErrors = Int(maxErr.Text, 50);
                Profile.Limits.WarnOps = Int(warnOps.Text, 200);
                Profile.Collision = (FaCollision)coll.SelectedIndex;
                Profile.Verify = (FaVerify)verify.SelectedIndex;
                Profile.Retry.Attempts = Int(attempts.Text, 3);
                Profile.Retry.BackoffMs = Int(backoff.Text, 500);
                Profile.Quarantine = quarantine.Text.Trim();
                Profile.TempPatterns = SplitList(temp.Text);
                Profile.OverrideTempPatterns = overrideTemp.Checked;
                Profile.Exclusions = SplitList(excl.Text);
                Profile.FollowReparse = reparse.Checked;
                Profile.WatchLastWrite = watchWrite.Checked;
                Profile.Sources = new List<string>();
                foreach (ListViewItem it in src.Items) Profile.Sources.Add(it.Text);

                List<string> problems = Problems(config);
                if (problems.Count > 0)
                {
                    MessageBox.Show(this, string.Join(Environment.NewLine, problems.ToArray()),
                        AccountsLoc.T("FaEditorTitle"), MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    if (enabled.Checked) enabled.Checked = false;   // unsafe edit never lands enabled
                    return;
                }
                Profile.Enabled = enabled.Checked;
                DialogResult = DialogResult.OK;
                Close();
            };
            AcceptButton = ok;
            CancelButton = cancel;
        }

        TabPage NewTab(TabControl tabs, string title)
        {
            TabPage p = new TabPage(title);
            p.BackColor = Palette.BG;
            p.ForeColor = Palette.TEXT;
            tabs.TabPages.Add(p);
            return p;
        }

        List<string> Problems(FaConfig config)
        {
            List<string> outp = new List<string>();
            if (string.IsNullOrEmpty(Profile.Name)) outp.Add(AccountsLoc.T("FaProfileNeedsName"));
            if (Profile.Sources.Count == 0) outp.Add(AccountsLoc.T("FaProfileNeedsSource"));
            if (Profile.Enabled)
            {
                foreach (FaTopologyIssue i in FaTopology.Inspect(Profile))
                    outp.Add(i.Detail + AccountsLoc.T("FaSavedDisabled"));
                if (Profile.Rules.Count == 0) outp.Add(AccountsLoc.T("FaNoRulesDisabled"));
            }
            FaConfig probe = new FaConfig();
            probe.Profiles.Add(Profile);
            foreach (string e in FaConfigValidator.Validate(probe).Errors) outp.Add(e);
            return outp;
        }

        void RefreshRules(ListView v) { RefreshRules(v, -1); }

        void RefreshRules(ListView v, int select)
        {
            v.Items.Clear();
            for (int i = 0; i < Profile.Rules.Count; i++)
            {
                FaRule r = Profile.Rules[i];
                ListViewItem it = new ListViewItem((i + 1).ToString());
                it.SubItems.Add(r.Name + (r.Enabled ? "" : AccountsLoc.T("FaOffSuffix")));
                it.SubItems.Add(DescribeConditions(r));
                it.SubItems.Add(FaUi.CodeText("FaAction", r.Action.ToString()));
                it.SubItems.Add(r.Action == FaAction.Rename ? r.RenameTemplate : r.Destination);
                v.Items.Add(it);
            }
            if (select >= 0 && select < v.Items.Count) v.Items[select].Selected = true;
        }

        public static string DescribeConditions(FaRule r)
        {
            List<string> parts = new List<string>();
            foreach (FaCondition c in r.Conditions)
            {
                string v = c.Values != null && c.Values.Length > 0 ? string.Join("/", c.Values) : c.Value;
                parts.Add(FaUi.CodeText("FaKind_", c.Kind) + " " + (string.IsNullOrEmpty(v) ? "" : v));
            }
            string glue = r.Match == FaMatch.All ? AccountsLoc.T("FaAnd") : (r.Match == FaMatch.Any ? AccountsLoc.T("FaOr") : AccountsLoc.T("FaUnless"));
            return string.Join(glue, parts.ToArray());
        }

        static List<string> SplitList(string s)
        {
            List<string> outp = new List<string>();
            if (string.IsNullOrEmpty(s)) return outp;
            foreach (string part in s.Split(new char[] { ';', ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string t = part.Trim();
                if (t.Length > 0) outp.Add(t);
            }
            return outp;
        }

        static int Int(string s, int def)
        {
            int v;
            return int.TryParse(s, out v) ? v : def;
        }

        static long Long(string s, long def)
        {
            long v;
            return long.TryParse(s, out v) ? v : def;
        }
    }

    internal sealed class FaHistoryForm : Form
    {
        readonly FaEngine _engine;
        readonly ListView _view;
        public bool Undone;

        public FaHistoryForm(FaEngine engine)
        {
            _engine = engine;
            Text = AccountsLoc.T("FaHistoryTitle");
            FormBorderStyle = FormBorderStyle.Sizable;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(760, 460);
            BackColor = Palette.BG;
            ForeColor = Palette.TEXT;
            Font = FaUi.Face;

            _view = new ListView();
            _view.Location = new Point(8, 8);
            _view.Size = new Size(744, 400);
            _view.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom;
            FaUi.Style(_view);
            _view.Columns.Add(AccountsLoc.T("FaWhen"), 130);
            _view.Columns.Add(AccountsLoc.T("FaProfiles"), 110);
            _view.Columns.Add(AccountsLoc.T("FaRule"), 110);
            _view.Columns.Add(AccountsLoc.T("FaAction"), 80);
            _view.Columns.Add(AccountsLoc.T("FaFile"), 200);
            _view.Columns.Add(AccountsLoc.T("FaResult"), 110);
            Controls.Add(_view);

            Button refresh = FaUi.Btn(AccountsLoc.T("Refresh"), 8, 416, 90);
            refresh.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            refresh.Click += delegate { Reload(); };
            Controls.Add(refresh);

            Button undo = FaUi.Btn(AccountsLoc.T("FaUndoSelected"), 106, 416, 168);
            undo.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            undo.Click += delegate
            {
                if (_view.SelectedIndices.Count == 0) { MessageBox.Show(this, AccountsLoc.T("FaPickRow"), "SAITULS"); return; }
                DoUndo(((ListViewItem)_view.SelectedItems[0]).Tag as FaJournalEntry);
            };
            Controls.Add(undo);

            Button undoLast = FaUi.Btn(AccountsLoc.T("FaUndoLast"), 282, 416, 168);
            undoLast.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            undoLast.Click += delegate
            {
                foreach (FaJournalEntry e in _engine.Journal.Recent(200))
                    if (e.Reversible) { DoUndo(e); return; }
                MessageBox.Show(this, AccountsLoc.T("FaNothingReversible"), "SAITULS");
            };
            Controls.Add(undoLast);

            Button openDir = FaUi.Btn(AccountsLoc.T("FaOpenDataFolder"), 458, 416, 140);
            openDir.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            openDir.Click += delegate
            {
                try { FaPaths.EnsureDir(FaPaths.DataDir()); System.Diagnostics.Process.Start(FaPaths.DataDir()); }
                catch (Exception ex) { MessageBox.Show(this, ex.Message, "SAITULS"); }
            };
            Controls.Add(openDir);

            Reload();
        }

        void DoUndo(FaJournalEntry e)
        {
            if (e == null) return;
            // W2-006: through the engine, so the undo takes the same
            // cross-process mutation authority a run takes. Undoing an
            // operation the running engine is still working on would restore a
            // state it has not observed.
            FaUndoResult r = _engine.Undo(e.OpId);
            if (r.Ok) Undone = true;
            MessageBox.Show(this, r.Message, AccountsLoc.T("FaEditorTitle"),
                MessageBoxButtons.OK, r.Ok ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
            Reload();
        }

        void Reload()
        {
            _view.Items.Clear();
            foreach (FaJournalEntry e in _engine.Journal.Recent(500))
            {
                ListViewItem it = new ListViewItem(e.Timestamp.Replace("T", " "));
                it.SubItems.Add(e.Profile);
                it.SubItems.Add(e.Rule);
                it.SubItems.Add(FaUi.CodeText("FaAction", e.Action));
                it.SubItems.Add(System.IO.Path.GetFileName(e.Source));
                it.SubItems.Add(e.Result == "completed"
                    ? (e.UndoState == "undone" ? AccountsLoc.T("FaDoneUndone") : (e.Reversible ? AccountsLoc.T("FaDoneReversible") : AccountsLoc.T("FaDone")))
                    : e.Result);
                if (e.Error != null && e.Error.Length > 0) it.SubItems.Add(e.Error);
                it.Tag = e;
                _view.Items.Add(it);
            }
        }
    }

    internal sealed class FaDryRunForm : Form
    {
        public FaDryRunForm(FaRunReport report)
        {
            Text = AccountsLoc.T("FaDryRunTitle") + report.ProfileName;
            FormBorderStyle = FormBorderStyle.Sizable;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(820, 480);
            BackColor = Palette.BG;
            ForeColor = Palette.TEXT;
            Font = FaUi.Face;

            Label head = FaUi.Label(AccountsLoc.T("FaDryRunUnchanged") + FaUi.RunSummary(report), 8, 8, 800);
            head.ForeColor = Palette.LINK;
            Controls.Add(head);

            ListView v = new ListView();
            v.Location = new Point(8, 32);
            v.Size = new Size(804, 400);
            v.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom;
            FaUi.Style(v);
            v.Columns.Add(AccountsLoc.T("FaFile"), 250);
            v.Columns.Add(AccountsLoc.T("FaRule"), 110);
            v.Columns.Add(AccountsLoc.T("FaAction"), 80);
            v.Columns.Add(AccountsLoc.T("FaDestinationColumn"), 250);
            v.Columns.Add(AccountsLoc.T("FaResult"), 110);
            Controls.Add(v);

            foreach (FaPlanItem it in report.Items)
            {
                ListViewItem row = new ListViewItem(it.Source);
                row.SubItems.Add(it.Rule);
                row.SubItems.Add(FaUi.CodeText("FaAction", it.Action));
                row.SubItems.Add(it.Destination);
                row.SubItems.Add(it.Warning != null && it.Warning.Length > 0
                    ? AccountsLoc.T("FaWarningPrefix") + it.Warning : (it.Message.Length > 0 ? it.Message : it.Outcome));
                if (it.Warning != null && it.Warning.Length > 0) row.ForeColor = Palette.DANGERTXT;
                v.Items.Add(row);
            }

            Button close = FaUi.Btn(AccountsLoc.T("Close"), 720, 440, 92);
            close.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            close.Click += delegate { Close(); };
            Controls.Add(close);
            AcceptButton = close;
        }
    }
}
