"""Tests for decision.py: completeness, none-literals, round trip, real pipeline."""
from dataclasses import MISSING, fields, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import battery
import capacity
import decision
import prices
import rules
import series
import trajectory

TZ = ZoneInfo("Europe/Brussels")
T0 = datetime(2026, 9, 29, 14, 35, tzinfo=TZ)
SAT = datetime(2026, 9, 29, 12, 45, tzinfo=TZ)

# Every key the contract requires in every record (timestamp is positional).
CONTRACT_FIELDS = [
    "action", "power", "soc", "cons", "inj", "solar_rem", "usage_rem",
    "saturation", "spill", "breach", "end_soc", "took", "avg", "ceiling",
    "budget", "vetoes", "selector", "why", "degraded",
]


def rec(**over):
    base = dict(
        timestamp=T0, action="export", target_power_kw=2.5,
        charge_percent=78.0, charge_kwh=7.8, consumption_price=0.214,
        injection_price=0.189, forecast_remaining_kwh=11.2,
        usage_remaining_kwh=14.6, saturation_block=SAT, spill_kwh=2.4,
        reserve_breach_block=None, projected_end_charge_kwh=2.1,
        duration_ms=84, running_average_kw=1.2, ceiling_kw=2.5,
        budget_kw=1.95, vetoes_applied=[], selector="S3",
        reasoning="leftover 4.1kWh with saturation at 12:45",
        degraded_inputs=["soc_stubbed"], source="planner")
    base.update(over)
    return decision.DecisionRecord(**base)


def fields_of(line):
    """{key: value} of a record line (timestamp under '_ts')."""
    parts = line.split(" | ")
    out = {"_ts": parts[0]}
    for p in parts[1:]:
        k, v = p.split("=", 1)
        out[k] = v
    return out


# ---- structure ---------------------------------------------------------------

def test_every_field_required_no_defaults():
    assert all(f.default is MISSING and f.default_factory is MISSING
               for f in fields(decision.DecisionRecord))
    with pytest.raises(TypeError):
        decision.DecisionRecord(timestamp=T0)


def test_omitting_one_field_fails():
    kw = {f.name: None for f in fields(decision.DecisionRecord)}
    kw.pop("degraded_inputs")
    with pytest.raises(TypeError):
        decision.DecisionRecord(**kw)


def test_empty_lists_preserved():
    r = rec()
    r2 = rec(degraded_inputs=[])
    assert r.vetoes_applied == [] and r2.degraded_inputs == []


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError):
        rec(timestamp=datetime(2026, 9, 29, 14, 35))


@pytest.mark.parametrize("over", [
    {"selector": "S7"}, {"action": "hold"}, {"source": "human"},
    {"vetoes_applied": [""]}, {"degraded_inputs": [" "]}])
def test_invalid_values_rejected(over):
    with pytest.raises(ValueError):
        rec(**over)


# ---- completeness --------------------------------------------------------------

def test_contract_field_list_matches_module():
    assert list(decision.FIELD_NAMES[:-1]) == CONTRACT_FIELDS
    assert decision.FIELD_NAMES[-1] == "source"


@pytest.mark.parametrize("record", [
    rec(),
    rec(action="idle", target_power_kw=0.0, saturation_block=None,
        selector="S6", vetoes_applied=[], degraded_inputs=[]),
    rec(running_average_kw=None, ceiling_kw=None, budget_kw=None,
        consumption_price=None, injection_price=None),
    rec(source="guard", budget_kw=-0.45),
], ids=["export", "idle", "capacity-off-unpriced", "guard"])
def test_every_contract_field_appears(record):
    line = decision.format_record(record)
    keys = fields_of(line)
    for name in CONTRACT_FIELDS + ["source"]:
        assert name in keys, "%s missing from %r" % (name, line)
        assert keys[name] != "", "%s is blank in %r" % (name, line)
    assert list(keys)[1:] == CONTRACT_FIELDS + ["source"]  # stable order


# ---- literals and formatting ---------------------------------------------------

def test_empty_lists_render_none_literal():
    line = decision.format_record(rec(degraded_inputs=[]))
    assert "vetoes=none" in line and "degraded=none" in line
    assert "vetoes= " not in line and "degraded= " not in line


