# What the planner does

How the planner chooses an action, what the peak guard does and what survives a restart. Back to the [README](../README.md).

**Contents**

- [Peak guard](#peak-guard)
- [What a restart does](#what-a-restart-does)

The planner builds a projection of the battery over the coming hours (as far as the price list
reaches) from the price list, the solar forecast and your usage profile. Then it checks a few
rules that forbid certain actions, and tries the possible actions in a fixed order. The first action
that none of the rules forbids is the decision. The log records the reason in words (`why`). The short labels it also prints for the rules and
actions are explained in [Labels in the decision log](decision-log.md#labels-in-the-decision-log).

The reserve (`battery.reserve_percent`) limits exporting to the grid. It never makes the planner buy
power to keep the battery up: when the battery reaches the reserve the house simply imports at that
time. Peak shaving may use charge below the reserve, and the inverter's own minimum charge still
applies.

Idle is a command, not silence: it cancels every forced mode the planner or the guard set (forced
grid charge, forced export, forced discharge) and returns the inverter to that default. It is sent
once after a forced mode, and not repeated while the planner keeps idling.

In this documentation, "charge" always means charge from the grid.

Grid charging never exceeds the budget: the charging level (`stay_under_percent` of the ceiling) minus
what the house is drawing. What the house draws is the grid power it would take without the battery
helping. With `battery.power_sensor` set, the planner measures it: the meter reading plus the power
the battery is delivering. A battery that covers a 3 kW house while the meter shows 0 kW still counts
as 3 kW, and the figure does not move when the planner starts charging from the grid. Without that
sensor the planner uses the meter reading minus its own grid charge, which cannot see a battery
that is covering the house. Grid charging stops one evaluation interval before the quarter-hour ends:
in the last `timing.evaluation_interval_minutes` (5 by default, never less than 1) the budget is 0,
so a charge sized for one quarter-hour does not run on into the next while the planner waits for its
next decision. The peak guard and the peak warning are not affected; they re-check every 30 seconds.

Price outage: if prices are missing, unparseable or already elapsed, no decisions are made. A `HALT`
line is logged each cycle, one e-mail is sent on entry and then at most one per
`alerts.realert_minutes`, and a `RECOVERED` line is logged when prices return. The e-mail names the
cause. Check the price entity and attribute (see [Configure](configuration.md#configure)). If the ENTSO-e integration
itself is down, wait for it; the planner resumes by itself.

Forecast outage: solar counts as zero, the record is marked `solar_zero_fallback`, and the planner
retries. It does not halt. With no forecast the planner is deliberately cautious: it does not buy
power from the grid and does not export, because a plan built on zero solar is a guess. It idles
("hold: no solar forecast") and only peak protection still acts. A forecast that is merely old but
still cached is used as before (`cache_age_solar=...`) and does not hold the planner.

## Peak guard

`pyscript/peak_guard.py` runs every 30 seconds and on each update of the netted offtake sensor. If the
current quarter-hour is heading above the ceiling (this month's peak, never below the 2.5 kW floor),
it records a shaving discharge and sets `pyscript.peak_guard_shaving` to `on`. While that is on, the
planner does not grid-charge. While shaving, the guard rewrites that entity every guard interval with
a fresh `last_beat` attribute; if `last_beat` is missing or older than three guard intervals (90
seconds by default) the planner ignores the `on`, logs a warning, and carries on, so a stopped guard
cannot block charging for good. The guard only reacts to a window that is already forming. It does not
hold charge back for an evening peak it could foresee. Like the planner, it only logs unless you add
an inverter driver. With a real driver the meter shows the offtake after the battery has already
taken some of the load. The guard therefore adds the discharge power it is commanding back to the
metered offtake before projecting the quarter-hour (the window energy comes from the meter and is not
adjusted). The shave then stays at one steady value while the load persists and stops only when the
load, without the battery, would no longer push the quarter-hour over the ceiling. The added-back
power counts only while the guard keeps confirming the shave (within two guard intervals) and never
with the `logging` driver, which commands nothing.

Shaving matters mainly when the planner itself is holding energy back from the house. The planner
does that when it charges the battery from the grid. The grid then supplies
the household plus the charge, and a sudden load can push the quarter-hour over the ceiling. The
guard then discharges to cut the grid draw, and the planner stops grid-charging while it shaves. If
your inverter already runs the house from the battery whenever it has charge, the grid draw is low
and the guard has nothing to do. It cannot add discharge beyond what the inverter allows, and it
stops only when the battery is empty. The reserve does not stop it: `battery.reserve_percent` only limits
exporting to the grid, so the guard may use charge below it. The inverter's own minimum charge still
applies.

## What a restart does

Home Assistant restarts, pyscript reloads and power cuts lose everything held in memory. These are
saved in `<ha-config>/battery_planner/state/` and picked up again, so a restart does not repeat
something that was just done:

| File | Keeps | Effect after a restart |
|---|---|---|
| `last_command.json` | The last command the driver accepted (action, power, time) | The driver is not sent the same command again until its resend time has passed (a record older than that is treated as expired, so an old file never blocks a command) |
| `halt.json` | A price outage in progress: cause, when it started, when the last e-mail went out | No early second e-mail, the same outage keeps its start time, and `RECOVERED` reports its full length |
| `average_mode_planner.json`, `average_mode_guard.json` | The quarter-hour average readings used to detect the meter's behaviour | A detected mode stays detected instead of falling back to "assumed" |
| `peak_warning.json` | When the last peak warning was sent | No second warning for the same quarter-hour, and the minimum interval still applies |
| `peak_alert.json` | The last month-peak e-mail | No repeat e-mail for an old peak |

A missing or damaged file is treated as "nothing saved": the planner starts fresh and never stops
because of it. Deleting a file is safe; the worst case is one repeated e-mail or command. Everything
else starts fresh, for example the guard's shaving state, which is re-derived from the meter on the
next evaluation.
