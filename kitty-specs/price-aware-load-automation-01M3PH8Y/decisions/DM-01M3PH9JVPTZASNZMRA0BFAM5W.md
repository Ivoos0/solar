# Decision Moment `01M3PH9JVPTZASNZMRA0BFAM5W`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.scope.controllable-loads`
- **Input key:** `controllable_loads`
- **Status:** `resolved`
- **Created:** `2026-09-29T12:12:38.134817+00:00`
- **Resolved:** `2026-09-29T12:15:20.277519+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Which devices in the home can actually be switched or scheduled by Home Assistant today (battery/inverter, EV charger, heat pump, boiler, washing machine/dryer, dishwasher), and which of those is the primary target for this mission?

## Options

_(none)_

## Final answer

Only a home battery and inverter are in scope; no EV charger, heat pump, or wet appliances. HA should eventually command the inverter, but the command code does not exist yet and is untested. For this mission the inverter-commanding code must live in a separate file and must NOT actually command the inverter: it writes what it would have done to a log file (dry-run / shadow mode).

## Rationale

_(none)_

## Change log

- `2026-09-29T12:12:38.134817+00:00` — opened
- `2026-09-29T12:15:20.277519+00:00` — resolved (final_answer="Only a home battery and inverter are in scope; no EV charger, heat pump, or wet appliances. HA should eventually command the inverter, but the command code does not exist yet and is untested. For this mission the inverter-commanding code must live in a separate file and must NOT actually command the inverter: it writes what it would have done to a log file (dry-run / shadow mode).")
