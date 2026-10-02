# Price-aware battery planner for Home Assistant (Flanders)

A Home Assistant [pyscript](https://github.com/custom-components/pyscript) project that decides,
every five minutes, what a home battery should do: charge, discharge, export or idle. It weighs
dynamic electricity prices (ENTSO-e), a solar forecast (forecast.solar), expected household usage,
and the Flemish capacity tariff (capaciteitstarief), which bills on your monthly quarter-hour peaks.

Source: <https://github.com/Ivoos0/solar>

## Read this first: it decides and records, it does not control anything

**Out of the box this project commands nothing.** It makes a decision every five minutes and
appends it to a log file (`decisions-YYYY-MM-DD.log`, one file per day). The default inverter driver is `logging`
(`pyscript/modules/inverter_logging.py`): it transmits nothing, so the log is the whole effect.
A different inverter is a drop-in driver file selected by `inverter.type` in your config (see
[Choosing an inverter driver](#choosing-an-inverter-driver) and
[Adding an inverter](#adding-an-inverter)); the decision is logged first, whatever the driver.
There is no dashboard; the log is the only output.

If you install it, **your battery will not start behaving differently.** Nothing is broken when
nothing happens. What you get is a week of logged decisions you can compare with what your battery
actually did, and a base to build real inverter control on.

Two more things you should know before spending an evening on this:

- **With the `logging` driver, battery state of charge is a stub.** The planner always assumes 50%.
  Every record carries `degraded=soc_stubbed`. That is expected, not a fault. A driver that reads
  the real charge removes the marker.
- **Household usage history is not sourced yet.** The function that should provide it
  (`read_usage_history` in `pyscript/battery_planner.py`) returns an empty list, so the planner
  currently treats household consumption as zero and every record carries
  `usage_history_unavailable`. Because of that the planner **does not charge from the grid at all**
  (veto V4, see [Known gaps](#known-gaps)) until a household load sensor or counter is wired into
  that one function. This is a known limitation with a planned follow-up.

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
| A `notify` service | For alerts | SMTP notifier named `battery_alert`, provider chosen in `secrets.yaml`; see [Alert e-mail](#alert-e-mail) |

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
3. **Use the repository's `configuration.yaml` as-is.** It contains the `pyscript:`, `rest:` and
   `notify:` blocks the planner needs, and you never edit them. Everything personal comes from
   `secrets.yaml` (below) and `battery_planner/user_config.yaml`. If your Home Assistant already has
   a `configuration.yaml`, see [Updating](#updating) for the one-time merge.

   The forecast.solar REST sensor takes its whole URL from the secret `forecast_solar_url`. Put your
   own roof in it in `secrets.yaml`; the template `secrets.example.yaml` shows the form
   (`https://api.forecast.solar/estimate/lat/lon/declination/azimuth/kwp`, with generic example
   values). **Azimuth: 0 = south, negative = east**, so `-10` is ten degrees east of south. This is
   the easiest thing to get backwards. The URL and `user_config.yaml` (`solar.*`) must describe the
   same roof.

   The built-in Forecast.Solar integration is not used: it is UI-configured only and exposes
   aggregates rather than the per-block series the planner needs.
4. **Check the entity names on your install.** The code expects the ENTSO-e price sensor
   `sensor.entso_prices_current_electricity_market_price` with its forward price series in the
   attribute `prices` (a list of `{time, price}` entries). These are **unconfirmed** on other
   installs. In Developer Tools -> States, filter "entso" and compare. If they differ the planner
   halts every cycle with "price data unavailable" while the sensor visibly has data; the names are
   constants at the top of `pyscript/battery_planner.py`.

### Alert e-mail

When prices are unavailable the planner halts and sends an e-mail through the Home Assistant
notify service `notify.<alerts.notify_service>` (default `notify.battery_alert`), to
`alerts.address`. `configuration.yaml` defines that notifier with no provider-specific values: the
server, port, encryption and credentials all come from `secrets.yaml`. No address or credential is
stored in the repository.

1. **Pick your provider** in `secrets.example.yaml`. It has ready-made presets for Gmail, Outlook /
   Microsoft 365, Yahoo and iCloud, plus a CUSTOM block (`smtp_server`, `smtp_port`,
   `smtp_encryption` = `starttls` | `tls` | `none`; typical ports 587 / 465 / 25). Keep exactly one
   preset uncommented. Most providers need an app password (Gmail: turn on 2-step verification, then
   Google Account -> Security -> App passwords); the normal account password will not work.
   **The preset hostnames and ports come from the providers' public documentation as known at the
   time of writing and have not been verified against live accounts.** Confirm them with your
   provider. Outlook / Microsoft 365 may not work at all, because basic-auth SMTP is disabled on many
   Microsoft accounts.
2. **Fill HA's `secrets.yaml`** (in your HA config folder; it is gitignored). Copy the keys from
   the tracked template `secrets.example.yaml` and put real values in: the provider block
   (`smtp_server`, `smtp_port`, `smtp_encryption`), `smtp_sender` (the sending account),
   `smtp_password` (the app password), `smtp_recipient` (where alerts go), and `forecast_solar_url`.
3. **Set `battery_planner/user_config.yaml`**: `alerts.address` to the same address as
   `smtp_recipient`, and `alerts.notify_service` to the notifier name (default `battery_alert`, which
   matches `configuration.yaml`; leave it out to use the default).

   *Rename note:* earlier versions named the notifier `gmail_alert`. If you set
   `alerts.notify_service: gmail_alert` before, either keep the old name (and keep that name in your
   merged notifier) or update the setting to `battery_alert`.
4. **Restart Home Assistant** (a notifier is only created at startup).
5. **Test the notifier** before relying on it: Developer Tools -> Actions, choose
   `notify.battery_alert` (or your service name), give it a message, and run it. The mail should
   arrive within a minute.

**Caveat, not verified.** I could not verify against a live Home Assistant that the SMTP YAML
platform is still supported in your HA version. SMTP notify may be deprecated or moved to the UI in
recent releases. Check the HA release notes; if it moved, create the notifier through the UI and put
its service name in `alerts.notify_service`.

**When the service does not exist.** pyscript's `service.call` needs the service to exist at call
time. If it does not (wrong name, notifier not loaded, restart pending), the error (for example
`ServiceNotFound`) is logged as an alert-send failure, the decision record shows `alerted=none`, and
the send is retried on the next cycle. The halt itself still proceeds.

### File layout on the Home Assistant side

```
<ha-config>/
  configuration.yaml          # taken as-is from this repo (merged once by hand if you had your own)
  secrets.yaml                # yours, gitignored: provider, credentials, forecast URL
  pyscript/                   # copied from this repo, overwrite freely
    battery_planner.py
    peak_guard.py
    modules/
  battery_planner/
    user_config.yaml          # created once from the example, never overwrite
    decisions-YYYY-MM-DD.log  # generated, one file per local day
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

### Updating

You edit exactly two files, both gitignored, so updating from git never produces a merge conflict:

- `battery_planner/user_config.yaml` (your site, battery, tariff and alert settings)
- Home Assistant's `secrets.yaml` (e-mail provider and credentials, forecast.solar URL)

Everything else is taken as shipped. To update: pull the new version, copy `pyscript/` over
`<ha-config>/pyscript/`, and take the tracked `configuration.yaml` as-is. **Never overwrite
`battery_planner/user_config.yaml` or `secrets.yaml`.** If a release adds new keys, compare
`user_config.example.yaml` and `secrets.example.yaml` with your files and add only what is missing.
Restart Home Assistant afterwards (see above).

If your Home Assistant already has its own `configuration.yaml`, you cannot just overwrite it. Merge
this repository's blocks (`pyscript:`, `rest:`, `notify:`, and the helper and template blocks you
want) into yours **once, by hand**, or pull them in with `!include`. After that, no further edits to
`configuration.yaml` are needed, because every personal value is a `!secret`.

**Not tested:** loading these blocks as a Home Assistant package or via split `!include` files was
not tested. Only the single `configuration.yaml` form is what this repository ships.

### Configuring `user_config.yaml`

The full schema, with comments and validation rules, is in
`kitty-specs/price-aware-load-automation-01M3PH8Y/contracts/user-config.md`; the annotated example is
`battery_planner/user_config.example.yaml`. Sections: `prices`, `battery`, `solar`,
`capacity_tariff`, `usage`, `timing`, `alerts`, `inverter`, `timezone`. Two fields have no sensible default and
must be set:

- `battery.capacity_kwh` (shipped as `CHANGE_ME`)
- `alerts.address` (shipped as `CHANGE_ME@example.com`)

A bad configuration is a startup error (logged, no decision made), not a quietly degraded cycle.
Check your provider's price coefficients (`prices.*`) and, for the capacity tariff, the rate on your
own Fluvius bill (`capacity_tariff.rate_eur_per_kw_year`, revised annually).

`usage.recency_weighting` (default `linear`, or `none`) makes newer weeks count more when the usage
profile is averaged, so changes in routine are picked up faster: with `usage.history_weeks: 4` the last
week weighs 4, the week before 3, then 2, then 1. `none` is the plain mean. Changing it discards the
cached usage profile.

`capacity_tariff.stay_under_percent` (default `80`, must be above 0 and at most 100) is a safety
margin for **grid charging only**: charging from the grid must stay under that percentage of the
ceiling (this month's peak, never below `billing_floor_kw`). With the 2.5 kW floor and 80, the
planner will not charge from the grid if the quarter-hour looks like passing 2.0 kW; with a 3.0 kW
month peak the limit is 2.4 kW. Peak shaving and the `ceiling=` value in the log still use the real
ceiling, and `budget=` is the room left under the reduced charging level. Set `100` to charge right up
to the ceiling.

### Choosing an inverter driver

```yaml
inverter:
  type: logging      # default. Nothing, or "none", means the same.
```

`inverter.type` is a lowercase name (letters, digits, underscore) that selects the file
`pyscript/modules/inverter_<type>.py`. `logging` ships with the project and only logs. Any other
value loads that driver, so `type: alphaess` means `pyscript/modules/inverter_alphaess.py`.

**Any driver other than `logging` transmits real commands to a real inverter. What it does is the
responsibility of whoever wrote it and whoever enabled it.** None is shipped, and the project does
not test against hardware.

Whatever the driver, the decision line is always written to that day's decision log first. A driver that
raises, times out, returns `False` or is missing never removes or changes that line and never
crashes the planner or the guard. A type with no usable driver file is an error in the Home Assistant
log on every cycle, nothing is transmitted (the same as `logging`), and decisions carry
`degraded=inverter_driver_unavailable` (or `inverter_read_failed` when only the charge reading is
unusable). Driver files are loaded once; after editing one, restart Home Assistant. A driver that
failed to load is retried on the next cycle, so adding a missing file needs no restart.

### Confirming it works

A record should appear in today's file, `<ha-config>/battery_planner/decisions-YYYY-MM-DD.log`,
within five minutes:

```bash
tail -f <ha-config>/battery_planner/decisions-$(date +%F).log
```

The log is rotated daily by name: every record goes to the file of its own local date (in the
`timezone` of your config), so `decisions-2026-09-30.log` holds exactly that calendar day, from
00:00:00 to 23:59:59 local time. The planner, the peak guard and the HALT / RECOVERED / SKIP
lines all write to the same day's file. **Nothing is deleted automatically**, so the directory
grows by one file per day. Add your own cleanup, for example a daily cron job on the host:

```bash
find <ha-config>/battery_planner -name 'decisions-*.log' -mtime +30 -delete
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
- **No usage history:** grid charging is vetoed (V4) and the record shows it, for example
  `vetoes=V4(suppressed S1 charge)`. Solar-surplus charging is not affected. See
  [Known gaps](#known-gaps).

## Known gaps

Stated as facts, so you can tell what is deliberate and what is not done yet.

Deliberate scope decisions (out of scope for this mission):

- **No inverter driver is shipped except `logging`.** No inverter control, no HF2211 or Modbus
  transport. By default decisions are logged only; a driver for your inverter is a single file (see
  [Adding an inverter](#adding-an-inverter)).
- **Battery state of charge is a stub** (fixed at 50%) until a driver reads the real value.
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
  chosen. Until then, **the planner never charges from the grid**: veto V4 ("no usage profile")
  forbids every grid-charging proposal while there is no history, because without it a grid charge
  could land on top of an unseen household peak and raise the capacity tariff. Charging from surplus
  solar, discharging and exporting are unaffected.
- **Alert delivery is unverified end to end.** The notifier is configured (see
  [Alert e-mail](#alert-e-mail)), but it was not tested against a live Home Assistant, and the SMTP
  YAML platform may have moved to the UI in your HA version. If sending fails the error is logged and
  the halt still proceeds.
- **ENTSO-e entity and attribute names are unconfirmed** on other installs (see step 4 above).
- **Future risk in the guard:** it reads net offtake, which its own discharge would lower. Before real
  inverter transmission exists, the commanded power must be added back or the shave will switch on
  and off repeatedly.
- The 13-month average sensor is exposed by the firmware but not read by the code.

## If you want to adapt this

- **Provider price coefficients:** `prices.*` in `user_config.yaml`. No code change.
- **A different grid operator or tariff:** the capacity-tariff logic is in
  `pyscript/modules/capacity.py` and the `capacity_tariff` config block.
- **Real inverter control:** add a driver file, see [Adding an inverter](#adding-an-inverter). You do
  not edit `inverter.py`, the planner or the guard.
- **Usage history:** `read_usage_history` in `pyscript/battery_planner.py`.

### Adding an inverter

A driver is one file, `pyscript/modules/inverter_<name>.py`, selected with `inverter.type: <name>`.
It provides two functions and one optional flag. Copy `inverter_logging.py` and replace the bodies.
Skeleton for a hypothetical `alphaess`:

```python
# pyscript/modules/inverter_alphaess.py
SOC_IS_STUB = False            # optional, default False. True = the charge is a placeholder


def send(action, target_power_kw):
    """action: "charge" | "discharge" | "export" | "idle". Power in kW, >= 0, 0.0 when idle.
    Return True when the inverter accepted it. Return False or raise when it did not."""
    ...  # talk to your inverter here


def read_charge_percent():
    """Battery charge, 0 to 100. Anything else counts as a failed reading."""
    ...
```

Then set `inverter.type: alphaess`, restart Home Assistant and watch the log. What the framework does
for you: it writes the decision line before calling you; runs your code as ordinary Python in an
executor thread (blocking network I/O is fine, the event loop is never blocked); gives each call
10 seconds, after which it is treated as failed; catches every failure and logs it; and marks
decisions `soc_stubbed` while `SOC_IS_STUB` is true. A driver is loaded under a private name; it is
not on `sys.path`, so it can import the standard library and installed packages but not the other
files in `pyscript/modules/`, and it cannot use pyscript names such as `log` or `state`.

Checklist before you enable a real driver:

- [ ] **Thread safety.** The planner (every cycle, whatever the action, including `idle`) and the peak
      guard (only when its state changes) may call `send` at the same time from different threads.
      Serialize access to your bus, for instance with a `threading.Lock`.
- [ ] **Network timeouts of your own.** After 10 seconds the call is abandoned, but its thread keeps
      running until it returns.
- [ ] **Idempotent commands.** `send("discharge", 2.5)` may arrive again unchanged on the next cycle.
      Some inverters also need a periodic re-send (heartbeat) or they fall back to their own mode;
      the planner's every-cycle calls give you that, the guard's change-only calls do not.
- [ ] **Guard feedback, FUTURE RISK.** The peak guard reads net grid offtake, and its own commanded
      discharge lowers that reading. With real control the commanded power must be added back to the
      reading, or the shave will switch on and off repeatedly. This is not implemented; do not enable
      a real driver together with the capacity-tariff guard until it is.
- [ ] **Failures are your call.** The framework only logs a failed `send`; it does not retry or fall
      back. Decide what your inverter should do when commands stop arriving.
- [ ] **Run `logging` first** and compare a week of logged decisions with what the battery should have
      done before sending anything real.
- [ ] Return the real charge from `read_charge_percent` and leave `SOC_IS_STUB` unset, or the
      decisions stay marked `soc_stubbed`.

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
