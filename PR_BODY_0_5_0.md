Summary
- Phase Mode: add transmission tower icon and map numeric values (1→Single Phase, 3→Three Phase).
- Amps display: show no decimals for Set/Min/Max/Current Amps.
- Charging Amps cleanup: rename original setpoint sensor to "Set Amps"; add new "Current Amps" sensor derived from power.
- Session Energy: normalize e_c when reported in Wh to kWh.
- Power sensor: detect more keys and estimate from amps×nominal_voltage (option, default 240 V) when missing.

Details
- sensor.py:
  - Phase Mode icon and mapping; Set Amps sensor (formerly Charging Amps) with 0-decimal precision; new Current Amps sensor; Min/Max Amp precision set to integer.
- coordinator.py:
  - Session energy normalization; broader power key mapping; estimate power from amps×voltage when charging and power missing.
- config_flow.py/const.py/translations:
  - Add `nominal_voltage` option; label updates for Set/Current Amps; translation cleanup.
- tests:
  - Updated session duration test; added power estimate test.

Breaking changes
- Display names: "Charging Amps" sensor is now "Set Amps"; dashboards may need relabeling. Unique IDs are unchanged.

Validation
- Ruff clean locally. Tests updated to cover new behaviors.
