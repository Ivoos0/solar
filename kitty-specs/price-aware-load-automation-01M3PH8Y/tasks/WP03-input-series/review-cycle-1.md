---
affected_files: []
cycle_number: 1
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-29T18:58:23Z'
reviewer_agent: user
wp_id: WP03
---

# WP03 review feedback (cycle 1) - CHANGES REQUESTED

Everything on the checklist passes except one real-data correctness defect in solar_series.
Please keep the rest of the work as it is.

## Issue 1 (blocking): a single global min-gap "resolution" breaks real forecast.solar payloads

`solar_series` works out ONE `resolution_minutes` as the minimum gap between payload keys, then treats
EVERY entry as a period of that length starting at its key. Real forecast.solar `watt_hours_period`
payloads are not evenly spaced: the API puts sunrise and sunset timestamps in the series
(e.g. 05:15, 06:00, 07:00 ... 20:00, 20:58). When one short gap is present, every hourly value
gets squeezed into a shorter window.

Reproduction (Europe/Brussels, 15-min blocks):
payload = {05:15: 0, 06:00: 100, 07:00: 400, 08:00: 800, 09:00: 1200} Wh
-> resolution becomes 45. Every hour's :45 block gets 0.0 kWh and the other three get 4/3 of their share
(06:00-06:30 = 0.0333 each, 06:45 = 0.0; 09:00-09:30 = 0.4 each, 09:45 = 0.0). Every block reports
source_resolution_minutes = 45. The total energy is still conserved, so the energy-conservation test
cannot catch this, but the per-block timing is wrong. The trajectory reads it as a saw-tooth that drops
to zero solar once an hour.

Required fix:
- Work out each entry's period from the entries next to it, not from one global minimum. Use an
  hourly default only when there is a single entry or at the edge of the series. Cap overlong gaps,
  such as the night gap from sunset to the next sunrise, at a sensible maximum (60 min), so a single
  value is not smeared across the night.
- Decide whether a key is the START or the END of its period, and write the decision in the docstring.
  forecast.solar watt_hours_period values are energy since the previous timestamp: the sunrise entry
  is ~0, which points to period-ENDING semantics. Check this against a captured live response, or say
  in the docstring why start semantics were chosen.
- Record source_resolution_minutes for each block from the period that actually covers it, or the
  typical (modal) spacing. It must not report the global minimum.
- Add a test with a realistic fixture that includes irregular sunrise and sunset entries. Assert
  (a) no daytime block between two hourly entries is zero, (b) each hourly entry splits into four equal
  quarters, (c) total energy is still conserved.

## Non-blocking notes (no change needed in WP03)

- sample_days question: ACCEPTED as implemented (distinct dates contributing to a bucket, with history
  windowing left to the adapter). WP10 must honour: (1) pass only the trailing seven days of per-interval
  kWh deltas (not cumulative meter totals); (2) report overall history coverage (distinct days in the
  window, degraded when < 7) in the decision record, because under a 7-day window per-bucket sample_days
  is at most 1 and cannot serve as the "short history" signal on its own; (3) data-model.md's
  "sample_days < 7 when history is short" note should be corrected to match.
- DST: _grid steps in wall-clock time on aware ZoneInfo datetimes, so the October fall-back day has
  96 blocks instead of 100 (and the spring-forward day produces non-existent 02:xx times). The whole
  mission shares this grid behaviour (WP02 prices must agree), so raise it at mission level rather than
  fixing it here alone.
- contracts/cache-file.md example shows expected_kwh repeated (1.82, 1.82) across sub-blocks. That
  contradicts the WP03 energy-divides rule. It is a docs inconsistency for the planner, not a WP03 code
  issue.
