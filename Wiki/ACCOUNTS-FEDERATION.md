# SAI Accounts federation criteria

The control plane is optional. SAI Accounts is federation, not captivity:
SAITULS provides convenience and coordination, and every SAI application
remains independently usable on its own.

Standalone when alone. Federated when available. Hybrid when useful.

## The thirteen gates

The overall SAI Accounts target is NOT DONE unless every box below is checked.

Each box cites the evidence that closes it. "Unit" means the consumer's own
federation suite; "live" means the A+B acceptance harness, which drives the real
installed engine and, for its STANDALONE arm, genuinely moves that engine aside
so "the plane is absent" is true rather than simulated.

- [x] Every consumer still works with SAI Accounts absent.
      Unit: `TestStandalone` in all four consumers. Live: arm B of
      `FastPrompter audit/sai_accounts_live_acceptance.py`,
      `AUDAPACK audit/sai_accounts_live_acceptance.py`,
      `LIMISAW tests/sai_accounts_live_acceptance.cs` (B1-B8),
      `ZAICODE packages/provider-node/scripts/sai-accounts-live-acceptance.ts`.
- [x] Every consumer retains a local account-management path.
      Live: FastPrompter B3-B4 (local account survives, keeps its own id),
      AUDAPACK B3-B5 (6 local rows, 6 ids), LIMISAW B3-B6 (local registry
      answers, tray discovery answers).
- [x] Shared and local accounts can coexist.
      Live: AUDAPACK arm A lists 7 = 6 local + 1 shared; FastPrompter arm A
      lists 5 local + 2 shared in one roster.
- [x] Local-only accounts are not automatically published centrally.
      Registry promotion is an explicit Account Manager action. Consumers
      discover and probe by canonical id; AUDAPACK additionally delegates an
      explicitly requested project launch and its bounded workdir permission
      repair to the plane. Neither operation adds or promotes a local account.
- [x] Shared accounts require no duplicate local configuration.
      Live: AUDAPACK A5 (5 proven locators merged into their local rows, 0
      duplicates), FastPrompter A4-A5 (5 claimed locators, 7 distinct ids),
      LIMISAW A6 (roster 2 = expected 2).
- [x] Central registry failure does not break local accounts.
      Unit: `TestCentralFailure` in all four consumers. Live: FastPrompter A7
      (typed probes only — `auth_required` for AG1, `shared_source_stale`
      "account context is offline" for AG2, never a borrowed reading).
- [x] The SAITULS GUI is not required to be running.
      No consumer references `SAITULS.exe`; each reaches the plane only through
      `sai-accounts.exe`. Verified by grep over all four adapters.
- [x] SAITULS is not required to be installed for standalone use.
      Unit `TestStandalone` plus live arm B in all four consumers. None of the
      five public READMEs says "Install SAITULS first"; each says the opposite.
- [x] A public GitHub user can download any individual project and configure it
      independently.
      All five public READMEs (SAITULS, FastPrompter, AUDAPACK, LIMISAW,
      ZAICODE) describe the control plane as optional and name no prerequisite.
- [x] Existing local account workflows remain backward compatible, or have a
      clear equivalent replacement.
      AUDAPACK still discovers through its own `CODEX_ACCOUNT_HOMES` and
      `.claude-account2` paths unchanged; LIMISAW's own registry is untouched and
      still answers when the plane is gone.
- [x] Duplicate local/shared identity is reconciled safely.
      Unit: `TestDuplicateDiscovery` in all four. Live: FastPrompter A6 and
      LIMISAW A8 — two entries with the same display name and different
      identities both survive, because identity is a locator and never a label.
- [x] No provider credentials are copied merely to move between local and
      shared modes.
      No consumer adapter writes, exports or promotes; detachment is not
      implemented as a credential copy anywhere. The plane's public projection
      carries no credential material (`windows_sid` is deliberately stripped).
- [x] A global hide or disable in the plane takes the account off every
      consumer's board, including a row that survived the merge as a local one.
      Unit: `TestGlobalState` in AUDAPACK and LIMISAW. Live: AUDAPACK arm C
      (C1-C9) against the real engine in an isolated plane root. This gate was
      added last and is the one the contract previously failed: the merge
      always kept the local row, so a global hide could be honoured on the
      shared half only.

## Global state, and why `--all` is load-bearing

Two rules, both about the same axis.

**GLOBAL hidden and operational_state are the plane's answer, and the consumer
honours it on every row.** This does NOT mean routing local reads through the
plane — that would make federation a prerequisite, which this whole design
exists to refuse. It means the consumer reads the plane's `list` answer it was
already making, and keeps a *suppression set* of the identity keys that answer
withdrew. A local record whose identity is provably one of those keys is
dropped. Three properties make this safe:

