"""Tests for pyscript/modules/history.py (pure energy-history core)."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import history as h

UTC = timezone.utc
BR = ZoneInfo("Europe/Brussels")
B0 = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
STEP = timedelta(minutes=15)


def snap(boundary=B0, late=0, readings=None, **ctx):
    """Snapshot at `boundary`, read `late` seconds after it."""
    return h.make_snapshot(boundary, boundary + timedelta(seconds=late),
                           readings or {}, **ctx)


def full(import_=100.0, export=50.0, solar=200.0, charge=10.0, discharge=20.0,
         load=None):
    r = {"import": [import_], "export": [export], "solar": [solar],
         "battery_charge": [charge], "battery_discharge": [discharge]}
    if load is not None:
        r["load"] = [load]
    return r


# ---- block_floor -------------------------------------------------------------

def test_block_floor_is_utc_and_dst_safe():
    t = datetime(2026, 10, 25, 2, 40, 33, 5, tzinfo=BR, fold=1)   # second 02:40
    assert h.block_floor(t, 15) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert h.block_floor(datetime(2026, 10, 25, 2, 40, tzinfo=BR), 15) \
        == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)               # first 02:40


# ---- snapshots -----------------------------------------------------------------

def test_counters_of_a_quantity_are_summed():
    s = snap(readings={"import": [10.5, 4.25], "export": [1.0, 2.0]})
    assert s.totals["import"] == 14.75 and s.totals["export"] == 3.0
    assert s.failed == ()


def test_unconfigured_quantity_is_none_and_not_a_failure():
    s = snap(readings={"import": [1.0]})
    assert s.totals["solar"] is None and "solar" not in s.failed


def test_one_unreadable_counter_makes_the_quantity_unknown_not_partial():
    s = snap(readings={"import": [10.0, None]})
    assert s.totals["import"] is None and s.failed == ("import",)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "x", True])
def test_non_finite_or_non_numeric_reading_is_a_failure(bad):
    assert snap(readings={"import": [bad]}).failed == ("import",)


def test_snapshot_dict_roundtrip_and_corrupt_input():
    s = snap(late=7, readings=full(), forecast_solar_kwh=0.5,
             consumption_price=0.2, injection_price=0.05, soc_percent=40.0)
    assert h.snapshot_from_dict(json.loads(json.dumps(h.snapshot_to_dict(s)))) == s
    for bad in (None, {}, {"boundary": "x"}, [], "s",
                {**h.snapshot_to_dict(s), "totals": {"import": "a"}}):
        assert h.snapshot_from_dict(bad) is None


# ---- deltas, resets, gaps -----------------------------------------------------

def rec(a, b):
    return h.block_record(a, b, 15, BR)


def test_delta_per_block_and_read_times():
    a = snap(B0, readings={"import": [100.0, 5.0], "export": [10.0]})
    b = snap(B0 + STEP, late=40, readings={"import": [100.25, 5.5], "export": [10.0]})
    r = rec(a, b)
    assert r["import_kwh"] == pytest.approx(0.75)
    assert r["export_kwh"] == 0.0
    assert r["block_start"] == "2026-09-30T10:00:00+00:00"
    assert r["local_date"] == "2026-09-30"
    assert r["start_read_at"] == B0.isoformat()
    assert r["snapshot_read_at"] == (B0 + STEP + timedelta(seconds=40)).isoformat()
    assert r["complete"] is True and r["schema"] == 1


def test_negative_delta_counter_reset_is_null_only_for_that_field():
    a = snap(B0, readings={"import": [100.0], "export": [10.0]})
    b = snap(B0 + STEP, readings={"import": [3.0], "export": [10.5]})
    r = rec(a, b)
    assert r["import_kwh"] is None and r["from_net_kwh"] is None
    assert r["export_kwh"] == 0.5
    assert r["complete"] is False          # a configured quantity has no delta


def test_unreadable_sensor_gives_null_and_incomplete():
    a = snap(B0, readings={"import": [1.0], "export": [1.0]})
    b = snap(B0 + STEP, readings={"import": [None], "export": [1.2]})
    r = rec(a, b)
    assert r["import_kwh"] is None and r["export_kwh"] == pytest.approx(0.2)
    assert r["complete"] is False


def test_unconfigured_quantities_do_not_make_a_record_incomplete():
    a = snap(B0, readings={"import": [1.0]})
    b = snap(B0 + STEP, readings={"import": [2.0]})
    r = rec(a, b)
    assert r["complete"] is True and r["solar_kwh"] is None


def test_first_snapshot_writes_nothing():
    assert h.records_between(None, snap(), 15, BR) == []


def test_same_or_older_boundary_writes_nothing():
    assert h.records_between(snap(B0), snap(B0), 15, BR) == []
    assert h.records_between(snap(B0 + STEP), snap(B0), 15, BR) == []


def test_adjacent_snapshots_give_exactly_one_record():
    out = h.records_between(snap(B0, readings={"import": [1.0]}),
                            snap(B0 + STEP, readings={"import": [1.5]}), 15, BR)
    assert len(out) == 1 and out[0]["import_kwh"] == 0.5


def test_gap_gives_null_records_never_spreads_one_delta():
    a = snap(B0, readings={"import": [1.0]})
    b = snap(B0 + 4 * STEP, readings={"import": [9.0]})
    out = h.records_between(a, b, 15, BR)
    assert [r["block_start"] for r in out] == [
        (B0 + i * STEP).isoformat() for i in range(4)]
    for r in out:
        assert r["import_kwh"] is None and r["load_kwh"] is None
        assert r["complete"] is False and r["snapshot_read_at"] is None


def test_long_gap_is_capped_to_the_most_recent_blocks():
    a = snap(B0, readings={"import": [1.0]})
    b = snap(B0 + 500 * STEP, readings={"import": [9.0]})
    out = h.records_between(a, b, 15, BR)
    assert len(out) == h.MAX_GAP_RECORDS
    assert out[-1]["block_start"] == (B0 + 499 * STEP).isoformat()


def test_block_context_comes_from_the_start_snapshot():
    a = snap(B0, readings={"import": [1.0]}, forecast_solar_kwh=0.4,
             consumption_price=0.21, injection_price=0.03, soc_percent=55.0)
    b = snap(B0 + STEP, readings={"import": [2.0]}, forecast_solar_kwh=9.9,
             consumption_price=9.9, injection_price=9.9, soc_percent=1.0)
    r = rec(a, b)
    assert (r["forecast_solar_kwh"], r["consumption_price"],
            r["injection_price"], r["soc_percent"]) == (0.4, 0.21, 0.03, 55.0)


# ---- charge at the end of the block -------------------------------------------

def test_end_charge_comes_from_the_closing_snapshot():
    a = snap(B0, readings={"import": [1.0]}, soc_percent=40.0)
    b = snap(B0 + STEP, late=1, readings={"import": [2.0]}, soc_percent=55.5)
    r = rec(a, b)
    assert r["soc_percent"] == 40.0 and r["soc_end_percent"] == 55.5


def test_consecutive_blocks_chain_end_to_start():
    s0 = snap(B0, readings={"import": [1.0]}, soc_percent=40.0)
    s1 = snap(B0 + STEP, readings={"import": [2.0]}, soc_percent=55.0)
    s2 = snap(B0 + 2 * STEP, readings={"import": [3.0]}, soc_percent=61.0)
    r1, r2 = rec(s0, s1), rec(s1, s2)
    assert (r1["soc_percent"], r1["soc_end_percent"]) == (40.0, 55.0)
    assert (r2["soc_percent"], r2["soc_end_percent"]) == (55.0, 61.0)


def test_end_charge_is_null_when_the_closing_reading_is_unavailable():
    a = snap(B0, readings={"import": [1.0]}, soc_percent=40.0)
    b = snap(B0 + STEP, readings={"import": [2.0]}, soc_percent=None)
    r = rec(a, b)
    assert r["soc_percent"] == 40.0 and r["soc_end_percent"] is None


def test_start_charge_null_does_not_hide_the_end_charge():
    a = snap(B0, readings={"import": [1.0]}, soc_percent=None)
    b = snap(B0 + STEP, readings={"import": [2.0]}, soc_percent=7.0)
    r = rec(a, b)
    assert r["soc_percent"] is None and r["soc_end_percent"] == 7.0


def test_gap_records_have_null_charges_and_the_field():
    a = snap(B0, readings={"import": [1.0]}, soc_percent=40.0)
    b = snap(B0 + 3 * STEP, readings={"import": [9.0]}, soc_percent=50.0)
    out = h.records_between(a, b, 15, BR)
    assert len(out) == 3
    assert all("soc_end_percent" in r and r["soc_end_percent"] is None
               and r["soc_percent"] is None for r in out)


def test_every_record_has_the_field_and_the_schema_is_unchanged():
    assert "soc_end_percent" in h.RECORD_FIELDS and h.SCHEMA == 1
    r = rec(snap(B0, readings={"import": [1.0]}),
            snap(B0 + STEP, readings={"import": [2.0]}))
    assert set(r) == set(h.RECORD_FIELDS)


def test_end_charge_survives_the_snapshot_file_round_trip():
    s0 = snap(B0, readings={"import": [1.0]}, soc_percent=40.0)
    s1 = snap(B0 + STEP, readings={"import": [2.0]}, soc_percent=55.0)
    restored = h.snapshot_from_dict(json.loads(json.dumps(h.snapshot_to_dict(s1))))
    assert rec(s0, restored)["soc_end_percent"] == 55.0


def test_jsonl_round_trip_and_old_records_without_the_field():
    r = rec(snap(B0, readings={"import": [1.0]}, soc_percent=40.0),
            snap(B0 + STEP, readings={"import": [2.0]}, soc_percent=55.0))
    old = {k: v for k, v in r.items() if k != "soc_end_percent"}
    old["load_kwh"] = 0.5
    text = h.to_line(r) + chr(10) + json.dumps(old) + chr(10)
    out, bad = h.parse_lines(text)
    assert bad == 0 and out[0]["soc_end_percent"] == 55.0
    assert "soc_end_percent" not in out[1]
    r["load_kwh"] = 0.25
    got = h.usage_series(out + [{**r, "block_start": (B0 + STEP).isoformat()}])
    assert [v for _, v in got] == [0.5, 0.25]
    cal = h.solar_calibration(out, B0, 4, 0.8, BR, 15)     # tolerates old rows
    assert cal["ratios"] == {}


# ---- load derivation -------------------------------------------------------------

def test_derived_load_formula():
    # import 2 - export 0.5 + solar 3 + discharge 1 - charge 0.25 = 5.25
    a = snap(B0, readings=full(0, 0, 0, 0, 0))
    b = snap(B0 + STEP, readings=full(2, 0.5, 3, 0.25, 1))
    r = rec(a, b)
    assert r["load_kwh"] == pytest.approx(5.25) and r["load_source"] == "derived"


@pytest.mark.parametrize("missing", ["import", "export", "solar",
                                     "battery_charge", "battery_discharge"])
def test_any_unknown_term_makes_derived_load_null(missing):
    r0, r1 = full(0, 0, 0, 0, 0), full(1, 1, 1, 1, 1)
    del r0[missing], r1[missing]
    r = rec(snap(B0, readings=r0), snap(B0 + STEP, readings=r1))
    assert r["load_kwh"] is None and r["load_source"] is None


def test_solar_alone_does_not_give_load_with_a_battery_unknown():
    a = snap(B0, readings={"import": [0], "export": [0], "solar": [0]})
    b = snap(B0 + STEP, readings={"import": [1], "export": [0], "solar": [1]})
    assert rec(a, b)["load_kwh"] is None


def test_measured_load_wins_over_derived():
    a = snap(B0, readings=full(0, 0, 0, 0, 0, load=0))
    b = snap(B0 + STEP, readings=full(2, 0.5, 3, 0.25, 1, load=4.0))
    r = rec(a, b)
    assert r["load_kwh"] == 4.0 and r["load_source"] == "measured"


def test_measured_load_reset_falls_back_to_derived():
    a = snap(B0, readings=full(0, 0, 0, 0, 0, load=50))
    b = snap(B0 + STEP, readings=full(1, 0, 0, 0, 0, load=1))
    r = rec(a, b)
    assert r["load_kwh"] == 1.0 and r["load_source"] == "derived"


def test_derived_load_never_negative():
    a = snap(B0, readings=full(0, 0, 0, 0, 0))
    b = snap(B0 + STEP, readings=full(0, 0.001, 0, 0, 0))
    assert rec(a, b)["load_kwh"] == 0.0


def test_load_measured_alone_with_nothing_else():
    r = rec(snap(B0, readings={"load": [1.0]}),
            snap(B0 + STEP, readings={"load": [1.4]}))
    assert r["load_kwh"] == pytest.approx(0.4) and r["from_net_kwh"] is None
    assert r["load_from_solar_kwh"] is None and r["split_method"] is None


# ---- split -----------------------------------------------------------------------

@pytest.mark.parametrize("load, solar, discharge, expected", [
    (5.0, 3.0, 1.0, (3.0, 1.0, 1.0)),
    (2.0, 3.0, 1.0, (2.0, 0.0, 0.0)),        # solar covers it all
    (4.0, 0.0, 9.0, (0.0, 4.0, 0.0)),        # battery covers the rest
    (4.0, 0.0, 0.0, (0.0, 0.0, 4.0)),        # night, no battery: all net
    (0.0, 1.0, 1.0, (0.0, 0.0, 0.0)),
])
def test_split_priority_rule(load, solar, discharge, expected):
    assert h.split_load(load, solar, discharge) == pytest.approx(expected)


@pytest.mark.parametrize("args", [(None, 1.0, 1.0), (1.0, None, 1.0),
                                  (1.0, 1.0, None)])
def test_split_needs_all_three_inputs(args):
    assert h.split_load(*args) == (None, None, None)


def test_split_in_record_sums_to_load_and_names_method():
    a = snap(B0, readings=full(0, 0, 0, 0, 0))
    b = snap(B0 + STEP, readings=full(2, 0.5, 3, 0.25, 1))
    r = rec(a, b)
    parts = (r["load_from_solar_kwh"], r["load_from_battery_kwh"],
             r["load_from_net_kwh"])
    assert sum(parts) == pytest.approx(r["load_kwh"])
    assert r["split_method"] == "priority_v1"
    assert r["from_net_kwh"] == 2.0                  # = import


# ---- serialisation ---------------------------------------------------------------

def test_jsonl_roundtrip_all_fields_present_and_one_line():
    a = snap(B0, readings=full(0, 0, 0, 0, 0), forecast_solar_kwh=0.3)
    b = snap(B0 + STEP, late=12, readings=full(2, 0.5, 3, 0.25, 1))
    r = rec(a, b)
    line = h.to_line(r)
    assert "\n" not in line and list(r) == list(h.RECORD_FIELDS)
    back, bad = h.parse_lines(line + "\n" + line + "\n")
    assert bad == 0 and back == [r, r]


def test_parse_lines_skips_and_counts_corrupt_lines():
    good = h.to_line(rec(snap(B0, readings={"import": [0.0]}),
                         snap(B0 + STEP, readings={"import": [1.0]})))
    text = "\n".join([good, "{truncated", "", "[1,2]", '{"x":1}',
                      '{"block_start":"nope"}', good])
    back, bad = h.parse_lines(text)
    assert len(back) == 2 and bad == 4
    assert h.parse_lines(None) == ([], 0)


# ---- aggregation -----------------------------------------------------------------

def mk(start, load, **kw):
    return {"block_start": start.isoformat(), "load_kwh": load, **kw}


def test_usage_series_orders_filters_and_skips_null_load():
    recs = [mk(B0 + STEP, 0.4), mk(B0, 0.3), mk(B0 + 2 * STEP, None),
            mk(B0 + 3 * STEP, "x"), mk(B0 + 4 * STEP, -1.0),
            {"load_kwh": 1.0}, {"block_start": "bad", "load_kwh": 1.0}]
    assert h.usage_series(recs) == [(B0, 0.3), (B0 + STEP, 0.4)]
    assert h.usage_series(recs, since=B0 + STEP) == [(B0 + STEP, 0.4)]


def test_usage_series_duplicate_block_keeps_last():
    assert h.usage_series([mk(B0, 1.0), mk(B0, 2.0)]) == [(B0, 2.0)]


def test_usage_series_dst_fall_back_day_has_100_distinct_blocks():
    start = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)      # 00:00 local 25 Oct
    recs = [mk(start + i * STEP, 0.1) for i in range(100)]
    out = h.usage_series(recs)
    assert len(out) == 100 and len({t for t, _ in out}) == 100
    local_times = [t.astimezone(BR).strftime("%H:%M") for t, _ in out]
    assert local_times.count("02:00") == 2          # repeated hour, two instants


def test_usage_series_feeds_series_usage_profile(site_config):
    import series
    start = datetime(2026, 9, 30, tzinfo=BR)
    recs = [mk(start - timedelta(days=1) + i * STEP, 0.25) for i in range(96)]
    out = series.usage_profile(h.usage_series(recs), site_config, start,
                               start + timedelta(hours=24))
    assert len(out) == 96 and out[0].expected_kwh == pytest.approx(0.25)


# ---- solar realisation ratio ------------------------------------------------------

def sol(i, actual, forecast):
    return mk(B0 + i * STEP, None, solar_kwh=actual, forecast_solar_kwh=forecast)


def test_ratio_is_sum_actual_over_sum_forecast():
    recs = [sol(0, 0.8, 1.0), sol(1, 1.6, 2.0), sol(2, 2.4, 3.0)]
    assert h.solar_realisation_ratio(recs, 4) == pytest.approx(0.8)


@pytest.mark.parametrize("recs", [
    [],
    [sol(0, None, 1.0), sol(1, 1.0, None)],         # never both
    [sol(0, 0.0, 0.0), sol(1, 0.0, 0.0)],           # zero forecast
    [sol(0, 0.3, 0.4)],                             # forecast below minimum
])
def test_ratio_none_when_insufficient(recs):
    assert h.solar_realisation_ratio(recs, 4) is None


def test_ratio_ignores_blocks_missing_either_side_and_old_blocks():
    old = mk(B0 - timedelta(weeks=9), None, solar_kwh=100.0,
             forecast_solar_kwh=1.0)
    recs = [old, sol(0, 1.0, 2.0), sol(1, None, 2.0), sol(2, 3.0, None),
            sol(3, 1.0, 2.0)]
    assert h.solar_realisation_ratio(recs, 4) == pytest.approx(0.5)


def test_ratio_zero_forecast_blocks_do_not_break_the_sum():
    recs = [sol(0, 0.1, 0.0), sol(1, 1.5, 2.0)]
    assert h.solar_realisation_ratio(recs, 4) == pytest.approx(0.8)
