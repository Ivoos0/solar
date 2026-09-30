---
work_package_id: WP05
title: Vetoes and selectors
dependencies:
- WP04
- WP11
requirement_refs:
- FR-009
- FR-010
- FR-011
- FR-012
- FR-013
- FR-014
- FR-015
- FR-016
- FR-017
- FR-040
- FR-046
- FR-047
- FR-048
- FR-050
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T022
- T050
- T051
- T023
- T024
- T025
- T026
- T027
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/rules.py
create_intent:
- pyscript/modules/rules.py
- tests/test_rules.py
execution_mode: code_change
owned_files:
- pyscript/modules/rules.py
- tests/test_rules.py
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

Decide what the battery should do, keeping **forbidding** rules structurally distinct from
**choosing** rules.

Satisfies **FR-009 through FR-017, FR-040, FR-046, FR-047, FR-048, FR-050**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP05 --agent <name>`. Depends on WP04 for the
  trajectory and on WP11 for the capacity arithmetic that V3 and S0 both consume.

## The distinction this work package exists to preserve

**This was specified wrongly twice before it was caught.** Read this section before writing anything.

The original design evaluated all eight rules as a single first-match chain. That conflated two
kinds of rule and produced a real behavioural bug: when the injection price went negative, the chain
terminated at the export-suppression rule and **never reached the solar-absorption rule**. So the
common case — sun on the roof, battery part-full, low prices — resolved to an idle decision while
free solar went to the grid.

The fix, and the structure you must implement:

- **Vetoes** (V1, V2, V3) forbid an *action class* for the cycle. They are established **before** any
  selector runs. A veto removes an option; **it never ends evaluation**.
- **Selectors** (S0–S6) propose an action. They are tried in priority order until one proposes
  something no veto forbids.
- A vetoed proposal advances to the **next** selector — never terminates the cycle, never restarts
  from the beginning.

The second bug the same fix cured: under the old chain, a battery at the reserve floor during a
*negative consumption price* would idle instead of taking free energy, because V1 blocked the whole
chain rather than just discharge.

If you find yourself writing `return` inside a veto check, stop and re-read this.

## Context you need

- `data-model.md` — the veto and selector tables, and the resolution order
- `spec.md` FR-009 through FR-017, and the edge-case list
- `spec.md` SC-003, SC-004, SC-010

This module is pure: it receives a `Trajectory`, a price series, a `BatteryState`, a `GridState`
and a `SiteConfig`, and returns a decision. No I/O, no Home Assistant imports. All capacity-tariff
arithmetic comes from WP11's `capacity.py` — do not recompute any of it here.

---

### T022 — Implement vetoes V1 and V2

**Purpose**: Establish the cycle's prohibitions before any selector runs.

**Steps**:

1. Create `pyscript/modules/rules.py`.
2. Represent the forbidden action classes explicitly — a set of strings such as `{"discharge",
   "export"}` is fine and keeps the record rendering simple.
3. Write `establish_vetoes(battery_state, current_prices, config)` returning both the forbidden set
   and a list of which vetoes fired, for the record:

   | Veto | Condition | Forbids | Still allowed |
   |---|---|---|---|
   | **V1** | `charge_percent <= reserve_percent` | all discharge, export included | charging only |
   | **V2** | `injection_price < 0` | export only | charging, and discharging to serve household load |

4. Be precise about the two discharge-shaped actions. **V1 forbids all discharge**, because any
   discharge takes the battery below the floor. **V2 forbids only export** — discharging to serve
   the house is still profitable whenever the consumption price is positive, and V2 must not block it.
5. Vetoes return data. They do not choose, do not log, and do not short-circuit.

**Files**: `pyscript/modules/rules.py` (new)

**Validation**:
- At exactly the reserve floor, V1 fires (`<=`, not `<`)
- `injection_price == 0.0` does **not** fire V2 — zero is not negative, and exporting at zero costs
  nothing
- V2 fires while consumption price is positive — the band from WP02
- Both may fire in the same cycle, forbidding `{"discharge", "export"}` together

---

### T050 — Implement veto V3 and the grid-charge cap

**Purpose**: Stop a cheap hour buying a capacity peak that is billed for twelve months.

**Steps**:

1. Add **V3** to `establish_vetoes`: forbid `grid_charge` when WP11's `budget_kw` is at or below
   zero. A negative budget means the window has already passed the ceiling; further draw only makes
   it worse.
2. V3 forbids **grid** charging only. Charging from solar excess does not cross the meter and is
   unaffected — this distinction matters, because on a sunny day with a high running average the
   battery should still be soaking up its own production.
3. Beyond the veto, expose the **cap**: every selector that charges from the grid clamps its target
   power to `budget_kw` (FR-046). S1 in particular must charge at `min(max_charge_kw, budget_kw)`
   rather than full inverter power.

   This is the case that motivated the whole addition. S1 fires on negative prices, which is exactly
   when every battery in Flanders charges hard — and full-power charging into an already-loaded
   quarter-hour is precisely how a new monthly peak gets set.
4. Record the cap in the proposal's reasoning whenever it bit, so the log shows *why* charging was
   slower than the inverter allows.

**Files**: `pyscript/modules/rules.py` (continues)

**Validation**:
- Budget 0.0 or negative → V3 fires, `grid_charge` forbidden
- Budget 1.2 kW with `max_charge_kw` 5.0 → S1 proposes **1.2 kW**, and says so
- Solar charging is never blocked by V3
- A generous budget leaves S1 at full power

---

### T051 — Implement selector S0 and its precedence

**Purpose**: Discharge to hold the window at the ceiling, ahead of every price consideration.

**Steps**:

1. Add **S0** as the **first** selector tried, before S1 (FR-050).
2. Condition and power both come from WP11's `shave_kw(state, config)`: a positive value means a
   peak is forming and names the power needed to hold the line. Zero means no proposal.
3. Action is `discharge` — serving household load, not exporting. V2 therefore does not apply to it;
   V1 does, because an empty battery cannot shave.
4. **Why S0 leads**: a capacity peak is billed across the next twelve months, while a price
   opportunity pays once. Even a very good arbitrage hour is worth cents where a peak increase is
   worth tens of euros. Write that reasoning into a comment — it is the kind of ordering a future
   reader will otherwise be tempted to "optimise".
5. S0 does **not** duplicate WP11's arithmetic. It asks for the shave power and proposes it. The
   floor check, the projection and the clamping all live in `capacity.py`.

**Files**: `pyscript/modules/rules.py` (continues)

**Validation**:
- S0 fires when `shave_kw` is positive and proposes exactly that power
- S0 is tried before S1 — assert ordering with a state where both would fire
- S0 at the reserve floor is vetoed by V1 and evaluation continues to S1
- S0 does not fire when offtake is below the billing floor (WP11 returns zero)

---

### T023 — Implement selectors S1 and S2

**Purpose**: The two charging selectors, highest priority first.

**Steps**:

1. Each selector is a function taking the same inputs and returning either a proposal
   `(action, target_power_kw, reasoning)` or `None` when its condition does not hold.
2. **S1 — charge at negative consumption price** (FR-012):
   - Condition: `consumption_price < 0`
   - Action: `charge` at `config.max_charge_kw`
   - Reasoning must quote the price. The house is being paid to consume; fill as fast as possible.
3. **S2 — absorb solar surplus** (FR-013):
   - Condition: **all three** must hold —
     (a) `battery_state.headroom_kwh > 0`
     (b) solar is currently producing more than the household is drawing
     (c) EITHER `injection_price_now < 0` (storing solar beats paying to export it, whatever later prices are) OR `best_remaining_injection_price × round_trip_efficiency > injection_price_now`. Round-trip losses are ALWAYS a plain multiplication by the efficiency, for every sign of price (storing 1 kWh returns `eff` kWh, so exporting later at a negative price costs LESS than now); never divide for negative prices.
   - Action: `charge` from solar, at the lesser of the current surplus rate and `max_charge_kw`
   - Condition (c) is what makes this testable and correctly ordered. The original wording was
     "injection price is low", which has no threshold and, worse, outranked the export selector — so
     at the horizon's **best** price the planner would have charged instead of sold. With (c), when
     now *is* the best price the condition is false and S2 stands aside for S3.

**Files**: `pyscript/modules/rules.py` (continues)

**Validation**:
- S1 fires at −0.001 and not at 0.0
- S2 does **not** fire when the battery is full (no headroom)
- S2 does **not** fire when now is the best injection price in the horizon — S3 gets it instead
- S2 fires when a materially better price exists later, even after efficiency losses

---

### T024 — Implement selectors S3 and S4 with bounded windows

**Purpose**: The two trajectory-driven selectors. Their windows are what make them correct.

**Steps**:

**Before both**: any window search — best injection price, cheapest consumption prices — must skip
blocks where `has_price` is False (FR-040). A block the market published no price for cannot be
compared against one it did, and must never be chosen as a window.

1. **S3 — export before the battery saturates** (FR-014):
   - Condition: the trajectory shows **either** spill ahead **or** leftover at the horizon's end,
     **and** now is the best injection price within the window from now to `saturation_block`
   - Action: `export` at `config.max_discharge_kw`
   - **The window is the point.** If saturation is at 12:45 and the best price of the horizon is at
     19:00, the relevant comparison is against prices *before 12:45*, because every kWh of solar
     arriving between those times is spilled while the battery waits. Waiting for a better price is
     wrong when waiting costs more than the difference.
   - When `saturation_block is None`, the window is the whole horizon — no constraint applies, and
     S3 picks the global best exactly as a scalar approach would have.
2. **S4 — import before the reserve floor is reached** (FR-015):
   - Condition: `reserve_breach_block is not None` **and** now is among the cheapest consumption
     prices in the window from now to that block
   - Action: `charge` from grid at `config.max_charge_kw`
   - Same logic mirrored: a cheap hour arriving after you have already hit the floor is no help.
   - "Among the cheapest" needs a concrete rule — use *the cheapest N blocks in the window*, where N
     is the number of blocks needed to cover the projected shortfall at `max_charge_kw`. Compute N
     rather than hard-coding it.

**Files**: `pyscript/modules/rules.py` (continues)

**Validation**:
- S3 with saturation at block 12 and the global best price at block 30 selects the best price
  **within blocks 0–12**
- S3 with `saturation_block is None` selects the global best across the horizon
- S3 fires on leftover alone, with no spill ahead
- S4 with a breach at block 40 ignores cheaper prices after block 40
- S4 does not fire when `reserve_breach_block is None`

---

### T025 — Implement selectors S5 and S6

**Purpose**: Arbitrage, and the explicit hold.

**Steps**:

1. **S5 — arbitrage above the loss threshold** (FR-016):
   - Condition: some later block's injection price, after round-trip losses, exceeds the current
     consumption price:
     `best_later_injection × round_trip_efficiency > consumption_price_now`
   - Action: `charge` now at `max_charge_kw`, with reasoning naming the target block and the spread
   - Requires headroom — no point proposing a charge with a full battery
   - This selector **self-guards against negative prices**: a negative sell price can never exceed a
     positive buy price, so S5 never proposes a loss-making cycle even without V2.
2. **S6 — hold** (FR-017):
   - No condition; always proposes `idle` at 0.0 kW
   - Reasoning must say *why* nothing applied — not merely "no rule fired". Name the salient facts:
     no spill ahead, no breach, spread too narrow. An idle decision the homeowner cannot interrogate
     is indistinguishable from a broken planner.
   - `idle` is never vetoed, so S6 always terminates the loop.

**Files**: `pyscript/modules/rules.py` (continues)

**Validation**:
- S5 fires when the spread beats efficiency losses; does not when it is narrower
- S5 with a 0.90 efficiency needs a spread better than ~11%, not 1% — assert a case that is
  profitable before losses and unprofitable after
- S6 always returns a proposal with substantive reasoning

---

### T026 — Implement the resolution loop with veto fall-through

**Purpose**: The mechanism this whole work package exists to get right.

**Steps**:

1. Write `decide(trajectory, prices, battery_state, config)`:
   ```
   forbidden, vetoes_fired = establish_vetoes(...)
   suppressed = []

   for selector in (S0, S1, S2, S3, S4, S5, S6):
       proposal = selector(...)
       if proposal is None:
           continue                       # condition not met — try the next
       if proposal.action in forbidden:
           suppressed.append((selector.name, proposal.action, blocking_veto))
           continue                       # VETOED — try the NEXT selector
       return Decision(proposal, selector.name, vetoes_fired, suppressed)
   ```
2. Note the two distinct `continue` paths. A selector that does not fire and a selector whose
   proposal was vetoed both advance — but only the second is recorded in `suppressed`. The decision
   record must be able to distinguish "did not apply" from "was overruled" (FR-028).
3. Record **which veto** blocked each suppressed proposal, not merely that one did.
4. S6 always proposes `idle`, which is never forbidden, so the loop always terminates with a decision.

**Files**: `pyscript/modules/rules.py` (continues)

**Validation**:
- A vetoed S3 proposal advances to S4, not back to S1 and not out of the loop
- Two consecutive vetoed proposals both advance; no infinite loop
- `suppressed` names the selector, the action, and the blocking veto
- A selector that simply did not fire is absent from `suppressed`

---

### T027 — Test every veto, every selector, and the fall-through ordering

**Purpose**: Pin mechanically what prose got wrong twice.

**Steps**:

1. Create `tests/test_rules.py`.
2. **Per-veto tests**: V1 at, above and below the floor; V2 at negative, zero and positive injection
   prices. Assert precisely which action classes each forbids — and that V2 leaves
   discharge-to-house available.
3. **Per-selector tests**: for each of S1–S6, one case that fires and one that does not, asserting
   the proposed action and that the reasoning names the deciding values.
4. **The scenario that motivated the redesign** — assert it explicitly and name the test so its
   purpose is obvious:
   > injection price negative, battery part-full, solar producing
   > → V2 forbids export, evaluation **continues**, S2 fires, decision is *charge from solar*

   This is the case the original chain got wrong, and it must not regress.
5. **The second bug**: battery at the reserve floor with a negative consumption price
   → V1 forbids discharge but **not** charging, so S1 still fires and the battery recovers.
6. **Ordering test**: construct a state where S2 and S3 would both fire, and assert S2 stands aside
   because now is the best injection price — proving condition (c) resolves the priority overlap.
7. **The price band** from WP02: injection negative while consumption positive, battery part-full.
   Assert no export is proposed and evaluation reaches a later selector.
8. **Fall-through test**: force a vetoed proposal and assert the next selector — not the first — is
   tried next.
9. **Price-gap test**: give S3 a window containing a block with `has_price = False` that would be
   the best price if it counted. Assert it is skipped and a priced block is chosen instead.
10. **Capacity tests**: V3 fires on a zero or negative budget; S1 charges at the budget rather than
    `max_charge_kw` when capped; solar charging is unaffected by V3; S0 is tried before S1 when both
    would fire; S0 vetoed by V1 at the reserve floor falls through to S1.

**Files**: `tests/test_rules.py` (new, ~250 lines)

**Validation**:
- `pytest tests/test_rules.py -v` passes
- Every veto and every selector has at least a fires/does-not-fire pair
- The two regression scenarios (4 and 5) are present and named for what they protect

---

## Definition of Done

- Vetoes are established before selectors and never terminate evaluation
- V1 forbids all discharge; V2 forbids only export
- S2's condition has a real threshold and stands aside when now is the best price
- S3's window ends at saturation; S4's ends at the projected breach
- The resolution loop advances to the **next** selector on a veto, recording what was suppressed
- All six selectors and both vetoes are tested, plus both regression scenarios
- `pytest tests/` is green

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **A veto short-circuiting the cycle** | The two named regression tests fail immediately |
| V2 blocking discharge-to-house as well as export | Explicit per-veto assertion of forbidden classes |
| S3 searching the whole horizon instead of stopping at saturation | Ordering test with a better price after saturation |
| S2 outranking S3 at the best price | Condition (c) plus the explicit ordering test |
| Arbitrage ignoring round-trip losses | Assert a spread profitable before losses and unprofitable after |
| `suppressed` conflating "did not fire" with "was vetoed" | Distinct assertions for each path |
| An idle decision with no substantive reasoning | Assert the reasoning names the salient facts |

## Reviewer guidance

**Open the resolution loop first.** Find the veto check. If it `return`s, `break`s, or jumps back to
the first selector, the work package has reproduced the exact bug this design was written to fix —
reject it regardless of test results, then check why the regression tests did not catch it.

**Then check V1 and V2's forbidden sets.** V2 forbidding `discharge` rather than only `export` is a
subtle, plausible-looking error that would stop the battery serving the house during ordinary
low-price hours — expensive, and invisible in the log unless you know to look for it.

**Then check S3's window.** It must be bounded by `saturation_block`, with the whole horizon used
only when saturation is `None`. A global search here quietly reintroduces the scalar-balance
behaviour the trajectory was built to replace.

Finally, read the reasoning strings. They are what the homeowner reads at 7am wondering why the
battery did something. "S3 fired" is not reasoning; "leftover 4.1 kWh, saturation at 12:45, best
injection price before then" is.
