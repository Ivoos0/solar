from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import cache
import config
from cache import CacheError

BRU = ZoneInfo("Europe/Brussels")
NOW = datetime(2026, 9, 29, 14, 30, tzinfo=BRU)


def payload(cfg, **over):
    d = {"kind": "solar", "computed_at": "2026-09-29T14:00:00+02:00",
         "source": "sensor.forecast_solar_estimate",
         "config_fingerprint": cfg.fingerprint(),
         "blocks": [{"block_start": "2026-09-29T14:00:00+02:00",
                     "expected_kwh": 1.82, "source_resolution_minutes": 60}]}
    d.update(over)
    return d


def test_parse_ok(site_config):
    s = cache.parse_series(payload(site_config), "solar")
    assert s.kind == "solar" and s.computed_at.utcoffset() == timedelta(hours=2)
    assert cache.to_dict(s) == payload(site_config)


@pytest.mark.parametrize("bad", [None, [], "x", {}, {"kind": "solar"}])
def test_parse_malformed(bad):
    with pytest.raises(CacheError):
        cache.parse_series(bad)


def test_parse_rejects_bad_fields(site_config):
    for over in ({"kind": "other"}, {"computed_at": "garbage"},
                 {"computed_at": "2026-09-29T14:00:00"}, {"blocks": "x"},
                 {"config_fingerprint": ""}):
        with pytest.raises(CacheError):
            cache.parse_series(payload(site_config, **over))


def test_wrong_kind_rejected(site_config):
    with pytest.raises(CacheError):
        cache.parse_series(payload(site_config), "usage")


def test_age_and_staleness(site_config):
    s = cache.parse_series(payload(site_config))
    assert cache.age_minutes(s, NOW) == 30
    assert not cache.is_stale(s, NOW, 30)
    assert cache.is_stale(s, NOW, 29)
    with pytest.raises(CacheError):
        cache.age_minutes(s, datetime(2026, 9, 29, 14, 30))


def test_stale_bounds_from_config(site_config):
    assert cache.stale_minutes_for("solar", site_config) == 120
    assert cache.stale_minutes_for("usage", site_config) == 2880
    with pytest.raises(CacheError):
        cache.stale_minutes_for("x", site_config)


def test_day_rollover_uses_local_date(site_config):
    # 23:30 UTC on the 28th is 01:30 local on the 29th: same local day as 08:00
    s = cache.parse_series(payload(site_config, computed_at="2026-09-28T23:30:00+00:00"))
    now = datetime(2026, 9, 29, 8, 0, tzinfo=BRU)
    assert not cache.is_from_previous_day(s, now, "Europe/Brussels")
    # 23:30 local on the 28th vs 00:10 local on the 29th (still 22:10 UTC the 28th)
    s2 = cache.parse_series(payload(site_config, computed_at="2026-09-28T23:30:00+02:00"))
    now2 = datetime(2026, 9, 28, 22, 10, tzinfo=timezone.utc)
    assert cache.is_from_previous_day(s2, now2, "Europe/Brussels")
    # same local date, though UTC dates differ from local ones
    assert not cache.is_from_previous_day(
        s2, datetime(2026, 9, 28, 21, 40, tzinfo=timezone.utc), "Europe/Brussels")


def test_fingerprint(site_config):
    s = cache.parse_series(payload(site_config))
    assert cache.fingerprint_matches(s, site_config)
    s2 = cache.parse_series(payload(site_config, config_fingerprint="deadbeef"))
    assert not cache.fingerprint_matches(s2, site_config)


def test_invalidation_reason(site_config):
    ok = cache.parse_series(payload(site_config))
    assert cache.invalidation_reason(ok, NOW, site_config, "solar") is None
    assert cache.invalidation_reason(ok, NOW, site_config, "usage") == "kind mismatch"
    old = cache.parse_series(payload(site_config, computed_at="2026-09-28T14:00:00+02:00"))
    assert "previous day" in cache.invalidation_reason(old, NOW, site_config)
    fp = cache.parse_series(payload(site_config, config_fingerprint="x"))
    assert "fingerprint" in cache.invalidation_reason(fp, NOW, site_config)


