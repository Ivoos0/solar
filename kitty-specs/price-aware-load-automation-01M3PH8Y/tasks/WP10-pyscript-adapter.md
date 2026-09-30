---
work_package_id: WP10
title: pyscript adapter and scheduling
dependencies:
- WP07
- WP08
- WP09
requirement_refs:
- FR-021
- FR-022
- FR-024
- FR-034
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T043
- T044
- T045
- T046
- T047
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/battery_planner.py
create_intent:
- pyscript/battery_planner.py
execution_mode: code_change
owned_files:
- pyscript/battery_planner.py
role: implementer
tags: []
tracker_refs: []
---

## ⚡ Do This First: Load Agent Profile

Before reading anything else in this file, load your assigned agent profile:

```
/ad-hoc-profile-load python-pedro
```

This establishes your identity, governance scope, and boundaries for this work package. Do not
begin implementation until the profile is loaded.

## Objective

The only code in this mission that knows Home Assistant exists. Trigger every five minutes, read
state and files, call the pure core, hand the decision to the inverter boundary.

Satisfies **FR-021, FR-022, FR-024, FR-034**. Supports **NFR-006, NFR-009**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP10 --agent <name>`. Depends on WP07, WP08 and WP09 — this
  is the last work package and it assembles everything.

## Your job, and what is not your job

**Yours**: every read and every write. Home Assistant state, the user config file, the cache files,
the halt state, the alert. All I/O in this mission is either here or in WP09's boundary.

**Not yours**: any decision. No price arithmetic, no trajectory, no rule evaluation, no formatting.
If you find yourself writing a comparison against a price or a charge level, it belongs in
`pyscript/modules/` — stop and put it there.

That split is what makes the whole test suite possible, and it is why this file should stay small
enough to review by eye in one sitting.

## Context you need

- `plan.md` — the three-layer structure and why the seam exists
- `research.md` R-02 (interpreter and I/O), R-04 (price attributes), R-07 (imports)
- `contracts/cache-file.md`, `contracts/user-config.md`, `contracts/inverter-boundary.md`
- `quickstart.md` — troubleshooting table; its symptoms map to failures in this file
- **WP08's T039 note** recording the real ENTSO-e entity and attribute names on this install

---

### T043 — Build the adapter skeleton with a 5-minute trigger

**Purpose**: The scheduled entry point, running at the right cadence without overlapping itself.

**Steps**:

1. Create `pyscript/battery_planner.py`.
2. Use pyscript's cron trigger. Fire **every minute** and gate the body on elapsed time — step 6
   explains why this rather than `cron(*/5 * * * *)`:
   ```python
   @time_trigger("cron(* * * * *)")
   @task_unique("battery_planner_cycle")
   def battery_planner_cycle():
       ...
   ```
3. `@task_unique` is what satisfies NFR-001's non-overlap requirement: a cycle that would start while
   the previous one is still running is skipped rather than queued. Record the skip so a persistently
   slow cycle is visible rather than silent.
4. Import the core from `pyscript/modules/` — always importable, regardless of `allow_all_imports`.
5. Wrap the whole cycle body so an unexpected exception is logged and the trigger survives. A crash
   that kills the trigger would stop the planner silently until the next Home Assistant restart,
   which is the worst possible failure mode for something whose only output is a log file.
6. **Make `evaluation_interval_minutes` actually work.** The cron expression is fixed at module load,
   so it cannot be built from a config value read at runtime. Rather than ship a field that looks
   configurable and is not, fire the trigger **every minute** and gate the body on elapsed time:

   ```python
   @time_trigger("cron(* * * * *)")
   @task_unique("battery_planner_cycle")
   def battery_planner_cycle():
       if minutes_since(last_run) < config.evaluation_interval_minutes:
           return
       ...
   ```

   The skipped invocations cost one timestamp comparison. In exchange the field honours any value of
   one minute or more, takes effect on the next config reload with no restart, and NFR-006 holds for
   it exactly as it does for every other tunable.
7. Record each cycle's duration in milliseconds and pass it into the decision record
   (NFR-009). Measure the whole body — reads, projection, rules, write — not just the projection, so
   NFR-001's five-second budget is checkable from a week of logs rather than trusted.

**Files**: `pyscript/battery_planner.py` (new)

**Validation**:
- A cycle fires every five minutes after Home Assistant restarts
- Changing `evaluation_interval_minutes` to 10 halves the record rate, with no restart
- An exception inside the cycle is logged and the next cycle still fires
- Overlapping invocations are skipped, not queued
- Every record carries a `took=` duration

---

### T044 — Load the user config file from the adapter

**Purpose**: Read and parse `user_config.yaml`, hand the dict to the pure core.

**Steps**:

1. Read `battery_planner/user_config.yaml` from the Home Assistant config directory.
2. **The read must be run off the event loop with `@pyscript_executor` (or `@pyscript_compile` plus `task.executor`; `@pyscript_compile` alone does NOT move work off the loop)** — unguarded
   `open()` blocks Home Assistant's event loop (`research.md` R-02).
3. Parse with `yaml.safe_load` — never `yaml.load`. This needs `allow_all_imports: true`, which WP08
   declared.
4. Hand the parsed **dict** to WP01's `config.from_dict`. The core does the validating; the adapter
   does the reading. Do not validate here.
5. A missing file, a YAML parse error, or a `ConfigError` from the core is a **startup failure**, not
   a degraded cycle. Log it prominently and produce no decision — a planner running on invalid
   configuration would emit confident, wrong decisions, which is worse than emitting none.
6. Cache the parsed config in a module-level variable and reload when the file's mtime changes, so
   288 cycles a day do not re-read and re-parse an unchanged file. That also satisfies NFR-006:
   a changed value takes effect within one cycle.

**Files**: `pyscript/battery_planner.py` (continues)

**Validation**:
- A valid config loads and its values appear in decisions
- Editing the file changes behaviour within one cycle, without a restart
- A missing file logs clearly and produces no decision
- An invalid value produces the core's `ConfigError` message, naming the field

---

### T045 — Read prices and forecast from Home Assistant state

**Purpose**: Pull the two upstream series out of entity state into plain data.

**Steps**:

1. Read the ENTSO-e price entity's forward series from its attributes. **Use the entity and
   attribute names WP08's T039 recorded from the live install** — the plan expects
   `attributes.prices` with `time`/`price` entries, but that is unconfirmed (`research.md` open
   item 2).
2. Read `sensor.forecast_solar_estimate`'s `watt_hours_period` attribute, created by WP08.
3. Convert both into the plain structures WP02 and WP03 expect. Parse timestamp strings into
   timezone-aware datetimes here — the core should receive real datetimes, not strings.
4. Distinguish the two failure modes sharply, because they have opposite consequences:
   - **Prices missing, stale, `unknown`, or `unavailable`** → halt (T047). Without prices no decision
     can be made, and inaction costs money.
   - **Forecast missing or failed** → **not** a halt. Call WP03's zero-solar fallback, mark the cycle
     degraded, and schedule a faster retry (FR-023, FR-024).
5. For FR-024's faster retry: the REST sensor polls hourly on its own schedule, so the adapter cannot
   force a fetch directly. Track consecutive forecast failures and, past a threshold, call Home
   Assistant's `homeassistant.update_entity` service on the REST sensor at roughly
   `forecast_retry_minutes` spacing. Keep a counter so the retry rate stays inside NFR-002's 12/hour
   budget — at 10-minute spacing that is 6/hour, leaving room for the hourly poll.
6. Treat a price series that exists but ends in the past as missing, not present. A stale series is
   exactly as useless as an absent one, and more dangerous because it looks fine.

**Files**: `pyscript/battery_planner.py` (continues)

**Validation**:
- Prices and forecast parse into series the core accepts
- An `unavailable` price entity triggers a halt
- A stale price series — last block already elapsed — triggers a halt
- A missing forecast yields a zero-solar cycle with a degraded marker, not a halt
- Retries stay within the rate budget

---

### T046 — Read and write the cache files

**Purpose**: Persist and restore the derived series, using WP07's verdicts.

**Steps**:

1. Read `battery_planner/cache/solar.json` and `usage.json`, executor-guarded like every other file
   operation.
2. Pass each raw payload to WP07's `evaluate(...)` along with `now` and the config. **Act on its
   verdict; do not re-implement any of the judgement here.**
   - `usable=True`, `reason="ok"` → use it
   - `reason` in `missing`/`unparseable`/`invalidated` → rebuild from source, then write
   - `usable=True`, `reason="stale"` → try to rebuild; if that is impossible, **use it anyway** and
     add `cache_age_<kind>=<age>` to the cycle's degraded markers (FR-036)
3. Write with `json.dumps` on the dict WP07's `to_dict` produced — the core returns a dict precisely
   so that serialisation lives here.
4. Write **atomically**: write to a temporary file and rename. A Home Assistant restart mid-write
   would otherwise leave a truncated file, which WP07 would correctly treat as a miss — recoverable,
   but a half-written cache is easy to avoid and confusing to debug.
5. Create `battery_planner/cache/` if absent.
6. Feed the usage profile `config.usage_history_weeks` weeks of per-interval kWh history (not cumulative meter totals) and report overall history coverage - distinct days actually present against the window - in the degraded markers when short (FR-005, FR-059); per-slot `sample_days` is a sample count, not the coverage signal. Rebuild the usage profile **daily**, not every cycle. It is the expensive series (~672 buckets
   over the trailing window) and rebuilding it 288 times a day is the cost this whole cache exists
   to avoid.
7. **Never cache the trajectory** (FR-034). It depends on live battery charge and on which blocks
   have elapsed; caching it would freeze the projection while its inputs moved underneath.

**Files**: `pyscript/battery_planner.py` (continues)

**Validation**:
- A cache miss rebuilds and writes; the next cycle reads it back
- Deleting `cache/` mid-run changes no decision (SC-013)
- A stale-but-usable series produces a decision with a `cache_age_` marker
- The usage profile is rebuilt daily, not per cycle
- No trajectory is ever persisted

---

### T047 — Orchestrate the cycle and handle halt plus alerting

**Purpose**: Assemble everything, and get the two degraded modes right.

**Steps**:

1. The full cycle, in order:
   ```
   config      = load_config()
   prices      = read_prices()          -> missing? halt and return
   forecast    = read_forecast()        -> missing? zero fallback + degraded
   solar       = cached or rebuilt solar series
   usage       = cached or rebuilt usage profile
   charge      = inverter.read_charge_percent()      # stubbed this mission; `import inverter` (it is a pyscript MODULE under pyscript/modules/, the only place scripts can import from)
   battery     = battery.from_percent(charge, config, is_stubbed=True)
   trajectory  = trajectory.project(battery, solar, usage, config)
   decision    = rules.decide(trajectory, prices, battery, config)
   record      = decision_mod.build(decision, trajectory, battery, prices_now, degraded)
   inverter.apply(decision.action, decision.target_power_kw, record)
   ```
2. **Halt handling** (FR-021): when prices are unavailable, produce no decision, write a halt record
   via WP06's `format_halt`, and send an alert. Keep halt state in a module-level variable:
   whether halted, why, when it started, when the last alert went out.
3. **Alert rate limiting** (FR-022, NFR-003): alert on **entry** into the halt state, then only once
   per `realert_minutes`. At 288 cycles a day, alerting per cycle would send 288 emails — which
   would train the homeowner to ignore them, defeating the purpose entirely.
4. Send through Home Assistant's notify service, not an SMTP client. That keeps email credentials in
   `secrets.yaml`, which `.gitignore` already excludes (NFR-007). **No credential ever appears in
   this file or in `user_config.yaml`** — only the destination address.
5. **Recovery** (FR-023 scenario 3): when prices return, clear the halt state, resume deciding, and
   record the recovery so the log shows the outage's start and end.
6. Assemble `degraded` markers from every source: `soc_stubbed` (always this mission),
   `solar_zero_fallback`, `cache_age_*`, `usage_samples=N` when the profile is short.

**Files**: `pyscript/battery_planner.py` (continues, ~250 lines total for the file)

**Validation**:
- A normal cycle produces one decision record within five minutes
- A price outage produces a halt record and exactly **one** email, then silence until the re-alert
  interval elapses
- Price recovery resumes decisions and records the recovery
- Every cycle this mission carries `soc_stubbed`
- No credential appears anywhere in the file

---

## Definition of Done

- A five-minute trigger runs without overlapping; exceptions do not kill it
- Config, prices, forecast and cache are all read with guarded I/O
- **Every** file operation runs in an executor thread (`@pyscript_executor`, or `@pyscript_compile` + `task.executor`) - never merely `@pyscript_compile`d
- Missing prices halt and alert once; a missing forecast degrades to zero solar
- Cache verdicts come from WP07; the trajectory is never cached
- The decision goes to `inverter.apply` and nowhere else
- **No decision logic in this file** — no price comparisons, no charge thresholds
- Deployed to the NAS, a record appears in `decisions.log` within five minutes with no blocking-I/O
  warning in the Home Assistant log

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Unguarded file I/O** in the event loop | Every `open()` compiled or executor-dispatched; the symptom is HA stalling every 5 minutes |
| **288 emails a day** | Alert on entry, then once per re-alert interval |
| Treating a forecast failure as a halt | Only prices halt; the forecast degrades to zero |
| A stale price series accepted as valid | Check the series ends in the future, not just that it exists |
| Decision logic creeping into the adapter | Any price or charge comparison belongs in `modules/` |
| Caching the trajectory for speed | FR-034 forbids it — live charge and elapsed blocks change every cycle |
| Rebuilding the usage profile every cycle | Daily; it is the expensive one |
| SMTP credentials in the config file | Use the notify service; credentials stay in `secrets.yaml` |
| Wrong ENTSO-e attribute name | Use WP08's T039 note; the symptom is a constant halt with a visibly healthy sensor |
| An exception killing the trigger permanently | Wrap the cycle body; a dead trigger is silent until restart |

## Reviewer guidance

**First, grep every `open()` in the file.** Each must sit inside a function that runs in an executor
thread: `@pyscript_executor`, or `@pyscript_compile` called through `task.executor`. `@pyscript_compile`
alone is NOT enough - it only makes the function native, and a native blocking call still blocks the
event loop (this exact defect was caught in the WP09 review). This is the highest-probability defect in the work package because the code
reads as perfectly normal Python, and the consequence appears only on the NAS as Home Assistant
becoming intermittently sluggish — a symptom almost nobody traces back to a log write.

**Second, look for decision logic.** Search for comparison operators near prices, charge levels or
thresholds. The adapter reads, calls, and writes. Anything resembling a rule has escaped from
`pyscript/modules/` and takes the test suite's coverage with it.

**Third, check the alert path.** Confirm the re-alert interval is honoured and that alerting goes
through the notify service rather than an SMTP client. Then confirm no credential appears anywhere in
the file — the address is configuration, the credentials are not.

**Fourth, confirm the two degraded modes are genuinely different.** Prices halt; forecast degrades.
If both take the same path, one of FR-021 or FR-023 is unimplemented, and the symptom — a planner
that stops working on a cloudy API outage — would look like a much deeper problem than it is.

**Finally, read the whole file end to end.** It should be short enough to hold in your head. If it is
not, logic has leaked in from the core, and the seam the entire plan rests on has started to close.
