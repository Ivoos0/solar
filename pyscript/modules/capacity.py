"""Capacity tariff arithmetic (Belgian capaciteitstarief). Pure - no I/O.

Billing is on grid offtake averaged over clock-aligned quarter-hours; the fee
is the mean of the last N monthly peaks, floored at billing_floor_kw.
"""
import math
from dataclasses import dataclass
from datetime import datetime

WINDOW_MINUTES = 15.0
WINDOW_HOURS = 0.25
MIN_ELAPSED_MINUTES = 1.0     # opening-seconds guard for divisions by elapsed
NO_BUDGET_MINUTES = 1.0       # least remaining at which a budget exists, whatever
                              # the evaluation interval (see no_budget_minutes)

# detector tuning
DETECT_MIN_MIN = 2.0
DETECT_MAX_MIN = 13.0
DETECT_MIN_GAP_MIN = 3.0
DETECT_MAX_LOAD_CHANGE = 0.25   # fraction of the larger offtake
DETECT_MIN_LOAD_KW = 0.5
DETECT_MIN_LEAD = 3             # votes one hypothesis must lead by


@dataclass(frozen=True)
class GridState:
    offtake_kw: float
    window_start: object
    window_energy_kwh: float
    elapsed_minutes: float
    running_average_kw: float
    month_peak_kw: float
    is_restored: bool
    average_mode: str = "accumulating"
    mode_confidence: str = "assumed"
    # Grid power the planner itself is currently drawing to charge (kW): the
    # last decision's grid-charge power, set by the adapter ONLY when a real
    # (non-logging) driver transmits it and it is still in effect; else 0.0.
    # offtake_kw includes it, so budget_kw subtracts it to find household draw.
    own_grid_charge_kw: float = 0.0
    # Battery power measured by a sensor (kW, discharge positive, charge
    # negative), or None when there is no usable sensor. See household_draw_kw.
    battery_discharge_kw: object = None


def battery_discharge_from_power(power_kw, power_positive):
    """Sensor reading (kW) -> discharge-positive battery power.

    power_positive is "discharge" (the reading is positive while discharging:
    kept) or "charge" (positive while charging: sign flipped).
    """
    return power_kw if power_positive == "discharge" else -power_kw


def window_start_of(now):
    """Floor a datetime to its clock-aligned quarter-hour."""
    return now.replace(minute=now.minute - now.minute % 15,
                       second=0, microsecond=0)


def normalise_average(reported_kw, elapsed_minutes, mode):
    """Meter figure -> true running average (energy / elapsed)."""
    if mode == "accumulating":
        return reported_kw * WINDOW_MINUTES / max(elapsed_minutes,
                                                  MIN_ELAPSED_MINUTES)
    return reported_kw


def build_state(offtake_kw, window_energy_kwh, now, month_peak_kw, config,
                is_restored=False, reported_average_kw=None,
                average_mode=None, mode_confidence=None,
                own_grid_charge_kw=0.0, battery_discharge_kw=None):
    """Build a GridState.

    If reported_average_kw (the meter 1-0:1.4.0 figure) is given it is
    authoritative and normalised per the active mode; window energy is then
    derived from it. Otherwise the average is derived from window_energy_kwh.
    """
    start = window_start_of(now)
    elapsed = (now - start).total_seconds() / 60.0
    if config.quarter_hour_average_mode != "auto":
        mode, conf = config.quarter_hour_average_mode, "configured"
    else:
        mode = average_mode or "accumulating"
        conf = mode_confidence or "assumed"
    eff = max(elapsed, MIN_ELAPSED_MINUTES)
    if reported_average_kw is not None:
        running = normalise_average(reported_average_kw, elapsed, mode)
        energy = running * eff / 60.0
    else:
        energy = window_energy_kwh
        running = energy / (eff / 60.0)
    return GridState(offtake_kw, start, energy, elapsed, running,
                     month_peak_kw, is_restored, mode, conf,
                     own_grid_charge_kw, battery_discharge_kw)


def ceiling_kw(state, config):
    """Level worth defending: this month's peak, never below the floor."""
    return max(config.billing_floor_kw, state.month_peak_kw)


def charging_ceiling_kw(state, config):
    """Level grid CHARGING must stay under: stay_under_percent of the ceiling.

    Only the grid-charging budget uses this; peak shaving and the reported
    ceiling defend the real ceiling_kw.
    """
    return ceiling_kw(state, config) * config.stay_under_percent / 100.0


def _remaining_minutes(state):
    return WINDOW_MINUTES - state.elapsed_minutes


