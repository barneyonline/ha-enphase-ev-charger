# Changelog

All notable changes to this project will be documented in this file.

## Unreleased
- No unreleased changes.

## v1.2.2
- Start/Stop: treat HTTP 400 “already in charging state” responses as a successful no-op so the charging switch remains on when a session is already running.
- Coordinator: mark chargers as active when the connector reports CHARGING/FINISHING/SUSPENDED to recover the correct state immediately after restarts.
- Docs: refresh the README with screenshots, consolidate documentation under `docs/`, remove the obsolete integration design drafts, and document the resilient switch behaviour.

## v1.2.1
- Control commands: include bearer authorization on all start/stop/trigger requests and log sanitized details when every variant fails with HTTP 400.
- Session history: throttle enrichment with a configurable interval, back off for 15 minutes after server errors, and surface a dedicated DNS resolution warning.
- Maintenance: wrap cookie-jar lookups with `yarl.URL` objects to silence the upcoming aiohttp `filter_cookies` deprecation.

## v1.2.0
- Session history: document the Enlighten session history endpoint, cache daily results, expose per-session energy/cost metadata via the Energy Today sensor, and trim cross-midnight sessions so only the in-day energy is counted.
- Lifetime energy & fast polling: ignore transient lifetime resets while keeping genuine ones, refresh cached session snapshots when data jumps, and align fast polling windows so dashboards stay stable during user actions.
- Charging services: clamp Start Charging requests to each charger's amp limits, reuse the last set amps when callers omit a value, and keep buttons/selectors in sync with the supported range.

## v1.1.0
- Authentication: auto-populate Enlighten site discovery headers (XSRF, cookies, bearer tokens) so account sites load reliably without manual header capture.
- Services: allow targeting Start/Stop Live Stream calls by site, wiring the service schema up with site-aware selectors.
- Coordinator: tolerate the new `charginglevel` payload casing and normalise operating voltage parsing so set amps and power math stay accurate.
- Bug fixes & performance: resolve the zero-amp charging state regression, keep live stream refreshes scoped to the requested site, and stabilise power calculations by smoothing voltage updates.
- Tooling & Docs: add a Docker-based dev environment, document its usage, and extend tests for the refreshed authentication flow.

## v1.0.0
- Coordinator & options: harden API retries with exponential backoff, raise Home Assistant Repairs issues when the cloud is unreachable or rate limited, and expose an adjustable API timeout in the options flow.
- Power sensor: derive each charger's max watt throughput from the reported amps/voltage so gauges and attributes scale to the installation.
- Lifetime energy: accept genuine API resets while rejecting noise, recording reset metadata without breaking Energy dashboard statistics.
- Docs: refresh the HACS installation steps to match the current installation flow.

## v0.8.7
- Manifest: opt into Home Assistant's `import_executor` so device automation imports no longer block the event loop.

## v0.8.6
- Device registry: drop the `default_model` field from charger entries to satisfy updated Home Assistant validation.

## v0.8.5
- Services: scope start/stop/trigger actions to Enphase chargers, allow multi-device calls, surface OCPP responses, and group optional inputs under advanced sections.
- UX: tighten the clear-reauth service with site-aware targeting and improved optional site selection.
- Docs: refresh the README services table for the revised experience.

## v0.8.4
- Sensors: rename Dynamic Load Balancing status, add enabled/disabled icons, and update translations.
- Cleanup: remove the deprecated `binary_sensor.iq_ev_charger_dlb_active` and its coordinator payload.
- Tests: extend regression coverage for the updated sensor states.
- Docs: mention the Dynamic Load Balancing sensor in the README entities table.

## v0.8.3
- Remove legacy manual header flow from config/reauth paths and translations
- Update documentation and tests for login-only setup
- Add standalone HACS validation workflow and HACS json

## v0.8.2
- Diagnostics: add Connection, IP Address, and Reporting Interval sensors with translation strings and icons sourced from the Enlighten summary metadata.
- Device info: surface the charger display name alongside the model (e.g., `IQ EV Charger (IQ-EVSE-EU-3032)`).
- Maintenance: remove redundant `custom_components/__init__.py`, bump manifest version to 0.8.2, and refresh README documentation.

## v0.8.1
- Sensors: derive IQ charger power from lifetime energy deltas with 5 minute smoothing, throughput capping, and legacy state restore support to eliminate transient spikes.
- Coordinator: drop estimated `power_w` fields so sensors own the calculation and keep cross-restart continuity.
- Tests: expand regression coverage for power smoothing scenarios and coordinator outputs.

## v0.8.0b3
- Options flow: avoid deprecated `config_entry` reassignment while remaining compatible with older Home Assistant releases, and guard non-awaitable reauth callbacks to prevent crashes.
- UX: replace placeholder abort strings (already configured, reconfiguration, re-authentication) with clear human-friendly text.

## v0.8.0b2
- Options flow: call Home Assistant's base initializer instead of reassigning `config_entry` to avoid the upcoming deprecation warning in 2025.12.
- Options flow: tolerate `async_start_reauth` returning `None` on older cores by only awaiting real awaitables, fixing the crash when users request reauthentication from the options dialog.

## v0.8.0b1
- Config Flow: add Enlighten email/password login with MFA prompts, site & charger selection, automatic token refresh, and a manual header fallback for advanced users.
- API & Coordinator: rewrite the client stack to handle the wider Enlighten variants, cache summary metadata, smooth rate limiting, and persist last set amps/session data after restarts.
- Diagnostics & Tests: expand diagnostics redaction and add extensive regression coverage for the new flow, API variations, and polling behavior.

## v0.8.0b2
- Options flow: call Home Assistant's base initializer instead of reassigning `config_entry` to avoid the upcoming deprecation warning in 2025.12.
- Options flow: tolerate `async_start_reauth` returning `None` on older cores by only awaiting real awaitables, fixing the crash when users request reauthentication from the options dialog.

## v0.7.9
- Sensors: IQ EV charger power sensor now derives wattage from lifetime energy deltas, smoothing the 5-minute samples, capping throughput at 19.2 kW, and preventing large transient spikes.

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
