# Adding an inverter driver

How to add an inverter driver so the planner's decisions reach your inverter. Back to the [README](../README.md).

**Contents**

- [Drivers that switch Home Assistant helpers (plan-style)](#drivers-that-switch-home-assistant-helpers-plan-style)

A driver is one file, `pyscript/modules/inverter_<name>.py`, selected with `inverter.type: <name>`
(lowercase letters, digits, underscore). The exact interface and rules are in [inverter-boundary.md](inverter-boundary.md). Copy `inverter_logging.py` and replace the bodies. Skeleton
for a hypothetical `alphaess`:

```python
# pyscript/modules/inverter_alphaess.py
COMMAND_HOLD_MINUTES = None    # optional. Minutes the inverter keeps a forced command without a
                               # refresh, as a positive number. None or left out = unknown


def send(action, target_power_kw):
    """action: "charge" (forced charge from the grid) | "discharge" | "export" | "idle".
    Power in kW, >= 0, 0.0 when idle. Return True when the inverter accepted it. Return False or
    raise when it did not.

    "idle" means: cancel every forced mode this project set (forced grid charge, forced export,
    forced discharge) and return the inverter to its default behaviour. It is not "do nothing"."""
    ...  # talk to your inverter here


def read_charge_percent():
    """Battery charge, 0 to 100. Anything else counts as a failed reading."""
    ...
```

Check the file before you enable it:

```bash
python3 -m pytest tests/test_driver_conformance.py --driver pyscript/modules/inverter_alphaess.py
```

It checks the file name, that importing it touches no network, that `send` returns `True` for
every action (`charge`, `discharge`, `export`, `idle`), also when repeated and within a few
seconds, and that `read_charge_percent` returns a number from 0 to 100. It really calls your
driver, so run it with the inverter disconnected or your connection mocked. Every
`inverter_*.py` in `pyscript/modules/` is checked as well. Passing does not prove the driver works
on your hardware.

A driver that defines `plan` instead of `send` is checked differently: `plan` is called for every
action and must return a valid, non-empty list of service calls quickly, the same every time and
without network access (see the section below).

Then set `inverter.type: alphaess`, restart Home Assistant and watch the log. Any driver other than
`logging` sends real commands to a real inverter, and nothing in this project is tested against
hardware. You are responsible for the driver you enable.


## Drivers that switch Home Assistant helpers (plan-style)

Some inverter integrations have no Home Assistant service to call. You control them by switching
helpers (`input_boolean` switches, `input_number` sliders) that the integration watches. Drivers are
loaded as plain Python and cannot call Home Assistant, so such a driver does not send anything
itself. It defines `plan(action, target_power_kw)` instead of `send`: a pure function that returns
the service calls to make, in order. The framework validates the list and runs the calls with
Home Assistant's `service.call`, one after the other.

Everything below uses made-up helper names. Replace them with the helpers of your own integration.
This is an example, not a working driver for any product:

```python
# pyscript/modules/inverter_myinverter.py   (EXAMPLE with placeholder entities)
SOC_ENTITY = "sensor.my_battery_soc"   # battery charge in percent, read by the framework
COMMAND_HOLD_MINUTES = 10              # optional; this made-up inverter drops a command after ~10 min


def plan(action, target_power_kw):
    """Return the service calls for this command. No I/O here: just data."""
    if action == "idle":
        # release every forced mode: the inverter goes back to its default behaviour
        return [
            {"domain": "input_boolean", "service": "turn_off",
             "data": {"entity_id": "input_boolean.my_force_charge"}},
            {"domain": "input_boolean", "service": "turn_off",
             "data": {"entity_id": "input_boolean.my_force_discharge"}},
        ]
    helper = {"charge": "input_boolean.my_force_charge",
              "discharge": "input_boolean.my_force_discharge",
              "export": "input_boolean.my_force_discharge"}[action]
    return [
        {"domain": "input_number", "service": "set_value",
         "data": {"entity_id": "input_number.my_power", "value": round(target_power_kw, 2)}},
        {"domain": "input_boolean", "service": "turn_on", "data": {"entity_id": helper}},
    ]
```

What the framework does with it:

- `plan` runs in the same worker thread, with the same 10 second limit, as `send`. If it raises,
  times out or returns something invalid, nothing is executed and the command counts as failed.
- The list may hold at most 12 calls. Each call is `{"domain", "service", "data"}`: `domain` and
  `service` are lowercase letters, digits and underscore; `data` is a dictionary with text keys
  whose values are text, whole or decimal numbers, true/false, or lists of those. The keys
  `blocking`, `return_response` and `limit` are not allowed. Anything else is rejected and logged.
- The calls run in order. If one raises (for example the helper does not exist), the rest are
  skipped, the error is logged (at most once per 30 minutes for the same cause) and the command
  counts as failed: it is not remembered as sent, so the next cycle tries it again.
- The command is only planned and run when the framework decides to send it (a changed command, or
  the resend time), exactly as for `send`. `idle` runs once after a forced mode.
- `SOC_ENTITY` is optional. When it is set, the framework reads that sensor for the battery charge
  and `read_charge_percent` is not needed. A value that is unavailable, not a number or outside 0 to
  100 gives no reading and `degraded=inverter_read_failed`; the planner then holds. A driver may also
  offer no charge reading at all and rely on `battery.soc_sensor`.

**Run it with `inverter.dry_run: true` first.** In a dry run the framework logs the service calls it
would make (one line per command, at info level) and executes none of them. The command is still
remembered as sent, so you see exactly when it would be repeated. Watch the Home Assistant log for a
day, compare it with what the planner decided, and only then set `dry_run: false`. A driver for real
hardware is your responsibility.

The service-call path was exercised only with test doubles and with the pyscript interpreter in a
test harness, never against real hardware or a real Home Assistant.

The full rules the framework applies (de-duplication, resend, validation) are in
[Inverter boundary](inverter-boundary.md).

What the framework does for you:

- It writes the decision line before calling your driver. A driver that raises, times out, returns
  `False` or is missing never changes that line and never crashes the planner or the guard.
- It runs your code in an executor thread, so blocking network calls are fine.
- It gives each call 10 seconds, then treats it as failed and logs it.
- It calls `send` only when the command changed or the resend time has passed since the
  last accepted send (80 % of your `COMMAND_HOLD_MINUTES`, else `inverter.resend_minutes`) (remembered in `state/last_command.json`, also across restarts and shared by
  the planner and the guard). A failed send is not remembered. `idle` is the exception: it is sent
  once when the last command was not `idle` (or none is on record) and is not repeated while idle
  lasts. The `logging` driver is not affected.
- If the driver file is missing, the HA log shows an error every cycle, nothing is sent and decisions
  carry `degraded=inverter_driver_unavailable`. Adding the file needs no restart. After editing an
  existing driver, restart Home Assistant.

A driver is loaded under a private name. It can import the standard library and installed packages
but not the other files in `pyscript/modules/`, and it cannot use pyscript names such as `log` or
`state`.

Before you enable a real driver:

- [ ] Serialize access to your inverter bus, for example with a `threading.Lock`. The planner (every
      cycle, including `idle`) and the peak guard (when its state changes) can call `send` at the
      same time from different threads.
- [ ] Set your own network timeouts. After 10 seconds the call is abandoned, but its thread keeps
      running until it returns.
- [ ] Make commands idempotent. `send("discharge", 2.5)` may arrive again unchanged. If your
      inverter drops a forced command after some time without a refresh, declare that time in the
      driver (`COMMAND_HOLD_MINUTES = 10`, a positive number of minutes) and the command is sent
      again at 80 % of it. If you do not know it, leave the line out: the setting
      `inverter.resend_minutes` (default 15) applies instead; lower it if the inverter needs a
      faster refresh, `0` sends on every planner cycle and every guard call.
- [ ] With a plan-style driver, keep `plan` pure (no network, no files, no waiting), list every helper
      that must be switched back off for `idle`, and run with `inverter.dry_run: true` before the
      first live run. Sliders and switches your integration owns are yours to check: nothing here has
      been tried against real hardware.
- [ ] Make `send("idle", 0)` a real release: it must cancel every forced mode the planner or the
      guard set (forced grid charge, forced export, forced discharge) and put the inverter back on
      its default behaviour. The planner sends it once when it stops a forced mode, and not again
      while it stays idle. The conformance check cannot test this without your hardware.
- [ ] Decide what the inverter does when commands stop. A failed `send` is logged and is not counted
      as sent, so the next decision tries it again.
- [ ] Know what the peak guard already handles, and what is left to you. Handled: the guard reads
      net grid offtake, which its own discharge lowers, so it adds the power it is commanding back
      before projecting (see [Peak guard](how-it-works.md#peak-guard)); the command therefore stays steady instead
      of switching on and off every 30 seconds. Still yours: the guard sends a command only when it
      changes (the framework repeats it at 80 % of your `COMMAND_HOLD_MINUTES`, else after
      `inverter.resend_minutes`, but only when the planner
      or the guard calls `send` again), so make sure the inverter holds a discharge command for as
      long as it takes to hear again, and that `send("idle", 0)` really releases it. The add-back
      assumes the commanded power is what the inverter delivers; a driver that clips it (for example
      at a lower inverter limit) should set `battery.max_discharge_kw` (or a `battery.max_discharge_sensor`) to that limit.
- [ ] Run with `logging` first and compare a week of decisions with what the battery should have done.
- [ ] Return the real charge from `read_charge_percent` (or declare `SOC_ENTITY`), or leave it out and set `battery.soc_sensor`.

The decision logic in `pyscript/modules/` contains no Home Assistant code and is tested without it:

```bash
python3.12 -m pip install pytest
python3.12 -m pytest tests/ -v
```

If you fork, do not publish `secrets.yaml` or `battery_planner/user_config.yaml`, and check with
`git check-ignore -v` that your `.gitignore` covers them.
