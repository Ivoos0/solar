"""Vetoes and selectors: decide what the battery does this cycle. Pure.

No I/O, no clock (``now`` is a parameter), no third-party / HA imports.

Entry point
-----------
    decide(trajectory, price_map, battery_state, grid_state, config, now,
           usage_history_available=False, forecast_available=True,
           soc_known=True, inputs_known=True) -> Decision

  trajectory     trajectory.Trajectory from trajectory.project()
  price_map      {block_start: prices.PricePoint}; keys may be aware or
                 fixed-offset datetimes. Joined to trajectory blocks by UTC
                 INSTANT only (never aware-datetime equality: the repeated
                 fall-back hour makes distinct instants compare equal).
  battery_state  battery.BatteryState
  grid_state     capacity.GridState, or None (then V3, S0 and the grid-charge
                 cap are inactive, exactly as when config.capacity_enabled is
                 False)
  now            aware datetime. The "current block" is the trajectory block
                 with start <= now < start + block_minutes (UTC compare).
                 NoCurrentBlockError if there is none; ValueError if naive.
  usage_history_available
                 True only when the usage profile rests on real history
                 (the adapter sets it from sample coverage). The default is the
                 SAFE value False: no history -> V4 holds every price-driven
                 selector (S1, S3, S4); only peak shaving (S0) still acts.
  forecast_available
                 False only when the solar series is the zero-solar fallback
                 (the forecast is missing or stale and no usable cache exists;
                 the adapter marks it solar_zero_fallback). Then V6 forbids
                 grid charging and export: the projection is a guess, and a
                 cost-bearing action must not rest on a guess. A stale but
                 usable cached series does NOT set it False. Default True
                 (other callers, the peak guard).
  soc_known      False only when a battery charge sensor is configured but
                 unreadable (the adapter marks it soc_unavailable). Then V7
                 forbids grid charging and export, and V1/V5, which need a
                 reading, are not evaluated. Default True (other callers).

Decision fields: action (charge|discharge|export|idle), target_power_kw,
selector ("S0", "S1", "S3".."S6"), reasoning, vetoes_fired (["V1", ...]),
suppressed ([(selector, action, blocking_veto)]), block_start (current block).
There is no S2: storing surplus solar is what the inverter does by default
(see "The inverter's default behaviour"), so no rule is needed. The numbering
is kept so labels in old logs keep their meaning. ``charge`` always means
charge from the grid.

The inverter's default behaviour
--------------------------------
When the planner sends nothing the inverter charges from solar surplus until
full and then exports, and drains to serve the house until empty and then uses
grid power. Sending ``idle`` returns it to that default. The rules therefore
only ever ASK for something that differs from the default: peak shaving,
grid charging, forced export. S6 (idle) means "leave the inverter to its
default", not "do nothing".

Structure (the point of this module)
------------------------------------
Vetoes FORBID an action class; selectors PROPOSE. Vetoes are established once,
before any selector runs, and never end evaluation. Selectors are tried in
order S0..S6; a proposal a veto forbids is recorded in ``suppressed`` and the
loop moves to the NEXT selector (it never returns, never restarts). A selector
whose condition simply does not hold is NOT recorded. S6 (idle) is never
forbidden, so the loop always terminates.

Action classes a veto can forbid: "discharge" (serve the house from the
battery), "export" (discharge to the grid), "grid_charge" (charge from the
grid).
  V1  charge_percent <= reserve_percent  -> {"export"} only
  V2  injection_price < 0 (0.0 does not) -> {"export"} only
  V3  capacity budget_kw <= 0            -> {"grid_charge"} only
  V4  no usable usage history            -> {"grid_charge", "export"}
  V5  battery empty (charge_percent <= 0
      or stored_kwh <= 0)                -> {"discharge"} only
  V6  no solar forecast (zero-solar
      fallback)                          -> {"grid_charge", "export"}
  V7  battery charge unreadable
      (soc_known False)                  -> {"grid_charge", "export"}
  V8  a sensor the calculation needs is
      missing (inputs_known False)       -> {"grid_charge", "export"}
V8 ("required input missing"): with the capacity tariff on, the budget and the
household draw need the grid sensors and battery.power_sensor. Without them
the numbers cannot be completed, so the planner falls back to idle (nothing
bought or exported); like V7 it does not stop peak shaving (S0).
V7 ("no battery reading"): a configured charge sensor that cannot be read is
not replaced by a guess. Buying or exporting energy needs the real charge, so
both are held; the battery keeps serving the house (the default) and peak
shaving (S0, discharge) is NOT suppressed by V7. V5 (empty battery) cannot be
judged without a reading, so with soc_known False it is not evaluated and the
peak guard may still shave.
RESERVE SEMANTICS: reserve_percent limits what the battery may EXPORT to the
grid; it is a floor for exporting, not a target to hold. The planner never buys
power to keep the battery up to the reserve (when the battery reaches it the
house simply imports), and peak shaving (S0) may use charge below the reserve.
The planner cannot know the inverter's own minimum charge (the driver / the
inverter enforces that), so the only lower bound it applies to a discharge is V5:
the battery is truly empty. V5 is a separate veto (not buried in S0) so an empty
battery shows up in the record's vetoes field as V5(suppressed S0 discharge).
V4 ("no usage profile"): without history the trajectory cannot know the
household load. A grid charge could land on top of an unseen peak, and the
spill/saturation maths of S3 are meaningless at zero usage. So every
price-driven selector (S1, S3, S4) is held and
the planner falls through to idle (S6), whose reasoning says "no usage history:
planner holds". V4 does NOT forbid "discharge": the only discharge proposal is
S0 peak shaving, which does not depend on usage history (it reads the live grid
state) and protects the capacity tariff, so it still acts (only V5, an empty
battery, can stop it). V4 fires whether or not capacity logic is active.
V6 ("no forecast"): with no forecast the trajectory assumes zero solar, so it
projects a battery that drains and a reserve breach that may never come (S4 would
over-buy from the grid), and it can never show a spill (S3). Being very
conservative, the planner then takes no cost-bearing action: grid charging and
export are forbidden. Peak shaving (S0) does not depend on the forecast and
still acts. Surplus solar needs no rule (the default behaviour stores it).
V3 and the grid-charge cap use the budget against stay_under_percent of the
ceiling (capacity.charging_ceiling_kw), e.g. 80 % of 2.5 kW = 2.0 kW; S0 and
the reported ceiling still use the real ceiling.

Resolved ambiguities / documented readings
------------------------------------------
* Effective value of a later injection price after round-trip losses is
  ALWAYS price * efficiency, for every sign of price. Storing 1 kWh returns
  only eff kWh later, so exporting later at a negative price costs LESS than
  now (-0.02 * 0.9 = -0.018 > -0.02); dividing would invert that.
* "Best remaining" injection price (S4, sale use) = priced blocks strictly AFTER the
  current block, to the horizon end. S3 compares against the window
  [current block, saturation_block) which includes now.
* S3/S4 window is [current, boundary): boundary = saturation_block (S3) /
  reserve_breach_block (S4), or the horizon end when that is None. If the
  boundary is at or before the current block the window is just the current
  block (saturation/breach is imminent, so acting now is the only option).
* S3 sells only when a spill is projected (the battery would be full and
  solar lost). It exports only the spare energy: the lowest projected charge
  above the reserve from now to saturation, capped by the charge now, so it
  never exports into a coming shortage. The power is the spare energy over
  the block, up to max_discharge_kw. Charge left over at the horizon end is
  never sold: what it would avoid buying beyond the horizon is unknown.
  Exporting now must also beat every other priced block of the window after
  round-trip losses (price * efficiency), so equal prices do not qualify.
* S4 shortfall = sum of grid_shortfall_kwh from the first reserve breach to
  the next refill to capacity (load the grid must serve because the battery
  sits at the floor; a shortage after a refill is a separate one that
  charging now cannot cover); N = max(1, ceil(shortfall
  / (max_charge_kw * block_hours))), capped at the window size. "Among the
  cheapest N" includes ties with the N-th cheapest price. S4 needs
  headroom > 0.
* S4 has two uses of a stored kWh and tries them in this order. (1) Import
  avoidance, below. (2) Sale: the best later injection price (after the
  current block, to the horizon end) times the round-trip efficiency; the
  candidate blocks are those before that sell block whose consumption price
  is below that value, and N = max(1, ceil(headroom / (max_charge_kw *
  block_hours))) capped at the candidates, so only the cheapest blocks that
  fill the battery's room charge (ties with the N-th included). The selector
  that was called S5 in older logs is this second use.
* S4 import avoidance acts only when charging is CHEAPER than simply importing at the breach.
  The reserve is a floor, not a target: when the battery gets there the house
  imports at that time. Charging now costs price_now / round_trip_efficiency
  per kWh delivered later; importing at the breach costs the energy-weighted
  average consumption price of the blocks that have grid shortfall (weights =
  their grid_shortfall_kwh; blocks without a published price are ignored; with
  no priced shortfall block S4 does nothing). Every candidate, the current
  block included, must satisfy price / efficiency < that weighted price,
  strictly; on a tie importing wins. Otherwise S4 proposes nothing and the
  loop falls through.
* Every grid-charging selector (S1, S4) clamps to budget_kw
  (capacity.budget_kw: charge power left after the household draw, capped there
  at max_charge_kw) and says so when the cap bit. budget_kw is 0.0 in the last minute of a window, so V3 then
  forbids grid charging; that is capacity.budget_kw semantics, not re-derived here.
* If the current block has no published price, every price selector (S1, S3, S4)
  is skipped and V2 cannot fire; S0 and S6 still work.
* A veto naming several causes is reported joined, e.g. "V1+V2" for export.
"""
import math
from dataclasses import dataclass, field
from datetime import timedelta, timezone

