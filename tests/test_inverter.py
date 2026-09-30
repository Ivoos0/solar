"""Tests for pyscript/modules/inverter.py.

Documented extension beyond the WP text: inverter.py is a pyscript module, but it
is thin enough to load here by injecting stand-ins for the names pyscript
provides at runtime: an identity `pyscript_executor` decorator and a `log`
object that collects warnings.
"""
import ast
import builtins
import importlib.util
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import decision

SRC = Path(__file__).resolve().parent.parent / "pyscript" / "modules" / "inverter.py"
TZ = ZoneInfo("Europe/Brussels")

ALLOWED_IMPORTS = {"os", "decision"}
ALLOWED_CALL_NAMES = {"open", "abs", "_append_line"}
ALLOWED_CALL_ATTRS = {"write", "flush", "fileno", "fsync", "makedirs",
                      "dirname", "format_record", "warning"}


class FakeLog:
    """Stand-in for pyscript's global `log`."""

    def __init__(self):
        self.messages = []

    def warning(self, msg, *args):
        self.messages.append(msg % args if args else msg)


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


def test_read_charge_percent_is_stub_constant(inverter):
    v = inverter.read_charge_percent()
    assert isinstance(v, float) and 0.0 <= v <= 100.0
    assert v == inverter.STUBBED_CHARGE_PERCENT == 50.0


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
    r = make_record(degraded_inputs=["soc\nstubbed"])
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
                assert node.args[1].value == "a"  # append mode only
    assert owners == {"_append_line"}
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


def test_public_surface_is_exactly_two_functions_plus_constants(inverter):
    public = {n for n in vars(inverter) if not n.startswith("_")}
    funcs = {n for n in public
             if callable(getattr(inverter, n))
             and getattr(getattr(inverter, n), "__module__", None)
             == inverter.__name__}
    assert funcs == {"apply", "read_charge_percent"}
    assert public == {"os", "decision", "apply", "read_charge_percent",
                      "DEFAULT_LOG_PATH", "STUBBED_CHARGE_PERCENT",
                      "POWER_TOLERANCE_KW"}
    assert inverter.DEFAULT_LOG_PATH == "/config/battery_planner/decisions.log"
