"""Predictive peak warning: the guard e-mails when this quarter-hour is
probably going to cross the ceiling (max(billing floor, month peak)).

Runs the real guard through test_peak_guard's fixtures. A forming peak:
12:07:30, average 4.0 kW so far (running mode), drawing 5.0 kW, ceiling 2.5:
projected = (4.0 * 7.5/60 + 5.0 * 7.5/60) / 0.25 = 4.5 kW.
"""
import builtins
from dataclasses import replace

import pytest

import capacity
import config
from test_peak_guard import (  # noqa: F401  (make_guard is a fixture)
    OFFTAKE, make_guard, _cfg)

HIGH = dict(offtake="5.0", avg="4.0", peak="2.5")
CALM = dict(offtake="0.5", avg="0.5", peak="2.5")


class FakeService:
    def __init__(self):
        self.calls = []
        self.fail = False

    def call(self, domain, name, **kwargs):
        self.calls.append((domain, name, kwargs))
        if self.fail:
            raise RuntimeError("service down")


@pytest.fixture
def wg(make_guard, monkeypatch, tmp_path):
    svc = FakeService()
    monkeypatch.setattr(builtins, "service", svc, raising=False)

    def build(alerts="", capacity_extra=""):
        g = make_guard()
        (tmp_path / "user_config.yaml").write_text(
            "battery:\n  capacity_kwh: 10.0\n"
            "alerts:\n  address: owner@example.com\n%s"
            "capacity_tariff:\n  quarter_hour_average_mode: running\n"
            "  stay_under_percent: 100\n%s" % (alerts, capacity_extra),
            encoding="utf-8")
        g.svc = svc
        return g
    return build


def go(g, h, m, s, **kw):
    g.at(h, m, s)
    g.tick(advance=30.0, **kw)


def warned(g):
    return [c for c in g.svc.calls if c[2]["title"].startswith(
        "Capacity peak warning")]


def two_ticks(g, **kw):
    go(g, 12, 7, 30, **kw)
    go(g, 12, 8, 0, **kw)


# ---- pure ---------------------------------------------------------------------

def test_projection_matches_the_guards_shave_projection(site_config):
    g = capacity.build_state(5.0, 0.0, __import__("datetime").datetime(
        2026, 9, 30, 12, 7, 30), 2.5,
        replace(site_config, quarter_hour_average_mode="running"),
        reported_average_kw=4.0)
    assert capacity.projected_average_kw(g) == pytest.approx(4.5)


def test_constants_are_documented_values():
    assert capacity.PEAK_WARN_SUSTAIN_SECONDS == 20.0


# ---- config ---------------------------------------------------------------------

def test_config_defaults_and_validation():
    raw = {"battery": {"capacity_kwh": 10.0}, "alerts": {"address": "a@b.c"}}
    c = config.from_dict(raw)
    assert c.peak_warning_enabled is True
    assert c.peak_warning_min_interval_minutes == 60
    assert c.peak_warning_ticks == 2
    raw["alerts"].update(peak_warning_enabled=False,
                         peak_warning_min_interval_minutes=5)
    c = config.from_dict(raw)
    assert c.peak_warning_enabled is False
    assert c.peak_warning_min_interval_minutes == 5
    for key, bad in (("peak_warning_enabled", "yes"),
                     ("peak_warning_enabled", 1),
                     ("peak_warning_min_interval_minutes", 0),
                     ("peak_warning_min_interval_minutes", -5),
                     ("peak_warning_min_interval_minutes", 1.5),
                     ("peak_warning_min_interval_minutes", "60"),
                     ("peak_warning_min_interval_minutes", True),
                     ("peak_warning_ticks", 0),
                     ("peak_warning_ticks", -1),
                     ("peak_warning_ticks", 2.0),
                     ("peak_warning_ticks", "2"),
                     ("peak_warning_ticks", True)):
        raw2 = {"battery": {"capacity_kwh": 10.0},
                "alerts": {"address": "a@b.c", key: bad}}
        with pytest.raises(config.ConfigError) as e:
            config.from_dict(raw2)
        assert key in str(e.value), (key, bad)