import capacity

FORBID_DISCHARGE = "discharge"
FORBID_EXPORT = "export"
FORBID_GRID_CHARGE = "grid_charge"

VETO_FORBIDS = {
    "V1": frozenset({FORBID_EXPORT}),
    "V2": frozenset({FORBID_EXPORT}),
    "V3": frozenset({FORBID_GRID_CHARGE}),
    "V4": frozenset({FORBID_GRID_CHARGE, FORBID_EXPORT}),
    "V5": frozenset({FORBID_DISCHARGE}),
    "V6": frozenset({FORBID_GRID_CHARGE, FORBID_EXPORT}),
    "V7": frozenset({FORBID_GRID_CHARGE, FORBID_EXPORT}),
    "V8": frozenset({FORBID_GRID_CHARGE, FORBID_EXPORT}),
}


class NoCurrentBlockError(Exception):
    """``now`` is not inside any trajectory block."""


@dataclass(frozen=True)
class Proposal:
    action: str            # charge (from the grid) | discharge | export | idle
    target_power_kw: float
    reasoning: str

    @property
    def action_class(self):
        """The class a veto can forbid, or None (idle)."""
        if self.action == "export":
            return FORBID_EXPORT
        if self.action == "discharge":
            return FORBID_DISCHARGE
        if self.action == "charge":
            return FORBID_GRID_CHARGE
        return None


