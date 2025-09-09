Summary
- Add Charging Amps number entity to view/set amps (A) per charger.
- Add Charging switch entity to toggle charging on/off.
- Include tests and translations; register new platforms.

Details
- number.py: `ChargingAmpsNumber`
  - Reads amps from `charging_level`, falls back to `last_set_amps`, else 0.
  - min/max from `summary_v2.chargeLevelDetails` via coordinator (`min_amp`/`max_amp`), defaults 6–40 A.
  - `async_set_native_value(amps)` calls `start_charging(sn, amps)`, updates `last_set_amps`, kicks fast polling, refreshes.
- switch.py: `ChargingSwitch`
  - `is_on` reflects `coordinator.data[sn]["charging"]`.
  - `turn_on` calls `start_charging(sn, last_set_amps or 32)`, kicks fast polling, refreshes.
  - `turn_off` calls `stop_charging(sn)`, kicks fast polling, refreshes.
- __init__.py: add `number` and `switch` to `PLATFORMS`.
- translations: add names for number/switch entities.
- tests: verify number read/set behavior and switch on/off calls.

Behavior notes
- Uses the existing start endpoint to adjust amps while charging or to start at the requested amps when idle.
- Gracefully handles unplugged/not-ready/idle cases thanks to prior no-op handling for 4xx responses in start/stop.
- Fast polling window after actions ensures the UI updates promptly.

Breaking changes
- None.

Validation
- Ruff clean.
- Tests for new entities added.
