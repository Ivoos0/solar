"""Tests for pyscript/modules/report.py: the daily report summary and Markdown."""
import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import config
import decision
import history
import report

BRU = ZoneInfo("Europe/Brussels")
UTC = timezone.utc
DAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 30, 0, 10, tzinfo=BRU)
MODULES = Path(__file__).resolve().parent.parent / "pyscript" / "modules"


def local(h, m=0, s=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=BRU)


def line(when, action="idle", selector="S6", vetoes=(), degraded=(), avg=None,
         ceiling=None, budget=None, source="planner", power=0.0):
    """A real decision line, produced by decision.format_record."""
    return decision.format_record(decision.DecisionRecord(
        timestamp=when, action=action, target_power_kw=power,
        charge_percent=50.0, charge_kwh=5.0, consumption_price=0.2,
        injection_price=0.1, forecast_remaining_kwh=3.0,
        usage_remaining_kwh=4.0, saturation_block=None, spill_kwh=0.0,
        reserve_breach_block=None, projected_end_charge_kwh=2.0,
        duration_ms=80, running_average_kw=avg, ceiling_kw=ceiling,
        budget_kw=budget, vetoes_applied=list(vetoes), selector=selector,
        reasoning="because | of reasons", degraded_inputs=list(degraded),
        source=source))


def hist_line(start_local, imp=None, exp=None, solar=None, forecast=None,
              charge=None, discharge=None, load=None, source=None,
              complete=True, soc=None, soc_end=None):
    rec = dict.fromkeys(history.RECORD_FIELDS)
    start = start_local.astimezone(UTC)
    rec.update(schema=1, block_start=start.isoformat(),
               local_date=start_local.date().isoformat(), block_minutes=15,
               import_kwh=imp, export_kwh=exp, solar_kwh=solar,
               forecast_solar_kwh=forecast, battery_charge_kwh=charge,
               battery_discharge_kwh=discharge, load_kwh=load,
               load_source=source, complete=complete, soc_percent=soc,
               soc_end_percent=soc_end)
    return history.to_line(rec)


# ---- expected blocks ---------------------------------------------------------

@pytest.mark.parametrize("day, expected", [
    (date(2026, 9, 29), 96),
    (date(2026, 3, 29), 92),      # spring forward: 23 local hours
    (date(2026, 10, 25), 100),    # fall back: 25 local hours
])
def test_expected_blocks_follow_the_real_length_of_the_local_day(day, expected):
    assert report.expected_blocks(day, BRU, 15) == expected


def test_expected_blocks_other_block_size():
    assert report.expected_blocks(date(2026, 3, 29), BRU, 30) == 46


def test_names():
    assert report.decisions_name(DAY) == "decisions-2026-09-29.log"
    assert report.history_name(DAY) == "blocks-2026-09-29.jsonl"
    assert report.report_name(DAY) == "report-2026-09-29.md"


# ---- decision log parser -------------------------------------------------------

def test_parses_real_lines_and_counts_by_hand():
    text = "\n".join([
        line(local(8), "charge", "S1", power=2.0, avg=1.0, ceiling=2.5,
             budget=1.5, degraded=["soc_stubbed", "usage_samples=3"]),
        line(local(9), "idle", "S6", vetoes=["V4(suppressed S1 charge)",
                                             "V4(suppressed S5 charge)"],
             avg=2.2, ceiling=2.5, budget=-0.4,
             degraded=["soc_stubbed"]),
        line(local(10), "export", "S3", vetoes=["V1+V2(suppressed S3 export)"],
             avg=0.5, ceiling=2.5, budget=2.0),
        line(local(11), "idle", "S6", vetoes=["V2"], degraded=["cache_age_solar=3h12m"]),
    ])
    s = report.parse_decision_log(text)
    assert s["records"] == 4 and s["malformed"] == 0
    assert s["actions"] == {"charge": 1, "idle": 2, "export": 1}
    assert s["selectors"] == {"S1": 1, "S6": 2, "S3": 1}
    # counted once per record: V4 appears twice in one record
    assert s["vetoes"] == {"V4": 1, "V1": 1, "V2": 2}
    assert s["suppressed"] == 3
    assert s["degraded"] == {"soc_stubbed": 2, "usage_samples": 1,
                             "cache_age_solar": 1}
    assert s["max_avg"] == 2.2 and s["max_avg_at"] == local(9)
    assert s["max_ceiling"] == 2.5
    assert s["min_budget"] == -0.4 and s["min_budget_at"] == local(9)


