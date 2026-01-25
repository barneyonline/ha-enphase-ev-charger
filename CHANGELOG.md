# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- None

### 🔧 Improvements
- None

### 🔄 Other changes
- None

## v1.7.2 – 2026-01-25

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Treat 204/205 API responses as empty JSON payloads to avoid parsing errors.
- Await the system health reachability check so connectivity status reports correctly.

### 🔧 Improvements
- Add system health labels for site summary and cache metrics across translations.

### 🔄 Other changes
- Fix the HACS integration name typo.

## v1.7.1 – 2026-01-02

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Refresh schedule helper default names when slot times change while preserving user edits.

### 🔧 Improvements
- Prefill the site selection in the config flow based on the current or first available site.

### 🔄 Other changes
- Simplified README documentation references to point to the Wiki.

## v1.7.0 – 2025-12-31

### 🚧 Breaking changes
- None

### ✨ New features
- Added schedule helper sync with scheduler-backed helpers, refresh service, and new options.
- Expanded localization support with new locale translations: bg, cs, da, el, en-AU, en-CA, en-IE, en-NZ, en-US, et, fi, hu, it, lt, lv, nb-NO, nl, pl, ro, sv-SE.

### 🐛 Bug fixes
- Preserve connector payload normalization so `dlb_active` reflects the status API when present.
- Last Session attributes now use session history metadata even when realtime session data is active.

### 🔧 Improvements
- Removed the `last_success_utc` attribute from cloud diagnostic sensors to keep metadata focused.
- Split energy aggregation/guard logic into a dedicated module to simplify coordinator responsibilities.
- Synced `strings.json` with locale translations for services, issues, device automation, and system health metadata.
- Removed the stale device automation action translation and rely on entity translations for site-level names.
- Added service section translations for advanced options and filled site ID service field labels.
- Replaced literal unit strings with Home Assistant unit constants for consistent unit handling.

### 🔄 Other changes
- Documented the `dlbActive` connector field in the cloud status API spec.

## v1.6.1 – 2025-12-27

### 🚧 Breaking changes
- Removed phase and DLB attributes from the Connectivity binary sensor and dropped the legacy `dlb_status` attribute.

### ✨ New features
- Added the Electrical Phase diagnostic sensor.
- Expanded Last Session sensor attributes with session history metadata (IDs, timing, cost, and profile details).

### 🐛 Bug fixes
- Fixed power reporting when a charger is suspended.

### 🔧 Improvements
- Status now reports the `offline_since` timestamp alongside existing diagnostics.
- Improved live stream polling lifecycle to better manage fast-refresh windows.

### 🔄 Other changes
- Updated README documentation and entity tables to reflect the latest sensor and attribute model.

## v1.6.0 – 2025-12-24

### 🚧 Breaking changes
- None

### ✨ New features
- Added Site Consumption lifetime energy sensor for total site usage alongside the existing site energy sensors (disabled by default).
- Validated manually entered site IDs during setup, blocking non-numeric values with a friendly error.
- Added MFA login support with an OTP verification step and resend flow in the config flow.

### 🐛 Bug fixes
- Allowed cookie-only authentication when the login response returns an empty JSON payload.
- Fixed grid import fallback for non-solar sites.
- Fixed MFA resend handling and reauthentication logging.

### 🔧 Improvements
- Added MFA translations and extended authentication/config-flow test coverage.

### 🔄 Other changes
- Documented pre-push coverage checks for touched modules in the developer guidelines.

## v1.5.2 – 2025-12-21

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed site lifetime energy kWh conversion by treating lifetime buckets as Wh values (no interval scaling), preventing over/under-counted totals.
- Corrected site lifetime energy flow mappings for grid import/export and consumption to align with the Enlighten payload fields.
- Fixed site-only setup by making charger serials optional, skipping charger entity creation when enabled, and always registering site energy entities.

### 🔧 Improvements
- None

### 🔄 Other changes
- Expanded config flow and site energy regression coverage and added translations for the new site ID validation error.

## v1.5.1 – 2025-12-12

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Corrected site lifetime energy bucket scaling by applying the reported interval minutes, fixing over/under-counted totals in the Energy Dashboard.

### 🔧 Improvements
- Site energy diagnostics now record the payload interval and source unit (W vs Wh) to aid troubleshooting.

### 🔄 Other changes
- Expanded site energy regression coverage to lock in interval handling.

## v1.5.0 – 2025-12-12

### 🚧 Breaking changes
- None

### ✨ New features
- Site lifetime energy sensors (Grid Import/Export, Solar Production, Battery Charge/Discharge) for the Home Assistant Energy Dashboard; disabled by default and restored across restarts.
- Site-only setup path and option toggle to keep the integration running for sites without chargers while still exposing site data.

