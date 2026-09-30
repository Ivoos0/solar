import pytest

from config import ConfigError, from_dict


def base(**over):
    raw = {"battery": {"capacity_kwh": 10.0},
           "alerts": {"address": "a@b.c"}}
    for path, v in over.items():
        sec, key = path.split("__")
        raw.setdefault(sec, {})[key] = v
    return raw


def test_defaults_applied(site_config):
    c = site_config
    assert c.consumption_multiplier == 1.07
    assert c.injection_offset == -0.011
    assert c.reserve_percent == 10.0
    assert c.block_minutes == 15
    assert c.timezone == "Europe/Brussels"
    assert c.array_azimuth == -10
    assert c.offtake_sensor == "sensor.slimmelezer_power_consumed"


def test_full_parse_flattens():
    c = from_dict({
        "prices": {"consumption_multiplier": 1.2},
        "battery": {"capacity_kwh": 12, "reserve_percent": 5},
        "solar": {"kwp": 6.0, "azimuth": 20},
        "timing": {"block_minutes": 30},
        "alerts": {"address": "x@y.z", "realert_minutes": 30},
        "capacity_tariff": {"enabled": False,
                            "quarter_hour_average_mode": "running"},
        "timezone": "Europe/Paris",
        "unknown_key": 1,
    })
    assert c.consumption_multiplier == 1.2
    assert c.capacity_kwh == 12
    assert c.array_kwp == 6.0 and c.array_azimuth == 20
    assert c.block_minutes == 30 and c.realert_minutes == 30
    assert c.capacity_enabled is False
    assert c.quarter_hour_average_mode == "running"
    assert c.timezone == "Europe/Paris"


@pytest.mark.parametrize("path,value,field", [
    ("battery__capacity_kwh", 0, "capacity_kwh"),
    ("battery__reserve_percent", 100, "reserve_percent"),
    ("battery__reserve_percent", -1, "reserve_percent"),
    ("battery__round_trip_efficiency", 0, "round_trip_efficiency"),
    ("battery__round_trip_efficiency", 1.1, "round_trip_efficiency"),
    ("timing__block_minutes", 7, "block_minutes"),
    ("timing__block_minutes", 0, "block_minutes"),
    ("solar__azimuth", 181, "azimuth"),
    ("solar__declination", 91, "declination"),
    ("solar__declination", -1, "declination"),
    ("alerts__address", "  ", "address"),
    ("battery__max_charge_kw", 0, "max_charge_kw"),
    ("battery__max_discharge_kw", 0, "max_discharge_kw"),
    ("capacity_tariff__quarter_hour_average_mode", "bogus",
     "quarter_hour_average_mode"),
    ("capacity_tariff__peak_averaging_months", 0, "peak_averaging_months"),
    ("capacity_tariff__stay_under_percent", 0, "stay_under_percent"),
    ("capacity_tariff__stay_under_percent", -5, "stay_under_percent"),
    ("capacity_tariff__stay_under_percent", 100.5, "stay_under_percent"),
    ("capacity_tariff__stay_under_percent", "80", "stay_under_percent"),
    ("capacity_tariff__stay_under_percent", True, "stay_under_percent"),
])
def test_validation_rejects(path, value, field):
    with pytest.raises(ConfigError) as e:
        from_dict(base(**{path: value}))
    assert field in str(e.value)
    assert repr(value) in str(e.value)


def test_required_fields_missing():
    with pytest.raises(ConfigError) as e:
        from_dict({})
    assert "capacity_kwh" in str(e.value) and "address" in str(e.value)


def test_all_errors_reported_at_once():
    with pytest.raises(ConfigError) as e:
        from_dict(base(battery__capacity_kwh=-1, timing__block_minutes=7))
    msg = str(e.value)
    assert "capacity_kwh" in msg and "block_minutes" in msg


def test_non_numeric_rejected():
    with pytest.raises(ConfigError) as e:
        from_dict(base(battery__capacity_kwh="ten"))
    assert "capacity_kwh" in str(e.value)


def test_fingerprint_stable_and_short(site_config):
    fp = site_config.fingerprint()
    assert len(fp) == 8
    int(fp, 16)
    assert fp == from_dict(base()).fingerprint()


def test_fingerprint_sensitivity():
    ref = from_dict(base()).fingerprint()
    for path, v in [("timing__block_minutes", 30), ("solar__latitude", 50.0),
                    ("solar__longitude", 4.0), ("solar__kwp", 9.0),
                    ("solar__declination", 40), ("solar__azimuth", 0),
                    ("usage__history_weeks", 8),
                    ("usage__grouping", "day_type")]:
        assert from_dict(base(**{path: v})).fingerprint() != ref, path


def test_fingerprint_ignores_other_fields():
    ref = from_dict(base()).fingerprint()
    for path, v in [("prices__consumption_multiplier", 2.0),
                    ("battery__capacity_kwh", 20.0),
                    ("alerts__address", "z@z.z"),
                    ("battery__reserve_percent", 20),
                    ("capacity_tariff__enabled", False),
                    ("capacity_tariff__stay_under_percent", 50),
                    ("timing__forecast_refresh_minutes", 30)]:
        assert from_dict(base(**{path: v})).fingerprint() == ref, path


def test_usage_defaults(site_config):
    assert site_config.usage_history_weeks == 4
    assert site_config.usage_grouping == "same_weekday"


def test_usage_section_parsed():
    c = from_dict(base(usage__history_weeks=6, usage__grouping="day_type"))
    assert c.usage_history_weeks == 6 and c.usage_grouping == "day_type"


@pytest.mark.parametrize("key,bad_value", [
    ("history_weeks", 0), ("history_weeks", -2), ("history_weeks", 2.5),
    ("history_weeks", "four"), ("grouping", "weekly"), ("grouping", 3),
])
def test_usage_validation_rejects(key, bad_value):
    with pytest.raises(ConfigError) as e:
        from_dict(base(**{"usage__" + key: bad_value}))
    assert "usage_" in str(e.value)


def test_usage_errors_collected_with_others():
    with pytest.raises(ConfigError) as e:
        from_dict(base(usage__history_weeks=0, usage__grouping="x",
                       battery__capacity_kwh=-1))
    msg = str(e.value)
    assert "usage_history_weeks" in msg and "usage_grouping" in msg
    assert "capacity_kwh" in msg


def test_fingerprint_changes_per_usage_setting():
    ref = from_dict(base()).fingerprint()
    assert from_dict(base(usage__history_weeks=5)).fingerprint() != ref
    assert from_dict(base(usage__grouping="day_type")).fingerprint() != ref
    # explicit defaults equal the implicit ones
    assert from_dict(base(usage__history_weeks=4,
                          usage__grouping="same_weekday")).fingerprint() == ref


def test_stay_under_percent_default_is_80():
    assert from_dict(base()).stay_under_percent == 80.0


@pytest.mark.parametrize("v", [0.5, 80, 100, 100.0])
def test_stay_under_percent_accepts_valid(v):
    cfg = from_dict(base(capacity_tariff__stay_under_percent=v))
    assert cfg.stay_under_percent == v
