# Phase 0 Research: Price-Aware Load Automation

**Mission**: `price-aware-load-automation-01M3PH8Y`
**Date**: 2026-09-29
**Purpose**: Resolve every unknown in the Technical Context before design, and settle the two spec tensions identified during planning interrogation.

---

## R-01 — Execution host

**Decision**: pyscript, installed through HACS, running inside the Home Assistant Container on the NAS.

**Rationale**: The spec needs file I/O (user config, decision log, derived-series cache), a ~140-block arithmetic projection every five minutes, and later a Modbus connection through the HF2211. That rules out YAML templates and automations entirely, and it rules out the built-in `python_script` integration, which is sandboxed with no imports and no file access. Home Assistant Container has no Supervisor, so no add-on is available — the AppDaemon add-on included. HACS works in Container because it is a custom component rather than an add-on, which leaves pyscript as the lowest-friction host that can do what the spec requires.

**Alternatives considered**:

- *AppDaemon as a second container* — native CPython and the best testability of the options, but adds a service to operate, a long-lived access token, and an API round-trip for every state read. The pure-core structure below delivers the testability benefit without that cost.
- *Custom component* — the most native option and far heavier than one planner warrants.
- *External process against the REST API* — maximum freedom, most moving parts, and another thing to keep running on the NAS.
- *Built-in `python_script`* — rejected outright: no imports, no file I/O.

**Consequence worth preserving**: because all decision logic sits behind a thin adapter, this choice is reversible. Moving to AppDaemon later replaces the adapter, not the planner.

---

## R-02 — pyscript's interpreter, and where that forces the seams

**Decision**: The pure core contains **no file I/O and no pyscript decorators**. Every read and write happens in the adapter layer. The core is plain Python that CPython imports directly for tests.

**Rationale**: This is the single most consequential finding of Phase 0, and it changes the shape of the code.

pyscript does not execute CPython. It runs an asynchronous AST interpreter, and the official reference is explicit about the consequences:

- Functions using `open()`, `read()`, or `write()` block Home Assistant's event loop, because the interpreter shares it. **CORRECTION (WP09 review):** `@pyscript_compile` alone is NOT enough - it makes the function native Python but a native blocking call still runs on the event loop. The function must also be run in an executor thread: use `@pyscript_executor` (compile + executor in one decorator), or `@pyscript_compile` and call it as `task.executor(fn, ...)`. Every file operation in this mission follows that rule.
- `task.executor` accepts "only a regular Python function (e.g., defined in an imported module), not a function defined in pyscript".
- Generators require `@pyscript_compile` to use `yield`.
- Interpreted code is "much slower than regular Python code".
- `@pyscript_compile` runs a function as genuine native Python, escaping all of the above.

The obvious move — decorate core functions with `@pyscript_compile` — **breaks the test story**. Those decorators are injected into pyscript's global scope and simply do not exist under CPython, so `import trajectory` in a pytest run would raise `NameError` at import time. Working around that needs a `try/except NameError` shim in every core module, which is exactly the kind of host-awareness the core is supposed to be free of.

Keeping all I/O at the edge dissolves the problem rather than working around it. The core becomes pure functions over plain data: in go price lists, forecast values, a usage profile and a battery charge; out comes a trajectory and a decision. No `open()`, so no compile requirement, so no decorator, so CPython imports it unmodified.

**Performance check**: the trajectory is ~140 iterations of a handful of arithmetic operations. Even at the order-of-magnitude penalty the docs warn about, this is milliseconds against NFR-001's five-second budget. Interpretation is not a risk here. If measurement ever says otherwise, `@pyscript_compile` behind a shim remains available as a targeted escape hatch for the trajectory function alone — but it is not needed up front and should not be added speculatively.

**Import scope**: code under `<config>/pyscript/modules/` is always importable "regardless of settings" — `allow_all_imports` does not gate it. The pure core therefore imports cleanly with no configuration change. Third-party imports in the *adapter* (see R-07) are a separate question.

**Alternatives considered**:

- *Decorate the core with `@pyscript_compile` + NameError shim* — rejected: puts host knowledge in every core module to buy performance that is not needed.
- *Core performs its own I/O via `task.executor`* — rejected: `task.executor` is a pyscript global, so the core would again be unimportable under CPython.
- *Accept the interpreter for I/O and hope it does not block* — rejected: the docs are unambiguous, and a blocking write every five minutes inside HA's event loop is a real defect, not a theoretical one.

---

## R-03 — Solar forecast retrieval, and a genuine spec tension

