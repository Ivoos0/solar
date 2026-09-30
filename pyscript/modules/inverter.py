"""Inverter boundary: records intent, transmits nothing (this mission).

Public surface (contracts/inverter-boundary.md):
    apply(action, target_power_kw, record, log_path=DEFAULT_LOG_PATH) -> bool
    read_charge_percent() -> float

Everything that will one day talk to the real inverter lives behind these two
signatures. They express intent, not transport.

This is a pyscript MODULE (pyscript/modules/), not a top-level script. Top-level
pyscript scripts cannot import each other; only files under <config>/pyscript/
modules/ are importable by scripts. The planner adapter and the peak guard both
need to call apply(), so this file must live here. Modules may still use
pyscript features (@pyscript_executor, log).
"""
import os

import decision

# Home Assistant config dir inside the container. Parameterised on apply() so
# tests can redirect it.
DEFAULT_LOG_PATH = "/config/battery_planner/decisions.log"

# STUB, NOT A MEASUREMENT. The inverter link is out of scope (C-003), so the
# battery charge is a fixed placeholder. Decisions built on it carry
# degraded=soc_stubbed (set from BatteryState.is_stubbed, not here). Do not
# replace this with another Home Assistant entity as a proxy.
STUBBED_CHARGE_PERCENT = 50.0

# Largest allowed gap between the target_power_kw argument and the record's.
POWER_TOLERANCE_KW = 1e-9


# WHY @pyscript_executor, two things in one decorator. (1) It compiles this
# helper to native Python, which pyscript's interpreter needs before anything
# can run it outside itself. (2) It runs the compiled helper in an executor
# thread, and THAT is what keeps the blocking open()/write()/fsync() off Home
# Assistant's event loop (research.md R-02). @pyscript_compile alone only does
# (1): the interpreted caller would still run the I/O on the loop and Home
# Assistant would log a blocking-call warning and stall every cycle. Do not
# downgrade the decorator or call this from a plain function.
#
# Concurrency assumption: the planner and the peak guard both call apply()
# from separate tasks, so appends may come from different worker threads. Each
# call does ONE write() of line+newline in append mode; measured tear-free
# (8 threads x 200 appends of 3.3 KB lines). No lock is needed while lines stay
# under the buffer size.
@pyscript_executor  # noqa: F821  (provided by pyscript at runtime)
def _append_line(path, line):
    """Open in append mode, write one line, flush to disk. Nothing else."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def apply(action, target_power_kw, record, log_path=DEFAULT_LOG_PATH):
    """Carry out a decision.

    This mission: append `record` to the decision log and return. Nothing is
    transmitted to the inverter. `record` is authoritative for the logged
    content; action and target_power_kw are the intent a later implementation
    will act on, so they must match the record or nothing is written.

    action           -- "charge" | "discharge" | "export" | "idle"
    target_power_kw  -- float, 0.0 when idle
    record           -- DecisionRecord, already complete

    Returns True when the intent was durably recorded, False on any failure
    or mismatch (never raises into the caller; the reason goes to log.warning).
    This function is the ONLY writer of the decision log.
    """
    try:
        # Written as not (<= tol) so a NaN or infinite gap is refused too:
        # every comparison with NaN is False, and inf is never <= tol.
        if (action != record.action
                or not (abs(target_power_kw - record.target_power_kw)
                        <= POWER_TOLERANCE_KW)):
            log.warning(  # noqa: F821  (pyscript global)
                "inverter.apply refused: action/target_power_kw do not match "
                "the decision record, nothing logged or transmitted")
            return False
        line = decision.format_record(record)
        if "\n" in line or "\r" in line:
            log.warning(  # noqa: F821
                "inverter.apply refused: decision record line is not a "
                "single physical line")
            return False
        _append_line(log_path, line)
    except Exception as exc:
        log.warning(  # noqa: F821
            f"inverter.apply: decision log append failed: {exc!r}")
        return False

    # ---- FUTURE TRANSMISSION POINT ----------------------------------------
    # Real inverter control is added HERE, and only here.
    # Ordering rule: LOG FIRST, TRANSMIT SECOND. The intent is already on
    # disk above, so the log explains a failure even if sending fails. The
    # transmission result must not change what was logged.
    # Not implemented in this mission (C-001, NFR-005).
    # -----------------------------------------------------------------------
    return True


def read_charge_percent():
    """Current battery charge, 0-100.

    This mission: returns the stub STUBBED_CHARGE_PERCENT (FR-007, C-003).
    Later: reads the real value from the inverter.
    """
    return STUBBED_CHARGE_PERCENT
