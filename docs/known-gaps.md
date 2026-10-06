# Known gaps

What the planner does not do yet, and what has not been tested. Back to the [README](../README.md).

- No inverter driver is shipped except `logging`, so nothing controls a battery.
- Until a household load source is configured and `usage.min_history_days` days are recorded, the planner
  only peak-shaves and otherwise idles: no grid charging, no solar storage, no exporting. The inverter's
  own behaviour applies meanwhile.
- The planner cannot curtail solar. At a negative injection price with a full battery the inverter's
  default exports the surplus and you pay for it.
- There is no minimum hold time or hysteresis: when the inputs move a little, a decision can change from one
  cycle to the next, and each change is a new command once a driver is enabled.
- A command sent to the inverter is not read back: the planner does not check that the inverter did what
  it was told.
- Peak protection is reactive. The projection covers battery charge, not grid offtake, so the planner
  does not hold charge back for a foreseeable evening peak.
- The peak guard adds the discharge it commands back to the offtake reading, assuming the inverter
  delivers that power. This has only been tested with simulated meter readings, never with a real
  inverter (see the [driver checklist](inverter-drivers.md#adding-an-inverter-driver)).
- Single solar plane, free forecast tier only, Flemish capacity tariff only, battery only.
- The alert e-mails and the entity names on installs other than the original one have not been tested
  against a live Home Assistant. Send a test mail ([install](install.md#install) step 8) and check the entity names as
  described in [Install](install.md#install) and [Sensors](alerts-and-sensors.md#sensors).
- The solar calibration (see [Solar calibration](history-and-reports.md#solar-calibration)) has only been tested with
  generated history. It needs a `solar` energy counter, which is not configured by default, and then
  about `solar.calibration_weeks` weeks of recording before it measures anything. The recorded prices,
  battery and split fields are for later analysis.

To adapt the planner: provider prices are `prices.*` in `user_config.yaml`; the capacity-tariff logic
is in `pyscript/modules/capacity.py`; usage history is read by `read_usage_history` in
`pyscript/battery_planner.py`.
