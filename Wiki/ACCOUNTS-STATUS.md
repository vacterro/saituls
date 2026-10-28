# SAI Accounts — final report

The canonical multi-account standard for the SAI ecosystem, delivered under one
hard constraint: **it is a federation layer, never a prerequisite.** Every
application in the ecosystem is independently useful software, and each one
keeps that property with the control plane deleted from the machine.

All twelve gates in [ACCOUNTS-FEDERATION.md](ACCOUNTS-FEDERATION.md) are
checked, each against evidence that actually ran.

---

## 1. What was built

**The plane** — `Scripts/accounts/` in SAITULS. A scope-rooted registry
(`C:\ProgramData\SAI\Accounts\scopes\op-<fingerprint>\accounts.json`, where the
fingerprint is `sha256(operator Windows SID)[:16]` and the SID itself lives
inside the registry, never in a path), the `sai-accounts.exe` CLI, an
Antigravity broker shim, and the Accounts tab in the GUI. The CLI answers
`list`, `usage`, `get`, `snapshot`, `status`, `launch`, `context`, `import`,
`rename`.

**The contract** — `Scripts/accounts/SAI_ACCOUNTS.md`, locked before any
consumer code was written. It defines the three operating modes, account
origin, the merge rule, source precedence, the two hide/disable axes, failure
behaviour, and the smallest consumer surface.

**Four consumers** — LIMISAW (`SaiAccounts.cs`), FastPrompter
(`core/usage_limits/sai_accounts.py`), AUDAPACK (`audapack/sai_accounts.py` +
`account_registry.py`), ZAICODE
(`packages/provider-node/src/sai-accounts-source.ts`).

---

## 2. The rule that shaped every decision

**Never merge on a display name.** A label can be duplicated and renamed
freely, so merging on one both loses real accounts and invents them. Every
consumer merges on a locator: canonical id, execution-context identity, stable
profile path, or a safe remote fingerprint — and a locator that cannot be
proven produces an *empty* key, which never matches, so the two accounts are
shown separately. That is the safe direction: two rows the operator can see
beats one row that is silently wrong.

This was not theoretical. Live acceptance found a real account whose identity
could not be proven, and the correct behaviour was to show it.

---

## 3. What live acceptance actually proved

Each consumer has an A+B harness that drives the **real installed engine**.
For the STANDALONE arm it genuinely moves `sai-accounts.exe` aside — restored
in a `finally` — because the module falls back *override → canonical install →
PATH*, and pretending the plane is absent by setting one env var would be
testing a fiction.

| | Federated (plane present) | Standalone (plane really gone) |
|---|---|---|
| **FastPrompter** | 7 shared accounts, all 9 identity keys provable; 5 locally claimed locators merged, not duplicated; name-only twins stay 7 separate; probes typed: AG1 `auth_required`, AG2 `shared_source_stale` "context is offline" | plane resolves to `""`, empty list, local account survives with its own id |
| **AUDAPACK** | roster 7 = 6 local + 1 shared; 5 proven locators merged, 1 genuinely new shared row marked `sai_accounts_shared` | 6 local rows still discovered, 0 shared invented, probe refuses cleanly |
| **LIMISAW** | both plane accounts merged into the local registry's 2 rows, 0 duplicates; same display name + different identity stays 2 | local registry answers 2, tray discovery answers, 0 shared invented, plane restored |
| **ZAICODE** | 8 `account:*` providers, plane publishes 7, **provider overlap 0** → the honest list is empty; real probe returns `auth_required` | plane resolves to `""`, empty list, typed `unavailable` |

That last row is the most important result in the table. ZAICODE's providers
and the plane's providers do not overlap today, and the correct answer was an
empty list — not an invented mapping. Forcing `antigravity` onto a
`zai-individual-coding-plan` provider would have been exactly the
"pretend local IDs are canonical ecosystem IDs" failure the amendment forbids.
The code that will consume them the day they appear is written and tested.

---

## 3b. ARM C — global state, live

A third arm was added to AUDAPACK's harness, because the suppression set is a
new claim and a stand-in plane cannot prove it. It seeds an **isolated** plane
root (`SAI_ACCOUNTS_ROOT` + `SAI_ACCOUNTS_SCOPE`, both restored in a `finally`)
with one account per global state, and drives the **real installed engine**.
Both overrides are needed: the root says where, the scope says which registry
inside it, and an unset scope resolves to the operator's real one — a root
override alone proves less than it looks.

