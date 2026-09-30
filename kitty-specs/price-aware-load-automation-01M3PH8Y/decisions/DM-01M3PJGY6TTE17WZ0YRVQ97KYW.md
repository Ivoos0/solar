# Decision Moment `01M3PJGY6TTE17WZ0YRVQ97KYW`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.constraints.battery-envelope`
- **Input key:** `battery_envelope`
- **Status:** `resolved`
- **Created:** `2026-09-29T12:34:07.706267+00:00`
- **Resolved:** `2026-09-29T12:40:11.473595+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

What is the battery's operating envelope and what can Home Assistant observe about it today - usable capacity, minimum reserve state of charge that must never be crossed, maximum charge and discharge power, round-trip efficiency - and is there already a working state-of-charge sensor in HA, or must state of charge also be estimated?

## Options

_(none)_

## Final answer

An HF2211 serial-to-network gateway fronts the inverter; it is expected to both command the inverter and read the battery charge percentage. Battery usable capacity (kWh) is a user-configurable value in the user config file, and current stored energy is derived as capacity * charge percentage. For this mission the charge-percentage read is STUBBED: the routine that computes current battery charge (calculateCurrentBatteryCharge or equivalent) returns a stub/placeholder value behind a stable seam, so the planner can run end-to-end without a live inverter link. Minimum reserve state of charge, maximum charge/discharge power, round-trip efficiency, and the exact inverter brand/model were not specified and must be treated as user-configurable values with documented defaults.

## Rationale

_(none)_

## Change log

- `2026-09-29T12:34:07.706267+00:00` — opened
- `2026-09-29T12:40:11.473595+00:00` — resolved (final_answer="An HF2211 serial-to-network gateway fronts the inverter; it is expected to both command the inverter and read the battery charge percentage. Battery usable capacity (kWh) is a user-configurable value in the user config file, and current stored energy is derived as capacity * charge percentage. For this mission the charge-percentage read is STUBBED: the routine that computes current battery charge (calculateCurrentBatteryCharge or equivalent) returns a stub/placeholder value behind a stable seam, so the planner can run end-to-end without a live inverter link. Minimum reserve state of charge, maximum charge/discharge power, round-trip efficiency, and the exact inverter brand/model were not specified and must be treated as user-configurable values with documented defaults.")