def no_budget_minutes(config):
    """Remaining minutes under which grid charging has no budget.

    max(NO_BUDGET_MINUTES, evaluation_interval_minutes): a charge sized for
    this quarter-hour keeps running until the planner decides again, which is
    one evaluation interval later, so a charge started in the last interval
    would run on into the next quarter-hour. Grid charging therefore stops one
    evaluation interval before the quarter-hour ends. The peak guard and the
    predictive warning keep the fixed NO_BUDGET_MINUTES: they re-evaluate every
    guard interval (seconds) and do not size a charge.
    """
    return max(NO_BUDGET_MINUTES, float(config.evaluation_interval_minutes))


def household_draw_kw(state):
    """The grid draw the household would have WITHOUT the battery helping
    (kW), never below 0. This is what a grid charge adds to.

    With a battery power sensor (state.battery_discharge_kw is not None):
        offtake = household - PV - battery_discharge
        =>  household - PV = offtake + battery_discharge
    so the draw is max(0, offtake_kw + battery_discharge_kw). Discharge is
    positive: a battery covering the house (meter 0 kW, battery +3 kW) gives
    3 kW, and a battery charging from the grid (offtake 5.14 kW, battery
    -2.14 kW) gives 3.0 kW. The planner's own grid charge is already inside
    both terms, so it needs no bookkeeping and the budget cannot flip between
    cycles.

    Without a sensor: offtake_kw minus the planner's own grid charge
    (own_grid_charge_kw, set only for a real driver), a fallback that cannot
    see the battery covering the house."""
    if state.battery_discharge_kw is not None:
        return max(0.0, state.offtake_kw + state.battery_discharge_kw)
    return max(0.0, state.offtake_kw - state.own_grid_charge_kw)


def allowed_offtake_kw(state, config):
    """TOTAL grid offtake rate (household + charging) that would land the
    quarter-hour average exactly on charging_ceiling_kw at the window end.
    May be negative; uncapped. 0.0 in the last no_budget_minutes(config)."""
    remaining_min = _remaining_minutes(state)
    if remaining_min < no_budget_minutes(config):
        return 0.0
    allowance = charging_ceiling_kw(state, config) * WINDOW_HOURS
    return (allowance - state.window_energy_kwh) / (remaining_min / 60.0)


def budget_kw(state, config):
    """Grid CHARGE power still available after the household's own draw:
    allowed_offtake_kw - household_draw_kw, capped above at max_charge_kw.
    May be negative (V3 fires at <= 0). Measured against charging_ceiling_kw
    (stay_under_percent of the ceiling), so it is deliberately more cautious
    than the real ceiling. 0.0 in the last no_budget_minutes(config) = the
    larger of NO_BUDGET_MINUTES and the evaluation interval.

    window_energy_kwh is metered at the connection point (draw so far, already
    included); household_draw_kw covers the draw still to come, assumed to
    continue at its current rate.
    """
    if _remaining_minutes(state) < no_budget_minutes(config):
        return 0.0
    budget = allowed_offtake_kw(state, config) - household_draw_kw(state)
    return min(budget, config.max_charge_kw)


def shave_kw(state, config):
    """Discharge power to bring the projected window average to the ceiling."""
    if state.offtake_kw <= config.billing_floor_kw:
        return 0.0
    remaining_h = _remaining_minutes(state) / 60.0
    if remaining_h <= 0:
        return 0.0
    projected_kwh = state.window_energy_kwh + state.offtake_kw * remaining_h
    if projected_kwh / WINDOW_HOURS <= ceiling_kw(state, config):
        return 0.0
    allowance = ceiling_kw(state, config) * WINDOW_HOURS
    needed = (projected_kwh - allowance) / remaining_h
    needed = min(needed, state.offtake_kw - config.billing_floor_kw)
    return max(0.0, min(needed, config.max_discharge_kw))


# ---- month-peak notice ----------------------------------------------------

# A further notice in the same month needs the peak to rise by at least this
# much (kW) above the last peak that was notified. Stops 0.01 kW creep mailing.
PEAK_ALERT_MIN_STEP_KW = 0.05
_STEP_EPSILON = 1e-9              # float noise, e.g. 3.10 - 3.05 = 0.0500000000000003


def peak_alert_due(peak_kw, month, last_month, last_peak_kw, config):
    """True when this month's peak warrants a (further) notice.

    peak_kw must be strictly above config.billing_floor_kw. The first such
    reading of a calendar month (month != last_month) always qualifies; later
    ones only when the peak is at least PEAK_ALERT_MIN_STEP_KW above
    last_peak_kw. An unchanged or lower peak never qualifies.
    """
    if peak_kw is None or not peak_kw > config.billing_floor_kw:
        return False
    if last_month != month or last_peak_kw is None:
        return True
    return peak_kw - last_peak_kw >= PEAK_ALERT_MIN_STEP_KW - _STEP_EPSILON


