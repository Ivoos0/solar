---
work_package_id: WP07
title: Cache validity logic
dependencies:
- WP01
requirement_refs:
- FR-033
- FR-035
- FR-036
- FR-037
- FR-038
planning_base_branch: feat/price-aware-load-automation
merge_target_branch: feat/price-aware-load-automation
branch_strategy: Planning artifacts for this mission were generated on feat/price-aware-load-automation. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into feat/price-aware-load-automation unless the human explicitly redirects the landing branch.
subtasks:
- T032
- T033
- T034
- T035
- T036
history:
- date: '2026-09-29'
  note: Created by /spec-kitty.tasks
agent_profile: python-pedro
agent: claude
authoritative_surface: pyscript/modules/cache.py
create_intent:
- pyscript/modules/cache.py
- tests/test_cache.py
execution_mode: code_change
owned_files:
- pyscript/modules/cache.py
- tests/test_cache.py
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

Decide, **purely**, when a cached series may be used — so that cache correctness is unit-testable
rather than a property you hope holds in production.

Satisfies **FR-033, FR-035, FR-036, FR-037, FR-038**.

## Branch Strategy

- **Planning base branch**: `feat/price-aware-load-automation`
- **Final merge target**: `feat/price-aware-load-automation`
- Execution worktrees are allocated **per computed lane** from `lanes.json` after task finalization.
  Run `spec-kitty agent action implement WP07 --agent <name>`. Depends only on WP01; runs in
  parallel with WP02 and WP03.

## Scope boundary — read this first

This work package owns **validity logic only**. It decides whether a series may be used. It does not
open files, does not read directories, does not write JSON to disk. WP10's adapter does all of that
and calls into here.

That split is exactly why cache correctness is testable at all: the interesting part — staleness,
fingerprint mismatch, day rollover — is pure, and pure code can be tested without a filesystem, a
clock you cannot control, or a Home Assistant instance.

## The thing that makes this non-trivial

The cache is a **file**, and a file outlives the process. Home Assistant restarts; the file is still
there. A series computed yesterday sits on disk at boot looking exactly like one computed a minute
ago, and nothing in its content distinguishes them.

That is why `computed_at` is not bookkeeping — it is the only thing standing between a fresh plan
and yesterday's sunshine being used to decide today's battery behaviour.

## Context you need

- `contracts/cache-file.md` — **authoritative** on format, staleness and invalidation
- `data-model.md` — the `CachedSeries` table
- `spec.md` FR-033 through FR-038, SC-013, SC-014

---

### T032 — Define CachedSeries and its serialised shape

**Purpose**: The structure and its dict representation, ready for the adapter to persist.

**Steps**:

1. Create `pyscript/modules/cache.py`.
2. Define `CachedSeries` with the `data-model.md` fields: `kind`, `computed_at`, `source`,
   `config_fingerprint`, `blocks`.
3. Write `to_dict(series)` and `from_dict(raw)` converting to and from a plain JSON-compatible
   mapping. **Return a dict; do not call `json.dumps`** — serialisation to text is the adapter's job,
   and keeping `json` out of the core keeps the import surface at zero.
4. Datetimes serialise as ISO 8601 strings **with offset**. Parsing them back must produce
   timezone-aware objects; a naive datetime here would make every staleness comparison wrong by up
   to two hours, twice a year.
5. `from_dict` must tolerate malformed input by raising a single defined `CacheError` rather than
   `KeyError` or `ValueError` from deep inside. The adapter catches one exception type and treats it
   as a miss (T035).

**Files**: `pyscript/modules/cache.py` (new)

**Validation**:
- `from_dict(to_dict(series))` round-trips every field including timezone offsets
- A mapping missing `computed_at` raises `CacheError`, not `KeyError`
- No `import json`, no `open()`, no Home Assistant or pyscript imports

---

### T033 — Implement the staleness check

**Purpose**: Refuse to let old data pass as current.

**Steps**:

1. Write `is_stale(series, now, config)` returning True when
   `now - series.computed_at > stale_bound(series.kind, config)`.
2. Pick the bound by `kind`: `solar_cache_stale_minutes` for solar, `usage_cache_stale_minutes` for
   usage. Both come from config — the defaults are twice each series' refresh interval, so an hourly
   solar series goes stale after two hours and a daily usage profile after two days.
3. Take `now` as a **parameter**, never from `datetime.now()` inside the function. This is what makes
   staleness testable without freezing the clock or sleeping.
4. Write `age_description(series, now)` returning a human-readable age such as `3h12m` for the
   decision record's degraded markers (FR-036, and WP06's `cache_age_solar=` field).

**Files**: `pyscript/modules/cache.py` (continues)

**Validation**:
- A solar series 1h59m old is fresh; 2h01m is stale
- A usage series 47h old is fresh; 49h is stale
- `age_description` renders `3h12m`, and handles sub-hour ages (`14m`) and multi-day ages
- The function never reads the system clock

**What stale means downstream**: a stale series is refreshed if possible. If refreshing is
impossible — forecast unreachable, history unavailable — the adapter **uses it anyway** and marks the
decision degraded (FR-036). Old is not the same as missing; only absent *price* data halts the
system. This module reports staleness; it does not decide the consequence.

---

### T034 — Implement fingerprint and day-rollover invalidation

**Purpose**: Discard a series whose assumptions no longer hold.