def test_stale_is_not_invalid(site_config):
    s = cache.parse_series(payload(site_config, computed_at="2026-09-29T11:00:00+02:00"))
    assert cache.invalidation_reason(s, NOW, site_config) is None
    assert cache.is_stale(s, NOW, 120)


def test_evaluate_fresh(site_config):
    st, s, d = cache.evaluate(payload(site_config), "solar", NOW, site_config)
    assert st == "fresh" and s is not None and d == "cache_age_solar=30m"


def test_evaluate_stale_gives_degraded_note(site_config):
    p = payload(site_config, computed_at="2026-09-29T11:00:00+02:00")
    st, s, d = cache.evaluate(p, "solar", NOW, site_config)
    assert st == "stale" and s is not None
    assert d == "cache_age_solar=3h30m"


def test_usage_fresh_within_bound(site_config):
    p = payload(site_config, kind="usage", computed_at="2026-09-29T00:30:00+02:00")
    assert cache.evaluate(p, "usage", NOW, site_config)[0] == "fresh"


@pytest.mark.parametrize("data", [None, "junk", {}, {"kind": "solar"}, 5, []])
def test_evaluate_miss_never_raises(site_config, data):
    st, s, d = cache.evaluate(data, "solar", NOW, site_config)
    assert st == "invalid" and s is None and d


def test_evaluate_invalid_cases(site_config):
    for over in ({"computed_at": "2026-09-28T14:00:00+02:00"},
                 {"config_fingerprint": "nope"}, {"kind": "usage"}):
        res = cache.evaluate(payload(site_config, **over), "solar", NOW, site_config)
        assert res[0] == "invalid"


def test_evaluate_naive_now_is_invalid_not_raise(site_config):
    res = cache.evaluate(payload(site_config), "solar", datetime(2026, 9, 29, 14, 30), site_config)
    assert res[0] == "invalid"


def _at(cfg, iso, kind="solar"):
    return cache.parse_series(payload(cfg, kind=kind, computed_at=iso))


def _cfg(**sections):
    raw = {"battery": {"capacity_kwh": 10.0}, "alerts": {"address": "o@example.com"}}
    raw.update(sections)
    return config.from_dict(raw)


def test_age_description(site_config):
    assert cache.age_description(_at(site_config, "2026-09-29T14:16:00+02:00"), NOW) == "14m"
    assert cache.age_description(_at(site_config, "2026-09-29T11:18:00+02:00"), NOW) == "3h12m"
    assert cache.age_description(_at(site_config, "2026-09-27T11:18:00+02:00"), NOW) == "2d3h"
    future = _at(site_config, "2026-09-29T15:00:00+02:00")
    assert cache.age_description(future, NOW) == "0m"


def test_age_marker_contract_and_fresh(site_config):
    s = _at(site_config, "2026-09-29T11:18:00+02:00")
    assert cache.age_marker(s, NOW) == "cache_age_solar=3h12m"
    u = _at(site_config, "2026-09-29T00:30:00+02:00", "usage")
    assert cache.age_marker(u, NOW) == "cache_age_usage=14h0m"
    # reachable for a fresh series too
    st, _, d = cache.evaluate(payload(site_config, computed_at="2026-09-29T14:16:00+02:00"),
                              "solar", NOW, site_config)
    assert st == "fresh" and d == "cache_age_solar=14m"


def test_disposable_roundtrip_and_miss(site_config):
    original = _at(site_config, "2026-09-29T11:18:00+02:00")
    st, rebuilt, _ = cache.evaluate(cache.to_dict(original), "solar", NOW, site_config)
    assert st == "stale"
    assert rebuilt == original
    assert rebuilt.computed_at == original.computed_at
    assert rebuilt.computed_at.utcoffset() == original.computed_at.utcoffset()
    assert rebuilt.blocks == original.blocks
    # deleting the cache: a miss yields no series, only a rebuild follows
    st, none, _ = cache.evaluate(None, "solar", NOW, site_config)
    assert st == "invalid" and none is None


