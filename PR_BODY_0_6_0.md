Summary
- Session Duration: normalize timestamps (ms→s), record a fixed end on stop, and prevent duration from growing after stop.
- Charging Amps: remove duplicate "Current Amps" sensor; keep a single "Set Amps" sensor.
- Binary sensors: rename Faulted → "Charger Problem"; DLB Active → "Load Balancing Active".
- Icons: add icons for Status (information) and Charge Mode (car-electric); Set Amps uses current-ac.
- Device info: include charger serial to distinguish multiple units.
- Number behavior: Charging Amps control now only stores the desired amps; it does not start charging. Start button/switch/service use the stored setpoint.
- Tests: remove dependency on hass fixture in two tests to silence deprecation warnings.

Details
- coordinator.py: unify ms/seconds; capture session_end on charging→not charging; normalize plug-out fallback; freeze duration.
- sensor.py/icons.json/translations: removed Current Amps; labels and icons updated; wording improved.
- entity.py: add serial_number to DeviceInfo.
- number.py/button.py/__init__.py: setpoint-only number; start uses last_set_amps (or 32 A default).
- tests: updated to avoid hass fixture warnings.

Breaking changes
- Current Amps sensor removed to avoid duplication. Set Amps remains with same unique_id; dashboards may need relabeling only.

Validation
- Ruff checks clean locally; tests adjusted.
