Title: number: initialize charging amps from current setpoint on startup

Summary
- Sync the control `number.iq_ev_charger_charging_amps` to the current charger setpoint on initial load and after restarts.
- Prevents the number control from defaulting to the minimum/0 until the first manual adjustment.

Details
- Coordinator now seeds `last_set_amps[sn]` from the API-reported `chargingLevel` on first refresh (only when not already set).
- This ensures both the Set Amps sensor and the Number control show the same value immediately after startup.
- No change to user interactions: setting the Number still only stores the desired value; Start actions use the stored setpoint.

Notes on duplicate sensors
- The integration exposes a single “Set Amps” sensor (`sensor.iq_ev_charger_set_amps`).
- If you also see `sensor.set_amp`, it is not created by this integration (likely an older helper/integration). You can remove/disable that entity to avoid confusion.

Testing
- Existing tests continue to pass in local runs; behavior when `chargingLevel` is present was verified manually.

Version
- Bump to 0.6.1

