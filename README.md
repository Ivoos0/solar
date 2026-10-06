# Price-aware battery planner for Home Assistant (Flanders)

A Home Assistant [pyscript](https://github.com/custom-components/pyscript) project. Every five
minutes it decides what a home battery should do (charge, discharge, export or idle), using dynamic
electricity prices (ENTSO-e), a solar forecast (forecast.solar), your expected household usage and
the Flemish capacity tariff (capaciteitstarief), which bills your monthly quarter-hour peaks.

Source: <https://github.com/Ivoos0/solar>

## What it does today

- It decides and writes the decision to a log file, one line per cycle. It also publishes its state
  as a few Home Assistant sensors you can put on a dashboard (see [Sensors](docs/alerts-and-sensors.md#sensors)). There is no
  dashboard of its own.
- It does not control your inverter. The default driver, `logging`, sends nothing. To act on the
  decisions you add a driver for your inverter (see [Adding an inverter driver](docs/inverter-drivers.md#adding-an-inverter-driver)).
  None is shipped. When one is enabled, the driver is sent a command only when it changes, and again
  after about 80 % of the time the driver says the inverter keeps a command (or, when it does not say,
  after `inverter.resend_minutes`, default 15), not on every five-minute cycle. `idle` is sent once
  after a forced mode and not repeated while idle lasts. The log line is still written every cycle.
- The battery charge is always a real reading. Set `battery.soc_sensor` to a sensor that holds it in
  percent (for example the AlphaESS sensor in
  [AlphaESS inverter, read-only](docs/alphaess.md#alphaess-inverter-read-only-home-assistant-modbus)); it is
  required with the `logging` driver, and the planner refuses to start without it. A real driver may read
  the charge itself instead. There is no placeholder value: if the reading is missing, frozen or not a number
  from 0 to 100, the planner does not guess. It holds (no charging from the grid, no exporting) and marks
  the record `soc_unavailable`. Peak shaving still works.
- It can read an AlphaESS inverter through Home Assistant's modbus integration, read-only: the real
  battery charge, the battery power and the energy counters (see
  [AlphaESS inverter, read-only](docs/alphaess.md#alphaess-inverter-read-only-home-assistant-modbus)). Nothing is
  written to the inverter, so its default behaviour is unchanged.
- Household usage history comes from energy counters you configure (see [Energy history](docs/history-and-reports.md#energy-history)).
  Until a `load` counter (or `solar` plus both battery counters) is configured, records carry
  `usage_history_unavailable` and the planner holds: it does nothing price-driven (no charging from
  the grid, no exporting) and logs idle with the reason "no usage history:
  planner holds". Only peak protection still acts, because it reads the live grid reading and does
  not need history. While the planner holds, the inverter's own behaviour applies.

- The main known gaps (the full list is in [Known gaps](docs/known-gaps.md)):
  - Peak protection is reactive. The projection covers battery charge, not grid offtake, so the planner
    does not hold charge back for a foreseeable evening peak.
  - The peak guard adds the discharge it commands back to the offtake reading, assuming the inverter
    delivers that power. This has only been tested with simulated meter readings, never with a real
    inverter (see the [driver checklist](docs/inverter-drivers.md#adding-an-inverter-driver)).
  - The alert e-mails and the entity names on installs other than the original one have not been tested
    against a live Home Assistant.
  - The solar calibration has only been tested with generated history.

If you install it, your battery behaves exactly as before. What you get is a log of decisions you can
compare with what the battery actually did.

## Does it fit your setup

| You need | Notes |
|---|---|
| Belgian digital meter with a P1 port | The capacity-tariff features read the meter's own demand registers |
| SlimmeLezer P1 reader | Stock firmware does not publish the demand registers. You must reflash it (see [step 3](docs/install.md#install-step-3)) |
| Home Assistant with HACS and pyscript | Container installs work, no add-ons needed |
| ENTSO-e API key | Free. Used by the Home Assistant ENTSO-e integration for dynamic prices |
| Solar array | One roof plane. The forecast logic assumes it |
| Home battery with a hybrid inverter | The planner decides what the battery should do; acting on it needs a driver |
| A working SMTP account | For the alert e-mails. Optional, but the price-outage alert needs it |

The capacity-tariff logic is specific to the Flemish tariff: billing on the average of the monthly
quarter-hour peaks, with a 2.5 kW floor, on grid offtake only. Elsewhere the price logic may still be
useful; set `capacity_tariff.enabled: false`.

The planner reads the meter's netted total offtake (`sensor.slimmelezer_power_consumed`) and never sums
per-phase sensors, because on a three-phase connection one phase can export while another imports. If
your reader only offers per-phase sensors, the capacity features will read the wrong value.

Not supported: control of anything but the battery (EV charger, heat pump, boiler), several solar
planes, paid forecast tiers, solar curtailment.

## Quick start

The full steps are in [Install](docs/install.md#install). In short:

1. Install HACS and pyscript, then the ENTSO-e integration with your API key ([steps 1 and 2](docs/install.md#install-step-1)).
2. Reflash the SlimmeLezer so it publishes the demand registers ([step 3](docs/install.md#install-step-3)).
3. Copy `pyscript/` and `configuration.yaml` to your Home Assistant config folder ([step 4](docs/install.md#install-step-4)).
4. Create `secrets.yaml` from `secrets.example.yaml` ([step 5](docs/install.md#install-step-5)).
5. Create `user_config.yaml` and set `battery.capacity_kwh` and `alerts.address` ([step 6](docs/install.md#install-step-6), [Configure](docs/configuration.md#configure)).
6. Restart Home Assistant and test the notifier ([steps 7 and 8](docs/install.md#install-step-7)).
7. Look for the first decision record in the log ([Check that it works](docs/troubleshooting.md#check-that-it-works)).

## Documentation

- [Install and update](docs/install.md): how to install the planner in Home Assistant and how to update it.
- [Configure](docs/configuration.md): every setting in `user_config.yaml`, rounding guidance, and notes on prices, the meter and the peak guard.
- [What the planner does](docs/how-it-works.md): how the planner chooses an action, what the peak guard does and what survives a restart.
- [Alert e-mails and sensors](docs/alerts-and-sensors.md): the e-mails the planner sends and the Home Assistant entities it publishes.
- [Energy history, reports and cleanup](docs/history-and-reports.md): energy history, solar calibration, the daily report and the daily cleanup.
- [Check that it works and troubleshooting](docs/troubleshooting.md): how to see that the planner is running, read a decision record and fix common problems.
- [Decision log format](docs/decision-log.md): the fields of a log line and the labels for rules and actions.
- [Adding an inverter driver](docs/inverter-drivers.md): how to add a driver so the decisions reach your inverter.
- [Inverter boundary](docs/inverter-boundary.md): the exact interface and rules between the planner and a driver.
- [AlphaESS, read-only](docs/alphaess.md): reading an AlphaESS inverter through Home Assistant's modbus integration.
- [Cache files](docs/cache-files.md): the cached series and when they are rebuilt.
- [Known gaps](docs/known-gaps.md): what the planner does not do yet, and what has not been tested.

## Credits and related projects

- [ramonvanraaij/ha-alphaess-modbus](https://github.com/ramonvanraaij/ha-alphaess-modbus): a Home
  Assistant Modbus integration for AlphaESS inverters by Rámon van Raaij (BSD 3-Clause for his own
  contributions, see its LICENSE.md). The sensor definitions in the [read-only AlphaESS example](docs/alphaess.md) follow its
  register definitions. Controlling the inverter is not implemented here; it is planned through that
  project's helper entities.
- [Projects @ Hillview Lodge](https://projects.hillviewlodge.ie/alphaess/): Axel Koegler's AlphaESS
  Modbus register coverage and dispatch automations (2023 to 2025), named as an upstream source there.
- [umrath/homeassistant_alphaess_modbus_tcp](https://github.com/umrath/homeassistant_alphaess_modbus_tcp):
  a 2024 fork with target-charge and dispatch branches, also named as an upstream source there.
- [snitzelweck92/homeassistant_alphaess_modbus_tcp](https://github.com/snitzelweck92/homeassistant_alphaess_modbus_tcp):
  the 2023 original skeleton, also named as an upstream source there.

These are separate projects. This one is not affiliated with them and they do not endorse it.

## Licence

MIT, see [LICENSE](LICENSE). If you build on this, a reference to the original repository,
<https://github.com/Ivoos0/solar>, is appreciated.
