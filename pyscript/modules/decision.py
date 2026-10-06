"""Decision record: build and format the one auditable line per cycle. Pure.

No file I/O (the inverter boundary appends the line to the day's decision log), no clock (the cycle
time is a parameter), no third-party / Home Assistant imports.

Public API
----------
    DecisionRecord            frozen dataclass, EVERY field required (no
                              defaults)
    HaltState                 a price outage with no decision
    build(decision, trajectory, battery_state, prices_now, degraded, *,
          now, duration_ms, grid_state=None, config=None, source="planner")
    format_record(record)     -> one line, fixed field order
    format_halt(halt_state, now) -> one HALT line
    degraded_markers(battery_state, solar_zero_fallback=False,
                     cache_markers=(), usage_samples=None) -> [str]
    render_vetoes(decision)   -> [str] (also used by build)
    FIELD_NAMES               the keys after the timestamp, in
                              output order

Field order (every record, always)
----------------------------------
timestamp | action | power | soc | cons | inj | solar_rem | usage_rem |
saturation | spill | breach | end_soc | took | avg | ceiling | budget |
vetoes | selector | why | degraded | source

Documented readings / discrepancies
-----------------------------------
* This module does not import cache.py, so age markers
  are not computed here. ``degraded`` is a list of already-rendered
  strings; the caller renders cache ages with ``cache.age_marker(series,
  now)`` and passes them via ``degraded_markers(cache_markers=...)``. This
  module never reimplements age formatting.
* ``source=planner|guard`` is the LAST field (after
  degraded), so the other columns keep their positions.
* avg / ceiling / budget come from the GridState the rules used (via
  capacity), not from the Decision. When capacity is inactive (grid_state
  None or config.capacity_enabled False) they render the literal ``n/a``:
  present but visibly not a number, never 0.00kW. Same for cons / inj when
  the current block has no published price (prices_now None).
* ``budget`` is rendered with its sign and never clamped; capacity.budget_kw
  is negative when the window is already over the ceiling.
* solar_rem / usage_rem / spill are None (rendered ``n/a``) when the source
  has no trajectory to derive them from (the peak guard): ``n/a`` is
  explicit, unlike a projected 0.00kWh.
* Free text is validated, not repaired: list entries and the halt cause may
  not contain ``|`` or CR/LF, and every number must be finite (ValueError).
  ``why`` keeps its own sanitising (``_why``); ``one_line`` is offered for
  callers that must feed untrusted text into a halt cause.
* solar_rem / usage_rem sum the trajectory blocks from the decision's
  current block (decision.block_start) to the horizon end; end_soc is the
  last block's projected charge (the battery's stored kWh if no blocks).
* A capped grid charge is explained by rules.decide's own reasoning ("capped
  by grid budget X kW, inverter allows Y kW"); ``why`` carries it verbatim.
* Vetoes: rules reports a suppressed proposal as (selector, action,
  blocking_veto) where blocking_veto may be joined ("V1+V2"); rendered as
  ``V1+V2(suppressed S3 export)``. A fired veto that suppressed nothing is
  rendered bare (``V2``). Empty -> ``none``. The selector field is always
  present, so a veto never appears without the selector that fired.
* ``why`` is single-lined: whitespace runs collapse to one space, a double
  quote becomes two single quotes, ``|`` becomes ``/`` so the line still
  splits on " | ".
* HaltState is not defined elsewhere; it is defined here.
  Halt timestamp is the cycle time ``now``; a never-alerted halt renders
  ``alerted=none``.
"""
from dataclasses import dataclass
import math
from datetime import timezone

ACTIONS = ("charge", "discharge", "export", "idle")
# There is no S2 (see rules.py); the labels keep their numbers.
# S5 was merged into S4; older logs still carry it.
SELECTORS = ("S0", "S1", "S3", "S4", "S5", "S6")
SOURCES = ("planner", "guard")

NONE = "none"
NA = "n/a"

# Keys after the leading timestamp, in output order.
FIELD_NAMES = (
    "action", "power", "soc", "cons", "inj", "solar_rem", "usage_rem",
    "saturation", "spill", "breach", "end_soc", "took", "avg", "ceiling",
    "budget", "vetoes", "selector", "why", "degraded", "source",
)


# Numeric fields that must be finite (a "nan"/"inf" would render as text that
# is not a number). The optional ones render n/a when None.
_REQUIRED_NUMBERS = ("target_power_kw", "duration_ms")
_OPTIONAL_NUMBERS = ("charge_percent", "charge_kwh",
                     "projected_end_charge_kwh",
                     "consumption_price", "injection_price",
                     "forecast_remaining_kwh", "usage_remaining_kwh",
                     "spill_kwh", "running_average_kw", "ceiling_kw",
                     "budget_kw")


