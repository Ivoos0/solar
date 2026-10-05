# AlphaESS inverter, read-only (Home Assistant modbus)

Setting up read-only AlphaESS readings through Home Assistant's modbus integration. Back to the [README](../README.md).

**Contents**

- [Set it up](#set-it-up)
- [Which sensor does what](#which-sensor-does-what)
- [Finding the slave id](#finding-the-slave-id)
- [If the sensors are unavailable](#if-the-sensors-are-unavailable)

This is an example of giving the planner real readings from an AlphaESS inverter, through Home
Assistant's built-in `modbus` integration and a Modbus-to-Ethernet bridge (an HF2211 in the example).
It only reads. This project does not write to the inverter yet, so nothing here changes what the
inverter does. With the readings the planner knows the real battery charge, sees how much power the
battery is giving the house, and gets its energy history from the inverter's own counters. A ready
made integration that also controls the inverter is
[ramonvanraaij/ha-alphaess-modbus](https://github.com/ramonvanraaij/ha-alphaess-modbus) (see
[Credits and related projects](../README.md#credits-and-related-projects)). The sensor definitions below follow
its register definitions; the register list itself comes from the AlphaESS Modbus documentation.

## Set it up

1. Connect the bridge to the inverter and your network. Give the bridge a **static IP address**:
   a DHCP reservation in your router, or a fixed address in the bridge's own network settings. Home
   Assistant addresses the bridge by that IP (`alphaess_modbus_host_ip`). If the address changes, every
   AlphaESS sensor goes `unavailable` and the planner falls back: it holds because it has no battery
   reading (`soc_unavailable`) and estimates the household draw without the battery sensor
   (`battery_power_unavailable`).
2. Find the Modbus slave id of your inverter (see [Finding the slave id](#finding-the-slave-id)).
3. Add three keys to Home Assistant's `secrets.yaml` (placeholders are in `secrets.example.yaml`):

   ```yaml
   alphaess_modbus_host_ip: 192.0.2.10    # the bridge's static IP
   alphaess_modbus_host_port: 502
   alphaess_modbus_slaveId: 1             # found in the previous step
   ```

4. Add the hub below to your configuration, either directly under a `modbus:` key in
   `configuration.yaml`, or in a separate file. With a separate file the line in
   `configuration.yaml` is `modbus: !include alphaess_modbus.yaml` and the file must contain only the
   list of hubs, not a `modbus:` key of its own.

   ```yaml
   - name: modbuspvsystem
     type: tcp
     host: !secret alphaess_modbus_host_ip
     port: !secret alphaess_modbus_host_port
     message_wait_milliseconds: 10
     timeout: 10
     delay: 1
     sensors:
       - name: AlphaESS SoC Battery
         unique_id: AlphaESS_SoC_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0102
         data_type: uint16
         unit_of_measurement: "%"
         device_class: battery
         state_class: measurement
         scan_interval: 10
         scale: 0.1
         precision: 1

       - name: AlphaESS Power Battery
         unique_id: AlphaESS_Power_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0126
         data_type: int16
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 10

       - name: AlphaESS Power Grid
         unique_id: AlphaESS_Power_Grid
         slave: !secret alphaess_modbus_slaveId
         address: 0x0021
         data_type: int32
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 10

       - name: AlphaESS Total Energy from PV
         unique_id: AlphaESS_Total_Energy_from_PV
         slave: !secret alphaess_modbus_slaveId
         address: 0x043E
         data_type: uint32
         unit_of_measurement: kWh
         device_class: energy
         state_class: total_increasing
         scan_interval: 60
         scale: 0.1
         precision: 2

       - name: AlphaESS Total Energy Charge Battery
         unique_id: AlphaESS_Total_Energy_Charge_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0120
         data_type: uint32
         unit_of_measurement: kWh
         device_class: energy
         state_class: total_increasing
         scan_interval: 60
         scale: 0.1
         precision: 2

       - name: AlphaESS Total Energy Discharge Battery
         unique_id: AlphaESS_Total_Energy_Discharge_Battery
         slave: !secret alphaess_modbus_slaveId
         address: 0x0122
         data_type: uint32
         unit_of_measurement: kWh
         device_class: energy
         state_class: total_increasing
         scan_interval: 60
         scale: 0.1
         precision: 2

       - name: AlphaESS Battery Capacity
         unique_id: AlphaESS_Battery_Capacity
         slave: !secret alphaess_modbus_slaveId
         address: 0x0119
         data_type: uint16
         unit_of_measurement: kWh
         state_class: measurement
         scan_interval: 60
         scale: 0.1
         precision: 1

       - name: AlphaESS Battery Max Charge Power
         unique_id: AlphaESS_Battery_Max_Charge_Power
         slave: !secret alphaess_modbus_slaveId
         address: 0x012C
         data_type: uint16
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 60

       - name: AlphaESS Battery Max Discharge Power
         unique_id: AlphaESS_Battery_Max_Discharge_Power
         slave: !secret alphaess_modbus_slaveId
         address: 0x012D
         data_type: uint16
         unit_of_measurement: W
         device_class: power
         state_class: measurement
         scan_interval: 60

       - name: AlphaESS Inverter Work Mode      # optional, informational
         unique_id: AlphaESS_Inverter_Work_Mode
         slave: !secret alphaess_modbus_slaveId
         address: 0x0440
         data_type: uint16
         state_class: measurement
         scan_interval: 10
   ```

5. Restart Home Assistant. Home Assistant builds the entity ids from the sensor names. Check them in
   Developer Tools -> States before you use them: with the names above they are
   `sensor.alphaess_soc_battery`, `sensor.alphaess_power_battery` and so on.
6. Point the planner at them in `user_config.yaml` (the table below).

The power sensors poll every 10 seconds, which is easy on the bridge; the planner runs once a minute.
The PV power register (`0x0453`) was unreadable on one install and the planner does not need it, so it
is left out.

## Which sensor does what

| Entity | What it is for | `user_config.yaml` key |
|---|---|---|
| `sensor.alphaess_soc_battery` | Battery charge in percent. The planner and the peak guard use it instead of the 50 % placeholder | `battery.soc_sensor` |
| `sensor.alphaess_power_battery` | Battery power in W. Positive while the battery discharges, which is the default sign. It lets the planner see the load the battery is covering | `battery.power_sensor`, with `battery.power_positive: discharge` (use `charge` if yours is the other way round) |
| `sensor.alphaess_battery_max_charge_power` | The inverter's maximum battery charge power in W. The planner uses it as its charge limit | `battery.max_charge_sensor` |
| `sensor.alphaess_battery_max_discharge_power` | The inverter's maximum battery discharge power in W. The planner uses it as its export and peak shaving limit | `battery.max_discharge_sensor` |
| `sensor.alphaess_total_energy_from_pv` | Solar energy counter, for the energy history and the solar calibration | `history.sensors.solar` |
| `sensor.alphaess_total_energy_charge_battery` | Energy into the battery counter | `history.sensors.battery_charge` |
| `sensor.alphaess_total_energy_discharge_battery` | Energy out of the battery counter | `history.sensors.battery_discharge` |
| `sensor.alphaess_power_grid` | Grid power in W. Not read by the planner; handy to compare with your meter and to check signs | none |
| `sensor.alphaess_battery_capacity` | Battery size in kWh. Not read by the planner; use it to choose `battery.capacity_kwh` (usable capacity, rounded down) | none |
| `sensor.alphaess_inverter_work_mode` | Work mode number, informational | none |

The matching lines in `user_config.yaml`:

```yaml
battery:
  capacity_kwh: 10.0
  soc_sensor: sensor.alphaess_soc_battery
  power_sensor: sensor.alphaess_power_battery
  power_positive: discharge
  max_charge_sensor: sensor.alphaess_battery_max_charge_power
  max_discharge_sensor: sensor.alphaess_battery_max_discharge_power
history:
  sensors:
    solar: [sensor.alphaess_total_energy_from_pv]
    battery_charge: [sensor.alphaess_total_energy_charge_battery]
    battery_discharge: [sensor.alphaess_total_energy_discharge_battery]
```

With the inverter's default behaviour (nothing is sent), the battery covers the house and the grid draw
stays near zero. That is why the planner needs the battery power: the meter alone would read the
house as drawing nothing.

## Finding the slave id

The slave id (unit id) depends on the inverter and the bridge settings. A short script that asks
holding register `0x0102` (the battery charge) for a few ids shows which one answers. Run it on a
computer that can reach the bridge, with `pip install pymodbus`:

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("192.0.2.10", port=502)   # your bridge
client.connect()
for slave in (0, 1, 2, 85, 255):
    result = client.read_holding_registers(0x0102, count=1, slave=slave)
    print(slave, result)
client.close()
```

The id that returns a value (the charge in tenths of a percent, so 576 means 57.6 %) is yours. Newer
pymodbus versions call the `slave` argument `device_id`.

## If the sensors are unavailable

| Symptom | Cause | Fix |
|---|---|---|
| All `sensor.alphaess_*` are `unavailable`, records carry `soc_unavailable` and `battery_power_unavailable` | The bridge cannot be reached. The usual cause is that its IP address changed | Check that the bridge still has the address in `alphaess_modbus_host_ip` and answers (the script above). Give it a static address so this does not happen again |
| Only some sensors are `unavailable` | A register is not readable on your inverter model | Remove that sensor from the hub; the planner does not need the optional ones |
| Values look 10 times too big or small | A `scale` is missing or wrong | Compare with the definitions above |
| The battery power has the wrong sign | Your inverter reports the opposite direction | Set `battery.power_positive: charge` |

Home Assistant has no write access here: nothing in this hub sends a command to the inverter.