| | |
|---|---|
| **C1** | the answering engine is the isolated one, not the operator's |
| **C2** | all 6 seeded accounts are projected, withdrawn ones included |
| **C3** | `hidden` and `operational_state` are on every entry |
| **C4** | `--all` is load-bearing: default `list` = **5** accounts, `list --all` = **6** |
| **C5** | the only drawable account is the live one |
| **C6** | the 4 provable withdrawals come back as identity keys, nothing else |
| **C7** | an unprovable withdrawal suppresses nothing |
| **C8** | the consumer's predicate agrees with the plane's own projection |
| **C9** | the isolated root is removed; the operator's registry was never read |

C4 is the one worth keeping: it is the difference between honouring an ARCHIVED
withdrawal and never hearing about it.

---

## 4. Bugs found and fixed

Four were real product defects, not test problems:

1. **AUDAPACK silently dropped unprovable shared accounts.** `_shared_accounts`
   skipped any entry whose identity key was empty — which is precisely the
   entry that needed showing. An account vanished with nobody told. Fixed in
   production: an unprovable locator now yields a shared-namespaced id via the
   plane's own canonical id as fallback, and the row is shown, marked shared,
   merged with nothing.

2. **`sai_accounts.py` raised `NameError` at import.** A trailing `_ = time`
   referenced a module that was never imported.

3. **ZAICODE's engine resolution made its own test suite machine-dependent.**
   The canonical install path was a hardcoded constant, so a developer *with*
   the plane installed could not test STANDALONE. `installDir` is now a
   parameter.

4. **The ambient-plane leak, twice.** The plane is an optional external
   service, so it leaked into tests that isolate a fake `HOME`: a real registry
   holding two Claude config directories merges into a fake-home run that asked
   for one account and got three. AUDAPACK's `conftest.py` additionally patches
   `subprocess.Popen` globally, so a real plane spawn would have failed the
   whole session. Both suites now neutralize the plane in an autouse fixture,
   the same way the Qt platform and browser spawn are neutralized.

---

## 5. Regression — all five repos

| Repo | Result |
|---|---|
| SAITULS | `test_sai_accounts.py` + `test_regs.py` 8 passed, `test_accounts_tab.ps1` all PASS incl. "deployed engine answers list --all" |
| LIMISAW | `build.ps1 -Tests` — **All tests passed**, exit 0 (federation 34/34) |
| FastPrompter | **not run** — its federation module is absent from that tree; see §9 |
| AUDAPACK | **2382 passed**, 16 skipped, 0 failed (783 s) |
| ZAICODE | oxlint 0/0, `typecheck` exit 0, architecture 0 violations, provider-node 35/35 |

AUDAPACK's figure is the **full** session, not the one federation file. That is
deliberate: FastPrompter's own board records that its equivalent change passed
standalone and in every partial run but failed in the whole session, so a
single-file run would have proved nothing here. Full session: green.

AUDAPACK's live harness runs **A (federated), C (global state) and
B (standalone)**, all green, against the real installed engine.

---

## 6. Security posture

Never stored, logged or transmitted: Windows passwords, Google passwords,
OAuth access or refresh tokens, session cookies, passkey material, Credential
Manager blob bytes, login authorization codes. No `/savecred`, no `/netonly`.
No credential swapping, no automatic failover.

The architecture enforces this rather than promising it. The four consumer
adapters invoke exactly **two** plane verbs between them — `list` and `usage`.
There is no write verb, so "promote a local account centrally" is not merely
unused, it is **unreachable**. Gate 4 and gate 12 close on that fact. The
plane's public projection strips `windows_sid` on purpose.

---

## 7. Runtime UX — the Account Manager that actually launches things

The first cut listed accounts and launched them, and that was enough for the
gates but not for use. Four defects were found and fixed, and one of them was a
real production bug rather than a rough edge.

### 7.1 What was broken

**Open console could not work at all.** It called `launch <account>` with no
`--workdir`, and the engine correctly refused with `WORKDIR_UNAVAILABLE`. The
button was dead on arrival.

**AG2 had no way to start.** A `windows_user` account with its broker stopped
shows `context=OFFLINE auth=UNKNOWN`, and the tab offered nothing to do about
it. There was no Start context, no Stop context, no way to move a secondary
account from OFFLINE to ONLINE without leaving the GUI for the console.

