# Energy history

How the planner records energy history, calibrates the solar forecast, writes the daily report and deletes old files. Back to the [README](../README.md).

**Contents**

- [Solar calibration](#solar-calibration)
- [Daily report](#daily-report)
- [Daily cleanup](#daily-cleanup)

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
| `soc_percent` | Battery charge (%) at the start of the block; `null` while the charge is unreadable |
| `soc_end_percent` | Battery charge (%) at the end of the block, read with the snapshot that closes it (the next block's start). `null` when that reading is unreadable, and in the `null` records of a gap. Records written before this field existed do not have it |
| `complete` | `true` only if every configured counter was readable at both readings and gave a valid difference |
| `start_read_at`, `snapshot_read_at` | When the counters were actually read |

Derived load = `import - export + solar + battery_discharge - battery_charge`, only when all five terms
are known. The split (`priority_v1`) assumes solar serves the load first, then the battery, then the
grid. The counters are block totals, so this is a convention, not a measurement.

A negative difference (counter reset or replacement) makes that field `null`. A gap of more than one
block (planner stopped, Home Assistant restarted) writes `null` records for the missed blocks, at most
one day of them. A difference is never spread over several blocks, and an unreadable counter makes its
whole quantity `null`.

A counter that reads exactly 0 after it has been above 0 counts as unreadable too: a cumulative counter does not go back to 0, and a sensor that bounces to 0 and then recovers would otherwise put its whole value into one block. The block with the 0 and the block after it are `null` for that quantity (the planner logs a warning); the next block is normal again. The last good reading of each counter is kept in `battery_planner/history/last_snapshot.json` for this, so it also works across an unavailable gap and a restart. A counter that really starts at 0 (a new meter, an export counter that never exported) is accepted. A drop to a small non-zero number is not caught here; it makes that block `null` as a reset, but a recovery from it is not recognised.

The planner runs every `timing.evaluation_interval_minutes`, so a block's counters are read up to one
interval after the boundary. Both read times are in every record.

History files are deleted after `retention.keep_days` days (default 90), see
[Daily cleanup](#daily-cleanup). To keep more, raise that setting. To keep it forever, copy the
`history` folder somewhere else before it expires.

## Solar calibration

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
`sensor.battery_planner_solar_ratio` entity shows the ratio now (see [Sensors](alerts-and-sensors.md#sensors)). Outside
daylight that sensor shows the configured value, because there is nothing to measure.

Choose `solar.calibration_default` as the share of the forecast you normally get, rounded down. Keep
`retention.keep_days` at 7 times `solar.calibration_weeks` or more; the planner refuses to start
otherwise, because the cleanup would delete history the calibration needs. Leave a few days of margin
above that minimum: right after the 03:30 cleanup the history can be a little shorter than the weeks you
asked for, and the configured value then applies until that morning's first measured blocks arrive.

## Daily report

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
- **Battery charge**: only when the history has charge readings and `battery.capacity_kwh` is set. It
  gives the first, last, lowest and highest charge of the day, then compares the battery counters with
  the change in charge over the blocks that have both a start and an end charge. The counters give
  the energy stored (charged minus discharged); the charge gives the same figure as the change in
  percentage times `battery.capacity_kwh`. Both are shown in kWh, with the difference as a percentage
  of the energy that moved (charged plus discharged). A small difference is normal, because the
  battery loses some energy when charging and discharging. When the difference is more than 25 % of the
  energy that moved (and at least 1 kWh moved), the Data quality section says so. That usually means
  `battery.capacity_kwh` is wrong, or a battery counter is wrong or was reset.
- **Blocks recorded**: how many of the expected blocks for that day exist. A normal day has 96; the
  day the clocks go forward has 92 and the day they go back has 100.
- **Data quality**: only present when something is off, for example a missing file, lines that could
  not be read (they are skipped), fewer blocks than expected, or a quantity that is empty in many
  blocks because its counter is not configured or was unreadable.

## Daily cleanup

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