# ---- guard -----------------------------------------------------------------------

def test_two_consecutive_evaluations_send_one_warning(wg):
    g = wg()
    go(g, 12, 7, 30, **HIGH)
    assert g.svc.calls == []                         # one evaluation: not yet
    go(g, 12, 8, 0, **HIGH)
    sent = warned(g)
    assert len(sent) == 1
    assert sent[0][:2] == ("notify", "battery_alert")
    kw = sent[0][2]
    assert kw["target"] == ["owner@example.com"]
    assert "4.47 kW" in kw["title"] and "2.50 kW" in kw["title"]
    msg = kw["message"]
    assert "PREDICTION" in msg and "projected to average 4.47 kW" in msg
    assert "2.50 kW ceiling" in msg and "Current offtake: 5.00 kW" in msg
    assert "Time left in this quarter-hour: 7.0 min" in msg   # at 12:08:00
    assert "The guard is shaving: discharging" in msg
    assert "stub" in msg and "'logging'" in msg


def test_at_most_one_warning_per_window(wg):
    g = wg(alerts="  peak_warning_min_interval_minutes: 1\n")
    two_ticks(g, **HIGH)
    go(g, 12, 8, 30, **HIGH)
    go(g, 12, 9, 0, **HIGH)
    assert len(warned(g)) == 1


def test_a_dip_resets_the_two_evaluation_rule(wg):
    g = wg()
    go(g, 12, 7, 30, **HIGH)
    go(g, 12, 8, 0, **CALM)                          # projection back under
    go(g, 12, 8, 30, **HIGH)
    assert g.svc.calls == []
    go(g, 12, 9, 0, **HIGH)
    assert len(warned(g)) == 1


def test_two_evaluations_one_second_apart_are_not_sustained(wg):
    g = wg()
    g.at(12, 7, 30)
    g.tick(advance=30.0, **HIGH)
    g.at(12, 7, 31)
    g.tick(advance=1.0, trigger_type="state", **HIGH)   # a state trigger
    assert g.svc.calls == []
    go(g, 12, 8, 0, **HIGH)                          # 31 s after the first
    assert len(warned(g)) == 1


def test_rate_limit_across_windows(wg):
    g = wg()                                         # default 60 minutes
    two_ticks(g, **HIGH)
    assert len(warned(g)) == 1
    go(g, 12, 16, 0, **HIGH)                         # next window, 2 min later
    go(g, 12, 16, 30, **HIGH)
    assert len(warned(g)) == 1                       # held back by the interval
    g.at(12, 31, 0)
    g.tick(advance=3600.0, **HIGH)                   # an hour later, new window
    go(g, 12, 31, 30, **HIGH)
    assert len(warned(g)) == 2


def test_configured_interval_is_honoured(wg):
    g = wg(alerts="  peak_warning_min_interval_minutes: 1\n")
    two_ticks(g, **HIGH)
    g.at(12, 23, 0)
    g.tick(advance=120.0, **HIGH)                    # > 1 minute later
    go(g, 12, 23, 30, **HIGH)
    assert len(warned(g)) == 2


def test_no_warning_when_projection_is_at_or_under_the_ceiling(wg):
    g = wg()
    two_ticks(g, offtake="2.0", avg="2.0", peak="2.5")
    two_ticks(g, offtake="2.5", avg="2.5", peak="2.5")   # projects exactly 2.5
    assert g.svc.calls == []


def test_no_warning_in_the_first_minute(wg):
    g = wg()
    go(g, 12, 0, 0, **HIGH)                          # elapsed 0
    go(g, 12, 0, 30, **HIGH)                         # elapsed 0.5 < 1
    assert g.svc.calls == []
    go(g, 12, 1, 0, **HIGH)                          # elapsed 1.0: first that counts
    assert g.svc.calls == []
    go(g, 12, 1, 30, **HIGH)
    assert len(warned(g)) == 1


def test_no_warning_in_the_last_minute(wg):
    g = wg()
    go(g, 12, 14, 10, **HIGH)                        # 0.83 min left
    go(g, 12, 14, 40, **HIGH)
    assert g.svc.calls == []