**Two silent, load-bearing bugs.** Every engine line ends CRLF, so splitting the
output on `\n` and taking the last element returned the empty string *after* the
JSON — `ParseEnv` therefore returned null for every single envelope. Separately,
a nonzero exit discarded the envelope even when it was perfectly readable, so
`STATUS_FAILED` swallowed the answer. Both are fixed; `LastEnvelopeLine` now
reads the last non-empty line and the envelope is parsed whatever the exit code.

**A hung child could hang the GUI.** `AccountsCli` called `ReadToEnd()` on stdout
*before* `WaitForExit(timeout)`, so the bounded timeout bounded nothing: a child
that floods stderr and never exits blocks the read forever. `SyncConsolesMenu`
had the same shape. Both now drain stdout and stderr asynchronously and wait for
the child, so the deadline is real.

### 7.2 The Open console flow

Nine steps, in order, all of them off the UI thread:

1. Refuse unless lifecycle allows a launch. `FROZEN`/`ARCHIVED` never starts a
   context and never launches.
2. Resolve a workdir — see 7.3.
3. Query status.
4. If the context is OFFLINE and the backend supports context creation, ask for
   one. Windows then asks for that account's sign-in **locally, in its own
   dialog**. SAITULS never sees, stores or logs it; there is no field for it.
5. Wait for ONLINE, bounded (`ContextStartBudgetMs`, 180 s). The wait narrates:
   `Waiting for Windows login...`.
6. Check auth.
7. `AUTH_REQUIRED` surfaces a Re-authenticate action. **It never substitutes a
   different account.**
8. Launch the exact selected account against the exact selected workdir.
9. Report `Launched AG2 PID 4242 (…)` or one concise actionable error.

For the operator's own Windows account there is no step 4 — AG1 is already the
context, and asking to start it would pop a sign-in prompt for the person
already signed in.

The progress line is the point of all this. Every wait has a word for it:
`Starting AG2 context...`, `Waiting for Windows login...`, `AG2 context online.`,
`Launching...`, `Launched PID ...`, `WORKDIR_UNAVAILABLE: choose a project
folder.`, `AUTH_REQUIRED: re-authenticate this account.`,
`CONTEXT_START_TIMEOUT.`, `CONTEXT_OFFLINE.` No step is silent and stderr is
never swallowed.

### 7.2.1 Which button you pressed, and how long it can wait

Three buttons look interchangeable and are not. Each is a different bounded
chain, and the budgets differ because the underlying work does:

| Button | Shown when | Chain | Worst case |
| --- | --- | --- | --- |
| CONNECT / DISCONNECT | account has its own context and is not your own Windows user | `context connect` (30 s) + re-probe `status` (15 s) | **45 s** |
| SIGN IN | `auth_state` is `AUTH_REQUIRED` | `status` (20 s) → if offline `context start` (30 s) + wait ONLINE (180 s) → `auth` (30 s) → poll for `AUTHENTICATED` (180 s) | **7 min** |
| Re-authenticate | `auth_state` is `AUTH_REQUIRED` or `AUTHENTICATED` | the same `SignInFlow`, which returns at once with "already signed in" | **~0.1 s** |
| OPEN CONSOLE | always the primary action unless the account needs signing in | `status` (20 s) + `context start` (30 s) + wait ONLINE (180 s) + `launch` (60 s), plus one `grant-workdir` (30 s) and a single `launch` retry (60 s) when the ACL is missing | **~6.3 min** |

The sign-in button is therefore not a thing you can go and press on demand: it
is drawn only for an account that actually needs signing in, and its absence is
the account being fine. An account that is already `AUTHENTICATED` shows
**Re-authenticate** instead, which runs the same flow and stops at the first
line — so "the sign-in button did nothing" is usually "there was nothing to do".
Conversely the primary action for a healthy account is **OPEN CONSOLE**, never
SIGN IN, which is one more reason the three look alike from a distance.

So "it does not respond for three minutes or more" is never the CONNECT
button — that one gives up at 45 s and says so. Three minutes is
`ContextStartBudgetMs` or `AuthPollBudgetMs`, and both of those are waiting on
*Windows* or on *you*: a sign-in dialog nobody has answered yet, or a Google
sign-in nobody has finished. Both narrate while they wait — `Waiting for
Windows login...`, and `Antigravity is open for AG2 (PID 4242). Finish the
Google sign-in there -- this window keeps watching.` A window that looks
frozen is a window whose message was not read, not a hung one.

