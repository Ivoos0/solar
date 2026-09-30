# Decision Moment `01M3PHEP4TBW5TN36WWKQKC3K9`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.behavior.price-signal-and-rule`
- **Input key:** `price_signal_and_rule`
- **Status:** `resolved`
- **Created:** `2026-09-29T12:15:25.338400+00:00`
- **Resolved:** `2026-09-29T12:34:21.490327+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Which price signal should drive the battery decisions - the dynamic Belpex-derived prices you currently only simulate, or the fixed Tweevoudig day/night tariff you are actually billed on - and by what rule should charge/hold/discharge be chosen (absolute EUR/kWh thresholds, N cheapest hours of the day, or relative to the day's price spread)?

## Options

_(none)_

## Final answer

Decisions are driven by DYNAMIC prices derived from sensor.entso_prices_*, using a per-provider linear transform (market_price * multiplier + offset). The multiplier and offset differ per energy provider AND differ between consumption (charging from the net) and injection (discharging to the net), so all four values must be user-configurable. All such configuration lives in a SEPARATE user config file, not inline in configuration.yaml. The system must estimate whether the battery still holds enough charge to cover the rest of the day - using average household usage over the last week - and export the surplus at the most opportune price moment. Expected solar production must be factored into that estimate, obtained from the forecast.solar estimate API (https://doc.forecast.solar/api:estimate); the API is wired into the HA config file, while location and solar panel strength (capacity/orientation) live in the user config file. Beyond the reserve-and-dump rule, additional decision algorithms for charging from / discharging to the net must be analysed and specified across all scenarios. Hard rule: when the injection price is negative and the battery is full, stop discharging to the electricity net.

## Rationale

_(none)_

## Change log

- `2026-09-29T12:15:25.338400+00:00` — opened
- `2026-09-29T12:34:21.490327+00:00` — resolved (final_answer="Decisions are driven by DYNAMIC prices derived from sensor.entso_prices_*, using a per-provider linear transform (market_price * multiplier + offset). The multiplier and offset differ per energy provider AND differ between consumption (charging from the net) and injection (discharging to the net), so all four values must be user-configurable. All such configuration lives in a SEPARATE user config file, not inline in configuration.yaml. The system must estimate whether the battery still holds enough charge to cover the rest of the day - using average household usage over the last week - and export the surplus at the most opportune price moment. Expected solar production must be factored into that estimate, obtained from the forecast.solar estimate API (https://doc.forecast.solar/api:estimate); the API is wired into the HA config file, while location and solar panel strength (capacity/orientation) live in the user config file. Beyond the reserve-and-dump rule, additional decision algorithms for charging from / discharging to the net must be analysed and specified across all scenarios. Hard rule: when the injection price is negative and the battery is full, stop discharging to the electricity net.")