@dataclass(frozen=True)
class Decision:
    action: str
    target_power_kw: float
    selector: str
    reasoning: str
    vetoes_fired: list = field(default_factory=list)
    suppressed: list = field(default_factory=list)  # (selector, action, veto)
    block_start: object = None


def _utc(dt):
    return dt.astimezone(timezone.utc)


def _hhmm(dt):
    return dt.strftime("%H:%M")


# ---- vetoes ---------------------------------------------------------------

def _capacity_active(grid_state, config):
    return grid_state is not None and config.capacity_enabled


def establish_vetoes(battery_state, current_price, config, grid_state=None,
                     usage_history_available=False, forecast_available=True,
                     soc_known=True, inputs_known=True):
    """Return (forbidden action classes, [fired veto ids]). Data only.

    usage_history_available defaults to the SAFE False (V4 fires).
    V1 (at or below the reserve) forbids only export; V5 (battery empty)
    forbids only discharge. forecast_available defaults to True (V6 quiet);
    False means the solar series is the zero-solar fallback and V6 fires.
    soc_known defaults to True; False means the charge sensor is unreadable:
    V7 fires and V1/V5 (which need the charge) are skipped.

    current_price is the PricePoint of the current block or None (no V2 then).
    Never chooses, logs or short-circuits.
    """
    fired = []
    if soc_known and battery_state.charge_percent <= config.reserve_percent:
        fired.append("V1")
    if current_price is not None and current_price.injection_price < 0:
        fired.append("V2")
    if _capacity_active(grid_state, config) \
            and capacity.budget_kw(grid_state, config) <= 0:
        fired.append("V3")
    if not usage_history_available:
        fired.append("V4")
    if soc_known and (battery_state.charge_percent <= 0
                      or battery_state.stored_kwh <= 0):
        fired.append("V5")
    if not forecast_available:
        fired.append("V6")
    if not soc_known:
        fired.append("V7")
    if not inputs_known:
        fired.append("V8")
    forbidden = frozenset()
    for v in fired:
        forbidden = forbidden | VETO_FORBIDS[v]
    return forbidden, fired


