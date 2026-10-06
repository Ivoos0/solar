"""Tests for pyscript/modules/inverter.py.

Documented extension beyond the WP text: inverter.py is a pyscript module, but it
is thin enough to load here by injecting stand-ins for the names pyscript
provides at runtime: an identity `pyscript_executor` decorator and a `log`
object that collects warnings.
"""
import ast
import builtins
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import os

import pytest

import decision

SRC = Path(__file__).resolve().parent.parent / "pyscript" / "modules" / "inverter.py"
TZ = ZoneInfo("Europe/Brussels")

MODULES = SRC.parent
ALLOWED_IMPORTS = {"os", "decision", "importlib.util", "re", "sys",
                   "threading", "datetime", "json", "math"}
ALLOWED_CALL_NAMES = {"open", "abs", "_append_line", "isinstance", "callable",
                      "getattr", "bool", "float", "ValueError", "AttributeError",
                      "FileNotFoundError", "repr", "_load_driver", "_invoke",
                      "_driver", "_transmit", "run", "fn", "log_path_for",
                      "_read_state", "_write_state", "_remove_state",
                      "_state_file", "_utc", "_last_command", "_should_send",
                      "_remember_sent", "_forget_sent", "round",
                      "_hold_minutes", "_log_limited", "_transmit_plan",
                      "_describe", "_check_plan", "sorted", "set", "enumerate",
                      "type", "list", "map", "len"}
ALLOWED_CALL_ATTRS = {"write", "flush", "fileno", "fsync", "makedirs",
                      "dirname", "format_record", "warning", "error", "get",
                      "isfile", "join", "fullmatch", "spec_from_file_location",
                      "module_from_spec", "exec_module", "pop", "append",
                      "Thread", "start", "rstrip", "load", "dumps",
                      "replace", "remove", "astimezone", "fromisoformat",
                      "total_seconds", "isoformat", "items", "info", "compile",
                      "isfinite", "getpid", "get_ident"}


class FakeLog:
    """Stand-in for pyscript's global `log`."""

    def __init__(self):
        self.messages = []
        self.errors = []
        self.infos = []

    def warning(self, msg, *args):
        self.messages.append(msg % args if args else msg)

    def error(self, msg, *args):
        self.errors.append(msg % args if args else msg)

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)


class FakeService:
    """Stand-in for pyscript's `service`: records calls, can fail."""

    def __init__(self):
        self.calls = []
        self.fail_at = None            # 0-based index of the call that raises
        self.fail_service = None       # or: every call of this service raises
        self.error = RuntimeError("boom")

    def call(self, domain, service, **data):
        index = len(self.calls)
        self.calls.append((domain, service, data))
        if (self.fail_at is not None and index == self.fail_at)                 or (self.fail_service is not None
                    and service == self.fail_service):
            raise self.error


class FakeState:
    """Stand-in for pyscript's `state`: get() of a known entity or NameError."""

    def __init__(self):
        self.values = {}

    def get(self, entity):
        if entity not in self.values:
            raise NameError("name %s is not defined" % entity)
        return self.values[entity]


_INJECTED = ("pyscript_executor", "log", "service", "state")


@pytest.fixture
def inverter():
    saved = {n: getattr(builtins, n) for n in _INJECTED if hasattr(builtins, n)}
    builtins.pyscript_executor = lambda fn: fn
    builtins.log = FakeLog()
    builtins.service = FakeService()
    builtins.state = FakeState()
    try:
        spec = importlib.util.spec_from_file_location("inverter_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.DEFAULT_DRIVER_DIR = str(MODULES)    # the shipped inverter_logging
        yield mod
    finally:
        for n in _INJECTED:
            if n in saved:
                setattr(builtins, n, saved[n])
            elif hasattr(builtins, n):
                delattr(builtins, n)


@pytest.fixture
def warnings(inverter):
    return builtins.log.messages


def make_record(**over):
    base = dict(
        timestamp=datetime(2026, 9, 29, 14, 35, tzinfo=TZ), action="export",
        target_power_kw=2.5, charge_percent=78.0, charge_kwh=7.8,
        consumption_price=0.214, injection_price=0.189,
        forecast_remaining_kwh=11.2, usage_remaining_kwh=14.6,
        saturation_block=None, spill_kwh=2.4, reserve_breach_block=None,
        projected_end_charge_kwh=2.1, duration_ms=84, running_average_kw=1.2,
        ceiling_kw=2.5, budget_kw=1.95, vetoes_applied=[], selector="S3",
        reasoning="leftover 4.1kWh", degraded_inputs=["soc_stubbed"],
        source="planner")
    base.update(over)
    return decision.DecisionRecord(**base)


# ---- behaviour -----------------------------------------------------------

def test_appends_one_line_equal_to_format_record(inverter, tmp_path, warnings):
    log = tmp_path / "decisions.log"
    r = make_record()
    assert inverter.apply("export", 2.5, r, log_path=str(log)) is True
    assert log.read_text(encoding="utf-8") == decision.format_record(r) + "\n"
    assert warnings == []


def test_repeated_calls_append_and_earlier_lines_survive(inverter, tmp_path):
    log = tmp_path / "decisions.log"
    r1 = make_record()
    r2 = make_record(action="idle", target_power_kw=0.0, selector="S6")
    assert inverter.apply("export", 2.5, r1, str(log))
    first = log.read_text()
    assert inverter.apply("idle", 0.0, r2, str(log))
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] + "\n" == first
    assert lines[1] == decision.format_record(r2)


def test_never_truncates_existing_content(inverter, tmp_path):
    log = tmp_path / "decisions.log"
    log.write_text("pre-existing line\n")
    assert inverter.apply("export", 2.5, make_record(), str(log))
    lines = log.read_text().splitlines()
    assert lines[0] == "pre-existing line" and len(lines) == 2


def test_creates_parent_directory(inverter, tmp_path):
    log = tmp_path / "a" / "b" / "decisions.log"
    assert inverter.apply("export", 2.5, make_record(), str(log)) is True
    assert log.is_file()


def test_unwritable_path_returns_false_and_warns(inverter, tmp_path, warnings):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    # A parent that is a regular file cannot be a directory.
    assert inverter.apply("export", 2.5, make_record(),
                          str(blocker / "sub" / "d.log")) is False
    assert len(warnings) == 1 and "append failed" in warnings[0]


