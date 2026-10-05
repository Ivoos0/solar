# Contract: User Configuration File

**Satisfies**: FR-025, FR-037, C-004
**Shipped as**: `battery_planner/user_config.example.yaml` (in this repository)
**Live file**: `battery_planner/user_config.yaml` (NAS only — gitignored, never overwritten)

Every value that differs between installations lives here and nowhere else. The decision logic contains no site constants; changing supplier, battery, or roof must never require editing code (C-004).

## Lifecycle

The repository ships only the `.example` file. First install is one deliberate rename on the NAS:

```
cp battery_planner/user_config.example.yaml battery_planner/user_config.yaml
```

After that, copying the repository over the top is always safe — the live file is not in the copy set and not in git. This is what protects hand-entered coefficients from a careless recursive copy.

## Schema

```yaml
# Provider price coefficients. Consumption and injection differ, and are never
# assumed symmetric. Values below are the ones already in configuration.yaml.
prices:
  consumption_multiplier: 1.07
  consumption_offset: 0.007
  injection_multiplier: 0.94
  injection_offset: -0.011      # negative: injection goes negative while the
                                # market price is still positive
  entity: sensor.entso_prices_average_electricity_price
                                # HA entity carrying the price list; find it in
                                # Developer Tools -> States. Confirmed on a live
                                # entsoe install. Entity ids: domain.object_id
                                # (lowercase letters, digits, underscore).
  attribute: prices             # non-empty attribute name; entries are
                                # {time, price}, time an ISO string (space or T
                                # separator) or datetime, price EUR/kWh

battery:
  capacity_kwh: 10.0            # REQUIRED - no sensible default
  reserve_percent: 10.0         # never export to the grid below (a limit, not a target)
  max_charge_kw: 5.0            # binds per block, independent of capacity
  max_discharge_kw: 5.0
  round_trip_efficiency: 0.90   # gates arbitrage and the solar-hold decision

# The roof (lat/lon/declination/azimuth/kwp) is NOT configured here: it lives
# only in forecast_solar_url in secrets.yaml.
solar:
  forecast_entity: sensor.forecast_solar_estimate   # unconfirmed on the live install
  forecast_attribute: watt_hours_period

# Belgian capaciteitstarief. Billed on the rolling average of the last twelve
# monthly peaks, floored at 2.5 kW, on grid offtake only.
capacity_tariff:
  enabled: true
  billing_floor_kw: 2.5         # no saving below this - do not shave past it
  guard_interval_seconds: 30    # how often the peak guard re-evaluates
  stay_under_percent: 80        # > 0 and <= 100. Grid CHARGING must stay under
                                # this percentage of the ceiling (the month peak,
                                # floored at billing_floor_kw): 80 with the 2.5 kW
                                # floor means never charge from the grid if the
                                # quarter-hour looks like passing 2.0 kW. Applies
                                # only to grid charging; peak shaving still
                                # defends the real ceiling. 100 = charge right up
                                # to the ceiling.
  quarter_hour_average_mode: auto
                                # auto | running | accumulating
                                # Whether the meter's 1-0:1.4.0 reports a true
                                # running average (energy / elapsed) or an
                                # accumulating one (energy / full 15 min).
                                # They differ by 15x one minute into a window.
                                # 'auto' detects it at runtime and logs the
                                # conclusion; pin it once you know.
  offtake_sensor: sensor.slimmelezer_power_consumed
                                # the meter's NETTED three-phase total.
                                # Never sum the per-phase sensors: on this
                                # connection one phase exports while others
                                # import, so a per-phase sum reads 0.937 kW
                                # where the meter reads 0.003 kW.
  quarter_hour_average_sensor: sensor.slimmelezer_huidig_kwartiervermogen
  month_peak_sensor: sensor.slimmelezer_maandpiek
                                # The peak guard reads all three from config,
                                # but its @state_trigger names
                                # sensor.slimmelezer_power_consumed literally
                                # (decorator arguments are static). With a
                                # different offtake sensor the guard runs on its
                                # 30 s tick only.

# Expected household consumption per block, averaged from recent history.
usage:
  history_weeks: 4              # trailing window
  grouping: same_weekday        # same_weekday | day_type
                                # same_weekday: a Wednesday block = mean of the
                                #   last 4 Wednesdays (faithful to routines)
                                # day_type: a Wednesday block = mean over every
                                #   weekday in the window; Saturday and Sunday
                                #   pool as weekend days (smoother, fills faster)
  recency_weighting: linear     # linear | none
                                # linear: newer weeks count more, so changes in
                                #   routine show up sooner. With history_weeks 4
                                #   the last week weighs 4, the week before 3,
                                #   then 2, then 1 (weight = history_weeks -
                                #   weeks ago; missing weeks just drop out)
                                # none: plain mean, every day counts equally

# Energy history: the planner records its own 15-minute energy history.
# Counters must be CUMULATIVE energy in kWh (Wh and MWh are converted).
# Several counters per quantity are summed (tariff 1 + tariff 2). Every list
# is optional; an empty list means "not available" and that quantity is
# recorded as null. See the README, section "Energy history".
history:
  enabled: true
  sensors:
    import:                     # energy taken from the grid
      - sensor.slimmelezer_energy_consumed_tariff_1
      - sensor.slimmelezer_energy_consumed_tariff_2
    export:                     # energy injected into the grid
      - sensor.slimmelezer_energy_produced_tariff_1
      - sensor.slimmelezer_energy_produced_tariff_2
    solar: []                   # PV production counter(s), e.g. from the inverter
    battery_charge: []          # energy into the battery
    battery_discharge: []       # energy out of the battery
    load: []                    # household consumption counter, if you have one
                                # Usage history (and so grid charging) unlocks
                                # with `load`, or with solar + battery_charge +
                                # battery_discharge all configured.

timing:
  block_minutes: 15
  evaluation_interval_minutes: 5
  forecast_retry_minutes: 10    # on failure
  solar_cache_stale_minutes: 120
  usage_cache_stale_minutes: 2880

alerts:
  address: you@example.com      # REQUIRED
  notify_service: battery_alert   # HA notify service name (notify.<name>); default battery_alert
  realert_minutes: 60           # not per cycle - that would be 288 emails/day
  peak_enabled: true            # notice AFTER: month peak (meter 1-0:1.6.0) above capacity_tariff.billing_floor_kw
  peak_warning_enabled: true    # warning BEFORE: this quarter-hour is projected above the ceiling (peak guard)
  peak_warning_min_interval_minutes: 60  # integer >= 1; at most one warning per interval (and per quarter-hour)
  peak_warning_ticks: 2         # integer >= 1; consecutive guard evaluations over the ceiling before warning

inverter:
  type: logging                 # selects pyscript/modules/inverter_<type>.py; default logging
                                # (log only). missing, null or "none" mean logging
  resend_minutes: 15            # integer >= 0; an UNCHANGED command is sent to the driver again
                                # only after this many minutes; 0 = send on every call

timezone: Europe/Brussels
```

