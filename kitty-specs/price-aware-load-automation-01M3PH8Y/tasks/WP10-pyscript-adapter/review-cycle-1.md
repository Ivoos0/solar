---
affected_files: []
cycle_number: 1
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-30T04:53:08Z'
reviewer_agent: user
wp_id: WP10
---

# WP10 review, cycle 1: CHANGES REQUESTED

The adapter is well built and its CPython tests pass (385 passed). 13 of 16 targeted mutations are caught. Price halt and forecast degrade take different paths. The alert is rate-limited. All file I/O runs in @pyscript_executor helpers. The trajectory is never cached.

Deployed, though, it would produce **no decisions at all**. Details and proof are in Issue 1.

## Required in WP10

**Issue 1 (BLOCKER): the core runs under pyscript's interpreter, and on a normal cycle it crashes.**
Files under `<config>/pyscript/modules/` are *pyscript* modules. pyscript loads them with its own AST interpreter through `GlobalContext.module_import`. It falls back to a native `importlib` import only when a module is NOT found there (eval.py `ast_import`). So `import prices`, `import rules` and the others are interpreted. R-02's premise that "the core is plain Python" holds for pytest, not for HA.

Several constructs in the core fail under the interpreter:
- **Generator expressions.** There is no `ast_generatorexp`, so they raise `NotImplementedError`. Sites: prices.py:79, config.py:97 (`fingerprint`), capacity.py:157, decision.py:120/206/207, rules.py:163/328/351/354/421.
- **`@property`.** rules.py:105 `Proposal.action_class` raises `TypeError: 'EvalFunc' object is not callable`.
- **Pyscript functions as native callbacks.** `max(starts, key=_utc)` at prices.py:117 raises `TypeError: '>' not supported between ... 'coroutine'`.
- **`DecisionRecord.__post_init__`.** A pyscript method called from the native dataclass `__init__` never validates: `R(-1)` constructed without error.

Proof: I ran the real `battery_planner.py`, with the real modules imported from a pyscript/modules dir, through pyscript master's own `AstEval` (v2.1.0; HA stubbed). Results:
- Prices present: `LOG.ERROR battery_planner: cycle failed, trigger kept: ... not implemented ast ast_generatorexp`. decisions.log stays empty. No alert.
- Prices unavailable: the HALT line is written and notify is called. The halt path works.

In production, every cycle with prices would fail silently. SC-001 fails, and no alert goes out.

Fix, preferably inside this file: load the pure core **natively**. Use a `@pyscript_executor` loader (native imports must also stay off the loop) that puts the core directory on `sys.path` and calls `importlib.import_module(...)` for config, prices, series, battery, trajectory, capacity, rules, decision and cache. Bind the returned modules as the adapter's globals. Keep `inverter` as a pyscript import, because it uses `log` and `@pyscript_executor`. Its interpreted `import decision` only calls `format_record`, which is interpreter-safe.

Caveats:
- (a) The bare names `config`, `cache`, `series` and `decision` on `sys.path[0]` can shadow same-named top-level modules. Prefer a dedicated directory, or namespaced loading.
- (b) Native modules do not hot-reload on edit. Document that a core code change needs an HA restart.
- (c) WP12's peak_guard imports the same core and has the same defect. Tell the orchestrator.

The alternative is relocating the core out of pyscript/modules into a package, which is cross-WP. Add a regression guard:
- a test that the adapter binds natively loaded core modules;
- an AST lint over every file pyscript interprets (the adapter and inverter.py) forbidding GeneratorExp, `@property`/`@staticmethod`/`@classmethod`, dunder methods, `yield`, `match`, and `key=`/`map`/`filter` taking a module-level pyscript function.

**Issue 2 (required): a skipped cycle must be recorded (NFR-001; T043 step 3).**
`@task_unique(..., kill_me=True)` kills the NEW invocation (docs: "the current task is killed if another task that is running previously called task.unique"). Skips are therefore silent. Worse, a hung cycle (for example an executor blocked on NAS I/O) silences the planner forever with no trace. Replace it with a module-level busy marker (`_cycle_started_at`), checked and set at the top of the trigger. That is atomic in pyscript, because there is no await between the check and the set. When a *due* cycle is skipped, log a warning and write a SKIP line. Log an error if the marker is older than roughly 2 intervals. Clear it in `finally`.

**Issue 3 (required): FR-027 / SC-014, every decision that used a cached series must state its age.**
Today only a stale cache adds `cache_age_<kind>=`. A fresh cache hit adds nothing, even though `cache.evaluate` returns the age marker as `detail` for "fresh" precisely for SC-014. Worse, if the forecast entity is down but the solar cache is fresh, the record shows no sign of it at all. Append the marker whenever a cached series is used, fresh or stale. Do not add it when the series was just rebuilt. Adjust the tests.

**Issue 4 (tests to add): three mutations survived.**
- (a) Alert send failure: `service.call` raises. The halt must NOT be marked alerted, the next cycle must retry, and `alerted=none` must be written. The mutation "treat failed send as sent" survives today.
- (b) `_sensor_kw` W to kW conversion (unit "W"). The mutation survives.
- (c) A DST fall-back cycle (2026-10-25, 02:30 local, both occurrences). The price map must not be re-keyed in local time. The mutation "re-key prices in local time" survives today; it loses 2 blocks in the repeated hour.

## Cross-WP items for the orchestrator (not blocking this WP, but please record them)

- **Notifier name.** `NOTIFY_SERVICE = "notify"` is a guess. configuration.yaml defines no notify platform at all, so `notify.notify` will most likely not exist and FR-021's email is never delivered. `_send_alert` then logs an error every cycle.
  - Add `alerts.notify_service`, required, to the config (WP01) and the example config.
  - Add an SMTP notify platform with `!secret` credentials to configuration.yaml (WP08).
  - `config.from_dict` ignores unknown keys, so the adapter could read the raw value as a stop-gap.
- **Decision logic in the adapter.** The GUARD and NOGRID overrides replace the decision with idle *after* `rules.decide`. That skips FR-009 fall-through to later selectors, and it contradicts rules.py's documented "grid_state None == capacity inactive".
  - Both are conservative and clearly recorded. `Decision(...)` matches rules.Decision's field order, and it renders `GUARD(suppressed S1 charge)` with selector S6.
  - They belong in rules as vetoes that forbid `grid_charge`.
  - `QUARTER_AVG_ENTITY` and `MONTH_PEAK_ENTITY` should be config like `offtake_sensor`. A wrong name silences grid charging permanently, although visibly.
- **Log writer contract.** inverter-boundary.md rule 4 says "apply is the only writer of the decision log". The adapter appends HALT and RECOVERED lines through its own helper, and formats the RECOVERED line itself. Either extend the boundary (`inverter.record_line` / `decision.format_recovery`) or amend the contract.
- **Usage history.** `read_usage_history()` returns [], so the usage profile is all zero. Every decision is made on zero household load, which makes the trajectory over-optimistic.
  - In a scratch end-to-end run at 20:05 it chose `action=export 5.00kW` (S3) because of projected saturation tomorrow. With zero load, S4 (breach) can never fire.
  - Grid import (`sensor.energy_consumed_total`) is NOT a valid substitute. See the review report for the design question.
