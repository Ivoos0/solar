# Decision Moment `01M3PMWGAEDEC447FVYTR10PEB`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.behavior.planning-horizon`
- **Input key:** `planning_horizon`
- **Status:** `resolved`
- **Created:** `2026-09-29T13:15:23.854839+00:00`
- **Resolved:** `2026-09-29T13:15:33.846798+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Should the planning horizon end at midnight tonight, or roll forward to cover every hour for which price and forecast data are known?

## Options

- end-of-day
- rolling-horizon
- Other

## Final answer

Rolling horizon: look as far ahead as the available data allows, rather than stopping at midnight. The horizon extends from now to the last interval for which BOTH a derived price and a solar forecast value are known, recomputed on every evaluation cycle. In practice this is roughly end-of-today before ENTSO-e publishes day-ahead prices (~13:00 CET) and roughly end-of-tomorrow afterwards, up to about 35 hours ahead. The reserve-and-dump calculation, the best-price export window, and the cheapest-hours import window are all evaluated across this rolling horizon instead of the remainder of the calendar day. Where forecast data runs out before price data, the uncovered intervals are treated as zero solar rather than truncating the horizon.

## Rationale

_(none)_

## Change log

- `2026-09-29T13:15:23.854839+00:00` — opened
- `2026-09-29T13:15:33.846798+00:00` — resolved (final_answer="Rolling horizon: look as far ahead as the available data allows, rather than stopping at midnight. The horizon extends from now to the last interval for which BOTH a derived price and a solar forecast value are known, recomputed on every evaluation cycle. In practice this is roughly end-of-today before ENTSO-e publishes day-ahead prices (~13:00 CET) and roughly end-of-tomorrow afterwards, up to about 35 hours ahead. The reserve-and-dump calculation, the best-price export window, and the cheapest-hours import window are all evaluated across this rolling horizon instead of the remainder of the calendar day. Where forecast data runs out before price data, the uncovered intervals are treated as zero solar rather than truncating the horizon.")