Every one of those numbers is a real deadline rather than a hopeful wait. Each
child process is capped on its own, both of its pipes are drained
concurrently with the wait (`CliRun` — reading one pipe to the end before
starting the other is the classic deadlock, and it is what an earlier revision
did), and a child that overruns is killed and reported as a timeout instead of
being allowed to hold the window open.

### 7.3 Workdir: never a guess

Order is: the directory SAITULS currently has selected, else the last directory
explicitly chosen (remembered in settings as `LastWorkDir`), else a folder
picker. If the folder does not exist or cannot be read, the launch **fails
closed**. There is no fallback to `C:\Windows\System32`, to the user profile, to
the repo root, or to any default.

### 7.4 Hidden accounts

`set-hidden` was sending `"1"`/`"0"`. The engine parses with `bool.TryParse`,
which rejects both, so the toggle never worked. It sends `true`/`"false"` now.

The Account Manager was also skipping hidden rows entirely — which makes them
impossible to un-hide, since the tab is the only place that can. Hidden now
means what it says: excluded from menus and launches, still listed here, marked
`(hidden)`.

### 7.4b The provider catalog, and why half of it cannot be pressed (T-244)

**Add Account** used to be a ComboBox holding three hardcoded strings and a
`pIdx == 1 ? "claude" : (pIdx == 2 ? "codex" : "antigravity")` ternary deciding
what they meant. Nothing in the tree could answer "does this provider actually
work".

There is now one registry, `FolderAutomation/ProviderCatalog.cs`, and the dialog
binds to it. Each entry carries `provider_id`, `display_name`, `auth_family`,
`availability_state`, a short `description`, an `icon_key`, the context kinds it
supports and a `support_note`.

| Provider | State | Why |
| --- | --- | --- |
| Antigravity (Google) | Available | in the engine's provider table |
| Claude Code | Available | in the engine's provider table |
| Codex | Available | in the engine's provider table |
| ZCode | Planned | verification pending |
| OpenCode | Planned | launcher and context menus exist; no account backend |
| Cline | Planned | runs inside VS Code; no managed accounts |
| Gemini CLI | Planned | distinct from Antigravity, which already uses Google sign-in |

Only three are marked Available because **the engine's provider table has exactly
three rows**. Listing seven and enabling seven would produce accounts that can
never finish signing in. A planned provider shows its `support_note`, greys its
alias field and leaves **Add disabled**; `EngineProviderId` returns `""` for it,
so the dialog cannot name a backend even if the button fired, and `AddAccountFlow`
refuses it a second time before the engine is contacted. Activating ZCode is then
a catalog row plus a backend — not another round of UI surgery.

The white control the operator reported was the provider ComboBox: its closed
state and its popup list are drawn by the system theme, not by SAITULS. It is now
`OwnerDrawFixed` + `FlatStyle.Flat` with the palette's own colours, and
`PaintProviderRow` is the single painter for both. The alias TextBox and the two
buttons were the same leak and got the same treatment. Text was **not** made
unreadable to hide the white: the selected row keeps at least 4.5:1 contrast, and
the regression tests read that off rendered pixels.

### 7.4c Add Account signs in through the account plane (T-264)

Creating an Antigravity account is `add` and then a sign-in, and the sign-in is
an **account-plane** operation: it needs no project folder, and it is the same
`auth "<account>"` the Re-authenticate button has always used.

The add flow did not do that. It called `launch <id> --workdir <folder>` and
threw the result away, so a freshly created managed account had to have a
readable project folder before its sign-in window could open, and every refusal
that call could produce — `launch_failed`, `WORKDIR_UNAVAILABLE`,
`CONTEXT_OFFLINE` — was invisible. The operator saw one 90-second
`Waiting for Google sign-in verification (up to 90s)...` that was waiting for
nothing, and then `Account created. Please complete Google sign-in`. Add now
does `status` → (if offline) `context start` + wait ONLINE → `auth`, and the
engine's own status is reported the moment it arrives:
`SIGN_IN_FAILED: auth_launch_failed`, never a wait.

