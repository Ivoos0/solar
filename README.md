# Price-aware battery planner for Home Assistant (Flanders)

A Home Assistant [pyscript](https://github.com/custom-components/pyscript) project. Every five
minutes it decides what a home battery should do (charge, discharge, export or idle), using dynamic
electricity prices (ENTSO-e), a solar forecast (forecast.solar), your expected household usage and
the Flemish capacity tariff (capaciteitstarief), which bills your monthly quarter-hour peaks.

Source: <https://github.com/Ivoos0/solar>

## What it does today

- It decides and writes the decision to a log file, one line per cycle. That is the only output.
  There is no dashboard.
- It does not control your inverter. The default driver, `logging`, sends nothing. To act on the
  decisions you add a driver for your inverter (see [Adding an inverter driver](#adding-an-inverter-driver)).
  None is shipped.
- With the `logging` driver the battery charge is a fixed 50 %. Every record carries
  `degraded=soc_stubbed`. A driver that reads the real charge removes the marker.
- Household usage history comes from energy counters you configure (see [Energy history](#energy-history)).
  Until a `load` counter (or `solar` plus both battery counters) is configured, records carry
  `usage_history_unavailable` and the planner never charges from the grid. Charging from surplus
  solar, discharging and exporting still work.

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
   with `!include`; the other blocks in the file are optional price helpers. The shipped file also
   includes `automations.yaml`, `scripts.yaml` and `scenes.yaml`; Home Assistant needs those files to
   exist. You never edit the blocks afterwards: personal values come from `secrets.yaml` and
   `user_config.yaml`. Loading the blocks as a Home Assistant package or from split `!include` files
   is untested.
5. Create `<ha-config>/secrets.yaml` from `secrets.example.yaml` (or add the keys to your existing
   one):
   - `forecast_solar_url`: `https://api.forecast.solar/estimate/<lat>/<lon>/<declination>/<azimuth>/<kwp>`.
     Azimuth 0 is south and negative is east, so `-10` is ten degrees east of south. This is the
     easiest value to get backwards. The roof described here must match the `solar:` section of
     `user_config.yaml`.
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
    cache/                      generated
    state/peak_alert.json       generated, last month-peak e-mail sent
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
| `battery.reserve_percent` | `10.0` | % | Charge level the planner will not discharge or export below (veto V1) |
| `battery.max_charge_kw` | `5.0` | kW | Highest charge power the planner proposes |
| `battery.max_discharge_kw` | `5.0` | kW | Power used when exporting |
| `battery.round_trip_efficiency` | `0.90` | 0 to 1 | Share of stored energy you get back. A later price only counts at this fraction |
| `solar.latitude`, `solar.longitude` | `51.12`, `3.85` | degrees | Roof position. The forecast itself comes from `forecast_solar_url`; these only reset the cached solar and usage series when you change them |
| `solar.kwp` | `8.1` | kWp | As above |
| `solar.declination` | `50` | degrees (0 to 90) | As above |
| `solar.azimuth` | `-10` | degrees (-180 to 180) | As above. 0 is south, negative is east |
| `solar.forecast_entity` | `sensor.forecast_solar_estimate` | entity id | The REST sensor from `configuration.yaml` |
| `solar.forecast_attribute` | `watt_hours_period` | attribute name | Attribute holding the Wh per period |
| `capacity_tariff.enabled` | `true` | true/false | `false` turns off all peak logic (V3, S0, the peak guard's shaving, grid-charge cap) |
| `capacity_tariff.billing_floor_kw` | `2.5` | kW | Peaks at or below this cost nothing extra. Sets the lowest ceiling |
| `capacity_tariff.rate_eur_per_kw_year` | `40.0` | EUR per kW per year | Only used for the cost estimate in the peak notice e-mail. `0` leaves the estimate out |
| `capacity_tariff.stay_under_percent` | `80` | % (above 0, up to 100) | Grid charging stays under this share of the ceiling |
| `capacity_tariff.guard_interval_seconds` | `30` | seconds | Minimum gap between peak guard runs. The guard's timer is fixed at 30, so values below 30 change nothing |
| `capacity_tariff.peak_averaging_months` | `13` | months | Number of monthly peaks your bill averages. Used for the cost estimate |
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
| `timing.block_minutes` | `15` | minutes | Planning block length. Must divide 60. Keep 15 to match the price list |
| `timing.evaluation_interval_minutes` | `5` | minutes | How often the planner runs |
| `timing.forecast_refresh_minutes` | `60` | minutes | Accepted but not used by the current code. The REST sensor refreshes hourly (`scan_interval` in `configuration.yaml`) |
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
| `timezone` | `Europe/Brussels` | time zone name | Local day for log files, history and monthly peak e-mails |

### Rounding: which way to err

When a value is a rounded figure or you are unsure of it, pick the direction that makes the planner
more cautious and cheaper for you. Where no direction is safe, use the exact value.

| Setting | Round | Why |
|---|---|---|
| `prices.consumption_multiplier`, `prices.consumption_offset` | up | A higher buying price makes grid charging (S1, S5) less attractive |
| `prices.injection_multiplier`, `prices.injection_offset` | down | A lower selling price makes arbitrage (S5) less attractive and makes the negative-injection veto (V2) fire sooner |
| `capacity_tariff.rate_eur_per_kw_year` | up | It only sets the euro estimate in the peak e-mail, so a high figure never understates the cost |
| `battery.capacity_kwh` | down | Use usable capacity, not nameplate. The planner then never counts on energy the battery does not have |
| `battery.reserve_percent` | up | The planner stops discharging and exporting earlier |
| `battery.max_charge_kw`, `battery.max_discharge_kw` | down | Commanded power never exceeds what the inverter does |
| `battery.round_trip_efficiency` | down | Arbitrage (S2, S5) needs a bigger price spread |
| `capacity_tariff.stay_under_percent` | lower is safer | Less grid charging near the peak ceiling, at the cost of fewer cheap charges |
| `forecast_solar_url` (kWp part) | down | A lower forecast means less counted-on solar. `solar.kwp` itself does not feed the forecast |
| `capacity_tariff.billing_floor_kw`, `peak_averaging_months` | exact | Take the value from your bill. A wrong floor changes what the ceiling protects |
| `solar.latitude`, `longitude`, `declination`, `azimuth` | exact | They must describe the roof in the forecast URL |
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
samples from several windows, and says so in its records. Pin the value once it is stable. The
detection state is held in memory, so after a restart the mode is "assumed" until enough windows have
been seen.

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
and the HALT / RECOVERED / SKIP lines all write to it. Nothing is deleted automatically, so add your
own cleanup, for example a daily cron job:

```bash
find <ha-config>/battery_planner -name 'decisions-*.log' -mtime +30 -delete
```

A normal record is one line of `|`-separated fields: timestamp, `action`, `power`, `soc`, `cons` and
`inj` (prices), forecast and usage remaining, `saturation`, `spill`, `breach`, `end_soc`, `took`,
`avg`, `ceiling`, `budget`, `vetoes`, `selector`, `why` (the reason in words), `degraded` and
`source` (`planner` or `guard`). Fields that do not apply show `n/a`.

Markers you can expect in `degraded=` on a fresh install:

| Marker | Meaning |
|---|---|
| `soc_stubbed` | The battery charge is the 50 % placeholder. Normal until a driver reads the real charge |
| `usage_history_unavailable` | No household usage history yet. Normal until a load source is configured; grid charging is vetoed meanwhile |
| `usage_samples=N` | The usage profile rests on fewer than `usage.history_weeks` x 7 days of history (N days). Disappears as history builds up |
| `cache_age_solar=...`, `cache_age_usage=...` | A cached series was used, with its age |
| `solar_zero_fallback` | The forecast was unavailable, so solar was treated as zero. The planner retries and does not halt |
| `grid_sensors_unavailable` | Capacity logic is on but the grid sensors are unreadable. Grid charging is suppressed |
| `inverter_driver_unavailable`, `inverter_read_failed` | The configured driver is missing or its charge reading failed |

When the peak guard is shaving a peak it writes its own records: when a shave starts, changes
materially, is vetoed or stops, not once per 30-second tick.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No records at all | pyscript not loaded, or the `pyscript:` block is missing | Add the block from `configuration.yaml`, restart Home Assistant |
| `ImportError` in the HA log | `allow_all_imports: true` is missing | Add it to the `pyscript:` block |
| HA log warns about blocking I/O | A file operation ran on the event loop | It must use `@pyscript_executor` |
| Forecast always zero | The REST sensor is failing | Check `forecast_solar_url` and the forecast.solar free-tier rate limit |
| Constant HALT with "attribute prices missing or empty on ..." | `prices.entity` or `prices.attribute` does not match your install | Open the entity in Developer Tools -> States and copy the sensor and attribute name into `user_config.yaml` |
| `peak_guard: ... not refreshed since window ...` | The quarter-hour average sensor has not published since this window began | At INFO level with a low value this is normal for a quiet house. At WARNING level with a high average and an old timestamp, the meter or its link is stuck and the guard is doing nothing |
| Config edits have no effect | You edited the repository copy | Edit `<ha-config>/battery_planner/user_config.yaml` and restart |
| Core code change has no effect | Core modules are not hot-reloaded | Restart Home Assistant |
| `SKIP` line in the log | A cycle was due while the previous one was still running | Occasional lines are harmless. If they repeat, the HA log shows a warning (an error when the earlier cycle looks hung); check for a slow or blocked price or forecast source |
| Alert not sent, `alerted=none` in HALT lines | The notify service does not exist (wrong name, notifier not loaded, restart pending) | Fix `alerts.notify_service` or the notifier and restart. The send is retried each cycle; the halt itself proceeds |

## What the planner does

The planner builds a projection of the battery over the coming hours (as far as the price list
reaches) from the price list, the solar forecast and your usage profile. Then it applies vetoes, which
forbid certain actions, and tries the selectors in order. The first selector whose proposal is not
vetoed wins. The log shows the vetoes that fired, the selector that produced the decision and the
reason.

Vetoes:

| Veto | Forbids | When |
|---|---|---|
| V1 | discharge, export | Charge is at or below `battery.reserve_percent` |
| V2 | export | The injection price is negative |
| V3 | grid charging | No capacity budget is left in this quarter-hour |
| V4 | grid charging | There is no usable usage history |

Selectors, in order:

| Selector | Action |
|---|---|
| S0 | Peak shave: discharge to the house when the quarter-hour is heading above the ceiling |
| S1 | Charge from the grid while the consumption price is negative |
| S2 | Charge from surplus solar when storing it beats exporting now |
| S3 | Export when the battery would otherwise overflow and now is the best injection price in the window |
| S4 | Charge from the grid in the cheapest blocks when the battery would otherwise drop to the reserve |
| S5 | Charge from the grid when a later injection price, after round-trip losses, beats the price now |
| S6 | Idle |

Grid charging never exceeds the budget: the charging level (`stay_under_percent` of the ceiling) minus
what the house is drawing. In the last minute of a quarter-hour the budget is 0.

Price outage: if prices are missing, unparseable or already elapsed, no decisions are made. A `HALT`
line is logged each cycle, one e-mail is sent on entry and then at most one per
`alerts.realert_minutes`, and a `RECOVERED` line is logged when prices return. The e-mail names the
cause. Check the price entity and attribute (see [Configure](#configure)). If the ENTSO-e integration
itself is down, wait for it; the planner resumes by itself.

Forecast outage: solar counts as zero, the record is marked `solar_zero_fallback`, and the planner
retries. It does not halt.

### Peak guard

`pyscript/peak_guard.py` runs every 30 seconds and on each update of the netted offtake sensor. If the
current quarter-hour is heading above the ceiling (this month's peak, never below the 2.5 kW floor),
it records a shaving discharge and sets `pyscript.peak_guard_shaving` to `on`. While that is on, the
planner does not grid-charge. The guard only reacts to a window that is already forming. It does not
hold charge back for an evening peak it could foresee. Like the planner, it only logs unless you add
an inverter driver.

### Alert e-mails

All e-mails go through the notifier `notify.<alerts.notify_service>` to `alerts.address`.

Price outage (halt). Sent when prices become unavailable, then at most once per
`alerts.realert_minutes`. Fix the price source as described above.

Peak warning, before (`alerts.peak_warning_enabled`). The peak guard projects the current
quarter-hour's average from the energy so far and the current offtake. If the projection is above the
ceiling on `alerts.peak_warning_ticks` consecutive evaluations (default 2), it sends a prediction: the
projected average, the ceiling, the time left, the current offtake and what the guard is doing. It is
not sent in the first or last minute of a quarter-hour, at most once per quarter-hour and at most once
per `alerts.peak_warning_min_interval_minutes` (default 60, forgotten on restart). When you get one,
reduce load now (oven, dryer, EV, heating) if you can. The prediction can be wrong, and a correct one
does not mean you were billed.

Peak notice, after (`alerts.peak_enabled`). Sent when this month's peak from the meter
(`capacity_tariff.month_peak_sensor`) goes above `capacity_tariff.billing_floor_kw`: one e-mail for the
first crossing in a calendar month, then one for each new peak at least 0.05 kW higher than the last
one mailed. It contains the peak, the floor, when the planner first saw it and an estimated cost: the
rise over the floor, at `rate_eur_per_kw_year`, over `capacity_tariff.peak_averaging_months` months
(default 13). The estimate is left out when no rate is configured, and it is not an invoice. The meter
only reports a new maximum after the quarter-hour has ended, so this arrives after the peak is set. It
works during a price outage. When you get one, nothing needs fixing; note which appliance caused it so
you can avoid repeating it this month. A failed send is retried on the next cycle.

The last mailed month and peak are kept in `<ha-config>/battery_planner/state/peak_alert.json`. If
that file is lost, you may get one extra e-mail for an old peak after a restart.

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

To get usage history, and with it grid charging, configure either a `load` counter, or `solar` together
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
| `forecast_solar_kwh` | The forecast for the block when it started; `null` if the forecast was missing |
| `consumption_price`, `injection_price` | Prices of the block, as known when it started |
| `soc_percent` | Charge at the start; `null` while the charge is a stub |
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

The planner never deletes history. To keep one year:

```bash
find <ha-config>/battery_planner/history -name 'blocks-*.jsonl' -mtime +365 -delete
```

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
(lowercase letters, digits, underscore). Copy `inverter_logging.py` and replace the bodies. Skeleton
for a hypothetical `alphaess`:

```python
# pyscript/modules/inverter_alphaess.py
SOC_IS_STUB = False            # optional, default False. True = the charge is a placeholder


def send(action, target_power_kw):
    """action: "charge" | "discharge" | "export" | "idle". Power in kW, >= 0, 0.0 when idle.
    Return True when the inverter accepted it. Return False or raise when it did not."""
    ...  # talk to your inverter here


def read_charge_percent():
    """Battery charge, 0 to 100. Anything else counts as a failed reading."""
    ...
```

Then set `inverter.type: alphaess`, restart Home Assistant and watch the log. Any driver other than
`logging` sends real commands to a real inverter, and nothing in this project is tested against
hardware. You are responsible for the driver you enable.

What the framework does for you:

- It writes the decision line before calling your driver. A driver that raises, times out, returns
  `False` or is missing never changes that line and never crashes the planner or the guard.
- It runs your code in an executor thread, so blocking network calls are fine.
- It gives each call 10 seconds, then treats it as failed and logs it.
- It marks decisions `soc_stubbed` while `SOC_IS_STUB` is true.
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
- [ ] Make commands idempotent. `send("discharge", 2.5)` may arrive again unchanged. If your inverter
      needs a periodic re-send to hold its mode, the planner's every-cycle calls provide it; the
      guard's change-only calls do not.
- [ ] Decide what the inverter does when commands stop. A failed `send` is only logged, not retried.
- [ ] Do not combine a real driver with the capacity-tariff guard yet. The guard reads net grid
      offtake, which its own discharge lowers, and the commanded power is not added back, so the
      shave would switch on and off repeatedly.
- [ ] Run with `logging` first and compare a week of decisions with what the battery should have done.
- [ ] Return the real charge from `read_charge_percent` and leave `SOC_IS_STUB` unset.

The decision logic in `pyscript/modules/` contains no Home Assistant code and is tested without it:

```bash
python3.12 -m pip install pytest
python3.12 -m pytest tests/ -v
```

If you fork, do not publish `secrets.yaml` or `battery_planner/user_config.yaml`, and check with
`git check-ignore -v` that your `.gitignore` covers them.

## Known gaps

- No inverter driver is shipped except `logging`, so nothing controls a battery.
- Battery charge is a fixed 50 % until a driver reads the real value.
- Grid charging stays off until a household load source is configured.
- Peak protection is reactive. The projection covers battery charge, not grid offtake, so the planner
  does not hold charge back for a foreseeable evening peak.
- The peak guard does not add its own commanded discharge back to the offtake reading (see the driver
  checklist).
- Single solar plane, free forecast tier only, Flemish capacity tariff only, battery only.
- The alert e-mails and the entity names on installs other than the original one have not been tested
  against a live Home Assistant. Send a test mail (install step 8) and check the entity names as
  described above.
- `solar_realisation_ratio` in `pyscript/modules/history.py` (measured over forecast solar) is
  implemented and tested but not applied to any decision. The recorded prices, battery and split
  fields are for later analysis.

To adapt the planner: provider prices are `prices.*` in `user_config.yaml`; the capacity-tariff logic
is in `pyscript/modules/capacity.py`; usage history is read by `read_usage_history` in
`pyscript/battery_planner.py`.

## Licence

MIT, see [LICENSE](LICENSE). If you build on this, a reference to the original repository,
<https://github.com/Ivoos0/solar>, is appreciated.
