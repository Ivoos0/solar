# Decision log format

The format of the decision log: one line per planner or peak guard evaluation. Back to the [README](../README.md).

**Contents**

- [Format](#format)
- [Fields](#fields)
- [Vetoes](#vetoes)
- [Degraded markers](#degraded-markers)
- [HALT record](#halt-record)
- [Labels in the decision log](#labels-in-the-decision-log)

The planner and the peak guard write one line per evaluation to
`<ha-config>/battery_planner/decisions-YYYY-MM-DD.log`. Nothing else is
recorded about what the planner decided, so the line carries everything needed
to understand it. The file holds one local calendar day (in your configured
`timezone`), is only ever appended to, and can be read with `tail`, `grep` or a
text editor. Files older than `retention.keep_days` (default 90) are deleted by
the daily cleanup at 03:30 local time.

The decision is written by the inverter boundary before any driver is called
(see [inverter-boundary.md](inverter-boundary.md)). The labels `V1` to `V8` and
`S0` to `S6` are explained under
[Labels in the decision log](#labels-in-the-decision-log) below.

## Format

Key-value pairs on a single line, separated by ` | `. Wrapped here for reading:

```
2026-09-29T14:35:00+02:00 | action=export | power=2.50kW | soc=78.0%/7.80kWh |
  cons=0.2140 | inj=0.1890 | solar_rem=11.20kWh | usage_rem=14.60kWh |
  saturation=2026-09-29T12:45:00+02:00 | spill=2.40kWh | breach=none |
  end_soc=2.10kWh | took=84ms | avg=1.20kW | ceiling=2.50kW | budget=1.95kW |
  vetoes=none | selector=S3 |
  why="spill ahead 2.40 kWh, saturation at 12:45; injection now 0.1890 EUR/kWh beats the other 6 priced blocks in the window after round-trip losses; 0.60 kWh is not needed before the battery refills: export at 2.50 kW" |
  degraded=soc_stubbed | source=planner
```

Every field appears in every record, in this fixed order: timestamp, action,
power, soc, cons, inj, solar_rem, usage_rem, saturation, spill, breach, end_soc,
took, avg, ceiling, budget, vetoes, selector, why, degraded, source.

A value that does not apply is written explicitly (`none`, `n/a`, or a computed
`0.00kWh`), never left out, so a missing field and a zero field never look alike.

## Fields

| Field | Format | Meaning |
|---|---|---|
| timestamp | ISO 8601 with offset | Local time in your configured `timezone` |
| `action` | `charge`, `discharge`, `export` or `idle` | What was decided |
| `power` | `N.NNkW` | `0.00kW` when idle |
| `soc` | `N.N%/N.NNkWh` | Battery charge as a percentage (what the inverter reports) and in kWh (what the rules use) |
| `cons`, `inj` | 4 decimals, EUR/kWh | Consumption and injection price of the current block, after your price formulas. `n/a` when the block has no market price |
| `solar_rem`, `usage_rem` | `N.NNkWh` or `n/a` | Forecast solar and expected usage over the rest of the projection. `n/a` for records from the peak guard, which has no projection |
| `saturation` | ISO 8601 or `none` | When the battery is projected to be full |
| `spill` | `N.NNkWh` or `n/a` | Solar energy projected to be wasted because the battery is full. `0.00kWh` when none; `n/a` for the guard |
| `breach` | ISO 8601 or `none` | When the battery is projected to reach the reserve |
| `end_soc` | `N.NNkWh` | Projected charge at the end of the projection |
| `took` | `NNNms` | How long the cycle took |
| `avg` | `N.NNkW` or `n/a` | Running quarter-hour average of grid offtake. `n/a` when capacity handling is off |
| `ceiling` | `N.NNkW` or `n/a` | The peak level being defended: the larger of 2.5 kW and this month's peak |
| `budget` | `N.NNkW` or `n/a` | Grid power still available for charging in this quarter-hour, after the household's draw (measured with `battery.power_sensor`: offtake plus battery discharge) and capped at the charge limit (`battery.max_charge_sensor` when readable, else `battery.max_charge_kw`), measured against `capacity_tariff.stay_under_percent` of the ceiling (80 % of 2.5 kW = 2.0 kW by default). Negative when the window is already over that level. `0.00kW` in the last `timing.evaluation_interval_minutes` of the quarter-hour (grid charging stops one evaluation interval before it ends) |
| `vetoes` | comma-separated or `none` | Rules that fired, and what each suppressed (see below) |
| `selector` | `S0` to `S6` | The action that was chosen |
| `why` | quoted text | The values that made the condition true |
| `degraded` | comma-separated or `none` | Inputs that were missing, stale or replaced (see below) |
| `source` | `planner` or `guard` | Which loop wrote the record. Always the last field |

Free text (each `vetoes` and `degraded` entry, the HALT cause) cannot contain
`|` or a line break, and numbers must be finite. A record that breaks this is
rejected rather than written.

## Vetoes

A veto that stopped a proposal names itself and what it stopped:

```
# S3 wanted to export but V2 forbade it, and nothing else applied
vetoes=V2(suppressed S3 export) | selector=S6 |
  why="injection -0.0043 forbids export; no spill or breach ahead, holding"

# several vetoes block the same proposal: joined with +
vetoes=V1+V2(suppressed S3 export) | selector=S6 | ...

# a veto fired but blocked nothing: written bare
vetoes=V2 | selector=S6 |
  why="hold: nothing applies, the inverter keeps its default behaviour - ..."
```

Several suppressed proposals are separate entries, for example
`V4(suppressed S1 charge),V4(suppressed S3 export)`. A record that shows a veto
always also shows the selector that finally fired.

What each label forbids, and when it fires, is listed under
[Labels in the decision log](#labels-in-the-decision-log).

V4 fires on every cycle until the energy history holds a known household load
(see [Energy history](history-and-reports.md#energy-history)), so it appears bare on most records
until then, and as `V4(suppressed ...)` whenever a price-driven selector (S1, S3 or
S4) would have acted. V4 does not stop the S0 peak shave, and neither does V1:
only V5 can stop a peak shave, shown as `V5(suppressed S0 discharge)`.

## Degraded markers

`degraded=` lists everything the decision had to work around:

| Marker | Meaning |
|---|---|
| `soc_stubbed` | The battery charge is the 50 % placeholder of the `logging` driver. Absent when `battery.soc_sensor` supplies the charge |
| `battery_reserve_fallback` | `battery.reserve_sensor` is set but cannot be read (unknown, unavailable, not a number, or outside 0 to below 100). `battery.reserve_percent` applies on that record |
| `battery_limits_fallback` | `battery.max_charge_sensor` or `battery.max_discharge_sensor` is set but cannot be read (unknown, unavailable, not a number, a unit other than W or kW, or 0 or less). The numeric `battery.max_charge_kw` / `max_discharge_kw` apply for that direction on that record |
| `soc_last_good`, `battery_power_last_good` | The reading failed this cycle; the last good one (at most two evaluation intervals old) was used. See the table under [Check that it works](troubleshooting.md#check-that-it-works) |
| `battery_power_not_configured` | The capacity tariff is on but `battery.power_sensor` is not set. V8 holds grid charging and export |
| `battery_power_unavailable` | `battery.power_sensor` is set but cannot be read (unknown, unavailable, not a number, or a unit other than W or kW). While the capacity tariff is on, V8 holds grid charging and export |
| `soc_unavailable` | `battery.soc_sensor` is set but cannot be read (unknown, unavailable, not a number or outside 0 to 100). The charge is not guessed: V7 holds grid charging and export. Replaces `soc_stubbed` on that record |
| `solar_zero_fallback` | The forecast was unavailable and no usable cached copy exists, so solar was treated as zero. V6 then holds grid charging and export |
| `cache_age_solar=3h12m`, `cache_age_usage=...` | A cached series was used, with its age |
| `forecast_age=1h20m` | The forecast was used, but its sensor last refreshed 75 minutes or more ago. A stamp older than `timing.solar_cache_stale_minutes` counts as a failed forecast instead |
| `consumption_offset_low` | `prices.consumption_offset` is below 0.05 EUR/kWh: a bare supplier coefficient without network costs, taxes and VAT. See the table under [Check that it works](troubleshooting.md#check-that-it-works) |
| `usage_gaps_pct=N` | N percent of the planned blocks have fewer than `usage.min_bucket_days` days of history behind them (the profile has holes or thin spots). Those blocks are filled from their neighbours; the number shrinks as history builds up |
| `usage_samples=N` | The usage profile rests on N days of history, fewer than `usage.history_weeks` x 7 |
| `solar_ratio=0.83` | The forecast of the current or next block that has solar was multiplied by this ratio, measured from your own history. Absent when it rounds to 1.00 |
| `solar_ratio_configured=0.80` | The same with `solar.calibration_default`, used while the history is too short or too thin around that time of day. Absent at 1.00 |
| `quarter_hour_average_stale` | The quarter-hour average has not been written in this window and is high; see the table under [Check that it works](troubleshooting.md#check-that-it-works) |
| `usage_history_unavailable`, `grid_sensors_unavailable`, `inverter_driver_unavailable`, `inverter_read_failed` | See the marker table under [Check that it works](troubleshooting.md#check-that-it-works) |

The daily report counts the two `solar_ratio` markers under their name without
the value.

## HALT record

When price data is missing no decision is produced, but the cycle is not silent:

```
2026-09-29T14:35:00+02:00 | HALT | cause=price_data_unavailable |
  entered=2026-09-29T14:20:00+02:00 | alerted=2026-09-29T14:20:00+02:00
```

`alerted=none` means no e-mail was sent for this outage yet. `RECOVERED` and
`SKIP` lines are written to the same file.

## Labels in the decision log

The decision log prints short labels for the rules and actions. These are the labels the log prints, and nothing else in the documentation uses them: elsewhere the same
rules are described in words. The field-by-field format of a log line, including the `degraded=`
markers, is described in the sections above. A rule that forbids an action is called a veto and
shows under `vetoes=`; an action the planner can pick is called a selector and shows under `selector=`.

Vetoes:

| Label | Forbids | When |
|---|---|---|
| V1 | export | Charge is at or below `battery.reserve_percent`. Only exporting to the grid is forbidden; peak shaving may still use charge below it |
| V2 | export | The injection price is negative |
| V3 | grid charging | No capacity budget is left in this quarter-hour |
| V4 | grid charging, export | There is no usable usage history. Peak shaving (S0) is not affected |
| V5 | discharge | The battery is empty (0 % charge). The planner cannot know your inverter's own minimum charge, so this is the only lower limit it applies to peak shaving |
| V6 | grid charging, export | There is no solar forecast: it is missing or too old and there is no usable cached copy (`degraded=solar_zero_fallback`). Peak shaving (S0) is not affected. A stale but usable cached forecast does not trigger it |
| V8 | grid charging, export | A sensor the budget needs is missing while the capacity tariff is on: the grid sensors are unreadable (or the quarter-hour average is stale), `battery.power_sensor` is not set (`degraded=battery_power_not_configured`) or cannot be read (`degraded=battery_power_unavailable`). Without the household draw the budget cannot be completed, so nothing is bought or exported. Peak shaving (S0) is not affected |
| V7 | grid charging, export | `battery.soc_sensor` is set but unreadable (`degraded=soc_unavailable`): there is no battery reading, so nothing is bought or exported. Peak shaving (S0) is not affected. V1 and V5 need a reading and are not checked meanwhile, so the peak guard may still shave. V7 forbids neither discharge nor peak shaving |

Selectors, in the order they are tried:

| Label | Action |
|---|---|
| S0 | Peak shave: discharge to the house when the quarter-hour is heading above the ceiling. Mostly relevant when the planner is holding energy back (see [Peak guard](how-it-works.md#peak-guard)) |
| S1 | Charge from the grid while the consumption price is negative. Never more than the room left in the battery (S4 too) |
| S3 | Export when a spill is projected (the battery would be full and solar lost), only the energy not needed before the battery refills, and only when the injection price now, after round-trip losses, beats every other priced block in the window (equal prices do not qualify). Charge left over at the horizon end is never sold |
| S4 | Charge from the grid when a stored kWh is worth more than it costs, in the cheapest blocks only. Two uses, tried in this order. (1) Import avoided: charging now, price divided by `battery.round_trip_efficiency`, is cheaper than the average buying price (weighted by energy) of the blocks where the house would otherwise import between the first reserve breach and the next refill. The cheapest blocks before the breach that cover that shortage charge. (2) Sale: the best later injection price after round-trip losses beats the price now. The cheapest blocks before that sell block that fill the room left in the battery charge. It needs usage history (`usage.min_history_days` days), like the other price-driven choices. Older logs may show this second use as `S5` |
| S6 | Idle: nothing applies, so the planner cancels any forced mode and the inverter does what it does by default (see [What the planner does](how-it-works.md#what-the-planner-does)) |

There is no S2, and the other labels keep their numbers. Storing surplus solar needs no rule: the inverter's own default does it. By default
the inverter charges the battery from solar surplus until it is full and then exports, and drains it to
serve the house until it is empty and then uses grid power. The planner only steps in when it wants
something different (peak shaving, charging from the grid, exporting); the rest of the time it idles.
