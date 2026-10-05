"""Conformance check for inverter drivers (pyscript/modules/inverter_<name>.py).

Use it while writing a driver:

    python3 -m pytest tests/test_driver_conformance.py \
        --driver pyscript/modules/inverter_<name>.py

or from Python:

    import driver_conformance
    driver_conformance.check_driver("pyscript/modules/inverter_alphaess.py")

WARNING: the check really calls your driver's send() and read_charge_percent()
with every action the planner can emit. A driver that talks to hardware will
send those commands. Run it with the inverter disconnected, in a simulator, or
with your transport mocked.

The interface is documented in pyscript/modules/inverter_logging.py and in the
README, "Adding an inverter driver". What is NOT checked, because it cannot be
verified without hardware: that send("idle", 0.0) really cancels every forced
mode this project set (forced grid charge, forced export, forced discharge)
and returns the inverter to its own default behaviour. Check that yourself.
What is checked:

  * file name inverter_<slug>.py, slug of lowercase letters, digits, underscore
  * importing the file does no network access (checked in a subprocess with
    sockets blocked) and does not raise
  * send(action, target_power_kw) exists and, for charge, discharge, export and
    idle, with 0.0 for idle and a positive power (and the maximum power)
    otherwise: returns True, does not raise, returns within the time bound,
    and gives the same answer when repeated
  * read_charge_percent() returns a real number 0..100 (not a bool, not NaN)
  * SOC_IS_STUB, when present, is a bool
  * COMMAND_HOLD_MINUTES, when present, is None (unknown) or a positive number
    (not a bool): how long the inverter keeps a forced command without a
    refresh. The planner re-sends an unchanged command at 0.8 x that.

Third-party imports (for example pymodbus) are allowed: drivers are loaded as
ordinary CPython. They only have to be installed where the check runs.
"""
import importlib.util
import json
import math
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ACTIONS = ("charge", "discharge", "export", "idle")
FILE_NAME = re.compile(r"inverter_([a-z0-9_]+)\.py")
# The framework gives a driver call 10 s (inverter.DRIVER_TIMEOUT_SECONDS).
# Staying well under it leaves room for a slow bus.
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_POWER_KW = 5.0
IMPORT_TIMEOUT_SECONDS = 30.0


class ConformanceError(AssertionError):
    """The driver does not follow the driver interface. str() lists every problem."""

    def __init__(self, path, problems):
        self.path = str(path)
        self.problems = list(problems)
        super().__init__(
            "%s does not conform to the inverter driver interface:\n  - %s"
            % (self.path, "\n  - ".join(self.problems)))


# Run in a subprocess: import the driver with every socket operation blocked
# and report what happened as one JSON line.
_IMPORT_PROBE = r"""
import importlib.util, json, socket, sys, traceback
attempts = []
def blocked(name):
    def fn(*args, **kwargs):
        attempts.append(name)
        raise OSError("network access is blocked during driver import")
    return fn
for attr in ("connect", "connect_ex", "sendto"):
    setattr(socket.socket, attr, blocked("socket." + attr))
for attr in ("create_connection", "getaddrinfo", "gethostbyname",
             "gethostbyname_ex"):
    setattr(socket, attr, blocked("socket." + attr))
path, name = sys.argv[1], sys.argv[2]
result = {"ok": True, "error": None, "network": attempts}
try:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
except BaseException as exc:
    result["ok"] = False
    result["error"] = "%s: %s" % (type(exc).__name__, exc)
print("CONFORMANCE_RESULT " + json.dumps(result))
"""


def _slug_problems(path):
    match = FILE_NAME.fullmatch(path.name)
    if not match:
        return None, [
            "file name %r must be inverter_<name>.py with <name> made of "
            "lowercase letters, digits and underscore" % path.name]
    return match.group(1), []


