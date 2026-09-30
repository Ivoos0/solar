---
affected_files: []
cycle_number: 1
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-30T04:41:51Z'
reviewer_agent: user
wp_id: WP12
---

# WP12 review, cycle 1: changes requested

What is right, keep it: the netted sensor (config.offtake_sensor) is the only offtake read, and no per-phase entity is read. All arithmetic goes through capacity.shave_kw, so the 2.5 kW floor and the max_discharge cap hold. V1 is recorded once per window with an honest reason, and V2/V3 do not block a shave. Records are written only on start, stop, a change of 0.25 kW or more, or a veto. The coordination entity and the T061 documentation block are clear. The guard stays narrow (no trajectory/prices/series/cache) and all file I/O runs in @pyscript_executor helpers. There are 344 tests and they pass. Mutation checks: summing the per-phase sensors, recording every tick, shaving below the floor, vetoing every tick and not clearing the buffer are each caught by at least one test.

## Required

**Issue 1: A stale quarter-hour average at a window boundary causes a false discharge (WHEN it acts).**
Take the first seconds of a new window. If `sensor.slimmelezer_huidig_kwartiervermogen` still holds the previous window's value, it gets normalised as energy so far in the new window. This happens in accumulating mode, which is also the default when the mode is only assumed. The step is `reported*15/max(elapsed,1)`, then `*1/60`, which gives about `reported*0.25` kWh as if it were already drawn.
I reproduced it in a scratch copy using the test harness: accumulating mode, peak 4.0, offtake 3.5 and a stale avg of 3.9 at 12:15:02, sent as a state trigger. Result: `discharge 1.0 kW` ("shaving started"). Then at 12:15:04, once avg=0.0, an `idle` ("shaving stopped"). So a window that ran near the peak is followed by a false discharge, two records and a flag flip on->off.
A stale value is likely, not only possible: the state trigger is on the offtake sensor, and ESPHome publishes a telegram's sensors one after another. The guard can therefore run on the new offtake while the quarter-average entity still shows the old window (I have not confirmed this in HA).
Fix: before acting on a reading, require that the quarter-average entity changed after `window_start`, for example by comparing its `last_changed`/`last_updated` with window_start. If you cannot rely on that, make the first N seconds of a window a no-op and log it rate-limited (not every tick). Add a test for this boundary case.

**Issue 2: Units are assumed kW and never checked (WHAT it reads).**
`_read_float` parses the state as a bare float. A probe with offtake="5000", avg="4000", peak="2500" (W) gave `discharge 5.0 kW`. offtake="3000" W with peak="2.5" also gave `discharge 5.0 kW`. In HA a user can change the display unit of a power sensor to W, and that changes the state value.
Fix: read `unit_of_measurement` from each of the three entities and accept only kW. Either convert W explicitly or treat anything else as unreadable (the existing logged no-op path, the same as unknown). Add tests for W and a missing unit.

**Issue 3: @task_unique can cancel a run between the log write and the flag update.**
`@task_unique("peak_guard")` defaults to kill_me=False, so every new trigger kills the run in progress. The offtake sensor changes about once a second, and the 1 s throttle is checked inside the NEW task after it has already killed the old one.
`_emit` awaits `inverter._append_line` (a @pyscript_executor call, with fsync). If the run is cancelled at that await, the line is still written, because the executor thread completes. But `_flags["shaving"]`, `_flags["shave_kw"]` and `_set_shaving_entity` never run. The next run then writes a second "shaving started" (or "stopped") record, and the planner flag lags.
Fix: update the flags and the entity before awaiting apply. Or use `@task_unique("peak_guard", kill_me=True)`, so a new trigger is dropped while a run is in progress (the next trigger is at most about 1 s away). Say in the header which you chose and why, and add a comment or test that pins the order.

## Should fix (small)

4. **Stale "on" flag.** Suppose pyscript reloads, which resets module state (`shaving=None`) while `pyscript.peak_guard_shaving` stays "on". If the meter is then unreadable, `_handle_unreadable` never clears the flag, because `None` is falsy. The `except` path in `peak_guard` never clears it either, for example after a config error while shaving. Either way the planner can stay locked out of grid charging with no time limit. Treat `None` like `True` for the grace clear, and apply the same grace in the error path.
5. **The V3 docstring does not match the records.** The header says V3 is "neither applied nor rendered". But `decision.render_vetoes` renders every fired veto that blocked nothing, so a vetoed record reads `vetoes=V1(suppressed S0 discharge),V3` (seen in a probe). Shave records, on the other hand, pass `[]` even when V3 fired. Choose one of the two and make the docstring match. Passing the fired list through, so V3 appears bare, matches the planner's records.
6. **History size.** `MAX_HISTORY_SAMPLES = 48` keeps 48 samples, which is 24 windows (2 per window), not 48 windows. Rename it or document it.

## Note (no change needed in WP12)

- **Record fields the guard cannot know.** solar_rem, usage_rem and spill render 0.00kWh, saturation and breach render none, and end_soc equals the current stored kWh. The contract allows `0.00kWh`/`none` for values that do not apply, and `why` says "not evaluated". But usage_rem=0.00kWh on a discharge record can read as a real zero. The honest rendering is `n/a`. That needs `decision.DecisionRecord`/`_kwh` to accept None, which is outside WP12's owned files. Raise it as a follow-up for the decision.py owner.
- **Before real transmission.** Two changes will be needed. (a) The guard reads NET offtake, which its own discharge reduces, so the shave would bang-bang on->off. It needs to add back the commanded discharge. (b) A discharge command will probably need re-sending while shaving (a heartbeat), but today records are written only on change.
