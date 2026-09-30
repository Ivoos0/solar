# Implementation Plan: Price-Aware Load Automation

**Branch**: `feat/price-aware-load-automation` | **Date**: 2026-09-29 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `kitty-specs/price-aware-load-automation-01M3PH8Y/spec.md`

## Summary

Every five minutes, project the home battery's charge forward across the horizon in 15-minute blocks, decide what the battery should be doing, and write that decision to a log file with its reasoning. Nothing is commanded: the inverter boundary records intent and transmits nothing, and the battery charge reading is stubbed.

The technical approach separates the work into three layers with a deliberate seam between them. All decision logic — price derivation, the trajectory, saturation and spill detection, the vetoes and selectors — lives in a **pure core** with no Home Assistant imports, no file I/O, and no pyscript decorators, which makes it fully testable under plain CPython and lets the adapters load it as native CPython through an `importlib` loader (pyscript's own interpreter cannot run it: no generator expressions, `@property`, etc.). A **thin pyscript adapter** performs every read and write and calls the core. A separate **inverter boundary** file holds what will one day command the HF2211.

That seam is not stylistic. Phase 0 established that pyscript runs an AST interpreter rather than CPython, and that any function touching `open()` must run off the event loop via `@pyscript_executor` (or `@pyscript_compile` plus `task.executor`; compile alone is not enough) — these are pyscript globals that do not exist under CPython and would break `pytest` at import. Keeping I/O at the edge dissolves that problem instead of shimming around it, and it makes the host choice reversible: replacing pyscript with AppDaemon later would rewrite the adapter, not the planner.

## Technical Context

**Language/Version**: Python 3.10+ (core written to 3.10-compatible syntax; runs under CPython 3.10.2/3.12.5 for tests and, on the NAS, as native CPython loaded by the adapters' executor loader; only the adapters and `inverter.py` go through pyscript's AST interpreter)
**Primary Dependencies**: None in the core — zero third-party runtime dependencies by construction. Host: pyscript (HACS, `custom-components/pyscript`, version pinned at install). Existing: `hass-entso-e` integration for market prices. New Home Assistant config: one `rest:` sensor for forecast.solar.
**Storage**: Plain files under the Home Assistant config directory — YAML user config (hand-edited), append-only text decision log, JSON derived-series cache
**Testing**: `pytest`, bare install (no venv scaffolding, no requirements file, no coverage tooling). Full unit coverage of the pure core: trajectory golden fixtures, every veto and selector, fall-through ordering, and the negative-injection/positive-consumption price band
**Target Platform**: Home Assistant Container (no Supervisor, therefore no add-ons) on a NAS; deployment is a manual file copy
**Project Type**: single
**Performance Goals**: One evaluation cycle under 5 s (NFR-001); ~140 blocks per trajectory for a 35-hour horizon; 288 cycles per day
**Constraints**: Zero commands to the inverter (C-001, NFR-005); ≤12 forecast fetches per hour (NFR-002); no third-party imports in the core; all tunables outside `configuration.yaml` (C-004); Europe/Brussels for every daily boundary (C-008)
**Scale/Scope**: One household, one battery, one solar plane; 38 FR / 8 NFR / 11 C; ~12 implementation concerns

## Charter Check

**SKIPPED — no charter exists.** `spec-kitty charter context --action plan --json` returned `mode: "missing"`; there is no `.kittify/charter/charter.yaml` in this project. No charter gates apply, and the Complexity Tracking section below is consequently empty. Built-in directives still informed this plan, notably DIRECTIVE_051 (supply-chain install safety), addressed in `research.md` R-08.

## Project Structure

### Documentation (this mission)

```
kitty-specs/price-aware-load-automation-01M3PH8Y/
├── plan.md              # This file
├── spec.md              # Mission specification
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── decision-record.md
│   ├── user-config.md
│   ├── cache-file.md
│   └── inverter-boundary.md
├── checklists/
│   └── requirements.md
└── decisions/           # Decision Moment records
```

### Source Code (repository root)

Paths below are relative to the repository root, which mirrors the Home Assistant config directory on the NAS.

```
configuration.yaml              # + rest: sensor for forecast.solar, + pyscript: block

pyscript/
├── battery_planner.py          # adapter script: @time_trigger, reads state and files,
│                               #   calls the core, writes log and cache
├── peak_guard.py               # fast-loop script (WP12): S0 only, every ~30 s
└── modules/                    # the ONLY importable place in pyscript (scripts cannot import each other)
    ├── config.py               # FR-025 — dict -> validated SiteConfig, fingerprint (pure)
    ├── prices.py               # FR-001, FR-002, FR-003, FR-030 (pure)
    ├── series.py               # FR-004, FR-005, FR-030, FR-059 — solar and usage block series (pure)
    ├── battery.py              # FR-006, FR-007 — charge from capacity x percent, stubbed (pure)
    ├── trajectory.py           # FR-008, FR-029, FR-031, FR-032 (pure)
    ├── capacity.py             # FR-041 - FR-055 — capacity tariff arithmetic (pure)
    ├── rules.py                # FR-009 - FR-017, FR-046 - FR-048 — vetoes and selectors (pure)
    ├── decision.py             # FR-018, FR-027, FR-028 — the record structure (pure)
    ├── cache.py                # FR-033 - FR-038 — cache validity logic (pure)
    └── inverter.py             # C-002 boundary: records intent, transmits nothing.
                                #   A module (importable by both scripts) but NOT pure: it does the
                                #   log append, via @pyscript_executor.

battery_planner/
├── user_config.example.yaml    # shipped; copied once to user_config.yaml on the NAS
├── user_config.yaml            # NAS only — gitignored, never overwritten
├── decisions.log               # NAS only — planner output
└── cache/                      # NAS only — derived series

tests/
├── test_prices.py
├── test_series.py
├── test_trajectory.py          # golden fixtures
├── test_rules.py               # every veto, every selector, fall-through ordering
├── test_cache.py
└── fixtures/                   # handmade price/solar/usage series with known answers
```

**Structure Decision**: Single project, organised by execution boundary rather than by layer type. `pyscript/modules/` holds the pure core plus the one impure boundary module (`inverter.py`) and is the only directory `tests/` imports from; `pyscript/battery_planner.py` and `pyscript/inverter.py` are the only files that know Home Assistant or pyscript exist; `battery_planner/` holds runtime state that is never copied between the repository and the NAS. The three directories have three distinct copy lifecycles, documented in `quickstart.md` — this is what keeps a manual deployment from destroying hand-entered configuration.

## Complexity Tracking

*No Charter Check violations — no charter exists for this project. Section intentionally empty.*

## Implementation Concern Map

> **Note**: Implementation concerns are NOT work packages and are NOT executable units.
> `/spec-kitty.tasks` translates these into executable WPs — one concern may become
> multiple WPs; multiple small concerns may merge into one WP.

### IC-01 — Site configuration

- **Purpose**: Give the homeowner one hand-edited file holding every value that differs between installations, and load it without letting stale settings go unnoticed.
- **Relevant requirements**: FR-025, FR-037, C-004
- **Affected surfaces**: `battery_planner/user_config.example.yaml`, adapter load path, `contracts/user-config.md`
- **Sequencing/depends-on**: none — every other concern reads its values
- **Risks**: A manual copy overwriting the live file destroys the homeowner's coefficients. Mitigated by shipping only the `.example` and gitignoring the real one. A configuration change to array geometry or block length must invalidate cached series (FR-037), so config load and cache validity are coupled.

### IC-02 — Price derivation and the rolling horizon

- **Purpose**: Turn the market price series into consumption and injection prices per block, and establish how far ahead the plan can see.
- **Relevant requirements**: FR-001, FR-002, FR-003, FR-030, C-007
- **Affected surfaces**: `pyscript/modules/prices.py`, `tests/test_prices.py`
- **Sequencing/depends-on**: IC-01
- **Risks**: The two directions use different coefficients and must never share one. The injection offset being negative means injection goes negative while the market price is still positive — the band that makes V2 load-bearing. Horizon length changes discontinuously when day-ahead prices publish around 13:00 local.

### IC-03 — Input series: solar forecast and usage profile

- **Purpose**: Produce expected solar production and expected household consumption per block across the horizon.
- **Relevant requirements**: FR-004, FR-005, FR-026, FR-030, C-005, C-006, NFR-002
- **Affected surfaces**: `configuration.yaml` (`rest:` sensor), `pyscript/modules/series.py`, adapter read path, `tests/test_series.py`
- **Sequencing/depends-on**: IC-01
- **Risks**: C-005 required resolving a real tension — the built-in Forecast.Solar integration has no YAML form, so a `rest:` sensor is used instead (`research.md` R-03). Forecast data is coarser than the block length and must be held flat, never interpolated (FR-030). The usage profile is the expensive series: ~672 quarter-hour buckets over seven days. Forecast fetches share a 12-per-hour budget with failure retries.

### IC-04 — Derived-series cache and staleness

- **Purpose**: Compute the input series once and reuse them across cycles, without ever letting a stale series pass as fresh.
- **Relevant requirements**: FR-033, FR-035, FR-036, FR-037, FR-038, C-011, SC-013, SC-014
- **Affected surfaces**: `pyscript/modules/cache.py` (validity logic), adapter (the actual file read/write), `battery_planner/cache/`, `contracts/cache-file.md`, `tests/test_cache.py`
- **Sequencing/depends-on**: IC-03
- **Risks**: The cache outlives the process, so a series written yesterday is on disk at boot looking exactly like a fresh one — the timestamp is what makes it safe, not an optimisation. The trajectory must never be cached (FR-034). Deleting the cache must change no decision (FR-038, SC-013). Validity logic is pure and testable; the file access is not, which is why they are split across two layers.

### IC-05 — Battery state

- **Purpose**: Present current stored energy as capacity × charge percentage, behind the seam the real HF2211 reading will later use.
- **Relevant requirements**: FR-006, FR-007, C-003
- **Affected surfaces**: `pyscript/modules/battery.py`
- **Sequencing/depends-on**: IC-01
- **Risks**: The stub must be unmistakable in the decision record (FR-027) so a nonsensical decision is traceable to the placeholder rather than to the rules. The seam's shape now determines how invasive the real reading is later.

### IC-06 — Trajectory projection

- **Purpose**: Project charge block by block across the horizon and surface the three facts a scalar balance cannot: when the battery fills, how much solar spills after that, and when it would hit the reserve floor.
- **Relevant requirements**: FR-008, FR-029, FR-031, FR-032, SC-011, SC-012
- **Affected surfaces**: `pyscript/modules/trajectory.py`, `tests/test_trajectory.py`, `tests/fixtures/`
- **Sequencing/depends-on**: IC-02, IC-03, IC-05
- **Risks**: The highest-value target for golden-fixture tests, and the most likely place for subtle errors. Two ceilings bind, not one: capacity *and* maximum charge power, so spill can begin before saturation. Clamping to the reserve floor and the capacity ceiling must not silently swallow energy that should have been reported as spill.

### IC-07 — Vetoes and selectors

- **Purpose**: Decide the action, with forbidding rules kept structurally distinct from choosing rules.
- **Relevant requirements**: FR-009 – FR-017, SC-003, SC-004, SC-010
- **Affected surfaces**: `pyscript/modules/rules.py`, `tests/test_rules.py`
- **Sequencing/depends-on**: IC-06
- **Risks**: The fall-through was specified wrongly twice during specify — a veto must remove one option and advance to the *next* selector, never terminate evaluation and never restart it. Tests pin the ordering mechanically. The export selector's window is bounded by saturation, not by the whole horizon; the import selector's by the projected breach.

### IC-08 — Decision record and logging

- **Purpose**: Write one auditable line per cycle that a human can check by hand against the prices and forecast for that moment.
- **Relevant requirements**: FR-018, FR-027, FR-028, NFR-004, NFR-008, SC-001, SC-002
- **Affected surfaces**: `pyscript/modules/decision.py` (structure), adapter (the write), `battery_planner/decisions.log`, `contracts/decision-record.md`
- **Sequencing/depends-on**: IC-07
- **Risks**: This is the mission's only output surface — trimming fields for brevity trades away the entire deliverable. Every field is mandatory, including an explicit "no veto applied" rather than an omitted field. Appends only, never rewrites.

### IC-09 — Inverter boundary

- **Purpose**: Hold everything that will one day speak to the HF2211, and for now record intent and send nothing.
- **Relevant requirements**: FR-019, FR-020, C-001, C-002, NFR-005, SC-009
- **Affected surfaces**: `pyscript/modules/inverter.py`, `contracts/inverter-boundary.md`
- **Sequencing/depends-on**: IC-08
- **Risks**: The interface shape decided here is what the real Modbus implementation must satisfy, so it should express intent (action, target power) rather than anything transport-shaped. Zero transmissions is a verifiable property, not an aspiration.

### IC-10 — Degraded modes and alerting

- **Purpose**: Distinguish losing prices (halt and email) from losing the forecast (continue with zero solar), and keep a long outage from flooding the inbox.
- **Relevant requirements**: FR-021, FR-022, FR-023, FR-024, NFR-003, NFR-007, SC-007, SC-008
- **Affected surfaces**: Adapter, `pyscript/modules/series.py` (zero-solar path), halt state handling
- **Sequencing/depends-on**: IC-03, IC-08
- **Risks**: At 288 cycles a day, alerting per cycle would send 288 emails — alert on entry and re-alert on an interval. Email credentials must be referenced indirectly and never committed (NFR-007); Home Assistant's own notify service is the natural carrier, keeping secrets in `secrets.yaml`, which `.gitignore` already excludes.

### IC-11 — pyscript adapter, scheduling and Home Assistant wiring

- **Purpose**: The only code that knows Home Assistant exists — trigger every five minutes, read sensor state and files, call the core, write the outputs.
- **Relevant requirements**: NFR-001, FR-026, and the I/O half of FR-033/FR-018
- **Affected surfaces**: `pyscript/battery_planner.py`, `configuration.yaml` (`pyscript:` block), `quickstart.md`
- **Sequencing/depends-on**: IC-01 – IC-10
- **Risks**: Every file operation must run off the event loop with `@pyscript_executor` (or `@pyscript_compile` plus `task.executor`; `@pyscript_compile` alone does NOT move work off the loop) — a blocking write inside Home Assistant's event loop is a real defect (`research.md` R-02). Cycles must not overlap (NFR-001); `@task_unique` covers this. ENTSO-e attribute key names need confirming against the live install. Assumes `allow_all_imports: true` for the YAML and JSON reads.

### IC-12 — Test suite

- **Purpose**: The only validation mechanism this mission has, since there is no inverter, no real battery reading, and no way to conjure interesting weather.
- **Relevant requirements**: Verification for FR-008 – FR-017, FR-029 – FR-038; SC-003, SC-004, SC-010 – SC-014
- **Affected surfaces**: `tests/`, `tests/fixtures/`
- **Sequencing/depends-on**: runs alongside IC-02 – IC-08, not after them
- **Risks**: Fixtures must encode cases live data will rarely produce — notably the band where injection is negative while consumption is positive, and a midday saturation with a better price afterwards. Writing tests after the fact would lose exactly the cases that motivated the rules.