- Suppression needs a **provable** identity. An unprovable locator suppresses
  nothing, because it would suppress every record that also has none.
- An **absent or broken** plane withdraws nothing. Standalone is unchanged.
- `enabled` here is a LOCAL setting. It is never read from, or written to, the
  registry.

**Consumers must call `list --all`.** Without it the plane omits ARCHIVED
accounts from the payload entirely — not filtered out of the drawing, never
delivered — so an ARCHIVED withdrawal is a withdrawal the consumer cannot see,
and therefore cannot honour. Measured live: default `list` returns 5 accounts,
`list --all` returns 6.

## Per-consumer evidence

| Consumer | Unit federation tests | Live acceptance | Standalone arm |
|---|---|---|---|
| LIMISAW | 34 checks (`tests/sai_accounts_federation.cs`) | A + B, PASS | B1-B8 |
| AUDAPACK | 66 (`tests/test_sai_accounts_federation.py`) | A + **C** + B, PASS | B1-B7 |
| ZAICODE | 35 (`packages/provider-node/test/sai-accounts-source.test.ts`) | A + B, PASS | B1-B3 |
| FastPrompter | 59 current focused federation/canonical-label tests (2026-10-07) | historical A+B acceptance above | included in the focused suite |

FastPrompter's federation module is present again and T-1405 is recorded DONE
in that repository. The 2026-10-07 compatibility check also ran the current
AUDAPACK federation suite (66 PASS), LIMISAW federation harness (34 PASS), and
ZAICODE provider-node federation suite (35 PASS). These are focused adapter
checks, not a new full-suite or live acceptance claim. Evidence is recorded in
`.saipen/evidence/T-268-account-system/REPORT.md`.

The earlier T-1405 failure remains useful history: a suppression set fed from
a plane read must not depend on ambient process state. Passing the isolated
adapter suite does not replace the consumer's full-session isolation gate.

## Stable selection and provider identity (2026-10-07)

This contract prevents a renamed or reordered display row from sending a
command to another account. Every consumer uses the plane's immutable
`account_id` for operations. Generated compact labels remain stable human
selectors; an alias cannot shadow them. Colliding aliases or legacy ids are
refused with `ACCOUNT_SELECTOR_AMBIGUOUS` and candidate canonical ids instead
of being resolved by list position.

An execution locator, an explicit provider binding, and an observed signed-in
provider identity answer different questions. Email addresses and display
names are labels; equal labels never merge different provider fingerprints.
Fingerprint comparisons are scoped to the provider, and a runtime claim must
be verified before it is displayed or used to deduplicate quota. A cleared
explicit binding must not survive in a consumer's cache. The field semantics
and CLI examples are owned by `Scripts/accounts/SAI_ACCOUNTS.md`.

The live harnesses are opt-in where they touch the machine. LIMISAW's lives
behind `pwsh .\build.ps1 -LiveAcceptance` and is deliberately NOT part of
`-Tests`, because its STANDALONE arm moves the installed engine aside.

## Test matrix per consumer repo

| Suite | Setup | Expected |
|---|---|---|
| STANDALONE | SAI Accounts absent (no engine on disk, no `SAI_ACCOUNTS_ROOT`) | App starts; local add/remove/configure/login/probe/launch all work; no exception about a missing central registry. |
| FEDERATED | canonical registry present | Shared accounts appear; no duplicate local setup is asked for. |
| HYBRID | shared AG1 + Claude 1, local Claude + Codex | All four listed; no identity collision; origin visible per account. |
| CENTRAL FAILURE | valid shared accounts, then the shared source made unavailable | App stays running; local accounts keep working; shared accounts go STALE or SHARED_SOURCE_OFFLINE; no local config is corrupted; recovery on restore. |
| DUPLICATE DISCOVERY | the same physical provider profile visible locally and canonically | One logical account when identity is provable; two clearly distinguished accounts when it is not. Never merged on display name alone. |

## Standing rules

- Canonical `account_id` is owned by SAI Accounts for shared accounts. A
  local-only id is owned by its application and is never presented as canonical.
- Precedence is identity-aware. SAI Accounts owns identity, provider, execution
  context, lifecycle state and canonical alias; the application keeps its own
  presentation and preferences.
- GLOBAL hidden / disabled is owned by the control plane. LOCAL hidden / local
  enabled / meter visibility is owned by the consumer and is invisible to the
  other applications.
- A local UI toggle never mutates global state unless it says it does.
- "Add to SAI Accounts" is opt-in and per account. Nothing is promoted centrally
  without explicit intent.
- Detaching never copies a token, cookie or Credential Manager blob.

The authoritative wording lives in
[../Scripts/accounts/SAI_ACCOUNTS.md](../Scripts/accounts/SAI_ACCOUNTS.md).
