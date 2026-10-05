"""Example plan-style driver used by the tests. GENERIC placeholder entities.

plan() is pure: it only returns the Home Assistant service calls to make. The
boundary (pyscript/modules/inverter.py) executes them in order with
service.call, and reads the battery charge from SOC_ENTITY itself. Nothing
here talks to hardware, and no real integration is implied by the names.
"""
SOC_ENTITY = "sensor.my_battery_soc"      # percent, 0..100
COMMAND_HOLD_MINUTES = 10                 # this made-up inverter drops a command after ~10 min

_FORCE = {
    "charge": "input_boolean.my_force_charge",
    "discharge": "input_boolean.my_force_discharge",
    "export": "input_boolean.my_force_export",
}


def _off(entity):
    return {"domain": "input_boolean", "service": "turn_off",
            "data": {"entity_id": entity}}


def plan(action, target_power_kw):
    if action == "idle":
        # release every forced mode: the inverter returns to its default behaviour
        return [_off(entity) for entity in _FORCE.values()]
    return [
        {"domain": "input_number", "service": "set_value",
         "data": {"entity_id": "input_number.my_power",
                  "value": round(target_power_kw, 2)}},
        {"domain": "input_boolean", "service": "turn_on",
         "data": {"entity_id": _FORCE[action]}},
    ]