def test_na_values_are_not_numbers():
    s = report.parse_decision_log(line(local(8)))      # avg/ceiling/budget n/a
    assert s["records"] == 1
    assert s["max_avg"] is None and s["max_ceiling"] is None
    assert s["min_budget"] is None


def test_halt_recovered_and_skip_lines_are_recognised():
    halt = decision.format_halt(
        decision.HaltState(True, "price data unavailable", local(14, 20),
                           local(14, 20)), local(14, 35))
    text = "\n".join([
        halt, halt.replace("14:35:00", "14:40:00"),
        " | ".join([local(14, 45).isoformat(), "RECOVERED",
                    "cause=price data unavailable",
                    "entered=" + local(14, 20).isoformat(),
                    "halted_for=25m"]),
        " | ".join([local(15).isoformat(), "SKIP",
                    "cause=previous_cycle_running", "busy_for=180s"]),
        line(local(15, 5)),
    ])
    s = report.parse_decision_log(text)
    assert s["records"] == 1 and s["malformed"] == 0
    assert s["halt_cycles"] == 2
    assert s["outages"] == {local(14, 20).isoformat(): "price data unavailable"}
    assert [r["halted_for"] for r in s["recovered"]] == ["25m"]
    assert s["skips"] == 1


def test_malformed_lines_are_skipped_and_counted_blank_lines_are_not():
    good = line(local(8))
    text = "\n".join([
        good, "", "   ", "garbage",
        good[:60],                                   # truncated mid-line
        "2026-09-29T08:00:00 | action=idle | selector=S6",    # naive time
        local(9).isoformat() + " | action=fly | selector=S6",  # unknown action
        local(9).isoformat() + " | action=idle | selector=S9",  # bad selector
        local(9).isoformat() + " | HALT | nokeyvalue",
        local(9).isoformat() + " | something=else",
        good,
    ])
    s = report.parse_decision_log(text)
    assert s["records"] == 2
    assert s["malformed"] == 7


def test_parser_never_raises_on_binary_noise():
    s = report.parse_decision_log("\x00\x01 | = | = \n|||\n=\n")
    assert s["records"] == 0 and s["malformed"] == 3


def test_guard_episodes_split_on_a_pause():
    text = "\n".join(
        [line(local(18, i // 2, 30 * (i % 2)), "discharge", "S0", source="guard",
              power=1.0 + i) for i in range(3)]
        + [line(local(18, 30, 0), "idle", "S6")]            # planner, ignored
        + [line(local(19, 0, 0), "discharge", "S0", source="guard", power=0.5)])
    s = report.parse_decision_log(text)
    assert s["guard_records"] == 4
    assert len(s["episodes"]) == 2
    first, second = s["episodes"]
    assert (first["start"], first["end"], first["records"]) == (
        local(18, 0, 0), local(18, 1, 0), 3)
    assert first["max_kw"] == 3.0
    assert second["records"] == 1 and second["max_kw"] == 0.5


def test_empty_text_gives_an_empty_summary():
    s = report.parse_decision_log("")
    assert s["records"] == 0 and s["malformed"] == 0 and s["episodes"] == []


# ---- history summary ---------------------------------------------------------------

def test_history_summary_numbers_by_hand():
    text = "\n".join([
        hist_line(local(10), imp=0.5, exp=0.0, solar=0.2, forecast=0.4,
                  charge=0.1, discharge=0.0, load=0.6, source="derived"),
        hist_line(local(10, 15), imp=0.25, exp=0.1, solar=0.3, forecast=0.3,
                  charge=0.0, discharge=0.05, load=0.45, source="measured"),
        hist_line(local(10, 30), imp=0.75, exp=0.0, solar=None, forecast=0.5,
                  complete=False),
        hist_line(local(10, 45), imp=0.0, solar=1.0),       # no forecast
        "not json",
    ])
    h = report.summarise_history(text, DAY, BRU, 15)
    assert h["malformed"] == 1
    assert h["recorded"] == 4 and h["complete"] == 3 and h["expected"] == 96
    t = h["totals"]
    assert t["import_kwh"] == {"sum": 1.5, "known": 4}
    assert t["export_kwh"] == {"sum": pytest.approx(0.1), "known": 3}
    assert t["solar_kwh"]["sum"] == pytest.approx(1.5)
    assert t["solar_kwh"]["known"] == 3
    assert t["load_kwh"]["sum"] == pytest.approx(1.05)
    assert (h["load_measured"], h["load_derived"]) == (1, 1)
    # both known in the first two blocks only: 0.5 produced of 0.7 forecast
    assert h["paired_blocks"] == 2
    assert h["paired_solar"] == pytest.approx(0.5)
    assert h["paired_forecast"] == pytest.approx(0.7)
    assert h["ratio"] == pytest.approx(0.5 / 0.7)


def test_history_duplicate_block_start_keeps_last_and_other_days_are_ignored():
    text = "\n".join([
        hist_line(local(10), imp=1.0),
        hist_line(local(10), imp=2.0),
        hist_line(local(10, 0, 0, day=date(2026, 9, 28)), imp=9.0),
    ])
    h = report.summarise_history(text, DAY, BRU, 15)
    assert h["recorded"] == 1
    assert h["totals"]["import_kwh"]["sum"] == 2.0


def test_ratio_is_none_without_forecast():
    h = report.summarise_history(hist_line(local(10), solar=1.0), DAY, BRU, 15)
    assert h["ratio"] is None and h["paired_blocks"] == 0


def test_history_on_a_dst_day_expects_92_and_100():
    spring = report.summarise_history(
        hist_line(datetime(2026, 3, 29, 12, tzinfo=BRU), imp=1.0),
        date(2026, 3, 29), BRU, 15)
    autumn = report.summarise_history(
        hist_line(datetime(2026, 10, 25, 12, tzinfo=BRU), imp=1.0),
        date(2026, 10, 25), BRU, 15)
    assert spring["expected"] == 92 and autumn["expected"] == 100


def test_fall_back_hour_keeps_both_blocks_distinct():
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)         # 02:30 CEST
    second = first + timedelta(hours=1)                       # 02:30 CET
    text = "\n".join(hist_line(t.astimezone(BRU), imp=1.0)
                     for t in (first, second))
    h = report.summarise_history(text, date(2026, 10, 25), BRU, 15)
    assert h["recorded"] == 2


