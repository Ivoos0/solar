# Specification Quality Checklist: Price-Aware Load Automation

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-29
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Requirement types are separated (Functional / Non-Functional / Constraints)
- [x] IDs are unique across FR-###, NFR-###, and C-### entries
- [x] All requirement rows include a non-empty Status value
- [x] Non-functional requirements include measurable thresholds
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Items marked incomplete require spec updates before `/spec-kitty.plan`

### Validation history

**Iteration 1** — three items failed and were repaired:

1. *No implementation details* — an early draft named the forecast service endpoint and the Modbus transport inside functional requirements. Endpoint shape and transport were moved out of FRs; the service is referred to by role ("the solar forecast service") in FR-004 and FR-026, and HF2211/Modbus appear only in C-003 and Out of Scope, where they bound the mission rather than prescribe a design.
2. *Success criteria technology-agnostic* — an early SC referred to log file format. Rewritten as SC-002, which measures whether a human can reconstruct a decision, not how the record is encoded.
3. *Requirements testable and unambiguous* — the original single rule-engine requirement bundled all eight rules into one row and could not be tested piecewise. Split into FR-009 (priority ordering) plus FR-010 through FR-017, one per rule, each independently verifiable.

**Iteration 2** — all items pass.

**Iteration 3** — one defect found during user review of the rule model, repaired:

4. *Requirements testable and unambiguous / user scenarios cover primary flows* — the original model evaluated all eight rules as a single first-match priority chain. That conflated two different kinds of rule and produced a real behavioural bug: when the injection price was negative, the chain terminated at the export-suppression rule and never reached the solar-absorption rule, so the common case of a part-full battery under sun on a low-price day resolved to an idle decision instead of charging. Repaired by separating **vetoes** (V1 reserve floor, V2 negative-price export) from **selectors** (R3–R8). A veto now removes an action from consideration without ending evaluation; selectors are tried in priority order until one proposes a permitted action. Changed: FR-009 through FR-011 and FR-017 rewritten, FR-028 added for veto traceability, FR-018 and NFR-004 extended from eleven to twelve record fields, SC-010 added, four edge cases added, two acceptance scenarios added to User Story 1, the decision flowchart redrawn, and Veto/Selector added to the domain language.

**Iteration 4** — full consistency audit at user request; eight discrepancies found and repaired:

5. *Infinite loop in the veto fall-through* — the flowchart routed every vetoed proposal back to the solar-absorption test rather than to the next selector in sequence, so two consecutive vetoes looped forever. Redrawn with an explicit per-selector check that falls through to the following selector.
6. *SC-001 contradicted the halt behaviour* — it required 99% of cycles to produce a decision record, but a halted cycle produces none by design, so a one-day price outage would fail a correctly behaving system. Rewritten to require one record per cycle, a decision record or a halt record.
7. *NFR-004 hard-coded a field count* — "all twelve required fields" could drift from FR-018's enumeration, which is arguably twelve or thirteen depending on whether the selector and its reasoning are one field. Now defers to FR-018's list rather than counting.
8. *Solar-absorption selector was untestable and mis-ordered* — its condition read "injection price is low", with no threshold, and it outranked the export selector, so at the horizon's best price the spec would have charged instead of sold. Condition is now "best remaining injection price after round-trip losses exceeds the injection price now", which is testable and stands the rule down precisely when the export selector should win.
9. *Selector numbering started at R3* — a hole left by the V1/V2 rename. Selectors renumbered S1–S6 throughout.
10. *FR-028 appeared between FR-017 and FR-018* — moved to the end of the table.
11. *C-009 categorised Regulatory* — it is a hardware limitation, not a legal one. Recategorised Technical and reworded to say curtailment is impossible with this installation's hardware.
12. *Terminology drift* — one edge case claimed a vetoed selector "cannot propose" an action when it proposes and is then suppressed; two acceptance scenarios and SC-002 still said "rule" where the spec now says "selector".

**Iteration 5** — scope correction raised by the user: the energy balance was a scalar over the whole horizon and could not see *when* energy arrives.

