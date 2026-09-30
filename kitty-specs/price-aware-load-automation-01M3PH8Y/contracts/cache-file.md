# Contract: Derived-Series Cache

**Satisfies**: FR-033, FR-035, FR-036, FR-037, FR-038, C-011, SC-013, SC-014
**Location**: `battery_planner/cache/` (NAS only — never copied in either direction)
**Files**: `solar.json`, `usage.json`

Two derived input series are cached so each cycle does only the work that actually changed. The **trajectory is never cached** (FR-034): it depends on live battery charge and on which blocks have elapsed, so caching it would freeze the projection while its inputs moved underneath.

## What is cached, and why these two

| Series | Rebuilt | Cost |
|---|---|---|
| `solar.json` | Hourly, when a new forecast arrives | Cheap — parsing a response |
| `usage.json` | Daily | **Expensive** — ~672 quarter-hour buckets averaged over the trailing window (four weeks by default) |

The usage profile is the one that matters. Caching only the solar forecast would have left the larger cost exactly where it was, rebuilt 288 times a day.

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

Note the equal `expected_kwh` across sub-blocks: an hourly period of 1.82 kWh is **divided** across its four blocks (0.455 each) because energy is extensive, whereas an hourly *price* would be repeated (FR-030). Source data is never interpolated. `source_resolution_minutes` records that honestly, so saturation timing is never read as more precise than the data supports.

## Required fields

| Field | Why it is mandatory |
|---|---|
| `computed_at` | A file outlives the process. Without this, a series written yesterday is on disk at boot looking exactly like a fresh one. This is what makes the cache safe, not an optimisation (FR-035, C-011). |
| `config_fingerprint` | Hash of array geometry, location, and `block_minutes` — the fields that change what a block means (FR-037). |
| `source` | So a decision can say where its inputs came from. |
| `kind` | Guards against a file being read as the wrong series. |

## Staleness

Past `*_cache_stale_minutes` from `computed_at`:

1. Refresh the series.
2. If refresh is impossible — forecast unreachable, history unavailable — **use the stale series and mark the decision degraded**, recording the age in `degraded` (FR-036, SC-014).

Old is not the same as missing. A stale forecast still beats no plan; only missing *price* data is a halt condition (FR-021).

## Invalidation

Discard and rebuild when:

- `computed_at` is from a previous day (FR-037)
- `config_fingerprint` does not match current configuration
- the file is missing, unreadable, or fails to parse — **a cache miss, not a failure**; rebuild and carry on

## Disposability

**Deleting the cache must change no decision** (FR-038). SC-013 makes this measurable: delete it mid-run, and the very next cycle reaches the same action, from the same selector, as the cycle before. If a decision changes, something the cache held was not derivable from its sources — which means it was state, not cache, and belongs elsewhere.

## Split across layers

Validity logic — is this stale, does the fingerprint match, is it from today — is **pure** and lives in `pyscript/modules/cache.py`, fully unit-tested. The file read and write live in the adapter, because `open()` inside pyscript's interpreter must be run off the event loop with `@pyscript_executor` (or `@pyscript_compile` plus `task.executor`; `@pyscript_compile` alone does NOT move work off the loop) (`research.md` R-02). This split is why cache correctness is testable at all.
