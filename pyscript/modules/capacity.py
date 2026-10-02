"""Capacity tariff arithmetic (Belgian capaciteitstarief). Pure - no I/O.

Billing is on grid offtake averaged over clock-aligned quarter-hours; the fee
is the mean of the last N monthly peaks, floored at billing_floor_kw.
"""
from dataclasses import dataclass

WINDOW_MINUTES = 15.0
WINDOW_HOURS = 0.25
MIN_ELAPSED_MINUTES = 1.0     # opening-seconds guard for divisions by elapsed
NO_BUDGET_MINUTES = 1.0       # under this remaining, budget is 0.0

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
                own_grid_charge_kw=0.0):
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
                     own_grid_charge_kw)


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


def household_draw_kw(state):
    """Estimated household draw: metered offtake minus the planner's own grid
    charge, never below 0 (offtake_kw is metered at the connection point and
    so includes whatever the planner is charging from the grid)."""
    return max(0.0, state.offtake_kw - state.own_grid_charge_kw)


def allowed_offtake_kw(state, config):
    """TOTAL grid offtake rate (household + charging) that would land the
    quarter-hour average exactly on charging_ceiling_kw at the window end.
    May be negative; uncapped. 0.0 in the last NO_BUDGET_MINUTES."""
    remaining_min = _remaining_minutes(state)
    if remaining_min < NO_BUDGET_MINUTES:
        return 0.0
    allowance = charging_ceiling_kw(state, config) * WINDOW_HOURS
    return (allowance - state.window_energy_kwh) / (remaining_min / 60.0)


def budget_kw(state, config):
    """Grid CHARGE power still available after the household's own draw:
    allowed_offtake_kw - household_draw_kw, capped above at max_charge_kw.
    May be negative (V3 fires at <= 0). Measured against charging_ceiling_kw
    (stay_under_percent of the ceiling), so it is deliberately more cautious
    than the real ceiling. 0.0 in the last NO_BUDGET_MINUTES.

    window_energy_kwh is metered at the connection point (draw so far, already
    included); household_draw_kw covers the draw still to come, assumed to
    continue at its current rate.
    """
    if _remaining_minutes(state) < NO_BUDGET_MINUTES:
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


def billed_average_increase_kw(delta_kw, config):
    """Rise in the billed average when this month's peak rises by delta_kw."""
    return delta_kw / config.peak_averaging_months


def peak_increase_cost_eur(delta_kw, config):
    """Euro cost of raising this month's peak by delta_kw.

    The billed average rises by delta/N. Billing is monthly at rate/12 and the
    elevated peak stays inside the N-month window for N months.

    NOTE - the billing floor is NOT applied here. delta_kw is taken as the
    rise of the BILLED peak, and the function is linear in it. Billing floors
    each monthly peak at config.billing_floor_kw (2.5 kW), so raising a peak
    that sits below the floor costs nothing until it passes the floor. A
    caller must therefore pass the difference of the floored figures,
    max(floor, new_peak) - max(floor, old_peak) (see ceiling_kw), never the
    raw difference of the measured peaks; this function cannot see the
    peaks and will price a sub-floor rise as if it were billed.
    """
    n = config.peak_averaging_months
    monthly = (billed_average_increase_kw(delta_kw, config)
               * config.capacity_rate_eur_per_kw_year / 12.0)
    return monthly * n


def arbitrage_value_eur(kwh, price_spread):
    """Value of moving kwh across a price spread (eur/kWh)."""
    return kwh * price_spread


# ---- quarter-hour average semantics detection (FR-055, FR-058) ----------

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