def _blocking_vetoes(action_class, fired):
    return "+".join(v for v in fired if action_class in VETO_FORBIDS[v])


# ---- context --------------------------------------------------------------

@dataclass(frozen=True)
class _Ctx:
    traj: object
    battery: object
    grid: object
    config: object
    idx: int                # current block index
    block: object
    price_now: object       # PricePoint | None
    prices: list            # per block: PricePoint | None (None if unpriced)
    hours: float


def _build_ctx(trajectory, price_map, battery_state, grid_state, config, now):
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    by_utc = {_utc(k): v for k, v in price_map.items()}
    step = timedelta(minutes=config.block_minutes)
    now_u = _utc(now)
    idx = None
    prices = []
    for i, b in enumerate(trajectory.blocks):
        s = _utc(b.block_start)
        if idx is None and s <= now_u < s + step:
            idx = i
        prices.append(by_utc.get(s) if b.has_price else None)
    if idx is None:
        raise NoCurrentBlockError(
            "no trajectory block contains %s" % now.isoformat())
    return _Ctx(trajectory, battery_state, grid_state, config, idx,
                trajectory.blocks[idx], prices[idx], prices,
                config.block_minutes / 60.0)


def _index_of(traj, instant):
    if instant is None:
        return None
    u = _utc(instant)
    for i, b in enumerate(traj.blocks):
        if _utc(b.block_start) == u:
            return i
    return None


def _window(ctx, boundary_instant):
    """Indices [current, boundary); just the current block if boundary <= it."""
    b = _index_of(ctx.traj, boundary_instant)
    n = len(ctx.traj.blocks)
    hi = n if b is None else b
    if hi <= ctx.idx:
        return [ctx.idx]
    return list(range(ctx.idx, hi))


def _priced(ctx, indices):
    """[(i, PricePoint)] skipping unpriced blocks."""
    return [(i, ctx.prices[i]) for i in indices if ctx.prices[i] is not None]


def _after_losses(price, efficiency):
    return price * efficiency


def _later_best_injection(ctx):
    """(index, PricePoint) of the best later injection price, or None.

    Earliest block wins ties. Only blocks after the current one, priced only.
    """
    later = _priced(ctx, range(ctx.idx + 1, len(ctx.traj.blocks)))
    best = None
    for i, p in later:
        if best is None or p.injection_price > best[1].injection_price:
            best = (i, p)
    return best


_TOL_KWH = 1e-6
_MIN_ENERGY_KWH = 0.01     # less than this is not worth a charge or export command


def _grid_power(ctx, wanted_kw):
    """Clamp a grid charge to the budget. Returns (kw, note or '')."""
    note = ""
    room_kw = ctx.battery.headroom_kwh / ctx.hours
    if room_kw < wanted_kw:
        note = (" (limited to the %.2f kWh of room left in the battery, "
                "inverter allows %.2f kW)" % (ctx.battery.headroom_kwh,
                                              wanted_kw))
        wanted_kw = room_kw
    if not _capacity_active(ctx.grid, ctx.config):
        return wanted_kw, note
    budget = capacity.budget_kw(ctx.grid, ctx.config)
    if budget < wanted_kw:
        return budget, (" (capped by grid budget %.2f kW, inverter allows "
                        "%.2f kW)" % (budget, wanted_kw))
    return wanted_kw, note


