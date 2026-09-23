# SAIPATCH

Standalone patch-management subtool for the locally installed OpenCode client.
Owned by SAITULS, shipped under `Scripts\saipatch\`, launched from SAITULS's
Tools tab (**OpenCode Patcher**). No SAICONT dependency, no network, no
telemetry, no credentials anywhere.

## What it is

`Scripts\saipatch\patcher.ps1` is a generic, patch-agnostic engine. A patch is a
directory under `patches\` with:

| file | role |
|---|---|
| `manifest.json` | id, supported OpenCode versions, authoritative file set, queue bounds |
| `probe.ps1` | read-only compatibility check; exit codes 0/3/4/5 = AVAILABLE/INSTALLED/NEEDS_REAPPLY/drifted |
| `apply.ps1` | `-Plan` prints the target/hash contract, `-Stage` writes staged copies |
| `verify.ps1` | post-apply proof that installed bytes match the contract |
| `restore.ps1` | removes exactly the files the patch created, only if they are still ours |

The first patch is **opencode-queue-mode**: safe FIFO follow-up messages for a
busy OpenCode session, delivered as an OpenCode server plugin (`index.mjs` +
`queue.mjs`) staged into a dedicated plugin directory. No OpenCode binary or
source file is modified.

## Patch states

`NOT_INSTALLED AVAILABLE INSTALLED NEEDS_REAPPLY UNSUPPORTED_VERSION
SOURCE_DRIFTED OPEN_CODE_RUNNING BROKEN_PATCH RESTORABLE`. An unknown
installation is never collapsed into "not installed"; an unknown OpenCode
version is never patched.

## Transactional apply

Plan → backup → stage → verify staged hashes → commit → post-verify. Any
failure rolls every touched file back; nothing half-applied survives. Backups
and state live outside the OpenCode package, in
`%LOCALAPPDATA%\SAITULS\SAIPATCH\` (state.json + per-version backups). Only
non-secret metadata is persisted: ids, versions, paths, hashes, timestamps.

## After an OpenCode update

The patch targets the *contract* (plugin hooks + SDK endpoints), proven from the
installed `@opencode-ai` artifacts. It also verifies that the supported
OpenCode binary retains its provider-scoped `x-opencode-session` header sourced
from the existing logical `sessionID`; SAIPATCH does not create another ID or
modify the binary. A new OpenCode version is reported as
`NEEDS_REAPPLY` or `UNSUPPORTED_VERSION` after a probe — never mutated
automatically. Remove and re-apply explicitly once the new version passes the
probe.

## Queue semantics

- A submit on an **idle** session is untouched — normal OpenCode behavior.
- A submit on a **busy** session is captured whole (message + parts, agent,
  model) and the active turn is not interrupted.
- Each authoritative completion (`session.idle`) dispatches exactly ONE head;
  duplicate idle events dispatch nothing while one is in flight.
- Retry/error/unknown states and aborts freeze the queue; failed sends stay
  `FAILED` (visible, requeueable), never silently retried or dropped.
- Bound: 8 per session, enforced visibly with an error on overflow.
- v1 keeps the queue in memory only: nothing fires after a restart.

## Limitations

- Supported OpenCode versions: those listed in the patch manifest (currently
  1.18.27 and 1.18.29).
- Live acceptance against a running real installation was not performed (the
  verification environment *was* that running instance); the deterministic
  fake-home harness is the executed substitute.
- The TUI indicator for queued depth is provided by the plugin store
  (`__queue.depth(sessionID)`); deeper composer-integrated UI would need
  OpenCode TUI hooks beyond the verified contract.

## Complete removal

SAITULS Tools → OpenCode Patcher → `R` (Restore). Or:
`powershell -File Scripts\saipatch\patcher.ps1 -Command Restore`. Restore
removes the staged plugin files, prunes the now-empty directory, clears the
state entry, and leaves backups for audit.
