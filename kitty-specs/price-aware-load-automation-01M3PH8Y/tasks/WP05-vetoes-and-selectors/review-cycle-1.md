---
affected_files: []
cycle_number: 1
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-29T19:46:34Z'
reviewer_agent: user
wp_id: WP05
---

# WP05 review - cycle 1 - CHANGES REQUESTED

## Summary
The structure is correct. The resolution loop in `decide()` (rules.py:428-442) never returns, breaks or restarts on a veto. A vetoed proposal is appended to `suppressed` with its real blocking veto(es), and the loop continues to the next selector. S6 always terminates. V1 forbids {discharge, export}, V2 forbids {export} only (0.0 does not fire), and V3 forbids {grid_charge} only, so solar charging is unaffected. S0 takes its power from capacity.shave_kw. S1/S4/S5 are capped to budget_kw and say so. The S3/S4 windows are bounded, and unpriced blocks are skipped. Prices are joined by UTC instant. `pytest tests -q` gives 246 passed.

The reviewer also ran a 60,000-case randomized property test over battery/price/trajectory/grid states (including the fall-back day and fixed-offset keys), and it passed. It checked for exactly one Decision, no forbidden action class, veto sets matching an independent oracle, a real blocking veto (and all of them) for every suppressed entry, suppressed entries in selector order and before the winner, and that every selector before the winner returned None or a forbidden proposal. It also checked that non-firing selectors are never in suppressed and that grid charge is <= budget. All seven selectors were reached.

One thing blocks approval.

## BLOCKING 1 - The price/eff rule for negative prices is wrong and brings the regression back

`_after_losses()` (rules.py:224) uses `price / efficiency` when the price is negative. The spec (FR-013, WP T023 (c), T025) says `best_remaining_injection_price x round_trip_efficiency`, with no sign exception. The spec formula is also the physically correct one. Storing 1 kWh now returns `eff` kWh later. At a negative price, exporting fewer kWh later means paying less, so the later value is `eff * p` whatever the sign. Dividing turns this into a larger payment, which has no physical basis.

Consequence, reproduced with the test helpers. The site is the default, 50 % charge, block 0 solar 1.0 / usage 0.4 (0.6 kWh = 2.4 kW surplus), spill 2.0:

- Flat negative horizon, injection -0.02 in every block: the decision is **S6 idle**, suppressed [(S3, export, V2)]. The spec formula gives -0.02*0.9 = -0.018 > -0.02, so S2 should charge 2.4 kW from solar.
- Later injection -0.019, slightly better than now: again **S6 idle**. The spec formula gives -0.0171 > -0.02, so S2 should fire.

This is the spec edge case "Battery part-full, injection price negative, sun on the roof ... S2 proposes charging from solar ... it must not degrade into an idle decision", which is the regression this WP exists to prevent. The docstring defends the deviation as keeping a flat negative-price horizon from making S2 fire. Firing in that case is the required behaviour.

Required:
1. Make `_after_losses(price, eff)` return `price * eff` for all signs. Or remove it and inline the spec formula in S2, S5 and the S6 reasoning. Update the module docstring ("Resolved ambiguities") to match.
2. Add a named test: flat negative injection horizon, sun, part-full battery. Assert V2 fires, S3 export is suppressed by V2, and S2 charges 2.4 kW from solar. Hand-derive -0.02*0.9 = -0.018 > -0.02 in a comment.
3. Add the "later slightly better but still negative" case (-0.019), which also expects S2.
4. Check that test_s5_never_proposes_loss_making_cycle_on_negative_prices still holds. It should: -0.01*0.9 = -0.009 < 0.20.

## Non-blocking notes (no action needed from the implementer; for the orchestrator/spec)

- SPEC GAP, not the implementer's fault. Even with the spec formula, if every later injection price is at least as negative as now after losses (for example now -0.02, later -0.03), S2 condition (c) is false. S3 export is then vetoed by V2 and the cycle idles while solar spills at a negative price. The same happens in the last block of the horizon, where there is no later block. When injection now is < 0, storing surplus solar always beats spilling it. The spec/data-model should decide whether S2 fires unconditionally (given headroom and surplus) when injection_now < 0. This needs a human decision; it is not in WP05's scope as written.
- contracts/decision-record.md lists `selector` as `S1`-`S6`, but S0 exists (FR-050). WP06 should render S0. The contract text needs a one-line fix.
- The blocking veto for a suppressed export is rendered joined, for example "V1+V2". WP06 must render that (e.g. `V1+V2(suppressed S3 export)`).
- Decision does not carry budget/ceiling/running_average. The WP06/WP10 record must take them from capacity.* with the same GridState, so the numbers match what V3/S0 used.
- In the last minute of each window, capacity.budget_kw returns 0.0 (WP11), so V3 blocks all grid charging then, including S1 at a negative price. That is WP11 semantics and is acceptable, but a 5-minute cycle landing at mm:14 will show V3. It is worth a line in the record/quickstart docs.
- The other documented readings are accepted: "best remaining" is strictly after the current block; the S3/S4 window collapses to the current block when the boundary is at or before now; S1-S5 are skipped when the current block has no price.
