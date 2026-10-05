"""Inverter boundary: always records intent, then hands it to the configured driver.

Public surface (documented in docs/inverter-boundary.md):
    apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None,
          log_dir=DEFAULT_LOG_DIR,
          resend_minutes=DEFAULT_RESEND_MINUTES, state_dir=None) -> bool
    log_path_for(day, log_dir=DEFAULT_LOG_DIR)
        -> "<log_dir>/decisions-YYYY-MM-DD.log"
    read_charge(inverter_type="logging", driver_dir=None)
        -> (percent, is_stub, marker)
driver_dir=None means DEFAULT_DRIVER_DIR. state_dir=None means
<log_dir>/state.

What "idle" means: send("idle", 0.0) cancels every forced mode this project
set (forced grid charge, forced export, forced discharge) and returns the
inverter to its own default behaviour (charge from solar surplus until full,
then export; drain to serve the house until empty, then use grid power).
Sending nothing is not the same as idle: a forced mode stays in force until it
is cancelled or the inverter drops it.

Command de-duplication: the decision log line is written on every call, but the
driver's send() is called only when the command (action, power rounded to
0.01 kW) differs from the last command SENT, or when that send is old enough to
need a refresh. A driver may declare COMMAND_HOLD_MINUTES (a positive number:
the inverter drops a forced command after about this long without a refresh;
absent or None = unknown): an unchanged command is then re-sent once its age is
0.8 x that. Without the declaration resend_minutes applies (0 = send on every
call). A record older than the window counts as expired, so a stale
last_command.json after a restart never blocks a send. An idle command is the
exception:
it is sent when the last command on record was not idle, or when no command is
on record (the first call after an install or restart), and then not again
while idle persists. The last sent command is kept in
<state_dir>/last_command.json, so it survives a restart and is shared by the
planner and the peak guard (both call apply). A send that failed (exception,
timeout, False, driver missing) is never recorded and clears the record, so the
next call sends again. The logging driver transmits nothing and is not tracked.

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
import datetime
import os

import decision

# Home Assistant config dir inside the container. Parameterised on apply() so
# tests can redirect it. The decision log is one file per local calendar day,
# decisions-YYYY-MM-DD.log, so old days can be removed by file name or age.
DEFAULT_LOG_DIR = "/config/battery_planner"
DEFAULT_DRIVER_DIR = "/config/pyscript/modules"
# Command de-duplication (see the module docstring).
DEFAULT_RESEND_MINUTES = 15
LAST_COMMAND_FILE = "last_command.json"
# Rounding of the commanded power when comparing commands (kW).
POWER_DECIMALS = 2
# A driver may declare COMMAND_HOLD_MINUTES: the inverter drops a forced command
# after about this long without a refresh. An unchanged command is then sent
# again once its age reaches this share of the hold (the margin covers a late
# cycle). Without the declaration the config resend_minutes applies instead.
HOLD_ATTRIBUTE = "COMMAND_HOLD_MINUTES"
RESEND_FRACTION = 0.8
# Largest hold that is believable (minutes); also rejects infinity and NaN.
HOLD_LIMIT_MINUTES = 1.0e9


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

# {driver key: True} for drivers whose COMMAND_HOLD_MINUTES was already
# reported as invalid, so the warning is logged once, not on every call.
_hold_warned = {}

# {state file path: (action, power_kw, sent_at)}. Fallback for when the state
# file cannot be written: without it an unwritable state dir would send every
# call again. The file is read on every call, so it stays the shared truth.
_last_sent = {}


# WHY @pyscript_executor, two things in one decorator. (1) It compiles this
# helper to native Python, which pyscript's interpreter needs before anything
# can run it outside itself. (2) It runs the compiled helper in an executor
# thread, and THAT is what keeps the blocking open()/write()/fsync() off Home
# Assistant's event loop. @pyscript_compile alone only does
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
def _read_state(path):
    """Decoded JSON of a small state file, or None when missing or corrupt."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


@pyscript_executor  # noqa: F821
def _write_state(path, payload):
    """Temp file, fsync, rename over the target. Error text or None."""
    import json
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        return repr(exc)
    return None


