---
work_package_id: WP06
title: Decision record
dependencies:
- WP05
requirement_refs:
- FR-018
- FR-027
- FR-028
- FR-052
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T028
- T029
- T030
- T052
- T031
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/decision.py
create_intent:
- pyscript/modules/decision.py
- tests/test_decision.py
execution_mode: code_change
owned_files:
- pyscript/modules/decision.py
- tests/test_decision.py
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

Produce one auditable line per cycle that a human can check by hand against the prices and forecast
for that moment.

Satisfies **FR-018, FR-027, FR-028, FR-052**. Supports **NFR-009**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP06 --agent <name>`. Depends on WP05.

## Why this is not just logging

Nothing is commanded this mission. No inverter is touched, the battery reading is stubbed, and no
dashboard exists. **The log file is the entire deliverable.** A decision that is not legible here did
not happen as far as anyone can tell, and the mission's whole premise — trust the decisions before
letting them touch hardware — rests on this file being complete enough to audit.

That makes one instinct actively harmful: trimming fields for readability. Every field is mandatory,
including ones that frequently say "none". A missing field and a zero field must not look alike.

## Context you need

- `contracts/decision-record.md` — **the authoritative format**. Follow it exactly; anything below
  that contradicts it loses.
- `data-model.md` — the `DecisionRecord` and `HaltState` tables
- `spec.md` FR-018, FR-027, FR-028, NFR-004, SC-002

This module is pure: it builds and **formats** records. It does not write files — WP09's inverter
boundary does that.

---

### T028 — Define the DecisionRecord structure

**Purpose**: One structure carrying everything needed to audit a decision.

**Steps**:

1. Create `pyscript/modules/decision.py`.
2. Define `DecisionRecord` with every field from `data-model.md`: `timestamp`, `action`,
   `target_power_kw`, `charge_percent`, `charge_kwh`, `consumption_price`, `injection_price`,
   `forecast_remaining_kwh`, `usage_remaining_kwh`, `saturation_block`, `spill_kwh`,
   `reserve_breach_block`, `projected_end_charge_kwh`, `duration_ms`, `vetoes_applied`, `selector`,
   `reasoning`, `degraded_inputs`.

   `duration_ms` (NFR-009) renders as `took=84ms` and is what makes NFR-001's five-second budget
   measurable from the log rather than assumed. The adapter measures it; this module only carries
   and formats it.
3. Write `build(decision, trajectory, battery_state, prices_now, degraded)` assembling a record from
   what WP04 and WP05 produced.
4. Make every field required at construction. Do **not** give them defaults — a default is how a
   field quietly goes missing, and NFR-004 requires all of them in 100% of records.
5. `vetoes_applied` and `degraded_inputs` are lists that may be empty. Empty is meaningful and must
   survive to formatting as an explicit "none", not as an omission.

**Files**: `pyscript/modules/decision.py` (new)

**Validation**:
- Constructing a record without a field fails rather than silently defaulting
- Empty lists are preserved as empty lists, not converted to `None`

---

### T029 — Format a decision as a log line

**Purpose**: Render the record in the contract's format.

**Steps**:

1. Write `format_record(record)` returning a single line matching `contracts/decision-record.md`:
   ```
   2026-09-29T14:35:00+02:00 | action=export | power=2.50kW | soc=78.0%/7.80kWh | cons=0.2140 | inj=0.1890 | solar_rem=11.20kWh | usage_rem=14.60kWh | saturation=2026-09-29T12:45:00+02:00 | spill=2.40kWh | breach=none | end_soc=2.10kWh | took=84ms | vetoes=none | selector=S3 | why="..." | degraded=soc_stubbed
   ```
2. Formatting rules from the contract:
   - timestamp: ISO 8601 **with offset**, local time (Europe/Brussels)
   - `power`: `N.NNkW` — `0.00kW` when idle, never blank
   - `soc`: `N.N%/N.NNkWh` — both forms, since percent is what the inverter reports and kWh is what
     the rules use
   - prices: **4 decimal places**, matching the existing template sensors so a logged price can be
     compared directly against what Home Assistant shows
   - energies: `N.NNkWh`
   - `saturation`, `breach`: ISO 8601 or the literal `none`
   - `why`: **quoted**, since it contains spaces
3. One physical line per record. No wrapping — the contract's example is wrapped for reading only.
4. Fields appear in the contract's order every time. A human scanning a hundred lines relies on
   column stability more than on any single value.

**Files**: `pyscript/modules/decision.py` (continues)

**Validation**:
- Output is a single line containing no newline
- An idle decision still shows `power=0.00kW`
- Prices show exactly 4 decimals, including trailing zeroes (`0.2000`, not `0.2`)
- `saturation=none` when the trajectory never saturates

---

### T030 — Render vetoes, degraded markers, and the halt record

**Purpose**: The three renderings that carry the most diagnostic weight.

**Steps**:

1. **Vetoes** (FR-028). An empty list renders `vetoes=none`. A veto that suppressed a proposal names
   both itself and what it suppressed:
   ```
   vetoes=V2(suppressed S3 export) | selector=S2 | why="injection -0.0043 forbids export; headroom 3.2kWh, charging from solar instead"
   ```
   Multiple suppressions are comma-separated; one suppressed proposal may be blocked by SEVERAL vetoes at once, which `rules.decide` reports joined, e.g. `V1+V2` - render that as `V1+V2(suppressed S3 export)`. The selector field ranges S0-S6. The budget, ceiling and running average come from the SAME GridState that V3 and S0 used (via `capacity`), not from the Decision. **A record naming a veto must also name the selector
   that finally fired** — a veto alone is never a complete decision (SC-010).
2. **Degraded inputs** (FR-027). Empty renders `degraded=none`. Otherwise comma-separated markers:
   - `soc_stubbed` — present every cycle this mission (C-003)
   - `solar_zero_fallback` — the forecast was unavailable
   - `cache_age_solar=3h12m` — a cached series was used, with its age
   - `usage_samples=3` — the profile had fewer than seven days
   Format ages human-readably (`3h12m`), not as raw seconds. This line is read by a person.
3. **Halt record** (FR-021). Write `format_halt(halt_state)`:
   ```
   2026-09-29T14:35:00+02:00 | HALT | cause=price_data_unavailable | entered=2026-09-29T14:20:00+02:00 | alerted=2026-09-29T14:20:00+02:00
   ```
   A halted cycle produces no decision but is **not silent** — SC-001 counts one record per cycle,
   of either kind.
4. Make `HALT` visually distinct at the start of the line so halts are greppable and obvious when
   scrolling.

**Files**: `pyscript/modules/decision.py` (continues)

**Validation**:
- `vetoes=none` for an empty list, never a blank value or a missing field
- A suppressed proposal renders selector, action and blocking veto
- `degraded=none` when nothing is degraded
- Cache ages render as `3h12m`
- The halt line starts with the timestamp then `HALT`

---

### T052 — Render the capacity position

**Purpose**: Show why charging was capped, or why the battery discharged into a household peak.

**Steps**:

1. Add three fields to the record and its formatter, per `contracts/decision-record.md`:
   `avg=N.NNkW` (running quarter-hour average), `ceiling=N.NNkW` (the level being defended), and
   `budget=N.NNkW` (grid power still available this window).
2. **`budget` can legitimately be negative**, meaning the window has already passed the ceiling.
   Render the minus sign; do not clamp it to zero in the formatter. A reader needs to tell "no room
   left" from "already over".
3. Guard records from WP12 carry `source=guard`, distinguishing them from the planner's records.
   Planner records carry `source=planner`.
4. When a grid-charge power was capped by the budget, the reasoning must say so and give both
   numbers — what was wanted and what was allowed.

**Files**: `pyscript/modules/decision.py` (continues)

**Validation**:
- All three fields appear in every record, including guard records
- A negative budget renders with its sign
- A capped charge names both the requested and the permitted power
- `source` distinguishes guard from planner

---

### T031 — Test record formatting and completeness

**Purpose**: Prove every field is present in every record.

**Steps**:

1. Create `tests/test_decision.py`.
2. **Completeness test, the most important one here**: format a record and assert **every** field
   name from the contract appears in the output. Drive it from a list of expected field names so
   adding a field to the contract without adding it to the formatter fails the test.
3. Test that empty lists render `none`, not empty strings — assert `vetoes=none` literally appears,
   since `vetoes=` followed by a space is exactly the failure mode that makes a record ambiguous.
4. Test a veto-suppressed record renders selector, suppressed action and blocking veto together.
5. Test the idle case: `power=0.00kW`, substantive `why`.
6. Test price formatting holds trailing zeroes.
7. Test `saturation=none` and a real ISO timestamp both render.
8. Test the halt record format.
9. **Round-trip readability test**: assert the formatted line splits on ` | ` into the expected
   number of fields, and that each splits on `=` into a key and a value. That is what makes the log
   greppable and mechanically checkable later.

**Files**: `tests/test_decision.py` (new, ~160 lines)

**Validation**:
- `pytest tests/test_decision.py -v` passes
- The completeness test enumerates fields from a list, not by eyeballing one sample

---

## Definition of Done

- `DecisionRecord` carries every field with no defaults
- `format_record` matches `contracts/decision-record.md` exactly, one line, stable field order
- Empty vetoes and degraded lists render as explicit `none`
- A veto-bearing record always also names the selector that fired
- `format_halt` produces a greppable halt line
- `pytest tests/` is green

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| Omitting a field when its value is empty | Completeness test driven by a field-name list |
| `vetoes=` with nothing after it | Assert the literal `vetoes=none` |
| Dropping trailing zeroes in prices | Assert `0.2000` formats as four decimals |
| Multi-line output from a long `why` | Assert no newline in the output |
| Naive timestamps with no offset | Assert the offset is present; Europe/Brussels has two of them per year |
| A veto recorded without the selector that fired | Explicit test; SC-010 depends on it |
| Raw seconds for cache age | Assert the `3h12m` shape |

## Reviewer guidance

Run the completeness test first and read how it is written. If it checks a handful of fields by hand
rather than enumerating them, it will pass forever while fields quietly go missing — which is the one
failure this work package genuinely cannot survive, because there is no other output surface to
catch it.

Then look at the empty-value handling. `vetoes=none` and `degraded=none` must be literal. A blank
value after `=` is the difference between "no veto applied" and "the formatter has a bug", and from
the log alone those would be indistinguishable.

Finally, read one formatted sample line end to end and try to reconstruct the decision from it, as
SC-002 requires. If you cannot tell why the planner did what it did, the `why` text is too thin —
and that is a defect in this work package, not a future improvement.
