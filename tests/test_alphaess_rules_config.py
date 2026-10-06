"""V7 (no battery reading) in rules, and the new battery.* keys in config."""
import pytest

import battery
from config import ConfigError, from_dict
from rules import establish_vetoes
from test_config import base
from test_rules import FLAT, NEG, SHAVE, go, price


# ---- V7 -----------------------------------------------------------------------

def test_v7_quiet_by_default(site_config):
    _, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config, usage_history_available=True)
    assert "V7" not in fired


def test_v7_forbids_grid_charge_and_export_only(site_config):
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config, usage_history_available=True, soc_known=False)
    assert fired == ["V7"] and forbidden == {"grid_charge", "export"}
    assert "discharge" not in forbidden


def test_v7_skips_the_checks_that_need_a_reading(site_config):
    _, fired = establish_vetoes(
        battery.from_percent(0, site_config), price(0, 0.2, 0.05),
        site_config, usage_history_available=True, soc_known=False)
    assert fired == ["V7"]                     # neither V1 nor V5


def test_v7_blocks_charging(site_config):
    d = go(site_config, [NEG] * 3, soc_known=False)
    assert d.suppressed == [("S1", "charge", "V7"), ("S4", "charge", "V7")]
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.reasoning.startswith("hold: no battery reading")


def test_v7_does_not_stop_peak_shaving(site_config):
    d = go(site_config, [FLAT] * 3, g=SHAVE, soc_known=False)
    assert (d.selector, d.action) == ("S0", "discharge")


def test_unknown_placeholder_is_stubbed_and_unusable(site_config):
    b = battery.unknown(site_config)
    assert b.is_stubbed and b.usable_kwh == 0.0
    assert b.charge_percent == site_config.reserve_percent


# ---- battery.soc_sensor ---------------------------------------------------------

def test_soc_sensor_defaults_to_none_and_is_read():
    assert from_dict(base()).soc_sensor is None
    c = from_dict(base(battery__soc_sensor="sensor.alphaess_soc_battery"))
    assert c.soc_sensor == "sensor.alphaess_soc_battery"


@pytest.mark.parametrize("bad", ["soc", "Sensor.x", "sensor.a.b", 5, ""])
def test_soc_sensor_must_be_an_entity_id(bad):
    with pytest.raises(ConfigError, match="battery.soc_sensor"):
        from_dict(base(battery__soc_sensor=bad))


def test_soc_sensor_is_not_in_the_fingerprint():
    a = from_dict(base())
    b = from_dict(base(battery__soc_sensor="sensor.alphaess_soc_battery"))
    assert a.fingerprint() == b.fingerprint()
