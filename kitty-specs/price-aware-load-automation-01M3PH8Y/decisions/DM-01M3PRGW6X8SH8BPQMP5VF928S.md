# Decision Moment `01M3PRGW6X8SH8BPQMP5VF928S`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `plan`
- **Slot key:** `plan.runtime.execution-host`
- **Input key:** `execution_host`
- **Status:** `resolved`
- **Created:** `2026-09-29T14:18:57.117896+00:00`
- **Resolved:** `2026-09-29T14:21:44.478682+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

What executes the planner on the Home Assistant box - pyscript, the AppDaemon add-on, a custom component, or an external process - and how does code in this repo reach that box?

## Options

- pyscript
- appdaemon
- custom-component
- external-process
- Other

## Final answer

Home Assistant runs as a Container install (no Supervisor), with HACS installed. The container form factor has already caused friction for the user. The decisive consequence is that NO add-ons are available - the AppDaemon add-on, File Editor, Samba, Terminal and VS Code add-ons all require Supervisor. HACS works because it is a custom component, not an add-on, so pyscript is installable. Remaining viable hosts are therefore: pyscript via HACS (in-config, lowest friction), AppDaemon run as a SEPARATE container alongside HA (better testability, another service to operate, talks to HA over the API with a long-lived token), or a custom component (heaviest). The built-in python_script integration remains ruled out: it is sandboxed with no imports and no file I/O, which the cache, log and user config files all require. The repository is confirmed to be a partial mirror of the HA config directory, holding configuration.yaml but none of its include targets. The mechanism by which this repo reaches the HA box was not yet specified.

## Rationale

_(none)_

## Change log

- `2026-09-29T14:18:57.117896+00:00` — opened
- `2026-09-29T14:21:44.478682+00:00` — resolved (final_answer="Home Assistant runs as a Container install (no Supervisor), with HACS installed. The container form factor has already caused friction for the user. The decisive consequence is that NO add-ons are available - the AppDaemon add-on, File Editor, Samba, Terminal and VS Code add-ons all require Supervisor. HACS works because it is a custom component, not an add-on, so pyscript is installable. Remaining viable hosts are therefore: pyscript via HACS (in-config, lowest friction), AppDaemon run as a SEPARATE container alongside HA (better testability, another service to operate, talks to HA over the API with a long-lived token), or a custom component (heaviest). The built-in python_script integration remains ruled out: it is sandboxed with no imports and no file I/O, which the cache, log and user config files all require. The repository is confirmed to be a partial mirror of the HA config directory, holding configuration.yaml but none of its include targets. The mechanism by which this repo reaches the HA box was not yet specified.")
