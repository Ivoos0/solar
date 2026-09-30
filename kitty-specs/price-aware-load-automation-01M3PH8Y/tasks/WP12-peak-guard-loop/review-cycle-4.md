---
affected_files: []
cycle_number: 4
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-30T05:07:43Z'
reviewer_agent: user
wp_id: WP12
---

# WP12 reopened (integration defect, not a review rejection)

peak_guard.py imports the pure core (capacity, config, battery, rules, decision) through pyscript, whose AST interpreter cannot run generator expressions, @property, key= callbacks with pyscript functions, or dataclass __post_init__ (proven for WP10, see review-feedback-WP10-cycle1.md and cycle2). Apply the same native-loader pattern WP10 landed in pyscript/battery_planner.py (_load_core / _ensure_core, bare-name aliases kept for process lifetime, inverter stays a pyscript import). Add the AST lint test for peak_guard.py, and a subprocess regression test with pyscript/modules NOT on sys.path. Also verify under the real pyscript AstEval harness at ~/scratch-wp10c3 (copy pattern) that one guard tick works. Keep freshness logic unchanged.
