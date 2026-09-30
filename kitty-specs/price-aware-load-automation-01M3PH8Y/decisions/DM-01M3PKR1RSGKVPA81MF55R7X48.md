# Decision Moment `01M3PKR1RSGKVPA81MF55R7X48`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.safety.invariants-and-dry-run-log`
- **Input key:** `invariants_and_dry_run_log`
- **Status:** `resolved`
- **Created:** `2026-09-29T12:55:29.305918+00:00`
- **Resolved:** `2026-09-29T13:04:27.628295+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

What must never happen (minimum reserve state of charge the planner may never plan below, and how the system should behave when ENTSO-e prices are stale/missing or the forecast.solar call fails), how often should the plan be re-evaluated, and what must the dry-run log file contain for you to trust the decisions before wiring up real inverter commands?

## Options

_(none)_

## Final answer

Safety floor: minimum reserve state of charge is user-configurable with a default of 10 percent; the planner may never plan below it. Missing/stale pricing data is a HALT condition - without prices no decision can be made and inaction costs money - so the system stops issuing decisions, logs the halt, and sends an email alert to a user-configurable address (alert on entry into the halt state and re-alert on a configurable interval, not once per evaluation cycle). Solar forecast failure is NOT a halt: the calculation proceeds exactly as normal but with expected solar production treated as zero for the rest of the day (conservative), and the forecast fetch retries faster than hourly (bounded retry, roughly every 10 minutes, which stays inside the free-tier rate budget). The proposed log line format is accepted: timestamp, decision (charge/discharge/idle/hold), target power, current SOC percent and kWh, consumption price now, injection price now, forecast kWh remaining today, estimated usage remaining today, projected end-of-day SOC, and which rule fired and why. Evaluation cadence is every 5 minutes, user-configurable. Clarification recorded: reading cached HA integration state is not rate limited, so a 5-minute evaluation loop is safe, but the underlying forecast.solar FETCH remains hourly because that upstream call is rate limited.

## Rationale

_(none)_

## Change log

- `2026-09-29T12:55:29.305918+00:00` — opened
- `2026-09-29T13:04:27.628295+00:00` — resolved (final_answer="Safety floor: minimum reserve state of charge is user-configurable with a default of 10 percent; the planner may never plan below it. Missing/stale pricing data is a HALT condition - without prices no decision can be made and inaction costs money - so the system stops issuing decisions, logs the halt, and sends an email alert to a user-configurable address (alert on entry into the halt state and re-alert on a configurable interval, not once per evaluation cycle). Solar forecast failure is NOT a halt: the calculation proceeds exactly as normal but with expected solar production treated as zero for the rest of the day (conservative), and the forecast fetch retries faster than hourly (bounded retry, roughly every 10 minutes, which stays inside the free-tier rate budget). The proposed log line format is accepted: timestamp, decision (charge/discharge/idle/hold), target power, current SOC percent and kWh, consumption price now, injection price now, forecast kWh remaining today, estimated usage remaining today, projected end-of-day SOC, and which rule fired and why. Evaluation cadence is every 5 minutes, user-configurable. Clarification recorded: reading cached HA integration state is not rate limited, so a 5-minute evaluation loop is safe, but the underlying forecast.solar FETCH remains hourly because that upstream call is rate limited.")