def test_saturation_and_breach_none_and_iso():
    line = decision.format_record(rec())
    assert "saturation=2026-09-29T12:45:00+02:00" in line
    assert "breach=none" in line
    assert "saturation=none" in decision.format_record(
        rec(saturation_block=None))


def test_timestamp_has_offset_both_seasons():
    summer = decision.format_record(rec())
    winter = decision.format_record(
        rec(timestamp=datetime(2026, 1, 15, 9, 0, tzinfo=TZ)))
    assert summer.startswith("2026-09-29T14:35:00+02:00 | ")
    assert winter.startswith("2026-01-15T09:00:00+01:00 | ")


def test_idle_shows_zero_power():
    line = decision.format_record(rec(action="idle", target_power_kw=0.0,
                                      selector="S6"))
    assert "power=0.00kW" in line and "action=idle" in line


def test_prices_four_decimals_keep_trailing_zeros():
    line = decision.format_record(
        rec(consumption_price=0.2, injection_price=-0.0043))
    assert "cons=0.2000" in line and "inj=-0.0043" in line
    assert "cons=0.2140" in decision.format_record(rec())


def test_energy_and_soc_formats():
    line = decision.format_record(rec())
    for frag in ("power=2.50kW", "soc=78.0%/7.80kWh", "solar_rem=11.20kWh",
                 "usage_rem=14.60kWh", "spill=2.40kWh", "end_soc=2.10kWh",
                 "took=84ms", "avg=1.20kW", "ceiling=2.50kW",
                 "budget=1.95kW", "source=planner"):
        assert frag in line


def test_negative_budget_keeps_sign_and_zero_stays_unsigned():
    assert "budget=-0.45kW" in decision.format_record(rec(budget_kw=-0.45))
    assert "budget=0.00kW" in decision.format_record(rec(budget_kw=0.0))


def test_single_line_even_with_hostile_reasoning():
    nasty = 'line one\nline two | has "quotes"\r\n  and   gaps'
    line = decision.format_record(rec(reasoning=nasty))
    assert "\n" not in line and "\r" not in line
    assert len(line.split(" | ")) == 21          # timestamp + 20 fields
    assert fields_of(line)["why"].startswith('"') \
        and fields_of(line)["why"].endswith('"')


def test_round_trip_split():
    line = decision.format_record(rec(vetoes_applied=["V2(suppressed S3 export)"]))
    parts = line.split(" | ")
    assert len(parts) == 21
    kv = fields_of(line)
    assert kv["vetoes"] == "V2(suppressed S3 export)"
    assert kv["selector"] == "S3"
    assert kv["degraded"] == "soc_stubbed"


# ---- vetoes and degraded --------------------------------------------------------

def dec(**over):
    base = dict(action="charge", target_power_kw=3.0, selector="S2",
                reasoning="r", vetoes_fired=[], suppressed=[],
                charge_source="solar", block_start=T0)
    base.update(over)
    return rules.Decision(**base)


def test_render_vetoes_single_joined_bare_and_empty():
    assert decision.render_vetoes(dec()) == []
    d = dec(vetoes_fired=["V2"], suppressed=[("S3", "export", "V2")])
    assert decision.render_vetoes(d) == ["V2(suppressed S3 export)"]
    d = dec(vetoes_fired=["V1", "V2"],
            suppressed=[("S3", "export", "V1+V2"),
                        ("S0", "discharge", "V1")])
    assert decision.render_vetoes(d) == [
        "V1+V2(suppressed S3 export)", "V1(suppressed S0 discharge)"]
    assert decision.render_vetoes(dec(vetoes_fired=["V3"])) == ["V3"]


def test_veto_line_names_selector_action_and_veto():
    line = decision.format_record(rec(
        vetoes_applied=["V1+V2(suppressed S3 export)"], selector="S2",
        action="charge"))
    kv = fields_of(line)
    assert kv["vetoes"] == "V1+V2(suppressed S3 export)"
    assert kv["selector"] == "S2"
    assert "vetoes=V1+V2(suppressed S3 export) | selector=S2 | why=" in line


