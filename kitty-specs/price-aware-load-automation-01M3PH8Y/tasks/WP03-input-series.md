---
work_package_id: WP03
title: Solar and usage input series
dependencies:
- WP01
requirement_refs:
- FR-004
- FR-005
- FR-023
- FR-030
- FR-039
- FR-059
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T011
- T012
- T013
- T014
- T015
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
authoritative_surface: pyscript/modules/series.py
create_intent:
- pyscript/modules/series.py
- tests/test_series.py
execution_mode: code_change
owned_files:
- pyscript/modules/series.py
- tests/test_series.py
role: implementer
tags: []
tracker_refs: []
---

## ⚡ Do This First: Load Agent Profile

Before reading anything else in this file, load your assigned agent profile:

```
/ad-hoc-profile-load python-pedro
```

This establishes your identity, governance scope, and boundaries for this work package. Do not
begin implementation until the profile is loaded.

## Objective

Produce expected solar production and expected household consumption, per block, across the horizon.
These two series plus the price series are everything the trajectory consumes.

Satisfies **FR-004, FR-005, FR-023, FR-030, FR-039**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP03 --agent <name>`. WP03 depends only on WP01 and runs
  in parallel with WP02.

## Context you need

- `data-model.md` — the `ForecastSlot` and `UsageSlot` tables
- `research.md` R-03 — why a REST sensor rather than the built-in integration, and the payload shape
- `contracts/cache-file.md` — these are the two series that get cached; their shape must serialise cleanly

This module is **pure**. It receives already-fetched data — a forecast payload mapping and a history
of consumption readings — and returns block series. It does not call the forecast API and does not
read Home Assistant state; the adapter does both.

**Both series are dense and contiguous** from `start_time` to `horizon_end`, one entry per block, no
holes — even where source data runs out (those blocks are zero). The **price** series from WP02 is a
map that may have holes. That asymmetry is deliberate: prices come from a market that sometimes
publishes nothing, while solar and usage are always estimable. Everything downstream joins the three
**by block start time**, never by position (FR-039).

**Asymmetry worth knowing up front**: the solar series is cheap to build (parse a payload). The usage
profile is the expensive one — roughly 672 quarter-hour buckets averaged over seven days. That is
why both are cached but on different schedules: solar hourly, usage daily.

---

### T011 — Build the solar series from the forecast payload

**Purpose**: Turn forecast.solar's `watt_hours_period` mapping into per-block expected energy.

**Steps**:

1. Create `pyscript/modules/series.py`.
2. Define `ForecastSlot` per `data-model.md`: `block_start`, `expected_kwh`, `is_zero_fallback`,
   `source_resolution_minutes`.
3. Write `solar_series(payload, config, start_time, horizon_end)` where `payload` is the
   `watt_hours_period` mapping — timestamp string keys to watt-hour values.
4. Convert **watt-hours to kilowatt-hours** (divide by 1000). Getting this wrong by a factor of 1000
   would make the battery appear to fill in one block, and the symptom — permanent saturation — would
   look like a trajectory bug rather than a unit bug. Name the variable so the unit is unmissable.
5. Expand onto the block grid **held flat**, exactly as WP02 does for prices: an hourly forecast
   value becomes four identical 15-minute blocks, each carrying the same `expected_kwh`.

   Note the subtlety: `watt_hours_period` is *energy for the period*, not power. When an hourly
   period becomes four blocks, each block gets **a quarter of the period's energy**, not the whole
   value. This is the opposite of the price case, where each sub-block carries the full price.
   Prices are intensive; energy is extensive. Confusing the two inflates expected solar fourfold.
6. Record `source_resolution_minutes` from the spacing of payload keys.
7. Blocks beyond the payload's coverage but inside the horizon get `expected_kwh = 0.0`. Price data
   often reaches further than forecast data; the horizon is not truncated to match (FR-030 note in
   `spec.md` edge cases).

**Files**: `pyscript/modules/series.py` (new)

**Validation**:
- An hourly payload of 1000 Wh for one hour yields four blocks of 0.25 kWh each
- Total energy across the expanded blocks equals total energy in the payload — **assert this**; it
  is the single best guard against both the unit error and the intensive/extensive error
- Blocks past the payload's end are zero, and the series still reaches `horizon_end`

---

### T012 — Implement the zero-solar fallback

**Purpose**: A forecast outage must degrade the plan, not stop it (FR-023).

**Steps**:

1. Write `zero_solar_series(config, start_time, horizon_end)` returning blocks across the whole
   horizon with `expected_kwh = 0.0` and `is_zero_fallback = True`.
2. Make `solar_series` fall back to this when the payload is `None`, empty, or unparseable — the
   adapter should be able to hand in whatever it got without pre-checking.
3. `is_zero_fallback` must be visible on **every** block of a fallback series, so the decision record
   can mark the cycle degraded (FR-027).

**Files**: `pyscript/modules/series.py` (continues)

**Validation**:
- `solar_series(None, ...)` returns a full-length series of zeroes with the flag set
- `solar_series({}, ...)` behaves identically
- A normal series has `is_zero_fallback` False on every block

**Why zero and not "last known"**: zero is the conservative assumption. It makes the planner keep
more charge and buy more, which is the safe direction to be wrong in when the sun is unknown.
Carrying yesterday's forecast forward would be a confident guess about something unmeasured.

---

> **Change request after approval (FR-059)**: WP03 was approved with a fixed block-of-week bucket over
> "the trailing seven days". The homeowner has asked for a configurable four-week window with a choice of
> grouping. This is being reworked as a follow-up cycle. Scope of the rework: `usage_profile` and its
> tests in `series.py` / `test_series.py`, PLUS a small documented cross-WP extension to
> `pyscript/modules/config.py` and `tests/test_config.py` (WP01-owned, approved): add
> `usage_history_weeks` (int, default 4, >= 1) and `usage_grouping` (`same_weekday` | `day_type`,
> default `same_weekday`), read from the `usage:` section (`usage.history_weeks`, `usage.grouping`),
> validated with the same collect-all-errors behaviour, and INCLUDE BOTH in `fingerprint()` (so
> changing them invalidates the cached usage profile). Update the WP01 fingerprint tests that assert
> the exact field set. Also add the `usage:` block to `battery_planner/user_config.example.yaml` and,
> while there, `peak_averaging_months: 13` under `capacity_tariff` (added by WP11, still missing from
> the example). Do not change the solar half of `series.py`.

### T013 — Build the usage profile from trailing history

**Purpose**: Expected household consumption per block, learned from this house rather than assumed.

**Steps**:

1. Define `UsageSlot` per `data-model.md`: `block_start`, `expected_kwh`, `sample_days`.
2. Write `usage_profile(history, config, start_time, horizon_end)` where `history` is a sequence of
   `(timestamp, kwh_consumed_in_that_interval)` readings supplied by the adapter.
3. Group history by **time of day within a day group**, never by time of day alone. The day group
   is chosen by `config.usage_grouping` (FR-059):
   - `same_weekday` (default): a Wednesday 18:00 block is the mean of the 18:00 block on each
     Wednesday in the window. Seven groups, 7 x 96 = 672 buckets, up to `usage_history_weeks`
     samples each (4 by default).
   - `day_type`: a Monday-to-Friday block is the mean of that time of day across **every weekday**
     in the window, and a Saturday or Sunday block the mean across **every weekend day**. Two
     groups, 2 x 96 = 192 buckets, up to roughly 5x more samples each (about 20 for weekdays, 8
     for weekend days at four weeks).
   Weekend days pool with each other only, never with weekdays: the existing `configuration.yaml`
   day/night tariff logic already encodes that weekends behave differently.
4. Average each bucket across the trailing window of `config.usage_history_weeks` weeks (4 by
   default). Readings older than the window are ignored. The module filters by the window itself
   (relative to `start_time`) so the result does not depend on how much history the caller passes.
5. Project the profile forward across the horizon: for each block in the horizon, look up its
   block-of-week bucket.
6. An empty bucket (no history for that slot) falls back to the overall mean rather than zero. Zero
   would tell the planner the house uses no power at that time, which is never true and would make
   it under-buy.

**Files**: `pyscript/modules/series.py` (continues)

**Validation**:
- Four weeks of flat 0.25 kWh readings yields 0.25 kWh in every block under both groupings
- `same_weekday`: a history where Tuesdays differ from Wednesdays produces different values for those days, and a Wednesday block equals the mean of exactly the Wednesdays in the window
- `day_type`: a Wednesday block equals the mean over Monday-Friday of the window, so Tuesday and Wednesday get the SAME value; Saturday and Sunday get the same weekend value, different from the weekday one
- Readings older than `usage_history_weeks` do not affect the result under either grouping
- Switching the grouping changes the output for the same history (so the cache fingerprint must cover it)
- A bucket with no samples gets the overall mean, not zero

---

### T014 — Handle short history with an honest sample count

**Purpose**: A fresh install has less than the configured window of history. Work with what exists and say so.

**Steps**:

1. Count how many distinct days contributed to each bucket and record it as `sample_days`.
2. With less history than the window, average over what there is rather than failing or padding. Note `day_type` grouping fills sooner: after a single Monday and Tuesday it already has weekday data for every weekday, whereas `same_weekday` has nothing yet for Wednesday and falls back to the overall mean.
3. `sample_days` must reach the decision record's degraded markers (FR-027) — a plan built on one
   day of history is a different kind of claim from one built on seven.
4. With **no** history at all, return a series of zeroes with `sample_days = 0`. The adapter decides
   what to do about that; the series module does not raise.

**Files**: `pyscript/modules/series.py` (continues)

**Validation**:
- `sample_days` is the number of distinct dates that fed the slot; with `day_type` and three weekdays of history a weekday slot has `sample_days = 3`, with `same_weekday` a slot has 0 or 1
- Empty history gives zeroes with `sample_days = 0` and no exception
- `sample_days` is per-slot, since a partial day may cover some slots and not others

---

### T015 — Test both series, including fallback and short history

**Purpose**: Cover the unit conversions, the extensive/intensive distinction, and the degraded paths.

**Steps**:

1. Create `tests/test_series.py`.
2. **Energy conservation test** — the most valuable single test here: build a payload with a known
   total in watt-hours, expand it, and assert the summed block kWh equals payload total ÷ 1000
   within floating-point tolerance. This catches the unit error and the quarter-vs-whole error at once.
3. Test that an hourly 1000 Wh period becomes four blocks of 0.25 kWh — explicitly, so the split is
   documented in the test name.
4. Test the zero-solar fallback for `None`, `{}`, and a malformed payload.
5. Test the usage profile with synthetic flat history, with weekday/weekend-differentiated history,
   and with three days of history asserting `sample_days = 3`.
6. Test the empty-bucket fallback uses the overall mean, not zero.
7. Test that both series span the full horizon even when their source data runs out early.

**Files**: `tests/test_series.py` (new)

**Validation**:
- `pytest tests/test_series.py -v` passes
- The energy-conservation assertion is present and would fail under either unit mistake

---

## Definition of Done

- `pyscript/modules/series.py` builds both series on the block grid, spanning the full horizon
- Solar energy is conserved through expansion; the zero fallback is flagged on every block
- The usage profile buckets by block-of-week and reports honest sample counts
- No file I/O, no third-party imports, no Home Assistant or pyscript imports
- `tests/test_series.py` passes, including the energy-conservation assertion
- `pytest tests/` is green, including WP01 and WP02 tests

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Watt-hours read as kilowatt-hours** | Energy-conservation test; name variables with units |
| **Giving each sub-block the full period energy** instead of a quarter | Same test catches it; remember energy is extensive, price is intensive |
| Bucketing by block-of-day, losing the weekday/weekend split | Test with differentiated history |
| Zero for an empty usage bucket | Fall back to the overall mean; zero would make the planner under-buy |
| Truncating the horizon to the forecast's coverage | Pad with zeroes instead; the price horizon governs |
| Carrying a stale forecast forward on failure | Zero is the conservative choice and is what FR-023 specifies |

## Reviewer guidance

Start with the energy-conservation test. If summed block energy does not equal payload energy, one of
the two classic mistakes is present — and both produce plausible-looking numbers that would survive
casual inspection while making the battery saturate at entirely the wrong time.

Then check the sub-block expansion in both this WP and WP02 side by side. **They must differ**:
prices repeat across sub-blocks, energy divides across them. If both do the same thing, one is wrong.

Finally, confirm the usage profile buckets by block-of-week. A block-of-day implementation will pass
casual tests and then systematically mispredict every weekend.
