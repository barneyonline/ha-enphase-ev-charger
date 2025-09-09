Title: quality: diagnostics, system health translations, icon mappings, and device triggers

Summary
- Add `quality_scale.yaml` to track Integration Quality Scale rules (aiming for Gold).
- Diagnostics: use `async_redact_data` with a shared `TO_REDACT`, enrich config-entry diagnostics, and add per-device diagnostics.
- System Health: add translated labels for site/poll/latency/backoff.
- Icons: move connector status, charge mode, and charging state icons into `icons.json` state mappings.
- Device automations: add triggers for charging started/stopped, plugged/unplugged, and faulted.

Details
- `diagnostics.py`: config-entry export now includes options, update interval, last scheduler modes, and client header names (names only), plus new `async_get_device_diagnostics`.
- `icons.json`: state mappings for `sensor.connector_status`, `sensor.charge_mode`, and `binary_sensor.charging`.
- `translations/en.json`: system health info keys; device trigger type labels; ensure `charge_mode` entity name.
- `device_trigger.py`: implements standard device triggers backed by state triggers on the binary sensors.
- `quality_scale.yaml`: initial rules file marking implemented items.

Version
- Bump to 0.6.5

