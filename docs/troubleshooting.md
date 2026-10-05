# Check that it works

How to see that the planner is running, how to read a decision record, and what to do when something is off. Back to the [README](../README.md).

A record appears in today's log within five minutes:

```bash
tail -f <ha-config>/battery_planner/decisions-$(date +%F).log
```

Each file holds one local calendar day, in your configured `timezone`. The planner, the peak guard
and the HALT / RECOVERED / SKIP lines all write to it. Old files are deleted automatically, see
[Daily cleanup](history-and-reports.md#daily-cleanup).

A normal record is one line of `|`-separated fields: timestamp, `action`, `power`, `soc`, `cons` and
`inj` (prices), forecast and usage remaining, `saturation`, `spill`, `breach`, `end_soc`, `took`,
`avg`, `ceiling`, `budget`, `vetoes`, `selector`, `why` (the reason in words; `vetoes` and `selector` use the labels explained under [Labels in the decision log](decision-log.md#labels-in-the-decision-log)), `degraded` and
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
| `solar_ratio=0.83` | The forecast for the current or next block was multiplied by this ratio, measured from your own history (see [Solar calibration](history-and-reports.md#solar-calibration)). Absent when the ratio is 1.00 |
| `solar_ratio_configured=0.80` | The same, but the ratio is `solar.calibration_default` because there is not enough measured history yet. Absent when it is 1.00 |
| `solar_zero_fallback` | The forecast was unavailable (or older than `timing.solar_cache_stale_minutes`) and no usable cached copy exists, so solar was treated as zero. The planner holds (no grid charging, no export) until a forecast is back, retries, and does not halt |
| `grid_sensors_unavailable` | Capacity logic is on but the grid sensors are unreadable. Grid charging is suppressed |
| `inverter_driver_unavailable`, `inverter_read_failed` | The configured driver is missing or its charge reading failed |

When the peak guard is shaving a peak it writes its own records: when a shave starts, changes
materially, is blocked or stops, not once per 30-second tick.

## Troubleshooting

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

If the AlphaESS sensors show `unavailable`, see [If the sensors are unavailable](alphaess.md#if-the-sensors-are-unavailable).
