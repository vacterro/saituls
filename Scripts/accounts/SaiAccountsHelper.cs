// SAI Accounts Helper — privileged lifecycle helper for managed account contexts.
// Contract: Scripts/accounts/SAI_ACCOUNTS.md. Owner: SAITULS.
// Compile:
//   csc -nologo -out:sai-accounts-helper.exe -optimize+
//       -r:System.dll -r:System.Core.dll -r:System.Security.dll
//       -r:System.ServiceProcess.dll -r:System.Web.Extensions.dll
//       SaiAccountsHelper.cs
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using Microsoft.Win32;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

public class SaiAccountsHelperService : ServiceBase
{
    private Thread _worker;
    private volatile bool _stopping;

    public SaiAccountsHelperService()
    {
        ServiceName = "SaiAccountsHelper";
    }

    protected override void OnStart(string[] args)
    {
        _stopping = false;
        _worker = new Thread(WorkerRun) { IsBackground = true };
        _worker.Start();
    }

    protected override void OnStop()
    {
        _stopping = true;
        SaiAccountsHelper.StopServer();
        if (_worker != null) _worker.Join(3000);
    }

    private void WorkerRun()
    {
        SaiAccountsHelper.RunServer(null, () => _stopping);
    }
}

public static class SaiAccountsHelper
{
    public const string ServiceName = "SaiAccountsHelper";
    public const string DefaultPipeName = "sai-accounts-helper";

    private static volatile bool _stopRequested = false;