def _reject_delimiters(name, text):
    """One-line, fixed-field-order contract: a value may not carry the field
    separator (``|``) or a line break, or it would split or add fields."""
    s = "" if text is None else str(text)
    if "|" in s or "\n" in s or "\r" in s:
        raise ValueError(
            "%s must not contain '|' or a line break, got %r" % (name, text))


def one_line(text):
    """Make free text safe for a HaltState cause: whitespace runs (line
    breaks included) collapse to one space and ``|`` becomes ``/``. Callers
    holding untrusted text (an exception repr) use this before HaltState."""
    return " ".join(str(text).split()).replace("|", "/")


@dataclass(frozen=True)
class DecisionRecord:
    timestamp: object               # aware datetime (offset is rendered)
    action: str
    target_power_kw: float
    charge_percent: float
    charge_kwh: float
    consumption_price: object       # float, or None when the block is unpriced
    injection_price: object         # float, or None when the block is unpriced
    forecast_remaining_kwh: object  # float, or None (source=guard: n/a)
    usage_remaining_kwh: object     # float, or None (source=guard: n/a)
    saturation_block: object        # aware datetime | None
    spill_kwh: object               # float, or None (source=guard: n/a)
    reserve_breach_block: object    # aware datetime | None
    projected_end_charge_kwh: float
    duration_ms: int
    running_average_kw: object      # float, or None when capacity inactive
    ceiling_kw: object              # float, or None when capacity inactive
    budget_kw: object               # float (may be negative) | None
    vetoes_applied: list            # rendered veto tokens; [] means none
    selector: str
    reasoning: str
    degraded_inputs: list           # marker strings; [] means none
    source: str

    def __post_init__(self):
        if self.timestamp is None or self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        for name in ("saturation_block", "reserve_breach_block"):
            v = getattr(self, name)
            if v is not None and v.tzinfo is None:
                raise ValueError("%s must be timezone-aware" % name)
        if self.action not in ACTIONS:
            raise ValueError("action %r not in %r" % (self.action, ACTIONS))
        if self.selector not in SELECTORS:
            raise ValueError("selector %r not in %r" % (self.selector, SELECTORS))
        if self.source not in SOURCES:
            raise ValueError("source %r not in %r" % (self.source, SOURCES))
        for name in ("vetoes_applied", "degraded_inputs"):
            items = getattr(self, name)
            if not isinstance(items, list):
                raise ValueError("%s must be a list" % name)
            if any(not isinstance(i, str) or not i.strip() for i in items):
                raise ValueError("%s holds a blank entry" % name)
        for name in _REQUIRED_NUMBERS + _OPTIONAL_NUMBERS:
            v = getattr(self, name)
            if v is None and name in _OPTIONAL_NUMBERS:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v):
                raise ValueError("%s must be a finite number, got %r"
                                 % (name, v))
        for name in ("vetoes_applied", "degraded_inputs"):
            for item in getattr(self, name):
                _reject_delimiters(name, item)
        if not str(self.reasoning).strip():
            raise ValueError("reasoning must not be blank")


@dataclass(frozen=True)
class HaltState:
    is_halted: bool
    cause: str
    entered_at: object              # aware datetime
    last_alert_at: object           # aware datetime | None

    def __post_init__(self):
        _reject_delimiters("cause", self.cause)


# ---- building --------------------------------------------------------------

def render_vetoes(decision):
    """Veto tokens: ``V1+V2(suppressed S3 export)``; bare id if it blocked
    nothing. [] when no veto fired."""
    tokens = []
    covered = set()
    for selector, action, blocking in decision.suppressed:
        tokens.append("%s(suppressed %s %s)" % (blocking, selector, action))
        covered.update(blocking.split("+"))
    for v in decision.vetoes_fired:
        if v not in covered:
            tokens.append(v)
    return tokens


def degraded_markers(battery_state, solar_zero_fallback=False,
                     cache_markers=(), usage_samples=None):
    """Assemble the degraded list.

    cache_markers: strings from cache.age_marker(series, now), passed through
    untouched. usage_samples: days of history behind the usage profile when
    fewer than seven (None = not degraded).
    """
    out = []
    if battery_state.is_stubbed:
        out.append("soc_stubbed")
    if solar_zero_fallback:
        out.append("solar_zero_fallback")
    out.extend(cache_markers)
    if usage_samples is not None:
        out.append("usage_samples=%d" % usage_samples)
    return out


def _from(blocks, start):
    if start is None:
        return list(blocks)
    s = start.astimezone(timezone.utc)
    return [b for b in blocks if b.block_start.astimezone(timezone.utc) >= s]


