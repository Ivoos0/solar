---
affected_files: []
cycle_number: 1
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-29T18:57:27Z'
reviewer_agent: user
wp_id: WP07
---

# WP07 review feedback (cycle 1) - changes requested

Core design is sound and mostly approvable: no datetime.now(), now always a parameter; day rollover
compares LOCAL dates in the configured timezone with a test where UTC and Brussels disagree (would fail
under UTC); staleness (is_stale) and invalidation (invalidation_reason) are separate functions with
separate tests; evaluate() never raises (None/garbage/wrong kind/naive now all return a verdict);
offsets round-trip; no json/open/HA imports; 49 tests pass; only cache.py and test_cache.py changed.
The (status, series, detail) shape with fresh/stale/invalid is acceptable in place of the
usable/reason object: "stale" carries the series (usable-when-stale, FR-036) and "invalid" never does.

Fix the following, all in pyscript/modules/cache.py and tests/test_cache.py only:

**Issue 1 - Degraded age format violates the decision-record contract (FR-036, SC-014).**
contracts/decision-record.md and WP06 T030 fix the marker as `cache_age_solar=3h12m`; WP10 T0xx adds
`cache_age_<kind>=<age>`. The WP required `age_description(series, now)` rendering `3h12m`, sub-hour
`14m`, and multi-day ages. It is missing, and `degraded_note()` emits `solar cache stale: 210 min old`,
which WP10 would have to re-parse or re-derive. Fix: add `age_description(series, now)` (e.g. `14m`,
`3h12m`, `2d3h` or `51h12m` - pick one and document it) and make the stale verdict detail the
contract marker, e.g. `cache_age_solar=3h30m`. Test: 3h12m, 14m, and a multi-day age; update
test_evaluate_stale_gives_degraded_note to assert the contract marker.
Note SC-014 wants the age stated for every decision that used a cached series, fresh included, so
make the age reachable for the fresh case too (either put the marker/age in `detail` for fresh, or
document that WP10 calls age_description on the returned series).

**Issue 2 - Disposability test is not a disposability test (FR-038, SC-013, T036.7).**
`test_disposable` only asserts `evaluate(None, ...)` is "invalid". That passes with any
implementation. Required: a series rebuilt from the same source data equals the cached one field for
field - e.g. build a CachedSeries, `to_dict` it, evaluate the dict, and assert the returned series
== the original (dataclass equality, blocks included, computed_at equal and same utcoffset); and
assert the miss path returns no series (so only a rebuild follows, never different values).

**Issue 3 - Required negative fingerprint case missing at the cache level (FR-037, T034/T036.3).**
Add a test: config with a different price coefficient (e.g. prices.consumption_multiplier changed via
config.from_dict) evaluates a cached series written under the original config as "fresh", not
"invalid". Also add a fingerprint-mismatch case that is otherwise old-but-same-day or fresh AND one
that is stale, both returning "invalid", to show mismatch invalidates regardless of age.

**Issue 4 - Staleness boundaries through config, both kinds (T033/T036.2).**
Current tests call is_stale with literal minute bounds and never check usage past its bound. Add
evaluate()-level (or is_stale with stale_minutes_for) cases using site_config defaults: solar 1h59m
fresh / 2h01m stale; usage 47h fresh / 49h stale. Note the usage 47h/49h case crosses a local day,
so it will hit day-rollover invalidation first - either assert that interaction explicitly (and
document that usage staleness can only bite within a day under the current rules) or construct the
case so it stays within one local day with a smaller bound. Please make the precedence explicit in
a test and a one-line docstring note, since WP10 relies on it.

Minor (optional): is_from_previous_day uses `!=`, so a computed_at on a LATER local date (clock
skew) also invalidates. That is conservative and fine, but rename or docstring it ("different local
day") so it matches behaviour.
