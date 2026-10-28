# Native queue contract investigation

Status: generation 2.1.0 has live strict native FIFO and real Ctrl+V large-paste
acceptance on the exact supported host. Production readiness remains blocked:
the real host rejects live transcript-store insertion, so hydration has not yet
proved visible transcript convergence.

**2026-09-06 live implementation blocker:** the legacy TUI runner and native
v2 scheduler are separate execution domains in this exact build. Preserving
legacy idle Enter and routing only busy submissions to v2 starts overlapping
agent turns. Native backend FIFO passes; routing-only TUI acceptance fails.
Do not apply either the retired package or the rejected experimental adapter.

Observed locally on 2026-09-05, OpenCode 1.18.29, Windows x64 executable
SHA-256 `88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5`.
Evidence comes from the installed executable and its running `/doc` endpoint,
not the separately installed SDK declarations.

* The TUI submit function `ni` calls
  `x.client.session.prompt({sessionID:HU,...LU,agent:C.name,model:LU,...})`.
  Its busy status is `Z().type`, read from the selected session's status.
* That legacy SDK method maps only its declared fields to
  `POST /session/{sessionID}/message`. It does **not** map `delivery`.
  Adding `delivery` to this call alone cannot implement queue mode.
* The same runtime exposes `client.v2.session.prompt`, which maps
  `POST /api/session/{sessionID}/prompt` with body
  `{id?, prompt: {text, files?, agents?}, delivery?: "queue" | "steer", resume?}`.
  Model and agent are session settings, not fields of this prompt payload.
* The response is `{data: {admittedSeq, id, sessionID, prompt, delivery,
  timeCreated, promotedSeq?}}`. A real legacy-created disposable session
  accepted a native queued prompt with `resume:false`, returning HTTP 200
  and `admittedSeq:1`. This deliberately did not start a model turn.
* The native default is `steer`. The scheduler persists admitted input,
  promotes queued input one at a time in ascending admission sequence, and
  has a separate `promoteSteers` implementation. Only that scheduler may
  own delivery. No plugin FIFO, idle dispatcher, or prompt replay is valid.

Implementation must preserve attachments, agent/model changes, error handling,
and explicit steer intent at the TUI admission seam. A binary modification
requires a recognized whole-file signature plus checked embedded-source
boundaries and a reversible transaction. These observations alone do not
authorize treating an arbitrary 1.18.29 executable as compatible.

Local raw evidence is in `.saipen/evidence/final-wave/`: `openapi.json`,
`session.json`, `admission.json`, and server startup output. Credentials and
provider configuration are not part of those evidence files.

## Real TUI boundary acceptance, 2026-09-06

The executable hash above was reverified. A non-binary extension seam exists:
`tui.json` loads a default-exported `{ id, tui }` module. The plugin receives
the same `sdk.client` used by the composer, `api.keymap` command transformers,
and `api.state.session.status(sessionID)`. Named module exports alone are
ignored. Managed textarea Enter can invoke the composer directly, bypassing
`prompt.submit` command dispatch; intercepting only that command is incomplete.
The upstream API description is
<https://github.com/anomalyco/opencode/blob/dev/packages/opencode/specs/tui-plugins.md>;
the installed executable and actual Windows TUI were the acceptance authority.

The source submit signature is unique at byte offset 110748950:
`x.client.session.prompt({sessionID:HU`. Its complete composer semantics were
left in place by a staged SDK-boundary adapter; busy input was mapped to the
real `client.v2.session.prompt` method, files to `prompt.files[].uri`, agent
mentions to `prompt.agents[]`, model/agent changes to native session settings.
No executable bytes or normal user configuration were modified.

### Backend positive control

`tests/native_queue_fixture.py` served an isolated local OpenAI-compatible
provider; no paid provider, credentials or global model changes were involved.
It emits a nonempty first delta before its eight-second delay, so
`session.next.step.started` is observable during real execution. Empty first
deltas do not establish that barrier. `tests/live_native_queue.py` now rejects
promotion-only barriers and requires successful terminal order.

`backend-active/` under `.saipen/evidence/final-wave/native-fixture/` records:

- cc1 admitted seq 1, promoted seq 2, step.started seq 3.
- cc2..cc5 admitted only after that busy barrier, seq 5..8.
- Five successful `finish: stop` completions in strict native FIFO; exit 0.
- Ten native messages; zero legacy messages for that session even when the
  legacy request includes the correct directory scope.

