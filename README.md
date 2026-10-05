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

   Edit it (see [Configure](#configure)). At minimum set `battery.capacity_kwh` and `alerts.address`.
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

## Configure

`battery_planner/user_config.example.yaml` lists every setting with comments. A bad configuration
is a startup error: it is logged and no decision is made.

### Settings reference

Every key in `user_config.yaml`. Keys you leave out use the default. Only `battery.capacity_kwh` and
`alerts.address` have no default and must be set.

| Key | Default | Unit | What it changes |
|---|---|---|---|
| `prices.consumption_multiplier` | `1.07` | factor | Buying price = market price x this + offset. Must be all-in (see below) |
| `prices.consumption_offset` | `0.007` | EUR/kWh | Added to the buying price. Put per-kWh network costs, taxes and VAT here |
| `prices.injection_multiplier` | `0.94` | factor | Selling price = market price x this + offset |
| `prices.injection_offset` | `-0.011` | EUR/kWh | Added to the selling price. Negative: injection can turn negative while the market price is still positive |
| `prices.entity` | `sensor.entso_prices_average_electricity_price` | entity id | Sensor that carries the price list |
| `prices.attribute` | `prices` | attribute name | Attribute of that sensor holding the `{time, price}` list |
| `battery.capacity_kwh` | **required** | kWh | Battery size the planner uses |
| `battery.reserve_percent` | `10.0` | % | Charge level the planner will not export to the grid below. It is a limit on exporting, not a target: the planner never buys power to keep the battery up to it, and peak shaving may use charge below it. The inverter's own minimum charge still applies |
| `battery.soc_sensor` | none | entity id | Sensor with the battery charge in percent (0 to 100). When set, the planner and the peak guard read the charge from it instead of the driver's placeholder. `unavailable`, `unknown`, a non-number or a value outside 0 to 100 counts as unreadable: the planner holds and marks `soc_unavailable` |
| `battery.power_sensor` | none | entity id | Sensor with the battery power, in W or kW (the unit attribute is read; any other unit counts as unreadable). Lets the planner measure the household draw (see [What the planner does](docs/how-it-works.md#what-the-planner-does)). Unreadable: the older estimate is used and records carry `battery_power_unavailable` |
| `battery.power_positive` | `discharge` | `discharge` or `charge` | Which direction is positive in `battery.power_sensor`. `discharge`: positive while the battery gives power (AlphaESS). `charge`: positive while it takes power |
| `battery.max_charge_sensor` | none | entity id | Sensor with the inverter's own maximum battery charge power, in W or kW (the unit attribute is read). When it reads above 0, the planner uses it as the charge limit, read again every cycle. Otherwise `battery.max_charge_kw` applies and records carry `battery_limits_fallback` |
| `battery.max_discharge_sensor` | none | entity id | The same for the maximum discharge power: the limit for exporting and for peak shaving |
| `battery.max_charge_kw` | `5.0` | kW | Highest charge power the planner proposes. Fallback when no `battery.max_charge_sensor` is set or it cannot be read |
| `battery.max_discharge_kw` | `5.0` | kW | Power used when exporting and the cap on peak shaving. Fallback when no `battery.max_discharge_sensor` is set or it cannot be read |
| `battery.round_trip_efficiency` | `0.90` | 0 to 1 | Share of stored energy you get back. A later price only counts at this fraction |
| `solar.forecast_entity` | `sensor.forecast_solar_estimate` | entity id | The REST sensor from `configuration.yaml` |
| `solar.forecast_attribute` | `watt_hours_period` | attribute name | Attribute holding the Wh per period |
| `solar.calibration_default` | `1.0` | factor (above 0, up to 2) | The forecast is multiplied by this until enough measured history exists. `0.8` counts on 80 % of the forecast. `1.0` changes nothing. See [Solar calibration](docs/history-and-reports.md#solar-calibration) |
| `solar.calibration_weeks` | `4` | weeks (whole number, 1 or more) | Weeks of measured history needed before measured ratios replace `solar.calibration_default`, and the number of weeks they are averaged over |
| `capacity_tariff.enabled` | `true` | true/false | `false` turns off all peak logic (the peak budget limit, peak shaving, the peak guard's shaving, the grid-charge cap) |
| `capacity_tariff.billing_floor_kw` | `2.5` | kW | Peaks at or below this cost nothing extra. Sets the lowest ceiling |
| `capacity_tariff.stay_under_percent` | `80` | % (above 0, up to 100) | Grid charging stays under this share of the ceiling |
| `capacity_tariff.guard_interval_seconds` | `30` | seconds | Minimum gap between peak guard runs. The guard's timer is fixed at 30, so values below 30 change nothing |
| `capacity_tariff.quarter_hour_average_mode` | `auto` | `auto`, `running`, `accumulating` | How the meter's quarter-hour average is read |
| `capacity_tariff.offtake_sensor` | `sensor.slimmelezer_power_consumed` | entity id | Netted total offtake |
| `capacity_tariff.quarter_hour_average_sensor` | `sensor.slimmelezer_huidig_kwartiervermogen` | entity id | Meter register 1-0:1.4.0 |
| `capacity_tariff.month_peak_sensor` | `sensor.slimmelezer_maandpiek` | entity id | Meter register 1-0:1.6.0 |
| `usage.history_weeks` | `4` | weeks | How many weeks are averaged into the usage profile |
| `usage.grouping` | `same_weekday` | `same_weekday`, `day_type` | `same_weekday` averages the same weekday; `day_type` pools weekdays and weekend days |
| `usage.recency_weighting` | `linear` | `linear`, `none` | `linear` weights newer weeks more (4 weeks: 4, 3, 2, 1). Changing it discards the cached profile |
| `history.enabled` | `true` | true/false | Records the energy history |
| `history.sensors.import`, `.export` | the SlimmeLezer tariff 1 and 2 counters | entity ids (list) | Cumulative kWh from and to the grid |
| `history.sensors.solar`, `.battery_charge`, `.battery_discharge`, `.load` | `[]` | entity ids (list) | Cumulative kWh counters. See [Energy history](docs/history-and-reports.md#energy-history) |
| `report.enabled` | `true` | true/false | Writes the [daily report](docs/history-and-reports.md#daily-report) |
| `sensors.enabled` | `true` | true/false | Publishes the planner state as Home Assistant [sensors](docs/alerts-and-sensors.md#sensors). `false` publishes nothing |
| `retention.keep_days` | `90` | days | Logs, history and reports older than this are deleted every night. At least 1, and at least 7 times the larger of `usage.history_weeks` and `solar.calibration_weeks`. See [Daily cleanup](docs/history-and-reports.md#daily-cleanup) |
| `timing.block_minutes` | `15` | minutes | Planning block length. Must divide 60. Keep 15 to match the price list |
| `timing.evaluation_interval_minutes` | `5` | minutes | How often the planner runs |
| `timing.forecast_retry_minutes` | `10` | minutes | Minimum gap between forced forecast refreshes after failures |
| `timing.solar_cache_stale_minutes` | `120` | minutes | Age after which the cached solar series is rebuilt |
| `timing.usage_cache_stale_minutes` | `2880` | minutes | Age after which the cached usage profile is rebuilt |
| `alerts.address` | **required** | e-mail address | Recipient passed to the notifier. Use the same address as `smtp_recipient` |
| `alerts.notify_service` | `battery_alert` | service name | Notifier `notify.<name>`; lowercase letters, digits, underscore |
| `alerts.realert_minutes` | `60` | minutes | Minimum gap between price-outage e-mails |
| `alerts.peak_enabled` | `true` | true/false | Peak notice e-mail (after) |
| `alerts.peak_warning_enabled` | `true` | true/false | Peak warning e-mail (before) |
| `alerts.peak_warning_min_interval_minutes` | `60` | minutes (whole number, 1 or more) | Minimum gap between peak warnings |
| `alerts.peak_warning_ticks` | `2` | evaluations (whole number, 1 or more) | Consecutive guard evaluations above the ceiling before a warning. Fewer is earlier and noisier |
| `inverter.type` | `logging` | driver name | Selects `pyscript/modules/inverter_<type>.py`. `none` means `logging` |
| `inverter.resend_minutes` | `15` | minutes (whole number, 0 or more) | Fallback for drivers that do not declare how long the inverter keeps a command (`COMMAND_HOLD_MINUTES`, see [Adding an inverter driver](docs/inverter-drivers.md#adding-an-inverter-driver)): the driver gets an unchanged command again only after this long. A changed command (other action, or power differing by 0.01 kW or more) goes out at once. `0` sends on every call. A driver that declares a hold is re-sent at 80 % of it and this setting is not used. The decision log line is written every cycle either way |
| `inverter.dry_run` | `false` | true/false | With a plan-style driver (one that defines `plan`, see [Adding an inverter driver](docs/inverter-drivers.md#adding-an-inverter-driver)): log the service calls it would make and execute none. The command is still remembered as sent. With a `send`-style driver the `send` call is skipped and logged. Run real hardware in dry run first. Nothing changes with the `logging` driver |
| `timezone` | `Europe/Brussels` | time zone name | Local day for log files, history and monthly peak e-mails |

### Rounding: which way to err

When a value is a rounded figure or you are unsure of it, pick the direction that makes the planner
more cautious and cheaper for you. Where no direction is safe, use the exact value.

| Setting | Round | Why |
|---|---|---|
| `prices.consumption_multiplier`, `prices.consumption_offset` | up | A higher buying price makes grid charging (for a negative price or for arbitrage) less attractive |
| `prices.injection_multiplier`, `prices.injection_offset` | down | A lower selling price makes arbitrage less attractive and stops exporting at a negative price sooner |
| `battery.capacity_kwh` | down | Use usable capacity, not nameplate. The planner then never counts on energy the battery does not have |
| `battery.reserve_percent` | up | The planner stops exporting to the grid earlier |
| `battery.max_charge_kw`, `battery.max_discharge_kw` | down | Commanded power never exceeds what the inverter does. Readings from `battery.max_charge_sensor` and `battery.max_discharge_sensor` are used as read |
| `battery.round_trip_efficiency` | down | Arbitrage needs a bigger price spread |
| `capacity_tariff.stay_under_percent` | lower is safer | Less grid charging near the peak ceiling, at the cost of fewer cheap charges |
| `forecast_solar_url` (kWp part) | down | A lower forecast means less counted-on solar |
| `solar.calibration_default` | down | A lower ratio counts on less solar. Use the share of the forecast you usually get, rounded down |
| `capacity_tariff.billing_floor_kw` | exact | Take the value from your bill. A wrong floor changes what the ceiling protects |
| `timing.*`, `capacity_tariff.guard_interval_seconds`, `alerts.*_minutes` | exact | Timing preferences with no safe direction |

All-in consumption price: the consumption price must be the price you actually pay per kWh, because the planner compares it with
selling prices and tests it against zero. Fold the per-kWh lines of your bill into the offset:

```
fees per kWh = (yearly EUR of each per-kWh bill line) / (yearly kWh taken from the grid)
all-in price = (market x m + o + fees) x (1 + VAT)
multiplier   = m x (1 + VAT)
offset       = (o + fees) x (1 + VAT)
```

`m` and `o` are your supplier's multiplier and offset. Do not include fixed charges or the capacity
tariff. Check first whether your supplier's coefficients already include VAT. Households are normally
not charged these fees or VAT on injection, so keep the injection coefficients as the supplier states
them.

Find the right ENTSO-e sensor in Developer Tools -> States: filter "entso" and open the sensor whose
attributes include a list of `{time, price}` entries (15-minute steps, EUR/kWh). Other ENTSO-e sensors,
such as the current market price, show a price but carry no list. A wrong name makes the planner halt
every cycle; the log line and the alert e-mail name the entity and attribute it tried. The forecast
sensor name and attribute have not been checked on other installs, so confirm them the same way.

Quarter-hour average mode: some meters report the quarter-hour average as energy so far divided by the
full 15 minutes (`accumulating`), others divide by elapsed time (`running`). One minute into a window
the two differ by a factor of 15. With `auto` the peak guard works out which applies by comparing
samples from several windows, and says so in its records. Pin the value once it is stable. What the
detection has seen is saved under `battery_planner/state/`, so a mode that was detected stays detected
after a restart. If you delete those files, the mode is "assumed" again until enough windows have been
seen.

The peak guard's `@state_trigger` names `sensor.slimmelezer_power_consumed` literally, because
decorator arguments are fixed at load time. If your netted offtake sensor has another name, the guard
still reads the configured sensor on every run but only runs on its 30-second tick. To run on every
sensor update, change the `@state_trigger(...)` argument in `pyscript/peak_guard.py`.

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
