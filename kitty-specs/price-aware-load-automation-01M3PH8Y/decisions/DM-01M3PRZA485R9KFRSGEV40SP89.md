# Decision Moment `01M3PRZA485R9KFRSGEV40SP89`

- **Mission:** `price-aware-load-automation-01M3PH8Y`
- **Origin flow:** `plan`
- **Slot key:** `plan.testing.strategy-and-toolchain`
- **Input key:** `testing_strategy`
- **Status:** `resolved`
- **Created:** `2026-09-29T14:26:50.120769+00:00`
- **Resolved:** `2026-09-29T14:29:11.801602+00:00`
- **Opened by:** `cli`
- **Other answer:** `false`

## Question

Is Python available on the Windows machine for running pytest against the pure core, and how much test coverage is wanted - full unit tests for the trajectory and every selector/veto, or only a smoke test - given that no hardware exists to validate against?

## Options

_(none)_

## Final answer

Complete and full unit tests on the pure core, kept as bare as possible. Python is available on the Windows machine: 3.10.2 on PATH and 3.12.5 via the py launcher; pytest is not yet installed. Tooling stays minimal - a bare pytest install and a tests directory, with no requirements-dev.txt, no venv scaffolding, and no coverage tooling. The core carries ZERO third-party runtime dependencies and is written in the syntax intersection accepted by both CPython 3.10+ and pyscript's own AST interpreter, since pyscript does not run CPython and restricts imports; confirming pyscript's exact interpreter and import limits is a Phase 0 research task rather than an assumption. Test coverage must include: trajectory golden fixtures with handmade price, solar and usage series and known expected saturation block, spill quantity and reserve-breach block; one test per veto and per selector; the veto fall-through ordering, which was the defect found twice during specify; and the negative-injection-positive-consumption price band, which is where V2 earns its place and which appears rarely in live data. Tests are the only validation mechanism available, because there is no inverter to command, the battery charge reading is stubbed, and real weather takes days to produce an interesting case.

## Rationale

_(none)_

## Change log

- `2026-09-29T14:26:50.120769+00:00` — opened
- `2026-09-29T14:29:11.801602+00:00` — resolved (final_answer="Complete and full unit tests on the pure core, kept as bare as possible. Python is available on the Windows machine: 3.10.2 on PATH and 3.12.5 via the py launcher; pytest is not yet installed. Tooling stays minimal - a bare pytest install and a tests directory, with no requirements-dev.txt, no venv scaffolding, and no coverage tooling. The core carries ZERO third-party runtime dependencies and is written in the syntax intersection accepted by both CPython 3.10+ and pyscript's own AST interpreter, since pyscript does not run CPython and restricts imports; confirming pyscript's exact interpreter and import limits is a Phase 0 research task rather than an assumption. Test coverage must include: trajectory golden fixtures with handmade price, solar and usage series and known expected saturation block, spill quantity and reserve-breach block; one test per veto and per selector; the veto fall-through ordering, which was the defect found twice during specify; and the negative-injection-positive-consumption price band, which is where V2 earns its place and which appears rarely in live data. Tests are the only validation mechanism available, because there is no inverter to command, the battery charge reading is stubbed, and real weather takes days to produce an interesting case.")