**Decision**: A `rest:` sensor declared in `configuration.yaml`, polling `https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}` once per hour, with the response payload captured into entity attributes.

**Rationale**: C-005 requires the forecast service to be "wired into the Home Assistant configuration file". Home Assistant's **built-in Forecast.Solar integration is UI config-flow only — it has no YAML form at all**, so satisfying C-005 with it is impossible. This tension was flagged during planning interrogation rather than resolved silently; the REST sensor is the resolution.

The REST route is also the better technical fit. The built-in integration surfaces aggregate sensors (production today, this hour, next hour); the trajectory needs production *per block across the horizon*. The raw API returns exactly that:

```
https://api.forecast.solar/estimate/:lat/:lon/:dec/:az/:kwp
```

with `result.watt_hours_period` giving energy per period — the series the trajectory consumes — alongside `watts`, `watt_hours`, and `watt_hours_day`.

Site parameters confirmed during specify: `kwp` 8.1 (20 × 405 Wp), `dec` 50, `az` −10 (the API uses 0 = south, negative = east, so ten degrees east of south is −10), lat/lon approximately 51.12 / 3.85 for the installation site. The `watt_hours` counter resets at local midnight, which is Europe/Brussels per C-008.

**Rate budget**: the free public tier allows a single plane, needs no API key, and is rate limited with the remaining allowance returned in response headers. An hourly `scan_interval` is one request per hour; FR-024's faster retry on failure at roughly ten-minute spacing is six per hour. Both sit inside NFR-002's ceiling of twelve.

**Alternatives considered**:

- *Built-in Forecast.Solar integration* — violates C-005 (no YAML configuration path) and exposes aggregates rather than the per-period series the trajectory needs.
- *Fetching the API from inside pyscript* — moves a network call into the planner, duplicates caching and retry logic that Home Assistant already implements for REST sensors, and makes the rate budget harder to reason about.
- *A paid tier with multiple planes* — unnecessary: C-006 records the single-plane limit and the array is a single plane.

---

## R-04 — Forward price data

**Decision**: Read the forward price series from the existing ENTSO-e integration's entity attributes; derive consumption and injection prices in the core.

**Rationale**: The installed integration (`hass-entso-e`) already backs `sensor.entso_prices_current_electricity_market_price`, referenced in the current `configuration.yaml`. It exposes a forward price series on entity attributes as `prices`, each entry carrying `time` and `price`. Tomorrow's prices appear "usually between 12:00 and 15:00", which is precisely the behaviour C-007 records and the reason the horizon is rolling rather than fixed.

The integration is UI config-flow only, but that is not a C-005 problem: C-005 governs the *forecast* service, and the price integration is already installed and configured. Nothing new needs declaring in YAML for prices.

Deriving prices in the core rather than in Home Assistant templates is deliberate. The integration offers a Jinja2 "Price Modifyer Template" field that could apply the provider coefficients, but doing so would scatter the price formula across the UI config of a third-party integration, in direct conflict with C-004's requirement that all tunables live in the user config file — and it would make the four coefficients invisible to the tests.

**Alternatives considered**:

- *Apply coefficients via the integration's price-modifier template* — rejected: violates C-004, hides the formula from tests, and splits consumption and injection handling across two places when the integration offers one field.
- *Add template sensors in `configuration.yaml` for derived prices* — rejected for the same reason, and it would duplicate logic the core must own to satisfy FR-001 and FR-002.

---

## R-05 — Where the numbers live and how copying stays safe

**Decision**: Everything under the Home Assistant config directory. Code overwritten on every copy; `battery_planner/user_config.yaml` never overwritten; `decisions.log` and `cache/` never copied in either direction. The repository ships `user_config.example.yaml`; the live file is gitignored and created once by a deliberate rename.

**Rationale**: Deployment is a manual copy to the NAS, which makes file lifecycle a correctness concern rather than a tidiness one. Three files change for three different reasons and at three different rates, and a careless recursive copy that treats them alike would silently destroy hand-entered provider coefficients, capacity, and alert address. Separating them by directory and by gitignore makes the safe action the default one.

Placing the runtime files under the config directory keeps everything inside the single bind mount already reachable on the NAS, which matters when no File Editor, Samba, or Terminal add-on is available to reach anywhere else.

---

## R-06 — Testing

**Decision**: `pytest` against the pure core, installed bare. No virtualenv scaffolding, no `requirements-dev.txt`, no coverage tooling.