def peak_alert_message(peak_kw, observed, previous_peak_kw, config):
    """(title, message) of the month-peak notice. `observed` is a string."""
    floor = config.billing_floor_kw
    over = peak_kw - floor
    lines = [
        "This month's capacity peak is now %.2f kW, above the billing floor "
        "of %.2f kW (%.2f kW over)." % (peak_kw, floor, over),
        "First seen by the planner at %s. The meter reports a new maximum "
        "only after the quarter-hour has completed, so this is a notice after "
        "the fact, not a prevention." % observed,
    ]
    if previous_peak_kw is not None:
        lines.append("Previous notice this month: %.2f kW." % previous_peak_kw)
    title = "Capacity peak %.2f kW is above the %.2f kW billing floor" % (
        peak_kw, floor)
    return title, " ".join(lines)


# ---- predictive warning (peak guard) ---------------------------------------

# Noise control for the "probably going to cross this quarter" e-mail.
# The number of consecutive evaluations over the ceiling is config
# alerts.peak_warning_ticks (default 2). When it is 2 or more, those
# evaluations must ALSO span at least this long since the first of them. Guard
# ticks are 30 s apart (jitter-tolerant 20 s), but a state trigger can fire a
# second after a tick; evaluations a second apart are not "sustained".
PEAK_WARN_SUSTAIN_SECONDS = 20.0


def projected_average_kw(state):
    """Quarter-hour average if the current offtake continues to the window end.

    Same projection shave_kw uses: (energy so far + offtake * time left) / 0.25 h.
    """
    remaining_h = max(0.0, _remaining_minutes(state)) / 60.0
    return (state.window_energy_kwh
            + state.offtake_kw * remaining_h) / WINDOW_HOURS


def peak_warning_due(mem, state, config, now_s):
    """True when a predictive warning should be sent NOW. Updates `mem`.

    mem is a dict kept by the caller between evaluations; now_s is a monotonic
    clock in seconds. Fires only when the projection exceeds ceiling_kw on
    config.peak_warning_ticks consecutive evaluations (spanning at least
    PEAK_WARN_SUSTAIN_SECONDS when that is 2 or more), at least MIN_ELAPSED_MINUTES into the window and
    with at least NO_BUDGET_MINUTES left; at most once per window and once per
    config.peak_warning_min_interval_minutes. The caller reports a successful
    send with peak_warning_sent(); until then it stays due (retry).
    """
    window = state.window_start
    if mem.get("window") != window:
        mem["window"] = window
        mem["streak"] = 0
        mem["since"] = None
    over = (state.elapsed_minutes >= MIN_ELAPSED_MINUTES
            and _remaining_minutes(state) >= NO_BUDGET_MINUTES
            and projected_average_kw(state) > ceiling_kw(state, config))
    if not over:
        mem["streak"] = 0
        mem["since"] = None
        return False
    mem["streak"] = mem.get("streak", 0) + 1
    if mem["streak"] == 1:
        mem["since"] = now_s
    ticks = config.peak_warning_ticks
    if mem["streak"] < ticks:
        return False
    if ticks > 1 and now_s - mem["since"] < PEAK_WARN_SUSTAIN_SECONDS:
        return False
    if mem.get("sent_window") == window:
        return False
    sent_at = mem.get("sent_at")
    if (sent_at is not None and now_s - sent_at
            < config.peak_warning_min_interval_minutes * 60.0):
        return False
    return True


def peak_warning_sent(mem, state, now_s):
    """Record a successful send (never call it after a failed one)."""
    mem["sent_window"] = state.window_start
    mem["sent_at"] = now_s


def peak_warning_to_data(mem, wall_now):
    """JSON-ready record of the last successful warning, or None.

    `mem` keeps sent_at on a monotonic clock, which means nothing after a
    restart, so the file stores the wall-clock send time instead. The caller
    saves it right after peak_warning_sent().
    """
    window = mem.get("sent_window")
    if window is None or mem.get("sent_at") is None:
        return None
    return {"sent_window": window.isoformat(),
            "sent_at": wall_now.isoformat()}


def peak_warning_restore(mem, data, wall_now, now_s):
    """Load a saved warning into `mem` after a restart. True when applied.

    Anything unreadable is ignored (the warning may then be sent once more,
    which is the safe direction). The wall-clock send time is turned back into
    the monotonic clock `mem` uses: sent_at = now_s - age.
    """
    try:
        window = datetime.fromisoformat(data["sent_window"])
        sent = datetime.fromisoformat(data["sent_at"])
        if window.tzinfo is None or sent.tzinfo is None:
            return False
        age = max(0.0, (wall_now - sent).total_seconds())
    except Exception:
        return False
    mem["sent_window"] = window
    mem["sent_at"] = now_s - age
    return True


