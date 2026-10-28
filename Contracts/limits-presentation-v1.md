# SAI limits presentation, version 1

`sai.limits/presentation/1` describes display preferences for one consuming
application. It contains no quota measurements, account lifecycle, credentials,
global account visibility or provider configuration. Each application retains
its existing persistence owner; this contract does not introduce a shared file
or make one application's settings overwrite another application's settings.

```json
{
  "schema": "sai.limits/presentation/1",
  "number_mode": "remaining",
  "headline": "lowest",
  "hidden_accounts": [],
  "hidden_readings": [],
  "reading_order": [],
  "hide_spent_accounts": false,
  "only_with_five_hour": false
}
```

| Field | Meaning and default |
|---|---|
| `number_mode` | `remaining` or `used` (100 minus remaining); default `remaining`. Does not change eligibility, identity, selection, availability or reset |
| `headline` | `lowest`, `any`, or a canonical `metric_id`; default `lowest`. A valid manual pin wins over local filters. An unavailable/vanished pin remains saved and its automatic fallback is disclosed |
| `hidden_accounts` | Unique local account **keys** from `sai.limits/snapshot/1`; default empty. Independent of SAI Accounts' global hidden flag |
| `hidden_readings` | Unique local `metric_id` values; default empty. Hidden readings are excluded from automatic selection, not from fetching or expanded inspection |
| `reading_order` | Ordered unique `metric_id` values; default empty (source order). New readings append in source order; temporarily absent IDs remain saved |
| `hide_spent_accounts` | Boolean, default false. Same usable-quota filter as LIMISAW; display filtering never changes account lifecycle |
| `only_with_five_hour` | Boolean, default false. Same usable-5h-account filter as LIMISAW; other independent windows remain in expanded details |

Automatic `lowest` chooses the smallest positive eligible remaining reading,
or zero when all eligible readings are spent. `any` retains its current
eligible reading, otherwise takes the first eligible reading in configured
order. Eligibility uses the full set before visual capacity limits. Global
out-of-service/hidden account rules apply before this local presentation.
Unreadable data is unavailable; carried values preserve their source status
and freshness. Reset information and compact/expanded UI are views of the
same windows, without a new display-mode enum.

The discriminator is required. Omitted fields use the defaults above. Known
fields reject incompatible types or an unknown number mode; unknown fields
are ignored for forward compatibility. Empty/duplicate list entries are
normalized without removing valid unavailable IDs. Parsing does not save.

## Implemented adapters and migration

`LimitsPresentation` in `SAITULS.cs` reads and applies the neutral model, can
serialize/parse it, and is used by the Home selector. Existing
`SAITULS.ini` keys remain authoritative and backward compatible:

| Neutral field | SAITULS key | One-time LIMISAW seed |
|---|---|---|
| `number_mode` | `LimitsShowUsed` | `ShowUsed` |
| `headline` | `LimitsMetric` | `TrayMetric` |
| `hidden_accounts` | `LimitsHidden` | No import of another application's local account hides |
| `hidden_readings` | `LimitsReadingHidden` | `TrayHidden` |
| `reading_order` | `LimitsReadingOrder` | `TrayItems` |
| `hide_spent_accounts` | `LimitsHideSpent` | `HideSpentAccounts` |
| `only_with_five_hour` | `LimitsOnlyFiveHour` | `OnlyWithFiveHour` |

`LimitsSeeded` retains the existing one-time import guard. LIMISAW's INI is
read only; later edits are independent. Existing explicit metric pins and
unavailable IDs survive migration. The DTO writes no extra persistence keys.

FastPrompter and ZAICODE can implement adapters to this contract without a
repository-wide migration. Their adapters are not implemented by this change;
their current app-specific settings remain intact.

For isolated integration tests, `SAITULS_LIMITS_DIR` scopes canonical snapshot
and consumer-export files to a separate directory. Normal installs use
`%LOCALAPPDATA%\SAI\Limits`. This process override adds no persistence key.