**Rationale**: Tests are not a quality ritual on this mission — they are the **only validation mechanism that exists**. There is no inverter to command (C-001), the battery charge reading is stubbed (C-003), and real weather takes days to produce an interesting case. The negative-injection / positive-consumption band in particular — market price between roughly −0.65 c and +1.17 c per kWh, where V2 is the only rule preventing a loss-making export — may not occur in live data for weeks.

Local toolchain confirmed: Python 3.10.2 on PATH, 3.12.5 via the `py` launcher, `pytest` not yet installed. The Home Assistant container runs a newer Python, so the core targets 3.10-compatible syntax to remain runnable on both without conditional code.

**Coverage required**:

| Area | What the tests pin down |
|---|---|
| Trajectory (FR-008, FR-029, FR-031, FR-032) | Golden fixtures: handmade price/solar/usage series with a known saturation block, spill quantity, and reserve-breach block |
| Each veto (FR-010, FR-011) | Fires on its condition; forbids only its own action class |
| Each selector (FR-012 – FR-017) | Fires on its condition; proposes the documented action |
| Fall-through ordering (FR-009) | A vetoed proposal advances to the *next* selector, never restarts — the defect found twice during specify |
| Price band (FR-011) | Injection negative while consumption positive; export suppressed, evaluation continues |
| Cache (FR-033 – FR-038) | Stale series refreshed; deletion changes no decision |

---

## R-07 — Import posture for the adapter

**Decision**: Assume `allow_all_imports: true` in the pyscript configuration; verify the default allowlist on the real install before relying on any specific standard-library import.

**Rationale**: The pure core needs no imports beyond the standard library and is unaffected either way, since `<config>/pyscript/modules/` is importable regardless of settings. The *adapter* must parse the YAML user config and read and write JSON cache files, which means at minimum `yaml` (third-party) and `json`.

pyscript restricts imports by default to a limited set of built-in packages, and `allow_all_imports` lifts that restriction. The exact contents of the default allowlist are not stated precisely enough in the documentation to build on, and this is a one-line configuration change with a well-understood effect, so the plan assumes it rather than guessing at what is permitted.

**Flagged as verify-on-install, not assumed**: whether `json` alone would have sufficed without the setting. If it would, and if a JSON user config were acceptable, the setting could be dropped — but a YAML config file is the better fit for a Home Assistant installation where every other configuration file is YAML, and the user config is meant to be hand-edited.

---

## R-08 — Supply-chain posture

**Decision**: Zero new runtime dependencies. The only new component is pyscript itself, installed through HACS.

Per DIRECTIVE_051 and the `supply-chain-install-safety` tactic:

- **Registry authenticity** — pyscript is installed via HACS from the `custom-components/pyscript` repository, not from an arbitrary archive. HACS itself is already installed and in use on this system.
- **Package freshness** — pin the pyscript version installed through HACS and record it in the plan's Technical Context, so an upgrade is a deliberate act rather than a silent one. The `croniter` dependency that backs `@time_trigger`'s cron syntax arrives as part of pyscript and is not separately managed.
- **Lifecycle-script discipline** — not applicable in the npm sense; no `preinstall`/`install`/`postinstall` hooks execute. HACS copies files into `custom_components/`. There is no `pip install` of project dependencies at runtime and no package manager in the execution path.
- **Node Active LTS** — not applicable; no JavaScript in this mission.
- **Project dependencies** — the core has none by construction, which is the strongest possible supply-chain position: the only third-party code in the decision path is the host itself.

**Adversarial evidence**: no adversarial-squad challenge pass was run for this dependency decision. Disposition: **deferred_with_rationale** — the decision adds a single, already-widely-deployed Home Assistant component through the package manager the user already runs, introduces no transitive project dependencies, and touches no build or install hook. The contested surface is small enough that a challenge pass would not change the outcome. If pyscript is later replaced by an AppDaemon container, that decision adds a container image and a network-exposed token and **must** receive a challenge pass before adoption.

---

## Open items carried into design

| # | Item | Why it is not blocking | Where it lands |
|---|---|---|---|
| 1 | Exact pyscript default import allowlist | `allow_all_imports: true` resolves it deterministically; only affects whether that line is needed | `quickstart.md` install step |
| 2 | ENTSO-e attribute key names on this specific install | Adapter reads them in one place; a key-name mismatch is a five-minute fix, not a design change | Adapter, IC-11 |
| 3 | Round-trip efficiency and max charge/discharge power for the real battery | Configurable with documented defaults (90%, 5 kW); the trajectory takes them as inputs | `user_config.example.yaml` |
| 4 | Whether the forecast REST payload exceeds attribute size limits | Only affects how much of the payload is retained; the block series is small | Adapter, IC-03 |
