"""Energy history: per-block records derived from cumulative kWh counters.

Pure - no I/O, no clock, no Home Assistant. The adapter reads the counters,
hands them in, and stores the lines this module produces.

Model
-----
A *snapshot* holds the summed cumulative counters (kWh) read at (just after) a
block boundary. The record of block B is the difference between the snapshot at
B and the snapshot at B + block_minutes. Rules that keep the numbers honest:

* A quantity is a SUM of its counters (tariff 1 + tariff 2). If any counter of a
  quantity could not be read, the whole quantity is unknown (None), never a
  partial sum.
* A negative delta (counter reset or replacement) makes that quantity None.
* A missing earlier snapshot, or a gap of more than one block between the two
  snapshots, yields null records for the blocks in between. One delta is NEVER
  spread over several blocks.
* The real read time of both snapshots is stored, so a late read is visible.

Household load (kWh):
  measured  = delta of the `load` counter, when configured and readable
  derived   = import - export + solar + battery_discharge - battery_charge,
              only when ALL five terms are known (else None)
Load split (priority rule "priority_v1": load is served by solar first, then by
the battery, the rest comes from the net). Needs load, solar and
battery_discharge:
  load_from_solar   = min(load, solar)
  load_from_battery = min(load - load_from_solar, battery_discharge)
  load_from_net     = load - load_from_solar - load_from_battery
This is a convention, not a measurement: the counters are block totals, so the
true instant-by-instant mix is unknowable.
"""
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

SCHEMA = 1

# Solar calibration (see "Solar calibration" at the end of this file).
# A block's solar_ratio is stored only when the forecast was at least this big:
# the ratio of two tiny numbers (dawn, dusk, heavy cloud) is noise.
SOLAR_RATIO_MIN_FORECAST_KWH = 0.05
SOLAR_RATIO_WINDOW_MINUTES = 60      # +- this many minutes around a time of day
SOLAR_RATIO_MIN_BLOCKS = 12          # fewer contributing blocks: use the default
SOLAR_RATIO_MAX = 2.0                # applied ratios are clamped to [0, this]
SPLIT_METHOD = "priority_v1"
QUANTITIES = ("import", "export", "solar", "battery_charge",
              "battery_discharge", "load")
MAX_GAP_RECORDS = 96            # null records written for one gap (a day)

# Energy fields of a record, in write order.
_ENERGY_FIELDS = (
    "import_kwh", "export_kwh", "solar_kwh", "battery_charge_kwh",
    "battery_discharge_kwh",
)
RECORD_FIELDS = (
    "schema", "block_start", "local_date", "block_minutes",
    "import_kwh", "export_kwh", "solar_kwh", "battery_charge_kwh",
    "battery_discharge_kwh", "load_kwh", "load_source", "from_net_kwh",
    "load_from_solar_kwh", "load_from_battery_kwh", "load_from_net_kwh",
    "split_method", "forecast_solar_kwh", "solar_ratio", "consumption_price",
    "injection_price", "soc_percent", "complete", "start_read_at",
    "snapshot_read_at",
)


def _utc(dt):
    return dt.astimezone(timezone.utc)


