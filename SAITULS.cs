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
using System.Text;

// SAITULS — unified toolkit manager.
// Tabs: Home | Explorer menus | Accounts | Tools | Organizer | Settings
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

    // The professional colour table a stock ContextMenuStrip draws itself with is
    // light grey -- the last surface SAITULS could still flash white. Pinning
    // every brush that renderer can reach to a Golden Default token fixes the
    // popup without replacing the control, so items, mnemonics, keyboard
    // navigation, accessibility and modality all behave exactly as before.
    sealed class SaitulsMenuColors : ProfessionalColorTable
    {
        // UseSystemColors is not virtual on this framework, so it is set here:
        // letting it default to true would let the theme fall back to the OS
        // greys on a high-contrast or classic desktop.
        public SaitulsMenuColors() { UseSystemColors = false; }
        public override Color ToolStripDropDownBackground { get { return Palette.BG_SOFT; } }
        public override Color ImageMarginGradientBegin { get { return Palette.BG_SOFT; } }
        public override Color ImageMarginGradientMiddle { get { return Palette.BG_SOFT; } }
        public override Color ImageMarginGradientEnd { get { return Palette.BG_SOFT; } }
        public override Color MenuBorder { get { return Palette.BDARK; } }
        public override Color MenuItemBorder { get { return Palette.BHL; } }
        public override Color MenuItemSelected { get { return Palette.RAISED; } }
        public override Color MenuItemSelectedGradientBegin { get { return Palette.RAISED; } }
        public override Color MenuItemSelectedGradientEnd { get { return Palette.RAISED; } }
        public override Color MenuItemPressedGradientBegin { get { return Palette.COMPARE; } }
        public override Color MenuItemPressedGradientMiddle { get { return Palette.COMPARE; } }
        public override Color MenuItemPressedGradientEnd { get { return Palette.COMPARE; } }
        public override Color SeparatorDark { get { return Palette.BEVEL; } }
        public override Color SeparatorLight { get { return Palette.BDARK; } }
    }

    sealed class SaitulsMenuRenderer : ToolStripProfessionalRenderer
    {
        public SaitulsMenuRenderer() : base(new SaitulsMenuColors()) { RoundedEdges = false; }

        // Item text is painted by the renderer rather than taken from the
        // control's ForeColor, so a disabled or highlighted item would otherwise
        // fall back to the OS greys.
        protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e)
        {
            if (e.Item != null && !e.Item.Enabled)
            {
                // A disabled item is painted from SystemColors.GrayText by the
                // framework however the event colour is set, so it is drawn here
                // in the muted token. Same font, same rectangle, same flags --
                // only the ink differs from the stock path.
                TextRenderer.DrawText(e.Graphics, e.Text, e.TextFont, e.TextRectangle,
                    Palette.MUTED, e.TextFormat);
                return;
            }
            e.TextColor = Palette.TEXT;
            base.OnRenderItemText(e);
        }

        protected override void OnRenderToolStripBorder(ToolStripRenderEventArgs e)
        {
            Rectangle r = e.AffectedBounds;
            using (Pen pen = new Pen(Palette.BDARK))
            {
                e.Graphics.DrawRectangle(pen, r.X, r.Y, r.Width - 1, r.Height - 1);
            }
        }

        // The professional separator is drawn from ControlLight, a stock grey that
        // is neither a palette token nor a Golden Default line. A separator is one
        // stroke wide, so it is drawn here directly.
        protected override void OnRenderSeparator(ToolStripSeparatorRenderEventArgs e)
        {
            Rectangle r = e.Item.Bounds;
            using (Pen pen = new Pen(Palette.BEVEL))
            {
                if (e.Vertical) e.Graphics.DrawLine(pen, r.Width / 2, r.Top + 4, r.Width / 2, r.Bottom - 5);
                else e.Graphics.DrawLine(pen, r.Left + 4, r.Height / 2, r.Right - 5, r.Height / 2);
            }
        }
    }

    static class SaitulsMenu
    {
        // One face for every application-owned popup: the same non-antialiased
        // Verdana pixel font the rest of the shell draws with. Process lifetime,
        // like the form's own cached fonts -- no caller owns or disposes it.
        static Font _font;

        static Font MenuFont()
        {
            if (_font == null) _font = SaitulsForm.MakePixelFont("Verdana", 8);
            return _font;
        }

        // The one way an application-owned popup menu is built here.
        public static ContextMenuStrip Themed()
        {
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.BackColor = Palette.BG_SOFT;
            menu.ForeColor = Palette.TEXT;
            // RenderMode must be set BEFORE Renderer: its setter installs a fresh
            // stock renderer and would discard the themed one assigned first.
            menu.RenderMode = ToolStripRenderMode.Professional;
            menu.Renderer = new SaitulsMenuRenderer();
            menu.ShowImageMargin = false;
            menu.Font = MenuFont();
            return menu;
        }
    }

    static class AccountsLoc
    {
        static string _lang = null;
        // The operator's one language setting (SAITULS.ini Language): "auto"
        // follows Windows; an explicit supported code pins it. Canonical bundles
        // supply translations; built-in EN/RU tables remain compatibility
        // fallback for older deployments and incomplete application coverage.
        static string _setting = "auto";
        static string[] _choices;
        static readonly object LocaleLock = new object();
        public static string[] Choices
        {
            get
            {
                lock (LocaleLock)
                {
                    if (_choices == null)
                    {
                        var supported = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
                        // Compatibility fallback remains usable if shipped
                        // bundles are absent or damaged.
                        supported.Add("en"); supported.Add("ru");
                        try
                        {
                            string dir = BundleDirectory();
                            if (dir != null)
                            {
                                string[] files = Directory.GetFiles(dir, "strings.*.json");
                                Array.Sort(files, StringComparer.OrdinalIgnoreCase);
                                for (int i = 0; i < files.Length && i < 128; i++)
                                {
                                    string name = Path.GetFileName(files[i]);
                                    string code = name.Substring(8, name.Length - 13).ToLowerInvariant();
                                    if (ValidLocale(code) && Bundle(code) != null) supported.Add(code);
                                }
                            }
                        }
                        catch { }
                        var choices = new List<string> { "auto", "en" };
                        supported.Remove("en"); choices.AddRange(supported);
                        _choices = choices.ToArray();
                    }
                    return (string[])_choices.Clone();
                }
            }
        }

        static bool ValidLocale(string code)
        {
            return code != null && Regex.IsMatch(code, @"\A[a-z]{2,3}(?:-[a-z0-9]{2,8})*\z");
        }

        internal static string ResolveLocale(string requested)
        {
            string code = (requested ?? "").Trim().Replace('_', '-').ToLowerInvariant();
            string[] choices = Choices;
            if (code != "auto" && Array.IndexOf(choices, code) >= 0) return code;
            int dash = code.IndexOf('-');
            if (dash > 0 && Array.IndexOf(choices, code.Substring(0, dash)) >= 0) return code.Substring(0, dash);
            return "en";
        }

        // Applied at startup and on every change: the UI re-reads it on the next
        // paint, and every child SAITULS starts from now on (Explorer menu
        // installer, account consoles, tools) receives the same language.
        public static void Apply(string setting)
        {
            _setting = string.IsNullOrEmpty(setting) ? "auto" : setting.Trim().ToLowerInvariant();
            _lang = null;
            string resolved = CurrentLang;
            try { Environment.SetEnvironmentVariable("SAITULS_LANG", resolved); } catch { }
        }

        public static string Setting { get { return _setting; } }

        public static string CurrentLang
        {
            get
            {
                if (_lang != null) return _lang;
                if (_setting != "auto") { _lang = ResolveLocale(_setting); return _lang; }
                try
                {
                    _lang = ResolveLocale(System.Globalization.CultureInfo.CurrentUICulture.Name);
                    return _lang;
                }
                catch { _lang = "en"; return _lang; }
            }
        }

        // The name a language is offered under, in that language.
        public static string LanguageName(string code)
        {
            switch (code)
            {
                case "auto": return T("LangAuto");
                case "en": return "English";
                case "ru": return "Русский";
                default:
                    try { return System.Globalization.CultureInfo.GetCultureInfo(code).NativeName; }
                    catch { return code; }
            }
        }

        // ---- the canonical translation bundles (i18n/strings) ----
        // Canonical translations live in i18n/strings/strings.<locale>.json.
        // Built-in EN/RU tables remain compatibility fallback: existing app
        // translations temporarily have two homes. Provider translations use
        // canonical bundles with English compatibility strings for damaged
        // deployments. Untranslated keys use the English bundle, then
        // the built-in English table. An absent or malformed bundle cannot keep
        // the app from starting (the load is lazy, cached, and guarded).
        static readonly Dictionary<string, Dictionary<string, string>> Bundles =
            new Dictionary<string, Dictionary<string, string>>(StringComparer.OrdinalIgnoreCase);

        static Dictionary<string, string> Bundle(string lang)
        {
            if (!ValidLocale(lang)) return null;
            lock (LocaleLock)
            {
            Dictionary<string, string> hit;
            if (Bundles.TryGetValue(lang, out hit)) return hit;
            hit = LoadBundle(lang);
            Bundles[lang] = hit;   // cached on success AND on failure: never re-read a bad file
            return hit;
            }
        }

        static Dictionary<string, string> LoadBundle(string lang)
        {
            try
            {
                string file = BundleFile(lang);
                if (file == null) return null;
                if (new FileInfo(file).Length > 2097152) return null;
                var raw = new System.Web.Script.Serialization.JavaScriptSerializer { MaxJsonLength = 2097152 }
                    .DeserializeObject(File.ReadAllText(file, Encoding.UTF8)) as Dictionary<string, object>;
                if (raw == null) return null;
                var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (var kv in raw)
                {
                    string v = kv.Value as string;
                    if (!string.IsNullOrEmpty(v)) map[kv.Key] = v;
                }
                return map.Count > 0 ? map : null;
            }
            catch { return null; }
        }

        static string BundleFile(string lang)
        {
            if (!ValidLocale(lang)) return null;
            try
            {
                string name = "strings." + lang + ".json";
                string dir = BundleDirectory();
                string file = dir == null ? null : Path.Combine(dir, name);
                return file != null && File.Exists(file) ? file : null;
            }
            catch { return null; }
        }

        static string BundleDirectory()
        {
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory;
                string[] roots = { baseDir, Path.Combine(baseDir, ".."), Path.Combine(baseDir, "..", "..") };
                foreach (string root in roots)
                {
                    string p = Path.Combine(root, "i18n", "strings");
                    if (Directory.Exists(p)) return Path.GetFullPath(p);
                }
                return null;
            }
            catch { return null; }
        }

        static readonly Dictionary<string, string> Ru = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            { "FaNewCondition", "Новое условие" },
            { "FaEditCondition", "Изменить условие" },
            { "FaWhen", "Условие" },
            { "FaTest", "Сравнение" },
            { "FaValue", "Значение" },
            { "FaValuesHint", "Значение (несколько — через ;)" },
            { "FaConditionExample", "Пример: расширение / из списка / .jpg;.png" },
            { "FaNewRule", "Новое правило" },
            { "FaEditRule", "Изменить правило" },
            { "FaRuleName", "Название правила" },
            { "FaMatch", "Совпадение" },
            { "FaMatchAll", "все условия" },
            { "FaMatchAny", "любое условие" },
            { "FaMatchExclude", "исключить" },
            { "FaEditorEnabled", "Включено" },
            { "FaAction", "Действие" },
            { "FaTo", "Назначение" },
            { "FaRenameTemplate", "Шаблон имени" },
            { "FaCreateDestination", "Создать папку назначения" },
            { "FaAcknowledgeDelete", "Удаление без восстановления" },
            { "FaAllConditions", "Условия (должны совпасть все)" },
            { "FaRemove", "Удалить" },
            { "FaSaveRule", "Сохранить правило" },
            { "FaRuleNeedsCondition", "Добавьте хотя бы одно условие." },
            { "FaEditorTitle", "SAITULS — автоматизация папок" },
            { "FaNewProfile", "Новый профиль" },
            { "FaEditProfile", "Изменить профиль — " },
            { "FaSourceTab", "Папки" },
            { "FaRulesTab", "Правила" },
            { "FaGeneralTab", "Общее" },
            { "FaScheduleTab", "Расписание" },
            { "FaSafetyTab", "Защита" },
            { "FaAdvancedTab", "Дополнительно" },
            { "FaName", "Название" },
            { "FaDestinationHint", "Корневая папка для относительных путей назначения" },
            { "FaSourceFolder", "Исходная папка" },
            { "FaState", "Состояние" },
            { "FaIncludeSubfolders", "Включая подпапки" },
            { "FaReachable", "доступна" },
            { "FaUnreachable", "сейчас недоступна" },
            { "FaAddSource", "Добавить папку..." },
            { "FaRule", "Правило" },
            { "FaAddRule", "Новое правило" },
            { "FaUp", "Выше" },
            { "FaDown", "Ниже" },
            { "FaTriggerRealtime", "При изменениях в папке" },
            { "FaTriggerEveryMinutes", "Каждые N минут" },
            { "FaTriggerHourly", "Каждый час" },
            { "FaTriggerDaily", "Ежедневно в HH:MM" },
            { "FaTriggerWeekly", "Еженедельно" },
            { "FaTriggerStartup", "При запуске SAITULS" },
            { "FaTriggerResume", "После выхода из сна" },
            { "FaTriggerPathAvailable", "Когда папка снова доступна" },
            { "FaTriggerOnce", "Однократный запуск" },
            { "FaTriggerManual", "Только вручную" },
            { "FaSchedulePrompt", "Когда запускать профиль?" },
            { "FaEveryMinutes", "Интервал, мин" },
            { "FaAtTime", "Время HH:MM" },
            { "FaWeekday", "День (0=вс)" },
            { "FaReconcileMinutes", "Сверка, мин (0=нет)" },
            { "FaMissedPrompt", "Если запуск пропущен во время сна" },
            { "FaMissedRunAsap", "один запуск при первой возможности" },
            { "FaMissedSkip", "пропустить" },
            { "FaMissedRunOnce", "один запуск в следующий срок" },
            { "FaMissedHint", "После сна выполняется не больше одного пропущенного запуска." },
            { "FaQuietSeconds", "Без изменений, с" },
            { "FaMaxOperations", "Операций за запуск" },
            { "FaMaxBytes", "Байтов за запуск" },
            { "FaMaxErrors", "Ошибок до паузы" },
            { "FaWarnOperations", "Предупредить после" },
            { "FaCollisionPolicy", "При совпадении имени" },
            { "FaCollisionSkip", "Пропустить" },
            { "FaCollisionCounter", "Добавить (1), (2)" },
            { "FaCollisionTimestamp", "Добавить дату и время" },
            { "FaCollisionReplace", "Заменить" },
            { "FaCollisionKeepNewest", "Оставить новее" },
            { "FaCollisionKeepOldest", "Оставить старее" },
            { "FaCollisionKeepLarger", "Оставить больший" },
            { "FaCollisionKeepSmaller", "Оставить меньший" },
            { "FaCollisionHint", "Перезапись существующего файла возможна только при выборе «Заменить»." },
            { "FaVerifyCopy", "Проверка копирования" },
            { "FaVerifyNone", "Без проверки" },
            { "FaVerifySize", "Размер" },
            { "FaVerifyHash", "Размер и SHA-256" },
            { "FaRetryAttempts", "Повторов для занятого файла" },
            { "FaRetryBackoff", "Пауза, мс" },
            { "FaQuarantineFolder", "Папка карантина (пусто = папка данных SAITULS)" },
            { "FaTempPatterns", "Шаблоны временных файлов для исключения" },
            { "FaOverridePatterns", "Мои шаблоны вместо встроенных" },
            { "FaExcludedPatterns", "Шаблоны имён, которые профиль никогда не трогает" },
            { "FaFollowLinks", "Следовать по ссылкам (возможны циклы)" },
            { "FaWatchChanges", "Следить также за изменениями содержимого" },
            { "FaConfigurationPath", "Файл настроек: " },
            { "FaSaveProfile", "Сохранить профиль" },
            { "FaProfileNeedsName", "Укажите название профиля." },
            { "FaProfileNeedsSource", "Добавьте хотя бы одну исходную папку." },
            { "FaSavedDisabled", "\nПрофиль сохранён выключенным." },
            { "FaNoRulesDisabled", "Без правил профиль ничего не делает; он сохранён выключенным." },
            { "FaOffSuffix", " (выкл.)" },
            { "FaAnd", " и " },
            { "FaOr", " или " },
            { "FaUnless", " кроме " },
            { "FaHistoryTitle", "SAITULS — история автоматизации" },
            { "FaFile", "Файл" },
            { "FaResult", "Результат" },
            { "FaUndoSelected", "Отменить выбранное" },
            { "FaPickRow", "Сначала выберите строку." },
            { "FaUndoLast", "Отменить последнее" },
            { "FaNothingReversible", "В истории нет действий, которые можно отменить." },
            { "FaOpenDataFolder", "Папка данных" },
            { "FaDoneUndone", "отменено" },
            { "FaDoneReversible", "готово (можно отменить)" },
            { "FaDone", "готово" },
            { "FaDryRunTitle", "SAITULS — проверка: " },
            { "FaDryRunUnchanged", "Файлы не изменены. " },
            { "FaDestinationColumn", "Назначение" },
            { "FaWarningPrefix", "предупреждение: " },
            { "FaActionMove", "Переместить" },
            { "FaActionCopy", "Копировать" },
            { "FaActionRename", "Переименовать" },
            { "FaActionIgnore", "Игнорировать" },
            { "FaActionStop", "Остановить" },
            { "FaActionQuarantine", "Карантин" },
            { "FaActionRecycle", "В корзину" },
            { "FaActionDelete", "Удалить навсегда" },
            { "FaKind_extension", "Расширение файла" },
            { "FaKind_filename", "Имя файла" },
            { "FaKind_filename_contains", "Имя содержит" },
            { "FaKind_filename_startswith", "Имя начинается с" },
            { "FaKind_filename_endswith", "Имя заканчивается на" },
            { "FaKind_extension_group", "Группа расширений" },
            { "FaKind_min_size", "Минимальный размер" },
            { "FaKind_max_size", "Максимальный размер" },
            { "FaKind_min_age", "Минимальный возраст" },
            { "FaKind_max_age", "Максимальный возраст" },
            { "FaKind_modified_after", "Изменён после" },
            { "FaKind_created_after", "Создан после" },
            { "FaKind_weekday", "День недели" },
            { "FaKind_time_window", "Промежуток времени" },
            { "FaKind_glob", "Шаблон имени" },
            { "FaKind_regex", "Регулярное выражение" },
            { "FaKind_path", "Путь" },
            { "FaKind_is_directory", "Папка" },
            { "FaKind_hidden", "Скрытый файл" },
            { "FaKind_created_before", "Создан до" },
            { "FaKind_modified_before", "Изменён до" },
            { "FaOp_is_one_of", "Из списка" },
            { "FaOp_is", "Равно" },
            { "FaOp_contains", "Содержит" },
            { "FaOp_starts", "Начинается с" },
            { "FaOp_ends", "Заканчивается на" },
            { "FaOp_matches", "Совпадает" },
            { "FaOp_is_inside", "Внутри" },
            { "FaRunSummary", "{0}: действий {1}, пропущено {2}, ошибок {3}" },
            { "FaRunBytes", ", байтов {0}" },
            { "FaRunPaused", "; пауза: {0}" },
            { "Cancel", "Отмена" },
            { "AddProvider", "Провайдер:" },
            { "AddAlias", "Название (необязательно):" },
            { "DownloadTitle", "SAITULS — загрузка" },
            { "DownloadMode", "Режим" },
            { "DownloadAudioType", "Аудио (mp3)" },
            { "DownloadVideoType", "Видео (mkv)" },
            { "DownloadPlaylist", "Плейлист" },
            { "DownloadDated", "Дата публикации в имени" },
            { "DownloadFolder", "Папка результата" },
            { "Browse", "Обзор..." },
            { "DownloadPickFolder", "Выберите папку для загрузки" },
            { "DownloadSource", "Ссылок в буфере: {0}. Файлы сохранятся в указанную папку." },
            { "DownloadNoUrl", "В буфере нет ссылки для загрузки." },
            { "DownloadChooseFolder", "Сначала выберите папку результата." },
            { "ConsoleChooseWorkdir", "Выберите папку проекта для консоли этого аккаунта." },
            { "FolderAccess", "Доступ к папкам" },
            { "FolderAccessMgr", "Менеджер доступа к папкам..." },
            { "MergeChooseVideo", "Слияние аудио — выберите видео с двумя аудиодорожками" },
            { "ChooseTargetFolder", "{0} — выберите папку" },
            { "ChooseTargetFile", "{0} — выберите файл" },
            { "AddAccountTitle", "Добавление аккаунта" },
            { "AddAccountConfirm", "Добавить" },
            { "MenusInstallHdr", "Пункты меню для установки:" },
            { "CheckAll", "Выбрать все" },
            { "UncheckAll", "Снять все" },
            { "Install", "Установить" },
            { "Remove", "Удалить" },
            { "menu.del_junk", "Удалить мусор" },
            { "menu.ffmpeg", "Действия FFmpeg" },
            { "menu.repair_mkv", "Исправить MKV" },
            { "menu.toggle_hid", "Показать / скрыть файлы" },
            { "MenuPsAdmin", "PowerShell (админ)" },
            { "FaOn", "Автоматизация: ВКЛ" },
            { "FaOff", "Автоматизация: ВЫКЛ" },
            { "FaPausedAll", "Вся автоматизация приостановлена." },
            { "FaBackgroundHint", "Файлы распределяются в фоне." },
            { "FaOffHint", "Автоматизация выключена; файлы не меняются." },
            { "FaProfiles", "Профили" },
            { "FaNoProfiles", "Профилей пока нет." },
            { "FaAdd", "Добавить" },
            { "FaEdit", "Изменить" },
            { "FaRunNow", "Запустить" },
            { "FaDryRun", "Проверить" },
            { "FaHistory", "История" },
            { "FaResumeAll", "Продолжить всё" },
            { "FaPauseAll", "Пауза для всех" },
            { "FaTemplates", "Шаблоны" },
            { "FaDownloads", "Загрузки по типу" },
            { "FaClipboard", "Буфер обмена" },
            { "FaProfile", "Профиль: {0}" },
            { "FaEnabled", "Вкл" },
            { "FaDisabled", "Выкл" },
            { "FaRecursive", "С подпапками" },
            { "FaTopLevel", "Без подпапок" },
            { "FaSources", "Папки для распределения:" },
            { "FaNoSources", "Папки пока не добавлены." },
            { "FaAddFolder", "+ Добавить папку..." },
            { "FaDestination", "Назначение (куда идут файлы):" },
            { "FaRulesCount", "Правил: {0}" },
            { "FaFolderSetup", "Настройка папок" },
            { "FaNoSelection", "Профиль не выбран." },
            { "FaPickHint", "Выберите папку для распределения файлов." },
            { "FaQuickAdd", "+ Настроить папку..." },
            { "FaQuickHint", "Разделяет медиа, документы и архивы по типу." },
            { "FaEngine", "Состояние автоматизации" },
            { "FaNotScheduled", "не запланирован" },
            { "FaBackground", "Работа в фоне" },
            { "FaNotLoaded", "не загружена" },
            { "FaPaused", "приостановлена" },
            { "FaWatchers", "Наблюдение" },
            { "FaNextRun", "Следующий запуск" },
            { "FaDestinationRoot", "Корень: {0}" },
            { "FaInPlace", "Подпапки исходной папки" },
            { "FaNoRules", "правил нет" },
            { "FaPickFolder", "Выберите папку для распределения файлов" },
            { "menu.copy_path", "Скопировать путь текстом" },
            { "menu.del_dup", "Удалить дубликаты файлов" },
            { "menu.del_empty", "Удалить пустые папки" },
            { "menu.del_same", "Удалить одинаковые по имени" },
            { "menu.dl_yt", "Скачать с YouTube" },
            { "menu.merge_aud", "Слияние аудиодорожек" },
            { "menu.new_proj", "Создать папку нового проекта" },
            { "menu.take_own", "Взять владение" },
            { "menu.pack", "Упаковать в папку" },
            { "MediaHdr", "МЕДИА" },
            { "AppDescription", "настольные инструменты" },
            { "SettingsSavedHint", "Изменения сохраняются автоматически." },
            { "ToolkitRoot", "Папка инструментария" },
            { "LimitsCache", "Кэш показателей" },
            { "ToolAiConsoles", "Консоли ИИ" },
            { "ToolShellDoctor", "Диагностика оболочки" },
            { "SecureNotSetUp", "не настроены" },
            { "SecureNoProfiles", "профилей нет" },
            { "StatusUnavailable", "состояние недоступно" },
            { "MediaChecking", "Медиа: проверка..." },
            { "MediaReady", "Медиа: готовы" },
            { "MediaRepair", "Медиа: откройте установку / исправление на главной" },
            { "Recheck", "Проверить" },
            { "DownloadAudio", "Скачать аудио" },
            { "DownloadVideo", "Скачать видео" },
            { "MergeAudio", "Слить аудио" },
            { "Download", "Загрузка" },
            { "Start", "Запустить" },
            { "Stop", "Остановить" },
            { "SafeCopy", "Safe Copy" },
            { "OtherTools", "ДРУГИЕ ИНСТРУМЕНТЫ" },
            { "ToolNewProject", "Новый проект" },
            { "ToolPackFile", "Упаковать файл" },
            { "ToolPackFolder", "Упаковать папку" },
            { "ToolDelEmpty", "Удалить пустые" },
            { "ToolDelDup", "Удалить дубликаты" },
            { "ToolDelSame", "Удалить одноимённые" },
            { "ToolDelJunk", "Удалить мусор" },
            { "ToolPsAdmin", "PowerShell (админ)" },
            { "ToolItemList", "Список элементов" },
            { "ToolItemTree", "Дерево элементов" },
            { "ToolPatcher", "Патчер OpenCode" },
            { "ToolOpenCodeSettings", "Настройки OpenCode" },
            { "ToolQueueViewer", "Очередь OpenCode" },
            { "ToolScenarios", "Сценарии" },
            { "ToolSecureApps", "Защищённые приложения" },
            { "ToolSaiSpinSettings", "Настройки SAISPIN" },
            { "ToolOptimizer", "Оптимизатор" },
            { "ClipNotInstalled", "не установлен" },
            { "ClipDependencyMissing", "нужна зависимость" },
            { "ClipRunning", "работает" },
            { "ClipStopped", "остановлен" },
            { "ClipMonitorActive", "наблюдение включено" },
            { "ClipMonitorInactive", "наблюдение выключено" },
            { "ClipStatusStale", "данные устарели" },
            { "ClipHotkey", "Клавиша Safe Copy: {0}" },
            { "ClipHotkeyMissing", "Клавиша Safe Copy недоступна: {0}; наблюдение за изображениями работает." },
            { "ClipImages", "Изображения -> " },
            { "ClipFallback", "(запасная папка)" },
            { "ClipSaved", "сохранено: " },
            { "ClipSaveUnavailable", "Папка недоступна; исправьте её в настройках: {0}" },
            { "ClipSaveFolder", "Папка изображений: {0}" },
            { "ClipDefaultFolder", "(по умолчанию)" },
            { "ClipMissing", "Clipboard+ не установлен. Откройте установку / исправление на главной." },
            { "ClipPythonMissing", "Python не установлен. Откройте установку / исправление на главной." },
            { "ClipChecking", "Проверка зависимостей Clipboard+..." },
            { "ClipRepair", "Clipboard+ требует исправления. Откройте установку / исправление на главной." },
            { "ClipAlreadyRunning", "Clipboard+ уже работает." },
            { "ClipAlreadyStopped", "Clipboard+ уже остановлен." },
            { "ClipStartFailed", "Не удалось запустить Clipboard+. Откройте установку / исправление на главной." },
            { "ClipStartRequested", "Запуск Clipboard+..." },
            { "ClipStartCancelled", "Запуск Clipboard+ отменён." },
            { "ClipStopFailed", "Clipboard+ не принял остановку. Проверьте состояние / журнал." },
            { "ClipStopRequested", "Остановка Clipboard+..." },
            { "ClipCommandTimedOut", "Нет ответа за 20 с. Проверьте журнал Clipboard+." },
            { "ClipCommandExit", "Команда завершилась с кодом {0}" },
            { "ClipSettingsFailed", "Не удалось открыть настройки Clipboard+. Откройте установку / исправление на главной." },
            { "ClipSettingsOpened", "Настройки Clipboard+ открыты. Сохраните изменения в том окне." },
            { "ClipStarted", "Clipboard+ работает." },
            { "ClipStartTimedOut", "Clipboard+ не запустился за 6 с. Остановите его и проверьте журнал." },
            { "ClipStopTimedOut", "Clipboard+ продолжает работать через 6 с. Проверьте журнал." },
            { "ClipStoppedMessage", "Clipboard+ остановлен." },
            { "TrayOpen", "Открыть SAITULS" },
            { "FolderAutomation", "Автоматизация папок" },
            { "ShowLog", "Показать журнал" },
            { "OpenOutput", "Открыть результат" },
            { "CopyOutputPath", "Копировать путь" },
            { "Win5h", "5ч" },
            { "WinWeek", "неделя" },
            { "WinMonth", "месяц" },
            { "UnitM", "м" },
            { "UnitH", "ч" },
            { "UnitD", "д" },
            { "LangAuto", "Авто (как в Windows)" },
            { "TabHome", "Главная" },
            { "TabMenus", "Меню Проводника" },
            { "TabAccounts", "Аккаунты" },
            { "TabTools", "Инструменты" },
            { "TabOrganizer", "Органайзер" },
            { "TabSettings", "Настройки" },
            { "HomeReady", "ГОТОВО" },
            { "HomeAttention", "ВНИМАНИЕ" },
            { "HomeSetup", "НУЖНА НАСТРОЙКА" },
            { "HomeAllGood", "Всё работает." },
            { "HomeColAccount", "Аккаунт" },
            { "HomeColPool", "Пул" },
            { "HomeColShort", "Короткий" },
            { "HomeColLong", "Долгий" },
            { "HomeColReset", "Сброс" },
            { "HomeColState", "Состояние" },
            { "HomeColComponent", "Компонент" },
            { "HomePeriodWeek", "нед." },
            { "HomePeriodMonth", "мес." },
            { "HomeLockedBy", "блок: {0}" },
            { "AiLimits", "ЛИМИТЫ ИИ" },
            { "LimitsFrom", "источник: {0}" },
            { "UpdatedAgo", "обновлено {0} назад" },
            { "UpdatedNow", "обновлено только что" },
            { "LimitsReading", "Читаю лимиты..." },
            { "LimitsNoSource", "Нет источника лимитов: установите LIMISAW или SAI Accounts." },
            { "LimitsNone", "Лимиты ИИ ещё не получены." },
            { "ShowingLeft", "Показ: осталось" },
            { "ShowingUsed", "Показ: израсходовано" },
            { "WordLeft", "осталось" },
            { "WordUsed", "израсходовано" },
            { "MetricLowest", "Наименьший остаток" },
            { "MetricAny", "Любой доступный" },
            { "MetricPinned", "Закреплённый показатель" },
            { "PinUnavailable", "Закреплённый показатель недоступен; показан наименьший остаток." },
            { "PinGone", "Закреплённый показатель исчез; показан наименьший остаток." },
            { "ReadingSettings", "Показатели: порядок и видимость здесь" },
            { "PinReading", "Закрепить" },
            { "ReadingShown", "Показан" },
            { "ReadingHidden", "Скрыт" },
            { "MoveReadingUp", "Выше" },
            { "MoveReadingDown", "Ниже" },
            { "UpdatedUnknown", "Время обновления неизвестно" },
            { "AssumedReset", "~ означает расчёт после сброса; ожидаются свежие данные." },
            { "TaskbarHdr", "Панель задач" },
            { "TaskbarReveal", "Показывать панель задач у края" },
            { "TaskbarAutostart", "Запуск с SAITULS / Windows" },
            { "TaskbarMissing", "Компонент не установлен" },
            { "TaskbarRunning", "работает" },
            { "TaskbarEnabledStopped", "включён, но не работает" },
            { "TaskbarStopped", "остановлен" },
            { "TaskbarRestartHint", "Работает после перезапуска SAITULS." },
            { "TaskbarEnableFirst", "Сначала включите показ панели задач." },
            { "TaskbarHint1", "Подведите указатель к нижнему краю экрана, чтобы показать" },
            { "TaskbarHint2", "скрытую панель задач даже под развёрнутым окном." },
            { "TaskbarHint3", "Продолжает работать после выхода из SAITULS; отключается здесь." },
            { "AccountKeeperHint", "Восстанавливает контексты аккаунтов с автоподключением; окна IDE не открывает." },
            { "Resets", "сброс через {0}" },
            { "LockedBy", "заблокировано: {0}" },
            { "StateStale", "УСТАРЕЛО" },
            { "StateSignIn", "НУЖЕН ВХОД" },
            { "StateOfflineWord", "ОФЛАЙН" },
            { "StateUnavailable", "НЕДОСТУПНО" },
            { "NotReported", "нет данных" },
            { "StaleSince", "последние данные {0}" },
            { "NReady", "{0} готово" },
            { "NSignIn", "{0} нужен вход" },
            { "NOffline", "{0} офлайн" },
            { "NFrozen", "{0} заморожено" },
            { "NDisabled", "{0} отключено" },
            { "SystemHdr", "СИСТЕМА" },
            { "AccountService", "Служба аккаунтов" },
            { "ExplorerMenusRow", "Меню Проводника" },
            { "ToolsRow", "Инструменты" },
            { "KeeperRow", "Фоновый хранитель" },
            { "WordOk", "OK" },
            { "WordOn", "ВКЛ" },
            { "WordOff", "ВЫКЛ" },
            { "NotInstalled", "не установлено" },
            { "NeedsRepair", "нужен ремонт" },
            { "Checking", "проверка..." },
            { "MissingN", "не хватает: {0}" },
            { "RecentIssue", "ПОСЛЕДНЯЯ ПРОБЛЕМА" },
            { "Repair", "Исправить" },
            { "Retry", "Повторить" },
            { "OpenAccounts", "Открыть аккаунты" },
            { "InstallRepair", "Установить / исправить" },
            { "LocateLimisaw", "Указать LIMISAW..." },
            { "Keep5h", "Держать 5ч окно открытым" },
            { "Keep5hHint", "Отправляет один крошечный запрос сразу после сброса 5ч окна, чтобы новое окно началось сразу. Тратит немного квоты." },
            { "Keep5hUnsupported", "5ч хранитель: только Claude и Codex" },
            { "GeneralHdr", "ОБЩИЕ" },
            { "LanguageRow", "Язык" },
            { "StartWithWindows", "Запуск с Windows" },
            { "StartWithWindowsHint", "Тихо стартует в трее." },
            { "LimitsHdr", "ЛИМИТЫ ИИ" },
            { "HideSpent", "Скрывать исчерпанные аккаунты" },
            { "OnlyFiveHour", "Только аккаунты с доступным 5ч окном" },
            { "HeadlineNumber", "Главное число" },
            { "LimitsSource", "Источник лимитов" },
            { "AccountsHdr", "АККАУНТЫ" },
            { "IntegrationHdr", "ИНТЕГРАЦИЯ" },
            { "AdvancedHdr", "ДОПОЛНИТЕЛЬНО" },
            { "ExitApp", "Выйти из SAITULS" },
            { "OpenFolder", "Открыть папку" },
            { "MenusVerified", "установлено, актуально" },
            { "MenusDrift", "устарело" },
            { "ErrEngineMissing", "Служба аккаунтов не установлена. Аккаунты недоступны, остальное работает." },
            { "ErrTimeout", "Служба аккаунтов не ответила вовремя. Повторите." },
            { "ErrEngineRepair", "Службе аккаунтов нужен ремонт." },
            { "ErrMenu", "Меню Проводника не обновилось. Нажмите «Исправить»." },
            { "ErrContextStart", "Контекст Windows аккаунта не запустился. Повторите." },
            { "ErrOffline", "Аккаунт офлайн. Сначала подключите его." },
            { "ErrSignIn", "Вход не открылся. Повторите." },
            { "ErrWorkdir", "Эту папку проекта нельзя открыть. Выберите другую." },
            { "ErrStatus", "Не удалось прочитать статус аккаунта. Повторите." },
            { "ErrGeneric", "Последнее действие не удалось. Подробности: Дополнительно > Копировать диагностику." },
            { "GoogleAccount", "Google-аккаунт" },
            { "NotCheckedYet", "ещё не проверен" },
            { "IdeDiffers", "IDE вошла как {0}. Квота считается для аккаунта выше." },
            { "UsageHdr", "ИСПОЛЬЗОВАНИЕ" },
            { "AllWindows", "Все окна лимитов" },
            { "LimitsFor", "Лимиты" },
            { "Loading", "Загрузка..." },
            { "ACCOUNTS", "АККАУНТЫ" },
            { "AddAccount", "+ Добавить аккаунт" },
            { "Refresh", "Обновить" },
            { "Import", "Импорт..." },
            { "ImportLegacy", "Импорт" },
            { "AllProviders", "Все провайдеры" },
            { "Search", "Поиск..." },
            { "SelectAccount", "Выберите аккаунт" },
            { "Status", "Статус" },
            { "Authentication", "Аутентификация" },
            { "Context", "Контекст" },
            { "Connection", "Подключение" },
            { "Connect", "Подключить" },
            { "Disconnect", "Отключить" },
            { "Visibility", "Видимость" },
            { "Hidden", "Скрыт" },
            { "Local", "Локальный" },
            { "Shared", "Общий" },
            { "Execution", "Исполнение" },
            { "ExecCurrentUser", "Текущий пользователь Windows" },
            { "ExecManaged", "Управляемый пользователь Windows" },
            { "AlwaysAvailable", "Всегда доступно" },
            { "OffByChoice", "Отключено вручную" },
            { "OffUnknown", "Причина отключения неизвестна" },
            { "OffDefault", "По умолчанию выключено" },
            // Two independent planes. A difference between them is a fact, so
            // neither word may be spelled as a fault.
            { "ConsoleAccount", "Аккаунт консоли" },
            { "IdeAccount", "Аккаунт IDE" },
            { "Unproven", "не подтверждён" },
            { "Quota", "Квота" },
            { "QuotaShared", "общая квота с {0}: оба слота расходуют один аккаунт квоты" },
            { "SwitchAccount", "Сменить Google-аккаунт" },
            { "Switching", "выход из Google ({0})..." },
            { "SwitchedTo", "вошли в другой Google-аккаунт: {0}" },
            { "SwitchUnchanged", "вошли в тот же Google-аккаунт ({0}). Смена не удалась." },
            { "Managed", "Управляемый" },
            { "Authenticated", "Аутентифицирован" },
            { "AuthRequired", "Требуется вход" },
            { "Ready", "ГОТОВ" },
            { "Offline", "ОФЛАЙН" },
            { "SignIn", "ВОЙТИ" },
            { "Disabled", "ОТКЛЮЧЕН" },
            { "Frozen", "ЗАМОРОЖЕН" },
            { "Archived", "В АРХИВЕ" },
            { "Error", "ОШИБКА" },
            { "OpenConsole", "Открыть консоль" },
            { "ConsoleShortcutsHint", "Вставка: Ctrl+Shift+V / ПКМ | Копирование: Enter / Ctrl+Shift+C | Выделение: ЛКМ" },
            { "Working", "Выполняется..." },
            { "Usage", "ИСПОЛЬЗОВАНИЕ" },
            { "Stale", "УСТАРЕЛО" },
            { "Workdir", "РАБОЧИЙ КАТАЛОГ" },
            { "NotSelected", "Не выбран" },
            { "Change", "Изменить" },
            { "Choose", "Выбрать..." },
            { "Manage", "УПРАВЛЕНИЕ" },
            { "MoveUp", "Вверх" },
            { "MoveDown", "Вниз" },
            { "Rename", "Переименовать" },
            { "Hide", "Скрыть" },
            { "Show", "Показать" },
            { "Disable", "Отключить" },
            { "Enable", "Включить" },
            { "Freeze", "Заморозить" },
            { "Unfreeze", "Разморозить" },
            { "Advanced", "Дополнительно" },
            { "Diagnostics", "Диагностика" },
            { "StatusDetails", "Сведения о статусе" },
            { "StartContext", "Запустить контекст" },
            { "StopContext", "Остановить контекст" },
            { "Reauth", "Повторный вход" },
            { "ConvertToManaged", "Сделать управляемым" },
            { "Destructive", "ОПАСНЫЕ ДЕЙСТВИЯ" },
            { "Archive", "Архивировать" },
            { "Restore", "Восстановить" },
            { "DeleteCtx", "Удалить контекст..." },
            { "CopyDiagnostics", "Копировать диагностику" },
            { "Copied", "Скопировано!" },
            { "NoAccounts", "Нет аккаунтов. Импортируйте реестр ниже." },
            { "StatusReady", "Статус: Готов." }
        };

        static readonly Dictionary<string, string> En = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            { "LanguageApply", "Apply" },
            { "ProviderAvailable", "Available" },
            { "ProviderExperimental", "Experimental" },
            { "ProviderUnavailable", "Unavailable" },
            { "ProviderPlanned", "Planned" },
            { "ProviderPlannedHelp", "Planned provider. Account creation is not available yet." },
            { "ProviderAntigravityHelp", "Google sign-in opens automatically in your browser.\nA managed Windows user keeps a second account separate." },
            { "ProviderClaudeHelp", "Uses your Claude Code environment and credentials.\nEach account gets its own config home." },
            { "ProviderCodexHelp", "Uses your Codex / OpenAI CLI environment.\nEach account gets its own config home." },
            { "ProviderZcodeHelp", "Verification pending. Support not enabled in this build." },
            { "ProviderOpencodeHelp", "Planned provider. The OpenCode launcher exists; account creation does not yet." },
            { "ProviderClineHelp", "Planned provider. Cline runs inside VS Code; managed accounts do not yet." },
            { "ProviderGeminiHelp", "Planned provider. Distinct from Antigravity, which already uses Google sign-in." },
            { "FaNewCondition", "New condition" },
            { "FaEditCondition", "Edit condition" },
            { "FaWhen", "When" },
            { "FaTest", "Test" },
            { "FaValue", "Value" },
            { "FaValuesHint", "Value (several values separated by ; )" },
            { "FaConditionExample", "Example: extension / is_one_of / .jpg;.png" },
            { "FaNewRule", "New rule" },
            { "FaEditRule", "Edit rule" },
            { "FaRuleName", "Rule name" },
            { "FaMatch", "Match" },
            { "FaMatchAll", "all of" },
            { "FaMatchAny", "any of" },
            { "FaMatchExclude", "exclude (never touch)" },
            { "FaEditorEnabled", "Enabled" },
            { "FaAction", "Action" },
            { "FaTo", "To" },
            { "FaRenameTemplate", "Rename template" },
            { "FaCreateDestination", "Create the destination folder if needed" },
            { "FaAcknowledgeDelete", "I understand this deletes permanently" },
            { "FaAllConditions", "When (all these must match)" },
            { "FaRemove", "Remove" },
            { "FaSaveRule", "Save rule" },
            { "FaRuleNeedsCondition", "A rule needs at least one condition." },
            { "FaEditorTitle", "SAITULS - Folder Automation" },
            { "FaNewProfile", "New profile" },
            { "FaEditProfile", "Edit profile - " },
            { "FaSourceTab", "Sources (folders)" },
            { "FaRulesTab", "Rules" },
            { "FaGeneralTab", "General" },
            { "FaScheduleTab", "Schedule" },
            { "FaSafetyTab", "Safety" },
            { "FaAdvancedTab", "Advanced" },
            { "FaName", "Name" },
            { "FaDestinationHint", "Destination root (used when a rule destination is relative)" },
            { "FaSourceFolder", "Source folder" },
            { "FaState", "State" },
            { "FaIncludeSubfolders", "Include subfolders" },
            { "FaReachable", "reachable" },
            { "FaUnreachable", "not reachable now" },
            { "FaAddSource", "Add folder..." },
            { "FaRule", "Rule" },
            { "FaAddRule", "Add rule" },
            { "FaUp", "Up" },
            { "FaDown", "Down" },
            { "FaTriggerRealtime", "Real-time (watch the folder)" },
            { "FaTriggerEveryMinutes", "Every N minutes" },
            { "FaTriggerHourly", "Hourly" },
            { "FaTriggerDaily", "Daily at HH:MM" },
            { "FaTriggerWeekly", "Weekly" },
            { "FaTriggerStartup", "At SAITULS startup" },
            { "FaTriggerResume", "After resume" },
            { "FaTriggerPathAvailable", "When the folder is available again" },
            { "FaTriggerOnce", "Run once at a time" },
            { "FaTriggerManual", "Manual only" },
            { "FaSchedulePrompt", "When does this profile run?" },
            { "FaEveryMinutes", "Interval (min)" },
            { "FaAtTime", "Time HH:MM" },
            { "FaWeekday", "Weekday (0=Sun)" },
            { "FaReconcileMinutes", "Reconcile (min, 0=off)" },
            { "FaMissedPrompt", "If runs were missed while the PC was asleep" },
            { "FaMissedRunAsap", "run once as soon as possible" },
            { "FaMissedSkip", "skip them" },
            { "FaMissedRunOnce", "wait for the next slot" },
            { "FaMissedHint", "Missed runs are never replayed one by one - a wake-up runs at most one catch-up." },
            { "FaQuietSeconds", "Quiet period (s)" },
            { "FaMaxOperations", "Max operations" },
            { "FaMaxBytes", "Max bytes" },
            { "FaMaxErrors", "Errors to pause" },
            { "FaWarnOperations", "Warn after files" },
            { "FaCollisionPolicy", "Collision policy" },
            { "FaCollisionSkip", "Skip" },
            { "FaCollisionCounter", "Rename with (1), (2)" },
            { "FaCollisionTimestamp", "Rename with timestamp" },
            { "FaCollisionReplace", "Replace" },
            { "FaCollisionKeepNewest", "Keep newest" },
            { "FaCollisionKeepOldest", "Keep oldest" },
            { "FaCollisionKeepLarger", "Keep larger" },
            { "FaCollisionKeepSmaller", "Keep smaller" },
            { "FaCollisionHint", "Only 'Replace' can overwrite an existing file, and only when you choose it here." },
            { "FaVerifyCopy", "Copy verification" },
            { "FaVerifyNone", "None" },
            { "FaVerifySize", "Size" },
            { "FaVerifyHash", "Size and SHA-256" },
            { "FaRetryAttempts", "Locked-file retries" },
            { "FaRetryBackoff", "Backoff (ms)" },
            { "FaQuarantineFolder", "Quarantine folder (empty = the SAITULS data folder)" },
            { "FaTempPatterns", "Temporary / in-progress patterns to ignore (overrides the defaults)" },
            { "FaOverridePatterns", "Use my patterns instead of the built-in ones" },
            { "FaExcludedPatterns", "Filename patterns this profile never touches" },
            { "FaFollowLinks", "Follow junctions and symbolic links (off by default: they can loop)" },
            { "FaWatchChanges", "Also watch modifications, not only creates and renames" },
            { "FaConfigurationPath", "Configuration file: " },
            { "FaSaveProfile", "Save profile" },
            { "FaProfileNeedsName", "The profile needs a name." },
            { "FaProfileNeedsSource", "Add at least one source folder." },
            { "FaSavedDisabled", "\nThis profile was saved disabled." },
            { "FaNoRulesDisabled", "A profile with no rules does nothing; it was saved disabled." },
            { "FaOffSuffix", " (off)" },
            { "FaAnd", " and " },
            { "FaOr", " or " },
            { "FaUnless", " unless " },
            { "FaHistoryTitle", "SAITULS - Folder Automation history" },
            { "FaFile", "File" },
            { "FaResult", "Result" },
            { "FaUndoSelected", "Undo selected" },
            { "FaPickRow", "Pick a row first." },
            { "FaUndoLast", "Undo last" },
            { "FaNothingReversible", "There is nothing reversible left in the history." },
            { "FaOpenDataFolder", "Open data folder" },
            { "FaDoneUndone", "done (undone)" },
            { "FaDoneReversible", "done (undoable)" },
            { "FaDone", "done" },
            { "FaDryRunTitle", "SAITULS - dry run: " },
            { "FaDryRunUnchanged", "Nothing below was changed. " },
            { "FaDestinationColumn", "Destination" },
            { "FaWarningPrefix", "warning: " },
            { "FaActionMove", "Move" },
            { "FaActionCopy", "Copy" },
            { "FaActionRename", "Rename" },
            { "FaActionIgnore", "Ignore" },
            { "FaActionStop", "Stop" },
            { "FaActionQuarantine", "Quarantine" },
            { "FaActionRecycle", "Recycle Bin" },
            { "FaActionDelete", "Delete permanently" },
            { "FaKind_extension", "File extension" },
            { "FaKind_filename", "File name" },
            { "FaKind_filename_contains", "Name contains" },
            { "FaKind_filename_startswith", "Name starts with" },
            { "FaKind_filename_endswith", "Name ends with" },
            { "FaKind_extension_group", "Extension group" },
            { "FaKind_min_size", "Minimum size" },
            { "FaKind_max_size", "Maximum size" },
            { "FaKind_min_age", "Minimum age" },
            { "FaKind_max_age", "Maximum age" },
            { "FaKind_modified_after", "Modified after" },
            { "FaKind_created_after", "Created after" },
            { "FaKind_weekday", "Weekday" },
            { "FaKind_time_window", "Time window" },
            { "FaKind_glob", "Filename pattern" },
            { "FaKind_regex", "Regular expression" },
            { "FaKind_path", "Path" },
            { "FaKind_is_directory", "Directory" },
            { "FaKind_hidden", "Hidden file" },
            { "FaKind_created_before", "Created before" },
            { "FaKind_modified_before", "Modified before" },
            { "FaOp_is_one_of", "One of" },
            { "FaOp_is", "Equals" },
            { "FaOp_contains", "Contains" },
            { "FaOp_starts", "Starts with" },
            { "FaOp_ends", "Ends with" },
            { "FaOp_matches", "Matches" },
            { "FaOp_is_inside", "Inside" },
            { "FaRunSummary", "{0}: {1} action(s), {2} skipped, {3} error(s)" },
            { "FaRunBytes", ", {0} bytes" },
            { "FaRunPaused", "; paused: {0}" },
            { "Cancel", "Cancel" },
            { "AddProvider", "Provider:" },
            { "AddAlias", "Display alias (optional):" },
            { "DownloadTitle", "SAITULS - download" },
            { "DownloadMode", "Mode" },
            { "DownloadAudioType", "Audio (mp3)" },
            { "DownloadVideoType", "Video (mkv)" },
            { "DownloadPlaylist", "Playlist" },
            { "DownloadDated", "Prefix upload date" },
            { "DownloadFolder", "Output folder" },
            { "Browse", "Browse..." },
            { "DownloadPickFolder", "Pick the download folder" },
            { "DownloadSource", "Clipboard links: {0}. Files go to the folder above." },
            { "DownloadNoUrl", "No downloadable URL found in the clipboard." },
            { "DownloadChooseFolder", "Pick an output folder first." },
            { "ConsoleChooseWorkdir", "Choose the project folder to open in this account console." },
            { "FolderAccess", "Folder access" },
            { "FolderAccessMgr", "Folder access manager..." },
            { "MergeChooseVideo", "Merge Audio - pick the video file with two audio tracks" },
            { "ChooseTargetFolder", "{0} — pick the target folder" },
            { "ChooseTargetFile", "{0} — pick the target file" },
            { "AddAccountTitle", "Add Account" },
            { "AddAccountConfirm", "Add Account" },
            { "MenusInstallHdr", "Context menu features to install:" },
            { "CheckAll", "Check All" },
            { "UncheckAll", "Uncheck All" },
            { "Install", "Install" },
            { "Remove", "Remove" },
            { "menu.del_junk", "Delete junk" },
            { "menu.ffmpeg", "FFmpeg actions" },
            { "menu.repair_mkv", "Repair MKV" },
            { "menu.toggle_hid", "Show / hide files" },
            { "MenuPsAdmin", "PowerShell as admin" },
            { "FaOn", "Automation: ON" },
            { "FaOff", "Automation: OFF" },
            { "FaPausedAll", "All automation is paused." },
            { "FaBackgroundHint", "Files are organized in the background." },
            { "FaOffHint", "Automation is off; files are untouched." },
            { "FaProfiles", "Profiles" },
            { "FaNoProfiles", "No profiles yet." },
            { "FaAdd", "Add" },
            { "FaEdit", "Edit" },
            { "FaRunNow", "Run now" },
            { "FaDryRun", "Dry run" },
            { "FaHistory", "History" },
            { "FaResumeAll", "Resume all" },
            { "FaPauseAll", "Pause all" },
            { "FaTemplates", "Templates" },
            { "FaDownloads", "Downloads by type" },
            { "FaClipboard", "Clipboard" },
            { "FaProfile", "Profile: {0}" },
            { "FaEnabled", "On" },
            { "FaDisabled", "Off" },
            { "FaRecursive", "Recursive" },
            { "FaTopLevel", "Top level" },
            { "FaSources", "Folders to organize:" },
            { "FaNoSources", "No folders added yet." },
            { "FaAddFolder", "+ Add folder..." },
            { "FaDestination", "Destination (where files go):" },
            { "FaRulesCount", "{0} rules" },
            { "FaFolderSetup", "Folder setup" },
            { "FaNoSelection", "No profile selected." },
            { "FaPickHint", "Choose a folder to organize automatically." },
            { "FaQuickAdd", "+ Organize folder..." },
            { "FaQuickHint", "Sorts media, documents and archives by type." },
            { "FaEngine", "Automation status" },
            { "FaNotScheduled", "not scheduled" },
            { "FaBackground", "Background" },
            { "FaNotLoaded", "not loaded" },
            { "FaPaused", "paused" },
            { "FaWatchers", "Watchers" },
            { "FaNextRun", "Next scheduled run" },
            { "FaDestinationRoot", "Root: {0}" },
            { "FaInPlace", "Subfolders inside the source folder" },
            { "FaNoRules", "no rules" },
            { "FaPickFolder", "Select folder to organize" },
            { "menu.copy_path", "Copy Path As Text" },
            { "menu.del_dup", "Delete Duplicate Files" },
            { "menu.del_empty", "Delete Empty Folders" },
            { "menu.del_same", "Delete Same-Name Items" },
            { "menu.dl_yt", "Download from YouTube" },
            { "menu.merge_aud", "Merge Audio Tracks" },
            { "menu.new_proj", "Create New Project Folder" },
            { "menu.take_own", "Take Ownership" },
            { "menu.pack", "Pack Into Folder" },
            { "MediaHdr", "MEDIA" },
            { "AppDescription", "desktop toolkit" },
            { "SettingsSavedHint", "Changes are saved automatically." },
            { "ToolkitRoot", "Toolkit root" },
            { "LimitsCache", "Limits cache" },
            { "ToolAiConsoles", "AI Consoles" },
            { "ToolShellDoctor", "Shell Doctor" },
            { "SecureNotSetUp", "not set up" },
            { "SecureNoProfiles", "no profiles recorded" },
            { "StatusUnavailable", "status unavailable" },
            { "MediaChecking", "Media tools: checking..." },
            { "MediaReady", "Media tools: ready" },
            { "MediaRepair", "Media tools: use Install / repair on Home" },
            { "Recheck", "Recheck" },
            { "DownloadAudio", "Download Audio" },
            { "DownloadVideo", "Download Video" },
            { "MergeAudio", "Merge Audio" },
            { "Download", "Download" },
            { "Start", "Start" },
            { "Stop", "Stop" },
            { "SafeCopy", "Safe Copy" },
            { "OtherTools", "OTHER TOOLS" },
            { "ToolNewProject", "New Project" },
            { "ToolPackFile", "Pack File" },
            { "ToolPackFolder", "Pack Folder" },
            { "ToolDelEmpty", "Del Empty" },
            { "ToolDelDup", "Del Dup" },
            { "ToolDelSame", "Del Same" },
            { "ToolDelJunk", "Del Junk" },
            { "ToolPsAdmin", "PS Admin" },
            { "ToolItemList", "Item List" },
            { "ToolItemTree", "Item Tree" },
            { "ToolPatcher", "OpenCode Patcher" },
            { "ToolOpenCodeSettings", "OpenCode Settings" },
            { "ToolQueueViewer", "OpenCode Queue Viewer" },
            { "ToolScenarios", "Scenarios" },
            { "ToolSecureApps", "Secure Apps" },
            { "ToolSaiSpinSettings", "SAISPIN Settings" },
            { "ToolOptimizer", "Optimizer" },
            { "ClipNotInstalled", "not installed" },
            { "ClipDependencyMissing", "dependency missing" },
            { "ClipRunning", "running" },
            { "ClipStopped", "stopped" },
            { "ClipMonitorActive", "monitor active" },
            { "ClipMonitorInactive", "monitor inactive" },
            { "ClipStatusStale", "status stale" },
            { "ClipHotkey", "Safe Copy hotkey: {0}" },
            { "ClipHotkeyMissing", "Safe Copy hotkey unavailable: {0}; image monitor still runs." },
            { "ClipImages", "Images -> " },
            { "ClipFallback", "(fallback in use)" },
            { "ClipSaved", "saved: " },
            { "ClipSaveUnavailable", "Save folder unavailable; repair it in Settings: {0}" },
            { "ClipSaveFolder", "Image save folder: {0}" },
            { "ClipDefaultFolder", "(default)" },
            { "ClipMissing", "Clipboard+ is missing. Use Install / repair on Home." },
            { "ClipPythonMissing", "Python is missing. Use Install / repair on Home." },
            { "ClipChecking", "Checking Clipboard+ dependencies..." },
            { "ClipRepair", "Clipboard+ needs repair. Use Install / repair on Home." },
            { "ClipAlreadyRunning", "Clipboard+ is already running." },
            { "ClipAlreadyStopped", "Clipboard+ is already stopped." },
            { "ClipStartFailed", "Clipboard+ could not start. Use Install / repair on Home." },
            { "ClipStartRequested", "Starting Clipboard+..." },
            { "ClipStartCancelled", "Clipboard+ start cancelled." },
            { "ClipStopFailed", "Clipboard+ did not accept Stop. Check its status/log." },
            { "ClipStopRequested", "Stopping Clipboard+..." },
            { "ClipCommandTimedOut", "No response within 20s. Check the Clipboard+ log." },
            { "ClipCommandExit", "Command exited: {0}" },
            { "ClipSettingsFailed", "Clipboard+ settings could not open. Use Install / repair on Home." },
            { "ClipSettingsOpened", "Clipboard+ settings opened. Save there to apply changes." },
            { "ClipStarted", "Clipboard+ is running." },
            { "ClipStartTimedOut", "Clipboard+ did not start within 6s. Stop it and check the log." },
            { "ClipStopTimedOut", "Clipboard+ is still running after 6s. Check the log." },
            { "ClipStoppedMessage", "Clipboard+ stopped." },
            { "TrayOpen", "Open SAITULS" },
            { "FolderAutomation", "Folder Automation" },
            { "ShowLog", "Show log" },
            { "OpenOutput", "Open output" },
            { "CopyOutputPath", "Copy output path" },
            { "Win5h", "5h" },
            { "WinWeek", "week" },
            { "WinMonth", "month" },
            { "UnitM", "m" },
            { "UnitH", "h" },
            { "UnitD", "d" },
            { "LangAuto", "Auto (Windows)" },
            { "TabHome", "Home" },
            { "TabMenus", "Explorer menus" },
            { "TabAccounts", "Accounts" },
            { "TabTools", "Tools" },
            { "TabOrganizer", "Organizer" },
            { "TabSettings", "Settings" },
            { "HomeReady", "READY" },
            { "HomeAttention", "ATTENTION" },
            { "HomeSetup", "SETUP REQUIRED" },
            { "HomeAllGood", "Everything is working." },
            { "HomeColAccount", "Account" },
            { "HomeColPool", "Pool" },
            { "HomeColShort", "Short limit" },
            { "HomeColLong", "Long limit" },
            { "HomeColReset", "Reset" },
            { "HomeColState", "State" },
            { "HomeColComponent", "Component" },
            { "HomePeriodWeek", "week" },
            { "HomePeriodMonth", "mo." },
            { "HomeLockedBy", "locked: {0}" },
            { "AiLimits", "AI LIMITS" },
            { "LimitsFrom", "from {0}" },
            { "UpdatedAgo", "updated {0} ago" },
            { "UpdatedNow", "updated just now" },
            { "LimitsReading", "Reading limits..." },
            { "LimitsNoSource", "No limits source: install LIMISAW or SAI Accounts." },
            { "LimitsNone", "No AI limits reported yet." },
            { "ShowingLeft", "Showing: Left" },
            { "ShowingUsed", "Showing: Used" },
            { "WordLeft", "left" },
            { "WordUsed", "used" },
            { "MetricLowest", "Lowest remaining" },
            { "MetricAny", "Any available" },
            { "MetricPinned", "Pinned reading" },
            { "PinUnavailable", "Pinned reading unavailable; showing lowest selected." },
            { "PinGone", "Pinned reading gone; showing lowest selected." },
            { "ReadingSettings", "Readings: order and visibility here" },
            { "PinReading", "Pin" },
            { "ReadingShown", "Shown" },
            { "ReadingHidden", "Hidden" },
            { "MoveReadingUp", "Up" },
            { "MoveReadingDown", "Down" },
            { "UpdatedUnknown", "Update time unknown" },
            { "AssumedReset", "~ means inferred after reset; waiting for a fresh reading." },
            { "TaskbarHdr", "Taskbar" },
            { "TaskbarReveal", "Reliable taskbar edge reveal" },
            { "TaskbarAutostart", "Start with SAITULS / Windows" },
            { "TaskbarMissing", "helper missing" },
            { "TaskbarRunning", "running" },
            { "TaskbarEnabledStopped", "enabled, not running" },
            { "TaskbarStopped", "stopped" },
            { "TaskbarRestartHint", "Survives a SAITULS restart." },
            { "TaskbarEnableFirst", "Turn the reveal on first." },
            { "TaskbarHint1", "Push the pointer into the bottom edge of a screen and that screen's" },
            { "TaskbarHint2", "auto-hidden taskbar appears, even under a maximized window." },
            { "TaskbarHint3", "Keeps running when SAITULS exits; switch it off here to stop it." },
            { "AccountKeeperHint", "Restores contexts for Auto-connect accounts; never opens IDE windows." },
            { "Resets", "resets {0}" },
            { "LockedBy", "locked by {0}" },
            { "StateStale", "STALE" },
            { "StateSignIn", "SIGN IN REQUIRED" },
            { "StateOfflineWord", "OFFLINE" },
            { "StateUnavailable", "UNAVAILABLE" },
            { "NotReported", "not reported" },
            { "StaleSince", "last good {0}" },
            { "NReady", "{0} ready" },
            { "NSignIn", "{0} need sign-in" },
            { "NOffline", "{0} offline" },
            { "NFrozen", "{0} frozen" },
            { "NDisabled", "{0} disabled" },
            { "SystemHdr", "SYSTEM" },
            { "AccountService", "Account service" },
            { "ExplorerMenusRow", "Explorer menus" },
            { "ToolsRow", "Tools" },
            { "KeeperRow", "Background keeper" },
            { "WordOk", "OK" },
            { "WordOn", "ON" },
            { "WordOff", "OFF" },
            { "NotInstalled", "not installed" },
            { "NeedsRepair", "needs repair" },
            { "Checking", "checking..." },
            { "MissingN", "{0} missing" },
            { "RecentIssue", "LAST ISSUE" },
            { "Repair", "Repair" },
            { "Retry", "Retry" },
            { "OpenAccounts", "Open accounts" },
            { "InstallRepair", "Install / repair" },
            { "LocateLimisaw", "Locate LIMISAW..." },
            { "Keep5h", "Keep 5h window open" },
            { "Keep5hHint", "Sends one tiny request right after the 5h window resets, so the next window starts at once. Uses a little quota." },
            { "Keep5hUnsupported", "5h keeper: Claude and Codex only" },
            { "GeneralHdr", "GENERAL" },
            { "LanguageRow", "Language" },
            { "StartWithWindows", "Start with Windows" },
            { "StartWithWindowsHint", "Starts quietly in the tray." },
            { "LimitsHdr", "AI LIMITS" },
            { "HideSpent", "Hide spent accounts" },
            { "OnlyFiveHour", "Only accounts with a usable 5h window" },
            { "HeadlineNumber", "Headline number" },
            { "LimitsSource", "Limits source" },
            { "AccountsHdr", "ACCOUNTS" },
            { "IntegrationHdr", "INTEGRATION" },
            { "AdvancedHdr", "ADVANCED" },
            { "ExitApp", "Exit SAITULS" },
            { "OpenFolder", "Open folder" },
            { "MenusVerified", "installed and up to date" },
            { "MenusDrift", "out of date" },
            { "ErrEngineMissing", "Account service is not installed. Accounts are unavailable; everything else works." },
            { "ErrTimeout", "Account service did not answer in time. Retry." },
            { "ErrEngineRepair", "Account service needs repair." },
            { "ErrMenu", "Explorer menu did not update. Use Repair." },
            { "ErrContextStart", "The account's Windows context did not start. Retry." },
            { "ErrOffline", "The account is offline. Connect it first." },
            { "ErrSignIn", "Sign-in did not open. Retry." },
            { "ErrWorkdir", "That project folder cannot be opened. Choose another." },
            { "ErrStatus", "Could not read account status. Retry." },
            { "ErrGeneric", "The last action failed. Details: Advanced > Copy diagnostics." },
            { "GoogleAccount", "Google account" },
            { "NotCheckedYet", "not checked yet" },
            { "IdeDiffers", "The IDE is signed in as {0}. Quota is counted for the account above." },
            { "UsageHdr", "USAGE" },
            { "AllWindows", "All limit windows" },
            { "LimitsFor", "Limits" },
            { "Loading", "Loading..." },
            { "ACCOUNTS", "ACCOUNTS" },
            { "AddAccount", "+ Add account" },
            { "Refresh", "Refresh" },
            { "Import", "Import..." },
            { "ImportLegacy", "Import legacy" },
            { "AllProviders", "All providers" },
            { "Search", "Search..." },
            { "SelectAccount", "Select an account" },
            { "Status", "Status" },
            { "Authentication", "Authentication" },
            { "Context", "Context" },
            { "Connection", "Connection" },
            { "Connect", "Connect" },
            { "Disconnect", "Disconnect" },
            { "Visibility", "Visibility" },
            { "Hidden", "Hidden" },
            { "Local", "Local" },
            { "Shared", "Shared" },
            { "Execution", "Execution" },
            { "ExecCurrentUser", "Current Windows user" },
            { "ExecManaged", "Managed Windows user" },
            { "AlwaysAvailable", "Always available" },
            { "OffByChoice", "Disconnected by you" },
            { "OffUnknown", "Not connected automatically" },
            { "OffDefault", "Off by default" },
            // Two independent planes. A difference between them is a fact, so
            // neither word may be spelled as a fault.
            { "ConsoleAccount", "Console account" },
            { "IdeAccount", "IDE account" },
            { "Unproven", "unproven" },
            { "Quota", "Quota" },
            { "QuotaShared", "Shares one quota with {0}: both slots draw on the same quota account" },
            { "SwitchAccount", "Switch Google account" },
            { "Switching", "signing out of Google ({0})..." },
            { "SwitchedTo", "signed into a different Google account: {0}" },
            { "SwitchUnchanged", "signed back into the same Google account ({0}). The switch did not change identity." },
            { "Managed", "Managed" },
            { "Authenticated", "Authenticated" },
            { "AuthRequired", "Sign in required" },
            { "Ready", "READY" },
            { "Offline", "OFFLINE" },
            { "SignIn", "SIGN IN" },
            { "Disabled", "DISABLED" },
            { "Frozen", "FROZEN" },
            { "Archived", "ARCHIVED" },
            { "Error", "ERROR" },
            { "OpenConsole", "Open console" },
            { "ConsoleShortcutsHint", "Paste: Ctrl+Shift+V / Right-Click | Copy: Enter / Ctrl+Shift+C | Select: Drag" },
            { "Working", "Working..." },
            { "Usage", "USAGE" },
            { "Stale", "STALE" },
            { "Workdir", "WORKING DIRECTORY" },
            { "NotSelected", "Not selected" },
            { "Change", "Change" },
            { "Choose", "Choose..." },
            { "Manage", "MANAGE" },
            { "MoveUp", "Move Up" },
            { "MoveDown", "Move Down" },
            { "Rename", "Rename" },
            { "Hide", "Hide" },
            { "Show", "Show" },
            { "Disable", "Disable" },
            { "Enable", "Enable" },
            { "Freeze", "Freeze" },
            { "Unfreeze", "Unfreeze" },
            { "Advanced", "Advanced" },
            { "Diagnostics", "Diagnostics" },
            { "StatusDetails", "Status details" },
            { "StartContext", "Start context" },
            { "StopContext", "Stop context" },
            { "Reauth", "Re-authenticate" },
            { "ConvertToManaged", "Convert to managed" },
            { "Destructive", "DESTRUCTIVE" },
            { "Archive", "Archive" },
            { "Restore", "Restore" },
            { "DeleteCtx", "Delete ctx..." },
            { "CopyDiagnostics", "Copy diagnostics" },
            { "Copied", "Copied!" },
            { "NoAccounts", "No accounts. Import the legacy registry below." },
            { "StatusReady", "Status: Ready." }
        };

        public static string T(string key)
        {
            string lang = CurrentLang;
            string val;
            Dictionary<string, string> bundle = Bundle(lang);
            if (bundle != null && bundle.TryGetValue(key, out val) && val.Length > 0) return val;
            if (lang == "ru" && Ru.TryGetValue(key, out val)) return val;
            Dictionary<string, string> english = Bundle("en");
            if (english != null && english.TryGetValue(key, out val) && val.Length > 0) return val;
            if (En.TryGetValue(key, out val)) return val;
            return key;
        }

        /// The same lookup, with one substitution. Only for strings that are
        /// ABOUT a specific account -- never for keys that decide behaviour, where
        /// an interpolated sentence would stop being a key.
        public static string T(string key, string arg)
        {
            string template = T(key);
            if (arg == null) return template;
            try { return template.Replace("{0}", arg); }
            catch { return template; }
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
        // Durable background keeper: whether Windows periodically runs
        // account reconciliation while SAITULS is closed. Global intent only;
        // per-account Auto-connect stays authoritative per account.
        public bool AccountKeeper = false;
        // Last directory the operator explicitly chose for an account console.
        // Remembered so Open console can offer a real workdir next time -- but
        // it is only ever a REMEMBERED EXPLICIT CHOICE. Never a default: if the
        // folder is gone or unreadable we ask again rather than launch into
        // something arbitrary.
        public string LastWorkDir = "";

        // ---- one application language (T-266) ----
        // "auto" follows Windows; an explicit code pins it. Applied to every
        // SAITULS surface and handed to child tools as SAITULS_LANG.
        public string Language = "auto";

        // ---- AI limits presentation (T-266) ----
        // Same names and meanings as LIMISAW (LIMISAW.ini): ShowUsed flips the
        // number between left and used and nothing else; Metric "lowest" or
        // "any" picks the headline reading; HideSpentAccounts and
        // OnlyWithFiveHour filter what is drawn, never what is read. Seeded once
        // from LIMISAW.ini when SAITULS has never stored its own values.
        public bool LimitsShowUsed = false;
        public string LimitsMetric = "lowest";
        public bool LimitsHideSpent = false;
        public bool LimitsOnlyFiveHour = false;
        public string LimitsHidden = "";     // '|'-separated limits account keys hidden HERE only
        public string LimitsReadingHidden = "", LimitsReadingOrder = "";
        public bool LimitsSeeded = false;
        public string LimisawPath = "";       // operator-chosen LIMISAW.exe, empty = discover
        // 5h window keeper: opted-in SAI account ids ('|'-separated) and the
        // per-account memory that keeps it to one ping per reset.
        public string KeepFiveHour = "";
        public string KeepFiveHourState = "";
        // Who owns the 5h window of the accounts SAITULS keeps: "SAITULS" (this
        // app) or "ZAICODE" (the other keeper). One owner per subscription: two
        // applications must never race for the same window.
        public string KeepFiveHourOwner = "SAITULS";
        // The model the keeper's bounded request runs on. Empty = the CLI's own
        // family alias, which follows the vendor's current cheap model instead
        // of a dated id that can be retired under us.
        public string KeepFiveHourModel = "";
        public int WindowW = 0, WindowH = 0;

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
            // The keeper's admission record is one long value: a small buffer
            // would truncate it and lose the record of an attempt.
            var sb = new System.Text.StringBuilder(65536);
            GetPrivateProfileString("saituls", key, def, sb, sb.Capacity, IniPath);
            return sb.ToString();
        }
        void Write(string key, string val) { WritePrivateProfileString("saituls", key, val, IniPath); }

        // Write one key and prove it landed. The keeper's admission record is
        // written BEFORE a paid request starts, so "written" has to mean "on
        // disk and readable back", not "the API did not complain".
        public bool WriteChecked(string key, string val)
        {
            try
            {
                if (!WritePrivateProfileString("saituls", key, val, IniPath)) return false;
                var sb = new System.Text.StringBuilder(65536);
                GetPrivateProfileString("saituls", key, "\u0001", sb, sb.Capacity, IniPath);
                return sb.ToString() == val;
            }
            catch { return false; }
        }

        string ReadSec(string section, string key, string def)
        {
            var sb = new System.Text.StringBuilder(8192);
            GetPrivateProfileString(section, key, def, sb, sb.Capacity, IniPath);
            return sb.ToString();
        }
        void WriteSec(string section, string key, string val) { WritePrivateProfileString(section, key, val, IniPath); }

        public string GetAccountWorkDir(string accountId)
        {
            if (string.IsNullOrEmpty(accountId)) return LastWorkDir;
            string v = ReadSec("AccountWorkDirs", accountId, "").Trim();
            if (v.Length > 0 && Directory.Exists(v)) return v;
            if (LastWorkDir.Length > 0 && Directory.Exists(LastWorkDir)) return LastWorkDir;
            return "";
        }

        public void SetAccountWorkDir(string accountId, string dir)
        {
            LastWorkDir = dir ?? "";
            Write("LastWorkDir", LastWorkDir);
            if (!string.IsNullOrEmpty(accountId))
                WriteSec("AccountWorkDirs", accountId, dir ?? "");
        }

        public List<string> GetAccountFolders(string accountId)
        {
            var res = new List<string>();
            if (string.IsNullOrEmpty(accountId)) return res;
            string raw = ReadSec("AccountFolders", accountId, "");
            foreach (string p in raw.Split(new[] { '|' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string tr = p.Trim();
                if (tr.Length > 0 && !res.Contains(tr)) res.Add(tr);
            }
            string wd = GetAccountWorkDir(accountId);
            if (wd.Length > 0 && !res.Contains(wd) && Directory.Exists(wd))
            {
                res.Insert(0, wd);
            }
            return res;
        }

        public void SaveAccountFolders(string accountId, List<string> folders)
        {
            if (string.IsNullOrEmpty(accountId)) return;
            string joined = string.Join("|", (folders ?? new List<string>()).ToArray());
            WriteSec("AccountFolders", accountId, joined);
        }

        public void Load()
        {
            string v;
            AutoStart = Read("AutoStart", "0") == "1";
            TaskbarEdge = Read("TaskbarEdge", "0") == "1";
            LastTab = Read("LastTab", "Home");
            AccountKeeper = Read("AccountKeeper", "0") == "1";
            v = Read("RootPath", Dir);
            if (Directory.Exists(v)) RootPath = v;
            v = Read("LastWorkDir", "");
            // A remembered path that no longer opens is worse than none: it
            // would silently become the workdir of a real launch.
            if (v.Length > 0 && Directory.Exists(v)) LastWorkDir = v;
            Language = Read("Language", "auto").Trim().ToLowerInvariant();
            if (Language.Length == 0) Language = "auto";
            LimitsSeeded = Read("LimitsSeeded", "0") == "1";
            LimitsShowUsed = Read("LimitsShowUsed", "0") == "1";
            LimitsMetric = Read("LimitsMetric", "lowest").Trim();
            if (LimitsMetric.Length == 0) LimitsMetric = "lowest";
            LimitsHideSpent = Read("LimitsHideSpent", "0") == "1";
            LimitsOnlyFiveHour = Read("LimitsOnlyFiveHour", "0") == "1";
            LimitsHidden = Read("LimitsHidden", "");
            LimitsReadingHidden = Read("LimitsReadingHidden", "");
            LimitsReadingOrder = Read("LimitsReadingOrder", "");
            LimisawPath = Read("LimisawPath", "");
            KeepFiveHour = Read("KeepFiveHour", "");
            KeepFiveHourState = Read("KeepFiveHourState", "");
            KeepFiveHourOwner = Read("KeepFiveHourOwner", "SAITULS").Trim();
            if (KeepFiveHourOwner.Length == 0) KeepFiveHourOwner = "SAITULS";
            KeepFiveHourModel = Read("KeepFiveHourModel", "").Trim();
            int n;
            if (int.TryParse(Read("WindowW", "0"), out n)) WindowW = n;
            if (int.TryParse(Read("WindowH", "0"), out n)) WindowH = n;
            if (!File.Exists(IniPath)) Save();
        }

        public void Save()
        {
            var ci = System.Globalization.CultureInfo.InvariantCulture;
            Write("AutoStart", AutoStart ? "1" : "0");
            Write("TaskbarEdge", TaskbarEdge ? "1" : "0");
            Write("LastTab", LastTab);
            Write("AccountKeeper", AccountKeeper ? "1" : "0");
            Write("RootPath", RootPath);
            Write("LastWorkDir", LastWorkDir);
            Write("Language", Language);
            Write("LimitsSeeded", LimitsSeeded ? "1" : "0");
            Write("LimitsShowUsed", LimitsShowUsed ? "1" : "0");
            Write("LimitsMetric", LimitsMetric);
            Write("LimitsHideSpent", LimitsHideSpent ? "1" : "0");
            Write("LimitsOnlyFiveHour", LimitsOnlyFiveHour ? "1" : "0");
            Write("LimitsHidden", LimitsHidden);
            Write("LimitsReadingHidden", LimitsReadingHidden);
            Write("LimitsReadingOrder", LimitsReadingOrder);
            Write("LimisawPath", LimisawPath);
            Write("KeepFiveHour", KeepFiveHour);
            Write("KeepFiveHourState", KeepFiveHourState);
            Write("KeepFiveHourOwner", KeepFiveHourOwner);
            Write("KeepFiveHourModel", KeepFiveHourModel);
            Write("WindowW", WindowW.ToString(ci));
            Write("WindowH", WindowH.ToString(ci));
        }

        public static List<string> SplitList(string v)
        {
            var list = new List<string>();
            foreach (string part in (v ?? "").Split('|'))
                if (part.Trim().Length > 0 && !list.Contains(part.Trim())) list.Add(part.Trim());
            return list;
        }

        public static string JoinList(List<string> items)
        {
            return string.Join("|", (items ?? new List<string>()).ToArray());
        }

        // One-time seed from LIMISAW's own ini: the operator already chose how
        // limits should read there, and SAITULS must not quietly disagree.
        // Never written back; LIMISAW keeps owning its file.
        public void SeedLimitsFrom(string limisawIni)
        {
            if (LimitsSeeded) return;
            LimitsSeeded = true;
            try
            {
                if (!string.IsNullOrEmpty(limisawIni) && File.Exists(limisawIni))
                {
                    var sb = new System.Text.StringBuilder(32768);
                    Func<string, string, string> rd = (k, d) => { sb.Length = 0; GetPrivateProfileString("limisaw", k, d, sb, sb.Capacity, limisawIni); return sb.ToString(); };
                    LimitsPresentation seed = LimitsPresentation.FromLimisaw(rd);
                    seed.HiddenAccounts = SplitList(LimitsHidden);
                    seed.ApplyTo(this);
                }
            }
            catch { }
            Save();
        }
    }

    // A neutral presentation boundary, independent of account lifecycle and
    // quota readings. Existing application INIs remain the persistence owners.
    // See Contracts/limits-presentation-v1.md for identity/filter semantics.
    class LimitsPresentation
    {
        public const string Schema = "sai.limits/presentation/1";
        public bool ShowUsed, HideSpentAccounts, OnlyWithFiveHour;
        public string Metric = "lowest";
        public List<string> HiddenAccounts = new List<string>();
        public List<string> HiddenReadings = new List<string>();
        public List<string> ReadingOrder = new List<string>();

        static string MetricValue(string value) { return string.IsNullOrWhiteSpace(value) ? "lowest" : value.Trim(); }

        public static LimitsPresentation FromSettings(SaitulsSettings settings)
        {
            return new LimitsPresentation {
                ShowUsed = settings.LimitsShowUsed, Metric = MetricValue(settings.LimitsMetric),
                HideSpentAccounts = settings.LimitsHideSpent, OnlyWithFiveHour = settings.LimitsOnlyFiveHour,
                HiddenAccounts = SaitulsSettings.SplitList(settings.LimitsHidden),
                HiddenReadings = SaitulsSettings.SplitList(settings.LimitsReadingHidden),
                ReadingOrder = SaitulsSettings.SplitList(settings.LimitsReadingOrder)
            };
        }

        public static LimitsPresentation FromLimisaw(Func<string, string, string> read)
        {
            return new LimitsPresentation {
                ShowUsed = read("ShowUsed", "0") == "1", Metric = MetricValue(read("TrayMetric", "lowest")),
                HideSpentAccounts = read("HideSpentAccounts", "0") == "1",
                OnlyWithFiveHour = read("OnlyWithFiveHour", "0") == "1",
                HiddenReadings = SaitulsSettings.SplitList(read("TrayHidden", "")),
                ReadingOrder = SaitulsSettings.SplitList(read("TrayItems", ""))
            };
        }

        // Does not save or mutate another application's preferences. The
        // existing one-time importer explicitly saves its own settings after.
        public void ApplyTo(SaitulsSettings settings)
        {
            settings.LimitsShowUsed = ShowUsed; settings.LimitsMetric = MetricValue(Metric);
            settings.LimitsHideSpent = HideSpentAccounts; settings.LimitsOnlyFiveHour = OnlyWithFiveHour;
            settings.LimitsHidden = SaitulsSettings.JoinList(HiddenAccounts);
            settings.LimitsReadingHidden = SaitulsSettings.JoinList(HiddenReadings);
            settings.LimitsReadingOrder = SaitulsSettings.JoinList(ReadingOrder);
        }

        public string ToJson()
        {
            return new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                { "schema", Schema }, { "number_mode", ShowUsed ? "used" : "remaining" }, { "headline", MetricValue(Metric) },
                { "hidden_accounts", HiddenAccounts }, { "hidden_readings", HiddenReadings }, { "reading_order", ReadingOrder },
                { "hide_spent_accounts", HideSpentAccounts }, { "only_with_five_hour", OnlyWithFiveHour }
            });
        }

        public static LimitsPresentation Parse(string json)
        {
            try
            {
                var doc = new System.Web.Script.Serialization.JavaScriptSerializer().DeserializeObject(json) as Dictionary<string, object>;
                object value;
                if (doc == null || !doc.TryGetValue("schema", out value) || !object.Equals(value, Schema)) return null;
                var result = new LimitsPresentation();
                if (doc.TryGetValue("number_mode", out value))
                {
                    if (!object.Equals(value, "remaining") && !object.Equals(value, "used")) return null;
                    result.ShowUsed = object.Equals(value, "used");
                }
                if (doc.TryGetValue("headline", out value))
                {
                    if (!(value is string)) return null;
                    result.Metric = MetricValue((string)value);
                }
                foreach (string key in new[] { "hide_spent_accounts", "only_with_five_hour" })
                    if (doc.TryGetValue(key, out value))
                    {
                        if (!(value is bool)) return null;
                        if (key == "hide_spent_accounts") result.HideSpentAccounts = (bool)value;
                        else result.OnlyWithFiveHour = (bool)value;
                    }
                foreach (string key in new[] { "hidden_accounts", "hidden_readings", "reading_order" })
                    if (doc.TryGetValue(key, out value))
                    {
                        var array = value as object[];
                        if (array == null) return null;
                        List<string> target = key == "hidden_accounts" ? result.HiddenAccounts
                            : key == "hidden_readings" ? result.HiddenReadings : result.ReadingOrder;
                        foreach (object item in array)
                        {
                            if (!(item is string)) return null;
                            string id = ((string)item).Trim();
                            if (id.Length > 0 && !target.Contains(id)) target.Add(id);
                        }
                    }
                return result;
            }
            catch { return null; }
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
            try { m = Mutex.OpenExisting(Environment.GetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_MUTEX") ?? MutexName); return true; }
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
                using (EventWaitHandle stop = EventWaitHandle.OpenExisting(Environment.GetEnvironmentVariable("SAITULS_CLIPBOARD_PLUS_STOP_EVENT") ?? StopEventName))
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
        public static ProcessStartInfo DependencyCommand(string root, out string problem)
        {
            problem = "";
            string exe = Pythonw();
            if (exe == null) { problem = "Python (pythonw.exe) not found on PATH"; return null; }
            string script = ScriptPath(root);
            if (!File.Exists(script)) { problem = "Clipboard+ script is missing"; return null; }
            return new ProcessStartInfo(exe, "\"" + script + "\" --check-deps")
            {
                WorkingDirectory = Path.GetDirectoryName(script), UseShellExecute = false,
                CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true
            };
        }

        public static string DependencyResult(int exitCode, string text)
        {
            if (exitCode == 0) return null;
            foreach (string line in (text ?? "").Split('\n'))
                if (line.TrimStart().StartsWith("MISSING=")) return line.TrimStart().Substring(8).Trim();
            string trimmed = (text ?? "").Trim();
            return trimmed.Length > 0 ? trimmed : ("readiness probe exited " + exitCode);
        }

        // The form runs this through its bounded, cancellable child runner.
        public static ProcessStartInfo OneShotCommand(string root, string argument, out string problem)
        {
            problem = "";
            string exe = Pythonw();
            if (exe == null) { problem = AccountsLoc.T("ClipPythonMissing"); return null; }
            string script = ScriptPath(root);
            if (!File.Exists(script)) { problem = AccountsLoc.T("ClipMissing"); return null; }
            return new ProcessStartInfo(exe, "\"" + script + "\" " + argument)
            {
                WorkingDirectory = Path.GetDirectoryName(script), UseShellExecute = false,
                CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true
            };
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
    // ===================== shared themed dialog (UI.md ZERO-WHITE) =====================
    // The ONE application-owned replacement for System.Windows.Forms.MessageBox.
    //
    // A stock MessageBox is light chrome on a dark product: its client area is
    // the system window colour and it flashes that light surface before it
    // paints -- exactly the "zero white" gap this closes. Every application-owned
    // message and confirmation in this shell routes through here and is drawn
    // from Palette tokens like every other SAITULS surface.
    //
    // It preserves everything MessageBox gave its callers:
    //   * the same DialogResult (OK / Cancel / Yes / No),
    //   * the same modality (window-modal with an owner, application-modal without),
    //   * Enter presses the default button, Esc presses Cancel or No,
    //   * focus starts on the button Enter will press,
    //   * no close box, so a confirmation cannot be dismissed without answering,
    //   * '&' in a message is literal text, never a mnemonic (paths keep working).
    //
    // Operating-system and vendor dialogs -- UAC, Windows Security, the real
    // folder picker, an OS error box raised by a vendor module -- are NOT
    // routed through this and stay native. Only this product's own surfaces
    // are themed.
    internal sealed class SaitulsMessageDialog : Form
    {
        const int BodyWidth = 400;
        const int MarginX = 14;
        const int ButtonW = 88;
        const int ButtonH = 26;
        const int ButtonGap = 8;

        readonly Label _text = new Label();
        readonly Panel _glyph = new Panel();
        readonly Button _accept = new Button();
        readonly Button _decline = new Button();
        readonly MessageBoxIcon _icon;
        readonly bool _hasDecline;

        internal readonly DialogResult AcceptResult;
        internal readonly DialogResult DeclineResult;

        internal SaitulsMessageDialog(string text, string caption,
                                     MessageBoxButtons buttons, MessageBoxIcon icon)
        {
            _icon = icon;
            Text = string.IsNullOrEmpty(caption) ? "SAITULS" : caption;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            ControlBox = false;                 // MessageBox shows no close box either
            ShowInTaskbar = false;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterParent;
            BackColor = Palette.BG_SOFT;
            ForeColor = Palette.TEXT;
            Font = new Font("Verdana", 8f);
            DoubleBuffered = true;              // no light flash while laying out

            string acceptLabel;
            string declineLabel = null;
            switch (buttons)
            {
                case MessageBoxButtons.OKCancel:
                    acceptLabel = "OK"; AcceptResult = DialogResult.OK;
                    declineLabel = AccountsLoc.T("Cancel"); DeclineResult = DialogResult.Cancel;
                    _hasDecline = true;
                    break;
                case MessageBoxButtons.YesNo:
                    acceptLabel = "Yes"; AcceptResult = DialogResult.Yes;
                    declineLabel = "No"; DeclineResult = DialogResult.No;
                    _hasDecline = true;
                    break;
                default:
                    // This shell uses only OK, OKCancel and YesNo. Anything else
                    // behaves as OK rather than inventing buttons the call site
                    // does not read; test_saituls_dialogs.ps1 pins that set.
                    acceptLabel = "OK"; AcceptResult = DialogResult.OK;
                    _hasDecline = false;
                    break;
            }

            string message = text ?? "";
            bool showGlyph = icon != MessageBoxIcon.None;
            int textLeft = MarginX + (showGlyph ? 30 : 0);
            int textWidth = BodyWidth - textLeft - MarginX;
            Size measured = TextRenderer.MeasureText(message, Font,
                new Size(textWidth, int.MaxValue),
                TextFormatFlags.WordBreak | TextFormatFlags.NoPrefix);
            int textHeight = Math.Max(18, Math.Min(measured.Height + 4, 420));

            _text.Text = message;
            _text.AutoSize = false;
            _text.UseMnemonic = false;          // '&' is text here, not a mnemonic
            _text.BackColor = Palette.BG_SOFT;
            _text.ForeColor = Palette.TEXT;
            _text.SetBounds(textLeft, 16, textWidth, textHeight);

            _glyph.SetBounds(MarginX, 16, 20, 20);
            _glyph.BackColor = Palette.BG_SOFT;
            _glyph.Visible = showGlyph;
            _glyph.Paint += delegate(object s, PaintEventArgs e) { DrawGlyph(e.Graphics); };

            _accept.Text = acceptLabel;
            _accept.DialogResult = AcceptResult;
            _accept.FlatStyle = FlatStyle.Flat;
            _accept.BackColor = Palette.RAISED;
            _accept.ForeColor = Palette.TEXT;
            _accept.Size = new Size(ButtonW, ButtonH);
            _accept.TabIndex = 0;

            int buttonsTop = 16 + textHeight + 16;
            int right = BodyWidth - MarginX;
            _accept.Location = new Point(right - ButtonW, buttonsTop);

            Controls.Add(_text);
            Controls.Add(_glyph);
            Controls.Add(_accept);
            if (_hasDecline)
            {
                _decline.Text = declineLabel;
                _decline.DialogResult = DeclineResult;
                _decline.FlatStyle = FlatStyle.Flat;
                _decline.BackColor = Palette.RAISED;
                _decline.ForeColor = Palette.TEXT;
                _decline.Size = new Size(ButtonW, ButtonH);
                _decline.Location = new Point(right - ButtonW - ButtonGap - ButtonW, buttonsTop);
                _decline.TabIndex = 1;
                Controls.Add(_decline);
            }

            ClientSize = new Size(BodyWidth, buttonsTop + ButtonH + MarginX);

            // Keyboard contract: Enter is the safe default, Esc is the refusal.
            AcceptButton = _accept;
            if (_hasDecline) CancelButton = _decline;
            ActiveControl = _accept;
        }

        internal string MessageText { get { return _text.Text; } }
        internal bool HasDeclineButton { get { return _hasDecline; } }

        void DrawGlyph(Graphics g)
        {
            g.FillRectangle(new SolidBrush(Palette.BG), _glyph.ClientRectangle);
            using (var pen = new Pen(Palette.BEVEL)) g.DrawRectangle(pen, 0, 0, _glyph.Width - 1, _glyph.Height - 1);
            string symbol;
            Color ink;
            switch (_icon)
            {
                case MessageBoxIcon.Error:
                    symbol = "x"; ink = Palette.DANGERTXT; break;
                case MessageBoxIcon.Warning:
                    symbol = "!"; ink = Palette.BHL; break;
                case MessageBoxIcon.Question:
                    symbol = "?"; ink = Palette.BHL; break;
                case MessageBoxIcon.Information:
                    symbol = "i"; ink = Palette.BHL; break;
                default:
                    symbol = "i"; ink = Palette.TEXT2; break;
            }
            TextRenderer.DrawText(g, symbol, Font, _glyph.ClientRectangle, ink,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter |
                TextFormatFlags.NoPrefix);
        }
    }

    // The call-site surface. It mirrors the MessageBox.Show overloads this shell
    // actually used, so a migration is a call-for-call rename that keeps every
    // return value, owner and modality intact.
    internal static class SaitulsDialog
    {
        public static DialogResult Show(string text)
        {
            return Run(null, text, "SAITULS", MessageBoxButtons.OK, MessageBoxIcon.None);
        }

        public static DialogResult Show(string text, string caption)
        {
            return Run(null, text, caption, MessageBoxButtons.OK, MessageBoxIcon.None);
        }

        public static DialogResult Show(string text, string caption,
                                        MessageBoxButtons buttons, MessageBoxIcon icon)
        {
            return Run(null, text, caption, buttons, icon);
        }

        public static DialogResult Show(IWin32Window owner, string text, string caption)
        {
            return Run(owner, text, caption, MessageBoxButtons.OK, MessageBoxIcon.None);
        }

        public static DialogResult Show(IWin32Window owner, string text, string caption,
                                        MessageBoxButtons buttons, MessageBoxIcon icon)
        {
            return Run(owner, text, caption, buttons, icon);
        }

        static DialogResult Run(IWin32Window owner, string text, string caption,
                                MessageBoxButtons buttons, MessageBoxIcon icon)
        {
            using (var dialog = new SaitulsMessageDialog(text, caption, buttons, icon))
            {
                // window-modal when an owner is known, application-modal when not:
                // exactly what MessageBox.Show did for the same call shape.
                return owner == null ? dialog.ShowDialog() : dialog.ShowDialog(owner);
            }
        }
    }

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

            Text = AccountsLoc.T("DownloadTitle");
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
            modeLabel.Text = AccountsLoc.T("DownloadMode");
            modeLabel.Location = new Point(12, 10);
            modeLabel.AutoSize = true;
            modeLabel.ForeColor = Palette.TEXT2;

            _audio.Text = AccountsLoc.T("DownloadAudioType");
            _audio.Location = new Point(12, 30);
            _audio.AutoSize = true;
            _audio.ForeColor = Palette.TEXT;
            _audio.Checked = Mode == "audio";

            _video.Text = AccountsLoc.T("DownloadVideoType");
            _video.Location = new Point(12, 54);
            _video.AutoSize = true;
            _video.ForeColor = Palette.TEXT;
            _video.Checked = Mode == "video";

            _playlist.Text = AccountsLoc.T("DownloadPlaylist");
            _playlist.Location = new Point(160, 30);
            _playlist.AutoSize = true;
            _playlist.ForeColor = Palette.TEXT;

            _dated.Text = AccountsLoc.T("DownloadDated");
            _dated.Location = new Point(160, 54);
            _dated.AutoSize = true;
            _dated.ForeColor = Palette.TEXT;

            var folderLabel = new Label();
            folderLabel.Text = AccountsLoc.T("DownloadFolder");
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
            browse.Text = AccountsLoc.T("Browse");
            browse.Location = new Point(348, 103);
            browse.Size = new Size(92, 24);
            browse.FlatStyle = FlatStyle.Flat;
            browse.BackColor = Palette.RAISED;
            browse.ForeColor = Palette.TEXT;
            browse.Click += delegate
            {
                using (var fb = new FolderBrowserDialog())
                {
                    fb.Description = AccountsLoc.T("DownloadPickFolder");
                    fb.SelectedPath = Directory.Exists(_folder.Text) ? _folder.Text : folder;
                    if (fb.ShowDialog(this) == DialogResult.OK) _folder.Text = fb.SelectedPath;
                }
            };

            var sourceLabel = new Label();
            sourceLabel.Location = new Point(12, 136);
            sourceLabel.Size = new Size(428, 30);
            sourceLabel.ForeColor = urlCount > 0 ? Palette.TEXT2 : Palette.DANGERTXT;
            sourceLabel.Text = urlCount > 0
                ? AccountsLoc.T("DownloadSource", urlCount.ToString())
                : AccountsLoc.T("DownloadNoUrl");

            var readiness = new Label();
            readiness.Location = new Point(12, 166);
            readiness.Size = new Size(428, 16);
            readiness.ForeColor = Palette.DANGERTXT;
            readiness.Text = readinessNote;

            var download = new Button();
            download.Text = AccountsLoc.T("Download");
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
                    SaitulsDialog.Show(AccountsLoc.T("DownloadChooseFolder"), AccountsLoc.T("DownloadTitle"));
                    return;
                }
                DialogResult = DialogResult.OK;
                Close();
            };

            var cancel = new Button();
            cancel.Text = AccountsLoc.T("Cancel");
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

    // ======================= AI LIMITS (sai.limits/snapshot/1) =======================
    // SAITULS shows AI limits; it does not measure them. The numbers, the
    // pool-aware gating ("locked by weekly"), "an elapsed window is full" and the
    // typed sign-in failures all come from LIMISAW, the ecosystem's limits
    // reader, through its published document (Limisaw\LimitsExport.cs). When
    // LIMISAW is not on the machine, Antigravity accounts can still be read
    // through SAI Accounts' own `snapshot`, which carries the vendor's real
    // windows (pool_index, window, remaining_fraction, disabled, reset_time).
    //
    // Nothing here ever turns a quota STATE into a number. A window without a
    // reading is unavailable and reads "--", never 0% and never a guess.
    class LimitWindow
    {
        public string MetricId = "", Key = "", Base = "", Label = "", Title = "", Group = "", GroupLabel = "", GatedBy = "";
        public bool Available, AssumedFull, IsShort;
        // 0..100 remaining. Meaningful only when Available.
        public int Remaining;
        public double? ResetEpoch;
    }

    class LimitAccount
    {
        public string Key = "", Provider = "", ProviderLabel = "", Name = "", Title = "", Short = "";
        public string SourceId = "", SourcePath = "", Status = "", Error = "", Plan = "";
        public bool Ok, Carried, AuthFailed, HasQuota, HasFiveHour;
        public string CarriedAt = "", CarriedNote = "", AuthFailureReason = "";
        // Filled by Limits.MapToSai: the SAI Accounts identity this reading
        // belongs to. Identity comes from the account plane, never from a match
        // between numbers.
        public string SaiAccountId = "", SaiCompact = "";
        public List<LimitWindow> Windows = new List<LimitWindow>();
    }

    class LimitsSnapshot
    {
        public string Source = "";
        public double CheckedAtEpoch;
        public List<LimitAccount> Accounts = new List<LimitAccount>();
    }

    class LimitReading
    {
        public LimitAccount Account;
        public LimitWindow Window;
        public int SourceOrder;
    }

    // One SAI account as the limits mapper needs it. Kept separate from the
    // form's AccountRow so the mapping can be tested without a window.
    class SaiAccountRef
    {
        public string Id = "", Compact = "", Provider = "", ProfileLocator = "", LegacyId = "", WindowsUser = "";
    }

    static class Limits
    {
        public const string Schema = "sai.limits/snapshot/1";
        static readonly DateTime Epoch1970 = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        public static double Now() { return (DateTime.UtcNow - Epoch1970).TotalSeconds; }

        public static bool IsFresh(LimitsSnapshot snapshot, double now, double maxAge)
        {
            if (snapshot == null) return false;
            double stamp = snapshot.CheckedAtEpoch;
            return !double.IsNaN(stamp) && !double.IsInfinity(stamp) && stamp > 0
                && stamp <= now + 60 && now - stamp <= maxAge;
        }

        // The producer's existing files are also a startup display cache. Do
        // not renew their timestamp or defer the normal background refresh.
        public static LimitsSnapshot ReadCache(string directory, double now)
        {
            LimitsSnapshot latest = null;
            foreach (string name in new[] { "snapshot.json", "saituls-export.json" })
                try
                {
                    string path = Path.Combine(directory, name);
                    if (!File.Exists(path)) continue;
                    LimitsSnapshot snapshot = ParseLimisaw(File.ReadAllText(path, Encoding.UTF8), "LIMISAW");
                    if (!IsFresh(snapshot, now, double.MaxValue)) continue;
                    if (latest == null || snapshot.CheckedAtEpoch > latest.CheckedAtEpoch) latest = snapshot;
                }
                catch { }
            return latest;
        }

        static object Get(object o, string key)
        {
            var d = o as Dictionary<string, object>;
            object v;
            return d != null && d.TryGetValue(key, out v) ? v : null;
        }
        static string Str(object o, string key) { object v = Get(o, key); return v == null ? "" : Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture); }
        static bool Flag(object o, string key) { return object.Equals(Get(o, key), true); }
        static double? Num(object o, string key)
        {
            object v = Get(o, key);
            if (v is int || v is long || v is double || v is decimal)
            {
                double number = Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture);
                if (!double.IsNaN(number) && !double.IsInfinity(number)) return number;
            }
            return null;
        }

        static object ParseJson(string json)
        {
            if (string.IsNullOrEmpty(json)) return null;
            try
            {
                return new System.Web.Script.Serialization.JavaScriptSerializer { MaxJsonLength = int.MaxValue }
                    .DeserializeObject(json.Trim());
            }
            catch { return null; }
        }

        // LIMISAW's published document. Null when it is not that schema.
        public static LimitsSnapshot ParseLimisaw(string json, string source)
        {
            object doc = ParseJson(json);
            if (Str(doc, "schema") != Schema) return null;
            var snap = new LimitsSnapshot { Source = source, CheckedAtEpoch = Num(doc, "checked_at_epoch") ?? 0 };
            object[] accounts = Get(doc, "accounts") as object[];
            if (accounts == null) return snap;
            foreach (object a in accounts)
            {
                var acc = new LimitAccount
                {
                    Key = Str(a, "key"), Provider = Str(a, "provider"), ProviderLabel = Str(a, "provider_label"),
                    Name = Str(a, "name"), Title = Str(a, "title"), Short = Str(a, "short"),
                    SourceId = Str(a, "source_id"), SourcePath = Str(a, "source_path"),
                    Status = Str(a, "status"), Error = Str(a, "error"), Plan = Str(a, "plan"),
                    Ok = Flag(a, "ok"), Carried = Flag(a, "carried"), AuthFailed = Flag(a, "auth_failed"),
                    HasQuota = Flag(a, "has_quota"), HasFiveHour = Flag(a, "has_five_hour"),
                    CarriedAt = Str(a, "carried_at"), CarriedNote = Str(a, "carried_note"),
                    AuthFailureReason = Str(a, "auth_failure_reason"),
                };
                if (acc.Key.Length == 0) continue;
                object[] windows = Get(a, "windows") as object[];
                if (windows != null)
                    foreach (object w in windows)
                    {
                        double? rem = Num(w, "remaining");
                        bool available = Flag(w, "available") && rem.HasValue && rem.Value >= 0 && rem.Value <= 100;
                        acc.Windows.Add(new LimitWindow
                        {
                            MetricId = Str(w, "metric_id"), Key = Str(w, "key"), Base = Str(w, "base"),
                            Label = Str(w, "label"), Title = Str(w, "title"), Group = Str(w, "group"),
                            GroupLabel = Str(w, "group_label"), GatedBy = Str(w, "gated_by"),
                            Available = available,
                            Remaining = available ? (int)Math.Round(rem.Value) : 0,
                            ResetEpoch = Num(w, "reset_epoch") > 0 ? Num(w, "reset_epoch") : null,
                            AssumedFull = Flag(w, "assumed_full"), IsShort = Flag(w, "is_short"),
                        });
                    }
                snap.Accounts.Add(acc);
            }
            return snap;
        }

        // SAI Accounts `snapshot` (sai.accounts/snapshot/1), Antigravity only.
        // The vendor windows are normalized with LIMISAW's pool rules: windows
        // are grouped by pool_index and never merged across pools; a spent
        // weekly locks the 5h window of ITS OWN pool; a `disabled` bucket is a
        // window with no usable reading (locked when its pool is spent, "--"
        // otherwise), never 100% free; a reset time that has passed reads full.
        public static LimitsSnapshot ParseSaiSnapshot(string json, double now)
        {
            object doc = ParseJson(json);
            if (Str(doc, "schema") != "sai.accounts/snapshot/1") return null;
            var snap = new LimitsSnapshot { Source = "SAI Accounts", CheckedAtEpoch = EpochOf(Str(doc, "checked_at")) ?? 0 };
            object[] accounts = Get(doc, "accounts") as object[];
            if (accounts == null) return snap;
            foreach (object a in accounts)
            {
                if (Str(a, "provider_id") != "antigravity") continue;
                string id = Str(a, "account_id");
                string compact = Str(a, "compact_label");
                var acc = new LimitAccount
                {
                    Key = "antigravity/" + id, Provider = "antigravity", ProviderLabel = "Antigravity",
                    Name = compact, Title = "Antigravity · " + compact, Short = ShortOf("antigravity", compact),
                    SourceId = id, SaiAccountId = id, SaiCompact = compact,
                };
                string usage = Str(a, "usage_status");
                string skipped = Str(a, "skipped_reason");
                string ctx = Str(a, "context_state");
                acc.Ok = usage == "authenticated_usage";
                acc.AuthFailed = !acc.Ok && Str(a, "auth_state") == "AUTH_REQUIRED";
                acc.Status = acc.Ok ? "OK" : (acc.AuthFailed ? "AUTH_REQUIRED" : "UNAVAILABLE");
                acc.Error = acc.Ok ? "" : (skipped.Length > 0 ? skipped : (ctx == "OFFLINE" ? "CONTEXT_OFFLINE" : usage));
                var pools = new SortedDictionary<int, List<object>>();
                object[] windows = Get(a, "windows") as object[];
                if (windows != null)
                    foreach (object w in windows)
                    {
                        int pool = (int)(Num(w, "pool_index") ?? 0);
                        List<object> list;
                        if (!pools.TryGetValue(pool, out list)) { list = new List<object>(); pools[pool] = list; }
                        list.Add(w);
                    }
                foreach (KeyValuePair<int, List<object>> pool in pools)
                {
                    string group = pools.Count > 1 ? "pool" + (pool.Key + 1) : "";
                    string groupLabel = pools.Count > 1 ? "pool " + (pool.Key + 1) : "";
                    LimitWindow weekly = null;
                    var built = new List<LimitWindow>();
                    foreach (object w in pool.Value)
                    {
                        string window = Str(w, "window");
                        string bas = window == "5h" ? "five_hour" : window == "weekly" || window == "7d" ? "weekly" : window == "monthly" || window == "30d" ? "monthly" : window;
                        double? fraction = Num(w, "remaining_fraction");
                        double? reset = EpochOf(Str(w, "reset_time"));
                        bool disabled = Flag(w, "disabled");
                        var lw = new LimitWindow
                        {
                            Key = group.Length > 0 ? bas + "@" + group : bas, Base = bas,
                            Label = bas == "five_hour" ? "5h" : bas == "weekly" ? "week" : bas == "monthly" ? "month" : window,
                            Group = group, GroupLabel = groupLabel,
                            Available = acc.Ok && !disabled && fraction.HasValue && fraction.Value >= 0 && fraction.Value <= 1,
                            Remaining = fraction.HasValue && fraction.Value >= 0 && fraction.Value <= 1 ? (int)Math.Round(fraction.Value * 100) : 0,
                            ResetEpoch = reset, IsShort = bas == "five_hour",
                        };
                        if (lw.Available && reset.HasValue && reset.Value <= now) { lw.Remaining = 100; lw.AssumedFull = true; }
                        lw.Title = groupLabel.Length > 0 ? lw.Label + " (" + groupLabel + ")" : lw.Label;
                        lw.MetricId = acc.Key + "/" + lw.Key;
                        if (bas == "weekly") weekly = lw;
                        built.Add(lw);
                    }
                    bool poolSpent = weekly != null && weekly.Available && weekly.Remaining <= 0;
                    foreach (LimitWindow lw in built)
                        if (lw.Base == "five_hour" && poolSpent)
                        { lw.GatedBy = weekly.Key; lw.Available = true; lw.Remaining = 0; }
                    acc.Windows.AddRange(built);
                }
                foreach (LimitWindow w in acc.Windows)
                {
                    if (w.Available && w.Remaining > 0) acc.HasQuota = true;
                    if (w.Base == "five_hour" && w.Available && w.Remaining > 0 && w.GatedBy.Length == 0) acc.HasFiveHour = true;
                }
                snap.Accounts.Add(acc);
            }
            return snap;
        }

        static double? EpochOf(string iso)
        {
            if (string.IsNullOrEmpty(iso)) return null;
            DateTimeOffset t;
            if (!DateTimeOffset.TryParse(iso, System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.AssumeUniversal, out t)) return null;
            return (t.UtcDateTime - Epoch1970).TotalSeconds;
        }

        // LIMISAW's ShortName: provider initial + the last digit of the name.
        public static string ShortOf(string provider, string name)
        {
            string p = provider.Length > 0 ? char.ToUpperInvariant(provider[0]).ToString() : "?";
            for (int i = (name ?? "").Length - 1; i >= 0; i--)
                if (char.IsDigit(name[i])) return p + name[i];
            return p;
        }

        // A sweep that could not read an account keeps the last good numbers,
        // marked carried, exactly like LIMISAW's own cards. A carried reading is
        // still counted (dropping it would make things look healthier after a
        // failure) and is always shown as stale.
        public static void Carry(LimitsSnapshot fresh, LimitsSnapshot previous, double now)
        {
            if (fresh == null || previous == null) return;
            foreach (LimitAccount a in fresh.Accounts)
            {
                if (a.Ok || a.Carried) continue;
                bool hasReading = false;
                foreach (LimitWindow w in a.Windows) if (w.Available) { hasReading = true; break; }
                if (hasReading) continue;
                LimitAccount prev = previous.Accounts.Find(p => p.Key == a.Key);
                if (prev == null) continue;
                bool prevReading = false;
                foreach (LimitWindow w in prev.Windows) if (w.Available) { prevReading = true; break; }
                if (!prevReading) continue;
                a.Windows = prev.Windows;
                a.Carried = true;
                a.HasQuota = prev.HasQuota; a.HasFiveHour = prev.HasFiveHour;
                a.CarriedAt = prev.Carried && prev.CarriedAt.Length > 0 ? prev.CarriedAt
                    : DateTime.Now.ToString("HH:mm", System.Globalization.CultureInfo.InvariantCulture);
                a.CarriedNote = a.Error;
            }
        }

        static string Canon(string path)
        {
            if (string.IsNullOrEmpty(path)) return "";
            try { return Path.GetFullPath(path).TrimEnd('\\', '/').ToLowerInvariant(); }
            catch { return path.Trim().TrimEnd('\\', '/').ToLowerInvariant(); }
        }

        // Which SAI account a reading belongs to, by IDENTITY only: the plane's
        // account id, its legacy id, the Windows SID a context runs as, or the
        // profile directory a CLI account lives in. Two readings with the same
        // numbers are never matched to each other.
        public static void MapToSai(LimitsSnapshot snap, List<SaiAccountRef> sai)
        {
            if (snap == null || sai == null) return;
            foreach (LimitAccount a in snap.Accounts)
            {
                if (a.SaiAccountId.Length > 0)
                {
                    SaiAccountRef current = sai.Find(r => string.Equals(r.Id, a.SaiAccountId, StringComparison.OrdinalIgnoreCase)
                        && string.Equals(r.Provider, a.Provider, StringComparison.OrdinalIgnoreCase));
                    if (current != null) a.SaiCompact = current.Compact;
                    continue;
                }
                foreach (SaiAccountRef r in sai)
                {
                    if (!string.Equals(r.Provider, a.Provider, StringComparison.OrdinalIgnoreCase)) continue;
                    bool hit =
                        (a.SourceId.Length > 0 && (string.Equals(a.SourceId, r.Id, StringComparison.OrdinalIgnoreCase)
                                                   || string.Equals(a.SourceId, r.LegacyId, StringComparison.OrdinalIgnoreCase)))
                        || (a.SourcePath.Length > 0 && r.LegacyId.Length > 0
                            && r.LegacyId.EndsWith(":" + a.SourcePath, StringComparison.OrdinalIgnoreCase))
                        || (a.SourcePath.Length > 0 && r.ProfileLocator.Length > 0
                            && Canon(a.SourcePath) == Canon(r.ProfileLocator));
                    if (!hit) continue;
                    a.SaiAccountId = r.Id; a.SaiCompact = r.Compact;
                    break;
                }
            }
        }

        // ---- presentation (same semantics as LIMISAW / FastPrompter) ----------
        // "Showing: Left" draws the remaining percent; "Showing: Used" draws
        // 100 - remaining. Presentation only: it never changes which reading is
        // selected. An unavailable window is "--" in both.
        public static string Shown(LimitWindow w, bool showUsed)
        {
            if (w == null || !w.Available) return "--";
            return (w.AssumedFull ? "~" : "") + (showUsed ? 100 - w.Remaining : w.Remaining) + "%";
        }

        // LIMISAW CountdownText: "<1m", "12m", "3h", "2d"; a past or unknown
        // reset is "--", never "0m".
        public static string Countdown(double? epoch, double now)
        {
            if (!epoch.HasValue || epoch.Value <= 0) return "--";
            double left = epoch.Value - now;
            if (left < 60) return left >= 0 ? "<1m" : "--";
            if (left < 3600) return ((int)(left / 60)) + "m";
            if (left < 86400) return ((int)Math.Round(left / 3600)) + "h";
            return ((int)Math.Round(left / 86400)) + "d";
        }

        // One word for an account's reading. OK, STALE (carried or old),
        // SIGN_IN (the vendor refused the authenticated read), OFFLINE (its
        // managed context is not running) or UNAVAILABLE. None of them is 0%.
        public static string State(LimitAccount a, bool snapshotStale)
        {
            if (a == null) return "UNAVAILABLE";
            if (a.AuthFailed || a.Status == "AUTH_REQUIRED") return "SIGN_IN";
            if (!a.Ok && !a.Carried && a.Error == "CONTEXT_OFFLINE") return "OFFLINE";
            if (a.Carried) return "STALE";
            if (!a.Ok) return "UNAVAILABLE";
            return snapshotStale ? "STALE" : "OK";
        }

        // The reading LIMISAW's tray would pick (ResolveReading, unpinned):
        //   "lowest" -> the lowest reading with quota left across the complete
        //               eligible set, else the lowest available one;
        //   "any"    -> the reading already shown while it is still usable, else
        //               the FIRST usable reading in stable order; no 0% fallback,
        //               and an auth-rejected reading is not usable.
        // Eligible = every reading of every account passed in, uncapped: what
        // fits on screen never decides what is selected.
        public static LimitWindow Resolve(List<LimitAccount> eligible, string mode, string currentMetricId, out LimitAccount owner)
        {
            string fallback;
            return Resolve(eligible, eligible, mode, currentMetricId, null, null, out owner, out fallback);
        }

        // LIMISAW's pin is resolved against the full in-service set. Local
        // picture filters never retarget an explicit pin. Automatic modes use
        // the complete selected set, with explicit reading order and hides.
        public static LimitWindow Resolve(List<LimitAccount> all, List<LimitAccount> eligible, string mode,
            string currentMetricId, ICollection<string> hidden, IEnumerable<string> order,
            out LimitAccount owner, out string fallback)
        {
            owner = null;
            fallback = "";
            if (!string.IsNullOrEmpty(mode) && mode != "lowest" && mode != "any")
            {
                bool found = false;
                foreach (LimitAccount a in all ?? new List<LimitAccount>())
                    foreach (LimitWindow w in a.Windows)
                        if (w.MetricId == mode)
                        {
                            found = true;
                            if ((a.Ok || a.Carried) && w.Available) { owner = a; return w; }
                        }
                fallback = found ? "PIN_UNAVAILABLE" : "PIN_GONE";
            }
            int minAny = int.MaxValue, minPos = int.MaxValue;
            LimitWindow pickAny = null, pickPos = null, pickCurrent = null, pickFirstUsable = null;
            LimitAccount ownAny = null, ownPos = null, ownCurrent = null, ownFirst = null;
            foreach (LimitReading reading in Readings(eligible, hidden, order))
            {
                    LimitAccount a = reading.Account;
                    LimitWindow w = reading.Window;
                    bool available = (a.Ok || a.Carried) && w.Available;
                    if (!available) continue;
                    if (w.Remaining < minAny) { minAny = w.Remaining; pickAny = w; ownAny = a; }
                    if (w.Remaining > 0 && w.Remaining < minPos) { minPos = w.Remaining; pickPos = w; ownPos = a; }
                    bool usableAny = w.Remaining > 0 && !a.AuthFailed;
                    if (usableAny && pickFirstUsable == null) { pickFirstUsable = w; ownFirst = a; }
                    if (usableAny && currentMetricId != null && w.MetricId == currentMetricId) { pickCurrent = w; ownCurrent = a; }
            }
            if (mode == "any")
            {
                if (pickCurrent != null) { owner = ownCurrent; return pickCurrent; }
                owner = ownFirst; return pickFirstUsable;
            }
            if (pickPos != null) { owner = ownPos; return pickPos; }
            owner = ownAny; return pickAny;
        }

        public static List<LimitReading> Readings(List<LimitAccount> accounts, ICollection<string> hidden, IEnumerable<string> order)
        {
            var readings = new List<LimitReading>();
            int sourceOrder = 0;
            foreach (LimitAccount a in accounts ?? new List<LimitAccount>())
                foreach (LimitWindow w in a.Windows)
                    if (hidden == null || !hidden.Contains(w.MetricId))
                        readings.Add(new LimitReading { Account = a, Window = w, SourceOrder = sourceOrder++ });
            var ranks = new Dictionary<string, int>(StringComparer.Ordinal);
            if (order != null)
                foreach (string id in order) if (!ranks.ContainsKey(id)) ranks[id] = ranks.Count;
            if (ranks.Count > 0)
                readings.Sort(delegate(LimitReading a, LimitReading b) {
                    int ra, rb;
                    if (!ranks.TryGetValue(a.Window.MetricId, out ra)) ra = int.MaxValue;
                    if (!ranks.TryGetValue(b.Window.MetricId, out rb)) rb = int.MaxValue;
                    int ranked = ra.CompareTo(rb);
                    return ranked != 0 ? ranked : a.SourceOrder.CompareTo(b.SourceOrder);
                });
            return readings;
        }

        // LIMISAW's account filters. A local hide, "hide spent" and "only with a
        // usable 5h window" decide what is DRAWN; every account is still read.
        // An account the SAI plane hides or takes out of service (global state)
        // is withdrawn separately, before these run.
        public static List<LimitAccount> Visible(List<LimitAccount> all, ICollection<string> hiddenKeys,
                                                 bool hideSpent, bool onlyFiveHour)
        {
            var shown = new List<LimitAccount>();
            foreach (LimitAccount a in all ?? new List<LimitAccount>())
            {
                if (hiddenKeys != null && hiddenKeys.Contains(a.Key)) continue;
                if (hideSpent && !a.HasQuota) continue;
                if (onlyFiveHour && !a.HasFiveHour) continue;
                shown.Add(a);
            }
            return shown;
        }

        // Fill colour reads the REMAINING percent in both Left and Used modes,
        // as in LIMISAW, so an exhausted account is never a full green bar.
        public static Color FillColor(int remaining, bool available)
        {
            if (!available) return Palette.MUTED;
            if (remaining < 25) return Palette.DANGERTXT;
            if (remaining < 50) return Palette.WARNING;
            if (remaining < 75) return Palette.LINK;
            return Palette.SUCCESS;
        }
    }

    // ======================= 5h WINDOW KEEPER (T-267) =======================
    // The same idea as ZAICODE's quota auto-ping (9router quotaAutoPing):
    // a 5-hour window only starts counting at the first request after it
    // resets, so an idle reset wastes the window. An opted-in account gets ONE
    // tiny request right after its 5h window resets, which starts the next
    // window immediately.
    //
    // Rules, as ZAICODE ships them:
    //   Claude -> ping once the 5h reset time has passed;
    //   Codex  -> ping when the 5h reset time SLIDES forward between readings
    //             (an inactive window reports "now + 5h" on every read), at
    //             most every 10 minutes, and never while the weekly is spent;
    //   both   -> never when the 5h window itself is spent, at most one ping
    //             per reset, 15 minutes of cool-down after a failed ping.
    // Opt-in per account and OFF by default: every ping spends a little quota.
    // Antigravity is not supported (ZAICODE does not ping it either).
    // The lifecycle of one account's 5h window. The keeper keeps it per account
    // so that "why has my window not started" always has an answer.
    static class KeeperStates
    {
        public const string Unknown = "UNKNOWN";                 // no usable evidence yet
        public const string IdleCandidate = "IDLE_CANDIDATE";     // one bounded bootstrap is allowed
        public const string PrimingPending = "PRIMING_PENDING";   // admitted, request in flight
        public const string PrimingAccepted = "PRIMING_ACCEPTED"; // accepted, window not yet confirmed
        public const string RunningVerified = "RUNNING_VERIFIED"; // the vendor confirms a running window
        public const string WaitingForReset = "WAITING_FOR_RESET"; // the window runs, its reset is ahead
        public const string RenewalEligible = "RENEWAL_ELIGIBLE"; // one bounded renewal is allowed
        public const string Cooldown = "COOLDOWN";                // a failed attempt is cooling down
        public const string Blocked = "BLOCKED";                  // something outside the window blocks it
        public const string Failed = "FAILED";                    // the last attempt failed
        public const string OutcomeUnknown = "OUTCOME_UNKNOWN";   // nobody knows how the last attempt ended
    }

    // How sure the keeper is about the last attempt. The vendor's own answer
    // decides it; an exit code of zero alone proves nothing.
    static class KeeperOutcomes
    {
        public const string None = "NONE";
        public const string AcceptedUnverified = "ACCEPTED_UNVERIFIED";
        public const string Verified = "VERIFIED";
        public const string FailedOutcome = "FAILED";
        public const string UnknownOutcome = "UNKNOWN";
    }

    class KeeperAccountState
    {
        public double LastResetEpoch;       // the 5h reset seen on the previous reading
        public double LastPingedReset;      // reset instant already pinged for
        public double LastPingAt;           // when a ping last succeeded
        public double LastFailureAt;        // when a ping last failed
        public string LastResult = "";
        // ---- T-303: the durable admission record of the one bounded request ----
        public string State = KeeperStates.Unknown;
        public string AttemptId = "";        // the identity of the last attempt
        public double AttemptAt;             // when that attempt was made
        public string ProfileId = "";        // the profile directory it ran in
        public string WindowId = "";         // the window it was for
        public double LastAcceptedAt;        // the last request the CLI accepted
        public double LastVerifiedReset;     // the last vendor-confirmed reset
        public double LastEstimatedReset;    // the last predicted renewal boundary
        public double CooldownUntil;         // no new attempt before this instant
        public string OutcomeConfidence = KeeperOutcomes.None;
        public int Attempts;                 // attempts made for the current window
        public bool PrimedOnce;              // the one bounded bootstrap was used
        public string LastDecision = "";     // why the keeper did or did not send
    }

    class KeeperDecision
    {
        public string State = KeeperStates.Unknown;
        public string Reason = "";
        public bool Eligible;      // exactly one bounded request may start now
        public bool Initial;       // this is the one bootstrap request
        public double EstimatedReset;  // the renewal boundary, when there is one
        public bool ResetEstimated;    // the boundary is a prediction, never vendor evidence
    }

    static class FiveHourKeeper
    {
        public const double ResetLeadS = 5;          // pingLeadMs
        public const double FailureCooldownS = 900;  // failureCooldownMs
        public const double CodexMinIntervalS = 600; // codex minPingIntervalMs
        public const double CodexDriftS = 30;        // codex resetAtDriftMs
        public const double WindowSeconds = 5 * 3600; // the documented five-hour window
        public const double RenewalMarginS = 60;      // safety margin before an estimated renewal
        public const double RetryGraceS = 300;       // an unanswered attempt is not replayed before this
        public const int MaxAttempts = 3;            // attempts per window: no retry storm
        public const string ModelAlias = "haiku";     // the CLI's own family alias, never a retired id
        public const string ModelFallback = "claude-haiku-4-5-20251001";

        public static bool Supported(string provider)
        {
            return provider == "claude" || provider == "codex";
        }

        // The 5h window of an account's main pool, or null.
        public static LimitWindow FiveHour(LimitAccount a)
        {
            if (a == null) return null;
            foreach (LimitWindow w in a.Windows) if (w.Base == "five_hour" && w.Group.Length == 0) return w;
            foreach (LimitWindow w in a.Windows) if (w.Base == "five_hour") return w;
            return null;
        }

        static bool BlockingSpent(LimitAccount a, LimitWindow five)
        {
            foreach (LimitWindow w in a.Windows)
                if (w != five && w.Base != "five_hour" && w.Group == five.Group && w.Available && w.Remaining <= 0) return true;
            return five.GatedBy.Length > 0;
        }

        // The whole decision, with the lifecycle state it leaves the account in.
        // Two questions are kept apart here (T-303):
        //   INITIAL PRIMING - one bounded request for an opted-in account with
        //                     fresh, valid, full quota and no confirmed window;
        //   RENEWAL         - one bounded request after a confirmed reset, or
        //                     after a conservatively ESTIMATED one.
        // A missing reset is never read as proof that a window is running, and
        // a predicted boundary is never presented as a vendor-confirmed reset.
        public static KeeperDecision DecideState(LimitAccount a, KeeperAccountState st, double now)
        {
            var d = new KeeperDecision();
            if (a == null || !Supported(a.Provider)) { d.State = KeeperStates.Unknown; d.Reason = "unsupported"; return d; }
            if (!a.Ok || a.Carried || a.AuthFailed) { d.State = KeeperStates.Unknown; d.Reason = "no fresh reading"; return d; }
            LimitWindow five = FiveHour(a);
            if (five == null) { d.State = KeeperStates.Unknown; d.Reason = "no 5h window reported"; return d; }

            // An attempt that is in flight, or whose outcome nobody knows, is
            // reconciled with the vendor's own reading -- never replayed blindly.
            bool allowRetry = true, vendorSaysIdle = false;
            if (st.State == KeeperStates.PrimingPending || Unproven(st))
            {
                string rec = Reconcile(a, five, st, now);
                if (rec == "verified")
                {
                    // The vendor confirms a window: the attempt did its work.
                    if (five.ResetEpoch.HasValue)
                    {
                        st.LastVerifiedReset = five.ResetEpoch.Value;
                        st.LastResetEpoch = five.ResetEpoch.Value;
                    }
                    st.LastEstimatedReset = 0;
                    st.OutcomeConfidence = KeeperOutcomes.Verified;
                    st.State = KeeperStates.RunningVerified;
                    st.Attempts = 0;                 // a new window, a new budget
                }
                else if (rec == "retry")
                {
                    vendorSaysIdle = true;   // the vendor says no window is running
                    allowRetry = true;
                }
                else
                {
                    // "hold": the attempt is not proven and is never replayed
                    // blindly. A request the CLI DID accept still has a
                    // predicted window end, and that prediction -- not the
                    // silence of the vendor -- governs the next request.
                    allowRetry = false;
                    if (st.OutcomeConfidence != KeeperOutcomes.AcceptedUnverified)
                    {
                        d.State = st.State == KeeperStates.PrimingPending ? KeeperStates.PrimingPending : KeeperStates.OutcomeUnknown;
                        d.Reason = "the last attempt's outcome is unknown";
                        return d;
                    }
                }
            }

            if (st.LastFailureAt > 0 && now - st.LastFailureAt < FailureCooldownS)
            { d.State = KeeperStates.Cooldown; d.Reason = "cooling down after a failed ping"; return d; }
            if (st.CooldownUntil > now)
            { d.State = KeeperStates.Cooldown; d.Reason = "cooling down after a failed ping"; return d; }
            if (st.Attempts >= MaxAttempts)
            { d.State = KeeperStates.Blocked; d.Reason = "the attempt budget for this window is used up"; return d; }
            if (BlockingSpent(a, five))
            { d.State = KeeperStates.Blocked; d.Reason = "a longer window is spent"; return d; }
            if (five.Available && five.Remaining <= 0 && !five.AssumedFull)
            { d.State = KeeperStates.Blocked; d.Reason = "5h window is spent"; return d; }

            return a.Provider == "codex"
                ? CodexDecision(five, st, now, d)
                : ClaudeDecision(five, st, now, d, allowRetry, vendorSaysIdle);
        }

        // The reason only: the old contract, on top of the new decision.
        public static string Decide(LimitAccount a, KeeperAccountState st, double now)
        {
            KeeperDecision d = DecideState(a, st, now);
            return d.Eligible ? "" : d.Reason;
        }

        // An attempt whose outcome nobody knows: the vendor's own reading
        // reconciles it. A confirmed reset proves the window runs; a full
        // window with no reset only proves that the attempt never reached
        // the vendor -- and only such an attempt may ever be tried again.
        static string Reconcile(LimitAccount a, LimitWindow five, KeeperAccountState st, double now)
        {
            if (a.Provider == "codex")
            {
                // The Codex reset slides while the window is idle and stands
                // still while it runs: a fixed reset is a running window.
                if (!five.ResetEpoch.HasValue) return "hold";
                double key = Math.Floor(five.ResetEpoch.Value / 60) * 60;
                if (st.LastPingedReset > 0 && Math.Abs(st.LastPingedReset - key) < 1) return "verified";
                return "retry";
            }
            // A reset that is still AHEAD is a running window the vendor
            // confirms. A reset that already passed only says that the window
            // ended: it proves nothing about the attempt that was made.
            if (five.ResetEpoch.HasValue && five.ResetEpoch.Value > now) return "verified";
            if (five.Available && five.Remaining < 100) return "verified";   // the quota moved: a window ran
            if (now < st.CooldownUntil) return "hold";
            // Only an attempt the CLI never answered may be tried again, and
            // only after a grace period that lets a late answer arrive.
            if ((st.OutcomeConfidence == KeeperOutcomes.UnknownOutcome || st.OutcomeConfidence == KeeperOutcomes.None)
                && st.AttemptAt > 0 && now - st.AttemptAt > RetryGraceS) return "retry";
            return "hold";
        }

        // The last attempt is not proven yet: it was sent, and the vendor has
        // not confirmed a window since.
        static bool Unproven(KeeperAccountState st)
        {
            return st.OutcomeConfidence == KeeperOutcomes.AcceptedUnverified
                || st.OutcomeConfidence == KeeperOutcomes.UnknownOutcome;
        }

        // Codex: the reset SLIDES forward while the window is idle, and stands
        // still while it runs.
        static KeeperDecision CodexDecision(LimitWindow five, KeeperAccountState st, double now, KeeperDecision d)
        {
            double previous = st.LastResetEpoch;
            if (!five.ResetEpoch.HasValue) { d.State = KeeperStates.Unknown; d.Reason = "no 5h reset reported"; return d; }
            double reset = five.ResetEpoch.Value;
            st.LastVerifiedReset = reset;
            st.LastResetEpoch = reset;
            double resetKey = Math.Floor(reset / 60) * 60;
            if (st.LastPingedReset > 0 && Math.Abs(st.LastPingedReset - resetKey) < 1)
            { d.State = KeeperStates.RunningVerified; d.Reason = "already pinged for this reset"; return d; }
            if (previous <= 0) { d.State = KeeperStates.Unknown; d.Reason = "waiting for a second reading"; return d; }
            if (reset - previous < CodexDriftS) { d.State = KeeperStates.RunningVerified; d.Reason = "5h window is running"; return d; }
            if (st.LastPingAt > 0 && now - st.LastPingAt < CodexMinIntervalS) { d.State = KeeperStates.Cooldown; d.Reason = "pinged recently"; return d; }
            d.State = KeeperStates.RenewalEligible; d.Eligible = true; d.EstimatedReset = reset;
            return d;
        }

        // Claude: a window that has reset and has seen no request since reports
        // NO reset time at all. That is neither proof of a running window nor
        // proof of an idle one -- it is the reason the keeper keeps a lifecycle.
        static KeeperDecision ClaudeDecision(LimitWindow five, KeeperAccountState st, double now, KeeperDecision d, bool allowRetry, bool vendorSaysIdle)
        {
            double previous = st.LastResetEpoch;
            if (five.ResetEpoch.HasValue)
            {
                double reset = five.ResetEpoch.Value;
                st.LastVerifiedReset = reset;
                st.LastEstimatedReset = 0;
                st.LastResetEpoch = reset;
                if (st.OutcomeConfidence == KeeperOutcomes.AcceptedUnverified)
                { st.OutcomeConfidence = KeeperOutcomes.Verified; st.State = KeeperStates.RunningVerified; st.Attempts = 0; }
                double resetKey = Math.Floor(reset / 60) * 60;
                if (st.LastPingedReset > 0 && Math.Abs(st.LastPingedReset - resetKey) < 1)
                { d.State = KeeperStates.RunningVerified; d.Reason = "already pinged for this reset"; return d; }
                if (now < reset - ResetLeadS)
                { d.State = KeeperStates.RunningVerified; d.Reason = "5h window is running"; return d; }
                // A vendor-confirmed reset is authoritative and is never
                // dressed up as an estimate.
                d.State = KeeperStates.RenewalEligible; d.Eligible = true; d.EstimatedReset = reset; d.ResetEstimated = false;
                return d;
            }

            // A window we have seen running is still running until the vendor
            // says otherwise.
            if (previous > now)
            { d.State = KeeperStates.RunningVerified; d.Reason = "5h window is running"; return d; }

            // The reset we last saw has passed and the vendor reports none: an
            // idle Claude window looks exactly like this.
            if (previous > 0)
            {
                double resetKey = Math.Floor(previous / 60) * 60;
                if (st.LastPingedReset > 0 && Math.Abs(st.LastPingedReset - resetKey) < 1)
                { d.State = KeeperStates.RunningVerified; d.Reason = "already pinged for this reset"; return d; }
                d.State = KeeperStates.RenewalEligible; d.Eligible = true;
                // The window this request starts would end at about now plus
                // the documented window length. A prediction, never a fact.
                d.EstimatedReset = now + WindowSeconds; d.ResetEstimated = true;
                return d;
            }

            // A request was accepted, but the vendor has not confirmed a window.
            // Its end is PREDICTED from the accepted request and the documented
            // window length -- an estimate, never a vendor-confirmed reset.
            if (st.LastAcceptedAt > 0)
            {
                double estimate = st.LastAcceptedAt + WindowSeconds;
                st.LastEstimatedReset = estimate;
                d.EstimatedReset = estimate; d.ResetEstimated = true;
                bool full = five.Available && five.Remaining >= 100;
                if (full && (vendorSaysIdle || now >= estimate - RenewalMarginS))
                {
                    d.State = KeeperStates.RenewalEligible; d.Eligible = true;
                    d.Reason = vendorSaysIdle ? "the vendor reports no running window" : "the estimated window has passed (ESTIMATED)";
                    return d;
                }
                d.State = KeeperStates.WaitingForReset;
                d.Reason = "the window is not confirmed yet (ESTIMATED until " + EstText(estimate) + ")";
                return d;
            }

            // Never primed, or the last attempt failed: a fresh, full window
            // with no reset is the ONE bounded bootstrap candidate -- or a
            // bounded retry of an attempt that never reached the vendor.
            if (five.Available && five.Remaining >= 100)
            {
                // The ONE bootstrap: only for an account that never had an
                // attempt. An account that already tried is either on its
                // cooldown or waiting for its own evidence.
                bool bootstrap = !st.PrimedOnce && st.AttemptAt <= 0;
                bool retry = allowRetry && (st.OutcomeConfidence == KeeperOutcomes.FailedOutcome
                                          || st.OutcomeConfidence == KeeperOutcomes.UnknownOutcome
                                          || st.OutcomeConfidence == KeeperOutcomes.None);
                if (bootstrap || retry)
                {
                    d.State = bootstrap ? KeeperStates.IdleCandidate : KeeperStates.RenewalEligible;
                    d.Eligible = true; d.Initial = bootstrap;
                    return d;
                }
            }
            d.State = KeeperStates.RunningVerified; d.Reason = "5h window is running";
            return d;
        }

        // A predicted instant, in the operator's own clock. .NET 4.0 has no
        // FromUnixTimeSeconds, so the epoch is added to the epoch itself.
        public static string EstText(double epoch)
        {
            if (epoch <= 0) return "";
            try { return new DateTime(1970, 1, 1).ToLocalTime().AddSeconds(epoch).ToString("HH:mm"); }
            catch { return ""; }
        }

        // One attempt, with the outcome the vendor's answer actually proves.
        public static void RecordAttempt(KeeperAccountState st, LimitAccount a, string outcome, double now, string result)
        {
            LimitWindow five = FiveHour(a);
            st.LastResult = result ?? "";
            st.OutcomeConfidence = outcome ?? KeeperOutcomes.UnknownOutcome;
            if (outcome == KeeperOutcomes.FailedOutcome)
            {
                st.LastFailureAt = now;
                st.CooldownUntil = now + FailureCooldownS;
                st.State = KeeperStates.Failed;
                return;
            }
            if (outcome == KeeperOutcomes.UnknownOutcome)
            {
                st.LastFailureAt = now;
                st.CooldownUntil = now + FailureCooldownS;
                st.State = KeeperStates.OutcomeUnknown;
                return;
            }
            st.LastFailureAt = 0;
            st.CooldownUntil = 0;
            st.LastPingAt = now;
            st.LastAcceptedAt = now;
            st.PrimedOnce = true;
            st.State = KeeperStates.PrimingAccepted;
            // The window this request starts is PREDICTED to end here. The
            // vendor has confirmed nothing: this stays an ESTIMATE.
            st.LastEstimatedReset = now + WindowSeconds;
            double reset = five != null && five.ResetEpoch.HasValue ? five.ResetEpoch.Value : st.LastResetEpoch;
            if (reset > 0) st.LastPingedReset = Math.Floor(reset / 60) * 60;
        }

        public static void RecordPing(KeeperAccountState st, LimitAccount a, bool ok, double now, string result)
        {
            RecordAttempt(st, a, ok ? KeeperOutcomes.AcceptedUnverified : KeeperOutcomes.FailedOutcome, now, result);
        }

        // What the CLI's answer proves. The Claude CLI answers with JSON, so
        // the result envelope is READ as JSON: "is_error" is a boolean, and
        // the vendor's own whitespace around it cannot change the verdict.
        // Exit code 0 alone proves nothing about the window, the exit code
        // and the envelope must agree, and stderr is never read as an
        // envelope. When the outcome cannot be established, it stays UNKNOWN.
        public static string ClassifyOutcome(string provider, bool exitedOk, string stdout, string stderr, bool timedOut)
        {
            if (timedOut) return KeeperOutcomes.UnknownOutcome;
            if (provider == "claude")
            {
                Dictionary<string, object> env = ResultEnvelope(stdout);
                if (env == null)
                {
                    // No envelope: a failed CLI is a failure, a silent CLI is
                    // an unknown outcome -- never an accepted request.
                    return exitedOk ? KeeperOutcomes.UnknownOutcome : KeeperOutcomes.FailedOutcome;
                }
                object flag;
                if (!env.TryGetValue("is_error", out flag) || !(flag is bool))
                    return KeeperOutcomes.UnknownOutcome;      // no boolean verdict: nothing is established
                if ((bool)flag) return KeeperOutcomes.FailedOutcome;   // the envelope says it failed
                if (!exitedOk) return KeeperOutcomes.FailedOutcome;   // exit and envelope must agree
                return KeeperOutcomes.AcceptedUnverified;
            }
            if (!exitedOk) return KeeperOutcomes.FailedOutcome;
            return (stdout ?? "").Trim().Length > 0 ? KeeperOutcomes.AcceptedUnverified : KeeperOutcomes.UnknownOutcome;
        }

        // The result envelope of the CLI's answer, or null when there is none:
        // one JSON value, or a stream of records where the last record that
        // carries the envelope wins. A nested "result" field of some other
        // event is not an envelope.
        static Dictionary<string, object> ResultEnvelope(string stdout)
        {
            if (string.IsNullOrEmpty(stdout)) return null;
            string text = stdout.Trim();
            if (text.Length == 0) return null;
            Dictionary<string, object> whole = JsonObject(text);
            if (whole != null) return IsResultEnvelope(whole) ? whole : null;
            Dictionary<string, object> found = null;
            foreach (string line in text.Split('\n'))
            {
                Dictionary<string, object> obj = JsonObject(line.Trim());
                if (obj == null) continue;
                if (IsResultEnvelope(obj)) found = obj;
            }
            return found;
        }

        // One JSON object, or null when the text is not one.
        static Dictionary<string, object> JsonObject(string text)
        {
            if (string.IsNullOrEmpty(text)) return null;
            try
            {
                var ser = new System.Web.Script.Serialization.JavaScriptSerializer { MaxJsonLength = 4194304 };
                return ser.DeserializeObject(text) as Dictionary<string, object>;
            }
            catch { return null; }
        }

        // The envelope of the CLI's own result event: the boolean verdict and
        // the type that names it. Another event that merely mentions a result
        // is not an envelope.
        static bool IsResultEnvelope(Dictionary<string, object> obj)
        {
            object flag;
            if (!obj.TryGetValue("is_error", out flag) || !(flag is bool)) return false;
            object type;
            if (obj.TryGetValue("type", out type))
            {
                string t = type as string;
                if (t == null || !t.Equals("result", StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
        }

        // A rejection the CLI makes on its own, before the vendor ever sees the
        // request: an unknown flag or an unknown model. Trying the next rung of
        // the ladder after such a rejection spends no quota.
        public static bool LocalRejection(string stdout, string stderr)
        {
            string t = ((stdout ?? "") + "\n" + (stderr ?? "")).ToLowerInvariant();
            return t.Contains("unknown option") || t.Contains("unknown argument") || t.Contains("invalid option")
                || t.Contains("unknown model") || t.Contains("invalid model") || t.Contains("model not found")
                || t.Contains("unsupported model") || t.Contains("invalid choice") || t.Contains("unknown argument");
        }

        // The vendor's own CLI, in the account's own profile directory, with a
        // one-word prompt and the smallest model -- the same request ZAICODE
        // sends. No credential is read, copied or passed by SAITULS: the CLI
        // authenticates itself from its profile.
        public static ProcessStartInfo PingCommand(string provider, string profileDir, string exe)
        {
            return PingCommand(provider, profileDir, exe, null, "", true);
        }

        // The same request, with only the flags the installed CLI understands
        // and a model the operator can override.
        public static ProcessStartInfo PingCommand(string provider, string profileDir, string exe,
                                                   KeeperCliCaps caps, string model, bool maxTurns)
        {
            return PingCommand(provider, profileDir, exe, caps,
                new PingRung { Model = model, MaxTurns = maxTurns, OptionalFlags = true });
        }

        // One rung of the request ladder: which model to name, and whether the
        // optional flags go with it. A rung the CLI rejects on its own (an
        // unknown flag, an unknown model) costs no quota, so the next rung
        // may be tried; a request the vendor did answer is never repeated.
        public class PingRung
        {
            public string Model = "";
            public bool MaxTurns = true;
            public bool OptionalFlags = true;
        }

        // The rungs of one bounded request, safest first: the CLI's own model
        // family alias (never a retired dated id), then the dated fallback,
        // then the same request without the optional flags.
        public static List<PingRung> Ladder(string provider, string model)
        {
            var rungs = new List<PingRung>();
            if (provider != "claude")
            {
                rungs.Add(new PingRung { Model = "", MaxTurns = false, OptionalFlags = false });
                return rungs;
            }
            string m = string.IsNullOrEmpty(model) ? ModelAlias : model.Trim();
            rungs.Add(new PingRung { Model = m });
            if (m != ModelFallback) rungs.Add(new PingRung { Model = ModelFallback });
            rungs.Add(new PingRung { Model = m, MaxTurns = false });
            rungs.Add(new PingRung { Model = m, MaxTurns = false, OptionalFlags = false });
            return rungs;
        }

        // The request itself, built from one rung and the flags the installed
        // CLI actually understands.
        public static ProcessStartInfo PingCommand(string provider, string profileDir, string exe,
                                                   KeeperCliCaps caps, PingRung rung)
        {
            if (string.IsNullOrEmpty(exe) || string.IsNullOrEmpty(profileDir)) return null;
            if (rung == null) rung = new PingRung();
            string args, home, homeVar;
            if (provider == "claude")
            {
                string m = string.IsNullOrEmpty(rung.Model) ? ModelAlias : rung.Model.Trim();
                args = "-p hi --model " + m + " --output-format json";
                if (rung.MaxTurns) args += " --max-turns 1";
                // No project instructions, no MCP, no session, no tools: one
                // short answer and nothing else.
                if (rung.OptionalFlags)
                {
                    if (caps == null || caps.SafeMode) args += " --safe-mode";
                    if (caps == null || caps.StrictMcp) args += " --strict-mcp-config";
                    if (caps == null || caps.DisableSlash) args += " --disable-slash-commands";
                    if (caps == null || caps.NoSession) args += " --no-session-persistence";
                    if (caps == null || caps.Tools) args += " --tools \"\"";
                }
                home = profileDir; homeVar = "CLAUDE_CONFIG_DIR";
            }
            else if (provider == "codex")
            {
                args = "exec --skip-git-repo-check \"Reply with OK.\"";
                home = profileDir; homeVar = "CODEX_HOME";
            }
            else return null;
            ProcessStartInfo psi = ShimAware(exe, args);
            psi.EnvironmentVariables[homeVar] = home;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.RedirectStandardInput = true;
            psi.WorkingDirectory = Path.GetTempPath();
            return psi;
        }

        // A .cmd/.bat found on PATH is a shell shim (an npm install puts one
        // next to every .exe-less CLI), and CreateProcess cannot start one:
        // run it through cmd.exe so the ping is not permanently broken on
        // machines that only have the shim. cmd.exe strips the first and the
        // last quote of a /c string, so the whole command is quoted twice.
        static ProcessStartInfo ShimAware(string exe, string args)
        {
            bool shim = exe.EndsWith(".cmd", StringComparison.OrdinalIgnoreCase)
                     || exe.EndsWith(".bat", StringComparison.OrdinalIgnoreCase);
            ProcessStartInfo psi = shim
                ? new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"), "/c \"\"" + exe + "\" " + args + "\"")
                : new ProcessStartInfo(exe, args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            return psi;
        }

        // The probe that asks the CLI what it understands. It sends no request
        // and spends no quota: only --help is asked for.
        public static ProcessStartInfo CapsProbeCommand(string exe)
        {
            if (string.IsNullOrEmpty(exe)) return null;
            return ShimAware(exe, "--help");
        }
    }

    // What the installed CLI understands, read from its own --help. An older
    // CLI simply gets fewer flags; a newer one is never fed a retired flag.
    class KeeperCliCaps
    {
        public bool SafeMode, StrictMcp, NoSession, DisableSlash, Tools, MaxTurns;

        public static KeeperCliCaps Parse(string help)
        {
            var c = new KeeperCliCaps();
            string t = help ?? "";
            c.SafeMode = t.IndexOf("--safe-mode", StringComparison.Ordinal) >= 0;
            c.StrictMcp = t.IndexOf("--strict-mcp-config", StringComparison.Ordinal) >= 0;
            c.NoSession = t.IndexOf("--no-session-persistence", StringComparison.Ordinal) >= 0;
            c.DisableSlash = t.IndexOf("--disable-slash-commands", StringComparison.Ordinal) >= 0;
            c.Tools = t.IndexOf("--tools <tools", StringComparison.Ordinal) >= 0;
            c.MaxTurns = t.IndexOf("--max-turns", StringComparison.Ordinal) >= 0;
            return c;
        }
    }

    // The other window keeper on this machine (ZAICODE) keeps its own record
    // of the windows it started. SAITULS only READS that record: it never
    // writes there and never changes the other application. One account, one
    // effective owner: that record is the evidence that stops a second
    // application from sending a duplicate priming request.
    static class KeeperCrossApp
    {
        public const string AppName = "ZAICODE";
        static DateTime _readAt = DateTime.MinValue;
        static bool _rolling;
        static readonly Dictionary<string, Dictionary<string, object>> _started = new Dictionary<string, Dictionary<string, object>>();

        public static string Dir()
        {
            string app = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            if (string.IsNullOrEmpty(app)) return "";
            return Path.Combine(app, AppName);
        }

        // The account key the other application uses for a profile.
        public static string AccountKey(string provider, string profileDir)
        {
            if (string.IsNullOrEmpty(provider) || string.IsNullOrEmpty(profileDir)) return "";
            return provider + ":" + profileDir.ToLowerInvariant();
        }

        // Reads the other application's files, at most once a minute.
        static void Load()
        {
            if ((DateTime.Now - _readAt).TotalSeconds < 60) return;
            _readAt = DateTime.Now;
            _rolling = false;
            _started.Clear();
            string dir = Dir();
            if (dir.Length == 0) return;
            try
            {
                var ser = new System.Web.Script.Serialization.JavaScriptSerializer { MaxJsonLength = 4194304 };
                string cfg = Path.Combine(dir, "zaicode-engines.json");
                if (File.Exists(cfg))
                {
                    var o = ser.DeserializeObject(File.ReadAllText(cfg)) as Dictionary<string, object>;
                    object roll;
                    if (o != null && o.TryGetValue("keepWindowsRolling", out roll))
                    {
                        try { _rolling = Convert.ToBoolean(roll); } catch { _rolling = false; }
                    }
                }
                string cache = Path.Combine(dir, "zaicode-engines-cache.json");
                if (!File.Exists(cache)) return;
                var c = ser.DeserializeObject(File.ReadAllText(cache)) as Dictionary<string, object>;
                if (c == null) return;
                object limObj;
                if (!c.TryGetValue("limits", out limObj)) return;
                var lim = limObj as Dictionary<string, object>;
                if (lim == null) return;
                foreach (var kv in lim)
                {
                    var acc = kv.Value as Dictionary<string, object>;
                    if (acc == null) continue;
                    object ws;
                    if (!acc.TryGetValue("windowStart", out ws)) continue;
                    var rec = ws as Dictionary<string, object>;
                    if (rec == null) continue;
                    object ok;
                    if (!rec.TryGetValue("ok", out ok)) continue;
                    bool started;
                    try { started = Convert.ToBoolean(ok); } catch { started = false; }
                    if (started) _started[kv.Key] = rec;
                }
            }
            catch { }
        }

        // True when the other application keeps windows rolling at all.
        public static bool RollingEnabled() { Load(); return _rolling; }

        // True when the other application already started a window for this
        // profile, in the other application's own words.
        public static bool WindowStarted(string provider, string profileDir, out string detail)
        {
            Load();
            detail = "";
            string key = AccountKey(provider, profileDir);
            if (key.Length == 0) return false;
            Dictionary<string, object> rec;
            if (!_started.TryGetValue(key, out rec)) return false;
            double at = 0;
            object atObj;
            if (rec.TryGetValue("at", out atObj))
            {
                try { at = Convert.ToDouble(atObj) / 1000.0; } catch { at = 0; }
            }
            string when = at > 0 ? FiveHourKeeper.EstText(at) : "";
            string why = "";
            object d;
            if (rec.TryGetValue("detail", out d)) { try { why = Convert.ToString(d); } catch { why = ""; } }
            detail = AppName + " already started this window"
                   + (when.Length > 0 ? " at " + when : "")
                   + (why.Length > 0 ? " (" + why + ")" : "");
            return true;
        }
    }

    class SaitulsForm : Form
    {
        SaitulsSettings S;
        NotifyIcon Tray;
        string CurrentTab = "Home";
        List<Rectangle> TabRects = new List<Rectangle>();
        string[] TabNames = { "Home", "Explorer menus", "Accounts", "Tools", "Organizer", "Settings" };
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
        bool _limitReadingSettingsExpanded = false;
        const int ActivateDebounceMs = 400;

        // Registry feature list
        string[] RegFeatures = {
            "COPY_PATH", "DEL_DUP", "DEL_EMPTY", "DEL_JUNK", "DEL_SAME", "DL_YT",
            "FFMPEG_MENU", "MERGE_AUD", "MKV_FIX", "NEW_PROJ", "PACK",
            "PS_ADMIN", "TAKE_OWN", "TOGGLE_HID"
        };
        string[] RegLabels = {
            "menu.copy_path", "menu.del_dup", "menu.del_empty", "menu.del_junk", "menu.del_same", "menu.dl_yt",
            "menu.ffmpeg", "menu.merge_aud", "menu.repair_mkv", "menu.new_proj", "menu.pack",
            "MenuPsAdmin", "menu.take_own", "menu.toggle_hid"
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
            // PERF-006: the failure reason and the summary tail are captured as
            // the output arrives, so reporting a job never re-reads the whole
            // log into memory.
            public string Marker = "";      // SAITULS_DL_ERROR=... as soon as it appears
            public string Tail = "";        // last few non-empty lines
        }

        // PERF-006: the two output pumps are the only writers, and they arrive
        // on thread-pool callbacks. One synchronized channel owns the durable
        // log and a bounded tail: the log is complete while the job runs, and
        // the completion dialog reads the tail instead of the whole file.
        sealed class JobOutputStream
        {
            public const int TailLines = 40;      // a summary, never the whole log
            readonly object Sync = new object();
            readonly Queue<string> Tail = new Queue<string>();
            FileStream _file;
            StreamWriter _writer;
            public string Marker = "";
            public string Last = "";

            public JobOutputStream(string path)
            {
                try
                {
                    _file = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read);
                    _writer = new StreamWriter(_file, new System.Text.UTF8Encoding(false));
                }
                catch { _file = null; _writer = null; }
            }

            public void Write(string prefix, string line)
            {
                if (line == null) return;
                string text = prefix + line;
                string trimmed = text.Trim();
                lock (Sync)
                {
                    // Flushed per line: this is the file a user opens to see what
                    // a still-running download is complaining about.
                    if (_writer != null) { try { _writer.WriteLine(text); _writer.Flush(); } catch { } }
                    if (trimmed.Length > 0) Last = trimmed;
                    Tail.Enqueue(text);
                    while (Tail.Count > TailLines) Tail.Dequeue();
                    if (Marker.Length == 0 && trimmed.StartsWith("SAITULS_DL_ERROR=", StringComparison.Ordinal))
                        Marker = trimmed.Substring("SAITULS_DL_ERROR=".Length).Trim();
                }
            }

            public string TailText()
            {
                lock (Sync)
                {
                    var sb = new StringBuilder();
                    foreach (string t in Tail)
                    {
                        string trimmed = t.Trim();
                        if (trimmed.Length > 0) sb.AppendLine(trimmed);
                    }
                    return sb.ToString().Trim();
                }
            }

            public void Dispose()
            {
                lock (Sync)
                {
                    try { if (_writer != null) _writer.Flush(); } catch { }
                    try { if (_writer != null) _writer.Dispose(); } catch { }
                    try { if (_file != null) _file.Dispose(); } catch { }
                    _writer = null;
                    _file = null;
                }
            }
        }

        class MediaProbeResult
        {
            public bool Ready;
            public string Line { get { return AccountsLoc.T(Ready ? "MediaReady" : "MediaRepair"); } }
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
        bool _clipStartRequested, _clipCommandBusy;
        string _clipDeps;                  // null == dependencies ready
        string _clipMsg = "";
        bool _clipRunning, _clipMonitorOn, _clipHotkeyOn, _clipPathOk, _clipFallbackActive, _clipStatusStale;
        string _clipSavePath = "", _clipHotkey = "", _clipEffectivePath = "", _clipHotkeyError = "", _clipSaveError = "", _clipSavedCount = "0";
        DateTime _clipStatusRead = DateTime.MinValue;
        System.Windows.Forms.Timer _clipWait;

        public SaitulsForm(SaitulsSettings s, NotifyIcon tray)
        {
            S = s; Tray = tray;
            // Explorer cascade reconciliation at startup, off the UI thread.
            //
            // The cascade is a projection of consoles.json plus the canonical
            // account registry, written by a separate process, so a menu built
            // before an account existed keeps offering the old set: a new
            // account is missing, a disabled one is still listed. Verify first
            // and repair only on drift, so a matching cascade costs one short
            // child and changes nothing.
            ThreadPool.QueueUserWorkItem(delegate { ReconcileConsolesMenuOnStartup(); });
            Centered.Alignment = StringAlignment.Center;
            Centered.LineAlignment = StringAlignment.Center;
            Text = "SAITULS";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            // Resizable (T-266): the content got real data, and a fixed 600x520
            // could only clip it. The minimum still fits a 1366x768 screen at
            // 150% scaling; the last size the operator chose is remembered and
            // clamped to the screen it opens on.
            MinimumSize = new Size(600, 460);
            ClientSize = new Size(S.WindowW >= 600 ? S.WindowW : 720, S.WindowH >= 460 ? S.WindowH : 580);
            try
            {
                Rectangle wa = Screen.PrimaryScreen.WorkingArea;
                if (Width > wa.Width) Width = Math.Max(MinimumSize.Width, wa.Width);
                if (Height > wa.Height) Height = Math.Max(MinimumSize.Height, wa.Height);
            }
            catch { }
            ResizeRedraw = true;
            ResizeEnd += (o, e) => { S.WindowW = ClientSize.Width; S.WindowH = ClientSize.Height; S.Save(); };
            BackColor = Palette.BG;
            DoubleBuffered = true;
            TopMost = false;
            KeyPreview = true;
            FeatureChecked = new bool[RegFeatures.Length];
            for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = true;
            if (Array.IndexOf(TabNames, S.LastTab) >= 0) CurrentTab = S.LastTab;
            try { Icon = Program.LoadIcon(s.IcoPath, SystemInformation.IconSize.Width); } catch { }
            // Activation repaints and lets the throttled sweep run; it no
            // longer forces a child on every focus change.
            Activated += (o, e) => {
                Invalidate();
                AutoConnectSweep(false);
            };
            // The periodic half of the same promise -- a broker that dies while
            // the account still wants to be connected comes back by itself --
            // now lives in BackgroundTick with the rest of the background work.
            StartBackground();
            _limits = Limits.ReadCache(LimitsDir(), Limits.Now());
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
                // Borderless, so the resize edges are ours to answer: a 6px band
                // on every side, corners first.
                const int grip = 6;
                bool l = p.X < grip, r = p.X >= ClientSize.Width - grip, t = p.Y < grip, b = p.Y >= ClientSize.Height - grip;
                if (t && l) m.Result = (IntPtr)13;          // HTTOPLEFT
                else if (t && r) m.Result = (IntPtr)14;     // HTTOPRIGHT
                else if (b && l) m.Result = (IntPtr)16;     // HTBOTTOMLEFT
                else if (b && r) m.Result = (IntPtr)17;     // HTBOTTOMRIGHT
                else if (l) m.Result = (IntPtr)10;          // HTLEFT
                else if (r) m.Result = (IntPtr)11;          // HTRIGHT
                else if (t) m.Result = (IntPtr)12;          // HTTOP
                else if (b) m.Result = (IntPtr)15;          // HTBOTTOM
                else if (p.X >= 0 && p.Y >= 0 && p.Y < 24 && p.X < Width - 20) m.Result = (IntPtr)HTCAPTION;
            }
        }

        // Shared with SaitulsMenu: the shell's popup surfaces draw from the same
        // pixel font as the form itself.
        internal static Font MakePixelFont(string name, int pt)
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
                StopBackground();
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
            DrawText(g, AccountsLoc.T("AppDescription"), 8 + TextW(g, "SAITULS", 14) + 6, 7, Palette.TEXT2, 10);
            var xr = new Rectangle(Width - 22, 2, 20, 20);
            Buttons.Add(new ButtonDef { R = xr, A = () => { Hide(); } });
            DrawText(g, "X", Width - 18, 5, Palette.TEXT2, 12, true);

            // tabs
            int tabY = 28;
            int tabH = 22;
            int totalGaps = (TabNames.Length - 1) * 2;
            int availW = Math.Max(0, (Width - 8) - totalGaps);

            // Tab labels shrink one step at a time when a language's names do
            // not fit, instead of being cut off mid-word.
            int[] textW = new int[TabNames.Length];
            int textWSum = 0;
            int tabPt = 12;
            for (int attempt = 0; attempt < 3; attempt++)
            {
                textWSum = 0;
                for (int i = 0; i < TabNames.Length; i++)
                {
                    textW[i] = (int)Math.Ceiling(g.MeasureString(TabLabel(TabNames[i]), F(tabPt)).Width) + 6;
                    textWSum += textW[i];
                }
                if (textWSum <= availW || tabPt <= 9) break;
                tabPt = tabPt == 12 ? 10 : 9;
            }

            int[] tabW = new int[TabNames.Length];
            if (availW >= textWSum)
            {
                int extra = availW - textWSum;
                int basePad = extra / TabNames.Length;
                int remExtra = extra % TabNames.Length;
                for (int i = 0; i < TabNames.Length; i++)
                {
                    tabW[i] = textW[i] + basePad + (i < remExtra ? 1 : 0);
                }
            }
            else
            {
                int allocated = 0;
                for (int i = 0; i < TabNames.Length; i++)
                {
                    int w = textWSum > 0 ? (int)((long)textW[i] * availW / textWSum) : (availW / TabNames.Length);
                    if (w < 20) w = 20;
                    tabW[i] = w;
                    allocated += w;
                }
                if (allocated > availW && TabNames.Length > 0)
                {
                    tabW[TabNames.Length - 1] = Math.Max(20, tabW[TabNames.Length - 1] - (allocated - availW));
                }
            }

            int tabX = 4;
            for (int i = 0; i < TabNames.Length; i++)
            {
                int tw = tabW[i];
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
                DrawTextCenter(g, TabLabel(TabNames[i]), r, sel ? Palette.LINK : Palette.TEXT, tabPt, true);
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

        // A tab whose content can outgrow the window draws shifted by its
        // scroll offset; its hit targets move with it and are clipped to the
        // content area, so nothing scrolled away can be clicked.
        void DrawScrolled(Graphics g, Rectangle area, string tab, Action<int> draw)
        {
            int scroll = TabScroll(tab);
            int start = Buttons.Count, startChecks = MenuCheckRects.Count;
            _tabContentH[tab] = 0;
            draw(area.Y + 8 - scroll);
            int bottom = area.Y;
            for (int i = start; i < Buttons.Count; i++) bottom = Math.Max(bottom, Buttons[i].R.Bottom);
            _tabContentH[tab] = Math.Max(_tabContentH[tab], bottom + scroll - area.Y + 36);
            for (int i = startChecks; i < MenuCheckRects.Count; i++)
            {
                Rectangle c = Rectangle.Intersect(MenuCheckRects[i], area);
                MenuCheckRects[i] = c.Width > 0 && c.Height > 0 ? c : Rectangle.Empty;
            }
            ClampButtons(start, area);
        }

        void DrawContent(Graphics g, Rectangle r)
        {
            int x = r.X + 8, y = r.Y + 8, w = r.Width - 16;
            switch (CurrentTab)
            {
                case "Home": DrawHomeTab(g, x, y, w, r.Height - 16); break;
                case "Explorer menus": DrawScrolled(g, r, "Explorer menus", yy => DrawMenusTab(g, x, yy, w, r.Height - 16)); break;
                case "Accounts": DrawAccountsTab(g, x, y, w, r.Height - 16); break;
                case "Tools": DrawScrolled(g, r, "Tools", yy => DrawToolsTab(g, x, yy, w, r.Height - 16)); break;
                case "Organizer": DrawScrolled(g, r, "Organizer", yy => DrawOrganizerTab(g, x, yy, w, r.Height - 16)); break;
                case "Settings": DrawSettingsTab(g, x, y, w, r.Height - 16); break;
            }
        }

        // ==================== background refresh (T-266) ====================
        // Paint paints cached state and nothing else. Every engine or vendor
        // call happens here, on one timer, off the UI thread, single-flight:
        //   * account status: one `status --all` child every 30 s, only while
        //     the window is visible on Home or Accounts;
        //   * AI limits: LIMISAW's published document, else one headless
        //     LIMISAW export, else SAI Accounts' Antigravity snapshot -- at the
        //     canonical 5-minute cadence, only while visible OR while the 5h
        //     keeper has an opted-in account that needs fresh readings;
        //   * auto-connect: every 2 minutes while visible; while hidden only
        //     every 10 minutes and only when the Windows keeper task is off
        //     (with it on, that task already does exactly this).
        // Hidden in the tray with nothing opted in, SAITULS starts no children.
        System.Windows.Forms.Timer _tick;
        volatile bool _closing;
        readonly List<Process> _trackedChildren = new List<Process>();
        const double StatusEveryS = 30, LimitsCadenceS = 300, LimitsFileFreshS = 900;
        const double AutoConnectHiddenS = 600;
        DateTime _statusTickAt = DateTime.MinValue;
        // PERF-007: while every account answers OFFLINE the poll slows down
        // (30s -> 5min). Any account that answers anything else, and any user
        // mutation, puts it straight back to the base cadence.
        int _offlineStreak;
        double _statusEveryS = StatusEveryS;

        // A user action changed something the status poll would report: read it
        // now instead of waiting out the cadence.
        void RequestStatusRefresh()
        {
            _offlineStreak = 0;
            _statusEveryS = StatusEveryS;
            _statusTickAt = DateTime.MinValue;
        }
        // Counts of children this window started, for the idle-cost evidence.
        internal int ChildLaunches;

        void StartBackground()
        {
            _tick = new System.Windows.Forms.Timer();
            _tick.Interval = 5000;
            _tick.Tick += (o, e) => BackgroundTick();
            _tick.Start();
            RefreshFacts(true);
            VisibleChanged += (o, e) =>
            {
                if (!Visible) return;
                // Coming back from the tray: show what is true now, once.
                _statusTickAt = DateTime.MinValue;
                _factsAt = DateTime.MinValue;
                BackgroundTick();
            };
        }

        void StopBackground()
        {
            _closing = true;
            _clipStartRequested = false;
            if (_clipWait != null) { _clipWait.Stop(); _clipWait.Dispose(); _clipWait = null; }
            if (_tick != null) { _tick.Stop(); _tick.Dispose(); _tick = null; }
            // Closing cancels this window's OWN bounded probe children (a
            // limits export, a status read, a 5h ping) -- never anything it
            // did not start.
            lock (_trackedChildren)
            {
                foreach (Process child in _trackedChildren)
                    try { if (!child.HasExited) child.Kill(); } catch { }
                _trackedChildren.Clear();
            }
        }

        // Is the window something a person is looking at right now?
        bool OnScreen { get { return Visible && WindowState != FormWindowState.Minimized; } }

        // PERF-007: a paint consumes this snapshot and nothing else. The 5s
        // background timer -- and every action that changes one of the answers
        // -- rebuilds it, so no paint touches the disk, the Run key or the
        // clipboard subsystem.
        sealed class ToolFacts
        {
            public bool Python;
            public string AccountsExe;
            public string LimisawExe;
            public bool AutostartApp;
            public bool AutostartTaskbar;
            public bool TaskbarInstalled;
            public bool TaskbarRunning;
            public string SecureLine;
        }
        ToolFacts _facts;
        DateTime _factsAt = DateTime.MinValue;
        const double FactsEveryS = 5.0;

        void RefreshFacts(bool force)
        {
            if (!force && _facts != null && (DateTime.Now - _factsAt).TotalSeconds < FactsEveryS) return;
            var f = new ToolFacts();
            f.Python = PythonPresent();
            f.AccountsExe = AccountsExe();
            f.LimisawExe = LimisawExe();
            f.AutostartApp = AutoStartEnabled("SaitulsApp");
            f.AutostartTaskbar = AutoStartEnabled(TaskbarEdgeHelper.RunKeyName);
            f.TaskbarInstalled = TaskbarEdgeHelper.Installed(S.RootPath);
            f.TaskbarRunning = TaskbarEdgeHelper.Running();
            f.SecureLine = SecureAppsStatusLine();
            _facts = f;
            _factsAt = DateTime.Now;
        }

        // An action that changes an answer rebuilds the snapshot on the action
        // path; the next paint still only reads it.
        void InvalidateFacts() { RefreshFacts(true); }

        ToolFacts Facts()
        {
            if (_facts == null) RefreshFacts(true);
            return _facts;
        }

        void BackgroundTick()
        {
            if (_closing || IsDisposed) return;
            bool onScreen = OnScreen;
            if (onScreen) RefreshFacts(false);
            if (onScreen && (CurrentTab == "Home" || CurrentTab == "Tools")) EnsureMediaProbe();
            if (onScreen && CurrentTab == "Tools") { EnsureClipboardDepsProbe(); RefreshClipboardStatus(); }
            if (onScreen && (CurrentTab == "Home" || CurrentTab == "Accounts" || CurrentTab == "Settings") && _accounts == null && !_accountsLoading)
                LoadAccountsInBackground();
            if (onScreen && (CurrentTab == "Home" || CurrentTab == "Accounts"))
            {
                if (_accounts != null && (DateTime.Now - _statusTickAt).TotalSeconds >= _statusEveryS)
                {
                    _statusTickAt = DateTime.Now;
                    PollStatusTick();
                }
            }
            if (onScreen && (CurrentTab == "Accounts" || CurrentTab == "Settings")) KeeperRefresh(false);
            if (onScreen || KeepFiveHourArmed()) RefreshLimits(false);
            if (onScreen) AutoConnectSweep(false);
            else if (!S.AccountKeeper && (DateTime.Now - _autoConnectSweepAt).TotalSeconds >= AutoConnectHiddenS)
                AutoConnectSweep(false);
        }

        bool _accountsLoading;
        void LoadAccountsInBackground()
        {
            if (_accountsLoading || AccountsExe() == null) return;
            _accountsLoading = true;
            ThreadPool.QueueUserWorkItem(delegate {
                try { LoadAccountsAsync(true); }
                finally { TryBeginInvoke(delegate { _accountsLoading = false; if (OnScreen) Invalidate(); }); }
            });
        }

        // A bounded child that the window can cancel: closing SAITULS kills
        // whatever it still has running instead of leaving it behind.
        CliRun RunTracked(string exe, string args, int timeoutMs, Action<ProcessStartInfo> configure)
        {
            if (string.IsNullOrEmpty(exe)) { var missing = new CliRun(); missing.Stderr = "not found"; return missing; }
            var psi = new ProcessStartInfo(exe, args)
            {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
            };
            if (configure != null) configure(psi);
            return RunTracked(psi, timeoutMs);
        }

        // The same bounded child, from a fully built start info: the keeper
        // needs the profile directory in the environment and a shell shim on
        // the command line, and both live in the start info.
        CliRun RunTracked(ProcessStartInfo psi, int timeoutMs)
        {
            var res = new CliRun();
            if (psi == null) { res.Stderr = "not found"; return res; }
            if (_closing) { res.Stderr = "closing"; return res; }
            Process p = null;
            try
            {
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                p = Process.Start(psi);
                if (p == null) { res.Stderr = "did not start"; return res; }
                Interlocked.Increment(ref ChildLaunches);
                lock (_trackedChildren)
                {
                    if (_closing)
                    {
                        try { p.Kill(); } catch { }
                        try { p.Dispose(); } catch { }
                        res.Stderr = "closing";
                        return res;
                    }
                    _trackedChildren.Add(p);
                }
                if (psi.RedirectStandardInput) { try { p.StandardInput.Close(); } catch { } }
                var outTask = p.StandardOutput.ReadToEndAsync();
                var errTask = p.StandardError.ReadToEndAsync();
                if (!p.WaitForExit(timeoutMs))
                {
                    res.TimedOut = true;
                    try { p.Kill(); } catch { }
                    try { p.WaitForExit(2000); } catch { }
                }
                else res.ExitCode = p.ExitCode;
                try { if (outTask.Wait(2000)) res.Stdout = outTask.Result ?? ""; } catch { }
                try { if (errTask.Wait(2000)) res.Stderr = errTask.Result ?? ""; } catch { }
                return res;
            }
            catch (Exception ex) { res.ExitCode = -1; res.Stderr = ex.Message; return res; }
            finally
            {
                if (p != null)
                {
                    lock (_trackedChildren) _trackedChildren.Remove(p);
                    p.Dispose();
                }
            }
        }

        CliRun AccountsExecTracked(string args, int timeoutMs)
        {
            return RunTracked(AccountsExe(), args, timeoutMs, null);
        }

        string AccountsCliTracked(string args, int timeoutMs)
        {
            CliRun r = AccountsExecTracked(args, timeoutMs);
            return r.Ok ? r.Stdout : null;
        }

        // ---------------------------- account status ----------------------------
        // `status --all` answers every account in one child. An engine older
        // than that verb answers with an error, and the tick falls back to the
        // previous one-row probe in the SAME tick, so nothing goes unrefreshed.
        bool? _statusAllSupported = null;
        DateTime _statusAllCheckedAt = DateTime.MinValue;

        void PollStatusTick()
        {
            if (_accounts == null || _accountsBusy || _accountsPolling) return;
            if ((DateTime.Now - _accountsPollAt).TotalSeconds < 6.0) return;
            _accountsPollAt = DateTime.Now;
            _accountsPolling = true;
            AccountRow single = PickRowForSingleProbe();
            string singleId = single != null ? single.Id : null, singleCompact = single != null ? single.Compact : null;
            bool tryAll = _statusAllSupported != false || (DateTime.Now - _statusAllCheckedAt).TotalMinutes > 10;
            ThreadPool.QueueUserWorkItem(delegate {
                List<Dictionary<string, object>> all = null;
                if (tryAll)
                {
                    CliRun r = AccountsExecTracked("status --all", 20000);
                    var env = ParseEnv(r.Stdout);
                    if (env != null && AsText(env, "schema") == "sai.accounts/status-all/1")
                    {
                        all = new List<Dictionary<string, object>>();
                        object[] arr = env.ContainsKey("accounts") ? env["accounts"] as object[] : null;
                        if (arr != null) foreach (object o in arr) { var d = o as Dictionary<string, object>; if (d != null) all.Add(d); }
                    }
                    _statusAllCheckedAt = DateTime.Now;
                    _statusAllSupported = all != null;
                }
                if (all != null)
                {
                    TryBeginInvoke(delegate {
                        _accountsPolling = false;
                        if (_accounts == null) return;
                        bool membershipChanged = all.Count != _accounts.Count;
                        foreach (var o in all)
                        {
                            string id = AsText(o, "account_id");
                            AccountRow cur = _accounts.Find(x => x.Id == id);
                            if (cur == null) { membershipChanged = true; continue; }
                            cur.ContextState = AsText(o, "context_state");
                            cur.AuthState = AsText(o, "auth_state");
                            ApplyRuntimeIdentity(cur, o);
                            if (cur.State != AsText(o, "operational_state") || cur.Hidden != object.Equals(o.ContainsKey("hidden") ? o["hidden"] : null, true)
                                || cur.Compact != AsText(o, "compact_label") || cur.Name != AsText(o, "display_name"))
                                membershipChanged = true;
                        }
                        MarkSharedIdentities(_accounts);
                        NotePollOutcome();
                        if (membershipChanged) LoadAccountsInBackground();
                        if (OnScreen) Invalidate();
                    });
                    return;
                }
                // Fallback: one row per tick, the selected one first, then the
                // first row whose identity has never been read.
                if (singleId == null) { TryBeginInvoke(delegate { _accountsPolling = false; }); return; }
                CliRun one = AccountsExecTracked("status \"" + singleCompact + "\"", 20000);
                var oe = ParseEnv(one.Stdout);
                TryBeginInvoke(delegate {
                    _accountsPolling = false;
                    AccountRow cur = _accounts != null ? _accounts.Find(x => x.Id == singleId) : null;
                    if (cur == null || oe == null) return;
                    cur.ContextState = AsText(oe, "context_state");
                    cur.AuthState = AsText(oe, "auth_state");
                    cur.QuotaState = AsText(oe, "quota_state");
                    ApplyRuntimeIdentity(cur, oe);
                    MarkSharedIdentities(_accounts);
                    NotePollOutcome();
                    if (OnScreen) Invalidate();
                });
            });
        }

        // Everything OFFLINE (or disabled) is not news worth re-reading on the
        // base cadence; anything else puts the poll straight back to it.
        void NotePollOutcome()
        {
            bool allOffline = _accounts != null && _accounts.Count > 0;
            if (allOffline)
                foreach (AccountRow r in _accounts)
                {
                    string hs = HumanState(r);
                    if (hs != "OFFLINE" && hs != "DISABLED") { allOffline = false; break; }
                }
            _offlineStreak = allOffline ? _offlineStreak + 1 : 0;
            _statusEveryS = StatusEveryS + Math.Min(_offlineStreak * 30.0, 270.0);
        }

        AccountRow PickRowForSingleProbe()
        {
            AccountRow row = SelectedAccount();
            if (row == null && _accounts != null && _accounts.Count > 0) row = _accounts[0];
            if (row != null && row.IdentitySeen && _accounts != null)
                foreach (AccountRow other in _accounts)
                    if (!other.IdentitySeen) { row = other; break; }
            return row;
        }

        // ------------------------------ AI limits ------------------------------
        LimitsSnapshot _limits;
        bool _limitsBusy;
        double _limitsAt;               // when the last refresh STARTED (epoch s)
        string _limitsProblem = "";     // why the last refresh produced nothing
        string _limitsCurrentMetric = "";
        string _limisawCached; DateTime _limisawCachedAt = DateTime.MinValue;

        static string LimitsDir()
        {
            string scoped = Environment.GetEnvironmentVariable("SAITULS_LIMITS_DIR");
            if (!string.IsNullOrWhiteSpace(scoped))
                try { return Path.GetFullPath(scoped); } catch { }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SAI", "Limits");
        }

        // Where LIMISAW is. An explicit operator choice first, then the places a
        // LIMISAW install itself announces: the override variable, its own
        // Windows-startup entry, and the conventional folders. (A running copy
        // publishes its numbers to the shared document anyway, so its process
        // is never looked up by name.)
        // Never a developer path compiled into this file.
        internal string LimisawExe()
        {
            if (_limisawCached != null && (DateTime.Now - _limisawCachedAt).TotalSeconds < 60)
                return _limisawCached.Length > 0 ? _limisawCached : null;
            var candidates = new List<string>();
            if (!string.IsNullOrEmpty(S.LimisawPath)) candidates.Add(S.LimisawPath);
            string env = Environment.GetEnvironmentVariable("SAI_LIMISAW_EXE");
            if (!string.IsNullOrEmpty(env)) candidates.Add(env);
            try
            {
                using (RegistryKey rk = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
                {
                    string cmd = rk != null ? rk.GetValue("LimisawApp") as string : null;
                    if (!string.IsNullOrEmpty(cmd))
                    {
                        cmd = cmd.Trim();
                        if (cmd.StartsWith("\"")) { int q = cmd.IndexOf('"', 1); if (q > 1) candidates.Add(cmd.Substring(1, q - 1)); }
                        else candidates.Add(cmd.Split(' ')[0]);
                    }
                }
            }
            catch { }
            candidates.Add(Path.Combine(S.RootPath, "Bin", "LIMISAW.exe"));
            candidates.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "LIMISAW", "LIMISAW.exe"));
            string found = "";
            foreach (string c in candidates)
                if (!string.IsNullOrEmpty(c) && File.Exists(c)) { found = c; break; }
            _limisawCached = found; _limisawCachedAt = DateTime.Now;
            if (found.Length > 0 && !S.LimitsSeeded)
                S.SeedLimitsFrom(Path.Combine(Path.GetDirectoryName(found), "LIMISAW.ini"));
            return found.Length > 0 ? found : null;
        }

        void RefreshLimits(bool force)
        {
            if (_limitsBusy || _closing) return;
            double now = Limits.Now();
            if (!force && _limitsAt > 0 && now - _limitsAt < LimitsCadenceS) return;
            _limitsBusy = true;
            _limitsAt = now;
            string limisaw = LimisawExe();
            string engine = AccountsExe();
            ThreadPool.QueueUserWorkItem(delegate {
                string problem = "";
                LimitsSnapshot snap = null;
                try { snap = FetchLimits(limisaw, engine, out problem); }
                catch (Exception ex) { problem = ex.GetType().Name; }
                TryBeginInvoke(delegate {
                    _limitsBusy = false;
                    if (snap != null)
                    {
                        Limits.Carry(snap, _limits, Limits.Now());
                        Limits.MapToSai(snap, SaiRefs());
                        _limits = snap;
                        _limitsProblem = problem;
                        KeepFiveHourTick();
                    }
                    else _limitsProblem = problem.Length > 0 ? problem : "no limits source answered";
                    if (OnScreen) Invalidate();
                });
            });
        }

        LimitsSnapshot FetchLimits(string limisaw, string engine, out string problem)
        {
            problem = "";
            double now = Limits.Now();
            // 1. The running LIMISAW tray's own document: free.
            try
            {
                string published = Path.Combine(LimitsDir(), "snapshot.json");
                if (File.Exists(published))
                {
                    LimitsSnapshot s = Limits.ParseLimisaw(File.ReadAllText(published, Encoding.UTF8), "LIMISAW");
                    if (Limits.IsFresh(s, now, LimitsFileFreshS)) return s;
                }
            }
            catch { }
            // 2. One headless LIMISAW sweep.
            if (limisaw != null)
            {
                string outFile = Path.Combine(LimitsDir(), "saituls-export.json");
                try { Directory.CreateDirectory(LimitsDir()); } catch { }
                CliRun r = RunTracked(limisaw, "--export-limits --out \"" + outFile + "\"", 150000, null);
                if (r.Ok && File.Exists(outFile))
                {
                    LimitsSnapshot s = Limits.ParseLimisaw(File.ReadAllText(outFile, Encoding.UTF8), "LIMISAW");
                    if (s != null) return s;
                    problem = "LIMISAW_EXPORT_UNSUPPORTED";
                }
                else problem = r.TimedOut ? "LIMISAW_TIMEOUT" : "LIMISAW_EXPORT_FAILED";
            }
            else problem = "LIMISAW_NOT_FOUND";
            // 3. SAI Accounts carries Antigravity's real windows itself.
            if (engine != null)
            {
                CliRun r = RunTracked(engine, "snapshot --provider antigravity --timeout 20000", 90000, null);
                if (r.Ok)
                {
                    LimitsSnapshot s = Limits.ParseSaiSnapshot(LastEnvelopeLine(r.Stdout), Limits.Now());
                    if (s != null) return s;
                }
            }
            return null;
        }

        List<SaiAccountRef> SaiRefs()
        {
            var refs = new List<SaiAccountRef>();
            if (_accounts == null) return refs;
            foreach (AccountRow r in _accounts)
                refs.Add(new SaiAccountRef { Id = r.Id, Compact = r.Compact, Provider = r.Provider,
                    ProfileLocator = r.ProfileLocator, LegacyId = r.LegacyId, WindowsUser = r.Context });
            return refs;
        }

        // The readings that belong to one SAI account (normally exactly one).
        LimitAccount LimitsFor(AccountRow row)
        {
            if (row == null || _limits == null) return null;
            if (_accounts != null)
                Limits.MapToSai(_limits, SaiRefs());
            return _limits.Accounts.Find(a => a.SaiAccountId == row.Id);
        }

        bool LimitsStale()
        {
            return !Limits.IsFresh(_limits, Limits.Now(), 2 * LimitsCadenceS + 120);
        }

        // What the Home/Accounts limits views draw: the SAI plane's global hide
        // and out-of-service state withdraw an account first (a globally hidden
        // or disabled account is not a usable quota source), then LIMISAW's
        // local filters decide what is shown. Order follows SAI Accounts.
        List<LimitAccount> LimitsInService()
        {
            var shown = new List<LimitAccount>();
            if (_limits == null) return shown;
            // Accounts may have loaded after the limits did: tie any reading
            // still unmapped to its account before deciding what is withdrawn.
            if (_accounts != null)
                Limits.MapToSai(_limits, SaiRefs());
            var ordered = new List<LimitAccount>();
            if (_accounts != null)
                foreach (AccountRow r in _accounts)
                {
                    LimitAccount la = _limits.Accounts.Find(a => a.SaiAccountId == r.Id);
                    if (la == null) continue;
                    if (r.Hidden || (r.State.Length > 0 && r.State != "ENABLED")) continue;
                    ordered.Add(la);
                }
            foreach (LimitAccount la in _limits.Accounts)
                if (la.SaiAccountId.Length == 0) ordered.Add(la);
            return ordered;
        }

        List<LimitAccount> LimitsShown()
        {
            return Limits.Visible(LimitsInService(), SaitulsSettings.SplitList(S.LimitsHidden), S.LimitsHideSpent, S.LimitsOnlyFiveHour);
        }

        // ------------------------------ 5h keeper ------------------------------
        Dictionary<string, KeeperAccountState> _keepStates;
        string _keepLast = "";
        bool _keepBusy;

        Dictionary<string, KeeperAccountState> KeepStates()
        {
            if (_keepStates != null) return _keepStates;
            _keepStates = new Dictionary<string, KeeperAccountState>();
            foreach (string item in (S.KeepFiveHourState ?? "").Split(';'))
            {
                string[] kv = item.Split('=');
                if (kv.Length != 2) continue;
                string[] f = kv[1].Split(',');
                if (f.Length < 4) continue;
                var st = new KeeperAccountState();
                double d;
                var ci = System.Globalization.CultureInfo.InvariantCulture;
                if (double.TryParse(f[0], System.Globalization.NumberStyles.Float, ci, out d)) st.LastResetEpoch = d;
                if (double.TryParse(f[1], System.Globalization.NumberStyles.Float, ci, out d)) st.LastPingedReset = d;
                if (double.TryParse(f[2], System.Globalization.NumberStyles.Float, ci, out d)) st.LastPingAt = d;
                if (double.TryParse(f[3], System.Globalization.NumberStyles.Float, ci, out d)) st.LastFailureAt = d;
                // The last answer is durable, so "why is my quota not being
                // kept" is answerable after a restart, not only in the session
                // that saw it. Percent-escaped: the text is human and may
                // contain the record separators.
                if (f.Length >= 5) st.LastResult = Unescape(f[4]);
                // ---- T-303: the admission record of the one bounded request ----
                if (f.Length >= 6) st.State = Unescape(f[5]);
                if (st.State.Length == 0) st.State = KeeperStates.Unknown;
                if (f.Length >= 7) st.AttemptId = Unescape(f[6]);
                if (f.Length >= 8 && double.TryParse(f[7], System.Globalization.NumberStyles.Float, ci, out d)) st.AttemptAt = d;
                if (f.Length >= 9) st.ProfileId = Unescape(f[8]);
                if (f.Length >= 10) st.WindowId = Unescape(f[9]);
                if (f.Length >= 11 && double.TryParse(f[10], System.Globalization.NumberStyles.Float, ci, out d)) st.LastAcceptedAt = d;
                if (f.Length >= 12 && double.TryParse(f[11], System.Globalization.NumberStyles.Float, ci, out d)) st.LastVerifiedReset = d;
                if (f.Length >= 13 && double.TryParse(f[12], System.Globalization.NumberStyles.Float, ci, out d)) st.LastEstimatedReset = d;
                if (f.Length >= 14 && double.TryParse(f[13], System.Globalization.NumberStyles.Float, ci, out d)) st.CooldownUntil = d;
                if (f.Length >= 15) st.OutcomeConfidence = Unescape(f[14]);
                if (st.OutcomeConfidence.Length == 0) st.OutcomeConfidence = KeeperOutcomes.None;
                int n;
                if (f.Length >= 16 && int.TryParse(f[15], out n)) st.Attempts = n;
                if (f.Length >= 17 && f[16] == "1") st.PrimedOnce = true;
                if (f.Length >= 18) st.LastDecision = Unescape(f[17]);
                _keepStates[kv[0]] = st;
            }
            return _keepStates;
        }

        static string Escape(string s) { try { return Uri.EscapeDataString(s ?? ""); } catch { return ""; } }

        static string Unescape(string s)
        {
            try { return Uri.UnescapeDataString(s ?? ""); } catch { return s ?? ""; }
        }

        // The whole keeper record, as one durable value.
        string KeepStateText()
        {
            var ci = System.Globalization.CultureInfo.InvariantCulture;
            var parts = new List<string>();
            foreach (var kv in KeepStates())
            {
                KeeperAccountState v = kv.Value;
                parts.Add(kv.Key + "=" + v.LastResetEpoch.ToString("0", ci) + "," + v.LastPingedReset.ToString("0", ci)
                          + "," + v.LastPingAt.ToString("0", ci) + "," + v.LastFailureAt.ToString("0", ci)
                          + "," + Escape(v.LastResult)
                          + "," + Escape(v.State) + "," + Escape(v.AttemptId) + "," + v.AttemptAt.ToString("0", ci)
                          + "," + Escape(v.ProfileId) + "," + Escape(v.WindowId)
                          + "," + v.LastAcceptedAt.ToString("0", ci) + "," + v.LastVerifiedReset.ToString("0", ci)
                          + "," + v.LastEstimatedReset.ToString("0", ci) + "," + v.CooldownUntil.ToString("0", ci)
                          + "," + Escape(v.OutcomeConfidence) + "," + v.Attempts.ToString(ci) + "," + (v.PrimedOnce ? "1" : "0")
                          + "," + Escape(v.LastDecision));
            }
            return string.Join(";", parts.ToArray());
        }

        void SaveKeepStates()
        {
            S.KeepFiveHourState = KeepStateText();
            S.Save();
        }

        // The admission record of a paid request must be ON DISK before the
        // request starts. A write that cannot be read back is a failed write:
        // the request does not start.
        bool SaveKeepStatesChecked()
        {
            string text = KeepStateText();
            S.KeepFiveHourState = text;
            return S.WriteChecked("KeepFiveHourState", text);
        }

        // The keeper's last answer, read from the persisted state: the same
        // sentence the tick wrote when it decided or pinged, still readable in a
        // later session. Session text wins when this session has one.
        string KeepLastDurable()
        {
            if (S == null) return "";
            foreach (string id in SaitulsSettings.SplitList(S.KeepFiveHour))
            {
                KeeperAccountState st;
                if (!KeepStates().TryGetValue(id, out st)) continue;
                string text = string.IsNullOrEmpty(st.LastResult) ? st.LastDecision : st.LastResult;
                if (string.IsNullOrEmpty(text)) continue;
                AccountRow row = _accounts != null ? _accounts.Find(r => r.Id == id) : null;
                return (row != null ? row.Compact : id) + ": " + text;
            }
            return "";
        }

        bool KeepFiveHourArmed() { return SaitulsSettings.SplitList(S.KeepFiveHour).Count > 0; }

        bool KeepFiveHourOn(string accountId) { return SaitulsSettings.SplitList(S.KeepFiveHour).Contains(accountId); }

        // A6: what the keeper is doing for one account, in one line: is it on,
        // which state it is in, what it decided, when the last request went
        // out, what the CLI accepted, when the next attempt is allowed, and
        // what blocks it right now. A missing reset is shown as unknown, and
        // a predicted one is always shown as ESTIMATED.
        string KeeperDiagnostics(string accountId)
        {
            if (S == null || string.IsNullOrEmpty(accountId)) return "";
            bool on = KeepFiveHourOn(accountId);
            KeeperAccountState st;
            if (!KeepStates().TryGetValue(accountId, out st)) st = new KeeperAccountState();
            var parts = new List<string>();
            parts.Add(on ? "keeper ON" : "keeper OFF");
            parts.Add(string.IsNullOrEmpty(st.State) ? KeeperStates.Unknown : st.State);
            if (!string.IsNullOrEmpty(st.LastDecision)) parts.Add(st.LastDecision);
            if (!string.IsNullOrEmpty(st.LastResult)) parts.Add(st.LastResult);
            if (st.LastPingAt > 0) parts.Add("last request " + FiveHourKeeper.EstText(st.LastPingAt));
            if (st.LastAcceptedAt > 0) parts.Add("accepted " + FiveHourKeeper.EstText(st.LastAcceptedAt));
            if (st.LastVerifiedReset > 0) parts.Add("reset " + FiveHourKeeper.EstText(st.LastVerifiedReset) + " (vendor confirmed)");
            else if (st.LastEstimatedReset > 0) parts.Add("reset " + FiveHourKeeper.EstText(st.LastEstimatedReset) + " (ESTIMATED)");
            double next = st.CooldownUntil > 0 ? st.CooldownUntil : st.LastFailureAt + FiveHourKeeper.FailureCooldownS;
            if (next > Limits.Now()) parts.Add("next attempt after " + FiveHourKeeper.EstText(next));
            if (KeeperCrossApp.RollingEnabled()) parts.Add("another keeper (" + KeeperCrossApp.AppName + ") is enabled for this provider");
            return string.Join("; ", parts.ToArray());
        }

        internal void ToggleKeepFiveHour(string accountId)
        {
            var list = SaitulsSettings.SplitList(S.KeepFiveHour);
            if (list.Contains(accountId)) list.Remove(accountId); else list.Add(accountId);
            S.KeepFiveHour = SaitulsSettings.JoinList(list);
            S.Save();
            if (list.Contains(accountId)) RefreshLimits(true);
            Invalidate();
        }

        // Local limit-list hide: this app's display filter only. It never reaches
        // the account plane, so hiding a row here cannot change an account.
        internal void ToggleLimitHidden(string limitKey)
        {
            if (string.IsNullOrEmpty(limitKey)) return;
            var list = SaitulsSettings.SplitList(S.LimitsHidden);
            if (list.Contains(limitKey)) list.Remove(limitKey); else list.Add(limitKey);
            S.LimitsHidden = SaitulsSettings.JoinList(list);
            S.Save();
            Invalidate();
        }

        internal void PinLimitReading(string metricId)
        {
            if (string.IsNullOrEmpty(metricId)) return;
            S.LimitsMetric = metricId;
            S.Save();
            Invalidate();
        }

        internal void ToggleLimitReadingHidden(string metricId)
        {
            if (string.IsNullOrEmpty(metricId)) return;
            var list = SaitulsSettings.SplitList(S.LimitsReadingHidden);
            if (list.Contains(metricId)) list.Remove(metricId); else list.Add(metricId);
            S.LimitsReadingHidden = SaitulsSettings.JoinList(list);
            S.Save();
            Invalidate();
        }

        internal void MoveLimitReading(string metricId, int direction)
        {
            if (_limits == null || string.IsNullOrEmpty(metricId) || direction == 0) return;
            var order = new List<string>();
            foreach (LimitReading r in Limits.Readings(_limits.Accounts, null, SaitulsSettings.SplitList(S.LimitsReadingOrder)))
                if (r.Window.MetricId.Length > 0 && !order.Contains(r.Window.MetricId)) order.Add(r.Window.MetricId);
            int from = order.IndexOf(metricId), to = from + Math.Sign(direction);
            if (from < 0 || to < 0 || to >= order.Count) return;
            string swap = order[to]; order[to] = order[from]; order[from] = swap;
            // Keep unavailable saved entries for when their accounts return.
            foreach (string id in SaitulsSettings.SplitList(S.LimitsReadingOrder)) if (!order.Contains(id)) order.Add(id);
            S.LimitsReadingOrder = SaitulsSettings.JoinList(order);
            S.Save();
            Invalidate();
        }

        static string CliOnPath(string provider)
        {
            string[] names = provider == "claude"
                ? new[] { "claude.exe", "claude.cmd" }
                : new[] { "codex.exe", "codex.cmd" };
            foreach (string n in names) { string p = Toolchain.FindOnPath(n); if (p != null) return p; }
            return null;
        }

        // Runs after every limits refresh. At most one ping in flight.
        void KeepFiveHourTick()
        {
            if (_keepBusy || _limits == null || _accounts == null || _closing || LimitsStale()) return;
            List<string> on = SaitulsSettings.SplitList(S.KeepFiveHour);
            if (on.Count == 0) return;
            double now = Limits.Now();
            var states = KeepStates();
            foreach (string id in on)
            {
                AccountRow row = _accounts.Find(r => r.Id == id);
                if (row == null)
                {
                    // A6: an account that cannot be found is a blocker to
                    // report, never a silent skip.
                    KeeperAccountState gone;
                    if (!states.TryGetValue(id, out gone)) { gone = new KeeperAccountState(); states[id] = gone; }
                    gone.State = KeeperStates.Blocked;
                    gone.LastDecision = "the account is not in the account list";
                    continue;
                }
                if (row.State != "ENABLED") continue;     // a disabled account takes no new work
                KeeperAccountState st;
                if (!states.TryGetValue(id, out st)) { st = new KeeperAccountState(); states[id] = st; }
                LimitAccount la = LimitsFor(row);
                if (la == null)
                {
                    st.State = KeeperStates.Blocked;
                    st.LastDecision = "no quota reading is mapped to this account";
                    continue;
                }
                KeeperDecision dec = FiveHourKeeper.DecideState(la, st, now);
                st.State = dec.State;
                if (!dec.Eligible) { st.LastDecision = dec.Reason; continue; }

                // Where the CLI has to run, and with what. A missing mapping
                // is a blocker to report, never a silent skip.
                string dir = row.ProfileLocator;
                string exe = CliOnPath(la.Provider);
                if (string.IsNullOrEmpty(dir))
                {
                    st.State = KeeperStates.Blocked;
                    st.LastDecision = "no profile directory is mapped to this account";
                    continue;
                }
                if (!Directory.Exists(dir))
                {
                    st.State = KeeperStates.Blocked;
                    st.LastDecision = "the profile directory " + dir + " does not exist";
                    continue;
                }
                if (exe == null)
                {
                    st.State = KeeperStates.Blocked;
                    st.LastDecision = la.Provider + " CLI not found on PATH";
                    continue;
                }
                // A5: one effective owner per provider account.
                string owner = string.IsNullOrEmpty(S.KeepFiveHourOwner) ? "SAITULS" : S.KeepFiveHourOwner.Trim();
                if (owner.Equals("ZAICODE", StringComparison.OrdinalIgnoreCase))
                {
                    st.State = KeeperStates.Blocked;
                    st.LastDecision = "the window keeper of this account is owned by " + KeeperCrossApp.AppName;
                    continue;
                }
                string crossDetail;
                if (KeeperCrossApp.WindowStarted(la.Provider, dir, out crossDetail))
                {
                    st.State = KeeperStates.RunningVerified;
                    st.LastDecision = crossDetail;
                    continue;
                }

                // A3: the admission record is committed BEFORE the request. A
                // request that cannot be persisted must not be sent.
                var ci = System.Globalization.CultureInfo.InvariantCulture;
                st.AttemptId = id + "-" + now.ToString("0", ci) + "-" + Guid.NewGuid().ToString("N").Substring(0, 8);
                st.AttemptAt = now;
                st.ProfileId = dir;
                st.WindowId = KeeperWindowId(la, dec);
                st.Attempts = st.Attempts + 1;
                st.State = KeeperStates.PrimingPending;
                st.LastDecision = dec.Initial
                    ? "priming the 5h window: one bounded request"
                    : "renewing the 5h window: one bounded request";
                if (!SaveKeepStatesChecked())
                {
                    st.State = KeeperStates.Blocked;
                    st.Attempts = st.Attempts - 1;      // no request was made
                    st.LastDecision = "BLOCKED_PERSISTENCE: the admission record could not be saved, so no request was sent";
                    SaveKeepStates();
                    continue;
                }

                _keepBusy = true;
                string compact = row.Compact, provider = la.Provider, model = S.KeepFiveHourModel;
                LimitAccount snapshotAccount = la;
                string profileDir = dir, cliPath = exe;
                ThreadPool.QueueUserWorkItem(delegate {
                    string note = "";
                    CliRun r = KeeperPing(provider, profileDir, cliPath, model, out note);
                    TryBeginInvoke(delegate {
                        _keepBusy = false;
                        string outcome = FiveHourKeeper.ClassifyOutcome(provider, r.Ok, r.Stdout, r.Stderr, r.TimedOut);
                        string result = KeeperResultText(outcome, r, note);
                        FiveHourKeeper.RecordAttempt(st, snapshotAccount, outcome, Limits.Now(), result);
                        _keepLast = compact + ": " + result;
                        SaveKeepStates();
                        // Read the new window soon, not at the next cadence.
                        if (outcome == KeeperOutcomes.AcceptedUnverified || outcome == KeeperOutcomes.Verified)
                            _limitsAt = Limits.Now() - LimitsCadenceS + 60;
                        if (OnScreen) Invalidate();
                    });
                });
                return;
            }
            SaveKeepStates();
        }

        // The identity of the window one attempt is for: the window the
        // vendor names, or the one the keeper predicts.
        static string KeeperWindowId(LimitAccount la, KeeperDecision dec)
        {
            LimitWindow five = FiveHourKeeper.FiveHour(la);
            if (five != null && five.ResetEpoch.HasValue)
                return "five_hour:" + Math.Floor(five.ResetEpoch.Value / 60) * 60;
            if (dec.EstimatedReset > 0) return "five_hour:estimated:" + Math.Floor(dec.EstimatedReset / 60) * 60;
            return "five_hour:initial";
        }

        // The caps of the installed CLI, read once per CLI path from its own
        // --help. It spends no quota: only the help text is asked for.
        readonly Dictionary<string, KeeperCliCaps> _keepCaps = new Dictionary<string, KeeperCliCaps>();

        KeeperCliCaps CapsFor(string exe)
        {
            KeeperCliCaps caps;
            if (_keepCaps.TryGetValue(exe, out caps)) return caps;
            caps = null;
            ProcessStartInfo probe = FiveHourKeeper.CapsProbeCommand(exe);
            if (probe != null)
            {
                CliRun r = RunTracked(probe, 20000);
                if (!string.IsNullOrEmpty(r.Stdout)) caps = KeeperCliCaps.Parse(r.Stdout);
            }
            _keepCaps[exe] = caps;
            return caps;
        }

        // One bounded request, with the ladder: a rung the CLI rejects on its
        // own costs no quota, so the next rung may be tried. A request the
        // vendor did answer is never repeated.
        CliRun KeeperPing(string provider, string profileDir, string exe, string model, out string note)
        {
            note = "";
            KeeperCliCaps caps = CapsFor(exe);
            List<FiveHourKeeper.PingRung> ladder = FiveHourKeeper.Ladder(provider, model);
            CliRun last = new CliRun();
            for (int i = 0; i < ladder.Count; i++)
            {
                ProcessStartInfo psi = FiveHourKeeper.PingCommand(provider, profileDir, exe, caps, ladder[i]);
                if (psi == null) { note = "no request could be built for " + provider; return last; }
                CliRun r = RunTracked(psi, 180000);
                last = r;
                if (!FiveHourKeeper.LocalRejection(r.Stdout, r.Stderr)) return r;   // the CLI answered
                note = "the CLI rejected the request: " + (r.Stderr ?? "").Trim();
            }
            return last;
        }

        // What the answer means, in words the operator can act on. An accepted
        // request is NOT a started window: only the vendor can confirm that.
        static string KeeperResultText(string outcome, CliRun r, string note)
        {
            if (outcome == KeeperOutcomes.AcceptedUnverified)
                return "the request was accepted at " + DateTime.Now.ToString("HH:mm") + " (the window is not confirmed yet)";
            if (outcome == KeeperOutcomes.Verified)
                return "the window is confirmed";
            if (outcome == KeeperOutcomes.FailedOutcome)
                return r.TimedOut ? "ping timed out" : "ping failed (exit " + r.ExitCode + ")";
            return "the outcome of the request is unknown" + (note.Length > 0 ? ": " + note : "");
        }

        // --------------------------- human messages ---------------------------
        // The status strip answers "what happened" and "what can I do" in
        // words. The raw engine text stays in _accountsNote (and in Copy
        // diagnostics); only its presentation changes here.
        internal static string HumanizeNote(string raw)
        {
            if (string.IsNullOrEmpty(raw)) return "";
            string n = raw.Trim();
            Func<string, bool> has = k => n.IndexOf(k, StringComparison.OrdinalIgnoreCase) >= 0;
            if (has("sai-accounts.exe not found") || has("engine missing")) return AccountsLoc.T("ErrEngineMissing");
            if (has("engine did not answer in time") || has("timed out")) return AccountsLoc.T("ErrTimeout");
            if (has("SCHEMA_MISMATCH") || has("REGISTRY_UNAVAILABLE") || has("unreadable response")) return AccountsLoc.T("ErrEngineRepair");
            if (has("EXPLORER MENU UPDATE FAILED") || has("menu sync failed")) return AccountsLoc.T("ErrMenu");
            if (has("CONTEXT_START_FAILED") || has("HELPER_UNAVAILABLE")) return AccountsLoc.T("ErrContextStart");
            if (has("CONTEXT_OFFLINE")) return AccountsLoc.T("ErrOffline");
            if (has("SIGN_IN_FAILED") || has("auth_launch_failed")) return AccountsLoc.T("ErrSignIn");
            if (has("WORKDIR_UNAVAILABLE") || has("WORKDIR_ACCESS_DENIED")) return AccountsLoc.T("ErrWorkdir");
            if (has("STATUS_FAILED") || has("status failed")) return AccountsLoc.T("ErrStatus");
            if (n.StartsWith("FAILED", StringComparison.OrdinalIgnoreCase) || n.StartsWith("ERROR", StringComparison.OrdinalIgnoreCase))
                return AccountsLoc.T("ErrGeneric");
            return n;
        }

        static bool NoteIsFailure(string raw)
        {
            if (string.IsNullOrEmpty(raw)) return false;
            return raw.StartsWith("FAILED") || raw.StartsWith("ERROR") || raw.IndexOf("_FAILED", StringComparison.Ordinal) >= 0
                || raw.StartsWith("CONTEXT_OFFLINE") || raw.StartsWith("WORKDIR_") || raw.StartsWith("STATUS_FAILED")
                || raw.IndexOf("UPDATE FAILED", StringComparison.Ordinal) >= 0;
        }

        // ============================ shared drawing ============================
        static string TabLabel(string tab)
        {
            switch (tab)
            {
                case "Home": return AccountsLoc.T("TabHome");
                case "Explorer menus": return AccountsLoc.T("TabMenus");
                case "Accounts": return AccountsLoc.T("TabAccounts");
                case "Tools": return AccountsLoc.T("TabTools");
                case "Organizer": return AccountsLoc.T("TabOrganizer");
                case "Settings": return AccountsLoc.T("TabSettings");
            }
            return tab;
        }

        // Text that must not run past maxW: trimmed with "~" rather than clipped,
        // so a cut-off label reads as "there is more", not as a rendering bug.
        string Fit(Graphics g, string s, int maxW, int pt)
        {
            if (string.IsNullOrEmpty(s) || maxW <= 0) return "";
            if (TextW(g, s, pt) <= maxW) return s;
            while (s.Length > 1 && TextW(g, s + "~", pt) > maxW) s = s.Substring(0, s.Length - 1);
            return s + "~";
        }

        // One limit window as a meter: the bar reads the remaining percent (its
        // colour too, in both modes, as in LIMISAW), the number follows
        // "Showing: Left/Used", and a window with no reading draws an empty
        // track and "--". Returns the x after the drawn text.
        int DrawLimitMeter(Graphics g, int x, int y, int barW, LimitWindow w, bool dim)
        {
            int h = 8;
            using (var back = new SolidBrush(Palette.BDARK)) g.FillRectangle(back, x, y + 3, barW, h);
            bool available = w != null && w.Available;
            if (available && w.Remaining > 0)
            {
                int fill = Math.Max(1, barW * Math.Min(100, w.Remaining) / 100);
                using (var b = new SolidBrush(dim ? Palette.MUTED : Limits.FillColor(w.Remaining, true)))
                    g.FillRectangle(b, x, y + 3, fill, h);
            }
            using (var p = new Pen(Palette.BEVEL)) g.DrawRectangle(p, x, y + 3, barW - 1, h - 1);
            string txt = Limits.Shown(w, S.LimitsShowUsed);
            DrawText(g, txt, x + barW + 4, y, dim ? Palette.MUTED : Palette.TEXT, 9);
            return x + barW + 4 + TextW(g, txt, 9);
        }

        // The windows an account summary row shows: for each window kind (5h,
        // week, month) the reading with the least left, ACROSS its pools but
        // labelled with the pool it came from -- so nothing is averaged or
        // merged, and the Accounts tab still lists every window on its own.
        static List<LimitWindow> SummaryWindows(LimitAccount a)
        {
            var picked = new List<LimitWindow>();
            foreach (string bas in new[] { "five_hour", "weekly", "monthly" })
            {
                LimitWindow best = null;
                foreach (LimitWindow w in a.Windows)
                {
                    if (w.Base != bas) continue;
                    if (best == null) { best = w; continue; }
                    bool wa = w.Available, ba = best.Available;
                    if (wa && !ba) best = w;
                    else if (wa == ba && w.Remaining < best.Remaining) best = w;
                }
                if (best != null) picked.Add(best);
            }
            if (picked.Count == 0 && a.Windows.Count > 0) picked.Add(a.Windows[0]);
            return picked;
        }

        // Window names in the UI language. LIMISAW's labels ("5h", "week")
        // are presentation, so they follow the one language setting too.
        static string WindowLabel(LimitWindow w)
        {
            switch (w.Base)
            {
                case "five_hour": return AccountsLoc.T("Win5h");
                case "weekly": return AccountsLoc.T("WinWeek");
                case "monthly": return AccountsLoc.T("WinMonth");
            }
            return w.Label.Length > 0 ? w.Label : w.Key;
        }

        static string WindowTitle(LimitWindow w)
        {
            return w.GroupLabel.Length > 0 ? WindowLabel(w) + " (" + w.GroupLabel + ")" : WindowLabel(w);
        }

        // The gating window, named the way its own row is named.
        static string GateLabel(LimitAccount a, LimitWindow w)
        {
            if (a != null)
                foreach (LimitWindow o in a.Windows) if (o.Key == w.GatedBy) return WindowLabel(o);
            string bas = w.GatedBy.Split('@')[0];
            return bas == "weekly" ? AccountsLoc.T("WinWeek") : bas == "monthly" ? AccountsLoc.T("WinMonth") : bas;
        }

        static string Countdown(double? epoch)
        {
            string c = Limits.Countdown(epoch, Limits.Now());
            if (c == "--" || AccountsLoc.CurrentLang == "en") return c;
            if (c.EndsWith("m")) return c.Substring(0, c.Length - 1) + AccountsLoc.T("UnitM");
            if (c.EndsWith("h")) return c.Substring(0, c.Length - 1) + AccountsLoc.T("UnitH");
            if (c.EndsWith("d")) return c.Substring(0, c.Length - 1) + AccountsLoc.T("UnitD");
            return c;
        }

        static string ShortWindowLabel(LimitWindow w)
        {
            string label = WindowLabel(w);
            if (w.GroupLabel.Length > 0)
            {
                // "Gemini Models" -> "G", "Claude and GPT models" -> "C/G": enough
                // to tell the pools apart on one line; the full name is on the
                // Accounts tab.
                string gl = w.GroupLabel.ToLowerInvariant();
                string tag = gl.StartsWith("gemini") ? "G" : gl.StartsWith("claude") ? "C/G" : w.GroupLabel.Substring(0, 1).ToUpperInvariant();
                label += " " + tag;
            }
            return label;
        }

        string LimitStateWord(string state)
        {
            switch (state)
            {
                case "STALE": return AccountsLoc.T("StateStale");
                case "SIGN_IN": return AccountsLoc.T("StateSignIn");
                case "OFFLINE": return AccountsLoc.T("StateOfflineWord");
                case "UNAVAILABLE": return AccountsLoc.T("StateUnavailable");
            }
            return "";
        }

        string LimitsAgeText()
        {
            if (_limits == null) return "";
            double stamp = _limits.CheckedAtEpoch;
            if (stamp <= 0 || double.IsNaN(stamp) || double.IsInfinity(stamp) || stamp > Limits.Now() + 60)
                return AccountsLoc.T("UpdatedUnknown");
            double age = Limits.Now() - _limits.CheckedAtEpoch;
            if (age < 90) return AccountsLoc.T("UpdatedNow");
            string span = age < 3600 ? ((int)(age / 60)) + "m" : age < 86400 ? ((int)(age / 3600)) + "h" : ((int)(age / 86400)) + "d";
            return AccountsLoc.T("UpdatedAgo", span);
        }

        // Clicks only where the content actually is: a scrolled-away button
        // must not answer a click on the tab strip above it. It keeps its
        // entry (and its action) with an empty hit rectangle, so it is neither
        // clickable nor a keyboard focus stop until it is scrolled into view.
        void ClampButtons(int from, Rectangle area)
        {
            for (int i = Buttons.Count - 1; i >= from; i--)
            {
                Rectangle r = Rectangle.Intersect(Buttons[i].R, area);
                Buttons[i].R = r.Width > 0 && r.Height > 0 ? r : Rectangle.Empty;
            }
        }

        readonly Dictionary<string, int> _tabScroll = new Dictionary<string, int>();
        readonly Dictionary<string, int> _tabContentH = new Dictionary<string, int>();

        int TabScroll(string tab) { int v; return _tabScroll.TryGetValue(tab, out v) ? v : 0; }

        void ScrollTab(string tab, int delta, int viewH)
        {
            int contentH; _tabContentH.TryGetValue(tab, out contentH);
            int max = Math.Max(0, contentH - viewH);
            _tabScroll[tab] = Math.Max(0, Math.Min(max, TabScroll(tab) + delta));
        }

        // Toolchain facts for Home, cached: Toolchain.FindOnPath walks PATH and
        // used to run on every paint.
        bool _pythonCached; DateTime _pythonCheckedAt = DateTime.MinValue;
        bool PythonPresent()
        {
            if ((DateTime.Now - _pythonCheckedAt).TotalSeconds > 60)
            {
                _pythonCached = Toolchain.FindOnPath("pythonw.exe") != null || Toolchain.FindOnPath("python.exe") != null;
                _pythonCheckedAt = DateTime.Now;
            }
            return _pythonCached;
        }

        // Explorer cascade state as the installer's own verifier reported it.
        // "" = not checked yet.
        string _menuState = "";

        // ================================ HOME ================================
        // Answers, in a few seconds: is SAITULS healthy, are my AI accounts
        // usable, how much is left and when does it reset, does anything need
        // me, and where do I click. Details stay on their own tabs.
        void DrawHomeTab(Graphics g, int x, int y, int w, int h)
        {
            int top = y;
            y -= TabScroll("Home");
            int startButtons = Buttons.Count;

            // ---- facts -------------------------------------------------------
            ToolFacts facts = Facts();
            bool python = facts.Python;
            MediaProbeResult probe = _lastProbe;
            bool probed = _mediaProbeDone && probe != null;
            bool aria = File.Exists(Path.Combine(S.RootPath, "Bin", "ARIA2C.EXE"));
            int missing = 0;
            if (!python) missing++;
            if (!aria) missing++;
            if (probed)
            {
                if (probe.Ffmpeg.Length > 0) missing++;
                if (probe.YtDlp.Length > 0) missing++;
                if (probe.JsRuntime.Length > 0) missing++;
            }
            bool engine = facts.AccountsExe != null;
            int nReady = 0, nSignIn = 0, nOffline = 0, nFrozen = 0, nDisabled = 0;
            if (_accounts != null)
                foreach (AccountRow r in _accounts)
                {
                    string hs = HumanState(r);
                    if (hs == "READY") nReady++;
                    else if (hs == "SIGN IN") nSignIn++;
                    else if (hs == "OFFLINE") { if (r.AutoConnect || r.ContextState == "OFFLINE") nOffline++; }
                    else if (hs == "FROZEN") nFrozen++;
                    else if (hs == "DISABLED") nDisabled++;
                }
            bool keeperBroken = S.AccountKeeper && (_keeperState.IndexOf("needs repair", StringComparison.OrdinalIgnoreCase) >= 0
                || _keeperState.IndexOf("missing", StringComparison.OrdinalIgnoreCase) >= 0);
            bool menuBad = _menuSyncFailed || _menuState == "failed";
            bool lastFailed = NoteIsFailure(_accountsNote);

            // ---- overall state: one word, one reason, one action -------------
            string word, reason = ""; Color wordColor; Action fix = null; string fixLabel = null;
            if (!python || (probed && !probe.Ready))
            {
                word = AccountsLoc.T("HomeSetup"); wordColor = Palette.DANGERTXT;
                reason = AccountsLoc.T("MissingN", missing.ToString()) + " -- " + AccountsLoc.T("ToolsRow");
                fix = () => StartSetup(); fixLabel = AccountsLoc.T("InstallRepair");
            }
            else if (nSignIn > 0 || menuBad || keeperBroken || lastFailed || (nOffline > 0 && engine))
            {
                word = AccountsLoc.T("HomeAttention"); wordColor = Color.FromArgb(210, 170, 60);
                if (nSignIn > 0) { reason = AccountsLoc.T("NSignIn", nSignIn.ToString()); fix = () => SwitchTab("Accounts"); fixLabel = AccountsLoc.T("OpenAccounts"); }
                else if (menuBad) { reason = AccountsLoc.T("ErrMenu"); fix = () => RepairMenuFromHome(); fixLabel = AccountsLoc.T("Repair"); }
                else if (keeperBroken) { reason = AccountsLoc.T("KeeperRow") + ": " + AccountsLoc.T("NeedsRepair"); fix = () => KeeperSet(S.AccountKeeper); fixLabel = AccountsLoc.T("Repair"); }
                else if (nOffline > 0) { reason = AccountsLoc.T("NOffline", nOffline.ToString()); fix = () => SwitchTab("Accounts"); fixLabel = AccountsLoc.T("OpenAccounts"); }
                else { reason = HumanizeNote(_accountsNote); fix = () => SwitchTab("Accounts"); fixLabel = AccountsLoc.T("OpenAccounts"); }
            }
            else if (!engine)
            {
                word = AccountsLoc.T("HomeAttention"); wordColor = Color.FromArgb(210, 170, 60);
                reason = AccountsLoc.T("ErrEngineMissing");
                fix = () => StartSetup(); fixLabel = AccountsLoc.T("InstallRepair");
            }
            else if (_limits == null && !_limitsBusy && _limitsProblem.Length > 0)
            {
                word = AccountsLoc.T("HomeAttention"); wordColor = Color.FromArgb(210, 170, 60);
                reason = AccountsLoc.T("LimitsNoSource");
                fix = () => LocateLimisaw(); fixLabel = AccountsLoc.T("LocateLimisaw");
            }
            else if (!probed)
            {
                word = AccountsLoc.T("Checking"); wordColor = Palette.TEXT2;
            }
            else
            {
                word = AccountsLoc.T("HomeReady"); wordColor = Palette.LINK;
                reason = AccountsLoc.T("HomeAllGood");
            }
            DrawText(g, word, x, y, wordColor, 14, true);
            int wx = x + TextW(g, word, 14) + 10;
            if (fix != null)
            {
                int bw = Math.Max(96, TextW(g, fixLabel, 9) + 18);
                var fr = new Rectangle(x + w - bw, y, bw, 22);
                Buttons.Add(new ButtonDef { R = fr, A = fix, Label = fixLabel });
                DrawButton(g, fr, fixLabel, true, 9);
                DrawText(g, Fit(g, reason, fr.X - wx - 8, 10), wx, y + 4, Palette.TEXT, 10);
            }
            else DrawText(g, Fit(g, reason, x + w - wx, 10), wx, y + 4, Palette.TEXT2, 10);
            y += 30;

            // ---- AI limits ----------------------------------------------------
            y = DrawLimitsSection(g, x, y, w);

            // ---- accounts ------------------------------------------------------
            y += 6;
            string openLbl = AccountsLoc.T("OpenAccounts");
            int ow = Math.Max(110, TextW(g, openLbl, 9) + 18);
            var orr = new Rectangle(x + w - ow, y - 2, ow, 20);
            Buttons.Add(new ButtonDef { R = orr, A = () => SwitchTab("Accounts"), Label = openLbl });
            DrawButton(g, orr, openLbl, false, 9);
            DrawSectionRule(g, x, y, w - ow - 8, AccountsLoc.T("AccountsHdr"));
            y += 18;
            if (!engine)
            {
                DrawText(g, Fit(g, AccountsLoc.T("ErrEngineMissing"), w, 10), x, y, Palette.TEXT2, 10);
                y += 18;
            }
            else if (_accounts == null)
            {
                DrawText(g, AccountsLoc.T("Loading"), x, y, Palette.TEXT2, 10);
                y += 18;
            }
            else
            {
                int cw = w / 5;
                int[] counts = { cw, cw, cw, cw, w - 4 * cw };
                y = DrawHomeTextRow(g, x, y, counts, new[] {
                    AccountsLoc.T("NReady", "").Trim(), AccountsLoc.T("NSignIn", "").Trim(),
                    AccountsLoc.T("NOffline", "").Trim(), AccountsLoc.T("NFrozen", "").Trim(),
                    AccountsLoc.T("NDisabled", "").Trim() }, true, false);
                y = DrawHomeTextRow(g, x, y, counts, new[] {
                    nReady.ToString(), nSignIn.ToString(), nOffline.ToString(),
                    nFrozen.ToString(), nDisabled.ToString() }, false, false);
            }

            // ---- system ---------------------------------------------------------
            y += 4;
            DrawSectionRule(g, x, y, w, AccountsLoc.T("SystemHdr"));
            y += 18;
            string toolsState = !probed ? AccountsLoc.T("Checking") : missing == 0 ? AccountsLoc.T("WordOk") : AccountsLoc.T("MissingN", missing.ToString());
            string menuText = menuBad ? AccountsLoc.T("NeedsRepair") : _menuState == "ok" ? AccountsLoc.T("WordOk") : AccountsLoc.T("Checking");
            string engineText = engine ? AccountsLoc.T("WordOk") : AccountsLoc.T("NotInstalled");
            string keeperText = !S.AccountKeeper ? AccountsLoc.T("WordOff") : keeperBroken ? AccountsLoc.T("NeedsRepair") : AccountsLoc.T("WordOn");
            int[] systemCols = { w / 2, w - w / 2 };
            y = DrawHomeTextRow(g, x, y, systemCols, new[] {
                AccountsLoc.T("HomeColComponent"), AccountsLoc.T("HomeColState") }, true, false);
            y = DrawHomeTextRow(g, x, y, systemCols, new[] { AccountsLoc.T("AccountService"), engineText }, false, false, engine ? Palette.TEXT : Palette.DANGERTXT);
            y = DrawHomeTextRow(g, x, y, systemCols, new[] { AccountsLoc.T("ExplorerMenusRow"), menuText }, false, true, menuBad ? Palette.DANGERTXT : Palette.TEXT);
            y = DrawHomeTextRow(g, x, y, systemCols, new[] { AccountsLoc.T("ToolsRow"), toolsState }, false, false, missing > 0 ? Palette.DANGERTXT : Palette.TEXT);
            y = DrawHomeTextRow(g, x, y, systemCols, new[] { AccountsLoc.T("KeeperRow"), keeperText }, false, true, keeperBroken ? Palette.DANGERTXT : Palette.TEXT);
            y += 4;
            if (probed && missing > 0)
            {
                string lbl = AccountsLoc.T("InstallRepair");
                var ir = new Rectangle(x, y, Math.Max(130, TextW(g, lbl, 9) + 18), 22);
                Buttons.Add(new ButtonDef { R = ir, A = () => StartSetup(), Label = lbl });
                DrawButton(g, ir, lbl, false, 9);
                y += 28;
            }

            // ---- last issue -----------------------------------------------------
            if (lastFailed || _limitsProblem.Length > 0 && _limits == null)
            {
                y += 2;
                DrawSectionRule(g, x, y, w, AccountsLoc.T("RecentIssue"));
                y += 18;
                string issue = lastFailed ? HumanizeNote(_accountsNote)
                    : (_limitsProblem == "LIMISAW_NOT_FOUND" ? AccountsLoc.T("LimitsNoSource") : _limitsProblem);
                string act = AccountsLoc.T("Retry");
                int aw = Math.Max(80, TextW(g, act, 9) + 18);
                var ar = new Rectangle(x + w - aw, y - 3, aw, 20);
                Buttons.Add(new ButtonDef { R = ar, A = lastFailed ? (Action)(() => { _accountsNote = ""; SwitchTab("Accounts"); }) : () => RefreshLimits(true), Label = act });
                DrawButton(g, ar, act, false, 9);
                DrawText(g, Fit(g, issue, ar.X - x - 8, 10), x, y, Palette.DANGERTXT, 10);
                y += 22;
            }

            _tabContentH["Home"] = (y + TabScroll("Home")) - top + 8;
            ClampButtons(startButtons, new Rectangle(x - 8, top - 8, w + 16, h + 16));
        }

        void DrawSectionRule(Graphics g, int x, int y, int w, string title)
        {
            DrawText(g, title, x, y, Palette.BHL, 9, true);
            int tx = x + TextW(g, title, 9) + 6;
            using (var p = new Pen(Palette.BMUTED)) g.DrawLine(p, tx, y + 7, x + w, y + 7);
        }

        const int HomeTableRowHeight = 22;

        void DrawHomeRowFrame(Graphics g, int x, int y, int[] widths, bool header, bool alternate)
        {
            int width = 0;
            foreach (int cell in widths) width += cell;
            using (var brush = new SolidBrush(header ? Palette.RAISED : alternate ? Palette.BG_SOFT : Palette.BG))
                g.FillRectangle(brush, x, y, width, HomeTableRowHeight);
            using (var pen = new Pen(Palette.BMUTED))
            {
                g.DrawRectangle(pen, x, y, width - 1, HomeTableRowHeight);
                int edge = x;
                for (int i = 0; i < widths.Length - 1; i++)
                { edge += widths[i]; g.DrawLine(pen, edge, y, edge, y + HomeTableRowHeight); }
            }
        }

        int DrawHomeTextRow(Graphics g, int x, int y, int[] widths, string[] values, bool header, bool alternate, Color? valueColor = null)
        {
            DrawHomeRowFrame(g, x, y, widths, header, alternate);
            for (int i = 0; i < widths.Length; i++)
            {
                DrawText(g, Fit(g, values[i], widths[i] - 12, 9), x + 6, y + 4,
                    header ? Palette.BHL : i == widths.Length - 1 && valueColor.HasValue ? valueColor.Value : Palette.TEXT, 9, header);
                x += widths[i];
            }
            return y + HomeTableRowHeight;
        }

        void DrawHomeQuotaCell(Graphics g, Rectangle cell, LimitAccount account, LimitWindow window, bool dim)
        {
            if (window == null) { DrawText(g, "--", cell.X + 6, cell.Y + 4, Palette.MUTED, 9); return; }
            var saved = g.Save();
            try
            {
                g.SetClip(cell, CombineMode.Intersect);
                int meterX = cell.X + 44;
                if (window.GatedBy.Length > 0)
                {
                    LimitWindow gate = account.Windows.Find(z => z.Key == window.GatedBy);
                    string blocked = AccountsLoc.T("HomeLockedBy", gate != null ? HomeWindowLabel(gate) : GateLabel(account, window));
                    string value = HomeWindowLabel(window) + " " + blocked;
                    if (TextW(g, value, 8) > cell.Width - 12 && window.Base == "five_hour") value = blocked;
                    DrawText(g, Fit(g, value, cell.Width - 12, 8), cell.X + 6, cell.Y + 5, Palette.TEXT2, 8);
                }
                else
                {
                    DrawText(g, Fit(g, HomeWindowLabel(window), 34, 8), cell.X + 6, cell.Y + 5, Palette.TEXT2, 8);
                    DrawLimitMeter(g, meterX, cell.Y + 4, Math.Max(12, Math.Min(56, cell.Width - 94)), window, dim);
                }
            }
            finally { g.Restore(saved); }
        }

        static string HomeWindowLabel(LimitWindow window)
        {
            if (window.Base == "weekly") return AccountsLoc.T("HomePeriodWeek");
            if (window.Base == "monthly") return AccountsLoc.T("HomePeriodMonth");
            return WindowLabel(window);
        }

        static string HomePoolLabel(LimitWindow window)
        {
            string label = window.GroupLabel;
            if (label.StartsWith("gemini", StringComparison.OrdinalIgnoreCase)) return "Gemini";
            if (label.StartsWith("claude", StringComparison.OrdinalIgnoreCase)) return "Claude/GPT";
            return label.Length > 0 ? label : "--";
        }

        // "Label: value" with the value in words. Colour only repeats what the
        // word already says.
        void DrawFact(Graphics g, int x, int y, int w, string label, string value, bool ok)
        {
            DrawText(g, label, x, y, Palette.TEXT2, 10);
            int vx = x + Math.Min(w / 2 + 10, TextW(g, label, 10) + 10);
            DrawText(g, Fit(g, value, x + w - vx, 10), vx, y, ok ? Palette.TEXT : Palette.DANGERTXT, 10);
        }

        void RepairMenuFromHome()
        {
            DispatchAccounts(delegate {
                string note = SyncConsolesMenu();
                TryBeginInvoke(delegate { if (note == MenuSynced) _menuState = "ok"; });
                if (note == MenuSynced) note = "Explorer menu repaired and re-verified.";
                Note(note);
            });
        }

        // The limits block shared by Home: header with source, age and the
        // Left/Used switch, the headline reading, then one row per account.
        int DrawLimitsSection(Graphics g, int x, int y, int w)
        {
            string toggle = S.LimitsShowUsed ? AccountsLoc.T("ShowingUsed") : AccountsLoc.T("ShowingLeft");
            int tw = Math.Max(112, TextW(g, toggle, 8) + 16);
            var tr = new Rectangle(x + w - tw, y - 2, tw, 18);
            Buttons.Add(new ButtonDef { R = tr, A = () => { S.LimitsShowUsed = !S.LimitsShowUsed; S.Save(); Invalidate(); }, Label = toggle });
            DrawButton(g, tr, toggle, S.LimitsShowUsed, 8);
            DrawSectionRule(g, x, y, tr.X - x - 8, AccountsLoc.T("AiLimits"));
            y += 18;
            if (_limits == null || _accounts == null && AccountsExe() != null)
            {
                string msg = _limitsBusy || (_limitsProblem.Length == 0) ? AccountsLoc.T("LimitsReading")
                    : _limitsProblem == "LIMISAW_NOT_FOUND" && AccountsExe() == null ? AccountsLoc.T("LimitsNoSource")
                    : AccountsLoc.T("LimitsNone");
                DrawText(g, Fit(g, msg, w, 10), x, y, Palette.TEXT2, 10);
                y += 18;
                if (LimisawExe() == null)
                {
                    string lbl = AccountsLoc.T("LocateLimisaw");
                    var lr = new Rectangle(x, y, Math.Max(130, TextW(g, lbl, 9) + 18), 20);
                    Buttons.Add(new ButtonDef { R = lr, A = () => LocateLimisaw(), Label = lbl });
                    DrawButton(g, lr, lbl, false, 9);
                    y += 26;
                }
                return y;
            }
            bool stale = LimitsStale();
            string src = AccountsLoc.T("LimitsFrom", _limits.Source) + "  ·  " + LimitsAgeText() + (stale ? "  ·  " + AccountsLoc.T("StateStale") : "");
            DrawText(g, Fit(g, src, w, 9), x, y, stale ? Color.FromArgb(210, 170, 60) : Palette.MUTED, 9);
            y += 16;

            List<LimitAccount> shown = LimitsShown();
            LimitAccount owner;
            string fallback;
            LimitsPresentation presentation = LimitsPresentation.FromSettings(S);
            var hiddenReadings = presentation.HiddenReadings;
            var readingOrder = presentation.ReadingOrder;
            LimitWindow head = Limits.Resolve(LimitsInService(), shown, S.LimitsMetric, _limitsCurrentMetric,
                hiddenReadings, readingOrder, out owner, out fallback);
            if (head != null && S.LimitsMetric == "any") _limitsCurrentMetric = head.MetricId;
            string modeName = S.LimitsMetric == "any" ? AccountsLoc.T("MetricAny")
                : S.LimitsMetric == "lowest" ? AccountsLoc.T("MetricLowest") : AccountsLoc.T("MetricPinned");
            if (head != null && owner != null)
            {
                string who = (owner.SaiCompact.Length > 0 ? owner.SaiCompact : owner.Title) + " · " + WindowTitle(head);
                string val = Limits.Shown(head, S.LimitsShowUsed) + " " + (S.LimitsShowUsed ? AccountsLoc.T("WordUsed") : AccountsLoc.T("WordLeft"));
                string reset = head.ResetEpoch.HasValue ? "  ·  " + AccountsLoc.T("Resets", Countdown(head.ResetEpoch)) : "";
                string headState = Limits.State(owner, stale);
                if (headState != "OK") reset += "  ·  " + LimitStateWord(headState);
                DrawText(g, modeName + ":", x, y, Palette.TEXT2, 10);
                int hx = x + TextW(g, modeName + ": ", 10);
                DrawText(g, Fit(g, val + "  " + who + reset, x + w - hx, 10), hx, y, Palette.TEXT, 10);
            }
            else DrawText(g, modeName + ": --", x, y, Palette.TEXT2, 10);
            y += 20;
            if (fallback.Length > 0)
            {
                DrawText(g, Fit(g, AccountsLoc.T(fallback == "PIN_GONE" ? "PinGone" : "PinUnavailable"), w, 8), x, y, Palette.TEXT2, 8);
                y += 15;
            }

            // Fixed columns; each independent pool has its own rows. Keep every
            // selected window, including extra short/long windows, instead of
            // squeezing pools into available width or merging their numbers.
            double now = Limits.Now();
            bool inferredReset = head != null && head.Available && head.AssumedFull;
            int quotaW = (w - 80 - 96 - 100 - 116) / 2;
            int[] columns = { 80, 96, quotaW, w - 80 - 96 - 100 - 116 - quotaW, 100, 116 };
            y = DrawHomeTextRow(g, x, y, columns, new[] {
                AccountsLoc.T("HomeColAccount"), AccountsLoc.T("HomeColPool"),
                AccountsLoc.T("HomeColShort"), AccountsLoc.T("HomeColLong"),
                AccountsLoc.T("HomeColReset"), AccountsLoc.T("HomeColState") }, true, false);
            int row = 0;
            foreach (LimitAccount a in shown)
            {
                var windows = new List<LimitWindow>();
                foreach (LimitReading r in Limits.Readings(new List<LimitAccount> { a }, hiddenReadings, readingOrder)) windows.Add(r.Window);
                if (windows.Exists(z => z.Available && z.AssumedFull)) inferredReset = true;
                if (a.Windows.Count > 0 && windows.Count == 0) continue;
                string label = a.SaiCompact.Length > 0 ? a.SaiCompact
                    : a.Short.Length > 0 ? a.Short : a.Title.Length > 0 ? a.Title : a.ProviderLabel;
                string state = Limits.State(a, stale);
                bool dim = state != "OK";
                bool anyWindow = false;
                foreach (LimitWindow lw0 in windows) if (lw0.Available || lw0.GatedBy.Length > 0) { anyWindow = true; break; }
                if (state == "SIGN_IN" || state == "OFFLINE" || !anyWindow)
                {
                    string word = !anyWindow && state == "OK" ? AccountsLoc.T("NotReported") : LimitStateWord(state);
                    y = DrawHomeTextRow(g, x, y, columns, new[] { label, "--", "--", "--", "--", word }, false, row++ % 2 != 0);
                    continue;
                }
                var pools = new List<string>();
                foreach (LimitWindow lw0 in windows) if (!pools.Contains(lw0.Group)) pools.Add(lw0.Group);
                foreach (string pool in pools)
                {
                    var inPool = windows.FindAll(z => z.Group == pool);
                    var shortWindows = inPool.FindAll(z => z.IsShort || z.Base == "five_hour");
                    var longWindows = inPool.FindAll(z => !z.IsShort && z.Base != "five_hour");
                    for (int i = 0; i < Math.Max(shortWindows.Count, longWindows.Count); i++)
                    {
                        LimitWindow shortWindow = i < shortWindows.Count ? shortWindows[i] : null;
                        LimitWindow longWindow = i < longWindows.Count ? longWindows[i] : null;
                        LimitWindow resetWindow = shortWindow != null && shortWindow.Available && shortWindow.GatedBy.Length == 0
                            && shortWindow.ResetEpoch.HasValue && shortWindow.ResetEpoch.Value > now ? shortWindow : longWindow;
                        string reset = resetWindow != null && resetWindow.Available && resetWindow.GatedBy.Length == 0
                            && resetWindow.ResetEpoch.HasValue && resetWindow.ResetEpoch.Value > now
                            ? HomeWindowLabel(resetWindow) + " " + Countdown(resetWindow.ResetEpoch) : "--";
                        DrawHomeTextRow(g, x, y, columns, new[] { label, HomePoolLabel(inPool[0]), "", "", reset,
                            state == "OK" ? AccountsLoc.T("WordOk") : LimitStateWord(state) }, false, row++ % 2 != 0);
                        int quotaX = x + columns[0] + columns[1];
                        DrawHomeQuotaCell(g, new Rectangle(quotaX, y, columns[2], HomeTableRowHeight), a, shortWindow, dim);
                        DrawHomeQuotaCell(g, new Rectangle(quotaX + columns[2], y, columns[3], HomeTableRowHeight), a, longWindow, dim);
                        y += HomeTableRowHeight;
                    }
                }
            }
            if (shown.Count == 0) { DrawText(g, AccountsLoc.T("LimitsNone"), x, y, Palette.TEXT2, 10); y += 18; }
            if (inferredReset) { DrawText(g, Fit(g, AccountsLoc.T("AssumedReset"), w, 8), x, y, Palette.MUTED, 8); y += 15; }
            return y + 2;
        }

        void LocateLimisaw()
        {
            using (var dlg = new OpenFileDialog())
            {
                dlg.Filter = "LIMISAW.exe|LIMISAW.exe|*.exe|*.exe";
                dlg.Title = AccountsLoc.T("LocateLimisaw");
                if (dlg.ShowDialog(this) != DialogResult.OK) return;
                S.LimisawPath = dlg.FileName;
                S.Save();
                _limisawCached = null;
                InvalidateFacts();
                RefreshLimits(true);
                Invalidate();
            }
        }

        // ============================== SETTINGS ==============================
        // The one place for normal product settings, in sections. Internal
        // tuning stays in its own files; technical facts sit under ADVANCED.
        int DrawLimitReadingSettings(Graphics g, int x, int y, int w)
        {
            if (_limits == null) return y;
            var readings = Limits.Readings(_limits.Accounts, null, SaitulsSettings.SplitList(S.LimitsReadingOrder));
            if (readings.Count == 0) return y;
            string disclosure = (_limitReadingSettingsExpanded ? "[-] " : "[+] ") + AccountsLoc.T("ReadingSettings");
            var disclosureRect = new Rectangle(x, y, Math.Min(w, TextW(g, disclosure, 9) + 18), 20);
            Buttons.Add(new ButtonDef { R = disclosureRect, A = () => { _limitReadingSettingsExpanded = !_limitReadingSettingsExpanded; Invalidate(); }, Label = disclosure });
            DrawButton(g, disclosureRect, disclosure, _limitReadingSettingsExpanded, 9);
            y += 24;
            if (!_limitReadingSettingsExpanded) return y;
            var hidden = SaitulsSettings.SplitList(S.LimitsReadingHidden);
            int pinW = TextW(g, "[X] " + AccountsLoc.T("PinReading"), 8) + 14;
            int showW = Math.Max(TextW(g, AccountsLoc.T("ReadingShown"), 8), TextW(g, AccountsLoc.T("ReadingHidden"), 8)) + 14;
            int upW = TextW(g, AccountsLoc.T("MoveReadingUp"), 8) + 14;
            int downW = TextW(g, AccountsLoc.T("MoveReadingDown"), 8) + 14;
            int actionsW = pinW + showW + upW + downW + 12;
            for (int i = 0; i < readings.Count; i++)
            {
                LimitReading reading = readings[i];
                string id = reading.Window.MetricId;
                if (id.Length == 0) continue;
                string who = reading.Account.SaiCompact.Length > 0 ? reading.Account.SaiCompact : reading.Account.Title;
                string title = who + " · " + WindowTitle(reading.Window);
                DrawText(g, Fit(g, title, w - actionsW - 8, 8), x, y + 4, Palette.TEXT2, 8);
                int bx = x + w - actionsW;
                bool pinned = S.LimitsMetric == id, hid = hidden.Contains(id);
                string pinLabel = (pinned ? "[X] " : "") + AccountsLoc.T("PinReading");
                var pr = new Rectangle(bx, y, pinW, 18);
                Buttons.Add(new ButtonDef { R = pr, A = () => PinLimitReading(id), Label = pinLabel, Sel = pinned });
                DrawButton(g, pr, pinLabel, pinned, 8);
                bx += pinW + 4;
                string showLabel = AccountsLoc.T(hid ? "ReadingHidden" : "ReadingShown");
                var vr = new Rectangle(bx, y, showW, 18);
                Buttons.Add(new ButtonDef { R = vr, A = () => ToggleLimitReadingHidden(id), Label = showLabel, Sel = !hid });
                DrawButton(g, vr, showLabel, !hid, 8);
                bx += showW + 4;
                string upLabel = AccountsLoc.T("MoveReadingUp");
                var ur = new Rectangle(bx, y, upW, 18);
                if (i > 0)
                {
                    Buttons.Add(new ButtonDef { R = ur, A = () => MoveLimitReading(id, -1), Label = upLabel });
                    DrawButton(g, ur, upLabel, false, 8);
                }
                else DrawText(g, upLabel, bx + 7, y + 4, Palette.MUTED, 8);
                bx += upW + 4;
                string downLabel = AccountsLoc.T("MoveReadingDown");
                var dr = new Rectangle(bx, y, downW, 18);
                if (i < readings.Count - 1)
                {
                    Buttons.Add(new ButtonDef { R = dr, A = () => MoveLimitReading(id, 1), Label = downLabel });
                    DrawButton(g, dr, downLabel, false, 8);
                }
                else DrawText(g, downLabel, bx + 7, y + 4, Palette.MUTED, 8);
                y += 22;
            }
            return y + 4;
        }

        void DrawSettingsTab(Graphics g, int x, int y, int w, int h)
        {
            int top = y;
            y -= TabScroll("Settings");
            int startButtons = Buttons.Count;
            int rowH = 24;
            ToolFacts facts = Facts();

            // ---- GENERAL ----
            DrawSectionRule(g, x, y, w, AccountsLoc.T("GeneralHdr"));
            y += 20;
            DrawText(g, AccountsLoc.T("LanguageRow"), x, y + 3, Palette.TEXT2, 10);
            int lx = x + 150;
            string languageLabel = AccountsLoc.LanguageName(S.Language) + "  v";
            var languageRect = new Rectangle(lx, y, Math.Max(80, Math.Min(310, w - 150)), 20);
            Buttons.Add(new ButtonDef { R = languageRect, A = ChooseLanguage, Label = languageLabel });
            DrawButton(g, languageRect, Fit(g, languageLabel, languageRect.Width - 12, 9), false, 9);
            y += rowH + 2;
            bool ao = facts.AutostartApp;
            string aoLbl = (ao ? "[X] " : "[ ] ") + AccountsLoc.T("StartWithWindows");
            var ar = new Rectangle(x, y, Math.Max(190, TextW(g, aoLbl, 10) + 18), 22);
            Buttons.Add(new ButtonDef { R = ar, A = () => ToggleAutostart("SaitulsApp", "SAITULS"), Label = aoLbl });
            DrawButton(g, ar, aoLbl, ao, 10);
            DrawText(g, Fit(g, AccountsLoc.T("StartWithWindowsHint"), x + w - ar.Right - 10, 10), ar.Right + 10, y + 4, Palette.MUTED, 10);
            y += rowH + 8;
            DrawTaskbarEdgeSection(g, x, y + 8, w);
            y += 138;

            // ---- AI LIMITS ----
            DrawSectionRule(g, x, y, w, AccountsLoc.T("LimitsHdr"));
            y += 20;
            string showLbl = S.LimitsShowUsed ? AccountsLoc.T("ShowingUsed") : AccountsLoc.T("ShowingLeft");
            var sr = new Rectangle(x, y, Math.Max(150, TextW(g, showLbl, 9) + 18), 20);
            Buttons.Add(new ButtonDef { R = sr, A = () => { S.LimitsShowUsed = !S.LimitsShowUsed; S.Save(); Invalidate(); }, Label = showLbl });
            DrawButton(g, sr, showLbl, S.LimitsShowUsed, 9);
            y += rowH;
            DrawText(g, AccountsLoc.T("HeadlineNumber"), x, y + 3, Palette.TEXT2, 10);
            int mx = x + 150;
            foreach (string mode in new[] { "lowest", "any" })
            {
                string lbl = mode == "any" ? AccountsLoc.T("MetricAny") : AccountsLoc.T("MetricLowest");
                int bw = TextW(g, lbl, 9) + 18;
                var r = new Rectangle(mx, y, bw, 20);
                string m = mode;
                Buttons.Add(new ButtonDef { R = r, A = () => { S.LimitsMetric = m; S.Save(); Invalidate(); }, Label = lbl, Sel = S.LimitsMetric == mode });
                DrawButton(g, r, lbl, S.LimitsMetric == mode, 9);
                mx += bw + 4;
            }
            y += rowH;
            y = DrawLimitReadingSettings(g, x, y, w);
            string hsLbl = (S.LimitsHideSpent ? "[X] " : "[ ] ") + AccountsLoc.T("HideSpent");
            var hsr = new Rectangle(x, y, Math.Max(220, TextW(g, hsLbl, 9) + 18), 20);
            Buttons.Add(new ButtonDef { R = hsr, A = () => { S.LimitsHideSpent = !S.LimitsHideSpent; S.Save(); Invalidate(); }, Label = hsLbl });
            DrawButton(g, hsr, hsLbl, S.LimitsHideSpent, 9);
            y += rowH;
            string ofLbl = (S.LimitsOnlyFiveHour ? "[X] " : "[ ] ") + AccountsLoc.T("OnlyFiveHour");
            var ofr = new Rectangle(x, y, Math.Max(220, TextW(g, ofLbl, 9) + 18), 20);
            Buttons.Add(new ButtonDef { R = ofr, A = () => { S.LimitsOnlyFiveHour = !S.LimitsOnlyFiveHour; S.Save(); Invalidate(); }, Label = ofLbl });
            DrawButton(g, ofr, ofLbl, S.LimitsOnlyFiveHour, 9);
            y += rowH + 2;
            string limisaw = facts.LimisawExe;
            DrawText(g, AccountsLoc.T("LimitsSource"), x, y + 3, Palette.TEXT2, 10);
            string srcText = limisaw != null ? limisaw : (facts.AccountsExe != null ? "SAI Accounts (Antigravity)" : AccountsLoc.T("NotInstalled"));
            string locLbl = AccountsLoc.T("LocateLimisaw");
            int lw2 = Math.Max(120, TextW(g, locLbl, 9) + 18);
            var locR = new Rectangle(x + w - lw2, y, lw2, 20);
            Buttons.Add(new ButtonDef { R = locR, A = () => LocateLimisaw(), Label = locLbl });
            DrawButton(g, locR, locLbl, false, 9);
            DrawText(g, Fit(g, srcText, locR.X - x - 158, 9), x + 150, y + 4, Palette.TEXT, 9);
            y += rowH + 4;

            // 5h keeper, per account. Only where it means something.
            DrawText(g, AccountsLoc.T("Keep5h"), x, y, Palette.TEXT, 10, true);
            y += 16;
            DrawText(g, Fit(g, AccountsLoc.T("Keep5hHint"), w, 8), x, y, Palette.MUTED, 8);
            y += 14;
            int kx = x;
            if (_accounts != null)
                foreach (AccountRow r in _accounts)
                {
                    if (!FiveHourKeeper.Supported(r.Provider)) continue;
                    bool on = KeepFiveHourOn(r.Id);
                    string lbl = (on ? "[X] " : "[ ] ") + r.Compact;
                    int bw = TextW(g, lbl, 9) + 16;
                    if (kx + bw > x + w) { kx = x; y += 24; }
                    var kr = new Rectangle(kx, y, bw, 20);
                    string id = r.Id;
                    Buttons.Add(new ButtonDef { R = kr, A = () => ToggleKeepFiveHour(id), Label = lbl, Sel = on });
                    DrawButton(g, kr, lbl, on, 9);
                    kx += bw + 4;
                }
            y += 24;
            // A6: what the keeper is doing for every armed account, in words.
            if (_accounts != null)
                foreach (AccountRow r in _accounts)
                {
                    if (!FiveHourKeeper.Supported(r.Provider)) continue;
                    if (!KeepFiveHourOn(r.Id)) continue;
                    string diag = r.Compact + ": " + KeeperDiagnostics(r.Id);
                    if (diag.Length == 0) continue;
                    DrawText(g, Fit(g, diag, w, 8), x, y, Palette.TEXT2, 8);
                    y += 14;
                }
            DrawText(g, AccountsLoc.T("Keep5hUnsupported"), x, y, Palette.MUTED, 8);
            y += 14;
            // "Hidden here" -- the local limit-list hide (S.LimitsHidden, keys from
            // the limits source, e.g. "claude/aaa"). It is a display filter of THIS
            // app, not the account plane's hidden flag, so the two are kept apart
            // on purpose and named by the two words the UI already has. Drawn from
            // the source account list, not from what is currently shown: a hidden
            // row must stay reachable or the control could only ever hide once.
            if (_limits != null && _limits.Accounts.Count > 0)
            {
                DrawText(g, Fit(g, AccountsLoc.T("AllWindows"), w, 8), x, y, Palette.MUTED, 8);
                y += 14;
                int hx = x;
                var hiddenNow = SaitulsSettings.SplitList(S.LimitsHidden);
                foreach (LimitAccount la in _limits.Accounts)
                {
                    if (la.Key.Length == 0) continue;
                    bool hid = hiddenNow.Contains(la.Key);
                    string lbl = (hid ? "[ ] " : "[X] ") + (la.SaiCompact.Length > 0 ? la.SaiCompact : la.Short) + " " + AccountsLoc.T("Hidden");
                    int bw = TextW(g, lbl, 8) + 14;
                    if (hx + bw > x + w) { hx = x; y += 22; }
                    var hr = new Rectangle(hx, y, bw, 18);
                    string key = la.Key;
                    Buttons.Add(new ButtonDef { R = hr, A = () => ToggleLimitHidden(key), Label = lbl, Sel = hid });
                    DrawButton(g, hr, lbl, false, 8);
                    hx += bw + 4;
                }
                y += 22;
            }
            string keepLine = _keepLast.Length > 0 ? _keepLast : KeepLastDurable();
            if (keepLine.Length > 0) { DrawText(g, Fit(g, keepLine, w, 8), x, y, Palette.TEXT2, 8); y += 14; }
            y += 6;

            // ---- ACCOUNTS ----
            DrawSectionRule(g, x, y, w, AccountsLoc.T("AccountsHdr"));
            y += 20;
            string keeperLabel = (S.AccountKeeper ? "[X] " : "[ ] ") + AccountsLoc.T("KeeperRow");
            var kpr = new Rectangle(x, y, Math.Max(190, TextW(g, keeperLabel, 9) + 18), 20);
            bool wantKeeper = S.AccountKeeper;
            Buttons.Add(new ButtonDef { R = kpr, A = () => KeeperSet(!wantKeeper), Label = keeperLabel });
            DrawButton(g, kpr, keeperLabel, wantKeeper, 9);
            DrawText(g, Fit(g, _keeperState.Length > 0 ? _keeperState : AccountsLoc.T("Checking"), x + w - kpr.Right - 10, 8), kpr.Right + 10, y + 5, Palette.TEXT2, 8);
            y += rowH;
            DrawText(g, Fit(g, AccountsLoc.T("AccountKeeperHint"), w, 8), x, y, Palette.MUTED, 8);
            y += 20;

            // ---- INTEGRATION ----
            DrawSectionRule(g, x, y, w, AccountsLoc.T("IntegrationHdr"));
            y += 20;
            bool menuBad = _menuSyncFailed || _menuState == "failed";
            DrawText(g, AccountsLoc.T("ExplorerMenusRow"), x, y + 3, Palette.TEXT2, 10);
            string menuText = menuBad ? AccountsLoc.T("NeedsRepair") : _menuState == "ok" ? AccountsLoc.T("MenusVerified") : AccountsLoc.T("Checking");
            DrawText(g, menuText, x + 150, y + 3, menuBad ? Palette.DANGERTXT : Palette.TEXT, 10);
            string repLbl = AccountsLoc.T("Repair");
            int rw = Math.Max(80, TextW(g, repLbl, 9) + 18);
            var rr = new Rectangle(x + w - rw, y, rw, 20);
            Buttons.Add(new ButtonDef { R = rr, A = () => RepairMenuFromHome(), Label = repLbl });
            DrawButton(g, rr, repLbl, menuBad, 9);
            y += rowH + 6;

            // ---- ADVANCED ----
            DrawSectionRule(g, x, y, w, AccountsLoc.T("AdvancedHdr"));
            y += 20;
            DrawText(g, Fit(g, AccountsLoc.T("ToolkitRoot"), 100, 9), x, y, Palette.TEXT2, 9);
            DrawText(g, Fit(g, S.RootPath, w - 110, 9), x + 110, y, Palette.TEXT, 9);
            y += 15;
            DrawText(g, "INI", x, y, Palette.TEXT2, 9);
            DrawText(g, Fit(g, S.IniPath, w - 110, 9), x + 110, y, Palette.MUTED, 9);
            y += 15;
            DrawText(g, "SAI Accounts", x, y, Palette.TEXT2, 9);
            DrawText(g, Fit(g, facts.AccountsExe ?? AccountsLoc.T("NotInstalled"), w - 110, 9), x + 110, y, Palette.MUTED, 9);
            y += 15;
            DrawText(g, AccountsLoc.T("LimitsCache"), x, y, Palette.TEXT2, 9);
            DrawText(g, Fit(g, LimitsDir(), w - 110, 9), x + 110, y, Palette.MUTED, 9);
            y += 20;
            int bw2 = 140;
            var exr = new Rectangle(x, y, bw2, 24);
            Buttons.Add(new ButtonDef { R = exr, A = () => { Application.Exit(); }, Label = AccountsLoc.T("ExitApp") });
            DrawButton(g, exr, AccountsLoc.T("ExitApp"), false, 9);
            var ofo = new Rectangle(x + bw2 + 8, y, bw2, 24);
            Buttons.Add(new ButtonDef { R = ofo, A = () => { Process.Start("explorer.exe", S.RootPath); }, Label = AccountsLoc.T("OpenFolder") });
            DrawButton(g, ofo, AccountsLoc.T("OpenFolder"), false, 9);
            y += 32;
            DrawText(g, AccountsLoc.T("SettingsSavedHint"), x, y, Palette.MUTED, 8);
            y += 16;

            _tabContentH["Settings"] = (y + TabScroll("Settings")) - top + 8;
            ClampButtons(startButtons, new Rectangle(x - 8, top - 8, w + 16, h + 16));
        }

        void SetLanguage(string code)
        {
            S.Language = code;
            S.Save();
            AccountsLoc.Apply(code);
            Invalidate();
        }

        void ChooseLanguage()
        {
            string[] codes = AccountsLoc.Choices;
            string[] names = new string[codes.Length];
            for (int i = 0; i < codes.Length; i++) names[i] = AccountsLoc.LanguageName(codes[i]) + (codes[i] == "auto" ? "" : " (" + codes[i] + ")");
            using (var dialog = new Form())
            using (var font = new Font("Verdana", 8.25f))
            {
                dialog.Text = AccountsLoc.T("LanguageRow");
                dialog.Font = font; dialog.BackColor = Palette.BG;
                dialog.FormBorderStyle = FormBorderStyle.FixedDialog;
                dialog.StartPosition = FormStartPosition.CenterParent;
                dialog.MinimizeBox = false; dialog.MaximizeBox = false;
                dialog.ClientSize = new Size(400, 125);
                dialog.Controls.Add(FolderAutomation.FaUi.Label(AccountsLoc.T("LanguageRow"), 16, 12, 368));
                ComboBox choice = FolderAutomation.FaUi.Combo(16, 34, 368, names);
                choice.SelectedIndex = Math.Max(0, Array.IndexOf(codes, S.Language));
                dialog.Controls.Add(choice);
                Button apply = FolderAutomation.FaUi.Btn(AccountsLoc.T("LanguageApply"), 164, 80, 110);
                apply.DialogResult = DialogResult.OK;
                Button cancel = FolderAutomation.FaUi.Btn(AccountsLoc.T("Cancel"), 282, 80, 102);
                cancel.DialogResult = DialogResult.Cancel;
                dialog.Controls.Add(apply); dialog.Controls.Add(cancel);
                dialog.AcceptButton = apply; dialog.CancelButton = cancel;
                if (dialog.ShowDialog(this) == DialogResult.OK && choice.SelectedIndex >= 0)
                    SetLanguage(codes[choice.SelectedIndex]);
            }
        }

        // ========================= Accounts: real usage =========================
        // Every window the limits source reports for this account, each with its
        // own pool, meter, number and reset. A reading that is not there says
        // why in words; nothing is ever drawn from the account's quota STATE.
        int DrawUsageSection(Graphics g, int indent, int oy, int pw, AccountRow sel)
        {
            using (var p = new Pen(Palette.BMUTED)) g.DrawLine(p, indent, oy, indent + pw - 20, oy);
            oy += 6;
            DrawText(g, AccountsLoc.T("UsageHdr"), indent, oy, Palette.MUTED, 9, true);
            LimitAccount la = LimitsFor(sel);
            string toggle = S.LimitsShowUsed ? AccountsLoc.T("ShowingUsed") : AccountsLoc.T("ShowingLeft");
            int tw = Math.Max(104, TextW(g, toggle, 8) + 14);
            var tr = new Rectangle(indent + pw - 20 - tw, oy - 3, tw, 17);
            Buttons.Add(new ButtonDef { R = tr, A = () => { S.LimitsShowUsed = !S.LimitsShowUsed; S.Save(); Invalidate(); }, Label = toggle });
            DrawButton(g, tr, toggle, S.LimitsShowUsed, 8);
            oy += 16;
            if (la == null)
            {
                string why = _limits == null ? (_limitsBusy || _limitsProblem.Length == 0 ? AccountsLoc.T("LimitsReading") : AccountsLoc.T("LimitsNone"))
                    : AccountsLoc.T("NotReported");
                DrawText(g, why, indent, oy, Palette.TEXT2, 9);
                return oy + 18;
            }
            string state = Limits.State(la, LimitsStale());
            if (state != "OK")
            {
                string word = LimitStateWord(state);
                if (state == "STALE" && la.CarriedAt.Length > 0) word += "  ·  " + AccountsLoc.T("StaleSince", la.CarriedAt);
                DrawText(g, word, indent, oy, state == "SIGN_IN" ? Color.FromArgb(210, 170, 60) : Palette.TEXT2, 9, true);
                oy += 15;
                string detail = la.AuthFailureReason.Length > 0 ? la.AuthFailureReason : la.CarriedNote.Length > 0 ? la.CarriedNote : la.Error;
                if (detail.Length > 0) { DrawText(g, Fit(g, detail, pw - 20, 8), indent, oy, Palette.MUTED, 8); oy += 13; }
            }
            double now = Limits.Now();
            bool dim = state != "OK";
            foreach (LimitWindow w in la.Windows)
            {
                int titleW = Math.Max(140, Math.Min(220, (pw - 20) * 45 / 100));
                string title = Fit(g, WindowTitle(w), titleW, 9);
                DrawText(g, title, indent, oy, Palette.TEXT2, 9);
                int mx = indent + titleW + 6;
                if (w.GatedBy.Length > 0)
                    DrawText(g, AccountsLoc.T("LockedBy", GateLabel(la, w)), mx, oy, Palette.MUTED, 9);
                else
                {
                    int after = DrawLimitMeter(g, mx, oy, 70, w, dim);
                    string reset = w.ResetEpoch.HasValue && w.ResetEpoch.Value > now
                        ? AccountsLoc.T("Resets", Countdown(w.ResetEpoch)) : "";
                    int rx = Math.Max(after + 10, mx + 120);
                    if (reset.Length > 0 && rx < indent + pw - 40)
                        DrawText(g, Fit(g, reset, indent + pw - 20 - rx, 8), rx, oy + 1, Palette.MUTED, 8);
                }
                oy += 15;
            }
            if (la.Windows.Count == 0) { DrawText(g, AccountsLoc.T("NotReported"), indent, oy, Palette.TEXT2, 9); oy += 15; }
            if (la.Windows.Exists(z => z.Available && z.AssumedFull))
            { DrawText(g, Fit(g, AccountsLoc.T("AssumedReset"), pw - 20, 8), indent, oy, Palette.MUTED, 8); oy += 15; }
            if (FiveHourKeeper.Supported(sel.Provider))
            {
                bool on = KeepFiveHourOn(sel.Id);
                string lbl = (on ? "[X] " : "[ ] ") + AccountsLoc.T("Keep5h");
                var kr = new Rectangle(indent, oy + 2, Math.Max(170, TextW(g, lbl, 8) + 16), 18);
                string id = sel.Id;
                Buttons.Add(new ButtonDef { R = kr, A = () => ToggleKeepFiveHour(id), Label = lbl });
                DrawButton(g, kr, lbl, on, 8);
                oy += 24;
                // A6: the keeper's own answer for this account, so "why has my
                // window not started" is answerable without a log file.
                string diag = KeeperDiagnostics(sel.Id);
                if (diag.Length > 0)
                { DrawText(g, Fit(g, diag, pw - 20, 8), indent, oy, Palette.TEXT2, 8); oy += 14; }
            }
            return oy + 4;
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
            DrawText(g, AccountsLoc.T("MenusInstallHdr"), x, y, Palette.TEXT, 12, true);
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
                string label = AccountsLoc.T(RegLabels[i]);
                int pt = TextW(g, label, 11) > colW - 22 ? 9 : 11;
                DrawText(g, Fit(g, label, colW - 22, pt), cx + 18, cy + 1, Palette.TEXT, pt);
            }

            int btnY = chkY + 8 * 20 + 12;
            int btnW = 120;
            int btnH = 26;
            // Check All / Uncheck All
            var caR = new Rectangle(x, btnY, btnW, btnH);
            Buttons.Add(new ButtonDef { R = caR, A = () => { for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = true; Refresh(); } });
            DrawButton(g, caR, AccountsLoc.T("CheckAll"), false);

            var uaR = new Rectangle(x + btnW + 8, btnY, btnW, btnH);
            Buttons.Add(new ButtonDef { R = uaR, A = () => { for (int i = 0; i < FeatureChecked.Length; i++) FeatureChecked[i] = false; Refresh(); } });
            DrawButton(g, uaR, AccountsLoc.T("UncheckAll"), false);

            // Install / Remove
            int btnY2 = btnY + btnH + 8;
            var inR = new Rectangle(x, btnY2, btnW, btnH);
            Buttons.Add(new ButtonDef { R = inR, A = () => InstallSelected() });
            DrawButton(g, inR, AccountsLoc.T("Install"), false);

            var rmR = new Rectangle(x + btnW + 8, btnY2, btnW, btnH);
            Buttons.Add(new ButtonDef { R = rmR, A = () => UninstallSelected() });
            DrawButton(g, rmR, AccountsLoc.T("Remove"), false);
        }

        // MEDIA section grid: label | worker | mode. This is the canonical list
        // the section draws AND the media matrix discovers, so there is no
        // second hardcoded list to drift. The workers own every rule; the shell
        // only picks the input and reports the real outcome.
        string[,] MediaDefs = {
            { "DownloadAudio", "DL_YT.PS1",     "audio" },
            { "DownloadVideo", "DL_YT.PS1",     "video" },
            { "MergeAudio",    "MERGE_AUD.PS1", "file"  },
        };

        // label | script | argument mode
        //   "none"   = no target needed
        //   "folder" = ask for a folder, pass it as %1
        //   "file"   = ask for a file, pass it as %1
        string[,] ToolDefs = {
            { "ToolNewProject",  "NEW_PROJ.CMD",     "folder" },
            { "ToolPackFile",    "PACK.PYW",         "file"   },
            { "ToolPackFolder",  "PACK.PYW",         "folder" },
            { "ToolDelEmpty",    "DEL_EMPTY.PYW",    "folder" },
            { "ToolDelDup",      "DEL_DUP.PYW",      "folder" },
            { "ToolDelSame",     "DEL_SAME.PYW",     "folder" },
            { "ToolDelJunk",     "DEL_JUNK.PYW",     "folder" },
            { "ToolPsAdmin",     "powershell.exe",   "none"   },
            // Item List / Item Tree: the shared first-party engine
            // (Scripts\item_tools.py) in its two modes. The shell only picks
            // the target and the output path, runs the worker hidden and
            // reports the real result -- no traversal logic lives here.
            { "ToolItemList",    "item_tools.py",    "itemlist" },
            { "ToolItemTree",    "item_tools.py",    "itemtree" },
            // OpenCode Patcher: the ONLY SAIPATCH surface in SAITULS. The button
            // launches the standalone patcher (Scripts\saipatch\patcher.ps1);
            // all patch logic lives there, never here.
            { "ToolPatcher", "saipatch",     "saipatch" },
            // OpenCode Settings: the SAIPATCH completion-sound settings GUI
            // (Scripts\saipatch\settings.ps1). Same boundary as the patcher.
            { "ToolOpenCodeSettings", "saipatch",    "saipatch-settings" },
            // OpenCode Queue Viewer: the SAIPATCH read-only queue cockpit
            // (Scripts\saipatch\queue.ps1 -> queue_viewer.pyw). Same SAIPATCH
            // boundary; no patch Apply/Restore controls live in the Viewer.
            { "ToolQueueViewer", "saipatch", "saipatch-queue-viewer" },
            // Scenarios: the ONLY surface for the migrated _SCENARIOS collection.
            // One button launches the dedicated data-driven launcher
            // (Scripts\scenarios\SCENARIOS.ps1); no scenario logic lives here.
            { "ToolScenarios", "scenarios",           "scenarios" },
            // Secure Apps: the ONLY surface for the FIDO2-gated protected
            // application subsystem (Scripts\secure_apps\SECURE_APPS.ps1).
            // No FIDO2 protocol code, no key handling, no BitLocker lifecycle,
            // no idle monitoring and no protected-process management lives in
            // this shell -- the subsystem owns all of it, exactly as SAIPATCH
            // and Scenarios own theirs. This button launches it and nothing else.
            { "ToolSecureApps", "secureapps",        "secureapps" },
            // AI Consoles: the data-driven, NON-elevated console launcher
            // (Scripts\consoles\CONSOLES.ps1). Separate from Secure Apps:
            // these profiles are not behind the encrypted vault.
            { "ToolAiConsoles", "consoles",          "consoles" },
            // Shell Doctor: read-only Explorer/Start lag diagnosis
            // (Scripts\shell_doctor\SHELL_DOCTOR.ps1) -- storage resets, idle
            // shell extensions, handler overlap. Its only mutation is an
            // opt-in per-user view/cache reset it asks for itself.
            { "ToolShellDoctor", "shelldoctor",      "shelldoctor" },
            // SAISPIN Settings: per-user watchdog policy and transactional task
            // registration. The editor stays non-elevated.
            { "ToolSaiSpinSettings", "saispin_settings.ps1", "saispin-settings" },
            // Optimizer: the SAIOPT background resident (Scripts\saiopt\SAIOPT.exe).
            // The button starts it -- or reaches the running copy -- and opens
            // its panel. Sampling, priority easing, disk watch and stale sweep
            // are all SAIOPT's; none of it lives in this shell.
            { "ToolOptimizer",   "saiopt",            "saiopt" },
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

            // ------------------------------------------------------------------
            // MEDIA - grouped, understandable, with ONE compact readiness line so
            // nobody has to press a button and watch a console disappear (B1).
            // ------------------------------------------------------------------
            DrawSectionHeader(g, x, cy, w, AccountsLoc.T("MediaHdr"));
            cy += 20;
            MediaProbeResult probe = _lastProbe;
            bool mediaKnown = _mediaProbeDone && probe != null;
            DrawText(g, mediaKnown ? probe.Line : AccountsLoc.T("MediaChecking"),
                x, cy, mediaKnown ? (probe.Ready ? Palette.LINK : Palette.DANGERTXT) : Palette.TEXT2, 10);
            cy += 16;
            int btnW = 126, btnH = 26, gap = 8;
            int bx = x;
            for (int i = 0; i < MediaDefs.GetLength(0); i++)
            {
                string label = AccountsLoc.T(MediaDefs[i, 0]);
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
            DrawButton(g, recheck, AccountsLoc.T("Recheck"), false, 10);
            cy += btnH + 4;
            cy = DrawJobLine(g, x, cy, w, DownloadJob, AccountsLoc.T("Download"));
            cy = DrawJobLine(g, x, cy, w, MergeJob, AccountsLoc.T("MergeAudio"));

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
            if (!clipInstalled) { state = AccountsLoc.T("ClipNotInstalled"); stateColor = Palette.DANGERTXT; }
            else if (_clipDepsDone && _clipDeps != null)
            { state = AccountsLoc.T("ClipDependencyMissing"); stateColor = Palette.DANGERTXT; }
            else if (_clipRunning) { state = AccountsLoc.T("ClipRunning"); stateColor = Palette.LINK; }
            else { state = AccountsLoc.T("ClipStopped"); stateColor = Palette.TEXT2; }
            if (_clipRunning)
            {
                state += " · " + AccountsLoc.T(_clipMonitorOn ? "ClipMonitorActive" : "ClipMonitorInactive");
                if (_clipStatusStale) state += " · " + AccountsLoc.T("ClipStatusStale");
            }
            DrawText(g, Fit(g, "Clipboard+: " + state, w, 11), x, cy, stateColor, 11, true);
            cy += 18;

            string hotkeyText = _clipHotkey.Length > 0 ? _clipHotkey : "Ctrl+Alt+Shift+C";
            if (_clipRunning && !_clipHotkeyOn)
                DrawText(g, Fit(g, AccountsLoc.T("ClipHotkeyMissing", hotkeyText), w, 10), x, cy, Palette.DANGERTXT, 10);
            else
                DrawText(g, Fit(g, AccountsLoc.T("ClipHotkey", hotkeyText), w, 10), x, cy, Palette.TEXT2, 10);
            cy += 15;

            if (_clipRunning)
            {
                if (_clipPathOk)
                    DrawText(g, Fit(g, AccountsLoc.T("ClipImages") + _clipEffectivePath + (_clipFallbackActive ? "  " + AccountsLoc.T("ClipFallback") : "") +
                        "    " + AccountsLoc.T("ClipSaved") + _clipSavedCount, w, 10), x, cy, Palette.TEXT2, 10);
                else
                    DrawText(g, Fit(g, AccountsLoc.T("ClipSaveUnavailable", _clipSavePath), w, 10), x, cy, Palette.DANGERTXT, 10);
            }
            else
            {
                string configured = _clipSavePath.Length > 0 ? _clipSavePath : AccountsLoc.T("ClipDefaultFolder");
                DrawText(g, Fit(g, AccountsLoc.T("ClipSaveFolder", configured), w, 10),
                    x, cy, _clipPathOk ? Palette.TEXT2 : Palette.DANGERTXT, 10);
            }
            cy += 16;

            int cbW = 100, cbH = 24, cbGap = 6;
            int cbx = x;
            var startR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = startR, A = ClipboardStartAction });
            DrawButton(g, startR, AccountsLoc.T("Start"), false, 10);
            cbx += cbW + cbGap;
            var stopR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = stopR, A = ClipboardStopAction });
            DrawButton(g, stopR, AccountsLoc.T("Stop"), false, 10);
            cbx += cbW + cbGap;
            var safeR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = safeR, A = ClipboardSafeCopyAction });
            DrawButton(g, safeR, AccountsLoc.T("SafeCopy"), false, 10);
            cbx += cbW + cbGap;
            int restoreW = cbW + 36;
            var restoreR = new Rectangle(cbx, cy, restoreW, cbH);
            Buttons.Add(new ButtonDef { R = restoreR, A = ClipboardRestoreAction });
            DrawButton(g, restoreR, AccountsLoc.T("Restore"), false, 10);
            cbx += restoreW + cbGap;
            var settingsR = new Rectangle(cbx, cy, cbW, cbH);
            Buttons.Add(new ButtonDef { R = settingsR, A = ClipboardSettingsAction });
            DrawButton(g, settingsR, AccountsLoc.T("TabSettings"), false, 10);
            cy += cbH + 2;

            if (_clipMsg.Length > 0)
            {
                DrawText(g, Fit(g, _clipMsg, w, 10), x, cy, Palette.TEXT2, 10);
                cy += 15;
            }

            // ------------------------------------------------------------------
            // OTHER TOOLS
            // ------------------------------------------------------------------
            cy += 6;
            DrawSectionHeader(g, x, cy, w, AccountsLoc.T("OtherTools"));
            cy += 20;
            int cols = 3;
            int cx = x;
            // Three columns across the full width: "OpenCode Queue Viewer" and
            // friends fit on one line instead of wrapping into a clipped pair.
            int toolW = (w - (cols - 1) * gap) / cols;
            for (int i = 0; i < ToolDefs.GetLength(0); i++)
            {
                string lbl = AccountsLoc.T(ToolDefs[i, 0]);
                string script = ToolDefs[i, 1];
                string mode = ToolDefs[i, 2];
                if (mode == "folder" || mode == "file") lbl += " *";
                var r = new Rectangle(cx, cy, toolW, btnH);
                string sCopy = script, mCopy = mode;
                Buttons.Add(new ButtonDef { R = r, A = () => LaunchTool(sCopy, mCopy) });
                DrawButton(g, r, lbl, false, TextW(g, lbl, 10) > toolW - 8 ? 9 : 10);
                cx += toolW + gap;
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
            DrawText(g, Fit(g, AccountsLoc.T("ToolSecureApps") + ": " + Facts().SecureLine, w, 10), x, cy + 2, Palette.TEXT2, 10);
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
                DrawButton(g, r, AccountsLoc.T("OpenFolder"), false, 9);
                bx += 114;
            }
            if (job.Finished && !string.IsNullOrEmpty(job.LogPath) && File.Exists(job.LogPath))
            {
                var r = new Rectangle(bx, y - 4, 90, 20);
                string log = job.LogPath;
                Buttons.Add(new ButtonDef { R = r, A = () => OpenPath(log) });
                DrawButton(g, r, AccountsLoc.T("ShowLog"), false, 9);
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
                DrawButton(g, ro, AccountsLoc.T("OpenOutput"), false, 9);
                bx += 114;
                var rc = new Rectangle(bx, y - 4, 122, 20);
                Buttons.Add(new ButtonDef { R = rc, A = () =>
                {
                    try { Clipboard.SetText(_itemOutPath); _itemMsg = "Output path copied."; }
                    catch { _itemMsg = "Could not copy the output path."; }
                    Refresh();
                } });
                DrawButton(g, rc, AccountsLoc.T("CopyOutputPath"), false, 9);
                bx += 128;
            }
            if (ItemJob.Finished && !string.IsNullOrEmpty(ItemJob.LogPath) && File.Exists(ItemJob.LogPath))
            {
                var rl = new Rectangle(bx, y - 4, 90, 20);
                string log = ItemJob.LogPath;
                Buttons.Add(new ButtonDef { R = rl, A = () => OpenPath(log) });
                DrawButton(g, rl, AccountsLoc.T("ShowLog"), false, 9);
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
                if (!File.Exists(statePath)) return AccountsLoc.T("SecureNotSetUp");
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
                return parts.Count == 0 ? AccountsLoc.T("SecureNoProfiles") : string.Join("  ", parts.ToArray());
            }
            catch { return AccountsLoc.T("StatusUnavailable"); }
        }

        static string ScanJsonString(string text, int from)
        {
            int open = text.IndexOf('"', from);
            if (open < 0) return null;
            int close = text.IndexOf('"', open + 1);
            if (close < 0) return null;
            return text.Substring(open + 1, close - open - 1);
        }

        // -------------------- ACCOUNTS (SAI control plane) --------------------
        // One canonical registry, one manager. Everything the tab shows comes
        // from sai-accounts.exe (Scripts/accounts/SAI_ACCOUNTS.md contract);
        // this UI never touches the registry file directly and never handles
        // credential material -- lifecycle, rename and launch are CLI calls.
        class AccountRow
        {
            public string Id, Name, Compact, Provider, Context, State, Backend;
            // Where a CLI account lives (CLAUDE_CONFIG_DIR / CODEX_HOME) and the
            // id an older registry knew it by. Identity facts from the plane,
            // used to tie a limits reading to this account -- never numbers.
            public string ProfileLocator = "", LegacyId = "", Sid = "";
            public bool Hidden;
            // Persisted intent, straight from the engine's auto_connect field.
            // Distinct from ContextState on purpose: this is what the operator
            // asked for, ContextState is what the machine is doing about it.
            public bool AutoConnect;
            // Why the intent reads the way it does, persisted beside it by the
            // engine. This is what lets OFF say "you disconnected this" instead
            // of leaving the operator to guess whether they did.
            public string AutoConnectReason = "";
            // Runtime facts, filled in by the background status poll. Empty
            // means "not polled yet" and is NOT the same as OFFLINE -- the UI
            // must not show Stop context for a context it never saw come up.
            public string ContextState = "", AuthState = "", QuotaState = "";
            // T-288: the last SAIPEN readiness verdict this GUI measured for the
            // account (sai-accounts saipen-readiness), with the project it was
            // measured on. Empty means "not measured", never "ready".
            public string SaipenVerdict = "", SaipenDetail = "", SaipenWorkdir = "";
            // T-264: the account model keeps TWO identity planes, and a
            // difference between them is NORMAL, not a fault.
            //
            // PLANE A -- CONSOLE / QUOTA ACCOUNT (ConfiguredIdentityFp/Label).
            // The canonical provider identity this slot owns. It comes from the
            // registry's `configured_identity` and is established only by an
            // explicit bind-identity -- the switch and sign-in workflows. Quota
            // and duplicate reasoning reads THIS plane and nothing else.
            public string ConfiguredIdentityFp = "", ConfiguredIdentityLabel = "";
            // PLANE B -- IDE ACCOUNT (RuntimeIdentityFp/Label). Whatever Google
            // account the running Antigravity context is signed into right now,
            // read from the credential probe. It is an OBSERVATION: it may equal
            // the console account, differ from it, equal another slot's console
            // account, or be unproven. None of those four is an error, and none
            // of them ever becomes canonical by being observed.
            public string RuntimeIdentityFp = "", RuntimeIdentityLabel = "";
            // The canonical plane under its original name, so the panel, the
            // diagnostics and "which account is this slot?" keep one field. Its
            // invariant is IdentityFp == ConfiguredIdentityFp (empty when no
            // canonical identity is proven). It must NEVER fall back to
            // RuntimeIdentityFp -- that fallback was the conflation T-264 removes.
            public string IdentityFp = "", IdentityLabel = "";
            // Compact labels of the OTHER accounts whose CANONICAL console
            // fingerprint equals this one: one human, two Windows contexts, one
            // quota pool. Empty means the slot is a distinct human. Deliberately
            // about Plane A only -- an IDE session signed into somebody else's
            // account is a login fact, not a second capacity.
            public string IdentitySharedWith = "";
            // Has this slot's provider identity ever been READ? Distinct from
            // IdentityFp, which is empty both for "signed out" and for "never
            // looked". The second one is not an answer, and a slot nobody has
            // asked cannot be reported as a duplicate of anybody.
            public bool IdentitySeen;
        }

        // One bounded child process, with everything needed to explain what it
        // did: stdout, stderr, the exit code, and whether we killed it for
        // running long.
        //
        // Both pipes are drained CONCURRENTLY with the wait. The old code read
        // stdout to the end and only then called WaitForExit -- that is the
        // classic child deadlock: a child that fills the stderr buffer blocks
        // on its own write while the parent blocks reading stdout, so neither
        // side moves and the timeout never gets its chance to fire. A wedged
        // engine could freeze this window no matter what timeoutMs said.
        class CliRun
        {
            public string Stdout = "", Stderr = "";
            public int ExitCode = -1;
            public bool TimedOut;
            public bool Ok { get { return !TimedOut && ExitCode == 0; } }
            // The last line, which is where every envelope lives.
            public string Last
            {
                get { return LastEnvelopeLine(Stdout); }
            }
            // Why it did not work, for the operator. Never "" when !Ok.
            public string Problem
            {
                get
                {
                    if (TimedOut) return "engine did not answer in time";
                    string err = Stderr == null ? "" : Stderr.Trim();
                    if (err.Length > 0) return err;
                    return "exit code " + ExitCode;
                }
            }
            // Human-readable one-liner: the envelope's status word plus its
            // error. A handled failure still answers with a valid JSON envelope and
            // a nonzero exit, so the failure path has to be rendered exactly like the
            // success path; returning the raw line there is what pushed a
            // 400-character payload into a 640px status strip.
            public string Say
            {
                get
                {
                    string parsed = AccountsLastLine(Stdout);
                    if (!Ok) return parsed.Length > 0 ? parsed : Problem;
                    return parsed;
                }
            }
        }

        static CliRun RunBounded(string exe, string args, int timeoutMs)
        {
            var res = new CliRun();
            if (exe == null || exe.Length == 0) { res.Stderr = "sai-accounts.exe not found"; return res; }
            Process p = null;
            try
            {
                var psi = new ProcessStartInfo(exe, args)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                p = Process.Start(psi);
                if (p == null) { res.Stderr = "engine did not start"; return res; }
                System.Threading.Tasks.Task<string> outTask = p.StandardOutput.ReadToEndAsync();
                System.Threading.Tasks.Task<string> errTask = p.StandardError.ReadToEndAsync();
                if (!p.WaitForExit(timeoutMs))
                {
                    res.TimedOut = true;
                    res.ExitCode = -1;
                    try { p.Kill(); } catch { }
                    try { p.WaitForExit(2000); } catch { }
                }
                else res.ExitCode = p.ExitCode;
                // The pumps finish on their own once the child is gone; bound
                // the wait anyway so a wedged pipe cannot become a new hang.
                try { if (outTask.Wait(2000)) res.Stdout = outTask.Result ?? ""; } catch { }
                try { if (errTask.Wait(2000)) res.Stderr = errTask.Result ?? ""; } catch { }
                return res;
            }
            catch (Exception ex) { res.ExitCode = -1; res.Stderr = ex.Message; return res; }
            finally { if (p != null) p.Dispose(); }
        }

        CliRun AccountsExec(string args, int timeoutMs)
        {
            return RunBounded(AccountsExe(), args, timeoutMs);
        }

        // Output-or-null shim for the callers that only want the answer. Same
        // bounded semantics as before; failures still report as null.
        string AccountsCli(string args, int timeoutMs)
        {
            CliRun r = AccountsExec(args, timeoutMs);
            return r.Ok ? r.Stdout : null;
        }

        // The engine's one-line envelope, found the right way round.
//
// Splitting on '\n' and taking [Length-1] is wrong: every line the engine
// writes ends CRLF, so the last element is the empty string AFTER the JSON and
// the caller sees "". Reading the LAST NON-EMPTY line is what actually wants.
static string LastEnvelopeLine(string json)
{
    if (json == null) return "";
    string[] lines = json.Split('\n');
    for (int i = lines.Length - 1; i >= 0; i--)
    {
        string t = lines[i].Trim();
        if (t.Length > 0) return t;
    }
    return "";
}

static Dictionary<string, object> ParseEnv(string json)
{
    try
    {
        string last = LastEnvelopeLine(json);
        if (!last.StartsWith("{")) return null;
        return new System.Web.Script.Serialization.JavaScriptSerializer()
            .DeserializeObject(last) as Dictionary<string, object>;
    }
    catch { return null; }
}

        List<AccountRow> _accounts = null;
        string _accountsNote = "";
        string _selectedAccount = null;

        // V2 two-pane layout state
        int _acctListScrollY = 0;      // list pane scroll offset (rows)
        int _acctDetailScrollY = 0;    // detail pane scroll offset (pixels)
        bool _acctAdvancedExpanded = false;  // Advanced ▸/▾ toggle

        // Account row drag-to-reorder state (T-264)
        Rectangle _acctListRect = Rectangle.Empty;
        string _acctDragAccountId = null;
        Point _acctDragStartPt = Point.Empty;
        bool _acctDragActive = false;
        int _acctDragInsertIndex = -1;
        const int AccountDragThresholdPx = 6;

        // Engine resolution, same chain the rest of the plane documents
        // (Wiki/SCRIPTS.md): SAI_ACCOUNTS_EXE override, canonical install,
        // source-tree build. The override is first because it is what makes a
        // test run against a stand-in engine possible at all.
        string AccountsExe()
        {
            string forced = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_EXE");
            if (!string.IsNullOrEmpty(forced)) return File.Exists(forced) ? forced : null;
            string canonical = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "SAI", "Accounts", "bin", "sai-accounts.exe");
            if (File.Exists(canonical)) return canonical;
            string repo = Path.Combine(S.RootPath, "Scripts", "accounts", "bin", "sai-accounts.exe");
            if (File.Exists(repo)) return repo;
            return null;
        }

        // Blocking: read the registry through the engine and build the rows. Safe on
        // a worker -- only ApplyAccounts touches the live list.
        List<AccountRow> FetchAccounts(bool tracked = false)
        {
            var rows = new List<AccountRow>();
            string json = tracked ? AccountsCliTracked("list --all", 15000) : AccountsCli("list --all", 15000);
            if (json == null) return null;
            try
            {
                var root = new System.Web.Script.Serialization.JavaScriptSerializer()
                    .DeserializeObject(json) as Dictionary<string, object>;
                object[] arr = root != null ? root["accounts"] as object[] : null;
                if (arr != null)
                    foreach (object o in arr)
                    {
                        var a = o as Dictionary<string, object>;
                        if (a == null) continue;
                        var ci = AsDict(a, "configured_identity");
                        var meta = AsDict(a, "provider_metadata");
                        string cfgFp = AsText(ci, "fingerprint");
                        string cfgLbl = AsText(ci, "account_label");
                        rows.Add(new AccountRow
                        {
                            Id = AsText(a, "account_id"),
                            Name = AsText(a, "display_name"),
                            Compact = AsText(a, "compact_label"),
                            Provider = AsText(a, "provider_id"),
                            Context = AsText(a, "context_label"),
                            State = AsText(a, "operational_state"),
                            Backend = AsText(a, "execution_backend"),
                            Hidden = object.Equals(a.ContainsKey("hidden") ? a["hidden"] : null, true),
                            AutoConnect = object.Equals(a.ContainsKey("auto_connect") ? a["auto_connect"] : null, true),
                            AutoConnectReason = AsText(a, "auto_connect_reason"),
                            ConfiguredIdentityFp = cfgFp,
                            ConfiguredIdentityLabel = cfgLbl,
                            ProfileLocator = AsText(meta, "profile_locator"),
                            LegacyId = AsText(meta, "legacy_id"),
                            Sid = !string.IsNullOrEmpty(AsText(a, "windows_sid")) ? AsText(a, "windows_sid") : AsText(meta, "windows_sid")
                        });
                    }
            }
            catch { }
            // Capabilities are per provider and never change while the engine
            // runs; asking here, on the worker that already talks to the engine,
            // is what keeps every later paint free of child processes.
            foreach (AccountRow r in rows)
                if ((r.Backend == "windows_user" || r.Backend == "managed_windows_user") && !string.IsNullOrEmpty(r.Provider))
                    FetchCapability(r.Provider);
            return rows;
        }

        List<AccountRow> _accountsRuntime = new List<AccountRow>();

        static void ApplyRuntimeIdentity(AccountRow cur, Dictionary<string, object> o)
        {
            if (cur == null || o == null) return;
            // Plane A. Written ONLY from the engine's configured_identity, which
            // the engine reads out of the registry. In a complete status/usage
            // descriptor, an absent binding means it was cleared. A partial
            // legacy observation cannot clear it, and the runtime identity is
            // NEVER adopted as a binding. That single
            // rule is the whole of the T-264 identity repair -- the running IDE
            // session is evidence about a LOGIN, never evidence about which
            // account the slot is configured for.
            var ci = AsDict(o, "configured_identity");
            string cfgFp = AsText(ci, "fingerprint");
            string schema = AsText(o, "schema");
            if (o.ContainsKey("configured_identity") || schema == "sai.accounts/status/1"
                || schema == "sai.accounts/usage/1")
            {
                cur.ConfiguredIdentityFp = cfgFp;
                cur.ConfiguredIdentityLabel = cfgFp.Length > 0 ? AsText(ci, "account_label") : "";
            }

            // Plane B, the observation.
            var pi = AsDict(AsDict(o, "credential"), "provider_identity");
            bool verified = pi != null && object.Equals(pi.ContainsKey("verified") ? pi["verified"] : null, true);
            cur.RuntimeIdentityFp = verified ? AsText(pi, "fingerprint") : "";
            cur.RuntimeIdentityLabel = cur.RuntimeIdentityFp.Length > 0 ? AsText(pi, "account_label") : "";
            cur.IdentitySeen = true;

            SyncCanonicalAlias(cur);
        }

        // IdentityFp / IdentityLabel are the canonical plane under their old
        // names. One place decides what they mean, so no caller can reintroduce a
        // runtime fallback by accident.
        static void SyncCanonicalAlias(AccountRow cur)
        {
            if (cur == null) return;
            cur.IdentityFp = cur.ConfiguredIdentityFp;
            cur.IdentityLabel = cur.ConfiguredIdentityFp.Length > 0 ? cur.ConfiguredIdentityLabel : "";
        }

        // One plane for the copied diagnostics: the fingerprint, the address, and
        // an explicit (unproven) when neither exists -- "unproven" must never read
        // as "no" or as "signed out". The fingerprint comes first so the line
        // still starts with the machine-readable fact.
        static string IdentityDiag(string fp, string label)
        {
            if (fp.Length == 0) return label.Length > 0 ? label + " (unproven)" : "(unproven)";
            return label.Length > 0 ? fp + " " + label : fp;
        }

        // The only writer of _accounts, always on the UI thread, so a paint can
        // never read a half-replaced list.
        void ApplyAccounts(List<AccountRow> rows)
        {
            _accounts = rows;
            if (rows == null) return;
            foreach (AccountRow r in rows)
            {
                // A reload is a registry re-read, not a context re-probe. Keep
                // what the status poll already learned, or Stop context blinks
                // out of the UI every time a rename repaints the list.
                AccountRow prev = _accountsRuntime.Find(x => x.Id == r.Id);
                if (prev == null) { SyncCanonicalAlias(r); continue; }
                r.ContextState = prev.ContextState;
                r.AuthState = prev.AuthState;
                r.QuotaState = prev.QuotaState;
                // The canonical plane is engine-authoritative: this list reply IS
                // the registry, so it is deliberately NOT carried forward from the
                // previous row. Carrying it forward is exactly how a cached -- and
                // possibly runtime-derived -- value survives a Refresh. The runtime
                // plane is a probe result, so it is kept like every other probe
                // result above; it just may never become canonical by being kept.
                r.RuntimeIdentityFp = prev.RuntimeIdentityFp;
                r.RuntimeIdentityLabel = prev.RuntimeIdentityLabel;
                r.IdentitySeen = prev.IdentitySeen;
                SyncCanonicalAlias(r);
            }
            _accountsRuntime = new List<AccountRow>(rows);
            MarkSharedIdentities(rows);
            if (_selectedAccount == null || !_accounts.Exists(r => r.Id == _selectedAccount))
                _selectedAccount = _accounts.Count > 0 ? _accounts[0].Id : null;
        }

        /// Two slots whose CANONICAL console fingerprints match are one human in
        /// two Windows contexts -- and, more to the point, one quota pool in two
        /// rows. Two ordinary READY rows say nothing about that, which is exactly
        /// how a second slot comes to look like a second capacity when it is not
        /// one.
        ///
        /// The comparison is on the canonical fingerprint alone (Plane A). The
        /// IDE plane is not consulted: an Antigravity context signed into
        /// somebody else's account is a login fact and says nothing about whose
        /// quota this slot draws on. Labels are for people to read: two addresses
        /// can be one human and a renamed address is still one human, so keying
        /// on the label would both miss real duplicates and invent false ones.
        /// An unproven canonical identity shares with nobody, because there is
        /// nothing to compare.
        static void MarkSharedIdentities(List<AccountRow> rows)
        {
            if (rows == null) return;
            var groups = new Dictionary<string, List<AccountRow>>(StringComparer.OrdinalIgnoreCase);
            foreach (AccountRow r in rows)
            {
                if (r == null) continue;
                r.IdentitySharedWith = "";
                if (r.ConfiguredIdentityFp.Length == 0 || string.IsNullOrEmpty(r.Provider)) continue;
                string identityKey = r.Provider + "|" + r.ConfiguredIdentityFp;
                List<AccountRow> g;
                if (!groups.TryGetValue(identityKey, out g)) { g = new List<AccountRow>(); groups[identityKey] = g; }
                g.Add(r);
            }
            foreach (List<AccountRow> g in groups.Values)
            {
                if (g.Count < 2) continue;
                foreach (AccountRow r in g)
                {
                    var names = new List<string>();
                    foreach (AccountRow other in g)
                        if (other.Id != r.Id && other.Compact.Length > 0) names.Add(other.Compact);
                    r.IdentitySharedWith = string.Join(", ", names.ToArray());
                }
            }
        }

        void LoadAccounts()
        {
            ApplyAccounts(FetchAccounts());
        }

        // Worker-side reload: the engine call stays on the worker, only the
        // swap is marshalled.
        // Returns the freshly fetched rows. The UI apply is marshalled, so a caller on
        // the worker thread that needs the new state (rather than the old list)
        // must read what this returns.
        List<AccountRow> LoadAccountsAsync(bool tracked = false)
        {
            List<AccountRow> rows = FetchAccounts(tracked);
            TryBeginInvoke(delegate { ApplyAccounts(rows); });
            return rows;
        }

        static string AsText(Dictionary<string, object> o, string key)
        {
            object v;
            return o != null && o.TryGetValue(key, out v) && v != null ? v.ToString() : "";
        }

        // A nested object, or null. The provider fingerprint lives two levels down
        // (credential.provider_identity.fingerprint) and is the only thing that
        // says whose Google account a slot really holds, so it is worth naming
        // rather than leaving to a manual CLI run.
        static Dictionary<string, object> AsDict(Dictionary<string, object> o, string key)
        {
            object v;
            if (o != null && o.TryGetValue(key, out v))
            {
                var d = v as Dictionary<string, object>;
                if (d != null) return d;
            }
            return null;
        }

        AccountRow SelectedAccount()
        {
            return _accounts != null ? _accounts.Find(r => r.Id == _selectedAccount) : null;
        }

        // One CLI mutation + reload; the reply's status is the operator note.
        // Dispatched, never inline: a rename also re-syncs the Explorer menu,
        // which is two bounded children and up to 40s of wall clock. Freezing
        // a painted WinForms window for that long is its own bug.
        void AccountsRun(string verb, string arg)
        {
            AccountRow row = SelectedAccount();
            if (row == null) { _accountsNote = "no account selected"; Refresh(); return; }
            string line = verb + " \"" + row.Compact + "\" " + (arg ?? "");
            DispatchAccounts(delegate {
                CliRun r = AccountsExec(line, 20000);
                string note = r.Ok ? r.Say : "FAILED: " + (r.Say.Length > 0 ? r.Say : r.Problem);
                LoadAccountsAsync();
                if (verb == "set-state" || verb == "set-hidden" || verb == "rename")
                    note = note + "  |  " + SyncConsolesMenu();
                Note(note);
            });
        }

        // Canonical account-order mutations (T-264). Always keyed by immutable
        // account_id resolved on the UI thread before dispatch, so generated
        // compact labels or a concurrent refresh cannot retarget another slot.
        void AccountsMoveUp()
        {
            if (_accountsBusy || _accounts == null || _accounts.Count < 2) return;
            int idx = _accounts.FindIndex(r => r.Id == _selectedAccount);
            if (idx <= 0) return;
            string id = _accounts[idx].Id;
            ExecuteAccountMove(id, "move \"" + id + "\" up");
        }

        void AccountsMoveDown()
        {
            if (_accountsBusy || _accounts == null || _accounts.Count < 2) return;
            int idx = _accounts.FindIndex(r => r.Id == _selectedAccount);
            if (idx < 0 || idx >= _accounts.Count - 1) return;
            string id = _accounts[idx].Id;
            ExecuteAccountMove(id, "move \"" + id + "\" down");
        }

        void ExecuteAccountMove(string movedAccountId, string cliArgs)
        {
            if (string.IsNullOrEmpty(movedAccountId)) return;
            DispatchAccounts(delegate {
                CliRun r = AccountsExec(cliArgs, 20000);
                string note = r.Ok ? r.Say : "FAILED: " + (r.Say.Length > 0 ? r.Say : r.Problem);
                List<AccountRow> rows = FetchAccounts();
                TryBeginInvoke(delegate {
                    if (rows != null && rows.Exists(x => x.Id == movedAccountId))
                        _selectedAccount = movedAccountId;
                    ApplyAccounts(rows);
                });
                if (r.Ok && r.Say != "noop")
                    note = note + "  |  " + SyncConsolesMenu();
                Note(note);
            });
        }

        // ------------------- non-blocking plumbing for this tab -------------------
        string _accountsWorkdir = null;   // the folder chosen for this account console
        bool _accountsBusy = false;       // one worker at a time
        bool _accountsPolling = false;
        DateTime _accountsPollAt = DateTime.MinValue;
        Dictionary<string, bool> _providerCaps = new Dictionary<string, bool>();

        // Every long action goes through here: refuse to double-start, say what
        // is happening, run off the UI thread, come back to the UI thread to
        // update the note.
        void DispatchAccounts(Action work)
        {
            if (_accountsBusy) { Note("busy -- an account action is already running."); return; }
            _accountsBusy = true;
            ThreadPool.QueueUserWorkItem(delegate {
                try { work(); }
                catch (Exception ex) { Note("failed: " + ex.Message); }
                finally
                {
                    // The action may have changed an account's state: read the
                    // new truth now instead of waiting out the poll cadence.
                    RequestStatusRefresh();
                    TryBeginInvoke(delegate { _accountsBusy = false; Refresh(); });
                }
            });
        }

        void Note(string text)
        {
            TryBeginInvoke(delegate { _accountsNote = text ?? ""; Refresh(); });
        }

        // One grant failure, named by what actually failed. The helper's status
        // is a closed machine enum and is mapped here by value, never by
        // matching text inside an English message, so a reworded exception can
        // never turn into a different diagnosis. Each case says what the
        // operator can act on, and nothing suggests a fallback folder: the
        // selected project either got the one ACE or it did not.
        static string WorkdirGrantReason(string label, string workdir, Dictionary<string, object> env)
        {
            string status = env != null && env.ContainsKey("status") ? AsText(env, "status") : "";
            switch (status)
            {
                case "ACL_READ_FAILED":
                    return "WORKDIR ACL read failed for " + label + ".";
                case "ACL_MUTATION_FAILED":
                    return "WORKDIR ACL update failed for " + label + ".";
                case "EFFECTIVE_ACCESS_DENIED":
                    return "Existing ACL policy denies " + label + " access to \"" + workdir + "\".";
                case "WORKDIR_REPARSE_REFUSED":
                    return "\"" + workdir + "\" is a link (junction/symlink) and was not modified.";
                case "WORKDIR_TARGET_REFUSED":
                    return "\"" + workdir + "\" is not an allowed grant target.";
                case "WORKDIR_NOT_FOUND":
                    return "Project folder not found: \"" + workdir + "\".";
                case "WORKDIR_NOT_READABLE":
                    return "Project folder could not be inspected: \"" + workdir + "\".";
                case "WORKDIR_INVALID":
                    return "Project folder is not a valid path: \"" + workdir + "\".";
                case "WORKDIR_REQUIRED":
                    return "No project folder was given for " + label + ".";
                case "HELPER_UNAVAILABLE":
                    return "The SAI Accounts helper service did not answer.";
                case "CONTEXT_IDENTITY_MISSING":
                    return label + " has no managed Windows identity to grant to.";
                case "BACKEND_UNSUPPORTED":
                    return label + " does not use a managed Windows identity.";
                case "LIFECYCLE_FORBIDDEN":
                    return label + " is not enabled, so it was not granted access.";
                case "ACCOUNT_NOT_FOUND":
                    return "Account " + label + " is no longer known.";
            }
            return "WORKDIR_ACCESS_DENIED: " + label + " could not access \"" + workdir + "\""
                + (status.Length > 0 ? " (" + status + ")" : "") + ".";
        }

        // ------------------- workdir: explicit, validated, never guessed -------------------
        // A launch is `sai-accounts launch <id> --workdir <dir>`, and the engine
        // answers WORKDIR_UNAVAILABLE for anything it cannot open. The tempting
        // shortcut is to hand it System32, the profile, or the toolkit root so
        // the button does something. That is worse than failing: the console
        // would open in a folder the operator did not choose and never know
        // which one it was. So there are only three sources, in order, and the
        // last one is a question, not a default.
        static bool WorkdirUsable(string dir)
        {
            if (string.IsNullOrEmpty(dir)) return false;
            try
            {
                // Directory.Exists swallows the exception and answers false,
                // which cannot tell "missing" from "denied". Reading the
                // directory can, and an unreadable workdir must fail closed.
                Directory.GetFileSystemEntries(dir);
                return true;
            }
            catch { return false; }
        }

        string CurrentWorkdir()
        {
            return CurrentWorkdirFor(_selectedAccount);
        }

        string CurrentWorkdirFor(string accountId)
        {
            if (!string.IsNullOrEmpty(accountId))
            {
                string acctWd = S.GetAccountWorkDir(accountId);
                if (WorkdirUsable(acctWd)) { _accountsWorkdir = acctWd; return _accountsWorkdir; }
            }
            if (WorkdirUsable(_accountsWorkdir)) return _accountsWorkdir;
            if (WorkdirUsable(S.LastWorkDir)) { _accountsWorkdir = S.LastWorkDir; return _accountsWorkdir; }
            return null;
        }

        void RememberWorkdir(string dir)
        {
            RememberWorkdirFor(_selectedAccount, dir);
        }

        void RememberWorkdirFor(string accountId, string dir)
        {
            _accountsWorkdir = dir;
            S.SetAccountWorkDir(accountId, dir);
            S.Save();
        }

        // Returns null when there is nothing usable AND the operator cancelled
        // the picker. Callers must treat null as "do not launch".
        string ChooseWorkdir()
        {
            string have = CurrentWorkdir();
            using (var fb = new FolderBrowserDialog())
            {
                fb.Description = AccountsLoc.T("ConsoleChooseWorkdir");
                fb.SelectedPath = have != null ? have : S.RootPath;
                if (fb.ShowDialog(this) != DialogResult.OK) return null;
                string picked = fb.SelectedPath;
                if (!WorkdirUsable(picked))
                {
                    _accountsNote = "WORKDIR_UNAVAILABLE: that folder cannot be read. Pick another.";
                    Refresh();
                    return null;
                }
                RememberWorkdir(picked);
                return picked;
            }
        }

        void OpenFolderAccessManager(string initialAccountId = null)
        {
            var labels = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var backends = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var sids = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

            if (_accounts != null)
            {
                foreach (var acc in _accounts)
                {
                    if (string.IsNullOrEmpty(acc.Id)) continue;
                    labels[acc.Id] = !string.IsNullOrEmpty(acc.Compact) ? acc.Compact : acc.Id;
                    backends[acc.Id] = acc.Backend ?? "";
                    sids[acc.Id] = acc.Sid ?? "";
                }
            }

            string initId = initialAccountId ?? _selectedAccount;
            if (string.IsNullOrEmpty(initId) && _accounts != null && _accounts.Count > 0)
                initId = _accounts[0].Id;

            string accExe = AccountsExe();
            string pyExe = Toolchain.FindOnPath("python.exe") ?? Toolchain.FindOnPath("pythonw.exe");
            string scrDir = Path.Combine(S.RootPath, "Scripts", "accounts");

            using (var dlg = new Saituls.FolderAutomation.FolderAccessManagerDialog(
                labels,
                backends,
                sids,
                initId,
                accExe,
                pyExe,
                scrDir,
                id => S.GetAccountFolders(id),
                (id, flds) => S.SaveAccountFolders(id, flds)))
            {
                dlg.ShowDialog(this);
            }
            Refresh();
        }

        // Explorer cascade auto-sync. A rename, a hide, a freeze or an archive
        // changes what the cascade should be listing, and a stale menu is a
        // wrong menu: it keeps offering an account that is globally disabled
        // and never offers a new one. The installer writes per-user HKCU and
        // never elevates, so this is a bounded call; if it fails the note says
        // so and the account change itself stands.
        // True once a sync has failed and not yet succeeded again. The Accounts
        // tab offers Repair while it is set, so a stale cascade is never left
        // silently in place after an account mutation.
        bool _menuSyncFailed = false;
        const string MenuSynced = "menu synced";

        static string PowerShellExe()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                @"WindowsPowerShell\v1.0\powershell.exe");
        }

        // Does the installed cascade still match what this machine should be
        // offering? The installer's own -VerifyOnly comparison answers that in
        // one process, so "in sync" has exactly one definition and this shell
        // never re-implements the menu it is checking.
        bool ConsolesMenuDrift()
        {
            string script = Path.Combine(S.RootPath, Path.Combine("Installers", "INSTALL_CONSOLES_MENU.PS1"));
            if (!File.Exists(script)) return false;
            CliRun r = RunBounded(PowerShellExe(),
                "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + script + "\" -VerifyOnly", 20000);
            return !r.Ok;
        }

        // Startup pass, on a worker thread: verify first, repair only on drift.
        // A cascade that already matches costs one short child and changes
        // nothing, which is what keeps this safe to run every time the shell
        // starts.
        void ReconcileConsolesMenuOnStartup()
        {
            try
            {
                if (!ConsolesMenuDrift()) { TryBeginInvoke(delegate { _menuState = "ok"; }); return; }
                string note = SyncConsolesMenu();
                TryBeginInvoke(delegate { _menuState = note == MenuSynced ? "ok" : "failed"; });
                if (note != MenuSynced)
                    Note("Explorer menu drift detected at startup. " + note);
            }
            catch (Exception ex)
            {
                _menuSyncFailed = true;
                Note("EXPLORER MENU UPDATE FAILED: " + ex.Message);
            }
        }

        string InstallConsolesMenuOnce()
        {
            string script = Path.Combine(S.RootPath, Path.Combine("Installers", "INSTALL_CONSOLES_MENU.PS1"));
            if (!File.Exists(script)) return "installer missing";
            CliRun r = RunBounded(PowerShellExe(),
                "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"", 20000);
            if (r.TimedOut) return "menu sync timed out";
            if (!r.Ok)
            {
                string detail = r.Stderr.Trim();
                if (detail.Length == 0) detail = r.Stdout.Trim();
                return "menu sync failed: " + (detail.Length > 0 ? detail : r.Problem);
            }
            return MenuSynced;
        }

        // One mutation, one menu. A failure is REPORTED as a failure -- the
        // account change still stands, but the operator is told the Explorer
        // projection did not follow it and is offered the repair, instead of
        // being left with a menu that quietly offers the wrong accounts.
        string SyncConsolesMenu()
        {
            string first = InstallConsolesMenuOnce();
            if (first == MenuSynced) { _menuSyncFailed = false; return first; }
            // The installer holds a machine-wide mutation mutex, and another
            // SAITULS action can hold it for a moment; one retry is honest.
            Thread.Sleep(400);
            string second = InstallConsolesMenuOnce();
            if (second == MenuSynced) { _menuSyncFailed = false; return second; }
            _menuSyncFailed = true;
            return "EXPLORER MENU UPDATE FAILED: " + second +
                "  -- use Explorer menus > Install or repair";
        }

        // The engine answers with a one-line JSON envelope. Showing it raw
        // would put 400 characters of payload in a 640px note; show the status
        // word, and the error when there is one.
        static string AccountsLastLine(string json)
        {
            if (json == null) return "command failed (engine missing or timed out)";
            string last = LastEnvelopeLine(json);
            if (last.Length == 0) return "";
            if (!last.StartsWith("{")) return last;
            try
            {
                Dictionary<string, object> o =
                    new System.Web.Script.Serialization.JavaScriptSerializer()
                        .Deserialize<Dictionary<string, object>>(last);
                string status = AsText(o, "status");
                string error = AsText(o, "error");
                // Never fall back to the payload itself. An envelope with no status
                // word is reported as no status word, and the caller substitutes the
                // real reason; a truncated or unparseable envelope says so plainly.
                if (!string.IsNullOrEmpty(status)) return string.IsNullOrEmpty(error) ? status : status + ": " + error;
                if (!string.IsNullOrEmpty(error)) return "failed: " + error;
                return "";
            }
            catch { return "engine returned an unreadable response"; }
        }

        // One top-level string field out of the last envelope on stdout. Used where a
        // specific field is the answer (context_state), not the status word.
        static string AccountsField(string json, string key)
        {
            if (json == null) return "";
            string last = LastEnvelopeLine(json);
            if (!last.StartsWith("{")) return "";
            try
            {
                var o = new System.Web.Script.Serialization.JavaScriptSerializer()
                    .Deserialize<Dictionary<string, object>>(last);
                return AsText(o, key);
            }
            catch { return ""; }
        }

        // What the engine says the context is RIGHT NOW. `list` carries no
        // context_state -- the row's Context field is the managed user name, not
        // the state -- so a post-Connect refresh has to ask `status` or it can
        // only ever echo what the mutation already claimed.
        string ProbeContextState(string compact)
        {
            CliRun s = AccountsExec("status \"" + compact + "\"", 15000);
            return s.Ok ? AccountsField(s.Stdout, "context_state") : "";
        }

        // ====================================================================
        // V2 ACCOUNTS UI — helper methods
        // ====================================================================

        // Derive one human-readable state from the underlying runtime fields.
        // Priority: ARCHIVED > FROZEN > DISABLED > AUTH_REQUIRED > ERROR > OFFLINE > READY.
        static string HumanState(AccountRow row)
        {
            if (row == null) return "UNKNOWN";
            if (row.State == "ARCHIVED") return "ARCHIVED";
            if (row.State == "FROZEN") return "FROZEN";
            if (row.State == "DISABLED") return "DISABLED";
            if (row.AuthState == "AUTH_REQUIRED") return "SIGN IN";
            if (row.AuthState == "ERROR" || row.ContextState == "ERROR") return "ERROR";
            if (row.ContextState == "OFFLINE") return "OFFLINE";
            if (row.ContextState == "ONLINE") return "READY";
            // Not polled yet — show the operational state
            if (row.State == "ENABLED") return "READY";
            return "UNKNOWN";
        }

        static Color StateColor(string humanState)
        {
            switch (humanState)
            {
                case "READY": return Palette.LINK;
                case "OFFLINE": return Color.FromArgb(190, 160, 80);
                case "SIGN IN": return Color.FromArgb(210, 170, 60);
                case "ERROR": return Palette.DANGERTXT;
                case "DISABLED": return Palette.MUTED;
                case "FROZEN": return Color.FromArgb(100, 140, 170);
                case "ARCHIVED": return Palette.MUTED;
                default: return Palette.TEXT2;
            }
        }

        // The one primary CTA label + action for this account's state.
        // Returns (label, action). label null = no primary action.
        string PrimaryCTALabel(AccountRow row)
        {
            if (row == null) return null;
            string hs = HumanState(row);
            switch (hs)
            {
                case "READY":
                case "OFFLINE": return AccountsLoc.T("OpenConsole");
                case "SIGN IN": return AccountsLoc.T("SignIn");
                case "DISABLED": return AccountsLoc.T("Enable");
                case "FROZEN": return AccountsLoc.T("Unfreeze");
                case "ARCHIVED": return AccountsLoc.T("Restore");
                default: return null;
            }
        }

        Action PrimaryCTAAction(AccountRow row)
        {
            if (row == null) return null;
            string hs = HumanState(row);
            switch (hs)
            {
                case "READY":
                case "OFFLINE": return () => OpenConsole();
                case "SIGN IN": return () => SignInSelectedAccount();
                case "DISABLED": return () => AccountsRun("set-state", "ENABLED");
                case "FROZEN": return () => AccountsRun("set-state", "ENABLED");
                case "ARCHIVED": return () => AccountsRun("set-state", "DISABLED");
                default: return null;
            }
        }

        // Human auth label
        static string HumanAuth(AccountRow row)
        {
            if (row == null) return "—";
            switch (row.AuthState)
            {
                case "AUTHENTICATED": return AccountsLoc.T("Authenticated");
                case "AUTH_REQUIRED": return AccountsLoc.T("AuthRequired");
                case "ERROR": return AccountsLoc.T("Error");
                case "UNKNOWN": return "—";
                default: return row.AuthState.Length > 0 ? row.AuthState : "—";
            }
        }


        // Draw the V2 detail pane content. Returns total content height for scroll calc.
        int DrawAccountDetailPane(Graphics g, int px, int py, int pw, int ph, AccountRow sel)
        {
            int oy = py - _acctDetailScrollY;
            int startY = oy;

            if (sel == null)
            {
                DrawText(g, AccountsLoc.T("SelectAccount"), px + 8, oy + 20, Palette.MUTED, 11);
                return 60;
            }

            int indent = px + 8;
            // The value column starts after the widest label in THIS language,
            // so a longer translation never runs into its value.
            int labelCol = 110;
            foreach (string k in new[] { "Status", "Authentication", "GoogleAccount", "ConsoleAccount", "IdeAccount", "Execution", "Connection", "Visibility", "Quota" })
                labelCol = Math.Max(labelCol, TextW(g, AccountsLoc.T(k), 9) + 12);
            int valueX = indent + Math.Min(labelCol, Math.Max(110, pw / 2));

            // Header: display name + provider/origin
            DrawText(g, sel.Name, indent, oy, Palette.TEXT, 12, true);
            oy += 18;
            string subtitle = sel.Provider;
            if (subtitle.Length > 0)
                subtitle = sel.Provider.Substring(0, 1).ToUpper() + sel.Provider.Substring(1);
            // Origin marker as text. It used to say "Shared" for every Antigravity
            // account and "Local" for the rest, which put "Shared" beside
            // "Connection: Local" on the very same panel: two words about two
            // different dimensions (registry origin vs execution) reading as one
            // contradiction. The subtitle now names the dimension that decides
            // whether Connect exists at all.
            subtitle += " · " + (sel.Hidden ? AccountsLoc.T("Hidden") : ExecutionLabel(sel));
            DrawText(g, Fit(g, subtitle, pw - 20, 9), indent, oy, Palette.TEXT2, 9);
            oy += 20;

            // Separator
            using (var p = new Pen(Palette.BMUTED)) g.DrawLine(p, indent, oy, indent + pw - 20, oy);
            oy += 8;

            // Status fields
            string hs = HumanState(sel);
            Color hsColor = StateColor(hs);
            DrawText(g, AccountsLoc.T("Status"), indent, oy, Palette.MUTED, 9, true);
            DrawText(g, AccountsLoc.T(hs), valueX, oy, hsColor, 9, true);
            oy += 16;

            DrawText(g, AccountsLoc.T("Authentication"), indent, oy, Palette.MUTED, 9);
            DrawText(g, Fit(g, HumanAuth(sel), indent + pw - 20 - valueX, 9), valueX, oy, Palette.TEXT, 9);
            if (sel.AuthState == "AUTH_REQUIRED")
            {
                int signW = 72;
                int signX = Math.Max(valueX + TextW(g, HumanAuth(sel) + " ", 9), indent + pw - 20 - signW);
                var signR = new Rectangle(signX, oy - 2, signW, 18);
                Buttons.Add(new ButtonDef { R = signR, A = () => SignInSelectedAccount() });
                DrawButton(g, signR, AccountsLoc.T("SignIn"), true, 8);
            }
            oy += 16;

            // WHICH accounts, on two lines, as two independent facts. This is
            // the row that answers the question the whole panel exists for:
            // authentication says a credential is present and never says whose.
            //
            // The console account is the quota-bearing identity of the slot; the
            // IDE account is what the running Antigravity context is signed into
            // right now. They may agree and they may differ, and a difference is
            // NORMAL -- so both are printed plainly and neither is coloured as a
            // problem. An operator should never have to open a console to learn
            // which account they are about to use.
            string consoleDisp = sel.ConfiguredIdentityLabel.Length > 0
                ? sel.ConfiguredIdentityLabel
                : (sel.ConfiguredIdentityFp.Length > 0 ? sel.ConfiguredIdentityFp : AccountsLoc.T("Unproven"));
            string ideDisp = sel.RuntimeIdentityLabel.Length > 0
                ? sel.RuntimeIdentityLabel
                : (sel.RuntimeIdentityFp.Length > 0 ? sel.RuntimeIdentityFp : AccountsLoc.T("Unproven"));
            bool consoleKnown = sel.ConfiguredIdentityFp.Length > 0;
            bool ideKnown = sel.RuntimeIdentityFp.Length > 0;
            bool samePerson = consoleKnown && ideKnown
                && string.Equals(sel.ConfiguredIdentityFp, sel.RuntimeIdentityFp, StringComparison.OrdinalIgnoreCase);
            if (samePerson || !consoleKnown)
            {
                // One person, nothing read yet, or only the IDE sign-in known:
                // one line. Repeating the same address twice used to take half
                // the panel. An IDE-only identity is labelled as such -- it is
                // what the IDE is signed into, not what the slot is bound to.
                string one = consoleKnown ? consoleDisp
                    : ideKnown ? ideDisp + "  (" + AccountsLoc.T("IdeAccount") + ")"
                    : AccountsLoc.T(sel.IdentitySeen ? "Unproven" : "NotCheckedYet");
                DrawText(g, AccountsLoc.T("GoogleAccount"), indent, oy, Palette.MUTED, 9);
                DrawText(g, Fit(g, one, pw - 20 - (valueX - indent), 9), valueX, oy, Palette.TEXT, 9);
                oy += 16;
            }
            else
            {
                // Two different accounts is a FACT, not a fault: both are shown
                // plainly with one neutral sentence saying which one counts.
                DrawText(g, AccountsLoc.T("ConsoleAccount"), indent, oy, Palette.MUTED, 9);
                DrawText(g, Fit(g, consoleDisp, pw - 20 - (valueX - indent), 9), valueX, oy, Palette.TEXT, 9);
                oy += 16;
                DrawText(g, AccountsLoc.T("IdeAccount"), indent, oy, Palette.MUTED, 9);
                DrawText(g, Fit(g, ideDisp, pw - 20 - (valueX - indent), 9), valueX, oy, Palette.TEXT2, 9);
                oy += consoleKnown && ideKnown ? 14 : 16;
                if (consoleKnown && ideKnown)
                {
                    DrawText(g, Fit(g, AccountsLoc.T("IdeDiffers", ideDisp), pw - 20, 8), indent, oy, Palette.MUTED, 8);
                    oy += 14;
                }
            }

            // The ONE warning this model has. Two slots whose CANONICAL console
            // accounts are one provider identity are one quota pool, and nothing
            // else on the screen would say so. It is stated plainly on BOTH rows
            // and it names the quota plane, because "same Google account" would
            // now be ambiguous between the two planes.
            if (sel.IdentitySharedWith.Length > 0)
            {
                DrawText(g, AccountsLoc.T("Quota"), indent, oy, Palette.MUTED, 9);
                DrawText(g, AccountsLoc.T("QuotaShared", sel.IdentitySharedWith), valueX, oy, Palette.DANGERTXT, 9);
                oy += 16;
            }

            // Execution first, for BOTH kinds. It is the fact every other row is
            // derived from, and printing it above Connection is what lets the
            // panel answer "do I need to do anything?" without the operator
            // reading the account-type vocabulary.
            DrawText(g, AccountsLoc.T("Execution"), indent, oy, Palette.MUTED, 9);
            DrawText(g, Fit(g, ExecutionLabel(sel), indent + pw - 20 - valueX, 9), valueX, oy, Palette.TEXT, 9);
            oy += 16;

            // T-288: the last measured SAIPEN readiness, only once measured.
            if (sel.SaipenVerdict.Length > 0)
            {
                string sv = sel.SaipenVerdict + (sel.SaipenDetail.Length > 0 ? " -- " + sel.SaipenDetail : "");
                DrawText(g, "SAIPEN", indent, oy, Palette.MUTED, 9);
                DrawText(g, Fit(g, sv, indent + pw - 20 - valueX, 9), valueX, oy,
                         sel.SaipenVerdict == "READY" ? Palette.LINK : Palette.DANGERTXT, 9);
                oy += 16;
            }

            if (ContextSecondary(sel))
            {
                bool online = sel.ContextState == "ONLINE";
                DrawText(g, AccountsLoc.T("Connection"), indent, oy, Palette.MUTED, 9);
                DrawText(g, online ? AccountsLoc.T("Ready") : AccountsLoc.T("Offline"), valueX, oy, online ? Palette.LINK : Palette.MUTED, 9);
                int connW = 74;
                int connX = Math.Max(valueX + TextW(g, (online ? AccountsLoc.T("Ready") : AccountsLoc.T("Offline")) + "   ", 9), indent + pw - 20 - connW);
                var connR = new Rectangle(connX, oy - 2, connW, 18);
                if (online)
                {
                    Buttons.Add(new ButtonDef { R = connR, A = () => AccountsContext("disconnect") });
                    DrawButton(g, connR, AccountsLoc.T("Disconnect"), false, 8);
                }
                else
                {
                    Buttons.Add(new ButtonDef { R = connR, A = () => AccountsContext("connect") });
                    DrawButton(g, connR, AccountsLoc.T("Connect"), true, 8);
                }
                oy += 16;
                // The intent, shown separately from the runtime on the line
                // above. "Offline" alone would be ambiguous in exactly the
                // case that caused this work: the operator cannot tell a
                // broker that crashed from an account they turned off, and
                // that ambiguity is why Connect became a ritual. The reason
                // below closes the gap: it names which of the two it is.
                DrawText(g, "Auto-connect", indent, oy, Palette.MUTED, 9);
                DrawText(g, sel.AutoConnect ? "ON" : "OFF", valueX, oy,
                         sel.AutoConnect ? Palette.LINK : Palette.DANGERTXT, 9);
                oy += 14;
                string acNote = AutoConnectNote(sel);
                if (acNote.Length > 0)
                {
                    DrawText(g, Fit(g, acNote, indent + pw - 20 - valueX, 8), valueX, oy, Palette.MUTED, 8);
                    oy += 14;
                }
                oy += 2;
            }
            else
            {
                // Not "Local": that word described where a registry entry came
                // from, and reading it beside a Windows-session statement
                // invited exactly the wrong inference. There is no broker here
                // to be unavailable, so the honest answer names the fact rather
                // than inventing a status the account cannot have.
                DrawText(g, AccountsLoc.T("Connection"), indent, oy, Palette.MUTED, 9);
                DrawText(g, Fit(g, AccountsLoc.T("AlwaysAvailable"), indent + pw - 20 - valueX, 9), valueX, oy, Palette.TEXT, 9);
                oy += 16;
            }

            if (sel.Hidden)
            {
                DrawText(g, AccountsLoc.T("Visibility"), indent, oy, Palette.MUTED, 9);
                DrawText(g, AccountsLoc.T("Hidden"), valueX, oy, Palette.DANGERTXT, 9);
                oy += 16;
            }
            oy += 4;

            // Real usage windows from the limits source. The account's quota
            // STATE is never turned into a percentage here (T-266): no reading
            // reads "--" or names why.
            oy = DrawUsageSection(g, indent, oy, pw, sel);

            // Workdir
            using (var p = new Pen(Palette.BMUTED)) g.DrawLine(p, indent, oy, indent + pw - 20, oy);
            oy += 6;
            DrawText(g, AccountsLoc.T("Workdir"), indent, oy, Palette.MUTED, 9, true);
            oy += 16;
            string wd = CurrentWorkdir();
            if (wd != null)
            {
                string wdShort = wd;
                if (wdShort.Length > 36) wdShort = wdShort.Substring(0, 10) + "..." + wdShort.Substring(wdShort.Length - 23);
                DrawText(g, wdShort, indent, oy, Palette.TEXT2, 9);
                int chgX = indent + TextW(g, wdShort + "  ", 9);
                int chgW = 60;
                if (chgX + chgW > indent + pw - 20) chgX = indent + pw - 20 - chgW;
                var chgR = new Rectangle(chgX, oy - 2, chgW, 18);
                Buttons.Add(new ButtonDef { R = chgR, A = () => {
                    string picked = ChooseWorkdir();
                    if (picked != null) Note("workdir: " + picked);
                    Refresh();
                } });
                DrawButton(g, chgR, AccountsLoc.T("Change"), false, 8);
            }
            else
            {
                DrawText(g, AccountsLoc.T("NotSelected"), indent, oy, Palette.MUTED, 9);
                var chR = new Rectangle(indent + 80, oy - 2, 66, 18);
                Buttons.Add(new ButtonDef { R = chR, A = () => {
                    string picked = ChooseWorkdir();
                    if (picked != null) Note("workdir: " + picked);
                    Refresh();
                } });
                DrawButton(g, chR, AccountsLoc.T("Choose"), false, 8);
            }
            oy += 22;

            string mgrLabel = AccountsLoc.T("FolderAccessMgr");
            int mgrW = Math.Max(140, TextW(g, mgrLabel, 8) + 16);
            var mgrR = new Rectangle(indent, oy - 2, mgrW, 18);
            Buttons.Add(new ButtonDef { R = mgrR, A = () => OpenFolderAccessManager(sel != null ? sel.Id : null) });
            DrawButton(g, mgrR, mgrLabel, false, 8);
            oy += 22;

            // Primary CTA
            using (var p = new Pen(Palette.BMUTED)) g.DrawLine(p, indent, oy, indent + pw - 20, oy);
            oy += 8;
            string ctaLabel = PrimaryCTALabel(sel);
            Action ctaAction = PrimaryCTAAction(sel);
            if (ctaLabel != null && ctaAction != null)
            {
                int ctaW = Math.Min(148, Math.Max(TextW(g, ctaLabel, 10) + 24, 110));
                var ctaR = new Rectangle(indent, oy, ctaW, 26);
                Buttons.Add(new ButtonDef { R = ctaR, A = _accountsBusy ? null : ctaAction });
                // Draw primary style
                using (var bg = new SolidBrush(Palette.COMPARE))
                    g.FillRectangle(bg, ctaR.X + 2, ctaR.Y + 2, ctaR.Width - 4, ctaR.Height - 4);
                DrawBevel(g, ctaR, true);
                using (var br = new SolidBrush(Palette.LINK))
                    g.DrawString(_accountsBusy ? AccountsLoc.T("Working") : ctaLabel, F(10), br,
                        new RectangleF(ctaR.X + 2, ctaR.Y + 2, ctaR.Width - 4, ctaR.Height - 4), Centered);
                oy += 32;
                if (ctaLabel == AccountsLoc.T("OpenConsole"))
                {
                    DrawText(g, AccountsLoc.T("ConsoleShortcutsHint"), indent, oy, Palette.MUTED, 8);
                    oy += 16;
                }
            }

            // MANAGE section
            oy += 4;
            DrawText(g, AccountsLoc.T("Manage"), indent, oy, Palette.MUTED, 9, true);
            oy += 16;
            int mbx = indent;
            int mbh = 20, mgap = 4;
            int maxRowX = indent + pw - 20;

            // Switch Google account. First in Manage, and deliberately NOT under
            // Advanced: the ritual this replaces was open a console, type /logout,
            // wait, re-login, reopen Accounts. If the replacement is one click
            // behind a disclosure triangle the ritual only got longer.
            //
            // It is offered only where it means something -- a provider with a
            // known credential, already signed in. An account that is not
            // authenticated needs Sign in, not a switch, and offering both would
            // be two buttons for one job.
            if (CanSwitchProviderAccount(sel))
            {
                int swW = Math.Max(124, TextW(g, AccountsLoc.T("SwitchAccount"), 8) + 16);
                if (mbx + swW > maxRowX) { mbx = indent; oy += mbh + mgap; }
                var swR = new Rectangle(mbx, oy, swW, mbh);
                Buttons.Add(new ButtonDef { R = swR, A = () => SwitchProviderAccount(), Label = AccountsLoc.T("SwitchAccount") });
                DrawButton(g, swR, AccountsLoc.T("SwitchAccount"), false, 8);
                mbx += swW + mgap;
            }

            // Move Up / Move Down (deterministic keyboard-accessible fallback for row drag)
            {
                int selIdx = _accounts != null ? _accounts.FindIndex(x => x.Id == sel.Id) : -1;
                bool canMoveUp = !_accountsBusy && selIdx > 0;
                bool canMoveDown = !_accountsBusy && selIdx >= 0 && _accounts != null && selIdx < _accounts.Count - 1;

                string upLbl = AccountsLoc.T("MoveUp");
                int bwUp = Math.Max(64, TextW(g, upLbl, 8) + 14);
                if (mbx + bwUp > maxRowX) { mbx = indent; oy += mbh + mgap; }
                var rUp = new Rectangle(mbx, oy, bwUp, mbh);
                Buttons.Add(new ButtonDef { R = rUp, A = () => AccountsMoveUp(), Label = upLbl });
                using (var bg = new SolidBrush(Palette.RAISED))
                    g.FillRectangle(bg, rUp.X + 2, rUp.Y + 2, rUp.Width - 4, rUp.Height - 4);
                DrawBevel(g, rUp, true);
                using (var br = new SolidBrush(canMoveUp ? Palette.TEXT : Palette.MUTED))
                    g.DrawString(upLbl, F(8), br, new RectangleF(rUp.X + 2, rUp.Y + 2, rUp.Width - 4, rUp.Height - 4), Centered);
                mbx += bwUp + mgap;

                string dnLbl = AccountsLoc.T("MoveDown");
                int bwDn = Math.Max(74, TextW(g, dnLbl, 8) + 14);
                if (mbx + bwDn > maxRowX) { mbx = indent; oy += mbh + mgap; }
                var rDn = new Rectangle(mbx, oy, bwDn, mbh);
                Buttons.Add(new ButtonDef { R = rDn, A = () => AccountsMoveDown(), Label = dnLbl });
                using (var bg = new SolidBrush(Palette.RAISED))
                    g.FillRectangle(bg, rDn.X + 2, rDn.Y + 2, rDn.Width - 4, rDn.Height - 4);
                DrawBevel(g, rDn, true);
                using (var br = new SolidBrush(canMoveDown ? Palette.TEXT : Palette.MUTED))
                    g.DrawString(dnLbl, F(8), br, new RectangleF(rDn.X + 2, rDn.Y + 2, rDn.Width - 4, rDn.Height - 4), Centered);
                mbx += bwDn + mgap;
            }

            // Rename
            {
                int bw2 = Math.Max(62, TextW(g, AccountsLoc.T("Rename"), 8) + 14);
                if (mbx + bw2 > maxRowX) { mbx = indent; oy += mbh + mgap; }
                var r = new Rectangle(mbx, oy, bw2, mbh);
                Buttons.Add(new ButtonDef { R = r, A = () => AccountsRename() });
                DrawButton(g, r, AccountsLoc.T("Rename"), false, 8);
                mbx += bw2 + mgap;
            }

            // Hide/Unhide
            {
                string lbl = sel.Hidden ? AccountsLoc.T("Show") : AccountsLoc.T("Hide");
                int bw2 = Math.Max(50, TextW(g, lbl, 8) + 14);
                if (mbx + bw2 > maxRowX) { mbx = indent; oy += mbh + mgap; }
                var r = new Rectangle(mbx, oy, bw2, mbh);
                Buttons.Add(new ButtonDef { R = r, A = () => AccountsRun("set-hidden", sel.Hidden ? "false" : "true") });
                DrawButton(g, r, lbl, false, 8);
                mbx += bw2 + mgap;
            }

            // Disable/Enable (only if not already handled by CTA)
            if (sel.State != "DISABLED" && sel.State != "ARCHIVED")
            {
                int bw2 = Math.Max(56, TextW(g, AccountsLoc.T("Disable"), 8) + 14);
                if (mbx + bw2 > maxRowX) { mbx = indent; oy += mbh + mgap; }
                var r = new Rectangle(mbx, oy, bw2, mbh);
                Buttons.Add(new ButtonDef { R = r, A = () => AccountsRun("set-state", "DISABLED") });
                DrawButton(g, r, AccountsLoc.T("Disable"), false, 8);
                mbx += bw2 + mgap;
            }

            // Freeze/Unfreeze (only if not already handled by CTA)
            if (sel.State != "FROZEN" && sel.State != "ARCHIVED")
            {
                string lbl = sel.State == "FROZEN" ? AccountsLoc.T("Unfreeze") : AccountsLoc.T("Freeze");
                int bw2 = Math.Max(58, TextW(g, lbl, 8) + 14);
                if (mbx + bw2 > maxRowX) { mbx = indent; oy += mbh + mgap; }
                var r = new Rectangle(mbx, oy, bw2, mbh);
                Buttons.Add(new ButtonDef { R = r, A = () => AccountsRun("set-state",
                    sel.State == "FROZEN" ? "ENABLED" : "FROZEN") });
                DrawButton(g, r, lbl, false, 8);
                mbx += bw2 + mgap;
            }
            oy += mbh + 8;

            // Advanced ▸/▾
            string advLabel = (AccountsLoc.T("Advanced")) + (_acctAdvancedExpanded ? " \u25BE" : " \u25B8");
            int advTw = TextW(g, advLabel, 9) + 4;
            var advR = new Rectangle(indent, oy, advTw, 16);
            Buttons.Add(new ButtonDef { R = advR, A = () => { _acctAdvancedExpanded = !_acctAdvancedExpanded; Refresh(); } });
            DrawText(g, advLabel, indent, oy, Palette.TEXT2, 9, true);
            oy += 20;

            if (_acctAdvancedExpanded)
            {
                // Advanced actions
                mbx = indent + 8;
                int abh = 20;

                // Status details
                {
                    var r = new Rectangle(mbx, oy, 84, abh);
                    Buttons.Add(new ButtonDef { R = r, A = () => AccountsStatus() });
                    DrawButton(g, r, AccountsLoc.T("StatusDetails"), false, 8);
                    mbx += 88;
                }

                // Start/Stop context
                if (ContextSecondary(sel))
                {
                    bool online = sel.ContextState == "ONLINE";
                    string cLabel = online ? AccountsLoc.T("StopContext") : AccountsLoc.T("StartContext");
                    Action cAction = online ? (Action)(() => AccountsContext("disconnect")) : () => AccountsContext("connect");
                    var r = new Rectangle(mbx, oy, 84, abh);
                    Buttons.Add(new ButtonDef { R = r, A = cAction });
                    DrawButton(g, r, cLabel, false, 8);
                    mbx += 88;
                }
                oy += abh + 4;
                mbx = indent + 8;

                // Re-authenticate
                if (sel.AuthState == "AUTH_REQUIRED" || sel.AuthState == "AUTHENTICATED")
                {
                    var r = new Rectangle(mbx, oy, 96, abh);
                    // Signing in again is the account-plane auth operation, not
                    // a second console launch: re-authenticating must not depend
                    // on a project folder being chosen or readable.
                    Buttons.Add(new ButtonDef { R = r, A = () => SignInSelectedAccount(), Label = AccountsLoc.T("Reauth") });
                    DrawButton(g, r, AccountsLoc.T("Reauth"), false, 8);
                    mbx += 100;
                }

                // Convert to managed context
                if (sel.Backend == "windows_user" && ContextSecondary(sel))
                {
                    var r = new Rectangle(mbx, oy, 134, abh);
                    Buttons.Add(new ButtonDef { R = r, A = () => AccountsMigrateContext() });
                    DrawButton(g, r, AccountsLoc.T("ConvertToManaged"), false, 8);
                    mbx += 138;
                }
                oy += abh + 8;

                // Background keeper (global). Whether Windows reconciles
                // managed contexts while SAITULS is closed -- at logon and
                // about every two minutes through one canonical task. This is
                // NOT per-account Auto-connect: the keeper only restores
                // accounts that are ENABLED with Auto-connect ON, and turning
                // it off never disconnects anything nor rewrites any intent.
                oy += 4;
                using (var kp = new Pen(Palette.BMUTED)) g.DrawLine(kp, indent + 8, oy, indent + pw - 28, oy);
                oy += 6;
                DrawText(g, "Background keeper", indent + 8, oy, Palette.MUTED, 9, true);
                string keeperLabel = S.AccountKeeper ? "ON" : "OFF";
                int keeperW = Math.Max(TextW(g, keeperLabel, 8) + 18, 54);
                var keeperR = new Rectangle(indent + 122, oy - 2, keeperW, 18);
                bool wantKeeper = S.AccountKeeper;
                Buttons.Add(new ButtonDef { R = keeperR, A = () => KeeperSet(!wantKeeper) });
                DrawButton(g, keeperR, keeperLabel, wantKeeper, 8);
                oy += 20;
                string keeperStatus = _keeperState.Length > 0 ? _keeperState : "Background keeper: checking...";
                DrawText(g, keeperStatus, indent + 8, oy, Palette.TEXT2, 8);
                oy += 14;
                if (_keeperLast.Length > 0)
                {
                    DrawText(g, _keeperLast, indent + 8, oy, Palette.MUTED, 8);
                    oy += 14;
                }
                bool keeperBroken = S.AccountKeeper
                    && (_keeperState.IndexOf("needs repair", StringComparison.OrdinalIgnoreCase) >= 0
                        || _keeperState.IndexOf("needs elevation", StringComparison.OrdinalIgnoreCase) >= 0
                        || _keeperState.IndexOf("missing", StringComparison.OrdinalIgnoreCase) >= 0
                        || _keeperState.IndexOf("disabled", StringComparison.OrdinalIgnoreCase) >= 0);
                bool keeperStaleOff = !S.AccountKeeper
                    && _keeperState.IndexOf("still registered", StringComparison.OrdinalIgnoreCase) >= 0;
                if (keeperBroken || keeperStaleOff)
                {
                    var repR = new Rectangle(indent + 8, oy, 96, 18);
                    Buttons.Add(new ButtonDef { R = repR, A = () => KeeperSet(S.AccountKeeper) });
                    DrawButton(g, repR, "Repair", false, 8);
                    oy += 22;
                }
                DrawText(g, "Restores contexts for Auto-connect accounts; never opens IDE windows.",
                    indent + 8, oy, Palette.MUTED, 8);
                oy += 14;

                // DANGER section
                oy += 4;
                using (var p = new Pen(Palette.BMUTED)) g.DrawLine(p, indent + 8, oy, indent + pw - 28, oy);
                oy += 6;
                DrawText(g, AccountsLoc.T("Destructive"), indent + 8, oy, Palette.DANGERTXT, 8, true);
                oy += 14;
                mbx = indent + 8;

                // Archive
                if (sel.State != "ARCHIVED")
                {
                    var r = new Rectangle(mbx, oy, 62, abh);
                    Buttons.Add(new ButtonDef { R = r, A = () => AccountsRun("set-state", "ARCHIVED") });
                    DrawButton(g, r, AccountsLoc.T("Archive"), false, 8);
                    mbx += 66;
                }

                // Delete managed context
                if (sel.Backend == "managed_windows_user")
                {
                    var r = new Rectangle(mbx, oy, 98, abh);
                    Buttons.Add(new ButtonDef { R = r, A = () => AccountsDeleteContext() });
                    DrawButton(g, r, AccountsLoc.T("DeleteCtx"), false, 8);
                    mbx += 102;
                }
                oy += abh + 4;

                // Diagnostics
                oy += 4;
                DrawText(g, "ID: " + sel.Id, indent + 8, oy, Palette.MUTED, 8);
                oy += 12;
                DrawText(g, "Provider: " + sel.Provider, indent + 8, oy, Palette.MUTED, 8);
                oy += 12;
                DrawText(g, "Backend: " + sel.Backend, indent + 8, oy, Palette.MUTED, 8);
                oy += 12;
                if (sel.Context.Length > 0)
                {
                    DrawText(g, "Context: " + sel.Context, indent + 8, oy, Palette.MUTED, 8);
                    oy += 12;
                }
                oy += 4;
                var copyR = new Rectangle(indent + 8, oy, 116, 18);
                Buttons.Add(new ButtonDef { R = copyR, A = () => {
                    try {
                        // The two planes as independent facts, and no verdict
                        // field. A single mismatch line here once told an
                        // operator that two independent logins differing from
                        // each other was a defect -- never true, and exactly
                        // what T-264 removed.
                        string diag = string.Format("ID: {0}\nProvider: {1}\nBackend: {2}\nState: {3}\nContext: {4}\nContextState: {5}\nAuthState: {6}\nQuotaState: {7}\nConsoleIdentity: {8}\nIdeIdentity: {10}\nHidden: {9}",
                            sel.Id, sel.Provider, sel.Backend, sel.State, sel.Context, sel.ContextState, sel.AuthState, sel.QuotaState,
                            IdentityDiag(sel.ConfiguredIdentityFp, sel.ConfiguredIdentityLabel), sel.Hidden,
                            IdentityDiag(sel.RuntimeIdentityFp, sel.RuntimeIdentityLabel));
                        if (sel.IdentitySharedWith.Length > 0)
                            diag += "\nQuotaSharedWith: " + sel.IdentitySharedWith;
                        Clipboard.SetText(diag);
                        Note(AccountsLoc.T("Copied"));
                    } catch { }
                } });
                DrawButton(g, copyR, AccountsLoc.T("CopyDiagnostics"), false, 8);
                oy += 22;
            }

            return oy - startY + _acctDetailScrollY;
        }

        void DrawAccountsTab(Graphics g, int x, int y, int w, int h)
        {
            int startY = y;

            if (Facts().AccountsExe == null)
            {
                DrawSectionHeader(g, x, y, w, AccountsLoc.T("ACCOUNTS"));
                y += 24;
                DrawText(g, Fit(g, AccountsLoc.T("ErrEngineMissing"), w, 10), x, y, Palette.DANGERTXT, 10);
                string repair = AccountsLoc.T("InstallRepair");
                var repairRect = new Rectangle(x, y + 22, Math.Max(130, TextW(g, repair, 9) + 18), 22);
                Buttons.Add(new ButtonDef { R = repairRect, A = () => StartSetup(), Label = repair });
                DrawButton(g, repairRect, repair, false, 9);
                string retry = AccountsLoc.T("Retry");
                var retryRect = new Rectangle(repairRect.Right + 8, y + 22, Math.Max(90, TextW(g, retry, 9) + 18), 22);
                Buttons.Add(new ButtonDef { R = retryRect, A = () => { LoadAccountsInBackground(); RefreshLimits(true); Invalidate(); }, Label = retry });
                DrawButton(g, retryRect, retry, false, 9);
                return;
            }

            // Top Toolbar:
            // Title + Action buttons: [+ Add account] [Refresh] [Import...]
            int toolbarH = 24;
            DrawText(g, AccountsLoc.T("ACCOUNTS"), x, y + 3, Palette.BHL, 11, true);

            // Right-aligned toolbar buttons
            int curBtnX = x + w;
            int btnH = 22, btnGap = 6;

            // Import legacy button
            string impLabel = AccountsLoc.T("ImportLegacy");
            int impW = Math.Max(TextW(g, impLabel, 9) + 16, 72);
            curBtnX -= impW;
            var impR = new Rectangle(curBtnX, y, impW, btnH);
            Buttons.Add(new ButtonDef { R = impR, A = () => DispatchAccounts(delegate {
                CliRun r = AccountsExec("import", 30000);
                string say = r.Ok ? r.Say : "FAILED: " + (r.Say.Length > 0 ? r.Say : r.Problem);
                LoadAccountsAsync();
                Note(say + "  |  " + SyncConsolesMenu());
            }) });
            DrawButton(g, impR, impLabel, false, 9);
            curBtnX -= btnGap;

            // Folder access button
            string fldLabel = AccountsLoc.T("FolderAccess");
            int fldW = Math.Max(TextW(g, fldLabel, 9) + 16, 96);
            curBtnX -= fldW;
            var fldR = new Rectangle(curBtnX, y, fldW, btnH);
            Buttons.Add(new ButtonDef { R = fldR, A = () => OpenFolderAccessManager(_selectedAccount) });
            DrawButton(g, fldR, fldLabel, false, 9);
            curBtnX -= btnGap;

            // Refresh button
            string refLabel = AccountsLoc.T("Refresh");
            int refW = Math.Max(TextW(g, refLabel, 9) + 16, 68);
            curBtnX -= refW;
            var refR = new Rectangle(curBtnX, y, refW, btnH);
            Buttons.Add(new ButtonDef { R = refR, A = () => DispatchAccounts(delegate {
                LoadAccountsAsync();
                Note("menu sync: " + SyncConsolesMenu());
            }) });
            DrawButton(g, refR, refLabel, false, 9);
            curBtnX -= btnGap;

            // Repair appears only after a sync actually failed: the account
            // mutation stays committed and the menu is repaired from here,
            // rather than the operator being asked to remember a script path.
            if (_menuSyncFailed)
            {
                string repairLabel = "Repair menu";
                int repairW = Math.Max(TextW(g, repairLabel, 9) + 16, 84);
                curBtnX -= (repairW + btnGap);
                var repairR = new Rectangle(curBtnX, y, repairW, btnH);
                Buttons.Add(new ButtonDef { R = repairR, A = () => DispatchAccounts(delegate {
                    string note = SyncConsolesMenu();
                    if (note == MenuSynced) note = "Explorer menu repaired and re-verified.";
                    Note(note);
                }) });
                DrawButton(g, repairR, repairLabel, false, 9);
                curBtnX -= btnGap;
            }

            // + Add account button
            string addLabel = AccountsLoc.T("AddAccount");
            int addW = Math.Max(TextW(g, addLabel, 9) + 16, 108);
            curBtnX -= addW;
            var addR = new Rectangle(curBtnX, y, addW, btnH);
            Buttons.Add(new ButtonDef { R = addR, A = () => AddAccountWizard() });
            DrawButton(g, addR, addLabel, false, 9);

            y += toolbarH + 8;

            // Status strip at bottom
            int statusStripH = 22;
            int availH = h - (y - startY) - statusStripH - 6;
            if (availH < 180) availH = 180;

            // Ensure valid selection
            if (_selectedAccount == null && _accounts != null && _accounts.Count > 0)
                _selectedAccount = _accounts[0].Id;
            else if (_selectedAccount != null && _accounts != null && !_accounts.Exists(a => a.Id == _selectedAccount) && _accounts.Count > 0)
                _selectedAccount = _accounts[0].Id;

            // Responsive horizontal split: left list vs right detail
            int listW = Math.Max(190, Math.Min(260, (int)(w * 0.38)));
            int detailX = x + listW + 6;
            int detailW = w - listW - 6;

            var listRect = new Rectangle(x, y, listW, availH);
            _acctListRect = listRect;
            var detailRect = new Rectangle(detailX, y, detailW, availH);

            // Left pane: Sunken box
            using (var b = new SolidBrush(Palette.BG))
                g.FillRectangle(b, listRect);
            DrawBevel(g, listRect, false);

            // Left pane content
            int rowH = 34;
            int maxVisibleRows = Math.Max(1, (availH - 8) / rowH);

            if (_accounts == null || _accounts.Count == 0)
            {
                DrawText(g, AccountsLoc.T("NoAccounts"), listRect.X + 8, listRect.Y + 12, Palette.TEXT2, 9);
            }
            else
            {
                // Clamp scroll
                int maxScroll = Math.Max(0, _accounts.Count - maxVisibleRows);
                if (_acctListScrollY > maxScroll) _acctListScrollY = maxScroll;
                if (_acctListScrollY < 0) _acctListScrollY = 0;

                // Clipping region for list
                Region prevClip = g.Clip;
                g.SetClip(new Rectangle(listRect.X + 2, listRect.Y + 2, listRect.Width - 4, listRect.Height - 4));

                int rowW = listRect.Width - 6;
                for (int i = 0; i < _accounts.Count; i++)
                {
                    AccountRow row = _accounts[i];
                    int ry = listRect.Y + 3 + (i - _acctListScrollY) * rowH;

                    // Row hit target must have Width >= 160 so tests identify account rows
                    var rowHit = new Rectangle(listRect.X + 3, ry, rowW, rowH - 2);
                    string id = row.Id;
                    Buttons.Add(new ButtonDef { R = rowHit, A = () => { _selectedAccount = id; Refresh(); } });

                    // Only paint if in visible area
                    if (ry + rowH >= listRect.Y && ry <= listRect.Bottom)
                    {
                        bool isSel = row.Id == _selectedAccount;
                        if (isSel)
                        {
                            using (var sb = new SolidBrush(Palette.SELECTION))
                                g.FillRectangle(sb, rowHit);
                            using (var sp = new Pen(Palette.BHL))
                                g.DrawRectangle(sp, rowHit.X, rowHit.Y, rowHit.Width - 1, rowHit.Height - 1);
                        }
                        else
                        {
                            // subtle row separator
                            using (var divP = new Pen(Palette.SURFACE))
                                g.DrawLine(divP, rowHit.X + 2, rowHit.Bottom, rowHit.Right - 2, rowHit.Bottom);
                        }

                        // Compact badge (e.g. AG1)
                        DrawText(g, row.Compact, rowHit.X + 6, ry + 3, isSel ? Palette.BHL : Palette.LINK, 9, true);

                        // Human state (right-aligned in row)
                        string hs = HumanState(row);
                        Color hsc = StateColor(hs);
                        string hsText = AccountsLoc.T(hs);
                        int stW = TextW(g, hsText, 8);
                        DrawText(g, hsText, rowHit.Right - stW - 6, ry + 4, hsc, 8, true);

                        // Name (middle)
                        int nameX = rowHit.X + 44;
                        int maxNameW = Math.Max(20, rowHit.Right - stW - 10 - nameX);
                        string name = row.Name;
                        while (name.Length > 4 && TextW(g, name, 9) > maxNameW)
                            name = name.Substring(0, name.Length - 2) + "~";
                        Color nameCol = row.Hidden ? Palette.MUTED : (isSel ? Palette.TEXT : Palette.TEXT2);
                        DrawText(g, name, nameX, ry + 3, nameCol, 9, isSel);

                        // Subtitle line: Provider + origin
                        string prov = row.Provider;
                        if (prov.Length > 0) prov = prov.Substring(0, 1).ToUpper() + prov.Substring(1);
                        if (row.Hidden) prov += " (" + AccountsLoc.T("Hidden") + ")";
                        else
                        {
                            // The subtitle answers "how much is left" at a glance:
                            // the account's lowest real reading, or why there is
                            // none. Never a number derived from a quota state.
                            LimitAccount rl = LimitsFor(row);
                            if (rl != null)
                            {
                                string st = Limits.State(rl, LimitsStale());
                                if (st == "OK" || st == "STALE")
                                {
                                    LimitAccount own;
                                    LimitWindow low = Limits.Resolve(new List<LimitAccount> { rl }, "lowest", null, out own);
                                    if (low != null) prov += " · " + ShortWindowLabel(low) + " " + Limits.Shown(low, S.LimitsShowUsed)
                                        + (st == "STALE" ? " (" + AccountsLoc.T("StateStale") + ")" : "");
                                }
                                else prov += " · " + LimitStateWord(st);
                            }
                        }
                        DrawText(g, prov, rowHit.X + 6, ry + 18, Palette.MUTED, 8);
                    }
                }

                if (_acctDragActive && _acctDragInsertIndex >= 0 && _acctDragInsertIndex <= _accounts.Count)
                {
                    int insY = listRect.Y + 3 + (_acctDragInsertIndex - _acctListScrollY) * rowH - 1;
                    if (insY < listRect.Y + 3) insY = listRect.Y + 3;
                    if (insY > listRect.Bottom - 3) insY = listRect.Bottom - 3;
                    using (var insPen = new Pen(Palette.BHL, 2))
                        g.DrawLine(insPen, listRect.X + 4, insY, listRect.Right - 4, insY);
                }

                g.Clip = prevClip;
            }

            // Right pane: Sunken box
            using (var b = new SolidBrush(Palette.BG))
                g.FillRectangle(b, detailRect);
            DrawBevel(g, detailRect, false);

            // Right pane content: Selected Account Details
            AccountRow selRow = SelectedAccount();
            Region prevClipD = g.Clip;
            g.SetClip(new Rectangle(detailRect.X + 2, detailRect.Y + 2, detailRect.Width - 4, detailRect.Height - 4));
            int detailStartButtons = Buttons.Count;
            int detailContentH = DrawAccountDetailPane(g, detailRect.X + 4, detailRect.Y + 6, detailRect.Width - 8, detailRect.Height - 12, selRow);
            ClampButtons(detailStartButtons, new Rectangle(detailRect.X + 2, detailRect.Y + 2, detailRect.Width - 4, detailRect.Height - 4));
            g.Clip = prevClipD;

            // Clamp detail scroll
            int maxDetailScroll = Math.Max(0, detailContentH - (detailRect.Height - 12));
            if (_acctDetailScrollY > maxDetailScroll) _acctDetailScrollY = maxDetailScroll;
            if (_acctDetailScrollY < 0) _acctDetailScrollY = 0;

            // Bottom quiet status strip
            int statusY = y + availH + 4;
            var statusRect = new Rectangle(x, statusY, w, statusStripH);
            using (var b = new SolidBrush(Palette.SURFACE))
                g.FillRectangle(b, statusRect);
            DrawBevel(g, statusRect, false);

            // Words first: what happened and what to do. The raw engine text is
            // kept for Copy diagnostics, never dumped on the strip.
            string statusMsg = _accountsNote.Length > 0 ? HumanizeNote(_accountsNote) : AccountsLoc.T("StatusReady");
            Color statusCol = NoteIsFailure(_accountsNote) ? Palette.DANGERTXT : Palette.TEXT2;
            DrawText(g, Fit(g, statusMsg, statusRect.Width - 12, 9), statusRect.X + 6, statusRect.Y + 4, statusCol, 9);

            // Nothing is polled from paint: BackgroundTick owns status, keeper
            // inspection and limits, so a repaint never starts a process.
        }

        void AccountsStatus()
        {
            AccountRow row = SelectedAccount();
            if (row == null) { Note("no account selected"); return; }
            PollStatus(row);
        }

        // The runtime probe for one account. Runs on a worker; fills in the
        // row's context/auth facts and refreshes. This is the ONLY place the
        // CONTEXT column and the Stop context button learn anything.
        static string DescribeStatus(AccountRow row)
        {
            return row.Compact + ": context=" + row.ContextState
                + " auth=" + row.AuthState
                + " quota=" + row.QuotaState;
        }

        void PollStatus(AccountRow row)
        {
            if (row == null) return;
            string id = row.Id, compact = row.Compact;
            ThreadPool.QueueUserWorkItem(delegate {
                CliRun r = AccountsExec("status \"" + compact + "\"", 20000);
                var o = ParseEnv(r.Stdout);
                TryBeginInvoke(delegate {
                    AccountRow cur = _accounts != null ? _accounts.Find(x => x.Id == id) : null;
                    if (cur == null) return;
                    if (o == null) { _accountsNote = "status failed: " + (r.Say.Length > 0 ? r.Say : r.Problem); Refresh(); return; }
                    cur.ContextState = AsText(o, "context_state");
                    cur.AuthState = AsText(o, "auth_state");
                    cur.QuotaState = AsText(o, "quota_state");
                    ApplyRuntimeIdentity(cur, o);
                    // Same recompute as the paint poll: a poll that changes a
                    // fingerprint changes who the slot shares with. Grouping only
                    // on reload makes the duplicate warning right exactly once,
                    // and stale from then on.
                    MarkSharedIdentities(_accounts);
                    _accountsNote = DescribeStatus(cur);
                    Refresh();
                });
            });
        }


        // ------------------- context controls -------------------
        // A context can only exist where a context is meaningful: a windows_user
        // backend on a provider that declares context creation, for an account
        // that is NOT the operator's own identity. Offering "start a broker" on
        // the current user would open a Windows sign-in prompt for the account
        // you are already logged in as; the engine answers already_current_user,
        // but the question should not have been asked.
        bool ContextCapable(AccountRow row)
        {
            return row != null && ContextCapableFor(row.Backend, row.Provider);
        }

        bool ContextCapableFor(string backend, string provider)
        {
            if (backend != "windows_user" && backend != "managed_windows_user") return false;
            if (string.IsNullOrEmpty(provider)) return false;
            bool cached;
            lock (_providerCaps)
                if (_providerCaps.TryGetValue(provider, out cached)) return cached;
            // Paint never waits on a child. The capability is fetched when the
            // account list loads (FetchAccounts); a paint that still finds it
            // missing draws the provisional answer -- a managed backend is a
            // separate context by definition -- and asks once in the background.
            if (IsHandleCreated && !InvokeRequired)
            {
                string prov = provider;
                ThreadPool.QueueUserWorkItem(delegate { FetchCapability(prov); TryBeginInvoke(delegate { Invalidate(); }); });
                return backend == "managed_windows_user";
            }
            return FetchCapability(provider);
        }

        bool FetchCapability(string provider)
        {
            bool cached;
            lock (_providerCaps)
                if (_providerCaps.TryGetValue(provider, out cached)) return cached;
            CliRun r = AccountsExec("capabilities \"" + provider + "\"", 15000);
            var o = ParseEnv(r.Stdout);
            bool ok = o != null && object.Equals(o.ContainsKey("supports_context_creation")
                ? o["supports_context_creation"] : null, true);
            // Only a real answer is remembered; a missing engine is asked again.
            if (o != null) lock (_providerCaps) _providerCaps[provider] = ok;
            return ok;
        }

        static bool IsCurrentUserContext(AccountRow row)
        {
            if (row == null) return false;
            return IsCurrentUserContext(row.Context);
        }

        static bool IsCurrentUserContext(string contextLabel)
        {
            if (string.IsNullOrEmpty(contextLabel)) return false;
            string label = contextLabel;
            int slash = Math.Max(label.LastIndexOf('\\'), label.LastIndexOf('/'));
            if (slash >= 0) label = label.Substring(slash + 1);
            string me = Environment.UserName;
            int meSlash = me.LastIndexOf('\\');
            if (meSlash >= 0) me = me.Substring(meSlash + 1);
            return string.Equals(label, me, StringComparison.OrdinalIgnoreCase);
        }

        // True when the engine can create this account's context at all.
        bool ContextSecondary(AccountRow row)
        {
            return ContextCapable(row) && !IsCurrentUserContext(row);
        }

        // The one sentence that decides the whole panel: an account either runs
        // as the operator's own Windows user -- nothing to connect, ever -- or
        // in a separate managed Windows user with a broker lifecycle. Every
        // other word the operator reads here derives from that one fact.
        string ExecutionLabel(AccountRow row)
        {
            return row == null ? "" : ContextSecondary(row)
                ? AccountsLoc.T("ExecManaged")
                : AccountsLoc.T("ExecCurrentUser");
        }

        // Why Auto-connect reads OFF. An operator who pressed Disconnect and a
        // broker that merely died both leave the account wanting no automatic
        // connection, and the difference is the whole point of the row -- so it
        // is stated, and an origin we cannot prove is never dressed up as a
        // decision somebody made.
        static string AutoConnectNote(AccountRow row)
        {
            if (row == null || row.AutoConnect) return "";
            string why = row.AutoConnectReason;
            if (why == "operator_disconnect") return AccountsLoc.T("OffByChoice");
            if (why == "unknown") return AccountsLoc.T("OffUnknown");
            return AccountsLoc.T("OffDefault");
        }

        /// "Switch Google account" is meaningful for exactly one case: a provider
        /// that has a real credential to replace, on an account that is already
        /// signed in. Anything else would be a second button competing with Sign
        /// in for the same job.
        static bool CanSwitchProviderAccount(AccountRow row)
        {
            return row != null
                && row.Provider == "antigravity"
                && row.AuthState == "AUTHENTICATED"
                && row.State != "DISABLED"
                && row.State != "ARCHIVED";
        }

        /// The switch, in the order the operator experiences it.
        ///
        /// Drop this account's Google credential, confirm it is gone, open the
        /// vendor's own sign-in in THIS account's Windows context, then poll until
        /// a new verified identity appears and say plainly whether the identity
        /// actually changed.
        ///
        /// Two things it deliberately does not do. It does not touch any other
        /// account -- the engine resolves the selector to one Windows SID and the
        /// broker deletes only its own credential store, so AG1 cannot be reached
        /// from here. And it does not claim success on the vendor's word: the old
        /// fingerprint is captured first, and signing back into the same account
        /// is reported as exactly that.
        void SwitchProviderAccount()
        {
            AccountRow row = SelectedAccount();
            if (row == null) { Note("no account selected"); return; }
            if (!CanSwitchProviderAccount(row))
            {
                Note(row.Compact + ": this account has no provider sign-in to switch.");
                return;
            }
            string compact = row.Compact;
            // The baseline is the CREDENTIAL about to be replaced -- Plane B when
            // it is proven, and only then the canonical identity as a fallback. A
            // console account is not what this operation signs out of, so reading
            // it here would let an unproven canonical plane turn "same account
            // again" into a reported switch.
            string oldFp = row.RuntimeIdentityFp.Length > 0 ? row.RuntimeIdentityFp : row.IdentityFp;
            string oldLabel = row.RuntimeIdentityFp.Length > 0 ? row.RuntimeIdentityLabel : row.IdentityLabel;
            string id = row.Id;

            Note(compact + ": " + AccountsLoc.T("Switching", oldLabel));
            DispatchAccounts(delegate {
                // Step 1: the provider logout, inside the account's own context.
                // AccountsCli returns null on a non-zero exit, so a refused
                // logout -- wrong lifecycle, unknown account, context offline --
                // stops here instead of opening a sign-in for a credential that
                // is still in place.
                string out1 = AccountsCli("logout \"" + id + "\"", 30000);
                if (out1 == null)
                {
                    SetAccountsNote(compact + ": logout failed or was refused.");
                    LoadAccountsAsync();
                    return;
                }

                // Step 2: confirm the credential is really gone before opening a
                // sign-in meant to replace it. The logout's own answer is not
                // evidence enough -- a delete that reported success over a store
                // the broker did not actually own would leave the old account
                // standing, and the operator would then be asked to choose between
                // two logins while one of them silently wins. Still there means
                // say so and stop.
                if (!WaitForCredentialGone(id, DateTime.UtcNow.AddSeconds(45)))
                {
                    SetAccountsNote(compact + ": sign-out did not take effect; the account is still signed in.");
                    LoadAccountsAsync();
                    return;
                }

                // Step 3: the vendor's own flow. This is the same `auth` the SIGN
                // IN button already uses, so there is no second OAuth path to keep
                // honest.
                string out2 = AccountsCli("auth \"" + id + "\"", 30000);
                if (out2 == null)
                {
                    SetAccountsNote(compact + ": signed out, but the sign-in window could not open.");
                    LoadAccountsAsync();
                    return;
                }

                // Step 4: poll for the new identity. Bounded: the operator has to
                // physically complete an OAuth in a browser, so this waits for a
                // human, and it stops waiting rather than blocking the UI thread.
                PollForIdentityChange(id, compact, oldFp, oldLabel, DateTime.UtcNow.AddMinutes(10));
            });
        }

        void SetAccountsNote(string text)
        {
            TryBeginInvoke(delegate { _accountsNote = text; Refresh(); });
        }

        /// Wait until the provider credential for this account is really absent,
        /// reading it the same way the rest of the tab does: the `credential`
        /// object of a fresh status, never the logout's return value.
        ///
        /// Two answers count as gone and no others do. `credential_present` is the
        /// direct evidence; AUTH_REQUIRED is the same fact seen through the auth
        /// state, and either one is enough. A reply that carries no credential
        /// object at all is NOT evidence -- it is the unknown case, and treating
        /// silence as a successful sign-out is exactly how a switch ends up
        /// reporting a clean slate it never had.
        bool WaitForCredentialGone(string id, DateTime deadline)
        {
            while (DateTime.UtcNow < deadline)
            {
                // A closed window ends the wait. Without this the thread runs
                // its full deadline after the operator has quit, holding a child
                // process slot and answering with notes nobody will ever read.
                if (IsDisposed) return false;
                string json = AccountsCli("status \"" + id + "\"", 15000);
                string line = LastEnvelopeLine(json);
                if (line.Length > 0)
                {
                    Dictionary<string, object> o = null;
                    try { o = new System.Web.Script.Serialization.JavaScriptSerializer().DeserializeObject(line) as Dictionary<string, object>; }
                    catch { }
                    if (o != null)
                    {
                        if (AsText(o, "auth_state") == "AUTH_REQUIRED") return true;
                        var cred = AsDict(o, "credential");
                        if (cred != null)
                        {
                            // A real bool, not a string compare: "false" as text
                            // and false as a value must not both pass here, or a
                            // store that answers with the string "true" would read
                            // as signed out.
                            object present;
                            if (cred.TryGetValue("credential_present", out present) && present is bool && !(bool)present) return true;
                        }
                    }
                }
                Thread.Sleep(1500);
            }
            return false;
        }

        /// Wait for a DIFFERENT verified fingerprint, then report what actually
        /// happened. Signing back into the same Google account is a real outcome
        /// and is named as one; the UI never reports a switch that did not switch.
        void PollForIdentityChange(string id, string compact, string oldFp, string oldLabel, DateTime deadline)
        {
            ThreadPool.QueueUserWorkItem(delegate {
                int waited = 0;
                while (DateTime.UtcNow < deadline && waited < 600000)
                {
                    // Same reason as the pre-auth wait: a poll that outlives the
                    // window keeps polling a store nobody is watching.
                    if (IsDisposed) return;
                    Thread.Sleep(4000);
                    waited += 4000;
                    string json = AccountsCli("status \"" + id + "\"", 15000);
                    if (json == null) continue;
                    Dictionary<string, object> o = null;
                    try { o = new System.Web.Script.Serialization.JavaScriptSerializer().DeserializeObject(json) as Dictionary<string, object>; }
                    catch { }
                    if (o == null) continue;
                    var pi = AsDict(AsDict(o, "credential"), "provider_identity");
                    string fp = AsText(pi, "fingerprint");
                    if (fp.Length == 0 || string.Equals(fp, oldFp, StringComparison.OrdinalIgnoreCase)) continue;

                    string label = AsText(pi, "account_label");
                    // An EXPLICIT bind: the operator chose this identity, so it
                    // is authorised to become canonical. The engine call above is
                    // what makes it real; the row update only mirrors it.
                    CliRun br = AccountsExec("bind-identity \"" + id + "\"", 15000);
                    if (!br.Ok)
                    {
                        TryBeginInvoke(delegate {
                            AccountRow cur = _accounts != null ? _accounts.Find(x => x.Id == id) : null;
                            if (cur != null)
                            {
                                cur.RuntimeIdentityFp = fp;
                                cur.RuntimeIdentityLabel = label;
                            }
                            AccountRow prev = _accountsRuntime != null ? _accountsRuntime.Find(x => x.Id == id) : null;
                            if (prev != null)
                            {
                                prev.RuntimeIdentityFp = fp;
                                prev.RuntimeIdentityLabel = label;
                            }
                        });
                        SetAccountsNote(compact + ": BIND_FAILED: " + (br.Say.Length > 0 ? br.Say : (br.Problem.Length > 0 ? br.Problem : "identity could not be bound.")));
                        LoadAccountsAsync();
                        return;
                    }
                    TryBeginInvoke(delegate {
                        AccountRow cur = _accounts != null ? _accounts.Find(x => x.Id == id) : null;
                        if (cur != null)
                        {
                            cur.ConfiguredIdentityFp = fp;
                            cur.ConfiguredIdentityLabel = label;
                            cur.RuntimeIdentityFp = fp;
                            cur.RuntimeIdentityLabel = label;
                            SyncCanonicalAlias(cur);
                        }
                        AccountRow prev = _accountsRuntime != null ? _accountsRuntime.Find(x => x.Id == id) : null;
                        if (prev != null)
                        {
                            prev.ConfiguredIdentityFp = fp;
                            prev.ConfiguredIdentityLabel = label;
                            prev.RuntimeIdentityFp = fp;
                            prev.RuntimeIdentityLabel = label;
                            SyncCanonicalAlias(prev);
                        }
                    });
                    SetAccountsNote(compact + ": " + AccountsLoc.T("SwitchedTo", label.Length > 0 ? label : fp));
                    LoadAccountsAsync();
                    return;
                }
                SetAccountsNote(compact + ": " + AccountsLoc.T("SwitchUnchanged", oldLabel));
                LoadAccountsAsync();
            });
        }

        void AccountsContext(string verb)
        {
            // "connect"/"disconnect" are intent; the process work underneath is
            // still start/stop, so the wording and the success test below speak
            // about the intent the operator actually expressed.
            bool connecting = verb == "connect";
            AccountRow row = SelectedAccount();
            if (row == null) { Note("no account selected"); return; }
            string compact = row.Compact;
            // Plain strings, captured on the UI thread: the worker decides
            // capability itself so no list read and no engine call happens on
            // the paint thread.
            string backend = row.Backend, provider = row.Provider, context = row.Context;
            DispatchAccounts(delegate {
                if (!ContextCapableFor(backend, provider))
                {
                    Note(compact + ": this backend has no context controls."); return;
                }
                if (IsCurrentUserContext(context))
                {
                    Note(compact + " runs as your own Windows account -- there is no separate context to "
                        + verb + ".");
                    return;
                }
                // The operator pressed Connect and can see nothing happened for up to the whole
                // timeout. Say so first, then replace it with the outcome. The row is
                // re-probed immediately after the verb returns, so the list never
                // keeps showing a stale OFFLINE that only a later repaint corrects.
                Note(compact + ": " + (connecting ? "connecting..." : "disconnecting..."));
                // connect/disconnect, not start/stop. These two record what the
                // operator WANTS and survive a restart; start/stop only move a
                // process and are what an explicit launch reaches for on its own.
                // Routing the buttons through the intent verbs is what makes one
                // press here enough, and what keeps a later launch from quietly
                // re-enabling an account the operator turned off.
                CliRun r = AccountsExec("context " + verb + " \"" + compact + "\"", 30000);
                string say = r.Say.Length > 0 ? r.Say : r.Problem;
                // Reload the list so the row is not left describing the old world,
                // then ask the engine what the context actually is. The row's own
                // fields cannot answer this -- `list` carries no context_state and
                // AccountRow.Context is the managed user name.
                LoadAccountsAsync();
                string state = ProbeContextState(compact);
                bool known = state.Length > 0;
                bool online = known && state.IndexOf("ONLINE", StringComparison.OrdinalIgnoreCase) >= 0;
                bool landed = connecting ? online : (known && !online);
                Note(compact + ": " + (landed ? (connecting ? "ONLINE" : "OFFLINE") : say));
            });
        }

        // ------------------- desired-state reconciliation -------------------
        // One bounded sweep of every managed account that is ENABLED and wants
        // to be connected. It runs on a worker, never on the paint thread, and
        // it never blocks startup: the window comes up while the sweep is
        // still going, and the sweep's own time budget caps how long it can
        // chase a broker that is not coming up.
        //
        // Idempotence is what makes the periodic re-run safe. Each account is
        // asked its pipe first, so a second pass over a healthy plane costs one
        // round trip per account and starts nothing.
        static int AutoConnectSweepBudgetMs = 90000;
        bool _autoConnectSweepRunning = false;
        DateTime _autoConnectSweepAt = DateTime.MinValue;

        void AutoConnectSweep(bool force)
        {
            if (AccountsExe() == null) return;
            if (_autoConnectSweepRunning) return;
            if (!force && (DateTime.Now - _autoConnectSweepAt).TotalSeconds < 120.0) return;
            _autoConnectSweepAt = DateTime.Now;
            _autoConnectSweepRunning = true;
            ThreadPool.QueueUserWorkItem(delegate {
                CliRun r = AccountsExecTracked("autoconnect --budget-ms " + AutoConnectSweepBudgetMs,
                                       AutoConnectSweepBudgetMs + 30000);
                var o = ParseEnv(r.Stdout);
                TryBeginInvoke(delegate {
                    _autoConnectSweepRunning = false;
                    // Only a real change is worth a repaint. A sweep that found
                    // everything already online would otherwise rewrite the note
                    // line every two minutes for no reason the operator can see.
                    // A sweep that could not be parsed says nothing about what it did, and
                    // writing the note anyway replaced whatever the operator was
                    // actually being told -- a switch's result, a refusal, a
                    // real error -- with a line of empty numbers. Silence from
                    // the engine is not a report.
                    string started = AsText(o, "started"), failed = AsText(o, "failed");
                    if (o != null && (started != "0" || (failed.Length > 0 && failed != "0")))
                    {
                        Note("auto-connect: " + started + " started, " + AsText(o, "already_online")
                             + " already online, " + failed + " failed.");
                        LoadAccountsAsync();
                    }
                });
            });
        }

        // ------------------- background keeper -------------------
        // Durable reconciliation owned by SAI Accounts, independent of this
        // GUI process. One canonical Task Scheduler task
        // (SAITULS\SAI Accounts Keeper) runs the INSTALLED engine as
        // `autoconnect --budget-ms <bounded>` at logon and about every two
        // minutes. The task owns no state; the engine owns the desired-state
        // rules and a named sweep mutex, so this in-process timer racing the
        // task is harmless by construction.
        //
        // GLOBAL BACKGROUND KEEPER (this toggle) says whether Windows heals
        // contexts while SAITULS is closed. PER-ACCOUNT Auto-connect says
        // whether one account may be restored at all. Keeper ON restores only
        // ENABLED managed accounts with Auto-connect ON; keeper OFF heals
        // nothing but never disconnects anything and never rewrites intent.
        //
        // Quota visibility is deliberately NOT coupled here: usage comes from
        // `snapshot`/`usage` against broker state, and the engine's keeper log
        // records availability, never fresh quota values. This panel shows
        // availability; it never polls /usage on a cadence.
        bool _keeperBusy = false;
        DateTime _keeperCheckAt = DateTime.MinValue;
        string _keeperState = "";
        string _keeperLast = "";

        string KeeperInstaller()
        {
            return Path.Combine(S.RootPath, "Scripts", "accounts", "Install-SaiAccountsKeeper.ps1");
        }

        static string KeeperPowerShell()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell\\v1.0\\powershell.exe");
        }

        // Throttled actual-state inspection: desired (the persisted toggle)
        // against registered (the real task definition). Never derives ON
        // from the checkbox alone.
        void KeeperRefresh(bool force)
        {
            if (AccountsExe() == null) return;
            if (_keeperBusy) return;
            if (!force && (DateTime.Now - _keeperCheckAt).TotalSeconds < 120.0) return;
            _keeperCheckAt = DateTime.Now;
            _keeperBusy = true;
            string installer = KeeperInstaller();
            ThreadPool.QueueUserWorkItem(delegate {
                string state, last;
                KeeperInspect(installer, out state, out last);
                TryBeginInvoke(delegate { _keeperState = state; _keeperLast = last; _keeperBusy = false; Refresh(); });
            });
        }

        void KeeperInspect(string installer, out string state, out string last)
        {
            state = ""; last = "";
            try
            {
                if (!File.Exists(installer)) { state = "Background keeper unavailable: installer script missing"; return; }
                CliRun v = RunBounded(KeeperPowerShell(),
                    "-NoProfile -ExecutionPolicy Bypass -File \"" + installer + "\" -VerifyOnly", 30000);
                bool verified = v.Ok;
                string problem = v.Ok ? "" : ((v.Stdout + " " + v.Stderr).Trim());
                if (problem.Length > 160) problem = problem.Substring(0, 157) + "...";
                last = KeeperLastRun();
                if (!S.AccountKeeper)
                {
                    state = verified
                        ? "Background keeper: OFF (a periodic task is still registered)"
                        : "Background keeper: OFF";
                    return;
                }
                if (verified) { state = "Background keeper: ON"; return; }
                if (problem.IndexOf("ELEVATION_REQUIRED", StringComparison.OrdinalIgnoreCase) >= 0
                    || problem.IndexOf("Access is denied", StringComparison.OrdinalIgnoreCase) >= 0)
                    state = "Background keeper: ON -- needs elevation to install the task";
                else if (problem.IndexOf("Not registered", StringComparison.OrdinalIgnoreCase) >= 0)
                    state = "Background keeper: ON -- task missing";
                else if (problem.IndexOf("disabled", StringComparison.OrdinalIgnoreCase) >= 0)
                    state = "Background keeper: ON -- task disabled";
                else
                    state = "Background keeper: ON -- needs repair" + (problem.Length > 0 ? ": " + problem : "");
            }
            catch (Exception ex) { state = "Background keeper unavailable: " + ex.Message; }
        }

        // Scheduler's own last-run facts plus the engine keeper-log tail.
        // Fail-open: any part that cannot be read is omitted, never fatal.
        string KeeperLastRun()
        {
            string info = "";
            try
            {
                CliRun q = RunBounded("schtasks",
                    "/Query /TN \"\\SAITULS\\SAI Accounts Keeper\" /FO LIST /V", 15000);
                if (q.Ok)
                {
                    string runTime = "", runResult = "";
                    foreach (string rawLine in q.Stdout.Split('\n'))
                    {
                        string line = rawLine.Trim();
                        if (line.StartsWith("Last Run Time:", StringComparison.OrdinalIgnoreCase))
                            runTime = line.Substring("Last Run Time:".Length).Trim();
                        else if (line.StartsWith("Last Result:", StringComparison.OrdinalIgnoreCase))
                            runResult = line.Substring("Last Result:".Length).Trim();
                    }
                    if (runTime.Length > 0 && !runTime.StartsWith("N/A", StringComparison.OrdinalIgnoreCase))
                        info = "Last check: " + runTime;
                    if (runResult.Length > 0) info += (info.Length > 0 ? "  " : "") + "result " + runResult;
                }
            }
            catch { }
            try
            {
                string logDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                    "SAI", "Accounts", "logs");
                if (Directory.Exists(logDir))
                {
                    FileInfo newest = null;
                    foreach (string path in Directory.GetFiles(logDir, "keeper-*.log"))
                    {
                        var fi = new FileInfo(path);
                        if (newest == null || fi.LastWriteTimeUtc > newest.LastWriteTimeUtc) newest = fi;
                    }
                    if (newest != null)
                    {
                        string tail = "";
                        foreach (string line in File.ReadLines(newest.FullName))
                            if (line.Trim().Length > 0) tail = line;
                        string summary = KeeperLogSummary(tail);
                        if (summary.Length > 0) info += (info.Length > 0 ? "  |  " : "") + summary;
                    }
                }
            }
            catch { }
            return info;
        }

        // One keeper.log line into "3 online, 1 restored, 0 failed".
        // Transitional, never fabricated: failures stay failures.
        static string KeeperLogSummary(string tail)
        {
            if (tail == null || tail.Length == 0) return "";
            try
            {
                Match started = Regex.Match(tail, @"started=(\d+)");
                Match already = Regex.Match(tail, @"already=(\d+)");
                Match failed = Regex.Match(tail, @"failed=(\d+)");
                if (!started.Success || !already.Success || !failed.Success) return "";
                return already.Groups[1].Value + " online, " + started.Groups[1].Value
                    + " restored, " + failed.Groups[1].Value + " failed";
            }
            catch { return ""; }
        }

        // The toggle contract. Persist desired first, then install or disable
        // the canonical task transactionally, verify, and report. OFF leaves
        // running brokers online and per-account Auto-connect untouched.
        void KeeperSet(bool on)
        {
            if (_keeperBusy) { Note("busy -- a keeper action is already running."); return; }
            S.AccountKeeper = on;
            S.Save();
            _keeperBusy = true;
            Note("Background keeper " + (on ? "ON -- installing periodic reconciliation..." : "OFF -- disabling periodic reconciliation..."));
            string installer = KeeperInstaller();
            ThreadPool.QueueUserWorkItem(delegate {
                string outcome;
                try { outcome = KeeperApply(installer, on); }
                catch (Exception ex) { outcome = "Background keeper unavailable: " + ex.Message; }
                TryBeginInvoke(delegate { _keeperBusy = false; Note(outcome); KeeperRefresh(true); });
            });
        }

        string KeeperApply(string installer, bool on)
        {
            if (!File.Exists(installer)) return "Background keeper unavailable: installer script missing";
            string sw = on ? "-Enable" : "-Disable";
            CliRun r = RunBounded(KeeperPowerShell(),
                "-NoProfile -ExecutionPolicy Bypass -File \"" + installer + "\" " + sw, 180000);
            if (r.Ok)
            {
                if (on)
                {
                    string say = (r.Stdout ?? "").Trim().Replace("\r", "").Replace("\n", " | ");
                    if (say.Length > 180) say = say.Substring(0, 177) + "...";
                    return "Background keeper ON: " + say;
                }
                return "Background keeper OFF: no background reconciliation; running contexts stay online.";
            }
            string problem = ((r.Stdout ?? "") + " " + (r.Stderr ?? "")).Trim();
            if (problem.IndexOf("ELEVATION_REQUIRED", StringComparison.OrdinalIgnoreCase) >= 0
                || problem.IndexOf("Access is denied", StringComparison.OrdinalIgnoreCase) >= 0
                || problem.IndexOf("0x80070005", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                // Established pattern (build.ps1 -Deploy): re-run this exact
                // installer elevated and wait for it. A silent failure here
                // would leave the toggle claiming ON with no task behind it.
                string transcript;
                bool ok = KeeperRunElevated(sw, 180000, out transcript);
                if (ok) return "Background keeper " + (on ? "ON" : "OFF") + " (installed elevated).";
                if ((transcript ?? "").IndexOf("1223", StringComparison.OrdinalIgnoreCase) >= 0
                    || (transcript ?? "").IndexOf("cancel", StringComparison.OrdinalIgnoreCase) >= 0)
                    return "Background keeper unavailable: elevation cancelled -- the GUI keeps working without it.";
                string t = (transcript ?? "").Trim().Replace("\r", "").Replace("\n", " | ");
                if (t.Length > 160) t = t.Substring(0, 157) + "...";
                return "Background keeper unavailable: needs elevation" + (t.Length > 0 ? " (" + t + ")" : "") + ".";
            }
            if (problem.Length > 200) problem = problem.Substring(0, 197) + "...";
            return "Background keeper unavailable: " + (problem.Length > 0 ? problem : r.Problem) + ".";
        }

        // One elevated installer run with its transcript on disk (Verb=runas
        // cannot redirect pipes, so the child writes its own log). True only
        // when the installer itself reports success.
        bool KeeperRunElevated(string installerArgs, int timeoutMs, out string transcript)
        {
            transcript = "";
            string log = Path.Combine(Path.GetTempPath(), "sai-keeper-install-" + Process.GetCurrentProcess().Id + ".log");
            try { if (File.Exists(log)) File.Delete(log); } catch { }
            string cmd = "-NoProfile -ExecutionPolicy Bypass -Command \"& '" + KeeperInstaller() + "' "
                + installerArgs + " 2>&1 | Out-File -LiteralPath '" + log + "' -Encoding UTF8; exit $LASTEXITCODE\"";
            try
            {
                var psi = new ProcessStartInfo("powershell", cmd)
                {
                    UseShellExecute = true,
                    Verb = "runas",
                    WindowStyle = ProcessWindowStyle.Hidden
                };
                using (Process proc = Process.Start(psi))
                {
                    if (proc == null) { transcript = "elevated start refused"; return false; }
                    if (!proc.WaitForExit(timeoutMs))
                    {
                        try { proc.Kill(); } catch { }
                        transcript = "elevated run timed out";
                        return false;
                    }
                    try { if (File.Exists(log)) transcript = File.ReadAllText(log, Encoding.UTF8); } catch { }
                    return proc.ExitCode == 0;
                }
            }
            catch (System.ComponentModel.Win32Exception ex) { transcript = ex.Message; return false; }
            catch (Exception ex) { transcript = ex.Message; return false; }
        }

        // ------------------- open console -------------------
        // Nine steps, in this order, and no substitutions anywhere in it:
        //   lifecycle -> workdir -> status -> start context -> wait for ONLINE
        //   -> auth -> launch the EXACT selected account with the EXACT workdir
        //   -> report the pid or a reason.
        // The one thing it will never do is launch a different account than the
        // one selected, and the other thing it will never do is pick a workdir
        // on the operator's behalf.
        // Everything that can refuse a launch BEFORE anything is started, as one pure
        // function so there is a single answer and it can be tested without a
        // window. "" means the launch may proceed.
        static string BlockedBy(AccountRow row)
        {
            if (row == null) return "no account selected";
            if (row.State != "ENABLED")
                return "LIFECYCLE_FORBIDDEN: " + row.Compact + " is " + row.State + ". Set it ENABLED first.";
            if (row.Hidden)
                return row.Compact + " is hidden. Unhide it before launching a console.";
            return "";
        }

        void OpenConsole()
        {
            AccountRow row = SelectedAccount();
            string block = BlockedBy(row);
            if (block.Length > 0) { Note(block); Refresh(); return; }
            string label = row.Compact;
            string backend = row.Backend, provider = row.Provider, context = row.Context;

            // 2. workdir, before anything is started. Asking here means we
            // never half-start a context for a launch that cannot happen.
            string workdir = CurrentWorkdir();
            if (workdir == null)
            {
                Note("Choose a project folder to open in the console.");
                Refresh();
                workdir = ChooseWorkdir();
                if (workdir == null) return;
            }

            DispatchAccounts(delegate {
                OpenConsoleFlow(label, workdir, backend, provider, context);
            });
        }

        // ------------------- sign in -------------------
        // Authentication is an ACCOUNT-PLANE operation and has nothing to do
        // with a project folder. It used to be OpenConsole(true), which is
        // precisely why clicking SIGN IN demanded a workdir and then refused
        // the whole thing with WORKDIR_UNAVAILABLE: signing in was wearing a
        // project launch's clothes, and a managed account that cannot read the
        // operator's project tree could therefore never sign in at all.
        //
        // Nothing below reads CurrentWorkdir(), ChooseWorkdir() or
        // WorkdirUsable(), and no step here can emit WORKDIR_UNAVAILABLE: the
        // engine's auth operation accepts no workdir at all and launches only
        // the fixed shared binary, under the selected account's own identity.
        static int AuthPollBudgetMs = 180000;

        void SignInSelectedAccount()
        {
            AccountRow row = SelectedAccount();
            string block = BlockedBy(row);
            if (block.Length > 0) { Note(block); Refresh(); return; }
            string label = row.Compact;
            string backend = row.Backend, provider = row.Provider, context = row.Context;
            DispatchAccounts(delegate { SignInFlow(label, backend, provider, context); });
        }

        void SignInFlow(string label, string backend, string provider, string context)
        {
            // 1. status.
            Note("Checking " + label + "...");
            CliRun st = AccountsExec("status \"" + label + "\"", 20000);
            var env = ParseEnv(st.Stdout);
            if (env == null)
            {
                Note("STATUS_FAILED: " + (st.Say.Length > 0 ? st.Say : st.Problem));
                return;
            }
            string ctx = AsText(env, "context_state");
            string auth = AsText(env, "auth_state");
            SetLiveState(label, ctx, auth, AsText(env, "quota_state"));

            // 2. an offline managed account cannot open its own sign-in window,
            // so the exact selected context is started and awaited first.
            if (ctx != "ONLINE")
            {
                if (IsCurrentUserContext(context))
                {
                    // The operator's own account has no separate broker.
                }
                else if (!ContextCapableFor(backend, provider))
                {
                    Note("CONTEXT_OFFLINE: " + label + " has no running context and this provider cannot create one.");
                    return;
                }
                else
                {
                    Note(backend == "managed_windows_user" ? "Starting " + label + " context via helper..." : "Starting " + label + " context...");
                    CliRun cs = AccountsExec("context start \"" + label + "\"", 30000);
                    if (!cs.Ok && ParseEnv(cs.Stdout) == null)
                    {
                        Note("CONTEXT_START_FAILED: " + (cs.Say.Length > 0 ? cs.Say : cs.Problem));
                        return;
                    }
                    string csSay = cs.Say;
                    if (csSay == "already_current_user" || csSay == "NO_CONTEXT_REQUIRED")
                    {
                        Note("CONTEXT_OFFLINE: " + label + " reports no separate context (" + csSay + ").");
                        return;
                    }
                    Note(backend == "managed_windows_user" ? "Waiting for managed context..." : "Waiting for Windows login...");
                    if (!WaitForOnline(label, out auth)) return;
                }
            }

            if (auth == "AUTHENTICATED")
            {
                Note(label + " is already signed in.");
                return;
            }

            // 3. the account-plane auth operation for the EXACT selected
            // account. No workdir, no program, no fallback to another account.
            Note("Opening Antigravity for " + label + " to sign in...");
            CliRun ar = AccountsExec("auth \"" + label + "\"", 30000);
            var ae = ParseEnv(ar.Stdout);
            string aStatus = ae != null ? AsText(ae, "status") : "";
            if (aStatus == "CONTEXT_OFFLINE")
            {
                Note("CONTEXT_OFFLINE: " + label + "'s context closed before the sign-in window opened.");
                return;
            }
            if (aStatus != "launched")
            {
                Note("SIGN_IN_FAILED: " + (aStatus.Length > 0 ? aStatus : (ar.Say.Length > 0 ? ar.Say : ar.Problem)));
                return;
            }
            Note("Antigravity is open for " + label + " (PID " + AsText(ae, "pid") + "). Finish the Google sign-in there -- this window keeps watching.");

            // 4. watch, quietly, until the credential probe says done. The
            // operator is typing into a different window; we never nag, never
            // time out into a false READY, and never open a second account to
            // "help" if they abandon it.
            var sw = Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < AuthPollBudgetMs)
            {
                Thread.Sleep(3000);
                CliRun r = AccountsExec("status \"" + label + "\"", 20000);
                var o = ParseEnv(r.Stdout);
                if (o == null) continue;
                string c2 = AsText(o, "context_state");
                string a2 = AsText(o, "auth_state");
                SetLiveState(label, c2, a2, AsText(o, "quota_state"));
                if (a2 == "AUTHENTICATED")
                {
                    CliRun br = AccountsExec("bind-identity \"" + label + "\"", 15000);
                    if (!br.Ok)
                    {
                        Note("BIND_FAILED: " + (br.Say.Length > 0 ? br.Say : (br.Problem.Length > 0 ? br.Problem : (label + " identity could not be bound."))));
                        LoadAccountsAsync();
                        return;
                    }
                    Note(label + " is signed in and ready.");
                    LoadAccountsAsync();
                    return;
                }
            }
            Note(label + " is still SIGN IN: finish the Google sign-in in the Antigravity window, then press SIGN IN again.");
        }

        void OpenConsoleFlow(string label, string workdir, string backend, string provider,
            string context)
        {
            // Fail closed, again, on the worker: the folder may have been
            // deleted or renamed while the operator was looking at it.
            if (!WorkdirUsable(workdir))
            {
                Note("WORKDIR_UNAVAILABLE: \"" + (workdir ?? "") + "\" cannot be opened. Choose another project folder.");
                return;
            }
            // 3. status.
            Note("Checking " + label + "...");
            CliRun st = AccountsExec("status \"" + label + "\"", 20000);
            // A nonzero exit is not automatically "no answer". The engine
            // reports some refusals (an offline context) with a real envelope
            // AND a nonzero code, and throwing that envelope away would turn
            // a legible answer into a shrug.
            var env = ParseEnv(st.Stdout);
            if (env == null)
            {
                Note("STATUS_FAILED: " + (st.Say.Length > 0 ? st.Say : st.Problem));
                return;
            }

            string ctx = AsText(env, "context_state");
            string auth = AsText(env, "auth_state");
            SetLiveState(label, ctx, auth, AsText(env, "quota_state"));

// 4 + 5. offline -> start the context, then wait a bounded time.
            if (ctx != "ONLINE")
            {
                // The operator's own Windows account has no separate broker to
                // start: asking Windows to log in as the person already logged
                // in would pop a sign-in prompt for your own account and buy
                // nothing. Go straight to the launch instead.
                if (IsCurrentUserContext(context))
                {
                    // fall through to the launch
                }
                else if (!ContextCapableFor(backend, provider))
                {
                    Note("CONTEXT_OFFLINE: " + label + " has no running context and this provider cannot create one.");
                    return;
                }
                else
                {
                    Note(backend == "managed_windows_user" ? "Starting " + label + " context via helper..." : "Starting " + label + " context...");
                    CliRun cs = AccountsExec("context start \"" + label + "\"", 30000);
                    if (!cs.Ok && ParseEnv(cs.Stdout) == null)
                    {
                        Note("CONTEXT_START_FAILED: " + (cs.Say.Length > 0 ? cs.Say : cs.Problem));
                        return;
                    }
                    string csSay = cs.Say;
                    if (csSay == "already_current_user" || csSay == "NO_CONTEXT_REQUIRED")
                    {
                        Note("CONTEXT_OFFLINE: " + label + " reports no separate context (" + csSay + ").");
                        return;
                    }
                    if (backend == "managed_windows_user")
                        Note("Waiting for managed context...");
                    else
                        Note("Waiting for Windows login...");
                    if (!WaitForOnline(label, out auth)) return;
                    Note(label + " context online.");
                }
            }

            // 6 + 7. auth. Signing in is a separate flow with its own button now,
            // so an unauthenticated console is never opened: that would drop the
            // operator into a project with a broken agent and no way to tell why.
            if (auth == "AUTH_REQUIRED")
            {
                Note("AUTH_REQUIRED: press SIGN IN for " + label + " first -- signing in no longer needs a project folder.");
                return;
            }

            // 7b. SAIPEN readiness, measured as the account (T-288). Only a
            // SAIPEN-managed folder is asked; an engine without the verb or an
            // unparseable answer changes nothing about the launch.
            string saipenSuffix = "";
            if (IsSaipenProject(workdir))
            {
                Note("Checking SAIPEN readiness for " + label + "...");
                CliRun rr = AccountsExec("saipen-readiness \"" + label + "\" --workdir \"" + workdir + "\"", 240000);
                var re = ParseEnv(rr.Stdout);
                string verdict = re != null ? AsText(re, "verdict") : "";
                if (verdict.Length > 0)
                {
                    string detail = SaipenDetailOf(re, verdict);
                    SetSaipenState(label, verdict, detail, workdir);
                    if (SaipenVerdictBlocksLaunch(verdict))
                    {
                        Note(SaipenNote(label, verdict, detail));
                        return;
                    }
                    if (verdict != "PROJECT_ACCESS_DENIED")
                        saipenSuffix = "  |  " + SaipenNote(label, verdict, detail);
                }
            }

            // 8. launch the exact account, exact workdir.
            Note("Launching " + label + "...");
            CliRun lr = AccountsExec("launch \"" + label + "\" --workdir \"" + workdir + "\"", 60000);
            var le = ParseEnv(lr.Stdout);
            string status = le != null ? AsText(le, "status") : "";
            string reason = le != null ? AsText(le, "reason") : "";
            // Empty until a grant is actually accepted, so the post-retry branch
            // can say "the ACE was written and still did not help" instead of
            // pretending no repair was tried.
            string grantStatus = "";

            // A managed account that cannot read the operator's project tree is
            // not a wrong folder -- the folder is valid and the managed SID
            // simply has no rights on it. That is the whole point of the reason
            // enum the engine now returns: this branch is chosen by a closed
            // set of values, never by matching text inside an error string.
            // The grant is narrow (one SID, one canonical path, minimum rights),
            // it is attempted ONCE, and the SAME path is retried ONCE. There is
            // no fallback directory and no second chance.
            if (status == "WORKDIR_UNAVAILABLE" && reason == "ACCESS_DENIED" && !IsCurrentUserContext(context))
            {
                Note("Granting " + label + " access to project...");
                CliRun gr = AccountsExec("grant-workdir \"" + label + "\" --workdir \"" + workdir + "\"", 30000);
                var ge = ParseEnv(gr.Stdout);
                string gs = ge != null ? AsText(ge, "status") : "";
                if (gs != "granted" && gs != "already_granted")
                {
                    Note(WorkdirGrantReason(label, workdir, ge));
                    return;
                }
                // Kept for the post-retry branch below: "the grant was accepted"
                // and "the grant was attempted" are different messages.
                grantStatus = gs;
                Note(label + " project access ready.");
                lr = AccountsExec("launch \"" + label + "\" --workdir \"" + workdir + "\"", 60000);
                le = ParseEnv(lr.Stdout);
                status = le != null ? AsText(le, "status") : "";
                reason = le != null ? AsText(le, "reason") : "";
            }

            if (status == "WORKDIR_UNAVAILABLE")
            {
                // Two failures, told apart by the engine's reason field: a path
                // that is not there is the operator's to fix by choosing another
                // folder, a path this account still cannot read is a permissions
                // problem we have already attempted to repair exactly once.
                //
                // The retry is also the effective-access proof: the engine hands
                // the folder to the managed identity as the child process's
                // working directory, so a launch that still comes back
                // ACCESS_DENIED means the ACE that was just written is not
                // effective -- a protected child DACL, a deny that outranks it,
                // or a link. That is a different problem from "no rights yet"
                // and it must not be reported as if a second grant would help.
                if (reason == "ACCESS_DENIED" && grantStatus.Length > 0)
                {
                    Note("Grant accepted (" + grantStatus + ") but " + label
                        + " still cannot enter \"" + workdir + "\". An existing ACL rule blocks it.");
                }
                else
                {
                    Note(reason == "ACCESS_DENIED"
                        ? "WORKDIR_ACCESS_DENIED: " + label + " could not access \"" + workdir + "\"."
                        : "WORKDIR_UNAVAILABLE: the engine would not take \"" + workdir + "\". Pick another folder.");
                }
                return;
            }
            if (!lr.Ok && status.Length == 0)
            {
                Note("LAUNCH_FAILED: " + (lr.Say.Length > 0 ? lr.Say : lr.Problem));
                return;
            }
            if (status == "launched")
            {
                Note("Launched " + label + " PID " + AsText(le, "pid") + "  (" + workdir + ")" + saipenSuffix);
                return;
            }
            string why = le != null ? AsText(le, "error") : "";
            Note(status.Length > 0
                ? (why.Length > 0 ? status + ": " + why : status)
                : "LAUNCH_FAILED: " + (lr.Say.Length > 0 ? lr.Say : lr.Problem));
        }

        // Bounded wait. The operator is typing into the runas sign-in dialog
        // right now, so this is generous -- but it is a deadline, and
        // hitting it is reported rather than left hanging. A field rather than
        // a const so a test can shrink it; nothing at runtime writes it.
        static int ContextStartBudgetMs = 180000;

        bool WaitForOnline(string label, out string auth)
        {
            auth = "";
            var sw = Stopwatch.StartNew();
            int tick = 0;
            while (sw.ElapsedMilliseconds < ContextStartBudgetMs)
            {
                Thread.Sleep(2000);
                if (sw.ElapsedMilliseconds > ContextStartBudgetMs) break;
                tick++;
                CliRun r = AccountsExec("status \"" + label + "\"", 20000);
                var o = ParseEnv(r.Stdout);
                if (o == null) continue;
                string ctx = AsText(o, "context_state");
                if (ctx == "ONLINE")
                {
                    auth = AsText(o, "auth_state");
                    SetLiveState(label, ctx, auth, AsText(o, "quota_state"));
                    return true;
                }
                // An explicit refusal ends the wait; only silence runs to the
                // deadline.
                if (tick % 4 == 0)
                    Note("Waiting for Windows login... (" + (sw.ElapsedMilliseconds / 1000) + "s)");
            }
            Note("CONTEXT_START_TIMEOUT: " + label + " did not come online within "
                + (ContextStartBudgetMs / 1000) + "s. Is the login prompt still waiting?");
            return false;
        }

        // Records the runtime facts onto the live row, from any thread.
        // ------------------- SAIPEN readiness (T-288) -------------------
        static bool IsSaipenProject(string workdir)
        {
            try { return !string.IsNullOrEmpty(workdir) && File.Exists(Path.Combine(workdir, ".saipen", "STATE.md")); }
            catch { return false; }
        }

        // A console whose agent cannot run SAIPEN at all helps nobody: these
        // classes have no in-console cure, so the launch stops and says why.
        // A lifecycle refusal does NOT stop it -- the agent in that console is
        // exactly who runs the repair SAIPEN names.
        static bool SaipenVerdictBlocksLaunch(string verdict)
        {
            return verdict == "RUNTIME_UNAVAILABLE" || verdict == "RUNTIME_STALE"
                || verdict == "HOST_IDENTITY_MISMATCH" || verdict == "ACCOUNT_DISABLED"
                || verdict == "CREDENTIALS_UNAVAILABLE";
        }

        static string SaipenDetailOf(Dictionary<string, object> env, string verdict)
        {
            var facts = env != null && env.ContainsKey("facts") ? env["facts"] as Dictionary<string, object> : null;
            Func<string, string, string> fact = delegate (string group, string key) {
                var g = facts != null && facts.ContainsKey(group) ? facts[group] as Dictionary<string, object> : null;
                object v = g != null && g.ContainsKey(key) ? g[key] : null;
                return v == null ? "" : Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
            };
            switch (verdict)
            {
                case "READY": return fact("project_admission", "next_action");
                case "PROJECT_LIFECYCLE_DENIED":
                {
                    string w = fact("project_admission", "write_admission");
                    string e = fact("project_admission", "error");
                    if (w.Length > 0 && w != "ADMITTED") return "write " + w;
                    return e.Length > 0 ? e : fact("project_admission", "bootstrap");
                }
                case "RUNTIME_UNAVAILABLE":
                case "RUNTIME_STALE": return fact("saipen_runtime_detail", "code");
                case "PROJECT_ACCESS_DENIED": return fact("project_access", "reason");
                default: return "";
            }
        }

        // The operator gets the class and the one step that changes it -- never
        // "retry cc" while nothing in the environment moved.
        static string SaipenNote(string label, string verdict, string detail)
        {
            string tail = detail.Length > 0 ? " (" + detail + ")" : "";
            switch (verdict)
            {
                case "READY": return "SAIPEN READY for " + label + tail;
                case "RUNTIME_UNAVAILABLE": return "SAIPEN RUNTIME_UNAVAILABLE for " + label + tail + ": the machine runtime is missing -- the operator installs it once with `saipen host machine install`";
                case "RUNTIME_STALE": return "SAIPEN RUNTIME_STALE for " + label + tail + ": refresh the machine runtime with `saipen host machine install` from the SAIPEN checkout";
                case "PROJECT_LIFECYCLE_DENIED": return "SAIPEN PROJECT_LIFECYCLE_DENIED for " + label + tail + ": the agent opens with `saipen continue --json`, which names the canonical repair";
                case "PROJECT_ACCESS_DENIED": return "SAIPEN PROJECT_ACCESS_DENIED for " + label + tail;
                case "CREDENTIALS_UNAVAILABLE": return "SAIPEN CREDENTIALS_UNAVAILABLE for " + label + ": press SIGN IN first";
                case "BROKER_UNAVAILABLE": return "SAIPEN BROKER_UNAVAILABLE for " + label + ": Connect the account, then open the console";
                case "BROKER_STALE": return "SAIPEN BROKER_STALE for " + label + ": Disconnect and Connect once so the context runs the installed engine";
                case "HOST_IDENTITY_MISMATCH": return "SAIPEN HOST_IDENTITY_MISMATCH for " + label + ": the context does not run as this account -- Disconnect and Connect it";
                case "ACCOUNT_DISABLED": return "SAIPEN ACCOUNT_DISABLED for " + label;
                default: return "SAIPEN " + verdict + " for " + label + tail;
            }
        }

        void SetSaipenState(string compact, string verdict, string detail, string workdir)
        {
            TryBeginInvoke(delegate {
                AccountRow cur = _accounts != null ? _accounts.Find(r => r.Compact == compact) : null;
                if (cur == null) return;
                cur.SaipenVerdict = verdict; cur.SaipenDetail = detail; cur.SaipenWorkdir = workdir;
                Refresh();
            });
        }

        void SetLiveState(string compact, string ctx, string auth, string quota)
        {
            TryBeginInvoke(delegate {
                AccountRow cur = _accounts != null ? _accounts.Find(r => r.Compact == compact) : null;
                if (cur == null) return;
                cur.ContextState = ctx; cur.AuthState = auth; cur.QuotaState = quota;
                Refresh();
            });
        }

        void AccountsRename()
        {
            AccountRow row = SelectedAccount();
            if (row == null) { Note("no account selected"); return; }
            string name = AskText("Rename account", "Display name for " + row.Compact + ":", row.Name);
            if (name == null || name.Trim().Length == 0) return;
            string compact = row.Compact;
            string clean = name.Trim().Replace("\"", "");
            DispatchAccounts(delegate {
                CliRun r = AccountsExec("rename \"" + compact + "\" \"" + clean + "\"", 15000);
                string say = r.Ok ? r.Say : "FAILED: " + (r.Say.Length > 0 ? r.Say : r.Problem);
                LoadAccountsAsync();
                Note(say + "  |  " + SyncConsolesMenu());
            });
        }

        string AskText(string title, string label, string initial)
        {
            using (var f = new Form())
            using (var lbl = new Label())
            using (var box = new TextBox())
            using (var ok = new Button())
            using (var cancel = new Button())
            {
                f.Text = title;
                f.FormBorderStyle = FormBorderStyle.FixedDialog;
                f.StartPosition = FormStartPosition.CenterParent;
                f.MinimizeBox = false; f.MaximizeBox = false;
                f.ClientSize = new Size(360, 104);
                f.BackColor = Palette.BG; f.ForeColor = Palette.TEXT;
                f.Font = new Font("Verdana", 8.25f);
                lbl.Text = label; lbl.AutoSize = true;
                lbl.Location = new Point(10, 12); lbl.BackColor = Palette.BG; lbl.ForeColor = Palette.TEXT;
                box.SetBounds(10, 34, 340, 22); box.Text = initial; box.BorderStyle = BorderStyle.FixedSingle;
                ok.Text = "OK"; ok.DialogResult = DialogResult.OK;
                ok.SetBounds(190, 68, 76, 24); ok.FlatStyle = FlatStyle.Flat;
                cancel.Text = AccountsLoc.T("Cancel"); cancel.DialogResult = DialogResult.Cancel;
                cancel.SetBounds(274, 68, 76, 24); cancel.FlatStyle = FlatStyle.Flat;
                f.Controls.AddRange(new Control[] { lbl, box, ok, cancel });
                f.AcceptButton = ok; f.CancelButton = cancel;
                return f.ShowDialog(this) == DialogResult.OK ? box.Text : null;
            }
        }

        void AccountsDeleteContext()
        {
            AccountRow row = SelectedAccount();
            if (row == null) { Note("no account selected"); return; }
            if (SaitulsDialog.Show(this,
                "Delete managed context for " + row.Compact + " (" + row.Name + ")?\n\nThis permanently removes the managed Windows account and secret. All local context data will be deleted.\n\nAre you sure?",
                "Delete Managed Context", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                return;
            string compact = row.Compact;
            DispatchAccounts(delegate {
                Note("Deleting context " + compact + "...");
                CliRun r = AccountsExec("context delete \"" + compact + "\" --force", 30000);
                string say = (r.Ok && !r.Say.Contains("cleanup_required") && !r.Say.Contains("failed")) ? "Context deleted." : "DELETE_FAILED: " + (r.Say.Length > 0 ? r.Say : r.Problem);
                LoadAccountsAsync();
                Note(say + "  |  " + SyncConsolesMenu());
            });
        }

        void AccountsMigrateContext()
        {
            AccountRow row = SelectedAccount();
            if (row == null) { Note("no account selected"); return; }
            if (SaitulsDialog.Show(this,
                "Convert " + row.Compact + " (" + row.Name + ") to a managed context?\n\nConverting to a managed secret invalidates cached Windows logon tokens and requires Google sign-in.\n\nProceed?",
                "Convert to Managed Context", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                return;
            string compact = row.Compact;
            DispatchAccounts(delegate {
                Note("Converting " + compact + " to managed context...");
                CliRun r = AccountsExec("context migrate \"" + compact + "\"", 30000);
                if (!r.Ok)
                {
                    string err = r.Say.Length > 0 ? r.Say : r.Problem;
                    Note("MIGRATE_FAILED: " + err);
                    LoadAccountsAsync();
                    return;
                }
                Note("Managed context converted. Starting context and verifying auth...");
                AccountsExec("context start \"" + compact + "\"", 30000);
                CliRun rStatus = AccountsExec("status \"" + compact + "\"", 15000);
                bool needsAuth = !rStatus.Ok || rStatus.Say.IndexOf("AUTH_REQUIRED", StringComparison.OrdinalIgnoreCase) >= 0 || rStatus.Say.IndexOf("NEEDS_AUTH", StringComparison.OrdinalIgnoreCase) >= 0;
                if (needsAuth)
                {
                    Note("Authentication required. Launching browser sign-in...");
                    AccountsExec("launch \"" + compact + "\"", 15000);
                    for (int i = 0; i < 15; i++)
                    {
                        Thread.Sleep(1000);
                        CliRun checkRun = AccountsExec("status \"" + compact + "\"", 5000);
                        if (checkRun.Ok && checkRun.Say.IndexOf("AUTH_REQUIRED", StringComparison.OrdinalIgnoreCase) < 0)
                        {
                            needsAuth = false;
                            break;
                        }
                    }
                }
                LoadAccountsAsync();
                Note(needsAuth ? "Context converted. Complete Google sign-in in browser." : "Converted to managed context and verified.  |  " + SyncConsolesMenu());
            });
        }

        /// One dropdown row, painted by hand.
        ///
        /// A ComboBox in DropDownList style opens a NATIVE list, and the native
        /// list paints itself in the system colours — that white rectangle was
        /// the light leak the operator reported. Owner-drawing replaces it: this
        /// is the only thing that decides what an item looks like, closed and
        /// open, and it is also what the regression tests read pixels from, so
        /// "themed" is a measurement rather than a claim.
        ///
        /// Public and static on purpose: the test harness calls exactly this
        /// method rather than reproducing its arithmetic.
        public static void PaintProviderRow(Graphics g, Rectangle r, string text, string tag,
                                           bool selected, bool hot, Font font)
        {
            // The selected row has to stay readable, which for this palette means
            // a RAISED fill under ordinary text -- the gold is for the tag, so
            // the name never has to fight a bright background for legibility.
            Color back = selected ? Palette.RAISED : (hot ? Palette.SURFACE : Palette.BG_SOFT);
            using (var fill = new SolidBrush(back)) g.FillRectangle(fill, r);

            // A selected row also gets a gold edge, so the highlight survives a
            // palette someone later darkens.
            if (selected)
                using (var edge = new Pen(Palette.BHL))
                    g.DrawRectangle(edge, r.X, r.Y, r.Width - 1, r.Height - 1);

            bool hasTag = !string.IsNullOrEmpty(tag);
            int tagW = hasTag ? MeasureTagWidth(g, tag, font) : 0;
            var textRect = new Rectangle(r.X + 6, r.Y, Math.Max(10, r.Width - 12 - tagW), r.Height);

            using (var tf = new StringFormat(StringFormatFlags.NoWrap))
            {
                tf.Trimming = StringTrimming.EllipsisCharacter;
                tf.LineAlignment = StringAlignment.Center;
                tf.FormatFlags |= StringFormatFlags.NoClip;
                using (var tb = new SolidBrush(Palette.TEXT))
                    g.DrawString(text, font, tb, textRect, tf);
                if (hasTag)
                {
                    var tagRect = new Rectangle(r.Right - tagW - 6, r.Y, tagW, r.Height);
                    using (var gb = new SolidBrush(selected ? Palette.BHL : Palette.MUTED))
                        g.DrawString(tag, font, gb, tagRect, tf);
                }
            }
        }

        static int MeasureTagWidth(Graphics g, string tag, Font font)
        {
            if (string.IsNullOrEmpty(tag)) return 0;
            SizeF s = g.MeasureString(tag, font);
            return (int)Math.Ceiling(s.Width);
        }

        /// Build the Add Account dialog without showing it.
        ///
        /// Public and static so the regression tests drive the SAME widgets the
        /// operator sees, instead of a second description of them that could
        /// drift into telling the truth about a dialog nobody has. The returned
        /// Form owns every control; the caller disposes it.
        public static Form BuildAddAccountDialog(out ComboBox cmbProvider, out TextBox txtAlias,
                                                   out Label lblNote, out Button btnOk)
        {
            var f = new Form();
            var lblProvider = new Label();
            var lblAlias = new Label();
            var btnCancel = new Button();
            // Locals, assigned to the out parameters at the end: an out
            // parameter may not be captured by a closure, and the selection
            // handler below has to close over every control it refreshes.
            var combo = FolderAutomation.FaUi.Combo(16, 30, 388);
            var aliasBox = new TextBox();
            var note = new Label();
            var ok = new Button();

            f.Text = AccountsLoc.T("AddAccountTitle");
            f.FormBorderStyle = FormBorderStyle.FixedDialog;
            f.StartPosition = FormStartPosition.CenterParent;
            f.MinimizeBox = false; f.MaximizeBox = false;
            f.ClientSize = new Size(420, 210);
            f.BackColor = Palette.BG; f.ForeColor = Palette.TEXT;
            f.Font = new Font("Verdana", 8.25f);

            lblProvider.Text = AccountsLoc.T("AddProvider");
            lblProvider.SetBounds(16, 12, 388, 16);
            lblProvider.ForeColor = Palette.TEXT2;

            // The shared combo paints the closed field and arrow as well as
            // dropdown rows. The catalog row painter adds readiness tags.
            combo.DropDownStyle = ComboBoxStyle.DropDownList;
            combo.DrawMode = DrawMode.OwnerDrawFixed;
            combo.FlatStyle = FlatStyle.Flat;
            combo.BackColor = Palette.BG_SOFT;
            combo.ForeColor = Palette.TEXT;
            combo.ItemHeight = 20;
            combo.Items.AddRange(ProviderCatalog.ListTexts());
            combo.SelectedIndex = 0;
            combo.SetBounds(16, 30, 388, 24);
            combo.DrawItem += delegate (object sender, DrawItemEventArgs e)
            {
                if (e.Index < 0 || e.Index >= ProviderCatalog.Count) return;
                ProviderEntry entry = ProviderCatalog.At(e.Index);
                e.DrawBackground();
                PaintProviderRow(e.Graphics, e.Bounds, entry.DisplayName,
                    entry.AvailabilityTag,
                    (e.State & DrawItemState.Selected) == DrawItemState.Selected,
                    false, f.Font);
            };

            lblAlias.Text = AccountsLoc.T("AddAlias");
            lblAlias.SetBounds(16, 64, 388, 16);
            lblAlias.ForeColor = Palette.TEXT2;

            // A TextBox is as white as a ComboBox until it is told not to be.
            aliasBox.SetBounds(16, 82, 388, 22);
            aliasBox.BorderStyle = BorderStyle.FixedSingle;
            aliasBox.BackColor = Palette.BG_SOFT;
            aliasBox.ForeColor = Palette.TEXT;

            note.SetBounds(16, 112, 388, 34);
            note.ForeColor = Palette.TEXT2;

            ok.Text = AccountsLoc.T("AddAccountConfirm");
            ok.DialogResult = DialogResult.OK;
            ok.SetBounds(214, 168, 116, 28);
            ok.FlatStyle = FlatStyle.Flat;
            ok.BackColor = Palette.RAISED; ok.ForeColor = Palette.BHL;

            btnCancel.Text = AccountsLoc.T("Cancel");
            btnCancel.DialogResult = DialogResult.Cancel;
            btnCancel.SetBounds(336, 168, 68, 28);
            btnCancel.FlatStyle = FlatStyle.Flat;
            btnCancel.BackColor = Palette.SURFACE; btnCancel.ForeColor = Palette.TEXT2;

            // The selection drives everything below it: what the note says,
            // whether the alias means anything, and whether the button is a
            // promise or a decoration. No provider-specific branch anywhere.
            Action refresh = delegate {
                ProviderEntry entry = ProviderCatalog.At(combo.SelectedIndex);
                if (entry == null) return;
                note.Text = entry.OperatorNote();
                ok.Enabled = entry.SupportsAddNow;
                aliasBox.Enabled = entry.SupportsAddNow;
                lblAlias.ForeColor = entry.SupportsAddNow ? Palette.TEXT2 : Palette.MUTED;
            };
            combo.SelectedIndexChanged += delegate { refresh(); };
            refresh();

            f.Controls.AddRange(new Control[] { lblProvider, combo, lblAlias, aliasBox, note, ok, btnCancel });
            f.AcceptButton = ok; f.CancelButton = btnCancel;

            cmbProvider = combo; txtAlias = aliasBox; lblNote = note; btnOk = ok;
            return f;
        }

        void AddAccountWizard()
        {
            ComboBox cmbProvider; TextBox txtAlias; Label lblNote; Button btnOk;
            using (var f = BuildAddAccountDialog(out cmbProvider, out txtAlias, out lblNote, out btnOk))
            {
                if (f.ShowDialog(this) != DialogResult.OK) return;

                // Read the provider from the catalog, never from the index: an
                // item that is not addable resolves to no provider at all, so a
                // disabled button that somehow fired cannot name a backend.
                string prov = ProviderCatalog.EngineProviderId(cmbProvider.SelectedIndex);
                if (prov.Length == 0) return;
                string alias = txtAlias.Text.Trim();
                if (alias.Length == 0) alias = null;

                DispatchAccounts(delegate {
                    AddAccountFlow(prov, alias);
                });
            }
        }

        void AddAccountFlow(string provider, string alias)
        {
            // The dialog already disables the button for a provider that has no
            // backend. This is the second half of the same promise: if anything
            // else ever calls this with a name the catalog does not mark addable,
            // nothing is created. A planned provider must leave no account behind,
            // because a half-account with no way to finish signing in is worse than
            // no account -- it is a broken account wearing an available label.
            ProviderEntry known = ProviderCatalog.ById(provider);
            if (known == null || !known.SupportsAddNow)
            {
                Note("ADD_REFUSED: " + (known == null
                    ? "Unknown provider '" + provider + "'."
                    : known.DisplayName + " is not available for account creation in this build."));
                return;
            }

            Note("Preparing account context...");
            string cmd = "add --provider " + provider;
            if (!string.IsNullOrEmpty(alias)) cmd += " --alias \"" + alias.Replace("\"", "") + "\"";
            CliRun r = AccountsExec(cmd, 30000);
            var env = ParseEnv(r.Stdout);
            if (!r.Ok || env == null)
            {
                string problem = r.Say.Length > 0 ? r.Say : r.Problem;
                if (problem.Contains("helper_unreachable"))
                    Note("ADD_FAILED: SAI Accounts Helper is not running. Install it as Administrator via 'sai-accounts.exe helper install'.");
                else
                    Note("ADD_FAILED: " + problem);
                return;
            }

            string accId = AsText(env, "account_id");
            string backend = AsText(env, "execution_backend");
            // The compact label the engine will accept and the row the live
            // state is written back onto -- read from the reload, not guessed
            // from the display name, so a fresh account cannot be reported
            // against some other row's slot.
            List<AccountRow> rows = LoadAccountsAsync();
            string label = accId;
            if (rows != null)
            {
                AccountRow created = rows.Find(x => x.Id == accId);
                if (created != null && !string.IsNullOrEmpty(created.Compact)) label = created.Compact;
            }

            if (provider == "antigravity")
            {
                // Signing in is an ACCOUNT-PLANE operation and nothing else, so
                // the add path uses the same routine as Re-authenticate. It used
                // to drive Google auth with a project `launch --workdir`: a
                // folder had to exist, be readable by a managed SID and survive a
                // 60s child before the sign-in window could open at all, and the
                // CliRun was discarded, so every refusal (launch_failed,
                // WORKDIR_UNAVAILABLE, CONTEXT_OFFLINE) surfaced as the same
                // 90-second "waiting for Google sign-in verification" -- and a
                // browser that never opened answered with a "Windows cannot
                // find" popup. `auth` takes no workdir and reports its own
                // answer immediately; that answer is what the operator now sees.
                Note("Account created: " + label);
                SignInFlow(label, backend, provider, AsText(env, "windows_user"));
                LoadAccountsAsync();
            }
            else
            {
                Note("Account created: " + label + "  |  " + SyncConsolesMenu());
                LoadAccountsAsync();
            }
        }

// ---- Folder Automation (Organizer tab) ---------------------------------
        // Compact status view only. Every real edit opens its own window from
        // FolderAutomation\FolderAutomationDialogs.cs.
        string _faSelected = "";
        string _faNote = "";

        Saituls.FolderAutomation.FaEngine FaEngine()
        {
            return Program.Fa;
        }

        List<Saituls.FolderAutomation.FaProfile> FaProfiles()
        {
            Saituls.FolderAutomation.FaEngine e = FaEngine();
            if (e == null) return new List<Saituls.FolderAutomation.FaProfile>();
            return e.Config.Profiles;
        }

        Saituls.FolderAutomation.FaProfile FaSelected()
        {
            Saituls.FolderAutomation.FaEngine e = FaEngine();
            if (e == null) return null;
            return e.FindProfile(_faSelected);
        }

        void DrawOrganizerTab(Graphics g, int x, int y, int w, int h)
        {
            int top = y;
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            bool on = eng != null && eng.Config.EngineEnabled;

            var toggle = new Rectangle(x, y, 190, 24);
            Buttons.Add(new ButtonDef { R = toggle, A = () => FaToggleEngine() });
            DrawButton(g, toggle, AccountsLoc.T(on ? "FaOn" : "FaOff"), false, 11);

            DrawText(g, Fit(g, AccountsLoc.T(eng == null ? "Loading" :
                eng.PausedAll ? "FaPausedAll" : on ? "FaBackgroundHint" : "FaOffHint"), w - 198, 10),
                x + 198, y + 6, on ? Palette.TEXT2 : Palette.MUTED, 10);

            List<Saituls.FolderAutomation.FaProfile> profiles = FaProfiles();
            if (string.IsNullOrEmpty(_faSelected) && profiles.Count > 0)
                _faSelected = profiles[0].Id;

            // Two-column layout: Left column = Profiles & Actions (250px)
            //                    Right column = Selected Profile Folders & Destination (remaining width)
            int colW = 250;
            int ly = y + 30;

            DrawText(g, AccountsLoc.T("FaProfiles"), x, ly, Palette.LINK, 11, true);
            ly += 18;

            if (profiles.Count == 0)
            {
                DrawText(g, AccountsLoc.T("FaNoProfiles"), x, ly, Palette.MUTED, 10);
                ly += 22;
            }
            for (int i = 0; i < profiles.Count; i++)
            {
                Saituls.FolderAutomation.FaProfile p = profiles[i];
                string id = p.Id;
                var row = new Rectangle(x, ly, colW, 20);
                bool sel = string.Equals(_faSelected, id, StringComparison.Ordinal);
                Buttons.Add(new ButtonDef { R = row, A = () => { _faSelected = id; Refresh(); } });
                DrawButton(g, row, FaProfileLabel(p, eng), sel, 10);
                ly += 22;
            }

            int by = ly + 4;
            int btnW3 = (colW - 8) / 3;
            int bx = x;
            AddFaButton(g, AccountsLoc.T("FaAdd"), ref bx, by, FaAddProfile, btnW3);
            AddFaButton(g, AccountsLoc.T("FaEdit"), ref bx, by, FaEditProfile, btnW3);
            AddFaButton(g, AccountsLoc.T("Remove"), ref bx, by, FaRemoveProfile, btnW3);
            by += 26;
            bx = x;
            int btnW2 = (colW - 4) / 2;
            AddFaButton(g, AccountsLoc.T("FaRunNow"), ref bx, by, () => FaRunNow(false), btnW2);
            AddFaButton(g, AccountsLoc.T("FaDryRun"), ref bx, by, () => FaRunNow(true), btnW2);
            by += 26;
            bx = x;
            AddFaButton(g, AccountsLoc.T("FaHistory"), ref bx, by, FaShowHistory, btnW2);
            AddFaButton(g, AccountsLoc.T(eng != null && eng.PausedAll ? "FaResumeAll" : "FaPauseAll"), ref bx, by, FaPauseAll, btnW2);

            int ty = by + 30;
            DrawText(g, AccountsLoc.T("FaTemplates"), x, ty, Palette.LINK, 11, true);
            int tby = ty + 18;
            bx = x;
            AddFaButton(g, AccountsLoc.T("FaDownloads"), ref bx, tby, () => FaAddTemplate(1), 132);
            AddFaButton(g, AccountsLoc.T("FaClipboard"), ref bx, tby, () => FaAddTemplate(2), 112);
            int leftBottom = tby + 28;

            // Right Column: Active Profile Details, Folders (Sources) & Destination
            int rx = x + colW + 14;
            int rw = w - (colW + 14);
            int ry = y + 30;

            Saituls.FolderAutomation.FaProfile selP = FaSelected();
            int rightBottom = ry;
            if (selP != null)
            {
                DrawText(g, Fit(g, AccountsLoc.T("FaProfile", selP.Name), rw, 11), rx, ry, Palette.LINK, 11, true);
                string modeStr = AccountsLoc.T(selP.Enabled ? "FaEnabled" : "FaDisabled") + " • " + AccountsLoc.T(selP.Recursive ? "FaRecursive" : "FaTopLevel");
                DrawText(g, Fit(g, modeStr, rw, 10), rx, ry + 16, selP.Enabled ? Palette.TEXT : Palette.MUTED, 10);

                int sy = ry + 36;
                DrawText(g, AccountsLoc.T("FaSources"), rx, sy, Palette.TEXT, 10, true);
                sy += 16;

                if (selP.Sources.Count == 0)
                {
                    DrawText(g, "• " + AccountsLoc.T("FaNoSources"), rx + 2, sy, Palette.MUTED, 10);
                    sy += 18;
                }
                else
                {
                    for (int i = 0; i < selP.Sources.Count; i++)
                    {
                        string srcPath = selP.Sources[i];
                        var delR = new Rectangle(rx + rw - 20, sy - 1, 18, 16);
                        Buttons.Add(new ButtonDef { R = delR, A = () => FaRemoveSourceFolder(selP, srcPath) });
                        DrawButton(g, delR, "x", false, 9);
                        DrawText(g, Fit(g, "• " + srcPath, rw - 28, 10), rx + 2, sy, Palette.TEXT, 10);
                        sy += 18;
                    }
                }

                // Folder action buttons
                var addFld = new Rectangle(rx, sy + 2, 150, 22);
                Buttons.Add(new ButtonDef { R = addFld, A = () => FaAddSourceFolder(selP) });
                DrawButton(g, addFld, AccountsLoc.T("FaAddFolder"), false, 10);

                var opnFld = new Rectangle(rx + 156, sy + 2, Math.Min(150, rw - 156), 22);
                Buttons.Add(new ButtonDef { R = opnFld, A = () => FaOpenSelectedSource(selP) });
                DrawButton(g, opnFld, AccountsLoc.T("OpenFolder"), false, 10);
                sy += 30;

                // Destination
                DrawText(g, Fit(g, AccountsLoc.T("FaDestination"), rw, 10), rx, sy, Palette.TEXT, 10, true);
                sy += 16;
                string destStr = FaDestinationDescription(selP);
                DrawText(g, Fit(g, "• " + destStr, rw - 2, 10), rx + 2, sy, Palette.TEXT2, 10);
                sy += 16;
                string rulesStr = AccountsLoc.T("FaRulesCount", selP.Rules.Count.ToString()) + " (" + FaRulesSummary(selP) + ")";
                DrawText(g, Fit(g, "• " + rulesStr, rw - 2, 9), rx + 2, sy, Palette.MUTED, 9);
                rightBottom = sy + 20;
            }
            else
            {
                DrawText(g, AccountsLoc.T("FaFolderSetup"), rx, ry, Palette.LINK, 11, true);
                DrawText(g, AccountsLoc.T("FaNoSelection"), rx, ry + 18, Palette.MUTED, 10);
                DrawText(g, Fit(g, AccountsLoc.T("FaPickHint"), rw, 10), rx, ry + 42, Palette.TEXT, 10);

                var qkAdd = new Rectangle(rx, ry + 66, 190, 24);
                Buttons.Add(new ButtonDef { R = qkAdd, A = FaQuickAddFolder });
                DrawButton(g, qkAdd, AccountsLoc.T("FaQuickAdd"), false, 10);

                DrawText(g, Fit(g, AccountsLoc.T("FaQuickHint"), rw, 9), rx, ry + 96, Palette.MUTED, 9);
                rightBottom = ry + 130;
            }

            // Lower area: Engine status & Notes
            int ey = Math.Max(leftBottom, rightBottom) + 10;
            if (ey < 270) ey = 270;
            DrawText(g, AccountsLoc.T("FaEngine"), x, ey, Palette.LINK, 11, true);
            ey += 16;
            int watchers = 0;
            string next = AccountsLoc.T("FaNotScheduled");
            if (eng != null)
            {
                foreach (var r in eng.AllRuntimes())
                    if (r.Watcher != null && r.Watcher.ActiveWatchCount > 0) watchers++;
                DateTime? soonest = null;
                foreach (var r in eng.AllRuntimes())
                {
                    if (!r.Profile.Enabled || !r.NextRun.HasValue) continue;
                    if (!soonest.HasValue || r.NextRun.Value < soonest.Value) soonest = r.NextRun.Value;
                }
                if (soonest.HasValue) next = soonest.Value.ToString("yyyy-MM-dd HH:mm");
            }
            DrawStatus(g, x, ey, AccountsLoc.T("FaBackground"), eng != null, AccountsLoc.T(eng == null ? "FaNotLoaded" : eng.PausedAll ? "FaPaused" : on ? "TaskbarRunning" : "FaDisabled"));
            DrawStatus(g, x, ey + 20, AccountsLoc.T("FaWatchers"), true, watchers.ToString());
            DrawStatus(g, x, ey + 40, AccountsLoc.T("FaNextRun"), true, next);
            if (!string.IsNullOrEmpty(_faNote))
                DrawText(g, Fit(g, _faNote, w, 10), x, ey + 64, Palette.DANGERTXT, 10);
            else if (!string.IsNullOrEmpty(eng == null ? "" : eng.ConfigError))
                DrawText(g, Fit(g, eng.ConfigError, w, 10), x, ey + 64, Palette.DANGERTXT, 10);
            _tabContentH["Organizer"] = ey + 90 - top + 8;
        }

        void AddFaButton(Graphics g, string label, ref int x, int y, Action act, int width = 84)
        {
            var r = new Rectangle(x, y, width, 24);
            Buttons.Add(new ButtonDef { R = r, A = act, Label = label });
            DrawButton(g, r, label, false, TextW(g, label, 10) > width - 8 ? 9 : 10);
            x += width + 6;
        }

        string FaProfileLabel(Saituls.FolderAutomation.FaProfile p, Saituls.FolderAutomation.FaEngine eng)
        {
            string state = AccountsLoc.T(p.Enabled ? "FaEnabled" : "FaDisabled");
            string tick = p.Enabled ? "[x]" : "[ ]";
            return string.Format("{0} {1,-14} {2}", tick, Trunc(p.Name, 14), state);
        }

        static string Trunc(string s, int n)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Length <= n ? s : s.Substring(0, n - 1) + "~";
        }

        static string TruncPath(string path, int maxChars)
        {
            if (string.IsNullOrEmpty(path)) return "";
            if (path.Length <= maxChars) return path;
            string root = Path.GetPathRoot(path);
            string leaf = Path.GetFileName(path);
            if (string.IsNullOrEmpty(leaf)) leaf = path;
            string combined = (root ?? "") + "...\\" + leaf;
            if (combined.Length <= maxChars) return combined;
            return path.Substring(0, Math.Max(0, maxChars - 3)) + "...";
        }

        static string FaDestinationDescription(Saituls.FolderAutomation.FaProfile p)
        {
            if (p == null) return "";
            if (!string.IsNullOrEmpty(p.DestinationRoot))
                return AccountsLoc.T("FaDestinationRoot", p.DestinationRoot);
            return AccountsLoc.T("FaInPlace");
        }

        static string FaRulesSummary(Saituls.FolderAutomation.FaProfile p)
        {
            if (p == null || p.Rules.Count == 0) return AccountsLoc.T("FaNoRules");
            List<string> names = new List<string>();
            for (int i = 0; i < p.Rules.Count && i < 3; i++) names.Add(p.Rules[i].Name);
            string s = string.Join(", ", names.ToArray());
            if (p.Rules.Count > 3) s += ", ...";
            return s;
        }

        void FaQuickAddFolder()
        {
            using (var fb = new FolderBrowserDialog())
            {
                fb.Description = AccountsLoc.T("FaPickFolder");
                if (fb.ShowDialog(this) != DialogResult.OK) return;
                string path = fb.SelectedPath;
                if (string.IsNullOrEmpty(path) || !Directory.Exists(path)) return;

                Saituls.FolderAutomation.FaEngine eng = FaEngine();
                if (eng == null) return;

                string folderName = Path.GetFileName(path);
                if (string.IsNullOrEmpty(folderName)) folderName = path;

                var made = Saituls.FolderAutomation.FaTemplates.DownloadsByType(path);
                if (made.Count > 0)
                {
                    made[0].Name = "Organize " + folderName;
                    made[0].Enabled = true;
                    eng.Config.Profiles.Add(made[0]);
                    eng.SaveConfig();
                    eng.ReloadProfiles();
                    _faSelected = made[0].Id;
                    _faNote = "Created profile for " + folderName + ".";
                    Refresh();
                }
            }
        }

        void FaAddSourceFolder(Saituls.FolderAutomation.FaProfile p)
        {
            if (p == null) return;
            using (var fb = new FolderBrowserDialog())
            {
                fb.Description = "Select additional folder to organize with '" + p.Name + "'";
                if (p.Sources.Count > 0 && Directory.Exists(p.Sources[0])) fb.SelectedPath = p.Sources[0];
                if (fb.ShowDialog(this) != DialogResult.OK) return;
                string path = fb.SelectedPath;
                if (string.IsNullOrEmpty(path) || !Directory.Exists(path)) return;

                if (!p.Sources.Contains(path))
                {
                    p.Sources.Add(path);
                    Saituls.FolderAutomation.FaEngine eng = FaEngine();
                    if (eng != null) { eng.SaveConfig(); eng.ReloadProfiles(); }
                    _faNote = "Added folder: " + Path.GetFileName(path);
                    Refresh();
                }
                else
                {
                    _faNote = "Folder is already in profile sources.";
                    Refresh();
                }
            }
        }

        void FaRemoveSourceFolder(Saituls.FolderAutomation.FaProfile p, string path)
        {
            if (p == null) return;
            p.Sources.Remove(path);
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            if (eng != null) { eng.SaveConfig(); eng.ReloadProfiles(); }
            _faNote = "Removed folder: " + Path.GetFileName(path);
            Refresh();
        }

        void FaOpenSelectedSource(Saituls.FolderAutomation.FaProfile p)
        {
            if (p == null || p.Sources.Count == 0) return;
            string path = p.Sources[0];
            if (Directory.Exists(path)) Process.Start("explorer.exe", path);
            else { _faNote = "Folder does not exist: " + path; Refresh(); }
        }

        void FaToggleEngine()
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            if (eng == null) return;
            eng.Config.EngineEnabled = !eng.Config.EngineEnabled;
            eng.SaveConfig();
            eng.ReloadProfiles();
            _faNote = eng.Config.EngineEnabled ? "" : "The engine is off. Nothing is moved until you turn it back on.";
            Refresh();
        }

        void FaAddProfile()
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            if (eng == null) return;
            using (var f = new Saituls.FolderAutomation.FaProfileEditorForm(null, eng.Config))
            {
                if (f.ShowDialog(this) != DialogResult.OK) return;
                eng.Config.Profiles.Add(f.Profile);
                eng.SaveConfig();
                eng.ReloadProfiles();
                _faSelected = f.Profile.Id;
                _faNote = "";
            }
            Refresh();
        }

        void FaEditProfile()
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            Saituls.FolderAutomation.FaProfile p = FaSelected();
            if (eng == null || p == null) { _faNote = "Pick a profile first."; Refresh(); return; }
            using (var f = new Saituls.FolderAutomation.FaProfileEditorForm(p, eng.Config))
            {
                if (f.ShowDialog(this) != DialogResult.OK) return;
                int i = eng.Config.Profiles.IndexOf(p);
                eng.Config.Profiles[i] = f.Profile;
                eng.SaveConfig();
                eng.ReloadProfiles();
                _faSelected = f.Profile.Id;
                _faNote = "";
            }
            Refresh();
        }

        void FaRemoveProfile()
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            Saituls.FolderAutomation.FaProfile p = FaSelected();
            if (eng == null || p == null) { _faNote = "Pick a profile first."; Refresh(); return; }
            if (SaitulsDialog.Show(this, "Remove profile '" + p.Name + "'?\nFiles already moved are not affected.",
                "SAITULS - Folder Automation", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK) return;
            eng.Config.Profiles.Remove(p);
            eng.SaveConfig();
            eng.ReloadProfiles();
            _faSelected = "";
            _faNote = "";
            Refresh();
        }

        void FaRunNow(bool dry)
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            Saituls.FolderAutomation.FaProfile p = FaSelected();
            if (eng == null) return;
            if (p == null)
            {
                // No selection means "every enabled profile", which is what the
                // button means to somebody who never clicked a row.
                var reports = eng.RunEveryEnabled(dry, "manual");
                if (reports.Count == 0) { _faNote = "No enabled profile to run."; Refresh(); return; }
                int acts = 0, errs = 0;
                foreach (var r in reports) { acts += r.Actions; errs += r.Errors; }
                _faNote = (dry ? "Dry run: " : "") + acts + " action(s), " + errs + " error(s) across " + reports.Count + " profile(s).";
                if (dry && reports.Count > 0) using (var d = new Saituls.FolderAutomation.FaDryRunForm(reports[0])) d.ShowDialog(this);
                Refresh();
                return;
            }
            var rep = eng.RunProfile(p.Id, dry, "manual");
            if (rep == null) return;
            _faNote = rep.Summary() + (string.IsNullOrEmpty(rep.PausedReason) ? "" : " -- " + rep.PausedReason);
            if (dry) using (var d = new Saituls.FolderAutomation.FaDryRunForm(rep)) d.ShowDialog(this);
            Refresh();
        }

        void FaShowHistory()
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            if (eng == null) return;
            using (var f = new Saituls.FolderAutomation.FaHistoryForm(eng)) f.ShowDialog(this);
            Refresh();
        }

        void FaPauseAll()
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            if (eng == null) return;
            eng.PausedAll = !eng.PausedAll;
            _faNote = eng.PausedAll ? "All Folder Automation is paused." : "";
            Refresh();
        }

        void FaAddTemplate(int which)
        {
            Saituls.FolderAutomation.FaEngine eng = FaEngine();
            if (eng == null) return;
            List<Saituls.FolderAutomation.FaProfile> made;
            if (which == 1)
            {
                string src = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                made = Saituls.FolderAutomation.FaTemplates.DownloadsByType(Path.Combine(src, "Downloads"));
            }
            else
            {
                string src = Environment.GetFolderPath(Environment.SpecialFolder.MyPictures);
                made = Saituls.FolderAutomation.FaTemplates.Clipboard(
                    Path.Combine(src, "Screenshots"), Path.Combine(src, "Clipboard\\Images"));
            }
            foreach (var p in made) eng.Config.Profiles.Add(p);
            eng.SaveConfig();
            eng.ReloadProfiles();
            _faSelected = made[0].Id;
            _faNote = "Template added, disabled. Run a dry run, then switch it on.";
            Refresh();
        }

        // Two switches and a status line. Polling cadence, dwell, cooldown and
        // strategy are deliberately NOT here -- they live in
        // Scripts\taskbar_edge\taskbar_edge.ini for the rare person who needs
        // them.
        void DrawTaskbarEdgeSection(Graphics g, int x, int y, int w)
        {
            DrawBevel(g, new Rectangle(x, y - 8, w, 2), false);
            DrawText(g, AccountsLoc.T("TaskbarHdr"), x, y, Palette.TEXT, 12, true);

            ToolFacts f = Facts();
            bool installed = f.TaskbarInstalled;
            bool enabled = S.TaskbarEdge;
            bool running = f.TaskbarRunning;

            var er = new Rectangle(x, y + 20, 300, 24);
            string revealLabel = (enabled ? "[X] " : "[ ] ") + AccountsLoc.T("TaskbarReveal");
            Buttons.Add(new ButtonDef { R = er, A = () => ToggleTaskbarEdge(), Label = revealLabel, Sel = enabled });
            DrawButton(g, er, revealLabel, enabled, 10);

            string state;
            Color stateColor;
            if (!installed) { state = AccountsLoc.T("TaskbarMissing"); stateColor = Palette.DANGERTXT; }
            else if (running) { state = AccountsLoc.T("TaskbarRunning"); stateColor = Palette.LINK; }
            else if (enabled) { state = AccountsLoc.T("TaskbarEnabledStopped"); stateColor = Palette.DANGERTXT; }
            else { state = AccountsLoc.T("TaskbarStopped"); stateColor = Palette.TEXT2; }
            DrawText(g, Fit(g, state, w - 310, 10), x + 310, y + 26, stateColor, 10);

            bool autoOn = f.AutostartTaskbar;
            var ar = new Rectangle(x, y + 50, 300, 24);
            string autoLabel = (autoOn ? "[X] " : "[ ] ") + AccountsLoc.T("TaskbarAutostart");
            Buttons.Add(new ButtonDef { R = ar, A = () => ToggleTaskbarEdgeAutostart(), Label = autoLabel, Sel = autoOn });
            DrawButton(g, ar, autoLabel, autoOn, 10);
            DrawText(g, Fit(g, AccountsLoc.T(enabled ? "TaskbarRestartHint" : "TaskbarEnableFirst"), w - 310, 10),
                x + 310, y + 56, Palette.MUTED, 10);

            DrawText(g, Fit(g, AccountsLoc.T("TaskbarHint1"), w, 10), x, y + 82, Palette.MUTED, 10);
            DrawText(g, Fit(g, AccountsLoc.T("TaskbarHint2"), w, 10), x, y + 96, Palette.MUTED, 10);
            DrawText(g, Fit(g, AccountsLoc.T("TaskbarHint3"), w, 10), x, y + 110, Palette.MUTED, 10);
        }

        void ToggleTaskbarEdge()
        {
            if (!TaskbarEdgeHelper.Installed(S.RootPath))
            {
                SaitulsDialog.Show("TaskbarEdge is missing:\n" + TaskbarEdgeHelper.ExePath(S.RootPath)
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
                    SaitulsDialog.Show("Could not start TaskbarEdge:\n" + TaskbarEdgeHelper.ExePath(S.RootPath), "SAITULS");
            }
            InvalidateFacts();
            Refresh();
        }

        void ToggleTaskbarEdgeAutostart()
        {
            if (!TaskbarEdgeHelper.Installed(S.RootPath))
            {
                SaitulsDialog.Show("TaskbarEdge is missing:\n" + TaskbarEdgeHelper.ExePath(S.RootPath), "SAITULS");
                return;
            }
            if (!S.TaskbarEdge && !AutoStartEnabled(TaskbarEdgeHelper.RunKeyName))
            {
                SaitulsDialog.Show("Turn on \"Reliable taskbar edge reveal\" first.", "SAITULS");
                return;
            }
            ToggleAutostart(TaskbarEdgeHelper.RunKeyName, "TaskbarEdge", TaskbarEdgeHelper.ExePath(S.RootPath));
        }

        void StartSetup()
        {
            string setup = Path.Combine(S.RootPath, "setup.ps1");
            if (!File.Exists(setup)) { SaitulsDialog.Show("Setup script not found:\n" + setup, "SAITULS"); return; }
            // Install / repair can change every payload, so the cached readiness
            // is discarded and measured again: never a stale READY.
            ResetMediaProbe();
            InvalidateFacts();
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
            catch (Exception ex) { SaitulsDialog.Show("Could not start setup:\n" + ex.Message, "SAITULS"); }
        }

        void OpenPath(string path)
        {
            if (!File.Exists(path) && !Directory.Exists(path)) { SaitulsDialog.Show("Not found:\n" + path, "SAITULS"); return; }
            try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
            catch (Exception ex) { SaitulsDialog.Show("Could not open:\n" + ex.Message, "SAITULS"); }
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
            if (selected.Count == 0) { SaitulsDialog.Show("Select at least one feature.", "SAITULS"); return; }
            string safe = ImportSafePath();
            if (!File.Exists(safe)) { SaitulsDialog.Show("IMPORT_SAFE.PS1 not found:\n" + safe, "SAITULS"); return; }
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
                SaitulsDialog.Show("Missing registry file(s) - nothing was imported:\n\n" +
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
            if (selected.Count == 0) { SaitulsDialog.Show("Select at least one feature.", "SAITULS"); return; }
            string safe = ImportSafePath();
            if (!File.Exists(safe)) { SaitulsDialog.Show("IMPORT_SAFE.PS1 not found:\n" + safe, "SAITULS"); return; }
            List<string> missing = new List<string>();
            foreach (string f in selected)
                if (!File.Exists(Path.Combine(S.RootPath, "Registry", f + "_REM.REG"))) missing.Add(f + "_REM.REG");
            if (missing.Count > 0)
            {
                SaitulsDialog.Show("Missing removal file(s) - nothing was removed:\n\n" +
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
                SaitulsDialog.Show("Elevation was cancelled. Nothing was changed.", "SAITULS");
            }
            catch (Exception ex)
            {
                try { if (tmp != null && File.Exists(tmp)) File.Delete(tmp); } catch { }
                SaitulsDialog.Show("Could not start the elevated import:\n" + ex.Message, "SAITULS");
            }
        }

        void ReportElevatedResult(int exitCode, int requested)
        {
            if (exitCode == 0)
            {
                SaitulsDialog.Show(requested + " feature(s) installed/removed successfully.",
                    "SAITULS", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            else
            {
                SaitulsDialog.Show(
                    exitCode + " of " + requested + " operation(s) FAILED.\n" +
                    "The console window listed which; the failing features were NOT applied.",
                    "SAITULS", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        void LaunchTool(string script, string mode)
        {
            string root = S.RootPath;
            string binDir = Path.Combine(root, "Bin");

            if (mode == "saiopt")
            {
                string saiopt = Path.Combine(root, "Scripts", "saiopt", "SAIOPT.exe");
                if (!File.Exists(saiopt))
                {
                    SaitulsDialog.Show("SAIOPT is missing:\n" + saiopt + "\n\nBuild it with Scripts\\saiopt\\build.ps1.", "SAITULS");
                    return;
                }
                try
                {
                    Process.Start(new ProcessStartInfo(saiopt)
                    {
                        Arguments = "--panel",
                        WorkingDirectory = Path.GetDirectoryName(saiopt),
                        UseShellExecute = false
                    });
                }
                catch (Exception ex)
                {
                    SaitulsDialog.Show("Could not start SAIOPT:\n" + ex.Message, "SAITULS");
                }
                return;
            }

            if (mode == "saispin-settings")
            {
                string settingsScript = Path.Combine(root, "Scripts", "saispin_settings.ps1");
                if (!File.Exists(settingsScript))
                {
                    SaitulsDialog.Show("SAISPIN Settings is missing:\n" + settingsScript, "SAITULS");
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
                    SaitulsDialog.Show("Could not start SAISPIN Settings:\n" + ex.Message, "SAITULS");
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
                    SaitulsDialog.Show("SAIPATCH is missing:\n" + saipatchScript, "SAITULS");
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
                    SaitulsDialog.Show("Could not start SAIPATCH:\n" + ex.Message, "SAITULS");
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
                    SaitulsDialog.Show(subsystemName + " is missing:\n" + subsystemScript, "SAITULS");
                    return;
                }
                try
                {
                    // Never elevated from here. Secure Apps raises its own UAC
                    // prompt for the privileged storage helper only; the console
                    // launcher raises none at all.
                    Process.Start(new ProcessStartInfo("powershell.exe")
                    {
                        // -FromExplorer arms the launcher's error boundary for
                        // the consoles subsystem: this window closes with its
                        // child, so a preflight failure has to arrive as a
                        // dialog and a log line, never as a flash.
                        Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + subsystemScript + "\""
                            + (mode == "consoles" ? " -FromExplorer" : ""),
                        WorkingDirectory = Path.GetDirectoryName(subsystemScript),
                        UseShellExecute = false
                    });
                }
                catch (Exception ex)
                {
                    SaitulsDialog.Show("Could not start " + subsystemName + ":\n" + ex.Message, "SAITULS");
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
                    SaitulsDialog.Show("Scenarios launcher is missing:\n" + scenariosScript, "SAITULS");
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
                    SaitulsDialog.Show("Could not start Scenarios:\n" + ex.Message, "SAITULS");
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
                SaitulsDialog.Show("Tool not found:\n" + fullPath, "SAITULS");
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
                    fb.Description = AccountsLoc.T("ChooseTargetFolder", script);
                    fb.SelectedPath = root;
                    if (fb.ShowDialog() != DialogResult.OK) return;
                    target = fb.SelectedPath;
                }
            }
            else if (mode == "file")
            {
                using (var of = new OpenFileDialog())
                {
                    of.Title = AccountsLoc.T("ChooseTargetFile", script);
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
                    if (exe == null) { SaitulsDialog.Show("Python is missing. Open Home and run Install / repair first.", "SAITULS"); return; }
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
                SaitulsDialog.Show("Could not launch " + script + ":\n" + ex.Message, "SAITULS");
            }
        }

        // ======================================================================
        // MEDIA: readiness, download options, tracked jobs
        // ======================================================================

        // The ONE readiness definition for the media tools. Existence is not
        // readiness: each tool is run with its own --version probe and the same
        // size floors Bin\media_payload.ps1 uses.
        static MediaProbeResult ProbeMedia(string root, Func<string, string, long, int, string> probe)
        {
            var result = new MediaProbeResult();
            string bin = Path.Combine(root, "Bin");
            var problems = new List<string>();
            var downloadBlockers = new List<string>();
            var mergeBlockers = new List<string>();

            result.Ffmpeg = probe(Path.Combine(bin, "FFMPEG.EXE"), "-version", 30L * 1024 * 1024, 10000) ?? "";
            if (result.Ffmpeg.Length > 0)
            {
                problems.Add("FFmpeg " + result.Ffmpeg);
                downloadBlockers.Add("FFmpeg " + result.Ffmpeg);
                mergeBlockers.Add("FFmpeg " + result.Ffmpeg);
            }
            result.Ffprobe = probe(Path.Combine(bin, "FFPROBE.EXE"), "-version", 10L * 1024 * 1024, 10000) ?? "";
            if (result.Ffprobe.Length > 0)
            {
                problems.Add("FFprobe " + result.Ffprobe);
                mergeBlockers.Add("FFprobe " + result.Ffprobe);
            }
            result.YtDlp = probe(Path.Combine(bin, "yt-dlp.exe"), "--version", 5L * 1024 * 1024, 15000) ?? "";
            if (result.YtDlp.Length > 0)
            {
                problems.Add("yt-dlp " + result.YtDlp);
                downloadBlockers.Add("yt-dlp " + result.YtDlp);
            }

            // A JavaScript runtime is required by the production download path.
            // Deno ships bundled; Node on PATH is accepted. Reported, but it does
            // not block a download for non-YouTube sources.
            string deno = Path.Combine(bin, "deno.exe");
            if (File.Exists(deno)) result.JsRuntime = probe(deno, "-V", 10L * 1024 * 1024, 10000) ?? "";
            else
            {
                string node = Toolchain.FindOnPath("node.exe");
                result.JsRuntime = node != null ? (probe(node, "-v", 0, 10000) ?? "") : "missing";
            }
            if (result.JsRuntime.Length > 0) problems.Add("JavaScript runtime " + result.JsRuntime);

            result.Ready = problems.Count == 0;
            result.DownloadBlocker = string.Join("\n", downloadBlockers.ToArray());
            result.MergeBlocker = string.Join("\n", mergeBlockers.ToArray());
            return result;
        }

        void EnsureMediaProbe()
        {
            if (_mediaProbeStarted || _closing) return;
            _mediaProbeStarted = true;
            string root = S.RootPath;
            ThreadPool.QueueUserWorkItem(delegate
            {
                MediaProbeResult result = ProbeMedia(root, ProbeMediaTracked);
                TryBeginInvoke(delegate
                {
                    _lastProbe = result;
                    _mediaProbeDone = true;
                    Refresh();
                });
            });
        }

        string ProbeMediaTracked(string exe, string args, long minBytes, int timeoutMs)
        {
            if (_closing) return "closing";
            if (string.IsNullOrEmpty(exe) || !File.Exists(exe)) return "missing";
            try
            {
                long bytes = new FileInfo(exe).Length;
                if (minBytes > 0 && bytes < minBytes) return "too small (" + bytes + " bytes)";
                CliRun result = RunTracked(exe, args, timeoutMs, null);
                if (result.TimedOut) return "did not answer " + args + " within " + (timeoutMs / 1000) + "s";
                if (result.ExitCode == 0) return null;
                return result.ExitCode >= 0 ? args + " exited " + result.ExitCode : "not runnable";
            }
            catch { return "not runnable"; }
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
            EnsureMediaProbe();
            return null;
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
                SaitulsDialog.Show(blocker, "SAITULS - download", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string problem;
            List<string> urls = ClipboardInput.ExtractUrls(out problem);
            MediaProbeResult probe = CurrentMediaProbe();
            using (var dialog = new DownloadDialog(mode, S.RootPath, urls.Count, problem,
                                                   probe != null && probe.Ready ? "" : probe == null ? AccountsLoc.T("MediaChecking") : probe.Line))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                // The clipboard is inspected again at launch time: it may have
                // changed while the dialog was open, and yt-dlp must never be
                // started with an empty or prose input.
                string lateProblem;
                List<string> current = ClipboardInput.ExtractUrls(out lateProblem);
                if (current.Count == 0)
                {
                    SaitulsDialog.Show("No downloadable URL found in the clipboard.\n\n" + lateProblem,
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
                SaitulsDialog.Show("A download is already running:\n" + DownloadJob.Line +
                    "\n\nWait for it to finish before starting another one.",
                    "SAITULS - download", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string worker = Path.Combine(S.RootPath, "Scripts", "DL_YT.PS1");
            if (!File.Exists(worker))
            {
                SaitulsDialog.Show("The download worker is missing:\n" + worker, "SAITULS - download",
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
                SaitulsDialog.Show("Could not start the download:\n" + ex.Message,
                    "SAITULS - download", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            Refresh();
        }

        string DownloadBlocker()
        {
            string yt = Path.Combine(S.RootPath, "Bin", "yt-dlp.exe");
            if (!File.Exists(yt))
                return AccountsLoc.T("MediaRepair");
            MediaProbeResult probe = CurrentMediaProbe();
            if (probe == null) return AccountsLoc.T("MediaChecking");
            if (probe.DownloadBlocker.Length > 0)
                return AccountsLoc.T("MediaRepair");
            return null;
        }

        string MergeBlocker()
        {
            string bin = Path.Combine(S.RootPath, "Bin");
            string ff = Path.Combine(bin, "FFMPEG.EXE");
            string fp = Path.Combine(bin, "FFPROBE.EXE");
            if (!File.Exists(ff))
                return AccountsLoc.T("MediaRepair");
            if (!File.Exists(fp))
                return AccountsLoc.T("MediaRepair");
            MediaProbeResult probe = CurrentMediaProbe();
            if (probe == null) return AccountsLoc.T("MediaChecking");
            if (probe.MergeBlocker.Length > 0)
                return AccountsLoc.T("MediaRepair");
            return null;
        }

        void PickAndMergeAudio()
        {
            string blocker = MergeBlocker();
            if (blocker != null)
            {
                SaitulsDialog.Show(blocker, "SAITULS - Merge Audio", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string file;
            using (var of = new OpenFileDialog())
            {
                of.Title = AccountsLoc.T("MergeChooseVideo");
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
                SaitulsDialog.Show("A merge is already running:\n" + MergeJob.Line,
                    "SAITULS - Merge Audio", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            string worker = Path.Combine(S.RootPath, "Scripts", "MERGE_AUD.PS1");
            if (!File.Exists(worker))
            {
                SaitulsDialog.Show("The Merge Audio worker is missing:\n" + worker,
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
                SaitulsDialog.Show("Could not start Merge Audio:\n" + ex.Message,
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
                SaitulsDialog.Show("The item tool engine is missing:\n" + engine, "SAITULS", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            string python = Toolchain.FindOnPath("python.exe") ?? Toolchain.FindOnPath("pythonw.exe");
            if (python == null)
            {
                SaitulsDialog.Show("Python is missing. Open Home and run Install / repair first.", "SAITULS");
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
                SaitulsDialog.Show("Could not start the item tool:\n" + ex.Message, "SAITULS");
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
            JobOutputStream stream = new JobOutputStream(job.LogPath);
            DataReceivedEventHandler onOut = delegate(object o, DataReceivedEventArgs e) { stream.Write("", e.Data); };
            DataReceivedEventHandler onErr = delegate(object o, DataReceivedEventArgs e) { stream.Write("stderr: ", e.Data); };
            try
            {
                p.OutputDataReceived += onOut;
                p.ErrorDataReceived += onErr;
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
            }
            catch { }
            ThreadPool.QueueUserWorkItem(delegate
            {
                try { p.WaitForExit(); } catch { }
                // The second wait is the documented way to let both async pumps
                // drain: the first returns at process exit, the second when the
                // streams reach end-of-stream. Without it the last lines of a
                // failed run can be missing from the log.
                try { p.WaitForExit(); } catch { }
                string tail = stream.TailText();
                string marker = stream.Marker;
                string last = stream.Last;
                stream.Dispose();
                int code = -1;
                // ExitCode has to be read before the Process is disposed.
                try { code = p.ExitCode; } catch { }
                job.Exit = code;
                job.Marker = marker;
                job.Tail = tail;
                job.Finished = true;
                job.Ok = code == 0;
                if (onComplete != null) { try { onComplete(); } catch { } }
                job.Line = job.Label + ": " + (job.Ok ? "COMPLETED" : "FAILED (exit " + code + ")") +
                           " -> " + job.OutDir;
                // Detach both pumps before disposing, so a late callback cannot
                // write into a channel that is already closed.
                try { p.OutputDataReceived -= onOut; } catch { }
                try { p.ErrorDataReceived -= onErr; } catch { }
                try { p.Dispose(); } catch { }
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
            // The worker's own marker first (it names the reason), then the tail
            // it printed last. The whole log is never read back: what a dialog
            // needs is a reason, and the file itself stays there to be opened.
            string reason = job.Marker.Length > 0 ? job.Marker : job.Tail;
            SaitulsDialog.Show(title + " FAILED (exit " + job.Exit + ").\n\n" +
                (reason.Length > 0 ? reason : "See the log for the full output.") +
                "\n\nDestination: " + job.OutDir + "\nLog: " + job.LogPath,
                "SAITULS - " + title, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }

        void ReportJobSuccess(ToolJob job, string title)
        {
            string message = "";
            foreach (string raw in (job.Tail ?? "").Split('\n'))
            { string line = raw.Trim(); if (line.Length > 0) message = line; }
            SaitulsDialog.Show(title + " finished.\n\n" + (message.Length > 0 ? message : "Done.") +
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
            if ((DateTime.Now - _clipStatusRead).TotalSeconds < 2.0) return;
            _clipStatusRead = DateTime.Now;
            _clipRunning = ClipboardPlusHelper.Installed(S.RootPath) && ClipboardPlusHelper.Running();
            _clipSavePath = ClipboardPlusHelper.Setting("save_path", "");
            if (_clipSavePath.Length == 0) _clipSavePath = ClipboardPlusHelper.Status("save_path", "");
            if (_clipSavePath.Length == 0) _clipSavePath = @"V:\___VAC\_PIC\_Clipboard_stuff";
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
            if (_clipDepsStarted || _closing) return;
            _clipDepsStarted = true;
            string root = S.RootPath;
            ThreadPool.QueueUserWorkItem(delegate
            {
                string problem;
                ProcessStartInfo command = ClipboardPlusHelper.DependencyCommand(root, out problem);
                if (command != null)
                {
                    CliRun result = RunTracked(command.FileName, command.Arguments, 20000,
                        psi => psi.WorkingDirectory = command.WorkingDirectory);
                    problem = result.TimedOut ? "the readiness probe did not answer within 20s"
                        : ClipboardPlusHelper.DependencyResult(result.ExitCode, result.Stdout + result.Stderr);
                }
                TryBeginInvoke(delegate
                {
                    _clipDeps = problem;
                    _clipDepsDone = true;
                    if (_clipStartRequested)
                    {
                        _clipStartRequested = false;
                        CompleteClipboardStart(problem);
                    }
                    Refresh();
                });
            });
        }

        void ClipboardStartAction()
        {
            if (_closing || _clipStartRequested) return;
            if (!ClipboardPlusHelper.Installed(S.RootPath))
            {
                _clipMsg = AccountsLoc.T("ClipMissing");
                Refresh();
                return;
            }
            if (ClipboardPlusHelper.Running())
            {
                _clipMsg = AccountsLoc.T("ClipAlreadyRunning");
                RefreshClipboardStatus();
                Refresh();
                return;
            }
            // Recheck an old result, or join the existing background check.
            // Start never waits for imports on the UI thread.
            if (_clipDepsDone) { _clipDepsStarted = false; _clipDepsDone = false; }
            _clipStartRequested = true;
            _clipMsg = AccountsLoc.T("ClipChecking");
            EnsureClipboardDepsProbe();
            Refresh();
        }

        void CompleteClipboardStart(string deps)
        {
            if (_closing) return;
            if (deps != null)
            {
                _clipMsg = AccountsLoc.T("ClipRepair") + " " + deps;
                Refresh();
                return;
            }
            if (ClipboardPlusHelper.Running())
            {
                // Idempotent start: the healthy resident and its RAM-only Safe
                // Copy history are left exactly as they are.
                _clipMsg = AccountsLoc.T("ClipAlreadyRunning");
                RefreshClipboardStatus();
                Refresh();
                return;
            }
            if (!ClipboardPlusHelper.Start(S.RootPath))
            {
                _clipMsg = AccountsLoc.T("ClipStartFailed");
                Refresh();
                return;
            }
            _clipMsg = AccountsLoc.T("ClipStartRequested");
            ArmClipboardWait(true);
        }

        void ClipboardStopAction()
        {
            bool cancelledStart = _clipStartRequested;
            _clipStartRequested = false;
            if (!ClipboardPlusHelper.Running())
            {
                _clipMsg = AccountsLoc.T(cancelledStart ? "ClipStartCancelled" : "ClipAlreadyStopped");
                RefreshClipboardStatus();
                Refresh();
                return;
            }
            if (!ClipboardPlusHelper.Stop())
            {
                _clipMsg = AccountsLoc.T("ClipStopFailed");
                Refresh();
                return;
            }
            _clipMsg = AccountsLoc.T("ClipStopRequested");
            ArmClipboardWait(false);
        }

        void ClipboardSafeCopyAction()
        {
            ClipboardOneShotAction("--sanitize-text-once", "SafeCopy");
        }

        void ClipboardRestoreAction()
        {
            ClipboardOneShotAction("--restore-last-safe-copy", "Restore");
        }

        void ClipboardOneShotAction(string argument, string titleKey)
        {
            if (_closing || _clipCommandBusy) return;
            string problem;
            ProcessStartInfo command = ClipboardPlusHelper.OneShotCommand(S.RootPath, argument, out problem);
            if (command == null) { _clipMsg = problem; Refresh(); return; }
            _clipCommandBusy = true;
            _clipMsg = AccountsLoc.T(titleKey) + ": " + AccountsLoc.T("Working");
            Refresh();
            ThreadPool.QueueUserWorkItem(delegate
            {
                CliRun result = RunTracked(command.FileName, command.Arguments, 20000,
                    psi => psi.WorkingDirectory = command.WorkingDirectory);
                TryBeginInvoke(delegate
                {
                    _clipCommandBusy = false;
                    string text = (result.Stdout + " " + result.Stderr).Trim();
                    if (result.TimedOut) text = AccountsLoc.T("ClipCommandTimedOut");
                    else if (text.Length == 0) text = AccountsLoc.T("ClipCommandExit", result.ExitCode.ToString());
                    _clipMsg = AccountsLoc.T(titleKey) + ": " + text;
                    _clipStatusRead = DateTime.MinValue;
                    RefreshClipboardStatus();
                    Refresh();
                });
            });
        }

        void ClipboardSettingsAction()
        {
            if (!ClipboardPlusHelper.Installed(S.RootPath))
            {
                _clipMsg = AccountsLoc.T("ClipMissing");
                Refresh();
                return;
            }
            if (!ClipboardPlusHelper.LaunchSettings(S.RootPath))
            {
                _clipMsg = AccountsLoc.T("ClipSettingsFailed");
            }
            else _clipMsg = AccountsLoc.T("ClipSettingsOpened");
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
                        ? AccountsLoc.T(running ? "ClipStarted" : "ClipStartTimedOut")
                        : AccountsLoc.T(running ? "ClipStopTimedOut" : "ClipStoppedMessage");
                }
                Refresh();
            };
            _clipWait.Start();
        }

        internal void SwitchTab(string name)
        {
            CurrentTab = name;
            FocusedButton = -1;
            S.LastTab = name;
            S.Save();
            Refresh();
            if (OnScreen) BackgroundTick();
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
                            SaitulsDialog.Show("Another SAITULS registry mutation is in progress. Try again in a moment.", "SAITULS");
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
                InvalidateFacts();
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
                // Account row selection + drag initiation (T-264). Handled before
                // TryActivate so rapid row clicks or drag gestures are never eaten
                // by ActivateDebounceMs, and scrolled lists map to the canonical
                // account_id at (visIdx + _acctListScrollY).
                if (CurrentTab == "Accounts" && _accounts != null && _accounts.Count > 0 && _acctListRect.Contains(e.Location))
                {
                    int relY = e.Y - (_acctListRect.Y + 3);
                    if (relY >= 0)
                    {
                        int idx = (relY / 34) + _acctListScrollY;
                        if (idx >= 0 && idx < _accounts.Count)
                        {
                            string clickedId = _accounts[idx].Id;
                            _selectedAccount = clickedId;
                            _acctDragAccountId = clickedId;
                            _acctDragStartPt = e.Location;
                            _acctDragActive = false;
                            _acctDragInsertIndex = -1;
                            Refresh();
                            return;
                        }
                    }
                }
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

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (_acctDragAccountId != null)
            {
                if ((e.Button & MouseButtons.Left) == 0 || CurrentTab != "Accounts" || _accounts == null)
                {
                    _acctDragAccountId = null;
                    _acctDragActive = false;
                    _acctDragInsertIndex = -1;
                    Refresh();
                    return;
                }
                int dy = Math.Abs(e.Y - _acctDragStartPt.Y);
                int dx = Math.Abs(e.X - _acctDragStartPt.X);
                if (!_acctDragActive && (dy >= AccountDragThresholdPx || dx >= AccountDragThresholdPx))
                    _acctDragActive = true;
                if (_acctDragActive)
                {
                    int newIns = ComputeAccountDragInsertIndex(e.Location);
                    if (newIns != _acctDragInsertIndex)
                    {
                        _acctDragInsertIndex = newIns;
                        Refresh();
                    }
                }
            }
        }

        int ComputeAccountDragInsertIndex(Point pt)
        {
            if (_accounts == null || _accounts.Count == 0 || _acctListRect.Width <= 0) return -1;
            Rectangle bounds = _acctListRect;
            bounds.Inflate(16, 12);
            if (!bounds.Contains(pt)) return -1;
            int rowH = 34;
            int relY = pt.Y - (_acctListRect.Y + 3);
            int slot = (relY + (rowH / 2)) / rowH + _acctListScrollY;
            if (slot < 0) slot = 0;
            if (slot > _accounts.Count) slot = _accounts.Count;
            return slot;
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            if (e.Button == MouseButtons.Left && _acctDragAccountId != null)
            {
                string draggedId = _acctDragAccountId;
                bool wasActive = _acctDragActive;
                int ins = wasActive ? ComputeAccountDragInsertIndex(e.Location) : -1;
                _acctDragAccountId = null;
                _acctDragActive = false;
                _acctDragInsertIndex = -1;
                Refresh();

                if (wasActive && ins >= 0 && _accounts != null && !_accountsBusy)
                {
                    int fromIdx = _accounts.FindIndex(r => r.Id == draggedId);
                    if (fromIdx >= 0 && ins != fromIdx && ins != fromIdx + 1)
                    {
                        if (ins < _accounts.Count)
                        {
                            string targetId = _accounts[ins].Id;
                            if (targetId != draggedId)
                                ExecuteAccountMove(draggedId, "move \"" + draggedId + "\" before \"" + targetId + "\"");
                        }
                        else if (_accounts.Count > 0)
                        {
                            string lastId = _accounts[_accounts.Count - 1].Id;
                            if (lastId != draggedId)
                                ExecuteAccountMove(draggedId, "move \"" + draggedId + "\" after \"" + lastId + "\"");
                        }
                    }
                }
            }
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            if (e.KeyCode == Keys.Escape) { Hide(); e.Handled = true; return; }
            if (CurrentTab != "Accounts" && (e.KeyCode == Keys.PageDown || e.KeyCode == Keys.PageUp))
            {
                ScrollTab(CurrentTab, e.KeyCode == Keys.PageDown ? ClientSize.Height / 2 : -ClientSize.Height / 2, ClientSize.Height - 70);
                Invalidate(); e.Handled = true; return;
            }
            // Ctrl+1..Ctrl+6, bounded by the tab list itself so adding a tab cannot leave
// the keyboard route behind.
            if (e.Control && e.KeyCode >= Keys.D1 && e.KeyCode <= Keys.D1 + TabNames.Length - 1)
            {
                int index = (int)e.KeyCode - (int)Keys.D1;
                if (index < TabNames.Length) SwitchTab(TabNames[index]);
                e.Handled = true; return;
            }
            if (e.KeyCode == Keys.Tab && Buttons.Count > 0)
            {
                int delta = e.Shift ? -1 : 1;
                // Skip targets that are scrolled out of view.
                for (int guard = 0; guard < Buttons.Count; guard++)
                {
                    FocusedButton = (FocusedButton + delta + Buttons.Count) % Buttons.Count;
                    if (!Buttons[FocusedButton].R.IsEmpty) break;
                }
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
            if (CurrentTab == "Accounts" && _accounts != null && _accounts.Count > 0)
            {
                if (e.Alt && e.KeyCode == Keys.Up)
                {
                    AccountsMoveUp();
                    e.Handled = true;
                    e.SuppressKeyPress = true;
                    return;
                }
                if (e.Alt && e.KeyCode == Keys.Down)
                {
                    AccountsMoveDown();
                    e.Handled = true;
                    e.SuppressKeyPress = true;
                    return;
                }
                if (e.KeyCode == Keys.Up)
                {
                    int idx = _accounts.FindIndex(x => x.Id == _selectedAccount);
                    if (idx < 0) idx = 0;
                    else idx = Math.Max(0, idx - 1);
                    _selectedAccount = _accounts[idx].Id;
                    Refresh();
                    e.Handled = true;
                    return;
                }
                if (e.KeyCode == Keys.Down)
                {
                    int idx = _accounts.FindIndex(x => x.Id == _selectedAccount);
                    if (idx < 0) idx = 0;
                    else idx = Math.Min(_accounts.Count - 1, idx + 1);
                    _selectedAccount = _accounts[idx].Id;
                    Refresh();
                    e.Handled = true;
                    return;
                }
                if (e.KeyCode == Keys.Enter && (FocusedButton < 0 || FocusedButton >= Buttons.Count))
                {
                    if (_enterSpaceHeld) { e.Handled = true; e.SuppressKeyPress = true; return; }
                    _enterSpaceHeld = true;
                    AccountRow sel = SelectedAccount();
                    Action cta = PrimaryCTAAction(sel);
                    if (cta != null) TryActivate(cta);
                    e.Handled = true;
                    e.SuppressKeyPress = true;
                    return;
                }
            }
            base.OnKeyDown(e);
        }

        protected override void OnMouseWheel(MouseEventArgs e)
        {
            if (CurrentTab == "Home" || CurrentTab == "Settings" || CurrentTab == "Tools" || CurrentTab == "Organizer" || CurrentTab == "Explorer menus")
            {
                ScrollTab(CurrentTab, e.Delta > 0 ? -40 : 40, ClientSize.Height - 70);
                Invalidate();
                return;
            }
            if (CurrentTab == "Accounts" && _accounts != null)
            {
                int listW = Math.Max(190, Math.Min(260, (int)((ClientSize.Width - 32) * 0.38)));
                if (e.X < listW + 28)
                {
                    int delta = e.Delta > 0 ? -1 : 1;
                    _acctListScrollY = Math.Max(0, _acctListScrollY + delta);
                    Refresh();
                }
                else
                {
                    int delta = e.Delta > 0 ? -24 : 24;
                    _acctDetailScrollY = Math.Max(0, _acctDetailScrollY + delta);
                    Refresh();
                }
                return;
            }
            base.OnMouseWheel(e);
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
// ---- Folder Automation lifetime -----------------------------------
        // The engine is deliberately ordinary user-context work owned by the
        // SAITULS process. It needs no administrator rights, no Windows service
        // and no scheduled task to organize ordinary user folders.
        internal static Saituls.FolderAutomation.FaEngine Fa;
        static bool _faStopped;

        internal static void StartFolderAutomation(NotifyIcon tray)
        {
            try
            {
                var log = new Saituls.FolderAutomation.FaLog(Saituls.FolderAutomation.FaPaths.LogPath());
                var journal = new Saituls.FolderAutomation.FaJournal(Saituls.FolderAutomation.FaPaths.JournalDir());
                Fa = new Saituls.FolderAutomation.FaEngine(new Saituls.FolderAutomation.SystemFileSystem(), log, journal);
                Fa.LoadConfig();
                Fa.Notice += delegate(Saituls.FolderAutomation.FaNotice n)
                {
                    // Silent by default: only a pause, a repeated failure or a
                    // broken configuration is worth a balloon. A moved file is not.
                    if (tray == null || n.Level == Saituls.FolderAutomation.FaNoticeLevel.Info) return;
                    try
                    {
                        tray.BalloonTipTitle = n.Title;
                        tray.BalloonTipText = n.Message.Length > 250 ? n.Message.Substring(0, 250) : n.Message;
                        tray.BalloonTipIcon = n.Level == Saituls.FolderAutomation.FaNoticeLevel.Error
                            ? ToolTipIcon.Error : ToolTipIcon.Warning;
                        tray.ShowBalloonTip(8000);
                    }
                    catch { }
                };
                SystemEvents.PowerModeChanged += delegate(object s, PowerModeChangedEventArgs e)
                {
                    if (e.Mode != PowerModes.Resume) return;
                    try { if (Fa != null) Fa.NotifyResume(); } catch { }
                };
                Fa.Start();
                Fa.NotifyStartup();
            }
            catch (Exception ex)
            {
                Fa = null;
                try
                {
                    Saituls.FolderAutomation.FaPaths.EnsureDir(Saituls.FolderAutomation.FaPaths.DataDir());
                    File.AppendAllText(Saituls.FolderAutomation.FaPaths.LogPath(),
                        DateTime.Now + " [ERR] engine failed to start: " + ex.Message + Environment.NewLine);
                }
                catch { }
            }
        }

        // W2-007: a stop that cannot prove quiescence must not look like a clean
        // shutdown. The message lands in the Folder Automation log, where the
        // operators who care about this look, and the tray exit is refused (the
        // menu handler) so a long operation is not closed over.
        internal static bool StopFolderAutomation()
        {
            if (_faStopped) return true;
            bool down;
            try { down = Fa != null ? Fa.Stop() : true; }
            catch { down = false; }
            if (down)
            {
                _faStopped = true;
                try { Fa = null; } catch { }
            }
            return down;
        }

        internal static void LogFaStopIncomplete()
        {
            try
            {
                Saituls.FolderAutomation.FaPaths.EnsureDir(Saituls.FolderAutomation.FaPaths.DataDir());
                File.AppendAllText(Saituls.FolderAutomation.FaPaths.LogPath(),
                    DateTime.Now + " [WARN] exit refused: a Folder Automation operation was still admitted; the engine was left running to finish it" + Environment.NewLine);
            }
            catch { }
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool AttachConsole(int processId);

        // ---- SAITULS.exe --fa <command> -----------------------------------
        // The engine has to be drivable without the GUI: these four commands are
        // what the tests and a curious user both need.
        internal static int FolderAutomationCli(string[] args, TextWriter output)
        {
            List<string> rest = new List<string>();
            for (int i = 0; i < args.Length; i++)
                if (string.Equals(args[i], "--fa", StringComparison.OrdinalIgnoreCase))
                {
                    for (int j = i + 1; j < args.Length; j++) rest.Add(args[j]);
                    break;
                }
            string cmd = rest.Count > 0 ? rest[0].ToLowerInvariant() : "status";
            string arg = rest.Count > 1 ? rest[1] : null;

            Saituls.FolderAutomation.FaEngine eng = null;
            try
            {
                var log = new Saituls.FolderAutomation.FaLog(Saituls.FolderAutomation.FaPaths.LogPath());
                var journal = new Saituls.FolderAutomation.FaJournal(Saituls.FolderAutomation.FaPaths.JournalDir());
                eng = new Saituls.FolderAutomation.FaEngine(new Saituls.FolderAutomation.SystemFileSystem(), log, journal);
                eng.LoadConfig();

                switch (cmd)
                {
                    case "validate":
                    {
                        var v = Saituls.FolderAutomation.FaConfigValidator.Validate(eng.Config);
                        output.WriteLine("config: " + Saituls.FolderAutomation.FaPaths.ConfigPath());
                        foreach (string w in v.Warnings) output.WriteLine("warning: " + w);
                        foreach (string e in v.Errors) output.WriteLine("error: " + e);
                        output.WriteLine(v.Ok ? "OK" : "FAILED (" + v.Errors.Count + " error(s))");
                        return v.Ok ? 0 : 2;
                    }
                    case "status":
                    {
                        eng.Prepare();
                        output.WriteLine("engine enabled: " + eng.Config.EngineEnabled +
                            (string.IsNullOrEmpty(eng.ConfigError) ? "" : "  [" + eng.ConfigError + "]"));
                        output.WriteLine("profiles: " + eng.Config.Profiles.Count);
                        foreach (var p in eng.Config.Profiles)
                        {
                            // PERF-004: the candidate counts are part of status,
                            // not just of the diagnostic log: "deferred" is the
                            // difference between a source that is fully admitted
                            // and one that is still waiting for capacity.
                            var rt = eng.Runtime(p.Id);
                            output.WriteLine("  " + (p.Enabled ? "[on] " : "[off] ") + p.Name +
                                "  " + p.Sources.Count + " source(s), " + p.Rules.Count + " rule(s)" +
                                (rt == null ? "" : "  queued " + rt.Queue.Count + ", deferred " + rt.Deferred.Count +
                                    (rt.ReconcileCursor == null ? "" : ", reconcile continuing")) +
                                (string.IsNullOrEmpty(p.Status) ? "" : "  " + p.Status));
                        }
                        return 0;
                    }
                    case "run":
                    {
                        bool dry = cmd == "dryrun";
                        // Runtimes (and their statuses) only exist once the
                        // engine is prepared; without this the profile lookup
                        // answers null and the run is a no-op.
                        // Recovery is a mutation-authority decision (it marks
                        // interrupted operations ambiguous), so on a dry run it
                        // is skipped: a preview never decides history.
                        if (!dry) eng.RecoverJournalForCli();
                        eng.Prepare();
                        List<Saituls.FolderAutomation.FaRunReport> reports;
                        var one = arg == null ? null : eng.FindProfile(arg);
                        if (one != null)
                        {
                            var report = eng.RunProfile(one.Id, dry, "cli");
                            reports = report == null
                                ? new List<Saituls.FolderAutomation.FaRunReport>()
                                : new List<Saituls.FolderAutomation.FaRunReport> { report };
                        }
                        else
                        {
                            if (arg != null) { output.WriteLine("no such profile: " + arg); return 2; }
                            reports = eng.RunEveryEnabled(dry, "cli");
                        }
                        if (reports.Count == 0) { output.WriteLine("nothing enabled to run"); return 0; }
                        foreach (var r in reports)
                        {
                            output.WriteLine(r.Summary());
                            foreach (var it in r.Items) output.WriteLine("  " + it.Line());
                        }
                        // A run that was blocked by another process holding the
                        // mutation authority is NOT a successful no-op: report
                        // it as blocked so a caller (and the operator) can see
                        // that nothing happened and why.
                        bool anyBlocked = false;
                        foreach (var r in reports)
                            if (string.Equals(r.Status, "Blocked", StringComparison.OrdinalIgnoreCase)) anyBlocked = true;
                        if (anyBlocked && reports.TrueForAll(x => string.Equals(x.Status, "Blocked", StringComparison.OrdinalIgnoreCase)))
                        {
                            output.WriteLine("blocked: another process holds the Folder Automation mutation authority; nothing was changed");
                            return 4;
                        }
                        return reports.Exists(x => x.Errors > 0) ? 1 : 0;
                    }
                    case "undo":
                    {
                        // W2-006: undo enters the same cross-process mutation
                        // authority as a run, through the engine, so a tray run
                        // in flight cannot be restored into underneath itself.
                        var r = eng.Undo(arg);
                        output.WriteLine(r.Message);
                        return r.Ok ? 0 : 1;
                    }
                    default:
                        output.WriteLine("usage: SAITULS.exe --fa validate|status|dryrun|run|undo [profile]");
                        return 2;
                }
            }
            catch (Exception ex)
            {
                output.WriteLine("error: " + ex.Message);
                return 3;
            }
            finally { if (eng != null) eng.Stop(); }
        }

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
            try
            {
                Icon exeIcon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
                if (exeIcon != null)
                {
                    if (exeIcon.Width == size && exeIcon.Height == size) return exeIcon;
                    return new Icon(exeIcon, size, size);
                }
            }
            catch { }
            using (Icon fallback = SystemIcons.Application) return (Icon)fallback.Clone();
        }

        [STAThread]
        static int Main(string[] args)
        {
            // --fa is a pure command surface: it runs before the single-instance
            // mutex so a dry run never has to fight the tray instance.
            if (Array.Exists(args, a => string.Equals(a, "--fa", StringComparison.OrdinalIgnoreCase)))
                return FolderAutomationCliFromShell(args);

            // Publish the activation channel before ownership. A secondary can
            // now always signal it, even while the primary is still building UI.
            using (var showEvent = new System.Threading.EventWaitHandle(false, System.Threading.EventResetMode.AutoReset, "Local\\SaitulsShow"))
            {
                bool createdNew;
                using (var mutex = new System.Threading.Mutex(true, "Local\\SaitulsApp", out createdNew))
                {
                    if (!createdNew)
                    {
                        showEvent.Set();
                        return 0;
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
                // One language for every surface and every child from here on.
                AccountsLoc.Apply(s.Language);

                NotifyIcon tray = new NotifyIcon();
                // Ask for the SMALL icon size: the default new Icon(path) hands
                // back the 32x32 frame, which the shell then downscales into the
                // 16x16 tray slot - exactly the blur UI.md forbids.
                tray.Icon = LoadIcon(s.IcoPath, SystemInformation.SmallIconSize.Width);
                tray.Text = "SAITULS";
                ContextMenuStrip menu = SaitulsMenu.Themed();
                menu.Items.Add(AccountsLoc.T("TrayOpen"), null, (o, e) => ShowForm(s, tray));
                menu.Items.Add(new ToolStripSeparator());
                menu.Items.Add(AccountsLoc.T("FolderAutomation"), null, (o, e) =>
                {
                    ShowForm(s, tray);
                    _form.SwitchTab("Organizer");
                });
                menu.Items.Add(AccountsLoc.T("ExitApp"), null, (o, e) =>
                {
                                        tray.Visible = false;
                    // W2-007: the exit only claims the engine is down when it
                    // really is. A stop that could not observe quiescence keeps
                    // the engine object alive so its in-flight operation can
                    // finish, and says so instead of closing over it.
                    if (!StopFolderAutomation())
                    {
                        LogFaStopIncomplete();
                        return;
                    }
                    Application.Exit();
                });
                menu.Opening += delegate
                {
                    menu.Items[0].Text = AccountsLoc.T("TrayOpen");
                    menu.Items[2].Text = AccountsLoc.T("FolderAutomation");
                    menu.Items[3].Text = AccountsLoc.T("ExitApp");
                };
                tray.ContextMenuStrip = menu;
                tray.Visible = true;
                // Folder Automation starts with the tray, opens no window of its own
                // and stays silent. --minimized only decides whether the GUI shows.
                StartFolderAutomation(tray);
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
                    // W2-007: the tray is going away regardless, but the engine
                    // is only dropped once it has no admitted work left. A stop
                    // that could not observe quiescence leaves the engine
                    // instance running so its last operation can complete; the
                    // process itself stays up only for that work, which is the
                    // honest reading of "the engine is not stopped".
                    bool faDown = StopFolderAutomation();
                    if (!faDown) LogFaStopIncomplete();
                    tray.Visible = false;
                    tray.Dispose();
                }
            }
            return 0;
        }

        static int FolderAutomationCliFromShell(string[] args)
        {
            StringWriter buffer = new StringWriter();
            int rc = FolderAutomationCli(args, buffer);
            string text = buffer.ToString();
            // A windowed executable owns no console; borrow the parent's so
            // --fa prints where the user typed it instead of into the void.
            if (AttachConsole(-1))
            {
                try
                {
                    StreamWriter stdout = new StreamWriter(Console.OpenStandardOutput());
                    stdout.AutoFlush = true;
                    Console.SetOut(stdout);
                }
                catch { }
            }
            bool printed = false;
            try { Console.Out.Write(text); printed = true; } catch { }
            // Only say "I put it in a file" when stdout really took nothing:
            // a shell that redirects a windowed app's output gives us a usable
            // channel even though AttachConsole reports failure.
            if (!printed)
            {
                string path = Path.Combine(Path.GetTempPath(), "saituls-fa.txt");
                File.WriteAllText(path, text);
                Console.Error.WriteLine("(no console attached; output written to " + path + ")");
            }
            return rc;
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
