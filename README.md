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
- With the `logging` driver the battery charge is a fixed 50 %. Every record carries
  `degraded=soc_stubbed`. A driver that reads the real charge removes the marker. The charge is real
  as soon as you set `battery.soc_sensor` to a sensor that holds it in percent (for example the AlphaESS
  sensor in [AlphaESS inverter, read-only](docs/alphaess.md#alphaess-inverter-read-only-home-assistant-modbus)):
  the planner and the peak guard then read that sensor and nothing is stubbed. If the sensor cannot
  be read, the planner does not guess: it holds (no charging from the grid, no exporting) and marks
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

If you install it, your battery behaves exactly as before. What you get is a log of decisions you can
compare with what the battery actually did.

## Does it fit your setup

| You need | Notes |
|---|---|
| Belgian digital meter with a P1 port | The capacity-tariff features read the meter's own demand registers |
| SlimmeLezer P1 reader | Stock firmware does not publish the demand registers. You must reflash it (see [step 3](#install)) |
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

## Install

Do these in order.

1. Install HACS, then install pyscript through HACS (Integrations). Restart Home Assistant.
2. Install the ENTSO-e integration and enter your API key. Day-ahead prices are published around
   13:00 local time.
3. Reflash the SlimmeLezer with the demand registers. In its ESPHome configuration, add this to the
   `dsmr` sensor block, then flash it (see the SlimmeLezer documentation):

   ```yaml
   - platform: dsmr
     active_energy_import_current_average_demand:
       name: "Huidig kwartiervermogen"
     active_energy_import_maximum_demand_running_month:
       name: "Maandpiek"
     active_energy_import_maximum_demand_last_13_months:
       name: "Gemiddelde maandpiek 13 maanden"
   ```

   Home Assistant then shows these entities (all in kW):

   | Entity | Register | Meaning |
   |---|---|---|
   | `sensor.slimmelezer_huidig_kwartiervermogen` | `1-0:1.4.0` | Average of the current quarter-hour |
   | `sensor.slimmelezer_maandpiek` | `1-0:1.6.0` | This month's peak, the level the planner defends |
   | `sensor.slimmelezer_gemiddelde_maandpiek_13_maanden` | - | Average of the last 13 months. Not read by the code |
   | `sensor.slimmelezer_power_consumed` | - | Netted total offtake |

   If your reader names these differently, set `capacity_tariff.offtake_sensor`,
   `capacity_tariff.quarter_hour_average_sensor` and `capacity_tariff.month_peak_sensor` in
   `user_config.yaml` (step 6).
4. Copy the `pyscript/` folder from this repository to `<ha-config>/pyscript/`. Copy
   `configuration.yaml` to `<ha-config>/configuration.yaml`. If you already have a
   `configuration.yaml`, merge the `pyscript:`, `rest:` and `notify:` blocks into it once, by hand or
   with `!include`. The shipped file also
   includes `automations.yaml`, `scripts.yaml` and `scenes.yaml`; Home Assistant needs those files to
   exist. You never edit the blocks afterwards: personal values come from `secrets.yaml` and
   `user_config.yaml`. Loading the blocks as a Home Assistant package or from split `!include` files
   is untested.
5. Create `<ha-config>/secrets.yaml` from `secrets.example.yaml` (or add the keys to your existing
   one):
   - `forecast_solar_url`: `https://api.forecast.solar/estimate/<lat>/<lon>/<declination>/<azimuth>/<kwp>`.
     Azimuth 0 is south and negative is east, so `-10` is ten degrees east of south. This is the
     easiest value to get backwards. The roof lives only in this URL; `user_config.yaml` has no roof
     settings. After you change the URL, the planner picks up the new forecast at the next cycle once
     the sensor has refreshed (hourly, or restart Home Assistant). It serves the previous forecast
     only while the sensor is unavailable, and for at most `timing.solar_cache_stale_minutes`
     (default 120). A sensor that stays available but stops refreshing is treated the same way:
     once its data is older than that limit the planner ignores it.
   - `smtp_sender`, `smtp_password`, `smtp_recipient`: the sending account, its app password (not
     the normal password; Gmail needs 2-step verification first) and the address that receives alerts.
   - `smtp_server`, `smtp_port`, `smtp_encryption` (`starttls`, `tls` or `none`; ports 587, 465, 25):
     keep exactly one provider preset uncommented. The presets for Gmail, Outlook / Microsoft 365,
     Yahoo and iCloud are from the providers' public documentation and are not tested against live
     accounts. Outlook may not work at all, because many Microsoft accounts have basic-auth SMTP
     disabled.
6. Create the planner config:

   ```bash
   mkdir -p <ha-config>/battery_planner
   cp battery_planner/user_config.example.yaml <ha-config>/battery_planner/user_config.yaml
   ```

   Edit it (see [Configure](docs/configuration.md#configure)). At minimum set `battery.capacity_kwh` and `alerts.address`.
7. Restart Home Assistant. Core modules under `pyscript/modules/` are loaded natively and are not
   hot-reloaded, so any change to them needs a restart. A notifier is also only created at startup.
8. Test the notifier: Developer Tools -> Actions, choose `notify.battery_alert`, give it a message
   and run it. The mail should arrive within a minute. If Home Assistant reports the SMTP YAML
   platform as unsupported in your version (it may have moved to the UI), create the notifier in the
   UI and set its service name in `alerts.notify_service`.

Files on the Home Assistant side:

```
<ha-config>/
  configuration.yaml
  secrets.yaml                  yours, do not publish
  pyscript/                     copied from this repository
  battery_planner/
    user_config.yaml            yours, created in step 6
    decisions-YYYY-MM-DD.log    generated, one file per local day
    cache/                      generated, see docs/cache-files.md
    state/                      generated, survives restarts (see "What a restart does")
      peak_alert.json           last month-peak e-mail sent
      halt.json                 price-outage state while prices are missing
      average_mode_planner.json, average_mode_guard.json
                                what the quarter-hour average mode detection has seen
      peak_warning.json         when the last peak warning was sent
      last_command.json         the last command the inverter driver accepted
    history/                    generated, energy history
```

Do not copy `tests/` to Home Assistant.

Configuration has moved to [docs/configuration.md](docs/configuration.md).

Checking that it works, and troubleshooting, have moved to [docs/troubleshooting.md](docs/troubleshooting.md).

How the planner decides, the peak guard and what a restart does have moved to [docs/how-it-works.md](docs/how-it-works.md).

The alert e-mails and the Home Assistant sensors have moved to [docs/alerts-and-sensors.md](docs/alerts-and-sensors.md).

Energy history, solar calibration, the daily report and the daily cleanup have moved to [docs/history-and-reports.md](docs/history-and-reports.md).

## Updating

You edit two files, and updating never touches them: `battery_planner/user_config.yaml` and Home
Assistant's `secrets.yaml`.

1. Copy the new `pyscript/` folder over `<ha-config>/pyscript/`.
2. Copy the new `configuration.yaml`, or merge its changed blocks into yours.
3. Never overwrite `user_config.yaml` or `secrets.yaml`. If a release adds keys, compare
   `user_config.example.yaml` and `secrets.example.yaml` with your files and add what is missing.
4. Restart Home Assistant.

Upgrade note: older versions named the notifier `gmail_alert`. If you set
`alerts.notify_service: gmail_alert`, either keep that name in your notifier or change the setting to
`battery_alert`.

Adding an inverter driver has moved to [docs/inverter-drivers.md](docs/inverter-drivers.md).

The read-only AlphaESS example has moved to [docs/alphaess.md](docs/alphaess.md).

The known gaps have moved to [docs/known-gaps.md](docs/known-gaps.md).

## Credits and related projects

- [ramonvanraaij/ha-alphaess-modbus](https://github.com/ramonvanraaij/ha-alphaess-modbus): a Home
  Assistant Modbus integration for AlphaESS inverters by Rámon van Raaij (BSD 3-Clause for his own
  contributions, see its LICENSE.md). The sensor definitions in the read-only example above follow its
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