# ---- build_report -----------------------------------------------------------------------

def test_nothing_to_report_returns_none():
    assert report.build_report(DAY, None, None, tz=BRU, block_minutes=15,
                               now=NOW) is None
    assert report.build_report(DAY, "", "  \n", tz=BRU, block_minutes=15,
                               now=NOW) is None


def _full_report():
    decisions = "\n".join([
        line(local(8), "charge", "S1", power=2.0, avg=1.0, ceiling=2.5,
             budget=1.5, degraded=["soc_stubbed"]),
        line(local(9), "idle", "S6", vetoes=["V2"], avg=2.7, ceiling=2.5,
             budget=-0.4, degraded=["soc_stubbed"]),
        line(local(18, 0, 0), "discharge", "S0", source="guard", power=1.5),
        line(local(18, 0, 30), "discharge", "S0", source="guard", power=2.0),
        decision.format_halt(decision.HaltState(
            True, "prices gone", local(14, 20), None), local(14, 35)),
        "oops",
    ])
    hist = "\n".join([
        hist_line(local(10), imp=0.5, exp=0.1, solar=0.4, forecast=0.5,
                  load=0.8, source="derived"),
        hist_line(local(10, 15), imp=0.25, exp=0.0, solar=0.6, forecast=0.5,
                  load=0.85, source="derived"),
    ])
    return report.build_report(DAY, decisions, hist, tz=BRU, block_minutes=15,
                               now=NOW)


def test_rendered_report_key_lines():
    text = _full_report()
    lines = text.splitlines()
    assert lines[0] == "# Battery planner report for 2026-09-29"
    assert "Generated 2026-09-30T00:10:00+02:00 (Europe/Brussels)." in lines
    assert "## Decisions" in lines and "## Energy" in lines
    assert "- Records: 4" in lines
    assert "- Actions: charge 1, discharge 2, export 0, idle 1" in lines
    assert "- Selectors: S0 2, S1 1, S6 1" in lines
    assert "- Vetoes seen: V2 1 (0 proposals suppressed)" in lines
    assert "- Degraded inputs: soc_stubbed 2" in lines
    assert "- Halts: 1 cycle without a decision: prices gone (since " \
        "2026-09-29T14:20:00+02:00)" in lines
    assert "- Peak guard: 1 shaving episode (2 records)" in lines
    assert "  - 18:00-18:00, 2 records, up to 2.00kW" in lines
    assert ("- Highest quarter-hour average: 2.70kW at 09:00 "
            "(ceiling 2.50kW, above it)") in lines
    assert "- Lowest charging budget: -0.40kW at 09:00" in lines
    assert "| Imported from the grid | 0.75 | 2 |" in lines
    assert "| Exported to the grid | 0.10 | 2 |" in lines
    assert "| Solar produced | 1.00 | 2 |" in lines
    assert "| Battery charged | n/a | 0 |" in lines
    assert "| Household load | 1.65 | 2 |" in lines
    assert ("- Solar against forecast: 1.00 kWh produced of 1.00 kWh "
            "forecast, 100% (2 blocks with both)") in lines
    assert "- Household load: 0 blocks measured, 2 derived from the other " \
        "counters" in lines
    assert "- Blocks recorded: 2 of 96 expected (2 complete)" in lines