def _import_probe(path, slug):
    """Problems found when importing `path` with the network blocked."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _IMPORT_PROBE, str(path),
             "inverter_driver_" + slug],
            capture_output=True, text=True, timeout=IMPORT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return ["importing the file did not finish within %.0f s (blocking "
                "work at import time? move it into send/read_charge_percent)"
                % IMPORT_TIMEOUT_SECONDS]
    lines = [ln for ln in proc.stdout.splitlines()
             if ln.startswith("CONFORMANCE_RESULT ")]
    if not lines:
        return ["importing the file crashed the interpreter: %s"
                % (proc.stderr.strip()[-300:] or "no output")]
    result = json.loads(lines[-1].split(" ", 1)[1])
    problems = []
    if result["network"]:
        problems.append(
            "network access at import time (%s); open connections inside "
            "send/read_charge_percent, never when the file is imported"
            % ", ".join(sorted(set(result["network"]))))
    if not result["ok"]:
        problems.append("importing the file failed: %s" % result["error"])
    return problems


def _load(path, slug):
    name = "inverter_driver_" + slug
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _call(fn, args, timeout):
    """(ok, value_or_exception_text, seconds). A call over `timeout` is a failure."""
    box = []

    def run():
        try:
            box.append((True, fn(*args)))
        except BaseException as exc:
            box.append((False, "%s: %s" % (type(exc).__name__, exc)))

    started = time.monotonic()
    worker = threading.Thread(target=run, daemon=True, name="conformance-call")
    worker.start()
    worker.join(timeout)
    elapsed = time.monotonic() - started
    if not box:
        return False, "no answer within %.1f s" % timeout, elapsed
    return box[0][0], box[0][1], elapsed


def _send_problems(module, timeout, max_power_kw):
    send = getattr(module, "send", None)
    if not callable(send):
        return ["send(action, target_power_kw) is missing or not callable"]
    problems = []
    cases = [("idle", 0.0)]
    for action in ("charge", "discharge", "export"):
        cases += [(action, 0.5), (action, max_power_kw)]
    for action, power in cases + cases[:3]:   # the tail repeats calls unchanged
        label = "send(%r, %s)" % (action, power)
        ok, value, elapsed = _call(send, (action, power), timeout)
        if not ok:
            problems.append("%s failed on valid input: %s" % (label, value))
        elif value is not True:
            problems.append(
                "%s returned %r; return True when the inverter accepted the "
                "command (False or raising means it did not)" % (label, value))
        elif elapsed > timeout:
            problems.append("%s took %.1f s, over the %.1f s bound"
                            % (label, elapsed, timeout))
    return _unique(problems)


def _read_problems(module, timeout):
    read = getattr(module, "read_charge_percent", None)
    if not callable(read):
        return ["read_charge_percent() is missing or not callable"]
    problems = []
    for _ in range(2):
        ok, value, elapsed = _call(read, (), timeout)
        if not ok:
            problems.append("read_charge_percent() failed: %s" % (value,))
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(
                "read_charge_percent() returned %r (%s); expected a number "
                "0..100" % (value, type(value).__name__))
        elif math.isnan(value) or not 0.0 <= value <= 100.0:
            problems.append(
                "read_charge_percent() returned %r; expected 0..100 (percent, "
                "not a 0..1 fraction)" % (value,))
        elif elapsed > timeout:
            problems.append("read_charge_percent() took %.1f s, over the "
                            "%.1f s bound" % (elapsed, timeout))
    return _unique(problems)


def _unique(items):
    return list(dict.fromkeys(items))


def find_problems(driver, timeout=DEFAULT_TIMEOUT_SECONDS,
                  max_power_kw=DEFAULT_MAX_POWER_KW):
    """List of human-readable problems; empty when the driver conforms.

    `driver` is the path of the driver file (or a module loaded from one).
    """
    path = getattr(driver, "__file__", None) or driver
    path = Path(path)
    slug, problems = _slug_problems(path)
    if not path.is_file():
        return ["driver file %s does not exist" % path]
    if slug is None:
        return problems
    problems += _import_probe(path, slug)
    if problems:      # never import in-process what failed or touched the network
        return problems
    try:
        module = _load(path, slug)
    except BaseException as exc:
        problems.append("importing the file failed: %s: %s"
                        % (type(exc).__name__, exc))
        return problems
    if hasattr(module, "SOC_IS_STUB") and not isinstance(
            module.SOC_IS_STUB, bool):
        problems.append("SOC_IS_STUB is %r; it must be a bool (or be left out)"
                        % (module.SOC_IS_STUB,))
    if hasattr(module, "COMMAND_HOLD_MINUTES"):
        hold = module.COMMAND_HOLD_MINUTES
        if hold is not None and (
                isinstance(hold, bool) or not isinstance(hold, (int, float))
                or not 0 < hold < 1.0e9):
            problems.append(
                "COMMAND_HOLD_MINUTES is %r; it must be a positive number of "
                "minutes, or None / left out when unknown" % (hold,))
    problems += _send_problems(module, timeout, max_power_kw)
    problems += _read_problems(module, timeout)
    return problems


def check_driver(driver, timeout=DEFAULT_TIMEOUT_SECONDS,
                 max_power_kw=DEFAULT_MAX_POWER_KW):
    """Raise ConformanceError (an AssertionError) listing every problem."""
    problems = find_problems(driver, timeout, max_power_kw)
    if problems:
        raise ConformanceError(getattr(driver, "__file__", None) or driver,
                               problems)
