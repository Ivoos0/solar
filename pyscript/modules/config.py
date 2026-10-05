"""Site configuration: parse and validate a plain dict. Pure - no I/O."""
import hashlib
import re
from dataclasses import dataclass, fields

_MODES = ("auto", "running", "accumulating")
_GROUPINGS = ("same_weekday", "day_type")
_RECENCY = ("linear", "none")

# (section, file key, attribute)
_MAP = [
    ("prices", "consumption_multiplier", "consumption_multiplier"),
    ("prices", "consumption_offset", "consumption_offset"),
    ("prices", "injection_multiplier", "injection_multiplier"),
    ("prices", "injection_offset", "injection_offset"),
    ("prices", "entity", "price_entity"),
    ("prices", "attribute", "price_attribute"),
    ("battery", "capacity_kwh", "capacity_kwh"),
    ("battery", "reserve_percent", "reserve_percent"),
    ("battery", "max_charge_kw", "max_charge_kw"),
    ("battery", "max_discharge_kw", "max_discharge_kw"),
    ("battery", "round_trip_efficiency", "round_trip_efficiency"),
    ("solar", "forecast_entity", "forecast_entity"),
    ("solar", "forecast_attribute", "forecast_attribute"),
    ("timing", "block_minutes", "block_minutes"),
    ("timing", "evaluation_interval_minutes", "evaluation_interval_minutes"),
    ("timing", "forecast_retry_minutes", "forecast_retry_minutes"),
    ("timing", "solar_cache_stale_minutes", "solar_cache_stale_minutes"),
    ("timing", "usage_cache_stale_minutes", "usage_cache_stale_minutes"),
    ("usage", "history_weeks", "usage_history_weeks"),
    ("usage", "grouping", "usage_grouping"),
    ("usage", "recency_weighting", "usage_recency_weighting"),
    ("alerts", "address", "alert_address"),
    ("alerts", "notify_service", "notify_service"),
    ("alerts", "realert_minutes", "realert_minutes"),
    ("alerts", "peak_enabled", "peak_alert_enabled"),
    ("alerts", "peak_warning_enabled", "peak_warning_enabled"),
    ("alerts", "peak_warning_min_interval_minutes",
     "peak_warning_min_interval_minutes"),
    ("alerts", "peak_warning_ticks", "peak_warning_ticks"),
    ("inverter", "type", "inverter_type"),
    ("inverter", "resend_minutes", "inverter_resend_minutes"),
    (None, "timezone", "timezone"),
    ("capacity_tariff", "enabled", "capacity_enabled"),
    ("capacity_tariff", "billing_floor_kw", "billing_floor_kw"),
    ("capacity_tariff", "guard_interval_seconds", "guard_interval_seconds"),
    ("capacity_tariff", "quarter_hour_average_mode", "quarter_hour_average_mode"),
    ("capacity_tariff", "offtake_sensor", "offtake_sensor"),
    ("capacity_tariff", "quarter_hour_average_sensor",
     "quarter_hour_average_sensor"),
    ("capacity_tariff", "month_peak_sensor", "month_peak_sensor"),
    ("capacity_tariff", "stay_under_percent", "stay_under_percent"),
    ("history", "enabled", "history_enabled"),
    ("report", "enabled", "report_enabled"),
    ("retention", "keep_days", "retention_keep_days"),
]

# history.sensors: quantity -> attribute holding a tuple of entity ids. An
# empty tuple means "not available" (the quantity is recorded as null).
_HISTORY_SENSORS = (
    ("import", "history_import_sensors"),
    ("export", "history_export_sensors"),
    ("solar", "history_solar_sensors"),
    ("battery_charge", "history_battery_charge_sensors"),
    ("battery_discharge", "history_battery_discharge_sensors"),
    ("load", "history_load_sensors"),
)
_HISTORY_SENSOR_ATTRS = tuple(a for _, a in _HISTORY_SENSORS)

_NON_NUMERIC = (
    "alert_address", "notify_service", "timezone", "offtake_sensor",
    "quarter_hour_average_mode", "capacity_enabled", "usage_grouping",
    "usage_recency_weighting", "inverter_type", "price_entity",
    "price_attribute", "forecast_entity", "forecast_attribute",
    "quarter_hour_average_sensor", "month_peak_sensor", "history_enabled",
    "peak_alert_enabled", "peak_warning_enabled", "report_enabled",
) + _HISTORY_SENSOR_ATTRS

