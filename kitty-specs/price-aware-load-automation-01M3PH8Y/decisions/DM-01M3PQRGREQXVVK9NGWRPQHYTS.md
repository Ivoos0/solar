# Decision Moment `01M3PQRGREQXVVK9NGWRPQHYTS`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `specify`
- **Slot key:** `specify.behavior.trajectory-granularity`
- **Input key:** `trajectory_granularity`
- **Status:** `resolved`
- **Created:** `2026-09-29T14:05:38.959633+00:00`
- **Resolved:** `2026-09-29T14:05:50.919159+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Should the energy balance be a single scalar over the horizon, or a per-interval projection of the battery charge trajectory fine enough to detect the battery saturating mid-horizon and spilling solar?

## Options

- scalar-horizon-balance
- 15-minute-trajectory
- Other

## Final answer

The scalar horizon balance is insufficient. The planner must project the battery charge trajectory forward across the horizon in blocks of 15 minutes (block length configurable), applying expected solar production and expected household usage to each block in turn and clamping the result to the capacity ceiling and the reserve floor. This exposes two things a scalar cannot: the moment the battery would reach full, and the solar energy that would be spilled to the grid after that moment because the battery can absorb no more. Holding stored energy for the single best selling price in the horizon is wrong when the trajectory shows the battery sitting at 100 percent in the meantime, because the free solar arriving during that period is lost. The export selector must therefore choose the best injection price within the window BEFORE projected saturation, not the best price across the whole horizon, and its trigger condition becomes trajectory-based - spill ahead, or energy left over at the end of the horizon - rather than a scalar surplus. Where price or forecast data is coarser than the block length, values are held constant across the sub-blocks of their source period.

## Rationale

_(none)_

## Change log

- `2026-09-29T14:05:38.959633+00:00` — opened
- `2026-09-29T14:05:50.919159+00:00` — resolved (final_answer="The scalar horizon balance is insufficient. The planner must project the battery charge trajectory forward across the horizon in blocks of 15 minutes (block length configurable), applying expected solar production and expected household usage to each block in turn and clamping the result to the capacity ceiling and the reserve floor. This exposes two things a scalar cannot: the moment the battery would reach full, and the solar energy that would be spilled to the grid after that moment because the battery can absorb no more. Holding stored energy for the single best selling price in the horizon is wrong when the trajectory shows the battery sitting at 100 percent in the meantime, because the free solar arriving during that period is lost. The export selector must therefore choose the best injection price within the window BEFORE projected saturation, not the best price across the whole horizon, and its trigger condition becomes trajectory-based - spill ahead, or energy left over at the end of the horizon - rather than a scalar surplus. Where price or forecast data is coarser than the block length, values are held constant across the sub-blocks of their source period.")
