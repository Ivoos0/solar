"""Tests for pyscript/housekeeping.py (daily report job).

Loaded with stand-ins for the names pyscript provides at runtime, like
test_battery_planner does. Paths are redirected to tmp_path.
"""
import ast
import builtins
import importlib.util
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alphaess_support import bump_mtime

import decision
import history

SRC = Path(__file__).resolve().parent.parent / "pyscript" / "housekeeping.py"
MODULES = SRC.parent / "modules"
BRU = ZoneInfo("Europe/Brussels")
UTC = timezone.utc
# 00:10 local on 2026-09-30; "yesterday" is 2026-09-29
NOW = datetime(2026, 9, 30, 0, 10, tzinfo=BRU)
YESTERDAY = date(2026, 9, 29)
_CORE_NAMES = ("config", "history", "report", "retention")
_INJECTED = ("pyscript_executor", "time_trigger", "log")


class FakeLog:
    def __init__(self):
        self.by_level = {"info": [], "warning": [], "error": []}

    def info(self, msg, *a):
        self.by_level["info"].append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.by_level["warning"].append(msg % a if a else msg)

    def error(self, msg, *a):
        self.by_level["error"].append(msg % a if a else msg)


class Env:
    def __init__(self, mod, tmp):
        self.mod = mod
        self.tmp = tmp
        self.base = tmp / "battery_planner"
        self.hist = self.base / "history"
        self.log = builtins.log
        self.config_path = self.base / "user_config.yaml"

    def write_config(self, extra=""):
        self.base.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            "battery:\n  capacity_kwh: 10.0\nalerts:\n"
            "  address: owner@example.com\n" + extra, encoding="utf-8")
        bump_mtime(self.config_path)

    def decisions(self, day, text="x"):
        self.base.mkdir(parents=True, exist_ok=True)
        path = self.base / ("decisions-%s.log" % day.isoformat())
        path.write_text(text, encoding="utf-8")
        return path

    def blocks(self, day, text="x"):
        self.hist.mkdir(parents=True, exist_ok=True)
        path = self.hist / ("blocks-%s.jsonl" % day.isoformat())
        path.write_text(text, encoding="utf-8")
        return path

    def report(self, day):
        return self.hist / ("report-%s.md" % day.isoformat())


def decision_line(when):
    return decision.format_record(decision.DecisionRecord(
        timestamp=when, action="idle", target_power_kw=0.0,
        charge_percent=50.0, charge_kwh=5.0, consumption_price=0.2,
        injection_price=0.1, forecast_remaining_kwh=3.0,
        usage_remaining_kwh=4.0, saturation_block=None, spill_kwh=0.0,
        reserve_breach_block=None, projected_end_charge_kwh=2.0,
        duration_ms=80, running_average_kw=1.2, ceiling_kw=2.5,
        budget_kw=1.0, vetoes_applied=[], selector="S6", reasoning="hold",
        degraded_inputs=[], source="planner"))


def block_line(start_local, imp=0.5):
    rec = dict.fromkeys(history.RECORD_FIELDS)
    rec.update(schema=1, block_start=start_local.astimezone(UTC).isoformat(),
               local_date=start_local.date().isoformat(), block_minutes=15,
               import_kwh=imp, complete=True)
    return history.to_line(rec)


def charge_block_line(start_local, soc, soc_end):
    rec = dict.fromkeys(history.RECORD_FIELDS)
    rec.update(schema=1, block_start=start_local.astimezone(UTC).isoformat(),
               local_date=start_local.date().isoformat(), block_minutes=15,
               battery_charge_kwh=2.0, battery_discharge_kwh=0.5,
               soc_percent=soc, soc_end_percent=soc_end, complete=True)
    return history.to_line(rec)


def day_files(env, day, imp=0.5):
    at = datetime(day.year, day.month, day.day, 12, tzinfo=BRU)
    env.decisions(day, decision_line(at) + "\n")
    env.blocks(day, block_line(at, imp) + "\n")