### 🐛 Bug fixes
- Grid import fallback now subtracts battery supply so self-consumption is no longer double-counted as grid usage.
- Site energy sensors remain available when only restored state is present, avoiding dropouts when the backend omits lifetime data temporarily.

### 🔧 Improvements
- Diagnostics include site energy cache details and options expose the site-only toggle for easier troubleshooting.

### 🔄 Other changes
- Documented the lifetime energy endpoint in the API spec and added translations for the new site energy sensors.

## v1.4.7 – 2025-11-27

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Align the Last Session energy sensor with the energy device class by reporting a total state class, eliminating Home Assistant warnings about incompatible state classes.

### 🔧 Improvements
- None

### 🔄 Other changes
- Removed the GitHub workflow that auto-posted an `@codex review` comment on pull requests.

## v1.4.6 – 2025-11-26

### 🚧 Breaking changes
- Removed separate Connection, Session Duration, Commissioned, and Charger Problem sensors in favor of consolidated attributes (see below).

### ✨ New features
- Last Session sensor replaces Energy Today, reporting the most recent session’s energy with duration, cost, range, and charge-level attributes without daily resets.

### 🐛 Bug fixes
- Last Session now prefers session history when real-time totals are zeroed or missing, preserves zero-energy sessions, and avoids wiping the most recent session when idle samples report zero energy.

### 🔧 Improvements
- Status sensor now exposes commissioned and charger problem flags as attributes.
- Connected binary sensor now carries connection interface, IP, phase mode, and DLB status as attributes.

### 🔄 Other changes
- Updated translations and docs to reflect the new sensor/attribute model and dockerized test guidance.

## v1.4.5 – 2025-11-24

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Start Charging button, switch, and service calls now honor the charger’s configured charge mode (Manual, Scheduled, or Green) so scheduler-driven or solar-only sessions are no longer forced into Manual mode when kicked off from Home Assistant.

### 🔧 Improvements
- The Enlighten start-charging API discovery now caches independent “include charging level” and “scheduler-driven” request variants, preventing repeated retries and making charge mode transitions faster and more reliable.

### 🔄 Other changes
- Expanded API, coordinator, button, and switch tests to capture the new charge-mode-aware behaviour and to keep coverage at 100%.

## v1.4.4 – 2025-11-17

### 🚧 Breaking changes
- None

### ✨ New features
- Added German, Spanish, and Brazilian Portuguese translations so the config flow, entities, and diagnostics match your Home Assistant language.

### 🐛 Bug fixes
- None

### 🔧 Improvements
- Changing the Charging Amps number while a charger is actively running now pauses, waits ~30 seconds, and restarts the session so the updated amp limit applies immediately without waiting for the next plug-in.
- System Health and diagnostics now expose the session history cache TTL, entry count, and in-progress enrichment tasks to simplify diagnosing high-frequency energy refreshes.

### 🔄 Other changes
- Refactored the coordinator into dedicated summary/session helper modules and expanded the coordinator, sensor, helper, and system health test suites to close the remaining Codecov coverage gaps.

## v1.4.3 – 2025-11-12

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Cloud Backoff Ends now exposes the backoff expiry as a timestamp entity and schedules a single refresh when the window finishes, eliminating the per-second state churn that crashed the UI when opening that sensor's history.
- Removed the stale `backoff_seconds` attribute from Cloud Backoff Ends since the timestamp entity already carries the necessary context and attributes no longer update each second.

### 🔧 Improvements
- Hold the coordinator in fast polling for a minute whenever a charger toggles between idle and charging so dashboards and automations pick up new states without waiting for the slow interval.

### 🔄 Other changes
- Split the start/stop API helpers and expand the coordinator/helper/unit test coverage to lock in the fast-poll and diagnostics behaviour.

## v1.4.2 – 2025-11-09

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Reset the Energy Today sensor cleanly at local midnight even when Enlighten omits session timestamps so stale totals no longer carry into the next day.
- Keep the Cloud Backoff Ends diagnostic sensor counting down once per second so the remaining duration no longer stalls at zero while a backoff is active.

### 🔧 Improvements
- Collect rich site diagnostics (last success and failure details, HTTP status codes, network/DNS counters, backoff windows, and phase timings) for repairs, System Health, and downloadable diagnostics to make outage triage easier.
- Consolidate charger metadata by exposing IP address, dynamic load balancing status, phase mode, and commissioning state on the Connection sensor and surfacing amp limits on the Set Amps sensor, trimming redundant diagnostic entities.
- Harden the Energy Today sensor by normalizing session metadata, persisting the latest session totals across restarts, and rescheduling session enrichment when Enlighten data drifts so dashboards stay accurate.
- Attach full site metrics to reauthentication repair issues and clear them automatically after a successful credential refresh so guidance stays actionable.

