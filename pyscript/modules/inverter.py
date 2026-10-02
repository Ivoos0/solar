"""Inverter boundary: always records intent, then hands it to the configured driver.

Public surface (contracts/inverter-boundary.md):
    apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None,
          log_dir=DEFAULT_LOG_DIR) -> bool
    log_path_for(day, log_dir=DEFAULT_LOG_DIR)
        -> "<log_dir>/decisions-YYYY-MM-DD.log"
    read_charge(inverter_type="logging", driver_dir=None)
        -> (percent, is_stub, marker)
driver_dir=None means DEFAULT_DRIVER_DIR.

Everything that talks to a real inverter lives in a driver file,
<driver_dir>/inverter_<inverter_type>.py (inverter_logging.py documents the
interface). The default driver, `logging`, transmits nothing. This file does
not change when an inverter is added.

This is a pyscript MODULE (pyscript/modules/), not a top-level script. Top-level
pyscript scripts cannot import each other; only files under <config>/pyscript/
modules/ are importable by scripts. The planner adapter and the peak guard both
need to call apply(), so this file must live here. Modules may still use
pyscript features (@pyscript_executor, log).

Drivers are loaded NATIVELY (importlib inside a @pyscript_executor helper), like
the pure core: they are ordinary CPython, may block, and run in an executor
thread under a timeout, never on the event loop. Each is registered only as
inverter_driver_<type>; nothing is added to sys.path and no bare alias is made.
A loaded driver is cached until pyscript reloads this file; a failed load is
retried on the next call, so fixing the file needs no restart.
"""
import os

import decision

# Home Assistant config dir inside the container. Parameterised on apply() so
# tests can redirect it. The decision log is one file per local calendar day,
# decisions-YYYY-MM-DD.log, so old days can be removed by file name or age.
DEFAULT_LOG_DIR = "/config/battery_planner"
DEFAULT_DRIVER_DIR = "/config/pyscript/modules"


def log_path_for(day, log_dir=DEFAULT_LOG_DIR):
    """Decision log file for `day`: <log_dir>/decisions-YYYY-MM-DD.log.

    `day` is any date or datetime; only its own year, month and day are used.
    Pass the record's timestamp (aware, in the configured local zone) and the
    file is that local calendar day. No clock is read here.
    """
    return "%s/decisions-%04d-%02d-%02d.log" % (
        log_dir.rstrip("/"), day.year, day.month, day.day)


# Fallback used ONLY when the configured driver cannot be loaded or its reading
# is unusable. It is a placeholder, not a measurement: decisions built on it
# carry degraded=soc_stubbed. Keep equal to inverter_logging.STUBBED_CHARGE_PERCENT.
STUBBED_CHARGE_PERCENT = 50.0

# Largest allowed gap between the target_power_kw argument and the record's.
POWER_TOLERANCE_KW = 1e-9

# A driver call that has not returned after this long counts as failed (the
# worker thread is abandoned, so drivers should also set network timeouts).
DRIVER_TIMEOUT_SECONDS = 10.0

# Degraded markers for decisions taken while the driver is not working.
MARKER_UNAVAILABLE = "inverter_driver_unavailable"
MARKER_READ_FAILED = "inverter_read_failed"

# {driver_dir + "/" + type: loaded driver module}. Successes only.
_drivers = {}


# WHY @pyscript_executor, two things in one decorator. (1) It compiles this
# helper to native Python, which pyscript's interpreter needs before anything
# can run it outside itself. (2) It runs the compiled helper in an executor
# thread, and THAT is what keeps the blocking open()/write()/fsync() off Home
# Assistant's event loop (research.md R-02). @pyscript_compile alone only does
# (1): the interpreted caller would still run the I/O on the loop and Home
# Assistant would log a blocking-call warning and stall every cycle. Do not
# downgrade the decorator or call this from a plain function. The same holds
# for the driver helpers below.
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