# ---- selectors ------------------------------------------------------------

def _s0(ctx):
    if not _capacity_active(ctx.grid, ctx.config):
        return None
    kw = capacity.shave_kw(ctx.grid, ctx.config)
    if kw <= 0:
        return None
    ceiling = capacity.ceiling_kw(ctx.grid, ctx.config)
    return Proposal(
        "discharge", kw,
        "peak shave: offtake %.2f kW, running average %.2f kW heading above "
        "the %.2f kW ceiling; discharge %.2f kW to the house to hold it"
        % (ctx.grid.offtake_kw, ctx.grid.running_average_kw, ceiling, kw))


def _s1(ctx):
    p = ctx.price_now
    if p is None or p.consumption_price >= 0:
        return None
    if ctx.battery.headroom_kwh <= _MIN_ENERGY_KWH:
        return None                       # full: nothing to charge into
    kw, note = _grid_power(ctx, ctx.config.max_charge_kw)
    return Proposal(
        "charge", kw,
        "consumption price %.4f EUR/kWh is negative (paid to consume): "
        "charge from grid at %.2f kW%s" % (p.consumption_price, kw, note))


def _exportable_kwh(ctx):
    """Energy the projection says is not needed before the battery refills.

    The lowest charge above the reserve over the window from now to the
    saturation block (inclusive) or the horizon end, also capped by the charge
    now. Exporting more than that would be bought back at the next shortage.
    """
    t = ctx.traj
    sat = _index_of(t, t.saturation_block)
    hi = len(t.blocks) if sat is None else max(sat + 1, ctx.idx + 1)
    reserve = ctx.config.capacity_kwh * ctx.config.reserve_percent / 100.0
    low = min(b.projected_charge_kwh for b in t.blocks[ctx.idx:hi])
    return max(0.0, min(low, ctx.battery.stored_kwh) - reserve)


def _s3(ctx):
    p, t = ctx.price_now, ctx.traj
    if p is None:
        return None
    # Only a projected spill justifies selling: the exported energy would
    # otherwise be lost when the battery is full. Charge left over at the
    # horizon end is never sold; what it would avoid buying beyond the
    # horizon is unknown, and buying and selling for a margin rarely pays.
    if not (t.total_spill_kwh > 0 and t.saturation_block is not None):
        return None
    cands = _priced(ctx, _window(ctx, t.saturation_block))
    if not cands:
        return None
    # Energy exported now is refilled from solar that would otherwise have
    # been exported at another price in the window, and the round trip loses
    # a share of it. So now must beat every other block after losses:
    # equal prices do not qualify.
    others = [c[1].injection_price for c in cands if c[0] != ctx.idx]
    if others and not _after_losses(
            p.injection_price, ctx.config.round_trip_efficiency) > max(others):
        return None
    spare = _exportable_kwh(ctx)
    if spare <= _MIN_ENERGY_KWH:
        return None
    kw = min(ctx.config.max_discharge_kw, spare / ctx.hours)
    return Proposal(
        "export", kw,
        "spill ahead %.2f kWh, saturation at %s; injection now %.4f "
        "EUR/kWh beats the other %d priced blocks in the window after "
        "round-trip losses; %.2f kWh is not needed before the battery "
        "refills: export at %.2f kW"
        % (t.total_spill_kwh, _hhmm(t.saturation_block), p.injection_price,
           len(others), spare, kw))


def _breach_span(ctx):
    """Block indices from the first reserve breach to the next refill.

    Charging now can only cover the shortage that starts at the breach and
    lasts until the battery is full again (a later shortage is a separate
    one, after a refill that needs no grid energy). The refill is the first
    block after the breach that reaches capacity; none means the horizon end.
    """
    start = _index_of(ctx.traj, ctx.traj.reserve_breach_block)
    blocks = ctx.traj.blocks
    full = ctx.config.capacity_kwh - _TOL_KWH
    for i in range(start + 1, len(blocks)):
        if blocks[i].projected_charge_kwh >= full:
            return range(start, i)
    return range(start, len(blocks))