def test_degraded_markers_all_kinds():
    stub = battery.BatteryState(50.0, 5.0, 4.0, 5.0, True)
    live = battery.BatteryState(50.0, 5.0, 4.0, 5.0, False)
    assert decision.degraded_markers(live) == []
    assert decision.degraded_markers(
        stub, True, ["cache_age_solar=3h12m"], 3) == [
        "soc_stubbed", "solar_zero_fallback", "cache_age_solar=3h12m",
        "usage_samples=3"]
    line = decision.format_record(rec(
        degraded_inputs=["soc_stubbed", "cache_age_solar=3h12m"]))
    assert "degraded=soc_stubbed,cache_age_solar=3h12m" in line


# ---- halt -----------------------------------------------------------------------

def test_halt_line():
    entered = datetime(2026, 9, 29, 14, 20, tzinfo=TZ)
    h = decision.HaltState(True, "price_data_unavailable", entered, entered)
    line = decision.format_halt(h, T0)
    assert line == (
        "2026-09-29T14:35:00+02:00 | HALT | cause=price_data_unavailable | "
        "entered=2026-09-29T14:20:00+02:00 | alerted=2026-09-29T14:20:00+02:00")
    assert "\n" not in line
    assert line.split(" | ")[1] == "HALT"


def test_halt_never_alerted_is_explicit():
    h = decision.HaltState(True, "price_data_unavailable", T0, None)
    assert decision.format_halt(h, T0).endswith("| alerted=none")


# ---- real pipeline: project() -> decide() -> build() -----------------------------
#
# Hand-check (site_config: 10 kWh, reserve 10 %, max charge/discharge 5 kW):
# start 90 % = 9.0 kWh, four 15-min blocks 14:30..15:15, usage 0.1 each,
# solar 1.0, 1.0, 0, 0.
#   b0 14:30 surplus 0.9, headroom 1.0 -> +0.9 = 9.90
#   b1 14:45 surplus 0.9, headroom 0.1 -> +0.1 = 10.00 (SATURATION 14:45),
#            spill 0.8
#   b2 15:00 deficit 0.1 -> 9.90 ; b3 15:15 -> 9.80 (end)
#   total spill 0.80, leftover 9.80-1.0 = 8.80, no breach.
#   forecast_remaining = 2.00, usage_remaining = 0.40 (from current block).
# Prices: b0 cons 0.2140 inj 0.1890; later injection 0.05 (S2 finds no better
# later price, S3: spill>0, now is the best injection in window [b0,b1) -> export
# at max_discharge 5.00 kW). Grid: peak 5.0, energy 0.1, 5 min elapsed:
#   ceiling 5.00, budget (1.25-0.1)*6 = 6.9 capped at max_charge 5.00,
#   avg 1.2 (as given).

T0P = datetime(2026, 9, 29, 14, 30, tzinfo=TZ)
STEP = timedelta(minutes=15)


def _pipeline(site_config, pct, grid, plist, solar=(1.0, 1.0, 0.0, 0.0)):
    cfg = site_config
    bs = battery.from_percent(pct, cfg, is_stubbed=True)
    sol = [series.ForecastSlot(T0P + i * STEP, s, False, 15)
           for i, s in enumerate(solar)]
    use = [series.UsageSlot(T0P + i * STEP, 0.1, 7) for i in range(4)]
    pm = {T0P + i * STEP: prices.PricePoint(T0P + i * STEP, 0.0, c, j, 15)
          for i, (c, j) in enumerate(plist)}
    traj = trajectory.project(bs, sol, use, pm, cfg, start_time=T0P)
    now = T0P + timedelta(minutes=1)
    d = rules.decide(traj, pm, bs, grid, cfg, now,
                    usage_history_available=True)
    r = decision.build(d, traj, bs, pm[T0P], decision.degraded_markers(bs),
                       now=now, duration_ms=84, grid_state=grid, config=cfg)
    return traj, d, r


def _grid(energy=0.1, peak=5.0, avg=1.2, offtake=1.0):
    return capacity.GridState(
        offtake_kw=offtake, window_start=T0P, window_energy_kwh=energy,
        elapsed_minutes=1.0, running_average_kw=avg, month_peak_kw=peak,
        is_restored=False)


