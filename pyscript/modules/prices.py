"""Price derivation and rolling horizon. Pure - no I/O, no HA/pyscript imports."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_DEFAULT_RESOLUTION_MINUTES = 60


@dataclass(frozen=True)
class PricePoint:
    block_start: datetime
    market_price: float
    consumption_price: float
    injection_price: float
    source_resolution_minutes: int


def derive_consumption(market_price, config):
    """Grid-draw price. Uses only the consumption coefficients."""
    return round(
        market_price * config.consumption_multiplier + config.consumption_offset, 4)


def derive_injection(market_price, config):
    """Export price. Uses only the injection coefficients (never shared)."""
    return round(
        market_price * config.injection_multiplier + config.injection_offset, 4)


def derive(market_price, config):
    """Return (consumption_price, injection_price), each rounded to 4 places."""
    return (derive_consumption(market_price, config),
            derive_injection(market_price, config))


def _as_time(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _utc(dt):
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt


def _fixed_offset(dt):
    """Same instant with a FIXED offset. Fixed-offset keys never collide in the
    repeated fall-back hour (equal-looking ZoneInfo datetimes do, PEP 495) and
    arithmetic on them is absolute time."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone(dt.utcoffset()))


def _floor_to_block(moment, block_minutes):
    minute = (moment.minute // block_minutes) * block_minutes
    return moment.replace(minute=minute, second=0, microsecond=0)


def _source_resolution(times):
    """Smallest positive spacing between entries; 60 minutes if unknowable."""
    gaps = [
        (b - a).total_seconds() / 60.0
        for a, b in zip(times, times[1:]) if b > a
    ]
    if not gaps:
        return _DEFAULT_RESOLUTION_MINUTES
    return int(round(min(gaps)))


def expand_to_blocks(raw_entries, config, start_time):
    """Expand raw {time, price} entries onto the block grid, held flat.

    Returns a dict keyed by block_start. No interpolation;
    gaps in the source stay gaps. Blocks before the block containing
    start_time are dropped; the current block is kept.
    """
    block = config.block_minutes
    entries = sorted(
        ((_as_time(e["time"]), float(e["price"])) for e in raw_entries),
        key=lambda item: item[0],
    )
    if not entries:
        return {}
    resolution = _source_resolution([t for t, _ in entries])
    per_period = max(1, resolution // block)
    earliest = _utc(_floor_to_block(start_time, block))
    step = timedelta(minutes=block)
    series = {}
    for moment, market in entries:
        consumption, injection = derive(market, config)
        first = _fixed_offset(_floor_to_block(moment, block))
        for i in range(per_period):
            start = first + i * step  # fixed offset: absolute, DST-safe
            if _utc(start) < earliest:
                continue
            series[start] = PricePoint(
                block_start=start,
                market_price=market,
                consumption_price=consumption,
                injection_price=injection,
                source_resolution_minutes=resolution,
            )
    return series


def horizon_end(price_points, block_minutes=15):
    """Exclusive end of the last block with a known price, or None if empty.

    Accepts the mapping from expand_to_blocks or any iterable of PricePoint.
    Set by prices alone; never clipped to the solar forecast.
    """
    values = price_points.values() if hasattr(price_points, "values") \
        else price_points
    starts = [p.block_start for p in values]
    if not starts:
        return None
    latest = max(starts, key=_utc)
    if latest.tzinfo is None:
        return latest + timedelta(minutes=block_minutes)
    return (_utc(latest) + timedelta(minutes=block_minutes)).astimezone(
        latest.tzinfo)