def test_last_allowed_minute_still_counts(wg):
    g = wg()
    go(g, 12, 13, 30, **HIGH)
    go(g, 12, 14, 0, **HIGH)                         # exactly 1.0 min left
    assert len(warned(g)) == 1


def test_message_uses_the_configured_floor_and_the_month_peak(wg):
    g = wg(capacity_extra="  billing_floor_kw: 3.0\n")
    two_ticks(g, offtake="5.0", avg="4.0", peak="2.0")   # ceiling = floor 3.0
    msg = warned(g)[0][2]["message"]
    assert "3.00 kW ceiling" in msg and "billing floor (this month's peak is 2.00 kW)" in msg
    assert "2.50" not in msg and "2.50" not in warned(g)[0][2]["title"]
    g2 = wg(capacity_extra="  billing_floor_kw: 3.0\n")
    two_ticks(g2, offtake="5.0", avg="4.0", peak="3.4")  # ceiling = peak 3.4
    msg = warned(g2)[-1][2]["message"]      # the service fake is shared
    assert "3.40 kW ceiling" in msg
    assert "this month's peak so far (billing floor 3.00 kW)" in msg


def test_message_says_when_the_guard_cannot_shave(wg):
    g = wg()
    g.inv.charge_percent = 0.0                       # empty: V5
    two_ticks(g, **HIGH)
    msg = warned(g)[0][2]["message"]
    assert "CANNOT shave" in msg and "V5" in msg and "empty" in msg
    assert "only limits exporting" in msg
    assert g.discharges == []


def test_message_says_the_guard_shaves_below_the_reserve(wg):
    g = wg()
    g.inv.charge_percent = 5.0                       # under the reserve
    two_ticks(g, **HIGH)
    msg = warned(g)[0][2]["message"]
    assert "CANNOT" not in msg and "is shaving" in msg
    assert len(g.discharges) >= 1


REAL = "inverter:\n  type: fake\n"


def test_warning_reads_the_metered_state_when_a_shave_does_not_take_effect(wg):
    # real driver, shave commanded, but the meter still shows the full load:
    # the add-back must not hide the crossing from the warning
    g = wg(capacity_extra=REAL)
    two_ticks(g, **HIGH)
    assert len(g.discharges) >= 1
    assert len(warned(g)) == 1


def test_no_warning_when_the_commanded_shave_takes_effect(wg):
    g = wg(capacity_extra=REAL)
    go(g, 12, 7, 30, **HIGH)
    # the battery now shaves: the meter shows 0.5 kW, window energy as before
    go(g, 12, 8, 0, offtake="0.5", avg="3.9", peak="2.5")
    assert warned(g) == []
    assert g.st.values["pyscript.peak_guard_shaving"] == "on"


def test_failed_send_is_retried_and_not_marked_sent(wg):
    g = wg()
    g.svc.fail = True
    two_ticks(g, **HIGH)
    assert len(warned(g)) == 1                       # attempted
    go(g, 12, 8, 30, **HIGH)
    assert len(warned(g)) == 2                       # retried, same window
    assert any("alert send failed" in m for m in g.log.messages)
    g.svc.fail = False
    go(g, 12, 9, 0, **HIGH)
    assert len(warned(g)) == 3                       # delivered
    go(g, 12, 9, 30, **HIGH)
    assert len(warned(g)) == 3                       # and now done


def test_failed_send_does_not_start_the_rate_limit(wg):
    g = wg()
    g.svc.fail = True
    two_ticks(g, **HIGH)
    g.svc.fail = False
    go(g, 12, 16, 0, **HIGH)                         # next window, soon after
    go(g, 12, 16, 30, **HIGH)
    assert len(warned(g)) == 2                       # delivered despite < 60 min


def test_disabled_config_is_silent(wg):
    g = wg(alerts="  peak_warning_enabled: false\n")
    two_ticks(g, **HIGH)
    assert g.svc.calls == []
    assert len(g.discharges) == 1                    # the shave is unaffected