def test_record_formatting_failure_returns_false_and_warns(
        inverter, tmp_path, warnings):
    log = tmp_path / "decisions.log"

    class Bad:
        pass

    assert inverter.apply("export", 2.5, Bad(), str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1


def test_format_record_exception_returns_false_and_warns(
        inverter, tmp_path, warnings, monkeypatch):
    def boom(_record):
        raise RuntimeError("cannot format")

    monkeypatch.setattr(decision, "format_record", boom)
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", 2.5, make_record(), str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1 and "cannot format" in warnings[0]


def test_default_read_charge_is_the_flagged_stub(inverter):
    v, is_stub, marker = inverter.read_charge()
    assert isinstance(v, float) and 0.0 <= v <= 100.0
    assert v == inverter.STUBBED_CHARGE_PERCENT == 50.0
    assert is_stub is True and marker is None


# ---- action / target power must match the record -------------------------

def test_action_mismatch_writes_nothing_and_warns(inverter, tmp_path, warnings):
    log = tmp_path / "decisions.log"
    assert inverter.apply("charge", 2.5, make_record(), str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1 and "do not match" in warnings[0]


def test_power_mismatch_writes_nothing_and_warns(inverter, tmp_path, warnings):
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", 9.9, make_record(), str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1


def test_tiny_float_difference_is_accepted(inverter, tmp_path, warnings):
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", 2.5 + 1e-12, make_record(), str(log)) is True
    assert len(log.read_text().splitlines()) == 1
    assert warnings == []


def test_visible_power_difference_is_rejected(inverter, tmp_path):
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", 2.5001, make_record(), str(log)) is False
    assert not log.exists()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_target_power_is_refused(inverter, tmp_path, warnings, bad):
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", bad, make_record(), str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1


def test_nan_target_power_warns_and_writes_nothing(inverter, tmp_path, warnings):
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", float("nan"), make_record(), str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1 and "do not match" in warnings[0]


def test_positive_infinity_target_power_is_refused(inverter, tmp_path):
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", float("inf"), make_record(), str(log)) is False
    assert not log.exists()


# ---- one physical line ----------------------------------------------------

@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\r\nb"])
def test_multiline_formatted_line_is_rejected(
        inverter, tmp_path, warnings, monkeypatch, bad):
    monkeypatch.setattr(decision, "format_record", lambda r: bad)
    log = tmp_path / "decisions.log"
    assert inverter.apply("export", 2.5, make_record(), str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1 and "single physical line" in warnings[0]


def test_unsanitised_list_entry_never_writes_two_lines(
        inverter, tmp_path, warnings):
    log = tmp_path / "decisions.log"
    r = make_record()
    # DecisionRecord now rejects this at construction; bypass that to keep the
    # boundary's own last-line defence covered
    object.__setattr__(r, "degraded_inputs", ["soc\nstubbed"])
    assert inverter.apply("export", 2.5, r, str(log)) is False
    assert not log.exists()
    assert len(warnings) == 1


# ---- source structure ------------------------------------------------------

def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _function(tree, name):
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def test_only_the_write_helper_opens_files_in_append_mode():
    tree = _tree()
    owners = set()
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open"):
                owners.add(fn.name)
                # the log is append-only; the state files are read, or
                # written whole through a temp file (never the log).
                assert node.args[1].value == {
                    "_append_line": "a", "_read_state": "r",
                    "_write_state": "w"}[fn.name]
    assert owners == {"_append_line", "_read_state", "_write_state"}
    # No open() call hiding at module level either.
    module_level = [n for n in tree.body if not isinstance(n, ast.FunctionDef)]
    for stmt in module_level:
        for node in ast.walk(stmt):
            assert not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "open")


def test_write_helper_is_decorated_with_exactly_pyscript_executor():
    helper = _function(_tree(), "_append_line")
    assert len(helper.decorator_list) == 1
    dec = helper.decorator_list[0]
    assert isinstance(dec, ast.Name) and dec.id == "pyscript_executor"


def _is_service_call(func):
    """Exactly `service.call(...)`: pyscript's service global, nothing else."""
    return (isinstance(func, ast.Attribute) and func.attr == "call"
            and isinstance(func.value, ast.Name) and func.value.id == "service")


def _violations(source):
    """Everything in `source` outside the import/call allowlist."""
    tree = ast.parse(source)
    found = set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("from:%s" % node.module)
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id not in ALLOWED_CALL_NAMES:
                found.add("call:%s" % f.id)
            elif _is_service_call(f):
                pass       # the one place a plan-style driver's calls run
            elif isinstance(f, ast.Attribute) and f.attr not in ALLOWED_CALL_ATTRS:
                found.add("call:.%s" % f.attr)
            elif not isinstance(f, (ast.Name, ast.Attribute)):
                found.add("call:<dynamic>")
    for extra in imports - ALLOWED_IMPORTS:
        found.add("import:%s" % extra)
    if imports != ALLOWED_IMPORTS:
        found.add("imports-not-exact")
    return found


def test_source_is_within_import_and_call_allowlist():
    assert _violations(SRC.read_text(encoding="utf-8")) == set()


@pytest.mark.parametrize("extra", [
    "import urllib.request",
    "import subprocess",
    "import socket",
    "from os import system",
    "os.open('x', 0)",
    "os.system('true')",
    "subprocess.run(['true'])",
    "urllib.request.urlopen('http://x')",
    "eval('1')",
    "getattr(os, 'system')('true')",
    "hass.services.call('a', 'b')",
    "self.service.call('a', 'b')",
    "service.other('a')",
])
def test_allowlist_rejects_mutated_source(extra):
    """Prove the allowlist bites: mutate a scratch COPY of the source text."""
    text = SRC.read_text(encoding="utf-8")
    assert _violations(text) == set()  # baseline is clean
    mutated = text + "\n" + extra + "\n"
    assert _violations(mutated) != set()


def test_no_transmission_vocabulary_in_source():
    text = SRC.read_text(encoding="utf-8")
    for word in ("socket", "modbus", "pymodbus", "serial", "connect", "HF2211"):
        assert not re.search(word, text, re.IGNORECASE), word


def test_public_surface_is_exactly_four_functions_plus_constants(inverter):
    public = {n for n in vars(inverter) if not n.startswith("_")}
    funcs = {n for n in public
             if callable(getattr(inverter, n))
             and getattr(getattr(inverter, n), "__module__", None)
             == inverter.__name__}
    # last_sent_action is read-only: the guard asks what is still on record
    # after a reload, so it never reaches into a private helper
    assert funcs == {"apply", "read_charge", "log_path_for",
                     "last_sent_action"}
    assert public == {"os", "decision", "datetime", "apply", "read_charge",
                      "last_sent_action",
                      "DEFAULT_RESEND_MINUTES", "LAST_COMMAND_FILE",
                      "HOLD_ATTRIBUTE", "RESEND_FRACTION", "HOLD_LIMIT_MINUTES",
                      "MAX_PLAN_CALLS", "LOG_REPEAT_MINUTES",
                      "POWER_DECIMALS",
                      "DEFAULT_LOG_DIR", "DEFAULT_DRIVER_DIR", "log_path_for",
                      "STUBBED_CHARGE_PERCENT", "POWER_TOLERANCE_KW",
                      "DRIVER_TIMEOUT_SECONDS", "MARKER_UNAVAILABLE",
                      "MARKER_READ_FAILED"}
    assert inverter.DEFAULT_LOG_DIR == "/config/battery_planner"


# ---- drivers: selection, safety, logging-first ------------------------------------
# Test drivers are written to tmp_path as inverter_<type>.py, exactly where a
# contributor would drop a real one. They record into module-level lists that
# the tests read back through sys.modules["inverter_driver_<type>"].

_RECORDING = '''
import os, threading
SOC_IS_STUB = False
SENT = []
LOG_LINES_AT_SEND = []
THREADS = []
LOADS = [0]
LOADS[0] += 1
def send(action, target_power_kw):
    SENT.append((action, target_power_kw))
    THREADS.append(threading.current_thread().name)
    path = os.environ.get("TEST_DRIVER_LOG")
    LOG_LINES_AT_SEND.append(
        len(open(path).read().splitlines()) if path and os.path.exists(path) else 0)
    return True
def read_charge_percent():
    return 42.5
'''

_DRIVERS = {
    "recording": _RECORDING,
    "raising": _RECORDING.replace(
        "    SENT.append((action, target_power_kw))",
        "    SENT.append((action, target_power_kw))\n    raise OSError('bus down')"
    ).replace("    return 42.5", "    raise OSError('bus down')"),
    "refusing": _RECORDING.replace("    return True\n", "    return False\n"),
    "slow": _RECORDING.replace(
        "def send(action, target_power_kw):",
        "def send(action, target_power_kw):\n    import time; time.sleep(1.5)"
    ).replace("def read_charge_percent():",
              "def read_charge_percent():\n    import time; time.sleep(1.5)"),
    "badread": _RECORDING.replace("return 42.5", "return 'full'"),
    "overread": _RECORDING.replace("return 42.5", "return 100.5"),
    "boolread": _RECORDING.replace("return 42.5", "return True"),
    "nanread": _RECORDING.replace("return 42.5", "return float('nan')"),
    "nosend": "def read_charge_percent():\n    return 1.0\n",
    "noread": "def send(a, p):\n    return True\n",
    "crashes": "raise RuntimeError('import time failure')\n",
    "toggle": (
        "SENT = []\nMODE = ['ok']\nSOC_IS_STUB = False\n"
        "def send(action, target_power_kw):\n"
        "    SENT.append((action, target_power_kw))\n"
        "    if MODE[0] == 'raise':\n        raise OSError('bus down')\n"
        "    if MODE[0] == 'slow':\n        import time; time.sleep(1.5)\n"
        "    return MODE[0] != 'false'\n"
        "def read_charge_percent():\n    return 50.0\n"),
}


@pytest.fixture
def drivers(tmp_path, inverter, monkeypatch):
    d = tmp_path / "drivers"
    d.mkdir()
    for name, src in _DRIVERS.items():
        (d / ("inverter_%s.py" % name)).write_text(src, encoding="utf-8")
    monkeypatch.setenv("TEST_DRIVER_LOG", str(tmp_path / "decisions.log"))
    monkeypatch.setattr(inverter, "DEFAULT_DRIVER_DIR", str(d))
    yield d
    for name in list(sys.modules):
        if name.startswith("inverter_driver_"):
            del sys.modules[name]


def _driver_module(name):
    return sys.modules["inverter_driver_" + name]


def _apply(inverter, tmp_path, kind, action="export", power=2.5,
           resend_minutes=0, **rec):
    log = tmp_path / "decisions.log"
    record = make_record(action=action, target_power_kw=power, **rec)
    ok = inverter.apply(action, power, record, str(log), inverter_type=kind,
                        resend_minutes=resend_minutes,
                        state_dir=str(tmp_path / "state"))
    return ok, log, record


def test_logging_driver_transmits_nothing_and_logs(inverter, tmp_path):
    ok, log, record = _apply(inverter, tmp_path, "logging")
    assert ok is True
    assert log.read_text() == decision.format_record(record) + "\n"
    assert builtins.log.errors == [] and builtins.log.messages == []
    drv = _driver_module("logging")
    assert drv.send("discharge", 3.0) is True      # no-op, nothing to observe
    assert drv.SOC_IS_STUB is True


def test_logging_driver_stub_matches_the_fallback_constant(inverter):
    import inverter_logging
    assert inverter_logging.STUBBED_CHARGE_PERCENT == inverter.STUBBED_CHARGE_PERCENT


def test_logging_driver_has_no_imports_and_no_calls():
    text = (MODULES / "inverter_logging.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    assert [n for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))] == []
    assert [n for n in ast.walk(tree) if isinstance(n, ast.Call)] == []


def test_driver_receives_action_and_power_after_the_log_line(
        inverter, drivers, tmp_path):
    ok, log, record = _apply(inverter, tmp_path, "recording",
                             action="discharge", power=3.25)
    assert ok is True
    drv = _driver_module("recording")
    assert drv.SENT == [("discharge", 3.25)]
    assert drv.LOG_LINES_AT_SEND == [1]            # line was already on disk
    assert log.read_text() == decision.format_record(record) + "\n"
    assert builtins.log.errors == [] and builtins.log.messages == []


def test_driver_runs_off_the_calling_thread(inverter, drivers, tmp_path):
    import threading
    _apply(inverter, tmp_path, "recording")
    assert _driver_module("recording").THREADS == ["inverter-driver"]
    assert threading.current_thread().name != "inverter-driver"


def test_every_action_is_forwarded_including_idle(inverter, drivers, tmp_path):
    for action, power in (("charge", 2.0), ("export", 1.5), ("idle", 0.0)):
        _apply(inverter, tmp_path, "recording", action=action, power=power)
    assert [a for a, _ in _driver_module("recording").SENT] == [
        "charge", "export", "idle"]


@pytest.mark.parametrize("kind, expect", [
    ("raising", "bus down"),
    ("refusing", "reported failure"),
    ("missing_type", "cannot be loaded"),
    ("crashes", "import time failure"),
    ("nosend", "defines no send"),
    ("noread", "defines no read_charge_percent"),
    ("../drivers/inverter_recording", "invalid driver name"),
    ("AlphaESS", "invalid driver name"),
])
def test_log_line_survives_any_driver_failure(
        inverter, drivers, tmp_path, kind, expect):
    ok, log, record = _apply(inverter, tmp_path, kind)
    assert ok is True                               # the record was written
    assert log.read_text() == decision.format_record(record) + "\n"
    said = builtins.log.errors + builtins.log.messages
    assert len(said) == 1 and expect in said[0], said


def test_missing_driver_says_nothing_was_transmitted(
        inverter, drivers, tmp_path):
    _apply(inverter, tmp_path, "missing_type")
    assert "NOT transmitting" in builtins.log.errors[0]
    assert "missing_type" in builtins.log.errors[0]


def test_missing_driver_errors_every_call_and_never_caches_failure(
        inverter, drivers, tmp_path):
    _apply(inverter, tmp_path, "later")
    _apply(inverter, tmp_path, "later")
    assert len(builtins.log.errors) == 2
    (drivers / "inverter_later.py").write_text(_RECORDING, encoding="utf-8")
    _apply(inverter, tmp_path, "later")
    assert len(builtins.log.errors) == 2            # now it works, no restart
    assert _driver_module("later").SENT == [("export", 2.5)]


def test_driver_is_loaded_once(inverter, drivers, tmp_path):
    _apply(inverter, tmp_path, "recording")
    first = _driver_module("recording")
    _apply(inverter, tmp_path, "recording")
    inverter.read_charge("recording")
    assert _driver_module("recording") is first
    assert first.LOADS == [1] and len(first.SENT) == 2


def test_hung_driver_is_abandoned_after_the_timeout(
        inverter, drivers, tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(inverter, "DRIVER_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    ok, log, record = _apply(inverter, tmp_path, "slow")
    assert time.monotonic() - started < 1.0
    assert ok is True and len(log.read_text().splitlines()) == 1
    assert "timed out" in builtins.log.errors[0]


def test_driver_not_called_when_the_log_cannot_be_written(
        inverter, drivers, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ok = inverter.apply("export", 2.5, make_record(),
                        str(blocker / "sub" / "d.log"),
                        inverter_type="recording")
    assert ok is False
    assert "inverter_driver_recording" not in sys.modules


def test_driver_not_called_on_action_mismatch(inverter, drivers, tmp_path):
    ok = inverter.apply("charge", 2.5, make_record(),
                        str(tmp_path / "decisions.log"),
                        inverter_type="recording")
    assert ok is False
    assert "inverter_driver_recording" not in sys.modules


def test_drivers_stay_out_of_sys_path_and_bare_names(inverter, drivers, tmp_path):
    _apply(inverter, tmp_path, "recording")
    assert "inverter_recording" not in sys.modules
    assert "inverter_driver_recording" in sys.modules
    assert str(drivers) not in sys.path


# ---- read_charge through the driver --------------------------------------------

def test_read_charge_stub_marker_preserved_for_logging(inverter):
    assert inverter.read_charge("logging") == (50.0, True, None)


def test_read_charge_real_driver_is_not_a_stub(inverter, drivers):
    assert inverter.read_charge("recording") == (42.5, False, None)


def test_read_charge_missing_driver_is_stub_with_marker(inverter, drivers):
    assert inverter.read_charge("missing_type") == (
        50.0, True, "inverter_driver_unavailable")


@pytest.mark.parametrize("kind", ["raising", "badread", "overread",
                                  "boolread", "nanread"])
def test_read_charge_unusable_reading_falls_back_with_marker(
        inverter, drivers, kind):
    assert inverter.read_charge(kind) == (50.0, True, "inverter_read_failed")
    assert len(builtins.log.messages) == 1


def test_read_charge_timeout_falls_back_with_marker(inverter, drivers, monkeypatch):
    monkeypatch.setattr(inverter, "DRIVER_TIMEOUT_SECONDS", 0.2)
    assert inverter.read_charge("slow") == (50.0, True, "inverter_read_failed")
    assert "timed out" in builtins.log.messages[0]


def test_read_charge_accepts_int_and_bounds(inverter, drivers):
    (drivers / "inverter_edge.py").write_text(
        _RECORDING.replace("return 42.5", "return 100"), encoding="utf-8")
    assert inverter.read_charge("edge") == (100.0, False, None)


def test_driver_without_soc_is_stub_attribute_is_a_real_reading(inverter, drivers):
    (drivers / "inverter_plain.py").write_text(
        "def send(a, p):\n    return True\n"
        "def read_charge_percent():\n    return 33.0\n", encoding="utf-8")
    assert inverter.read_charge("plain") == (33.0, False, None)


def test_unexpected_failure_inside_the_boundary_never_reaches_the_caller(
        inverter, drivers, tmp_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("executor exploded")
    monkeypatch.setattr(inverter, "_invoke", broken)
    ok, log, record = _apply(inverter, tmp_path, "recording")
    assert ok is True
    assert log.read_text() == decision.format_record(record) + "\n"
    assert "executor exploded" in builtins.log.errors[0]


# ---- daily rotation: one file per local day ---------------------------------------

def test_log_path_for_names_the_file_by_date(inverter):
    assert (inverter.log_path_for(datetime(2026, 1, 5, 3, 0, tzinfo=TZ), "/x/y")
            == "/x/y/decisions-2026-01-05.log")
    assert (inverter.log_path_for(date(2026, 12, 31), "/x/y/")
            == "/x/y/decisions-2026-12-31.log")
    assert (inverter.log_path_for(date(2026, 9, 30))
            == "/config/battery_planner/decisions-2026-09-30.log")


def test_apply_derives_the_dated_file_from_the_record_timestamp(
        inverter, tmp_path):
    r = make_record(timestamp=datetime(2026, 9, 29, 14, 35, tzinfo=TZ))
    assert inverter.apply("export", 2.5, r, log_dir=str(tmp_path)) is True
    f = tmp_path / "decisions-2026-09-29.log"
    assert f.read_text(encoding="utf-8") == decision.format_record(r) + "\n"
    assert [p.name for p in tmp_path.iterdir()] == [f.name]


def test_midnight_boundary_is_the_records_own_local_date(inverter, tmp_path):
    before = make_record(timestamp=datetime(2026, 9, 29, 23, 59, 59, tzinfo=TZ))
    after = make_record(timestamp=datetime(2026, 9, 30, 0, 0, 0, tzinfo=TZ))
    assert inverter.apply("export", 2.5, before, log_dir=str(tmp_path))
    assert inverter.apply("export", 2.5, after, log_dir=str(tmp_path))
    assert (tmp_path / "decisions-2026-09-29.log").read_text().count("\n") == 1
    assert (tmp_path / "decisions-2026-09-30.log").read_text().count("\n") == 1


def test_date_is_local_not_utc(inverter, tmp_path):
    # 00:30 on 30 Sep in Brussels is still 29 Sep in UTC.
    r = make_record(timestamp=datetime(2026, 9, 30, 0, 30, tzinfo=TZ))
    assert inverter.apply("export", 2.5, r, log_dir=str(tmp_path))
    assert (tmp_path / "decisions-2026-09-30.log").exists()


def test_dst_change_days_keep_one_file_per_calendar_date(inverter, tmp_path):
    for day in (date(2026, 3, 29), date(2026, 10, 25)):      # spring, autumn
        for hour in (0, 1, 3, 23):
            ts = datetime(day.year, day.month, day.day, hour, 5, tzinfo=TZ)
            assert inverter.apply("export", 2.5, make_record(timestamp=ts),
                                  log_dir=str(tmp_path))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["decisions-2026-03-29.log", "decisions-2026-10-25.log"]
    assert all(p.read_text().count("\n") == 4 for p in tmp_path.iterdir())


def test_two_days_two_files_and_same_day_appends(inverter, tmp_path):
    days = (29, 29, 30)
    for d in days:
        r = make_record(timestamp=datetime(2026, 9, d, 12, 0, tzinfo=TZ))
        assert inverter.apply("export", 2.5, r, log_dir=str(tmp_path))
    assert (tmp_path / "decisions-2026-09-29.log").read_text().count("\n") == 2
    assert (tmp_path / "decisions-2026-09-30.log").read_text().count("\n") == 1


def test_explicit_log_path_wins_over_the_dated_name(inverter, tmp_path):
    explicit = tmp_path / "mine.log"
    assert inverter.apply("export", 2.5, make_record(), str(explicit),
                          log_dir=str(tmp_path / "ignored"))
    assert explicit.exists() and not (tmp_path / "ignored").exists()


def test_planner_and_guard_records_of_one_day_share_a_file(inverter, tmp_path):
    ts = datetime(2026, 9, 29, 9, 0, tzinfo=TZ)
    assert inverter.apply("export", 2.5, make_record(timestamp=ts),
                          log_dir=str(tmp_path))
    guard = make_record(timestamp=ts.replace(hour=18), source="guard",
                        selector="S0")
    assert inverter.apply("export", 2.5, guard, log_dir=str(tmp_path))
    f = tmp_path / "decisions-2026-09-29.log"
    assert [p.name for p in tmp_path.iterdir()] == [f.name]
    lines = f.read_text().splitlines()
    assert "source=planner" in lines[0] and "source=guard" in lines[1]


# ---- command de-duplication (inverter.resend_minutes, last_command.json) ----------

T_BASE = datetime(2026, 9, 29, 14, 0, tzinfo=TZ)


def _send(inverter, tmp_path, minutes=0, action="export", power=2.5,
          kind="toggle", resend=15, source="planner", state=None, seconds=0,
          dry_run=False):
    """One apply() call `minutes` after T_BASE; returns the decision-log path."""
    ts = T_BASE + timedelta(minutes=minutes, seconds=seconds)
    record = make_record(timestamp=ts, action=action, target_power_kw=power,
                         source=source)
    ok = inverter.apply(
        action, power, record, inverter_type=kind, resend_minutes=resend,
        dry_run=dry_run, log_dir=str(tmp_path / "logs"),
        state_dir=str(state if state is not None else tmp_path / "state"))
    assert ok is True
    return tmp_path / "logs" / ("decisions-%s.log" % ts.date())


def _sent():
    return _driver_module("toggle").SENT


def _state_json(tmp_path):
    return json.loads((tmp_path / "state" / "last_command.json")
                      .read_text(encoding="utf-8"))


def test_identical_command_within_the_window_is_sent_once(
        inverter, drivers, tmp_path):
    for i in range(5):
        _send(inverter, tmp_path, minutes=i)
    assert _sent() == [("export", 2.5)]


def test_the_log_line_is_written_on_every_call_even_when_not_sent(
        inverter, drivers, tmp_path):
    for i in range(5):
        log = _send(inverter, tmp_path, minutes=i)
    assert len(log.read_text().splitlines()) == 5
    assert len(_sent()) == 1


def test_changed_power_is_sent_again(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, power=2.5)
    _send(inverter, tmp_path, 1, power=2.5)
    _send(inverter, tmp_path, 2, power=1.75)
    _send(inverter, tmp_path, 3, power=1.75)
    assert _sent() == [("export", 2.5), ("export", 1.75)]


def test_changed_action_is_sent_again(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, action="idle", power=0.0)
    _send(inverter, tmp_path, 1, action="discharge", power=1.5)
    _send(inverter, tmp_path, 2, action="idle", power=0.0)
    assert [a for a, _ in _sent()] == ["idle", "discharge", "idle"]


def test_power_is_compared_rounded_to_a_hundredth_of_a_kw(
        inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, power=2.5)
    _send(inverter, tmp_path, 1, power=2.5004)      # same at 0.01 kW
    _send(inverter, tmp_path, 2, power=2.4996)
    assert len(_sent()) == 1
    _send(inverter, tmp_path, 3, power=2.52)        # a visible step
    assert len(_sent()) == 2


def test_resend_happens_once_resend_minutes_have_passed(
        inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0)
    _send(inverter, tmp_path, 14, seconds=59)
    assert len(_sent()) == 1
    _send(inverter, tmp_path, 15)                    # exactly the window
    assert len(_sent()) == 2
    _send(inverter, tmp_path, 20)                    # new window started at 15
    assert len(_sent()) == 2
    _send(inverter, tmp_path, 30)
    assert len(_sent()) == 3


def test_resend_window_is_measured_from_the_last_send_not_the_last_call(
        inverter, drivers, tmp_path):
    for m in range(0, 15):                           # calls every minute
        _send(inverter, tmp_path, m)
    assert len(_sent()) == 1
    _send(inverter, tmp_path, 15)
    assert len(_sent()) == 2


def test_resend_minutes_zero_sends_every_call(inverter, drivers, tmp_path):
    for i in range(4):
        _send(inverter, tmp_path, i, resend=0)
    assert len(_sent()) == 4


def test_custom_window(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, resend=5)
    _send(inverter, tmp_path, 4, resend=5)
    assert len(_sent()) == 1
    _send(inverter, tmp_path, 5, resend=5)
    assert len(_sent()) == 2


def test_last_command_is_persisted_with_utc_time(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, action="discharge", power=1.5)
    data = _state_json(tmp_path)
    assert data == {"action": "discharge", "power_kw": 1.5,
                    "sent_at": "2026-09-29T12:00:00+00:00"}


def test_state_survives_a_restart(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0)
    assert len(_sent()) == 1
    # Restart: a fresh module has no memory, only the file remains.
    spec = importlib.util.spec_from_file_location("inverter_restarted", SRC)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.DEFAULT_DRIVER_DIR = inverter.DEFAULT_DRIVER_DIR
    fresh._drivers = inverter._drivers               # one driver, to count sends
    assert fresh._last_sent == {}
    _send(fresh, tmp_path, 5)
    assert len(_sent()) == 1                         # not re-sent
    _send(fresh, tmp_path, 16)                       # window elapsed: re-sent
    assert _sent() == [("export", 2.5), ("export", 2.5)]


def test_planner_and_guard_share_the_state(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, source="planner")
    _send(inverter, tmp_path, 1, source="guard")
    assert len(_sent()) == 1
    _send(inverter, tmp_path, 2, source="guard", action="discharge", power=1.0)
    _send(inverter, tmp_path, 3, source="planner", action="discharge",
          power=1.0)
    assert [a for a, _ in _sent()] == ["export", "discharge"]
    _send(inverter, tmp_path, 4, source="planner")   # back to the old command
    assert len(_sent()) == 3


def test_state_is_shared_between_module_instances(inverter, drivers, tmp_path):
    # The guard and the planner may hold separate module objects: only the
    # file connects them, and it is read on every call.
    spec = importlib.util.spec_from_file_location("inverter_other", SRC)
    other = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other)
    other.DEFAULT_DRIVER_DIR = inverter.DEFAULT_DRIVER_DIR
    other._drivers = inverter._drivers               # one driver, to count sends
    _send(inverter, tmp_path, 0)
    _send(other, tmp_path, 1)
    assert len(_sent()) == 1


@pytest.mark.parametrize("mode", ["false", "raise", "slow"])
def test_a_failed_send_is_not_recorded_and_retries_next_call(
        inverter, drivers, tmp_path, monkeypatch, mode):
    monkeypatch.setattr(inverter, "DRIVER_TIMEOUT_SECONDS", 0.2)
    _send(inverter, tmp_path, 0)                     # loads the driver
    _driver_module("toggle").MODE[0] = mode
    _send(inverter, tmp_path, 15, power=3.0)         # new command, fails
    assert _sent()[-1] == ("export", 3.0)
    assert not (tmp_path / "state" / "last_command.json").exists()
    _driver_module("toggle").MODE[0] = "ok"
    _send(inverter, tmp_path, 16, power=3.0)         # retried at once
    assert _sent()[-2:] == [("export", 3.0), ("export", 3.0)]
    assert _state_json(tmp_path)["power_kw"] == 3.0
    _send(inverter, tmp_path, 17, power=3.0)         # and now de-duplicated
    assert len(_sent()) == 3


def test_failed_send_of_the_same_command_is_retried(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0)
    _driver_module("toggle").MODE[0] = "false"
    _send(inverter, tmp_path, 15)                    # resend due, refused
    _send(inverter, tmp_path, 16)                    # still due: retried
    assert len(_sent()) == 3


def test_failure_forgets_the_previous_command_too(inverter, drivers, tmp_path):
    # idle was sent; charge fails (inverter state unknown); idle again must
    # be sent rather than assumed to be in force.
    _send(inverter, tmp_path, 0, action="idle", power=0.0)
    _driver_module("toggle").MODE[0] = "raise"
    _send(inverter, tmp_path, 1, action="charge", power=2.0)
    _driver_module("toggle").MODE[0] = "ok"
    _send(inverter, tmp_path, 2, action="idle", power=0.0)
    assert [a for a, _ in _sent()] == ["idle", "charge", "idle"]


def test_missing_driver_is_not_recorded_as_sent(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, kind="later")
    assert not (tmp_path / "state" / "last_command.json").exists()
    (drivers / "inverter_later.py").write_text(_DRIVERS["toggle"],
                                               encoding="utf-8")
    _send(inverter, tmp_path, 1, kind="later")
    assert _driver_module("later").SENT == [("export", 2.5)]


@pytest.mark.parametrize("content", [
    "", "not json", "[]", "null", "{}", '{"action": 3}',
    '{"action": "export", "power_kw": "x", "sent_at": "2026-09-29T12:00:00+00:00"}',
    '{"action": "export", "power_kw": 2.5, "sent_at": "yesterday"}',
    '{"action": "export", "power_kw": 2.5, "sent_at": "2026-09-29T12:00:00"}',
    '{"action": "export", "power_kw": true, "sent_at": "2026-09-29T12:00:00+00:00"}',
    '{"action": "export", "power_kw": NaN, "sent_at": "2026-09-29T12:00:00+00:00"}',
])
def test_corrupt_state_file_means_send_and_never_crashes(
        inverter, drivers, tmp_path, content):
    state = tmp_path / "state"
    state.mkdir()
    (state / "last_command.json").write_text(content, encoding="utf-8")
    _send(inverter, tmp_path, 0)
    assert len(_sent()) == 1
    assert _state_json(tmp_path)["action"] == "export"    # healed
    _send(inverter, tmp_path, 1)
    assert len(_sent()) == 1


def test_missing_state_dir_is_created(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, state=tmp_path / "deep" / "er" / "state")
    assert (tmp_path / "deep" / "er" / "state" / "last_command.json").is_file()


def test_unwritable_state_falls_back_to_memory(inverter, drivers, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad = blocker / "state"
    for i in range(3):
        _send(inverter, tmp_path, i, state=bad)
    assert len(_sent()) == 1                         # memory still dedupes
    assert any("cannot save" in m for m in builtins.log.messages)


def test_clock_going_backwards_sends(inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 30)
    _send(inverter, tmp_path, 10)
    assert len(_sent()) == 2


def test_logging_driver_keeps_no_state(inverter, tmp_path):
    for i in range(3):
        _send(inverter, tmp_path, i, kind="logging")
    assert not (tmp_path / "state").exists()
    assert builtins.log.errors == [] and builtins.log.messages == []


def test_default_state_dir_is_below_the_log_dir(inverter, drivers, tmp_path):
    record = make_record(timestamp=T_BASE)
    assert inverter.apply("export", 2.5, record, inverter_type="toggle",
                          log_dir=str(tmp_path / "bp")) is True
    assert (tmp_path / "bp" / "state" / "last_command.json").is_file()


def test_apply_signature_is_backwards_compatible(inverter, drivers, tmp_path):
    # Old positional/keyword use keeps working: no new required arguments.
    record = make_record(timestamp=T_BASE)
    assert inverter.apply("export", 2.5, record, str(tmp_path / "d.log"),
                          "logging", None, str(tmp_path)) is True
    assert inverter.DEFAULT_RESEND_MINUTES == 15


# ---- idle: clear every command we sent, sent once ---------------------------------

def _idle(inverter, tmp_path, minutes, **kw):
    return _send(inverter, tmp_path, minutes, action="idle", power=0.0, **kw)


def _restarted(inverter):
    spec = importlib.util.spec_from_file_location("inverter_restarted", SRC)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.DEFAULT_DRIVER_DIR = inverter.DEFAULT_DRIVER_DIR
    fresh._drivers = inverter._drivers               # one driver, to count sends
    assert fresh._last_sent == {}
    return fresh


def test_repeated_idle_cycles_send_idle_once(inverter, drivers, tmp_path):
    for i in range(0, 120, 5):                       # far beyond resend_minutes
        _idle(inverter, tmp_path, i)
    assert _sent() == [("idle", 0.0)]


def test_forced_to_idle_sends_idle_immediately_then_never_again(
        inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, action="charge", power=2.0)
    _send(inverter, tmp_path, 1, action="charge", power=2.0)
    _idle(inverter, tmp_path, 2)                     # one minute later
    assert _sent() == [("charge", 2.0), ("idle", 0.0)]
    for i in range(3, 60):
        _idle(inverter, tmp_path, i)
    assert _sent() == [("charge", 2.0), ("idle", 0.0)]


def test_each_forced_mode_is_released_by_one_idle(inverter, drivers, tmp_path):
    for n, action in enumerate(("charge", "export", "discharge")):
        _send(inverter, tmp_path, 4 * n, action=action, power=1.5)
        _idle(inverter, tmp_path, 4 * n + 1)
        _idle(inverter, tmp_path, 4 * n + 2)
    assert [a for a, _ in _sent()] == [
        "charge", "idle", "export", "idle", "discharge", "idle"]


def test_first_idle_with_no_command_on_record_is_sent_once(
        inverter, drivers, tmp_path):
    _idle(inverter, tmp_path, 0)
    _idle(inverter, tmp_path, 5)
    assert _sent() == [("idle", 0.0)]


def test_idle_record_survives_a_restart_and_is_not_resent(
        inverter, drivers, tmp_path):
    _idle(inverter, tmp_path, 0)
    fresh = _restarted(inverter)
    _idle(fresh, tmp_path, 60)                       # hours later, same file
    assert _sent() == [("idle", 0.0)]


def test_forced_record_survives_a_restart_and_idle_then_releases_it(
        inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, action="charge", power=2.0)
    fresh = _restarted(inverter)
    _idle(fresh, tmp_path, 5)
    _idle(fresh, tmp_path, 10)
    assert _sent() == [("charge", 2.0), ("idle", 0.0)]


def test_first_idle_after_a_restart_without_a_state_file_is_sent_once(
        inverter, drivers, tmp_path):
    fresh = _restarted(inverter)
    _idle(fresh, tmp_path, 0)
    _idle(fresh, tmp_path, 5)
    assert _sent() == [("idle", 0.0)]


@pytest.mark.parametrize("mode", ["false", "raise", "slow"])
def test_failed_idle_send_is_not_recorded_and_is_retried(
        inverter, drivers, tmp_path, monkeypatch, mode):
    monkeypatch.setattr(inverter, "DRIVER_TIMEOUT_SECONDS", 0.2)
    _send(inverter, tmp_path, 0, action="charge", power=2.0)
    _driver_module("toggle").MODE[0] = mode
    _idle(inverter, tmp_path, 1)                     # fails
    assert not (tmp_path / "state" / "last_command.json").exists()
    _idle(inverter, tmp_path, 2)                     # tried again, fails again
    _driver_module("toggle").MODE[0] = "ok"
    _idle(inverter, tmp_path, 3)                     # accepted
    assert _state_json(tmp_path)["action"] == "idle"
    _idle(inverter, tmp_path, 4)                     # now de-duplicated
    assert [a for a, _ in _sent()] == ["charge", "idle", "idle", "idle"]


def test_idle_after_idle_is_sent_every_call_when_resend_minutes_is_zero(
        inverter, drivers, tmp_path):
    for i in range(3):
        _idle(inverter, tmp_path, i, resend=0)
    assert len(_sent()) == 3


def test_idle_log_line_is_still_written_every_call(inverter, drivers, tmp_path):
    for i in range(4):
        log = _idle(inverter, tmp_path, i)
    assert len(log.read_text().splitlines()) == 4
    assert len(_sent()) == 1


def test_non_idle_commands_still_resend_after_the_window(
        inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, action="charge", power=2.0)
    _send(inverter, tmp_path, 15, action="charge", power=2.0)
    assert len(_sent()) == 2


# ---- COMMAND_HOLD_MINUTES: refresh a forced command before the inverter drops it --

def _hold_driver(drivers, name, declaration):
    """A recording driver that declares (or not) COMMAND_HOLD_MINUTES."""
    src = _DRIVERS["toggle"]
    if declaration is not None:
        src += "COMMAND_HOLD_MINUTES = %s\n" % declaration
    (drivers / ("inverter_%s.py" % name)).write_text(src, encoding="utf-8")
    return name


def _sent_by(name):
    return _driver_module(name).SENT


def test_hold_10_minutes_resends_at_eight_minutes_and_not_before(
        inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold10", "10")
    _send(inverter, tmp_path, 0, kind=kind)
    _send(inverter, tmp_path, 7, seconds=59, kind=kind)
    assert len(_sent_by(kind)) == 1
    _send(inverter, tmp_path, 8, kind=kind)          # exactly 0.8 x 10
    assert len(_sent_by(kind)) == 2
    _send(inverter, tmp_path, 15, kind=kind)         # new window began at 8
    assert len(_sent_by(kind)) == 2
    _send(inverter, tmp_path, 16, kind=kind)
    assert len(_sent_by(kind)) == 3


def test_declared_hold_overrides_resend_minutes(inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold10", "10")
    for resend in (60, 15, 0):                        # 0 would mean every call
        tag = tmp_path / ("s%d" % resend)
        _send(inverter, tmp_path, 0, kind=kind, resend=resend, state=tag)
        _send(inverter, tmp_path, 4, kind=kind, resend=resend, state=tag)
        _send(inverter, tmp_path, 8, kind=kind, resend=resend, state=tag)
    # per state dir: sent at 0, not at 4, again at 8 = 2 sends; three dirs
    assert len(_sent_by(kind)) == 6


def test_hold_may_be_fractional_and_changes_the_window(
        inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold25", "2.5")     # window 2 minutes
    _send(inverter, tmp_path, 0, kind=kind)
    _send(inverter, tmp_path, 1, kind=kind)
    assert len(_sent_by(kind)) == 1
    _send(inverter, tmp_path, 2, kind=kind)
    assert len(_sent_by(kind)) == 2


@pytest.mark.parametrize("declaration", [None, "None"])
def test_no_declaration_falls_back_to_resend_minutes(
        inverter, drivers, tmp_path, declaration):
    kind = _hold_driver(drivers, "nohold", declaration)
    _send(inverter, tmp_path, 0, kind=kind, resend=5)
    _send(inverter, tmp_path, 4, kind=kind, resend=5)
    assert len(_sent_by(kind)) == 1
    _send(inverter, tmp_path, 5, kind=kind, resend=5)
    assert len(_sent_by(kind)) == 2
    assert builtins.log.messages == []                # unknown is not an error


def test_fallback_zero_still_sends_every_call(inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "nohold", None)
    for i in range(3):
        _send(inverter, tmp_path, i, kind=kind, resend=0)
    assert len(_sent_by(kind)) == 3


@pytest.mark.parametrize("declaration", [
    "0", "-5", "'10'", "True", "float('nan')", "float('inf')", "[10]", "1e12",
])
def test_invalid_hold_is_ignored_with_one_warning(
        inverter, drivers, tmp_path, declaration):
    kind = _hold_driver(drivers, "badhold", declaration)
    for i in range(0, 16):
        _send(inverter, tmp_path, i, kind=kind, resend=15)
    # fell back to resend_minutes = 15: sent at 0 and again at 15
    assert len(_sent_by(kind)) == 2
    warned = [m for m in builtins.log.messages if "COMMAND_HOLD_MINUTES" in m]
    assert len(warned) == 1 and "badhold" in warned[0]


def _plant_record(tmp_path, minutes_before, action="export", power=2.5):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    when = (T_BASE - timedelta(minutes=minutes_before)).astimezone(
        ZoneInfo("UTC"))
    (state / "last_command.json").write_text(json.dumps({
        "action": action, "power_kw": power, "sent_at": when.isoformat()}),
        encoding="utf-8")


def test_expired_record_after_a_restart_does_not_block_the_send(
        inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold10", "10")
    _plant_record(tmp_path, minutes_before=180)       # left by an old run
    fresh = _restarted(inverter)
    _send(fresh, tmp_path, 0, kind=kind)
    assert len(_sent_by(kind)) == 1


def test_expired_record_after_a_restart_with_fallback_window(
        inverter, drivers, tmp_path):
    _plant_record(tmp_path, minutes_before=30)
    fresh = _restarted(inverter)
    _send(fresh, tmp_path, 0, resend=15)              # toggle declares nothing
    assert len(_sent()) == 1


def test_fresh_record_after_a_restart_still_blocks_a_duplicate(
        inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold10", "10")
    _plant_record(tmp_path, minutes_before=3)         # younger than 8 minutes
    fresh = _restarted(inverter)
    _send(fresh, tmp_path, 0, kind=kind)
    assert _sent_by(kind) == []
    _send(fresh, tmp_path, 5, kind=kind)              # now 8 minutes old
    assert len(_sent_by(kind)) == 1


def test_identical_commands_inside_the_hold_window_are_not_resent(
        inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold10", "10")
    for i in range(8):                                # one a minute, 0..7
        _send(inverter, tmp_path, i, kind=kind)
    assert len(_sent_by(kind)) == 1


def test_hold_does_not_make_idle_repeat(inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold10", "10")
    for i in range(0, 60, 5):
        _idle(inverter, tmp_path, i, kind=kind)
    assert _sent_by(kind) == [("idle", 0.0)]


def test_a_changed_command_is_sent_at_once_whatever_the_hold(
        inverter, drivers, tmp_path):
    kind = _hold_driver(drivers, "hold10", "10")
    _send(inverter, tmp_path, 0, kind=kind, power=2.0)
    _send(inverter, tmp_path, 1, kind=kind, power=3.0)
    assert _sent_by(kind) == [("export", 2.0), ("export", 3.0)]


# ---- plan-style drivers: service calls executed by the boundary -------------------

_PLAN_BASE = '''
SOC_ENTITY = "sensor.my_soc"
CALLS = []
SEND = []
def send(action, target_power_kw):
    SEND.append((action, target_power_kw))
    return True
def plan(action, target_power_kw):
    CALLS.append((action, target_power_kw))
    if action == "idle":
        return [{"domain": "input_boolean", "service": "turn_off",
                 "data": {"entity_id": "input_boolean.my_force_charge"}}]
    return [
        {"domain": "input_number", "service": "set_value",
         "data": {"entity_id": "input_number.my_power", "value": target_power_kw}},
        {"domain": "input_boolean", "service": "turn_on",
         "data": {"entity_id": "input_boolean.my_force_charge"}},
    ]
'''


def _plan_driver(drivers, name="planner1", source=None):
    (drivers / ("inverter_%s.py" % name)).write_text(
        source if source is not None else _PLAN_BASE, encoding="utf-8")
    return name


def _service_calls():
    return builtins.service.calls


def _plan_send(inverter, tmp_path, minutes=0, kind="planner1", **kw):
    return _send(inverter, tmp_path, minutes, kind=kind, action=kw.pop("action", "charge"),
                 power=kw.pop("power", 2.0), **kw)


def test_plan_calls_run_in_order_through_service_call(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    _plan_send(inverter, tmp_path, kind=kind)
    assert _service_calls() == [
        ("input_number", "set_value",
         {"entity_id": "input_number.my_power", "value": 2.0}),
        ("input_boolean", "turn_on",
         {"entity_id": "input_boolean.my_force_charge"}),
    ]
    assert _state_json(tmp_path)["action"] == "charge"
    assert builtins.log.errors == []


def test_send_is_not_used_when_plan_exists(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    _plan_send(inverter, tmp_path, kind=kind)
    assert _driver_module(kind).SEND == []


def test_the_log_line_comes_first_and_is_unchanged_by_plan_failure(
        inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    builtins.service.fail_at = 0
    log = _plan_send(inverter, tmp_path, kind=kind)
    assert len(log.read_text().splitlines()) == 1
    assert "action=charge" in log.read_text()


def test_plan_runs_only_when_apply_decides_to_send(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers, source=_PLAN_BASE + "COMMAND_HOLD_MINUTES = 10\n")
    for i in range(0, 8):                             # 0..7 minutes: unchanged
        _plan_send(inverter, tmp_path, i, kind=kind)
    assert _driver_module(kind).CALLS == [("charge", 2.0)]
    assert len(_service_calls()) == 2
    _plan_send(inverter, tmp_path, 8, kind=kind)       # 0.8 x hold
    assert len(_driver_module(kind).CALLS) == 2
    assert len(_service_calls()) == 4


def test_plan_resends_by_the_fallback_when_no_hold_is_declared(
        inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    _plan_send(inverter, tmp_path, 0, kind=kind)
    _plan_send(inverter, tmp_path, 14, kind=kind)
    assert len(_driver_module(kind).CALLS) == 1
    _plan_send(inverter, tmp_path, 15, kind=kind)
    assert len(_driver_module(kind).CALLS) == 2


def test_idle_plan_runs_on_the_transition_and_once(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    _plan_send(inverter, tmp_path, 0, kind=kind)
    _plan_send(inverter, tmp_path, 1, kind=kind, action="idle", power=0.0)
    _plan_send(inverter, tmp_path, 2, kind=kind, action="idle", power=0.0)
    _plan_send(inverter, tmp_path, 40, kind=kind, action="idle", power=0.0)
    assert _driver_module(kind).CALLS == [("charge", 2.0), ("idle", 0.0)]
    assert _service_calls()[-1] == (
        "input_boolean", "turn_off",
        {"entity_id": "input_boolean.my_force_charge"})
    assert len(_service_calls()) == 3


def test_failure_mid_sequence_stops_is_not_recorded_and_retries(
        inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    _plan_send(inverter, tmp_path, 0, kind=kind, power=1.0)       # ok, recorded
    builtins.service.fail_at = 1                      # second call of the next plan
    del builtins.service.calls[:]
    _plan_send(inverter, tmp_path, 1, kind=kind, power=3.0)
    assert len(_service_calls()) == 2                 # stopped at the failing one
    assert not (tmp_path / "state" / "last_command.json").exists()
    assert any("stopped at call 2 of 2" in m for m in builtins.log.errors)
    builtins.service.fail_at = None
    del builtins.service.calls[:]
    _plan_send(inverter, tmp_path, 2, kind=kind, power=3.0)       # retried at once
    assert len(_service_calls()) == 2
    assert _state_json(tmp_path)["power_kw"] == 3.0


def test_service_not_found_counts_as_a_failed_send(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    builtins.service.fail_at = 0
    builtins.service.error = KeyError("ServiceNotFound")
    assert _plan_send(inverter, tmp_path, kind=kind)
    assert not (tmp_path / "state" / "last_command.json").exists()


def test_repeated_failures_are_logged_once_per_window(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    builtins.service.fail_service = "set_value"
    for i in range(5):                                # every minute, all failing
        _plan_send(inverter, tmp_path, i, kind=kind)
    assert len(builtins.log.errors) == 1
    _plan_send(inverter, tmp_path, 31, kind=kind)
    assert len(builtins.log.errors) == 2


def test_dry_run_executes_nothing_logs_the_plan_and_records_the_command(
        inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    _plan_send(inverter, tmp_path, 0, kind=kind, dry_run=True)
    assert _service_calls() == []
    assert len(builtins.log.infos) == 1
    info = builtins.log.infos[0]
    assert "dry run" in info and "input_boolean.turn_on" in info
    assert "input_number.set_value" in info
    assert _state_json(tmp_path)["action"] == "charge"        # recorded as sent
    assert builtins.log.errors == []


def test_dry_run_dedupes_and_resends_like_a_live_run(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers)
    for i in range(14):
        _plan_send(inverter, tmp_path, i, kind=kind, dry_run=True)
    assert len(builtins.log.infos) == 1
    _plan_send(inverter, tmp_path, 15, kind=kind, dry_run=True)
    assert len(builtins.log.infos) == 2
    assert _service_calls() == []


def test_dry_run_with_a_send_style_driver_sends_nothing(
        inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0, dry_run=True)
    assert _sent() == []
    assert any("would be sent" in m for m in builtins.log.infos)
    assert _state_json(tmp_path)["action"] == "export"


def test_send_style_driver_is_unaffected_by_the_plan_machinery(
        inverter, drivers, tmp_path):
    _send(inverter, tmp_path, 0)
    assert _sent() == [("export", 2.5)]
    assert _service_calls() == []
    assert builtins.log.infos == []


_GOOD_CALL = {"domain": "input_boolean", "service": "turn_on",
              "data": {"entity_id": "input_boolean.my_x"}}


@pytest.mark.parametrize("plan", [
    "None", "'turn_on'", "{'domain': 'a', 'service': 'b', 'data': {}}",
    "[1]", "['x']", "[{'service': 'b', 'data': {}}]",
    "[{'domain': 'a', 'data': {}}]",
    "[{'domain': 'A', 'service': 'b', 'data': {}}]",
    "[{'domain': 'a-b', 'service': 'b', 'data': {}}]",
    "[{'domain': 'a', 'service': 'b.c', 'data': {}}]",
    "[{'domain': 'a', 'service': '', 'data': {}}]",
    "[{'domain': 1, 'service': 'b', 'data': {}}]",
    "[{'domain': 'a', 'service': 'b', 'data': []}]",
    "[{'domain': 'a', 'service': 'b', 'data': None}]",
    "[{'domain': 'a', 'service': 'b', 'data': {1: 2}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'k': {'n': 1}}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'k': None}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'k': [[1]]}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'k': object()}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'k': float('nan')}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'k': float('inf')}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'k': [1, float('nan')]}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'blocking': True}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {'return_response': True}}]",
    "[{'domain': 'a', 'service': 'b', 'data': {}, 'extra': 1}]",
    "[{'domain': 'a', 'service': 'b', 'data': {}}] * 13",
])
def test_invalid_plans_execute_nothing_and_count_as_failed(
        inverter, drivers, tmp_path, plan):
    kind = _plan_driver(drivers, source=(
        "SOC_ENTITY = 'sensor.my_soc'\ndef plan(a, p):\n    return %s\n" % plan))
    log = _plan_send(inverter, tmp_path, kind=kind)
    assert _service_calls() == []
    assert not (tmp_path / "state" / "last_command.json").exists()
    assert len(builtins.log.errors) == 1 and "invalid" in builtins.log.errors[0]
    assert len(log.read_text().splitlines()) == 1             # the line stays


@pytest.mark.parametrize("plan,count", [
    ("[]", 0),
    ("[{'domain': 'a', 'service': 'b'}]", 1),                 # data is optional
    ("[{'domain': 'a1_x', 'service': 'b_2', 'data': {'k': [1, 2.5, 'x', True]}}]", 1),
    ("[{'domain': 'a', 'service': 'b', 'data': {'k': 'v'}}] * 12", 12),
    ("({'domain': 'a', 'service': 'b', 'data': {'k': False}},)", 1),
])
def test_valid_plans_run(inverter, drivers, tmp_path, plan, count):
    kind = _plan_driver(drivers, source=(
        "SOC_ENTITY = 'sensor.my_soc'\ndef plan(a, p):\n    return %s\n" % plan))
    _plan_send(inverter, tmp_path, kind=kind)
    assert len(_service_calls()) == count
    assert builtins.log.errors == []
    assert _state_json(tmp_path)["action"] == "charge"


def test_plan_that_raises_is_a_failed_send(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers, source=(
        "SOC_ENTITY = 'sensor.my_soc'\ndef plan(a, p):\n    raise ValueError('bad mapping')\n"))
    _plan_send(inverter, tmp_path, kind=kind)
    assert _service_calls() == []
    assert not (tmp_path / "state" / "last_command.json").exists()
    assert any("bad mapping" in m for m in builtins.log.errors)


def test_plan_that_hangs_times_out(inverter, drivers, tmp_path, monkeypatch):
    monkeypatch.setattr(inverter, "DRIVER_TIMEOUT_SECONDS", 0.2)
    kind = _plan_driver(drivers, source=(
        "SOC_ENTITY = 'sensor.my_soc'\nimport time\n"
        "def plan(a, p):\n    time.sleep(1.5)\n    return []\n"))
    _plan_send(inverter, tmp_path, kind=kind)
    assert _service_calls() == []
    assert any("timed out" in m for m in builtins.log.errors)
    assert not (tmp_path / "state" / "last_command.json").exists()


def test_a_driver_with_neither_send_nor_plan_is_unusable(
        inverter, drivers, tmp_path):
    kind = _plan_driver(drivers, source="SOC_ENTITY = 'sensor.my_soc'\n")
    _plan_send(inverter, tmp_path, kind=kind)
    assert any("send() or plan()" in m for m in builtins.log.errors)
    assert _service_calls() == []


def test_the_calls_are_copies_not_the_drivers_own_objects(
        inverter, drivers, tmp_path):
    kind = _plan_driver(drivers, source=_PLAN_BASE)
    _plan_send(inverter, tmp_path, kind=kind)
    first = _service_calls()[0][2]
    first["value"] = 99.0                             # caller mutates its copy
    _plan_send(inverter, tmp_path, 20, kind=kind)
    assert _service_calls()[2][2]["value"] == 2.0


# ---- SOC_ENTITY ----------------------------------------------------------------------

def _soc(inverter, drivers, value, source=_PLAN_BASE, entity="sensor.my_soc"):
    kind = _plan_driver(drivers, source=source)
    if value is not None:
        builtins.state.values[entity] = value
    return inverter.read_charge(kind, str(drivers))


@pytest.mark.parametrize("raw,expected", [
    ("63.5", 63.5), ("0", 0.0), ("100", 100.0), (42, 42.0), ("  7.25 ", 7.25)])
def test_soc_entity_good_value(inverter, drivers, raw, expected):
    assert _soc(inverter, drivers, raw) == (expected, False, None)
    assert builtins.log.messages == []


@pytest.mark.parametrize("raw", [
    "unavailable", "unknown", "abc", "150", "-1", "100.5", "nan", "inf", "", None])
def test_soc_entity_bad_value_gives_placeholder_and_marker(
        inverter, drivers, raw):
    percent, is_stub, marker = _soc(inverter, drivers, raw)
    assert (percent, is_stub, marker) == (50.0, True, "inverter_read_failed")
    assert builtins.log.messages          # said why


def test_soc_entity_wins_over_a_reading_function_and_over_the_stub_flag(
        inverter, drivers):
    src = _PLAN_BASE + "SOC_IS_STUB = True\ndef read_charge_percent():\n    return 11.0\n"
    assert _soc(inverter, drivers, "80", source=src) == (80.0, False, None)


@pytest.mark.parametrize("entity", [
    "'Sensor.X'", "'nodot'", "'a.b.c'", "5", "''", "['sensor.x']"])
def test_invalid_soc_entity_makes_the_driver_unusable(inverter, drivers, entity):
    src = _PLAN_BASE.replace('SOC_ENTITY = "sensor.my_soc"',
                             "SOC_ENTITY = %s" % entity)
    percent, is_stub, marker = inverter.read_charge(
        _plan_driver(drivers, source=src), str(drivers))
    assert (percent, is_stub, marker) == (50.0, True, "inverter_driver_unavailable")


def test_plan_driver_may_omit_read_charge_percent_only_with_soc_entity(
        inverter, drivers):
    src = "def plan(a, p):\n    return []\n"
    percent, is_stub, marker = inverter.read_charge(
        _plan_driver(drivers, source=src), str(drivers))
    assert marker == "inverter_driver_unavailable"


def test_send_style_read_charge_is_unchanged(inverter, drivers):
    assert inverter.read_charge("recording", str(drivers)) == (42.5, False, None)


def test_list_values_in_the_calls_are_copies_too(inverter, drivers, tmp_path):
    kind = _plan_driver(drivers, source=(
        "SOC_ENTITY = 'sensor.my_soc'\nSHARED = ['a', 'b']\n"
        "def plan(a, p):\n    return [{'domain': 'x', 'service': 'y', "
        "'data': {'names': SHARED}}]\n"))
    _plan_send(inverter, tmp_path, kind=kind)
    sent_list = _service_calls()[0][2]["names"]
    assert sent_list == ["a", "b"]
    assert sent_list is not _driver_module(kind).SHARED


# ---- last_sent_action -----------------------------------------------------------------

def test_last_sent_action_reads_the_shared_file(inverter, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    assert inverter.last_sent_action(state_dir=str(state)) is None
    (state / "last_command.json").write_text(
        '{"action": "discharge", "power_kw": 1.5, '
        '"sent_at": "2026-09-30T12:00:00+00:00"}', encoding="utf-8")
    assert inverter.last_sent_action(state_dir=str(state)) == "discharge"


@pytest.mark.parametrize("content", ["", "junk", "[]", '{"action": 3}'])
def test_last_sent_action_never_raises(inverter, tmp_path, content):
    state = tmp_path / "state"
    state.mkdir()
    (state / "last_command.json").write_text(content, encoding="utf-8")
    assert inverter.last_sent_action(state_dir=str(state)) is None


# ---- L6: two writers of last_command.json ----------------------------------------------

def test_concurrent_writers_never_clobber_each_other(inverter, tmp_path):
    import json
    import threading

    path = str(tmp_path / "state" / "last_command.json")
    errors = []

    def writer(action):
        for i in range(150):
            err = inverter._write_state(path, {
                "action": action, "power_kw": float(i),
                "sent_at": "2026-09-30T12:00:00+00:00"})
            if err:
                errors.append(err)

    threads = [threading.Thread(target=writer, args=(a,))
               for a in ("discharge", "idle", "charge", "export")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["action"] in {"discharge", "idle", "charge", "export"}
    assert [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")] == []


def test_a_failed_write_leaves_no_temp_file(inverter, tmp_path, monkeypatch):
    path = str(tmp_path / "state" / "last_command.json")

    def broken(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(inverter.os, "replace", broken)
    err = inverter._write_state(path, {"action": "idle"})
    assert "disk says no" in err
    assert [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")] == []