def test_data_quality_section_lists_what_is_wrong():
    text = _full_report()
    assert "## Data quality" in text
    assert "- 1 decision log line could not be read and was skipped." in text
    assert "- Only 2 of 96 blocks were recorded" in text
    assert ("- Battery charged has no value in 2 of 2 blocks "
            "(not configured or never readable).") in text


def test_complete_day_does_not_complain_about_missing_blocks():
    decisions = line(local(8))
    hist = "\n".join(hist_line(local(0) + timedelta(minutes=15 * i), imp=0.1)
                     for i in range(96))
    text = report.build_report(DAY, decisions, hist, tz=BRU, block_minutes=15,
                               now=NOW)
    assert "- Blocks recorded: 96 of 96 expected (96 complete)" in text
    assert "Only " not in text


def test_missing_history_is_called_out():
    text = report.build_report(DAY, line(local(8)), None, tz=BRU,
                               block_minutes=15, now=NOW)
    assert "## Energy" not in text
    assert "- No energy history for this day." in text


def test_missing_decisions_is_called_out():
    text = report.build_report(DAY, None, hist_line(local(8), imp=1.0),
                               tz=BRU, block_minutes=15, now=NOW)
    assert "## Decisions" not in text
    assert "- No decision log for this day." in text


def test_dst_day_report_says_92_and_100():
    spring = report.build_report(
        date(2026, 3, 29), None,
        hist_line(datetime(2026, 3, 29, 12, tzinfo=BRU), imp=1.0),
        tz=BRU, block_minutes=15, now=NOW)
    autumn = report.build_report(
        date(2026, 10, 25), None,
        hist_line(datetime(2026, 10, 25, 12, tzinfo=BRU), imp=1.0),
        tz=BRU, block_minutes=15, now=NOW)
    assert "of 92 expected" in spring and "of 100 expected" in autumn


def test_report_is_pure_given_now():
    a = _full_report()
    assert a == _full_report()


# ---- days_to_report -----------------------------------------------------------------------

def test_days_to_report_picks_only_missing_days_with_data():
    today = date(2026, 9, 30)
    d = lambda n: report.decisions_name(today - timedelta(days=n))
    h = lambda n: report.history_name(today - timedelta(days=n))
    r = lambda n: report.report_name(today - timedelta(days=n))
    days = report.days_to_report(
        today,
        [d(1), d(2), d(4), d(8), d(0)],          # d(8) too old, d(0) today
        [h(1), h(3)],
        [r(2), r(7)],                            # 2 already reported
        7)
    assert days == [today - timedelta(days=n) for n in (4, 3, 1)]


def test_days_to_report_never_includes_today_or_future():
    today = date(2026, 9, 30)
    assert report.days_to_report(
        today, [report.decisions_name(today),
                report.decisions_name(today + timedelta(days=1))], [], [], 7) == []


def test_days_to_report_ignores_unrelated_names():
    today = date(2026, 9, 30)
    assert report.days_to_report(
        today, ["decisions-2026-09-29.log.bak", "notes.txt"],
        ["blocks-2026-09-29.jsonl.tmp"], [], 7) == []


# ---- config -------------------------------------------------------------------------------

def _cfg(extra):
    raw = {"battery": {"capacity_kwh": 10.0, "soc_sensor": "sensor.test_battery_soc"},
           "alerts": {"address": "o@example.com"}}
    raw.update(extra)
    return config.from_dict(raw)


def test_report_enabled_defaults_true_and_is_configurable():
    assert _cfg({}).report_enabled is True
    assert _cfg({"report": {"enabled": False}}).report_enabled is False


@pytest.mark.parametrize("bad", ["yes", 1, 0, []])
def test_report_enabled_must_be_a_boolean(bad):
    with pytest.raises(config.ConfigError, match=r"report\.enabled"):
        _cfg({"report": {"enabled": bad}})


