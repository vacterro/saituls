# SAI Accounts — the account control plane

SAI Accounts is one shared subsystem that owns **which AI vendor accounts exist**
for this machine's operator and **how commands run on their behalf**. SAITULS is
the human-facing Account Manager; every other SAI application (LIMISAW,
FastPrompter, ZAICODE, AUDAPACK, and future ones) is a consumer that reads the
same registry through the same CLI. Consumers keep working when SAITULS.exe is
closed: the registry and the brokers are independent of the GUI.

Source lives here, in SAITULS, under `Scripts/accounts/`:

| File | Role |
|------|------|
| `SaiAccounts.cs` | single-file C# implementation (CLI + broker) |
| `build.ps1` | compiles `sai-accounts.exe` with the Windows csc |
| `README.md` | operator-facing quick start |

Installed layout (documented Windows locations, never a checkout path):

```
C:\ProgramData\SAI\Accounts\
    bin\sai-accounts.exe          vendor-neutral CLI + broker host
    bin\agy.exe                   shared read/execute Antigravity CLI copy
    scopes\<scope>\accounts.json  THE canonical registry (one per operator)
    scopes\<scope>\events.log     control-plane audit log (JSONL)
    scopes\<scope>\bin-state.json recorded shared-binary version + hash
```

## One source of truth

`C:\ProgramData\SAI\Accounts\scopes\<scope>\accounts.json` is the only canonical
account registry on the machine. `<scope>` is `op-<fingerprint>` where the
fingerprint is `sha256(operator Windows SID)[:16]`. The full SID lives inside
the registry under `operator_scope.operator_sid`; the directory name itself
carries no reversible identity.

Two historical experimental files exist and are **migration inputs only**:

- `%LOCALAPPDATA%\SAI\accounts\antigravity-accounts.json`
- `C:\ProgramData\SAI\Antigravity\accounts.json`

`sai-accounts import` reads them (and existing Claude/Codex profile
directories), never writes them back, and the runtime never falls back to them.
The old `sai-antigravity.exe` is a deprecated compatibility shim forwarding to
`sai-accounts.exe`.

### Compatibility shim

`sai-antigravity.exe` accepts exactly one verb — `usage <account-ref>` — and
translates the canonical envelope into the legacy contract. Every outcome,
including every failure, is one JSON line on stdout; the shim never exits
silently, because a caller that gets no line at all cannot tell a wedged engine
from a dead shim.

| Legacy `status` | Meaning |
| --- | --- |
| `CONTEXT_OFFLINE` | no broker for that context (also `skipped_reason: CONTEXT_OFFLINE`) |
| `AUTH_REQUIRED` | context online, credential missing |
| `ok` + `windows[]` | quota payload |
| `SHIM_ENGINE_MISSING` | no engine resolved (non-zero exit) |
| `SHIM_ENGINE_TIMEOUT` | engine did not exit within its budget (non-zero exit) |
| `SHIM_BAD_PAYLOAD` | engine exited, but printed no parseable envelope |
| `SHIM_ENGINE_FAILED` | the engine could not be started |

The shim waits 120 s for the engine and enforces that deadline on the *process*,
not on the output streams. Both stdout and stderr are drained concurrently: an
undrained pipe fills at about 64 KB and would block an engine that writes
ordinary diagnostics, while the shim sat waiting for stdout to close — both
sides waiting on the other. On expiry the child is killed and the shim answers
`SHIM_ENGINE_TIMEOUT`. The budget can be lowered for tests via
`SAI_ACCOUNTS_SHIM_TIMEOUT_MS` (250–120000 ms; anything else is ignored).

### Registry ACL

