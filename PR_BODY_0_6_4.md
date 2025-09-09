Title: icons: dynamic icons for connector status, charging state, and charge modes

Summary
- Improve UI clarity with dynamic icons based on live state.

Details
- Connector Status sensor: adds runtime icon mapping (e.g., CHARGING → `mdi:ev-plug-ccs2`, PLUGGED/CONNECTED → `mdi:ev-plug-type2`, DISCONNECTED/UNPLUGGED → `mdi:power-plug-off`, FAULTED/ERROR → `mdi:alert`, fallback `mdi:ev-station`).
- Charging binary sensor: shows `mdi:flash` when charging, `mdi:flash-off` when not.
- Charge Mode sensor: maps modes to friendly icons (`MANUAL/IMMEDIATE` → `mdi:flash`, `SCHEDULED_CHARGING` → `mdi:calendar-clock`, `GREEN_CHARGING` → `mdi:leaf`, `IDLE` → `mdi:timer-sand-paused`, fallback `mdi:car-electric`).
- Static defaults remain in `icons.json`, but these entities now override icons dynamically based on state.

Version
- Bump to 0.6.4