## Validation

Checked at load. A failure is a **startup error, not a degraded cycle** — bad configuration must not quietly produce plausible-looking decisions.

| Rule | Why |
|---|---|
| `capacity_kwh > 0` | Every energy figure scales from it |
| `0 <= reserve_percent < 100` | 100 would leave nothing usable |
| `0 < round_trip_efficiency <= 1` | Above 1 would make arbitrage always profitable |
| `block_minutes` divides 60 | Blocks must align to hourly price and forecast periods |
| `address` non-empty | A halt with nowhere to alert is a silent failure |
| `notify_service` matches `[a-z0-9_]+` | It is the service name under the `notify` domain; a dotted, spaced or upper-case value can never resolve. It does not join the cache fingerprint |
| `inverter.type` matches `[a-z0-9_]+`; missing, null or `none` become `logging` | It names the driver file `inverter_<type>.py`, so path separators, dots, spaces and upper case can never select a file outside the modules directory. It does not join the cache fingerprint. Whether the driver file exists is checked at run time, not here: a missing driver falls back to log-only behaviour with an error and a degraded marker (see `inverter-boundary.md`) |
| `inverter.resend_minutes` is an integer `>= 0` (no booleans, no floats, no negatives); default 15 | It is the command de-duplication window (`inverter-boundary.md`). A typo must not silently turn de-duplication off or make a command wait for hours. It does not join the cache fingerprint |
| `alerts.peak_enabled` and `alerts.peak_warning_enabled` are booleans; `alerts.peak_warning_min_interval_minutes` and `alerts.peak_warning_ticks` are integers `>= 1` (no booleans, no floats) | A typo must not silently disable or flood a mail. None of them joins the cache fingerprint |
| `max_charge_kw > 0`, `max_discharge_kw > 0` | Zero would make the trajectory meaningless |
| `history.enabled` is a boolean; each `history.sensors.<quantity>` is a list of entity ids (`domain.object_id`) without duplicates; only the quantities `import`, `export`, `solar`, `battery_charge`, `battery_discharge`, `load` are accepted | A typo must not silently disable a counter. The `history` section does not join the cache fingerprint (it names where data is read from) |

## Cache invalidation

Changing `solar.*`, `timing.block_minutes`, `usage.history_weeks`, `usage.grouping` or `usage.recency_weighting` changes what a cached block *means*, so cached series built under the old values are discarded (FR-037). (The usage settings only affect the usage profile, but they share the fingerprint for simplicity; a rare change costs one extra solar refetch.) The cache stores a fingerprint of exactly these fields; a mismatch is a miss, not an error.

## Secrets

The alert address and the notify service name are not secrets. Email **credentials** are not stored here — alerting goes through Home Assistant's notify service (`alerts.notify_service`, default `battery_alert`, an SMTP notifier defined in `configuration.yaml`), keeping credentials in `secrets.yaml`, which `.gitignore` already excludes (NFR-007). The tracked `secrets.example.yaml` holds placeholders only.
