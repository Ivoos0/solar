---
affected_files: []
cycle_number: 3
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-29T19:10:15Z'
reviewer_agent: user
wp_id: WP03
---

# WP03 change request (post-approval) - FR-059 configurable usage grouping

Not a defect: the homeowner asked for a configurable usage profile. Approved WP03 buckets by weekday with a seven-day intent.

Required:
1. Usage window is `config.usage_history_weeks` (default 4) weeks, filtered inside `usage_profile` relative to `start_time` (readings older than the window are ignored).
2. `config.usage_grouping`:
   - `same_weekday` (default): a Wednesday block = mean of that time-of-day block over the Wednesdays in the window (up to 4 samples).
   - `day_type`: a Monday-Friday block = mean over EVERY weekday in the window; Saturday and Sunday pool as weekend days only (never with weekdays).
3. `sample_days` = distinct dates that fed the slot. Empty group falls back to the overall mean with sample_days 0; no history -> zeros, no exception.
4. Cross-WP extension (documented, small): config.py + test_config.py gain `usage_history_weeks` (int >=1, default 4) and `usage_grouping` (same_weekday|day_type, default same_weekday) from the `usage:` section; validation collects all errors; BOTH are added to `fingerprint()`; update the WP01 fingerprint tests that assert the exact field set (they must now show that changing either usage setting changes the fingerprint while price/battery/alert changes still do not). Add the `usage:` block and `capacity_tariff.peak_averaging_months: 13` to battery_planner/user_config.example.yaml (contracts/user-config.md shows the shape).
5. Do not change the solar half of series.py. Tests: per the updated WP03 T013/T014 Validation lists (both groupings, window cutoff, day-type pooling, weekend pooling, grouping switch changes output, thin-history behaviour). Existing solar tests must still pass.
