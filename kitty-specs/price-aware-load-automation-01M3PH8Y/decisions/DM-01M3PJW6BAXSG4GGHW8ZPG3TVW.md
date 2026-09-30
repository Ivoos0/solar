# Decision Moment `01M3PJW6BAXSG4GGHW8ZPG3TVW`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.constraints.solar-array-and-forecast`
- **Input key:** `solar_array_and_forecast`
- **Status:** `resolved`
- **Created:** `2026-09-29T12:40:16.490392+00:00`
- **Resolved:** `2026-09-29T12:55:24.155544+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

What is the solar array geometry to configure for forecast.solar (installed kWp, roof tilt/declination, azimuth) - and is it a single plane or split across multiple orientations such as east/west? The free public forecast.solar tier supports only ONE plane and is rate limited, so a multi-orientation roof needs either a paid API key or an approximation. How often should the forecast be refreshed?

## Options

_(none)_

## Final answer

Single plane: 20 panels x 405 Wp = 8.1 kWp installed. Orientation is slightly south-east, about 10 degrees east of south, giving forecast.solar azimuth -10 (0 = south, negative = east). Tilt/declination is 50 degrees measured from horizontal. Location is the installation site, East Flanders, Belgium - prefill approximately 51.12 N, 3.85 E as the default latitude/longitude (user should verify, the API resolves to ~10 m). All of these values (kWp, azimuth, declination, latitude, longitude) are user-configurable in the user config file with those values as prefilled defaults. Use the FREE public forecast.solar tier (single plane, no API key) and refresh the forecast hourly, which stays within the free-tier rate limit. Local timezone is Europe/Brussels, which governs the daily watt_hours reset.

## Rationale

_(none)_

## Change log

- `2026-09-29T12:40:16.490392+00:00` — opened
- `2026-09-29T12:55:24.155544+00:00` — resolved (final_answer="Single plane: 20 panels x 405 Wp = 8.1 kWp installed. Orientation is slightly south-east, about 10 degrees east of south, giving forecast.solar azimuth -10 (0 = south, negative = east). Tilt/declination is 50 degrees measured from horizontal. Location is the installation site, East Flanders, Belgium - prefill approximately 51.12 N, 3.85 E as the default latitude/longitude (user should verify, the API resolves to ~10 m). All of these values (kWp, azimuth, declination, latitude, longitude) are user-configurable in the user config file with those values as prefilled defaults. Use the FREE public forecast.solar tier (single plane, no API key) and refresh the forecast hourly, which stays within the free-tier rate limit. Local timezone is Europe/Brussels, which governs the daily watt_hours reset.")