Why the popup: the agy scrub (`BROWSER`, `AGY_CLI_DISABLE_AUTO_UPDATE`,
`AGY_CLI_HIDE_ACCOUNT_INFO`, `AGY_CLI_INTERACTIVE_HEADLESS`, the API-key names)
was applied with `Environment.SetEnvironmentVariable` on the process doing the
work. For a one-shot CLI that is invisible. For a **broker it is a leak**: the
broker is long-lived, so scrubbing it to shape one `/usage` capture reshaped
every child it started afterwards, and the Antigravity sign-in window inherited
`BROWSER=<System32>\where.exe`. There was then no browser to open the Google
sign-in URL with, Windows answered with a "Windows cannot find …" popup, and the
account stayed `AUTH_REQUIRED`. The overrides are now a **child environment
block** (`BuildEnvironmentBlock` → `CreateProcess` with
`CREATE_UNICODE_ENVIRONMENT`) handed to the capture child alone; the broker's own
environment is never written, and the sign-in and console children inherit the
operator's real one. `tests/test_managed_console_context.py` measures both
halves from the child's own environment (`t264c*`, `t264d*`, three rounds for
drift) — and both sign-in checks fail on the pre-fix engine.

### 7.5 Evidence

`tests/test_accounts_runtime.ps1` compiles a stand-in engine
(`tests/fake_sai_accounts/FakeAccountsEngine.cs`) and drives the real Accounts
tab against it, asserting what the engine *actually received* rather than what
the source text looks like. 15 scenarios, 45 assertions, all green. "Never fall
back to another account" and "never fall back to another workdir" are claims
about what reached the engine, so they are tested as exactly that.

`tests/test_accounts_tab.ps1` (17 checks) still passes.

Two bugs in this fixture are worth recording, because both are the kind that
survive review. It once counted status probes in a per-process static while
running as one process per call, so "offline for the first probe" meant
"always offline" and the whole AG2 flow looked broken when only the fixture was.
And it hand-built JSON by string concatenation, so a Windows workdir went out as
`"V:\___VAC\..."` — `\V` is not a legal JSON escape, `JavaScriptSerializer`
threw, and every answer carrying a workdir was silently lost. The fixture now
serializes through the same serializer the real engine uses.

---

## 8. A client hanging up is not a broker failing

A short-lived `sai-accounts` client is supposed to connect, send one request,
read one response and close the pipe. The broker creates one
`NamedPipeServerStream` per connection, so its own teardown runs *after* the
client is already gone — and the framework surfaces that as an exception from
the write, the reader, the writer, or the stream disposal.

The broker's accept loop wrapped the whole connection lifecycle in one generic
handler, so it logged every one of those as a broker failure:

```
broker loop: Pipe is broken.
broker loop: Pipe is broken.
```

Two things were wrong with that. The peer had received what it came for and
left; that is the protocol working. And the handler slept 500 ms, so *every
ordinary command* cost half a second before the next connection was accepted.

Matching the Win32 code alone did not fix it. The actual exception is a plain
`IOException` built from a localized string: `HResult` is `0x80131620`
(`COR_E_IO`), whose low word is 5664 and means nothing. `IsExpectedPipeDisconnect`
therefore matches three Windows codes (`ERROR_BROKEN_PIPE` 109, `ERROR_NO_DATA`
232, `ERROR_PIPE_NOT_CONNECTED` 233) *and*, for the case that carries no code,
the closed set of pipe-disconnect wordings the framework can produce. Anything
else keeps the loud path and its backoff — a blanket `catch (IOException)` would
quiet every normal disconnect while hiding a genuinely broken broker.

A second, quieter bug lived in `SendPipeCommand`. The broker tears its pipe
instance down and builds a new one between requests, so the name is briefly out
of the pipe namespace. A single `File.Exists` probe turned that gap into a
spurious `context_state = OFFLINE` — a live broker reporting itself dead to its
own clients. `PipePresenceGraceMs` (1500 ms, bounded) gives the instance a short
window to come back; a broker that is genuinely gone still fails fast.

The responses already use `AutoFlush`, so a delivered response is flushed
before any of this disposal runs. A command that succeeded is never reported as
failed because cleanup noticed the peer had left.

### Evidence

Seven scenarios in `tests/test_sai_accounts.py` drive a real broker over a real
named pipe with raw Win32 clients, so disconnect timing is under the test's
control rather than hoped for: read-then-close, 100 sequential short-lived
clients (asserting no per-request backoff), disconnect mid-request, disconnect
after sending but before reading, malformed request, an unexpected-exception
path, and shutdown. The full plane suite is 68 checks, all passing.