@pyscript_executor  # noqa: F821
def _remove_state(path):
    """Delete a state file; a missing file is fine."""
    try:
        os.remove(path)
    except Exception:
        pass


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

    Returns True only when the driver accepted the command (no exception, no
    timeout, did not return False); apply() records only those as sent.
    """
    try:
        driver, problem = _driver(inverter_type, driver_dir)
        if driver is None:
            log.error(  # noqa: F821  (pyscript global)
                "inverter: %s; NOT transmitting %s %s kW, decision logged "
                "only" % (problem, action, target_power_kw))
            return False
        ok, value, err = _invoke(driver, "send", (action, target_power_kw),
                                 DRIVER_TIMEOUT_SECONDS)
        if not ok:
            log.error(  # noqa: F821
                "inverter: driver %r send(%s, %s) failed: %s"
                % (inverter_type, action, target_power_kw, err))
            return False
        if value is False:
            log.warning(  # noqa: F821
                "inverter: driver %r send(%s, %s) reported failure"
                % (inverter_type, action, target_power_kw))
            return False
        return True
    except Exception as exc:
        log.error(  # noqa: F821
            "inverter: driver %r transmit failed: %r" % (inverter_type, exc))
        return False


def _state_file(state_dir, log_dir):
    if state_dir is None:
        state_dir = log_dir.rstrip("/") + "/state"
    return state_dir.rstrip("/") + "/" + LAST_COMMAND_FILE


def _utc(when):
    return when.astimezone(datetime.timezone.utc)


def _last_command(path):
    """(action, power_kw, sent_at) of the last command sent, or None.

    Reads the shared file; falls back to this process's own memory when the
    file is missing, corrupt or older. Never raises.
    """
    found = None
    data = _read_state(path)
    if isinstance(data, dict):
        action = data.get("action")
        power = data.get("power_kw")
        stamp = data.get("sent_at")
        try:
            if (isinstance(action, str) and isinstance(stamp, str)
                    and not isinstance(power, bool)
                    and isinstance(power, (int, float))
                    and -1.0e9 < power < 1.0e9):
                when = datetime.datetime.fromisoformat(stamp)
                if when.tzinfo is not None:
                    found = (action, float(power), when)
        except Exception:
            found = None
    remembered = _last_sent.get(path)
    if remembered is not None and (found is None or remembered[2] > found[2]):
        found = remembered
    return found


def _hold_minutes(inverter_type, driver_dir):
    """The driver's COMMAND_HOLD_MINUTES as a float, or None when unknown.

    Unknown = the driver is not loaded, declares nothing, or declares None. A
    declaration that is not a positive finite number is ignored with one
    warning. Never raises.
    """
    try:
        driver, problem = _driver(inverter_type, driver_dir)
        if driver is None:
            return None
        value = getattr(driver, HOLD_ATTRIBUTE, None)
        if value is None:
            return None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not (0 < value < HOLD_LIMIT_MINUTES)):
            key = "%s/%s" % (driver_dir, inverter_type)
            if not _hold_warned.get(key):
                _hold_warned[key] = True
                log.warning(  # noqa: F821
                    "inverter: driver %r declares %s = %r, expected a "
                    "positive number of minutes; ignored, resend_minutes "
                    "applies" % (inverter_type, HOLD_ATTRIBUTE, value))
            return None
        return float(value)
    except Exception as exc:
        log.warning(  # noqa: F821
            "inverter: cannot read %s from driver %r: %r"
            % (HOLD_ATTRIBUTE, inverter_type, exc))
        return None


def _should_send(action, target_power_kw, record, resend_minutes, path,
                 hold_minutes=None):
    """True when the driver must be called: new command or the resend is due.

    An unchanged command is due again when its age reaches the resend window:
    RESEND_FRACTION of the driver's declared hold (hold_minutes), else
    resend_minutes (0 = every call). A record older than that is expired, so a
    stale file left by a restart never blocks a needed send.

    Any trouble while deciding means send: a repeated command is harmless, a
    withheld one is not.
    """
    try:
        if hold_minutes is not None:
            window = RESEND_FRACTION * hold_minutes
        else:
            window = resend_minutes
        if window <= 0:
            return True
        last = _last_command(path)
        if last is None:
            return True
        if action == "idle":
            # idle = clear every command we sent and go back to the inverter's
            # default. Once is enough: it is sent after a forced command (or
            # with nothing on record) and never repeated while idle persists.
            return last[0] != "idle"
        same = (last[0] == action
                and round(last[1], POWER_DECIMALS)
                == round(target_power_kw, POWER_DECIMALS))
        if not same:
            return True
        age = (record.timestamp - last[2]).total_seconds()
        return age < 0 or age >= window * 60
    except Exception as exc:
        log.warning(  # noqa: F821
            "inverter: cannot check the last sent command (%r); sending" % (exc,))
        return True


def _remember_sent(action, target_power_kw, record, path):
    """Persist (and remember in memory) the command the driver accepted."""
    try:
        power = round(target_power_kw, POWER_DECIMALS)
        when = _utc(record.timestamp)
        _last_sent[path] = (action, power, when)
        err = _write_state(path, {
            "action": action, "power_kw": power, "sent_at": when.isoformat()})
        if err:
            log.warning(  # noqa: F821
                "inverter: cannot save the last sent command to %s: %s"
                % (path, err))
    except Exception as exc:
        log.warning(  # noqa: F821
            "inverter: cannot record the last sent command: %r" % (exc,))


def _forget_sent(path):
    """A failed send leaves the inverter's state unknown: forget the record."""
    try:
        _last_sent.pop(path, None)
        _remove_state(path)
    except Exception as exc:
        log.warning(  # noqa: F821
            "inverter: cannot clear the last sent command: %r" % (exc,))


def apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None, log_dir=DEFAULT_LOG_DIR,
          resend_minutes=DEFAULT_RESEND_MINUTES, state_dir=None):
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
    resend_minutes   -- fallback for drivers that declare no COMMAND_HOLD_MINUTES:
                        the driver gets an unchanged command again only after
                        this many minutes; 0 = every call (config key
                        inverter.resend_minutes). Idle is never re-sent while
                        it persists (unless this is 0). A driver that declares
                        COMMAND_HOLD_MINUTES is re-sent at 0.8 x that instead
    state_dir        -- where last_command.json lives; None = <log_dir>/state

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
    # that could not be recorded is not sent. The log line above is written on
    # every call; only the driver call below is de-duplicated.
    if inverter_type == "logging":
        _transmit(action, target_power_kw, inverter_type, driver_dir)
        return True
    path = _state_file(state_dir, log_dir)
    hold = _hold_minutes(inverter_type, driver_dir)
    if _should_send(action, target_power_kw, record, resend_minutes, path,
                    hold):
        if _transmit(action, target_power_kw, inverter_type, driver_dir):
            _remember_sent(action, target_power_kw, record, path)
        else:
            _forget_sent(path)
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