def test_real_pipeline_export_record(site_config):
    # elapsed 1 min -> 14 min remain; allowed offtake (1.25-0.1)/(14/60) =
    # 4.93 kW, minus household 1.0 kW (offtake) -> budget 3.93 kW
    plist = [(0.2140, 0.1890), (0.30, 0.05), (0.30, 0.05), (0.30, 0.05)]
    traj, d, r = _pipeline(site_config, 90.0, _grid(), plist)
    assert d.selector == "S3" and d.action == "export"
    line = decision.format_record(r)
    kv = fields_of(line)
    assert kv["_ts"] == "2026-09-29T14:31:00+02:00"
    assert kv["action"] == "export"
    assert kv["power"] == "5.00kW"
    assert kv["soc"] == "90.0%/9.00kWh"
    assert kv["cons"] == "0.2140" and kv["inj"] == "0.1890"
    assert kv["solar_rem"] == "2.00kWh" and kv["usage_rem"] == "0.40kWh"
    assert kv["saturation"] == "2026-09-29T14:45:00+02:00"
    assert kv["spill"] == "0.80kWh"
    assert kv["breach"] == "none"
    assert kv["end_soc"] == "9.80kWh"
    assert kv["took"] == "84ms"
    assert kv["avg"] == "1.20kW" and kv["ceiling"] == "5.00kW"
    assert kv["budget"] == "3.93kW"
    assert kv["vetoes"] == "none" and kv["selector"] == "S3"
    assert kv["degraded"] == "soc_stubbed" and kv["source"] == "planner"
    assert "spill ahead 0.80 kWh" in kv["why"]
    assert "\n" not in line


def test_real_pipeline_negative_budget_and_veto(site_config):
    # 10 % battery: V1 forbids discharge/export. Exhausted window: energy 0.7
    # -> allowance 1.25 (peak 5.0)... use peak 2.5 for allowance 0.625:
    # (0.625-0.7)/(14/60) = -0.32 kW -> V3 as well.
    plist = [(0.2140, 0.1890), (0.30, 0.05), (0.30, 0.05), (0.30, 0.05)]
    traj, d, r = _pipeline(site_config, 10.0,
                           _grid(energy=0.7, peak=2.5), plist,
                           solar=(0.0, 0.0, 0.0, 0.0))
    line = decision.format_record(r)
    kv = fields_of(line)
    assert kv["budget"].startswith("-")
    assert kv["ceiling"] == "2.50kW"
    assert "V1" in kv["vetoes"] and "V3" in kv["vetoes"]
    assert kv["selector"] == d.selector
    assert kv["soc"] == "10.0%/1.00kWh"


def test_real_pipeline_capped_grid_charge_reports_both_powers(site_config):
    # Negative consumption price -> S1 grid charge; tight budget caps it.
    plist = [(-0.05, 0.10), (0.30, 0.05), (0.30, 0.05), (0.30, 0.05)]
    traj, d, r = _pipeline(site_config, 50.0,
                           _grid(energy=0.5, peak=2.5, offtake=0.0), plist,
                           solar=(0.0, 0.0, 0.0, 0.0))
    assert d.selector == "S1" and d.action == "charge"
    kv = fields_of(decision.format_record(r))
    assert "capped by grid budget" in kv["why"]
    assert "inverter allows 5.00 kW" in kv["why"]
    # allowance 0.625, energy 0.5, 14 min remain: 0.125/(14/60) = 0.54 kW
    assert kv["power"] == "0.54kW" and kv["budget"] == "0.54kW"


def test_real_pipeline_no_history_idle_record(site_config):
    # Same situation as the export record above, but without usage history:
    # S3 export is held by V4 and the record is an idle one saying why.
    cfg = site_config
    bs = battery.from_percent(90.0, cfg, is_stubbed=True)
    sol = [series.ForecastSlot(T0P + i * STEP, s, False, 15)
           for i, s in enumerate((1.0, 1.0, 0.0, 0.0))]
    use = [series.UsageSlot(T0P + i * STEP, 0.1, 7) for i in range(4)]
    plist = [(0.2140, 0.1890), (0.30, 0.05), (0.30, 0.05), (0.30, 0.05)]
    pm = {T0P + i * STEP: prices.PricePoint(T0P + i * STEP, 0.0, c, j, 15)
          for i, (c, j) in enumerate(plist)}
    traj = trajectory.project(bs, sol, use, pm, cfg, start_time=T0P)
    now = T0P + timedelta(minutes=1)
    d = rules.decide(traj, pm, bs, _grid(), cfg, now,
                     usage_history_available=False)
    r = decision.build(d, traj, bs, pm[T0P], decision.degraded_markers(bs),
                       now=now, duration_ms=5, grid_state=_grid(), config=cfg)
    kv = fields_of(decision.format_record(r))
    assert (kv["action"], kv["selector"], kv["power"]) == (
        "idle", "S6", "0.00kW")
    assert kv["vetoes"] == "V4(suppressed S3 export)"
    assert "no usage history: planner holds" in kv["why"]


