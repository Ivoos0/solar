---
work_package_id: WP04
title: Battery state and charge trajectory
dependencies:
- WP02
- WP03
requirement_refs:
- FR-006
- FR-007
- FR-008
- FR-029
- FR-031
- FR-032
- FR-034
- FR-039
- FR-040
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T016
- T017
- T018
- T019
- T020
- T021
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/trajectory.py
create_intent:
- pyscript/modules/battery.py
- pyscript/modules/trajectory.py
- tests/test_trajectory.py
execution_mode: code_change
owned_files:
- pyscript/modules/battery.py
- pyscript/modules/trajectory.py
- tests/test_trajectory.py
- tests/fixtures/**
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

Project the battery's charge forward block by block across the horizon, and surface the three facts
a single scalar balance cannot: **when the battery fills**, **how much solar spills after that**, and
**when it would hit the reserve floor**.

Satisfies **FR-006, FR-007, FR-008, FR-029, FR-031, FR-032, FR-034, FR-039, FR-040**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP04 --agent <name>`. Depends on WP02 and WP03 — both must
  land first.

## Why this work package matters more than the others

The specification originally computed a single figure — total expected production against total
expected consumption — and decided from that. That is not merely coarser than a per-block
projection; **it is wrong in one consistent, expensive direction**. A scalar can only ever argue for
waiting, because it has no way to represent the cost of waiting. It cannot see a battery sitting at
100% through the sunniest hours of the day, spilling free solar while it holds charge for a better
price that evening.

Everything here exists to make that visible. If this module is subtly wrong, every selector built on
it in WP05 inherits the error, and the decision log will look entirely reasonable while being wrong.

## Context you need

- `data-model.md` — `BatteryState`, `TrajectoryBlock`, `Trajectory`
- `spec.md` FR-008, FR-029, FR-031, FR-032 and the edge-case list
- `spec.md` SC-011, SC-012 — what "correct" is measured against

---

### T016 — Build `battery.py`: BatteryState from capacity and charge percent

**Purpose**: One definition of how full the battery is, shared by every rule.

**Steps**:

1. Create `pyscript/modules/battery.py`.
2. Define `BatteryState` with the `data-model.md` fields: `charge_percent`, `stored_kwh`,
   `usable_kwh`, `headroom_kwh`, `is_stubbed`.
3. Write `from_percent(charge_percent, config, is_stubbed=True)`:
   ```
   stored_kwh   = capacity_kwh * charge_percent / 100
   usable_kwh   = max(0, stored_kwh - capacity_kwh * reserve_percent / 100)
   headroom_kwh = capacity_kwh - stored_kwh
   ```
4. Floor `usable_kwh` at zero — below the reserve it is not negative usable energy, it is none.
5. Default `is_stubbed` to `True` for this mission (C-003) and carry it through, so the decision
   record can mark every cycle `soc_stubbed` (FR-027).

**Files**: `pyscript/modules/battery.py` (new, ~50 lines)

**Validation**:
- 10 kWh capacity at 50% with a 10% reserve: stored 5.0, usable 4.0, headroom 5.0
- At 5% (below reserve): stored 0.5, usable **0.0** not −0.5, headroom 9.5
- At 100%: headroom 0.0

---

### T017 — Build the trajectory block loop with capacity and power clamps

**Purpose**: The projection itself. Walk the horizon block by block, applying solar and usage.

**Steps**:

1. Create `pyscript/modules/trajectory.py` and define `TrajectoryBlock` per `data-model.md`.
2. Write `project(battery_state, solar_series, usage_series, price_map, config)` returning a
   `Trajectory`.

   **Iterate the dense contiguous block grid** from now to `horizon_end`, looking each block's
   solar, usage and price up **by block start time** (FR-039). The price map may have holes; solar
   and usage never do. Set `has_price = False` on a block with no published price — it is still
   projected, because the battery charges and discharges regardless, but WP05 will refuse to choose
   it as an export or import window (FR-040).

   Never index the three series positionally against each other. A single missing market hour would
   then shift solar onto the wrong price for the rest of the horizon, and every resulting decision
   would look perfectly reasonable.
3. For each block, in order:
   ```
   net = solar_kwh - usage_kwh

   if net > 0:                      # surplus available to store
       max_by_power   = config.max_charge_kw * (config.block_minutes / 60)
       max_by_headroom = capacity_kwh - charge
       absorbed = min(net, max_by_power, max_by_headroom)
       spilled  = net - absorbed
       charge  += absorbed
   else:                            # household draws from the battery
       drawn   = min(-net, config.max_discharge_kw * (block_minutes / 60),
                     charge - reserve_kwh)
       charge -= drawn
       absorbed = 0.0
       spilled  = 0.0

   charge = min(max(charge, reserve_kwh), capacity_kwh)
   ```
4. **Two ceilings bind, not one.** `max_by_power` and `max_by_headroom` are independent: a block
   whose solar exceeds the maximum charge rate spills the excess **even with capacity headroom
   remaining**. This is why spill can begin before saturation, and it is the detail most likely to
   be dropped.
5. Record every field on each `TrajectoryBlock`, including `absorbed_kwh` and `spilled_kwh` —
   they are needed for FR-031 and for the record.
6. This projection describes what happens **if the planner does nothing**. It is the baseline the
   selectors reason against; it does not include their proposed actions.

**Files**: `pyscript/modules/trajectory.py` (new)

**Validation**:
- A block with no solar and no usage leaves charge unchanged
- A block with 3 kWh of surplus at `max_charge_kw: 5.0` and 15-minute blocks absorbs at most
  **1.25 kWh** (5 kW × 0.25 h) and spills 1.75 kWh — with headroom to spare
- Charge never exceeds capacity and never falls below the reserve floor
- Discharge is limited by `max_discharge_kw` per block

**The invariant that must hold**: energy is conserved. For every block,
`solar_kwh == usage_covered + absorbed_kwh + spilled_kwh` (within tolerance). Energy that vanishes
in a clamp instead of being reported as spill makes the planner look better than it is, and nothing
downstream would notice.

---

### T018 — Detect saturation and quantify spill

**Purpose**: The two facts that make the export selector correct rather than merely plausible.

**Steps**:

1. `saturation_block` is the `block_start` of the **first** block whose projected charge reaches
   capacity. `None` if the battery never fills across the horizon.
2. Use a small tolerance when comparing against capacity — floating-point accumulation over ~140
   blocks will not land exactly on the ceiling. A tolerance of 1e-6 kWh is ample.
3. `total_spill_kwh` sums `spilled_kwh` across **all** blocks, not only those after saturation.
   Power-limited spill can occur before the battery is full, and it is just as real.

   Note that `data-model.md` describes spill as "solar arriving after saturation". That is the
   common case and the motivating one, but the power ceiling means it is not the only one. Sum
   everything; the distinction between pre- and post-saturation spill belongs in the reasoning text,
   not in the total.
4. Once saturated, the battery may later discharge below capacity and re-saturate. `saturation_block`
   records the **first** crossing — that is what bounds the export window in WP05.

**Files**: `pyscript/modules/trajectory.py` (continues)

**Validation**:
- A sunny fixture with a small battery saturates at a known block
- A winter fixture never saturates: `saturation_block is None`, `total_spill_kwh == 0.0`
- A fixture with a brief high-power solar burst spills **without** saturating — proving the power
  ceiling is independent of the capacity ceiling

---

### T019 — Detect reserve breach and compute leftover

**Purpose**: The mirror of saturation, bounding the import window.

**Steps**:

1. `reserve_breach_block` is the `block_start` of the first block whose projected charge reaches the
   reserve floor. `None` if it never does.
2. `leftover_kwh` is charge above the reserve floor at the **final** block of the horizon — what the
   plan expects to still be holding when visibility runs out.
3. `horizon_end` comes from the price series (WP02), not from the solar forecast. Blocks beyond
   forecast coverage carry zero solar and are still projected.
4. Use the same tolerance convention as saturation.

**Files**: `pyscript/modules/trajectory.py` (continues)

**Validation**:
- A fixture with heavy evening usage breaches at a known block
- A fixture that stays well charged has `reserve_breach_block is None` and positive `leftover_kwh`
- `leftover_kwh` is never negative — clamped at the floor, it is zero at worst

---

### T020 — Author golden fixtures with known answers

**Purpose**: Encode the cases live data will rarely hand you, with answers computed by hand.

**Steps**:

1. Create `tests/fixtures/` with small, **hand-computable** series. Keep them short — 8 to 16 blocks
   is plenty, and a fixture you can verify with a pocket calculator is worth more than a realistic
   one you cannot.
2. Build at least these five:

   | Fixture | Shape | What it pins |
   |---|---|---|
   | `sunny_saturates` | Strong midday solar, small battery | Saturation block, post-saturation spill |
   | `power_limited` | Solar burst above `max_charge_kw`, plenty of headroom | **Spill without saturation** |
   | `winter_breach` | No solar, steady usage | Reserve breach block, zero spill |
   | `balanced` | Solar matches usage | No saturation, no breach, leftover unchanged |
   | `saturate_then_drain` | Fills at midday, drains that evening | First-crossing semantics for saturation |

3. For each, write the expected values as **explicit constants in the test**, derived by hand and
   commented with the arithmetic. Do not compute expectations with the code under test — that
   proves only self-consistency.
4. Keep fixtures as plain Python data structures in a module rather than JSON files. They are read
   by humans reviewing the arithmetic, and inline comments explaining each figure matter more than
   format purity.

**Files**: `tests/fixtures/` (new; a Python module of fixture data, ~150 lines with comments)

**Validation**:
- Each fixture's expected values are hand-derivable from its inputs
- The arithmetic is commented so a reviewer can check it without running anything

---

### T021 — Test the trajectory against the golden fixtures

**Purpose**: Prove the projection, the two ceilings, and the three detections.

**Steps**:

1. Create `tests/test_trajectory.py`.
2. For each fixture, assert `saturation_block`, `total_spill_kwh`, `reserve_breach_block`, and
   `leftover_kwh` against the hand-computed constants.
3. **Add the energy-conservation test** across every fixture: for each block,
   `absorbed + spilled + usage_covered == solar` within tolerance. This is the test most likely to
   catch a clamping bug, and it applies to every fixture at once.
4. Test `battery.py` separately — the three derived figures at 0%, below reserve, at reserve, at
   50%, and at 100%.
5. Test that the trajectory spans the full price horizon even when the solar series runs out early.
7. **Price-gap fixture**: a horizon with one missing market hour. Assert those blocks are still
   projected with correct solar and usage, carry `has_price = False`, and that every other block's
   price still lines up with its own start time — nothing shifted.
6. Test the floating-point tolerance behaves: a fixture landing within 1e-9 of capacity is treated as
   saturated, not missed.

**Files**: `tests/test_trajectory.py` (new, ~200 lines)

**Validation**:
- `pytest tests/test_trajectory.py -v` passes
- The `power_limited` fixture asserts spill > 0 while `saturation_block is None`
- Energy conservation holds for every block of every fixture

---

## Definition of Done

- `battery.py` derives stored, usable and headroom energy with `usable_kwh` floored at zero
- `trajectory.py` projects block by block honouring **both** the capacity ceiling and the charge-power
  ceiling, and reports saturation, spill, breach and leftover
- Energy is conserved in every block — nothing disappears into a clamp
- Five golden fixtures exist with hand-derived expectations and commented arithmetic
- The trajectory is computed fresh; nothing here caches it (FR-034)
- `pytest tests/` is green

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Only the capacity ceiling implemented**, power ceiling forgotten | The `power_limited` fixture fails loudly |
| Energy lost in a clamp rather than reported as spill | Per-block conservation assertion |
| Exact float comparison against capacity | Tolerance of 1e-6; ~140 accumulating additions will not land exactly |
| Spill counted only after saturation | Sum all blocks; power-limited spill is real spill |
| `saturation_block` recording the last crossing, not the first | `saturate_then_drain` fixture pins it |
| Computing test expectations with the code under test | Hand-derive and comment the arithmetic |
| Caching the trajectory for speed | FR-034 forbids it — it depends on live charge and elapsed blocks |

## Reviewer guidance

Three checks, in order of value:

**First, the two ceilings.** Read the absorption line. It must take a `min` of three things — the
available surplus, the power limit, and the headroom. An implementation with only two is the most
likely defect in this work package, and its symptom is a battery that fills faster than physics
allows, which then makes the export selector sell too early.

**Second, energy conservation.** Confirm the test exists and covers every block of every fixture.
Without it, a clamping bug silently destroys energy and every downstream number looks plausible.

**Third, the fixtures.** Check the commented arithmetic by hand for at least `power_limited` and
`sunny_saturates`. If the expected values were generated by running the implementation, the tests
prove only that the code agrees with itself.
