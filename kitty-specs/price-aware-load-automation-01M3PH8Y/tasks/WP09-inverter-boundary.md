---
work_package_id: WP09
title: Inverter boundary
dependencies:
- WP06
requirement_refs:
- FR-019
- FR-020
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T040
- T041
- T042
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/inverter.py
create_intent:
- pyscript/modules/inverter.py
execution_mode: code_change
owned_files:
- pyscript/modules/inverter.py
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

Hold everything that will one day speak to the HF2211. For this mission it records intent and
**transmits nothing**.

Satisfies **FR-019, FR-020**. Enforces **C-001, C-002, NFR-005, SC-009**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP09 --agent <name>`. Depends on WP06 — the record format
  must settle before the file that writes it.

## What you are actually deciding here

This file is small, and it would be easy to treat as plumbing. It is not. **The interface signed
here is what the real Modbus implementation must satisfy**, and it is the difference between
enabling real control later being a change to one file or a rewrite of the planner.

The rule that keeps it honest: express **intent**, not transport. No register numbers, no connection
handles, no Modbus vocabulary, no HF2211 specifics. Those belong inside a later implementation,
behind this same signature.

A useful test while writing: could this interface be satisfied by a completely different inverter
brand, or by a mock in a test? If not, transport has leaked in.

## Context you need

- `contracts/inverter-boundary.md` — **authoritative**. The two function signatures are specified
  there in full.
- `contracts/decision-record.md` — the format this file writes
- `research.md` R-02 — why file I/O cannot sit in pyscript's interpreter unguarded
- `spec.md` FR-019, FR-020, C-001, C-002, NFR-005

---

### T040 — Implement `inverter.apply()`: record intent, transmit nothing

**Purpose**: The single call the adapter makes to act on a decision.

**Steps**:

1. Create `pyscript/modules/inverter.py`.
2. Implement the signature from the contract:
   ```python
   def apply(action, target_power_kw, record):
       """Carry out a decision.

       This mission: append `record` to the decision log and return.
       Nothing is transmitted to the inverter.
       """
   ```
3. Format the record using WP06's `format_record` and append it to
   `battery_planner/decisions.log`, one line.
4. Return `True` when the intent was durably recorded, `False` otherwise. The adapter uses this to
   decide whether the cycle succeeded.
5. **Append, never rewrite** (NFR-008). Open in append mode; never read the file first, never
   truncate, never rotate it from here.
6. Leave a comment marking exactly where the future Modbus write goes, and state the ordering that
   will matter then: **log first, transmit second**, so the log records intent even if the
   transmission fails. A log that only records successful commands cannot explain a failure.
7. `apply` is the **only** writer of the decision log. That is deliberate: it keeps the log from
   drifting away from what was actually attempted. Do not add a second logging path elsewhere.

**Files**: `pyscript/modules/inverter.py` (new, ~80 lines)

**Validation**:
- A decision passed to `apply` appears as one line in the log
- Repeated calls append; earlier lines survive
- `grep` for `socket`, `modbus`, `connect`, `serial` in this file returns **nothing**
- Returns `True` on success

---

### T041 — Implement `read_charge_percent()` as a visible stub

**Purpose**: Supply a battery charge reading through the seam the real one will later use.

**Steps**:

1. Implement:
   ```python
   def read_charge_percent():
       """Current battery charge, 0-100.

       This mission: returns a stubbed value (FR-007, C-003).
       Later: reads the inverter over Modbus via the HF2211.
       """
   ```
2. Return a fixed, plausible value — 50.0 is a reasonable midpoint that exercises most rules without
   sitting at a boundary.
3. Make the stub **configurable** from the user config if that is cheap, so the homeowner can dial it
   to 95% or 5% and watch which selectors fire. That turns the stub from a limitation into a testing
   instrument, and it costs one config field. If it complicates the signature, keep the constant and
   note the idea.
4. The stub must be **unmistakable**. Name the constant so it cannot be read as a measurement
   (`STUBBED_CHARGE_PERCENT`), and comment it clearly.
5. The resulting decision carries `degraded=soc_stubbed` — that marker comes from WP06's rendering,
   driven by `BatteryState.is_stubbed` from WP04. Confirm the flag is set when the adapter builds
   the state; do not set it here.

**Files**: `pyscript/modules/inverter.py` (continues)

**Validation**:
- Returns a float in 0–100
- The constant's name makes the stub obvious at a glance
- Every decision built on it is marked degraded downstream

**Why this returns a constant rather than reading Home Assistant**: C-003 puts the HF2211 link
entirely out of scope. Reading some other entity as a proxy would create a dependency the real
implementation then has to unpick, and would make `degraded=soc_stubbed` a lie.

---

### T042 — Make the log append executor-safe

**Purpose**: Keep a blocking write out of Home Assistant's event loop.

**Steps**:

1. pyscript runs an asynchronous AST interpreter that shares Home Assistant's event loop. **CORRECTED
   after the cycle-1 review:** `@pyscript_compile` alone does NOT take blocking I/O off that loop - it
   only makes the function native Python, and a native `open()`/`write()`/`os.fsync` still blocks the
   loop. The write must run in an executor thread: decorate the helper with `@pyscript_executor`
   (compile + executor in one decorator), or keep `@pyscript_compile` and call it as
   `task.executor(_append_line, path, line)` (`research.md` R-02, corrected). An unguarded write here
   blocks Home Assistant every five minutes; fsync on a NAS is the worst of it.
2. Put the actual file write in a small helper decorated as above, and call it from `apply`. Keep the
   helper minimal — open, append one line, close.
3. Handle write failure without taking the planner down: catch, return `False`, and let the adapter
   record the problem. A full disk should degrade the mission, not stop Home Assistant.
4. Create the parent directory if it does not exist, so a fresh install works without manual setup.
5. Add a brief comment explaining *why* the decorator is there. Without it, a future reader will
   reasonably assume it is redundant and remove it, and the symptom — Home Assistant becoming
   sluggish every five minutes — would be very hard to trace back.

**Files**: `pyscript/modules/inverter.py` (continues)

**Validation**:
- The write path runs in an executor thread (`@pyscript_executor`, or `@pyscript_compile` + `task.executor`) - not merely `@pyscript_compile`d
- Home Assistant logs no blocking-I/O warning when the planner runs
- A write failure returns `False` and raises nothing into the adapter
- A missing `battery_planner/` directory is created rather than erroring

**Note on testing**: this file is **not** unit-tested by the suite, because it is pyscript-hosted and
performs I/O. That is expected and is precisely why it is kept this thin. Everything worth testing
lives in `pyscript/modules/`. Verification here is by inspection and by watching the log on the NAS.

---

## Definition of Done

- `pyscript/modules/inverter.py` exists with exactly the two contract functions
- `apply` appends one formatted line per decision and returns a success flag
- `read_charge_percent` returns an unmistakably stubbed value
- The write path is compiled or executor-dispatched, with a comment saying why
- **Zero** transmission code: no socket, no Modbus, no serial, no connection handling
- A comment marks where the future Modbus write goes, and states log-first ordering

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Blocking `open()` on the event loop** | run the write helper in an executor (`@pyscript_executor`); `@pyscript_compile` alone is not enough; the symptom otherwise is HA stalling every 5 minutes |
| Transport vocabulary leaking into the signature | Ask: could a different brand satisfy this interface? |
| A second code path writing the log | `apply` is the only writer, so the log cannot drift from intent |
| Rewriting or rotating the log from here | Append only (NFR-008) |
| The stub reading a real entity as a proxy | Makes `degraded=soc_stubbed` a lie and creates a dependency to unpick later |
| A write failure propagating into the adapter | Catch and return `False`; a full disk must not stop Home Assistant |

## Reviewer guidance

**First, grep for transmission.** `socket`, `modbus`, `pymodbus`, `serial`, `connect`, `HF2211` —
any hit is a C-001 violation and the work package fails regardless of anything else. NFR-005 makes
the count of transmissions exactly zero, and that is verifiable by reading one short file.

**Second, check the write guard.** Find the `open()` call and confirm it sits inside a
function that runs in an executor thread (`@pyscript_executor`, or `@pyscript_compile` + `task.executor`). This is the defect most likely to survive
review, because the code looks perfectly ordinary and the consequence only appears on the NAS as
intermittent sluggishness.

**Third, read the interface as if you were implementing Modbus behind it next month.** Does `apply`
give you everything you need — action and target power — without also constraining *how* you do it?
Does `read_charge_percent` have an obvious real implementation? If enabling real control would
require touching `pyscript/modules/`, the boundary is in the wrong place and now is the cheap moment
to move it.
