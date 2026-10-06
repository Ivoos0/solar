# Alert e-mails and sensors

The e-mails the planner sends and the Home Assistant entities it publishes. Back to the [README](../README.md).

**Contents**

- [Alert e-mails](#alert-e-mails)
- [Sensors](#sensors)

## Alert e-mails

All e-mails go through the notifier `notify.<alerts.notify_service>` to `alerts.address`.

Price outage (halt). Sent when prices become unavailable, then at most once per
`alerts.realert_minutes`. Fix the price source as described in [What the planner does](how-it-works.md#what-the-planner-does).

Sensor outage (`alerts.sensor_enabled`). Sent when configured sensors stay unavailable (unknown, unavailable or missing) for `alerts.sensor_outage_minutes` (default 15). It covers the inputs the planner reads each cycle: `battery.soc_sensor`, `battery.power_sensor`, the limit and reserve sensors, the forecast entity, the three meter sensors while the capacity tariff is on, and the `history.sensors` counters. The price sensor is not included: it has the price outage alert above. One e-mail lists every sensor that has been down long enough, how long, and what the planner does meanwhile (for example "battery charge unknown: the planner holds"). It repeats every `alerts.realert_minutes` while any of them stays down, and stops when all are back. It works during a price outage. When you get one, check the integration behind the sensor (a Modbus bridge that lost its IP, an integration that needs a reload). The count lives in memory, so a restart during an outage starts it again. A failed send is retried on the next cycle.

Peak warning, before (`alerts.peak_warning_enabled`). The peak guard projects the current
quarter-hour's average from the energy so far and the current offtake. If the projection is above the
ceiling on `alerts.peak_warning_ticks` consecutive evaluations (default 2), it sends a prediction: the
projected average, the ceiling, the time left, the current offtake and what the guard is doing. It is
not sent in the first or last minute of a quarter-hour, at most once per quarter-hour and at most once
per `alerts.peak_warning_min_interval_minutes` (default 60, also across a restart). When you get one,
reduce load now (oven, dryer, EV, heating) if you can. The prediction can be wrong, and a correct one
does not mean you were billed.

Peak notice, after (`alerts.peak_enabled`). Sent when this month's peak from the meter
(`capacity_tariff.month_peak_sensor`) goes above `capacity_tariff.billing_floor_kw`: one e-mail for the
first crossing in a calendar month, then one for each new peak at least 0.05 kW higher than the last
one mailed. It contains the peak, the floor and when the planner first saw it. The meter
only reports a new maximum after the quarter-hour has ended, so this arrives after the peak is set. It
works during a price outage. When you get one, nothing needs fixing; note which appliance caused it so
you can avoid repeating it this month. A failed send is retried on the next cycle.

The last mailed month and peak are kept in `<ha-config>/battery_planner/state/peak_alert.json`. If
that file is lost, you may get one extra e-mail for an old peak after a restart.

## Sensors

Once per planner cycle (every `timing.evaluation_interval_minutes`) the planner publishes its latest
state as four Home Assistant entities. Publishing never changes a decision; if it fails, the
planner logs a warning (at most once an hour) and carries on. Set `sensors.enabled: false` to turn it
off.

| Entity | State | Attributes |
|---|---|---|
| `sensor.battery_planner_action` | The last action: `charge`, `discharge`, `export` or `idle` (`unknown` during a price outage) | `power_kw`, `selector`, `vetoes`, `why` (the reason in words), `degraded` (the markers, comma separated), `soc_percent` (`null` while the charge is the 50 % placeholder), `decided_at` (local time), `source` |
| `sensor.battery_planner_budget` | Power the planner may still draw from the grid for charging, in kW. Can be negative. `unknown` when the peak logic is off or its sensors cannot be read | `ceiling_kw`, `average_kw`, `month_peak_kw` |
| `binary_sensor.battery_planner_halted` | `on` while prices are missing and no decisions are made, `off` otherwise | `cause`, `since` (both empty while `off`) |
| `sensor.battery_planner_solar_ratio` | The [solar calibration](history-and-reports.md#solar-calibration) ratio applied to the forecast block now. `unknown` while there is no forecast | `source` (`measured` or `configured`), `weeks_of_history` (weeks between the oldest and newest block with a ratio, counted over the window the planner reads, which is `solar.calibration_weeks` plus one day) |

A missing value is shown as `unknown` (or empty in an attribute), never as the number from an earlier
cycle. The long texts (`why`, `degraded`) are attributes because a state is limited to 255 characters.
The peak guard keeps its own `pyscript.peak_guard_shaving` entity, unchanged.

Things to know:

- The entities are created by pyscript at run time. They disappear when Home Assistant restarts and
  come back with the next planner cycle, at most `timing.evaluation_interval_minutes` later.
- They have no unique id, so Home Assistant cannot edit them in the UI (no renaming, no area, no
  icon change). Use a dashboard card or a template to present them differently.
- If you turn `sensors.enabled` off after they were created, the last values stay until the next
  restart.

A small dashboard card (Edit dashboard, add card, Manual):

```yaml
type: entities
title: Battery planner
entities:
  - entity: sensor.battery_planner_action
  - type: attribute
    entity: sensor.battery_planner_action
    attribute: why
    name: Why
  - entity: sensor.battery_planner_budget
  - entity: binary_sensor.battery_planner_halted
```

An automation that reacts when the planner halts (for example to send a phone notification in
addition to the e-mail; use your own notifier):

```yaml
automation:
  - alias: Battery planner halted
    trigger:
      - platform: state
        entity_id: binary_sensor.battery_planner_halted
        to: "on"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: Battery planner halted
          message: "{{ state_attr('binary_sensor.battery_planner_halted', 'cause') }}"
```
