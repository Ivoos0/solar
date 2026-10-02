# Quickstart: Price-Aware Load Automation

**Mission**: `price-aware-load-automation-01M3PH8Y`

Two audiences: running the tests on this Windows machine, and deploying to Home Assistant on the NAS. The tests need no Home Assistant at all — that is the point of the pure core.

---

## Running the tests (Windows, no Home Assistant needed)

```powershell
py -3.12 -m pip install pytest
py -3.12 -m pytest tests/ -v
```

That is the whole toolchain. No virtualenv, no requirements file, no coverage tooling — the core has zero third-party dependencies, so there is nothing to isolate.

`tests/` imports directly from `pyscript/modules/`. Those modules contain no Home Assistant imports, no pyscript decorators, and no file I/O, so CPython loads them unmodified (pytest imports them; in Home Assistant the adapters load them natively with an executor `importlib` loader).

**This is the only validation this mission has.** There is no inverter to command, the battery reading is stubbed, and real weather takes days to produce an interesting case. If the tests pass and the fixtures are honest, the planner is as verified as it can be before hardware exists.

---

## Deploying to Home Assistant (NAS)

### 1. Install pyscript

Through HACS → Integrations → search "pyscript" → install. Restart Home Assistant. **Record the installed version** — pinning it makes a future upgrade a deliberate act (`research.md` R-08).

HACS works on Container because it is a custom component, not an add-on. Add-ons need Supervisor, which Container does not have.

### 2. Add to `configuration.yaml`

```yaml
pyscript:
  allow_all_imports: true      # adapter needs yaml and json
  hass_is_global: true

rest:
  - resource: "https://api.forecast.solar/estimate/51.12/3.85/50/-10/8.1"
    scan_interval: 3600        # hourly: inside the free-tier rate budget
    sensor:
      - name: "Forecast Solar Estimate"
        value_template: "{{ value_json.result.watt_hours_day.values() | list | first }}"
        json_attributes_path: "$.result"
        json_attributes:
          - watt_hours_period
        unit_of_measurement: "Wh"
```

Path parameters are `lat/lon/declination/azimuth/kwp`. Azimuth is **0 = south, negative = east**, so `-10` is ten degrees east of south. Keep these in step with `user_config.yaml` — the REST URL and the config file must describe the same roof.

The built-in Forecast.Solar integration is **not** used: it is UI config-flow only with no YAML form, which C-005 forbids, and it exposes aggregates rather than the per-block series the trajectory needs (`research.md` R-03).

### 3. Copy the files

| From (repo) | To (NAS, under HA config) | On each copy |
|---|---|---|
| `pyscript/` | `<config>/pyscript/` | **overwrite** |
| `battery_planner/user_config.example.yaml` | `<config>/battery_planner/` | overwrite (the example only) |
| — | `<config>/battery_planner/user_config.yaml` | **never touch** |
| — | `<config>/battery_planner/decisions-YYYY-MM-DD.log` | **never copy** (one file per local day; nothing is deleted automatically) |
| — | `<config>/battery_planner/cache/` | **never copy** |

Do not copy `tests/` or `kitty-specs/` to the NAS — they are development artifacts.

### 4. Create your config, once

```bash
cd <config>/battery_planner
cp user_config.example.yaml user_config.yaml
# edit user_config.yaml: capacity_kwh and alerts.address have no defaults
```

From here on, copying the repo over the top is safe. `user_config.yaml` is gitignored and outside the copy set, so it survives.

### 5. Restart and watch

```bash
tail -f <config>/battery_planner/decisions-$(date +%F).log
```

Within five minutes a record should appear. A healthy first record looks roughly like:

```
2026-09-29T14:35:00+02:00 | action=idle | power=0.00kW | soc=50.0%/5.00kWh | ...
  | vetoes=none | selector=S6 | why="..." | degraded=soc_stubbed
```

`degraded=soc_stubbed` is **expected** — the battery reading is stubbed for this entire mission (C-003). Its disappearance later is the signal that real hardware reading has landed.

---

## Checking it is behaving

| Question | How |
|---|---|
| Is it running at all? | A new record every 5 minutes; 288/day (SC-001) |
| Are prices being derived right? | `cons` and `inj` in the record against the ENTSO-e sensor × your coefficients |
| Is the trajectory sane? | `saturation` and `spill` against a sunny day you can see |
| Is the cache working? | Delete `cache/`; the next record must be identical in action and selector (SC-013) |
| Does a forecast outage degrade gracefully? | Break the REST URL; records continue, `degraded` gains the zero-solar marker, no halt (SC-008) |
| Does a price outage halt and alert? | Disable the ENTSO-e integration; a HALT record and one email, not 288 (SC-007, NFR-003) |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No records at all | pyscript not loaded, or the `pyscript:` block missing from `configuration.yaml` |
| `ImportError` in the HA log | `allow_all_imports: true` missing — the adapter needs `yaml` and `json` |
| HA log warns about blocking I/O | A file operation ran on the event loop: it must use `@pyscript_executor` (or `@pyscript_compile` + `task.executor`); `@pyscript_compile` alone is not enough (`research.md` R-02) |
| Forecast always zero | REST sensor failing — check the URL, and whether the free-tier rate limit was exceeded |
| Prices always missing → constant HALT | ENTSO-e attribute key names differ on this install; confirm against the live entity |
| Config edits have no effect | Editing the repo copy instead of `<config>/battery_planner/user_config.yaml` on the NAS |