Created by the operator (who becomes owner), with inheritance disabled and
exactly: operator SID FullControl, SYSTEM FullControl. No `Everyone`, no
`Authenticated Users`, no `BUILTIN\Users` write. The shared `bin\` grants
operator FullControl, SYSTEM FullControl, `BUILTIN\Users` ReadAndExecute
(secondary contexts must execute the broker, never replace it). `doctor`
verifies both.

## Schema

`sai.accounts/registry/2`:

```json
{
  "schema": "sai.accounts/registry/2",
  "generation": 14,
  "operator_scope": { "scope_id": "op-9f2a...", "operator_sid": "S-1-5-..." },
  "accounts": [ { "...AccountDescriptor..." } ]
}
```

AccountDescriptor — safe metadata only, **no credentials ever**:

| Field | Meaning |
|-------|---------|
| `account_id` | canonical identity, see below |
| `provider_id` | `antigravity` \| `claude` \| `codex` |
| `display_name` | alias, or `null` to use the generated default |
| `execution_backend` | `windows_user` \| `profile_directory` |
| `execution_context_id` | identity of the context the account runs in |
| `operational_state` | `ENABLED` \| `DISABLED` \| `FROZEN` \| `ARCHIVED` |
| `hidden` | presentation-only flag, independent of operational state |
| `created_at` / `updated_at` | ISO-8601 UTC |
| `provider_metadata` | backend-safe fields (`windows_sid`, `windows_user`, `profile_locator`, `env_scrub` …) |
| `tags` | free-form strings |
| `provider_ordinal` | stable 1-based per-provider ordinal persisted by the engine so presentation reordering never swaps generated labels |
| `configured_identity` | optional explicit provider binding (`fingerprint`, optional `account_label`, `bound_at`); never inferred or persisted by a passive probe |

Default labels (`Antigravity 1`, `Claude 2` for display; `AG1`, `CL2` compact) use the account's stable `provider_ordinal` (assigned on first creation/mutation in provider order), while `accounts[]` holds the canonical presentation order. Reordering `accounts[]` via `sai-accounts move` changes presentation order across SAITULS, Explorer, and the AI Console picker without changing `account_id`, `compact_label`, or default `display_name`. An explicit `display_name` (alias) overrides the default without touching `account_id`.

### Account identity

`account_id = provider_id ":" backend-segment ":" fingerprint(16 hex)`:

- `antigravity:windows-user:<sha256(lowercased SID)[:16]>`
- `claude:profile-dir:<sha256(lowercased canonical profile path)[:16]>`
- `codex:profile-dir:<sha256(lowercased canonical profile path)[:16]>`

Never an ordinal, never a display name, never an email. `execution_context_id`
is separate: `windows-user:<fingerprint(SID)>` for the Antigravity Windows-user
backend, `current-user` for profile-directory backends. The same physical
account therefore has exactly one ID across all five repositories.

### Codex per-account context window

A Codex account's context limit is a property of its `CODEX_HOME`
`config.toml`, never of the plane: the engine sets `CODEX_HOME` per launch
(`SaiAccounts.cs`, the claude/codex launch branch) and then never reads or
writes vendor config. Raising an account to the 1M context window is
therefore a per-home operator action:

```
model = "gpt-6.1-sol"
model_context_window = 1000000
model_auto_compact_token_limit = 900000
```

Keep the pre-change file as a backup (the live homes carry
`config.toml.bak-context-1m-<timestamp>`); Codex itself re-reads the file on
the next launch, so no plane restart is involved. The engine deliberately has
no verb for this: writing vendor config would make the control plane the
author of a setting it does not understand, and a future Codex schema change
would then be a registry migration instead of a one-file edit.

## Lifecycle and runtime state

`operational_state` is the lifecycle, `hidden` is presentation, and health is
reported separately by probes — three axes, never collapsed:

| Lifecycle | Auto-probe | Launch | Notes |
|-----------|-----------|--------|-------|
| `ENABLED` | yes | yes | normal account |
| `DISABLED` | no | no | stays registered; manual Verify from SAITULS still allowed |
| `FROZEN` | no | no | nothing touches it except explicit Account Manager actions |
| `ARCHIVED` | no | no | out of active lists; identity and history retained; reversible |

`hidden=true` hides an account from ordinary compact UIs while it stays fully
operational; the SAITULS Account Manager always shows it.

Runtime dimensions, and **which verb actually returns them** — they are not
interchangeable:

| Dimension | Values | Returned by |
| --- | --- | --- |
| `context_state` | `ONLINE` \| `OFFLINE` \| `ERROR` | `status`, `snapshot` |
| `auth_state` | `AUTHENTICATED` \| `AUTH_REQUIRED` \| `UNKNOWN` \| `ERROR` | `status`, `snapshot` |
| `quota_state` | `READY` \| `LOW` \| `EXHAUSTED` \| `STALE` \| `ERROR` \| `UNKNOWN` | `usage`, `snapshot` only |

`status` returns `context_state` and `auth_state` and **no `quota_state` key at
all** — an absent key, not `UNKNOWN`. Anything reading `quota_state` from a
`status` reply is reading nothing. `quota_state` costs a real quota probe
(`agy /usage`, up to 30 s per account), which is exactly why `status` does not
pay for it; `snapshot` probes all eligible accounts concurrently in batches of
four and is the verb to use when the whole picture is wanted.

### Provider identity (who is signed in)

An execution context says *where* an account runs. It does not say *who* is
signed in. Two Windows users can hold the same vendor account, and one Windows
user can hold a different one after a re-login, so `windows_user` /
`profile_locator` can never settle the question — and quota cannot settle it
either, because two distinct accounts legitimately both read 100%.

The broker therefore reports a **provider identity**, read from authenticated
provider metadata inside the account's own context, under
`credential.provider_identity`:

| Field | Meaning |
|---|---|
| `fingerprint` | 16 hex chars, SHA-256 of the vendor's stable subject id |
| `account_label` | human-readable address from the same verified token, when it carries one |
| `source` | `google_id_token_sub` \| `google_id_token_email` |
| `verified` | `true` only when a subject was actually read |

For Antigravity this is the `sub` claim of the stored Google `id_token`. It is
the provider's own account id: it survives token refresh, reconnect, broker
restart and reboot, and changes only when a different human signs in. That is
precisely the stability a fingerprint needs and precisely what a digest of the
token bytes cannot give — a token rotation would read as a new person.

`account_label` is the Google address from that **same** token, so it cannot
disagree with the fingerprint above it. It is a **label and nothing keys on
it**: `sub` stays the identity. That is deliberate, in both directions. Two
addresses can be one human, and a renamed address is still one human, so
deduplicating on the email would both miss real duplicates and invent false
ones. The label exists so a local operator UI can answer *which Google account
is this?* without opening a console and reading a fingerprint. It appears in
the Accounts panel and nowhere else — never in a routine helper or broker log,
and never in SAIPEN evidence unless a local acceptance result needs it.

A credential **timestamp is not an identity**. `credential_last_written` says
when a store was last written; it moves on refresh, on reconnect and on
re-login of the same human, and two slots on one Google account routinely carry
different ones. Nothing may treat it as evidence of who is signed in. The only
evidence is the verified `sub` digest.

`verified: false` is a normal, expected answer for an unauthenticated account,
and it means *unproven*, not *absent*: consumers must keep such an account in
its own private cache scope rather than merging it with another. Only two
accounts with the same verified `fingerprint` are one quota pool.

Onboarding a second slot therefore buys a second quota pool only when a
*second human* signs in. Two managed Windows users holding the same Google
login are one pool, and a consumer that dedups will collapse them to one —
that is the identity model working, not a fault. An account having "its own
credential" says nothing about whose it is; only the fingerprint does. Compare
them before believing a second slot is live:

```
sai-accounts status AG1
sai-accounts status AG2
```

Equal `fingerprint` under `credential.provider_identity` means one human.
`Scripts/accounts/AG2_ACCEPTANCE.PS1` section D asserts the difference, and
reports a failure rather than a pass when the two match. The live record of what
AG1 and AG2 actually hold on a real machine — and which acceptance steps were
run against it and which were staged for a human — is `ACCEPTANCE_T243.md`.

No token byte or subject string ever leaves the broker. The claim is hashed in
place and only the digest travels; the address travels only as the label above.

### Explicit binding and observed identity

This distinction prevents a runtime login observation from silently rewriting
the account slot's explicit configuration. `configured_identity` is the binding
written by `bind-identity`; `credential.provider_identity` is the independent
observation returned by a probe. Neither a matching email label nor a credential
timestamp proves that these fingerprints identify the same provider account.
Identity comparisons require the same provider and the same fingerprint;
runtime observations additionally require `verified: true`.

`bind-identity <selector> --fingerprint <16 hex characters> [--label <label>]`
normalizes the fingerprint to lowercase. Invalid fingerprints are refused with
`IDENTITY_INVALID` before any persisted change. With no explicit fingerprint,
the command binds only a verified observation in that account's own context.
`bind-identity <selector> --clear` removes the binding and cannot be combined
with fingerprint or label options.

A complete `status` or `usage` account descriptor that omits
`configured_identity` reports an unbound slot. Consumers must clear their cached
binding instead of resurrecting it from a previous reading. A partial legacy
observation without that descriptor is not evidence that the binding was
cleared. An unverified observation stays unproven and must not display a
fingerprint or address as an established runtime identity.

Remove/Archive, Logout, and Delete-local-context are three distinct actions.
Ordinary Remove never deletes a Windows user, a vendor account, or credentials.

## Logout

```
sai-accounts logout AG2
```

Drops the provider credential for **this account only**, from the Windows
credential store of whichever Windows user the broker is already running as.
It answers with the identity that was there (`identity_before`) so a caller can
prove the slot changed rather than merely lost something:

| `status` | Meaning |
|---|---|
| `logged_out` | the credential is gone (`credential_present: false`) |
| `LOGOUT_INCOMPLETE` | the store still holds it — the call did NOT do its job |
| `LOGOUT_FAILED` | `CredDeleteW` failed; `win32_error` says why |

This is measured, not assumed. Antigravity exposes no non-interactive sign-out:
`agy.exe --help` has no auth or logout verb, and a recursive scan of the
Antigravity profile for `gemini:antigravity`, `id_token`, `refresh_token`,
`oauth2` and `accounts.google` finds no auth material in any file. `cmdkey /list`
shows exactly one Antigravity target, `gemini:antigravity`. Deleting that one
target *is* the provider logout.

Narrow by construction, not by convention: the command takes a selector and
nothing else — no credential target, registry path, file path, executable or
command line — and the broker rejects any request carrying one
(`OPERATION_NOT_ALLOWED`, `logout_args`). There is deliberately **no generic
credential-delete API**. Because the broker already runs as the selected
account's own Windows user, "log AG2 out" cannot become "log AG1 out": there is
no identifier in the call for one to arrive on. No Windows user is ever deleted.

A caller must not take a logout at its word. Re-read `status` and continue only
on `credential_present: false` or `auth_state: AUTH_REQUIRED`; a
`LOGOUT_INCOMPLETE` means the old credential is still standing, and starting a
sign-in on top of it leaves the operator choosing between two logins while one
of them silently wins.

## Secure File Handoff (SAIHANDOFF)

```
sai-accounts handoff-file <selector> --source <path> [--workdir <path>]
sai-accounts verify-file <selector> --path <path>
```

Transfers an explicitly selected operator file (e.g., screenshot or reference asset) to a managed isolated Windows agent account without granting access to the operator's private parent directories.

- **Staging directory**: `<workdir>\.sai-handoff\<account_compact>`
- **Strict DACL isolation**: Inheritance is disabled (`isProtected: true, preserveInheritance: false`). The directory DACL grants `FullControl` only to the Operator and `NT AUTHORITY\SYSTEM`, and `Modify | Synchronize` only to the target account's SID. Foreign accounts and unprivileged users receive no access (`Access is denied`).
- **Atomic integrity**: Files are written to a unique `.partial.<guid>.tmp` file, validated against source byte count and SHA-256 hash, and atomically published via `File.Replace` / `File.Move`.
- **Git isolation**: Provisions `<workdir>\.sai-handoff\.gitignore` containing `*\n!.gitignore\n` so staged handoff files are never tracked by source control.
- **Cross-identity verification**: `verify-file` (and automatic handoff verification) queries the managed account's broker over named pipe using the `file_probe` command, proving readability under the agent's actual Windows token without executing code or exposing arbitrary file operations.

## Scoped Agent Access and Live File Relay (SAIHANDOFF modes)

Two operator-owned companions extend handoff beyond a one-time copy. Both live in `Scripts/accounts/` (`sai_relay.py`, `sai_access.py`), run as the operator, add no dependency, and keep control state in `%LOCALAPPDATA%\SAITULS\SAI_HANDOFF\` (override `SAI_HANDOFF_ROOT`) — a directory the managed account can neither read nor renew, so a lease cannot be extended by the agent that benefits from it.

- **Staging boundary reuse**: `handoff-stage` builds the same isolated DACL (Operator/SYSTEM FullControl, target SID `Modify | Synchronize`, inheritance off) without copying bytes, so a relay lands where the agent — and only the agent — can read.
- **Reuse, not reimplementation**: nothing inherits `grant-workdir`, no new broker verb, no second account registry. Identity comes from the canonical registry, and traversal is proven with one `probe-workdir` of the parent.

```
python Scripts/accounts/sai_relay.py start --account <selector> --source <path> --workdir <path> \
    [--staged-name <name>] [--interval 1.0] [--keep 2] [--lease-minutes 60|--session] \
    [--missing-grace 300] [--partial-flush 5]