# Entity-name fields (checked as domain.object_id) and attribute-name fields
# (checked as non-empty strings), with the config key shown in errors.
_ENTITY_FIELDS = (
    ("price_entity", "prices.entity"),
    ("forecast_entity", "solar.forecast_entity"),
    ("quarter_hour_average_sensor", "capacity_tariff.quarter_hour_average_sensor"),
    ("month_peak_sensor", "capacity_tariff.month_peak_sensor"),
)
_ATTRIBUTE_FIELDS = (
    ("price_attribute", "prices.attribute"),
    ("forecast_attribute", "solar.forecast_attribute"),
)

_FINGERPRINT_FIELDS = (
    "block_minutes",
    "usage_history_weeks", "usage_grouping", "usage_recency_weighting",
)


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. A startup error."""


@dataclass(frozen=True)
class SiteConfig:
    capacity_kwh: float
    alert_address: str
    consumption_multiplier: float = 1.07
    consumption_offset: float = 0.007
    injection_multiplier: float = 0.94
    injection_offset: float = -0.011
    reserve_percent: float = 10.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    round_trip_efficiency: float = 0.90
    block_minutes: int = 15
    evaluation_interval_minutes: int = 5
    forecast_retry_minutes: int = 10
    solar_cache_stale_minutes: int = 120
    usage_cache_stale_minutes: int = 2880
    realert_minutes: int = 60
    notify_service: str = "battery_alert"
    peak_alert_enabled: bool = True
    peak_warning_enabled: bool = True
    peak_warning_min_interval_minutes: int = 60
    peak_warning_ticks: int = 2
    timezone: str = "Europe/Brussels"
    capacity_enabled: bool = True
    billing_floor_kw: float = 2.5
    guard_interval_seconds: int = 30
    quarter_hour_average_mode: str = "auto"
    offtake_sensor: str = "sensor.slimmelezer_power_consumed"
    stay_under_percent: float = 80.0
    usage_history_weeks: int = 4
    usage_grouping: str = "same_weekday"
    usage_recency_weighting: str = "linear"
    inverter_type: str = "logging"
    inverter_resend_minutes: int = 15
    # Home Assistant entities. Deliberately NOT in fingerprint(): they name
    # where data is read from, not what a block means.
    price_entity: str = "sensor.entso_prices_average_electricity_price"
    price_attribute: str = "prices"
    forecast_entity: str = "sensor.forecast_solar_estimate"
    forecast_attribute: str = "watt_hours_period"
    quarter_hour_average_sensor: str = "sensor.slimmelezer_huidig_kwartiervermogen"
    month_peak_sensor: str = "sensor.slimmelezer_maandpiek"
    # Energy history (recorder). Cumulative kWh counters, summed per quantity.
    # Also not in fingerprint(): they name where data is read from.
    history_enabled: bool = True
    history_import_sensors: tuple = (
        "sensor.slimmelezer_energy_consumed_tariff_1",
        "sensor.slimmelezer_energy_consumed_tariff_2")
    history_export_sensors: tuple = (
        "sensor.slimmelezer_energy_produced_tariff_1",
        "sensor.slimmelezer_energy_produced_tariff_2")
    history_solar_sensors: tuple = ()
    history_battery_charge_sensors: tuple = ()
    history_battery_discharge_sensors: tuple = ()
    history_load_sensors: tuple = ()
    # Daily report (written next to the history). Not in fingerprint().
    report_enabled: bool = True
    # Retention: the daily cleanup deletes log, history and report files whose
    # DATE IN THE NAME is older than this many days. Not in fingerprint().
    retention_keep_days: int = 90

    def history_sensors(self):
        """{quantity: tuple of entity ids} (empty tuple = not available)."""
        return {q: getattr(self, a) for q, a in _HISTORY_SENSORS}

    def fingerprint(self):
        """Stable 8-hex digest of the fields that change what a block means."""
        canon = "|".join(
            "%s=%r" % (n, getattr(self, n)) for n in _FINGERPRINT_FIELDS
        )
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _errors(cfg):
    errs = []

    def bad(field, value, rule):
        errs.append("%s=%r: %s" % (field, value, rule))

    for f in fields(SiteConfig):
        if f.name in _NON_NUMERIC:
            continue
        v = getattr(cfg, f.name)
        if not _num(v):
            bad(f.name, v, "must be a number")
    if errs:
        return errs
    if cfg.capacity_kwh <= 0:
        bad("capacity_kwh", cfg.capacity_kwh, "must be > 0")
    if not 0 <= cfg.reserve_percent < 100:
        bad("reserve_percent", cfg.reserve_percent, "must be >= 0 and < 100")
    if not 0 < cfg.round_trip_efficiency <= 1:
        bad("round_trip_efficiency", cfg.round_trip_efficiency,
            "must be > 0 and <= 1")
    if cfg.block_minutes <= 0 or 60 % cfg.block_minutes != 0:
        bad("block_minutes", cfg.block_minutes, "must divide 60")
    if not isinstance(cfg.alert_address, str) or not cfg.alert_address.strip():
        bad("alerts.address", cfg.alert_address, "must be non-empty")
    if (not isinstance(cfg.notify_service, str)
            or not re.fullmatch(r"[a-z0-9_]+", cfg.notify_service)):
        bad("alerts.notify_service", cfg.notify_service,
            "must be a service slug: lowercase letters, digits, underscore")
    if (not isinstance(cfg.inverter_type, str)
            or not re.fullmatch(r"[a-z0-9_]+", cfg.inverter_type)):
        bad("inverter.type", cfg.inverter_type,
            "must be a driver name: lowercase letters, digits, underscore "
            "(it selects pyscript/modules/inverter_<type>.py)")
    if (isinstance(cfg.inverter_resend_minutes, float)
            or cfg.inverter_resend_minutes < 0):
        bad("inverter.resend_minutes", cfg.inverter_resend_minutes,
            "must be an integer >= 0 (0 = send every call)")
    for attr, label in _ENTITY_FIELDS:
        v = getattr(cfg, attr)
        if not isinstance(v, str) or not re.fullmatch(
                r"[a-z0-9_]+\.[a-z0-9_]+", v):
            bad(label, v, "must be an entity id like domain.object_id: "
                "lowercase letters, digits, underscore, exactly one dot")
    for attr, label in _ATTRIBUTE_FIELDS:
        v = getattr(cfg, attr)
        if not isinstance(v, str) or not v.strip():
            bad(label, v, "must be a non-empty attribute name")
    if not isinstance(cfg.peak_alert_enabled, bool):
        bad("alerts.peak_enabled", cfg.peak_alert_enabled,
            "must be true or false")
    if not isinstance(cfg.peak_warning_enabled, bool):
        bad("alerts.peak_warning_enabled", cfg.peak_warning_enabled,
            "must be true or false")
    if (isinstance(cfg.peak_warning_min_interval_minutes, float)
            or cfg.peak_warning_min_interval_minutes < 1):
        bad("alerts.peak_warning_min_interval_minutes",
            cfg.peak_warning_min_interval_minutes, "must be an integer >= 1")
    if (isinstance(cfg.peak_warning_ticks, float)
            or cfg.peak_warning_ticks < 1):
        bad("alerts.peak_warning_ticks", cfg.peak_warning_ticks,
            "must be an integer >= 1")
    if not isinstance(cfg.history_enabled, bool):
        bad("history.enabled", cfg.history_enabled, "must be true or false")
    if not isinstance(cfg.report_enabled, bool):
        bad("report.enabled", cfg.report_enabled, "must be true or false")
    for q, attr in _HISTORY_SENSORS:
        v = getattr(cfg, attr)
        label = "history.sensors.%s" % q
        if not isinstance(v, tuple):
            bad(label, v, "must be a list of entity ids")
            continue
        for item in v:
            if not isinstance(item, str) or not re.fullmatch(
                    r"[a-z0-9_]+\.[a-z0-9_]+", item):
                bad(label, item, "must be an entity id like domain.object_id: "
                    "lowercase letters, digits, underscore, exactly one dot")
        if len(set(v)) != len(v):
            bad(label, list(v), "lists the same entity twice (it would be "
                "counted twice)")
    if cfg.max_charge_kw <= 0:
        bad("max_charge_kw", cfg.max_charge_kw, "must be > 0")
    if cfg.max_discharge_kw <= 0:
        bad("max_discharge_kw", cfg.max_discharge_kw, "must be > 0")
    if not 0 < cfg.stay_under_percent <= 100:
        bad("stay_under_percent", cfg.stay_under_percent,
            "must be > 0 and <= 100")
    if cfg.quarter_hour_average_mode not in _MODES:
        bad("quarter_hour_average_mode", cfg.quarter_hour_average_mode,
            "must be one of %s" % ", ".join(_MODES))
    if isinstance(cfg.usage_history_weeks, float) or cfg.usage_history_weeks < 1:
        bad("usage_history_weeks", cfg.usage_history_weeks,
            "must be an integer >= 1")
    if isinstance(cfg.retention_keep_days, float) or cfg.retention_keep_days < 1:
        bad("retention.keep_days", cfg.retention_keep_days,
            "must be an integer >= 1")
    elif (isinstance(cfg.usage_history_weeks, int)
            and cfg.usage_history_weeks >= 1
            and cfg.retention_keep_days < 7 * cfg.usage_history_weeks):
        bad("retention.keep_days", cfg.retention_keep_days,
            "must be at least 7 * usage.history_weeks (usage.history_weeks=%d "
            "needs %d days of history to build the usage profile)"
            % (cfg.usage_history_weeks, 7 * cfg.usage_history_weeks))
    if cfg.usage_grouping not in _GROUPINGS:
        bad("usage_grouping", cfg.usage_grouping,
            "must be one of %s" % ", ".join(_GROUPINGS))
    if cfg.usage_recency_weighting not in _RECENCY:
        bad("usage_recency_weighting", cfg.usage_recency_weighting,
            "must be one of %s" % ", ".join(_RECENCY))
    return errs


def _history_sensors(raw, kwargs, errs):
    """Read history.sensors (optional lists of entity ids) into kwargs."""
    section = raw.get("history")
    if not isinstance(section, dict) or section.get("sensors") is None:
        return
    sensors = section["sensors"]
    if not isinstance(sensors, dict):
        errs.append("history.sensors=%r: must be a mapping" % (sensors,))
        return
    known = dict(_HISTORY_SENSORS)
    for key, value in sensors.items():
        if key not in known:
            errs.append("history.sensors.%s: unknown quantity (expected one of "
                        "%s)" % (key, ", ".join(known)))
        elif value is None:
            continue
        elif not isinstance(value, (list, tuple)):
            errs.append("history.sensors.%s=%r: must be a list of entity ids"
                        % (key, value))
        else:
            kwargs[known[key]] = tuple(value)


def from_dict(raw):
    """Build a validated SiteConfig from the parsed YAML mapping."""
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a mapping, got %r" % (raw,))
    kwargs = {}
    errs = []
    for section, key, attr in _MAP:
        if section is None:
            container = raw
        else:
            container = raw.get(section)
            if container is None:
                container = {}
            elif not isinstance(container, dict):
                msg = "%s=%r: must be a mapping" % (section, container)
                if msg not in errs:
                    errs.append(msg)
                continue
        if key in container and container[key] is not None:
            kwargs[attr] = container[key]
    _history_sensors(raw, kwargs, errs)
    if (isinstance(kwargs.get("inverter_type"), str)
            and kwargs["inverter_type"].strip().lower() == "none"):
        kwargs["inverter_type"] = "logging"      # "none" means log only
    for req, label in (("capacity_kwh", "battery.capacity_kwh"),
                       ("alert_address", "alerts.address")):
        if req not in kwargs:
            errs.append("%s: required, no default" % label)
            kwargs[req] = None
    if errs:
        raise ConfigError("Invalid configuration: " + "; ".join(errs))
    cfg = SiteConfig(**kwargs)
    errs = _errors(cfg)
    if errs:
        raise ConfigError("Invalid configuration: " + "; ".join(errs))
    return cfg
