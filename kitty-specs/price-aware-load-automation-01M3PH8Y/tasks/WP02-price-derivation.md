---
work_package_id: WP02
title: Price derivation and rolling horizon
dependencies:
- WP01
requirement_refs:
- FR-001
- FR-002
- FR-003
- FR-030
- FR-039
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T006
- T007
- T008
- T009
- T010
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/prices.py
create_intent:
- pyscript/modules/prices.py
- tests/test_prices.py
execution_mode: code_change
owned_files:
- pyscript/modules/prices.py
- tests/test_prices.py
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

Turn the raw market price series into consumption and injection prices per block, and establish how
far ahead the plan can see.

Satisfies **FR-001, FR-002, FR-003, FR-030, FR-039**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP02 --agent <name>` and work inside the workspace it
  gives you. WP02 depends on WP01, so implement it after WP01 lands.

## Context you need

- `data-model.md` — the `PricePoint` table
- `research.md` R-04 — where the price series comes from and why coefficients are applied here
  rather than in a Home Assistant template
- `spec.md` FR-001, FR-002, FR-003, FR-030

This module is **pure**: it receives a list of raw price entries and a `SiteConfig`, and returns
derived `PricePoint` objects. It does not read Home Assistant state — the adapter does that and
hands in plain data.

### The one thing to understand before writing code

Consumption and injection are **not symmetric**, and the asymmetry is the reason a whole veto exists.

```
consumption = market × 1.07 + 0.007   → negative below market −0.00654 EUR/kWh
injection   = market × 0.94 − 0.011   → negative below market +0.01170 EUR/kWh
```

Because the injection offset is negative, **injection price goes negative while the market price is
still positive** — anywhere below about 1.17 c/kWh. That creates a band roughly 1.8 c/kWh wide where
injection is negative but consumption is still positive. Inside that band nothing forces the battery
to charge, so the export veto (V2, built in WP05) is the only thing preventing a loss-making export.

That band is ordinary — it covers low-price sunny hours, not a rare edge. It is also exactly the
case live data will rarely hand you during development. **T009 exists to make it testable.**

---

### T006 — Derive consumption and injection prices from market price

**Purpose**: Apply the provider's linear transform, separately per direction.

**Steps**:

1. Create `pyscript/modules/prices.py`.
2. Define `PricePoint` with the fields from `data-model.md`: `block_start`, `market_price`,
   `consumption_price`, `injection_price`, `source_resolution_minutes`.
3. Write `derive(market_price, config)` returning the two derived prices:
   ```
   consumption = market_price * config.consumption_multiplier + config.consumption_offset
   injection   = market_price * config.injection_multiplier   + config.injection_offset
   ```
4. Round to 4 decimal places — matching the existing template sensors in `configuration.yaml`, so a
   derived price in the log can be compared directly against what Home Assistant shows.
5. Keep the two computations visibly separate. Do **not** factor them into one helper taking a
   direction flag: the whole point is that they never share coefficients, and a shared code path
   invites a future edit that applies one pair to both.

**Files**: `pyscript/modules/prices.py` (new)

**Validation**:
- `derive(0.10, config)` with default coefficients gives consumption 0.1140, injection 0.0830
- `derive(0.0, config)` gives consumption +0.0070, injection **−0.0110** — positive and negative
  from the same zero input, which is the asymmetry in miniature

---

### T007 — Expand a coarse price series onto the block grid, held flat

**Purpose**: Price data arrives hourly; the trajectory needs 15-minute blocks. Bridge the two
**without inventing precision**.

**Steps**:

1. Write `expand_to_blocks(raw_entries, config, start_time)` taking the raw series — a list of
   `{time, price}` mappings as the ENTSO-e integration publishes them — and returning `PricePoint`
   objects on the block grid.
2. For each source period, emit `source_period_minutes / block_minutes` blocks, **all carrying the
   same price**. One hourly price becomes four identical 15-minute blocks.
3. Record the source resolution on each `PricePoint` (`source_resolution_minutes`), so downstream
   code and the decision record can be honest about how precise the timing really is.
4. Infer the source resolution from the spacing between consecutive raw entries rather than assuming
   60 minutes. If Belgium moves to 15-minute market periods, this keeps working.
5. Align blocks to the clock: a block starts at :00, :15, :30, :45, never at an arbitrary offset from
   `start_time`.

**Files**: `pyscript/modules/prices.py` (continues)

**Validation**:
- An hourly series of 24 entries expands to 96 blocks at `block_minutes: 15`
- All four blocks within an hour carry identical prices
- `source_resolution_minutes` is 60 for hourly input
- A 15-minute input series expands 1:1 with `source_resolution_minutes` 15

**Do not interpolate.** Smoothing prices across an hour would invent detail the market did not
publish, and FR-030 forbids it. The same rule applies to the solar series in WP03.

**Edge cases**:
- Irregular gaps in the raw series (a missing hour): emit no entries for the gap rather than
  stretching the neighbouring price across it. A hole is honest; a fabricated price is not.

  **Return the price series as a mapping keyed by `block_start`, not as a positional list**
  (FR-039). Solar and usage are dense and contiguous; prices may have holes. Every join between the
  three happens by block start time, so a missing hour can never shift solar or usage onto the wrong
  price. A positional list would do exactly that, silently, and the resulting decisions would look
  entirely reasonable while being wrong.
- A single raw entry with no successor to measure spacing against: fall back to 60 minutes and
  record that assumption.

---

### T008 — Determine the rolling horizon end from available price data

**Purpose**: The horizon is not "today" — it runs as far ahead as prices are known, which changes
during the day.

**Steps**:

1. Write `horizon_end(price_points)` returning the end of the last block with a known price.
2. Everything downstream derives its window from this. Before the day-ahead auction publishes,
   typically around 13:00 local, this reaches roughly the end of today; afterwards, roughly the end
   of tomorrow — up to about 35 hours out.
3. Return the horizon as the exclusive end instant, and keep the block list itself as the primary
   artefact — callers mostly want to iterate blocks, not compare timestamps.
4. Handle the empty series: return `None`. The adapter treats that as a halt condition (FR-021), so
   this function must not raise — a missing horizon is data, not an error.

**Files**: `pyscript/modules/prices.py` (continues)

**Validation**:
- 24 hourly entries from midnight yields a horizon ending at the next midnight
- 35 hours of entries yields a 35-hour horizon
- An empty list yields `None`, not an exception

**Note for later**: WP04's trajectory extends over the price horizon even where solar forecast data
runs out sooner — those blocks are treated as zero solar rather than truncating the horizon. This
function defines the outer bound; do not clip it to the forecast.

---

### T009 — Test price derivation, including the negative-injection band

**Purpose**: Pin the asymmetry, especially the band live data rarely produces.

**Steps**:

1. Create `tests/test_prices.py`.
2. Test straightforward derivation at a normal price (say 0.10 EUR/kWh).
3. **Test the band explicitly** with a table:

   | Market price | Consumption sign | Injection sign | Meaning |
   |---|---|---|---|
   | 0.1000 | + | + | Ordinary hour |
   | 0.0200 | + | + | Just above the band |
   | 0.0100 | + | **−** | **Inside the band** — V2 is load-bearing here |
   | 0.0000 | + | **−** | Inside the band |
   | −0.0050 | + | **−** | Inside the band |
   | −0.0100 | **−** | **−** | Below the band; the charge selector fires |
   | −0.0500 | − | − | Strongly negative |

4. Assert the two boundary crossings directly: consumption turns negative below −0.00654,
   injection below +0.01170. Compute them from the configured coefficients rather than hard-coding,
   so changing a coefficient in the fixture updates the expectation.
5. Add a test proving a **different** coefficient set moves the band — this is per-provider, and the
   band exists for any provider whose injection offset is negative.

**Files**: `tests/test_prices.py` (new)

**Validation**:
- Every row in the table above is asserted
- The boundary values are derived from config, not hard-coded constants
- A test documents *why* the band matters, in a comment or the test name

---

### T010 — Test horizon determination and flat expansion

**Purpose**: Cover the block-grid mechanics and the horizon edges.

**Steps**:

1. Test that hourly input expands to four identical blocks per hour with `source_resolution_minutes`
   set to 60.
2. Test that a 15-minute input series expands 1:1.
3. Test the no-interpolation rule directly: assert all four blocks in an hour are **equal**, and
   assert that no block carries a value absent from the source series.
4. Test horizon length before and after the day-ahead publication boundary — a 24-entry series and a
   35-entry series.
5. Test the empty series returns `None`.
6. Test a gap in the middle of the series produces a gap in the map, not a stretched price — and
   that blocks either side of the gap keep their correct start times, so nothing shifts.

**Files**: `tests/test_prices.py` (continues)

**Validation**:
- `pytest tests/test_prices.py -v` passes
- The no-interpolation assertion would fail if someone "improved" the expansion with smoothing

---

## Definition of Done

- `pyscript/modules/prices.py` derives both prices independently, expands to blocks without
  interpolating, and reports the horizon
- No file I/O, no third-party imports, no Home Assistant or pyscript imports
- `tests/test_prices.py` covers the band, the boundaries, flat expansion, and horizon edges
- `pytest tests/` is green, including WP01's tests

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| Factoring the two derivations into one function with a direction flag | Keep them separate; sharing a path invites applying one coefficient pair to both |
| Interpolating a coarse series to "improve" resolution | FR-030 forbids it; the test asserts equality across sub-blocks |
| Assuming a 60-minute source resolution | Infer from entry spacing; Belgian market periods may change |
| Raising on an empty price series | Return `None` — the adapter turns that into a halt, which is a decision, not a crash |
| Hard-coding band boundaries in tests | Derive them from the fixture's coefficients so the test tracks the config |

## Reviewer guidance

The band tests are the heart of this review. Open `tests/test_prices.py` and confirm there is a case
where **injection is negative while consumption is positive**. If that case is missing, the work
package is not done, regardless of how much else passes — that band is why V2 exists, and it is the
case the specification got wrong before it was caught.

Second, `grep` the expansion code for any averaging, weighting, or interpolation. The correct
implementation repeats a value; anything cleverer is a defect.

Third, check that `horizon_end` is not clipped to the solar forecast. The horizon is set by prices
alone; forecast gaps become zero-solar blocks in WP04, not a shorter horizon.
