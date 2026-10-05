# Contract: Decision Record

**Satisfies**: FR-018, FR-027, FR-028, NFR-004, NFR-008, SC-001, SC-002
**Written to**: `battery_planner/decisions-YYYY-MM-DD.log` (NAS only), one file per local calendar day of the record's own timestamp (in `timezone`); files whose date is older than `retention.keep_days` (default 90) are deleted by the daily cleanup at 03:30 local time
**Written by**: the inverter boundary, via the adapter
**Read by**: a human, by eye

This is the mission's **only output surface**. Nothing is commanded, so a decision that is not legible in this file did not happen as far as anyone can tell. Trimming fields for brevity trades away the deliverable.

## Format

One record per evaluation cycle, appended, never rewritten (NFR-008). Key-value pairs on a single line, `|`-separated, so a record stays greppable and readable without tooling.

```
2026-09-29T14:35:00+02:00 | action=export | power=2.50kW | soc=78.0%/7.80kWh |
  cons=0.2140 | inj=0.1890 | solar_rem=11.20kWh | usage_rem=14.60kWh |
  saturation=2026-09-29T12:45:00+02:00 | spill=2.40kWh | breach=none |
  end_soc=2.10kWh | took=84ms | avg=1.20kW | ceiling=2.50kW | budget=1.95kW |
  vetoes=none | selector=S3 |
  why="leftover 4.1kWh with saturation at 12:45; best injection price before saturation" |
  degraded=soc_stubbed | source=planner
```

(Wrapped here for reading; one physical line in the file.)

## Required fields

Every field appears in **every** record. An inapplicable value is written explicitly (`none`, `n/a`, or a real `0.00kWh` that was computed) and never omitted — a missing field and a zero field must not look alike (NFR-004).

| Field | Format | Notes |
|---|---|---|
| timestamp | ISO 8601 with offset | Local time, Europe/Brussels (C-008) |
| `action` | `charge` \| `discharge` \| `export` \| `idle` | |
| `power` | `N.NNkW` | `0.00kW` when idle |
| `soc` | `N.N%/N.NNkWh` | Both forms — percent is what the inverter reports, kWh is what the rules use |
| `cons`, `inj` | 4 decimal places, EUR/kWh | Derived prices now, not market price |
| `solar_rem`, `usage_rem` | `N.NNkWh` \| `n/a` | Across the horizon; `n/a` when the source has no trajectory (`source=guard`) |
| `saturation` | ISO 8601 \| `none` | FR-031 |
| `spill` | `N.NNkWh` \| `n/a` | `0.00kWh` when none projected; `n/a` when the source has no trajectory (`source=guard`) |
| `breach` | ISO 8601 \| `none` | FR-032 |
| `end_soc` | `N.NNkWh` | Projected at horizon end |
| `took` | `NNNms` | Cycle duration (NFR-009); makes NFR-001's 5-second budget checkable from the log |
| `avg` | `N.NNkW` | Running quarter-hour average grid offtake (FR-052) |
| `ceiling` | `N.NNkW` | The peak level being defended — `max(2.5, month peak)` |
| `budget` | `N.NNkW` | Grid power still available for **charging** this window after the household's own estimated draw (allowed total offtake rate minus household draw, capped at `max_charge_kw`), measured against `capacity_tariff.stay_under_percent` of the ceiling (80 % of 2.5 kW = 2.0 kW by default), so it is lower than the room under the real ceiling. **Negative** when the window is already over that charging level |
| `vetoes` | comma-separated \| `none` | FR-028 — a veto that suppressed a proposal, with what it suppressed |
| `selector` | `S0`–`S6` | Which one fired |
| `why` | quoted free text | The values that made the condition true |
| `degraded` | comma-separated \| `none` | FR-027 — `soc_stubbed`, `solar_zero_fallback`, `cache_age_solar=3h12m`, `forecast_age=1h20m` (forecast used but its sensor stamp is 75 minutes or more old; a stamp older than `timing.solar_cache_stale_minutes` counts as a failed forecast instead), `usage_samples=N` |
| `source` | `planner` \| `guard` | Which loop produced the record; always the last field |

**Absent values.** When capacity handling is off, `avg`, `ceiling` and `budget` render the literal `n/a`; when the current block has no market price, `cons` and `inj` render `n/a`; the peak guard has no trajectory, so its `solar_rem`, `usage_rem` and `spill` render `n/a` too. Free-text parts (each `vetoes`/`degraded` entry, the HALT `cause`) may not contain `|` or a line break, and numbers must be finite; a record violating this is rejected (ValueError) rather than written. `n/a` is an explicit value, never a blank and never confusable with `0.00kW`.

**Field order is fixed**: timestamp, action, power, soc, cons, inj, solar_rem, usage_rem, saturation, spill, breach, end_soc, took, avg, ceiling, budget, vetoes, selector, why, degraded, source.

## Veto rendering

A veto that suppressed a proposal names both itself and what it suppressed. With the real selector order (S2 is tried before S3) the reachable cases are:

```
# S3 wanted to export but V2 forbade it, and nothing else applied
vetoes=V2(suppressed S3 export) | selector=S6 |
  why="injection -0.0043 forbids export; no spill or breach ahead, holding"

# several vetoes block the same proposal: joined with +
vetoes=V1+V2(suppressed S3 export) | selector=S6 | ...

# V4 (no usable usage history) forbids grid charging, solar charging and
# export (every price-driven selector S1-S5), never peak shaving (S0): S1
# wanted to grid-charge and S2 to store solar, both were suppressed, and
# nothing else applied. The idle record says so plainly.
vetoes=V4(suppressed S1 charge),V4(suppressed S2 charge) | selector=S6 |
  why="hold: no usage history: planner holds (only peak shaving acts) - ..."

# a veto fired but blocked nothing (S2 won first): rendered bare
vetoes=V2 | selector=S2 |
  why="injection -0.0043; surplus 2.4kW, headroom 3.2kWh, charging from solar"
```

V4 fires on every cycle while there is no usage history (until the energy history holds a known household load, see the README section "Energy history"), so it appears bare on most records, and as `V4(suppressed ...)` whenever a price-driven selector (grid charge, solar charge or export) would have acted. The suppressed entries are rendered separately, one per suppressed proposal, e.g. `V4(suppressed S1 charge)` and `V4(suppressed S2 charge)` (see render_vetoes). V4 does not forbid the S0 peak-shave discharge. Neither does V1 (reserve): V1 forbids export only. Only V5 (battery empty, forbids discharge) stops a peak shave, e.g. `V5(suppressed S0 discharge)`.

This is the case the spec got wrong twice. A record showing a veto **must** also name the selector that finally fired (SC-010) — a veto alone is never a complete decision.

## Halt record

When price data is missing, no decision is produced (FR-021), but the cycle is still not silent (SC-001):

```
2026-09-29T14:35:00+02:00 | HALT | cause=price_data_unavailable |
  entered=2026-09-29T14:20:00+02:00 | alerted=2026-09-29T14:20:00+02:00
```

## Verification

- **SC-002**: sample 20 records; each must be reconstructible by hand from its own fields, without reading code.
- **SC-014**: every record using a cached series states that series' age in `degraded`.
- **NFR-008**: append-only; a seven-day history readable with `tail`, `grep`, or a text editor.
