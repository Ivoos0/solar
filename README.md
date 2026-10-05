# Price-aware battery planner for Home Assistant (Flanders)

A Home Assistant [pyscript](https://github.com/custom-components/pyscript) project. Every five
minutes it decides what a home battery should do (charge, discharge, export or idle), using dynamic
electricity prices (ENTSO-e), a solar forecast (forecast.solar), your expected household usage and
the Flemish capacity tariff (capaciteitstarief), which bills your monthly quarter-hour peaks.

Source: <https://github.com/Ivoos0/solar>

## What it does today

- It decides and writes the decision to a log file, one line per cycle. It also publishes its state
  as a few Home Assistant sensors you can put on a dashboard (see [Sensors](#sensors)). There is no
  dashboard of its own.
- It does not control your inverter. The default driver, `logging`, sends nothing. To act on the
  decisions you add a driver for your inverter (see [Adding an inverter driver](#adding-an-inverter-driver)).
  None is shipped. When one is enabled, the driver is sent a command only when it changes, and again
  after about 80 % of the time the driver says the inverter keeps a command (or, when it does not say,
  after `inverter.resend_minutes`, default 15), not on every five-minute cycle. `idle` is sent once
  after a forced mode and not repeated while idle lasts. The log line is still written every cycle.
- With the `logging` driver the battery charge is a fixed 50 %. Every record carries
  `degraded=soc_stubbed`. A driver that reads the real charge removes the marker. The charge is real
  as soon as you set `battery.soc_sensor` to a sensor that holds it in percent (for example the AlphaESS
  sensor in [AlphaESS inverter, read-only](#alphaess-inverter-read-only-home-assistant-modbus)):
  the planner and the peak guard then read that sensor and nothing is stubbed. If the sensor cannot
  be read, the planner does not guess: it holds (no charging from the grid, no exporting) and marks
  the record `soc_unavailable`. Peak shaving still works.
- It can read an AlphaESS inverter through Home Assistant's modbus integration, read-only: the real
  battery charge, the battery power and the energy counters (see
  [AlphaESS inverter, read-only](#alphaess-inverter-read-only-home-assistant-modbus)). Nothing is
  written to the inverter, so its default behaviour is unchanged.
- Household usage history comes from energy counters you configure (see [Energy history](#energy-history)).
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
   is untested. The file no longer contains the price and cost helper sensors an earlier version
   had; if you used them, keep them in your own configuration. `examples/cost_simulation.yaml` is an
   optional example of such helpers (its header explains how to include it); the planner does not
   need it.
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
| `battery.power_sensor` | none | entity id | Sensor with the battery power, in W or kW (the unit attribute is read; any other unit counts as unreadable). Lets the planner measure the household draw (see [What the planner does](#what-the-planner-does)). Unreadable: the older estimate is used and records carry `battery_power_unavailable` |
| `battery.power_positive` | `discharge` | `discharge` or `charge` | Which direction is positive in `battery.power_sensor`. `discharge`: positive while the battery gives power (AlphaESS). `charge`: positive while it takes power |
| `battery.max_charge_sensor` | none | entity id | Sensor with the inverter's own maximum battery charge power, in W or kW (the unit attribute is read). When it reads above 0, the planner uses it as the charge limit, read again every cycle. Otherwise `battery.max_charge_kw` applies and records carry `battery_limits_fallback` |
| `battery.max_discharge_sensor` | none | entity id | The same for the maximum discharge power: the limit for exporting and for peak shaving |
| `battery.max_charge_kw` | `5.0` | kW | Highest charge power the planner proposes. Fallback when no `battery.max_charge_sensor` is set or it cannot be read |
| `battery.max_discharge_kw` | `5.0` | kW | Power used when exporting and the cap on peak shaving. Fallback when no `battery.max_discharge_sensor` is set or it cannot be read |
| `battery.round_trip_efficiency` | `0.90` | 0 to 1 | Share of stored energy you get back. A later price only counts at this fraction |
| `solar.forecast_entity` | `sensor.forecast_solar_estimate` | entity id | The REST sensor from `configuration.yaml` |
| `solar.forecast_attribute` | `watt_hours_period` | attribute name | Attribute holding the Wh per period |
| `solar.calibration_default` | `1.0` | factor (above 0, up to 2) | The forecast is multiplied by this until enough measured history exists. `0.8` counts on 80 % of the forecast. `1.0` changes nothing. See [Solar calibration](#solar-calibration) |
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
| `history.sensors.solar`, `.battery_charge`, `.battery_discharge`, `.load` | `[]` | entity ids (list) | Cumulative kWh counters. See [Energy history](#energy-history) |
| `report.enabled` | `true` | true/false | Writes the [daily report](#daily-report) |
| `sensors.enabled` | `true` | true/false | Publishes the planner state as Home Assistant [sensors](#sensors). `false` publishes nothing |
| `retention.keep_days` | `90` | days | Logs, history and reports older than this are deleted every night. At least 1, and at least 7 times the larger of `usage.history_weeks` and `solar.calibration_weeks`. See [Daily cleanup](#daily-cleanup) |
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
| `inverter.resend_minutes` | `15` | minutes (whole number, 0 or more) | Fallback for drivers that do not declare how long the inverter keeps a command (`COMMAND_HOLD_MINUTES`, see [Adding an inverter driver](#adding-an-inverter-driver)): the driver gets an unchanged command again only after this long. A changed command (other action, or power differing by 0.01 kW or more) goes out at once. `0` sends on every call. A driver that declares a hold is re-sent at 80 % of it and this setting is not used. The decision log line is written every cycle either way |
| `inverter.dry_run` | `false` | true/false | With a plan-style driver (one that defines `plan`, see [Adding an inverter driver](#adding-an-inverter-driver)): log the service calls it would make and execute none. The command is still remembered as sent. With a `send`-style driver the `send` call is skipped and logged. Run real hardware in dry run first. Nothing changes with the `logging` driver |
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

## Check that it works

A record appears in today's log within five minutes:

```bash
tail -f <ha-config>/battery_planner/decisions-$(date +%F).log
```

Each file holds one local calendar day, in your configured `timezone`. The planner, the peak guard
and the HALT / RECOVERED / SKIP lines all write to it. Old files are deleted automatically, see
[Daily cleanup](#daily-cleanup).

A normal record is one line of `|`-separated fields: timestamp, `action`, `power`, `soc`, `cons` and
`inj` (prices), forecast and usage remaining, `saturation`, `spill`, `breach`, `end_soc`, `took`,
`avg`, `ceiling`, `budget`, `vetoes`, `selector`, `why` (the reason in words; `vetoes` and `selector` use the labels explained under [Labels in the decision log](#labels-in-the-decision-log)), `degraded` and
`source` (`planner` or `guard`). Fields that do not apply show `n/a`.

Markers you can expect in `degraded=` on a fresh install:

| Marker | Meaning |
|---|---|
| `soc_stubbed` | The battery charge is the 50 % placeholder. Normal until a driver or `battery.soc_sensor` supplies the real charge |
| `battery_limits_fallback` | `battery.max_charge_sensor` or `battery.max_discharge_sensor` is set but cannot be read (or reads 0 or less). The numeric `battery.max_charge_kw` / `max_discharge_kw` apply for that direction |
| `battery_power_unavailable` | `battery.power_sensor` is set but cannot be read (or its unit is not W or kW). The household draw is estimated from the meter alone, which can undercount while the battery covers the house |
| `soc_unavailable` | `battery.soc_sensor` is set but cannot be read. The planner holds: no charging from the grid and no exporting until the sensor is back. Peak shaving is not affected |
| `usage_history_unavailable` | No household usage history yet. Normal until a load source is configured; the planner only peak-shaves and otherwise idles meanwhile |
| `usage_samples=N` | The usage profile rests on fewer than `usage.history_weeks` x 7 days of history (N days). Disappears as history builds up |
| `cache_age_solar=...`, `cache_age_usage=...` | A cached series was used, with its age |
| `forecast_age=...` | The forecast was used, but the sensor last refreshed 75 minutes or more ago (a normal hourly refresh keeps it under that) |
| `solar_ratio=0.83` | The forecast for the current or next block was multiplied by this ratio, measured from your own history (see [Solar calibration](#solar-calibration)). Absent when the ratio is 1.00 |
| `solar_ratio_configured=0.80` | The same, but the ratio is `solar.calibration_default` because there is not enough measured history yet. Absent when it is 1.00 |
| `solar_zero_fallback` | The forecast was unavailable (or older than `timing.solar_cache_stale_minutes`) and no usable cached copy exists, so solar was treated as zero. The planner holds (no grid charging, no export) until a forecast is back, retries, and does not halt |
| `grid_sensors_unavailable` | Capacity logic is on but the grid sensors are unreadable. Grid charging is suppressed |
| `inverter_driver_unavailable`, `inverter_read_failed` | The configured driver is missing or its charge reading failed |

When the peak guard is shaving a peak it writes its own records: when a shave starts, changes
materially, is blocked or stops, not once per 30-second tick.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No records at all | pyscript not loaded, or the `pyscript:` block is missing | Add the block from `configuration.yaml`, restart Home Assistant |
| `ImportError` in the HA log | `allow_all_imports: true` is missing | Add it to the `pyscript:` block |
| HA log warns about blocking I/O | A file operation ran on the event loop | It must use `@pyscript_executor` |
| Forecast always zero, records show `solar_zero_fallback` and the planner idles with "hold: no solar forecast" | The REST sensor is failing | Check `forecast_solar_url` and the forecast.solar free-tier rate limit. The planner does not buy or export power until a forecast is back |
| `forecast_age=...` on records, or a HA log warning "forecast sensor ... has not refreshed for ..." | The forecast sensor is not refreshing hourly. Past `timing.solar_cache_stale_minutes` the planner ignores its data and uses the cached series, or zero solar | Check the sensor's last updated time in Developer Tools -> States, the forecast.solar rate limit, and that `scan_interval` in `configuration.yaml` is still 3600 |
| Constant HALT with "attribute prices missing or empty on ..." | `prices.entity` or `prices.attribute` does not match your install | Open the entity in Developer Tools -> States and copy the sensor and attribute name into `user_config.yaml` |
| `peak_guard: ... not refreshed since window ...` | The quarter-hour average sensor has not published since this window began | At INFO level with a low value this is normal for a quiet house. At WARNING level with a high average and an old timestamp, the meter or its link is stuck and the guard is doing nothing |
| Config edits have no effect | You edited the repository copy | Edit `<ha-config>/battery_planner/user_config.yaml` and restart |
| Core code change has no effect | Core modules are not hot-reloaded | Restart Home Assistant |
| `SKIP` line in the log | A cycle was due while the previous one was still running | Occasional lines are harmless. If they repeat, the HA log shows a warning (an error when the earlier cycle looks hung); check for a slow or blocked price or forecast source |
| Alert not sent, `alerted=none` in HALT lines | The notify service does not exist (wrong name, notifier not loaded, restart pending) | Fix `alerts.notify_service` or the notifier and restart. The send is retried each cycle; the halt itself proceeds |

## What the planner does

The planner builds a projection of the battery over the coming hours (as far as the price list
reaches) from the price list, the solar forecast and your usage profile. Then it checks a few
rules that forbid certain actions, and tries the possible actions in a fixed order. The first action
that none of the rules forbids is the decision. The log records the reason in words (`why`).

The reserve (`battery.reserve_percent`) limits exporting to the grid. It never makes the planner buy
power to keep the battery up: when the battery reaches the reserve the house simply imports at that
time. Peak shaving may use charge below the reserve, and the inverter's own minimum charge still
applies.

### Labels in the decision log

The decision log prints short labels for the rules and actions. These are the labels the log prints,
and nothing else in this README uses them. The full field-by-field format of a log line, including every `degraded=` marker, is in [docs/decision-log.md](docs/decision-log.md). A rule that forbids an action is called a veto and shows
under `vetoes=`; an action the planner can pick is called a selector and shows under `selector=`.

Vetoes:

| Label | Forbids | When |
|---|---|---|
| V1 | export | Charge is at or below `battery.reserve_percent`. Only exporting to the grid is forbidden; peak shaving may still use charge below it |
| V2 | export | The injection price is negative |
| V3 | grid charging | No capacity budget is left in this quarter-hour |
| V4 | grid charging, export | There is no usable usage history. Peak shaving (S0) is not affected |
| V5 | discharge | The battery is empty (0 % charge). The planner cannot know your inverter's own minimum charge, so this is the only lower limit it applies to peak shaving |
| V6 | grid charging, export | There is no solar forecast: it is missing or too old and there is no usable cached copy (`degraded=solar_zero_fallback`). Peak shaving (S0) is not affected. A stale but usable cached forecast does not trigger it |
| V7 | grid charging, export | `battery.soc_sensor` is set but unreadable (`degraded=soc_unavailable`): there is no battery reading, so nothing is bought or exported. Peak shaving (S0) is not affected. V1 and V5 need a reading and are not checked meanwhile, so the peak guard may still shave |

Selectors, in the order they are tried:

| Label | Action |
|---|---|
| S0 | Peak shave: discharge to the house when the quarter-hour is heading above the ceiling. Mostly relevant when the planner is holding energy back (see [Peak guard](#peak-guard)) |
| S1 | Charge from the grid while the consumption price is negative |
| S3 | Export when the battery would otherwise overflow and now is the best injection price in the window |
| S4 | Charge from the grid in the cheapest blocks when that is cheaper than importing later, ahead of the battery reaching the reserve. The comparison is the price now divided by `battery.round_trip_efficiency` against the average buying price (weighted by energy) of the blocks where the house would otherwise import. It needs usage history, like the other price-driven choices |
| S5 | Charge from the grid when a later injection price, after round-trip losses, beats the price now |
| S6 | Idle: nothing applies, so the planner cancels any forced mode and the inverter does what it does by default (see below) |

There is no S2. Storing surplus solar needs no rule: the inverter's own default does it. By default
the inverter charges the battery from solar surplus until it is full and then exports, and drains it to
serve the house until it is empty and then uses grid power. The planner only steps in when it wants
something different (peak shaving, charging from the grid, exporting); the rest of the time it idles.

Idle is a command, not silence: it cancels every forced mode the planner or the guard set (forced
grid charge, forced export, forced discharge) and returns the inverter to that default. It is sent
once after a forced mode, and not repeated while the planner keeps idling.

In this README, "charge" always means charge from the grid.

Grid charging never exceeds the budget: the charging level (`stay_under_percent` of the ceiling) minus
what the house is drawing. What the house draws is the grid power it would take without the battery
helping. With `battery.power_sensor` set, the planner measures it: the meter reading plus the power
the battery is delivering. A battery that covers a 3 kW house while the meter shows 0 kW still counts
as 3 kW, and the figure does not move when the planner starts charging from the grid. Without that
sensor the planner uses the meter reading minus its own grid charge, which cannot see a battery
that is covering the house. Grid charging stops one evaluation interval before the quarter-hour ends:
in the last `timing.evaluation_interval_minutes` (5 by default, never less than 1) the budget is 0,
so a charge sized for one quarter-hour does not run on into the next while the planner waits for its
next decision. The peak guard and the peak warning are not affected; they re-check every 30 seconds.

Price outage: if prices are missing, unparseable or already elapsed, no decisions are made. A `HALT`
line is logged each cycle, one e-mail is sent on entry and then at most one per
`alerts.realert_minutes`, and a `RECOVERED` line is logged when prices return. The e-mail names the
cause. Check the price entity and attribute (see [Configure](#configure)). If the ENTSO-e integration
itself is down, wait for it; the planner resumes by itself.

Forecast outage: solar counts as zero, the record is marked `solar_zero_fallback`, and the planner
retries. It does not halt. With no forecast the planner is deliberately cautious: it does not buy
power from the grid and does not export, because a plan built on zero solar is a guess. It idles
("hold: no solar forecast") and only peak protection still acts. A forecast that is merely old but
still cached is used as before (`cache_age_solar=...`) and does not hold the planner.

### Peak guard

`pyscript/peak_guard.py` runs every 30 seconds and on each update of the netted offtake sensor. If the
current quarter-hour is heading above the ceiling (this month's peak, never below the 2.5 kW floor),
it records a shaving discharge and sets `pyscript.peak_guard_shaving` to `on`. While that is on, the
planner does not grid-charge. While shaving, the guard rewrites that entity every guard interval with
a fresh `last_beat` attribute; if `last_beat` is missing or older than three guard intervals (90
seconds by default) the planner ignores the `on`, logs a warning, and carries on, so a stopped guard
cannot block charging for good. The guard only reacts to a window that is already forming. It does not
hold charge back for an evening peak it could foresee. Like the planner, it only logs unless you add
an inverter driver. With a real driver the meter shows the offtake after the battery has already
taken some of the load. The guard therefore adds the discharge power it is commanding back to the
metered offtake before projecting the quarter-hour (the window energy comes from the meter and is not
adjusted). The shave then stays at one steady value while the load persists and stops only when the
load, without the battery, would no longer push the quarter-hour over the ceiling. The added-back
power counts only while the guard keeps confirming the shave (within two guard intervals) and never
with the `logging` driver, which commands nothing.

Shaving matters mainly when the planner itself is holding energy back from the house. The planner
does that when it charges the battery from the grid. The grid then supplies
the household plus the charge, and a sudden load can push the quarter-hour over the ceiling. The
guard then discharges to cut the grid draw, and the planner stops grid-charging while it shaves. If
your inverter already runs the house from the battery whenever it has charge, the grid draw is low
and the guard has nothing to do. It cannot add discharge beyond what the inverter allows, and it
stops only when the battery is empty. The reserve does not stop it: `battery.reserve_percent` only limits
exporting to the grid, so the guard may use charge below it. The inverter's own minimum charge still
applies.

### Alert e-mails

All e-mails go through the notifier `notify.<alerts.notify_service>` to `alerts.address`.

Price outage (halt). Sent when prices become unavailable, then at most once per
`alerts.realert_minutes`. Fix the price source as described above.

Peak warning, before (`alerts.peak_warning_enabled`). The peak guard projects the current
quarter-hour's average from the energy so far and the current offtake. If the projection is above the
ceiling on `alerts.peak_warning_ticks` consecutive evaluations (default 2), it sends a prediction: the
projected average, the ceiling, the time left, the current offtake and what the guard is doing. It is
not sent in the first or last minute of a quarter-hour, at most once per quarter-hour and at most once
per `alerts.peak_warning_min_interval_minutes` (default 60, also across a restart). When you get one,
reduce load now (oven, dryer, EV, heating) if you can. The prediction can be wrong, and a correct one
does not mean you were billed.

Peak notice, after (`alerts.peak_enabled`). Sent when this month's peak from the meter
(`capacity_tariff.month_peak_sensor`) goes above `capacity_tariff.billing_floor_kw`: one e-mail for the
first crossing in a calendar month, then one for each new peak at least 0.05 kW higher than the last
one mailed. It contains the peak, the floor and when the planner first saw it. The meter
only reports a new maximum after the quarter-hour has ended, so this arrives after the peak is set. It
works during a price outage. When you get one, nothing needs fixing; note which appliance caused it so
you can avoid repeating it this month. A failed send is retried on the next cycle.

The last mailed month and peak are kept in `<ha-config>/battery_planner/state/peak_alert.json`. If
that file is lost, you may get one extra e-mail for an old peak after a restart.

### What a restart does

Home Assistant restarts, pyscript reloads and power cuts lose everything held in memory. These are
saved in `<ha-config>/battery_planner/state/` and picked up again, so a restart does not repeat
something that was just done:

| File | Keeps | Effect after a restart |
|---|---|---|
| `last_command.json` | The last command the driver accepted (action, power, time) | The driver is not sent the same command again until its resend time has passed (a record older than that is treated as expired, so an old file never blocks a command) |
| `halt.json` | A price outage in progress: cause, when it started, when the last e-mail went out | No early second e-mail, the same outage keeps its start time, and `RECOVERED` reports its full length |
| `average_mode_planner.json`, `average_mode_guard.json` | The quarter-hour average readings used to detect the meter's behaviour | A detected mode stays detected instead of falling back to "assumed" |
| `peak_warning.json` | When the last peak warning was sent | No second warning for the same quarter-hour, and the minimum interval still applies |
| `peak_alert.json` | The last month-peak e-mail | No repeat e-mail for an old peak |

A missing or damaged file is treated as "nothing saved": the planner starts fresh and never stops
because of it. Deleting a file is safe; the worst case is one repeated e-mail or command. Everything
else starts fresh, for example the guard's shaving state, which is re-derived from the meter on the
next evaluation.

## Sensors

Once per planner cycle (every `timing.evaluation_interval_minutes`) the planner publishes its latest
state as four Home Assistant entities. Publishing never changes a decision; if it fails, the
planner logs a warning (at most once an hour) and carries on. Set `sensors.enabled: false` to turn it
off.

| Entity | State | Attributes |
|---|---|---|
| `sensor.battery_planner_action` | The last action: `charge`, `discharge`, `export` or `idle` (`unknown` during a price outage) | `power_kw`, `selector`, `vetoes`, `why` (the reason in words), `degraded` (the markers, comma separated), `soc_percent` (`null` while the charge is the 50 % placeholder), `decided_at` (local time), `source` |
| `sensor.battery_planner_budget` | Power the planner may still draw from the grid for charging, in kW. Can be negative. `unknown` when the peak logic is off or its sensors cannot be read | `ceiling_kw`, `average_kw`, `month_peak_kw` |
| `binary_sensor.battery_planner_halted` | `on` while prices are missing and no decisions are made, `off` otherwise | `cause`, `since` (both empty while `off`) |
| `sensor.battery_planner_solar_ratio` | The [solar calibration](#solar-calibration) ratio applied to the forecast block now. `unknown` while there is no forecast | `source` (`measured` or `configured`), `weeks_of_history` (weeks between the oldest and newest block with a ratio, counted over the window the planner reads, which is `solar.calibration_weeks` plus one day) |

A missing value is shown as `unknown` (or empty in an attribute), never as the number from an earlier
cycle. The long texts (`why`, `degraded`) are attributes because a state is limited to 255 characters.
The peak guard keeps its own `pyscript.peak_guard_shaving` entity, unchanged.

Things to know:

- The entities are created by pyscript at run time. They disappear when Home Assistant restarts and
  come back with the next planner cycle, at most `timing.evaluation_interval_minutes` later.
- They have no unique id, so Home Assistant cannot edit them in the UI (no renaming, no area, no
  icon change). Use a dashboard card or a template to present them differently.
- If you turn `sensors.enabled` off after they were created, the last values stay until the next
  restart.

A small dashboard card (Edit dashboard, add card, Manual):

```yaml
type: entities
title: Battery planner
entities:
  - entity: sensor.battery_planner_action
  - type: attribute
    entity: sensor.battery_planner_action
    attribute: why
    name: Why
  - entity: sensor.battery_planner_budget
  - entity: binary_sensor.battery_planner_halted
```

An automation that reacts when the planner halts (for example to send a phone notification in
addition to the e-mail; use your own notifier):

```yaml
automation:
  - alias: Battery planner halted
    trigger:
      - platform: state
        entity_id: binary_sensor.battery_planner_halted
        to: "on"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: Battery planner halted
          message: "{{ state_attr('binary_sensor.battery_planner_halted', 'cause') }}"
```

## Energy history

Home Assistant's recorder keeps raw states for only about ten days and long-term statistics hourly.
The planner therefore keeps its own 15-minute energy history in
`<ha-config>/battery_planner/history/blocks-YYYY-MM-DD.jsonl` (one JSON object per block, one file per
local day) and `last_snapshot.json` (the counters at the last boundary, so a restart does not lose the
block in progress). Set `history.enabled: false` to stop recording. A recording failure is logged
(at most once an hour per kind) and never affects a decision.

You list cumulative kWh counters under `history.sensors` in `user_config.yaml`. Several counters for
one quantity are summed (tariff 1 plus tariff 2). The planner reads them at each block boundary and
records the difference. Defaults: `import` and `export` use the four SlimmeLezer tariff counters;
`solar`, `battery_charge`, `battery_discharge` and `load` are empty.

To get usage history, and with it the price-driven actions, configure either a `load` counter, or `solar` together
with `battery_charge` and `battery_discharge`. Solar alone is not enough while a battery is installed.
With only the default counters, import and export are recorded but `load_kwh` stays `null`.

Fields per block (energy in kWh; any field is `null` when unknown):

| Field | Meaning |
|---|---|
| `block_start`, `local_date`, `block_minutes` | Block start (UTC ISO), local date (the file it is in), length |
| `import_kwh`, `export_kwh` | Energy taken from the grid and injected into it. `from_net_kwh` equals `import_kwh` |
| `solar_kwh` | Measured PV production |
| `battery_charge_kwh`, `battery_discharge_kwh` | Energy into and out of the battery |
| `load_kwh`, `load_source` | Household consumption: `measured` from a `load` counter, else `derived`, else `null` |
| `load_from_solar_kwh`, `load_from_battery_kwh`, `load_from_net_kwh`, `split_method` | Where the load came from |
| `forecast_solar_kwh` | The forecast for the block when it started, before any [solar calibration](#solar-calibration); `null` if the forecast was missing |
| `solar_ratio` | `solar_kwh` divided by `forecast_solar_kwh`. `null` unless both are known and the forecast was at least 0.05 kWh (below that the ratio is noise). Not capped; the cap applies only when the ratio is used |
| `consumption_price`, `injection_price` | Prices of the block, as known when it started |
| `soc_percent` | Battery charge (%) at the start of the block; `null` while the charge is a stub or unreadable |
| `soc_end_percent` | Battery charge (%) at the end of the block, read with the snapshot that closes it (the next block's start). `null` when that reading is a stub or unreadable, and in the `null` records of a gap. Records written before this field existed do not have it |
| `complete` | `true` only if every configured counter was readable at both readings and gave a valid difference |
| `start_read_at`, `snapshot_read_at` | When the counters were actually read |

Derived load = `import - export + solar + battery_discharge - battery_charge`, only when all five terms
are known. The split (`priority_v1`) assumes solar serves the load first, then the battery, then the
grid. The counters are block totals, so this is a convention, not a measurement.

A negative difference (counter reset or replacement) makes that field `null`. A gap of more than one
block (planner stopped, Home Assistant restarted) writes `null` records for the missed blocks, at most
one day of them. A difference is never spread over several blocks, and an unreadable counter makes its
whole quantity `null`.

The planner runs every `timing.evaluation_interval_minutes`, so a block's counters are read up to one
interval after the boundary. Both read times are in every record.

History files are deleted after `retention.keep_days` days (default 90), see
[Daily cleanup](#daily-cleanup). To keep more, raise that setting. To keep it forever, copy the
`history` folder somewhere else before it expires.

### Solar calibration

The forecast is often too high (or too low) for your roof. If real production is usually about 80 %
of what forecast.solar says, the planner would count on solar that never arrives. The planner
therefore compares the measured solar energy with the forecast in its energy history and corrects
the forecast with a ratio (0.8 in that example).

It needs a `solar` counter under `history.sensors` (see [Energy history](#energy-history)). Without
one nothing is measured, and the configured `solar.calibration_default` applies all the time; with the
default of `1.0` the forecast is used as it is. No `solar` counter is shipped as a default, so this
is the situation on a fresh install.

Which ratio is used:

1. **Not enough history yet.** Until the measured blocks span `solar.calibration_weeks` weeks (default 4,
   counted from the oldest to the newest block that has a ratio), every block uses
   `solar.calibration_default`. Records then carry `solar_ratio_configured=0.80`.
2. **Enough history.** For a forecast block that starts at a certain time of day, the planner looks at all
   blocks of the last `solar.calibration_weeks` weeks that start within one hour before or after that
   time of day (the hour on either side included; at 15-minute blocks that is 9 blocks a day) and
   divides the total measured solar by the total forecast solar of those blocks. Records then carry
   `solar_ratio=0.83`. Blocks without a ratio (no counter value, or a forecast below 0.05 kWh) are left
   out.
3. **Too few blocks around that time of day.** With fewer than 12 usable blocks the configured value
   is used for that time of day. That is why early morning and evening usually show the configured
   value even when midday shows a measured one.

The ratio is never above 2 and never below 0. It is recalculated at most once an hour, from the history
files, and applied to the stored forecast every cycle, so changing `solar.calibration_default` takes
effect on the next cycle. The stored forecast and the forecast in the energy history are always the
uncorrected figures, so the ratio never feeds back into itself. The [daily report](#daily-report) also
compares measured solar with the uncorrected forecast.

Totals are used rather than an average of the per-block ratios, because the total energy error is what
matters: a dawn block with 0.06 kWh forecast and a noisy 0.12 kWh measured (ratio 2.0) should not
weigh as much as a midday block with 1.2 kWh.

A marker `solar_ratio=...` or `solar_ratio_configured=...` is added to the `degraded` field when the
ratio for the current or next block with forecast solar is not 1.00, and the
`sensor.battery_planner_solar_ratio` entity shows the ratio now (see [Sensors](#sensors)). Outside
daylight that sensor shows the configured value, because there is nothing to measure.

Choose `solar.calibration_default` as the share of the forecast you normally get, rounded down. Keep
`retention.keep_days` at 7 times `solar.calibration_weeks` or more; the planner refuses to start
otherwise, because the cleanup would delete history the calibration needs. Leave a few days of margin
above that minimum: right after the 03:30 cleanup the history can be a little shorter than the weeks you
asked for, and the configured value then applies until that morning's first measured blocks arrive.

### Daily report

Shortly after midnight (00:10) the planner writes a short report of the day that just ended, next to
the history: `<ha-config>/battery_planner/history/report-YYYY-MM-DD.md`. Open it in any text editor or
Markdown viewer. It is built from that day's decision log and energy history, in your configured
`timezone`. If either file is missing the report still appears, with a note; if both are missing
nothing is written. After a restart or an outage the reports missing for the last seven days are
written too. An existing report is never rewritten. Set `report.enabled: false` to turn it off.

What it contains:

- **Decisions**: how many records, how often each action and each selector was used, which vetoes
  and degraded markers were seen and how often, halts, recoveries and skipped cycles, the peak guard
  shaving periods, the highest quarter-hour average against the ceiling, and the lowest charging
  budget (negative means the quarter-hour was already over the charging level). Units match the log.
- **Energy**: grid import and export, solar produced against what was forecast (as a percentage; 100%
  means the forecast was right), battery charge and discharge, and household load when it is known.
  Totals only add up the blocks that have a value, and the table shows how many that was.
- **Blocks recorded**: how many of the expected blocks for that day exist. A normal day has 96; the
  day the clocks go forward has 92 and the day they go back has 100.
- **Data quality**: only present when something is off, for example a missing file, lines that could
  not be read (they are skipped), fewer blocks than expected, or a quantity that is empty in many
  blocks because its counter is not configured or was unreadable.

### Daily cleanup

Every night at 03:30 (local time) the planner deletes old files. The date in the file name decides,
not the time the file was last changed. A file is deleted when its date is more than
`retention.keep_days` days ago (default 90, so on 30 September the file for 1 July is still there and
the one for 30 June is gone). Only these files are ever deleted:

- `<ha-config>/battery_planner/decisions-YYYY-MM-DD.log` (the decision logs)
- `<ha-config>/battery_planner/history/blocks-YYYY-MM-DD.jsonl` (the energy history)
- `<ha-config>/battery_planner/history/report-YYYY-MM-DD.md` (the daily reports)

Everything else is left alone: your `user_config.yaml`, the `state` and `cache` folders,
`last_snapshot.json`, folders, and any file with a different name (so you can keep a copy by renaming
it). Each night one line in the Home Assistant log says how many files were removed. A file that
cannot be removed is reported as a warning and tried again the next night.

`retention.keep_days` must be a whole number of at least 1 and at least 7 times the larger of
`usage.history_weeks` and `solar.calibration_weeks`, because the usage profile and the solar calibration
are built from that many weeks of history. With the default 4 weeks the lowest accepted value is 28; if
the value is too low the planner reports a configuration error that names the settings, and nothing is
deleted until you fix it. If you set either of them above 12, raise `retention.keep_days` too.

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

## Adding an inverter driver

A driver is one file, `pyscript/modules/inverter_<name>.py`, selected with `inverter.type: <name>`
(lowercase letters, digits, underscore). The exact interface and rules are in [docs/inverter-boundary.md](docs/inverter-boundary.md). Copy `inverter_logging.py` and replace the bodies. Skeleton
for a hypothetical `alphaess`:

```python
# pyscript/modules/inverter_alphaess.py
SOC_IS_STUB = False            # optional, default False. True = the charge is a placeholder
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


### Drivers that switch Home Assistant helpers (plan-style)

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
  100 gives the 50 % placeholder and `degraded=inverter_read_failed`.

**Run it with `inverter.dry_run: true` first.** In a dry run the framework logs the service calls it
would make (one line per command, at info level) and executes none of them. The command is still
remembered as sent, so you see exactly when it would be repeated. Watch the Home Assistant log for a
day, compare it with what the planner decided, and only then set `dry_run: false`. A driver for real
hardware is your responsibility.

The service-call path was exercised only with test doubles and with the pyscript interpreter in a
test harness, never against real hardware or a real Home Assistant.

What the framework does for you:

- It writes the decision line before calling your driver. A driver that raises, times out, returns
  `False` or is missing never changes that line and never crashes the planner or the guard.
- It runs your code in an executor thread, so blocking network calls are fine.
- It gives each call 10 seconds, then treats it as failed and logs it.
- It marks decisions `soc_stubbed` while `SOC_IS_STUB` is true.
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
      before projecting (see [Peak guard](#peak-guard)); the command therefore stays steady instead
      of switching on and off every 30 seconds. Still yours: the guard sends a command only when it
      changes (the framework repeats it at 80 % of your `COMMAND_HOLD_MINUTES`, else after
      `inverter.resend_minutes`, but only when the planner
      or the guard calls `send` again), so make sure the inverter holds a discharge command for as
      long as it takes to hear again, and that `send("idle", 0)` really releases it. The add-back
      assumes the commanded power is what the inverter delivers; a driver that clips it (for example
      at a lower inverter limit) should set `battery.max_discharge_kw` (or a `battery.max_discharge_sensor`) to that limit.
- [ ] Run with `logging` first and compare a week of decisions with what the battery should have done.
- [ ] Return the real charge from `read_charge_percent` and leave `SOC_IS_STUB` unset.

The decision logic in `pyscript/modules/` contains no Home Assistant code and is tested without it:

```bash
python3.12 -m pip install pytest
python3.12 -m pytest tests/ -v
```

If you fork, do not publish `secrets.yaml` or `battery_planner/user_config.yaml`, and check with
`git check-ignore -v` that your `.gitignore` covers them.

## AlphaESS inverter, read-only (Home Assistant modbus)

This is an example of giving the planner real readings from an AlphaESS inverter, through Home
Assistant's built-in `modbus` integration and a Modbus-to-Ethernet bridge (an HF2211 in the example).
It only reads. This project does not write to the inverter yet, so nothing here changes what the
inverter does. With the readings the planner knows the real battery charge, sees how much power the
battery is giving the house, and gets its energy history from the inverter's own counters. A ready
made integration that also controls the inverter is
[ramonvanraaij/ha-alphaess-modbus](https://github.com/ramonvanraaij/ha-alphaess-modbus) (see
[Credits and related projects](#credits-and-related-projects)). The sensor definitions below follow
its register definitions; the register list itself comes from the AlphaESS Modbus documentation.

### Set it up

1. Connect the bridge to the inverter and your network. Give the bridge a **static IP address**:
   a DHCP reservation in your router, or a fixed address in the bridge's own network settings. Home
   Assistant addresses the bridge by that IP (`alphaess_modbus_host_ip`). If the address changes, every
   AlphaESS sensor goes `unavailable` and the planner falls back: it holds because it has no battery
   reading (`soc_unavailable`) and estimates the household draw without the battery sensor
   (`battery_power_unavailable`).
2. Find the Modbus slave id of your inverter (see [Finding the slave id](#finding-the-slave-id)).
3. Add three keys to Home Assistant's `secrets.yaml` (placeholders are in `secrets.example.yaml`):

   ```yaml
   alphaess_modbus_host_ip: 192.0.2.10    # the bridge's static IP
   alphaess_modbus_host_port: 502
   alphaess_modbus_slaveId: 1             # found in the previous step
   ```

4. Add the hub below to your configuration, either directly under a `modbus:` key in
   `configuration.yaml`, or in a separate file. With a separate file the line in
   `configuration.yaml` is `modbus: !include alphaess_modbus.yaml` and the file must contain only the
   list of hubs, not a `modbus:` key of its own.

   ```yaml
   - name: modbuspvsystem
     type: tcp
     host: !secret alphaess_modbus_host_ip
     port: !secret alphaess_modbus_host_port
     message_wait_milliseconds: 10
     timeout: 10
     delay: 1
     sensors:
       - name: AlphaESS SoC Battery
         unique_id: AlphaESS_SoC_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0102
         data_type: uint16
         unit_of_measurement: "%"
         device_class: battery
         state_class: measurement
         scan_interval: 10
         scale: 0.1
         precision: 1

       - name: AlphaESS Power Battery
         unique_id: AlphaESS_Power_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0126
         data_type: int16
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 10

       - name: AlphaESS Power Grid
         unique_id: AlphaESS_Power_Grid
         slave: !secret alphaess_modbus_slaveId
         address: 0x0021
         data_type: int32
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 10

       - name: AlphaESS Total Energy from PV
         unique_id: AlphaESS_Total_Energy_from_PV
         slave: !secret alphaess_modbus_slaveId
         address: 0x043E
         data_type: uint32
         unit_of_measurement: kWh
         device_class: energy
         state_class: total_increasing
         scan_interval: 60
         scale: 0.1
         precision: 2

       - name: AlphaESS Total Energy Charge Battery
         unique_id: AlphaESS_Total_Energy_Charge_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0120
         data_type: uint32
         unit_of_measurement: kWh
         device_class: energy
         state_class: total_increasing
         scan_interval: 60
         scale: 0.1
         precision: 2

       - name: AlphaESS Total Energy Discharge Battery
         unique_id: AlphaESS_Total_Energy_Discharge_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0122
         data_type: uint32
         unit_of_measurement: kWh
         device_class: energy
         state_class: total_increasing
         scan_interval: 60
         scale: 0.1
         precision: 2

       - name: AlphaESS Battery Capacity
         unique_id: AlphaESS_Battery_Capacity
         slave: !secret alphaess_modbus_slaveId
         address: 0x0119
         data_type: uint16
         unit_of_measurement: kWh
         state_class: measurement
         scan_interval: 60
         scale: 0.1
         precision: 1

       - name: AlphaESS Battery Max Charge Power
         unique_id: AlphaESS_Battery_Max_Charge_Power
         slave: !secret alphaess_modbus_slaveId
         address: 0x012C
         data_type: uint16
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 60

       - name: AlphaESS Battery Max Discharge Power
         unique_id: AlphaESS_Battery_Max_Discharge_Power
         slave: !secret alphaess_modbus_slaveId
         address: 0x012D
         data_type: uint16
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 60

       - name: AlphaESS Inverter Work Mode      # optional, informational
         unique_id: AlphaESS_Inverter_Work_Mode
         slave: !secret alphaess_modbus_slaveId
         address: 0x0440
         data_type: uint16
         state_class: measurement
         scan_interval: 10
   ```

5. Restart Home Assistant. Home Assistant builds the entity ids from the sensor names. Check them in
   Developer Tools -> States before you use them: with the names above they are
   `sensor.alphaess_soc_battery`, `sensor.alphaess_power_battery` and so on.
6. Point the planner at them in `user_config.yaml` (the table below).

The power sensors poll every 10 seconds, which is easy on the bridge; the planner runs once a minute.
The PV power register (`0x0453`) was unreadable on one install and the planner does not need it, so it
is left out.

### Which sensor does what

| Entity | What it is for | `user_config.yaml` key |
|---|---|---|
| `sensor.alphaess_soc_battery` | Battery charge in percent. The planner and the peak guard use it instead of the 50 % placeholder | `battery.soc_sensor` |
| `sensor.alphaess_power_battery` | Battery power in W. Positive while the battery discharges, which is the default sign. It lets the planner see the load the battery is covering | `battery.power_sensor`, with `battery.power_positive: discharge` (use `charge` if yours is the other way round) |
| `sensor.alphaess_battery_max_charge_power` | The inverter's maximum battery charge power in W. The planner uses it as its charge limit | `battery.max_charge_sensor` |
| `sensor.alphaess_battery_max_discharge_power` | The inverter's maximum battery discharge power in W. The planner uses it as its export and peak shaving limit | `battery.max_discharge_sensor` |
| `sensor.alphaess_total_energy_from_pv` | Solar energy counter, for the energy history and the solar calibration | `history.sensors.solar` |
| `sensor.alphaess_total_energy_charge_battery` | Energy into the battery counter | `history.sensors.battery_charge` |
| `sensor.alphaess_total_energy_discharge_battery` | Energy out of the battery counter | `history.sensors.battery_discharge` |
| `sensor.alphaess_power_grid` | Grid power in W. Not read by the planner; handy to compare with your meter and to check signs | none |
| `sensor.alphaess_battery_capacity` | Battery size in kWh. Not read by the planner; use it to choose `battery.capacity_kwh` (usable capacity, rounded down) | none |
| `sensor.alphaess_inverter_work_mode` | Work mode number, informational | none |

The matching lines in `user_config.yaml`:

```yaml
battery:
  capacity_kwh: 10.0
  soc_sensor: sensor.alphaess_soc_battery
  power_sensor: sensor.alphaess_power_battery
  power_positive: discharge
  max_charge_sensor: sensor.alphaess_battery_max_charge_power
  max_discharge_sensor: sensor.alphaess_battery_max_discharge_power
history:
  sensors:
    solar: [sensor.alphaess_total_energy_from_pv]
    battery_charge: [sensor.alphaess_total_energy_charge_battery]
    battery_discharge: [sensor.alphaess_total_energy_discharge_battery]
```

With the inverter's default behaviour (nothing is sent), the battery covers the house and the grid draw
stays near zero. That is why the planner needs the battery power: the meter alone would read the
house as drawing nothing.

### Finding the slave id

The slave id (unit id) depends on the inverter and the bridge settings. A short script that asks
holding register `0x0102` (the battery charge) for a few ids shows which one answers. Run it on a
computer that can reach the bridge, with `pip install pymodbus`:

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("192.0.2.10", port=502)   # your bridge
client.connect()
for slave in (0, 1, 2, 85, 255):
    result = client.read_holding_registers(0x0102, count=1, slave=slave)
    print(slave, result)
client.close()
```

The id that returns a value (the charge in tenths of a percent, so 576 means 57.6 %) is yours. Newer
pymodbus versions call the `slave` argument `device_id`.

### If the sensors are unavailable

| Symptom | Cause | Fix |
|---|---|---|
| All `sensor.alphaess_*` are `unavailable`, records carry `soc_unavailable` and `battery_power_unavailable` | The bridge cannot be reached. The usual cause is that its IP address changed | Check that the bridge still has the address in `alphaess_modbus_host_ip` and answers (the script above). Give it a static address so this does not happen again |
| Only some sensors are `unavailable` | A register is not readable on your inverter model | Remove that sensor from the hub; the planner does not need the optional ones |
| Values look 10 times too big or small | A `scale` is missing or wrong | Compare with the definitions above |
| The battery power has the wrong sign | Your inverter reports the opposite direction | Set `battery.power_positive: charge` |

Home Assistant has no write access here: nothing in this hub sends a command to the inverter.

## Known gaps

- No inverter driver is shipped except `logging`, so nothing controls a battery.
- Battery charge is a fixed 50 % unless `battery.soc_sensor` is set or a driver reads the real value.
- Until a household load source is configured (usage history exists), the planner only peak-shaves
  and otherwise idles: no grid charging, no solar storage, no exporting. The inverter's own behaviour
  applies meanwhile.
- Peak protection is reactive. The projection covers battery charge, not grid offtake, so the planner
  does not hold charge back for a foreseeable evening peak.
- The peak guard adds the discharge it commands back to the offtake reading, assuming the inverter
  delivers that power. This has only been tested with simulated meter readings, never with a real
  inverter (see the driver checklist).
- Single solar plane, free forecast tier only, Flemish capacity tariff only, battery only.
- The alert e-mails and the entity names on installs other than the original one have not been tested
  against a live Home Assistant. Send a test mail (install step 8) and check the entity names as
  described above.
- The solar calibration (see [Solar calibration](#solar-calibration)) has only been tested with
  generated history. It needs a `solar` energy counter, which is not configured by default, and then
  about `solar.calibration_weeks` weeks of recording before it measures anything. The recorded prices,
  battery and split fields are for later analysis.

To adapt the planner: provider prices are `prices.*` in `user_config.yaml`; the capacity-tariff logic
is in `pyscript/modules/capacity.py`; usage history is read by `read_usage_history` in
`pyscript/battery_planner.py`.

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