def peak_warning_message(state, config, guard_note):
    """(title, message) of the predictive warning. guard_note says what the
    guard is doing about it."""
    projected = projected_average_kw(state)
    ceiling = ceiling_kw(state, config)
    if state.month_peak_kw > config.billing_floor_kw:
        basis = "this month's peak so far (billing floor %.2f kW)" % (
            config.billing_floor_kw)
    else:
        basis = "the billing floor (this month's peak is %.2f kW)" % (
            state.month_peak_kw)
    title = ("Capacity peak warning: this quarter-hour is heading for %.2f kW "
             "(ceiling %.2f kW)" % (projected, ceiling))
    message = (
        "PREDICTION, not a measured peak: the quarter-hour that started at "
        "%s is projected to average %.2f kW if the current load continues, "
        "above the %.2f kW ceiling, which is %s. Time left in this quarter-hour: "
        "%.1f min. Current offtake: %.2f kW. %s" % (
            state.window_start.strftime("%H:%M"), projected, ceiling, basis,
            _remaining_minutes(state), state.offtake_kw, guard_note))
    return title, message


def arbitrage_value_eur(kwh, price_spread):
    """Value of moving kwh across a price spread (eur/kWh)."""
    return kwh * price_spread


# ---- quarter-hour average semantics detection ----------

@dataclass(frozen=True)
class Sample:
    window_start: object
    elapsed_minutes: float
    reported_kw: float
    offtake_kw: float


@dataclass(frozen=True)
class ModeVerdict:
    mode: str
    confidence: str
    running_votes: int = 0
    accumulating_votes: int = 0


def _window_vote(samples):
    """Vote from one window's samples: running, accumulating or None."""
    good = sorted((s for s in samples
                   if DETECT_MIN_MIN <= s.elapsed_minutes <= DETECT_MAX_MIN),
                  key=lambda s: s.elapsed_minutes)
    if len(good) < 2:
        return None
    early, late = good[0], good[-1]
    if late.elapsed_minutes - early.elapsed_minutes < DETECT_MIN_GAP_MIN:
        return None
    if min(early.offtake_kw, late.offtake_kw) < DETECT_MIN_LOAD_KW:
        return None
    hi = max(early.offtake_kw, late.offtake_kw)
    if abs(late.offtake_kw - early.offtake_kw) / hi > DETECT_MAX_LOAD_CHANGE:
        return None
    if early.reported_kw <= 0 or late.reported_kw <= 0:
        return None
    ratio = early.reported_kw / late.reported_kw
    expected = early.elapsed_minutes / late.elapsed_minutes
    if abs(ratio - 1.0) < abs(ratio - expected):
        return "running"
    return "accumulating"


def samples_to_data(samples):
    """JSON-ready form of the detector samples that can still matter.

    Only samples inside the detection minutes ever take part in a vote
    (_window_vote ignores the rest), so only those are kept.
    """
    out = []
    for s in samples:
        if DETECT_MIN_MIN <= s.elapsed_minutes <= DETECT_MAX_MIN:
            out.append([s.window_start.isoformat(), s.elapsed_minutes,
                        s.reported_kw, s.offtake_kw])
    return out


def samples_from_data(data, limit=None):
    """Detector samples from samples_to_data() output; [] for anything else.

    A malformed entry is skipped, a malformed whole is an empty list, so a
    damaged file means a fresh start, never an error. `limit` keeps only the
    newest entries.
    """
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        try:
            start = datetime.fromisoformat(item[0])
            nums = [item[1], item[2], item[3]]
            ok = start.tzinfo is not None and len(item) == 4
            for n in nums:
                if isinstance(n, bool) or not isinstance(n, (int, float)) \
                        or not math.isfinite(n):
                    ok = False
            if ok:
                out.append(Sample(start, float(nums[0]), float(nums[1]),
                                  float(nums[2])))
        except Exception:
            continue
    if limit is not None:
        out = out[-limit:]
    return out


def detect_average_mode(samples, config):
    """Pure verdict from samples (adapter stores them across cycles)."""
    if config.quarter_hour_average_mode != "auto":
        return ModeVerdict(config.quarter_hour_average_mode, "configured")
    by_window = {}
    for s in samples:
        by_window.setdefault(s.window_start, []).append(s)
    run = acc = 0
    for group in by_window.values():
        vote = _window_vote(group)
        if vote == "running":
            run += 1
        elif vote == "accumulating":
            acc += 1
    if run - acc >= DETECT_MIN_LEAD:
        return ModeVerdict("running", "detected", run, acc)
    if acc - run >= DETECT_MIN_LEAD:
        return ModeVerdict("accumulating", "detected", run, acc)
    return ModeVerdict("accumulating", "assumed", run, acc)
