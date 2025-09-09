Title: number/sensor: default Set Amps to 32A when unknown on startup

Summary
- Align initial value of both the Set Amps sensor and the charging amps number control to 32A when the API does not report a `chargingLevel` on first refresh.
- Prevents the control from showing 0 A (or min) after re-install/restart until the first user action.

Details
- `EnphaseChargingLevelSensor` and `ChargingAmpsNumber` now fall back to the coordinator’s `last_set_amps[sn]` or 32A if missing.
- The coordinator still seeds `last_set_amps` from `chargingLevel` when provided by the API.
- Start/stop actions remain unchanged; they already default to 32A when unset.

Notes on duplicate sensors
- This integration creates `sensor.iq_ev_charger_set_amps`. If you also see `sensor.set_amp`, that entity is not created by this integration and likely comes from an older/other integration or a leftover helper. You can safely remove/disable it.

Version
- Bump to 0.6.2

