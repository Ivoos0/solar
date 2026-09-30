---
work_package_id: WP11
title: Capacity tariff model
dependencies:
- WP01
requirement_refs:
- FR-041
- FR-044
- FR-045
- FR-049
- FR-053
- FR-054
- FR-055
- FR-057
- FR-058
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T053
- T054
- T055
- T056
- T062
- T067
- T057
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/capacity.py
create_intent:
- pyscript/modules/capacity.py
- tests/test_capacity.py
execution_mode: code_change
owned_files:
- pyscript/modules/capacity.py
- tests/test_capacity.py
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

The arithmetic of the Belgian capaciteitstarief: how close the current quarter-hour is to setting a
peak, what level is worth defending, how much grid power remains available, and how hard to
discharge to hold the line.

Satisfies **FR-041, FR-044, FR-045, FR-049, FR-053, FR-054, FR-055, FR-057, FR-058**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP11 --agent <name>`. Depends only on WP01; runs in
  parallel with WP02, WP03 and WP07.

## What the tariff actually charges

Worth internalising before writing any code, because the arithmetic follows from it and several
plausible-looking implementations are wrong.

The capacity tariff bills on **grid offtake**, measured at the connection point, averaged over each
clock-aligned **quarter-hour**. The highest such average in a calendar month is that month's peak.
The fee is charged on the **mean of the last twelve monthly peaks**, with a **2.5 kW floor**.

Three consequences shape everything here:

1. **The peak is an average, not an instant.** A 9 kW spike for two minutes inside a quarter-hour
   contributes 9 × (2/15) = 1.2 kW to that window's average. Reacting to instantaneous power would
   both over- and under-react. Always reason about the window.
2. **Within a month, once a peak is set, staying below it is free.** So the level worth defending is
   `max(2.5, this month's peak)` — not zero, and not the twelve-month average.
3. **There is nothing to gain below 2.5 kW.** Discharging to hold offtake at 1 kW earns exactly what
   holding it at 2.5 kW earns. Charge spent below the floor is charge wasted.

## Context you need

- `data-model.md` — the `GridState` section, its derived formulas and its three invariants
- `contracts/user-config.md` — the `capacity_tariff` block
- `spec.md` FR-041 – FR-050, C-012, C-013, C-015

This module is **pure**: it receives a `GridState` and a `SiteConfig` and returns numbers. It does
not read Home Assistant, does not touch files, and does not decide anything — S0 in WP05 consumes
what this produces.

---

### T053 — Define GridState and build it from meter figures

**Purpose**: One structure holding the capacity position, with the running average derived correctly.

**Steps**:

1. Create `pyscript/modules/capacity.py`.
2. Define `GridState` with the `data-model.md` fields: `offtake_kw`, `window_start`,
   `window_energy_kwh`, `elapsed_minutes`, `running_average_kw`, `month_peak_kw`, `is_restored`.
3. Write `build_state(offtake_kw, window_energy_kwh, now, month_peak_kw, config, is_restored)`.
4. Compute the window from the clock, not from `now` minus something: windows start at :00, :15,
   :30 and :45. Derive `window_start` by flooring `now` to the quarter hour, and `elapsed_minutes`
   from that.
5. **`running_average_kw` comes from the meter, not from arithmetic.** The reflashed SlimmeLezer
   exposes `sensor.slimmelezer_huidig_kwartiervermogen` (`1-0:1.4.0`), and the adapter passes it in.
   Reading the meter's own figure is authoritative: it cannot drift from what the grid operator
   measures, which a reconstruction eventually would.
6. **Normalise according to the active mode** (FR-055). The meter's value is either a true running
   average or an accumulating one; `config.quarter_hour_average_mode` says which, and T067 detects it
   when the mode is `auto`:
   ```
   if mode == "accumulating":
       true_running = reported * 15 / elapsed_minutes
   else:
       true_running = reported
   ```
   Everything downstream works in true-running terms, so this conversion happens once, here, and
   nowhere else.
7. `window_energy_kwh` is still derived — `running_average_kw × elapsed_minutes / 60` — because the
   budget arithmetic needs energy, not power.

**Files**: `pyscript/modules/capacity.py` (new)

**Validation**:
- At 14:07:30 the window is 14:00–14:15 and `elapsed_minutes` is 7.5
- A reported running average of 4.0 kW at 7.5 minutes yields `window_energy_kwh` of 0.5
- `window_start` is always on a quarter-hour boundary
- If T048 found the value accumulating, the conversion is applied and a test pins it

---

### T054 — Compute the peak ceiling

**Purpose**: The level worth defending.

**Steps**:

1. Write `ceiling_kw(state, config)` returning `max(config.billing_floor_kw, state.month_peak_kw)`.
2. That is the whole rule, and it gives the right behaviour in both regimes without a special case:
   - **Early in a month**, `month_peak_kw` is small, so the ceiling sits at the 2.5 kW floor and the
     planner protects hard.
   - **Later**, once a peak is set, the ceiling is that peak and the planner defends only what is
     already conceded. Shaving below it would spend charge for no saving this month.
3. Do **not** use the twelve-month average as the ceiling. It is what gets billed, but it is not
   what this month's behaviour can change — only this month's peak is still in play (C-015).

**Files**: `pyscript/modules/capacity.py` (continues)

**Validation**:
- Month peak 0.0 → ceiling 2.5 (the floor)
- Month peak 1.8 → ceiling 2.5 (floor still wins)
- Month peak 6.2 → ceiling 6.2
- The twelve-month average never enters the calculation

---

### T055 — Compute the grid budget

**Purpose**: How much more grid power can be drawn this window without breaching the ceiling.

**Steps**:

1. Write `budget_kw(state, config)`:
   ```
   allowance   = ceiling_kw(state, config) * 0.25        # kWh over a full window
   remaining_h = (15 - state.elapsed_minutes) / 60
   budget      = (allowance - state.window_energy_kwh) / remaining_h
   ```
2. **Let it go negative.** A negative budget means the window has already exceeded the ceiling and
   no further draw is acceptable. Clamping to zero would hide that from V3 and from the record.
3. **Clamp the upper end** to `config.max_charge_kw`. As `remaining_h` approaches zero the division
   diverges; a huge number arising from a vanishing denominator is arithmetic, not opportunity.
4. **Treat under one minute remaining as no budget.** In the last minute, any meaningful charge
   would land almost entirely inside the window and the arithmetic is least trustworthy. Return
   zero and let the next window decide.
5. Subtract nothing for household draw here — `window_energy_kwh` already includes it, because it is
   metered at the connection point. Subtracting it again would halve the budget. This is the
   easiest mistake to make in this module.

**Files**: `pyscript/modules/capacity.py` (continues)

**Validation**:
- Ceiling 4.0 kW, 7.5 minutes elapsed, 0.2 kWh drawn: allowance 1.0, remaining 0.125 h,
  budget `(1.0 − 0.2) / 0.125` = 6.4 kW, clamped to `max_charge_kw`
- Ceiling 4.0 kW, 7.5 minutes elapsed, 0.9 kWh drawn: budget is (1.0 - 0.9) / 0.125 = +0.8 kW; with 1.1 kWh drawn it is **negative** (-0.8 kW), not zero
- 30 seconds remaining returns 0.0
- Household draw is not subtracted twice — assert against a hand-computed case

---

### T056 — Compute the shave power

**Purpose**: How hard to discharge to hold the window at the ceiling, and when not to bother.

**Steps**:

1. Write `shave_kw(state, config)` returning the discharge power needed to bring the window's
   *projected* final average back to the ceiling.
2. Project first: if the current `offtake_kw` continues for the rest of the window, what will the
   final average be?
   ```
   projected_kwh = window_energy_kwh + offtake_kw * remaining_h
   projected_avg = projected_kwh / 0.25
   ```
3. If `projected_avg <= ceiling`, return 0.0 — nothing to do.
4. Otherwise return the power that brings the projected average back to the ceiling, clamped to
   `max_discharge_kw`.
5. **Return 0.0 whenever `offtake_kw <= billing_floor_kw`** (FR-049, C-013). Below the floor there
   is no saving, and the charge is worth more later. This check comes first.
6. Never return a value that would push offtake below the floor — shave *to* the ceiling, not past it.

**Files**: `pyscript/modules/capacity.py` (continues)

**Validation**:
- Offtake 7 kW against a 4 kW ceiling mid-window returns a positive shave power
- Offtake 2.0 kW returns 0.0 even with a 2.5 kW ceiling — below the floor, nothing to gain
- The returned power never drives projected offtake below `billing_floor_kw`
- Shave power never exceeds `max_discharge_kw`
- A window already comfortably under the ceiling returns 0.0

---

### T062 — Price a peak increase in euros

**Purpose**: Turn "a peak costs more than an arbitrage gain" from an assertion into a number.

**Steps**:

1. The meter now exposes `sensor.slimmelezer_gemiddelde_maandpiek_13_maanden` — the 13-month average
   of monthly peaks, which is what the fee is actually charged on. That makes the marginal cost of
   raising this month's peak computable.
2. Write `peak_increase_cost_eur(delta_kw, config)`. Raising this month's peak by `delta_kw` raises
   the billed average by `delta_kw / 13`, and that elevated average is charged for as long as this
   month stays inside the averaging window. Express the calculation from named components —
   `capacity_rate_eur_per_kw_year`, the averaging window length, and `delta_kw` — rather than a magic
   constant, so a tariff revision is a config edit.
3. Write `arbitrage_value_eur(kwh, price_spread)` for the other side of the comparison.
4. These two make FR-050's ranking checkable. The ordering itself stays fixed — S0 before the price
   selectors — but the log can now state what the trade was worth, and a future reader can verify
   the ranking still holds at current rates rather than taking it on trust.
5. Keep both pure and free of policy. They return euros; deciding what to do with the comparison
   belongs to the rules module.

**Files**: `pyscript/modules/capacity.py` (continues)

**Validation**:
- A 1 kW peak increase at the default rate produces a plausible euro figure, hand-checkable
- The 13 in the divisor comes from configuration, not a literal in the code
- Both functions are pure and return euros

---

### T067 — Detect the quarter-hour average's semantics

**Purpose**: Work out which definition the meter uses, passively, without a manual test.

**Steps**:

1. A manual test is impractical here: the battery sits behind the meter and masks household draw, so
   switching on a known load does not produce a clean reading. The detector settles it from ordinary
   consumption instead.
2. **The discriminator.** Under a roughly constant load, the two definitions behave completely
   differently across a window:

   | Sampled at | Accumulating | True running |
   |---|---|---|
   | 3 minutes in | ≈ 0.2 × load | ≈ load |
   | 12 minutes in | ≈ 0.8 × load | ≈ load |

   So take two samples from the **same window** at different elapsed times and compute
   `ratio = early_value / late_value`. A ratio near `early_minutes / late_minutes` means
   accumulating; a ratio near 1 means true running.
3. **The comparison must be mid-window.** At a window's end the two converge exactly — energy ÷ 15
   equals energy ÷ elapsed when elapsed is 15 — so samples taken near the boundary prove nothing.
   Use samples between roughly 2 and 13 minutes.
4. **Require a stable load.** The discriminator assumes the draw did not change much between the two
   samples. Compare the instantaneous `offtake_kw` at both points and discard the pair if it moved
   more than a modest fraction. Discarding a pair costs nothing — the guard produces thirty samples
   per window and there are ninety-six windows a day.
5. **Require a non-trivial load.** At near-zero draw both definitions give near-zero and the ratio is
   noise. Discard pairs where offtake is below a small threshold.
6. **Accumulate evidence rather than deciding on one pair.** Hold a count for each hypothesis and
   conclude only once one leads by a clear margin across several windows. Until then report
   `mode_confidence = "assumed"` and use the **accumulating** interpretation, which is the safer
   guess: it makes the planner treat an early-window reading as understated and react sooner.
7. Once concluded, report `mode_confidence = "detected"` and the evidence. If the config pins a mode
   explicitly, skip detection entirely and report `"configured"`.
8. Keep this pure: it takes samples and returns a verdict. Storing samples across cycles is the
   adapter's job.

**Files**: `pyscript/modules/capacity.py` (continues)

**Validation**:
- Synthetic accumulating samples produce an `accumulating` verdict; running samples produce `running`
- Pairs straddling a window boundary are rejected
- Pairs where the load moved substantially are rejected
- Near-zero-draw pairs are rejected
- Before a conclusion, the mode is `accumulating` with confidence `assumed`
- A configured mode bypasses detection entirely

---

### T057 — Test the capacity model

**Purpose**: Pin arithmetic that is easy to get subtly and expensively wrong.

**Steps**:

1. Create `tests/test_capacity.py`.
2. Test window alignment across the four boundaries and mid-window, including 14:59:59 → window
   14:45.
3. Test the opening-seconds guard: a tiny energy figure moments into a window must not produce an
   enormous running average.
4. Test the ceiling in all three regimes — below floor, at floor, above floor.
5. Test the budget positive, negative, clamped high, and zeroed in the final minute.
6. **Test the double-subtraction trap explicitly**: a hand-computed case where subtracting household
   draw a second time would give a visibly different answer, with a comment naming the trap.
7. Test shave power: above ceiling, below floor, between floor and ceiling, and clamped at
   `max_discharge_kw`.
8. Test `peak_increase_cost_eur` against a hand-computed figure, and assert the averaging-window
   divisor is taken from config rather than hard-coded.
9. Test the detector against both synthetic sample sets, plus each rejection path — boundary
   straddle, unstable load, near-zero draw — and confirm the safe default holds until a conclusion.
10. Test a full worked scenario end to end — 6 kW draw, 4 kW ceiling, 5 minutes into the window —
   with every intermediate value asserted and the arithmetic commented.

**Files**: `tests/test_capacity.py` (new, ~200 lines)

**Validation**:
- `pytest tests/test_capacity.py -v` passes
- Expected values are hand-derived and commented, never produced by the code under test

---

## Definition of Done

- `capacity.py` derives the running average from window energy with the opening-seconds guard
- The ceiling is `max(floor, month peak)` — the twelve-month average is not used
- The budget may be negative, is clamped at `max_charge_kw`, and is zero in the final minute
- Shave power returns zero below the billing floor and never shaves past the ceiling
- Household draw is not subtracted twice
- No file I/O, no third-party imports, no Home Assistant or pyscript imports
- `pytest tests/` is green

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Subtracting household draw from the budget** when window energy already includes it | Dedicated test with a hand-computed contrast |
| Reacting to instantaneous power rather than the window average | Every function takes `GridState`; the average is the unit of reasoning |
| Division blow-up at the start or end of a window | Opening-seconds guard; final-minute zero; upper clamp |
| Clamping a negative budget to zero | V3 and the record both need the negative value |
| Shaving below the 2.5 kW floor | Floor check runs first in `shave_kw` |
| Using the twelve-month average as the ceiling | Only this month's peak is still changeable |
| Windows aligned to `now` rather than the clock | Floor to the quarter hour; assert boundaries |

## Reviewer guidance

**Check the budget formula for double subtraction first.** `window_energy_kwh` comes from a meter at
the connection point and therefore already contains the household's own draw. Subtracting
`offtake_kw` again is the single most plausible error here, it roughly halves the budget, and the
symptom is a planner that seems oddly reluctant to charge — easy to misread as conservative tuning
rather than a bug.

**Then check the floor logic in `shave_kw`.** It must return zero when offtake is already below
2.5 kW, and it must never propose a discharge that drives offtake below the floor. Both waste
stored charge for exactly zero saving.

**Then confirm the ceiling ignores the twelve-month average.** Using it would make the planner
defend a level this month's behaviour cannot change, either over-protecting or under-protecting
depending on history.

Finally, read the window arithmetic at the boundaries. A quarter past the hour is where a naive
implementation produces its most alarming numbers, and this module is about to be trusted to
discharge a battery on the strength of them.
