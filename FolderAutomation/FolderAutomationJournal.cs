using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

// Folder Automation — durable transaction journal and undo.
//
// Append-only, one JSON object per line, folded by OpId on read. A crash
// between "about to move" and "moved" leaves a pending record that startup
// can see, which is the whole point: an operation whose outcome is unknown is
// never silently repeated.

namespace Saituls.FolderAutomation
{
    public sealed class FaJournalEntry
    {
        public string OpId = "";
        public string Timestamp = "";
        public string ProfileId = "";
        public string Profile = "";
        public string RuleId = "";
        public string Rule = "";
        public string Source = "";
        public string Destination = "";
        public string Action = "";
        public string Result = "";         // pending | completed | failed | dry-run
        public string Undo = "";           // MoveBack | RenameBack | DeleteCopy | NotReversible | None
        public string UndoState = "";      // none | available | undone | failed | ambiguous
        public string OriginalDir = "";
        public string OriginalName = "";
        public string DisplacedPath = "";
        public long Bytes;
        public string Error = "";
        public bool DryRun;

        public bool Reversible
        {
            get { return UndoState == "available" && (Undo == "MoveBack" || Undo == "RenameBack" || Undo == "DeleteCopy"); }
        }
    }

    public sealed class FaUndoResult
    {
        public bool Ok;
        public string Message = "";
        public FaJournalEntry Entry;
    }

    public sealed class FaJournal
    {
        public const int KeepFiles = 12;
        public const int MaxLinesPerFile = 20000;

        readonly string Dir;
        readonly object Gate = new object();
        readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        int _linesInCurrentFile;

        public FaJournal(string dir)
        {
            Dir = dir;
            Json.MaxJsonLength = 4 * 1024 * 1024;
            FaPaths.EnsureDir(Dir);
            _linesInCurrentFile = CountLines(CurrentFile());
        }

        public string CurrentFile()
        {
            return Path.Combine(Dir, "journal-" + DateTime.Now.ToString("yyyy-MM", CultureInfo.InvariantCulture) + ".jsonl");
        }

        public static int CountLines(string file)
        {
            if (!File.Exists(file)) return 0;
            int n = 0;
            try
            {
                using (StreamReader sr = new StreamReader(file))
                    while (sr.ReadLine() != null) n++;
            }
            catch { }
            return n;
        }

        public bool Append(FaJournalEntry e)
        {
            if (e == null) return false;
            lock (Gate)
            {
                try
                {
                    if (_linesInCurrentFile >= MaxLinesPerFile) { Roll(); }
                    File.AppendAllText(CurrentFile(), Json.Serialize(e) + "\r\n", new UTF8Encoding(false));
                    _linesInCurrentFile++;
                    return true;
                }
                catch
                {
                    return false;
                }
            }
        }

        void Roll()
        {
            string f = CurrentFile();
            if (File.Exists(f))
            {
                string baseMonth = DateTime.Now.ToString("yyyy-MM", CultureInfo.InvariantCulture);
                int seq = 1;
                string next;
                do
                {
                    next = Path.Combine(Dir, string.Format("journal-{0}-{1:D6}.jsonl", baseMonth, seq));
                    seq++;
                } while (File.Exists(next));

                File.Move(f, next);
            }
            _linesInCurrentFile = 0;
            Prune();
        }

        // History is bounded by construction: the newest KeepFiles segments are
        // kept and the rest are deleted once a new segment starts.
        public void Prune()
        {
            try
            {
                string[] files = Directory.GetFiles(Dir, "journal-*.jsonl");
                if (files.Length <= KeepFiles) return;
                Array.Sort(files, StringComparer.OrdinalIgnoreCase);
                for (int i = 0; i < files.Length - KeepFiles; i++) File.Delete(files[i]);
            }
            catch { }
        }

        // Newest first, folded so a later update of an OpId wins.
        public List<FaJournalEntry> Recent(int limit)
        {
            List<FaJournalEntry> outp = new List<FaJournalEntry>();
            Dictionary<string, FaJournalEntry> byOp = new Dictionary<string, FaJournalEntry>(StringComparer.Ordinal);
            List<string> order = new List<string>();
            string[] files;
            try { files = Directory.GetFiles(Dir, "journal-*.jsonl"); }
            catch { return outp; }
            Array.Sort(files, StringComparer.OrdinalIgnoreCase);
            for (int fi = files.Length - 1; fi >= 0; fi--)
            {
                string[] lines;
                try { lines = File.ReadAllLines(files[fi]); }
                catch { continue; }
                for (int i = lines.Length - 1; i >= 0; i--)
                {
                    if (lines[i].Length == 0) continue;
                    FaJournalEntry e;
                    try { e = Json.Deserialize<FaJournalEntry>(lines[i]); }
                    catch { continue; }
                    if (e == null || string.IsNullOrEmpty(e.OpId)) continue;
                    if (byOp.ContainsKey(e.OpId)) continue;
                    byOp[e.OpId] = e;
                    order.Add(e.OpId);
                    if (limit > 0 && order.Count >= limit) return Finish(byOp, order, limit);
                }
            }
            return Finish(byOp, order, limit);
        }