def test_price_coefficient_change_keeps_cache_valid(site_config):
    other = _cfg(prices={"consumption_multiplier": 1.5})
    assert other.consumption_multiplier != site_config.consumption_multiplier
    assert other.fingerprint() == site_config.fingerprint()
    st, s, _ = cache.evaluate(payload(site_config), "solar", NOW, other)
    assert st == "fresh" and s is not None


def test_fingerprint_mismatch_invalid_regardless_of_age(site_config):
    for iso in ("2026-09-29T14:29:00+02:00", "2026-09-29T08:00:00+02:00"):  # fresh, stale
        p = payload(site_config, computed_at=iso, config_fingerprint="deadbeef")
        st, s, d = cache.evaluate(p, "solar", NOW, site_config)
        assert st == "invalid" and s is None and "fingerprint" in d
    # a real change (block_minutes) also invalidates a same-day fresh series
    changed = _cfg(timing={"block_minutes": 30})
    assert changed.fingerprint() != site_config.fingerprint()
    assert cache.evaluate(payload(site_config), "solar", NOW, changed)[0] == "invalid"


def test_solar_staleness_boundaries_via_config(site_config):
    fresh = payload(site_config, computed_at="2026-09-29T12:31:00+02:00")  # 1h59m
    stale = payload(site_config, computed_at="2026-09-29T12:29:00+02:00")  # 2h01m
    assert cache.evaluate(fresh, "solar", NOW, site_config)[0] == "fresh"
    st, s, d = cache.evaluate(stale, "solar", NOW, site_config)
    assert st == "stale" and s is not None and d == "cache_age_solar=2h1m"


def test_usage_staleness_bounds_and_day_rollover_precedence(site_config):
    bound = cache.stale_minutes_for("usage", site_config)
    u47 = _at(site_config, "2026-09-27T15:30:00+02:00", "usage")  # 47h
    u49 = _at(site_config, "2026-09-27T13:30:00+02:00", "usage")  # 49h
    assert not cache.is_stale(u47, NOW, bound)
    assert cache.is_stale(u49, NOW, bound)
    # precedence: both are from an earlier local day, so evaluate() says
    # invalid (rebuild), never fresh/stale
    for u in (u47, u49):
        st, s, d = cache.evaluate(cache.to_dict(u), "usage", NOW, site_config)
        assert st == "invalid" and s is None and "previous day" in d
    # under default bounds usage cannot be stale within one local day
    same_day = payload(site_config, kind="usage", computed_at="2026-09-29T00:01:00+02:00")
    assert cache.evaluate(same_day, "usage", NOW, site_config)[0] == "fresh"
    # with a smaller configured bound it goes stale within the day
    small = _cfg(timing={"usage_cache_stale_minutes": 600})
    st, s, d = cache.evaluate(same_day, "usage", NOW, small)
    assert st == "stale" and s is not None and d == "cache_age_usage=14h29m"


def test_purity():
    import inspect
    src = inspect.getsource(cache)
    assert "import json" not in src and "open(" not in src and "datetime.now" not in src


def test_format_minutes():
    assert [cache.format_minutes(m) for m in (-5, 0, 59.9, 60, 80, 1439, 1440, 3000)] == [
        "0m", "0m", "59m", "1h0m", "1h20m", "23h59m", "1d0h", "2d2h"]


def test_as_datetime_and_stamp_age():
    aware = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    now = aware + timedelta(minutes=80)
    assert cache.as_datetime(aware) == aware
    assert cache.as_datetime(aware.isoformat()) == aware
    assert cache.as_datetime(aware.replace(tzinfo=None)) == aware      # naive = UTC
    for bad in (None, "nope", 12, object()):
        assert cache.as_datetime(bad) is None
    assert cache.stamp_age_minutes(aware, now) == 80
    assert cache.stamp_age_minutes(now, aware) == 0                    # never negative
    assert cache.stamp_age_minutes(None, now) is None
    assert cache.stamp_age_minutes(aware, now.replace(tzinfo=None)) is None
