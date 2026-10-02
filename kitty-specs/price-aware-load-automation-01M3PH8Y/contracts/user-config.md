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

battery:
  capacity_kwh: 10.0            # REQUIRED - no sensible default
  reserve_percent: 10.0         # never planned below
  max_charge_kw: 5.0            # binds per block, independent of capacity
  max_discharge_kw: 5.0
  round_trip_efficiency: 0.90   # gates arbitrage and the solar-hold decision

# forecast.solar: 0 = south, negative = east. Verify lat/lon: the API
# resolves to about 10 metres.
solar:
  latitude: 51.12               # the installation site
  longitude: 3.85
  kwp: 8.1                      # 20 panels x 405 Wp
  declination: 50               # from horizontal
  azimuth: -10                  # 10 degrees east of south

# Belgian capaciteitstarief. Billed on the rolling average of the last twelve
# monthly peaks, floored at 2.5 kW, on grid offtake only.
capacity_tariff:
  enabled: true
  billing_floor_kw: 2.5         # no saving below this - do not shave past it
  rate_eur_per_kw_year: 40.0    # CHECK YOUR FLUVIUS BILL - revised annually
  guard_interval_seconds: 30    # how often the peak guard re-evaluates
  stay_under_percent: 80        # > 0 and <= 100. Grid CHARGING must stay under
                                # this percentage of the ceiling (the month peak,
                                # floored at billing_floor_kw): 80 with the 2.5 kW
                                # floor means never charge from the grid if the
                                # quarter-hour looks like passing 2.0 kW. Applies
                                # only to grid charging; peak shaving still
                                # defends the real ceiling. 100 = charge right up
                                # to the ceiling.
  peak_averaging_months: 13     # months in the meter's billed average (>= 1)
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

timing:
  block_minutes: 15
  evaluation_interval_minutes: 5
  forecast_refresh_minutes: 60  # upstream fetch; keep within the free tier
  forecast_retry_minutes: 10    # on failure
  solar_cache_stale_minutes: 120
  usage_cache_stale_minutes: 2880

alerts:
  address: you@example.com      # REQUIRED
  notify_service: gmail_alert   # HA notify service name (notify.<name>); default gmail_alert
  realert_minutes: 60           # not per cycle - that would be 288 emails/day

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
| `-180 <= azimuth <= 180`, `0 <= declination <= 90` | forecast.solar's accepted ranges |
| `address` non-empty | A halt with nowhere to alert is a silent failure |
| `notify_service` matches `[a-z0-9_]+` | It is the service name under the `notify` domain; a dotted, spaced or upper-case value can never resolve. It does not join the cache fingerprint |
| `max_charge_kw > 0`, `max_discharge_kw > 0` | Zero would make the trajectory meaningless |

## Cache invalidation

Changing `solar.*`, `timing.block_minutes`, `usage.history_weeks`, `usage.grouping` or `usage.recency_weighting` changes what a cached block *means*, so cached series built under the old values are discarded (FR-037). (The usage settings only affect the usage profile, but they share the fingerprint for simplicity; a rare change costs one extra solar refetch.) The cache stores a fingerprint of exactly these fields; a mismatch is a miss, not an error.

## Secrets

The alert address and the notify service name are not secrets. Email **credentials** are not stored here — alerting goes through Home Assistant's notify service (`alerts.notify_service`, default `gmail_alert`, an SMTP notifier defined in `configuration.yaml`), keeping credentials in `secrets.yaml`, which `.gitignore` already excludes (NFR-007). The tracked `secrets.example.yaml` holds placeholders only.