def test_build_capacity_off_renders_na(site_config):
    cfg = replace(site_config, capacity_enabled=False)
    bs = battery.from_percent(50.0, cfg)
    sol = [series.ForecastSlot(T0P, 0.0, False, 15)]
    use = [series.UsageSlot(T0P, 0.1, 7)]
    pm = {T0P: prices.PricePoint(T0P, 0.0, 0.2, 0.02, 15)}
    traj = trajectory.project(bs, sol, use, pm, cfg, start_time=T0P)
    now = T0P + timedelta(minutes=1)
    d = rules.decide(traj, pm, bs, None, cfg, now,
                    usage_history_available=True)
    r = decision.build(d, traj, bs, pm[T0P], [], now=now, duration_ms=3,
                       grid_state=None, config=cfg)
    kv = fields_of(decision.format_record(r))
    assert kv["avg"] == kv["ceiling"] == kv["budget"] == "n/a"
    assert kv["degraded"] == "none" and kv["vetoes"] == "none"
    assert kv["action"] == d.action and kv["selector"] == d.selector


# ---- one-line contract: delimiters and non-finite numbers rejected ------------

@pytest.mark.parametrize("field", ["vetoes_applied", "degraded_inputs"])
@pytest.mark.parametrize("bad", ["a|b", "a\nb", "a\rb", "x | y=1"])
def test_list_entries_reject_delimiters_and_line_breaks(field, bad):
    with pytest.raises(ValueError, match=field):
        rec(**{field: ["ok", bad]})


@pytest.mark.parametrize("field", [
    "target_power_kw", "charge_percent", "charge_kwh", "consumption_price",
    "injection_price", "forecast_remaining_kwh", "usage_remaining_kwh",
    "spill_kwh", "projected_end_charge_kwh", "running_average_kw",
    "ceiling_kw", "budget_kw"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_numeric_fields_reject_non_finite(field, bad):
    with pytest.raises(ValueError, match=field):
        rec(**{field: bad})


def test_required_number_may_not_be_none():
    with pytest.raises(ValueError, match="target_power_kw"):
        rec(target_power_kw=None)


@pytest.mark.parametrize("bad", ["a|b", "line\none", "cr\rhere"])
def test_halt_cause_rejects_delimiters_and_line_breaks(bad):
    with pytest.raises(ValueError, match="cause"):
        decision.HaltState(True, bad, T0, None)


def test_format_halt_rechecks_the_cause():
    h = decision.HaltState(True, "fine", T0, None)
    object.__setattr__(h, "cause", "sneaky | alerted=none")
    with pytest.raises(ValueError, match="cause"):
        decision.format_halt(h, T0)


def test_one_line_makes_untrusted_text_halt_safe():
    cause = decision.one_line("bad\n  entries | KeyError(\"x\")\r\n")
    assert cause == "bad entries / KeyError(\"x\")"
    h = decision.HaltState(True, cause, T0, None)
    assert len(decision.format_halt(h, T0).split(" | ")) == 5


# ---- None renders n/a (guard: no trajectory) ---------------------------------

def test_none_remaining_and_spill_render_na_and_keep_field_order():
    line = decision.format_record(rec(
        forecast_remaining_kwh=None, usage_remaining_kwh=None, spill_kwh=None))
    kv = fields_of(line)
    assert (kv["solar_rem"], kv["usage_rem"], kv["spill"]) == ("n/a",) * 3
    assert len(line.split(" | ")) == 21
    assert "0.00kWh" not in line.split(" | ")[7]          # usage_rem
    # a real zero is still a zero
    kv0 = fields_of(decision.format_record(rec(spill_kwh=0.0)))
    assert kv0["spill"] == "0.00kWh"