def block_floor(when, block_minutes):
    """Start of the block containing `when`, as an aware UTC datetime.

    Floors in UTC minutes, which equals local flooring for whole-hour and
    half-hour offsets, and is DST-safe (no wall-clock arithmetic).
    """
    utc = _utc(when).replace(second=0, microsecond=0)
    return utc.replace(minute=(utc.minute // block_minutes) * block_minutes)


@dataclass(frozen=True)
class Snapshot:
    """Counters at the start of the block `boundary`, read at `read_at`.

    totals: {quantity: kWh or None}; None = not configured OR unreadable.
    failed: quantities that are configured but unreadable (marks incomplete).
    The forecast, prices and SOC are what applied to the block STARTING at
    `boundary` (they become part of that block's record).
    """
    boundary: datetime
    read_at: datetime
    totals: dict
    failed: tuple = ()
    forecast_solar_kwh: object = None
    consumption_price: object = None
    injection_price: object = None
    soc_percent: object = None


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def make_snapshot(boundary, read_at, readings, forecast_solar_kwh=None,
                  consumption_price=None, injection_price=None,
                  soc_percent=None):
    """Build a Snapshot. readings: {quantity: [kWh or None, ...]}.

    An empty list means "quantity not configured" (total None, not a failure).
    Any None in a non-empty list makes the quantity unknown AND failed.
    """
    totals, failed = {}, []
    for q in QUANTITIES:
        values = readings.get(q) or []
        if not values:
            totals[q] = None
        elif all(_finite(v) for v in values):
            totals[q] = float(sum(values))
        else:
            totals[q] = None
            failed.append(q)
    return Snapshot(_utc(boundary), _utc(read_at), totals, tuple(failed),
                    forecast_solar_kwh, consumption_price, injection_price,
                    soc_percent)


def snapshot_to_dict(snap):
    return {
        "boundary": snap.boundary.isoformat(),
        "read_at": snap.read_at.isoformat(),
        "totals": dict(snap.totals),
        "failed": list(snap.failed),
        "forecast_solar_kwh": snap.forecast_solar_kwh,
        "consumption_price": snap.consumption_price,
        "injection_price": snap.injection_price,
        "soc_percent": snap.soc_percent,
    }


def snapshot_from_dict(data):
    """Snapshot from a persisted dict, or None when absent/corrupt."""
    try:
        totals = {q: data["totals"].get(q) for q in QUANTITIES}
        for v in totals.values():
            if v is not None and not _finite(v):
                return None
        return Snapshot(
            _utc(datetime.fromisoformat(data["boundary"])),
            _utc(datetime.fromisoformat(data["read_at"])),
            totals, tuple(data.get("failed") or ()),
            data.get("forecast_solar_kwh"), data.get("consumption_price"),
            data.get("injection_price"), data.get("soc_percent"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _delta(start, end):
    """end - start rounded to 6 decimals (kills float noise); None on reset."""
    if start is None or end is None:
        return None
    diff = round(end - start, 6)
    return diff if diff >= 0 else None          # negative = counter reset


def derive_load(import_kwh, export_kwh, solar_kwh, discharge_kwh, charge_kwh):
    """import - export + solar + discharge - charge; None if any term unknown."""
    terms = (import_kwh, export_kwh, solar_kwh, discharge_kwh, charge_kwh)
    if any(t is None for t in terms):
        return None
    return round(import_kwh - export_kwh + solar_kwh + discharge_kwh
                 - charge_kwh, 6)


def split_load(load, solar, discharge):
    """(from_solar, from_battery, from_net) by the priority rule, or Nones."""
    if load is None or solar is None or discharge is None:
        return None, None, None
    load = max(load, 0.0)
    from_solar = min(load, solar)
    from_battery = min(load - from_solar, discharge)
    return from_solar, from_battery, round(load - from_solar - from_battery, 6)


def _blank(block_start, block_minutes, tz):
    local = _utc(block_start).astimezone(tz)
    rec = dict.fromkeys(RECORD_FIELDS)
    rec.update(schema=SCHEMA, block_start=_utc(block_start).isoformat(),
               local_date=local.date().isoformat(),
               block_minutes=block_minutes, complete=False)
    return rec


def block_record(start, end, block_minutes, tz):
    """Record of the block [start.boundary, end.boundary). Adjacent snapshots."""
    rec = _blank(start.boundary, block_minutes, tz)
    d = {q: _delta(start.totals.get(q), end.totals.get(q)) for q in QUANTITIES}
    for q, name in (("import", "import_kwh"), ("export", "export_kwh"),
                    ("solar", "solar_kwh"),
                    ("battery_charge", "battery_charge_kwh"),
                    ("battery_discharge", "battery_discharge_kwh")):
        rec[name] = d[q]
    if d["load"] is not None:
        rec["load_kwh"], rec["load_source"] = d["load"], "measured"
    else:
        derived = derive_load(d["import"], d["export"], d["solar"],
                              d["battery_discharge"], d["battery_charge"])
        if derived is not None:
            rec["load_kwh"], rec["load_source"] = max(derived, 0.0), "derived"
    rec["from_net_kwh"] = d["import"]
    s, b, n = split_load(rec["load_kwh"], d["solar"], d["battery_discharge"])
    rec["load_from_solar_kwh"], rec["load_from_battery_kwh"] = s, b
    rec["load_from_net_kwh"] = n
    if s is not None:
        rec["split_method"] = SPLIT_METHOD
    rec["forecast_solar_kwh"] = start.forecast_solar_kwh
    rec["solar_ratio"] = solar_ratio_of(d["solar"], start.forecast_solar_kwh)
    rec["consumption_price"] = start.consumption_price
    rec["injection_price"] = start.injection_price
    rec["soc_percent"] = start.soc_percent
    configured = [q for q in QUANTITIES
                  if start.totals.get(q) is not None
                  or end.totals.get(q) is not None
                  or q in start.failed or q in end.failed]
    rec["complete"] = (not start.failed and not end.failed
                       and all(d[q] is not None for q in configured))
    rec["start_read_at"] = start.read_at.isoformat()
    rec["snapshot_read_at"] = end.read_at.isoformat()
    return rec


def records_between(previous, current, block_minutes, tz):
    """Records for the blocks finished between two snapshots (oldest first).

    previous None -> []. Adjacent -> one real record. A longer gap -> null
    records (complete False, no energy, no delta) for the missed blocks, at most
    MAX_GAP_RECORDS of the most recent ones. current not after previous -> [].
    """
    if previous is None or current.boundary <= previous.boundary:
        return []
    step = timedelta(minutes=block_minutes)
    count = (current.boundary - previous.boundary) // step
    if count <= 0:
        return []
    if count == 1:
        return [block_record(previous, current, block_minutes, tz)]
    first = max(0, count - MAX_GAP_RECORDS)
    return [_blank(previous.boundary + i * step, block_minutes, tz)
            for i in range(first, count)]


# ---- serialisation ------------------------------------------------------------

def to_line(record):
    """One JSON object, no newline."""
    return json.dumps(record, separators=(",", ":"))


def parse_lines(text):
    """(records, bad_line_count). Blank lines are ignored, not counted bad."""
    records, bad = [], 0
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            datetime.fromisoformat(obj["block_start"])
            if not isinstance(obj, dict):
                raise TypeError("not an object")
        except (ValueError, KeyError, TypeError):
            bad += 1
            continue
        records.append(obj)
    return records, bad


def _start(record):
    return _utc(datetime.fromisoformat(record["block_start"]))


def usage_series(records, since=None):
    """[(aware UTC block_start, load_kwh)] oldest first, for series.usage_profile.

    Only records with a finite non-negative load_kwh. Records are keyed by the
    UTC instant, so the 100 blocks of a fall-back day stay distinct; a duplicate
    block_start (restart between write and snapshot save) keeps the last one.
    `since` (aware) drops older blocks.
    """
    by_start = {}
    for rec in records:
        load = rec.get("load_kwh")
        if not _finite(load) or load < 0:
            continue
        try:
            start = _start(rec)
        except (KeyError, TypeError, ValueError):
            continue
        if since is not None and start < _utc(since):
            continue
        by_start[start] = float(load)
    return sorted(by_start.items())


def solar_realisation_ratio(records, weeks, min_forecast_kwh=1.0):
    """sum(actual solar) / sum(forecast solar) over the trailing `weeks` weeks.

    The window ends at the newest block_start in `records` (no clock). Only
    blocks having BOTH solar_kwh and forecast_solar_kwh count. None when no such
    block exists or the forecast sum is below `min_forecast_kwh` (too little to
    divide by). NOT applied to any decision yet: recorded for later use.
    """
    pairs = []
    for rec in records:
        actual, forecast = rec.get("solar_kwh"), rec.get("forecast_solar_kwh")
        if not (_finite(actual) and _finite(forecast)):
            continue
        try:
            pairs.append((_start(rec), actual, forecast))
        except (KeyError, TypeError, ValueError):
            continue
    if not pairs:
        return None
    cutoff = max(p[0] for p in pairs) - timedelta(weeks=weeks)
    chosen = [p for p in pairs if p[0] > cutoff]
    total_forecast = sum(p[2] for p in chosen)
    if total_forecast < min_forecast_kwh or total_forecast <= 0:
        return None
    return sum(p[1] for p in chosen) / total_forecast


# ---- solar calibration -----------------------------------------------------------
#
# The forecast tends to over- or underestimate (for example real solar is
# consistently about 80 % of the forecast). Every block record stores
# solar_ratio = measured solar / forecast solar (None unless both are known and
# the forecast was at least SOLAR_RATIO_MIN_FORECAST_KWH). The planner corrects
# the forecast with a ratio per time of day:
#
# * Fewer than `weeks` weeks of history (the span between the oldest and the
#   newest block that has a ratio is under 7 * weeks days): the configured
#   default for every block.
# * Otherwise, for a forecast block starting at local time of day T: over all
#   history blocks of the last `weeks` weeks whose local time of day is within
#   +- SOLAR_RATIO_WINDOW_MINUTES of T (inclusive; 9 blocks a day at 15 minutes)
#   and that have a ratio,
#       ratio(T) = sum(measured solar) / sum(forecast solar).
#   This energy-weighted average is used instead of a plain mean of the
#   per-block ratios because a plain mean lets a block with 0.06 kWh forecast
#   (and a noisy 0.12 measured, ratio 2.0) count as much as a midday block of
#   1.2 kWh: the total energy error is what matters, so big blocks must weigh
#   more. With fewer than SOLAR_RATIO_MIN_BLOCKS contributing blocks the default
#   is used for that T.
# * The applied ratio is clamped to [0, SOLAR_RATIO_MAX] at use time; the stored
#   per-block value is never clamped.
#
# Pure functions: the adapter reads the files and the clock, caches the result
# for the hour, and applies it on top of the RAW forecast series every cycle.

def solar_ratio_of(solar_kwh, forecast_kwh):
    """measured / forecast solar of one block, or None when either is unknown
    (or negative) or the forecast is below SOLAR_RATIO_MIN_FORECAST_KWH."""
    if not (_finite(solar_kwh) and _finite(forecast_kwh)):
        return None
    if solar_kwh < 0 or forecast_kwh < SOLAR_RATIO_MIN_FORECAST_KWH:
        return None
    return round(solar_kwh / forecast_kwh, 4)


def _ratio_blocks(records, tz):
    """[(start UTC, local minute of day, solar kWh, forecast kWh)], oldest first.

    Worked out from solar_kwh and forecast_solar_kwh with the rule of
    solar_ratio_of, so records written before the solar_ratio field existed
    count too. A duplicate block_start keeps the last record.
    """
    by_start = {}
    for rec in records:
        solar, forecast = rec.get("solar_kwh"), rec.get("forecast_solar_kwh")
        if solar_ratio_of(solar, forecast) is None:
            continue
        try:
            start = _start(rec)
        except (KeyError, TypeError, ValueError):
            continue
        local = start.astimezone(tz)
        by_start[start] = (start, local.hour * 60 + local.minute,
                           float(solar), float(forecast))
    return [by_start[k] for k in sorted(by_start)]


def default_calibration(default, weeks):
    """Calibration that applies the configured default everywhere."""
    return {"default": default, "weeks": weeks, "span_days": 0.0,
            "enough": False, "ratios": {}}


def solar_calibration(records, now, weeks, default, tz, block_minutes):
    """Per-time-of-day solar ratios from history `records`.

    Returns {"default", "weeks", "span_days" (oldest to newest block that has a
    ratio), "enough" (that span >= 7 * weeks days), "ratios"}; ratios maps a
    local minute of day (a multiple of block_minutes) to the measured ratio for
    blocks starting then, only where at least SOLAR_RATIO_MIN_BLOCKS blocks
    contribute. Anything not in ratios uses the default. No clock: `now` ends
    the window.
    """
    cal = default_calibration(default, weeks)
    blocks = _ratio_blocks(records, tz)
    if not blocks:
        return cal
    span = blocks[-1][0] - blocks[0][0]
    cal["span_days"] = span.total_seconds() / 86400.0
    if span < timedelta(days=7 * weeks):
        return cal
    cal["enough"] = True
    end = _utc(now)
    cutoff = end - timedelta(days=7 * weeks)
    by_minute = {}                         # local minute of day -> [solar, forecast, n]
    for start, minute, solar, forecast in blocks:
        if start < cutoff or start > end:
            continue
        cell = by_minute.setdefault(minute, [0.0, 0.0, 0])
        cell[0] += solar
        cell[1] += forecast
        cell[2] += 1
    ratios = {}
    for target in range(0, 24 * 60, block_minutes):
        solar_sum, forecast_sum, count = 0.0, 0.0, 0
        for minute, cell in by_minute.items():
            gap = abs(minute - target)
            gap = min(gap, 24 * 60 - gap)            # around midnight too
            if gap <= SOLAR_RATIO_WINDOW_MINUTES:
                solar_sum += cell[0]
                forecast_sum += cell[1]
                count += cell[2]
        if count >= SOLAR_RATIO_MIN_BLOCKS and forecast_sum > 0:
            ratios[target] = min(max(solar_sum / forecast_sum, 0.0),
                                 SOLAR_RATIO_MAX)
    cal["ratios"] = ratios
    return cal


def _minute_of_block(when, tz, block_minutes):
    local = when.astimezone(tz)
    minute = local.hour * 60 + local.minute
    return minute - minute % block_minutes


def calibration_ratio(cal, minute_of_day):
    """(ratio, "measured" | "configured") for a block starting at this local
    minute of day. Clamped to [0, SOLAR_RATIO_MAX]."""
    measured = cal["ratios"].get(minute_of_day)
    if measured is not None:
        return min(max(measured, 0.0), SOLAR_RATIO_MAX), "measured"
    return min(max(cal["default"], 0.0), SOLAR_RATIO_MAX), "configured"


def apply_solar_calibration(slots, cal, tz, block_minutes):
    """New ForecastSlot list with each block's expected_kwh times its ratio.

    Takes the RAW series and returns a new list (slots are frozen): applying it
    to its own output would apply the ratio twice, so the adapter always starts
    from the raw series. Zero-fallback slots stay as they are.
    """
    out = []
    for slot in slots:
        if slot.is_zero_fallback:
            out.append(slot)
            continue
        ratio, _ = calibration_ratio(
            cal, _minute_of_block(slot.block_start, tz, block_minutes))
        out.append(slot if ratio == 1.0
                   else replace(slot, expected_kwh=slot.expected_kwh * ratio))
    return out


def current_solar_ratio(cal, now, tz, block_minutes):
    """(ratio, source) applied to the block containing `now`."""
    return calibration_ratio(
        cal, _minute_of_block(block_floor(now, block_minutes), tz, block_minutes))


def weeks_of_history(cal):
    """Weeks between the oldest and newest block with a ratio (one decimal)."""
    return round(cal["span_days"] / 7.0, 1)


def calibration_marker(cal, raw_slots, now, tz, block_minutes):
    """Degraded marker for the current and the next block, or None.

    "solar_ratio=0.83" for a measured ratio, "solar_ratio_configured=0.80" for
    the configured one; none when the ratio rounds to 1.00. Blocks without
    forecast solar are skipped: the ratio changes nothing there (at night a
    configured 0.8 would otherwise mark every record).
    """
    step = timedelta(minutes=block_minutes)
    current = block_floor(now, block_minutes)
    by_start = {}
    for slot in raw_slots:
        if not slot.is_zero_fallback and slot.expected_kwh > 0:
            by_start[_utc(slot.block_start)] = slot
    for start in (current, current + step):
        if start not in by_start:
            continue
        ratio, source = calibration_ratio(
            cal, _minute_of_block(start, tz, block_minutes))
        if round(ratio, 2) == 1.0:
            continue
        if source == "measured":
            return "solar_ratio=%.2f" % ratio
        return "solar_ratio_configured=%.2f" % ratio
    return None
