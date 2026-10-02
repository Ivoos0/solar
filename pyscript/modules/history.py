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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = 1
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
    "split_method", "forecast_solar_kwh", "consumption_price",
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
