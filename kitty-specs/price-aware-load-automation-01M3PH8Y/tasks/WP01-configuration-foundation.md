---
work_package_id: WP01
title: Configuration foundation and test scaffolding
dependencies: []
requirement_refs:
- FR-025
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T001
- T002
- T003
- T004
- T005
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/config.py
create_intent:
- battery_planner/user_config.example.yaml
- pyscript/modules/config.py
- tests/conftest.py
- tests/test_config.py
execution_mode: code_change
owned_files:
- battery_planner/user_config.example.yaml
- pyscript/modules/config.py
- tests/conftest.py
- tests/test_config.py
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

Give the homeowner one hand-edited file holding every value that differs between installations, and
parse it with pure code that fails loudly on bad input. Establish the test scaffolding every later
work package depends on.

Satisfies **FR-025**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Do not create a branch by hand — run `spec-kitty agent action implement WP01 --agent <name>` and
  work inside the workspace it gives you.

## Context you need

Read before starting:

- `kitty-specs/price-aware-load-automation-01M3PH8Y/contracts/user-config.md` — the schema and
  validation rules are specified there in full. Treat it as authoritative over anything below.
- `kitty-specs/price-aware-load-automation-01M3PH8Y/data-model.md` — the `SiteConfig` table.

**The single most important constraint in this work package**: `config.py` lives in the pure core.
It takes a **plain dict** and returns a validated config object. It must **never** open a file,
never import `yaml`, never import anything from Home Assistant or pyscript. The adapter (WP10) reads
the file and hands the dict in.

This is not a stylistic preference. pyscript runs an AST interpreter, not CPython, and any function
touching `open()` needs `@pyscript_executor` (or `@pyscript_compile` plus `task.executor`) — both pyscript globals that do not
exist under CPython, which would break `pytest` at import time. Keeping I/O out of the core is what
makes the entire test suite possible. Getting this wrong in the first work package contaminates
every later one.

---

### T001 — Create `battery_planner/user_config.example.yaml`

**Purpose**: The file the homeowner copies once and then hand-edits forever. It is the only place
site-specific values live (C-004).

**Steps**:

1. Create the file with the full schema from `contracts/user-config.md`, grouped into the five
   sections: `prices`, `battery`, `solar`, `timing`, `alerts`, plus top-level `timezone`.
2. Pre-fill **this site's** real values, which were confirmed during the specify interview:
   - `prices`: multipliers and offsets 1.07 / +0.007 (consumption), 0.94 / −0.011 (injection) —
     these match the formulas already in `configuration.yaml`
   - `solar`: `latitude: 51.12`, `longitude: 3.85`, `kwp: 8.1`, `declination: 50`, `azimuth: -10`
   - `timing`: `block_minutes: 15`, `evaluation_interval_minutes: 5`,
     `forecast_refresh_minutes: 60`, `forecast_retry_minutes: 10`,
     `solar_cache_stale_minutes: 120`, `usage_cache_stale_minutes: 2880`
   - `battery`: `reserve_percent: 10.0`, `max_charge_kw: 5.0`, `max_discharge_kw: 5.0`,
     `round_trip_efficiency: 0.90`
   - `alerts`: `realert_minutes: 60`
   - `timezone: Europe/Brussels`
3. Leave `battery.capacity_kwh` and `alerts.address` as obvious placeholders — they have **no
   sensible default** and must be filled in by the homeowner.
4. Comment the non-obvious entries. Three deserve explanation in particular:
   - `injection_offset` is negative, which is why injection price goes negative while the market
     price is still positive
   - `max_charge_kw` binds **per block**, independently of capacity
   - `azimuth` uses forecast.solar's convention: 0 = south, negative = east

**Files**: `battery_planner/user_config.example.yaml` (new, ~60 lines including comments)

**Validation**:
- Parses as valid YAML
- Every field named in `contracts/user-config.md` is present
- The two required-no-default fields are unmistakably placeholders

**Note**: Do **not** create `battery_planner/user_config.yaml`. Only the `.example` is committed;
the live file is created once on the NAS by a deliberate rename and is gitignored. Creating the live
file here would risk it being copied over the homeowner's real settings.

---

### T002 — Build `config.py`: parse a plain dict into SiteConfig

**Purpose**: Turn the loaded YAML mapping into a typed, validated object the rest of the core reads.

**Steps**:

1. Create `pyscript/modules/config.py`.
2. Define `SiteConfig` holding every field from the `data-model.md` table. A `dataclass` is the
   natural choice; if you prefer a plain class, keep attribute access identical.
3. Write `from_dict(raw)` taking the parsed YAML mapping and returning a `SiteConfig`.
4. Apply defaults for every field that has one (see the `data-model.md` Default column). Only
   `capacity_kwh` and `alert_address` are required with no default.
5. Flatten the nested YAML groups into flat attributes — `prices.consumption_multiplier` in the file
   becomes `config.consumption_multiplier` in code. The nesting exists for human readability; the
   core should not navigate it.

**Files**: `pyscript/modules/config.py` (new, ~120 lines with T003 and T004)

**Validation**:
- `from_dict({...})` with a complete mapping returns an object with every attribute populated
- A mapping omitting an optional field gets the documented default
- No `import yaml`, no `open()`, no Home Assistant import anywhere in the file