@pytest.fixture
def env(tmp_path):
    saved = {n: getattr(builtins, n) for n in _INJECTED if hasattr(builtins, n)}
    saved_bare = {n: sys.modules.get(n) for n in _CORE_NAMES}
    builtins.pyscript_executor = lambda fn: fn
    builtins.time_trigger = lambda *a, **k: (lambda fn: fn)
    builtins.log = FakeLog()
    try:
        spec = importlib.util.spec_from_file_location(
            "housekeeping_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        e = Env(mod, tmp_path)
        mod.CORE_DIR = str(MODULES)
        mod.CONFIG_PATH = str(e.config_path)
        mod.LOG_DIR = str(e.base) + "/"
        mod.HISTORY_DIR = str(e.hist) + "/"
        e.write_config()
        yield e
    finally:
        for n in _INJECTED:
            if n in saved:
                setattr(builtins, n, saved[n])
            elif hasattr(builtins, n):
                delattr(builtins, n)
        for n in list(sys.modules):
            if n.startswith("housekeeping_core_"):
                del sys.modules[n]
        for n, previous in saved_bare.items():      # loader aliases are permanent
            if previous is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = previous


# ---- report job --------------------------------------------------------------

def test_writes_yesterdays_report(env):
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    text = env.report(YESTERDAY).read_text(encoding="utf-8")
    assert text.startswith("# Battery planner report for 2026-09-29")
    assert "- Records: 1" in text
    assert "| Imported from the grid | 0.50 | 1 |" in text
    assert env.log.by_level["error"] == [] and env.log.by_level["warning"] == []
    assert env.log.by_level["info"] == [
        "housekeeping: wrote daily report for 2026-09-29"]


def test_report_lives_next_to_the_history_and_leaves_no_temp_file(env):
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    assert sorted(p.name for p in env.hist.iterdir()) == [
        "blocks-2026-09-29.jsonl", "report-2026-09-29.md"]


def test_only_decisions_file_is_enough(env):
    env.decisions(YESTERDAY, decision_line(NOW - timedelta(hours=9)) + "\n")
    env.mod.run_report(NOW)
    text = env.report(YESTERDAY).read_text(encoding="utf-8")
    assert "- No energy history for this day." in text


def test_nothing_is_written_when_neither_file_exists(env):
    env.mod.run_report(NOW)
    assert not env.hist.exists()
    assert env.log.by_level["error"] == []


def test_blank_files_write_nothing(env):
    env.decisions(YESTERDAY, "\n")
    env.blocks(YESTERDAY, "")
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()


def test_day_is_decided_in_the_configured_timezone(env):
    # 00:10 on the 30th in Brussels is 15:10 on the 29th in Los Angeles: there
    # the 29th is still today, so only the 28th is complete.
    env.write_config("timezone: America/Los_Angeles\n")
    day_files(env, date(2026, 9, 29))
    day_files(env, date(2026, 9, 28))
    env.mod.run_report(NOW)
    assert env.report(date(2026, 9, 28)).exists()
    assert not env.report(date(2026, 9, 29)).exists()


def test_today_is_never_reported(env):
    today = date(2026, 9, 30)
    day_files(env, today)
    env.mod.run_report(NOW)
    assert not env.report(today).exists()


def test_backfills_missing_days_of_the_last_week_and_only_those(env):
    for back in (1, 3, 5, 7, 8):
        day_files(env, YESTERDAY - timedelta(days=back - 1))
    done = YESTERDAY - timedelta(days=2)             # 3 days back: already done
    env.hist.mkdir(parents=True, exist_ok=True)
    env.report(done).write_text("keep me", encoding="utf-8")
    env.mod.run_report(NOW)
    written = sorted(p.name for p in env.hist.glob("report-*.md"))
    assert written == ["report-2026-09-23.md", "report-2026-09-25.md",
                       "report-2026-09-27.md", "report-2026-09-29.md"]
    # day 8 back (09-22) is outside the window; existing report untouched
    assert env.report(done).read_text(encoding="utf-8") == "keep me"
    assert not env.report(date(2026, 9, 22)).exists()


def test_never_overwrites_an_existing_report(env):
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    first = env.report(YESTERDAY).read_text(encoding="utf-8")
    env.blocks(YESTERDAY, block_line(datetime(2026, 9, 29, 12, tzinfo=BRU), 9.0))
    env.mod.run_report(NOW + timedelta(days=1, minutes=1))
    assert env.report(YESTERDAY).read_text(encoding="utf-8") == first


def test_write_helper_refuses_to_overwrite(env):
    target = env.hist / "report-2026-01-01.md"
    env.hist.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    assert env.mod._write_text_new(str(target), "new") == "exists"
    assert target.read_text(encoding="utf-8") == "old"


def test_write_is_atomic_a_failed_replace_leaves_no_report(env, monkeypatch):
    day_files(env, YESTERDAY)

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()       # never a partial file
    assert any("not written" in m and "disk full" in m
               for m in env.log.by_level["warning"])
    assert env.log.by_level["error"] == []


def test_report_disabled_writes_nothing(env):
    env.write_config("report:\n  enabled: false\n")
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()


def test_broken_config_logs_and_does_not_raise(env):
    env.config_path.write_text("battery: [", encoding="utf-8")
    bump_mtime(env.config_path, 20)
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()
    assert any("config unusable" in m for m in env.log.by_level["error"])


def test_missing_config_logs_and_does_not_raise(env):
    env.config_path.unlink()
    env.mod.run_report(NOW)
    assert any("config unusable" in m for m in env.log.by_level["error"])


def test_a_failing_day_does_not_stop_the_other_days(env, monkeypatch):
    day_files(env, YESTERDAY)
    day_files(env, YESTERDAY - timedelta(days=1))
    real = env.mod._read_text

    def flaky(path):
        if "2026-09-28" in path:
            raise RuntimeError("nas hiccup")
        return real(path)
    monkeypatch.setattr(env.mod, "_read_text", flaky)
    env.mod.run_report(NOW)
    assert env.report(YESTERDAY).exists()
    assert not env.report(YESTERDAY - timedelta(days=1)).exists()
    assert any("2026-09-28" in m and "failed" in m
               for m in env.log.by_level["warning"])


def test_unexpected_failure_is_logged_never_raised(env, monkeypatch):
    monkeypatch.setattr(env.mod, "_list_names",
                        lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    env.mod.run_report(NOW)
    assert any("daily report failed" in m for m in env.log.by_level["error"])


def test_symlinked_report_dir_entries_are_not_listed(env, tmp_path):
    env.hist.mkdir(parents=True)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("x", encoding="utf-8")
    (env.hist / "blocks-2026-09-29.jsonl").symlink_to(target)
    assert env.mod._list_names(str(env.hist)) == []


def test_trigger_function_is_registered_at_00_10(env):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    crons = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call)
                        and getattr(dec.func, "id", "") == "time_trigger"):
                    crons[node.name] = dec.args[0].value
    assert crons["housekeeping_report"] == "cron(10 0 * * *)"


