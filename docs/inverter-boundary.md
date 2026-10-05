# Inverter boundary

The planner and the peak guard never talk to an inverter directly. They call
`pyscript/modules/inverter.py`, which writes the decision line and then hands
the intent to a driver file, `pyscript/modules/inverter_<type>.py`, selected
with `inverter.type`. The default `logging` driver records the intent and
transmits nothing. Other types are drop-in files; none is shipped. The
walkthrough for writing one is in
[Adding an inverter driver](inverter-drivers.md#adding-an-inverter-driver); this page
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
          resend_minutes=DEFAULT_RESEND_MINUTES, state_dir=None,
          dry_run=False):
    """Log the decision, THEN hand the intent to the configured driver.

    The line goes to log_path when given (tests); otherwise to
    log_path_for(record.timestamp, log_dir), i.e.
    <log_dir>/decisions-YYYY-MM-DD.log for the record's own local date.

    The driver's send() is called only when the command (action, power
    rounded to 0.01 kW) differs from the last command SENT, or the last send
    is old enough to need a refresh: 0.8 x the driver's COMMAND_HOLD_MINUTES
    when it declares one, else resend_minutes (0 = every call). An idle
    command is sent once (rule 11). state_dir (default <log_dir>/state) holds
    last_command.json.

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
COMMAND_HOLD_MINUTES = None              # optional: positive number of minutes

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
    rounded to 0.01 kW, or (c) the recorded send is old enough to need a refresh
    (see rule 12; a record dated in the future also sends). The record is
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
12. **When an unchanged command is sent again.** A driver may declare
    `COMMAND_HOLD_MINUTES`: a positive number, the time after which the
    inverter drops a forced command that was not refreshed. Absent or `None`
    means unknown. An unchanged non-`idle` command is then sent again once its
    recorded send is 0.8 x that old (hold 10 minutes: at 8 minutes, not before),
    and `inverter.resend_minutes` is not used for that driver. A driver that
    declares nothing falls back to `inverter.resend_minutes` (integer 0 or more,
    default 15; `0` = every call). A value that is not a positive number (a
    string, a bool, zero, negative, NaN, infinity) is ignored with one warning
    in the Home Assistant log and the fallback applies. A recorded send older
    than the window is expired: after a restart, a stale `last_command.json`
    never blocks a send. The attribute is read from the loaded driver the way
    `SOC_IS_STUB` is.

## Plan-style drivers (service calls through Home Assistant)

Some integrations are controlled by switching Home Assistant helpers rather than
by a service or a bus. Drivers are loaded as plain CPython in an executor thread
and cannot call Home Assistant, so the interface has a second form. A driver may
define

```python
SOC_ENTITY = "sensor.my_battery_soc"     # optional: battery charge in percent
COMMAND_HOLD_MINUTES = 10                # optional, as above

def plan(action, target_power_kw):       # pure: returns data, does no I/O
    return [{"domain": "input_number", "service": "set_value",
             "data": {"entity_id": "input_number.my_power", "value": 2.0}},
            {"domain": "input_boolean", "service": "turn_on",
             "data": {"entity_id": "input_boolean.my_force_charge"}}]
```

instead of `send`. If `plan` exists it is used and `send` is ignored. Existing
`send`-style drivers, and the `logging` driver, are not affected.

1. **Where it runs.** `plan` runs in the same worker thread and under the same
   10 second limit as `send`; it returns data only. The boundary (interpreted
   pyscript code in `inverter.py`) then executes the calls one after another
   with `service.call(domain, service, **data)`. A `plan` that raises, times out
   or returns an invalid list is a failed send.
2. **Validation.** The result must be a list (or tuple) of at most 12 items.
   Each item is a dict with the keys `domain`, `service` and optionally `data`
   and no others. `domain` and `service` are strings made of `[a-z0-9_]`.
   `data` is a dict whose keys are non-empty strings (not `blocking`,
   `return_response` or `limit`) and whose values are `str`, `int`, `float`
   (finite), `bool` or a flat list of those. `None`, nested lists, dicts and
   other objects are rejected. A rejected plan executes nothing and is logged
   as an error; an empty list is valid. The validated calls are copies, not the
   driver's own objects.
3. **Failure.** A call that raises (for example `ServiceNotFound`) stops the
   sequence, is logged, and counts as a failed send: not recorded as sent, and
   the existing record in `last_command.json` is cleared, exactly as for
   `send`. Errors from `plan` (raising, timeout, invalid) and from the calls are
   logged at most once per 30 minutes for the same driver and cause. Nothing is
   ever raised into the planner or the guard, and the decision log line is
   written first and unchanged.
4. **When it runs.** `plan` is called, and its calls executed, only when `apply`
   decides to send (rules 10 to 12): a changed command, the resend time, or the
   one `idle` after a forced mode. Unchanged commands inside the window call
   neither `plan` nor `service.call`.
5. **`SOC_ENTITY`.** When the driver declares it (an entity id such as
   `sensor.my_battery_soc`), `read_charge` reads it with `state.get` in the
   interpreted layer and the driver needs no `read_charge_percent`. A value that
   is unavailable, unknown, not a number or outside 0 to 100 gives the 50.0
   placeholder with `is_stub=True` and the marker `inverter_read_failed`, as for
   an unusable reading. `SOC_IS_STUB` is not consulted: a good reading from
   `SOC_ENTITY` is never a stub. A `SOC_ENTITY` that is not an entity id makes the
   driver unusable (`inverter_driver_unavailable`). A driver without
   `SOC_ENTITY` still needs `read_charge_percent`.
6. **Dry run.** With `inverter.dry_run: true` the boundary logs, at info level,
   one line per command that lists the planned service calls, and executes none
   of them. The command is recorded as sent, so de-duplication and resend behave
   exactly as in a live run. A `send`-style driver in a dry run is not called; a
   line saying it would have been is logged instead. The `logging` driver is
   unchanged. In a dry run the planner and the guard also assume that nothing was
   really commanded (no own-charge or discharge correction). Run a new driver in
   dry run on your hardware before the first live run.
7. **What was tested.** The service-call path has been exercised with test
   doubles for `service` and `state`, and with the pyscript interpreter in a test
   harness. It has never been run against real hardware or a real Home
   Assistant. `tests/driver_conformance.py` checks a plan-style driver's `plan`
   for every action (structure, speed, repeatability, no network) but never
   executes the calls; `tests/drivers/inverter_planstyle.py` is a generic
   example.

## Settings

`inverter.type` (default `logging`; missing, null or `none` mean `logging`;
otherwise lowercase letters, digits and underscore), `inverter.dry_run`
(default `false`; see above) and `inverter.resend_minutes`
(the fallback resend time for drivers that declare no `COMMAND_HOLD_MINUTES`; see
the README settings table).

A non-`logging` driver sends real commands and is your responsibility. The
[driver checklist](inverter-drivers.md#adding-an-inverter-driver) covers what the peak guard already handles (it adds the
commanded discharge back to the net offtake it reads, so a shave does not flip
on and off; it refreshes a `last_beat` attribute on
`pyscript.peak_guard_shaving` that the planner requires to be at most 3 guard
intervals old) and what the driver author must still check (the guard sends a
command only on change, so the inverter must hold a discharge command until the
next one and honour `idle` as defined above; the add-back assumes the commanded power is
delivered).

## Adding an inverter

Add one file. `inverter.py`, the planner and the guard do not change.