13. *Scalar balance replaced by a block trajectory* — a single "expected production versus expected consumption" figure cannot detect the battery reaching full capacity mid-horizon, so the spec would have held stored energy for the best evening price while the battery sat at 100% through the sunniest hours, spilling free solar the whole time. The balance is now a projection of battery charge across 15-minute blocks (configurable), clamped to the capacity ceiling and reserve floor, exposing the saturation block, the spill it implies, and any projected reserve breach. FR-008 rewritten; FR-029 through FR-032 added for block length, coarse-input handling, saturation/spill detection, and reserve-breach detection.
14. *Export and import selectors rebased on the trajectory* — the export selector (S3) now triggers on spill ahead or leftover at the horizon's end, and picks the best injection price in the window **before** saturation rather than across the whole horizon; the import selector (S4) picks the cheapest prices before the projected reserve breach. The solar-absorption selector (S2) additionally requires headroom.
15. *Record, entities, and criteria extended* — FR-018 now logs the saturation block, the spill, and the reserve-breach block; the Trajectory entity was added; SC-011 and SC-012 cover avoidable spill and saturation-timing accuracy; NFR-001 states the ~140-block cost of a 35-hour horizon; seven edge cases were added covering partial avoidance, no-saturation, competing constraints, the charge-power ceiling as a binding constraint, and coarse forecast resolution.

Note that a scalar balance is not merely less precise — it is wrong in a specific, expensive direction, because it always favours waiting for a better price and never sees the cost of waiting.

**Iteration 6** — user confirmed 15-minute blocks and asked for the expected solar input to be cached to a file rather than recalculated every cycle.

16. *Derived input series are cached; the trajectory is not* — FR-033 caches the per-block solar and usage series, and FR-034 states explicitly that the trajectory is rebuilt every cycle. The distinction is load-bearing: the trajectory depends on live battery charge and on which blocks have already elapsed, so caching it would freeze the projection while its inputs moved underneath. The user's own phrasing — "current charge + expected" — already drew that line correctly; the spec now states it so it cannot be lost at plan time.
17. *Staleness made visible rather than implicit* — FR-035 through FR-038 require each series to carry its computation time and source, refresh past a configurable bound, invalidate on a day roll-over or a configuration change affecting array geometry, location, or block length, and remain disposable. C-011 records that the cache persists to a file, which is why the timestamp is mandatory: a file outlives the process, so a series written yesterday would otherwise be trusted silently at start-up. SC-013 and SC-014 make both properties measurable.
18. *Cost correction worth carrying into plan* — the solar series is the cheap one. The expensive series is the usage profile: roughly 672 quarter-hour buckets averaged over seven days of history. Caching only the solar forecast would leave the larger cost in place, so both are cached, on different schedules — solar hourly with the forecast, usage daily.

### Deliberate judgements worth re-reading at plan time

- **Eleven-field decision record (FR-018, NFR-004)** is the mission's only output surface. If the plan proposes trimming it for brevity, that trade is against the mission's stated purpose.
- **R2 generalisation (FR-011)**: the user's rule was "when prices are negative to put on the net and the battery is full, stop discharging". This spec applies the suppression whenever the injection price is negative, full or not, because exporting at a negative price always loses money. The user confirmed this generalisation and initially expected it to be redundant — reasoning that a negative price would make the battery charge, leaving no net discharge to suppress. It is not redundant. With the current coefficients, consumption price turns negative only below a market price of −0.65 c/kWh, while injection price turns negative below **+1.17 c/kWh**, because the injection offset is negative. In the roughly 1.8 c/kWh band between them, injection is negative, consumption is still positive, the max-charge rule does not fire, and the battery can sit part-full — so R2 is the only rule preventing a loss-making export. That band covers ordinary low-price sunny hours, not a rare edge. The band's width is coefficient-dependent and therefore per-provider, but it exists for any provider whose injection offset is negative.

  The user then asked a sharper question: in that band, isn't the passive inverter behaviour — solar charges the battery, load drains it, nothing reaches the grid until full — already sufficient, leaving nothing to suppress? For a *passive* system, yes. But the planner is not passive: the surplus-export selector (R5) commands export at any charge level above the reserve floor, and when every interval in the horizon carries a negative injection price it would select the least-negative one as its "best" price. V2 is the guard on that specific behaviour. The arbitrage selector (R7) needs no such guard, since a negative sell price can never beat a positive buy price.
- **Rolling horizon (FR-003)** replaced an end-of-day horizon after the user chose it explicitly. It makes evening decisions materially better but means the plan changes discontinuously when day-ahead prices publish; that discontinuity is specified as an edge case, not a defect.
- **C-009 (no curtailment)** means SC-004 can only be satisfied for *battery* export. Solar export during negative prices is unavoidable with current hardware and is logged rather than prevented.