### TUI negative control: two simultaneous runners

A visible, disposable `opencode attach` session used the real composer and
ordinary Enter, driven through Windows console input records. Idle Enter cc1
ran through the untouched legacy request. While its directory-scoped legacy
status was `busy`, Enter cc2 reached native admission with `delivery: queue`.

The native scheduler immediately promoted cc2 because **its** runner was idle:

| Event | Runtime timestamp (ms) |
|---|---:|
| legacy cc1 assistant created | 1788644241001 |
| native cc2 admitted and promoted | 1788644241181 |
| native cc2 step.started | 1788644241566 |
| legacy cc1 assistant completed | 1788644249567 |
| native cc2 step.ended | 1788644249758 |

Native cc2 started **8001 ms before** legacy cc1 completed. At that instant,
`/session/status?directory=...` said `busy` and `/api/session/active` said
`running` for the same session ID. The visible TUI still displayed legacy cc1
while the adapter's native acceptance toast appeared. Native cc2 was absent
from the legacy message list and renderer. Five admissions or the legacy
TUI's own `QUEUED` labels are therefore not native FIFO evidence.

Raw authority: `mixed-runner-history.json`, `mixed-runner-status.json`,
`mixed-native-active.json`, `mixed-final-native-history.json`,
`mixed-final-legacy-messages.json`, `mixed-final-native-messages.json`, and
`tui-mixed-adapter.txt` in that evidence directory.

### Consequence and retained work

There is no proven compliant routing-only patch at this boundary. Waiting for
legacy idle and then resuming v2 would reintroduce the explicitly forbidden
second scheduler. Routing idle submissions to v2 also needs native message,
status and interrupt integration with the TUI; changing only admission loses
visible messages, busy detection and abort semantics. The embedded native
Data context exists, but the active session renderer/status/composer still
use the legacy state contract. No native TUI mode flag was found in this build.

The failed adapter and its seven passing unit tests are preserved exclusively
under `.saipen/kitchen/failed/T-144-native-routing/`, outside the installable
package. They are rejected experimental evidence, not a native implementation
or a deployment candidate. The disposable TUI/server/provider were stopped;
the fixture TUI config no longer loads the adapter. The original executable
remains byte-identical. SAIPATCH transaction repair and later work remain open.

Unblocking requires a proven shared native TUI execution/state boundary, or an
explicitly scoped native TUI migration covering admission, rendering, status
and interrupt together. A different version string alone is insufficient.

## Generation 2.x acceptance, 2026-09-07

The installable 2.1.0 plugin replaces the rejected routing-only design. It wraps
the shared SDK client so every ordinary composer admission uses
`client.v2.session.prompt` with `delivery: "queue"`; explicit steer alone uses
`delivery: "steer"`; abort maps to `client.v2.session.interrupt`; model and
agent changes map to native session settings. It has no local FIFO, busy gate,
timer or replay scheduler. SAIPATCH Apply/Restore operates transactionally on a
disposable exact-hash host and leaves the live executable untouched.

`.saipen/evidence/final-wave/patch2x-20260907/ctrlv-run6/` records the real
Windows TUI combined gate on the supported executable:

- strict native FIFO completed cc1..cc5 with five `finish: stop` turns;
- cc2 arrived while cc1 was running through a semantic four-record Ctrl+V;
- paste became visible in 515 ms, required a separate Enter, admitted exactly
  once and preserved both markers across 16,396 characters;
- ambient trace was `NEUTRAL`, `RUNNING`, `DONE`, with no inter-turn DONE flash;
- key trace contains normalized key metadata only; clipboard evidence contains
  counts only. Secret-hygiene tests reject raw clipboard serialization.

This does not close production readiness. The same host reports
`LIVE_PROJECTION_SEAM_UNSUPPORTED`: writes through the exposed `api.state`
views do not verify in the active renderer. Native HTTP history is not local
transcript evidence, and hydration alone cannot prove live visible transcript
convergence. Permission/question `NEEDS_HUMAN`, session switching, abort/steer,
real model/agent settings, attachments, tool/task/MCP turns, retry/reconnect and
the full combined stress matrix also remain unaccepted. The executable remains
byte-identical to the hash above.