# ---- native loader ------------------------------------------------------------

def test_ensure_core_binds_modules_and_aliases_bare_names(env):
    mod = env.mod
    for n in mod.CORE_MODULES:
        sys.modules.pop(n, None)
    mod._ensure_core()
    assert mod.CORE_MODULES == ("config", "history", "report", "retention")
    assert mod.report.__name__ == "housekeeping_core_report"
    assert sys.modules["report"] is mod.report
    assert sys.modules["history"] is mod.history
    assert "inverter" not in mod.CORE_MODULES


def test_loader_reuses_a_module_already_loaded_from_the_same_file(env):
    mod = env.mod
    first = mod._load_core(str(MODULES), ("config",))
    again = mod._load_core(str(MODULES), ("config",))
    assert again["config"] is first["config"]


def test_failed_load_undoes_the_bare_aliases(env, tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text("raise RuntimeError('broken')\n")
    (tmp_path / "history.py").write_text("X = 1\n")
    monkeypatch.delitem(sys.modules, "housekeeping_core_config", raising=False)
    before = sys.modules.get("history")
    with pytest.raises(RuntimeError):
        env.mod._load_core(str(tmp_path), ("history", "config"))
    assert sys.modules.get("history") is before
    assert "housekeeping_core_config" not in sys.modules


# ---- retention cleanup ---------------------------------------------------------

NOW_CLEAN = datetime(2026, 9, 30, 3, 30, tzinfo=BRU)       # today = 2026-09-30


def old(days):
    return date(2026, 9, 30) - timedelta(days=days)


def make_tree(env, days=(0, 1, 89, 90, 91, 120, 400)):
    """decisions, blocks and report files for each age, plus bystanders."""
    env.base.mkdir(parents=True, exist_ok=True)
    env.hist.mkdir(parents=True, exist_ok=True)
    for n in days:
        d = old(n).isoformat()
        (env.base / ("decisions-%s.log" % d)).write_text("x")
        (env.hist / ("blocks-%s.jsonl" % d)).write_text("x")
        (env.hist / ("report-%s.md" % d)).write_text("x")
    for rel in ("state/halt.json", "state/last_command.json",
                "cache/solar.json", "history/last_snapshot.json",
                "decisions-notes.log", "decisions-2026-02-30.log",
                "history/blocks-2025-01-01.jsonl.tmp",
                "history/report-2025-01-01.md.bak", "README.txt"):
        path = env.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")


def tree(env):
    return sorted(str(p.relative_to(env.base))
                  for p in env.base.rglob("*") if p.is_file() or p.is_symlink())


BYSTANDERS = ["README.txt", "cache/solar.json", "decisions-2026-02-30.log",
              "decisions-notes.log", "history/blocks-2025-01-01.jsonl.tmp",
              "history/last_snapshot.json", "history/report-2025-01-01.md.bak",
              "state/halt.json", "state/last_command.json", "user_config.yaml"]


def test_cleanup_deletes_only_matching_files_older_than_keep_days(env):
    make_tree(env)
    env.mod.run_cleanup(NOW_CLEAN)
    left = tree(env)
    for n in (91, 120, 400):
        d = old(n).isoformat()
        for rel in ("decisions-%s.log", "history/blocks-%s.jsonl",
                    "history/report-%s.md"):
            assert rel % d not in left
    for n in (0, 1, 89, 90):          # 90 days old: kept (boundary)
        d = old(n).isoformat()
        for rel in ("decisions-%s.log", "history/blocks-%s.jsonl",
                    "history/report-%s.md"):
            assert rel % d in left
    for rel in BYSTANDERS:
        assert rel in left, rel


def test_cleanup_logs_one_info_line_with_counts(env):
    make_tree(env)
    env.mod.run_cleanup(NOW_CLEAN)
    assert env.log.by_level["info"] == [
        "housekeeping: cleanup removed 9 files older than 90 days "
        "(3 decision logs, 3 history files, 3 reports), 0 could not be removed"]
    assert env.log.by_level["warning"] == [] and env.log.by_level["error"] == []


def test_cleanup_with_nothing_to_delete_still_logs_once(env):
    make_tree(env, days=(0, 1, 90))
    env.mod.run_cleanup(NOW_CLEAN)
    assert len(env.log.by_level["info"]) == 1
    assert "removed 0 files" in env.log.by_level["info"][0]


def test_cleanup_honours_configured_keep_days(env):
    env.write_config("retention:\n  keep_days: 28\n")
    make_tree(env, days=(27, 28, 29, 30))
    env.mod.run_cleanup(NOW_CLEAN)
    left = tree(env)
    assert "decisions-%s.log" % old(28) in left
    assert "decisions-%s.log" % old(29) not in left
    assert "history/report-%s.md" % old(30) not in left


def test_cleanup_day_is_decided_in_the_configured_timezone(env):
    # 00:30 on 2026-09-30 in Brussels is still 29 Sep 15:30 in Los Angeles,
    # so there the cutoff is one day earlier than in Brussels.
    env.write_config("timezone: America/Los_Angeles\n")
    make_tree(env, days=(91, 92))
    env.mod.run_cleanup(datetime(2026, 9, 30, 0, 30, tzinfo=BRU))
    left = tree(env)
    assert "decisions-%s.log" % old(91) in left       # exactly 90 days old there
    assert "decisions-%s.log" % old(92) not in left


def test_cleanup_never_follows_symlinks_or_removes_directories(env, tmp_path):
    make_tree(env, days=())
    outside = tmp_path / "precious.log"
    outside.write_text("precious")
    victim = env.base / ("decisions-%s.log" % old(300))
    victim.symlink_to(outside)
    directory = env.hist / ("blocks-%s.jsonl" % old(300))
    directory.mkdir()
    (directory / "inner.txt").write_text("inner")
    env.mod.run_cleanup(NOW_CLEAN)
    assert victim.is_symlink() and outside.read_text() == "precious"
    assert (directory / "inner.txt").exists()


def test_delete_helper_ignores_symlinks_dirs_and_vanished_files(env, tmp_path):
    target = tmp_path / "t.txt"
    target.write_text("t")
    link = tmp_path / "l"
    link.symlink_to(target)
    sub = tmp_path / "d"
    sub.mkdir()
    real = tmp_path / "r"
    real.write_text("r")
    deleted, failures = env.mod._delete_files(
        [str(link), str(sub), str(tmp_path / "missing"), str(real)])
    assert deleted == [str(real)] and failures == []
    assert target.exists() and link.is_symlink() and sub.is_dir()


def test_vanished_file_between_listing_and_delete_is_ignored(env, monkeypatch):
    make_tree(env, days=(300,))
    real = os.remove

    def racing(path):
        real(path)
        raise FileNotFoundError(path)
    monkeypatch.setattr(os, "remove", racing)
    env.mod.run_cleanup(NOW_CLEAN)
    assert env.log.by_level["warning"] == [] and env.log.by_level["error"] == []


def test_failures_warn_and_the_rest_is_still_deleted(env, monkeypatch):
    make_tree(env, days=(300, 301))
    real = os.remove

    def flaky(path):
        if "blocks-" in str(path):
            raise PermissionError("denied")
        real(path)
    monkeypatch.setattr(os, "remove", flaky)
    env.mod.run_cleanup(NOW_CLEAN)
    left = tree(env)
    assert "decisions-%s.log" % old(300) not in left
    assert "history/report-%s.md" % old(301) not in left
    assert "history/blocks-%s.jsonl" % old(300) in left
    assert len(env.log.by_level["warning"]) == 2
    assert all("cannot remove" in m and "denied" in m
               for m in env.log.by_level["warning"])
    assert "2 could not be removed" in env.log.by_level["info"][0]


def test_failure_warnings_are_rate_limited(env, monkeypatch):
    make_tree(env, days=tuple(range(100, 112)))             # 12 days x 3 files

    def deny(path):
        raise PermissionError("no")
    monkeypatch.setattr(os, "remove", deny)
    env.mod.run_cleanup(NOW_CLEAN)
    warnings = env.log.by_level["warning"]
    assert len(warnings) == env.mod.MAX_FAILURE_WARNINGS + 1
    assert warnings[-1] == (
        "housekeeping: %d more files could not be removed"
        % (36 - env.mod.MAX_FAILURE_WARNINGS))
    assert "36 could not be removed" in env.log.by_level["info"][0]


def test_cleanup_with_broken_config_deletes_nothing(env):
    make_tree(env)
    before = tree(env)
    env.config_path.write_text("retention: [", encoding="utf-8")
    bump_mtime(env.config_path, 20)
    env.mod.run_cleanup(NOW_CLEAN)
    assert tree(env) == before
    assert any("config unusable" in m for m in env.log.by_level["error"])


def test_cleanup_with_invalid_keep_days_deletes_nothing(env):
    make_tree(env)
    before = tree(env)
    env.write_config("retention:\n  keep_days: 3\n")        # < 28
    env.mod.run_cleanup(NOW_CLEAN)
    assert tree(env) == before
    assert any("retention.keep_days" in m for m in env.log.by_level["error"])


def test_cleanup_runs_even_when_the_report_is_disabled(env):
    env.write_config("report:\n  enabled: false\n")
    make_tree(env, days=(300,))
    env.mod.run_cleanup(NOW_CLEAN)
    assert "decisions-%s.log" % old(300) not in tree(env)


def test_cleanup_missing_directories_is_fine(env):
    env.mod.run_cleanup(NOW_CLEAN)
    assert env.log.by_level["error"] == []


def test_cleanup_unexpected_failure_is_logged_never_raised(env, monkeypatch):
    def boom(paths):
        raise RuntimeError("boom")
    monkeypatch.setattr(env.mod, "_delete_files", boom)
    make_tree(env, days=(300,))
    env.mod.run_cleanup(NOW_CLEAN)
    assert any("cleanup failed" in m for m in env.log.by_level["error"])


def test_a_report_for_an_expired_day_is_not_regenerated(env):
    """Cleanup (03:30) and report (00:10) cannot fight: the report only looks
    back 7 days, and keep_days is at least 28."""
    make_tree(env, days=(300,))
    env.mod.run_cleanup(NOW_CLEAN)
    env.mod.run_report(datetime(2026, 10, 1, 0, 10, tzinfo=BRU))
    assert not any(n.startswith("history/report-") and old(300).isoformat() in n
                   for n in tree(env))


def test_both_triggers_are_registered_cleanup_after_report(env):
    parsed = ast.parse(SRC.read_text(encoding="utf-8"))
    crons = {}
    for node in parsed.body:
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call)
                        and getattr(dec.func, "id", "") == "time_trigger"):
                    crons[node.name] = dec.args[0].value
    assert crons == {"housekeeping_report": "cron(10 0 * * *)",
                     "housekeeping_cleanup": "cron(30 3 * * *)"}


def test_report_gets_the_battery_capacity_from_the_config(env):
    at = datetime(2026, 9, 29, 12, tzinfo=BRU)
    env.blocks(YESTERDAY, charge_block_line(at, 40.0, 55.0) + "\n")
    env.mod.run_report(NOW)                                # capacity_kwh: 10.0
    text = env.report(YESTERDAY).read_text(encoding="utf-8")
    assert "## Battery charge" in text
    assert "+1.50 kWh" in text and "Difference: +0.00 kWh, 0%" in text
    assert "differ by" not in text


def test_report_flags_a_wrong_capacity_from_the_config(env):
    at = datetime(2026, 9, 29, 12, tzinfo=BRU)
    env.blocks(YESTERDAY, charge_block_line(at, 40.0, 55.0) + "\n")
    env.config_path.write_text(
        env.config_path.read_text(encoding="utf-8").replace(
            "capacity_kwh: 10.0", "capacity_kwh: 20.0"), encoding="utf-8")
    env.mod.run_report(NOW)
    text = env.report(YESTERDAY).read_text(encoding="utf-8")
    assert "Change in charge (percentage times battery capacity): +3.00 kWh" in text
    assert "differ by 60%" in text
