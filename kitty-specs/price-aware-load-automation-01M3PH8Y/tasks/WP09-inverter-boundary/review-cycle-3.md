---
affected_files: []
cycle_number: 3
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-30T04:27:11Z'
reviewer_agent: user
wp_id: WP09
---

# WP09 change request (post-approval): move the inverter boundary into pyscript/modules/

Not a defect in the logic. The pyscript docs state that top-level script files cannot import each other ("functions within one source file can call each other ... but just within that one file"); only `<config>/pyscript/modules/` is importable by other scripts, and modules may use pyscript features (@pyscript_executor, log, task, state). The planned location pyscript/inverter.py would therefore not be importable by the adapter (WP10) or the peak guard (WP12) on the NAS, even though every test passes.

Required:
1. `git mv pyscript/inverter.py pyscript/modules/inverter.py` (keep history). Behaviour unchanged.
2. tests/test_inverter.py: load the module from its new path (keep the importlib + builtins-injection approach: identity `pyscript_executor` and a FakeLog `log`, restored afterwards; tests/conftest.py already puts pyscript/modules on sys.path, but the builtins must exist BEFORE the module is imported, so keep the explicit loader). Update the AST/allowlist tests' file path. All 316 tests must still pass.
3. Module docstring: state that this is a pyscript MODULE (importable by scripts), not a top-level script, and why.
4. Hardening folded in (you are touching the file anyway): the mismatch guard must reject a NaN or infinite target_power_kw (use `not (abs(target - record.target_power_kw) <= POWER_TOLERANCE_KW)` or math.isfinite; note `math` would be a new import - if you use it, add it to the import allowlist deliberately, or prefer the comparison form with no new import). Add tests: NaN target -> False + warning + nothing written; +inf -> False.
5. Do not change decision.py or anything else. Owned files for this rework: pyscript/modules/inverter.py, tests/test_inverter.py only (the old path pyscript/inverter.py must no longer exist).
