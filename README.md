# Price-aware battery planner for Home Assistant (Flanders)

A Home Assistant [pyscript](https://github.com/custom-components/pyscript) project that decides,
every five minutes, what a home battery should do: charge, discharge, export or idle. It weighs
dynamic electricity prices (ENTSO-e), a solar forecast (forecast.solar), expected household usage,
and the Flemish capacity tariff (capaciteitstarief), which bills on your monthly quarter-hour peaks.

Source: <https://github.com/Ivoos0/solar>

## Read this first: it decides and records, it does not control anything

**In its current state this project commands nothing.** It makes a decision every five minutes and
appends it to a log file (`decisions.log`). The inverter layer is deliberately stubbed
(`pyscript/modules/inverter.py`): `apply()` writes the decision to the log and transmits nothing.
There is no dashboard; the log is the only output.

If you install it, **your battery will not start behaving differently.** Nothing is broken when
nothing happens. What you get is a week of logged decisions you can compare with what your battery
actually did, and a base to build real inverter control on.

Two more things you should know before spending an evening on this:

- **Battery state of charge is a stub.** The planner always assumes 50%. Every record carries
  `degraded=soc_stubbed`. That is expected, not a fault.
- **Household usage history is not sourced yet.** The function that should provide it
  (`read_usage_history` in `pyscript/battery_planner.py`) returns an empty list, so the planner
  currently treats household consumption as zero and every record carries
  `usage_history_unavailable`. Decision quality is limited until a household load sensor or counter
  is wired into that one function. This is a known limitation with a planned follow-up.

## Does this fit your setup? (two-minute check)

| Component | Required? | Notes |
|---|---|---|
| Belgian digital meter with P1 port | Yes | The capacity-tariff features read the meter's own demand registers |
| SlimmeLezer (or another P1 reader) | Yes | Must expose the demand fields below. Stock firmware does not: **reflash needed** |
| Home Assistant | Yes | Container installs work (no add-ons needed) |
| HACS + pyscript | Yes | pyscript is the host for the code |
| Solar array | Effectively yes | The forecast and solar-absorption logic assume one, on a single plane |
| Home battery + hybrid inverter | Yes, for any value | The planner decides what a battery should do. Today it cannot act on it |
| ENTSO-e API key | Yes | Free. Used for dynamic prices via the Home Assistant ENTSO-e integration |
| A `notify` platform | For alerts | See [Known gaps](#known-gaps) |

**Geography.** The capacity-tariff logic is specific to the **Flemish** capaciteitstarief
(billing on the average of monthly quarter-hour peaks, with a 2.5 kW floor, on grid offtake only).
In the Netherlands, Germany or Wallonia the price-arbitrage half may be useful; set
`capacity_tariff.enabled: false` for the rest. The provider price coefficients are set in
`user_config.yaml`.

**Three-phase connections.** The code reads the meter's **netted** total offtake
(`sensor.slimmelezer_power_consumed`) and deliberately never sums per-phase sensors. On the
installation this was built for, one phase exports while others import, so a per-phase sum read
0.937 kW where the meter itself read 0.003 kW. If your reader only offers per-phase sensors, this
project will read the wrong number.

**Not supported:** control of anything but the battery (no EV charger, heat pump, boiler),
multi-plane solar, paid forecast tiers, solar curtailment.

## SlimmeLezer firmware configuration

The capacity-tariff half needs the meter's e-MUCS demand registers. Stock SlimmeLezer firmware does
not publish them, so **you must reflash the device** with the fields below added. This is an ESPHome
change, not a Home Assistant setting. See the SlimmeLezer documentation for how to flash and edit the
configuration.

Add to the ESPHome `dsmr` sensor block:

```yaml
- platform: dsmr
  active_energy_import_current_average_demand:
    name: "Huidig kwartiervermogen"
  active_energy_import_maximum_demand_running_month:
    name: "Maandpiek"
  active_energy_import_maximum_demand_last_13_months:
    name: "Gemiddelde maandpiek 13 maanden"
```

This produces the following Home Assistant entities (all in kW):

| Entity | Register | Role | Read by this code? |
|---|---|---|---|
| `sensor.slimmelezer_huidig_kwartiervermogen` | `1-0:1.4.0` | Quarter-hour average of the current window | Yes (planner and peak guard) |
| `sensor.slimmelezer_maandpiek` | `1-0:1.6.0` | This month's peak: the level to defend | Yes (planner and peak guard) |
| `sensor.slimmelezer_gemiddelde_maandpiek_13_maanden` | none listed | Average of the last 13 months: what the grid fee is billed on | **No.** Exposed by the firmware but not read by any code today |
| `sensor.slimmelezer_power_consumed` | (existing) | Netted total offtake | Yes (default `capacity_tariff.offtake_sensor`) |

A reader with differently named entities can match them by role and register, and set
`capacity_tariff.offtake_sensor` in the config; the two quarter-hour and month-peak entity names
are currently fixed constants in the code.

### The quarter-hour average has two possible meanings

A meter may report the quarter-hour average as (a) an **accumulating** value (energy so far divided
by the full 15 minutes, climbing from zero) or (b) a true **running** average (energy so far divided
by elapsed time). One minute into a window they differ by a factor of 15. The software detects which
applies at runtime by comparing samples from different points in the same window. The setting is
`capacity_tariff.quarter_hour_average_mode` (`auto`, `running` or `accumulating`; default `auto`).
The peak guard states the detected mode in its records, so you can see what it decided and pin it
once you are sure. The detection state is kept in memory only, so after a restart the mode is
"assumed" until enough windows have been seen. It is detected rather than measured because a battery
behind the meter masks household draw, so a manual test with a known load gives no clean reading.

## Home Assistant setup

Install in this order.

1. **HACS, then pyscript.** HACS -> Integrations -> pyscript, then restart Home Assistant. The
   version tested was not recorded; note yours so a later upgrade is a deliberate act.
2. **ENTSO-e integration.** Provides the dynamic market price. Needs a free API key. Day-ahead prices
   publish around 13:00 local time.
3. **Edit `configuration.yaml`.** The repository's `configuration.yaml` contains the blocks needed;
   the relevant ones are `pyscript:` and `rest:`:

   ```yaml
   pyscript:
     allow_all_imports: true      # the adapter parses YAML and JSON
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

   The URL path is `lat/lon/declination/azimuth/kwp`. **Change the coordinates to your own
   roof.** The ones shown are an example. **Azimuth: 0 = south, negative = east**, so `-10` is ten
   degrees east of south. This is the easiest thing to get backwards. The URL and `user_config.yaml`
   (`solar.*`) must describe the same roof.

   The built-in Forecast.Solar integration is not used: it is UI-configured only and exposes
   aggregates rather than the per-block series the planner needs.
4. **Check the entity names on your install.** The code expects the ENTSO-e price sensor
   `sensor.entso_prices_current_electricity_market_price` with its forward price series in the
   attribute `prices` (a list of `{time, price}` entries). These are **unconfirmed** on other
   installs. In Developer Tools -> States, filter "entso" and compare. If they differ the planner
   halts every cycle with "price data unavailable" while the sensor visibly has data; the names are
   constants at the top of `pyscript/battery_planner.py`.

### File layout on the Home Assistant side

```
<ha-config>/
  configuration.yaml          # edited
  pyscript/                   # copied from this repo, overwrite freely
    battery_planner.py
    peak_guard.py
    modules/
  battery_planner/
    user_config.yaml          # created once from the example, never overwrite
    decisions.log             # generated
    cache/                    # generated
```

Copy `pyscript/` to `<ha-config>/pyscript/` and `battery_planner/user_config.example.yaml` to
`<ha-config>/battery_planner/`. Do not copy `tests/` or `kitty-specs/`. Then, once:

```bash
cd <ha-config>/battery_planner
cp user_config.example.yaml user_config.yaml
```

**Restart Home Assistant after copying.** The script files (`battery_planner.py`, `peak_guard.py`)
hot-reload, but the core under `pyscript/modules/` is loaded natively by an executor loader and is
**not** hot-reloaded. Any change to a core file needs a Home Assistant restart.

### Configuring `user_config.yaml`

The full schema, with comments and validation rules, is in
`kitty-specs/price-aware-load-automation-01M3PH8Y/contracts/user-config.md`; the annotated example is
`battery_planner/user_config.example.yaml`. Sections: `prices`, `battery`, `solar`,
`capacity_tariff`, `usage`, `timing`, `alerts`, `timezone`. Two fields have no sensible default and
must be set:

- `battery.capacity_kwh` (shipped as `CHANGE_ME`)
- `alerts.address` (shipped as `CHANGE_ME@example.com`)

A bad configuration is a startup error (logged, no decision made), not a quietly degraded cycle.
Check your provider's price coefficients (`prices.*`) and, for the capacity tariff, the rate on your
own Fluvius bill (`capacity_tariff.rate_eur_per_kw_year`, revised annually).

### Confirming it works

A record should appear in `<ha-config>/battery_planner/decisions.log` within five minutes:

```bash
tail -f <ha-config>/battery_planner/decisions.log
```

Each record has the action, power, state of charge, vetoes, the selector that produced it, a
reason, and a `degraded=` field. Expect `degraded=soc_stubbed` (and, until usage history is wired
in, `usage_history_unavailable`). If the peak guard is shaving a peak, it writes its own records
(one when a shave starts, changes materially, is vetoed, or stops; not one per 30-second tick).

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| No records at all | pyscript not loaded, or the `pyscript:` block is missing from `configuration.yaml` |
| `ImportError` in the HA log | `allow_all_imports: true` missing |
| HA log warns about blocking I/O | A file operation ran on the event loop; it must use `@pyscript_executor` |
| Forecast always zero | REST sensor failing: check the URL and the free-tier rate limit |
| Prices always missing, constant HALT | ENTSO-e entity or attribute names differ on your install (step 4) |
| Config edits have no effect | You edited the repo copy instead of `<ha-config>/battery_planner/user_config.yaml` |
| Core code change has no effect | Core modules are not hot-reloaded: restart Home Assistant |

## How it behaves

- **Price outage:** no decisions are made. A `HALT` line is written to the log each cycle, one alert
  is sent on entry and then at most one per `alerts.realert_minutes`, and a `RECOVERED` line is
  written when prices return.
- **Forecast outage:** solar is treated as zero, the record is marked, and a bounded number of
  refresh attempts are made. It does not halt.
- **Peak guard** (`pyscript/peak_guard.py`): every ~30 seconds and on each change of the offtake
  sensor, it checks whether the current quarter-hour is heading above the month's peak (floored at
  2.5 kW) and, if so, records a shaving discharge. While it is shaving it sets
  `pyscript.peak_guard_shaving` to `on`, and the planner then refuses to grid-charge. The guard is
  **reactive only**: it defends a window already forming. It does not hold charge back for a
  foreseeable evening peak. It reads the netted offtake sensor. Like the planner, it only logs.
- **Grid sensors unreadable** (capacity enabled): grid charging is suppressed and the record is
  marked `grid_sensors_unavailable`.

## Known gaps

Stated as facts, so you can tell what is deliberate and what is not done yet.

Deliberate scope decisions (out of scope for this mission):

- **It commands nothing.** No inverter control, no HF2211 or Modbus transport. Decisions are logged
  only; the one place real transmission would be added is marked in `pyscript/modules/inverter.py`.
- **Battery state of charge is a stub** (fixed at 50%); reading real hardware is out of scope.
- **Predictive peak protection** is not implemented. The trajectory projects battery charge but not
  grid offtake, so protection is reactive.
- **Only the battery is controlled.** No other loads, no solar curtailment, no change of supply
  contract.
- **Single solar plane**, free forecast tier only.
- **Flanders-specific** capacity logic.
- **No dashboard, and no replay or analysis tooling.** The log is designed so that both can be built
  later.

Not done yet (known, with a planned follow-up):

- **Household usage history is not sourced.** `read_usage_history` returns `[]`, so household load is
  treated as zero. Replace only that function once a household consumption sensor or counter is
  chosen.
- **Alert delivery is unverified.** The notify service name in `pyscript/battery_planner.py`
  (`NOTIFY_SERVICE = "notify"`) is a placeholder, and alerts are sent to it with
  `target=[alerts.address]`. A notify platform (for example SMTP) must be configured in Home
  Assistant, and the name may need changing. If sending fails the error is logged and the halt still
  proceeds.
- **ENTSO-e entity and attribute names are unconfirmed** on other installs (see step 4 above).
- **Future risk in the guard:** it reads net offtake, which its own discharge would lower. Before real
  inverter transmission exists, the commanded power must be added back or the shave will switch on
  and off repeatedly.
- The 13-month average sensor is exposed by the firmware but not read by the code.

## If you want to adapt this

- **Provider price coefficients:** `prices.*` in `user_config.yaml`. No code change.
- **A different grid operator or tariff:** the capacity-tariff logic is in
  `pyscript/modules/capacity.py` and the `capacity_tariff` config block.
- **Real inverter control:** `pyscript/modules/inverter.py`, `apply()` and `read_charge_percent()`.
  Everything that would talk to the inverter belongs behind those two functions.
- **Usage history:** `read_usage_history` in `pyscript/battery_planner.py`.

The decision logic (`pyscript/modules/`) contains no Home Assistant code and is tested without it:

```powershell
py -3.12 -m pip install pytest
py -3.12 -m pytest tests/ -v
```

If you fork, scrub before publishing: no API keys, no e-mail addresses, no coordinates more precise
than you want public, and no `battery_planner/user_config.yaml`. Make sure your `.gitignore` covers
`secrets.yaml` and `battery_planner/user_config.yaml`, and verify it with `git check-ignore -v`
rather than assuming.

## Licence

MIT, see [LICENSE](LICENSE). Anyone may use, copy and adapt this. There is no fee; the only request
(and, in the licence text, the condition) is that you keep the copyright notice, and if you build on
this, a reference to the original repository, <https://github.com/Ivoos0/solar>, is appreciated.