def _breach_import_price(ctx, span):
    """Energy-weighted average consumption price of the shortfall blocks.

    The blocks where the projection has the grid serving load because the
    battery sits at the reserve; weights are their grid_shortfall_kwh. Blocks
    without a published price are ignored. None when no priced block has a
    shortfall. span: the block indices to look at.
    """
    energy = cost = 0.0
    for i in span:
        p = ctx.prices[i]
        kwh = ctx.traj.blocks[i].grid_shortfall_kwh
        if p is None or kwh <= 0:
            continue
        energy += kwh
        cost += kwh * p.consumption_price
    if energy <= 0:
        return None
    return cost / energy


def _s4_import(ctx):
    p, t, cfg = ctx.price_now, ctx.traj, ctx.config
    if t.reserve_breach_block is None:
        return None
    cands = _priced(ctx, _window(ctx, t.reserve_breach_block))
    if not cands:
        return None
    span = _breach_span(ctx)
    import_price = _breach_import_price(ctx, span)
    if import_price is None:
        return None
    eff = cfg.round_trip_efficiency
    # the reserve is a floor, not a target: charge only when cheaper than the
    # import it would replace, every candidate block judged after losses
    # (the current block is in the window, so it is judged here too)
    cands = [c for c in cands if c[1].consumption_price / eff < import_price]
    if not cands:
        return None
    shortfall = sum(t.blocks[i].grid_shortfall_kwh for i in span)
    per_block = cfg.max_charge_kw * ctx.hours
    n = min(len(cands), max(1, math.ceil(shortfall / per_block - 1e-9)))
    ranked = sorted(c[1].consumption_price for c in cands)
    threshold = ranked[n - 1]
    if p.consumption_price > threshold:
        return None
    kw, note = _grid_power(ctx, cfg.max_charge_kw)
    return Proposal(
        "charge", kw,
        "reserve breach at %s, shortfall %.2f kWh until the next refill needs "
        "%d block(s) at "
        "%.2f kW; charging now costs %.4f EUR/kWh after losses vs %.4f "
        "importing at the breach, and consumption price now %.4f is within "
        "the cheapest %d of %d qualifying blocks before the breach (cutoff "
        "%.4f): charge from grid at %.2f kW%s"
        % (_hhmm(t.reserve_breach_block), shortfall, n, cfg.max_charge_kw,
           p.consumption_price / eff, import_price, p.consumption_price, n,
           len(cands), threshold, kw, note))


def _s4_sale(ctx):
    """Charge now to sell later: stored energy is worth the best later
    injection price after round-trip losses.

    The charge blocks are the ones before that sell block that beat its value;
    the battery needs only enough of them to fill its room, so only the
    cheapest N charge.
    """
    p, cfg = ctx.price_now, ctx.config
    best = _later_best_injection(ctx)
    if best is None:
        return None
    sell_idx, sell = best
    value = _after_losses(sell.injection_price, cfg.round_trip_efficiency)
    cands = [c for c in _priced(ctx, range(ctx.idx, sell_idx))
             if c[1].consumption_price < value]
    if not cands or not p.consumption_price < value:
        return None
    per_block = cfg.max_charge_kw * ctx.hours
    n = min(len(cands),
            max(1, math.ceil(ctx.battery.headroom_kwh / per_block - 1e-9)))
    threshold = sorted(c[1].consumption_price for c in cands)[n - 1]
    if p.consumption_price > threshold:
        return None
    kw, note = _grid_power(ctx, cfg.max_charge_kw)
    return Proposal(
        "charge", kw,
        "arbitrage: injection %.4f at %s after efficiency %.2f = %.4f beats "
        "consumption price now %.4f (spread %.4f EUR/kWh after losses), and "
        "now is within the cheapest %d of %d qualifying blocks before it "
        "(cutoff %.4f): charge from grid at %.2f kW%s"
        % (sell.injection_price, _hhmm(ctx.traj.blocks[sell_idx].block_start),
           cfg.round_trip_efficiency, value, p.consumption_price,
           value - p.consumption_price, n, len(cands), threshold, kw, note))