def test_report_enabled_is_not_in_the_fingerprint():
    assert (_cfg({}).fingerprint()
            == _cfg({"report": {"enabled": False}}).fingerprint())


# ---- purity ---------------------------------------------------------------------------------

def test_report_module_has_no_io_clock_or_ha_imports():
    tree = ast.parse((MODULES / "report.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
    assert imported <= {"math", "re", "datetime", "history"}
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"now", "utcnow", "today", "open"}
    assert "open" not in {n.func.id for n in ast.walk(tree)
                          if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name)}


# ---- battery charge section -------------------------------------------------------

def charge_report(blocks, capacity=10.0, day=DAY):
    """Report text for blocks [(start_local, charge, discharge, soc, soc_end)]."""
    hist = "\n".join(hist_line(t, charge=c, discharge=d, soc=a, soc_end=b)
                      for t, c, d, a, b in blocks)
    return report.build_report(day, None, hist, tz=BRU, block_minutes=15,
                               now=NOW, capacity_kwh=capacity)


def section(text):
    lines = text.splitlines()
    start = lines.index("## Battery charge")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


def test_hand_computed_cross_check_without_difference():
    # 10 kWh battery, 2.0 kWh in, 0.5 kWh out, 40 % -> 55 %:
    # expected +1.5 kWh, charge change 15 % of 10 kWh = +1.5 kWh, difference 0.
    text = charge_report([(local(10), 2.0, 0.5, 40.0, 55.0)])
    assert section(text) == [
        "## Battery charge", "",
        "- Charge: first 40.0%, last 55.0%, lowest 40.0%, highest 55.0%",
        "- Counters against charge, over 1 block with both charges and both "
        "counters:",
        "  - Stored change from the counters (charged minus discharged): "
        "+1.50 kWh",
        "  - Change in charge (percentage times battery capacity): +1.50 kWh",
        "  - Difference: +0.00 kWh, 0% of the 2.50 kWh moved (charged plus "
        "discharged)", ""]
    assert "differ by" not in text


def test_first_last_lowest_highest_use_both_fields_in_time_order():
    text = charge_report([                       # file order is not time order
        (local(12), 0.1, 0.1, 30.0, 35.0),
        (local(10), 0.1, 0.1, 50.0, 20.0),
        (local(11), 0.1, 0.1, 20.0, 30.0),
    ])
    assert ("- Charge: first 50.0%, last 35.0%, lowest 20.0%, highest 50.0%"
            in section(text))


def test_blocks_without_both_charges_count_for_range_not_for_cross_check():
    text = charge_report([
        (local(10), 2.0, 0.5, 40.0, 55.0),
        (local(11), 1.0, 0.0, 90.0, None),       # no end charge
        (local(12), 1.0, 0.0, None, 10.0),       # no start charge
        (local(13), None, 0.0, 40.0, 50.0),      # no counter
    ])
    sec = section(text)
    assert "- Charge: first 40.0%, last 50.0%, lowest 10.0%, highest 90.0%" in sec
    assert any("over 1 block " in l for l in sec)
    assert ("  - Difference: +0.00 kWh, 0% of the 2.50 kWh moved (charged plus "
            "discharged)") in sec


def test_only_end_values_still_give_the_range():
    text = charge_report([(local(10), None, None, None, 61.0)])
    sec = section(text)
    assert "- Charge: first 61.0%, last 61.0%, lowest 61.0%, highest 61.0%" in sec
    assert ("- Counters against charge: n/a (no block with both charges and "
            "both battery counters)") in sec


def test_large_difference_is_flagged_in_data_quality():
    # charge change +0.5 kWh against +1.5 kWh expected: 1.0 of 2.5 kWh = 40 %
    text = charge_report([(local(10), 2.0, 0.5, 40.0, 45.0)])
    assert "  - Difference: -1.00 kWh, 40% of the 2.50 kWh moved" in text
    quality = text.split("## Data quality")[1]
    assert ("differ by 40% of the 2.50 kWh moved (+1.50 kWh against "
            "+0.50 kWh)") in quality
    assert "battery.capacity_kwh" in quality


@pytest.mark.parametrize("soc_end, flagged", [
    (50.0, False),        # difference exactly 25 % of 4.0 kWh: not above it
    (49.0, True),         # 27.5 %
    (51.0, False),        # 22.5 %
    (80.0, True),         # more charge gained than counted: 50 %
])
def test_flag_threshold_is_above_25_percent(soc_end, flagged):
    text = charge_report([(local(10), 3.0, 1.0, 40.0, soc_end)])
    assert ("differ by" in text) is flagged