**Edge cases**:
- Missing optional section entirely (e.g. no `timing:` block): all its defaults apply
- Extra unknown keys: ignore rather than error — a newer example file should not break an older core

---

### T003 — Add configuration validation with startup-error semantics

**Purpose**: Bad configuration must fail loudly, not quietly produce plausible-looking decisions.

**Steps**:

1. Add `validate()` (or validate inside `from_dict` — your call, but be consistent) enforcing every
   rule from `contracts/user-config.md`:

   | Rule | Why |
   |---|---|
   | `capacity_kwh > 0` | Every energy figure scales from it |
   | `0 <= reserve_percent < 100` | 100 would leave nothing usable |
   | `0 < round_trip_efficiency <= 1` | Above 1 would make arbitrage always profitable |
   | `60 % block_minutes == 0` | Blocks must align to hourly price and forecast periods |
   | `-180 <= azimuth <= 180` | forecast.solar's accepted range |
   | `0 <= declination <= 90` | forecast.solar's accepted range |
   | `alert_address` non-empty | A halt with nowhere to alert is a silent failure |
   | `max_charge_kw > 0` and `max_discharge_kw > 0` | Zero makes the trajectory meaningless |

2. Raise a single clear exception type on failure — define `ConfigError` in this module. The message
   must name the offending field and the value seen, because the homeowner reading the Home
   Assistant log is the audience.
3. Report **all** validation failures, not just the first. Someone filling in a fresh config should
   learn about three mistakes in one restart, not three restarts.

**Files**: `pyscript/modules/config.py` (continues)

**Validation**:
- Each rule above has a test proving it rejects a bad value
- The exception message names the field
- Two simultaneous errors produce one exception mentioning both

---

### T004 — Add config fingerprint over block-meaning fields

**Purpose**: WP07 invalidates cached series when configuration changes what a block *means*. This
computes the value it compares against.

**Steps**:

1. Add `fingerprint()` returning a short stable string derived from **only** these fields:
   `latitude`, `longitude`, `array_kwp`, `array_declination`, `array_azimuth`, `block_minutes`.
2. Use `hashlib` (standard library — allowed in the core) over a canonical string of those values.
   Truncate to 8 hex characters; collision risk is irrelevant at this scale and short values keep
   cache files readable.
3. **Do not** include price coefficients, battery parameters, or alert settings. Changing a price
   multiplier does not change what a cached solar block means, and invalidating the cache for it
   would throw away good data.

**Files**: `pyscript/modules/config.py` (continues)

**Validation**:
- Same inputs produce the same fingerprint across processes (no `hash()` — it is salted per run)
- Changing `block_minutes` changes the fingerprint
- Changing `consumption_multiplier` does **not** change it

**Why this matters**: `hash()` in Python is randomised per interpreter run for strings. Using it here
would invalidate the cache on every Home Assistant restart, silently, and the only symptom would be
a slightly slower first cycle — a bug that could hide for months.

---

### T005 — Create `tests/conftest.py` so tests import from `pyscript/modules/`

**Purpose**: Let `pytest` import the core from a directory that is not a Python package root, with
no install step and no path manipulation in every test file.

**Steps**:

1. Create `tests/conftest.py`.
2. Add `pyscript/modules/` to `sys.path` relative to the repository root, resolved from
   `__file__` rather than the working directory — tests must pass whether invoked from the repo root
   or from inside `tests/`.
3. Add a small shared fixture returning a valid `SiteConfig`, since every later test file needs one.
   Give it this site's real values so fixtures read realistically.
4. Create `tests/test_config.py` covering T002–T004: happy-path parse, every validation rule,
   default application, and fingerprint stability.

**Files**:
- `tests/conftest.py` (new, ~30 lines)
- `tests/test_config.py` (new, ~130 lines)

**Validation**:
- `py -3.12 -m pytest tests/ -v` passes from the repository root
- `cd tests && py -3.12 -m pytest . -v` also passes
- No test imports Home Assistant or pyscript

---

## Definition of Done

- `battery_planner/user_config.example.yaml` exists, parses, and carries this site's real values
- `pyscript/modules/config.py` parses a dict, validates it, and computes a stable fingerprint
- `config.py` contains **zero** file I/O, **zero** third-party imports, and **zero** HA/pyscript imports
- `tests/conftest.py` makes the core importable; `tests/test_config.py` passes
- `pytest tests/` is green
- `battery_planner/user_config.yaml` was **not** created or committed

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| Putting file loading in `config.py` because it feels natural | The adapter loads; the core parses. This seam is the plan's central decision. |
| Using `hash()` for the fingerprint | Salted per run — use `hashlib`. |
| Committing a live `user_config.yaml` | Only the `.example` is committed; verify with `git status` before finishing. |
| Validating only the first error | Collect all failures; the homeowner should not restart three times. |

## Reviewer guidance

Check first, before anything else: **does `config.py` import anything it should not?** `grep` it for
`open`, `yaml`, `hass`, `pyscript`. A single hit means the seam is already broken and every later
work package inherits the problem.

Then confirm the fingerprint covers exactly the six block-meaning fields — no more, no fewer. Too
many fields means needless cache invalidation; too few means a stale cache surviving a change that
should have killed it.

Finally, check the example config against `contracts/user-config.md` field by field. A missing field
here becomes a `KeyError` somewhere far away, in a work package written days later.
