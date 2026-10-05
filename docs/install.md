# Install and update

How to install the planner in Home Assistant and how to update it. Back to the [README](../README.md).

**Contents**

- [Install](#install)
- [Updating](#updating)

## Install

Do these in order.

1. <a id="install-step-1"></a>Install HACS, then install pyscript through HACS (Integrations). Restart Home Assistant.
2. <a id="install-step-2"></a>Install the ENTSO-e integration and enter your API key. Day-ahead prices are published around
   13:00 local time.
3. <a id="install-step-3"></a>Reflash the SlimmeLezer with the demand registers. In its ESPHome configuration, add this to the
   `dsmr` sensor block, then flash it (see the SlimmeLezer documentation):

   ```yaml
   - platform: dsmr
     active_energy_import_current_average_demand:
       name: "Huidig kwartiervermogen"
     active_energy_import_maximum_demand_running_month:
       name: "Maandpiek"
     active_energy_import_maximum_demand_last_13_months:
       name: "Gemiddelde maandpiek 13 maanden"
   ```

   Home Assistant then shows these entities (all in kW):

   | Entity | Register | Meaning |
   |---|---|---|
   | `sensor.slimmelezer_huidig_kwartiervermogen` | `1-0:1.4.0` | Average of the current quarter-hour |
   | `sensor.slimmelezer_maandpiek` | `1-0:1.6.0` | This month's peak, the level the planner defends |
   | `sensor.slimmelezer_gemiddelde_maandpiek_13_maanden` | - | Average of the last 13 months. Not read by the code |
   | `sensor.slimmelezer_power_consumed` | - | Netted total offtake |

   If your reader names these differently, set `capacity_tariff.offtake_sensor`,
   `capacity_tariff.quarter_hour_average_sensor` and `capacity_tariff.month_peak_sensor` in
   `user_config.yaml` (step 6).
4. <a id="install-step-4"></a>Copy the `pyscript/` folder from this repository to `<ha-config>/pyscript/`. Copy
   `configuration.yaml` to `<ha-config>/configuration.yaml`. If you already have a
   `configuration.yaml`, merge the `pyscript:`, `rest:` and `notify:` blocks into it once, by hand or
   with `!include`. The shipped file also
   includes `automations.yaml`, `scripts.yaml` and `scenes.yaml`; Home Assistant needs those files to
   exist. You never edit the blocks afterwards: personal values come from `secrets.yaml` and
   `user_config.yaml`. Loading the blocks as a Home Assistant package or from split `!include` files
   is untested.
5. <a id="install-step-5"></a>Create `<ha-config>/secrets.yaml` from `secrets.example.yaml` (or add the keys to your existing
   one):
   - `forecast_solar_url`: `https://api.forecast.solar/estimate/<lat>/<lon>/<declination>/<azimuth>/<kwp>`.
     Azimuth 0 is south and negative is east, so `-10` is ten degrees east of south. This is the
     easiest value to get backwards. The roof lives only in this URL; `user_config.yaml` has no roof
     settings. After you change the URL, the planner picks up the new forecast at the next cycle once
     the sensor has refreshed (hourly, or restart Home Assistant). It serves the previous forecast
     only while the sensor is unavailable, and for at most `timing.solar_cache_stale_minutes`
     (default 120). A sensor that stays available but stops refreshing is treated the same way:
     once its data is older than that limit the planner ignores it.
   - `smtp_sender`, `smtp_password`, `smtp_recipient`: the sending account, its app password (not
     the normal password; Gmail needs 2-step verification first) and the address that receives alerts.
   - `smtp_server`, `smtp_port`, `smtp_encryption` (`starttls`, `tls` or `none`; ports 587, 465, 25):
     keep exactly one provider preset uncommented. The presets for Gmail, Outlook / Microsoft 365,
     Yahoo and iCloud are from the providers' public documentation and are not tested against live
     accounts. Outlook may not work at all, because many Microsoft accounts have basic-auth SMTP
     disabled.
6. <a id="install-step-6"></a>Create the planner config:

   ```bash
   mkdir -p <ha-config>/battery_planner
   cp battery_planner/user_config.example.yaml <ha-config>/battery_planner/user_config.yaml
   ```

   Edit it (see [Configure](configuration.md#configure)). At minimum set `battery.capacity_kwh` and `alerts.address`.
7. <a id="install-step-7"></a>Restart Home Assistant. Core modules under `pyscript/modules/` are loaded natively and are not
   hot-reloaded, so any change to them needs a restart. A notifier is also only created at startup.
8. <a id="install-step-8"></a>Test the notifier: Developer Tools -> Actions, choose `notify.battery_alert`, give it a message
   and run it. The mail should arrive within a minute. If Home Assistant reports the SMTP YAML
   platform as unsupported in your version (it may have moved to the UI), create the notifier in the
   UI and set its service name in `alerts.notify_service`.

Files on the Home Assistant side:

```
<ha-config>/
  configuration.yaml
  secrets.yaml                  yours, do not publish
  pyscript/                     copied from this repository
  battery_planner/
    user_config.yaml            yours, created in step 6
    decisions-YYYY-MM-DD.log    generated, one file per local day
    cache/                      generated, see docs/cache-files.md
    state/                      generated, survives restarts (see "What a restart does"
                                in docs/how-it-works.md)
      peak_alert.json           last month-peak e-mail sent
      halt.json                 price-outage state while prices are missing
      average_mode_planner.json, average_mode_guard.json
                                what the quarter-hour average mode detection has seen
      peak_warning.json         when the last peak warning was sent
      last_command.json         the last command the inverter driver accepted
    history/                    generated, energy history
```

Do not copy `tests/` to Home Assistant.

## Updating

You edit two files, and updating never touches them: `battery_planner/user_config.yaml` and Home
Assistant's `secrets.yaml`.

1. Copy the new `pyscript/` folder over `<ha-config>/pyscript/`.
2. Copy the new `configuration.yaml`, or merge its changed blocks into yours.
3. Never overwrite `user_config.yaml` or `secrets.yaml`. If a release adds keys, compare
   `user_config.example.yaml` and `secrets.example.yaml` with your files and add what is missing.
4. Restart Home Assistant.

Upgrade note: older versions named the notifier `gmail_alert`. If you set
`alerts.notify_service: gmail_alert`, either keep that name in your notifier or change the setting to
`battery_alert`.
