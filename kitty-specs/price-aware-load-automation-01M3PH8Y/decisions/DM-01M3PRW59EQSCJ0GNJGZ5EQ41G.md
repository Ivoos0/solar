# Decision Moment `01M3PRW59EQSCJ0GNJGZ5EQ41G`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `plan`
- **Slot key:** `plan.runtime.file-placement`
- **Input key:** `file_placement`
- **Status:** `resolved`
- **Created:** `2026-09-29T14:25:06.862296+00:00`
- **Resolved:** `2026-09-29T14:26:45.078056+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Where should the user config file, the decision log, and the cache file live on the NAS relative to the HA config directory, and how is the hand-edited user config protected from being overwritten by a manual copy of the repo?

## Options

_(none)_

## Final answer

Agreed layout, all under the Home Assistant config directory so everything sits in the existing bind mount: configuration.yaml carries the forecast.solar REST sensor and the pyscript block; pyscript/battery_planner.py is the thin adapter; pyscript/modules/ holds the pure testable core; pyscript/inverter.py is the C-002 boundary that writes intent to the log and transmits nothing; and battery_planner/ holds the runtime files - user_config.yaml, decisions.log, and cache/. Lifecycles are explicitly separated for safe manual copying: code is overwritten on every copy; the user config is hand-edited on the NAS and must NEVER be overwritten; the log and cache are written only by the planner on the NAS and are never copied in either direction. The repository ships user_config.example.yaml; the real user_config.yaml is gitignored and excluded from the copy set, created once by a deliberate rename at first install. After that, copying the repo over the top is always safe.

## Rationale

_(none)_

## Change log

- `2026-09-29T14:25:06.862296+00:00` — opened
- `2026-09-29T14:26:45.078056+00:00` — resolved (final_answer="Agreed layout, all under the Home Assistant config directory so everything sits in the existing bind mount: configuration.yaml carries the forecast.solar REST sensor and the pyscript block; pyscript/battery_planner.py is the thin adapter; pyscript/modules/ holds the pure testable core; pyscript/inverter.py is the C-002 boundary that writes intent to the log and transmits nothing; and battery_planner/ holds the runtime files - user_config.yaml, decisions.log, and cache/. Lifecycles are explicitly separated for safe manual copying: code is overwritten on every copy; the user config is hand-edited on the NAS and must NEVER be overwritten; the log and cache are written only by the planner on the NAS and are never copied in either direction. The repository ships user_config.example.yaml; the real user_config.yaml is gitignored and excluded from the copy set, created once by a deliberate rename at first install. After that, copying the repo over the top is always safe.")