python Scripts/accounts/sai_relay.py run|once|status|stop|list ...
```

The relay continuously publishes new bytes of a selected log into that staging directory. It reads by offset, distinguishes objects by `(st_dev, st_ino)` so an append is not mistaken for a replacement, and names every discontinuity in the staged output instead of closing a gap silently: `SOURCE_REPLACED`, `SOURCE_TRUNCATED`, `SOURCE_MISSING`, `SOURCE_LOST`, `SOURCE_RETURNED`, `SOURCE_DENIED`, `SOURCE_UNSAFE_OBJECT`, `PARTIAL_LINE_FLUSHED`, `RUNNER_RESTARTED`. A trailing partial line is published once the source goes quiet (`--partial-flush`), so the line a process was writing when it stopped still arrives. The primary staged path never changes name; history rotates aside (`--max-bytes`, `--keep`). Reparse points are refused, never followed. Start reports `agent_readable` only after the broker proves readability under the managed identity.

```
python Scripts/accounts/sai_access.py grant --account <selector> --source <path> --minutes 15|60|--session [--recursive]
python Scripts/accounts/sai_access.py revoke|status|sweep|list ...
```

For the case where staging is unsuitable, this grants one expiring, read-only ACE on one object the operator selected, using the platform tool (`icacls`) so every other ACE is preserved and no ownership is taken. It refuses instead of widening: a target whose parent the account cannot traverse is `ANCESTOR_NOT_TRAVERSABLE` (the staged relay is the right answer there), an ACE indistinguishable from one already present is `EQUIVALENT_ACE_ALREADY_PRESENT`, and `--recursive` is honoured only for a directory. Each lease records the exact ACL before and after; a revoke removes only granted ACEs (`/remove:g`) and reports `ROLLBACK_MISMATCH` with the evidence rather than smoothing it. Removing an ACE stops new access; a handle already open keeps reading until it closes, and that limit is reported, never hidden. `sweep` revokes only expired leases this journal provably owns.

## AUDAPACK integration contract

Any consumer that wants one of these three capabilities against a managed identity — an isolated agent that must observe a live log, receive a copied file, or read one operator-chosen object for a bounded time — uses the surface above and nothing else.

- **No consumer ever receives an operator filesystem tool.** The three verbs here take an explicit path from the operator, or from relay state the operator's own process wrote. A consumer cannot widen the scope by changing its own request: there is no browse, no list, no recursive grant of the operator's tree, and no verb that accepts a path chosen by an agent.
- **Identity, never privileges.** Selectors resolve through the canonical registry. A consumer that names an account it does not own gets `ACCOUNT_NOT_FOUND` or a `context_mismatch` refusal from the broker, never a grant.
- **Staged bytes are the only thing the agent sees.** For `handoff-file` and the relay, the agent's read path is `<workdir>\.sai-handoff\<compact>\...`, which carries its own protected DACL and breaks inheritance, so no foreign managed identity inherits anything. `verify-file`/`file_probe` exist so a consumer can prove readability under the real token rather than assuming success from a zero exit code.
- **Everything is revocable and reported.** Leases, stop and revoke are operator actions against operator-owned control state in `%LOCALAPPDATA%\SAITULS\SAI_HANDOFF\`. The managed account can neither read nor renew that state.
- **Consumers treat gap codes as data, never as instructions.** Nothing in staged content is executed, forwarded, or followed; a `SOURCE_REPLACED` or a reparse point in a referenced path is reported and refused, not resolved.
- **A consumer proves its own acceptance with the account token.** A mock or simulated pass is not acceptance for these paths: `verify-file` under the managed SID, an actual append observed in the staged copy, and an explicit non-target account refused.

## Provider adapters

| Capability | antigravity | claude | codex |
|------------|-------------|--------|-------|
| `supports_multi_account` | yes | yes | yes |
| `supports_interactive_login` | yes (vendor-native flow) | yes | yes |
| `supports_quota` | yes | no | no |
| `supports_launch` | yes | yes | yes |
| `supports_logout` | yes (own Windows credential store) | no | no |
| `supports_context_creation` | yes (Windows user) | yes (profile dir) | yes (profile dir) |

Claude/Codex quota probing stays with each consumer's own adapters for now; SAI
Accounts owns their **identity, lifecycle and launch**. Login happens only in
vendor-native official flows; SAI Accounts never stores or transports tokens.

## Execution backends

- `profile_directory` (claude, codex): the account is a dedicated config home
  (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`) inside the current Windows user. Launch
  spawns the vendor CLI with the child environment carrying the profile dir and
  — for non-primary codex homes — the inherited API-key variables scrubbed.
