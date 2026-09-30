---
affected_files: []
cycle_number: 2
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-30T05:03:04Z'
reviewer_agent: user
wp_id: WP10
---

# WP10 review cycle 2: CHANGES REQUESTED (one blocker)

The loader aliases core modules under bare names only while later core files load, then restores sys.modules. Function-local imports run later: pyscript/modules/decision.py:185 has  inside build(), which runs every normal cycle. /config/pyscript/modules is not on sys.path in HA, so it raises ModuleNotFoundError: No module named capacity; caught as "cycle failed, trigger kept"; decisions.log stays empty (SC-001 fails). Proven under pyscript AstEval v2.1.0 with HA stubbed (scratch: ~/scratch-wp10c2/harness.py, probe_c2.py). tests/conftest.py:6-8 puts pyscript/modules on sys.path, hiding it.

Required (WP10 scope, loader + tests only; do not edit modules): keep the bare aliases registered in sys.modules for the process lifetime (document the shadowing trade-off and that only the nine core names are aliased). Add a regression test, in a subprocess, that loads the core via _load_core with pyscript/modules NOT on sys.path and no bare core names pre-existing, runs a full priced cycle and asserts a decision record is written. Everything else in cycle 2 verified OK (11/11 mutations caught, busy marker, age markers, halt/recovery paths).
