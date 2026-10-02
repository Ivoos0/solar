# Contract: Inverter Boundary

**Satisfies**: FR-019, FR-020, C-001, C-002, NFR-005, SC-009
**Files**: `pyscript/modules/inverter.py` (the boundary), `pyscript/modules/inverter_<type>.py` (drivers)

**Location: `pyscript/modules/`.** pyscript top-level script files cannot import each other (each runs in its own isolated global context); only code in `<config>/pyscript/modules/` is importable by other scripts, and modules may use pyscript features (`@pyscript_executor`, `log`). The adapter and peak guard therefore `import inverter`.

**Default behaviour**: `inverter.type: logging` records intent and transmits nothing. Other types are drop-in driver files; none is shipped.

The boundary expresses **intent**, not transport. No register numbers, no connection handles, no Modbus vocabulary appear in `inverter.py`; those belong inside a driver.

## Interface the adapters call (`inverter.py`)

```python
def apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None, log_dir=DEFAULT_LOG_DIR):
    """Log the decision, THEN hand the intent to the configured driver.

    The line goes to log_path when given (tests, back-compat); otherwise to
    log_path_for(record.timestamp, log_dir), i.e.
    <log_dir>/decisions-YYYY-MM-DD.log for the record's own local date.

    Returns True when the decision line was durably recorded, False on any
    refusal or logging failure. Never raises. Driver outcomes never change it.
    """


def log_path_for(day, log_dir=DEFAULT_LOG_DIR):
    """Pure: '<log_dir>/decisions-YYYY-MM-DD.log' from the year, month and day of `day`."""


def read_charge(inverter_type="logging", driver_dir=None):
    """Returns (percent, is_stub, marker).

    is_stub -- the value is a placeholder: the caller marks decisions soc_stubbed.
    marker  -- None, or "inverter_driver_unavailable" / "inverter_read_failed":
               the caller adds it to the decision's degraded list. percent is
               then 50.0 and is_stub is True.
    """
```

Both adapters pass `cfg.inverter_type` and their `CORE_DIR` as `driver_dir`, so the planner and the guard always select the driver from the same config value.

## Interface a driver implements (`inverter_<type>.py`)

```python
SOC_IS_STUB = False                      # optional, default False

def send(action, target_power_kw): ...   # True = accepted; False or raise = failed
def read_charge_percent(): ...           # number, 0..100
```

`action` is `charge`, `discharge`, `export` or `idle`; power is >= 0 kW, 0.0 when idle. `inverter_logging.py` is the reference implementation and the copy-me template.

## Rules

1. **The decision log line is always written, by `apply`, for every driver, and first.** The driver is called only after the line is durably on disk. If the line cannot be written, or `apply` refuses (action/power do not match the record), the driver is not called.
2. **No driver outcome changes the record or the return value.** A driver that raises, times out (`DRIVER_TIMEOUT_SECONDS`, 10 s), returns `False`, lacks `send`/`read_charge_percent`, fails to import or does not exist is logged (`log.error`/`log.warning`) and nothing else happens. The cycle and the guard continue.
3. **Unknown or broken driver is safe and visible.** Nothing is transmitted (log-only behaviour), `apply` logs an error on every call, and `read_charge` returns the 50.0 placeholder with `is_stub=True` and marker `inverter_driver_unavailable`, which lands in `degraded=`. A failed load is not cached, so a fixed file is picked up on the next call. An unusable reading (exception, timeout, not a number in 0..100, a bool) gives the same placeholder with `inverter_read_failed`.
4. **The stub is visible.** `logging` sets `SOC_IS_STUB = True`; its decisions are marked `soc_stubbed` (FR-027). A driver that reads real hardware leaves the flag unset, and the marker disappears, which is itself the signal that the switch happened.
5. **Drivers never run on the event loop.** `inverter.py` loads them with importlib inside a `@pyscript_executor` helper (private name `inverter_driver_<type>`, no `sys.path` entry, no bare alias) and calls them through another executor helper that applies the timeout in a daemon thread. A driver is ordinary CPython and must not use pyscript globals. Drivers are cached after a successful load; changing one needs a Home Assistant restart.
6. **`apply` is the only writer of the decision log lines for decisions**, which is why "record the intent" and "act on the intent" are one call rather than two. The planner also appends its own HALT/RECOVERED/SKIP lines, through its own helper, into the same day's file (`log_path_for`).
7. **The planner never imports anything below this boundary.** The core does not know a driver exists; the adapters know only `apply` and `read_charge`.
8. **File I/O lives here or in the adapter, never in the core**, run off the event loop with `@pyscript_executor` (`@pyscript_compile` alone does NOT move work off the loop; `research.md` R-02).
9. **Concurrency.** The planner and the guard may call a driver at the same time from different threads; a driver serializes its own bus access.

## Config

`inverter.type` (default `logging`; missing, null or `none` mean `logging`; otherwise `[a-z0-9_]+`). See `user-config.md`. A non-`logging` driver transmits real commands and is the contributor's responsibility; README "Adding an inverter" holds the skeleton and the checklist, including the guard's FUTURE RISK (commanded discharge lowers the net offtake it reads; add-back and heartbeat needed before real control).

## Adding an inverter

Add one file. `inverter.py`, the planner and the guard do not change.
