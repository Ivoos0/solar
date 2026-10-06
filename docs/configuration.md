# Configure

Every setting in `user_config.yaml`, which way to round values, and notes on prices, the meter and the peak guard. Back to the [README](../README.md).

**Contents**

- [Settings reference](#settings-reference)
- [Rounding: which way to err](#rounding-which-way-to-err)

`battery_planner/user_config.example.yaml` lists every setting with comments. A bad configuration
is a startup error: it is logged and no decision is made. If the planner was already running, it also
sends one `idle` to release any forced command and one e-mail, using the last good configuration.

## Settings reference

Every key in `user_config.yaml`. Keys you leave out use the default. `battery.capacity_kwh`, `alerts.address`
and, with the default `logging` driver, `battery.soc_sensor` have no default and must be set. While the
capacity tariff is on, `battery.power_sensor` is needed too, or the planner holds.

| Key | Default | Unit | What it changes |
|---|---|---|---|
| `prices.consumption_multiplier` | `1.07` | factor | Buying price = market price x this + offset. Must be all-in (see below) |
| `prices.consumption_offset` | `0.007` | EUR/kWh | Added to the buying price. Put per-kWh network costs, taxes and VAT here. The default is only a supplier coefficient: below 0.05 records carry `consumption_offset_low`, because every grid charge then looks cheaper than it is (the example config shows how to work out the real figure, about 0.14 in Flanders) |
| `prices.injection_multiplier` | `0.94` | factor | Selling price = market price x this + offset |
| `prices.injection_offset` | `-0.011` | EUR/kWh | Added to the selling price. Negative: injection can turn negative while the market price is still positive |
| `prices.entity` | `sensor.entso_prices_average_electricity_price` | entity id | Sensor that carries the price list |
| `prices.attribute` | `prices` | attribute name | Attribute of that sensor holding the `{time, price}` list |
| `battery.capacity_kwh` | **required** | kWh | Battery size the planner uses |
| `battery.reserve_sensor` | none | entity id | Sensor with the inverter's own minimum charge in percent (for the AlphaESS the discharge cut-off, register `0x0850`). When it reads from 0 up to below 100, the planner uses it as the reserve, read again every cycle. Otherwise `battery.reserve_percent` applies and records carry `battery_reserve_fallback` |
| `battery.reserve_percent` | `10.0` | % | Charge level the planner will not export to the grid below. It is a limit on exporting, not a target: the planner never buys power to keep the battery up to it, and peak shaving may use charge below it. The inverter's own minimum charge still applies. Keep it at or above the inverter's own discharge cut-off, otherwise the planner counts charge the inverter will not release as usable. Fallback when no `battery.reserve_sensor` is set or it cannot be read |
| `battery.soc_sensor` | none | entity id | **Required** with the `logging` driver (the config is refused without it); optional with a real driver that reads the charge itself. Sensor with the battery charge in percent (0 to 100), read by the planner and the peak guard. `unavailable`, `unknown`, not refreshed for `timing.sensor_stale_minutes`, a non-number or a value outside 0 to 100 counts as unreadable: the planner holds and marks `soc_unavailable`. There is no placeholder charge |
| `battery.power_sensor` | none | entity id | Sensor with the battery power, in W or kW (the unit attribute is read; any other unit counts as unreadable). Lets the planner measure the household draw (see [What the planner does](how-it-works.md#what-the-planner-does)). Required while `capacity_tariff.enabled` is true: when it is not set, or cannot be read, the planner holds (no charging, no exporting; peak shaving still acts) and records carry `battery_power_not_configured` or `battery_power_unavailable` |
| `battery.power_positive` | `discharge` | `discharge` or `charge` | Which direction is positive in `battery.power_sensor`. `discharge`: positive while the battery gives power (AlphaESS). `charge`: positive while it takes power |
| `battery.max_charge_sensor` | none | entity id | Sensor with the inverter's own maximum battery charge power, in W or kW (the unit attribute is read). When it reads above 0, the planner uses it as the charge limit, read again every cycle. Otherwise (unreadable, 0 or less, not refreshed for `timing.sensor_stale_minutes`, or above 4 times `battery.max_charge_kw`) `battery.max_charge_kw` applies and records carry `battery_limits_fallback` |
| `battery.max_discharge_sensor` | none | entity id | The same for the maximum discharge power: the limit for exporting and for peak shaving |
| `battery.max_charge_kw` | `5.0` | kW | Highest charge power the planner proposes. Fallback when no `battery.max_charge_sensor` is set or it cannot be read |
| `battery.max_discharge_kw` | `5.0` | kW | Power used when exporting and the cap on peak shaving. Fallback when no `battery.max_discharge_sensor` is set or it cannot be read |
| `battery.round_trip_efficiency` | `0.90` | 0 to 1 | Share of stored energy you get back. A later price only counts at this fraction |
| `solar.forecast_entity` | `sensor.forecast_solar_estimate` | entity id | The REST sensor from `configuration.yaml` |
| `solar.forecast_attribute` | `watt_hours_period` | attribute name | Attribute holding the Wh per period |
| `solar.calibration_default` | `1.0` | factor (above 0, up to 2) | The forecast is multiplied by this until enough measured history exists. `0.8` counts on 80 % of the forecast. `1.0` changes nothing. See [Solar calibration](history-and-reports.md#solar-calibration) |
| `solar.calibration_weeks` | `4` | weeks (whole number, 1 or more) | Weeks of measured history needed before measured ratios replace `solar.calibration_default`, and the number of weeks they are averaged over |
| `capacity_tariff.enabled` | `true` | true/false | `false` turns off all peak logic (the peak budget limit, peak shaving, the peak guard's shaving, the grid-charge cap) |
| `capacity_tariff.billing_floor_kw` | `2.5` | kW | Peaks at or below this cost nothing extra. Sets the lowest ceiling |
| `capacity_tariff.stay_under_percent` | `80` | % (above 0, up to 100) | Grid charging stays under this share of the ceiling |
| `capacity_tariff.guard_interval_seconds` | `30` | seconds | Minimum gap between peak guard runs. The guard's timer is fixed at 30, so values below 30 change nothing |
| `capacity_tariff.quarter_hour_average_mode` | `accumulating` | `accumulating`, `running` | How the meter's quarter-hour average is read (see below). `auto` no longer exists |
| `capacity_tariff.offtake_sensor` | `sensor.slimmelezer_power_consumed` | entity id | Netted total offtake |
| `capacity_tariff.quarter_hour_average_sensor` | `sensor.slimmelezer_huidig_kwartiervermogen` | entity id | Meter register 1-0:1.4.0 |
| `capacity_tariff.month_peak_sensor` | `sensor.slimmelezer_maandpiek` | entity id | Meter register 1-0:1.6.0 |
| `usage.history_weeks` | `4` | weeks | How many weeks are averaged into the usage profile |
| `usage.min_bucket_days` | `2` | days | Days of history a block of the day needs before its own average is trusted. A block with fewer (none, or too few) takes the interpolated value of the nearest well backed blocks of the same weekday group. During the first days, when no block has this many days yet, every block keeps its own value. Integer, at least 1. Changing it discards the cached profile |
| `usage.min_history_days` | `3` | days | Days of recorded history needed before the planner charges from the grid or exports. Until then it only peak-shaves and idles (`usage_history_unavailable` while there is none, `usage_samples=N` while the profile is still short). At least 1, at most 7 x `usage.history_weeks` |
| `usage.grouping` | `same_weekday` | `same_weekday`, `day_type` | `same_weekday` averages the same weekday; `day_type` pools weekdays and weekend days |
| `usage.recency_weighting` | `linear` | `linear`, `none` | `linear` weights newer weeks more (4 weeks: 4, 3, 2, 1). Changing it discards the cached profile |
| `history.enabled` | `true` | true/false | Records the energy history |
| `history.sensors.import`, `.export` | the SlimmeLezer tariff 1 and 2 counters | entity ids (list) | Cumulative kWh from and to the grid |
| `history.sensors.solar`, `.battery_charge`, `.battery_discharge`, `.load` | `[]` | entity ids (list) | Cumulative kWh counters. See [Energy history](history-and-reports.md#energy-history) |
| `report.enabled` | `true` | true/false | Writes the [daily report](history-and-reports.md#daily-report) |
| `sensors.enabled` | `true` | true/false | Publishes the planner state as Home Assistant [sensors](alerts-and-sensors.md#sensors). `false` publishes nothing |
| `retention.keep_days` | `90` | days | Logs, history and reports older than this are deleted every night. At least 1, and at least 7 times the larger of `usage.history_weeks` and `solar.calibration_weeks`. See [Daily cleanup](history-and-reports.md#daily-cleanup) |
| `timing.sensor_stale_minutes` | `15` | minutes | A continuously published sensor (battery charge, battery power, limit and reserve sensors) that has not been written for this long counts as unavailable: a frozen value is never used. A reading that has no timestamp is never stale. Integer, at least 1 |
| `timing.block_minutes` | `15` | minutes | Planning block length. Must divide 60. Keep 15 to match the price list |
| `timing.evaluation_interval_minutes` | `5` | minutes | How often the planner runs |
| `timing.forecast_retry_minutes` | `10` | minutes | Minimum gap between forced forecast refreshes after failures |
| `timing.solar_cache_stale_minutes` | `120` | minutes | Age after which the cached solar series is rebuilt |
| `timing.usage_cache_stale_minutes` | `2880` | minutes | Age after which the cached usage profile is rebuilt |
| `alerts.address` | **required** | e-mail address | Recipient passed to the notifier. Use the same address as `smtp_recipient` |
| `alerts.notify_service` | `battery_alert` | service name | Notifier `notify.<name>`; lowercase letters, digits, underscore |
| `alerts.realert_minutes` | `60` | minutes | Minimum gap between repeats of the price-outage, sensor-outage and inverter-refuses-commands e-mails |
| `alerts.sensor_enabled` | `true` | true/false | [Sensor outage e-mail](alerts-and-sensors.md#alert-e-mails) when configured sensors stay unavailable |
| `alerts.sensor_outage_minutes` | `15` | minutes | How long a sensor must be unavailable before it is mailed. Integer, at least 1. Repeated every `alerts.realert_minutes` |
| `alerts.peak_enabled` | `true` | true/false | Peak notice e-mail (after) |
| `alerts.peak_warning_enabled` | `true` | true/false | Peak warning e-mail (before) |
| `alerts.peak_warning_min_interval_minutes` | `60` | minutes (whole number, 1 or more) | Minimum gap between peak warnings |
| `alerts.peak_warning_ticks` | `2` | evaluations (whole number, 1 or more) | Consecutive guard evaluations above the ceiling before a warning. Fewer is earlier and noisier |
| `inverter.type` | `logging` | driver name | Selects `pyscript/modules/inverter_<type>.py`. `none` means `logging` |
| `inverter.resend_minutes` | `15` | minutes (whole number, 0 or more) | Fallback for drivers that do not declare how long the inverter keeps a command (`COMMAND_HOLD_MINUTES`, see [Adding an inverter driver](inverter-drivers.md#adding-an-inverter-driver)): the driver gets an unchanged command again only after this long. A changed command (other action, or power differing by 0.01 kW or more) goes out at once. `0` sends on every call. A driver that declares a hold is re-sent at 80 % of it and this setting is not used. The decision log line is written every cycle either way |
| `inverter.dry_run` | `false` | true/false | With a plan-style driver (one that defines `plan`, see [Adding an inverter driver](inverter-drivers.md#adding-an-inverter-driver)): log the service calls it would make and execute none. The command is still remembered as sent. With a `send`-style driver the `send` call is skipped and logged. Run real hardware in dry run first. Nothing changes with the `logging` driver |
| `timezone` | `Europe/Brussels` | time zone name | Local day for log files, history and monthly peak e-mails |

## Rounding: which way to err

When a value is a rounded figure or you are unsure of it, pick the direction that makes the planner
more cautious and cheaper for you. Where no direction is safe, use the exact value.

| Setting | Round | Why |
|---|---|---|
| `prices.consumption_multiplier`, `prices.consumption_offset` | up | A higher buying price makes grid charging (for a negative price or for arbitrage) less attractive |
| `prices.injection_multiplier`, `prices.injection_offset` | down | A lower selling price makes arbitrage less attractive and stops exporting at a negative price sooner |
| `battery.capacity_kwh` | down | Use usable capacity, not nameplate. The planner then never counts on energy the battery does not have |
| `battery.reserve_percent` | up | The planner stops exporting to the grid earlier. A reading from `battery.reserve_sensor` is used as read |
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
the two differ by a factor of 15. You set which one your meter does; there is no detection. To find
out, open the history graph of `capacity_tariff.quarter_hour_average_sensor`: a sawtooth that drops to 0
at every quarter-hour and climbs from there is `accumulating` (the SlimmeLezer 1-0:1.4.0 register
behaves like this); a line that jumps to roughly the current load right after the boundary is
`running`. A config that still says `auto` is refused at startup with a message saying so. The old
`average_mode_planner.json` and `average_mode_guard.json` files under `battery_planner/state/` are no
longer used and can be deleted.

The peak guard's `@state_trigger` names `sensor.slimmelezer_power_consumed` literally, because
decorator arguments are fixed at load time. If your netted offtake sensor has another name, the guard
still reads the configured sensor on every run but only runs on its 30-second tick. To run on every
sensor update, change the `@state_trigger(...)` argument in `pyscript/peak_guard.py`.
