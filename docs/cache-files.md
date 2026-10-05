# Cache files

`<ha-config>/battery_planner/cache/` holds two derived series so each cycle only
does the work that changed:

| File | Rebuilt | Why cached |
|---|---|---|
| `solar.json` | Hourly, when a new forecast arrives | Cheap to rebuild; saves re-parsing the sensor every cycle |
| `usage.json` | Daily | Expensive: about 672 quarter-hour buckets averaged over the trailing window (four weeks by default) |

The battery projection is never cached, because it depends on the live battery
charge. The files are safe to delete: the next cycle rebuilds them and reaches
the same decision. The installation steps never copy anything into or out of the
cache folder.

## Format

```json
{
  "kind": "solar",
  "computed_at": "2026-09-29T14:00:00+02:00",
  "source": "sensor.forecast_solar_estimate",
  "config_fingerprint": "a3f1c8e2",
  "blocks": [
    {"block_start": "2026-09-29T14:00:00+02:00", "expected_kwh": 0.455,
     "source_resolution_minutes": 60},
    {"block_start": "2026-09-29T14:15:00+02:00", "expected_kwh": 0.455,
     "source_resolution_minutes": 60}
  ]
}
```

An hourly forecast of 1.82 kWh is divided over its four quarter-hour blocks
(0.455 each), because energy adds up over time. An hourly price is repeated
instead. Source data is never interpolated; `source_resolution_minutes` records
how coarse the source was.

| Field | Meaning |
|---|---|
| `kind` | `solar` or `usage`; a file read as the wrong kind is discarded |
| `computed_at` | When the series was built, so a file written yesterday never looks fresh |
| `source` | Where the data came from |
| `config_fingerprint` | Hash of the settings that change what a block means (array geometry, location, block length) |
| `blocks` | The series itself |

The solar series is always the raw forecast. The solar calibration ratio is
applied on top every cycle, so changing it needs no cache rebuild.

## When a file is used, refreshed or discarded

- Older than `timing.solar_cache_stale_minutes` or `timing.usage_cache_stale_minutes`:
  the series is rebuilt. If that is impossible (forecast unreachable, history
  unavailable) the stale series is used and the record carries
  `cache_age_solar=...` or `cache_age_usage=...` in `degraded`.
- Discarded and rebuilt: `computed_at` is from a previous local day, the
  fingerprint does not match the current settings, or the file is missing,
  unreadable or does not parse. None of these is an error.

A stale forecast still beats no plan; only missing price data stops the planner.
