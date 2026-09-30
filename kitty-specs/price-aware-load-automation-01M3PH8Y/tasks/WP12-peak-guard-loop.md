---
work_package_id: WP12
title: Peak guard loop
dependencies:
- WP08
- WP09
- WP11
requirement_refs:
- FR-051
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T058
- T059
- T060
- T061
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/peak_guard.py
create_intent:
- pyscript/peak_guard.py
execution_mode: code_change
owned_files:
- pyscript/peak_guard.py
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

A small, fast loop that does one thing: watch the quarter-hour window and call for discharge before
a peak is set. Separate from the five-minute planner, because a peak can form and be locked in long
before the next planning cycle runs.

Satisfies **FR-051**. Enforces **NFR-010, NFR-011**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP12 --agent <name>`. Depends on WP08 (the helpers it
  reads), WP09 (the boundary it calls) and WP11 (the arithmetic it applies).

## Why this is a second loop

The planner runs every five minutes because building a 140-block trajectory is expensive and prices
do not move faster than that. A quarter-hour capacity window is 900 seconds, and a peak set in one
is billed for the next twelve months.

At a five-minute cadence the guard would see a forming peak, at best, on the third sample — a third
of the window already spent and the average largely determined. That is too late.

So: this loop is deliberately **cheap and narrow**. It evaluates S0 and nothing else. No trajectory,
no price derivation, no cache, no forecast. Read three numbers, do arithmetic from WP11, and either
call for discharge or do nothing. If you find yourself importing `trajectory` or `prices` here,
something has gone wrong.

## Context you need

- `data-model.md` — `GridState` and its derived formulas
- `contracts/inverter-boundary.md` — the `apply()` call
- `research.md` R-02 — why file I/O must be guarded
- WP08 T049 — the three meter entities; WP11 T067 — the detector this loop feeds samples to

---

### T058 — Build the guard loop skeleton

**Purpose**: A trigger that fires fast, cheaply, and without tripping over itself.

**Steps**:

1. Create `pyscript/peak_guard.py`.
2. Trigger on **both** a state change of the offtake sensor and a periodic tick, so the guard reacts
   promptly to a step change in load *and* cannot sit idle if the sensor goes quiet:
   ```python
   @state_trigger("sensor.slimmelezer_power_consumed")
   @time_trigger("period(now, 30sec)")
   @task_unique("peak_guard")
   def peak_guard():
       ...
   ```
3. `@task_unique` prevents overlap. Use the same guard interval the config declares
   (`guard_interval_seconds`, default 30).
4. Keep the body under 200 ms (NFR-010). That budget is generous for three state reads and some
   arithmetic, and it is what keeps a 30-second cadence harmless.
5. Wrap the body so an exception is logged without killing the trigger. A dead guard is silent, and
   its absence shows up only on a bill twelve months later.
6. Read the same `user_config.yaml` the planner uses. Reuse WP10's load-and-cache approach rather
   than inventing a second one; a divergence between the two loops' configuration would be very
   hard to spot.

**Files**: `pyscript/peak_guard.py` (new)

**Validation**:
- The guard fires on a change in metered offtake within a second or two
- It also fires on its periodic tick when the sensor is static
- Invocations never overlap
- An exception is logged and the next tick still fires

---

### T059 — Read the capacity position

**Purpose**: Turn three Home Assistant entities into a `GridState`.

**Steps**:

1. Read **`sensor.slimmelezer_power_consumed`** for `offtake_kw`. This is the meter's **netted
   three-phase total** (C-012).

   Do not sum `..._power_consumed_phase_1/2/3`. On this connection one phase commonly exports while
   the others import — a real observed sample had per-phase imports summing to 0.937 kW while the
   meter's netted total read 0.003 kW. Summing per-phase figures would have the guard discharging
   the battery to shave a peak that does not exist.
2. Read **`sensor.slimmelezer_huidig_kwartiervermogen`** (`1-0:1.4.0`) for the quarter-hour average,
   normalised by WP11 according to the active mode. While the mode is being detected, **retain each
   sample with its elapsed time and the instantaneous offtake alongside it** — WP11's detector (T067)
   needs pairs from the same window to settle the question. Discard the buffer at each window
   boundary; samples from different windows cannot be compared.
3. Read **`sensor.slimmelezer_maandpiek`** (`1-0:1.6.0`) for `month_peak_kw`, and
   **`sensor.slimmelezer_gemiddelde_maandpiek_13_maanden`** for the billed average used in costing.
4. Build the `GridState` via WP11's `build_state`, passing `now`.
5. Handle unavailable entities the way the planner handles a missing forecast, not the way it
   handles missing prices: **log it, do nothing, and let the next tick try again.** A guard that
   cannot read the meter must not command a discharge on a guess — but it also must not halt the
   system, because the planner is still working fine.
6. `is_restored` is no longer a concern the planner owns: the meter keeps these registers, so a
   Home Assistant restart cannot lose a window in progress. Set the flag only if a sensor reports
   `unknown` after a restart and then recovers, so a record built during that gap says so.

**Files**: `pyscript/peak_guard.py` (continues)

**Validation**:
- `offtake_kw` matches the netted sensor, not a per-phase sum
- An unavailable sensor produces a logged no-op, not an exception and not a halt
- `GridState` fields line up with what Developer Tools shows for the same instant

---

### T060 — Evaluate S0 and act

**Purpose**: The decision itself — call for discharge, or do nothing.

**Steps**:

1. Call WP11's `shave_kw(state, config)`. Zero means nothing to do; end the tick quietly.
2. A positive value means a peak is forming. Apply the vetoes that still matter here:
   - **V1 (reserve floor)** — if the battery is at or below reserve, the shave is vetoed. Record
     that the peak was allowed to form because the battery was empty. Nothing else can be done, and
     saying so plainly is more useful than silence.
   - **V2 does not apply** — shaving serves household load, it does not export.
   - **V3 does not apply** — this is a discharge, not a grid draw.
3. Hand the action to `inverter.apply("discharge", shave_power, record)` (`import inverter`: it is a pyscript module under `pyscript/modules/`, importable from scripts; top-level script files are not). The boundary logs it and
   transmits nothing, exactly as in the planning loop.
4. **Do not write a record on every tick.** At 30-second intervals that would be 2,880 lines a day
   of "nothing to do", burying the planner's 288 real decisions. Record only when:
   - a shave is proposed, or
   - a shave is vetoed, or
   - the guard's state changes materially (it starts or stops shaving)
5. Mark guard records distinctly — `selector=S0` and a `source=guard` field — so they are
   distinguishable from planner records when reading the log.

**Files**: `pyscript/peak_guard.py` (continues)

**Validation**:
- A forming peak with usable charge produces a discharge call and one record
- A battery at reserve produces a vetoed record naming V1, not a discharge
- A quiet window produces no records at all
- Guard records are visibly distinguishable from planner records

---

### T061 — Keep the two loops from fighting

**Purpose**: The planner and the guard both command the battery. They must not contradict each other
every thirty seconds.

**Steps**:

1. The failure to avoid is straightforward: the planner decides to charge from the grid in a cheap
   hour, the guard sees offtake rise and calls for discharge, the planner charges again on its next
   cycle. The battery cycles pointlessly and both logs look individually reasonable.
2. **Give the guard precedence and make it explicit.** While the guard is actively shaving, record
   that state somewhere the planner reads, and have the planner treat a grid-charge proposal as
   vetoed for as long as it holds. This is FR-050's ordering expressed across two processes rather
   than within one loop.
3. In practice the grid budget already prevents most of this — a window near its ceiling has little
   or no budget, so V3 fires and the planner does not propose charging anyway. The explicit state is
   the belt to that braces: budget arithmetic is a projection, and the guard has fresher numbers.
4. Clear the shaving state when `shave_kw` returns to zero, and record the transition.
5. Document the interaction at the top of the file. The next person to read these two loops needs to
   understand which wins before they change either.

**Files**: `pyscript/peak_guard.py` (continues)

**Validation**:
- With the guard shaving, a planner cycle does not propose grid charging
- When shaving stops, the planner resumes normally on its next cycle
- The transition in and out of shaving is recorded
- The log makes it clear which loop produced each record

---

## Definition of Done

- The guard fires on metered change and on a periodic tick, without overlapping
- It reads the **netted** offtake sensor, never a per-phase sum
- It evaluates S0 only — no trajectory, no prices, no cache, no forecast imported
- A forming peak produces a discharge call; a battery at reserve produces a vetoed record
- Quiet ticks produce no log lines
- The guard takes precedence over planner grid-charging, and the interaction is documented in the file
- An unreadable sensor is a logged no-op, never a halt

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Summing per-phase sensors** instead of the netted total | Would shave a phantom 0.9 kW peak; C-012 and T059 both call it out |
| Logging every 30-second tick | 2,880 lines a day would bury 288 real decisions |
| The two loops fighting over the battery | Explicit shaving state the planner honours (T061) |
| Reacting to instantaneous power rather than the window average | All arithmetic comes from WP11, which reasons in windows |
| A halt when the meter is unavailable | Log and no-op; the planner is unaffected |
| Importing the planner's heavy modules | Defeats the point of a second loop |
| An exception killing the trigger silently | Wrap the body; a dead guard shows up on a bill, not in the log |

## Reviewer guidance

**Check which sensor is read, first.** If `peak_guard.py` sums per-phase power anywhere, it will
discharge the battery against a peak that does not exist — on the sample we have, reading 0.937 kW
where the meter says 0.003. That is a wrong action taken confidently, and the log would look entirely
sensible.

**Then check the logging volume.** Every-tick records make the decision log useless, and the log is
this mission's only deliverable. Records belong on proposals, vetoes and state transitions only.

**Then check the two-loop interaction.** Read T061's documentation block at the top of the file and
decide whether you could predict what happens when a cheap hour and a forming peak coincide. If the
answer is not obvious from reading, it will not be obvious at 7am when the battery has been cycling
all night.

Finally, confirm the guard stays narrow. `grep` its imports: `capacity`, `config`, and the inverter
boundary belong there. `trajectory`, `prices`, `series` or `cache` do not — their presence means the
cheap fast loop has quietly become a second expensive one.
