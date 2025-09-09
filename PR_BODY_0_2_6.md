Summary
- Handle Start/Stop when charger is unplugged or not active without raising errors.
- Prefer scheduler-reported Charge Mode in UI and sensor for consistency.
- Bump manifest to 0.2.6.

Details
- api.py: start_charging treats 409/422 as benign (returns {status: not_ready}); stop_charging treats 400/404/409/422 as benign (returns {status: not_active}).
- select.py/sensor.py: both reflect scheduler preference (charge_mode_pref).
- tests: added test_api_noop_cases.py to validate no-op behavior.

Validation
- Ruff clean; existing tests updated to cover new behavior.