def test_small_throughput_is_never_flagged():
    # difference 0.4 of 0.6 kWh (67 %), but under 1 kWh moved
    text = charge_report([(local(10), 0.5, 0.1, 40.0, 40.0)])
    assert "  - Difference: -0.40 kWh, 67% of the 0.60 kWh moved" in text
    assert "differ by" not in text
    # exactly 1 kWh moved counts
    text = charge_report([(local(10), 0.5, 0.5, 40.0, 50.0)])
    assert "differ by 100% of the 1.00 kWh moved" in text


def test_round_trip_losses_are_not_flagged():
    # 3.0 in, 2.0 out, +9 % of 10 kWh = +0.9 kWh against +1.0 kWh expected
    text = charge_report([(local(10), 3.0, 2.0, 40.0, 49.0)])
    assert "  - Difference: -0.10 kWh, 2% of the 5.00 kWh moved" in text
    assert "differ by" not in text


def test_sums_are_over_blocks_not_a_net_of_the_day():
    text = charge_report([(local(10), 1.0, 0.0, 40.0, 50.0),
                          (local(11), 0.0, 1.0, 50.0, 40.0)])
    assert ("  - Stored change from the counters (charged minus discharged): "
            "+0.00 kWh") in text
    assert "0% of the 2.00 kWh moved" in text


@pytest.mark.parametrize("capacity", [None, 0, -1.0, float("nan"), "10"])
def test_no_usable_capacity_skips_the_section(capacity):
    text = charge_report([(local(10), 2.0, 0.5, 40.0, 55.0)], capacity=capacity)
    assert "## Battery charge" not in text


def test_no_charge_data_skips_the_section():
    assert "## Battery charge" not in charge_report(
        [(local(10), 2.0, 0.5, None, None)])
    old = history.parse_lines(
        hist_line(local(10), imp=1.0, charge=1.0, discharge=0.0))[0][0]
    del old["soc_percent"], old["soc_end_percent"]           # old format
    text = report.build_report(DAY, None, history.to_line(old) + "\n",
                               tz=BRU, block_minutes=15, now=NOW,
                               capacity_kwh=10.0)
    assert "## Battery charge" not in text and "| Imported from the grid" in text


def test_garbage_charge_values_are_ignored():
    rec = history.parse_lines(
        hist_line(local(10), charge=1.0, discharge=0.0))[0][0]
    rec.update(soc_percent="40", soc_end_percent=True)
    text = report.build_report(DAY, None, history.to_line(rec) + "\n", tz=BRU,
                               block_minutes=15, now=NOW, capacity_kwh=10.0)
    assert "## Battery charge" not in text


def test_without_capacity_argument_the_report_is_unchanged():
    hist = hist_line(local(10), imp=1.0, charge=2.0, discharge=0.5, soc=40.0,
                     soc_end=55.0)
    text = report.build_report(DAY, None, hist, tz=BRU, block_minutes=15, now=NOW)
    assert "## Battery charge" not in text


def test_section_sits_after_energy_before_data_quality():
    text = charge_report([(local(10), 2.0, 0.5, 40.0, 45.0)])
    assert text.index("## Energy") < text.index("## Battery charge") \
        < text.index("## Data quality")


@pytest.mark.parametrize("day, count", [
    (date(2026, 3, 29), 92), (date(2026, 10, 25), 100)])
def test_dst_days_count_every_block_once_and_only_that_day(day, count):
    first = datetime(day.year, day.month, day.day, tzinfo=BRU).astimezone(UTC)
    blocks = [(first + timedelta(minutes=15 * i), 0.1, 0.1,
               20.0 + i * 0.5, 20.5 + i * 0.5) for i in range(count)]
    # a block of the next day and of the previous day must not count
    blocks.append((first + timedelta(minutes=15 * count), 0.1, 0.1, 99.0, 99.0))
    blocks.append((first - timedelta(minutes=15), 0.1, 0.1, 1.0, 1.0))
    sec = section(charge_report(blocks, day=day))
    assert "over %d blocks" % count in sec[3]
    last = 20.5 + (count - 1) * 0.5
    assert ("- Charge: first 20.0%%, last %.1f%%, lowest 20.0%%, highest %.1f%%"
            % (last, last)) in sec
