# Decision Moment `01M3PR73Q8S249DKZ8PK4F8YNA`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.behavior.derived-input-cache`
- **Input key:** `derived_input_cache`
- **Status:** `resolved`
- **Created:** `2026-09-29T14:13:37.128608+00:00`
- **Resolved:** `2026-09-29T14:13:49.276820+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Should the derived per-block input series (expected solar, expected household usage) be cached to a file and reused across evaluation cycles rather than recomputed every five minutes, and what staleness rules apply?

## Options

_(none)_

## Final answer

Yes, cache the derived per-block input series to a file and reuse them across evaluation cycles. Two series are cached: expected solar production per block (refreshed when a new forecast arrives, hourly) and the expected household usage profile per block (recomputed on a schedule, not every cycle, since it derives from seven days of history and is by far the more expensive of the two). The trajectory itself is NOT cached and must be recomputed every cycle, because it depends on the live battery charge and on which blocks have already elapsed; only its inputs are cached. Every cached series carries the time it was computed and the source it came from, so a decision made on cached data can state its age. A series older than a configurable staleness bound is refreshed rather than used, and if refreshing is impossible the decision proceeds but is recorded as degraded - a stale cache must never be indistinguishable from a fresh one. The cache is a derived artifact: deleting the file costs a recomputation and never changes a decision. It is invalidated by a configuration change affecting array geometry or location, by the day rolling over, and by a block-length change. Persisting to a file also means the forecast survives a Home Assistant restart, which is a benefit, but makes the timestamp mandatory so a file written yesterday is never silently trusted at boot.

## Rationale

_(none)_

## Change log

- `2026-09-29T14:13:37.128608+00:00` — opened
- `2026-09-29T14:13:49.276820+00:00` — resolved (final_answer="Yes, cache the derived per-block input series to a file and reuse them across evaluation cycles. Two series are cached: expected solar production per block (refreshed when a new forecast arrives, hourly) and the expected household usage profile per block (recomputed on a schedule, not every cycle, since it derives from seven days of history and is by far the more expensive of the two). The trajectory itself is NOT cached and must be recomputed every cycle, because it depends on the live battery charge and on which blocks have already elapsed; only its inputs are cached. Every cached series carries the time it was computed and the source it came from, so a decision made on cached data can state its age. A series older than a configurable staleness bound is refreshed rather than used, and if refreshing is impossible the decision proceeds but is recorded as degraded - a stale cache must never be indistinguishable from a fresh one. The cache is a derived artifact: deleting the file costs a recomputation and never changes a decision. It is invalidated by a configuration change affecting array geometry or location, by the day rolling over, and by a block-length change. Persisting to a file also means the forecast survives a Home Assistant restart, which is a benefit, but makes the timestamp mandatory so a file written yesterday is never silently trusted at boot.")