### 🔄 Other changes
- Expand the Home Assistant test suite with comprehensive API, coordinator, entity, and diagnostics coverage to guard the new behaviour.

## v1.4.1 – 2025-11-01

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Reset the Energy Today sensor at local midnight even when Enlighten only reports session totals, ensuring the Energy Dashboard continues to increment correctly across days.

### 🔧 Improvements
- Enrich cloud diagnostics by surfacing DNS failures and the remaining backoff window so you can tell when the next retry will occur.
- Streamline reconfigure and reauthentication flows by locking the existing site selection and providing a descriptive `wrong_account` message when the configured site differs from the newly selected one.

### 🔄 Other changes
- Auto-assign every new GitHub issue to `barneyonline` so triage stays consistent.

## v1.4.0 – 2025-10-26

### 🚧 Breaking changes
- None

### ✨ New features
- Enphase site diagnostics now surface a dedicated Cloud Error Code sensor with descriptive context and raw response metadata so outages are easier to triage from Home Assistant.
- Connector Status sensor now exposes the cloud-side status reason so automations can react to the underlying pause cause (for example, insufficient solar or load management).

### 🐛 Bug fixes
- Ensure the Energy Today sensor resets at the start of each local day even when using session totals.

### 🔧 Improvements
- Reclassify Enphase site diagnostics sensors and align their device classes so cloud reachability, latency, and error metadata land under the diagnostics category while remaining available through outages.
- Simplify Energy Today sensor attributes and localize the range added value using the user's preferred length unit.
- Remove the redundant Cloud Last Error sensor, standardize inactive cloud states to `none`, and emit ISO-formatted timestamps for Cloud Backoff Ends.
- Integrate Codecov coverage reporting into CI, guard uploads in reusable workflows, skip uploads on fork pull requests, and publish pytest results to Codecov analytics to keep telemetry reliable.
- Avoid concurrency deadlocks in the reusable workflow so coverage jobs cannot block other contributors.

### 🔄 Other changes
- Expand automated coverage across the integration, including API client, switch module, service helpers, and diagnostics regression tests.
- Harden GitHub Actions by granting minimally scoped permissions across workflows to address code scanning alerts.
- Refresh the issue templates to capture the context needed for troubleshooting.
- Document official Enphase API status/error codes, capture connector status behaviours, and tidy related README badges/workflows in the EV cloud API spec and docs.

## v1.3.1 – 2025-10-25

### 🚧 Breaking changes
- None

### ✨ New features
- Energy sensors: drive the Energy Today reading from the status API session energy (falling back to lifetime deltas) and expose plug timestamps, energy, range, cost, and charge level metadata via attributes.

### 🐛 Bug fixes
- Charging controls: persist the requested charging state, auto-resume sessions that fall into `SUSPENDED_EVSE` after reconnects, and restore charging automatically after Home Assistant restarts or cloud outages.

### 🔧 Improvements
- None

### 🔄 Other changes
- None

## v1.3.0
- Charger discovery: automatically register new Enlighten chargers at runtime so freshly installed hardware appears without reconfiguring the integration.
- Coordinator & diagnostics: streamline the first refresh, record backend phase timings, and surface additional error/backoff counters through diagnostics and System Health.
- Charging safeguards: block start requests while the EV is unplugged, raise translated validation errors, and keep switches/buttons in sync with charger reality.
- Testing & tooling: migrate the integration tests under `tests/components/enphase_ev`, refresh fixtures, and align the Docker dev image with Home Assistant’s test harness.

## v1.2.6
- Localisation: add full French translations for the integration strings.
- Docs: note supported languages and bump the integration manifest version.

## v1.2.5
- Coordinator: anchor HTTP and network backoff windows to the configured slow polling interval (and any dynamic interval overrides) so recovery pacing always respects user settings.
- Coordinator: surface the last successful sync, last failure metadata, and the current backoff end as tracked fields and keep site-level diagnostics entities available during cloud outages.
- Diagnostics: add dedicated site sensors for the last error code/message and the active backoff expiry timestamp, and expose the same metadata as attributes on the site cloud reachability binary sensor and existing site latency sensor.

## v1.2.4
- Coordinator: expand HTTP error handling to apply exponential backoff to every response while respecting `Retry-After`, improving stability during cloud outages and throttling.

## v1.2.3
- Charging controls: hold the requested charging state for up to 90 seconds after start/stop commands so the Home Assistant switch and buttons stay steady while the cloud catches up, clearing the hold as soon as the backend confirms the change.
- Coordinator: treat the newer `SUSPENDED_*` connector status variants as active sessions and share the temporary state expectation across all control entry points.
- Docs & tests: document the expanded connector status enums and add regression coverage for the expectation window.
- Tooling: publish a zipped copy of `custom_components/enphase_ev` as a release asset automatically when a GitHub release is created.

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
