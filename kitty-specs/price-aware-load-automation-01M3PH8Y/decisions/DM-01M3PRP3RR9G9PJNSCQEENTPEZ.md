# Decision Moment `01M3PRP3RR9G9PJNSCQEENTPEZ`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `plan`
- **Slot key:** `plan.runtime.host-and-deployment`
- **Input key:** `host_and_deployment`
- **Status:** `resolved`
- **Created:** `2026-09-29T14:21:48.697217+00:00`
- **Resolved:** `2026-09-29T14:25:01.962200+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Given HA Container with HACS and no add-ons: host the planner in pyscript inside the config directory, or in a separate AppDaemon container? And how does code in this repo reach the HA config directory - is HA running on this same Windows machine via Docker Desktop, or on a separate box?

## Options

- pyscript
- separate-appdaemon-container
- Other

## Final answer

Host the planner in pyscript, installed through HACS. Home Assistant Container runs on the user's NAS; this Windows repository is a partial mirror of its config directory and files are copied to the NAS MANUALLY for now, with no automated deployment. Architecture agreed: a pure Python core holding all decision logic - price derivation, the 15-minute trajectory, saturation and spill detection, vetoes and selectors, and the cache - with zero Home Assistant imports, so it is testable with ordinary pytest on the Windows machine without Home Assistant present; a thin pyscript adapter that reads sensor state, calls the core, and writes the log; and the separate inverter boundary file already required by C-002. This keeps the host choice reversible: moving to an AppDaemon container later would replace the adapter, not the planner. Because deployment is a manual copy, the deployable surface must be small and explicitly enumerated, and the quickstart must state exactly which files go where on the NAS.

## Rationale

_(none)_

## Change log

- `2026-09-29T14:21:48.697217+00:00` — opened
- `2026-09-29T14:25:01.962200+00:00` — resolved (final_answer="Host the planner in pyscript, installed through HACS. Home Assistant Container runs on the user's NAS; this Windows repository is a partial mirror of its config directory and files are copied to the NAS MANUALLY for now, with no automated deployment. Architecture agreed: a pure Python core holding all decision logic - price derivation, the 15-minute trajectory, saturation and spill detection, vetoes and selectors, and the cache - with zero Home Assistant imports, so it is testable with ordinary pytest on the Windows machine without Home Assistant present; a thin pyscript adapter that reads sensor state, calls the core, and writes the log; and the separate inverter boundary file already required by C-002. This keeps the host choice reversible: moving to an AppDaemon container later would replace the adapter, not the planner. Because deployment is a manual copy, the deployable surface must be small and explicitly enumerated, and the quickstart must state exactly which files go where on the NAS.")