def _s4(ctx):
    """Charge from the grid when a stored kWh is worth more than it costs:
    the import it avoids at a coming shortage, else the later sale."""
    if ctx.price_now is None or ctx.battery.headroom_kwh <= _MIN_ENERGY_KWH:
        return None
    return _s4_import(ctx) or _s4_sale(ctx)


def _s6_reasoning(ctx, fired, suppressed):
    t, p, cfg = ctx.traj, ctx.price_now, ctx.config
    facts = []
    facts.append("spill ahead %.2f kWh" % t.total_spill_kwh
                 if t.total_spill_kwh > 0 else "no spill ahead")
    facts.append("leftover %.2f kWh" % t.leftover_kwh)
    facts.append("reserve breach at %s" % _hhmm(t.reserve_breach_block)
                 if t.reserve_breach_block is not None
                 else "no reserve breach")
    if p is None:
        facts.append("no price published for the current block")
    else:
        best = _later_best_injection(ctx)
        if best is None:
            facts.append("no later priced block to arbitrage against")
        else:
            v = _after_losses(best[1].injection_price,
                              cfg.round_trip_efficiency)
            facts.append(
                "spread too narrow (best later injection %.4f, %.4f after "
                "losses, vs consumption now %.4f)"
                % (best[1].injection_price, v, p.consumption_price))
    if _capacity_active(ctx.grid, cfg):
        facts.append("no peak forming (grid budget %.2f kW)"
                     % capacity.budget_kw(ctx.grid, cfg))
    if fired:
        facts.append("vetoes fired: %s" % ", ".join(fired))
    if suppressed:
        facts.append("suppressed: " + ", ".join(
            "%s %s blocked by %s" % s for s in suppressed))
    if "V7" in fired:
        facts.append("battery charge unreadable: nothing is bought or "
                     "exported without a reading")
    if "V8" in fired:
        facts.append("a required sensor is missing (see degraded): nothing is "
                     "bought or exported without it")
    if "V6" in fired:
        facts.append("no solar forecast: nothing is bought or exported on a "
                     "guess")
    if "V7" in fired:
        return ("hold: no battery reading: planner holds (only peak shaving "
                "acts) - " + "; ".join(facts))
    if "V8" in fired:
        return ("hold: required sensor missing: planner holds (only peak "
                "shaving acts) - " + "; ".join(facts))
    if "V4" in fired:
        return ("hold: no usage history: planner holds (only peak shaving "
                "acts) - " + "; ".join(facts))
    if "V6" in fired:
        return ("hold: no solar forecast: planner holds (only peak shaving "
                "acts) - " + "; ".join(facts))
    return ("hold: nothing applies, the inverter keeps its default behaviour - "
            + "; ".join(facts))


# S0 leads: a capacity peak is billed across the next twelve months while a
# price opportunity pays once. Even a very good arbitrage hour is worth cents
# where a peak increase is worth tens of euros. Do not "optimise" this order.
_SELECTORS = (("S0", _s0), ("S1", _s1), ("S3", _s3), ("S4", _s4))


def decide(trajectory, price_map, battery_state, grid_state, config, now,
           usage_history_available=False, forecast_available=True,
           soc_known=True, inputs_known=True):
    """Establish vetoes, then try S0..S6 in order; see module docstring."""
    ctx = _build_ctx(trajectory, price_map, battery_state, grid_state,
                     config, now)
    forbidden, fired = establish_vetoes(battery_state, ctx.price_now, config,
                                        grid_state, usage_history_available,
                                        forecast_available, soc_known,
                                        inputs_known)
    suppressed = []
    for name, selector in _SELECTORS:
        proposal = selector(ctx)
        if proposal is None:
            continue                      # did not apply: not recorded
        cls = proposal.action_class
        if cls is not None and cls in forbidden:
            suppressed.append(
                (name, proposal.action, _blocking_vetoes(cls, fired)))
            continue                      # vetoed: try the NEXT selector
        return Decision(proposal.action, proposal.target_power_kw, name,
                        proposal.reasoning, list(fired), suppressed,
                        ctx.block.block_start)
    return Decision("idle", 0.0, "S6",
                    _s6_reasoning(ctx, fired, suppressed), list(fired),
                    suppressed, ctx.block.block_start)