**Steps**:

1. Write `is_invalidated(series, now, config)` returning True when **either**:
   - `series.config_fingerprint != config.fingerprint()` — the array geometry, location or block
     length changed, so the blocks no longer mean what they meant (FR-037)
   - `series.computed_at` falls on an earlier **local** day than `now`
2. Use local dates for the day comparison, in the configured timezone (C-008). A UTC-based
   comparison would roll over at 01:00 or 02:00 Brussels time depending on the season — invalidating
   perfectly good data in the middle of the night, and doing it inconsistently across the year.
3. Keep invalidation **separate** from staleness. They mean different things: a stale series may
   still be used when refresh fails; an invalidated series may **never** be used, because it
   describes a configuration that no longer exists. Conflating them would either use bad data or
   discard usable data.

**Files**: `pyscript/modules/cache.py` (continues)

**Validation**:
- A fingerprint mismatch invalidates regardless of age
- A series from yesterday local time invalidates even if only minutes old (23:58 → 00:02)
- A series from earlier today does not invalidate on this rule
- Changing a price coefficient does **not** invalidate — WP01's fingerprint excludes it deliberately

---

### T035 — Treat a missing or unreadable cache as a miss

**Purpose**: A cache problem must never be an outage.

**Steps**:

1. Write `evaluate(raw_or_none, now, config, kind)` returning a small verdict object with:
   - `usable` — may this series be used at all
   - `series` — the parsed series, or `None`
   - `reason` — `"ok"`, `"missing"`, `"unparseable"`, `"invalidated"`, `"stale"`
   - `age` — human-readable, when a series parsed
2. Handle the whole range in one place: `None` input, malformed mapping, wrong `kind`, fingerprint
   mismatch, previous day, stale.
3. **None of these raise.** A missing cache is a normal state on first run. An unreadable one is a
   miss, not a failure (`contracts/cache-file.md`). The adapter rebuilds and carries on.
4. Return `usable=True` with `reason="stale"` for the stale-but-parseable case, so the adapter can
   choose to use it when refresh fails while still marking the decision degraded.

**Files**: `pyscript/modules/cache.py` (continues)

**Validation**:
- `evaluate(None, ...)` returns `usable=False`, `reason="missing"`, no exception
- Garbage input returns `reason="unparseable"`, no exception
- A solar payload evaluated as `kind="usage"` is a miss, not a silent mismatch
- Stale but parseable returns `usable=True` with `reason="stale"` and a populated `age`

---

### T036 — Test staleness, invalidation, and disposability

**Purpose**: Cover the logic and the property that makes the cache safe to delete.

**Steps**:

1. Create `tests/test_cache.py`.
2. Test the staleness boundaries for both kinds, just inside and just outside.
3. Test fingerprint invalidation, including the negative case — a changed price coefficient must not
   invalidate.
4. Test day rollover across a local midnight, and confirm it is **local**: construct a case where UTC
   and Brussels disagree about the date and assert the local answer wins.
5. Test every `evaluate` path: missing, unparseable, wrong kind, invalidated, stale, ok.
6. Test the serialisation round trip preserves timezone offsets.
7. **Disposability test (SC-013)**: assert that `evaluate(None, ...)` and
   `evaluate(<valid fresh series>, ...)` lead to the same *decision inputs* — that is, a cache miss
   changes only whether a rebuild is needed, never the values themselves. Express this as: a series
   rebuilt from the same source data equals the cached one field for field.

**Files**: `tests/test_cache.py` (new, ~180 lines)

**Validation**:
- `pytest tests/test_cache.py -v` passes
- The day-rollover test would fail under a UTC-based comparison
- No test sleeps, freezes the clock, or touches the filesystem

---

## Definition of Done

- `cache.py` parses, serialises, and judges validity with `now` passed in
- Staleness and invalidation are separate concepts with separate functions
- Day rollover is evaluated in local time
- Every failure path returns a miss verdict; none raise
- No `json`, no `open()`, no Home Assistant or pyscript imports
- `pytest tests/` is green

## Risks and gotchas

| Risk | Mitigation |
|---|---|
| Reading the clock inside the module | Pass `now` as a parameter; otherwise staleness is untestable |
| Naive datetimes | Assert offsets survive the round trip; Brussels has two per year |
| UTC-based day rollover | Explicit test where UTC and local dates disagree |
| Conflating stale with invalidated | Separate functions, separate tests — the consequences differ |
| Calling `json.dumps` here | Return a dict; text serialisation belongs to the adapter |
| Raising on a missing cache | First run has no cache; a miss is normal |
| Fingerprint covering too much | A price-coefficient change must not invalidate solar data |

## Reviewer guidance

Check for `datetime.now()` first — anywhere in this module it is a defect, because it makes every
staleness test depend on wall-clock timing. `now` must arrive as an argument.

Then check the day-rollover comparison. If it compares UTC dates, the cache will invalidate itself at
01:00 or 02:00 local depending on the season, throwing away good data on a schedule nobody
intended — and the only symptom would be a slightly slower cycle in the small hours.

Then confirm staleness and invalidation are genuinely separate. If one function does both, the
"use stale data when refresh fails" behaviour of FR-036 cannot be expressed, and a forecast outage
would degrade further than it should.

Finally, verify nothing in this module can raise into the adapter except `CacheError`. A cache is an
optimisation; it must never be able to take the planner down.