Live, against a broker serving the current Windows identity over its real pipe:
100 short-lived clients in 1.9 s (19 ms each, against a 500 ms-per-request bug),
30 clients that closed without reading, and 10 that vanished mid-request — the
next client was served correctly every time, and the broker log never printed
`broker loop` or `Pipe is broken`. Shutdown returned `shutting_down`, the pipe
disappeared, and no error was logged on the way out.

### The fix was correct and still did not reach the machine

All of the above was true, and the operator kept seeing `broker loop: Pipe is
broken.` The SID in their output ended `-1008` — the AG2 broker running as
`antigravity_b` — while every test here ran the repo build under `-1002`.

The broker does not run from `Scripts\accounts\bin`. `CmdContext` launches
`SelfExePath()`, and every consumer resolves
`C:\ProgramData\SAI\Accounts\bin\sai-accounts.exe` first (SAITULS deliberately
prefers the installed copy over the source tree, so a stale repo binary cannot
shadow the plane). That installed engine was built at 16:09. The fix was built
at 22:45 — into the repository, and nowhere else. Six hours of green tests
proved a binary the machine had never heard of.

The gap was structural rather than a one-time slip: `build.ps1` compiled into
the repo and stopped there, with no deploy step and no mention of one. It now
takes `-Deploy` (off by default, so CI stays hermetic — `test_ci_hermetic.ps1`
still passes), warns when a broker is still running because a loaded image
survives an overwrite, and prints `NOT deployed` on every plain build so the
next person cannot make the same omission.

Verified after deploying both binaries: the installed engine carries the
no-code message matchers, its two hashes match the repo build exactly, 100
short-lived clients completed at 16 ms each with no broker-loop error, 30
abandoned clients left the next one served correctly, and shutdown was clean.

---

## 9. Known limits, stated plainly

- **ZAICODE sees zero shared accounts today.** Provider rosters do not overlap.
  Correct, and honest; not a gap in the integration.
- **LIMISAW's two Antigravity accounts are always local**, because its own
  registry already claims both locators. That is the merge working, not the
  federation failing — a proven duplicate is one account, and the local row is
  the one already configured.
- **Global hide is now enforced on a merged local row.** CLOSED. This was the
  one known divergence from the contract's hide/disable axes: because the merge
  always survives as the local row, a global hide in SAI Accounts did not
  suppress it in the consumer. The fix keeps local reads local — nothing is
  rerouted through the plane, so federation stays optional — and adds a
  *suppression set* built from the SAME `list` call that already feeds the
  merge. One read, one predicate, two answers: the accounts that may be drawn,
  and the identity keys the plane has withdrawn. A local record whose identity
  is provably one of those keys is dropped, in AUDAPACK, LIMISAW and (already,
  by its own design) ZAICODE. An unprovable identity suppresses nothing —
  suppressing on a guess would hide the wrong account — and an absent or
  broken plane withdraws nothing at all. `--all` turned out to be load-bearing:
  without it the plane omits ARCHIVED accounts from the payload entirely, so a
  withdrawal the consumer cannot see is a withdrawal it cannot honour.
- **FastPrompter's port is not in this tree.** Its own SAIPEN seat removed the
  federation module and held it out of release 0.8.70, and tracks the port as
  its ticket **T-1405**: the change is order-dependent in a full `pytest tests/`
  session, passing standalone and in every partial run. That ticket owns the
  FastPrompter-side fix; it is not duplicated here. The finding it records — that
  a suppression set fed from `read_registry` must not depend on ambient process
  state — is why AUDAPACK's FULL suite was run, not only the one federation
  file: 2382 passed, 16 skipped, no order dependence.
- **LIMISAW's live harness is opt-in**, because it moves the installed engine
  aside. A routine `-Tests` run must never touch the machine's install.
- **The runtime UX fix has not been live-accepted against the real engine.**
  Section 7 is proven against a stand-in engine plus the existing deployed-engine
  checks. The end-to-end pass — a real AG2 with its broker stopped, a real
  Windows sign-in prompt, a real Antigravity launch — is the operator's step and
  has not been performed. It is written down as pending rather than implied.

---

## 10. Nothing was published

No push, no tag, no release. Work is in the working trees, uncommitted and
awaiting the operator's separate authorization.