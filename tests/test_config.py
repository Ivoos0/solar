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
    ("alerts__notify_service", "", "notify_service"),
    ("alerts__notify_service", "Gmail_Alert", "notify_service"),
    ("alerts__notify_service", "gmail-alert", "notify_service"),
    ("alerts__notify_service", "notify.battery_alert", "notify_service"),
    ("alerts__notify_service", "gmail alert", "notify_service"),
    ("alerts__notify_service", "battery_alert\n", "notify_service"),
    ("alerts__notify_service", 5, "notify_service"),
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
                    ("usage__grouping", "day_type"),
                    ("usage__recency_weighting", "none")]:
        assert from_dict(base(**{path: v})).fingerprint() != ref, path


def test_fingerprint_ignores_other_fields():
    ref = from_dict(base()).fingerprint()
    for path, v in [("prices__consumption_multiplier", 2.0),
                    ("battery__capacity_kwh", 20.0),
                    ("alerts__address", "z@z.z"),
                    ("alerts__notify_service", "other_notifier"),
                    ("battery__reserve_percent", 20),
                    ("capacity_tariff__enabled", False),
                    ("capacity_tariff__stay_under_percent", 50),
                    ("timing__forecast_refresh_minutes", 30)]:
        assert from_dict(base(**{path: v})).fingerprint() == ref, path


def test_usage_defaults(site_config):
    assert site_config.usage_history_weeks == 4
    assert site_config.usage_grouping == "same_weekday"
    assert site_config.usage_recency_weighting == "linear"


def test_usage_section_parsed():
    c = from_dict(base(usage__history_weeks=6, usage__grouping="day_type"))
    assert c.usage_history_weeks == 6 and c.usage_grouping == "day_type"


@pytest.mark.parametrize("key,bad_value", [
    ("history_weeks", 0), ("history_weeks", -2), ("history_weeks", 2.5),
    ("history_weeks", "four"), ("grouping", "weekly"), ("grouping", 3),
    ("recency_weighting", "exponential"), ("recency_weighting", 1),
    ("recency_weighting", True),
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
    assert from_dict(base(usage__recency_weighting="none")).fingerprint() != ref
    # explicit defaults equal the implicit ones
    assert from_dict(base(usage__history_weeks=4,
                          usage__grouping="same_weekday",
                          usage__recency_weighting="linear")).fingerprint() == ref


def test_recency_weighting_parsed():
    assert from_dict(base(usage__recency_weighting="none")
                     ).usage_recency_weighting == "none"


def test_stay_under_percent_default_is_80():
    assert from_dict(base()).stay_under_percent == 80.0


@pytest.mark.parametrize("v", [0.5, 80, 100, 100.0])
def test_stay_under_percent_accepts_valid(v):
    cfg = from_dict(base(capacity_tariff__stay_under_percent=v))
    assert cfg.stay_under_percent == v


def test_notify_service_defaults_to_battery_alert(site_config):
    assert site_config.notify_service == "battery_alert"


def test_notify_service_is_read_from_alerts_section():
    c = from_dict(base(alerts__notify_service="smtp_2_alerts"))
    assert c.notify_service == "smtp_2_alerts"


# ---- inverter.type (selects pyscript/modules/inverter_<type>.py) --------------

def test_inverter_type_defaults_to_logging(site_config):
    assert site_config.inverter_type == "logging"


@pytest.mark.parametrize("raw", [
    {},                                  # no inverter section at all
    {"inverter": None},                  # empty section
    {"inverter": {}},
    {"inverter": {"type": None}},        # `type:` left blank / null
    {"inverter": {"type": "none"}},
    {"inverter": {"type": "None"}},
    {"inverter": {"type": "logging"}},
])
def test_inverter_type_nothing_or_none_means_logging(raw):
    assert from_dict({**base(), **raw}).inverter_type == "logging"


@pytest.mark.parametrize("name", ["alphaess", "alpha_ess2", "hf2211"])
def test_inverter_type_accepts_driver_names(name):
    assert from_dict(base(inverter__type=name)).inverter_type == name


@pytest.mark.parametrize("value", [
    "", " ", "AlphaESS", "alpha-ess", "alpha ess", "../etc/passwd", "a.b",
    "alphaess\n", "inverter_alphaess.py", 5, True, ["alphaess"]])
def test_inverter_type_rejects_bad_names(value):
    with pytest.raises(ConfigError) as exc:
        from_dict(base(inverter__type=value))
    assert "inverter.type" in str(exc.value)


def test_inverter_section_must_be_a_mapping():
    with pytest.raises(ConfigError):
        from_dict({**base(), "inverter": "alphaess"})


def test_inverter_type_is_not_in_the_cache_fingerprint():
    ref = from_dict(base()).fingerprint()
    assert from_dict(base(inverter__type="alphaess")).fingerprint() == ref
