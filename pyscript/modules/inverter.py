"""Inverter boundary: always records intent, then hands it to the configured driver.

Public surface (documented in docs/inverter-boundary.md):
    apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None,
          log_dir=DEFAULT_LOG_DIR,
          resend_minutes=DEFAULT_RESEND_MINUTES, state_dir=None,
          dry_run=False) -> bool
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

Two driver styles. A send-style driver defines send(action, power) and talks to
the inverter itself. A plan-style driver defines plan(action, power): a pure
function returning an ordered list of Home Assistant service calls
[{"domain": ..., "service": ..., "data": {...}}, ...]. It runs natively in the
driver-call helper (thread + timeout) and only returns data; THIS file then
executes the calls with pyscript's service.call, one after the other. The list
is validated first (at most MAX_PLAN_CALLS items; domain and service are
lowercase slugs [a-z0-9_]; data is a dict with str keys and values that are
str, int, float, bool or a list of those; anything else is a failed send). A
call that raises stops the sequence and counts as a failed send. With plan() the
driver's send() is not used. A plan-style driver may also declare SOC_ENTITY (the
battery charge sensor in percent), read here with state.get, so it needs no
read_charge_percent(). dry_run (config inverter.dry_run) logs the planned calls
and executes nothing, yet the command is recorded as sent, so the de-duplication
and the resend behave exactly as in a live run.

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
# Plan-style drivers: the longest service-call list one command may produce, and
# how often the same kind of failure is logged again (minutes).
MAX_PLAN_CALLS = 12
LOG_REPEAT_MINUTES = 30
# {key: UTC time of the last log line of that kind} for rate-limited errors.
_logged_at = {}


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
    """Temp file, fsync, rename over the target. Error text or None.

    The temp name is unique per writer (process and thread): the planner and
    the peak guard both write last_command.json from executor threads, and a
    shared temp name let one writer replace or rename the other's file.
    """
    import json
    import threading
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        try:
            os.remove(tmp)
        except Exception:
            pass
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
def _check_plan(plan):
    """Validate a plan() result. Returns (calls, None) or (None, reason).

    calls is a fresh list of {"domain", "service", "data"} dicts, so nothing the
    driver thread still holds is shared with the caller.
    """
    import math
    import re
    if not isinstance(plan, (list, tuple)):
        return None, "plan() returned %s, expected a list" % type(plan).__name__
    if len(plan) > 12:
        return None, "plan() returned %d calls, at most 12 are allowed" % len(plan)
    slug = re.compile(r"[a-z0-9_]+")
    out = []
    for i, item in enumerate(plan):
        where = "call %d" % i
        if not isinstance(item, dict):
            return None, "%s is not a dict" % where
        extra = set(item) - {"domain", "service", "data"}
        if extra or "domain" not in item or "service" not in item:
            return None, ("%s must have exactly the keys domain, service and "
                          "data (got %s)" % (where, sorted(map(str, item))))
        for key in ("domain", "service"):
            v = item[key]
            if not isinstance(v, str) or not slug.fullmatch(v):
                return None, ("%s: %s %r must be a lowercase slug "
                              "[a-z0-9_]" % (where, key, v))
        data = item.get("data", {})
        if not isinstance(data, dict):
            return None, "%s: data must be a dict" % where
        clean = {}
        for k, v in data.items():
            if not isinstance(k, str) or not k or k in (
                    "blocking", "return_response", "limit"):
                return None, "%s: data key %r is not allowed" % (where, k)
            values = v if isinstance(v, list) else [v]
            for x in values:
                if isinstance(x, float) and not math.isfinite(x):
                    return None, "%s: data %r holds a non-finite number" % (where, k)
                if not isinstance(x, (str, int, float, bool)):
                    return None, ("%s: data %r holds a %s; only str, int, "
                                  "float, bool or a list of those"
                                  % (where, k, type(x).__name__))
            clean[k] = list(v) if isinstance(v, list) else v
        out.append({"domain": item["domain"], "service": item["service"],
                    "data": clean})
    return out, None


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
        if not callable(getattr(module, "plan", None)):
            if not callable(getattr(module, "send", None)):
                raise AttributeError("%s defines no send() or plan()" % path)
        entity = getattr(module, "SOC_ENTITY", None)
        if entity is not None:
            if not isinstance(entity, str) or not re.fullmatch(
                    r"[a-z0-9_]+\.[a-z0-9_]+", entity):
                raise ValueError("%s: SOC_ENTITY %r is not an entity id"
                                 % (path, entity))
        elif not callable(getattr(module, "read_charge_percent", None)):
            raise AttributeError(
                "%s defines no read_charge_percent() or SOC_ENTITY" % path)
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


def _log_limited(key, message, when):
    """log.error at most once per LOG_REPEAT_MINUTES for the same key."""
    try:
        last = _logged_at.get(key)
        if last is not None:
            gap = (when - last).total_seconds()
            if 0 <= gap < LOG_REPEAT_MINUTES * 60:
                return
        _logged_at[key] = when
    except Exception:
        pass
    log.error(message)  # noqa: F821


def _describe(calls):
    parts = []
    for c in calls:
        parts.append("%s.%s %r" % (c["domain"], c["service"], c["data"]))
    return "; ".join(parts) or "(no calls)"


def _transmit_plan(driver, action, target_power_kw, inverter_type, dry_run,
                   when):
    """Ask the driver's plan() for service calls and execute them in order.

    True only when every call ran (or, in a dry run, the plan was valid). A
    bad plan, a plan() that raises or times out, or a call that raises stops
    here and returns False; nothing is raised into the caller.
    """
    ok, value, err = _invoke(driver, "plan", (action, target_power_kw),
                             DRIVER_TIMEOUT_SECONDS)
    if not ok:
        _log_limited(
            inverter_type + ":plan",
            "inverter: driver %r plan(%s, %s) failed: %s"
            % (inverter_type, action, target_power_kw, err), when)
        return False
    calls, why = _check_plan(value)
    if calls is None:
        _log_limited(
            inverter_type + ":invalid",
            "inverter: driver %r plan(%s, %s) is invalid, nothing executed: %s"
            % (inverter_type, action, target_power_kw, why), when)
        return False
    if dry_run:
        log.info(  # noqa: F821
            "inverter: dry run, %s %s kW would call: %s (not executed)"
            % (action, target_power_kw, _describe(calls)))
        return True
    for index, call in enumerate(calls):
        try:
            service.call(  # noqa: F821  (pyscript global)
                call["domain"], call["service"], **call["data"])
        except Exception as exc:
            _log_limited(
                inverter_type + ":call",
                "inverter: driver %r %s %s kW stopped at call %d of %d "
                "(%s.%s): %r; the command counts as failed"
                % (inverter_type, action, target_power_kw, index + 1,
                   len(calls), call["domain"], call["service"], exc), when)
            return False
    return True


def _transmit(action, target_power_kw, inverter_type, driver_dir,
              dry_run=False, when=None):
    """Hand the intent to the driver. Called only AFTER the log line is on disk.

    Never raises and never changes what apply() returns: the log already
    explains the intent, whatever the driver does. A missing driver means
    nothing is sent (logging behaviour), loudly.

    Returns True only when the driver accepted the command (no exception, no
    timeout, did not return False; for a plan-style driver every service call
    ran); apply() records only those as sent. In a dry run nothing is sent or
    executed and the command counts as accepted.
    """
    try:
        driver, problem = _driver(inverter_type, driver_dir)
        if driver is None:
            log.error(  # noqa: F821  (pyscript global)
                "inverter: %s; NOT transmitting %s %s kW, decision logged "
                "only" % (problem, action, target_power_kw))
            return False
        if callable(getattr(driver, "plan", None)):
            return _transmit_plan(driver, action, target_power_kw,
                                  inverter_type, dry_run, when)
        if dry_run:
            log.info(  # noqa: F821
                "inverter: dry run, driver %r would be sent %s %s kW "
                "(not sent)" % (inverter_type, action, target_power_kw))
            return True
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


_send_streak = [0]        # consecutive failed sends (see send_failure_streak)


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


def send_failure_streak():
    """Number of driver commands in a row that were not accepted (an
    exception, a timeout, False, a missing driver). 0 after any accepted
    command; the logging driver and a dry run never count. Read-only."""
    return _send_streak[0]


def last_sent_action(log_dir=DEFAULT_LOG_DIR, state_dir=None):
    """Action of the last command a driver accepted ("charge", "discharge",
    "export" or "idle"), or None when nothing is on record. Reads the shared
    last_command.json (and this process's memory); never raises."""
    try:
        last = _last_command(_state_file(state_dir, log_dir))
        return None if last is None else last[0]
    except Exception:
        return None


def apply(action, target_power_kw, record, log_path=None,
          inverter_type="logging", driver_dir=None, log_dir=DEFAULT_LOG_DIR,
          resend_minutes=DEFAULT_RESEND_MINUTES, state_dir=None,
          dry_run=False):
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
    dry_run          -- True: transmit and execute nothing, log what would have
                        been done (info level); the command is still recorded as
                        sent, so de-duplication and resend behave as when live
                        (config inverter.dry_run)

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
        _transmit(action, target_power_kw, inverter_type, driver_dir,
                  dry_run, record.timestamp)
        return True
    path = _state_file(state_dir, log_dir)
    hold = _hold_minutes(inverter_type, driver_dir)
    if _should_send(action, target_power_kw, record, resend_minutes, path,
                    hold):
        if _transmit(action, target_power_kw, inverter_type, driver_dir,
                     dry_run, record.timestamp):
            _send_streak[0] = 0
            _remember_sent(action, target_power_kw, record, path)
        else:
            _send_streak[0] = _send_streak[0] + 1
            _forget_sent(path)
    return True


def read_charge(inverter_type="logging", driver_dir=None):
    """Battery charge from the configured driver: (percent, is_stub, marker).

    percent  -- 0-100. A driver that declares SOC_ENTITY (a sensor in percent)
                is read with state.get here; unavailable, unknown, not a
                number or outside 0-100 gives the placeholder and the marker
                inverter_read_failed. Such a driver is never a stub.
    is_stub  -- True when the value is a placeholder (driver says SOC_IS_STUB);
                the caller marks its decisions degraded=soc_stubbed
    marker   -- None, or a degraded marker string the caller must add to its
                decisions: the driver is unavailable or its reading unusable.
                Then percent is the safe placeholder and is_stub is True.
    """
    driver, problem = _driver(inverter_type, driver_dir)
    if driver is None:
        return STUBBED_CHARGE_PERCENT, True, MARKER_UNAVAILABLE
    entity = getattr(driver, "SOC_ENTITY", None)
    if entity is not None:
        try:
            value = float(state.get(entity))  # noqa: F821  (pyscript global)
            if not (0.0 <= value <= 100.0):
                raise ValueError("%r is outside 0-100" % (value,))
            return value, False, None
        except Exception as exc:
            log.warning(  # noqa: F821
                "inverter: driver %r charge sensor %s unusable: %r"
                % (inverter_type, entity, exc))
            return STUBBED_CHARGE_PERCENT, True, MARKER_READ_FAILED
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
