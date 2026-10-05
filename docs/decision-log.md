# Decision log format

The planner and the peak guard write one line per evaluation to
`<ha-config>/battery_planner/decisions-YYYY-MM-DD.log`. Nothing else is
recorded about what the planner decided, so the line carries everything needed
to understand it. The file holds one local calendar day (in your configured
`timezone`), is only ever appended to, and can be read with `tail`, `grep` or a
text editor. Files older than `retention.keep_days` (default 90) are deleted by
the daily cleanup at 03:30 local time.

The decision is written by the inverter boundary before any driver is called
(see [inverter-boundary.md](inverter-boundary.md)). The labels `V1` to `V7` and
`S0` to `S6` are explained in the README under
[Labels in the decision log](../README.md#labels-in-the-decision-log). There is
no `S2`: storing surplus solar is the inverter's own default, so no rule is
needed, and the other labels keep their numbers.

## Format

Key-value pairs on a single line, separated by ` | `. Wrapped here for reading:

```
2026-09-29T14:35:00+02:00 | action=export | power=2.50kW | soc=78.0%/7.80kWh |
  cons=0.2140 | inj=0.1890 | solar_rem=11.20kWh | usage_rem=14.60kWh |
  saturation=2026-09-29T12:45:00+02:00 | spill=2.40kWh | breach=none |
  end_soc=2.10kWh | took=84ms | avg=1.20kW | ceiling=2.50kW | budget=1.95kW |
  vetoes=none | selector=S3 |
  why="leftover 4.1kWh with saturation at 12:45; best injection price before saturation" |
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
| `budget` | `N.NNkW` or `n/a` | Grid power still available for charging in this quarter-hour, after the household's draw (measured with `battery.power_sensor`: offtake plus battery discharge; else offtake minus the planner's own grid charge) and capped at the charge limit (`battery.max_charge_sensor` when readable, else `battery.max_charge_kw`), measured against `capacity_tariff.stay_under_percent` of the ceiling (80 % of 2.5 kW = 2.0 kW by default). Negative when the window is already over that level. `0.00kW` in the last `timing.evaluation_interval_minutes` of the quarter-hour (grid charging stops one evaluation interval before it ends) |
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
`V4(suppressed S1 charge),V4(suppressed S5 charge)`. A record that shows a veto
always also shows the selector that finally fired.

| Label | Forbids | Fires when |
|---|---|---|
| V1 | export | Charge is at or below `battery.reserve_percent` |
| V2 | export | The injection price is negative |
| V3 | grid charging | No capacity budget is left in this quarter-hour |
| V4 | grid charging, export | There is no usable usage history |
| V5 | discharge | The battery is empty (0 % charge) |
| V6 | grid charging, export | There is no solar forecast (`solar_zero_fallback`, no usable cache) |
| V7 | grid charging, export | No battery reading: `battery.soc_sensor` is set but unreadable (`soc_unavailable`). V1 and V5 need the reading and are skipped, so the peak guard may still shave; V7 forbids neither discharge nor peak shaving |

V4 fires on every cycle until the energy history holds a known household load
(see the README section "Energy history"), so it appears bare on most records
until then, and as `V4(suppressed ...)` whenever a price-driven selector (S1 to
S5) would have acted. V4 does not stop the S0 peak shave, and neither does V1:
only V5 can stop a peak shave, shown as `V5(suppressed S0 discharge)`.

## Degraded markers

`degraded=` lists everything the decision had to work around:

| Marker | Meaning |
|---|---|
| `soc_stubbed` | The battery charge is the 50 % placeholder of the `logging` driver. Absent when `battery.soc_sensor` supplies the charge |
| `battery_limits_fallback` | `battery.max_charge_sensor` or `battery.max_discharge_sensor` is set but cannot be read (unknown, unavailable, not a number, a unit other than W or kW, or 0 or less). The numeric `battery.max_charge_kw` / `max_discharge_kw` apply for that direction on that record |
| `battery_power_unavailable` | `battery.power_sensor` is set but cannot be read (unknown, unavailable, not a number, or a unit other than W or kW). The budget then uses the older estimate: meter offtake minus the planner's own grid charge |
| `soc_unavailable` | `battery.soc_sensor` is set but cannot be read (unknown, unavailable, not a number or outside 0 to 100). The charge is not guessed: V7 holds grid charging and export. Replaces `soc_stubbed` on that record |
| `solar_zero_fallback` | The forecast was unavailable and no usable cached copy exists, so solar was treated as zero. V6 then holds grid charging and export |
| `cache_age_solar=3h12m`, `cache_age_usage=...` | A cached series was used, with its age |
| `forecast_age=1h20m` | The forecast was used, but its sensor last refreshed 75 minutes or more ago. A stamp older than `timing.solar_cache_stale_minutes` counts as a failed forecast instead |
| `usage_samples=N` | The usage profile rests on N days of history, fewer than `usage.history_weeks` x 7 |
| `solar_ratio=0.83` | The forecast of the current or next block that has solar was multiplied by this ratio, measured from your own history. Absent when it rounds to 1.00 |
| `solar_ratio_configured=0.80` | The same with `solar.calibration_default`, used while the history is too short or too thin around that time of day. Absent at 1.00 |
| `usage_history_unavailable`, `grid_sensors_unavailable`, `inverter_driver_unavailable`, `inverter_read_failed` | See the marker table in the README under "Check that it works" |

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
