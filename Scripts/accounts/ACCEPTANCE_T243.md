# T-243 live acceptance — Antigravity provider identity

Recorded 2026-10-04, on the operator's machine, against the deployed
`sai-accounts.exe` and the rebuilt `SAITULS.exe`. The addresses below appear only
in this file: they are the local acceptance result §1 allows, and they are kept
out of the broker log, the helper log and the SAIPEN LOG lines on purpose.

## Truth recorded first, before anything was touched

§13 asks for the post-manual-relogin truth to be recorded rather than assumed
from a console screenshot. It is, taken directly from the engine before any
acceptance step ran:

| Slot | Backend | Context | Auth | Fingerprint | Google account |
|---|---|---|---|---|---|
| AG1 | `windows_user` (current, vac34) | ONLINE | AUTHENTICATED | `ce93fa79b66ad0c7` | aleks.nelin34@gmail.com |
| AG2 | `managed_windows_user` (antigravity_b) | OFFLINE | UNKNOWN | — none — | — none — |

So the operator's manual `/logout` and re-authentication DID work: AG2 holds the
second Google account and no longer shares AG1's identity. What was missing was
the product path that gets there without typing `/logout` in a console — which is
what this ticket builds. Nothing here reopens T-239's identity model.

## A — select AG1

Engine: verified fingerprint, `google_id_token_sub` provenance, label present,
`credential_present: true`, `auth_state: AUTHENTICATED`.
UI: AG1 runs in the current Windows user, so it offers no managed Connect or
Disconnect control at all (state matrix SM1/SM2). The Google account row renders
beside Authentication, not under Advanced (T30, T32c).

## B — select AG2

AG2 was OFFLINE when the run started (`auto_connect: true`,
`auto_connect_reason: operator_connect`); the context was started, which is what
the app itself does on launch and which changes no account mapping.

| Slot | Context | Auth | Fingerprint | Google account |
|---|---|---|---|---|
| AG2 | ONLINE | AUTHENTICATED | `62b01cb67a9a27af` | potatoddas@gmail.com |

Two verified fingerprints differ, so the duplicate-identity warning is correctly
absent (T33) and the two slots are two independent quota pools.

## C — disconnect and reconnect AG2

`context stop AG2` → `context_state: OFFLINE`. `context start AG2` →
`context_state: ONLINE`, `auth_state: AUTHENTICATED`, fingerprint still
`62b01cb67a9a27af`, label still potatoddas@gmail.com.

No OAuth was required and none was requested: over the whole window the managed
broker logged eight lines, all of them broker start/listen/shutdown lifecycle,
and zero `auth` or `logout` requests. The credential survived the disconnect in
the managed user's own store, which is what §14.9 asks for.

A live illustration of §14.7 while this was happening: AG2's
`credential_last_written` moved from `134356072652945093` to
`134356128452142387` across the reconnect, and the identity did not change. The
timestamp is a fact about a file. AG1's and AG2's timestamps already differ from
each other (`…102…` vs `…072…`), so any grouping keyed on it would have invented
a second identity for AG1 and AG2 — which is what T42 now pins.

## D, E, F — staged for the operator, not executed

These three require a human to complete a Google OAuth in a browser and to pick
an account on purpose. An agent cannot do that, and attempting it would change
the account mapping §13 forbids leaving altered. They are staged, not run.

- **D** — select AG2, click *Switch Google account*, complete the OAuth, confirm
  AG1 keeps `ce93fa79b66ad0c7` throughout, that only AG2 goes AUTH_REQUIRED, that
  the OAuth runs under AG2's managed SID, and that a new fingerprint and label
  appear on the row.
- **E** — deliberately sign AG2 into AG1's Google account: both fingerprints
  become equal, SAITULS warns on BOTH rows that they are one Google identity
  (T32 proves both rows are probed, not only the selected one).
- **F** — restore AG2 to its own account: fingerprints differ again, the warning
  disappears (T33), two pools again.

The FastPrompter half of D/E/F (one quota pool versus two) cannot be run here:
FastPrompter is not in this repository (tracked separately as T-1405). What this
tree does prove is the contract it would consume — the grouping keys on
`fingerprint` and never on the label (T31, T42), and the payload carries no
address in any machine-readable field (`broker-status-identity-safe`).

## Final mapping left as the operator intended

AG1 = aleks.nelin34@gmail.com, AG2 = potatoddas@gmail.com. Both contexts online,
both AUTHENTICATED. No provider credential was deleted, created or replaced during
this acceptance, and no OAuth was performed.