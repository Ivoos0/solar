# Inverter boundary

The planner and the peak guard never talk to an inverter directly. They call
`pyscript/modules/inverter.py`, which writes the decision line and then hands
the intent to a driver file, `pyscript/modules/inverter_<type>.py`, selected
with `inverter.type`. The default `logging` driver records the intent and
transmits nothing. Other types are drop-in files; none is shipped. The
walkthrough for writing one is in the README under
[Adding an inverter driver](../README.md#adding-an-inverter-driver); this page
is the exact interface and rules.

The boundary expresses intent, not transport. No register numbers, connection
handles or Modbus vocabulary appear in `inverter.py`; those belong in a driver.

`inverter.py` lives in `pyscript/modules/` because pyscript top-level scripts
cannot import each other: only code under `<config>/pyscript/modules/` is
importable from several scripts, and modules may use pyscript features
(`@pyscript_executor`, `log`). Both the planner and the guard import it.

## What the adapters call (`inverter.py`)

```python
def apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None, log_dir=DEFAULT_LOG_DIR,
          resend_minutes=DEFAULT_RESEND_MINUTES, state_dir=None):
    """Log the decision, THEN hand the intent to the configured driver.

    The line goes to log_path when given (tests); otherwise to
    log_path_for(record.timestamp, log_dir), i.e.
    <log_dir>/decisions-YYYY-MM-DD.log for the record's own local date.

    The driver's send() is called only when the command (action, power
    rounded to 0.01 kW) differs from the last command SENT, or the last send
    is resend_minutes old (0 = every call). state_dir (default
    <log_dir>/state) holds last_command.json.

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

Both adapters pass `cfg.inverter_type`, `cfg.inverter_resend_minutes`, their own
directory as `driver_dir` and `<config>/battery_planner/state/` as `state_dir`,
so they always select the same driver and share the same de-duplication window
and `last_command.json`.

## What a driver implements (`inverter_<type>.py`)

```python
SOC_IS_STUB = False                      # optional, default False

def send(action, target_power_kw): ...   # True = accepted; False or raise = failed
def read_charge_percent(): ...           # number, 0..100
```

`action` is `charge`, `discharge`, `export` or `idle`; power is 0 or more kW and
0.0 when idle. `charge` is a forced charge from the grid. `inverter_logging.py`
is the reference implementation and the file to copy.

**`idle` means: cancel every forced mode this project set (forced grid charge,
forced export, forced discharge) and return the inverter to its default
behaviour.** That default is what the inverter does when it is sent nothing:
charge from solar surplus until full, then export; drain to serve the house
until empty, then use grid power. A driver must therefore make `send("idle",
0.0)` a real release. It is not "do nothing", because a forced mode stays in
force until it is cancelled or the inverter drops it. The conformance check
cannot verify this without hardware; you must.

## Rules

1. **The decision line is written first, by `apply`, for every driver.** The
   driver is called only after the line is on disk. If the line cannot be
   written, or `apply` refuses (action and power do not match the record), the
   driver is not called.
2. **No driver outcome changes the record or the return value.** A driver that
   raises, times out (10 seconds), returns `False`, lacks `send` or
   `read_charge_percent`, fails to import or does not exist is logged and
   nothing else happens. The cycle and the guard continue.
3. **An unknown or broken driver is safe and visible.** Nothing is transmitted,
   `apply` logs an error on every call, and `read_charge` returns the 50.0
   placeholder with `is_stub=True` and marker `inverter_driver_unavailable`,
   which appears in `degraded=`. A failed load is not cached, so a fixed file
   is picked up on the next call. An unusable reading (exception, timeout, not a
   number from 0 to 100, a bool) gives the same placeholder with
   `inverter_read_failed`.
4. **The stub is visible.** `logging` sets `SOC_IS_STUB = True`, so its
   decisions are marked `soc_stubbed`. A driver that reads real hardware leaves
   the flag unset and the marker disappears.
5. **Drivers never run on the event loop.** `inverter.py` loads them with
   importlib inside a `@pyscript_executor` helper (private name
   `inverter_driver_<type>`, no `sys.path` entry, no bare alias) and calls them
   through another executor helper that applies the timeout in a daemon thread.
   A driver is ordinary CPython and cannot use pyscript names. Drivers are
   cached after a successful load; changing one needs a Home Assistant restart.
6. **`apply` is the only writer of decision lines.** The planner also appends
   its own HALT, RECOVERED and SKIP lines, through its own helper, to the same
   day's file.
7. **The core never imports anything below this boundary.** It does not know a
   driver exists; the adapters know only `apply` and `read_charge`.
8. **File I/O lives here or in the adapter, never in the core**, and runs off
   the event loop with `@pyscript_executor`. `@pyscript_compile` alone does not
   move work off the loop.
9. **Concurrency.** The planner and the guard may call a driver at the same time
   from different threads; a driver serializes its own bus access.
10. **Command de-duplication.** The decision line is written on every call. The
    driver's `send()` is called only when (a) no command is on record, (b) the
    command differs from the recorded one in `action` or in `target_power_kw`
    rounded to 0.01 kW, or (c) the recorded send is `resend_minutes` old or older
    (`inverter.resend_minutes`, integer 0 or more, default 15; `0` = every call;
    a record dated in the future also sends). The record is
    `<state_dir>/last_command.json`:
    `{"action": ..., "power_kw": ..., "sent_at": <UTC ISO 8601>}`, written
    atomically and read on every call, so it survives a restart and is shared by
    the planner and the guard. It is kept in memory as a fallback when the file
    cannot be written. The time used is the decision record's timestamp. A
    command is recorded only when the driver accepted it; a send that raised,
    timed out, returned `False` or whose driver is missing is not recorded and
    also clears the existing record, because the inverter's state is then
    unknown and the next call must send. A missing, empty, corrupt or incomplete
    file means "no command on record", never an error. The `logging` driver
    transmits nothing and keeps no record.
11. **`idle` is sent once.** An `idle` command is sent when the recorded command
    was not `idle` (a forced mode is in force and must be cancelled), or when no
    command is on record (the first call after an install or restart). It is
    not re-sent while idle persists, however old the record is. The planner
    therefore sends at most one `idle` after a forced action. `resend_minutes: 0`
    still means every call, `idle` included. A failed `idle` send is not
    recorded, so the next call sends it again.

## Settings

`inverter.type` (default `logging`; missing, null or `none` mean `logging`;
otherwise lowercase letters, digits and underscore) and `inverter.resend_minutes`
(see the README settings table).

A non-`logging` driver sends real commands and is your responsibility. The
README checklist covers what the peak guard already handles (it adds the
commanded discharge back to the net offtake it reads, so a shave does not flip
on and off; it refreshes a `last_beat` attribute on
`pyscript.peak_guard_shaving` that the planner requires to be at most 3 guard
intervals old) and what the driver author must still check (the guard sends a
command only on change, so the inverter must hold a discharge command until the
next one and honour `idle` as defined above; the add-back assumes the commanded power is
delivered).

## Adding an inverter

Add one file. `inverter.py`, the planner and the guard do not change.