- `windows_user` (antigravity): the account is a dedicated Windows standard
  user. Commands for the current user run directly; commands for a secondary
  user run through that user's persistent broker (below).

## Broker model

Quota polling for a secondary Windows context must not prompt for a password on
every refresh, so each secondary context runs one persistent broker:

```
sai-accounts context start <account>   (once; runas asks for the target
                                        user's password locally, no /savecred,
                                        no /netonly, no stored password)
runas /profile /user:<user> "C:\ProgramData\SAI\Accounts\bin\sai-accounts.exe" --broker <sid> <scope>
```

The broker:

- refuses to start unless the current Windows SID equals the expected target
  SID (`context_mismatch`, fail closed);
- is single-instance per context within its Windows session (local mutex);
- serves exactly the allowlisted operations `status`, `usage`, `launch`,
  `auth`, `logout`, `shutdown` — any other command, raw command line, EXE path or script is
  `OPERATION_NOT_ALLOWED`;
- bounds request lines to 8 KiB and rejects malformed JSON safely;
- validates `launch` workdir: it must exist and be openable by the target user,
  otherwise `WORKDIR_UNAVAILABLE` — never a silent fallback to a profile or
  system directory;
- shapes `auth` and `logout` as account-only operations with no project,
  executable, script or arbitrary command argument;
