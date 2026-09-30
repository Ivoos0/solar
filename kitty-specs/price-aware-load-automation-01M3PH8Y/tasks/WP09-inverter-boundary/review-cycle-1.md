---
affected_files: []
cycle_number: 1
mission_slug: price-aware-load-automation-01M3PH8Y
reproduction_command:
reviewed_at: '2026-09-29T20:11:38Z'
reviewer_agent: user
wp_id: WP09
---

# WP09 review - cycle 1 - CHANGES REQUESTED

Zero-transmission (C-001/NFR-005/SC-009): PASS. The only imports are `os` and `decision`. The only calls are `open`, `write`, `flush`, `os.fsync`, `os.makedirs`, `os.path.dirname` and `decision.format_record`. There is no network, serial, subprocess, HA service, state or device-path access. The interface matches the contract, the stub is clear, and 294 tests pass. The one blocking issue is the write guard. The WP says it is the defect most likely to survive review.

## Issue 1 (BLOCKING): `@pyscript_compile` alone does not take the write off Home Assistant's event loop

`@pyscript_compile` only turns `_append_line` into native Python. `apply` still calls it directly and synchronously from interpreted code, which runs as a coroutine on HA's event-loop thread. Nothing moves that call to another thread. So `os.makedirs` + `open` + `write` + `os.fsync` still block the loop on every cycle. `os.fsync` on a NAS disk makes this the worst case: a forced physical flush every five minutes. HA's blocking-call detector (which patches `builtins.open`) will also log "Detected blocking call to open ... inside the event loop by custom integration 'pyscript'". That fails the T042 validation "Home Assistant logs no blocking-I/O warning when the planner runs" and the T042 purpose ("Keep a blocking write out of Home Assistant's event loop"). The comment at lines 25-29 ("Compiling this helper ... runs it outside the interpreter ... the symptom is Home Assistant stalling") is therefore wrong: outside the interpreter is not the same as outside the event loop.

Fix (small):
- Keep `@pyscript_compile` on `_append_line`. `task.executor` only accepts native functions, which is exactly what the decorator provides. Dispatch it with `task.executor(_append_line, log_path, line)` inside the existing `try`. `task.executor` re-raises the worker's exception, so the False-on-failure path still works.
- Rewrite the WHY comment to say both things: the decorator makes the helper native so that `task.executor` can run it in a worker thread, and the executor is what keeps `open`/`fsync` off HA's event loop. Removing either one brings the stall back.
- Tests: in the `inverter` fixture, inject a `task` stand-in whose `executor(fn, *args)` calls `fn(*args)`, and restore or delete it like `pyscript_compile`. Add an AST assertion that `apply` reaches `_append_line` only through `task.executor`, never as a direct call.
- Note for the orchestrator (not WP09's to change): research.md R-02 and WP10 (lines 146, 293, 305, 318) treat "compiled OR executor" as equivalent. The same flaw will show up in the adapter's reads and writes. WP10 should dispatch compiled I/O helpers through `task.executor`.

## Recommended (non-blocking, cheap; please include)

2. **Action/record divergence.** `apply("charge", 9.9, record_with_action_export)` returns True and logs "export". This is harmless today, but once transmission is added the device would act on `action`/`target_power_kw` while the log says something else. That breaks contract rule 4 ("the log cannot drift from what was attempted"). Return False (without writing) when `action != record.action` or `target_power_kw != record.target_power_kw`, and add a test for it.
3. **One physical line.** `format_record` normalises whitespace in `reasoning`. It does not normalise items in `degraded_inputs`/`vetoes_applied`, and `__post_init__` only rejects blank items. A record with `degraded_inputs=["soc\nstubbed"]` makes `apply` write 2 physical lines (reproduced). Today those values are code constants, but the boundary claims "one line". Add `if "\n" in line or "\r" in line: return False` before the append, with a test. The root-cause fix belongs in decision.py validation (WP06 follow-up).
4. **Failure reason is swallowed.** Catching all exceptions and returning False is the right choice (a full disk must not stop HA), but the adapter learns nothing about why. Inside the `except`, capture the exception and emit it through pyscript's `log.warning(...)`, guarded or injected for tests. Alternatively, document that the adapter logs a generic "decision log append failed".
5. **The test allowlist is a denylist.** Mutations adding `import urllib.request`, `import subprocess`, or `os.open(...)` all pass the current suite. Assert that the module's imports are exactly `{"os", "decision"}`, and assert the set of called names or attributes against an allowlist.
6. **Concurrency note.** Once executor dispatch is added, the planner and the peak guard may append from different worker threads. One `write()` of line+"\n" in `O_APPEND` mode, with lines around 400 B (well under the 8 KiB buffer, so one syscall), is atomic in practice on a local Linux FS. 8 threads x 200 appends of 3.3 KB lines produced no torn lines. Add a one-line comment stating this assumption. No lock is needed.

## Checklist
1 Dead code: N/A for this WP. The adapter (WP10) is the caller. |
2 Synthetic fixture: PASS. |
3 Silent empty return: PASS. `except Exception: return False` is documented. See item 4. |
4 FR coverage: PASS. |
5 Frozen surface: PASS. |
6 Locked decision: FAIL. The T042 goal of no blocking I/O on the event loop is not met (Issue 1). |
7 Shared files: PASS. Only inverter.py and test_inverter.py are touched. |
8 Fragility: PASS. There is no new `raise`.
