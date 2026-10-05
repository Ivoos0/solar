"""Tests for pyscript/modules/retention.py and the retention.keep_days setting."""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import config
import retention

MODULES = Path(__file__).resolve().parent.parent / "pyscript" / "modules"
TODAY = date(2026, 9, 30)


def dlog(day):
    return "decisions-%s.log" % day.isoformat()


def blocks(day):
    return "blocks-%s.jsonl" % day.isoformat()


def rep(day):
    return "report-%s.md" % day.isoformat()


def ago(days):
    return TODAY - timedelta(days=days)


# ---- the boundary ------------------------------------------------------------

def test_exactly_keep_days_old_is_kept_one_day_older_is_deleted():
    result = retention.files_to_delete(
        [dlog(ago(90)), dlog(ago(91))],
        [blocks(ago(90)), blocks(ago(91)), rep(ago(90)), rep(ago(91))],
        TODAY, 90)
    assert sorted(result) == sorted([
        ("", dlog(ago(91))), ("history", blocks(ago(91))),
        ("history", rep(ago(91)))])


def test_boundary_follows_keep_days():
    names = [dlog(ago(n)) for n in range(0, 40)]
    result = retention.files_to_delete(names, [], TODAY, 28)
    assert sorted(n for _k, n in result) == sorted(dlog(ago(n)) for n in range(29, 40))


def test_today_and_future_are_kept():
    future = TODAY + timedelta(days=5)
    assert retention.files_to_delete(
        [dlog(TODAY), dlog(future)], [blocks(TODAY), rep(future)], TODAY, 1) == []


def test_keep_days_one_deletes_the_day_before_yesterday_and_older():
    result = retention.files_to_delete(
        [dlog(ago(0)), dlog(ago(1)), dlog(ago(2))], [], TODAY, 1)
    assert result == [("", dlog(ago(2)))]


def test_result_is_oldest_first():
    result = retention.files_to_delete(
        [dlog(ago(100)), dlog(ago(300))], [rep(ago(200))], TODAY, 90)
    assert [n for _k, n in result] == [dlog(ago(300)), rep(ago(200)),
                                        dlog(ago(100))]


# ---- only exact patterns, only in the right directory ---------------------------

def test_each_pattern_only_in_its_own_directory():
    old = ago(200)
    # decisions-* in history/, blocks-*/report-* in the log dir: not managed
    assert retention.files_to_delete(
        [blocks(old), rep(old)], [dlog(old)], TODAY, 90) == []


@pytest.mark.parametrize("name", [
    "decisions-2026-01-01.log.bak",
    "decisions-2026-01-01.log.tmp",
    "xdecisions-2026-01-01.log",
    "decisions-2026-01-01.LOG",
    "decisions-2026-1-1.log",
    "decisions-26-01-01.log",
    "decisions-2026-01-01.log\n",
    "decisions-2026-01-01 .log",
    "decisions-2026-01-01.txt",
    "Decisions-2026-01-01.log",
    "decisions-٢٠٢٦-01-01.log",     # Arabic-Indic digits
    "decisions-latest.log",
    "last_command.json",
    "user_config.yaml",
    "halt.json",
    "solar.json",
    "last_snapshot.json",
    "..",
    ".",
    "",
    "../decisions-2026-01-01.log",
    "state/decisions-2026-01-01.log",
])
def test_other_names_are_never_selected_in_the_log_dir(name):
    assert retention.files_to_delete([name], [], TODAY, 90) == []


@pytest.mark.parametrize("name", [
    "blocks-2026-01-01.jsonl.tmp",
    "blocks-2026-01-01.json",
    "blocks-2026-01-01.jsonl.gz",
    "report-2026-01-01.md.tmp",
    "report-2026-01-01.txt",
    "report-2026-01-01.MD",
    "last_snapshot.json",
    "blocks-2026-01-01.jsonl/",
    "sub/blocks-2026-01-01.jsonl",
])
def test_other_names_are_never_selected_in_the_history_dir(name):
    assert retention.files_to_delete([], [name], TODAY, 90) == []