- returns only sanitized data: credential **presence** and last-written
  filetime, quota windows, process ids. No blob bytes, no tokens.

Pipe name: `SAI.Accounts.<scope>.<context-fingerprint>`. Pipe ACL grants
exactly the target SID, the operator SID, and SYSTEM — no `BUILTIN\Users`. On
every connection the broker impersonates the client and verifies the connected
identity against that list before answering.

## Shared Antigravity binary

Secondary contexts cannot execute the operator's private `agy` install, so
`bin\agy.exe` is the one shared copy. `sai-accounts sync-binary [--source p]`
stages and atomically replaces it, recording version and SHA-256 in
`bin-state.json`; `doctor` reports drift between the recorded and actual hash.
Never one copy per account, never an update while a probe is mid-flight (the
registry mutation lock serializes syncs against probes).

## CLI

```
sai-accounts list [--provider p] [--all]      descriptors (no probing)
sai-accounts get <selector>
sai-accounts snapshot [--provider p] [--timeout ms]   concurrent probe of eligible accounts
sai-accounts status <selector>                context + auth, no quota
sai-accounts status --all                       every account's context + auth in one process (sai.accounts/status-all/1)
sai-accounts auth <selector>                  official sign-in flow for that account
sai-accounts logout <selector>               drop that account's provider credential
sai-accounts usage <selector>                 quota probe (respects lifecycle)
sai-accounts launch <selector> --workdir <path>
sai-accounts context start|stop <selector>
sai-accounts import [--dry-run]               migrate experimental registries + known profiles
sai-accounts rename <selector> "<alias>"|--
sai-accounts move <selector> up|down|before <target>|after <target>
sai-accounts bind-identity <selector> [--fingerprint <16-hex> [--label <label>]]
sai-accounts bind-identity <selector> --clear
sai-accounts set-state <selector> <ENABLED|DISABLED|FROZEN|ARCHIVED>
sai-accounts set-hidden <selector> <true|false>
sai-accounts archive <selector> / restore <selector>
sai-accounts grant-workdir <selector> --workdir <path>
sai-accounts handoff-file <selector> --source <path> [--workdir <path>]
sai-accounts handoff-stage <selector> --workdir <path>
sai-accounts probe-workdir <selector> --workdir <path>
sai-accounts verify-file <selector> --path <path>
sai-accounts sync-binary [--source <path>]
sai-accounts doctor
sai-accounts --broker <expected-sid> <scope>
```

