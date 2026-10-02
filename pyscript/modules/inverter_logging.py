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
    def read_charge_percent(): ...          # battery charge, 0..100 (float)

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
