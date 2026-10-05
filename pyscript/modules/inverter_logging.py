"""Logging-only inverter driver: the default, and the template for new ones.

Selected by `inverter.type: logging` (or no `inverter:` section, or `none`).
It transmits nothing: the decision log line written by inverter.apply IS the
whole effect. It reports a placeholder battery charge and says so
(SOC_IS_STUB), so decisions stay marked degraded=soc_stubbed.

THE DRIVER INTERFACE (everything a new inverter has to provide):

    SOC_IS_STUB = False                     # optional; True while the charge
                                            # you return is a placeholder
    def send(action, target_power_kw): ...  # action: "charge" | "discharge" |
                                            # "export" | "idle"; power >= 0 kW,
                                            # 0.0 when idle. Return True on
                                            # success; False or raise = failed.
                                            # "charge" is a forced charge from
                                            # the grid.

A driver may define plan(action, target_power_kw) INSTEAD of send(): a pure
function returning the Home Assistant service calls to make, as an ordered list
of {"domain": ..., "service": ..., "data": {...}} dicts. The boundary validates
and runs them (see docs/inverter-boundary.md, "Plan-style drivers"). It may then
declare SOC_ENTITY (the battery charge sensor, percent) and omit
read_charge_percent(). This driver uses neither.

WHAT "idle" MEANS: send("idle", 0.0) must cancel every forced mode this project
set (forced grid charge, forced export, forced discharge) and return the
inverter to its own default behaviour. It is sent once after a forced mode, not
repeatedly, so it has to be a real release, not a "do nothing for now".
    def read_charge_percent(): ...          # battery charge, 0..100 (float)
    COMMAND_HOLD_MINUTES = 10               # optional: the inverter drops a
                                            # forced command after about this
                                            # many minutes without a refresh.
                                            # The planner then re-sends an
                                            # unchanged command at 0.8 x this.
                                            # Leave it out (or None) when
                                            # unknown: the config
                                            # inverter.resend_minutes applies.
                                            # This driver declares nothing.

Copy this file to inverter_<name>.py and set `inverter.type: <name>`.
Drivers run as ordinary CPython in an executor thread (blocking I/O is fine,
set your own network timeouts), never on the Home Assistant event loop. They
must not use pyscript globals (log, state, ...). See README, "Adding an
inverter".
"""

SOC_IS_STUB = True
STUBBED_CHARGE_PERCENT = 50.0


def send(action, target_power_kw):
    """Transmit nothing."""
    return True


def read_charge_percent():
    """Placeholder charge, flagged by SOC_IS_STUB."""
    return STUBBED_CHARGE_PERCENT
