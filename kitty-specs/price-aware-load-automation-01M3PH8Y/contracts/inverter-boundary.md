# Contract: Inverter Boundary

**Satisfies**: FR-019, FR-020, C-001, C-002, NFR-005, SC-009
**File**: `pyscript/modules/inverter.py`

**Location: `pyscript/modules/inverter.py`.** pyscript top-level script files cannot import each other (each runs in its own isolated global context); only code in `<config>/pyscript/modules/` is importable by other scripts, and modules may use pyscript features (`@pyscript_executor`, `log`). The adapter and peak guard therefore `import inverter`. (Found in the WP09 review; the original plan placed it at `pyscript/inverter.py`.)

**This mission**: records intent, transmits nothing.

The interface defined here is what the eventual HF2211 Modbus implementation must satisfy. Its shape is therefore a decision with consequences well past this mission: it should express **intent**, not transport. No register numbers, no connection handles, no Modbus vocabulary — those belong inside a later implementation, behind this same signature.

## Interface

```python
def apply(action, target_power_kw, record):
    """Carry out a decision.

    This mission: append `record` to the decision log and return.
    Nothing is transmitted to the inverter.

    action           -- "charge" | "discharge" | "export" | "idle"
    target_power_kw  -- float, 0.0 when idle
    record           -- DecisionRecord, already complete

    Returns True when the intent was durably recorded.
    """


def read_charge_percent():
    """Current battery charge, 0-100.

    This mission: returns a stubbed value (FR-007, C-003).
    Later: reads the inverter over Modbus via the HF2211.
    """
```

## Rules

1. **Nothing is transmitted.** No socket is opened, no Modbus frame is built, no write is attempted. NFR-005 and SC-009 make the count of transmissions exactly zero, verifiable from this file's own records.
2. **The planner never imports anything below this boundary.** The core does not know the HF2211 exists; the adapter knows only these two functions.
3. **The stub is visible.** `read_charge_percent` returns a placeholder and the resulting decision is marked `soc_stubbed` (FR-027), so a nonsensical decision is traceable to the placeholder rather than to the rules.
4. **`apply` is the only writer of the decision log**, which is why "record the intent" and "act on the intent" are one call rather than two — the log cannot drift from what was attempted.
5. **File I/O lives here or in the adapter, never in the core** — and must be run off the event loop with `@pyscript_executor` (or `@pyscript_compile` plus `task.executor`; `@pyscript_compile` alone does NOT move work off the loop), since blocking I/O inside pyscript's interpreter runs in Home Assistant's event loop (`research.md` R-02).

## Swapping in real control, later

Enabling real commands should touch this file and nothing else:

- `apply` gains a Modbus write after the log append, ordered so the log records intent even if the write fails.
- `read_charge_percent` reads a holding register instead of returning a stub.
- The stub marker stops appearing in `degraded`, which is itself the signal that the switch happened.

If a future change requires editing `pyscript/modules/`, the boundary was drawn in the wrong place.
