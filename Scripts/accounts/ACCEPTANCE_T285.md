# T-285 acceptance — bounded journal persistence correction

Recorded 2026-10-14, on the operator's machine, against the working tree.
Source request: SRC-103 — "T-282 bounded persistence correction: atomic journal
update mechanism + fault-injection acceptance; operator accepted power-loss
ceiling". The verify criterion is that the requested change is present and
demonstrated against the user's own description of it, which is what this file
records. Nothing here creates, removes or accesses a Windows account: all
harnesses run against a disposable `SAI_ACCOUNTS_ROOT` plane with fabricated
SIDs.

## 1 — Atomic journal update mechanism: PRESENT

`Scripts/accounts/SaiAccountsHelper.cs`:

- `PublishTxnJournal` (line 2116) is the ONLY publish path for
  `grants.txn.json`. The journal file is never opened for writing: the complete
  updated record is staged beside it as `grants.txn.json.tmp.<nonce>`, flushed
  durably (`WriteTextDurable`), re-checked against the exact raw generation
  read when authority was acquired, and only then published via
  `File.Replace` (existing journal) or `File.Move` (new journal). Every
  attempt's stage is cleaned in `finally`.
- `UpdateTxnJournal` (line 2279) is the single journal-update mechanism: an
  EXISTING journal is evolved, never rewritten. Transaction identity
  (`txn_id`, `op`, `target_sid`, `workdir`, `created`, `target_workdirs`) and
  every witness (`pre_mutation_acl_fp`, `post_write_acl_fp`,
  `post_mutation_acl_fp`) carry forward; a new witness that disagrees with the
  recorded one refuses the update.
- Failure bookkeeping (`MarkTxnIntentState`, `MarkTxnIntentFailed`) stamps the
  failing state onto the retained journal through the same mechanism.

This eliminates the defect class of in-place journal truncation: a crash or
error mid-write can no longer leave `grants.txn.json` half-written, which
previously destroyed the only durable proof of a pending grant/revoke
intention and forced manual reconciliation.

## 2 — Fault-injection acceptance: DEMONSTRATED

Test seam: `SAI_ACCOUNTS_JOURNAL_FAULT` names a publish stage
(`pre-stage`, `stage-write`, `post-flush`, `replace`), optionally qualified
with a publish ordinal (`replace@2`), and the named stage fails exactly as a
real failure would. `SAI_ACCOUNTS_JOURNAL_CHECKPOINT` (test mode + disposable
temp root only) freezes a disposable helper process at a verified stage for
process-kill tests. Neither variable is set in any production path.

| Suite | Result | Classification |
|---|---|---|
| `python tests/test_accounts_journal_atomicity.py` | 50/50 PASS | SIMULATED_PASS (fault seam) + WINDOWS_DISPOSABLE_PASS (real read-only destination failures) |
| `python tests/test_accounts_journal_authority.py` | 59/59 PASS | WINDOWS_DISPOSABLE_PASS |
| `python tests/test_accounts_journal_crash.py` | 97/97 PASS | PROCESS_TERMINATION_PASS (real kills of Popen-owned PIDs) |

Total: 206/206 PASS, 0 FAIL, 0 SKIP, run 2026-10-14 on this machine.

Assertions exercised per stage: a valid earlier journal is not truncated in
place; `txn_id` stays stable across state transitions; previously recorded
witnesses survive; no false successful completion is reported when journal
persistence failed; recovery either repeats cleanly or fails closed with
retained evidence.

## 3 — Power-loss ceiling: CLASSIFIED, NOT CLAIMED, ACCEPTED

The published guarantees cover process-crash recovery, staged-write failures
and publication-time refusals. They do NOT cover physical power-loss
durability — whether the OS honours an atomic replace through an actual power
cut is outside what any of these harnesses can force. That ceiling is
classified, recorded in `Scripts/accounts/SAI_ACCOUNTS.md`
("Transaction journal" section) and accepted as-is per the source request. No
harness claims otherwise.

## 4 — Publication state

Publication is disabled for this work (`mode: no-publish` in
`.saipen/STATE.md`). No push, no deploy, no release artifact. The mechanism and
suites above live in the working tree.
