---
affected_files: []
cycle_number: 1
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-29T19:21:40Z'
reviewer_agent: user
wp_id: WP04
---

# WP04 review - cycle 1 - CHANGES REQUESTED

What passed: I recomputed every fixture by hand and all of them are correct (sunny_saturates, power_limited, winter_breach, balanced, saturate_then_drain, price_gap). Absorption takes min(surplus, max_charge_kw*h, headroom). I ran 10,648 random cases against an independent reference model, covering starts below the reserve, near-zero power, huge surplus and shuffled usage order. Both energy identities, the charge bounds, charge continuity, the power limits, first-crossing saturation and breach, total spill and leftover >= 0 held in every case. The expectations are commented constants, the module is pure, and 96 tests pass. Keep all of this.

## BLOCKING 1 - project() does not honour the price horizon (FR-003, FR-008, T017 step 2, T019 step 3)
`project()` walks the solar series' own blocks. `horizon_end` is computed from prices but is only reported and never used to bound the loop. As a result the trajectory silently disagrees with its own `horizon_end`:
- Solar shorter than the price horizon (4 solar blocks, 16 priced blocks, usage 1.0/block, 50 kWh at 50%): you get 4 blocks, the last is 10:45, horizon_end is 14:00, and leftover_kwh is 16.0. The true leftover over the horizon is 4.0. Leftover, breach and saturation are all wrong with no error.
- Solar longer than the horizon (24 blocks): it projects to 15:45, past horizon_end 14:00, and leftover is taken at a block outside the horizon.
- A hole in the solar series (block 3 deleted) is silently skipped: you get 7 blocks and no error, even though "solar and usage never have holes".
- `test_trajectory_spans_price_horizon_when_solar_runs_out` builds the zero pad itself with `series.zero_solar_series`, so it tests the caller and not `project()`.

Fix: iterate the dense grid from the first block to `horizon_end` in fixed block steps, stepping in UTC (see blocking 2). Look solar and usage up by block_start. A block beyond the solar forecast's coverage gets 0.0 solar, as T019 requires. A missing usage block raises TrajectoryError. Either reject solar or usage entries at or after horizon_end, or ignore them. Also raise when the solar series has an interior hole or is non-contiguous. Add tests that call `project()` directly with (a) a short solar series and no manual padding, asserting len(blocks) == grid length to horizon_end and a hand-derived leftover, (b) a long series, and (c) an interior hole.

## BLOCKING 2 - DST fall-back: the block_start join conflates the repeated hour (FR-039)
Python treats aware datetimes that share the same ZoneInfo as naive wall-clock times when comparing and hashing: 2026-10-25 02:00 fold=0 (+02:00) == 02:00 fold=1 (+01:00), and their hashes are equal. On a correct, UTC-stepped 25 Oct 2026 grid (12 blocks from 2026-10-24T23:30Z), `project()` raises `TrajectoryError: duplicate usage block 2026-10-25 02:00:00+01:00`. Separately, PEP 495 means a fixed-offset price key such as `2026-10-25T02:00+01:00`, which is what `prices.expand_to_blocks` produces from ISO strings, never compares equal to a ZoneInfo time in the ambiguous hour (both fold=0 and fold=1 compare unequal). Those blocks would get has_price False even when a price exists.

Fix: normalise every join key to UTC (`t.astimezone(timezone.utc)`) for the usage dict, the solar dict, the duplicate check and the price lookup. Build a UTC-keyed view of price_map, and keep the original block_start on the TrajectoryBlock.

## BLOCKING 3 - the DST test proves nothing
`test_dst_day_follows_solar_grid` uses 29 Mar 2026 01:30 plus wall-clock 15-minute steps. That produces 02:00 and 02:15, which do not exist in Europe/Brussels. The test then only asserts that the output order equals the input. Replace it with a fall-back test (25 Oct 2026, UTC-stepped, 12 blocks from 23:30Z, so 02:00-02:45 occurs twice). Assert 12 distinct blocks, no error, correct usage per block with the usage list shuffled, and has_price matching a fixed-offset price map across the repeated hour. Also add a spring-forward test on real UTC-stepped instants.

## Non-blocking
- NaN solar or usage is accepted: `min(nan, x)` makes the identities meaningless and the charge silently stays unchanged. Reject non-finite values with TrajectoryError, as you already do for negative values.
- Empty solar or grid input currently returns 0 blocks with leftover = start minus reserve. Once blocking 1 is fixed, decide whether that case should raise.
- Note for WP03 and the coordinator: `series._grid` steps in wall-clock time (`t += timedelta` on a ZoneInfo datetime), so on the fall-back day it produces 02:45(+02:00) followed by 03:00(+01:00) and drops the repeated hour from the grid. This is outside WP04's files, but it affects what project() receives.