    // ------------------------------------------------------------- paths & config
    public static string RootDir()
    {
        string env = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_ROOT");
        if (!string.IsNullOrEmpty(env)) return env;
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), @"SAI\Accounts");
    }

    public static string BinDir() { return Path.Combine(RootDir(), "bin"); }
    // The one Antigravity the helper will ever name. Same rule the engine
    // follows: the caller picks the account, the trusted component picks the
    // binary. This exists only to derive the managed account's per-executable
    // console key name, and it reads the same canonical path the broker will.
    public static string SharedAgyPath() { return Path.Combine(BinDir(), "agy.exe"); }
    public static string VaultDir() { return Path.Combine(RootDir(), "vault"); }
    public static string HelperDir() { return Path.Combine(RootDir(), "helper"); }
    public static string HelperConfigFile() { return Path.Combine(HelperDir(), "helper.json"); }
    public static string PipeName()
    {
        string env = Environment.GetEnvironmentVariable("SAI_HELPER_PIPE");
        return !string.IsNullOrEmpty(env) ? env : DefaultPipeName;
    }

    public static string BrokerExePath()
    {
        string env = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_EXE");
        if (!string.IsNullOrEmpty(env) && File.Exists(env)) return env;
        string inBin = Path.Combine(BinDir(), "sai-accounts.exe");
        if (File.Exists(inBin)) return inBin;
        string inSelfDir = Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location), "sai-accounts.exe");
        if (File.Exists(inSelfDir)) return inSelfDir;
        return inBin;
    }

    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024, RecursionLimit = 24 };

    static Dictionary<string, object> Obj() { return new Dictionary<string, object>(StringComparer.Ordinal); }
    static void Set(Dictionary<string, object> d, string k, object v) { d[k] = v; }
    static object Get(Dictionary<string, object> d, string k) { object v; return d != null && d.TryGetValue(k, out v) ? v : null; }
    static string S(Dictionary<string, object> d, string k) { object v = Get(d, k); return v != null ? v.ToString() : null; }

    static string Fingerprint(string raw)
    {
        if (string.IsNullOrEmpty(raw)) return "0000000000000000";
        using (var sha = SHA256.Create())
        {
            byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(raw.Trim().ToLowerInvariant()));
            var sb = new StringBuilder(16);
            for (int i = 0; i < 8; i++) sb.Append(hash[i].ToString("x2"));
            return sb.ToString();
        }
    }

    public static void StopServer()
    {
        _stopRequested = true;
    }

    // ------------------------------------------------------------- Win32 P/Invoke
    // Win32 CreateProcessWithLogonW takes ELEVEN parameters and there is no hToken
    // in it. The phantom hToken that used to sit between dwLogonFlags and
    // lpApplicationName shifted every later argument by one register-width, so the
    // CLR marshalled the application name into dwCreationFlags and the whole tail
    // of the call was garbage. This is the ABI, not a naming style.
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern bool CreateProcessWithLogonW(string lpUsername, string lpDomain, string lpPassword,
        int dwLogonFlags,
        string lpApplicationName, string lpCommandLine,
        uint dwCreationFlags, IntPtr lpEnvironment, string lpCurrentDirectory,
        ref STARTUPINFO lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);

    // dwLogonFlags values for CreateProcessWithLogonW. LOGON_NETCREDENTIALS_ONLY (2)
    // leaves the child on the caller's own token, which for a LocalSystem helper means
    // the broker would run as SYSTEM and the expected-SID check would be a lie.
    private const int LOGON_WITH_PROFILE = 1;
    private const int LOGON_NETCREDENTIALS_ONLY = 2;

    private const uint CREATE_NO_WINDOW = 0x08000000;
    private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
    private const uint STARTF_USESHOWWINDOW = 0x00000001;
    private const uint DUPLICATE_SAME_ACCESS = 0x00000002;
    private const uint TOKEN_ALL_ACCESS = 0x000F01FF;
    private const int SecurityImpersonation = 2;
    private const int TokenImpersonation = 2;

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool ImpersonateNamedPipeClient(IntPtr hNamedPipe);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool RevertToSelf();

    // The session bridge. A password logon only produces a usable logon session when
    // it is performed by a process that already lives in the interactive session;
    // from session 0 the child dies with ERROR_NO_SUCH_LOGON_SESSION. So the helper
    // never logs on. It borrows the operator's genuine token and runs the logon there.
    [DllImport("wtsapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern bool WTSEnumerateSessionsW(IntPtr hServer, int reserved, int version,
        out IntPtr ppSessionInfo, out int pCount);

    [DllImport("wtsapi32.dll", ExactSpelling = true)]
    private static extern void WTSFreeMemory(IntPtr p);

    [DllImport("wtsapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern bool WTSQueryUserToken(uint sessionId, out IntPtr phToken);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool DuplicateTokenEx(IntPtr hExistingToken, uint dwDesiredAccess,
        IntPtr pTokenAttributes, int ImpersonationLevel, int TokenType, out IntPtr phNewToken);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool CreateProcessAsUser(IntPtr hToken, string lpApplicationName,
        string lpCommandLine, IntPtr lpProcessAttributes, IntPtr lpThreadAttributes,
        bool bInheritHandles, uint dwCreationFlags, IntPtr lpEnvironment, string lpCurrentDirectory,
        ref STARTUPINFO lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);

    [DllImport("userenv.dll", SetLastError = true)]
    private static extern bool CreateEnvironmentBlock(out IntPtr lpEnvironment, IntPtr hToken, bool bInherit);

    [DllImport("userenv.dll", SetLastError = true)]
    private static extern bool DestroyEnvironmentBlock(IntPtr lpEnvironment);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int RegLoadKeyW(IntPtr hKey, string lpSubKey, string lpFile);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int RegUnLoadKeyW(IntPtr hKey, string lpSubKey);

    [DllImport("advapi32.dll", SetLastError = true, ExactSpelling = true)]
    private static extern int RegCloseKey(IntPtr hKey);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int RegOpenKeyExW(IntPtr hKey, string lpSubKey, int ulOptions, int samDesired, out IntPtr phkResult);

    private static readonly IntPtr HKEY_USERS = new IntPtr(unchecked((int)0x80000003));

    // KEY_READ | KEY_WRITE, asked for explicitly because the managed console
    // contract has to be WRITTEN into this hive. RegistryKey.OpenBaseKey and the
    // one-argument OpenSubKey hand back read-plus-create-subkey only: enough to
    // find HKU\<sid> and to create Console\ beneath it, then
    // UnauthorizedAccessException at the first SetValue. The rights-taking
    // overload of OpenBaseKey does not exist on the .NET 4.0 framework this
    // helper is compiled against, so the handle is taken directly.
    const int ManagedHiveRights = 0x20019 | 0x20006; // KEY_READ | KEY_WRITE

    // Returns a writable handle on an ALREADY-LOADED hive, or null. FromHandle
    // takes ownership, so the caller closes it and nothing else does.
    static RegistryKey OpenManagedHiveForWrite(string targetSid)
    {
        IntPtr h = IntPtr.Zero;
        if (RegOpenKeyExW(HKEY_USERS, targetSid, 0, ManagedHiveRights, out h) != 0) return null;
        if (h == IntPtr.Zero) return null;
        var safe = new Microsoft.Win32.SafeHandles.SafeRegistryHandle(h, true);
        try { return RegistryKey.FromHandle(safe); }
        catch { safe.Dispose(); return null; }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WTS_SESSION_INFO
    {
        public IntPtr SessionId;
        [MarshalAs(UnmanagedType.LPWStr)] public string WinStationName;
        public int State;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public uint dwX; public uint dwY;
        public uint dwXSize; public uint dwYSize;
        public uint dwXCountChars; public uint dwYCountChars;
        public uint dwFillAttribute;
        public uint dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    // netapi32 for local account creation
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct USER_INFO_1
    {
        public string usri1_name;
        public string usri1_password;
        public uint usri1_password_age;
        public uint usri1_priv;
        public string usri1_home_dir;
        public string usri1_comment;
        public uint usri1_flags;
        public string usri1_script_path;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct USER_INFO_1003
    {
        public string usri1003_password;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct LOCALGROUP_MEMBERS_INFO_3
    {
        public string lgrmi3_domainandname;
    }

    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int NetUserAdd(string servername, uint level, ref USER_INFO_1 buf, out uint parm_err);

    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int NetUserSetInfo(string servername, string username, uint level, ref USER_INFO_1003 buf, out uint parm_err);

    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int NetUserDel(string servername, string username);

    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int NetLocalGroupAddMembers(string servername, string groupname, uint level,
        ref LOCALGROUP_MEMBERS_INFO_3 buf, uint totalentries);

    const uint UF_SCRIPT = 0x0001;
    const uint UF_DONT_EXPIRE_PASSWORD = 0x10000;
    const uint UF_NORMAL_ACCOUNT = 0x0200;
    const uint USER_PRIV_USER = 1;
    const uint STILL_ACTIVE = 259;

    // ------------------------------------------------------------- secret management
    private static byte[] EntropyFor(string sid)
    {
        using (var sha = SHA256.Create())
        {
            return sha.ComputeHash(Encoding.UTF8.GetBytes("SAI_HELPER_SEAL_V1:" + (sid ?? "").ToLowerInvariant()));
        }
    }

    private static bool ValidateDirectoryAcl(string dir, IEnumerable<SecurityIdentifier> allowedPrincipals, bool requireFullControlForSystemAndAdmins)
    {
        try
        {
            var dinfo = new DirectoryInfo(dir);
            var sec = dinfo.GetAccessControl();
            if (!sec.AreAccessRulesProtected) return false;

            var inheritedRules = sec.GetAccessRules(false, true, typeof(SecurityIdentifier));
            if (inheritedRules.Count > 0) return false;

            var explicitRules = sec.GetAccessRules(true, false, typeof(SecurityIdentifier));
            if (explicitRules.Count == 0) return false;

            bool systemHasFull = false;
            bool adminsHaveFull = false;

            var allowedSet = new HashSet<SecurityIdentifier>(allowedPrincipals);
            var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            var adminsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);

            foreach (FileSystemAccessRule rule in explicitRules)
            {
                if (rule.AccessControlType != AccessControlType.Allow) return false;
                var sid = rule.IdentityReference as SecurityIdentifier;
                if (sid == null || !allowedSet.Contains(sid)) return false;

                if (sid.Equals(systemSid) && (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl)
                {
                    systemHasFull = true;
                }
                if (sid.Equals(adminsSid) && (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl)
                {
                    adminsHaveFull = true;
                }
            }

            if (requireFullControlForSystemAndAdmins)
            {
                if (!systemHasFull || !adminsHaveFull) return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("ValidateDirectoryAcl exception: " + ex.Message);
            return false;
        }
    }

    private static bool ValidateFileAcl(string file, IEnumerable<SecurityIdentifier> allowedPrincipals, bool requireFullControlForSystemAndAdmins)
    {
        try
        {
            var finfo = new FileInfo(file);
            var sec = finfo.GetAccessControl();
            if (!sec.AreAccessRulesProtected) return false;

            var inheritedRules = sec.GetAccessRules(false, true, typeof(SecurityIdentifier));
            if (inheritedRules.Count > 0) return false;

            var explicitRules = sec.GetAccessRules(true, false, typeof(SecurityIdentifier));
            if (explicitRules.Count == 0) return false;

            bool systemHasFull = false;
            bool adminsHaveFull = false;

            var allowedSet = new HashSet<SecurityIdentifier>(allowedPrincipals);
            var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            var adminsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);

            foreach (FileSystemAccessRule rule in explicitRules)
            {
                if (rule.AccessControlType != AccessControlType.Allow) return false;
                var sid = rule.IdentityReference as SecurityIdentifier;
                if (sid == null || !allowedSet.Contains(sid)) return false;

                if (sid.Equals(systemSid) && (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl)
                {
                    systemHasFull = true;
                }
                if (sid.Equals(adminsSid) && (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl)
                {
                    adminsHaveFull = true;
                }
            }

            if (requireFullControlForSystemAndAdmins)
            {
                if (!systemHasFull || !adminsHaveFull) return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("ValidateFileAcl exception: " + ex.Message);
            return false;
        }
    }

    public static bool EnsureHelperDirAcl(string dir)
    {
        if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
        try
        {
            var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            var adminsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
            var allowed = new List<SecurityIdentifier> { systemSid, adminsSid };

            var sec = new DirectorySecurity();
            sec.SetAccessRuleProtection(true, false);
            sec.AddAccessRule(new FileSystemAccessRule(systemSid,
                FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            sec.AddAccessRule(new FileSystemAccessRule(adminsSid,
                FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            var curUser = WindowsIdentity.GetCurrent().User;
            if (curUser != null && !curUser.IsWellKnown(WellKnownSidType.LocalSystemSid))
            {
                sec.AddAccessRule(new FileSystemAccessRule(curUser,
                    FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
                allowed.Add(curUser);
            }
            new DirectoryInfo(dir).SetAccessControl(sec);
            return ValidateDirectoryAcl(dir, allowed, true);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("EnsureHelperDirAcl error: " + ex.Message);
            return false;
        }
    }

    public static bool EnsureHelperConfigFileAcl(string file, string operatorSid)
    {
        try
        {
            var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            var adminsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
            var allowed = new List<SecurityIdentifier> { systemSid, adminsSid };

            var fsec = new FileSecurity();
            fsec.SetAccessRuleProtection(true, false);
            fsec.AddAccessRule(new FileSystemAccessRule(systemSid,
                FileSystemRights.FullControl, AccessControlType.Allow));
            fsec.AddAccessRule(new FileSystemAccessRule(adminsSid,
                FileSystemRights.FullControl, AccessControlType.Allow));
            var curUser = WindowsIdentity.GetCurrent().User;
            if (curUser != null && !curUser.IsWellKnown(WellKnownSidType.LocalSystemSid))
            {
                fsec.AddAccessRule(new FileSystemAccessRule(curUser,
                    FileSystemRights.FullControl, AccessControlType.Allow));
                allowed.Add(curUser);
            }
            if (!string.IsNullOrEmpty(operatorSid) && operatorSid.StartsWith("S-1-5-"))
            {
                try
                {
                    var opSid = new SecurityIdentifier(operatorSid);
                    fsec.AddAccessRule(new FileSystemAccessRule(opSid,
                        FileSystemRights.ReadAndExecute, AccessControlType.Allow));
                    allowed.Add(opSid);
                }
                catch { }
            }
            new FileInfo(file).SetAccessControl(fsec);
            return ValidateFileAcl(file, allowed, true);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("EnsureHelperConfigFileAcl error: " + ex.Message);
            return false;
        }
    }

    public static bool SaveAuthorizedOperatorSid(string operatorSid)
    {
        if (string.IsNullOrEmpty(operatorSid) || !operatorSid.StartsWith("S-1-5-")) return false;
        try { new SecurityIdentifier(operatorSid); }
        catch { return false; }

        string tmp = null;
        try
        {
            string dir = HelperDir();
            if (!EnsureHelperDirAcl(dir)) return false;
            string file = HelperConfigFile();
            tmp = file + ".staged." + Guid.NewGuid().ToString("N");

            var d = Obj();
            Set(d, "schema", "sai.accounts.helper/config/1");
            Set(d, "schema_version", 1);
            Set(d, "authorized_operator_sid", operatorSid);
            Set(d, "updated_at", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));
            string jsonStr = Json.Serialize(d);
            File.WriteAllText(tmp, jsonStr, Encoding.UTF8);

            if (!EnsureHelperConfigFileAcl(tmp, operatorSid))
            {
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
                return false;
            }

            if (File.Exists(file))
            {
                File.Replace(tmp, file, null);
            }
            else
            {
                File.Move(tmp, file);
            }

            if (!EnsureHelperConfigFileAcl(file, operatorSid))
            {
                return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            try { if (tmp != null && File.Exists(tmp)) File.Delete(tmp); } catch { }
            Console.Error.WriteLine("SaveAuthorizedOperatorSid error: " + ex.Message);
            return false;
        }
    }

    public static string LoadAuthorizedOperatorSid()
    {
        try
        {
            string file = HelperConfigFile();
            if (!File.Exists(file)) return null;
            string raw = File.ReadAllText(file, Encoding.UTF8);
            var d = Json.DeserializeObject(raw) as Dictionary<string, object>;
            if (d == null) return null;
            string sid = S(d, "authorized_operator_sid");
            if (!string.IsNullOrEmpty(sid) && sid.Trim().StartsWith("S-1-5-"))
            {
                return sid.Trim();
            }
            return null;
        }
        catch
        {
            return null;
        }
    }

    public static bool EnsureVaultAcl(string dir)
    {
        if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
        try
        {
            var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            var adminsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
            var allowed = new List<SecurityIdentifier> { systemSid, adminsSid };

            var sec = new DirectorySecurity();
            sec.SetAccessRuleProtection(true, false); // disable inheritance, remove inherited rules
            sec.AddAccessRule(new FileSystemAccessRule(systemSid,
                FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            sec.AddAccessRule(new FileSystemAccessRule(adminsSid,
                FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            var curUser = WindowsIdentity.GetCurrent().User;
            if (curUser != null && !curUser.IsWellKnown(WellKnownSidType.LocalSystemSid))
            {
                sec.AddAccessRule(new FileSystemAccessRule(curUser,
                    FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
                allowed.Add(curUser);
            }
            new DirectoryInfo(dir).SetAccessControl(sec);
            return ValidateDirectoryAcl(dir, allowed, true);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("EnsureVaultAcl error: " + ex.Message);
            return false;
        }
    }

    public static bool EnsureSealedFileAcl(string file)
    {
        try
        {
            var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            var adminsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
            var allowed = new List<SecurityIdentifier> { systemSid, adminsSid };

            var fsec = new FileSecurity();
            fsec.SetAccessRuleProtection(true, false);
            fsec.AddAccessRule(new FileSystemAccessRule(systemSid,
                FileSystemRights.FullControl, AccessControlType.Allow));
            fsec.AddAccessRule(new FileSystemAccessRule(adminsSid,
                FileSystemRights.FullControl, AccessControlType.Allow));
            var curUser = WindowsIdentity.GetCurrent().User;
            if (curUser != null && !curUser.IsWellKnown(WellKnownSidType.LocalSystemSid))
            {
                fsec.AddAccessRule(new FileSystemAccessRule(curUser,
                    FileSystemRights.FullControl, AccessControlType.Allow));
                allowed.Add(curUser);
            }
            new FileInfo(file).SetAccessControl(fsec);
            return ValidateFileAcl(file, allowed, true);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("EnsureSealedFileAcl error: " + ex.Message);
            return false;
        }
    }

    private static string SealedFilePath(string targetSid)
    {
        return Path.Combine(VaultDir(), Fingerprint(targetSid) + ".seal");
    }

    // W2-004: staged secret + explicit promotion. The stage step is the
    // proof half (encrypted bytes on disk, restrictive ACL applied, blob
    // decrypted back to the exact plaintext); promotion is the publication
    // half and its failure is reported, never swallowed. SaveSealedSecret is
    // the composition of both, so every existing caller keeps its contract.
    static string StagedSealPath(string targetSid)
    {
        return Path.Combine(VaultDir(), ".pending." + Fingerprint(targetSid) + "." + Guid.NewGuid().ToString("N") + ".seal");
    }

    public static bool StageSealedSecret(string targetSid, string password, out string stagedPath)
    {
        stagedPath = null;
        if (string.IsNullOrEmpty(targetSid) || !targetSid.StartsWith("S-1-5-") || password == null) return false;
        try { new SecurityIdentifier(targetSid); }
        catch { return false; }

        string staged = null;
        try
        {
            if (!EnsureVaultAcl(VaultDir())) return false;
            byte[] plain = Encoding.UTF8.GetBytes(password);
            byte[] encrypted;
            try
            {
                encrypted = ProtectedData.Protect(plain, EntropyFor(targetSid), DataProtectionScope.LocalMachine);
            }
            finally
            {
                Array.Clear(plain, 0, plain.Length);
            }

            staged = StagedSealPath(targetSid);
            File.WriteAllBytes(staged, encrypted);

            if (!EnsureSealedFileAcl(staged))
            {
                try { if (File.Exists(staged)) File.Delete(staged); } catch { }
                return false;
            }

            // Verify readability and unprotect from staged file before replacing!
            byte[] verifyEncrypted = File.ReadAllBytes(staged);
            byte[] verifyPlain = ProtectedData.Unprotect(verifyEncrypted, EntropyFor(targetSid), DataProtectionScope.LocalMachine);
            string verifyPass = Encoding.UTF8.GetString(verifyPlain);
            Array.Clear(verifyPlain, 0, verifyPlain.Length);
            if (verifyPass != password)
            {
                try { if (File.Exists(staged)) File.Delete(staged); } catch { }
                return false;
            }

            stagedPath = staged;
            return true;
        }
        catch (Exception ex)
        {
            try { if (staged != null && File.Exists(staged)) File.Delete(staged); } catch { }
            Console.Error.WriteLine("StageSealedSecret error: " + ex.Message);
            return false;
        }
    }

    // Publish a staged blob at the canonical path. Re-verified immediately
    // before the swap: what gets published is proven decryptable bytes, not
    // bytes that were checkable some calls ago. On failure the staged file is
    // deliberately left in place -- it is the only copy of the secret that
    // matches a password that may already have been applied.
    public static bool PromoteStagedSealedSecret(string stagedPath, string targetSid, out string error)
    {
        error = null;
        if (string.IsNullOrEmpty(stagedPath) || !File.Exists(stagedPath)) { error = "staged_secret_missing"; return false; }
        string file;
        try { file = SealedFilePath(targetSid); }
        catch (Exception ex) { error = ex.Message; return false; }
        try
        {
            byte[] enc = File.ReadAllBytes(stagedPath);
            byte[] plain = ProtectedData.Unprotect(enc, EntropyFor(targetSid), DataProtectionScope.LocalMachine);
            Array.Clear(plain, 0, plain.Length);

            if (File.Exists(file)) File.Replace(stagedPath, file, null);
            else File.Move(stagedPath, file);

            if (!EnsureSealedFileAcl(file)) { error = "acl_verify_failed_after_publish"; return false; }
            if (ReadSealedSecret(targetSid) == null) { error = "published_secret_unreadable"; return false; }
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    public static bool SaveSealedSecret(string targetSid, string password)
    {
        string staged; string error;
        if (!StageSealedSecret(targetSid, password, out staged)) return false;
        return PromoteStagedSealedSecret(staged, targetSid, out error);
    }

    // The staged blob is ours: the helper holds the machine entropy, so a
    // resumed operation can recover the plaintext it staged earlier instead of
    // generating a second password the account will never learn.
    static string ReadStagedSealedSecret(string stagedPath, string targetSid)
    {
        try
        {
            if (string.IsNullOrEmpty(stagedPath) || !File.Exists(stagedPath)) return null;
            byte[] enc = File.ReadAllBytes(stagedPath);
            byte[] plain = ProtectedData.Unprotect(enc, EntropyFor(targetSid), DataProtectionScope.LocalMachine);
            string s = Encoding.UTF8.GetString(plain);
            Array.Clear(plain, 0, plain.Length);
            return s;
        }
        catch { return null; }
    }

    // --------------------------------------------------- context lifecycle ops
    // W2-004: a managed-context lifecycle is a transaction keyed by operation
    // id, not a best-effort sequence of Windows calls with independent failure
    // modes. This ledger is the durable half: it names the username before the
    // user exists and the staged secret before any password changes, so a retry
    // with the same operation id resumes the same context and never spends
    // another SAIAG_* account. Writes go through a unique staging name plus
    // replace, so a crash leaves the previous ledger, never a torn one.
    static readonly object ContextOpsGate = new object();
    static string ContextOpsDir() { return Path.Combine(RootDir(), "ops"); }
    static string ContextOpsFile() { return Path.Combine(ContextOpsDir(), "context-ops.json"); }
    static string OpStamp() { return DateTime.UtcNow.ToString("o"); }

    static List<Dictionary<string, object>> LoadContextOps()
    {
        lock (ContextOpsGate)
        {
            try
            {
                string file = ContextOpsFile();
                if (!File.Exists(file)) return new List<Dictionary<string, object>>();
                var doc = Json.DeserializeObject(File.ReadAllText(file)) as Dictionary<string, object>;
                var list = new List<Dictionary<string, object>>();
                object[] ops = doc != null ? Get(doc, "ops") as object[] : null;
                foreach (object o in ops ?? new object[0])
                {
                    var d = o as Dictionary<string, object>;
                    if (d != null) list.Add(d);
                }
                return list;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("LoadContextOps error: " + ex.Message);
                return new List<Dictionary<string, object>>();
            }
        }
    }

    static void SaveContextOpsLedger(List<Dictionary<string, object>> ops)
    {
        lock (ContextOpsGate)
        {
            string tmp = null;
            try
            {
                Directory.CreateDirectory(ContextOpsDir());
                var doc = Obj();
                Set(doc, "schema", "sai.accounts/context-ops/1");
                Set(doc, "updated_at", OpStamp());
                Set(doc, "ops", ops.ToArray());
                string file = ContextOpsFile();
                tmp = file + "." + Guid.NewGuid().ToString("N") + ".tmp";
                File.WriteAllText(tmp, Json.Serialize(doc), new UTF8Encoding(true));
                if (File.Exists(file)) File.Replace(tmp, file, null);
                else File.Move(tmp, file);
            }
            catch (Exception ex)
            {
                try { if (tmp != null && File.Exists(tmp)) File.Delete(tmp); } catch { }
                Console.Error.WriteLine("SaveContextOpsLedger error: " + ex.Message);
            }
        }
    }

    static Dictionary<string, object> FindContextOp(List<Dictionary<string, object>> ops, string opId)
    {
        if (string.IsNullOrEmpty(opId)) return null;
        foreach (var o in ops)
            if (string.Equals(S(o, "operation_id"), opId, StringComparison.Ordinal)) return o;
        return null;
    }

    // One state transition, one durable write. Null fields are left as recorded
    // before, so a later transition never erases an earlier fact (the username
    // or staged path) that a resume depends on.
    static Dictionary<string, object> UpsertContextOp(string opId, string state, string username, string sid, string stagedSeal, string detail)
    {
        if (string.IsNullOrEmpty(opId)) return null;
        var ops = LoadContextOps();
        var rec = FindContextOp(ops, opId);
        if (rec == null)
        {
            rec = new Dictionary<string, object>();
            Set(rec, "operation_id", opId);
            Set(rec, "created_at", OpStamp());
            ops.Add(rec);
        }
        Set(rec, "state", state);
        if (!string.IsNullOrEmpty(username)) Set(rec, "username", username);
        if (!string.IsNullOrEmpty(sid)) Set(rec, "windows_sid", sid);
        if (stagedSeal != null) Set(rec, "staged_seal", stagedSeal);
        if (detail != null) Set(rec, "detail", detail);
        Set(rec, "updated_at", OpStamp());
        SaveContextOpsLedger(ops);
        return rec;
    }

    public static string ReadSealedSecret(string targetSid)
    {
        try
        {
            string file = SealedFilePath(targetSid);
            if (!File.Exists(file)) return null;
            byte[] encrypted = File.ReadAllBytes(file);
            byte[] plain = ProtectedData.Unprotect(encrypted, EntropyFor(targetSid), DataProtectionScope.LocalMachine);
            string s = Encoding.UTF8.GetString(plain);
            Array.Clear(plain, 0, plain.Length);
            return s;
        }
        catch
        {
            return null; // corrupt or access denied
        }
    }

    public static bool DeleteSealedSecret(string targetSid)
    {
        try
        {
            string file = SealedFilePath(targetSid);
            if (File.Exists(file)) File.Delete(file);
            return true;
        }
        catch { return false; }
    }

    public static bool HasSealedSecret(string targetSid)
    {
        return File.Exists(SealedFilePath(targetSid));
    }

    public static string GenerateStrongPassword()
    {
        // 36 characters with mix of upper, lower, digits, symbols
        const string upper = "ABCDEFGHJKLMNPQRSTUVWXYZ";
        const string lower = "abcdefghjkmnpqrstuvwxyz";
        const string digits = "23456789";
        const string symbols = "!@#$%^&*()_+-=";
        string all = upper + lower + digits + symbols;
        var bytes = new byte[36];
        using (var rng = new RNGCryptoServiceProvider())
        {
            rng.GetBytes(bytes);
        }
        var sb = new StringBuilder(36);
        sb.Append(upper[bytes[0] % upper.Length]);
        sb.Append(lower[bytes[1] % lower.Length]);
        sb.Append(digits[bytes[2] % digits.Length]);
        sb.Append(symbols[bytes[3] % symbols.Length]);
        for (int i = 4; i < 36; i++)
        {
            sb.Append(all[bytes[i] % all.Length]);
        }
        return sb.ToString();
    }

    // ------------------------------------------------------------- account operations
    private static bool IsTestMode()
    {
        return Environment.GetEnvironmentVariable("SAI_HELPER_TEST_MODE") == "1";
    }

    private static string ResolveUserSid(string username)
    {
        if (IsTestMode())
            return "S-1-5-21-999999999-888888888-777777777-" + Math.Abs(username.GetHashCode() % 9000 + 1000);
        return ((SecurityIdentifier)new NTAccount(username).Translate(typeof(SecurityIdentifier))).Value;
    }

    public static bool CreateLocalUser(string username, string password, out string sid, out string error)
    {
        sid = null; error = null;
        if (IsTestMode())
        {
            // Simulated user for hermetic testing
            sid = ResolveUserSid(username);
            return true;
        }

        try
        {
            var ui = new USER_INFO_1
            {
                usri1_name = username,
                usri1_password = password,
                usri1_priv = USER_PRIV_USER,
                usri1_flags = UF_SCRIPT | UF_DONT_EXPIRE_PASSWORD | UF_NORMAL_ACCOUNT,
                usri1_comment = "SAI Managed Account Context"
            };
            uint parmErr;
            int ret = NetUserAdd(null, 1, ref ui, out parmErr);
            if (ret != 0 && ret != 2224) // 2224 = NERR_UserExists
            {
                error = "NetUserAdd failed with code " + ret;
                return false;
            }

            var groupMember = new LOCALGROUP_MEMBERS_INFO_3 { lgrmi3_domainandname = username };
            NetLocalGroupAddMembers(null, "Users", 3, ref groupMember, 1);

            var account = new NTAccount(username);
            var si = (SecurityIdentifier)account.Translate(typeof(SecurityIdentifier));
            sid = si.Value;
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    public static bool UpdateUserPassword(string username, string newPassword, out string error)
    {
        error = null;
        if (IsTestMode()) return true;
        try
        {
            var ui = new USER_INFO_1003 { usri1003_password = newPassword };
            uint parmErr;
            int ret = NetUserSetInfo(null, username, 1003, ref ui, out parmErr);
            if (ret != 0)
            {
                error = "NetUserSetInfo failed with code " + ret;
                return false;
            }
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    private static readonly HashSet<string> _deletedTestUsers = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    public static bool DeleteLocalUser(string username, out string error)
    {
        error = null;
        if (IsTestMode())
        {
            if (username != null) _deletedTestUsers.Add(username);
            return true;
        }
        try
        {
            int ret = NetUserDel(null, username);
            if (ret != 0 && ret != 2221) // 2221 = NERR_UserNotFound
            {
                error = "NetUserDel failed with code " + ret;
                return false;
            }
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    public static string TranslateSidToUser(string sid)
    {
        if (IsTestMode())
        {
            if (sid != null && _deletedTestUsers.Contains(sid)) return null;
            if (_deletedTestUsers.Contains("SAIAG_testuser")) return null;
            if (sid != null && (sid.EndsWith("-0000") || sid.EndsWith("-nonsaiag"))) return "OperatorUser";
            return "SAIAG_testuser";
        }
        try
        {
            var s = new SecurityIdentifier(sid);
            var account = (NTAccount)s.Translate(typeof(NTAccount));
            string name = account.Value;
            int slash = name.LastIndexOf('\\');
            if (slash >= 0) name = name.Substring(slash + 1);
            return name;
        }
        catch { return null; }
    }

    public static bool CheckUserExists(string sid)
    {
        if (IsTestMode())
        {
            if (sid != null && _deletedTestUsers.Contains(sid)) return false;
            if (_deletedTestUsers.Contains("SAIAG_testuser")) return false;
            return true;
        }
        return TranslateSidToUser(sid) != null;
    }

    // ------------------------------------------------------------- process launcher
    static void LogHelper(string msg)
    {
        try
        {
            string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "SAI", "Accounts", "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "helper.log"), DateTime.UtcNow.ToString("o") + " " + msg + "\r\n");
        }
        catch { }
    }

    public static bool LaunchBrokerAsUser(string targetSid, string username, string password, string scopeId, string operatorSid, out int pid, out string error)
    {
        pid = 0; error = null;
        string brokerExe = BrokerExePath();
        if (!File.Exists(brokerExe))
        {
            error = "broker executable not found: " + brokerExe;
            LogHelper(error);
            return false;
        }

        if (IsTestMode())
        {
            // In test mode, spawn the broker directly with current environment or mock
            try
            {
                string args = "--broker " + targetSid + " " + scopeId + " " + operatorSid;
                var psi = new ProcessStartInfo(brokerExe, args)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                psi.EnvironmentVariables["SAI_HELPER_TEST_MODE"] = "1";
                string root = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_ROOT");
                if (!string.IsNullOrEmpty(root)) psi.EnvironmentVariables["SAI_ACCOUNTS_ROOT"] = root;
                string pipe = Environment.GetEnvironmentVariable("SAI_HELPER_PIPE");
                if (!string.IsNullOrEmpty(pipe)) psi.EnvironmentVariables["SAI_HELPER_PIPE"] = pipe;
                var p = Process.Start(psi);
                pid = p != null ? p.Id : 1234;
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

try
        {
            LogHelper("LaunchBrokerAsUser for user=" + username + " targetSid=" + targetSid + " scope=" + scopeId + " op=" + operatorSid);

            int sessionId;
            string sessionError;
            if (!TryFindInteractiveSession(out sessionId, out sessionError))
            {
                error = sessionError;
                LogHelper(error);
                return false;
            }
            LogHelper("operator interactive session=" + sessionId);

            // One-shot handoff channel. The name is random per launch so nothing can
            // pre-open it, and only SYSTEM (writer) and the operator (reader) may
            // touch it: ACL is SYSTEM + the authorized operator SID, nothing else.
            string pipeName = "sai-spawn-" + Guid.NewGuid().ToString("N").Substring(0, 16);

            var env = new Dictionary<string, object>();
            env["broker"] = brokerExe;
            env["user"] = username;
            env["password"] = password;
            env["target_sid"] = targetSid;
            env["scope"] = scopeId;
            env["operator_sid"] = operatorSid;

            // The launcher is this same binary, re-entered with a different first argument.
            // No second artifact to keep in step with the service during a deploy.
            string selfPath = Assembly.GetExecutingAssembly().Location;
            string cmdLine = "\"" + selfPath + "\" --in-session-spawn --pipe \"" + pipeName + "\"";

            IntPtr opTok = IntPtr.Zero, opDup = IntPtr.Zero, envBlock = IntPtr.Zero;
            try
            {
                if (!WTSQueryUserToken((uint)sessionId, out opTok) || opTok == IntPtr.Zero)
                {
                    error = "WTSQueryUserToken(session " + sessionId + ") failed with error " + Marshal.GetLastWin32Error();
                    LogHelper(error);
                    return false;
                }
                if (!DuplicateTokenEx(opTok, TOKEN_ALL_ACCESS, IntPtr.Zero, SecurityImpersonation, TokenImpersonation, out opDup))
                {
                    error = "DuplicateTokenEx(operator) failed with error " + Marshal.GetLastWin32Error();
                    LogHelper(error);
                    return false;
                }
                CloseHandle(opTok); opTok = IntPtr.Zero;

                // A Unicode environment block REQUIRES CREATE_UNICODE_ENVIRONMENT;
                // without the flag the call fails with ERROR_INVALID_PARAMETER.
                CreateEnvironmentBlock(out envBlock, opDup, false);

                var si = new STARTUPINFO();
                si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                si.dwFlags = STARTF_USESHOWWINDOW;
                si.wShowWindow = 0;
                // lpDesktop stays null: the launcher's own station is the caller's
                // (session 0 / non-interactive) and naming winsta0\default here is
                // fatal -- the child dies 0xC0000142 before it can initialize.
                si.lpDesktop = null;

                var pi = new PROCESS_INFORMATION();
                bool started = CreateProcessAsUser(opDup, null, cmdLine, IntPtr.Zero, IntPtr.Zero, false,
                    CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, envBlock, null, ref si, out pi);
                if (!started)
                {
                    error = "in-session launcher failed with error " + Marshal.GetLastWin32Error();
                    LogHelper(error);
                    return false;
                }
                LogHelper("in-session launcher pid=" + pi.dwProcessId);

                string reply = SpawnRoundTrip(pipeName, env, operatorSid);
                if (string.IsNullOrEmpty(reply))
                {
                    error = "in-session launcher did not report a spawn result";
                    LogHelper(error);
                    return false;
                }

                var result = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reply);
                object okVal, pidVal, errVal;
                result.TryGetValue("ok", out okVal);
                result.TryGetValue("pid", out pidVal);
                result.TryGetValue("error", out errVal);
                bool ok = okVal != null && Convert.ToBoolean(okVal);
                if (ok)
                {
                    pid = pidVal != null ? Convert.ToInt32(pidVal) : 0;
                    LogHelper("broker spawned pid=" + pid + " in session " + sessionId + " as " + username);
                }
                else
                {
                    error = errVal != null ? Convert.ToString(errVal) : "in-session launcher reported a spawn failure";
                    LogHelper(error);
                }

                if (pi.hProcess != IntPtr.Zero) CloseHandle(pi.hProcess);
                if (pi.hThread != IntPtr.Zero) CloseHandle(pi.hThread);
                return ok;
            }
            finally
            {
                WipeSecret(env);
                if (opTok != IntPtr.Zero) CloseHandle(opTok);
                if (opDup != IntPtr.Zero) CloseHandle(opDup);
                if (envBlock != IntPtr.Zero) DestroyEnvironmentBlock(envBlock);
            }
        }
        catch (Exception ex)
        {
            error = ex.Message;
            LogHelper("LaunchBrokerAsUser exception: " + ex.ToString());
            return false;
        }
    }

    // ------------------------------------------------------- session bridge
    // Which session is the human sitting in? WTSGetActiveConsoleSessionId is not
    // usable: on this host it returns 0, which is the service session. Enumerate
    // instead and take the highest non-zero session that is currently active, so a
    // disconnected RDP session does not win over the console the operator can see.
    private static bool TryFindInteractiveSession(out int sessionId, out string error)
    {
        sessionId = 0; error = null;
        IntPtr buf; int count;
        if (!WTSEnumerateSessionsW(IntPtr.Zero, 0, 1, out buf, out count))
        {
            error = "WTSEnumerateSessionsW failed with error " + Marshal.GetLastWin32Error();
            return false;
        }
        try
        {
            int size = Marshal.SizeOf(typeof(WTS_SESSION_INFO));
            for (int i = 0; i < count; i++)
            {
                var info = (WTS_SESSION_INFO)Marshal.PtrToStructure(IntPtr.Add(buf, i * size), typeof(WTS_SESSION_INFO));
                int id = info.SessionId.ToInt32();
                if (id != 0 && info.State == 0 && id > sessionId) sessionId = id; // 0 == WTSActive
            }
        }
        finally { WTSFreeMemory(buf); }

        if (sessionId == 0)
        {
            error = "no interactive session found: log on to the desktop and retry";
            return false;
        }
        return true;
    }

    private static void WipeSecret(Dictionary<string, object> env)
    {
        if (env == null) return;
        object v;
        if (env.TryGetValue("password", out v))
        {
            string s = v as string;
            if (!string.IsNullOrEmpty(s))
            {
                byte[] bytes = Encoding.Unicode.GetBytes(s);
                Array.Clear(bytes, 0, bytes.Length);
            }
            env["password"] = "";
        }
    }

    // Hand the credential to the session-1 launcher over a pipe that only the
    // authorized operator may open, take its one-line answer, and drop both ends.
    // Nothing is written to disk and nothing is echoed into the helper log.
    private static string SpawnRoundTrip(string pipeName, Dictionary<string, object> env, string operatorSid)
    {
        var acl = new PipeSecurity();
        try
        {
            var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            acl.SetAccessRuleProtection(true, false);
            acl.AddAccessRule(new PipeAccessRule(systemSid, PipeAccessRights.FullControl, AccessControlType.Allow));
            if (!string.IsNullOrEmpty(operatorSid) && !string.Equals(operatorSid, "S-1-5-18", StringComparison.OrdinalIgnoreCase))
                acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(operatorSid), PipeAccessRights.ReadWrite, AccessControlType.Allow));
        }
        catch (Exception ex) { LogHelper("spawn pipe ACL fell back to default: " + ex.Message); }

        for (int attempt = 0; attempt < 60; attempt++)
        {
            try
            {
                // PipeOptions.None, not Asynchronous: the launcher opens this with the
                // plain synchronous NamedPipeClientStream, and mixing an
                // asynchronous server with a synchronous client on one channel
                // leaves the payload sitting unsignalled -- the client blocks in
                // ReadLine until the server side is torn down. This channel is one
                // message each way, so there is nothing to overlap.
                using (var server = new NamedPipeServerStream(pipeName, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.None, 65536, 65536, acl))
                {
                    server.WaitForConnection();
                    string payload = new JavaScriptSerializer().Serialize(env);
                    // The terminator is not decoration. The peer reads a LINE; a
                    // payload with no '\n' never completes its ReadLine, so it
                    // blocks until the pipe is torn down at the reply timeout and
                    // only then reports the whole payload as if it had just
                    // arrived. The broker still starts; the reply never does.
                    byte[] payloadBytes = Encoding.UTF8.GetBytes(payload + "\n");
                    server.Write(payloadBytes, 0, payloadBytes.Length);
                    server.Flush();
                    // Deliberately NOT zeroing payloadBytes here. NamedPipeServerStream
                    // hands the buffer to overlapped I/O, so clearing it right after
                    // the write can zero bytes that are still in flight -- the peer
                    // then reads NULs with no newline and blocks in ReadLine forever.
                    // A serialized string is immutable anyway; the only durable copy
                    // is the sealed vault, and that is never opened into this frame
                    // more than once per spawn.

                    var reader = new StreamReader(server, Encoding.UTF8);
                    // Bounded: a launcher that dies mid-handshake must not park the
                    // helper's request thread forever.
                    var pending = reader.ReadLineAsync();
                    if (!pending.Wait(60000)) { LogHelper("spawn handoff reply timed out"); return null; }
                    // A peer that closes its end without answering reads back as
                    // EOF, not as an error. Say so, or the next occurrence is
                    // indistinguishable from a launcher that never started.
                    if (string.IsNullOrEmpty(pending.Result))
                    {
                        LogHelper("spawn handoff channel closed with no reply (peer exited before answering)");
                        return null;
                    }
                    return pending.Result;
                }
            }
            catch (System.TimeoutException) { return null; }
            catch (Exception ex)
            {
                LogHelper("SpawnRoundTrip attempt " + attempt + ": " + ex.Message);
                Thread.Sleep(250);
            }
        }
        return null;
    }

    // Runs INSIDE the operator's interactive session, as the operator. This is the
    // only place a password logon is performed, because a logon performed from
    // session 0 yields a logon session the child cannot use (ERROR_NO_SUCH_LOGON_SESSION).
    // The broker still runs under the managed account identity -- this process is only
    // the logon vehicle, it exits as soon as the broker is up.
    // Is a broker for this context answering right now? A clean early exit from a
    // freshly spawned broker is what the single-instance mutex looks like, so the
    // only way to tell "another broker already owns this context" from "this one
    // died" is to ask the context itself.
    static bool BrokerAlreadyServing(string brokerPipe)
    {
        try
        {
            using (var c = new NamedPipeClientStream(".", brokerPipe, PipeDirection.InOut,
                PipeOptions.None, TokenImpersonationLevel.Identification))
            {
                c.Connect(2000);
                using (var w = new StreamWriter(c, new UTF8Encoding(false), 1024, true) { AutoFlush = true })
                using (var r = new StreamReader(c, Encoding.UTF8, false, 1024, true))
                {
                    w.WriteLine("{\"cmd\":\"status\"}");
                    var pending = r.ReadLineAsync();
                    return pending.Wait(3000) && !string.IsNullOrEmpty(pending.Result);
                }
            }
        }
        catch { return false; }
    }

    public static int RunInSessionSpawn(string pipeName)
    {
        string reply = "{\"ok\":false,\"error\":\"in_session_spawn_failed: not attempted\"}";
        int pid = 0;
        using (var handoff = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut))
        {
            try
            {
                handoff.Connect(15000);
                LogHelper("in-session launcher connected as " + WindowsIdentity.GetCurrent().Name
                    + " session=" + System.Diagnostics.Process.GetCurrentProcess().SessionId);
                string line;
                // leaveOpen on both ends. A StreamReader/StreamWriter closes the
                // stream it wraps unless told not to, and this channel carries a
                // request in one direction and the answer back in the other.
                using (var reader = new StreamReader(handoff, Encoding.UTF8, false, 1024, true))
                    line = reader.ReadLine();
                LogHelper("in-session launcher received handoff (" + (line == null ? "nothing" : line.Length + " bytes") + ")");

                if (string.IsNullOrEmpty(line)) reply = "{\"ok\":false,\"error\":\"spawn handoff channel delivered nothing\"}";
                else
                {
                    var env = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(line);
                    string brokerExe = Convert.ToString(env["broker"]);
                    string username = Convert.ToString(env["user"]);
                    string password = Convert.ToString(env["password"]);
                    string targetSid = Convert.ToString(env["target_sid"]);
                    string scopeId = Convert.ToString(env["scope"]);
                    string operatorSid = Convert.ToString(env["operator_sid"]);

                    var si = new STARTUPINFO();
                    si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                    si.lpDesktop = null; // inherit winsta0\default from this process
                    var pi = new PROCESS_INFORMATION();
                    string cmdLine = "\"" + brokerExe + "\" --broker " + targetSid + " " + scopeId + " " + operatorSid;

                    LogHelper("in-session launcher calling CreateProcessWithLogonW for " + username);
                    if (!CreateProcessWithLogonW(username, ".", password, LOGON_WITH_PROFILE, null, cmdLine,
                            CREATE_NO_WINDOW, IntPtr.Zero, null, ref si, out pi))
                    {
                        reply = "{\"ok\":false,\"error\":\"CreateProcessWithLogonW failed with error " + Marshal.GetLastWin32Error() + "\"}";
                    }
                    else
                    {
                        pid = (int)pi.dwProcessId;
                        LogHelper("in-session launcher spawned broker pid=" + pid);
                        // Spawned is not started. The broker has a fail-closed SID check
                        // that refuses to serve if it is not the managed identity, so an
                        // early exit must be reported as the failure it is -- unless a
                        // broker is already answering. A clean exit 0 inside 500ms is
                        // exactly what the single-instance mutex produces, and calling
                        // that a failed connect makes a working context look broken.
                        WaitForSingleObject(pi.hProcess, 500);
                        uint exitCode;
                        if (GetExitCodeProcess(pi.hProcess, out exitCode) && exitCode != STILL_ACTIVE)
                        {
                            if (BrokerAlreadyServing("SAI.Accounts." + scopeId + "." + Fingerprint(targetSid)))
                            {
                                LogHelper("in-session launcher: broker already serving on " + pipeName);
                                reply = "{\"ok\":true,\"pid\":0,\"note\":\"already_running\"}";
                            }
                            else
                            {
                                pid = 0;
                                reply = "{\"ok\":false,\"error\":\"broker_exited_0x" + exitCode.ToString("X8") + "_within_500ms\"}";
                            }
                        }
                        else
                        {
                            reply = "{\"ok\":true,\"pid\":" + pid + "}";
                        }
                        if (pi.hProcess != IntPtr.Zero) CloseHandle(pi.hProcess);
                        if (pi.hThread != IntPtr.Zero) CloseHandle(pi.hThread);
                    }

                    if (!string.IsNullOrEmpty(password))
                    {
                        byte[] bytes = Encoding.Unicode.GetBytes(password);
                        Array.Clear(bytes, 0, bytes.Length);
                    }
                    WipeSecret(env);
                }

                // The answer goes back down the same connection: one channel, one
                // payload, closed the instant both directions are done.
                using (var writer = new StreamWriter(handoff, new UTF8Encoding(false), 1024, true) { AutoFlush = true })
                    writer.WriteLine(reply);
            }
            catch (Exception ex)
            {
                reply = "{\"ok\":false,\"error\":\"in_session_spawn_failed: " + ex.Message.Replace("\"", "'") + "\"}";
                Console.Error.WriteLine(reply);
            }
        }
        return pid != 0 ? 0 : 1;
    }

    // ------------------------------------------------------------- IPC Server
    public static void RunServer(string pipeNameOverride, Func<bool> shouldStop)
    {
        string pipe = pipeNameOverride ?? PipeName();
        _stopRequested = false;

        string currentSid = WindowsIdentity.GetCurrent().User != null ? WindowsIdentity.GetCurrent().User.Value : "S-1-5-18";
        bool isLocalSystem = string.Equals(currentSid, "S-1-5-18", StringComparison.OrdinalIgnoreCase) ||
            (IsTestMode() && Environment.GetEnvironmentVariable("SAI_SIMULATE_SYSTEM_SERVICE") == "1");

        string persistedOperatorSid = LoadAuthorizedOperatorSid();
        string envOperatorSid = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_OPERATOR_SID");
        string operatorSid = persistedOperatorSid ?? envOperatorSid;

        if (isLocalSystem)
        {
            if (string.IsNullOrEmpty(operatorSid) || !operatorSid.StartsWith("S-1-5-"))
            {
                Console.Error.WriteLine("FATAL: SaiAccountsHelper running as LocalSystem without authorized operator SID in helper config. Failing closed.");
                return;
            }
        }
        else
        {
            if (string.IsNullOrEmpty(operatorSid)) operatorSid = currentSid;
        }

        var sec = new PipeSecurity();
        // SYSTEM: FullControl
        sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
            PipeAccessRights.FullControl, AccessControlType.Allow));
        // Builtin Administrators: FullControl
        sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
            PipeAccessRights.FullControl, AccessControlType.Allow));
        // Authorized Operator SID: ReadWrite
        if (!string.IsNullOrEmpty(operatorSid) && !string.Equals(operatorSid, "S-1-5-18", StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(operatorSid),
                    PipeAccessRights.ReadWrite, AccessControlType.Allow));
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("Warning: failed to add pipe ACL for operator SID " + operatorSid + ": " + ex.Message);
            }
        }
        // Current user (if not SYSTEM): FullControl
        if (!string.Equals(currentSid, "S-1-5-18", StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(currentSid),
                    PipeAccessRights.FullControl, AccessControlType.Allow));
            }
            catch { }
        }

        var allowedSids = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "S-1-5-18" // LocalSystem
        };
        if (!string.IsNullOrEmpty(currentSid)) allowedSids.Add(currentSid);
        if (!string.IsNullOrEmpty(operatorSid)) allowedSids.Add(operatorSid);

        while (!_stopRequested && (shouldStop == null || !shouldStop()))
        {
            try
            {
                using (var server = new NamedPipeServerStream(pipe, PipeDirection.InOut, 10,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 65536, 65536, sec))
                {
                    var ar = server.BeginWaitForConnection(null, null);
                    while (!ar.IsCompleted)
                    {
                        if (_stopRequested || (shouldStop != null && shouldStop())) break;
                        Thread.Sleep(100);
                    }
                    if (_stopRequested || (shouldStop != null && shouldStop())) break;
                    server.EndWaitForConnection(ar);

                    string clientSid = null;
                    bool isAdmin = false;
                    try
                    {
                        if (ImpersonateNamedPipeClient(server.SafePipeHandle.DangerousGetHandle()))
                        {
                            try
                            {
                                using (var client = WindowsIdentity.GetCurrent())
                                {
                                    clientSid = client.User != null ? client.User.Value : null;
                                    var principal = new WindowsPrincipal(client);
                                    isAdmin = principal.IsInRole(WindowsBuiltInRole.Administrator);
                                }
                            }
                            finally { RevertToSelf(); }
                        }
                    }
                    catch { clientSid = null; }

                    using (var reader = new StreamReader(server, Encoding.UTF8))
                    using (var writer = new StreamWriter(server, new UTF8Encoding(false)) { AutoFlush = true })
                    {
                        // Check authorization: must be allowed SID or admin
                        if (clientSid == null || (!allowedSids.Contains(clientSid) && !isAdmin))
                        {
                            var denied = Obj();
                            Set(denied, "status", "ACCESS_DENIED");
                            Set(denied, "client_sid", clientSid ?? "unknown");
                            writer.WriteLine(Json.Serialize(denied));
                            continue;
                        }

                        string line = reader.ReadLine();
                        if (line == null) continue;

                        Dictionary<string, object> req = null;
                        try { req = Json.DeserializeObject(line) as Dictionary<string, object>; } catch { }
                        if (req == null)
                        {
                            var bad = Obj(); Set(bad, "status", "MALFORMED_JSON");
                            writer.WriteLine(Json.Serialize(bad));
                            continue;
                        }

                        // Strict parameter validation: reject arbitrary command or path fields
                        string[] banned = { "exe", "command", "command_line", "program", "args", "path", "binary", "script", "powershell" };
                        string bannedFound = null;
                        foreach (string b in banned)
                        {
                            if (req.ContainsKey(b)) { bannedFound = b; break; }
                        }
                        if (bannedFound != null)
                        {
                            var bad = Obj();
                            Set(bad, "status", "OPERATION_NOT_ALLOWED");
                            Set(bad, "rejected_field", bannedFound);
                            writer.WriteLine(Json.Serialize(bad));
                            continue;
                        }

                        string op = S(req, "op") ?? S(req, "cmd");
                        var resp = HandleRequest(op, req, clientSid);
                        writer.WriteLine(Json.Serialize(resp));
                    }
                }
            }
            catch (Exception)
            {
                if (_stopRequested || (shouldStop != null && shouldStop())) break;
                Thread.Sleep(200);
            }
        }
    }

    private static Dictionary<string, object> HandleRequest(string op, Dictionary<string, object> req, string clientSid)
    {
        var resp = Obj();
        Set(resp, "schema", "sai.accounts.helper/response/1");
        Set(resp, "op", op);

        if (op == "status")
        {
            Set(resp, "status", "online");
            Set(resp, "service_sid", WindowsIdentity.GetCurrent().User != null ? WindowsIdentity.GetCurrent().User.Value : null);
            Set(resp, "authorized_operator_sid", LoadAuthorizedOperatorSid() ?? Environment.GetEnvironmentVariable("SAI_ACCOUNTS_OPERATOR_SID"));
            Set(resp, "client_sid", clientSid);
            return resp;
        }

        if (op == "managed_context_create")
        {
            string provider = S(req, "provider") ?? "antigravity";
            if (!string.Equals(provider, "antigravity", StringComparison.OrdinalIgnoreCase))
            {
                Set(resp, "status", "PROVIDER_UNSUPPORTED");
                return resp;
            }

            bool migrateExisting = object.Equals(Get(req, "migrate_existing"), true);
            bool compensate = object.Equals(Get(req, "compensate_registry_failure"), true);
            string opId = S(req, "operation_id");
            string requestedUser = S(req, "username");
            string error;

            if (migrateExisting)
            {
                if (string.IsNullOrEmpty(requestedUser))
                {
                    Set(resp, "status", "MIGRATION_USER_REQUIRED");
                    return resp;
                }
                // Safety guard: only allow migration of dedicated antigravity users (e.g. antigravity_b, antigravity_*, SAIAG_*)
                if (!requestedUser.StartsWith("antigravity", StringComparison.OrdinalIgnoreCase) &&
                    !requestedUser.StartsWith("SAIAG_", StringComparison.OrdinalIgnoreCase))
                {
                    Set(resp, "status", "MIGRATION_TARGET_REJECTED");
                    Set(resp, "error", "Refusing to migrate non-dedicated user profile '" + requestedUser + "'");
                    return resp;
                }
            }

            var ledger = LoadContextOps();
            var prev = FindContextOp(ledger, opId);

            // The caller's registry write failed after this operation had already
            // produced a context. Cleaning up is what makes the two planes
            // agree again: a user the registry does not know about is a secret
            // and a launch authority nobody owns.
            if (compensate)
            {
                if (prev == null)
                {
                    Set(resp, "status", "UNKNOWN_OPERATION");
                    Set(resp, "error", "no lifecycle record for operation " + opId);
                    return resp;
                }
                string cUser = S(prev, "username");
                string cSid = S(prev, "windows_sid");
                var failed = new List<string>();
                if (!string.IsNullOrEmpty(cSid) && !DeleteSealedSecret(cSid)) failed.Add("delete_sealed_secret");
                if (!string.IsNullOrEmpty(cSid) && HasSealedSecret(cSid)) failed.Add("sealed_secret_still_present");
                if (!string.IsNullOrEmpty(cSid) && cUser != null && cUser.StartsWith("SAIAG_", StringComparison.OrdinalIgnoreCase)
                    && CheckUserExists(cSid))
                {
                    if (!DeleteLocalUser(cUser, out error)) failed.Add("delete_windows_account");
                    else if (CheckUserExists(cSid)) failed.Add("windows_account_still_present");
                }
                if (failed.Count > 0)
                {
                    UpsertContextOp(opId, "cleanup_failed", cUser, cSid, null, string.Join(",", failed.ToArray()));
                    Set(resp, "status", "CLEANUP_FAILED");
                    Set(resp, "failed_steps", failed.ToArray());
                    Set(resp, "error", "compensating cleanup left evidence behind; the context still exists");
                    return resp;
                }
                UpsertContextOp(opId, "compensated", cUser, cSid, null, null);
                Set(resp, "status", "compensated");
                Set(resp, "windows_user", cUser);
                Set(resp, "target_sid", cSid);
                return resp;
            }

            // Replay: the durable half is already done, so the retry resumes
            // that context instead of allocating another account. Only
            // helper-side work repeats, and it repeats knowingly.
            if (prev != null)
            {
                string st = S(prev, "state");
                if (st == "completed" || st == "pending_registration")
                {
                    string psid = S(prev, "windows_sid");
                    Set(resp, "status", "ok");
                    Set(resp, "target_sid", psid);
                    Set(resp, "windows_user", S(prev, "username"));
                    Set(resp, "context_id", "antigravity:windows-user:" + Fingerprint(psid));
                    Set(resp, "idempotent_replay", true);
                    if (st == "pending_registration") Set(resp, "registry_pending", true);
                    if (opId != null) Set(resp, "operation_id", opId);
                    return resp;
                }
            }

            string username;
            if (migrateExisting)
            {
                username = requestedUser;
            }
            else if (prev != null && !string.IsNullOrEmpty(S(prev, "username")))
            {
                username = S(prev, "username");
            }
            else
            {
                string tag = Guid.NewGuid().ToString("N").Substring(0, 8);
                username = "SAIAG_" + tag;
            }

            string sid = prev != null ? S(prev, "windows_sid") : null;
            string stagedSeal = prev != null ? S(prev, "staged_seal") : null;
            bool passwordApplied = prev != null && string.Equals(S(prev, "state"), "password_applied", StringComparison.Ordinal);

            if (migrateExisting)
            {
                // The SID is resolved before the existing password is touched.
                // It used to be translated after the update, so a translate
                // failure left the profile on a password the vault never held
                // -- a secret nobody could use, and nothing the operator could
                // recover from.
                try
                {
                    sid = ResolveUserSid(username);
                }
                catch (Exception ex)
                {
                    Set(resp, "status", "TRANSLATE_FAILED");
                    Set(resp, "error", ex.Message);
                    return resp;
                }
                if (!CheckUserExists(sid))
                {
                    Set(resp, "status", "USER_NOT_FOUND");
                    Set(resp, "error", "dedicated account '" + username + "' does not exist on this host");
                    return resp;
                }
                UpsertContextOp(opId, "target_resolved", username, sid, null, null);
            }
            else if (opId != null)
            {
                // The username is durable before the user exists. This is the
                // line that makes a retry resumable rather than allocative.
                UpsertContextOp(opId, "pending", username, null, null, null);
            }

            // Step 1: the account has to exist and be named before the secret
            // is staged, because the staged blob's entropy is derived from the
            // target SID -- you cannot stage for an identity that does not
            // resolve yet. A brand-new account is therefore created with a
            // throwaway bootstrap password that the real password replaces
            // below, after the staged secret has proven recoverable. The
            // bootstrap is never written to the vault and never returned.
            string stageNote = null;
            if (migrateExisting)
            {
                if (!CheckUserExists(sid))
                {
                    Set(resp, "status", "USER_NOT_FOUND");
                    Set(resp, "error", "dedicated account '" + username + "' does not exist on this host");
                    return resp;
                }
            }
            else if (!string.IsNullOrEmpty(sid) && CheckUserExists(sid))
            {
                // A crash between user creation and the ledger write leaves
                // the real SID on disk. Adopt it; never invent a second one.
                UpsertContextOp(opId, "user_created", username, sid, null, null);
            }
            else
            {
                string bootstrap = GenerateStrongPassword();
                if (!CreateLocalUser(username, bootstrap, out sid, out error))
                {
                    Set(resp, "status", "USER_CREATION_FAILED");
                    Set(resp, "error", error);
                    return resp;
                }
                if (!string.IsNullOrEmpty(stagedSeal)) stageNote = "existing staged seal references a pre-creation SID; restaging for the created account";
                UpsertContextOp(opId, "user_created", username, sid, null, null);
            }

            // Step 2: staged secret for a resolved SID. Recover the plaintext
            // of an already-staged secret when the process that started this
            // operation did not survive to apply it; generate and stage a new
            // one otherwise.
            string password = null;
            if (!string.IsNullOrEmpty(stagedSeal) && File.Exists(stagedSeal))
            {
                password = ReadStagedSealedSecret(stagedSeal, sid);
                if (password == null) stagedSeal = null;
            }
            if (!string.IsNullOrEmpty(stagedSeal) && ReadStagedSealedSecret(stagedSeal, sid) == null)
            {
                // The blob on the recorded path does not decrypt for the real
                // SID. That is corruption, not a condition to paper over.
                Set(resp, "status", "SECRET_STAGED_UNRECOVERABLE");
                Set(resp, "error", "staged secret " + Path.GetFileName(stagedSeal) + " cannot be decrypted for " + sid);
                return resp;
            }

            if (password == null)
            {
                // Staged before anything depends on the secret existing. The
                // vault record is provably decodable before the password is
                // applied, so "no secret" can no longer mean "account we
                // cannot start".
                password = GenerateStrongPassword();
                string staged;
                if (!StageSealedSecret(sid, password, out staged))
                {
                    Set(resp, "status", "SECRET_SEAL_FAILED");
                    Set(resp, "error", "staged secret could not be written, ACL-verified and decryption-verified");
                    return resp;
                }
                stagedSeal = staged;
                UpsertContextOp(opId, "secret_staged", username, sid, stagedSeal, stageNote);
            }

            // Step 3: the password change itself, only now that a staged secret
            // matching it is provably recoverable.
            if (!passwordApplied)
            {
                if (!UpdateUserPassword(username, password, out error))
                {
                    Set(resp, "status", migrateExisting ? "MIGRATION_FAILED" : "PASSWORD_UPDATE_FAILED");
                    Set(resp, "error", error);
                    return resp;
                }
                UpsertContextOp(opId, "password_applied", username, sid, stagedSeal, null);
            }

            // Step 4: publish the staged secret at the canonical path.
            if (!PromoteStagedSealedSecret(stagedSeal, sid, out error))
            {
                // The staged blob stays on disk under its recorded name: the next
                // attempt (or an operator with the entropy) can still adopt a
                // secret that matches the password already in force. Deleting
                // it here is what used to strand the account.
                Set(resp, "status", "SECRET_PUBLISH_FAILED");
                Set(resp, "error", error);
                Set(resp, "staged_secret_retained", Path.GetFileName(stagedSeal));
                Set(resp, "target_sid", sid);
                Set(resp, "windows_user", username);
                if (opId != null) Set(resp, "operation_id", opId);
                return resp;
            }

            // The registry write is the caller's step. The durable record stays
            // in pending_registration until the caller either confirms the
            // account is registered or invokes the same operation id with
            // compensate_registry_failure=true.
            UpsertContextOp(opId, "completed", username, sid, null, null);
            if (!object.Equals(Get(req, "registry_committed"), true))
                UpsertContextOp(opId, "pending_registration", username, sid, null, null);

            Set(resp, "status", "ok");
            Set(resp, "target_sid", sid);
            Set(resp, "windows_user", username);
            Set(resp, "context_id", "antigravity:windows-user:" + Fingerprint(sid));
            if (opId != null) Set(resp, "operation_id", opId);
            return resp;
        }

        if (op == "managed_context_start")
        {
            string targetSid = S(req, "target_sid");
            string scopeId = S(req, "scope_id");
            string operatorSid = S(req, "operator_sid") ?? clientSid;
            if (string.IsNullOrEmpty(operatorSid) || string.Equals(operatorSid, "S-1-5-18", StringComparison.OrdinalIgnoreCase))
            {
                operatorSid = LoadAuthorizedOperatorSid() ?? clientSid;
            }

            if (string.IsNullOrEmpty(targetSid) || !targetSid.StartsWith("S-1-5-"))
            {
                Set(resp, "status", "INVALID_TARGET_SID");
                return resp;
            }

            string password = ReadSealedSecret(targetSid);
            if (password == null)
            {
                Set(resp, "status", HasSealedSecret(targetSid) ? "SECRET_CORRUPT" : "SECRET_NOT_FOUND");
                return resp;
            }

            string username = TranslateSidToUser(targetSid);
            if (string.IsNullOrEmpty(username))
            {
                Set(resp, "status", "USER_NOT_FOUND");
                return resp;
            }

            int pid; string err;
            // Before the logon, not after. The console contract is a default
            // the managed account's own console host reads when it starts, so
            // writing it once the session exists would leave the first console
            // of the session -- the one the operator is about to type into --
            // on stock settings.
            var profile = EnsureManagedConsoleProfile(targetSid, SharedAgyPath(), operatorSid);
            Set(resp, "console_profile", S(profile, "status"));
            Set(resp, "input_languages", S(profile, "input_languages"));
            Set(resp, "console_presentation", S(profile, "console_presentation"));
            if (Get(profile, "console_presentation_error") != null) Set(resp, "console_presentation_error", Get(profile, "console_presentation_error"));
            if (!string.Equals(S(profile, "status"), "applied", StringComparison.Ordinal)
                && !string.Equals(S(profile, "status"), "already_correct", StringComparison.Ordinal))
            {
                Set(resp, "console_profile_error", S(profile, "error"));
                LogHelper("managed console profile for " + targetSid + ": " + S(profile, "status") + " " + (S(profile, "error") ?? ""));
            }

            if (!LaunchBrokerAsUser(targetSid, username, password, scopeId, operatorSid, out pid, out err))
            {
                Set(resp, "status", "LAUNCH_FAILED");
                Set(resp, "error", err);
                return resp;
            }

            // LOGON_WITH_PROFILE has now loaded the managed hive. A first
            // logon may provision it, and an unloaded hive may have refused
            // RegLoadKey before logon. Retry before any visible console starts.
            if (!string.Equals(S(profile, "status"), "applied", StringComparison.Ordinal)
                && !string.Equals(S(profile, "status"), "already_correct", StringComparison.Ordinal))
            {
                profile = EnsureManagedConsoleProfile(targetSid, SharedAgyPath(), operatorSid);
                Set(resp, "console_profile", S(profile, "status"));
                Set(resp, "input_languages", S(profile, "input_languages"));
                Set(resp, "console_presentation", S(profile, "console_presentation"));
                resp.Remove("console_presentation_error");
                if (Get(profile, "console_presentation_error") != null) Set(resp, "console_presentation_error", Get(profile, "console_presentation_error"));
                resp.Remove("console_profile_error");
                if (!string.Equals(S(profile, "status"), "applied", StringComparison.Ordinal)
                    && !string.Equals(S(profile, "status"), "already_correct", StringComparison.Ordinal))
                    Set(resp, "console_profile_error", S(profile, "error"));
            }

            Set(resp, "status", "started");
            Set(resp, "target_sid", targetSid);
            Set(resp, "pid", pid);
            return resp;
        }

        if (op == "managed_console_profile")
        {
            // Deliberately narrow: a SID and nothing else. The console value
            // set is a compile-time constant below, so there is no field a
            // caller could add here that would widen it into a registry API.
            string targetSid = S(req, "target_sid");
            // The operator whose input languages are mirrored is the one this
            // helper is authorised for, or the pipe client itself -- never a
            // SID named in the request.
            var profile = EnsureManagedConsoleProfile(targetSid, SharedAgyPath(), LoadAuthorizedOperatorSid() ?? clientSid);
            foreach (var kv in profile) Set(resp, kv.Key, kv.Value);
            return resp;
        }

        if (op == "managed_context_status")
        {
            string targetSid = S(req, "target_sid");
            if (string.IsNullOrEmpty(targetSid))
            {
                Set(resp, "status", "INVALID_TARGET_SID");
                return resp;
            }

            bool userExists = CheckUserExists(targetSid);
            bool secretExists = HasSealedSecret(targetSid);

            // Check if broker is running
            string scopeId = S(req, "scope_id") ?? "default";
            string pipeName = "SAI.Accounts." + scopeId + "." + Fingerprint(targetSid);
            bool brokerRunning = false;
            try
            {
                using (var test = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut))
                {
                    test.Connect(200);
                    brokerRunning = true;
                }
            }
            catch { }

            Set(resp, "status", "ok");
            Set(resp, "target_sid", targetSid);
            Set(resp, "user_exists", userExists);
            Set(resp, "secret_exists", secretExists);
            Set(resp, "broker_running", brokerRunning);
            return resp;
        }

        if (op == "managed_context_rotate_secret")
        {
            string targetSid = S(req, "target_sid");
            if (string.IsNullOrEmpty(targetSid) || !targetSid.StartsWith("S-1-5-"))
            {
                Set(resp, "status", "INVALID_TARGET_SID");
                return resp;
            }

            string opId = S(req, "operation_id");
            string username = TranslateSidToUser(targetSid);
            if (string.IsNullOrEmpty(username))
            {
                Set(resp, "status", "USER_NOT_FOUND");
                return resp;
            }

            // W2-004 ordering. Rotation used to change the Windows password
            // first and seal the new secret afterwards, so a sealing failure
            // left an account whose password nobody could read: the context
            // could not start and no retry could promote the right secret.
            // Verify-recoverable staged secret -> password -> publish.
            var prev = FindContextOp(LoadContextOps(), opId);
            string staged = prev != null ? S(prev, "staged_seal") : null;
            string newPassword = null;
            if (!string.IsNullOrEmpty(staged) && File.Exists(staged))
            {
                newPassword = ReadStagedSealedSecret(staged, targetSid);
                if (newPassword == null)
                {
                    Set(resp, "status", "SECRET_STAGED_UNRECOVERABLE");
                    Set(resp, "error", "staged secret " + Path.GetFileName(staged) + " cannot be decrypted; cannot change the password to match an unreadable secret");
                    return resp;
                }
            }
            else
            {
                newPassword = GenerateStrongPassword();
                string stagedNow;
                if (!StageSealedSecret(targetSid, newPassword, out stagedNow))
                {
                    Set(resp, "status", "SECRET_SEAL_FAILED");
                    Set(resp, "error", "staged secret could not be written, ACL-verified and decryption-verified");
                    return resp;
                }
                staged = stagedNow;
                UpsertContextOp(opId, "secret_staged", username, targetSid, staged, null);
            }

            string pwError;
            if (!UpdateUserPassword(username, newPassword, out pwError))
            {
                Set(resp, "status", "PASSWORD_UPDATE_FAILED");
                Set(resp, "error", pwError);
                return resp;
            }
            UpsertContextOp(opId, "password_applied", username, targetSid, staged, null);

            string pubError;
            if (!PromoteStagedSealedSecret(staged, targetSid, out pubError))
            {
                Set(resp, "status", "SECRET_PUBLISH_FAILED");
                Set(resp, "error", pubError);
                Set(resp, "staged_secret_retained", Path.GetFileName(staged));
                Set(resp, "target_sid", targetSid);
                if (opId != null) Set(resp, "operation_id", opId);
                return resp;
            }
            UpsertContextOp(opId, "completed", username, targetSid, null, null);

            Set(resp, "status", "rotated");
            Set(resp, "target_sid", targetSid);
            if (opId != null) Set(resp, "operation_id", opId);
            return resp;
        }

        if (op == "managed_context_remove")
        {
            string targetSid = S(req, "target_sid");
            bool deleteUser = object.Equals(Get(req, "delete_windows_account"), true);

            if (string.IsNullOrEmpty(targetSid) || !targetSid.StartsWith("S-1-5-"))
            {
                Set(resp, "status", "INVALID_TARGET_SID");
                return resp;
            }
            try { new SecurityIdentifier(targetSid); }
            catch { Set(resp, "status", "INVALID_TARGET_SID"); return resp; }

            var failedOps = new List<string>();
            var errors = new Dictionary<string, string>();

            // 1. Revoke workdir access grants owned by this helper for targetSid
            var revokeResp = RevokeManagedWorkdirAccess(targetSid);
            string rStat = S(revokeResp, "status");
            int failedRules = 0;
            object frObj = Get(revokeResp, "failed_rules");
            if (frObj is int) failedRules = (int)frObj;

            bool grantsRevokedOk = (rStat == "revoked" && failedRules == 0);
            if (!grantsRevokedOk)
            {
                failedOps.Add("revoke_workdir_grants");
                errors["revoke_workdir_grants"] = rStat ?? "revoke_failed";
            }
            Set(resp, "workdir_grants_revoked", grantsRevokedOk);

            // 2. Delete sealed secret and verify absence
            bool secretDeletedOk = false;
            if (HasSealedSecret(targetSid))
            {
                if (DeleteSealedSecret(targetSid) && !HasSealedSecret(targetSid))
                {
                    secretDeletedOk = true;
                }
                else
                {
                    failedOps.Add("delete_sealed_secret");
                    errors["delete_sealed_secret"] = "secret_file_deletion_failed";
                }
            }
            else
            {
                secretDeletedOk = true;
            }
            Set(resp, "sealed_secret_deleted", secretDeletedOk);

            // 3. If delete_windows_account requested, remove Windows account
            bool userDeletedOk = true;
            if (deleteUser)
            {
                string username = TranslateSidToUser(targetSid);
                if (username != null)
                {
                    if (!username.StartsWith("SAIAG_", StringComparison.OrdinalIgnoreCase))
                    {
                        failedOps.Add("delete_local_user");
                        errors["delete_local_user"] = "USER_DELETE_REFUSED_NOT_MANAGED";
                        userDeletedOk = false;
                    }
                    else
                    {
                        string uErr;
                        bool delRes = DeleteLocalUser(username, out uErr);
                        bool stillExists = CheckUserExists(targetSid);
                        if (delRes && !stillExists)
                        {
                            userDeletedOk = true;
                        }
                        else
                        {
                            failedOps.Add("delete_local_user");
                            errors["delete_local_user"] = uErr ?? "user_still_exists";
                            userDeletedOk = false;
                        }
                    }
                }
                else
                {
                    if (CheckUserExists(targetSid))
                    {
                        failedOps.Add("delete_local_user");
                        errors["delete_local_user"] = "unresolvable_sid_user_exists";
                        userDeletedOk = false;
                    }
                    else
                    {
                        userDeletedOk = true;
                    }
                }
                Set(resp, "windows_user_deleted", userDeletedOk);
            }

            if (failedOps.Count > 0)
            {
                Set(resp, "status", "PARTIAL_CLEANUP_FAILED");
                Set(resp, "failed_operations", failedOps.ToArray());
                Set(resp, "errors", errors);
                LogHelper("managed_context_remove PARTIAL_CLEANUP_FAILED target_sid=" + targetSid +
                          " failed=" + string.Join(",", failedOps.ToArray()));
                return resp;
            }

            Set(resp, "status", "removed");
            Set(resp, "target_sid", targetSid);
            LogHelper("managed_context_remove removed target_sid=" + targetSid);
            return resp;
        }

        if (op == "managed_workdir_ensure_access") { return EnsureManagedWorkdirAccess(S(req, "target_sid"), S(req, "workdir")); }
        if (op == "managed_workdir_revoke_access") { return RevokeManagedWorkdirAccess(S(req, "target_sid")); }
        if (op == "managed_workdir_revoke_path_access") { return RevokeManagedPathAccess(S(req, "target_sid"), S(req, "workdir")); }

        Set(resp, "status", "OPERATION_NOT_ALLOWED");
        return resp;
    }

    // ------------------------------------------------------- managed workdir access
    //
    // A managed account cannot open an operator project tree by default: the
    // folder is perfectly valid and the managed SID simply has no rights on it.
    // That is a cross-identity filesystem authorization gap, not a bad folder,
    // and it is the only reason a signed-in secondary account still could not
    // do any work. This is the narrow repair: grant ONE named local SID Modify
    // on ONE canonical directory, inheriting to its children, and nothing else.
    //
    // What this deliberately is not: a general "grant filesystem permissions"
    // API. It takes a SID and a path and nothing else -- no program, no script,
    // no rights mask, no inheritance switch -- so the reachable set of damage
    // is exactly one ACL entry on one directory, chosen by the operator's own
    // project picker. FullControl is never granted; inheritance is never
    // blocked, converted or reset; existing rules are merged, never replaced.

    // What an agent needs to work in a project: Modify (read, write, create,
    // delete) plus Synchronize, which Modify alone does not carry and without
    // which some handles fail in ways that look like flakiness. Not FullControl:
    // nothing here needs to rewrite an ACL or take ownership of anything.
    const FileSystemRights ManagedWorkdirRights = FileSystemRights.Modify | FileSystemRights.Synchronize;

    static string GrantsLedgerFile() { return Path.Combine(RootDir(), "grants.json"); }
    static string GrantsTxnFile() { return Path.Combine(RootDir(), "grants.txn.json"); }
    static string GrantsLockFile() { return Path.Combine(RootDir(), "grants.lock"); }

    private static IDisposable AcquireGrantsLock(int timeoutMs = 5000)
    {
        string lockPath = GrantsLockFile();
        try { Directory.CreateDirectory(Path.GetDirectoryName(lockPath)); } catch { }
        DateTime start = DateTime.UtcNow;
        while ((DateTime.UtcNow - start).TotalMilliseconds < timeoutMs)
        {
            try
            {
                return new FileStream(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            }
            catch (IOException)
            {
                Thread.Sleep(50);
            }
            catch (UnauthorizedAccessException)
            {
                Thread.Sleep(50);
            }
        }
        return null;
    }

    internal enum LedgerStatus
    {
        Valid,
        Missing,
        ReadFailed,
        Corrupted
    }

    internal class LedgerLoadResult
    {
        public LedgerStatus Status;
        public string Error;
        public List<Dictionary<string, object>> Rows = new List<Dictionary<string, object>>();
    }

    static LedgerLoadResult LoadGrantLedgerSafe()
    {
        var res = new LedgerLoadResult();
        string path = GrantsLedgerFile();
        if (!File.Exists(path))
        {
            res.Status = LedgerStatus.Missing;
            return res;
        }

        string raw;
        try
        {
            raw = File.ReadAllText(path, Encoding.UTF8);
        }
        catch (Exception ex)
        {
            res.Status = LedgerStatus.ReadFailed;
            res.Error = ex.Message;
            return res;
        }

        try
        {
            if (string.IsNullOrEmpty(raw) || string.IsNullOrEmpty(raw.Trim()))
            {
                res.Status = LedgerStatus.Corrupted;
                res.Error = "empty ledger file";
                return res;
            }
            var parsed = Json.DeserializeObject(raw) as object[];
            if (parsed == null)
            {
                res.Status = LedgerStatus.Corrupted;
                res.Error = "grant ledger is not a JSON array";
                return res;
            }
            foreach (var row in parsed)
            {
                var d = row as Dictionary<string, object>;
                if (d == null)
                {
                    res.Status = LedgerStatus.Corrupted;
                    res.Error = "grant ledger row is not an object";
                    res.Rows.Clear();
                    return res;
                }
                res.Rows.Add(d);
            }
            res.Status = LedgerStatus.Valid;
            return res;
        }
        catch (Exception ex)
        {
            res.Status = LedgerStatus.Corrupted;
            res.Error = ex.Message;
            return res;
        }
    }

    static List<Dictionary<string, object>> LoadGrantLedger()
    {
        var res = LoadGrantLedgerSafe();
        return res.Rows;
    }

    // Ownership witness. A ledger row proves the helper owns an ACE only
    // when the directory's DACL is still in exactly the generation the
    // helper left it in: the ordered list of explicit rules plus owner and
    // inheritance-protection state. Any external change -- removal, a
    // foreign equivalent rule, a re-added instead-of-removed rule -- moves
    // this fingerprint, and an unmoved fingerprint is the only thing that
    // makes "this is still the rule WE wrote" a checkable fact.
    static string AclFingerprint(DirectorySecurity sec)
    {
        try
        {
            var sb = new System.Text.StringBuilder();
            sb.Append("owner=").Append(sec.GetOwner(typeof(SecurityIdentifier)).Value).Append(';');
            sb.Append("protected=").Append(sec.AreAccessRulesProtected ? "1" : "0").Append(';');
            foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, false, typeof(SecurityIdentifier)))
            {
                sb.Append(rule.IdentityReference.Value).Append('|')
                  .Append(((int)rule.FileSystemRights).ToString(CultureInfo.InvariantCulture)).Append('|')
                  .Append(rule.AccessControlType == AccessControlType.Allow ? "A" : "D").Append('|')
                  .Append(((int)rule.InheritanceFlags).ToString(CultureInfo.InvariantCulture)).Append('|')
                  .Append(((int)rule.PropagationFlags).ToString(CultureInfo.InvariantCulture)).Append(';');
            }
            return Fingerprint(sb.ToString());
        }
        catch { return null; }
    }

    static string CurrentAclFingerprint(string canonical)
    {
        try { return AclFingerprint(Directory.GetAccessControl(canonical)); }
        catch { return null; }
    }

    // Flush staged content before publication. Flush(true) plus Replace/Move
    // does not prove physical power-loss durability of content and filesystem
    // metadata; that ceiling is an operator-accepted limitation (E-3009).
    static bool WriteTextDurable(string path, string text, out string error)
    {
        error = null;
        try
        {
            byte[] bytes = Encoding.UTF8.GetBytes(text);
            using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                fs.Write(bytes, 0, bytes.Length);
                fs.Flush(true);
            }
            return true;
        }
        catch (Exception ex) { error = ex.Message; return false; }
    }

    static bool SaveGrantLedgerSafe(List<Dictionary<string, object>> rows, out string error)
    {
        error = null;
        try
        {
            string path = GrantsLedgerFile();
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string tmp = path + ".staged." + Guid.NewGuid().ToString("N");
            if (!WriteTextDurable(tmp, Json.Serialize(rows.ToArray()), out error))
                return false;
            if (File.Exists(path))
            {
                File.Replace(tmp, path, null);
            }
            else
            {
                File.Move(tmp, path);
            }
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    static void SaveGrantLedger(List<Dictionary<string, object>> rows)
    {
        string err;
        SaveGrantLedgerSafe(rows, out err);
    }

    // Test-only fault-injection seam. SAI_ACCOUNTS_JOURNAL_FAULT names a stage
    // that must fail, so the fault-injection suite can exercise points that
    // are otherwise unreachable from outside the process. Unset in every
    // production path; a named stage fails exactly the way a real failure at
    // that stage does, and never corrupts the staged bytes.
    static string TxnJournalFaultStage()
    {
        try { return Environment.GetEnvironmentVariable("SAI_ACCOUNTS_JOURNAL_FAULT"); }
        catch { return null; }
    }

    // The ONLY publish path for the authoritative transaction journal. The
    // journal file itself is never opened for writing: the complete updated
    // record is staged beside it, flushed, and only then published through an
    // atomic replace. A failure at any point therefore leaves the previous
    // valid journal intact instead of truncating the evidence in place.
    // Counts journal publishes in this process so a fault stage may be
    // qualified with an ordinal. The suite starts one helper per scenario.
    static int _txnPublishOrdinal = 0;
    static string _txnAuthorityRaw;
    static bool _txnAuthorityLegacy;

    // Deterministic process-termination controls require BOTH test mode and
    // a disposable temp root. The marker proves the checkpoint and PID; only
    // the test that owns that PID may release or terminate the process.
    static void TxnJournalCheckpoint(string stage, int ordinal = 0)
    {
        if (!IsTestMode()) return;
        string selected = Environment.GetEnvironmentVariable("SAI_ACCOUNTS_JOURNAL_CHECKPOINT");
        if (selected != stage && selected != stage + "@" + ordinal.ToString(CultureInfo.InvariantCulture)) return;
        string root = Path.GetFullPath(RootDir());
        string temp = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!root.StartsWith(temp, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("journal checkpoint requires a disposable temp root");
        string marker = GrantsTxnFile() + ".checkpoint";
        var d = Obj();
        Set(d, "stage", stage); Set(d, "ordinal", ordinal);
        Set(d, "pid", Process.GetCurrentProcess().Id);
        File.WriteAllText(marker, Json.Serialize(d));
        DateTime deadline = DateTime.UtcNow.AddSeconds(20);
        while (!File.Exists(marker + ".release"))
        {
            if (DateTime.UtcNow >= deadline) throw new IOException("journal checkpoint timed out: " + stage);
            Thread.Sleep(10);
        }
    }

    static bool PublishTxnJournal(string txnFile, Dictionary<string, object> d, out string error,
        string previousRaw, bool requireAbsent)
    {
        error = null;
        string tmp = txnFile + ".tmp." + Guid.NewGuid().ToString("N");
        string fault = TxnJournalFaultStage();
        // A stage may be qualified with a publish ordinal ("replace@2") so the
        // suite can fail one specific state transition and leave the others to
        // publish normally. Unqualified stages always fail.
        int faultAt = 0;
        if (!string.IsNullOrEmpty(fault))
        {
            int at = fault.LastIndexOf('@');
            if (at > 0)
            {
                if (!int.TryParse(fault.Substring(at + 1), out faultAt)) faultAt = 0;
                fault = fault.Substring(0, at);
            }
        }
        int ordinal = ++_txnPublishOrdinal;
        bool failHere = string.IsNullOrEmpty(fault) ? false
                      : (faultAt == 0 || faultAt == ordinal);
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(txnFile));
            TxnJournalCheckpoint("pre-stage", ordinal);
            if (failHere && fault == "pre-stage")
            {
                error = "journal fault: pre-stage";
                return false;
            }
            string payload = Json.Serialize(d);
            if (IsTestMode() && Environment.GetEnvironmentVariable("SAI_ACCOUNTS_JOURNAL_CHECKPOINT") == "stage-write@" + ordinal)
            {
                File.WriteAllText(tmp, payload.Substring(0, Math.Min(1, payload.Length)));
                TxnJournalCheckpoint("stage-write", ordinal);
            }
            if (failHere && fault == "stage-write")
            {
                // Staged bytes are incomplete; the authoritative journal is
                // never opened, so it keeps its previous content verbatim.
                File.WriteAllText(tmp, payload.Substring(0, Math.Min(1, payload.Length)));
                error = "journal fault: stage-write";
                return false;
            }
            string werr;
            if (!WriteTextDurable(tmp, payload, out werr))
            {
                error = werr;
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
                return false;
            }
            if (failHere && fault == "post-flush")
            {
                error = "journal fault: post-flush";
                return false;
            }
            TxnJournalCheckpoint("post-flush", ordinal);
            if (failHere && fault == "replace")
            {
                error = "journal fault: replace";
                return false;
            }
            // The grants lock serializes cooperating helpers. Recheck the
            // exact read generation after staging as well, so an observed
            // non-cooperating replacement is never overwritten by stale data.
            if (requireAbsent ? File.Exists(txnFile) :
                (!File.Exists(txnFile) || File.ReadAllText(txnFile, Encoding.UTF8) != previousRaw))
            {
                error = "transaction journal changed before publication";
                return false;
            }
            if (File.Exists(txnFile)) File.Replace(tmp, txnFile, null);
            else File.Move(tmp, txnFile);
            _txnAuthorityRaw = payload;
            TxnJournalCheckpoint("after-publish", ordinal);
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
        finally
        {
            // Clean only this attempt's stage, including real OS refusals.
            try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
        }
    }

    static string ValidateTxnJournal(Dictionary<string, object> d, bool allowLegacy)
    {
        if (d == null) return "corrupted transaction file: expected an object";
        string schema = S(d, "schema");
        if (schema != "sai.accounts.helper/txn/2" && !(allowLegacy && string.IsNullOrEmpty(schema)))
            return "unsupported transaction schema: " + (schema ?? "<missing>");
        if (!(Get(d, "txn_id") is string) || string.IsNullOrWhiteSpace(S(d, "txn_id")))
            return "missing or invalid transaction identity";
        string op = S(d, "op");
        if (op != "grant" && op != "revoke") return "unknown transaction operation: " + (op ?? "<null>");
        try { if (!S(d, "target_sid").StartsWith("S-1-5-")) return "invalid transaction target_sid"; new SecurityIdentifier(S(d, "target_sid")); }
        catch { return "invalid transaction target_sid"; }
        if (!(Get(d, "created") is bool) || !(Get(d, "state") is string) || string.IsNullOrWhiteSpace(S(d, "state")))
            return "malformed transaction state or ownership flag";
        string wd = S(d, "workdir");
        if (op == "grant" && string.IsNullOrEmpty(wd)) return "grant transaction missing workdir";
        if (!string.IsNullOrEmpty(wd) && (!Path.IsPathRooted(wd) || !string.Equals(Path.GetFullPath(wd), wd, StringComparison.OrdinalIgnoreCase)))
            return "transaction workdir is not a canonical path";
        object targets = Get(d, "target_workdirs");
        if (targets != null)
        {
            var paths = targets as object[];
            if (paths == null) return "malformed transaction target scope";
            foreach (object item in paths)
            {
                string path = item as string;
                if (string.IsNullOrEmpty(path) || !Path.IsPathRooted(path)
                    || !string.Equals(Path.GetFullPath(path), path, StringComparison.OrdinalIgnoreCase))
                    return "transaction target scope is not canonical";
            }
        }
        return null;
    }

    static string LoadTxnJournalAuthority(bool allowLegacy, out Dictionary<string, object> d, out string raw)
    {
        d = null; raw = null;
        try
        {
            string path = GrantsTxnFile();
            if (!File.Exists(path)) return "existing transaction journal is missing";
            raw = File.ReadAllText(path, Encoding.UTF8);
            d = Json.DeserializeObject(raw) as Dictionary<string, object>;
            string error = ValidateTxnJournal(d, allowLegacy);
            if (error != null) return error;
            if (_txnAuthorityRaw != null && _txnAuthorityRaw != raw)
                return "transaction journal changed since authority was acquired";
            _txnAuthorityRaw = raw;
            _txnAuthorityLegacy = string.IsNullOrEmpty(S(d, "schema"));
            return null;
        }
        catch (Exception ex) { return "transaction journal read failed: " + ex.Message; }
    }

    static bool TxnFieldAgrees(Dictionary<string, object> d, string key, object value)
    {
        return value == null || Json.Serialize(Get(d, key)) == Json.Serialize(value);
    }

    // The transaction journal is the only durable proof of what the helper
    // INTENDED before touching a DACL. Version 2 adds the two facts that make
    // reconciliation possible after an arbitrary interruption: the ACL
    // fingerprint read immediately before the mutation (pre_mutation_acl_fp)
    // and the fingerprint read after the helper verified its own rule
    // (post_mutation_acl_fp). Together they distinguish "the helper's own
    // verified mutation" from "an ACE that appeared by other means".
    //
    // Returns null on success, or the persistence error. Callers must never
    // treat a non-null return as a persisted journal. This is the single
    // journal-update mechanism: an EXISTING journal is evolved, never
    // rewritten, so transaction identity (txn_id, op, target_sid, workdir,
    // created, target_workdirs) and every ownership witness already recorded
    // survive each state transition.
    static string UpdateTxnJournal(string state, string cleanupError,
        string op, string targetSid, string workdir, bool? created,
        List<string> targetWorkdirs, string preMutationAclFp,
        string postMutationAclFp, string postWriteAclFp, bool beginTransaction)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(state)) return "transaction transition requires a non-empty state";
            string txnFile = GrantsTxnFile();
            Dictionary<string, object> d = null;
            string previousRaw = null;
            if (beginTransaction)
            {
                if (state != "intent_logged") return "new transaction requires an explicit intent_logged start";
                if (File.Exists(txnFile) || _txnAuthorityRaw != null)
                    return "existing transaction must be reconciled before a new start";
                d = Obj();
                Set(d, "schema", "sai.accounts.helper/txn/2");
                Set(d, "txn_id", Guid.NewGuid().ToString("N"));
                Set(d, "op", op); Set(d, "target_sid", targetSid);
                Set(d, "workdir", workdir); Set(d, "created", created);
                if (targetWorkdirs != null) Set(d, "target_workdirs", targetWorkdirs.ToArray());
                Set(d, "state", state);
                string invalid = ValidateTxnJournal(d, false);
                if (invalid != null) return invalid;
                _txnAuthorityLegacy = false;
            }
            else
            {
                string invalid = LoadTxnJournalAuthority(_txnAuthorityLegacy, out d, out previousRaw);
                if (invalid != null) return invalid;
                if (!TxnFieldAgrees(d, "op", op) || !TxnFieldAgrees(d, "target_sid", targetSid)
                    || !TxnFieldAgrees(d, "workdir", workdir) || !TxnFieldAgrees(d, "created", created)
                    || !TxnFieldAgrees(d, "target_workdirs", targetWorkdirs == null ? null : targetWorkdirs.ToArray()))
                    return "transaction operation, SID or target scope cannot change";
                if (!string.IsNullOrEmpty(preMutationAclFp) && Get(d, "pre_mutation_acl_fp") != null
                    && S(d, "pre_mutation_acl_fp") != preMutationAclFp)
                    return "transaction pre-mutation witness cannot change";
                if (!string.IsNullOrEmpty(postWriteAclFp) && Get(d, "post_write_acl_fp") != null
                    && S(d, "post_write_acl_fp") != postWriteAclFp)
                    return "transaction write witness cannot change";
                if (!string.IsNullOrEmpty(postMutationAclFp) && Get(d, "post_mutation_acl_fp") != null
                    && S(d, "post_mutation_acl_fp") != postMutationAclFp)
                    return "transaction verified witness cannot change";
            }
            TxnJournalCheckpoint("after-load");
            Set(d, "state", state);
            if (!string.IsNullOrEmpty(preMutationAclFp)) Set(d, "pre_mutation_acl_fp", preMutationAclFp);
            if (!string.IsNullOrEmpty(postMutationAclFp)) Set(d, "post_mutation_acl_fp", postMutationAclFp);
            // Carried forward by default: an ace_verified transition must never
            // drop the write witness that made this transaction provably the
            // helper's. Later caller-supplied witnesses must agree.
            if (!string.IsNullOrEmpty(postWriteAclFp)) Set(d, "post_write_acl_fp", postWriteAclFp);
            if (!string.IsNullOrEmpty(cleanupError)) Set(d, "cleanup_error", cleanupError);
            Set(d, "updated_at", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));

            string error;
            return PublishTxnJournal(txnFile, d, out error, previousRaw, beginTransaction) ? null : error;
        }
        catch (Exception ex) { return ex.Message; }
    }

    // Convenience wrapper for the grant/revoke write path. beginTransaction
    // mints a fresh transaction identity (state "intent_logged"); the later
    // state transitions of that same transaction pass false so their
    // witnesses and identity carry forward.
    static bool WriteTxnIntent(string op, string targetSid, string workdir, bool created, string state, List<string> targetWorkdirs = null, string preMutationAclFp = null, string postMutationAclFp = null, string postWriteAclFp = null, bool beginTransaction = true)
    {
        string err = UpdateTxnJournal(state, null, op, targetSid, workdir, created,
            targetWorkdirs, preMutationAclFp, postMutationAclFp, postWriteAclFp, beginTransaction);
        if (err != null) Console.Error.WriteLine("WriteTxnIntent error: " + err);
        return err == null;
    }

    // Stamp a state onto the EXISTING journal, preserving its identity and
    // every witness. Used by failure/cleanup paths that must not rewrite
    // history. Returns false when the state could not be persisted.
    static bool MarkTxnIntentState(string state, string reason)
    {
        string err = UpdateTxnJournal(state, reason, null, null, null, null,
            null, null, null, null, false);
        if (err != null) Console.Error.WriteLine("MarkTxnIntentState write error: " + err);
        return err == null;
    }

    // Failure-path bookkeeping for the grant journal: the journal is RETAINED
    // with the failing state stamped on it unless it can be deleted after a
    // proven rollback. Deleting the journal on an unproven rollback is what
    // destroys the ability to reconcile deterministically. The stamp goes
    // through the shared update mechanism, so the authoritative journal is
    // never truncated in place.
    static bool MarkTxnIntentFailed(string op, string targetSid, string workdir, string state, string cleanupError)
    {
        string err = UpdateTxnJournal(state, cleanupError, op, targetSid, workdir, null,
            null, null, null, null, false);
        if (err != null) Console.Error.WriteLine("MarkTxnIntentFailed write error: " + err);
        return err == null;
    }

    static bool RemoveTxnIntent(out string error)
    {
        error = null;
        try
        {
            string txnFile = GrantsTxnFile();
            if (!File.Exists(txnFile))
            {
                if (_txnAuthorityRaw != null) { error = "existing transaction journal is missing"; return false; }
                return true;
            }
            Dictionary<string, object> d;
            string raw;
            error = LoadTxnJournalAuthority(_txnAuthorityLegacy, out d, out raw);
            if (error != null) return false;
            File.Delete(txnFile);
            if (File.Exists(txnFile))
            {
                error = "transaction journal still exists after deletion";
                return false;
            }
            _txnAuthorityRaw = null;
            _txnAuthorityLegacy = false;
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            // The journal could not be deleted; stamp the failure onto it via
            // the shared atomic update mechanism instead of truncating it in
            // place, so the record of the committed state survives.
            string werr = UpdateTxnJournal("committed_journal_cleanup_failed", ex.Message,
                null, null, null, null, null, null, null, null, false);
            if (werr != null)
            {
                error += "; cleanup-error state could not be persisted: " + werr;
                Console.Error.WriteLine("RemoveTxnIntent state write error: " + werr);
            }
            return false;
        }
    }

    static void RemoveTxnIntent()
    {
        string ignored;
        RemoveTxnIntent(out ignored);
    }

    static bool IsExactHelperRule(FileSystemAccessRule rule, SecurityIdentifier sid)
    {
        if (rule == null) return false;
        if (rule.IsInherited) return false;
        if (rule.AccessControlType != AccessControlType.Allow) return false;
        if (!sid.Equals(rule.IdentityReference)) return false;
        if (rule.FileSystemRights != ManagedWorkdirRights) return false;
        if (rule.InheritanceFlags != (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) return false;
        if (rule.PropagationFlags != PropagationFlags.None) return false;
        return true;
    }

    static bool HasExactHelperRule(DirectorySecurity sec, SecurityIdentifier sid)
    {
        if (sec == null) return false;
        foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, false, typeof(SecurityIdentifier)))
        {
            if (IsExactHelperRule(rule, sid)) return true;
        }
        return false;
    }

    static bool RollbackAce(string canonical, SecurityIdentifier sid)
    {
        try
        {
            if (!Directory.Exists(canonical)) return true;
            var info = new DirectoryInfo(canonical);
            DirectorySecurity sec = info.GetAccessControl();
            var exact = new List<FileSystemAccessRule>();
            foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, false, typeof(SecurityIdentifier)))
            {
                if (IsExactHelperRule(rule, sid)) exact.Add(rule);
            }
            if (exact.Count == 0) return true;

            foreach (FileSystemAccessRule rule in exact) sec.RemoveAccessRuleSpecific(rule);
            info.SetAccessControl(sec);

            DirectorySecurity after = Directory.GetAccessControl(canonical);
            return !HasExactHelperRule(after, sid);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("RollbackAce error: " + ex.Message);
            return false;
        }
    }

    // The single decision every revoke path funnels through: may WE delete
    // the exact rule that lives on this directory right now?
    //   NoRule               -- nothing of ours is there; the row just retires
    //   ForeignReplacement   -- the ACL generation moved since this helper
    //                           verified its own mutation; refuse and keep evidence
    //   Provable             -- the recorded witness matches the live DACL
    //   LegacyUnverified     -- no witness recorded (pre-witness ledger row):
    //                           compatibility removal for older deployments
    enum AceOwnership { NoRule, ForeignReplacement, Provable, LegacyUnverified }

    static AceOwnership CheckAceOwnership(Dictionary<string, object> row, string workdir, SecurityIdentifier sid, out string currentFp)
    {
        currentFp = null;
        if (!Directory.Exists(workdir)) return AceOwnership.NoRule;
        try
        {
            currentFp = CurrentAclFingerprint(workdir);
            if (!HasExactHelperRule(Directory.GetAccessControl(workdir), sid))
                return AceOwnership.NoRule;
        }
        catch
        {
            return AceOwnership.NoRule;
        }
        string witness = S(row, "acl_fp");
        if (string.IsNullOrEmpty(witness)) return AceOwnership.LegacyUnverified;
        if (!string.IsNullOrEmpty(currentFp) && string.Equals(witness, currentFp, StringComparison.Ordinal))
            return AceOwnership.Provable;
        return AceOwnership.ForeignReplacement;
    }

    static bool ReconcileInterruptedTxn(out string error)
    {
        error = null;
        string txnFile = GrantsTxnFile();
        if (!File.Exists(txnFile))
        {
            if (_txnAuthorityRaw != null) { error = "existing transaction journal is missing"; return false; }
            return true;
        }

        try
        {
            string raw = File.ReadAllText(txnFile, Encoding.UTF8);
            if (string.IsNullOrEmpty(raw) || string.IsNullOrEmpty(raw.Trim()))
            {
                error = "empty transaction file";
                return false;
            }

            var d = Json.DeserializeObject(raw) as Dictionary<string, object>;
            if (d == null)
            {
                error = "corrupted transaction file";
                return false;
            }

            string authorityError = LoadTxnJournalAuthority(true, out d, out raw);
            if (authorityError != null) { error = authorityError; return false; }

            string op = S(d, "op");
            string targetSid = S(d, "target_sid");
            string workdir = S(d, "workdir");
            string schema = S(d, "schema");
            string state = S(d, "state");

            // A journal written by an unknown future format is never
            // reinterpreted: refusing keeps the evidence intact for an
            // operator who can read the newer contract.
            if (!string.IsNullOrEmpty(schema) && schema != "sai.accounts.helper/txn/2")
            {
                error = "unsupported transaction schema: " + schema;
                return false;
            }

            // Boundary 7: Refuse unknown operations, malformed records and ambiguous ownership
            if (string.IsNullOrEmpty(op) || (op != "grant" && op != "revoke"))
            {
                error = "unknown transaction operation: " + (op ?? "<null>");
                return false;
            }

            if (string.IsNullOrEmpty(targetSid) || !targetSid.StartsWith("S-1-5-"))
            {
                error = "invalid or ambiguous transaction target_sid: " + (targetSid ?? "<null>");
                return false;
            }

            SecurityIdentifier sid;
            try
            {
                sid = new SecurityIdentifier(targetSid);
            }
            catch (Exception ex)
            {
                error = "malformed transaction target_sid: " + ex.Message;
                return false;
            }

            var load = LoadGrantLedgerSafe();
            if (load.Status == LedgerStatus.ReadFailed || load.Status == LedgerStatus.Corrupted)
            {
                error = "ledger unreadable during transaction reconciliation: " + load.Error;
                return false;
            }

            // A transaction that already committed on disk but whose journal
            // cleanup failed must never re-run its ACL work: the retry only
            // deletes the journal.
            if (state == "committed_journal_cleanup_failed")
            {
                string retryErr;
                if (!RemoveTxnIntent(out retryErr))
                {
                    error = "committed transaction journal cleanup still failing: " + retryErr;
                    return false;
                }
                return true;
            }

            if (op == "grant")
            {
                string canonical;
                try
                {
                    canonical = string.IsNullOrEmpty(workdir) ? null : Path.GetFullPath(workdir);
                }
                catch
                {
                    error = "grant transaction workdir is not a canonical path: " + workdir;
                    return false;
                }
                if (string.IsNullOrEmpty(canonical))
                {
                    error = "grant transaction missing workdir";
                    return false;
                }

                bool hasAce = false;
                string currentFp = null;
                if (Directory.Exists(canonical))
                {
                    try
                    {
                        currentFp = CurrentAclFingerprint(canonical);
                        hasAce = HasExactHelperRule(Directory.GetAccessControl(canonical), sid);
                    }
                    catch (Exception ex)
                    {
                        error = "stale transaction against unreadable workdir ACL: " + ex.Message;
                        return false;
                    }
                }

                string key = targetSid + "|" + canonical.ToUpperInvariant();
                string recordedWriteFp = S(d, "post_write_acl_fp");
                if (schema == "sai.accounts.helper/txn/2" && hasAce
                    && !string.IsNullOrEmpty(recordedWriteFp)
                    && !string.Equals(recordedWriteFp, currentFp, StringComparison.Ordinal))
                {
                    error = "foreign ACL generation during transaction recovery; no ACE deleted; journal evidence retained";
                    return false;
                }
                Dictionary<string, object> row = null;
                foreach (var r in load.Rows)
                {
                    if (string.Equals(S(r, "key"), key, StringComparison.Ordinal)) { row = r; break; }
                }

                if (row != null && object.Equals(Get(row, "created"), true))
                {
                    // A live ownership claim must be re-proven against its
                    // witness; a moved generation retracts the claim instead of
                    // letting a later revoke delete whatever replaced our rule.
                    string witness = S(row, "acl_fp");
                    if (!string.IsNullOrEmpty(witness) && !string.IsNullOrEmpty(currentFp)
                        && !string.Equals(witness, currentFp, StringComparison.Ordinal))
                    {
                        Set(row, "created", false);
                        Set(row, "acl_fp", null);
                        Set(row, "ownership_retracted_at", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));
                        string saveErr;
                        if (!SaveGrantLedgerSafe(load.Rows, out saveErr))
                        {
                            error = "failed to commit ownership retraction during reconciliation: " + saveErr;
                            return false;
                        }
                    }
                }
                else if (hasAce)
                {
                    // An exact rule exists and no proven-ownership row covers
                    // it. Ownership may be adopted ONLY from a v2 journal that
                    // proves THIS helper wrote and verified its own mutation:
                    // state=ace_verified (or mutation_written, mid-flight)
                    // plus a pre-mutation witness the ACL has since moved away
                    // from and a recorded write witness that still matches it.
                    // Anything else (a legacy journal, an externally-placed
                    // ACE) is recorded created=false so no later revoke may
                    // ever delete it.
                    bool adopt = false;
                    if (schema == "sai.accounts.helper/txn/2")
                    {
                        string preFp = S(d, "pre_mutation_acl_fp");
                        if ((state == "ace_verified" || state == "mutation_written" || state == "ace_verified_write_failed")
                            && !string.IsNullOrEmpty(preFp) && !string.IsNullOrEmpty(currentFp)
                            && !string.Equals(preFp, currentFp, StringComparison.Ordinal))
                        {
                            string writeFp = S(d, "post_write_acl_fp");
                            // A recorded write witness must agree with the live
                            // ACL; ace_verified journals predating the witness
                            // stay valid.
                            string verifiedFp = S(d, "post_mutation_acl_fp");
                            if ((!string.IsNullOrEmpty(writeFp) && string.Equals(writeFp, currentFp, StringComparison.Ordinal))
                                || (state == "ace_verified" && string.IsNullOrEmpty(writeFp)
                                    && !string.IsNullOrEmpty(verifiedFp) && string.Equals(verifiedFp, currentFp, StringComparison.Ordinal)))
                            {
                                adopt = true;
                            }
                        }
                    }

                    if (row == null)
                    {
                        row = Obj();
                        Set(row, "key", key);
                        Set(row, "target_sid", targetSid);
                        Set(row, "workdir", canonical);
                        load.Rows.Add(row);
                    }
                    if (adopt)
                    {
                        object pg = Get(row, "gen");
                        int gen = (pg is int) ? (int)pg + 1 : 1;
                        Set(row, "gen", gen);
                        Set(row, "created", true);
                        Set(row, "acl_fp", currentFp);
                        Set(row, "schema", "sai.accounts.helper/ledger/2");
                    }
                    else
                    {
                        Set(row, "created", false);
                        Set(row, "acl_fp", null);
                        Set(row, "last_seen", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));
                    }
                    string saveErr;
                    if (!SaveGrantLedgerSafe(load.Rows, out saveErr))
                    {
                        if (adopt)
                        {
                            error = "reconciliation failed to commit adopted ownership for " + canonical + ": " + saveErr;
                            return false;
                        }
                        error = "reconciliation failed to commit ledger for " + canonical + ": " + saveErr;
                        return false;
                    }
                }

                string removeErr;
                if (!RemoveTxnIntent(out removeErr))
                {
                    error = "grant reconciliation succeeded but journal cleanup failed: " + removeErr;
                    return false;
                }
                return true;
            }
            else if (op == "revoke")
            {
                // Boundary 3: Implement real interrupted-revoke reconciliation!
                var workdirsToCheck = new List<string>();
                object twObj = Get(d, "target_workdirs");
                if (twObj is object[])
                {
                    foreach (object item in (object[])twObj)
                    {
                        if (item is string && !string.IsNullOrEmpty((string)item))
                            workdirsToCheck.Add((string)item);
                    }
                }
                if (!string.IsNullOrEmpty(workdir))
                {
                    workdirsToCheck.Add(workdir);
                }

                // Justification: every path the transaction intends to touch
                // must be backed by a created=true ledger row for this exact
                // SID. A path the ledger does not own is not ours to touch --
                // refuse and preserve the journal as evidence.
                foreach (string wdRaw in workdirsToCheck)
                {
                    string wd;
                    try { wd = Path.GetFullPath(wdRaw); }
                    catch { error = "revoke transaction target path is not canonical: " + wdRaw; return false; }
                    bool justified = false;
                    foreach (var r in load.Rows)
                    {
                        if (string.Equals(S(r, "target_sid"), targetSid, StringComparison.OrdinalIgnoreCase)
                            && object.Equals(Get(r, "created"), true)
                            && string.Equals(S(r, "workdir"), wd, StringComparison.OrdinalIgnoreCase))
                        {
                            justified = true;
                            break;
                        }
                    }
                    if (!justified)
                    {
                        error = "revoke transaction references a path without a proven ownership record: " + wd;
                        return false;
                    }
                }

                // The persisted intent names its own scope. Reconciliation
                // operates on EXACTLY that set: a created=true row for the same
                // SID at a path the transaction did not name was produced by
                // some other operation and is not this transaction's to run.
                var scope = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
                foreach (string wdRaw in workdirsToCheck)
                {
                    try { scope[Path.GetFullPath(wdRaw)] = true; } catch { }
                }

                string refusal = null;
                var remaining = new List<Dictionary<string, object>>();
                bool ledgerChanged = false;
                foreach (var row in load.Rows)
                {
                    if (string.Equals(S(row, "target_sid"), targetSid, StringComparison.OrdinalIgnoreCase)
                        && object.Equals(Get(row, "created"), true)
                        && scope.ContainsKey(S(row, "workdir")))
                    {
                        string wd = S(row, "workdir");
                        if (string.IsNullOrEmpty(wd))
                        {
                            remaining.Add(row);
                            continue;
                        }

                        AceOwnership own;
                        string ownFp = null;
                        try
                        {
                            own = CheckAceOwnership(row, wd, sid, out ownFp);
                        }
                        catch (Exception ex)
                        {
                            error = "failed reading/mutating ACL during revoke reconciliation for " + wd + ": " + ex.Message;
                            return false;
                        }

                        if (own == AceOwnership.Provable || own == AceOwnership.LegacyUnverified)
                        {
                            if (!RollbackAce(wd, sid))
                            {
                                error = "failed to revoke ACE during reconciliation for " + wd;
                                return false;
                            }
                            ledgerChanged = true;
                            continue;
                        }
                        if (own == AceOwnership.ForeignReplacement)
                        {
                            // Never delete a rule the recorded generation does
                            // not account for; keep the row and refuse below.
                            remaining.Add(row);
                            refusal = refusal ?? ("ownership of the current ACE cannot be proven for " + wd
                                + " (foreign replacement or unrecorded generation); refusing to delete it");
                            continue;
                        }
                        // nothing of ours on this directory: retire the row
                        ledgerChanged = true;
                        continue;
                    }
                    remaining.Add(row);
                }

                if (refusal != null)
                {
                    error = refusal;
                    return false;
                }

                if (ledgerChanged)
                {
                    string saveErr;
                    if (!SaveGrantLedgerSafe(remaining, out saveErr))
                    {
                        error = "failed to commit ledger during revoke reconciliation: " + saveErr;
                        return false;
                    }
                }

                string removeErr;
                if (!RemoveTxnIntent(out removeErr))
                {
                    error = "revoke reconciliation succeeded but journal cleanup failed: " + removeErr;
                    return false;
                }
                return true;
            }

            error = "unhandled transaction op: " + op;
            return false;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    // `created` is a statement about a MUTATION the helper performed and
    // verified, not about a row's history. Two corrections the old freeze
    // ("whatever the row already said wins") could never make:
    //   CASE A: a third-party ACE was removed by an admin and this call then
    //           wrote and verified the helper's own rule -> the row now owns a
    //           fresh generation, gen+1, stamped with the witness read right
    //           after the verified change. The old boolean left created=false
    //           and orphaned the grant: revoke would never look at it again.
    //   CASE B: the helper's rule disappeared and was externally replaced by an
    //           equivalent rule, and this call only OBSERVED it (created=false)
    //           -> the recorded witness no longer matches the live ACL, so the
    //           created=true claim is retracted. Without the retraction, revoke
    //           would delete the foreign rule as if it were ours.
    // A row without a witness is legacy: an observation cannot verify it and
    // the claim is left exactly as found -- nothing is silently promoted.
    static bool RecordManagedWorkdirGrantSafe(List<Dictionary<string, object>> all, string targetSid, string canonical, bool created, out string error, string currentAclFp = null)
    {
        error = null;
        try
        {
            string key = targetSid + "|" + canonical.ToUpperInvariant();
            string now = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
            bool found = false;
            foreach (var g in all)
            {
                if (string.Equals(S(g, "key"), key, StringComparison.Ordinal))
                {
                    object was = Get(g, "created");
                    bool wasCreated = (was is bool) && (bool)was;
                    if (created)
                    {
                        object pg = Get(g, "gen");
                        int gen = (pg is int) ? (int)pg + 1 : 1;
                        Set(g, "gen", gen);
                        Set(g, "created", true);
                        Set(g, "acl_fp", currentAclFp);
                        Set(g, "schema", "sai.accounts.helper/ledger/2");
                    }
                    else if (wasCreated)
                    {
                        // Observation only: re-prove the existing claim against
                        // its recorded generation before letting it stand.
                        string witness = S(g, "acl_fp");
                        if (!string.IsNullOrEmpty(witness)
                            && !string.IsNullOrEmpty(currentAclFp)
                            && !string.Equals(witness, currentAclFp, StringComparison.Ordinal))
                        {
                            Set(g, "created", false);
                            Set(g, "acl_fp", null);
                            Set(g, "ownership_retracted_at", now);
                        }
                    }
                    Set(g, "last_seen", now);
                    found = true;
                    break;
                }
            }
            if (!found)
            {
                var row = Obj();
                Set(row, "key", key);
                Set(row, "target_sid", targetSid);
                Set(row, "workdir", canonical);
                Set(row, "created", created);
                Set(row, "last_seen", now);
                if (created)
                {
                    Set(row, "gen", 1);
                    Set(row, "acl_fp", currentAclFp);
                    Set(row, "schema", "sai.accounts.helper/ledger/2");
                }
                all.Add(row);
            }
            return SaveGrantLedgerSafe(all, out error);
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    // Records that a grant exists for this SID+path. `created` says whether
    // THIS HELPER wrote the rule. Only rows with created=true may ever be
    // revoked; a row with created=false is a note that equivalent rights were
    // already there, and deleting someone else's rule is not ours to do.
    static void RecordManagedWorkdirGrant(string targetSid, string canonical, bool created)
    {
        var all = LoadGrantLedger();
        string err;
        RecordManagedWorkdirGrantSafe(all, targetSid, canonical, created, out err);
    }

    static string[] ProtectedWorkdirRoots()
    {
        var roots = new List<string>();
        try { roots.Add(Environment.GetFolderPath(Environment.SpecialFolder.Windows)); } catch { }
        try { roots.Add(Environment.SystemDirectory); } catch { }
        try { roots.Add(Environment.GetFolderPath(Environment.SpecialFolder.System)); } catch { }
        try { roots.Add(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles)); } catch { }
        try { roots.Add(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86)); } catch { }
        try { roots.Add(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData)); } catch { }
        return roots.ToArray();
    }

    // Refuse the catastrophic targets before any mutation: a drive root is
    // every file on the volume rather than a project, and the system roots are
    // never something an agent was asked to work in. An ANCESTOR of a protected
    // root is refused too, because granting Modify there would hand out the
    // whole protected subtree along with it.
    static string RejectManagedWorkdirTarget(string canonical)
    {
        try
        {
            string trimmed = canonical.TrimEnd('\\', '/');
            if (trimmed.Length <= 2 || (trimmed.Length == 2 && trimmed[1] == ':')) return "WORKDIR_TARGET_REFUSED";
            foreach (string raw in ProtectedWorkdirRoots())
            {
                if (string.IsNullOrEmpty(raw)) continue;
                string prot = raw.TrimEnd('\\', '/');
                if (prot.Length == 0) continue;
                if (string.Equals(trimmed, prot, StringComparison.OrdinalIgnoreCase)) return "WORKDIR_TARGET_REFUSED";
                if (prot.StartsWith(trimmed + "\\", StringComparison.OrdinalIgnoreCase)) return "WORKDIR_TARGET_REFUSED";
            }
            return null;
        }
        catch { return "WORKDIR_TARGET_REFUSED"; }
    }

    static bool IsWellKnownSidValue(SecurityIdentifier sid)
    {
        // Enumerated rather than hardcoded, so a future WellKnownSidType cannot
        // slip through as an unlisted "just a SID" that happens to be Everyone
        // or BUILTIN\Users.
        foreach (WellKnownSidType t in Enum.GetValues(typeof(WellKnownSidType)))
        {
            try { if (new SecurityIdentifier(t, null).Equals(sid)) return true; }
            catch { }
        }
        return false;
    }

    // Test mode short-circuits TranslateSidToUser, because the context
    // operations have to keep working against SIDs that only exist in a
    // fixture. CheckUserExists keeps that contract; the hermetic suite
    // therefore cannot exercise UNKNOWN_TARGET_SID, and says so rather than
    // asserting a branch it cannot reach.
    static string RejectManagedWorkdirSid(string sid, bool requireUserExists = true)
    {
        if (string.IsNullOrEmpty(sid)) return "INVALID_TARGET_SID";
        SecurityIdentifier parsed;
        try { parsed = new SecurityIdentifier(sid); }
        catch { return "INVALID_TARGET_SID"; }
        // Everyone, BUILTIN\Users, Authenticated Users and SYSTEM are all
        // well-known SIDs, and granting any of them would publish the project
        // to the whole machine. This op exists to name ONE managed account.
        if (IsWellKnownSidValue(parsed)) return "WELL_KNOWN_SID_REFUSED";
        if (requireUserExists && !CheckUserExists(sid)) return "UNKNOWN_TARGET_SID";
        return null;
    }

    static bool HasSufficientManagedGrant(DirectorySecurity sec, SecurityIdentifier sid)
    {
        foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, true, typeof(SecurityIdentifier)))
        {
            if (rule.AccessControlType != AccessControlType.Allow) continue;
            if (!sid.Equals(rule.IdentityReference)) continue;
            if ((rule.FileSystemRights & ManagedWorkdirRights) != ManagedWorkdirRights) continue;
            // The mask alone is not enough: a rule that does not propagate to
            // children grants the directory but leaves the files inside it
            // unreadable, which would look exactly like the bug we are fixing.
            if ((rule.InheritanceFlags & (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) == 0) continue;
            return true;
        }
        return false;
    }

    // Win32 code of a managed exception, e.g. 5 for ACCESS_DENIED and 32 for
    // SHARE_VIOLATION. The low word of the HRESULT is the Win32 error and
    // carries no path, no token and no message, so it is safe to put in an
    // envelope that gets logged.
    static int Win32Code(Exception ex)
    {
        try { return unchecked((int)(ex.HResult & 0xFFFF)); }
        catch { return 0; }
    }

    // Structured failure detail. The TYPE and the Win32 code are what makes
    // "could not read the ACL" distinguishable from "read it, and it does not
    // grant what we need"; an English message would have to be parsed.
    static void SetAclFailure(Dictionary<string, object> resp, string status, Exception ex, string phase)
    {
        Set(resp, "status", status);
        Set(resp, "error_type", ex.GetType().Name);
        Set(resp, "win32_error", Win32Code(ex));
        Set(resp, "phase", phase);
        LogHelper("managed_workdir_ensure_access " + status + " phase=" + phase +
                  " type=" + ex.GetType().Name + " win32=" + Win32Code(ex) +
                  " target_sid=" + S(resp, "target_sid"));
    }

    // A deny ACE naming the managed SID (explicitly or through a group it is in)
    // defeats any Allow this helper could add, because Deny wins. Piling more
    // Allow rules on top would never help and would silently rewrite the
    // operator's policy, so the conflict is reported instead.
    static bool HasDenyConflict(DirectorySecurity sec, SecurityIdentifier sid)
    {
        try
        {
            foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                if (rule.AccessControlType != AccessControlType.Deny) continue;
                IdentityReference id = rule.IdentityReference;
                SecurityIdentifier ruleSid = id as SecurityIdentifier;
                if (ruleSid == null) continue;
                if (ruleSid.Equals(sid)) return true;
                // A group the managed account belongs to. The helper cannot
                // resolve group membership for an arbitrary SID without the
                // token, so a deny on a well-known group the account is in is
                // the case worth naming.
                if ((ruleSid.Value == "S-1-5-32-545" || ruleSid.Value == "S-1-5-11"))
                    return true;
            }
        }
        catch { }
        return false;
    }

    // ------------------------------------------------------------ console profile
    // Ctrl+V stopped working in a managed console because the operator's
    // console contract lives in the OPERATOR's HKCU, and a managed account is
    // a different Windows user with its own hive. Nothing the operator's
    // profile says reaches it, and CONSOLES.ps1 setting modes on its own
    // input handle does not travel either -- the handle it changed belongs to
    // a different process on a different console.
    //
    // So the contract is written into the managed account's own hive, by the
    // one component that already holds the managed SID's authority. It is a
    // fixed list of nine known console values. It is not a registry-write API:
    // the caller names a SID and nothing else, and the value set, the value
    // names and the subkeys below Console are all decided here.
    static readonly string[] ManagedConsoleSubkeys = { "Console", @"Console\C_ProgramData_SAI_Accounts_bin_agy.exe" };
    static readonly string[] ManagedConsoleDwords = { "ForceV2", "CtrlKeyShortcutsDisabled", "ExtendedEditKey", "QuickEdit", "InsertMode", "FilterOnPaste", "InterceptCopyPaste", "LineSelection", "VirtualTerminalLevel" };
    static readonly string[] ConsolePresentationDwords = {
        "ScreenColors", "PopupColors", "WindowAlpha", "FontFamily", "FontSize", "FontWeight",
        "ColorTable00", "ColorTable01", "ColorTable02", "ColorTable03", "ColorTable04", "ColorTable05", "ColorTable06", "ColorTable07",
        "ColorTable08", "ColorTable09", "ColorTable10", "ColorTable11", "ColorTable12", "ColorTable13", "ColorTable14", "ColorTable15"
    };
    static readonly string[] ConsolePresentationStrings = { "FaceName", "FontFace", "WintagePalette" };

    // Only the existing operator presentation contract is mirrored. Missing or
    // unsupported values leave the target's safe defaults intact; no registry
    // subtree, credentials or request-supplied values enter this writer.
    static Dictionary<string, object> ReconcileConsolePresentation(RegistryKey operatorUser,
        RegistryKey managedUser, string[] subkeys)
    {
        var result = Obj();
        Set(result, "status", "unavailable");
        int written = 0, correct = 0, unavailable = 0;
        if (operatorUser == null || managedUser == null) return result;
        try
        {
            using (RegistryKey defaults = operatorUser.OpenSubKey("Console"))
            {
                var names = new List<string>(ConsolePresentationDwords);
                names.AddRange(ConsolePresentationStrings);
                foreach (string sub in new HashSet<string>(subkeys, StringComparer.OrdinalIgnoreCase))
                using (RegistryKey specific = sub == "Console" ? null : operatorUser.OpenSubKey(sub))
                {
                    foreach (string name in names)
                    {
                        RegistryKey source = specific;
                        object want = source == null ? null : source.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                        if (want == null)
                        {
                            source = defaults;
                            want = source == null ? null : source.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                        }
                        RegistryValueKind kind = Array.IndexOf(ConsolePresentationDwords, name) >= 0
                            ? RegistryValueKind.DWord : RegistryValueKind.String;
                        if (want == null || source.GetValueKind(name) != kind
                            || (kind == RegistryValueKind.DWord && !(want is int))
                            || (kind == RegistryValueKind.String && (!(want is string)
                                || ((string)want).Length == 0 || ((string)want).Length > 256)))
                        { unavailable++; continue; }
                        if (want is int)
                        {
                            int number = (int)want;
                            int maximum = name == "WindowAlpha" ? 255
                                : (name == "ScreenColors" || name == "PopupColors") ? 65535
                                : name.StartsWith("ColorTable", StringComparison.Ordinal) ? 0xFFFFFF : int.MaxValue;
                            if (number < 0 || number > maximum) { unavailable++; continue; }
                        }
                        using (RegistryKey target = managedUser.CreateSubKey(sub))
                        {
                            if (target == null) throw new IOException("could not open console presentation key " + sub);
                            object current = target.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                            if (object.Equals(current, want) && target.GetValueKind(name) == kind)
                            { correct++; continue; }
                            target.SetValue(name, want, kind);
                            written++;
                        }
                    }
                }
            }
            Set(result, "status", written > 0 ? "applied" : correct > 0 ? "already_correct" : "unavailable");
        }
        catch (Exception ex)
        {
            Set(result, "status", "failed");
            Set(result, "error", ex.GetType().Name + ": " + ex.Message);
        }
        Set(result, "values_written", written);
        Set(result, "values_already_correct", correct);
        Set(result, "values_unavailable", unavailable);
        return result;
    }

    static Dictionary<string, object> MirrorOperatorConsolePresentation(string operatorSid,
        RegistryKey managedUser, string[] subkeys)
    {
        string authorized = LoadAuthorizedOperatorSid() ?? WindowsIdentity.GetCurrent().User.Value;
        if (string.IsNullOrEmpty(operatorSid) || !operatorSid.StartsWith("S-1-5-21-", StringComparison.Ordinal))
            return new Dictionary<string, object> { { "status", "unavailable" } };
        if (!string.Equals(operatorSid, authorized, StringComparison.OrdinalIgnoreCase))
            return new Dictionary<string, object> { { "status", "unavailable" } };
        try
        {
            using (RegistryKey operatorUser = Registry.Users.OpenSubKey(operatorSid))
                return ReconcileConsolePresentation(operatorUser, managedUser, subkeys);
        }
        catch (Exception ex)
        {
            return new Dictionary<string, object> { { "status", "failed" }, { "error", ex.GetType().Name + ": " + ex.Message } };
        }
    }

    // T-265: the value each name must hold. Eight of the nine are switches that
    // mean "on" at 1. CtrlKeyShortcutsDisabled is the inverted one: it is the
    // console's "disable Ctrl key shortcuts" flag, so 1 turned OFF Ctrl+C,
    // Ctrl+V, Ctrl+A and Ctrl+M selection in every managed console. Writing 1
    // across the board is exactly the regression the operator saw: no copy, no
    // paste, no select-all. The operator-side installer has always written 0
    // here (Installers\INSTALL_AI_AGENT_MENUS.PS1), and this mirrors it.
    static int ManagedConsoleValue(string name)
    {
        return string.Equals(name, "CtrlKeyShortcutsDisabled", StringComparison.Ordinal) ? 0 : 1;
    }

    // ------------------------------------------------------------ input languages
    // T-265: EN/RU switching did not work in a managed console because the
    // managed account is a separate Windows user. Its hive carries only the
    // input languages Windows gives a brand-new profile, so the operator's
    // Russian layout and the Ctrl+Shift switch simply do not exist there.
    //
    // The operator's own list is mirrored into the managed hive. The values come
    // from the OPERATOR'S hive, read here by the helper -- never from the request
    // -- and every one of them must match a strict pattern before it is written:
    // a keyboard layout id, an input-method id, a language tag or a toggle digit.
    // Nothing else in either hive is read or written.
    static readonly string[] KeyboardPreloadSlots = { "1", "2", "3", "4", "5", "6", "7", "8", "9" };
    static readonly string[] KeyboardToggleNames = { "Hotkey", "Language Hotkey", "Layout Hotkey" };
    static readonly System.Text.RegularExpressions.Regex KeyboardLayoutId =
        new System.Text.RegularExpressions.Regex("^[0-9A-Fa-f]{8}$");
    static readonly System.Text.RegularExpressions.Regex KeyboardToggleValue =
        new System.Text.RegularExpressions.Regex("^[1-4]$");
    static readonly System.Text.RegularExpressions.Regex LanguageTag =
        new System.Text.RegularExpressions.Regex("^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$");
    static readonly System.Text.RegularExpressions.Regex InputMethodId =
        new System.Text.RegularExpressions.Regex("^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{8}$");

    // Returns a status word for the response: applied, already_correct, or why
    // nothing was mirrored. Never throws; a failure here must not stop the
    // context from starting -- the console still works, it just keeps the
    // profile's stock languages.
    static string MirrorOperatorInputLanguages(string operatorSid, RegistryKey managedUser)
    {
        if (managedUser == null) return "managed_hive_unavailable";
        if (string.IsNullOrEmpty(operatorSid) || !operatorSid.StartsWith("S-1-5-21-", StringComparison.Ordinal))
            return "operator_unknown";
        try
        {
            var preload = new List<string>();
            var toggle = new Dictionary<string, string>(StringComparer.Ordinal);
            var languages = new List<string>();
            var methods = new Dictionary<string, List<KeyValuePair<string, int>>>(StringComparer.OrdinalIgnoreCase);
            using (RegistryKey op = Registry.Users.OpenSubKey(operatorSid))
            {
                if (op == null) return "operator_hive_not_loaded";
                using (RegistryKey k = op.OpenSubKey(@"Keyboard Layout\Preload"))
                    if (k != null)
                        foreach (string slot in KeyboardPreloadSlots)
                        {
                            string v = k.GetValue(slot) as string;
                            if (v != null && KeyboardLayoutId.IsMatch(v)) preload.Add(v);
                        }
                using (RegistryKey k = op.OpenSubKey(@"Keyboard Layout\Toggle"))
                    if (k != null)
                        foreach (string t in KeyboardToggleNames)
                        {
                            string v = k.GetValue(t) as string;
                            if (v != null && KeyboardToggleValue.IsMatch(v)) toggle[t] = v;
                        }
                using (RegistryKey k = op.OpenSubKey(@"Control Panel\International\User Profile"))
                {
                    string[] raw = k != null ? k.GetValue("Languages") as string[] : null;
                    if (raw != null)
                        foreach (string tag in raw)
                        {
                            if (tag == null || !LanguageTag.IsMatch(tag) || languages.Count >= KeyboardPreloadSlots.Length) continue;
                            var list = new List<KeyValuePair<string, int>>();
                            using (RegistryKey lk = k.OpenSubKey(tag))
                                if (lk != null)
                                    foreach (string im in lk.GetValueNames())
                                    {
                                        object v = lk.GetValue(im);
                                        if (InputMethodId.IsMatch(im) && v is int) list.Add(new KeyValuePair<string, int>(im, (int)v));
                                    }
                            languages.Add(tag);
                            methods[tag] = list;
                        }
                }
            }
            if (preload.Count == 0 && languages.Count == 0) return "operator_languages_unreadable";

            int written = 0;
            if (preload.Count > 0)
                using (RegistryKey k = managedUser.CreateSubKey(@"Keyboard Layout\Preload"))
                {
                    if (k == null) return "keyboard_key_failed";
                    for (int i = 0; i < KeyboardPreloadSlots.Length; i++)
                    {
                        string slot = KeyboardPreloadSlots[i];
                        string have = k.GetValue(slot) as string;
                        if (i < preload.Count)
                        {
                            if (!string.Equals(have, preload[i], StringComparison.OrdinalIgnoreCase))
                            { k.SetValue(slot, preload[i], RegistryValueKind.String); written++; }
                        }
                        else if (k.GetValue(slot) != null) { k.DeleteValue(slot, false); written++; }
                    }
                }
            if (toggle.Count > 0)
                using (RegistryKey k = managedUser.CreateSubKey(@"Keyboard Layout\Toggle"))
                {
                    if (k == null) return "keyboard_key_failed";
                    foreach (string t in KeyboardToggleNames)
                    {
                        string want;
                        if (!toggle.TryGetValue(t, out want)) continue;
                        if (!string.Equals(k.GetValue(t) as string, want, StringComparison.Ordinal))
                        { k.SetValue(t, want, RegistryValueKind.String); written++; }
                    }
                }
            if (languages.Count > 0)
                using (RegistryKey k = managedUser.CreateSubKey(@"Control Panel\International\User Profile"))
                {
                    if (k == null) return "language_key_failed";
                    string[] have = k.GetValue("Languages") as string[];
                    if (have == null || string.Join("\0", have) != string.Join("\0", languages.ToArray()))
                    { k.SetValue("Languages", languages.ToArray(), RegistryValueKind.MultiString); written++; }
                    foreach (string tag in languages)
                        using (RegistryKey lk = k.CreateSubKey(tag))
                        {
                            if (lk == null) continue;
                            foreach (KeyValuePair<string, int> im in methods[tag])
                            {
                                object cur = lk.GetValue(im.Key);
                                if (cur is int && (int)cur == im.Value) continue;
                                lk.SetValue(im.Key, im.Value, RegistryValueKind.DWord);
                                written++;
                            }
                        }
                }
            return written == 0 ? "already_correct" : "applied";
        }
        catch (Exception ex)
        {
            LogHelper("input language mirror failed: " + ex.GetType().Name);
            return "failed";
        }
    }

    // Windows names a per-executable console key by the image path with the
    // separators replaced, extension kept: C:\...\agy.exe becomes
    // Console\C:_..._agy.exe. The drive colon is preserved, as in HKCU\Console
    // carries "%SystemRoot%_System32_cmd.exe" and
    // "C:_Program Files_PowerShell_7_pwsh.EXE".
    static string ConsoleSubkeyFor(string exePath)
    {
        return @"Console\" + exePath.Replace('\\', '_').Replace('/', '_');
    }

    // The managed profile directory, read from the machine's own profile list
    // rather than guessed from the user name: it is the only answer that is
    // correct on a machine where the profile folder was relocated.
    static string ManagedProfileDir(string targetSid)
    {
        try
        {
            using (RegistryKey profiles = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"))
            {
                if (profiles == null) return null;
                using (RegistryKey one = profiles.OpenSubKey(targetSid))
                {
                    if (one == null) return null;
                    object path = one.GetValue("ProfileImagePath");
                    return path == null ? null : Environment.ExpandEnvironmentVariables(path.ToString());
                }
            }
        }
        catch { }
        return null;
    }

    public static Dictionary<string, object> EnsureManagedConsoleProfile(string targetSid, string agyExePath, string operatorSid)
    {
        var resp = Obj();
        Set(resp, "op", "managed_console_profile");
        Set(resp, "target_sid", targetSid);

        string sidFail = RejectManagedWorkdirSid(targetSid);
        if (sidFail != null) { Set(resp, "status", sidFail); return resp; }

        string profileDir = ManagedProfileDir(targetSid);
        if (string.IsNullOrEmpty(profileDir))
        {
            Set(resp, "status", "PROFILE_NOT_FOUND");
            Set(resp, "error", "no ProfileList entry for " + targetSid);
            return resp;
        }
        string ntuser = Path.Combine(profileDir, "NTUSER.DAT");
        if (!File.Exists(ntuser))
        {
            // The account exists but has never been logged in. That is not an
            // error worth refusing the start over: the logon this is
            // preceding is what creates the profile.
            Set(resp, "status", "PROFILE_NOT_PROVISIONED");
            Set(resp, "profile_dir", profileDir);
            return resp;
        }

        bool loadedHere = false;
        RegistryKey userKey = null;
        try
        {
            userKey = OpenManagedHiveForWrite(targetSid);
            if (userKey == null)
            {
                int rc = RegLoadKeyW(HKEY_USERS, targetSid, ntuser);
                if (rc != 0)
                {
                    Set(resp, "status", "HIVE_LOAD_FAILED");
                    Set(resp, "error", "RegLoadKey rc=" + rc);
                    return resp;
                }
                loadedHere = true;
                userKey = OpenManagedHiveForWrite(targetSid);
                if (userKey == null)
                {
                    Set(resp, "status", "HIVE_OPEN_FAILED");
                    Set(resp, "error", "hive loaded but HKU\\" + targetSid + " would not open");
                    return resp;
                }
            }

            var subkeys = new List<string>(ManagedConsoleSubkeys);
            if (!string.IsNullOrEmpty(agyExePath)) subkeys.Add(ConsoleSubkeyFor(agyExePath));
            // The guard is the first image of the interactive console. Its
            // executable override must receive the same authorized profile.
            subkeys.Add(ConsoleSubkeyFor(BrokerExePath()));
            subkeys = new List<string>(new HashSet<string>(subkeys, StringComparer.OrdinalIgnoreCase));

            int written = 0, unchanged = 0;
            foreach (string sub in subkeys)
            {
                using (RegistryKey k = userKey.CreateSubKey(sub))
                {
                    if (k == null)
                    {
                        Set(resp, "status", "CONSOLE_KEY_FAILED");
                        Set(resp, "error", "could not open " + sub);
                        return resp;
                    }
                    foreach (string name in ManagedConsoleDwords)
                    {
                        int want = ManagedConsoleValue(name);
                        object current = k.GetValue(name);
                        if (current is int && (int)current == want) { unchanged++; continue; }
                        k.SetValue(name, want, RegistryValueKind.DWord);
                        written++;
                    }
                }
            }

            var presentation = MirrorOperatorConsolePresentation(operatorSid, userKey, subkeys.ToArray());
            Set(resp, "console_presentation", S(presentation, "status"));
            if (Get(presentation, "error") != null) Set(resp, "console_presentation_error", Get(presentation, "error"));
            if (Get(presentation, "values_written") != null) written += (int)Get(presentation, "values_written");
            if (Get(presentation, "values_already_correct") != null) unchanged += (int)Get(presentation, "values_already_correct");
            Set(resp, "status", S(presentation, "status") == "failed" ? "CONSOLE_PROFILE_FAILED" : written == 0 ? "already_correct" : "applied");
            if (S(presentation, "status") == "failed") Set(resp, "error", Get(presentation, "error"));
            Set(resp, "input_languages", MirrorOperatorInputLanguages(operatorSid, userKey));
            Set(resp, "values_written", written);
            Set(resp, "values_already_correct", unchanged);
            Set(resp, "presentation_values_unavailable", Get(presentation, "values_unavailable"));
            Set(resp, "keys", subkeys.ToArray());
            Set(resp, "hive_loaded_by_this_operation", loadedHere);
            return resp;
        }
        catch (Exception ex)
        {
            Set(resp, "status", "CONSOLE_PROFILE_FAILED");
            Set(resp, "error", ex.GetType().Name + ": " + ex.Message);
            return resp;
        }
        finally
        {
            if (userKey != null) userKey.Close();
            // Only ever unload what this operation loaded. A hive that was
            // already mounted belongs to a live session, and pulling it out
            // from under the broker is how a running console turns into a
            // crash.
            if (loadedHere)
            {
                int rc = RegUnLoadKeyW(HKEY_USERS, targetSid);
                if (rc != 0) LogHelper("RegUnLoadKey(" + targetSid + ") rc=" + rc + " -- left mounted, it was loaded here but will not unload");
            }
        }
    }

    // Every grant failure branch lands here. The rule is uniform: a partial
    // mutation is only repairable by a rollback whose result is READ BACK
    // from the DACL. If the rollback verifies, the journal may be deleted;
    // if it does not, the journal is retained with the failing state stamped
    // on it and the caller reports INCONSISTENT_GRANT_STATE -- a deleted
    // journal would turn the next reconciliation into deterministic guessing.
    // Stamps a terminal failure state onto the EXISTING journal without
    // rewriting it from scratch: the transaction identity, operation, path,
    // fingerprints and any pre-mutation witness survive for the operator, only
    // the state and the reason change. Rewriting the journal with WriteTxnIntent
    // would discard exactly the evidence this branch is trying to keep.
    // Implemented by MarkTxnIntentState above, which routes every state stamp
    // through the shared atomic update mechanism.

    // What the live DACL proves about a rollback the helper is about to perform.
    //   Unchanged      -- the ACL is still exactly the generation recorded
    //                    before the mutation, so nothing of ours can be there.
    //   OwnMutation    -- the ACL is exactly the generation this helper left
    //                    after its own written mutation: deleting the exact rule
    //                    is deleting our own rule.
    //   ForeignGeneration -- the ACL moved in a way the journal does not
    //                    account for. No rollback may run: an equivalent ACE
    //                    is indistinguishable from ours by SID, rights and
    //                    inheritance alone, which is precisely the case where
    //                    deleting it would destroy a third party's grant.
    enum RollbackOwnership { Unchanged, OwnMutation, ForeignGeneration }

    // Decides whether HandleGrantFailure may delete an ACE, using the same
    // ownership witness the ledger and the reconciler trust: the fingerprint
    // written into the journal at each mutation boundary. Structural ACE
    // matching alone is never sufficient authority.
    static RollbackOwnership CheckRollbackOwnershipAt(string canonical, string preFp, string postWriteFp, string postFp)
    {
        string now;
        try { now = CurrentAclFingerprint(canonical); }
        catch { now = null; }
        if (string.IsNullOrEmpty(now)) return RollbackOwnership.ForeignGeneration;
        if (!string.IsNullOrEmpty(preFp) && string.Equals(preFp, now, StringComparison.Ordinal))
            return RollbackOwnership.Unchanged;
        if (!string.IsNullOrEmpty(postWriteFp) && string.Equals(postWriteFp, now, StringComparison.Ordinal))
            return RollbackOwnership.OwnMutation;
        if (!string.IsNullOrEmpty(postFp) && string.Equals(postFp, now, StringComparison.Ordinal))
            return RollbackOwnership.OwnMutation;
        return RollbackOwnership.ForeignGeneration;
    }

    static void HandleGrantFailure(Dictionary<string, object> resp, string canonical, string targetSid, SecurityIdentifier sid, string phase, Exception ex, string status)
    {
        // The journal this call wrote is the only evidence of what the helper
        // intended, so it is read back before anything is deleted: the
        // fingerprints stamped along the way are what make the rollback below
        // provable rather than a guess based on a structural signature.
        string preFp = null, postWriteFp = null, postFp = null;
        string authorityError = null;
        try
        {
            Dictionary<string, object> d;
            string raw;
            authorityError = LoadTxnJournalAuthority(false, out d, out raw);
            if (authorityError == null)
            {
                if (S(d, "op") == "grant" && S(d, "target_sid") == targetSid
                    && string.Equals(S(d, "workdir"), canonical, StringComparison.OrdinalIgnoreCase))
                {
                    preFp = S(d, "pre_mutation_acl_fp");
                    postWriteFp = S(d, "post_write_acl_fp");
                    postFp = S(d, "post_mutation_acl_fp");
                }
                else authorityError = "transaction journal does not authorize this grant scope";
            }
        }
        catch (Exception ex2)
        {
            Console.Error.WriteLine("HandleGrantFailure journal read error: " + ex2.Message);
            authorityError = ex2.Message;
        }

        if (authorityError != null)
        {
            Set(resp, "status", "INCONSISTENT_GRANT_STATE");
            Set(resp, "error", "rollback refused: " + authorityError + "; no ACE deleted or journal changed");
            Set(resp, "phase", phase);
            return;
        }

        RollbackOwnership own = CheckRollbackOwnershipAt(canonical, preFp, postWriteFp, postFp);

        if (own == RollbackOwnership.ForeignGeneration)
        {
            // The ACL generation the helper wrote is gone. Rolling back now
            // would delete a rule that is not provably ours, so the journal is
            // kept as evidence and the caller retries only after the operator
            // resolves the divergence.
            bool stamped = MarkTxnIntentState("foreign_generation_at_failure",
                "rollback refused at phase " + phase + ": the live ACL no longer matches any generation this helper wrote; no ACE was deleted");
            if (!stamped)
            {
                Set(resp, "status", "INCONSISTENT_GRANT_STATE");
                Set(resp, "error", "grant failed at phase " + phase + " against a foreign ACL generation and the transaction evidence could not be stamped");
                Set(resp, "phase", phase);
                LogHelper("managed_workdir_ensure_access INCONSISTENT_GRANT_STATE phase=" + phase +
                          " target_sid=" + targetSid + " workdir=" + canonical + " evidence_stamp=failed");
                return;
            }
            Set(resp, "status", "INCONSISTENT_GRANT_STATE");
            Set(resp, "error", "grant failed at phase " + phase + " and the live ACL generation moved since this helper wrote it; "
                + "no ACE was deleted because ownership of the current rule cannot be proven; transaction evidence retained");
            Set(resp, "phase", phase);
            LogHelper("managed_workdir_ensure_access INCONSISTENT_GRANT_STATE phase=" + phase +
                      " target_sid=" + targetSid + " workdir=" + canonical + " rollback=refused_foreign_generation");
            return;
        }

        // An unchanged pre-mutation generation requires no deletion. An ACE
        // already present in that generation belongs to the pre-existing ACL.
        bool rolledBack = own == RollbackOwnership.Unchanged || RollbackAce(canonical, sid);
        bool rollbackProven = rolledBack && (own == RollbackOwnership.Unchanged
            ? CurrentAclFingerprint(canonical) == preFp : HasExactHelperRuleVerified(canonical, sid, preFp));
        if (rolledBack && !rollbackProven)
        {
            rolledBack = false;
        }

        if (rolledBack)
        {
            string cleanupErr;
            if (RemoveTxnIntent(out cleanupErr))
            {
                if (ex == null)
                {
                    Set(resp, "status", status);
                    Set(resp, "error_type", "GrantNotObserved");
                    Set(resp, "win32_error", 0);
                    Set(resp, "phase", phase);
                    LogHelper("managed_workdir_ensure_access " + status + " phase=" + phase +
                              " type=GrantNotObserved win32=0 target_sid=" + targetSid);
                }
                else
                {
                    SetAclFailure(resp, status, ex, phase);
                }
                return;
            }
            Set(resp, "status", "JOURNAL_CLEANUP_FAILED");
            Set(resp, "error", "rollback verified but journal cleanup failed: " + cleanupErr);
            LogHelper("managed_workdir_ensure_access JOURNAL_CLEANUP_FAILED after rollback: " + cleanupErr);
            return;
        }

        // Rollback failed, or its result could not be proven. The journal is
        // stamped, not rewritten: identity, operation and witnesses stay
        // readable so a retry can decide deterministically.
        bool persisted = MarkTxnIntentState("inconsistent",
            "rollback could not be verified after failure at phase " + phase + "; no ACE deletion was trusted");
        Set(resp, "journal_state_persisted", persisted);
        Set(resp, "status", "INCONSISTENT_GRANT_STATE");
        Set(resp, "error", "grant failed at phase " + phase + " (" +
            (ex == null ? "GrantNotObserved" : ex.GetType().Name + ": " + ex.Message) +
            ") and rollback could not be verified; " + (persisted ? "transaction evidence retained" : "failure state could not be persisted; earlier evidence preserved"));
        Set(resp, "phase", phase);
        LogHelper("managed_workdir_ensure_access INCONSISTENT_GRANT_STATE phase=" + phase +
                  " target_sid=" + targetSid + " workdir=" + canonical);
    }

    // The rollback may only claim success when the directory really is back to
    // a generation this helper wrote. RollbackAce already checks that no exact
    // rule remains; this additionally checks the fingerprint returned to the
    // pre-mutation generation, so an ACE removed into a *newer* generation is
    // still reported as unproven.
    static bool HasExactHelperRuleVerified(string canonical, SecurityIdentifier sid, string preFp)
    {
        try
        {
            if (HasExactHelperRule(Directory.GetAccessControl(canonical), sid)) return false;
            if (string.IsNullOrEmpty(preFp)) return true;
            return string.Equals(CurrentAclFingerprint(canonical), preFp, StringComparison.Ordinal);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("HasExactHelperRuleVerified error: " + ex.Message);
            return false;
        }
    }

    static Dictionary<string, object> EnsureManagedWorkdirAccess(string targetSid, string workdirRaw)
    {
        var resp = Obj();
        Set(resp, "schema", "sai.accounts.helper/response/1");
        Set(resp, "op", "managed_workdir_ensure_access");
        Set(resp, "target_sid", targetSid);

        string sidFail = RejectManagedWorkdirSid(targetSid);
        if (sidFail != null) { Set(resp, "status", sidFail); return resp; }

        if (string.IsNullOrEmpty(workdirRaw)) { Set(resp, "status", "WORKDIR_REQUIRED"); return resp; }
        string canonical;
        try { canonical = Path.GetFullPath(workdirRaw); }
        catch { Set(resp, "status", "WORKDIR_INVALID"); return resp; }
        Set(resp, "workdir", canonical);

        string targetFail = RejectManagedWorkdirTarget(canonical);
        if (targetFail != null) { Set(resp, "status", targetFail); return resp; }

        using (var lockHandle = AcquireGrantsLock())
        {
            if (lockHandle == null)
            {
                Set(resp, "status", "LEDGER_LOCK_TIMEOUT");
                return resp;
            }

            string reconErr;
            if (!ReconcileInterruptedTxn(out reconErr))
            {
                Set(resp, "status", "INCONSISTENT_GRANT_STATE");
                Set(resp, "error", reconErr);
                return resp;
            }

            var loadRes = LoadGrantLedgerSafe();
            if (loadRes.Status == LedgerStatus.ReadFailed)
            {
                Set(resp, "status", "LEDGER_READ_FAILED");
                Set(resp, "error", loadRes.Error);
                return resp;
            }
            if (loadRes.Status == LedgerStatus.Corrupted)
            {
                Set(resp, "status", "LEDGER_CORRUPTED");
                Set(resp, "error", loadRes.Error);
                return resp;
            }

            bool exists;
            try { exists = Directory.Exists(canonical); }
            catch { exists = false; }
            if (!exists) { Set(resp, "status", "WORKDIR_NOT_FOUND"); return resp; }

            try
            {
                FileAttributes attrs = File.GetAttributes(canonical);
                if ((attrs & FileAttributes.ReparsePoint) == FileAttributes.ReparsePoint)
                {
                    Set(resp, "status", "WORKDIR_REPARSE_REFUSED");
                    LogHelper("managed_workdir_ensure_access WORKDIR_REPARSE_REFUSED target_sid=" +
                              targetSid + " workdir=" + canonical);
                    return resp;
                }
                Set(resp, "path_kind", "directory");
            }
            catch (Exception ex)
            {
                Set(resp, "path_kind", "unknown");
                SetAclFailure(resp, "WORKDIR_NOT_READABLE", ex, "attributes");
                return resp;
            }

            var sid = new SecurityIdentifier(targetSid);
            DirectorySecurity current;
            try { current = Directory.GetAccessControl(canonical); }
            catch (Exception ex)
            {
                SetAclFailure(resp, "ACL_READ_FAILED", ex, "read");
                return resp;
            }

            bool already = HasSufficientManagedGrant(current, sid);
            if (already && !HasDenyConflict(current, sid))
            {
                // Observation only, but carried with the live ACL witness so a
                // stale created=true claim gets retracted on the spot (CASE B).
                string recErr;
                if (!RecordManagedWorkdirGrantSafe(loadRes.Rows, targetSid, canonical, false, out recErr, CurrentAclFingerprint(canonical)))
                {
                    Set(resp, "status", "LEDGER_SAVE_FAILED");
                    Set(resp, "error", recErr);
                    return resp;
                }
                Set(resp, "status", "already_granted");
                return resp;
            }
            if (HasDenyConflict(current, sid))
            {
                Set(resp, "status", "EFFECTIVE_ACCESS_DENIED");
                LogHelper("managed_workdir_ensure_access EFFECTIVE_ACCESS_DENIED deny-conflict target_sid=" +
                          targetSid + " workdir=" + canonical);
                return resp;
            }

            // The pre-mutation witness is captured BEFORE the journal is
            // written: only the journal itself can later prove the mutation
            // that followed was this helper's.
            string preFp = CurrentAclFingerprint(canonical);
            if (!WriteTxnIntent("grant", targetSid, canonical, true, "intent_logged", null, preFp))
            {
                Set(resp, "status", "TRANSACTION_INIT_FAILED");
                return resp;
            }

            try
            {
                var info = new DirectoryInfo(canonical);
                DirectorySecurity sec = info.GetAccessControl();
                sec.AddAccessRule(new FileSystemAccessRule(
                    sid,
                    ManagedWorkdirRights,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None,
                    AccessControlType.Allow));
                info.SetAccessControl(sec);
                TxnJournalCheckpoint("after-acl");
            }
            catch (Exception ex)
            {
                HandleGrantFailure(resp, canonical, targetSid, sid, "write", ex, "ACL_MUTATION_FAILED");
                return resp;
            }

            // The write witness is stamped IMMEDIATELY after SetAccessControl
            // returns, before verification: it is the only proof that the ACE
            // now on disk was written by this helper and not placed by a third
            // party between the write and the verification. Without it, a
            // foreign equivalent ACE is structurally indistinguishable from
            // the helper's own and a rollback would delete the foreign rule.
            string writeFp;
            try { writeFp = CurrentAclFingerprint(canonical); }
            catch (Exception ex)
            {
                HandleGrantFailure(resp, canonical, targetSid, sid, "witness", ex, "ACL_MUTATION_FAILED");
                return resp;
            }
            bool witnessed = WriteTxnIntent("grant", targetSid, canonical, true, "mutation_written", null, preFp, null, writeFp, beginTransaction: false);
            if (!witnessed)
            {
                // No durable witness means no provable rollback. Fail closed:
                // the ACE is left in place and the evidence, whatever exists,
                // is preserved for an operator to resolve.
                bool failureStamped = MarkTxnIntentState("mutation_witness_write_failed",
                    "post-write ACL witness could not be journaled");
                Set(resp, "journal_state_persisted", failureStamped);
                Set(resp, "status", "INCONSISTENT_GRANT_STATE");
                Set(resp, "error", "grant ACL mutation succeeded but its post-write witness could not be journaled; "
                    + "no ACE was deleted and transaction evidence retained");
                Set(resp, "phase", "witness");
                LogHelper("managed_workdir_ensure_access INCONSISTENT_GRANT_STATE phase=witness " +
                          "target_sid=" + targetSid + " workdir=" + canonical);
                return resp;
            }

            try
            {
                DirectorySecurity after = Directory.GetAccessControl(canonical);
                if (!HasExactHelperRule(after, sid) || !HasSufficientManagedGrant(after, sid))
                {
                    HandleGrantFailure(resp, canonical, targetSid, sid, "verify", null, "ACL_MUTATION_FAILED");
                    return resp;
                }
                string postFp = CurrentAclFingerprint(canonical);
                // The ace_verified transition EVOLVES the journal: it reuses the
                // transaction identity and carries the mutation_written witness
                // forward rather than replacing the record and losing it. A
                // failure to persist it is never silently treated as persisted:
                // the journal is retained (still proving the mutation) and the
                // caller fails closed so an operator can reconcile.
                if (!WriteTxnIntent("grant", targetSid, canonical, true, "ace_verified", null, preFp, postFp, beginTransaction: false))
                {
                    bool failureStamped = MarkTxnIntentState("ace_verified_write_failed",
                        "verified grant state could not be journaled; evidence retained");
                    Set(resp, "journal_state_persisted", failureStamped);
                    Set(resp, "status", "INCONSISTENT_GRANT_STATE");
                    Set(resp, "error", "grant ACL was verified but its ace_verified journal state could not be persisted; "
                        + "no evidence was deleted and the mutation_written record is retained");
                    Set(resp, "phase", "verified");
                    LogHelper("managed_workdir_ensure_access INCONSISTENT_GRANT_STATE phase=verified " +
                              "target_sid=" + targetSid + " workdir=" + canonical);
                    return resp;
                }
            }
            catch (Exception ex)
            {
                HandleGrantFailure(resp, canonical, targetSid, sid, "verify", ex, "ACL_MUTATION_FAILED");
                return resp;
            }

            string saveErr;
            if (!RecordManagedWorkdirGrantSafe(loadRes.Rows, targetSid, canonical, true, out saveErr, CurrentAclFingerprint(canonical)))
            {
                HandleGrantFailure(resp, canonical, targetSid, sid, "persist", new InvalidOperationException("ledger save failed: " + saveErr), "LEDGER_SAVE_FAILED");
                return resp;
            }

            string journalErr;
            TxnJournalCheckpoint("ledger-committed");
            if (!RemoveTxnIntent(out journalErr))
            {
                Set(resp, "status", "JOURNAL_CLEANUP_FAILED");
                Set(resp, "error", journalErr);
                LogHelper("managed_workdir_ensure_access JOURNAL_CLEANUP_FAILED: " + journalErr);
                return resp;
            }

            LogHelper("managed_workdir_ensure_access granted target_sid=" + targetSid +
                      " workdir=" + canonical);
            Set(resp, "status", "granted");
            return resp;
        }
    }

    static Dictionary<string, object> RevokeManagedWorkdirAccess(string targetSid)
    {
        var resp = Obj();
        Set(resp, "schema", "sai.accounts.helper/response/1");
        Set(resp, "op", "managed_workdir_revoke_access");
        Set(resp, "target_sid", targetSid);

        string sidFail = RejectManagedWorkdirSid(targetSid, false);
        if (sidFail != null) { Set(resp, "status", sidFail); return resp; }

        using (var lockHandle = AcquireGrantsLock())
        {
            if (lockHandle == null)
            {
                Set(resp, "status", "LEDGER_LOCK_TIMEOUT");
                return resp;
            }

            string reconErr;
            if (!ReconcileInterruptedTxn(out reconErr))
            {
                Set(resp, "status", "INCONSISTENT_GRANT_STATE");
                Set(resp, "error", reconErr);
                return resp;
            }

            var loadRes = LoadGrantLedgerSafe();
            if (loadRes.Status == LedgerStatus.ReadFailed)
            {
                Set(resp, "status", "LEDGER_READ_FAILED");
                Set(resp, "error", loadRes.Error);
                return resp;
            }
            if (loadRes.Status == LedgerStatus.Corrupted)
            {
                Set(resp, "status", "LEDGER_CORRUPTED");
                Set(resp, "error", loadRes.Error);
                return resp;
            }

            var all = loadRes.Rows;
            var sid = new SecurityIdentifier(targetSid);
            int revoked = 0, preserved = 0, failed = 0, ownershipUnproven = 0, unverifiedOwnership = 0;
            var keep = new List<Dictionary<string, object>>();

            var targetWorkdirs = new List<string>();
            foreach (var row in all)
            {
                if (string.Equals(S(row, "target_sid"), targetSid, StringComparison.OrdinalIgnoreCase) &&
                    object.Equals(Get(row, "created"), true))
                {
                    string wd = S(row, "workdir");
                    if (!string.IsNullOrEmpty(wd)) targetWorkdirs.Add(wd);
                }
            }

            // Boundary 2: Require successful durable WriteTxnIntent before modifying any ACL
            if (!WriteTxnIntent("revoke", targetSid, "", false, "intent_logged", targetWorkdirs))
            {
                Set(resp, "status", "TRANSACTION_INIT_FAILED");
                return resp;
            }

            foreach (var row in all)
            {
                bool mine = string.Equals(S(row, "target_sid"), targetSid, StringComparison.OrdinalIgnoreCase);
                if (!mine) { keep.Add(row); continue; }

                if (!object.Equals(Get(row, "created"), true)) { preserved++; keep.Add(row); continue; }

                string workdir = S(row, "workdir");
                string ownFp = null;
                bool done = false;
                try
                {
                    if (string.IsNullOrEmpty(workdir))
                    {
                        failed++;
                    }
                    else if (Directory.Exists(workdir))
                    {
                        // Ownership of the exact rule on disk decides, never
                        // structural signature alone: a witness that matches
                        // the live DACL is the only clean proof.
                        AceOwnership own = CheckAceOwnership(row, workdir, sid, out ownFp);
                        if (own == AceOwnership.ForeignReplacement)
                        {
                            ownershipUnproven++;
                            failed++;
                        }
                        else
                        {
                            var info = new DirectoryInfo(workdir);
                            DirectorySecurity sec = info.GetAccessControl();
                            var exact = new List<FileSystemAccessRule>();
                            foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, false, typeof(SecurityIdentifier)))
                            {
                                if (IsExactHelperRule(rule, sid)) exact.Add(rule);
                            }
                            foreach (FileSystemAccessRule rule in exact) sec.RemoveAccessRuleSpecific(rule);
                            if (exact.Count > 0) info.SetAccessControl(sec);

                            DirectorySecurity after = Directory.GetAccessControl(workdir);
                            // Boundary 4: Ensure exact helper rule is removed while preserving unrelated rules
                            if (!HasExactHelperRule(after, sid))
                            {
                                revoked += exact.Count;
                                if (own == AceOwnership.LegacyUnverified) unverifiedOwnership += exact.Count;
                                done = true;
                            }
                            else
                            {
                                failed++;
                            }
                        }
                    }
                    else
                    {
                        done = true;
                    }
                }
                catch { failed++; }

                if (!done) keep.Add(row);
            }

            string saveErr;
            if (!SaveGrantLedgerSafe(keep, out saveErr))
            {
                Set(resp, "status", "LEDGER_SAVE_FAILED");
                Set(resp, "error", saveErr);
                Set(resp, "revoked_rules", revoked);
                Set(resp, "preserved_rules", preserved);
                Set(resp, "failed_rules", failed);
                Set(resp, "ownership_unproven_rules", ownershipUnproven);
                Set(resp, "unverified_ownership_rules", unverifiedOwnership);
                return resp;
            }

            // Boundary 5: Propagate cleanup failures and preserve recoverable terminal state
            string revokeJournalErr;
            if (!RemoveTxnIntent(out revokeJournalErr))
            {
                Set(resp, "status", "JOURNAL_CLEANUP_FAILED");
                Set(resp, "error", revokeJournalErr);
                Set(resp, "revoked_rules", revoked);
                Set(resp, "preserved_rules", preserved);
                Set(resp, "failed_rules", failed);
                Set(resp, "ownership_unproven_rules", ownershipUnproven);
                Set(resp, "unverified_ownership_rules", unverifiedOwnership);
                return resp;
            }

            // Boundary 6: Do not return unqualified status=revoked when failed_rules > 0
            if (failed > 0)
            {
                Set(resp, "status", "PARTIAL_REVOCATION_FAILED");
                Set(resp, "revoked_rules", revoked);
                Set(resp, "preserved_rules", preserved);
                Set(resp, "failed_rules", failed);
                Set(resp, "ownership_unproven_rules", ownershipUnproven);
                Set(resp, "unverified_ownership_rules", unverifiedOwnership);
                return resp;
            }

            Set(resp, "status", "revoked");
            Set(resp, "revoked_rules", revoked);
            Set(resp, "preserved_rules", preserved);
            Set(resp, "failed_rules", failed);
            Set(resp, "ownership_unproven_rules", ownershipUnproven);
            Set(resp, "unverified_ownership_rules", unverifiedOwnership);
            return resp;
        }
    }

    static Dictionary<string, object> RevokeManagedPathAccess(string targetSid, string workdirRaw)
    {
        var resp = Obj();
        Set(resp, "schema", "sai.accounts.helper/response/1");
        Set(resp, "op", "managed_workdir_revoke_path_access");
        Set(resp, "target_sid", targetSid);

        string sidFail = RejectManagedWorkdirSid(targetSid, false);
        if (sidFail != null) { Set(resp, "status", sidFail); return resp; }

        if (string.IsNullOrEmpty(workdirRaw))
        {
            Set(resp, "status", "WORKDIR_REQUIRED");
            return resp;
        }

        string canonical;
        try { canonical = Path.GetFullPath(workdirRaw); }
        catch { Set(resp, "status", "WORKDIR_INVALID"); return resp; }
        Set(resp, "workdir", canonical);

        if (Path.GetPathRoot(canonical).Equals(canonical, StringComparison.OrdinalIgnoreCase))
        {
            Set(resp, "status", "DISK_ROOT_REFUSED");
            return resp;
        }

        using (var lockHandle = AcquireGrantsLock())
        {
            if (lockHandle == null)
            {
                Set(resp, "status", "LEDGER_LOCK_TIMEOUT");
                return resp;
            }

            string reconErr;
            if (!ReconcileInterruptedTxn(out reconErr))
            {
                Set(resp, "status", "INCONSISTENT_GRANT_STATE");
                Set(resp, "error", reconErr);
                return resp;
            }

            var loadRes = LoadGrantLedgerSafe();
            if (loadRes.Status == LedgerStatus.ReadFailed)
            {
                Set(resp, "status", "LEDGER_READ_FAILED");
                Set(resp, "error", loadRes.Error);
                return resp;
            }
            if (loadRes.Status == LedgerStatus.Corrupted)
            {
                Set(resp, "status", "LEDGER_CORRUPTED");
                Set(resp, "error", loadRes.Error);
                return resp;
            }

            var all = loadRes.Rows;
            var sid = new SecurityIdentifier(targetSid);
            int revoked = 0, preserved = 0, failed = 0, ownershipUnproven = 0, unverifiedOwnership = 0;
            var keep = new List<Dictionary<string, object>>();

            var targetList = new List<string> { canonical };
            if (!WriteTxnIntent("revoke", targetSid, canonical, false, "intent_logged", targetList))
            {
                Set(resp, "status", "TRANSACTION_INIT_FAILED");
                return resp;
            }

            bool matchedAny = false;
            bool done = false;
            foreach (var row in all)
            {
                bool isTarget = string.Equals(S(row, "target_sid"), targetSid, StringComparison.OrdinalIgnoreCase) &&
                                string.Equals(S(row, "workdir"), canonical, StringComparison.OrdinalIgnoreCase);

                if (!isTarget)
                {
                    keep.Add(row);
                    continue;
                }

                matchedAny = true;
                if (!object.Equals(Get(row, "created"), true))
                {
                    preserved++;
                    keep.Add(row);
                    continue;
                }

                string ownFp = null;
                try
                {
                    if (Directory.Exists(canonical))
                    {
                        AceOwnership own = CheckAceOwnership(row, canonical, sid, out ownFp);
                        if (own == AceOwnership.ForeignReplacement)
                        {
                            ownershipUnproven++;
                            failed++;
                        }
                        else
                        {
                            var info = new DirectoryInfo(canonical);
                            DirectorySecurity sec = info.GetAccessControl();
                            var exact = new List<FileSystemAccessRule>();
                            foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, false, typeof(SecurityIdentifier)))
                            {
                                if (IsExactHelperRule(rule, sid)) exact.Add(rule);
                            }
                            foreach (FileSystemAccessRule rule in exact) sec.RemoveAccessRuleSpecific(rule);
                            if (exact.Count > 0) info.SetAccessControl(sec);

                            DirectorySecurity after = Directory.GetAccessControl(canonical);
                            if (!HasExactHelperRule(after, sid))
                            {
                                revoked += exact.Count;
                                if (own == AceOwnership.LegacyUnverified) unverifiedOwnership += exact.Count;
                                done = true;
                            }
                            else
                            {
                                failed++;
                            }
                        }
                    }
                    else
                    {
                        done = true;
                    }
                }
                catch { failed++; }

                if (!done) keep.Add(row);
            }

            if (!matchedAny && Directory.Exists(canonical))
            {
                try
                {
                    var info = new DirectoryInfo(canonical);
                    DirectorySecurity sec = info.GetAccessControl();
                    var exact = new List<FileSystemAccessRule>();
                    foreach (FileSystemAccessRule rule in sec.GetAccessRules(true, false, typeof(SecurityIdentifier)))
                    {
                        if (IsExactHelperRule(rule, sid)) exact.Add(rule);
                    }
                    if (exact.Count > 0)
                    {
                        ownershipUnproven++;
                        failed++;
                    }
                    else
                    {
                        done = true;
                    }
                }
                catch { failed++; }
            }

            string saveErr;
            if (!SaveGrantLedgerSafe(keep, out saveErr))
            {
                Set(resp, "status", "LEDGER_SAVE_FAILED");
                Set(resp, "error", saveErr);
                Set(resp, "revoked_rules", revoked);
                Set(resp, "preserved_rules", preserved);
                Set(resp, "failed_rules", failed);
                Set(resp, "ownership_unproven_rules", ownershipUnproven);
                Set(resp, "unverified_ownership_rules", unverifiedOwnership);
                return resp;
            }

            string revokeJournalErr;
            if (!RemoveTxnIntent(out revokeJournalErr))
            {
                Set(resp, "status", "JOURNAL_CLEANUP_FAILED");
                Set(resp, "error", revokeJournalErr);
                Set(resp, "revoked_rules", revoked);
                Set(resp, "preserved_rules", preserved);
                Set(resp, "failed_rules", failed);
                Set(resp, "ownership_unproven_rules", ownershipUnproven);
                Set(resp, "unverified_ownership_rules", unverifiedOwnership);
                return resp;
            }

            if (failed > 0)
            {
                if (ownershipUnproven > 0)
                    Set(resp, "status", "OWNERSHIP_UNPROVEN");
                else
                    Set(resp, "status", "REVOCATION_FAILED");
                Set(resp, "revoked_rules", revoked);
                Set(resp, "preserved_rules", preserved);
                Set(resp, "failed_rules", failed);
                Set(resp, "ownership_unproven_rules", ownershipUnproven);
                Set(resp, "unverified_ownership_rules", unverifiedOwnership);
                return resp;
            }

            Set(resp, "status", revoked > 0 || done ? "revoked" : "already_revoked");
            Set(resp, "revoked_rules", revoked);
            Set(resp, "preserved_rules", preserved);
            Set(resp, "failed_rules", failed);
            Set(resp, "ownership_unproven_rules", ownershipUnproven);
            Set(resp, "unverified_ownership_rules", unverifiedOwnership);
            LogHelper("managed_workdir_revoke_path_access revoked target_sid=" + targetSid + " workdir=" + canonical + " count=" + revoked);
            return resp;
        }
    }

    // ------------------------------------------------------------- service control
    public static int InstallService(string operatorSid)
    {
        string exe = Assembly.GetExecutingAssembly().Location;
        try
        {
            if (string.IsNullOrEmpty(operatorSid) || !operatorSid.StartsWith("S-1-5-"))
            {
                Console.Error.WriteLine("InstallService error: no valid operator SID provided");
                return 1;
            }
            try
            {
                new SecurityIdentifier(operatorSid);
            }
            catch
            {
                Console.Error.WriteLine("InstallService error: operator SID is not a valid security identifier: " + operatorSid);
                return 1;
            }

            if (!SaveAuthorizedOperatorSid(operatorSid))
            {
                Console.Error.WriteLine("InstallService error: failed to persist and verify operator configuration");
                return 1;
            }

            var psi = new ProcessStartInfo("sc.exe", "create " + ServiceName + " binPath= \"" + exe + "\" start= auto DisplayName= \"SAI Accounts Helper Service\"")
            {
                UseShellExecute = false,
                CreateNoWindow = true
            };
            var p = Process.Start(psi);
            p.WaitForExit(10000);
            if (p.ExitCode != 0 && p.ExitCode != 1073) // 1073 = ERROR_SERVICE_EXISTS
            {
                return p.ExitCode;
            }

            // Start the service
            psi = new ProcessStartInfo("sc.exe", "start " + ServiceName)
            {
                UseShellExecute = false,
                CreateNoWindow = true
            };
            p = Process.Start(psi);
            p.WaitForExit(10000);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("InstallService error: " + ex.Message);
            return 1;
        }
    }

    public static int UninstallService()
    {
        try
        {
            var psi = new ProcessStartInfo("sc.exe", "stop " + ServiceName)
            {
                UseShellExecute = false,
                CreateNoWindow = true
            };
            var p = Process.Start(psi);
            p.WaitForExit(10000);

            psi = new ProcessStartInfo("sc.exe", "delete " + ServiceName)
            {
                UseShellExecute = false,
                CreateNoWindow = true
            };
            p = Process.Start(psi);
            p.WaitForExit(10000);
            return p.ExitCode;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("UninstallService error: " + ex.Message);
            return 1;
        }
    }

    // ------------------------------------------------------------- Entry point
    public static int Main(string[] args)
    {
        if (args.Length > 0)
        {
            string cmd = args[0].ToLowerInvariant();
            // The in-session launcher. Started by the helper inside the operator's
            // interactive session; it takes nothing but the handoff pipe name, so it
            // cannot be turned into a general process launcher.
            if (cmd == "--in-session-spawn")
            {
                string spawnPipe = null;
                for (int i = 1; i < args.Length; i++)
                    if (args[i] == "--pipe" && i + 1 < args.Length) spawnPipe = args[i + 1];
                if (string.IsNullOrEmpty(spawnPipe)) return 2;
                return RunInSessionSpawn(spawnPipe);
            }
            if (cmd == "--install")
            {
                string operatorSid = null;
                for (int i = 1; i < args.Length; i++)
                {
                    if (args[i] == "--operator-sid" && i + 1 < args.Length)
                    {
                        operatorSid = args[i + 1];
                    }
                }
                return InstallService(operatorSid);
            }
            if (cmd == "--uninstall")
            {
                return UninstallService();
            }
            if (cmd == "--run")
            {
                string pipe = null;
                for (int i = 1; i < args.Length; i++)
                {
                    if (args[i] == "--pipe" && i + 1 < args.Length) pipe = args[i + 1];
                }
                Console.WriteLine("Running SAI Accounts Helper on pipe " + (pipe ?? PipeName()) + "...");
                RunServer(pipe, null);
                return 0;
            }
            if (cmd == "--test-seal")
            {
                if (!IsTestMode())
                {
                    Console.Error.WriteLine("Error: test hooks disabled in production");
                    return 1;
                }
                if (args.Length < 3) return 1;
                string sid = args[1]; string secret = args[2];
                bool ok = SaveSealedSecret(sid, secret);
                Console.WriteLine(ok ? "SEAL_OK" : "SEAL_FAIL");
                return ok ? 0 : 1;
            }
            if (cmd == "--test-unseal")
            {
                if (!IsTestMode())
                {
                    Console.Error.WriteLine("Error: test hooks disabled in production");
                    return 1;
                }
                if (args.Length < 2) return 1;
                string sid = args[1];
                string s = ReadSealedSecret(sid);
                Console.WriteLine(s != null ? "UNSEAL_OK:" + s.Length : "UNSEAL_FAIL");
                return s != null ? 0 : 1;
            }
        }

        // If not started with flags and in non-interactive environment, run as service
        if (!Environment.UserInteractive)
        {
            ServiceBase.Run(new SaiAccountsHelperService());
            return 0;
        }

        // Running in console directly
        Console.WriteLine("SAI Accounts Helper (service host and CLI)");
        Console.WriteLine("usage: sai-accounts-helper.exe [--run [--pipe <name>]] [--install] [--uninstall]");
        return 0;
    }
}
