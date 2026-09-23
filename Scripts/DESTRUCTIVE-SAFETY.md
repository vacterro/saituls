# Permanent deletion safety

## DEL JUNK ALL DISKS (--all-disks)

The default `--all-disks` run is **streaming and unattended-safe**:

- `SAFE_DISK` only; `AGGRESSIVE_PROJECT` is unreachable from the batch path.
- Eligible disks are local fixed writable volumes (network, removable, optical
  and RAM disks excluded, each with a recorded reason).
- **Sequence:** journal destination self-test -> runtime destructive gate
  (`all_disks_destructive_ready()`, ten named clauses proven on throwaway
  fixtures in-process) -> previous-run inspection -> ONE explicit start
  confirmation -> durable `RUN_START` -> per-drive `DRIVE_START`/`DRIVE_END`
  -> durable `RUN_END` + atomic `stream-*.summary.json`.
- The confirmation shows eligible roots (system drive marked), skipped-disk
  count, gate verdict, journal destination, and states that deletion starts
  immediately and continues unattended. Cancel = zero deletions, zero INTENT.
- A blocked gate means zero deletion with no override; the process exits 3.
- Every candidate: witness -> durable fsynced `INTENT` (with a stable
  classification reason such as `dir:gpucache` / `file:.pyc` /
  `file:aged-.part`) -> delete -> durable outcome. Journal loss before or
  after deletion fails closed (`JOURNAL_UNAVAILABLE`), and a durable FAILED
  outcome from an unexplained delete I/O error stops the whole run
  (`IO_FAILURE_STOP`) instead of marching through thousands more candidates.
- Scan-only permission failures are recorded as `SKIPPED_UNREADABLE` and do
  not stop the run, but they keep the drive from reporting CLEANED.
- Crash windows are truthful: an `INTENT` without a later terminal outcome is
  reported by `dangling_intents()` as `UNKNOWN_AFTER_INTENT` (a crash
  witness), never reclassified as a deletion from path absence; a torn final
  journal line is a `TRUNCATED_RECORD` warning that never fabricates
  completion.
- Cancellation (Cancel button or closing the progress window) stops before
  the next candidate, records `CANCELLED`, and the run reports PARTIAL with
  exit code 2 when anything was already deleted. Cancellation is not
  rollback; deletion is permanent and the Recycle Bin is not used.
- Exit codes: 0 completed/nothing to do, 1 cancelled before mutation,
  2 fatal/partial destructive run, 3 safety gate blocked.
- `%LOCALAPPDATA%\SAITULS\DEL_JUNK` (journals, summaries, manifests) and
  `%TEMP%\SAITULS\DEL_JUNK\locks` are the worker's own audit evidence and are
  refused as candidates (`is_own_evidence`).
- There is no `--yes` / `--force` / `--unsafe` / `--skip-gate` bypass in the
  product CLI. `--preview` is scan/report-only: it never deletes, never
  writes INTENT, and never invokes the destructive candidate path.

Regression evidence: `python tests/test_del_junk_all_disks.py` prints
`ALL_DISKS_UNATTENDED_READY = TRUE` only when the full DEL JUNK matrix passes.

## DEL DUP / DEL SAME / DEL EMPTY

DEL DUP performs its final comparison with both files held open. The handles
deny concurrent writes and deletion/renaming. The keeper must still match the
original digest, and the candidate must have a different stable identity and
the same digest. Deletion uses `SetFileInformationByHandle(FileDispositionInfo)`
on the verified candidate handle. There is no final `os.remove(path)` window.

The current identity implementation supports local NTFS only. Remote paths,
unavailable/nonzero-unproven file IDs, other filesystems, reparse files,
directories, changed content, and sharing failures are skipped. No pathname
deletion fallback is used. ReFS support requires a separate 128-bit file-ID
implementation and acceptance evidence.

DEL SAME refuses directory reparse points, including a selected root or an
ancestor containing one. Windows directory handles pin ancestors during
enumeration and moves. Every mutation is confined to the selected root.
Moves use same-volume rename, without a recursive copy fallback. Empty shells
are removed with `rmdir`; a late-arriving file prevents removal. Skipped paths
and errors are shown to the user.

Run `python tests/test_destructive_identity.py` on Windows for nine disposable
live filesystem checks, including a junction to external content and a real
replacement attempt after both final digests and before disposition. The old
hash/close/unlink sequence is retained only as a negative-control test.
`python tests/test_workers.py` covers existing confirmation, collision,
confinement, and worker behavior.

API contract: [Microsoft SetFileInformationByHandle documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle).