A selector resolves first by canonical `account_id`, then by the generated
stable compact label. These selectors cannot be shadowed by a display alias.
Aliases, current display labels and legacy ids are conveniences: if more than
one account matches, every operation refuses `ACCOUNT_SELECTOR_AMBIGUOUS`,
returns the candidate canonical ids, and leaves state and order unchanged.
Choose an `account_id` or a compact label from `list --all` to resolve it.
Reordering must never break an ambiguity by selecting the first matching row.
Consumers pass canonical ids for operations; labels are presentation only.
Ordinals are never resolved by code — `AG4` works because it was generated,
not because a branch exists.

Every command prints one JSON object to stdout and exits 0 on success, 1 on
handled failure, 2 on `CONTEXT_OFFLINE`. Output schemas are namespaced and
versioned (`sai.accounts/list/1`, `sai.accounts/snapshot/1`, …). Consumers read
JSON only; they never see `CredReadW`, `runas` quoting, Google OAuth, Claude
config folders or Codex auth files.

## Change propagation, concurrency, audit

- Every successful mutation increments `generation`, rewrites via
  temp-file + atomic replace under a global mutation mutex, and appends
  `{timestamp, account_id, action, result, generation}` to `events.log`.
- Consumers cache the last `generation` they saw and reload when it changes
  (file mtime/generation check on their normal refresh path — no watcher
  dependency required).