        static List<FaJournalEntry> Finish(Dictionary<string, FaJournalEntry> byOp, List<string> order, int limit = 0)
        {
            List<FaJournalEntry> outp = new List<FaJournalEntry>();
            foreach (string id in order)
            {
                outp.Add(byOp[id]);
                if (limit > 0 && outp.Count >= limit) break;
            }
            return outp;
        }

        // Any record still "pending" means a process died mid-operation.
        public List<FaJournalEntry> Ambiguous()
        {
            List<FaJournalEntry> outp = new List<FaJournalEntry>();
            foreach (FaJournalEntry e in Recent(0))
                if (e.Result == "pending") outp.Add(e);
            return outp;
        }

        public void MarkAmbiguous(FaJournalEntry e)
        {
            FaJournalEntry u = new FaJournalEntry();
            u.OpId = e.OpId; u.Timestamp = e.Timestamp; u.ProfileId = e.ProfileId;
            u.Profile = e.Profile; u.Rule = e.Rule; u.Source = e.Source;
            u.Destination = e.Destination; u.Action = e.Action;
            u.Result = "ambiguous"; u.Undo = "None"; u.UndoState = "ambiguous";
            u.Error = "process ended while this operation was in flight";
            Append(u);
        }

        public FaUndoResult Undo(IFileSystem fs, string opId)
        {
            FaUndoResult r = new FaUndoResult();
            FaJournalEntry target = null;
            foreach (FaJournalEntry e in Recent(0))
            {
                if (opId == null || string.Equals(e.OpId, opId, StringComparison.Ordinal))
                {
                    if (e.Result == "completed" && e.Reversible) { target = e; break; }
                    if (e.Result != "dry-run")
                    {
                        r.Message = "not reversible: " + Describe(e);
                        r.Entry = e;
                        return r;
                    }
                }
            }
            if (target == null) { r.Message = "nothing to undo"; return r; }
            r.Entry = target;

            try
            {
                string reason = null;
                switch (target.Undo)
                {
                    case "MoveBack":
                        if (!fs.DirectoryExists(target.OriginalDir ?? ""))
                            reason = "the original folder no longer exists";
                        else if (fs.FileExists(Path.Combine(target.OriginalDir, target.OriginalName ?? "")))
                            reason = "a file with the original name is already there";
                        else { fs.CreateDirectory(target.OriginalDir); fs.Move(target.Destination, Path.Combine(target.OriginalDir, target.OriginalName)); }
                        break;
                    case "RenameBack":
                        if (!fs.FileExists(target.Destination))
                            reason = "the renamed file no longer exists";
                        else if (fs.FileExists(target.Source))
                            reason = "a file with the original name is already there";
                        else fs.Move(target.Destination, target.Source);
                        break;
                    case "DeleteCopy":
                        if (!fs.FileExists(target.Destination)) reason = "the copy no longer exists";
                        else fs.Delete(target.Destination);
                        break;
                    default:
                        reason = "this action has no reverse operation";
                        break;
                }
                FaJournalEntry u = new FaJournalEntry();
                u.OpId = target.OpId; u.Timestamp = Now(); u.ProfileId = target.ProfileId;
                u.Profile = target.Profile; u.Rule = target.Rule;
                u.Source = target.Source; u.Destination = target.Destination;
                u.Action = target.Action; u.Result = "completed"; u.Undo = target.Undo;
                u.UndoState = reason == null ? "undone" : "failed";
                u.Error = reason ?? "";
                u.Bytes = -target.Bytes;
                Append(u);
                r.Ok = reason == null;
                r.Message = reason ?? ("undone: " + Describe(target));
                return r;
            }
            catch (Exception ex)
            {
                r.Ok = false;
                r.Message = "undo failed: " + ex.Message;
                return r;
            }
        }

        static string Describe(FaJournalEntry e)
        {
            return e.Action + " " + Path.GetFileName(e.Source) +
                (string.IsNullOrEmpty(e.Error) ? "" : " (" + e.Error + ")");
        }

        public static string Now()
        {
            return DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss.fff", CultureInfo.InvariantCulture);
        }
    }
}