@pyscript_executor  # noqa: F821
def _load_driver(driver_dir, inverter_type):
    """Import <driver_dir>/inverter_<type>.py as CPython. Raises when unusable."""
    import importlib.util
    import re
    import sys
    if not isinstance(inverter_type, str) or not re.fullmatch(
            r"[a-z0-9_]+", inverter_type):
        raise ValueError("invalid driver name %r" % (inverter_type,))
    path = os.path.join(driver_dir, "inverter_%s.py" % inverter_type)
    if not os.path.isfile(path):
        raise FileNotFoundError("no driver file %s" % path)
    full = "inverter_driver_" + inverter_type
    spec = importlib.util.spec_from_file_location(full, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module          # dataclasses etc. look themselves up
    try:
        spec.loader.exec_module(module)
        for name in ("send", "read_charge_percent"):
            if not callable(getattr(module, name, None)):
                raise AttributeError("%s defines no %s()" % (path, name))
    except BaseException:
        sys.modules.pop(full, None)
        raise
    return module


@pyscript_executor  # noqa: F821
def _invoke(driver, method, args, timeout):
    """Call driver.<method>(*args) with a deadline. Never raises.

    Returns (ok, value, error_text). The call runs in a daemon thread that is
    abandoned on timeout, so a hung driver cannot hold up the cycle or the guard.
    """
    import threading
    box = []

    def run():
        try:
            fn = getattr(driver, method)
            box.append((True, fn(*args), None))
        except BaseException as exc:
            box.append((False, None, repr(exc)))

    worker = threading.Thread(target=run, name="inverter-driver", daemon=True)
    worker.start()
    worker.join(timeout)
    if not box:
        return (False, None, "timed out after %ss" % timeout)
    return box[0]


def _driver(inverter_type, driver_dir):
    """(module, None) when the driver is usable, else (None, reason)."""
    if driver_dir is None:
        driver_dir = DEFAULT_DRIVER_DIR
    key = driver_dir + "/" + inverter_type
    cached = _drivers.get(key)
    if cached is not None:
        return cached, None
    try:
        module = _load_driver(driver_dir, inverter_type)
    except Exception as exc:
        return None, "driver %r cannot be loaded: %r" % (inverter_type, exc)
    _drivers[key] = module
    return module, None


def _transmit(action, target_power_kw, inverter_type, driver_dir):
    """Hand the intent to the driver. Called only AFTER the log line is on disk.

    Never raises and never changes what apply() returns: the log already
    explains the intent, whatever the driver does. A missing driver means
    nothing is sent (logging behaviour), loudly.
    """
    try:
        driver, problem = _driver(inverter_type, driver_dir)
        if driver is None:
            log.error(  # noqa: F821  (pyscript global)
                "inverter: %s; NOT transmitting %s %s kW, decision logged "
                "only" % (problem, action, target_power_kw))
            return
        ok, value, err = _invoke(driver, "send", (action, target_power_kw),
                                 DRIVER_TIMEOUT_SECONDS)
        if not ok:
            log.error(  # noqa: F821
                "inverter: driver %r send(%s, %s) failed: %s"
                % (inverter_type, action, target_power_kw, err))
        elif value is False:
            log.warning(  # noqa: F821
                "inverter: driver %r send(%s, %s) reported failure"
                % (inverter_type, action, target_power_kw))
    except Exception as exc:
        log.error(  # noqa: F821
            "inverter: driver %r transmit failed: %r" % (inverter_type, exc))


def apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None, log_dir=DEFAULT_LOG_DIR):
    """Carry out a decision: log it, then hand it to the configured driver.

    The decision log line is ALWAYS written, for every driver, and FIRST. The
    driver is called only once the line is durably on disk, and nothing the
    driver does (exception, timeout, False, missing file) can prevent or alter
    the line or change the return value. `record` is authoritative for the
    logged content; action and target_power_kw are the intent given to the
    driver, so they must match the record or nothing is logged or sent.

    action           -- "charge" | "discharge" | "export" | "idle"
    target_power_kw  -- float, 0.0 when idle
    record           -- DecisionRecord, already complete
    log_path         -- explicit file to append to (tests, back-compat); None
                        means log_path_for(record.timestamp, log_dir), the
                        record's own local day
    inverter_type    -- selects driver file inverter_<type>.py in driver_dir

    Returns True when the intent was durably recorded, False on any failure
    or mismatch (never raises into the caller; the reason goes to log.warning).
    This function is the ONLY writer of the decision log lines for decisions.
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
        if log_path is None:
            log_path = log_path_for(record.timestamp, log_dir)
        _append_line(log_path, line)
    except Exception as exc:
        log.warning(  # noqa: F821
            f"inverter.apply: decision log append failed: {exc!r}")
        return False

    # Ordering rule: LOG FIRST, TRANSMIT SECOND (above, then here). A command
    # that could not be recorded is not sent.
    _transmit(action, target_power_kw, inverter_type, driver_dir)
    return True


def read_charge(inverter_type="logging", driver_dir=None):
    """Battery charge from the configured driver: (percent, is_stub, marker).

    percent  -- 0-100
    is_stub  -- True when the value is a placeholder (driver says SOC_IS_STUB);
                the caller marks its decisions degraded=soc_stubbed
    marker   -- None, or a degraded marker string the caller must add to its
                decisions: the driver is unavailable or its reading unusable.
                Then percent is the safe placeholder and is_stub is True.
    """
    driver, problem = _driver(inverter_type, driver_dir)
    if driver is None:
        return STUBBED_CHARGE_PERCENT, True, MARKER_UNAVAILABLE
    try:
        ok, value, err = _invoke(driver, "read_charge_percent", (),
                                 DRIVER_TIMEOUT_SECONDS)
        if not ok:
            log.warning(  # noqa: F821
                "inverter: driver %r read_charge_percent failed: %s"
                % (inverter_type, err))
            return STUBBED_CHARGE_PERCENT, True, MARKER_READ_FAILED
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not (0.0 <= value <= 100.0)):
            log.warning(  # noqa: F821
                "inverter: driver %r read_charge_percent returned %r, "
                "expected a number 0-100" % (inverter_type, value))
            return STUBBED_CHARGE_PERCENT, True, MARKER_READ_FAILED
        return float(value), bool(getattr(driver, "SOC_IS_STUB", False)), None
    except Exception as exc:
        log.warning(  # noqa: F821
            "inverter: driver %r read failed: %r" % (inverter_type, exc))
        return STUBBED_CHARGE_PERCENT, True, MARKER_READ_FAILED
