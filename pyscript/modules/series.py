"""Solar and usage input series on the block grid. Pure - no I/O.

Both series are DENSE and CONTIGUOUS: one entry per block from start_time up
to (excluding) horizon_end, no holes. Downstream joins are by block_start.

Unit traps handled here:
  * forecast.solar reports watt-hours; the core works in kWh (divide by 1000).
  * Energy is EXTENSIVE: an hourly period split into four blocks divides its
    energy (a quarter each). Prices are intensive and repeat instead.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_WH_PER_KWH = 1000.0


@dataclass(frozen=True)
class ForecastSlot:
    block_start: datetime
    expected_kwh: float
    is_zero_fallback: bool
    source_resolution_minutes: int


@dataclass(frozen=True)
class UsageSlot:
    block_start: datetime
    expected_kwh: float
    sample_days: int


def _tz(config):
    return ZoneInfo(config.timezone)


def _aware(dt, config):
    return dt.replace(tzinfo=_tz(config)) if dt.tzinfo is None else dt


def _grid(config, start_time, horizon_end):
    """Block start times from start_time up to (excluding) horizon_end.

    Steps in absolute (UTC) time and converts back to local, so the repeated
    fall-back hour is kept (100 blocks that day) and nonexistent spring times
    are never invented (92 blocks). Wall-clock arithmetic on ZoneInfo
    datetimes is wrong across DST and is avoided.
    """
    step = timedelta(minutes=config.block_minutes)
    start = _aware(start_time, config)
    end = _aware(horizon_end, config).astimezone(timezone.utc)
    local = start.tzinfo
    out = []
    t = start.astimezone(timezone.utc)
    while t < end:
        out.append(t.astimezone(local))
        t += step
    return out


def zero_solar_series(config, start_time, horizon_end):
    """All-zero forecast across the horizon, flagged on every block."""
    return [
        ForecastSlot(t, 0.0, True, config.block_minutes)
        for t in _grid(config, start_time, horizon_end)
    ]


def _parse_payload(payload, config):
    """Return sorted [(period_start, energy_kwh)] or None if unusable."""
    if not payload or not hasattr(payload, "items"):
        return None
    parsed = []
    try:
        for key, wh in payload.items():
            ts = _aware(datetime.fromisoformat(str(key)), config).astimezone(timezone.utc)
            energy_kwh = float(wh) / _WH_PER_KWH
            if energy_kwh != energy_kwh or energy_kwh in (float("inf"), float("-inf")):
                return None
            parsed.append((ts, energy_kwh))
    except (ValueError, TypeError):
        return None
    if not parsed:
        return None
    parsed.sort(key=lambda p: p[0])
    return parsed


_DEFAULT_PERIOD_MIN = 60
_MAX_PERIOD_MIN = 60


def _periods(parsed):
    """Return [(p_start, p_end, energy_kwh, minutes)] with END-of-period keys.

    Each key marks the END of its period; the period starts at the previous
    key. The first entry (no predecessor) and any entry whose gap is
    non-positive use the hourly default; gaps longer than _MAX_PERIOD_MIN
    (e.g. sunset to next sunrise) are capped so a value is not smeared across
    the night: the period is then the _MAX_PERIOD_MIN just before the key.
    """
    out = []
    prev = None
    for ts, kwh in parsed:
        gap = (ts - prev).total_seconds() / 60.0 if prev is not None else 0.0
        if gap <= 0:
            gap = _DEFAULT_PERIOD_MIN
        gap = min(gap, _MAX_PERIOD_MIN)
        out.append((ts - timedelta(minutes=gap), ts, kwh, gap))
        prev = ts
    return out


def solar_series(payload, config, start_time, horizon_end):
    """Per-block expected solar energy (kWh) from a watt_hours_period mapping.

    Timestamp semantics: each key marks the END of its period. forecast.solar
    watt_hours_period is the energy produced in the period since the previous
    timestamp (https://doc.forecast.solar/api:estimate names the route but does
    not state the boundary; end semantics is inferred from the sunrise entry
    being ~0 and from the cumulative watt_hours series), which is why
    the sunrise entry is ~0 and the first daytime entry carries the energy of
    the (short) interval before it. Each entry's period runs from the previous
    key to its own key, so uneven sunrise/sunset timestamps are handled
    per entry rather than through one global resolution. Gaps over 60 minutes
    are capped at 60 (the period is the 60 minutes before the key); the first
    entry uses an hourly default.

    Energy is extensive: it is split over the blocks a period overlaps in
    proportion to overlap. Each block records the length of the period that
    covers most of it as source_resolution_minutes (modal spacing when no
    period covers it).

    Falls back to a flagged zero series when the payload is None, empty or
    unparseable. Blocks beyond the payload are 0.0; the horizon is never
    truncated to the coverage of the forecast.
    """
    parsed = _parse_payload(payload, config)
    if parsed is None:
        return zero_solar_series(config, start_time, horizon_end)

    periods = _periods(parsed)
    counts = {}
    for *_, minutes in periods:
        m = int(round(minutes))
        counts[m] = counts.get(m, 0) + 1
    modal = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    block = timedelta(minutes=config.block_minutes)
    starts = _grid(config, start_time, horizon_end)
    # Periods are in UTC; blocks are keyed by index, not by aware datetime
    # (which would conflate the repeated fall-back hour).
    utc_starts = [t.astimezone(timezone.utc) for t in starts]
    kwh = [0.0] * len(starts)
    best = {}  # block index -> (overlap, period minutes)

    for p_start, p_end, energy_kwh, minutes in periods:
        length = p_end - p_start
        for i, t in enumerate(utc_starts):
            overlap = min(p_end, t + block) - max(p_start, t)
            if overlap > timedelta(0):
                kwh[i] += energy_kwh * (overlap / length)
                if i not in best or overlap > best[i][0]:
                    best[i] = (overlap, int(round(minutes)))

    return [
        ForecastSlot(t, kwh[i], False, best[i][1] if i in best else modal)
        for i, t in enumerate(starts)
    ]


def _bucket(ts, config):
    """Bucket key: (day group, block-of-day).

    same_weekday: group is the weekday (0-6). day_type: 0 for Monday-Friday,
    1 for Saturday/Sunday, so weekends pool only with weekends.
    """
    block_of_day = (ts.hour * 60 + ts.minute) // config.block_minutes
    if config.usage_grouping == "day_type":
        group = 0 if ts.weekday() < 5 else 1
    else:
        group = ts.weekday()
    return (group, block_of_day)


def usage_profile(history, config, start_time, horizon_end):
    """Expected consumption per block from history of (timestamp, kwh) readings.

    Only readings within config.usage_history_weeks weeks before start_time
    are used. Buckets are (day group, time of day), the day group chosen by
    config.usage_grouping. A bucket's value is a weighted mean over the
    distinct dates that contributed; sample_days is the plain count of those
    dates (coverage, independent of weighting). An empty bucket gets the
    overall mean over all contributing dates, weighted the same way, with
    sample_days 0. With no history every block is 0.0 with sample_days 0
    (never raises).

    Recency weighting (config.usage_recency_weighting):
      "none":   every date weighs 1 (plain mean).
      "linear": week index = floor((start_time - reading) / 7 days) in UTC
                instants, 0 being the most recent 7 days; weight =
                usage_history_weeks - index (4,3,2,1 for four weeks). A date
                takes the index of its LATEST reading in the window, so a date
                straddling a week boundary counts as the newer week. Future
                readings are clamped to index 0 and a reading exactly on the
                cutoff to the oldest week (weight 1). Absent weeks simply
                drop out; weights are not renormalised beyond the weighted
                mean itself.
    """
    tz = _tz(config)
    start = _aware(start_time, config)
    weeks = config.usage_history_weeks
    week = timedelta(weeks=1)
    cutoff = (start - timedelta(weeks=weeks)).astimezone(timezone.utc)
    start_utc = start.astimezone(timezone.utc)
    linear = config.usage_recency_weighting == "linear"
    # (date, bucket) -> kWh, so finer-grained readings sum into their block.
    per_day = {}
    day_index = {}  # date -> week index of its latest in-window reading
    for ts, kwh in history or ():
        aware = _aware(ts, config)
        instant = aware.astimezone(timezone.utc)
        if instant < cutoff:
            continue
        local = aware.astimezone(tz)
        d = local.date()
        key = (d, _bucket(local, config))
        per_day[key] = per_day.get(key, 0.0) + float(kwh)
        idx = min(max((start_utc - instant) // week, 0), weeks - 1)
        day_index[d] = min(day_index.get(d, idx), idx)

    def weight(d):
        return float(weeks - day_index[d]) if linear else 1.0

    sums, wsum, days = {}, {}, {}
    total, total_w = 0.0, 0.0
    for (d, b), v in per_day.items():
        w = weight(d)
        sums[b] = sums.get(b, 0.0) + w * v
        wsum[b] = wsum.get(b, 0.0) + w
        days[b] = days.get(b, 0) + 1
        total += w * v
        total_w += w
    overall_mean = total / total_w if total_w else 0.0

    out = []
    for t in _grid(config, start_time, horizon_end):
        b = _bucket(t.astimezone(tz) if t.tzinfo else t, config)
        if b in days:
            out.append(UsageSlot(t, sums[b] / wsum[b], days[b]))
        else:
            out.append(UsageSlot(t, overall_mean, 0))
    return out