def test_capacity_disabled_is_silent(wg):
    g = wg(capacity_extra="  enabled: false\n")
    two_ticks(g, **HIGH)
    assert g.svc.calls == []


def test_a_failing_sender_never_changes_the_guard_decision(wg):
    g = wg()
    g.svc.fail = True
    two_ticks(g, **HIGH)
    assert len(g.discharges) == 1
    assert g.st.values["pyscript.peak_guard_shaving"] == "on"


def test_a_failing_message_never_changes_the_guard_decision(wg, monkeypatch):
    g = wg()

    def boom(*a, **k):
        raise RuntimeError("wording broke")
    monkeypatch.setattr(g.mod.capacity, "peak_warning_message", boom)
    two_ticks(g, **HIGH)
    assert len(g.discharges) == 1
    assert g.svc.calls == []
    assert any("predictive warning failed" in m for m in g.log.messages)


def test_stale_average_gives_no_warning(wg):
    g = wg()
    g.st.reported[
        "sensor.slimmelezer_huidig_kwartiervermogen"] = g.at(11, 59, 0).now
    two_ticks(g, **HIGH)                             # stamp predates the window
    assert g.svc.calls == []


def test_ceiling_is_the_configured_floor_not_2_5(wg):
    g = wg(capacity_extra="  billing_floor_kw: 3.0\n")
    two_ticks(g, offtake="2.8", avg="2.8", peak="1.0")   # over 2.5, under 3.0
    assert g.svc.calls == []
    two_ticks(g, offtake="2.8", avg="2.8", peak="2.9")   # month peak below floor
    assert g.svc.calls == []
    g2 = wg()                                            # default floor 2.5
    two_ticks(g2, offtake="2.8", avg="2.8", peak="1.0")
    assert len(warned(g2)) == 1


def test_ceiling_follows_a_month_peak_above_the_floor(wg):
    g = wg()
    two_ticks(g, offtake="3.5", avg="3.5", peak="4.0")   # over floor, under peak
    assert g.svc.calls == []


# ---- alerts.peak_warning_ticks ------------------------------------------------

def test_one_tick_warns_on_the_first_evaluation(wg):
    g = wg(alerts="  peak_warning_ticks: 1\n")
    go(g, 12, 7, 30, **HIGH)
    assert len(warned(g)) == 1
    go(g, 12, 8, 0, **HIGH)
    assert len(warned(g)) == 1                       # still once per window


def test_three_ticks_need_three_evaluations(wg):
    g = wg(alerts="  peak_warning_ticks: 3\n")
    go(g, 12, 7, 30, **HIGH)
    go(g, 12, 8, 0, **HIGH)
    assert g.svc.calls == []                         # two are not enough
    go(g, 12, 8, 30, **HIGH)
    assert len(warned(g)) == 1


def test_counter_resets_when_the_projection_falls_back(wg):
    g = wg(alerts="  peak_warning_ticks: 3\n")
    go(g, 12, 7, 30, **HIGH)
    go(g, 12, 8, 0, **HIGH)
    go(g, 12, 8, 30, **CALM)                         # back under: counter to 0
    go(g, 12, 9, 0, **HIGH)
    go(g, 12, 9, 30, **HIGH)
    assert g.svc.calls == []                         # only two since the dip
    go(g, 12, 10, 0, **HIGH)
    assert len(warned(g)) == 1


def test_counter_resets_at_a_window_boundary(wg):
    g = wg(alerts="  peak_warning_ticks: 3\n")
    go(g, 12, 13, 0, **HIGH)
    go(g, 12, 13, 30, **HIGH)
    go(g, 12, 14, 0, **HIGH)                         # third: fires in window 1
    assert len(warned(g)) == 1
    g2 = wg(alerts="  peak_warning_ticks: 3\n")
    go(g2, 12, 13, 30, **HIGH)
    go(g2, 12, 14, 0, **HIGH)
    go(g2, 12, 16, 0, **HIGH)                        # new window: counting restarts
    go(g2, 12, 16, 30, **HIGH)
    assert len(warned(g2)) == 1                      # the shared fake: only g's
