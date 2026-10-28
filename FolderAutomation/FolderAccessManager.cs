using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace Saituls.FolderAutomation
{
    public enum FolderAccessStatus
    {
        UNVERIFIED,
        GRANTED,
        INHERITED_OR_PREEXISTING,
        DENIED,
        EXPIRED,
        REVOKED,
        INCONSISTENT,
        CONTEXT_OFFLINE
    }

    public enum FolderAccessMode
    {
        ManagedWorkdirModify,
        ReadOnlyLease15m,
        ReadOnlyLease60m,
        ReadOnlySession,
        StagedHandoff,
        LiveFileRelay
    }

    public class FolderAccessEntry
    {
        public string AccountId { get; set; }
        public string AccountLabel { get; set; }
        public string AccountBackend { get; set; }
        public string AccountSid { get; set; }
        public string FolderPath { get; set; }
        public string AccessMode { get; set; }
        public FolderAccessStatus Status { get; set; }
        public bool IsTrustedRoot { get; set; }
        public string Owner { get; set; }
        public string Expires { get; set; }
        public string LastVerification { get; set; }
        public string Diagnostics { get; set; }
    }

    public class TrustedModeProbeResult
    {
        public bool IsSuccess;
        public string Preference;
        public string Status;
        public string ErrorDetail;
        public int ActiveSessions;
        public List<string> ApprovedRoots = new List<string>();
        public Dictionary<string, object> RawRecord;
        public bool IsDiagnosticError;
    }

    public static class FolderAccessManagerEngine
    {
        static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        public static bool IsReparsePoint(string path)
        {
            try
            {
                if (!Directory.Exists(path) && !File.Exists(path)) return false;
                var attr = File.GetAttributes(path);
                return (attr & FileAttributes.ReparsePoint) != 0;
            }
            catch { return false; }
        }

        public static bool IsDiskRoot(string path)
        {
            try
            {
                string full = Path.GetFullPath(path);
                string root = Path.GetPathRoot(full);
                return string.Equals(full.TrimEnd('\\', '/'), root.TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase);
            }
            catch { return false; }
        }

        public static bool IsProfileRoot(string path)
        {
            try
            {
                string full = Path.GetFullPath(path).TrimEnd('\\', '/');
                string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile).TrimEnd('\\', '/');
                return string.Equals(full, userProfile, StringComparison.OrdinalIgnoreCase);
            }
            catch { return false; }
        }

        public static bool RunProcessBounded(string exe, string args, int timeoutMs, out int exitCode, out string stdout, out string stderr, out bool timedOut)
        {
            exitCode = -1;
            stdout = "";
            stderr = "";
            timedOut = false;

            if (string.IsNullOrEmpty(exe) || !File.Exists(exe))
            {
                stderr = "Executable not found: " + (exe ?? "<null>");
                return false;
            }

            try
            {
                var psi = new ProcessStartInfo(exe, args)
                {
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true
                };

                using (var p = new Process { StartInfo = psi })
                {
                    var outSb = new StringBuilder();
                    var errSb = new StringBuilder();
                    using (var outWait = new ManualResetEvent(false))
                    using (var errWait = new ManualResetEvent(false))
                    {
                        p.OutputDataReceived += (s, e) =>
                        {
                            if (e.Data == null) outWait.Set();
                            else outSb.AppendLine(e.Data);
                        };
                        p.ErrorDataReceived += (s, e) =>
                        {
                            if (e.Data == null) errWait.Set();
                            else errSb.AppendLine(e.Data);
                        };

                        if (!p.Start())
                        {
                            stderr = "Failed to start process.";
                            return false;
                        }

                        p.BeginOutputReadLine();
                        p.BeginErrorReadLine();

                        if (p.WaitForExit(timeoutMs))
                        {
                            outWait.WaitOne(1000);
                            errWait.WaitOne(1000);
                            exitCode = p.ExitCode;
                            stdout = outSb.ToString();
                            stderr = errSb.ToString();
                            return true;
                        }
                        else
                        {
                            timedOut = true;
                            try
                            {
                                if (!p.HasExited) p.Kill();
                            }
                            catch { }
                            stderr = "Process timed out after " + timeoutMs + "ms and was terminated.";
                            return false;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                stderr = "Exception starting process: " + ex.Message;
                return false;
            }
        }

        public static TrustedModeProbeResult ProbeTrustedMode(string label, string accountId, string exe)
        {
            var res = new TrustedModeProbeResult
            {
                IsSuccess = false,
                Preference = "OFF",
                Status = "BACKEND_UNAVAILABLE",
                IsDiagnosticError = true,
                ErrorDetail = "No accounts backend executable configured."
            };

            if (string.IsNullOrEmpty(exe) || !File.Exists(exe))
            {
                res.Status = "BACKEND_UNAVAILABLE";
                res.ErrorDetail = "Backend executable not found at: " + (exe ?? "<null>");
                return res;
            }

            if (string.IsNullOrEmpty(label))
            {
                res.Status = "ACCOUNT_NOT_FOUND";
                res.ErrorDetail = "No account label specified.";
                return res;
            }

            int exitCode;
            string stdout, stderr;
            bool timedOut;
            bool started = RunProcessBounded(exe, "trusted-mode-status \"" + label + "\"", 5000, out exitCode, out stdout, out stderr, out timedOut);

            if (timedOut)
            {
                res.Status = "TIMEOUT";
                res.ErrorDetail = "Backend probe timed out after 5000ms.";
                return res;
            }

            if (!started)
            {
                res.Status = "BACKEND_UNAVAILABLE";
                res.ErrorDetail = "Failed to launch backend probe: " + stderr;
                return res;
            }

            string jsonLine = "";
            if (!string.IsNullOrEmpty(stdout))
            {
                string[] lines = stdout.Split('\n');
                for (int i = lines.Length - 1; i >= 0; i--)
                {
                    string t = lines[i].Trim();
                    if (t.Length > 0 && t.StartsWith("{") && t.EndsWith("}"))
                    {
                        jsonLine = t;
                        break;
                    }
                }
            }

            Dictionary<string, object> dict = null;
            if (!string.IsNullOrEmpty(jsonLine))
            {
                try
                {
                    var js = new JavaScriptSerializer();
                    dict = js.DeserializeObject(jsonLine) as Dictionary<string, object>;
                }
                catch (Exception ex)
                {
                    res.Status = "INVALID_RESPONSE";
                    res.ErrorDetail = "Failed to parse JSON response: " + ex.Message;
                    return res;
                }
            }
            else if (!string.IsNullOrEmpty(stdout) && stdout.Trim().StartsWith("{"))
            {
                try
                {
                    var js = new JavaScriptSerializer();
                    dict = js.DeserializeObject(stdout.Trim()) as Dictionary<string, object>;
                }
                catch (Exception ex)
                {
                    res.Status = "INVALID_RESPONSE";
                    res.ErrorDetail = "Failed to parse JSON response: " + ex.Message;
                    return res;
                }
            }

            if (dict == null)
            {
                res.Status = exitCode != 0 ? "ERROR" : "INVALID_RESPONSE";
                res.ErrorDetail = !string.IsNullOrEmpty(stderr) ? stderr.Trim() : (!string.IsNullOrEmpty(stdout) ? stdout.Trim() : ("Empty response from backend (exit code " + exitCode + ")"));
                return res;
            }

            string schema = dict.ContainsKey("schema") ? dict["schema"] as string : "";

            if (exitCode != 0 || string.Equals(schema, "sai.accounts/error/1", StringComparison.OrdinalIgnoreCase))
            {
                string errStatus = dict.ContainsKey("status") ? dict["status"] as string : "";
                string errDetail = dict.ContainsKey("detail") ? dict["detail"] as string : "";

                if (string.Equals(errStatus, "unknown_action", StringComparison.OrdinalIgnoreCase) ||
                    (!string.IsNullOrEmpty(errDetail) && errDetail.IndexOf("trusted-mode-status", StringComparison.OrdinalIgnoreCase) >= 0))
                {
                    res.Status = "BACKEND_OUTDATED";
                    res.ErrorDetail = "Installed backend does not support Trusted Mode (unknown_action). An update to the managed installation is required.";
                }
                else if (string.Equals(errStatus, "ACCOUNT_NOT_FOUND", StringComparison.OrdinalIgnoreCase))
                {
                    res.Status = "ACCOUNT_NOT_FOUND";
                    res.ErrorDetail = "Account " + label + " not found in backend registry.";
                }
                else if (string.Equals(errStatus, "ACCESS_DENIED", StringComparison.OrdinalIgnoreCase))
                {
                    res.Status = "ACCESS_DENIED";
                    res.ErrorDetail = !string.IsNullOrEmpty(errDetail) ? errDetail : "Access denied by backend.";
                }
                else
                {
                    res.Status = !string.IsNullOrEmpty(errStatus) ? errStatus : "ERROR";
                    res.ErrorDetail = !string.IsNullOrEmpty(errDetail) ? errDetail : ("Backend error: exit code " + exitCode);
                }
                return res;
            }

            if (!string.Equals(schema, "sai.accounts/trusted-mode-status/1", StringComparison.OrdinalIgnoreCase))
            {
                res.Status = "INVALID_RESPONSE";
                res.ErrorDetail = "Unexpected response schema: " + schema;
                return res;
            }

            string respAcc = dict.ContainsKey("account_id") ? dict["account_id"] as string : null;
            string respSel = dict.ContainsKey("selector") ? dict["selector"] as string : null;
            if (!string.IsNullOrEmpty(label) &&
                !string.Equals(respSel, label, StringComparison.OrdinalIgnoreCase) &&
                !string.Equals(respAcc, accountId, StringComparison.OrdinalIgnoreCase))
            {
                res.Status = "INVALID_RESPONSE";
                res.ErrorDetail = "Account identity mismatch: requested " + label + " (" + accountId + ") but received selector=" + (respSel ?? "<none>");
                return res;
            }

            if (!dict.ContainsKey("status") || !dict.ContainsKey("preference"))
            {
                res.Status = "INVALID_RESPONSE";
                res.ErrorDetail = "Missing required status or preference field in response.";
                return res;
            }

            string statusStr = dict["status"] as string ?? "ERROR";
            string prefStr = dict["preference"] as string ?? "OFF";

            res.IsSuccess = true;
            res.IsDiagnosticError = false;
            res.Preference = prefStr;
            res.Status = statusStr;
            res.RawRecord = dict;
            if (dict.ContainsKey("last_error"))
                res.ErrorDetail = dict["last_error"] as string ?? "";

            if (dict.ContainsKey("active_sessions"))
                int.TryParse(dict["active_sessions"].ToString(), out res.ActiveSessions);

            if (dict.ContainsKey("approved_roots"))
            {
                var rootsArr = dict["approved_roots"] as object[];
                if (rootsArr != null)
                {
                    foreach (object r in rootsArr)
                    {
                        if (r is string && !string.IsNullOrEmpty((string)r))
                            res.ApprovedRoots.Add((string)r);
                    }
                }
            }

            return res;
        }

        public static FolderAccessStatus EvaluateFolderStatus(
            string accountLabel,
            string targetSid,
            string backend,
            string folderPath,
            string accountsExe,
            string pythonExe,
            string scriptDir,
            out string owner,
            out string expires,
            out string diagnostics)
        {
            owner = "Unknown";
            expires = "None";
            diagnostics = "";

            if (string.IsNullOrEmpty(folderPath) || !Directory.Exists(folderPath))
            {
                diagnostics = "Folder does not exist or cannot be accessed.";
                return FolderAccessStatus.UNVERIFIED;
            }

            if (IsReparsePoint(folderPath))
            {
                diagnostics = "REPARSE_POINT_REFUSED: Symlink or junction detected.";
                return FolderAccessStatus.DENIED;
            }

            if (backend != "windows_user" && backend != "managed_windows_user")
            {
                owner = "Profile Directory";
                expires = "Persistent";
                diagnostics = "Backend is not a managed Windows identity.";
                return FolderAccessStatus.INHERITED_OR_PREEXISTING;
            }

            if (string.IsNullOrEmpty(targetSid))
            {
                diagnostics = "Managed account Windows SID is missing.";
                return FolderAccessStatus.CONTEXT_OFFLINE;
            }

            // 1. Probe through real account token if accountsExe is available
            bool tokenCanAccess = false;
            if (!string.IsNullOrEmpty(accountsExe) && File.Exists(accountsExe))
            {
                try
                {
                    int probeExit;
                    string probeOut, probeErr;
                    bool probeTimeout;
                    if (RunProcessBounded(accountsExe, "probe-workdir \"" + accountLabel + "\" --workdir \"" + folderPath + "\"", 4000, out probeExit, out probeOut, out probeErr, out probeTimeout))
                    {
                        if (probeOut.Contains("\"status\":\"READY\"") || probeOut.Contains("\"status\":\"ALREADY_OWNED\""))
                        {
                            tokenCanAccess = true;
                        }
                        else if (probeOut.Contains("\"status\":\"CONTEXT_IDENTITY_MISSING\"") || probeOut.Contains("\"status\":\"REGISTRY_UNAVAILABLE\""))
                        {
                            diagnostics = "Context is offline or registry unavailable.";
                            return FolderAccessStatus.CONTEXT_OFFLINE;
                        }
                    }
                    else if (probeTimeout)
                    {
                        diagnostics = "Workdir probe timed out.";
                    }
                }
                catch (Exception ex)
                {
                    diagnostics = "Probe execution error: " + ex.Message;
                }
            }

            // 2. Check live NTFS ACL rules on the folder
            bool hasHelperAce = false;
            bool hasDenyAce = false;
            bool hasPreexistingAce = false;
            try
            {
                var dirInfo = new DirectoryInfo(folderPath);
                DirectorySecurity sec = dirInfo.GetAccessControl();
                var sid = new SecurityIdentifier(targetSid);

                foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                {
                    if (rule.IdentityReference == sid)
                    {
                        if (rule.AccessControlType == AccessControlType.Deny)
                        {
                            hasDenyAce = true;
                        }
                        else if (rule.AccessControlType == AccessControlType.Allow)
                        {
                            if (rule.FileSystemRights.HasFlag(FileSystemRights.Modify) && !rule.IsInherited)
                            {
                                hasHelperAce = true;
                            }
                            else
                            {
                                hasPreexistingAce = true;
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                diagnostics = "ACL read failed: " + ex.Message;
                return FolderAccessStatus.DENIED;
            }

            if (hasDenyAce)
            {
                diagnostics = "DENY ACE takes precedence over any grant.";
                return FolderAccessStatus.DENIED;
            }

            // 3. Check temporary lease state via sai_access.py
            bool leaseActive = false;
            bool leaseExpired = false;
            if (!string.IsNullOrEmpty(pythonExe) && !string.IsNullOrEmpty(scriptDir))
            {
                string accessPy = Path.Combine(scriptDir, "sai_access.py");
                if (File.Exists(accessPy))
                {
                    try
                    {
                        int pyExit;
                        string pyOut, pyErr;
                        bool pyTimeout;
                        if (RunProcessBounded(pythonExe, "\"" + accessPy + "\" status --account \"" + accountLabel + "\" --source \"" + folderPath + "\"", 3000, out pyExit, out pyOut, out pyErr, out pyTimeout))
                        {
                            if (pyOut.Contains("\"status\": \"ok\""))
                            {
                                owner = "sai_access.py";
                                if (pyOut.Contains("\"expired\": true"))
                                {
                                    leaseExpired = true;
                                    expires = "EXPIRED";
                                }
                                else
                                {
                                    leaseActive = true;
                                    expires = "Active Lease";
                                }
                            }
                            else if (pyOut.Contains("\"status\": \"revoked\""))
                            {
                                diagnostics = "Lease was explicitly revoked.";
                                return FolderAccessStatus.REVOKED;
                            }
                        }
                    }
                    catch { }
                }
            }

            if (leaseExpired)
            {
                diagnostics = "Temporary read-only lease has expired.";
                return FolderAccessStatus.EXPIRED;
            }

            if (hasHelperAce)
            {
                owner = "sai-accounts-helper";
                expires = "Persistent (Workdir)";
                diagnostics = "Managed Modify ACE verified on folder.";
                return FolderAccessStatus.GRANTED;
            }

            if (leaseActive)
            {
                diagnostics = "Temporary read-only lease active and verified.";
                return FolderAccessStatus.GRANTED;
            }

            if (hasPreexistingAce || tokenCanAccess)
            {
                owner = "System / Inherited";
                expires = "Persistent";
                diagnostics = "Account has inherited or pre-existing permission.";
                return FolderAccessStatus.INHERITED_OR_PREEXISTING;
            }

            diagnostics = "Account token has no access rights to this folder.";
            return FolderAccessStatus.DENIED;
        }
    }

    public class FolderAccessManagerDialog : Form
    {
        readonly List<FolderAccessEntry> _entries = new List<FolderAccessEntry>();
        readonly Dictionary<string, string> _accountLabels = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        readonly Dictionary<string, string> _accountBackends = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        readonly Dictionary<string, string> _accountSids = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        readonly ComboBox _cmbAccount;
        readonly ListView _lvGrants;
        readonly Label _lblNote;
        readonly Label _lblTrustedBadge;
        readonly Label _lblTrustedDetail;
        readonly Label _lblAccountDetail;
        readonly Label _lblRootHint;
        readonly Button _btnToggleTrusted;
        readonly Button _btnMarkTrustedRoot;
        readonly Button _btnStopSession;
        readonly Button _btnAddFolder;
        readonly Button _btnRemoveFolder;
        readonly Button _btnGrant;
        readonly Button _btnRevoke;
        readonly Button _btnVerify;
        readonly Button _btnOpenExplorer;
        readonly Button _btnRefresh;
        readonly Button _btnClose;

        Dictionary<string, object> _currentTrustedRecord = null;
        string _lastProbedStatus = "OFF";
        string _lastProbedPreference = "OFF";
        int _refreshGen = 0;
        bool _isBusy = false;

        readonly string _accountsExe;
        readonly string _pythonExe;
        readonly string _scriptsDir;
        readonly Action<string, List<string>> _saveAccountFolders;
        readonly Func<string, List<string>> _getAccountFolders;

        internal static void StyleButton(Button b)
        {
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderColor = Palette.BEVEL;
            b.FlatAppearance.MouseOverBackColor = Palette.ALT;
            b.FlatAppearance.MouseDownBackColor = Palette.SURFACE;
            b.BackColor = Palette.RAISED;
            b.ForeColor = Palette.TEXT;
            b.UseVisualStyleBackColor = false;
            b.Font = new Font("Segoe UI", 9f);
        }

        public FolderAccessManagerDialog(
            Dictionary<string, string> accountLabels,
            Dictionary<string, string> accountBackends,
            Dictionary<string, string> accountSids,
            string initialAccountId,
            string accountsExe,
            string pythonExe,
            string scriptsDir,
            Func<string, List<string>> getAccountFolders,
            Action<string, List<string>> saveAccountFolders)
        {
            _accountLabels = accountLabels ?? new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            _accountBackends = accountBackends ?? new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            _accountSids = accountSids ?? new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            _accountsExe = accountsExe;
            _pythonExe = pythonExe;
            _scriptsDir = scriptsDir;
            _getAccountFolders = getAccountFolders;
            _saveAccountFolders = saveAccountFolders;

            Text = "Folder Access Manager — SAITULS Accounts";
            Size = new Size(960, 560);
            MinimumSize = new Size(820, 460);
            StartPosition = FormStartPosition.CenterParent;
            BackColor = Palette.BG;
            ForeColor = Palette.TEXT;
            Font = new Font("Segoe UI", 9f);

            // Responsive top panel using TableLayoutPanel (4 rows)
            var topPanel = new TableLayoutPanel
            {
                Dock = DockStyle.Top,
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                ColumnCount = 1,
                RowCount = 4,
                BackColor = Palette.BG_SOFT,
                Padding = new Padding(12, 8, 12, 8)
            };
            topPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            topPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            topPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            topPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            topPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));

            // ROW 1: Managed Account selector
            var rowAccount = new FlowLayoutPanel
            {
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = true,
                AutoSize = true,
                Dock = DockStyle.Fill,
                Margin = new Padding(0, 0, 0, 4),
                BackColor = Palette.BG_SOFT
            };
            var lblAcc = new Label
            {
                Text = "Managed Account:",
                AutoSize = true,
                Font = new Font("Segoe UI", 9f, FontStyle.Bold),
                ForeColor = Palette.BHL,
                Margin = new Padding(0, 4, 8, 4)
            };
            _cmbAccount = new FaUi.FaComboBox
            {
                Width = 320,
                DropDownStyle = ComboBoxStyle.DropDownList,
                BackColor = Palette.COMPARE,
                ForeColor = Palette.TEXT,
                Margin = new Padding(0, 2, 12, 2)
            };
            _cmbAccount.SelectedIndexChanged += (s, e) => OnAccountChanged();

            _lblAccountDetail = new Label
            {
                Text = "",
                AutoSize = true,
                ForeColor = Palette.TEXT2,
                Margin = new Padding(0, 4, 0, 4)
            };
            rowAccount.Controls.Add(lblAcc);
            rowAccount.Controls.Add(_cmbAccount);
            rowAccount.Controls.Add(_lblAccountDetail);
            topPanel.Controls.Add(rowAccount, 0, 0);

            // ROW 2: Trusted Mode status, ON/OFF control, Stop Session
            var rowTrusted = new FlowLayoutPanel
            {
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = true,
                AutoSize = true,
                Dock = DockStyle.Fill,
                Margin = new Padding(0, 0, 0, 4),
                BackColor = Palette.BG_SOFT
            };
            _lblTrustedBadge = new Label
            {
                Text = "Trusted Mode: OFF",
                AutoSize = true,
                Font = new Font("Segoe UI", 9.5f, FontStyle.Bold),
                ForeColor = Palette.MUTED,
                Margin = new Padding(0, 4, 10, 4)
            };
            _lblTrustedDetail = new Label
            {
                Text = "",
                AutoSize = true,
                ForeColor = Palette.TEXT2,
                Margin = new Padding(0, 5, 12, 4)
            };
            _btnToggleTrusted = new Button
            {
                Text = "Turn ON",
                AutoSize = true,
                Height = 26,
                Margin = new Padding(0, 1, 8, 1)
            };
            StyleButton(_btnToggleTrusted);
            _btnToggleTrusted.Click += (s, e) => ToggleTrustedMode();

            _btnStopSession = new Button
            {
                Text = "Stop Session",
                AutoSize = true,
                Height = 26,
                Enabled = false,
                Margin = new Padding(0, 1, 8, 1)
            };
            StyleButton(_btnStopSession);
            _btnStopSession.Click += (s, e) => StopCurrentSession();

            rowTrusted.Controls.Add(_lblTrustedBadge);
            rowTrusted.Controls.Add(_lblTrustedDetail);
            rowTrusted.Controls.Add(_btnToggleTrusted);
            rowTrusted.Controls.Add(_btnStopSession);
            topPanel.Controls.Add(rowTrusted, 0, 1);

            // ROW 3: Trusted Mode explanation
            var lblDesc = new Label
            {
                Text = "Trusted Antigravity Mode: Relaxed folder isolation for AUDAPACK launches. Operator approval required per root.",
                AutoSize = true,
                Dock = DockStyle.Fill,
                ForeColor = Palette.TEXT2,
                Margin = new Padding(0, 2, 0, 4)
            };
            topPanel.Controls.Add(lblDesc, 0, 2);

            // ROW 4: Toggle Trusted Root action & explanation
            var rowRoot = new FlowLayoutPanel
            {
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = true,
                AutoSize = true,
                Dock = DockStyle.Fill,
                Margin = new Padding(0, 0, 0, 2),
                BackColor = Palette.BG_SOFT
            };
            _btnMarkTrustedRoot = new Button
            {
                Text = "Toggle Trusted Root for Selected",
                AutoSize = true,
                Height = 26,
                Enabled = false,
                Margin = new Padding(0, 1, 10, 1)
            };
            StyleButton(_btnMarkTrustedRoot);
            _btnMarkTrustedRoot.Click += (s, e) => ToggleMarkTrustedRoot();

            _lblRootHint = new Label
            {
                Text = "(Grants AUDAPACK permission to bind this folder as an authorized root when Trusted Mode is enabled)",
                AutoSize = true,
                ForeColor = Palette.MUTED,
                Margin = new Padding(0, 5, 0, 4)
            };
            rowRoot.Controls.Add(_btnMarkTrustedRoot);
            rowRoot.Controls.Add(_lblRootHint);
            topPanel.Controls.Add(rowRoot, 0, 3);

            // CENTER: Folder access table (ListView)
            _lvGrants = new ListView
            {
                Dock = DockStyle.Fill,
                View = View.Details,
                FullRowSelect = true,
                GridLines = false,
                MultiSelect = false,
                BackColor = Palette.COMPARE,
                ForeColor = Palette.TEXT,
                Font = new Font("Segoe UI", 9f),
                BorderStyle = BorderStyle.FixedSingle,
                HeaderStyle = ColumnHeaderStyle.Nonclickable
            };
            FaUi.Style(_lvGrants);
            _lvGrants.Columns.Add("Account", 80);
            _lvGrants.Columns.Add("Folder", 230);
            _lvGrants.Columns.Add("Access Mode", 110);
            _lvGrants.Columns.Add("Status", 130);
            _lvGrants.Columns.Add("Trusted Root", 90);
            _lvGrants.Columns.Add("Owner", 100);
            _lvGrants.Columns.Add("Expires", 100);
            _lvGrants.Columns.Add("Last Verification", 110);

            _lvGrants.DrawSubItem += delegate(object sender, DrawListViewSubItemEventArgs e)
            {
                bool selected = e.Item.Selected;
                Color textColor = e.Item.ForeColor;
                if (e.ColumnIndex == 4)
                {
                    textColor = string.Equals(e.SubItem.Text, "YES", StringComparison.OrdinalIgnoreCase) ? Palette.BHL : Palette.MUTED;
                }
                using (var brush = new SolidBrush(selected ? Palette.SELECTION : Palette.COMPARE))
                {
                    e.Graphics.FillRectangle(brush, e.Bounds);
                }
                if (selected)
                {
                    using (var pen = new Pen(Palette.BEVEL))
                    {
                        e.Graphics.DrawRectangle(pen, e.Bounds.X, e.Bounds.Y, e.Bounds.Width - 1, e.Bounds.Height - 1);
                    }
                }
                TextRenderer.DrawText(e.Graphics, e.SubItem.Text ?? "", e.Item.Font ?? Font, e.Bounds, textColor,
                    TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.WordEllipsis | TextFormatFlags.NoPrefix);
                if (e.Item.Focused && _lvGrants.Focused && e.ColumnIndex == 0)
                {
                    ControlPaint.DrawFocusRectangle(e.Graphics, e.Bounds);
                }
            };

            _lvGrants.SelectedIndexChanged += (s, e) => UpdateButtonStates();

            // BOTTOM: Selected folder details + Context-sensitive actions
            var bottomPanel = new TableLayoutPanel
            {
                Dock = DockStyle.Bottom,
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                ColumnCount = 1,
                RowCount = 2,
                BackColor = Palette.BG_SOFT,
                Padding = new Padding(12, 6, 12, 8)
            };
            bottomPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
            bottomPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            bottomPanel.RowStyles.Add(new RowStyle(SizeType.AutoSize));

            _lblNote = new Label
            {
                Dock = DockStyle.Fill,
                AutoSize = true,
                Text = "Select a folder row to verify or manage access.",
                ForeColor = Palette.TEXT2,
                Margin = new Padding(0, 0, 0, 6)
            };

            var flowButtons = new FlowLayoutPanel
            {
                Dock = DockStyle.Fill,
                AutoSize = true,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = true,
                BackColor = Palette.BG_SOFT,
                Margin = new Padding(0)
            };

            _btnAddFolder = new Button { Text = "+ Add Folder...", AutoSize = true, Height = 28, Margin = new Padding(0, 2, 6, 2) };
            StyleButton(_btnAddFolder);
            _btnAddFolder.Click += (s, e) => AddFolderPrompt();

            _btnRemoveFolder = new Button { Text = "Remove from List", AutoSize = true, Height = 28, Enabled = false, Margin = new Padding(0, 2, 6, 2) };
            StyleButton(_btnRemoveFolder);
            _btnRemoveFolder.Click += (s, e) => RemoveSelectedFolder();

            _btnGrant = new Button { Text = "Grant Access...", AutoSize = true, Height = 28, Enabled = false, Margin = new Padding(0, 2, 6, 2) };
            StyleButton(_btnGrant);
            _btnGrant.Click += (s, e) => GrantAccessFlow();

            _btnRevoke = new Button { Text = "Revoke Grant", AutoSize = true, Height = 28, Enabled = false, Margin = new Padding(0, 2, 6, 2) };
            StyleButton(_btnRevoke);
            _btnRevoke.Click += (s, e) => RevokeSelectedGrant();

            _btnVerify = new Button { Text = "Verify Access", AutoSize = true, Height = 28, Enabled = false, Margin = new Padding(0, 2, 6, 2) };
            StyleButton(_btnVerify);
            _btnVerify.Click += (s, e) => VerifySelectedAccess();

            _btnOpenExplorer = new Button { Text = "Open in Explorer", AutoSize = true, Height = 28, Enabled = false, Margin = new Padding(0, 2, 6, 2) };
            StyleButton(_btnOpenExplorer);
            _btnOpenExplorer.Click += (s, e) => OpenSelectedInExplorer();

            _btnRefresh = new Button { Text = "Refresh All", AutoSize = true, Height = 28, Margin = new Padding(0, 2, 6, 2) };
            StyleButton(_btnRefresh);
            _btnRefresh.Click += (s, e) => RefreshAccountFolders();

            _btnClose = new Button { Text = "Close", AutoSize = true, Height = 28, DialogResult = DialogResult.OK, Margin = new Padding(0, 2, 0, 2) };
            StyleButton(_btnClose);

            flowButtons.Controls.AddRange(new Control[] {
                _btnAddFolder, _btnRemoveFolder, _btnGrant, _btnRevoke, _btnVerify, _btnOpenExplorer, _btnRefresh, _btnClose
            });

            bottomPanel.Controls.Add(_lblNote, 0, 0);
            bottomPanel.Controls.Add(flowButtons, 0, 1);

            Controls.Add(_lvGrants);
            Controls.Add(bottomPanel);
            Controls.Add(topPanel);

            Resize += (s, e) => UpdateColumnWidths();
            Load += (s, e) => UpdateColumnWidths();

            // Populate account combo
            int selectIdx = 0;
            int idx = 0;
            foreach (var kvp in _accountLabels)
            {
                string id = kvp.Key;
                string label = kvp.Value;
                string backend = _accountBackends.ContainsKey(id) ? _accountBackends[id] : "unknown";
                _cmbAccount.Items.Add(new AccountComboItem { Id = id, Label = label, Backend = backend });
                if (string.Equals(id, initialAccountId, StringComparison.OrdinalIgnoreCase))
                    selectIdx = idx;
                idx++;
            }
            if (_cmbAccount.Items.Count > 0)
                _cmbAccount.SelectedIndex = selectIdx;
        }

        void UpdateColumnWidths()
        {
            if (_lvGrants == null || _lvGrants.Columns.Count < 8) return;
            int clientW = _lvGrants.ClientSize.Width;
            if (clientW <= 0) return;

            if (clientW < 850)
            {
                _lvGrants.Columns[0].Width = 70;  // Account
                _lvGrants.Columns[2].Width = 95;  // Access Mode
                _lvGrants.Columns[3].Width = 110; // Status
                _lvGrants.Columns[4].Width = 75;  // Trusted Root
                _lvGrants.Columns[5].Width = 80;  // Owner
                _lvGrants.Columns[6].Width = 80;  // Expires
                _lvGrants.Columns[7].Width = 90;  // Last Verification
                int fixedTotal = 70 + 95 + 110 + 75 + 80 + 80 + 90;
                int avail = clientW - fixedTotal - 4;
                _lvGrants.Columns[1].Width = Math.Max(120, avail); // Folder
            }
            else
            {
                _lvGrants.Columns[0].Width = 80;  // Account
                _lvGrants.Columns[2].Width = 110; // Access Mode
                _lvGrants.Columns[3].Width = 130; // Status
                _lvGrants.Columns[4].Width = 90;  // Trusted Root
                _lvGrants.Columns[5].Width = 100; // Owner
                _lvGrants.Columns[6].Width = 100; // Expires
                _lvGrants.Columns[7].Width = 110; // Last Verification
                int fixedTotal = 80 + 110 + 130 + 90 + 100 + 100 + 110;
                int avail = clientW - fixedTotal - 4;
                _lvGrants.Columns[1].Width = Math.Max(160, avail); // Folder
            }
        }

        void OnAccountChanged()
        {
            string accId = SelectedAccountId();
            string backend = SelectedAccountBackend();
            string sid = _accountSids.ContainsKey(accId) ? _accountSids[accId] : "";
            _lblAccountDetail.Text = "SID: " + (sid.Length > 0 ? sid : "(none)") + "  |  Backend: " + backend;
            RefreshAccountFolders();
        }

        class AccountComboItem
        {
            public string Id { get; set; }
            public string Label { get; set; }
            public string Backend { get; set; }
            public override string ToString()
            {
                return Label + " (" + Id + ") [" + Backend + "]";
            }
        }

        string SelectedAccountId()
        {
            var item = _cmbAccount.SelectedItem as AccountComboItem;
            return item != null ? item.Id : "";
        }

        string SelectedAccountLabel()
        {
            var item = _cmbAccount.SelectedItem as AccountComboItem;
            return item != null ? item.Label : "";
        }

        string SelectedAccountBackend()
        {
            var item = _cmbAccount.SelectedItem as AccountComboItem;
            return item != null ? item.Backend : "";
        }

        FolderAccessEntry SelectedEntry()
        {
            if (_lvGrants.SelectedIndices.Count == 0) return null;
            int idx = _lvGrants.SelectedIndices[0];
            if (idx >= 0 && idx < _entries.Count) return _entries[idx];
            return null;
        }

        void UpdateButtonStates()
        {
            var sel = SelectedEntry();
            bool hasSel = sel != null;
            _btnRemoveFolder.Enabled = !_isBusy && hasSel;
            _btnGrant.Enabled = !_isBusy && hasSel;
            _btnRevoke.Enabled = !_isBusy && hasSel && (sel.Status == FolderAccessStatus.GRANTED || sel.Status == FolderAccessStatus.INCONSISTENT);
            _btnVerify.Enabled = !_isBusy && hasSel;
            _btnOpenExplorer.Enabled = !_isBusy && hasSel && Directory.Exists(sel.FolderPath);

            bool isDiagnostic = _lastProbedStatus == "BACKEND_OUTDATED" ||
                                _lastProbedStatus == "BACKEND_UNAVAILABLE" ||
                                _lastProbedStatus == "INVALID_RESPONSE" ||
                                _lastProbedStatus == "ACCOUNT_NOT_FOUND" ||
                                _lastProbedStatus == "ACCESS_DENIED" ||
                                _lastProbedStatus == "TIMEOUT" ||
                                _lastProbedStatus == "ERROR";

            _btnMarkTrustedRoot.Enabled = !_isBusy && hasSel && !isDiagnostic;

            if (hasSel)
            {
                _lblNote.Text = sel.FolderPath + " — Status: " + sel.Status + (sel.IsTrustedRoot ? " [TRUSTED ROOT]" : "") + " | " + sel.Diagnostics;
            }
            else
            {
                _lblNote.Text = "Select a folder row to verify or manage access.";
            }
        }

        public void LoadTrustedModeState(string label)
        {
            string accId = SelectedAccountId();
            var tm = FolderAccessManagerEngine.ProbeTrustedMode(label, accId, _accountsExe);
            ApplyTrustedModeResult(tm);
        }

        void ApplyTrustedModeResult(TrustedModeProbeResult tm)
        {
            _currentTrustedRecord = tm.RawRecord;
            _lastProbedStatus = tm.Status;
            _lastProbedPreference = tm.Preference;

            _lblTrustedBadge.Text = "Trusted Mode: " + tm.Status;
            if (tm.IsDiagnosticError && !string.IsNullOrEmpty(tm.ErrorDetail))
            {
                _lblTrustedDetail.Text = tm.ErrorDetail;
                _lblTrustedDetail.ForeColor = Palette.DANGERTXT;
            }
            else if (tm.ActiveSessions > 0)
            {
                _lblTrustedDetail.Text = "(" + tm.ActiveSessions + " active session" + (tm.ActiveSessions > 1 ? "s" : "") + ")";
                _lblTrustedDetail.ForeColor = Palette.BHL;
            }
            else if (!string.IsNullOrEmpty(tm.ErrorDetail))
            {
                _lblTrustedDetail.Text = tm.ErrorDetail;
                _lblTrustedDetail.ForeColor = Palette.WARNING;
            }
            else
            {
                _lblTrustedDetail.Text = "";
            }

            switch (tm.Status)
            {
                case "ON_VERIFIED":
                    _lblTrustedBadge.ForeColor = Palette.SUCCESS;
                    break;
                case "EMPTY_SCOPE":
                case "ENABLING":
                case "DISABLING":
                case "REVOKE_PENDING":
                    _lblTrustedBadge.ForeColor = Palette.WARNING;
                    break;
                case "OFF_VERIFIED":
                case "OFF":
                    _lblTrustedBadge.ForeColor = Palette.MUTED;
                    break;
                case "BACKEND_OUTDATED":
                case "BACKEND_UNAVAILABLE":
                case "INVALID_RESPONSE":
                case "ACCOUNT_NOT_FOUND":
                case "ACCESS_DENIED":
                case "TIMEOUT":
                case "ERROR":
                default:
                    _lblTrustedBadge.ForeColor = Palette.DANGERTXT;
                    break;
            }

            if (tm.IsDiagnosticError)
            {
                _btnToggleTrusted.Enabled = false;
                _btnToggleTrusted.Text = "Turn ON";
                _btnStopSession.Enabled = false;
                _btnMarkTrustedRoot.Enabled = false;
            }
            else
            {
                _btnToggleTrusted.Enabled = !_isBusy && (tm.Status != "ERROR");
                _btnToggleTrusted.Text = (tm.Preference == "ON") ? "Turn OFF" : "Turn ON";
                _btnStopSession.Enabled = !_isBusy && (tm.ActiveSessions > 0 || tm.Status == "REVOKE_PENDING");
            }
        }

        public void RefreshAccountFolders()
        {
            string accId = SelectedAccountId();
            if (string.IsNullOrEmpty(accId)) return;

            string label = SelectedAccountLabel();
            string backend = SelectedAccountBackend();
            string sid = _accountSids.ContainsKey(accId) ? _accountSids[accId] : "";

            int reqGen = Interlocked.Increment(ref _refreshGen);

            _btnRefresh.Enabled = false;
            _btnToggleTrusted.Enabled = false;
            _btnMarkTrustedRoot.Enabled = false;
            _btnStopSession.Enabled = false;

            _lblTrustedBadge.Text = "Trusted Mode: Checking...";
            _lblTrustedBadge.ForeColor = Palette.BHL;
            _lblTrustedDetail.Text = "Probing backend...";
            _lblTrustedDetail.ForeColor = Palette.TEXT2;
            _lblNote.Text = "Evaluating folder permissions and Trusted Mode status...";

            List<string> folders = _getAccountFolders != null ? _getAccountFolders(accId) : new List<string>();

            ThreadPool.QueueUserWorkItem(delegate
            {
                var tm = FolderAccessManagerEngine.ProbeTrustedMode(label, accId, _accountsExe);
                if (reqGen != _refreshGen) return;

                var evaluatedEntries = new List<FolderAccessEntry>();
                foreach (string folder in folders)
                {
                    if (reqGen != _refreshGen) return;

                    string owner, expires, diagnostics;
                    FolderAccessStatus status = FolderAccessManagerEngine.EvaluateFolderStatus(
                        label, sid, backend, folder, _accountsExe, _pythonExe, _scriptsDir,
                        out owner, out expires, out diagnostics);

                    bool isTrusted = false;
                    if (tm != null && tm.ApprovedRoots != null)
                    {
                        foreach (string r in tm.ApprovedRoots)
                        {
                            if (string.Equals(r.TrimEnd('\\', '/'), folder.TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase))
                            {
                                isTrusted = true;
                                break;
                            }
                        }
                    }

                    evaluatedEntries.Add(new FolderAccessEntry
                    {
                        AccountId = accId,
                        AccountLabel = label,
                        AccountBackend = backend,
                        AccountSid = sid,
                        FolderPath = folder,
                        AccessMode = status == FolderAccessStatus.GRANTED ? (owner.Contains("helper") ? "Modify workdir" : "Read-only lease") : "None",
                        Status = status,
                        IsTrustedRoot = isTrusted,
                        Owner = owner,
                        Expires = expires,
                        LastVerification = DateTime.Now.ToString("HH:mm:ss"),
                        Diagnostics = diagnostics
                    });
                }

                if (reqGen != _refreshGen) return;

                try
                {
                    if (IsDisposed || Disposing) return;
                    BeginInvoke(new Action(delegate
                    {
                        if (reqGen != _refreshGen || IsDisposed) return;
                        ApplyRefreshResults(tm, evaluatedEntries);
                    }));
                }
                catch { }
            });
        }

        void ApplyRefreshResults(TrustedModeProbeResult tm, List<FolderAccessEntry> evaluatedEntries)
        {
            ApplyTrustedModeResult(tm);

            _entries.Clear();
            _entries.AddRange(evaluatedEntries);
            _lvGrants.Items.Clear();

            foreach (var entry in _entries)
            {
                var lvi = new ListViewItem(entry.AccountLabel);
                lvi.SubItems.Add(entry.FolderPath);
                lvi.SubItems.Add(entry.AccessMode);
                lvi.SubItems.Add(entry.Status.ToString());
                lvi.SubItems.Add(entry.IsTrustedRoot ? "YES" : "NO");
                lvi.SubItems.Add(entry.Owner);
                lvi.SubItems.Add(entry.Expires);
                lvi.SubItems.Add(entry.LastVerification);

                switch (entry.Status)
                {
                    case FolderAccessStatus.GRANTED:
                        lvi.ForeColor = Palette.SUCCESS;
                        break;
                    case FolderAccessStatus.INHERITED_OR_PREEXISTING:
                        lvi.ForeColor = Palette.BHL;
                        break;
                    case FolderAccessStatus.DENIED:
                        lvi.ForeColor = Palette.DANGERTXT;
                        break;
                    case FolderAccessStatus.EXPIRED:
                        lvi.ForeColor = Palette.WARNING;
                        break;
                    case FolderAccessStatus.REVOKED:
                        lvi.ForeColor = Palette.MUTED;
                        break;
                    case FolderAccessStatus.INCONSISTENT:
                        lvi.ForeColor = Palette.DANGERTXT;
                        break;
                    default:
                        lvi.ForeColor = Palette.MUTED;
                        break;
                }

                _lvGrants.Items.Add(lvi);
            }

            _btnRefresh.Enabled = true;
            UpdateButtonStates();
            UpdateColumnWidths();
        }

        void ToggleTrustedMode()
        {
            if (_isBusy) return;
            string label = SelectedAccountLabel();
            if (string.IsNullOrEmpty(label)) return;

            string pref = _lastProbedPreference ?? "OFF";
            string target = (pref == "ON") ? "off" : "on";
            string args = "trusted-mode-set \"" + label + "\" " + target;

            if (target == "on")
            {
                var roots = new List<string>();
                foreach (var entry in _entries)
                {
                    if (entry.IsTrustedRoot)
                    {
                        if (!roots.Contains(entry.FolderPath)) roots.Add(entry.FolderPath);
                    }
                }
                if (roots.Count > 0)
                {
                    args += " --roots \"" + string.Join(";", roots.ToArray()) + "\"";
                }
            }

            if (string.IsNullOrEmpty(_accountsExe) || !File.Exists(_accountsExe))
            {
                SaitulsDialog.Show(this, "Cannot update Trusted Mode: accounts backend executable not found.", "Backend Unavailable", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            _isBusy = true;
            _btnToggleTrusted.Enabled = false;
            _btnMarkTrustedRoot.Enabled = false;
            _lblNote.Text = "Updating Trusted Mode preference to " + target.ToUpperInvariant() + "...";

            ThreadPool.QueueUserWorkItem(delegate
            {
                int exitCode;
                string stdout, stderr;
                bool timedOut;
                bool ok = FolderAccessManagerEngine.RunProcessBounded(_accountsExe, args, 15000, out exitCode, out stdout, out stderr, out timedOut);

                string errorMsg = null;
                if (timedOut)
                {
                    errorMsg = "Operation timed out after 15 seconds.";
                }
                else if (!ok)
                {
                    errorMsg = "Failed to launch backend: " + stderr;
                }
                else
                {
                    try
                    {
                        var js = new JavaScriptSerializer();
                        var res = js.DeserializeObject(stdout.Trim()) as Dictionary<string, object>;
                        if (exitCode != 0 || res == null)
                        {
                            errorMsg = (res != null && res.ContainsKey("error") && res["error"] != null)
                                ? res["error"].ToString()
                                : (!string.IsNullOrEmpty(stderr) ? stderr : ("Backend error: exit code " + exitCode));
                        }
                        else if (res.ContainsKey("status") && string.Equals(res["status"] as string, "ERROR", StringComparison.OrdinalIgnoreCase))
                        {
                            errorMsg = res.ContainsKey("error") ? res["error"].ToString() : "Backend reported transition ERROR";
                        }
                    }
                    catch (Exception ex)
                    {
                        errorMsg = "Invalid response from backend: " + ex.Message;
                    }
                }

                BeginInvoke(new Action(delegate
                {
                    _isBusy = false;
                    if (!string.IsNullOrEmpty(errorMsg))
                    {
                        SaitulsDialog.Show(this, "Trusted Mode transition failed:\n" + errorMsg, "Trusted Mode Refusal", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    }
                    RefreshAccountFolders();
                }));
            });
        }

        void ToggleMarkTrustedRoot()
        {
            if (_isBusy) return;
            var sel = SelectedEntry();
            if (sel == null) return;

            string label = SelectedAccountLabel();
            if (string.IsNullOrEmpty(label)) return;

            var currentRoots = new List<string>();
            if (_currentTrustedRecord != null && _currentTrustedRecord.ContainsKey("approved_roots"))
            {
                var appObj = _currentTrustedRecord["approved_roots"] as object[];
                if (appObj != null)
                {
                    foreach (object o in appObj) if (o is string) currentRoots.Add((string)o);
                }
            }

            bool alreadyMarked = currentRoots.Exists(r => string.Equals(r.TrimEnd('\\', '/'), sel.FolderPath.TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase));
            if (alreadyMarked)
            {
                currentRoots.RemoveAll(r => string.Equals(r.TrimEnd('\\', '/'), sel.FolderPath.TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase));
            }
            else
            {
                currentRoots.Add(sel.FolderPath);
            }

            string pref = _lastProbedPreference ?? "OFF";
            string targetPref = (pref == "ON") ? "on" : "off";
            string args = "trusted-mode-set \"" + label + "\" " + targetPref + " --roots \"" + string.Join(";", currentRoots.ToArray()) + "\"";

            if (string.IsNullOrEmpty(_accountsExe) || !File.Exists(_accountsExe))
            {
                SaitulsDialog.Show(this, "Cannot update Trusted Roots: backend executable not found.", "Backend Unavailable", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            _isBusy = true;
            _btnMarkTrustedRoot.Enabled = false;
            _lblNote.Text = "Updating approved roots list...";

            ThreadPool.QueueUserWorkItem(delegate
            {
                int exitCode;
                string stdout, stderr;
                bool timedOut;
                bool ok = FolderAccessManagerEngine.RunProcessBounded(_accountsExe, args, 12000, out exitCode, out stdout, out stderr, out timedOut);

                string errorMsg = null;
                if (timedOut)
                {
                    errorMsg = "Operation timed out.";
                }
                else if (!ok)
                {
                    errorMsg = "Backend execution failed: " + stderr;
                }
                else
                {
                    try
                    {
                        var js = new JavaScriptSerializer();
                        var res = js.DeserializeObject(stdout.Trim()) as Dictionary<string, object>;
                        if (exitCode != 0 || res == null)
                        {
                            errorMsg = (res != null && res.ContainsKey("error") && res["error"] != null)
                                ? res["error"].ToString()
                                : (!string.IsNullOrEmpty(stderr) ? stderr : ("Backend error: exit code " + exitCode));
                        }
                    }
                    catch (Exception ex)
                    {
                        errorMsg = "Invalid backend response: " + ex.Message;
                    }
                }

                BeginInvoke(new Action(delegate
                {
                    _isBusy = false;
                    if (!string.IsNullOrEmpty(errorMsg))
                    {
                        SaitulsDialog.Show(this, "Could not update Trusted Roots:\n" + errorMsg, "Trusted Root Refusal", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    }
                    RefreshAccountFolders();
                }));
            });
        }

        void StopCurrentSession()
        {
            if (_isBusy) return;
            string label = SelectedAccountLabel();
            if (string.IsNullOrEmpty(label)) return;

            var dr = SaitulsDialog.Show(this,
                "Terminate active Antigravity session(s) for account " + label + "?\n\nThis will release any open file handles held by the agent session.",
                "Stop Account Session", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
            if (dr != DialogResult.Yes) return;

            if (string.IsNullOrEmpty(_accountsExe) || !File.Exists(_accountsExe))
            {
                SaitulsDialog.Show(this, "Cannot stop session: backend executable not found.", "Backend Unavailable", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            _isBusy = true;
            _btnStopSession.Enabled = false;
            _lblNote.Text = "Terminating active agent session for " + label + "...";

            ThreadPool.QueueUserWorkItem(delegate
            {
                int exitCode;
                string stdout, stderr;
                bool timedOut;
                bool ok = FolderAccessManagerEngine.RunProcessBounded(_accountsExe, "stop-session \"" + label + "\"", 10000, out exitCode, out stdout, out stderr, out timedOut);

                string errorMsg = null;
                if (timedOut)
                {
                    errorMsg = "Stop session timed out.";
                }
                else if (!ok || exitCode != 0)
                {
                    errorMsg = !string.IsNullOrEmpty(stderr) ? stderr : (!string.IsNullOrEmpty(stdout) ? stdout : "Exit code " + exitCode);
                }

                BeginInvoke(new Action(delegate
                {
                    _isBusy = false;
                    if (!string.IsNullOrEmpty(errorMsg))
                    {
                        SaitulsDialog.Show(this, "Could not stop session:\n" + errorMsg, "Stop Session Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    }
                    RefreshAccountFolders();
                }));
            });
        }

        void AddFolderPrompt()
        {
            string accId = SelectedAccountId();
            if (string.IsNullOrEmpty(accId)) return;

            using (var fb = new FolderBrowserDialog())
            {
                fb.Description = "Select a folder to add to the account's managed folder list:";
                if (fb.ShowDialog(this) != DialogResult.OK) return;

                string picked = fb.SelectedPath;
                if (FolderAccessManagerEngine.IsDiskRoot(picked))
                {
                    SaitulsDialog.Show(this, "Blanket access to disk roots is prohibited for security.", "Root Folder Refused", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                if (FolderAccessManagerEngine.IsReparsePoint(picked))
                {
                    SaitulsDialog.Show(this, "Reparse points, symlinks and junctions are refused for security.", "Reparse Point Refused", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }

                List<string> folders = _getAccountFolders != null ? _getAccountFolders(accId) : new List<string>();
                if (!folders.Exists(x => string.Equals(x, picked, StringComparison.OrdinalIgnoreCase)))
                {
                    folders.Add(picked);
                    if (_saveAccountFolders != null) _saveAccountFolders(accId, folders);
                }

                RefreshAccountFolders();
            }
        }

        void RemoveSelectedFolder()
        {
            var sel = SelectedEntry();
            if (sel == null) return;

            var dr = SaitulsDialog.Show(this,
                "Remove \"" + sel.FolderPath + "\" from the favorites list?\n\nNOTE: Removing from the list does NOT alter NTFS permissions.",
                "Remove Folder from List", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
            if (dr != DialogResult.Yes) return;

            List<string> folders = _getAccountFolders != null ? _getAccountFolders(sel.AccountId) : new List<string>();
            folders.RemoveAll(p => string.Equals(p, sel.FolderPath, StringComparison.OrdinalIgnoreCase));
            if (_saveAccountFolders != null) _saveAccountFolders(sel.AccountId, folders);

            RefreshAccountFolders();
        }

        void VerifySelectedAccess()
        {
            var sel = SelectedEntry();
            if (sel == null) return;
            RefreshAccountFolders();
        }

        void OpenSelectedInExplorer()
        {
            var sel = SelectedEntry();
            if (sel == null || !Directory.Exists(sel.FolderPath)) return;
            try
            {
                Process.Start("explorer.exe", sel.FolderPath);
            }
            catch (Exception ex)
            {
                SaitulsDialog.Show(this, "Could not open Explorer: " + ex.Message, "Explorer Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        void GrantAccessFlow()
        {
            var sel = SelectedEntry();
            if (sel == null) return;

            using (var dlg = new GrantAccessConfirmDialog(sel.AccountLabel, sel.FolderPath))
            {
                if (dlg.ShowDialog(this) != DialogResult.OK) return;

                FolderAccessMode mode = dlg.SelectedMode;
                string err = ExecuteGrant(sel.AccountLabel, sel.FolderPath, mode);
                if (!string.IsNullOrEmpty(err))
                {
                    SaitulsDialog.Show(this, "Grant failed: " + err, "Access Grant Failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
                }
                RefreshAccountFolders();
            }
        }

        string ExecuteGrant(string accountLabel, string folderPath, FolderAccessMode mode)
        {
            if (mode == FolderAccessMode.ManagedWorkdirModify)
            {
                if (string.IsNullOrEmpty(_accountsExe) || !File.Exists(_accountsExe))
                    return "sai-accounts.exe not found.";
                try
                {
                    int exitCode;
                    string stdout, stderr;
                    bool timedOut;
                    bool ok = FolderAccessManagerEngine.RunProcessBounded(_accountsExe, "grant-workdir \"" + accountLabel + "\" --workdir \"" + folderPath + "\"", 20000, out exitCode, out stdout, out stderr, out timedOut);
                    if (timedOut) return "Grant operation timed out.";
                    if (!ok || exitCode != 0) return !string.IsNullOrEmpty(stderr) ? stderr : stdout;
                    if (stdout.Contains("\"status\":\"granted\"") || stdout.Contains("\"status\":\"already_granted\""))
                        return null;
                    return stdout.Trim();
                }
                catch (Exception ex) { return ex.Message; }
            }
            else if (mode == FolderAccessMode.ReadOnlyLease15m || mode == FolderAccessMode.ReadOnlyLease60m || mode == FolderAccessMode.ReadOnlySession)
            {
                if (string.IsNullOrEmpty(_pythonExe) || !File.Exists(_pythonExe))
                    return "Python runtime not found.";
                string script = Path.Combine(_scriptsDir, "sai_access.py");
                if (!File.Exists(script)) return "sai_access.py not found.";

                string flag = mode == FolderAccessMode.ReadOnlySession ? "--session" : (mode == FolderAccessMode.ReadOnlyLease15m ? "--minutes 15" : "--minutes 60");
                try
                {
                    int exitCode;
                    string stdout, stderr;
                    bool timedOut;
                    bool ok = FolderAccessManagerEngine.RunProcessBounded(_pythonExe, "\"" + script + "\" grant --account \"" + accountLabel + "\" --source \"" + folderPath + "\" " + flag, 15000, out exitCode, out stdout, out stderr, out timedOut);
                    if (timedOut) return "Grant lease timed out.";
                    if (!ok || exitCode != 0) return !string.IsNullOrEmpty(stderr) ? stderr : stdout;
                    if (stdout.Contains("\"status\": \"ok\"")) return null;
                    return stdout.Trim();
                }
                catch (Exception ex) { return ex.Message; }
            }
            else if (mode == FolderAccessMode.StagedHandoff)
            {
                if (string.IsNullOrEmpty(_accountsExe) || !File.Exists(_accountsExe))
                    return "sai-accounts.exe not found.";
                try
                {
                    int exitCode;
                    string stdout, stderr;
                    bool timedOut;
                    bool ok = FolderAccessManagerEngine.RunProcessBounded(_accountsExe, "handoff-stage \"" + accountLabel + "\" --workdir \"" + folderPath + "\"", 20000, out exitCode, out stdout, out stderr, out timedOut);
                    if (timedOut) return "Handoff staging timed out.";
                    if (!ok || exitCode != 0) return !string.IsNullOrEmpty(stderr) ? stderr : stdout;
                    if (stdout.Contains("\"status\":\"ok\"") || stdout.Contains("\"status\":\"staged\"")) return null;
                    return stdout.Trim();
                }
                catch (Exception ex) { return ex.Message; }
            }
            return "Selected mode is not supported directly.";
        }

        void RevokeSelectedGrant()
        {
            var sel = SelectedEntry();
            if (sel == null) return;

            var dr = SaitulsDialog.Show(this,
                "Revoke access for account \"" + sel.AccountLabel + "\" on folder:\n" + sel.FolderPath +
                "\n\nOnly the specific grant rule owned by SAITULS will be removed. All foreign/pre-existing permissions remain intact.",
                "Confirm Exact-Path Revocation", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
            if (dr != DialogResult.Yes) return;

            string err = null;
            if (sel.Owner.Contains("sai_access"))
            {
                if (!string.IsNullOrEmpty(_pythonExe) && !string.IsNullOrEmpty(_scriptsDir))
                {
                    string script = Path.Combine(_scriptsDir, "sai_access.py");
                    try
                    {
                        int exitCode;
                        string stdout, stderr;
                        bool timedOut;
                        bool ok = FolderAccessManagerEngine.RunProcessBounded(_pythonExe, "\"" + script + "\" revoke --account \"" + sel.AccountLabel + "\" --source \"" + sel.FolderPath + "\"", 15000, out exitCode, out stdout, out stderr, out timedOut);
                        if (timedOut) err = "Revocation timed out.";
                        else if (!ok || exitCode != 0 || !stdout.Contains("\"status\": \"ok\"")) err = !string.IsNullOrEmpty(stderr) ? stderr : stdout.Trim();
                    }
                    catch (Exception ex) { err = ex.Message; }
                }
            }
            else
            {
                if (!string.IsNullOrEmpty(_accountsExe) && File.Exists(_accountsExe))
                {
                    try
                    {
                        int exitCode;
                        string stdout, stderr;
                        bool timedOut;
                        bool ok = FolderAccessManagerEngine.RunProcessBounded(_accountsExe, "revoke-workdir \"" + sel.AccountLabel + "\" --workdir \"" + sel.FolderPath + "\"", 20000, out exitCode, out stdout, out stderr, out timedOut);
                        if (timedOut) err = "Revocation timed out.";
                        else if (!ok || exitCode != 0 || (!stdout.Contains("\"status\":\"revoked\"") && !stdout.Contains("\"status\":\"already_revoked\"")))
                            err = !string.IsNullOrEmpty(stderr) ? stderr : stdout.Trim();
                    }
                    catch (Exception ex) { err = ex.Message; }
                }
            }

            if (!string.IsNullOrEmpty(err))
            {
                SaitulsDialog.Show(this, "Revoke failed: " + err, "Revocation Refusal", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            RefreshAccountFolders();
        }
    }

    public class GrantAccessConfirmDialog : Form
    {
        readonly RadioButton _rbModify;
        readonly RadioButton _rbLease15;
        readonly RadioButton _rbLease60;
        readonly RadioButton _rbLeaseSession;
        readonly RadioButton _rbHandoff;
        readonly Label _lblExplanation;

        public FolderAccessMode SelectedMode
        {
            get
            {
                if (_rbModify.Checked) return FolderAccessMode.ManagedWorkdirModify;
                if (_rbLease15.Checked) return FolderAccessMode.ReadOnlyLease15m;
                if (_rbLease60.Checked) return FolderAccessMode.ReadOnlyLease60m;
                if (_rbLeaseSession.Checked) return FolderAccessMode.ReadOnlySession;
                if (_rbHandoff.Checked) return FolderAccessMode.StagedHandoff;
                return FolderAccessMode.ManagedWorkdirModify;
            }
        }

        public GrantAccessConfirmDialog(string accountLabel, string folderPath)
        {
            Text = "Grant Folder Access — Operator Confirmation";
            Size = new Size(580, 420);
            StartPosition = FormStartPosition.CenterParent;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            Font = new Font("Segoe UI", 9f);
            BackColor = Palette.BG;
            ForeColor = Palette.TEXT;

            var lblHeader = new Label
            {
                Location = new Point(16, 12),
                Size = new Size(540, 36),
                Text = "Granting filesystem access for " + accountLabel + " to:\n" + folderPath,
                Font = new Font("Segoe UI", 9f, FontStyle.Bold),
                ForeColor = Palette.TEXT
            };

            var grpMode = new GroupBox
            {
                Text = "Supported Access Modes",
                Location = new Point(16, 52),
                Size = new Size(540, 160),
                ForeColor = Palette.TEXT
            };

            _rbModify = new RadioButton { Text = "Managed workdir Modify access (Persistent ACE with child inheritance)", Location = new Point(16, 24), AutoSize = true, Checked = true, ForeColor = Palette.TEXT };
            _rbLease15 = new RadioButton { Text = "Read-only temporary lease — 15 minutes (Automatic expiry)", Location = new Point(16, 50), AutoSize = true, ForeColor = Palette.TEXT };
            _rbLease60 = new RadioButton { Text = "Read-only temporary lease — 60 minutes (Automatic expiry)", Location = new Point(16, 76), AutoSize = true, ForeColor = Palette.TEXT };
            _rbLeaseSession = new RadioButton { Text = "Read-only session lease (Expires on logout/reboot)", Location = new Point(16, 102), AutoSize = true, ForeColor = Palette.TEXT };
            _rbHandoff = new RadioButton { Text = "Staged file handoff (Isolated copy in .sai-handoff, read-only to agent)", Location = new Point(16, 128), AutoSize = true, ForeColor = Palette.TEXT };

            EventHandler updateExpl = (s, e) => UpdateExplanation();
            _rbModify.CheckedChanged += updateExpl;
            _rbLease15.CheckedChanged += updateExpl;
            _rbLease60.CheckedChanged += updateExpl;
            _rbLeaseSession.CheckedChanged += updateExpl;
            _rbHandoff.CheckedChanged += updateExpl;

            grpMode.Controls.AddRange(new Control[] { _rbModify, _rbLease15, _rbLease60, _rbLeaseSession, _rbHandoff });

            _lblExplanation = new Label
            {
                Location = new Point(16, 220),
                Size = new Size(540, 110),
                BackColor = Palette.COMPARE,
                ForeColor = Palette.TEXT,
                BorderStyle = BorderStyle.FixedSingle,
                Padding = new Padding(8)
            };

            var btnConfirm = new Button { Text = "Confirm & Apply", DialogResult = DialogResult.OK, Location = new Point(310, 340), Width = 130, Height = 28 };
            FolderAccessManagerDialog.StyleButton(btnConfirm);
            var btnCancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Location = new Point(450, 340), Width = 100, Height = 28 };
            FolderAccessManagerDialog.StyleButton(btnCancel);

            Controls.AddRange(new Control[] { lblHeader, grpMode, _lblExplanation, btnConfirm, btnCancel });
            UpdateExplanation();
        }

        void UpdateExplanation()
        {
            if (_rbModify.Checked)
            {
                _lblExplanation.Text = "MODE: Managed Workdir Modify Access\n" +
                    "• Scope: Target directory and all subdirectories/files (OI|CI|Inherit).\n" +
                    "• Persistence: Persistent until explicitly revoked.\n" +
                    "• Journal: Journaled in grants.json with ACL witness for exact rollback.\n" +
                    "• Security: Foreign ACEs are preserved. Disk roots and junctions are refused.";
            }
            else if (_rbLease15.Checked || _rbLease60.Checked)
            {
                string mins = _rbLease15.Checked ? "15" : "60";
                _lblExplanation.Text = "MODE: Temporary Read-Only Lease (" + mins + " minutes)\n" +
                    "• Scope: Target directory files (non-inheriting container lease).\n" +
                    "• Persistence: Automatically swept and revoked after " + mins + " minutes.\n" +
                    "• Journal: Owned by sai_access.py journal with baseline DACL witness.\n" +
                    "• Security: Read-only rights only. No modify or execute permission.";
            }
            else if (_rbLeaseSession.Checked)
            {
                _lblExplanation.Text = "MODE: Read-Only Session Lease\n" +
                    "• Scope: Target directory files (read-only).\n" +
                    "• Persistence: Valid during current session; swept on restart/reconcile.\n" +
                    "• Journal: Recorded with baseline witness.\n" +
                    "• Security: Does not broaden ancestor traversal.";
            }
            else if (_rbHandoff.Checked)
            {
                _lblExplanation.Text = "MODE: Staged File Handoff\n" +
                    "• Scope: Isolated staging directory inside .sai-handoff.\n" +
                    "• Persistence: Session/managed workspace lifetime.\n" +
                    "• Journal: Created and tracked by sai_relay.\n" +
                    "• Security: Agent never touches original directory directly.";
            }
        }
    }
}