- Readers never observe half-written JSON (atomic replace); a malformed single
  account record is reported, never auto-repaired or silently dropped.
- Telemetry and history in every consumer key by `account_id`, so rename, hide,
  disable, freeze and archive never lose history.

### Transaction journal (grant/revoke persistence)

The helper's grant/revoke work is journaled to `grants.txn.json`
(schema `sai.accounts.helper/txn/2`) as the only durable record of what was
INTENDED before a DACL is touched.

- The journal file is never opened for writing. The complete updated record is
  staged beside it, flushed durably, and only then published through an atomic
  replace (`PublishTxnJournal` in `SaiAccountsHelper.cs`). A failure at any
  point leaves the previous valid journal intact instead of truncating the
  evidence in place.
- Updates evolve an EXISTING journal (`UpdateTxnJournal`): identity
  (`txn_id`, `op`, `target_sid`, `workdir`, `created`, `target_workdirs`) and
  every recorded witness (`pre_mutation_acl_fp`, `post_write_acl_fp`,
  `post_mutation_acl_fp`) survive each state transition, and a caller-supplied
  witness that disagrees with the recorded one refuses the update.
- Before publication the staged bytes are re-checked against the exact
  generation read at authority-acquisition time, so a non-cooperating
  replacement is never overwritten by stale data.
- Failure paths RETAIN the journal with the failing state stamped on it
  (`MarkTxnIntentState`/`MarkTxnIntentFailed`); deleting a journal on an
  unproven rollback is what destroys deterministic reconciliation.

Durability ceiling (operator-accepted, SRC-103 / T-285): the published
guarantees are process-crash and staged-write-failure recovery, proved by
`tests/test_accounts_journal_crash.py` (PROCESS_TERMINATION_PASS) and
`tests/test_accounts_journal_atomicity.py` (SIMULATED_PASS via the
`SAI_ACCOUNTS_JOURNAL_FAULT` seam, WINDOWS_DISPOSABLE_PASS via real
filesystem-forced failures). Physical power-loss durability — the OS honouring
an atomic replace through an actual power cut — is NOT proved and is not
claimed. The ceiling is accepted as-is.

## Consumer integration

SAI Accounts is federation, not captivity. It is an OPTIONAL control plane. No
consumer may require SAITULS, the SAITULS GUI, or this engine to be installed in
order to run, configure accounts, or launch anything. Every SAI application is
independently useful software and must ship with a working local account path.

### Operating modes

| Mode | Condition | Behaviour |
|---|---|---|
| STANDALONE | SAI Accounts absent, unavailable, or never installed | The application keeps its own local account configuration, discovery, login and launch flows exactly as it had them. |
| FEDERATED | SAI Accounts present | Shared canonical accounts are discovered and may be used. Local management stays available. |
| HYBRID | SAI Accounts present AND the application has local-only accounts | Shared and local accounts coexist in one list. |

The user's public contract: standalone, configure accounts inside the
application; optional, if SAI Accounts / SAITULS is installed, shared accounts
also appear automatically. No public README may say "Install SAITULS first."

### Account origin

Every consumer-visible account carries an origin:

- `SHARED` — the canonical `account_id` is owned by SAI Accounts.
- `LOCAL` — identity is owned by the application through its existing local
  mechanism. A local id is never dressed up as a canonical ecosystem id.
- `IMPORTED` — an optional third value recorded during a migration or
  onboarding workflow, so the user can still see what came from where.

Local-only accounts are never published centrally without explicit user
intent. "Add to SAI Accounts" is opt-in, per account.

### The consumer merge layer

Each consumer normalizes its own accounts and the shared ones into one
presentation type. The field names are a contract; the implementation is each
application's:

