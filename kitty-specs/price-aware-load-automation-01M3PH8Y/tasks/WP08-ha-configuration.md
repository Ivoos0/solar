---
work_package_id: WP08
title: Home Assistant configuration wiring
dependencies: []
requirement_refs:
- FR-026
- FR-042
- FR-043
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T037
- T038
- T048
- T049
- T039
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: implementer-ivan
agent: claude
authoritative_surface: configuration.yaml
create_intent: []
execution_mode: code_change
owned_files:
- configuration.yaml
role: implementer
tags: []
tracker_refs: []
---

## ⚡ Do This First: Load Agent Profile

Before reading anything else in this file, load your assigned agent profile:

```
/ad-hoc-profile-load implementer-ivan
```

This establishes your identity, governance scope, and boundaries for this work package. Do not
begin implementation until the profile is loaded.

## Objective

Declare the forecast.solar REST sensor and the pyscript block in `configuration.yaml`, so forecast
data arrives as ordinary Home Assistant state and pyscript can run the planner.

Satisfies **FR-026, FR-042, FR-043**. Enforces **NFR-011**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP08 --agent <name>`. **No dependencies** — this can start
  immediately, in parallel with WP01.

## Context you need

- `research.md` R-03 — why a REST sensor rather than the built-in integration
- `quickstart.md` — the YAML this work package produces appears there too; keep them consistent
- The existing `configuration.yaml`, which already defines the price template sensors and utility meters

**This is the only work package that touches `configuration.yaml`.** It is an existing, working file
that runs a live Home Assistant instance. Add to it; do not restructure it, do not reformat
unrelated sections, and do not "tidy" the existing template sensors.

## The constraint that shaped this

C-005 requires the forecast service to be wired into the Home Assistant **config file**. Home
Assistant's built-in Forecast.Solar integration is **UI config-flow only — it has no YAML form at
all**, so it cannot satisfy C-005.

The REST sensor is the resolution, and it is also the better technical fit: the built-in integration
exposes aggregates (production today, this hour, next hour) while the trajectory needs production
**per period across the horizon**, which is exactly what the raw API's `watt_hours_period` provides.

---

### T037 — Add the forecast.solar REST sensor to `configuration.yaml`

**Purpose**: Bring per-period solar forecast into Home Assistant state, within the free tier's rate
budget.

**Steps**:

1. Add a `rest:` section (or extend one if present) with:
   ```yaml
   rest:
     - resource: "https://api.forecast.solar/estimate/51.12/3.85/50/-10/8.1"
       scan_interval: 3600
       sensor:
         - name: "Forecast Solar Estimate"
           value_template: "{{ value_json.result.watt_hours_day.values() | list | first }}"
           json_attributes_path: "$.result"
           json_attributes:
             - watt_hours_period
           unit_of_measurement: "Wh"
   ```
2. **URL path order is `lat/lon/declination/azimuth/kwp`** — `51.12/3.85/50/-10/8.1` for this site.
   Azimuth uses forecast.solar's convention: **0 = south, negative = east**, so `-10` is ten degrees
   east of south. Getting the sign backwards would model a south-west roof and shift every
   prediction later in the day by roughly an hour.
3. `scan_interval: 3600` is one request per hour. The free tier is rate limited (and returns the
   remaining allowance in response headers), and NFR-002 caps total fetches at 12/hour including
   WP10's failure retries at ~10-minute spacing. One hourly poll leaves ample headroom.
4. The **state** is a summary figure; the useful payload rides on the `watt_hours_period` attribute.
   That attribute is what WP03's `solar_series` consumes and what WP10's adapter reads.
5. Do not add an API key. The free public tier needs none, supports one plane, and this array is a
   single plane (C-006).

**Files**: `configuration.yaml` (edit)

**Validation**:
- Home Assistant restarts with no configuration error
- `sensor.forecast_solar_estimate` exists and has a `watt_hours_period` attribute containing
  timestamp-keyed watt-hour values
- Developer Tools → States shows the attribute populated within an hour of restart

**Watch for**: a REST sensor state longer than 255 characters is truncated. That is why the payload
goes to an attribute and the state holds only a summary. If the attribute itself proves too large,
note it — `research.md` lists this as open item 4 — but do not solve it by moving the payload into
the state.

---

### T038 — Add the pyscript block to `configuration.yaml`

**Purpose**: Enable pyscript and let the adapter import what it needs.

**Steps**:

1. Add:
   ```yaml
   pyscript:
     allow_all_imports: true
     hass_is_global: true
   ```
2. `allow_all_imports: true` is required because the **adapter** parses YAML and JSON. pyscript
   restricts imports to a limited built-in set by default, and the exact contents of that default
   allowlist are not documented precisely enough to rely on (`research.md` R-07).

   Note that the pure core under `pyscript/modules/` is importable **regardless** of this setting —
   pyscript always allows imports from its own modules directory. The setting exists solely for the
   adapter's `yaml` and `json` needs.
3. `hass_is_global: true` gives the adapter access to Home Assistant internals where needed.
4. Place the block near the other integration configuration, not appended at the end of the file —
   `configuration.yaml` already has a readable structure worth preserving.

**Files**: `configuration.yaml` (edit)

**Validation**:
- Home Assistant restarts cleanly with pyscript loaded
- The Home Assistant log shows pyscript initialising with no import warnings

**Prerequisite the homeowner must have done**: pyscript installed through HACS and Home Assistant
restarted once. This YAML configures it; it does not install it. Note the installed version
somewhere durable — `research.md` R-08 asks for it to be pinned so an upgrade is deliberate.

---

### T048 — Record the quarter-hour semantics question for the detector

**Purpose**: Capture what is unknown about `1-0:1.4.0` and hand it to WP11's runtime detector, rather
than trying to answer it by hand.

**Steps**:

1. `sensor.slimmelezer_huidig_kwartiervermogen` has **two plausible definitions** and the firmware
   documentation does not settle which:
   - **Accumulating**: energy so far ÷ the full 15 minutes. Climbs from zero every quarter, reaching
     the true average only at the window's end.
   - **True running**: energy so far ÷ elapsed time. Reflects the actual draw rate from the first
     sample.
2. The difference is enormous early in a window. One minute in they differ by a factor of 15 — under
   the accumulating reading, a 7 kW load starting at :01 shows as roughly 0.5 kW, and a planner
   comparing that against a 2.5 kW ceiling would see nothing wrong while a large peak forms.
3. **Do not attempt a manual test with a known load.** The obvious approach — switch on a kettle and
   watch — does not work on this installation, because the battery is behind the meter and masks
   household draw. Getting a clean reading would mean taking the battery out of the loop, which is
   not a reasonable ask for a calibration step.
4. Instead, record the two candidate definitions and the discriminator in `configuration.yaml`
   comments, and leave the determination to WP11's detector (T067), which settles it passively from
   ordinary household consumption.
5. Note the config field that controls it: `capacity_tariff.quarter_hour_average_mode`, defaulting to
   `auto`. Once the answer is known and stable, it can be pinned.

**Files**: `configuration.yaml` (comment)

**Validation**:
- Both candidate definitions and their consequence are written down where an implementer will see them
- No manual load test is prescribed
- The config field and its default are recorded

---

### T049 — Wire the three capacity sensors

**Purpose**: Confirm the sensors the planner and guard will read, and record their names.

**Steps**:

1. The reflash added three entities. Record all three as the canonical sources:

   | Entity | e-MUCS field | Role |
   |---|---|---|
   | `sensor.slimmelezer_huidig_kwartiervermogen` | `1-0:1.4.0` | Running quarter-hour average |
   | `sensor.slimmelezer_maandpiek` | `1-0:1.6.0` | This month's peak — the level to defend |
   | `sensor.slimmelezer_gemiddelde_maandpiek_13_maanden` | 13-month average | What the fee is billed on |

2. **No Home Assistant accumulation is needed.** C-014 previously assumed these were unavailable and
   called for a quarter-hourly utility meter plus a monthly maximum helper. The reflash made that
   obsolete: reading the meter's own registers is both simpler and authoritative, because it cannot
   drift from what the grid operator measures.
3. Note the reference values observed at the time of writing, so a later reader can sanity-check
   units and magnitudes: month peak 1.475 kW, 13-month average 2.169 kW, quarter-hour average 0.0 kW
   at near-zero draw.
4. Note that the **month peak was below the 2.5 kW floor** when recorded. That means the ceiling is
   the floor, and the planner will be in its most protective mode — expected behaviour, not a fault,
   and worth saying so before someone reports it as one.
5. Confirm all three report in **kW** with `device_class: power`, so no unit conversion is needed.

**Files**: `configuration.yaml` (comment), carried into WP11, WP12 and WP13

**Validation**:
- All three entities exist and report kW
- Their names are recorded where WP11 and WP12 will read them
- The obsolete helper approach is not implemented

---

### T039 — Record how to verify entity names on the live install

**Purpose**: Two entity-name assumptions in this plan are unverified, and WP10 will fail confusingly
if either is wrong.

**Steps**:

1. Add a comment block in `configuration.yaml` next to the new sections, or a short section in the
   repository's documentation, recording the two names the adapter will read:
   - `sensor.forecast_solar_estimate` — created by T037, so its name is known
   - the ENTSO-e price entity and the attribute holding the forward series. The integration is
     expected to expose `attributes.prices` with `time`/`price` entries, but the exact entity id and
     attribute key must be confirmed **on this install**.
2. Record the verification procedure plainly: Developer Tools → States, filter for `entso`, inspect
   the attributes of the price entity, and note the attribute key holding the forward series.
3. State the expected shape so a mismatch is recognisable: a list of mappings, each with a timestamp
   and a price, covering at least 24 hours and extending to ~48 after the day-ahead auction
   publishes around 13:00 local.
4. Keep this short. It is a note for whoever implements WP10, not documentation for its own sake.

**Files**: `configuration.yaml` (comment) or a brief note alongside it

**Validation**:
- The two entity names the adapter depends on are written down somewhere findable
- The verification steps are concrete enough to follow without guessing

**Why this is a subtask rather than an assumption**: `research.md` open item 2 flags the ENTSO-e
attribute names as unconfirmed. A key-name mismatch is a five-minute fix **if you know that is what
went wrong** — and a baffling afternoon if the only symptom is the planner halting every cycle with
"price data unavailable" while the sensor visibly has data.

---

## Definition of Done

- `configuration.yaml` declares the forecast.solar REST sensor with this site's real parameters
- `configuration.yaml` declares the pyscript block with `allow_all_imports: true`
- Home Assistant restarts cleanly and the forecast sensor populates its attribute
- The entity names WP10 depends on are recorded, with a way to verify them
- No existing section of `configuration.yaml` was reformatted or restructured

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| **Azimuth sign reversed** (+10 instead of −10) | Models a south-west roof; every prediction shifts ~1h later. Check against `user_config.example.yaml` |
| URL path parameters in the wrong order | The order is `lat/lon/dec/az/kwp`; a swapped dec/az silently produces plausible-but-wrong numbers |
| `scan_interval` shorter than 3600 | Burns the free-tier rate budget that NFR-002 caps at 12/hour |
| Roof parameters drifting between the REST URL and `user_config.yaml` | They appear in two places by necessity — note the coupling |
| Reformatting the existing file | This is a live configuration; add, do not tidy |
| Adding an API key | The free tier needs none and supports the single plane this array has |

## Reviewer guidance

Check the URL character by character against `user_config.example.yaml` from WP01. The two must
describe the same roof: `51.12/3.85/50/-10/8.1` against `latitude: 51.12`, `longitude: 3.85`,
`declination: 50`, `azimuth: -10`, `kwp: 8.1`. A mismatch means the forecast models one roof while
the planner assumes another, and **nothing anywhere will report an error** — the numbers will simply
be wrong in a way that looks like a bad forecast.

Then confirm `scan_interval` is 3600 or greater. Shorter risks the rate limit, and an exhausted quota
manifests as an empty forecast, which WP10 will correctly treat as a zero-solar fallback — a
degraded plan caused by a configuration choice rather than an outage.

Finally, `git diff configuration.yaml` should show **additions only**. Any modification to the
existing price template sensors or utility meters is out of scope for this work package.
