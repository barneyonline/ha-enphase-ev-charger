# Changelog

All notable changes to this project will be documented in this file.

## v0.8.0b1
- Config Flow: add Enlighten email/password login with MFA prompts, site & charger selection, automatic token refresh, and a manual header fallback for advanced users.
- API & Coordinator: rewrite the client stack to handle the wider Enlighten variants, cache summary metadata, smooth rate limiting, and persist last set amps/session data after restarts.
- Diagnostics & Tests: expand diagnostics redaction and add extensive regression coverage for the new flow, API variations, and polling behavior.

## v0.7.9
- Sensors: IQ EV charger power sensor now returns the live `power_w` reading when the API provides it, avoiding a stuck 0 W display while preserving the energy-derived fallback for historical accuracy.

## v0.7.8
- Sensors: harden the lifetime energy meter so startup zeroes and small API dips no longer reset Energy statistics; added regression coverage.
- Coordinator: preserve `config_entry` on older Home Assistant cores and reapply fast polling changes via `async_set_update_interval` when available.
- Config Flow: backport `_get_reconfigure_entry` and `_abort_if_unique_id_mismatch` helpers for legacy cores while retaining reconfigure validation.
- Tests: silence the frame helper guard for unit tests that instantiate the coordinator outside Home Assistant.
- Config Flow: add Enlighten email/password login with site & charger selection, automatic token refresh, and manual header fallback.

## v0.7.5
- Devices: correct DeviceInfo usage (kwargs) and enrich with model/model_id/hw/sw when available.
- Backfill: update existing device registry entries on setup and link chargers under the site device via via_device_id; log only when changes are needed.
- Performance: throttle summary_v2 fetches to at most every 10 minutes after initial refresh.
- Consistency: use enum device classes (BinarySensorDeviceClass, SensorDeviceClass) instead of string literals.
- UX: mark Charging switch as the device’s main feature so it inherits the device name.
- Options: default "Fast while streaming" to True.
- Lint: satisfy ruff import order and long-line rules.

## v0.7.4
- sensor: harden lifetime energy sensor for Energy dashboard
  - Use RestoreSensor to restore native value on restart.
  - Add one-shot boot filter to ignore initial 0/None sample.
  - Clamp invalid/negative samples to last good value to prevent spikes.

## v0.7.2
- Sensors: replace old daily/session energy with a new Energy Today derived from the lifetime meter
  - Monotonic within a day; resets at local midnight; persists baseline across restarts.
  - Keeps state_class total for Energy dashboard compatibility.
- Power: simplify by deriving power from the rate of change of Energy Today
  - Average power between updates; persists sampling state across restarts.
- Coordinator: expose operating voltage where available; sensors show it in attributes.
- Tests: add coverage for new daily sensor and power restore behavior.

## v0.7.3
- Docs & Badges: add dynamic Shields.io badges; remove static version text.
- Devices: enrich DeviceInfo from summary_v2 (sw/hw versions, model name/id, part/kernel/bootloader where available).
- Config Flow: add reconfiguration flow (async_step_reconfigure) with validation and in-place update; README reconfigure section.
- Tests: add reconfigure flow tests (form, submit, wrong_account abort, cURL auto-fill).
- Quality Scale: mark docs for actions/supported devices/removal as done; bump manifest quality_scale to gold.
- CI: add auto-assign workflow to assign/request review for new PRs; add quality scale validator workflow.

## v0.6.5
- Quality: diagnostics, system health translations, icon mappings, and device triggers
  - Add `quality_scale.yaml` to track Integration Quality Scale rules.
  - Diagnostics: use `async_redact_data` with a shared `TO_REDACT`, enrich config-entry diagnostics, and add per-device diagnostics.
  - System Health: add translated labels for site/poll/latency/backoff.
  - Icons: move connector status, charge mode, and charging state icons into `icons.json` state mappings.
  - Device automations: add triggers for charging started/stopped, plugged/unplugged, and faulted.

## v0.6.4
- Icons: dynamic icons for connector status, charging state, and charge modes
  - Connector Status: CHARGING/PLUGGED/DISCONNECTED/FAULTED map to friendly icons.
  - Charging binary sensor: `mdi:flash` / `mdi:flash-off`.
  - Charge Mode: MANUAL/IMMEDIATE/SCHEDULED/GREEN/IDLE map to icons.

## v0.6.3
- Diagnostics: include options, poll interval, scheduler mode cache, and header names
  - Redact sensitive fields; export current options and header names only.

## v0.6.2
- Number/Sensor: default Set Amps to 32A when unknown on startup
  - Prevents 0 A after reinstall/restart until first user action.

## v0.6.1
- Number: initialize charging amps from current setpoint on startup
  - Seed `last_set_amps` from API `chargingLevel` on first refresh/restart.

## v0.6.0
- Session Duration: normalize timestamps (ms→s); fix end time after stop.
- Sensors: remove duplicate Current Amps; keep Set Amps; improved icons/labels.
- Device info: include serial; number now stores setpoint only.

## v0.5.0
- Phase Mode: icon + mapping (1→Single, 3→Three); show 0 decimals for amps.
- Power: detect more keys; estimate from amps×voltage when missing; option for nominal voltage.

## v0.4.0
- Add Charging Amps number; add Charging switch; tests and translations.

## v0.3.0
- Charging Level → Charging Amps (A); temporary fast polling after start/stop.
- Remove unreliable schedule/connector/session miles sensors.

## v0.2.6
- Start/Stop: treat unplugged/not-active as benign; prefer scheduler charge mode.

## v0.2.5
- API headers: merge per-call headers; prefer scheduler charge mode in selector.

## Tests coverage (meta)
- Add tests for buttons, fast window, and latency/connectivity sensors.