def build(decision, trajectory, battery_state, prices_now, degraded, *,
          now, duration_ms, grid_state=None, config=None, source="planner"):
    """Assemble a DecisionRecord from what rules/trajectory/capacity produced.

    prices_now: prices.PricePoint of the current block, or None (unpriced).
    grid_state/config: the SAME GridState and SiteConfig passed to
    rules.decide; avg/ceiling/budget are None (rendered n/a) when capacity is
    inactive.
    """
    import capacity
    ahead = _from(trajectory.blocks, decision.block_start)
    end = (trajectory.blocks[-1].projected_charge_kwh if trajectory.blocks
           else battery_state.stored_kwh)
    if grid_state is not None and config is not None \
            and config.capacity_enabled:
        avg = grid_state.running_average_kw
        ceiling = capacity.ceiling_kw(grid_state, config)
        budget = capacity.budget_kw(grid_state, config)
    else:
        avg = ceiling = budget = None
    return DecisionRecord(
        timestamp=now,
        action=decision.action,
        target_power_kw=decision.target_power_kw,
        charge_percent=battery_state.charge_percent,
        charge_kwh=battery_state.stored_kwh,
        consumption_price=None if prices_now is None
        else prices_now.consumption_price,
        injection_price=None if prices_now is None
        else prices_now.injection_price,
        forecast_remaining_kwh=sum(b.solar_kwh for b in ahead),
        usage_remaining_kwh=sum(b.usage_kwh for b in ahead),
        saturation_block=trajectory.saturation_block,
        spill_kwh=trajectory.total_spill_kwh,
        reserve_breach_block=trajectory.reserve_breach_block,
        projected_end_charge_kwh=end,
        duration_ms=int(duration_ms),
        running_average_kw=avg,
        ceiling_kw=ceiling,
        budget_kw=budget,
        vetoes_applied=render_vetoes(decision),
        selector=decision.selector,
        reasoning=decision.reasoning,
        degraded_inputs=list(degraded),
        source=source,
    )


# ---- formatting ------------------------------------------------------------

def _iso(dt):
    return NONE if dt is None else dt.isoformat(timespec="seconds")


def _kw(v):
    return NA if v is None else "%.2fkW" % v


def _kwh(v):
    return NA if v is None else "%.2fkWh" % v


def _price(v):
    return NA if v is None else "%.4f" % v


def _why(text):
    s = " ".join(str(text).split())
    return '"%s"' % s.replace('"', "''").replace("|", "/")


def format_record(record):
    """One physical line, fixed field order, every field present."""
    r = record
    parts = [
        r.timestamp.isoformat(timespec="seconds"),
        "action=%s" % r.action,
        "power=%s" % _kw(r.target_power_kw),
        "soc=%s" % (NA if r.charge_percent is None else "%.1f%%/%s" % (
            r.charge_percent, _kwh(r.charge_kwh))),
        "cons=%s" % _price(r.consumption_price),
        "inj=%s" % _price(r.injection_price),
        "solar_rem=%s" % _kwh(r.forecast_remaining_kwh),
        "usage_rem=%s" % _kwh(r.usage_remaining_kwh),
        "saturation=%s" % _iso(r.saturation_block),
        "spill=%s" % _kwh(r.spill_kwh),
        "breach=%s" % _iso(r.reserve_breach_block),
        "end_soc=%s" % _kwh(r.projected_end_charge_kwh),
        "took=%dms" % r.duration_ms,
        "avg=%s" % _kw(r.running_average_kw),
        "ceiling=%s" % _kw(r.ceiling_kw),
        "budget=%s" % _kw(r.budget_kw),
        "vetoes=%s" % (",".join(r.vetoes_applied) or NONE),
        "selector=%s" % r.selector,
        "why=%s" % _why(r.reasoning),
        "degraded=%s" % (",".join(r.degraded_inputs) or NONE),
        "source=%s" % r.source,
    ]
    return " | ".join(parts)


def release_record(now, reasoning, source="planner", degraded=()):
    """An idle DecisionRecord for a release that has no decision behind it.

    Used when the planner halts, its configuration is invalid or the peak
    guard loses its meter: a forced command sent earlier may still be in
    force, and the inverter must be told to go back to its default. Nothing
    was read, so the battery and price fields are n/a.
    """
    return DecisionRecord(
        timestamp=now, action="idle", target_power_kw=0.0,
        charge_percent=None, charge_kwh=None, consumption_price=None,
        injection_price=None, forecast_remaining_kwh=None,
        usage_remaining_kwh=None, saturation_block=None, spill_kwh=None,
        reserve_breach_block=None, projected_end_charge_kwh=None,
        duration_ms=0, running_average_kw=None, ceiling_kw=None,
        budget_kw=None, vetoes_applied=[], selector="S6", reasoning=reasoning,
        degraded_inputs=list(degraded), source=source)