@pytest.mark.parametrize("name", [
    "decisions-2026-13-01.log", "decisions-2026-02-30.log",
    "decisions-0000-01-01.log", "decisions-2026-00-10.log",
    "decisions-2026-01-32.log", "decisions-9999-99-99.log",
])
def test_invalid_dates_are_skipped(name):
    assert retention.files_to_delete([name], [], TODAY, 90) == []


def test_invalid_dates_do_not_stop_valid_ones():
    result = retention.files_to_delete(
        ["decisions-2026-13-01.log", dlog(ago(200))], [], TODAY, 90)
    assert result == [("", dlog(ago(200)))]


def test_leap_day_is_a_valid_date():
    result = retention.files_to_delete(
        ["decisions-2024-02-29.log"], [], TODAY, 90)
    assert result == [("", "decisions-2024-02-29.log")]


def test_the_date_in_the_name_decides_nothing_else_is_consulted():
    # Only names go in; there is no way for an mtime to influence the result.
    import inspect
    assert list(inspect.signature(retention.files_to_delete).parameters) == [
        "log_names", "history_names", "today", "keep_days"]


# ---- argument checking ---------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, 1.5, 90.0, True, False, None, "90"])
def test_bad_keep_days_raises_instead_of_deleting(bad):
    with pytest.raises(ValueError):
        retention.files_to_delete([dlog(ago(500))], [], TODAY, bad)


@pytest.mark.parametrize("bad", [datetime(2026, 9, 30, 3, 30), "2026-09-30",
                                 None])
def test_today_must_be_a_date(bad):
    with pytest.raises(ValueError):
        retention.files_to_delete([dlog(ago(500))], [], bad, 90)


def test_empty_listings():
    assert retention.files_to_delete([], [], TODAY, 90) == []


def test_module_is_pure():
    tree = ast.parse((MODULES / "retention.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
    assert imported <= {"re", "datetime"}
    attrs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "remove", "unlink"}


# ---- config: retention.keep_days ------------------------------------------------------------

def _cfg(extra=None, **sections):
    raw = {"battery": {"capacity_kwh": 10.0},
           "alerts": {"address": "o@example.com"}}
    raw.update(sections)
    if extra:
        raw.update(extra)
    return config.from_dict(raw)


def test_default_is_90_days():
    assert _cfg().retention_keep_days == 90


def test_keep_days_is_configurable():
    assert _cfg(retention={"keep_days": 400}).retention_keep_days == 400


@pytest.mark.parametrize("bad", [True, False, 0, -5, 90.0, 89.5, "90", [], {}])
def test_keep_days_rejects_bool_float_and_non_positive(bad):
    with pytest.raises(config.ConfigError, match="keep_days"):
        _cfg(retention={"keep_days": bad})


def test_keep_days_below_the_usage_window_is_rejected_naming_both_keys():
    with pytest.raises(config.ConfigError) as exc:
        _cfg(retention={"keep_days": 27})
    message = str(exc.value)
    assert "retention.keep_days" in message
    assert "usage.history_weeks" in message
    assert "28" in message


def test_exactly_the_usage_window_is_allowed():
    assert _cfg(retention={"keep_days": 28}).retention_keep_days == 28


def test_history_weeks_two_allows_14_days():
    cfg = _cfg(retention={"keep_days": 14}, usage={"history_weeks": 2})
    assert cfg.retention_keep_days == 14
    with pytest.raises(config.ConfigError, match="usage.history_weeks"):
        _cfg(retention={"keep_days": 13}, usage={"history_weeks": 2})


def test_default_keep_days_conflicts_with_a_very_long_usage_window():
    with pytest.raises(config.ConfigError, match="retention.keep_days"):
        _cfg(usage={"history_weeks": 13})                   # needs 91 days
    assert _cfg(usage={"history_weeks": 13},
                retention={"keep_days": 91}).retention_keep_days == 91


def test_invalid_history_weeks_is_reported_without_a_crash():
    with pytest.raises(config.ConfigError, match="usage_history_weeks"):
        _cfg(usage={"history_weeks": 0}, retention={"keep_days": 1})


def test_keep_days_is_not_in_the_fingerprint():
    assert (_cfg().fingerprint()
            == _cfg(retention={"keep_days": 400}).fingerprint())
