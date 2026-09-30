---
affected_files: []
cycle_number: 2
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-30T04:49:47Z'
reviewer_agent: user
wp_id: WP12
---

# WP12 review, cycle 2: changes requested (one blocking issue, small fix)

Verified fixed and kept: units (kW as is, W /1000, other or missing unit = rate-limited no-op, on all three sensors including config.offtake_sensor; the W probe now gives the kW decision). Cancellation: flags and the entity are set before inverter.apply on start, change and stop; the CancelledError tests pass; no duplicate start/stop record. First run forces the entity to "off"; None counts as "maybe shaving" in the grace logic; the error path clears the flag after GRACE_SECONDS. V3 is rendered bare and tested; the MAX_HISTORY_SAMPLES comment is honest; the FUTURE RISK note (add back commanded discharge, heartbeat) is present. Netted sensor only, narrow imports, file I/O only in @pyscript_executor, no per-tick records. 369 tests pass; only peak_guard.py and test_peak_guard.py changed. Mutations caught: per-phase sum, log every tick, shave whole offtake, drop freshness, drop fallback, drop unit check, W not scaled, flag after apply, no prime, None-as-off grace, no error clear, V3 dropped.

## Required

**Issue 1: The last_updated freshness check can never fire in real pyscript. Production runs only on the 15 s fallback, and the tests pass because they fake an API that does not exist.**

Pyscript source (custom_components/pyscript/state.py):
`STATE_VIRTUAL_ATTRS = {"entity_id", "last_changed", "last_updated", "last_reported"}`. `state.getattr("sensor.x")` returns `hass.states.get(name).attributes.copy()`. For a StateVal argument it pops the STATE_VIRTUAL_ATTRS. Either way the dict **never contains last_updated**. HA State.attributes does not hold the timestamps; they are properties of the State object. So `_read_sensor` always returns `avg_updated=None` in HA. FakeState.getattr in the tests injects `last_updated` into that dict, which real pyscript never does (anti-pattern 2: synthetic fixture). The docstring statement "read via state.getattr" is false.

Consequences in production, reproduced with a FakeState whose getattr behaves like the real one:
- A previous-window average still present at 12:15:16 (for example a late or throttled telegram) gives `discharge 1.0 kW`, the same false shave as in cycle 1. The addendum's "a stale value NEVER produces a discharge" holds only if the meter refreshes within 15 s.
- The "not refreshed since window began" warning fires in EVERY window: 24 in 6 h, 96 a day in the HA log. It claims a staleness that was never measured.

Fix:
1. Take the timestamps from the StateVal that `state.get(entity)` returns, not from getattr. Pyscript sets `.last_updated`, `.last_changed` and `.last_reported` on it. Use `last_reported` when present (HA 2024.3+), else `last_updated`:
   `raw = state.get(entity); stamp = getattr(raw, "last_reported", None) or getattr(raw, "last_updated", None)`.
   Keep `state.getattr` for unit_of_measurement only.
2. **Why last_reported and not last_updated.** HA does not bump last_updated when a sensor writes the same state and attributes again (no force_update). Only last_reported moves. Picture a genuinely current average that repeats the previous window's exact value, for example a quiet house at 0.000, or a running-mode average holding steady at the meter's resolution. With last_updated that reading looks stale for as long as the value holds, and the 15 s fallback never rescues it, because a timestamp IS present. For accumulating mode at 0.0 this is mostly harmless: nothing is drawn, so there is nothing to shave, and the first real offtake changes the value. But a constant average while the load is high (running mode) would blind the guard during a real peak. last_reported is bumped on every write, so it closes this gap.
3. Fall back to the 15 s rule only when neither timestamp exists.
4. In the fallback path, do NOT emit the per-window "not refreshed" warning (log at most once per WARN_EVERY_SECONDS, or at debug level). Nothing was measured, and 96 warnings a day is noise.
5. Tests: make FakeState.get return a str subclass carrying last_updated/last_reported attributes, and make FakeState.getattr return ONLY real attributes (never timestamps), so the tests model pyscript. Cover: stale last_reported = no action; fresh last_reported with an unchanged last_updated from the previous window = acts; only last_updated present; neither present = 15 s rule with no per-window warning.
6. Correct the docstring bullet.

## Should fix (small)

- `_as_datetime` calls `ZoneInfo("UTC")` on the event loop, and the file routes ZoneInfo through `_zone` precisely to avoid that. Use `datetime.timezone.utc` instead. Treating a naive value as UTC is reasonable: HA timestamps are always tz-aware UTC, so this branch is only a fallback.

## Notes (no change required)

- Veto-while-shaving ordering: the vetoed record is emitted (awaited) BEFORE the stop transition clears the flags. A kill at that await leaves the entity "on" for one more run (about 1 s), and the next run then writes "shaving stopped". The lag is bounded and there is no duplicate. Optionally move the flag clear above the veto emit for consistency with the documented rule.
- A run killed between the flag update and the log append loses one record, for example a stop record with no start record. The addendum accepts this, and the docstring documents it. That is fine.
- Residual lock-out: if peak_guard.py is removed or fails to load while the entity is "on", nothing clears it until an HA restart (pyscript.* entities are not persisted). The planner could defensively ignore "on" when the entity is older than some bound. That needs a heartbeat, which is already in the FUTURE RISK list. Not required for WP12.
