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

import pytest

import decision

SRC = Path(__file__).resolve().parent.parent / "pyscript" / "modules" / "inverter.py"
TZ = ZoneInfo("Europe/Brussels")

MODULES = SRC.parent
ALLOWED_IMPORTS = {"os", "decision", "importlib.util", "re", "sys",
                   "threading", "datetime", "json"}
ALLOWED_CALL_NAMES = {"open", "abs", "_append_line", "isinstance", "callable",
                      "getattr", "bool", "float", "ValueError", "AttributeError",
                      "FileNotFoundError", "repr", "_load_driver", "_invoke",
                      "_driver", "_transmit", "run", "fn", "log_path_for",
                      "_read_state", "_write_state", "_remove_state",
                      "_state_file", "_utc", "_last_command", "_should_send",
                      "_remember_sent", "_forget_sent", "round"}
ALLOWED_CALL_ATTRS = {"write", "flush", "fileno", "fsync", "makedirs",
                      "dirname", "format_record", "warning", "error", "get",
                      "isfile", "join", "fullmatch", "spec_from_file_location",
                      "module_from_spec", "exec_module", "pop", "append",
                      "Thread", "start", "rstrip", "load", "dumps",
                      "replace", "remove", "astimezone", "fromisoformat",
                      "total_seconds", "isoformat"}


class FakeLog:
    """Stand-in for pyscript's global `log`."""

    def __init__(self):
        self.messages = []
        self.errors = []

    def warning(self, msg, *args):
        self.messages.append(msg % args if args else msg)

    def error(self, msg, *args):
        self.errors.append(msg % args if args else msg)


_INJECTED = ("pyscript_executor", "log")


@pytest.fixture
def inverter():
    saved = {n: getattr(builtins, n) for n in _INJECTED if hasattr(builtins, n)}
    builtins.pyscript_executor = lambda fn: fn
    builtins.log = FakeLog()
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


def test_public_surface_is_exactly_three_functions_plus_constants(inverter):
    public = {n for n in vars(inverter) if not n.startswith("_")}
    funcs = {n for n in public
             if callable(getattr(inverter, n))
             and getattr(getattr(inverter, n), "__module__", None)
             == inverter.__name__}
    assert funcs == {"apply", "read_charge", "log_path_for"}
    assert public == {"os", "decision", "datetime", "apply", "read_charge",
                      "DEFAULT_RESEND_MINUTES", "LAST_COMMAND_FILE",
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
          kind="toggle", resend=15, source="planner", state=None, seconds=0):
    """One apply() call `minutes` after T_BASE; returns the decision-log path."""
    ts = T_BASE + timedelta(minutes=minutes, seconds=seconds)
    record = make_record(timestamp=ts, action=action, target_power_kw=power,
                         source=source)
    ok = inverter.apply(
        action, power, record, inverter_type=kind, resend_minutes=resend,
        log_dir=str(tmp_path / "logs"),
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
    assert fresh._last_sent == {}
    _send(fresh, tmp_path, 5)
    assert len(_sent()) == 1                         # not re-sent
    before = _sent()
    _send(fresh, tmp_path, 16)                       # window elapsed: re-sent
    assert _sent() is not before                     # (by the freshly loaded driver)
    assert _sent() == [("export", 2.5)]


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
    # the restarted module loads its own copy of the driver: only idle, once
    assert _sent() == [("idle", 0.0)]


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