```
ResolvedAccount
  resolved_id            application-local, unique within the app
  origin                 SHARED | LOCAL | IMPORTED
  provider_id
  display_name
  capabilities
  availability           operational state, with SHARED_SOURCE_OFFLINE / STALE
                         when the shared source cannot be reached
  shared_account_id      canonical id, or null for local-only
  local_account_id       the app's own id, or null when absent
```

### Duplicate discovery and merging

When the same physical account is visible both locally and through SAI
Accounts, match on, in order: canonical `account_id`, execution context
identity, stable provider profile locator, and a safe remote identity
fingerprint where the provider offers one. Display names are NEVER a matching
key.

- Confidently identical -> one logical account, origin `SHARED`, optionally
  annotated that a legacy local configuration points at the same account.
- Not provably identical -> two separate accounts, each labelled, never guessed
  together.

### Source precedence

Precedence is identity-aware, not "shared always wins". When one verified
account exists in both places, SAI Accounts owns the account id, provider,
execution context, lifecycle state and canonical alias; the application keeps
owning its own presentation and preferences (meter order, meter visibility,
compact layout, per-app settings). A local toggle never mutates global state
unless the UI explicitly says it disables the account globally.

### Hide and disable are two axes

- GLOBAL HIDDEN / canonical operational state — owned by SAI Accounts for
  shared accounts. Normal consumer views respect it; advanced views may still
  show that the canonical account exists.
- LOCAL HIDDEN / LOCAL ENABLED / meter visibility — owned by the consumer,
  per application, and never observable by the other applications.

### Failure behaviour

If SAI Accounts disappears while an application is running, the application
does not collapse. Local-only accounts keep working normally. Shared accounts
move to `SHARED_SOURCE_OFFLINE` (context unreachable) or `STALE` (a safe cached
last-known snapshot still presented) instead of vanishing or being silently
converted into local identities. No identity is lost, no local configuration is
rewritten, and the app recovers on the next successful read.

### Detaching

"Use only locally" removes the central binding and keeps whatever local
configuration is safe to keep. It never exports or copies a token, cookie or
Credential Manager blob to manufacture independence. Where a provider's
semantics make safe detachment impossible, the UI says so plainly.

### The smallest consumer surface

```
list_accounts()               -> sai-accounts list      (optional; may be absent)
get_account(id)               -> sai-accounts get id
get_snapshot(id_or_provider)  -> sai-accounts snapshot ...
launch(id, workdir)           -> sai-accounts launch id --workdir ...
reload_on_generation_change() -> compare generation field
```

Every one of these is optional. A consumer that finds no engine keeps its
local discovery and launch path; a bounded legacy discovery is a compatibility
aid, not a requirement.

### Acceptance gates for federation

- Every consumer starts, and keeps its local account management, with SAI
  Accounts completely absent.
- With SAI Accounts present, shared accounts appear with no duplicate setup.
- Hybrid fixture (shared AG1 + Claude 1, local Claude + Codex) lists all four
  with no identity collision.
- Making the shared registry unavailable leaves the app running, keeps local
  accounts functional, and marks shared accounts offline/stale; restoring it
  recovers them.
- Duplicate discovery resolves to one logical account when identity is provable,
  and to two clearly distinguished accounts when it is not.

Sai Accounts is not DONE until all twelve federation criteria in
`../../Wiki/ACCOUNTS-FEDERATION.md` are checked.

## Security rules

- Never store, log or transport: Windows passwords, vendor passwords, OAuth
  tokens, session cookies, Credential Manager blob bytes, login codes.
- Windows password entry belongs to Windows `runas`; vendor login belongs to
  the vendor's official flow.
- Identity fails closed: if the selected account's context cannot be proven,
  do not silently run a different account.
- No credential swapping or automatic failover between accounts. Accounts are
  independent workers; schedulers choose among them, never mutate auth.
- Workdir fails closed to the requested directory.
- Registry corruption: surface the error and preserve last-known-good data;
  never invent accounts.

## Antigravity quota model

Probes return every real upstream pool/window verbatim — `pool_index`,
`window` (`5h` | `weekly` | `monthly` | `7d` | `30d`), `remaining_fraction`
(0..1), `disabled`, `reset_time`. Consumers may summarize but the underlying
data stays faithful; nobody flattens Antigravity into one fake percentage, and
A+B is never presented as one official quota bucket.