def format_halt(halt_state, now):
    """HALT line for a cycle that produced no decision."""
    _reject_delimiters("cause", halt_state.cause)
    return " | ".join([
        now.isoformat(timespec="seconds"),
        "HALT",
        "cause=%s" % (halt_state.cause or "unknown"),
        "entered=%s" % _iso(halt_state.entered_at),
        "alerted=%s" % _iso(halt_state.last_alert_at),
    ])


# ---- sensor outage alert ----------------------------------------------------
# What the planner does while each kind of input is missing (the e-mail says so).
_SENSOR_EFFECTS = {
    "soc": "battery charge unknown: the planner holds (no charging, no exporting)",
    "power": "the household draw cannot be measured: the planner holds (no charging, no exporting)",
    "max_charge": "battery.max_charge_kw applies",
    "max_discharge": "battery.max_discharge_kw applies",
    "reserve": "battery.reserve_percent applies",
    "offtake": "peak protection and the grid budget cannot be evaluated",
    "quarter_hour_average": "peak protection falls back to its own estimate",
    "month_peak": "the ceiling and the month-peak notice cannot be evaluated",
    "forecast": "the cached solar forecast is used, or no solar is assumed",
    "history": "energy history for these blocks is recorded as unknown",
}


def required_sensors(cfg):
    """[(entity, kind)] for every configured sensor the planner reads each
    cycle, except the price entity (a price outage halts the planner and has
    its own alert). Each entity appears once."""
    out = []
    if cfg.soc_sensor:
        out.append((cfg.soc_sensor, "soc"))
    if cfg.power_sensor:
        out.append((cfg.power_sensor, "power"))
    if cfg.max_charge_sensor:
        out.append((cfg.max_charge_sensor, "max_charge"))
    if cfg.max_discharge_sensor:
        out.append((cfg.max_discharge_sensor, "max_discharge"))
    if cfg.reserve_sensor:
        out.append((cfg.reserve_sensor, "reserve"))
    out.append((cfg.forecast_entity, "forecast"))
    if cfg.capacity_enabled:
        out.append((cfg.offtake_sensor, "offtake"))
        out.append((cfg.quarter_hour_average_sensor, "quarter_hour_average"))
        out.append((cfg.month_peak_sensor, "month_peak"))
    if cfg.history_enabled:
        for attr in ("history_import_sensors", "history_export_sensors",
                     "history_solar_sensors", "history_battery_charge_sensors",
                     "history_battery_discharge_sensors",
                     "history_load_sensors"):
            for entity in getattr(cfg, attr):
                out.append((entity, "history"))
    seen = set()
    unique = []
    for entity, kind in out:
        if entity not in seen:
            seen.add(entity)
            unique.append((entity, kind))
    return unique


def outage_step(first_down, last_alert, down, now, after_minutes,
                realert_minutes):
    """One cycle of the sensor outage tracker. Pure.

    first_down: {entity: when it was first seen down}; last_alert: when the
    last alert went out (None = none for this outage); down: the entities down
    now. Returns (first_down, last_alert, due): the new state and the entities
    to report, or [] when no alert is due. An entity is reported once it has
    been down for after_minutes; the alert repeats every realert_minutes while
    any entity stays down, and the repeat timer resets when all are back.
    """
    first = {e: first_down.get(e, now) for e in down}
    long = sorted(e for e in first
                  if (now - first[e]).total_seconds() >= after_minutes * 60)
    if not long:
        return first, None, []
    if (last_alert is not None
            and (now - last_alert).total_seconds() < realert_minutes * 60):
        return first, last_alert, []
    return first, last_alert, long


def outage_message(kinds, first_down, now, realert_minutes):
    """(title, message) for the entities in kinds ({entity: kind})."""
    lines = []
    for entity in sorted(kinds):
        minutes = int((now - first_down[entity]).total_seconds() // 60)
        lines.append("- %s: unavailable for %d min; %s" % (
            entity, minutes, _SENSOR_EFFECTS[kinds[entity]]))
    count = len(lines)
    title = "Battery planner: %d sensor%s unavailable" % (
        count, "" if count == 1 else "s")
    message = ("The planner keeps running with fallbacks, but these inputs "
               "cannot be read:\n%s\nRe-alert every %d min while any stays "
               "unavailable." % ("\n".join(lines), realert_minutes))
    return title, message
