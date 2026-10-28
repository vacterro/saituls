# T-283 acceptance — 1M context window for both Codex accounts

Recorded 2026-10-14, on the operator's machine, against the live plane.
Source request: SRC-101 — "Привет нужно для двух кодексов увеличить лимиты до
1м контекста". The verify criterion is that the requested change is present
and demonstrated against the user's own description of it. The two "codexes"
are the operator's Codex 1 (CX1, `codex:profile-dir:ff7e3619f84e4e05`) and
Codex 2 (CX2, `codex:profile-dir:5c5ef150033ae3d8`).

## 1 — The requested change: PRESENT in both Codex homes

`C:\Users\vac34\.codex\config.toml` (CX1) and
`C:\Users\vac34\.codex-account2\config.toml` (CX2) both carry:

```toml
model = "gpt-6.1-sol"
model_context_window = 1000000
model_auto_compact_token_limit = 900000
model_reasoning_effort = "max"
```

Verified 2026-10-14 by direct read of both files. The third local Codex home
(`.codex-account3free`, CX3) is DISABLED and was intentionally not touched.
Pre-change backups are retained in both homes as
`config.toml.bak-context-1m-20261012-095141-827`.

## 2 — How the plane participates: DEMONSTRATED from `sai-accounts list`

```json
{"account_id":"codex:profile-dir:ff7e3619f84e4e05","operational_state":"ENABLED",
 "provider_metadata":{"profile_locator":"C:\\Users\\vac34\\.codex",...}}
{"account_id":"codex:profile-dir:5c5ef150033ae3d8","operational_state":"ENABLED",
 "provider_metadata":{"profile_locator":"C:\\Users\\vac34\\.codex-account2",...}}
```

Both accounts are ENABLED with their own `profile_locator` homes, which is
exactly what the engine pins as `CODEX_HOME` on each launch
(`SaiAccounts.cs`, claude/codex launch branch). The Codex process started from
a SAI Accounts launch therefore reads the 1M `config.toml` of its own home.
No engine change, no registry field, no restart involved.

## 3 — No code delta, by design

The engine has no verb that writes vendor config: making the control plane the
author of a setting it does not understand would turn every future Codex
schema change into a registry migration instead of a one-file edit. The
mechanism is documented in `Scripts/accounts/SAI_ACCOUNTS.md`
("Codex per-account context window"). The deliverable for this ticket is that
record plus this demonstration: the change is present and shown against the
user's own words — how to enable it was SRC-101's own body.
