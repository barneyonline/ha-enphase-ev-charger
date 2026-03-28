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
- Expanded the service-status synthetic checks to group results by service category and cover the broader set of safe EVSE, BatteryConfig, dashboard, HEMS, timeseries, and inventory endpoints used by the integration.

### 🔄 Other changes
- None

## v2.6.1 - 2026-03-29

### 🚧 Breaking changes
- None

### ✨ New features
- Added HEMS-backed heat-pump daily energy entities, including `Heat Pump Daily Energy` plus optional diagnostic breakdown sensors for daily grid, solar, and battery energy.
- Added a dedicated `Heat Pump SG-Ready Gateway Status` entity for SG-Ready gateway devices discovered in the heat-pump inventory.

### 🐛 Bug fixes
- Preserved Green mode from the live EVSE schedule summary when Enphase temporarily omits the scheduler preference, keeping `preferred_mode` and the charge-mode select stable instead of dropping to `null` or `unknown`.
- Fixed IQ Battery charge-from-grid schedule helper availability so schedule controls stay editable when an existing schedule is present even if Enphase omits parts of the capability metadata.
- Hardened battery write gating so battery profile, reserve, shutdown-level, and charge-from-grid helpers only surface when the account has confirmed owner or installer write access.
- Improved battery settings parsing and reserve-limit handling so battery reserve writes respect Enphase-provided minimum and maximum bounds instead of assuming a fixed upper limit.
- Hardened EV charger auth/config requests and gateway inventory fallbacks so auth settings, config metadata, and connectivity details remain available across more Enphase payload and authorization variants.
- Improved HEMS heat-pump runtime and power parsing so alternate endpoint payload shapes keep runtime, SG-Ready, daily-energy, and power telemetry available instead of dropping state.

### 🔧 Improvements
- Expanded EV charger diagnostic attributes with preserved gateway connectivity details, phase-switch configuration, default charge level, and additional endpoint metadata surfaced from charger config payloads.
- Enriched heat-pump runtime and power diagnostics with endpoint timestamps, device identifiers, and daily-energy context to make diagnostics captures more actionable.

### 🔄 Other changes
- Expanded the API reference documentation for EVSE feature flags, the mobile constants endpoint, and the EV firmware-details endpoint with newer captures and observed payload notes.

## v2.6.0 - 2026-03-28

### 🚧 Breaking changes
- None

### ✨ New features
- Added AI Optimisation battery profile support, including runtime handling for the new battery profile mode.

### 🐛 Bug fixes
- Preserved Green mode when refreshing battery profile state from a fresh payload so existing battery operating mode is not lost during profile updates.
- Mapped IQ Battery LED runtime status `15` to `Idle` for battery status reporting.
- Hardened site power outlier handling so anomalous samples are filtered more safely during live power calculations.
- Stabilized heat-pump power diagnostics and standardized heat-pump runtime async boundaries so runtime refreshes keep telemetry and diagnostics consistent.

### 🔧 Improvements
- Reworked the coordinator refresh pipeline and continued the runtime ownership split by extracting battery, EVSE, heat-pump, and inventory runtime logic into dedicated helpers.
- Refactored coordinator state and diagnostics helpers and extracted remaining coordinator issue-management paths to reduce coordinator complexity and clarify runtime responsibilities.
- Redesigned runtime test ownership and expanded refactor coverage to support the runtime extraction work.

### 🔄 Other changes
- Documented the tariff API endpoint in `docs/api/`.
- Refreshed GitHub issue templates.

## v2.5.0 - 2026-03-24

### 🚧 Breaking changes
- Reworked the per-battery `Status` sensor to report IQ Battery LED runtime state (`Charging`, `Discharging`, `Idle`, or `Unknown`) instead of the prior diagnostic `status/statusText` health label. The raw numeric LED code is now exposed in the sensor `state` attribute, and the battery charge sensor no longer exposes `led_status`.
- Removed the legacy battery inventory count entity and battery-level `Battery Active Microinverters` diagnostic sensor.

### ✨ New features
- Added a diagnostic `Battery CFG Schedule Status` sensor to expose cloud/Envoy CFG schedule sync state (`None`, `Pending`, or `Active`).
- Added the `Update CFG Schedule` service to update the charge-from-grid schedule start time, end time, and charge limit in one atomic operation.

### 🐛 Bug fixes
- Further hardened lifetime-derived site power startup restoration so same-bucket, zeroed, or inconsistent restored history is discarded instead of being reused as live wattage after Home Assistant restart.
- Blocked charge-from-grid schedule time and limit writes while Enphase still reports the CFG schedule as pending Envoy sync, preventing conflicting updates.
- Switched existing CFG schedule edits from delete-and-recreate to in-place `PUT /schedules/{id}` updates, preserving the live schedule while changes are applied.
- Fixed HEMS heat-pump power and event endpoint fallbacks so alternate payload shapes and optional HTML responses no longer break runtime power selection or wipe event diagnostics.

### 🔧 Improvements
- None

### 🔄 Other changes
- Documented the observed IQ Battery LED/runtime state legend and additional API endpoint behavior in the API reference.

## v2.4.1 - 2026-03-22

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed lifetime-derived `Current Grid Power` and `Current Battery Power` startup restoration so stale zeroed restore state no longer produces impossible spike values after Home Assistant restart, including legacy restore entries that did not persist the previous calculation method.

### 🔧 Improvements
- Refactored shared power-sensor restore parsing so EV charger and site lifetime power sensors use the same safe numeric restore helpers for timestamps, power values, and reset markers.

### 🔄 Other changes
- None

## v2.4.0 - 2026-03-22

### 🚧 Breaking changes
- Replaced the legacy heat-pump `SG-Ready Gateway` entity with runtime-backed heat-pump runtime entities, including dedicated `Heat Pump Runtime Status`, `Heat Pump Connectivity Status`, `Heat Pump SG-Ready Mode`, and `Heat Pump Runtime Last Reported` sensors.

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed EV charger charge-mode state fallback so temporary scheduler payload gaps no longer drop the last known preferred mode or force misleading idle/immediate state transitions.
- Fixed the derived `Current Grid Power` sensor so tiny or skewed lifetime-energy timestamp gaps no longer create impossible import/export spikes. The interval floor now also applies when restoring the last live site-power samples after restart.
- Fixed heat-pump runtime, SG-Ready, and daily-consumption reporting to use the updated HEMS runtime and energy-consumption endpoints instead of inferring operating state from inventory health.
- Fixed the `Current Production Power` sensor so malformed live-power payloads no longer clear the entity immediately; the last good sample and attributes are now retained and restored while invalid payload shapes are logged for diagnostics.

### 🔧 Improvements
- Split heat-pump runtime status, connectivity status, SG-Ready mode, and component-status entities so the heat-pump layout aligns more closely with the other device families.

### 🔄 Other changes
- Documented HEMS heat-pump runtime states and SG-Ready mappings in the API reference notes.

## v2.3.5 - 2026-03-19

### 🚧 Breaking changes
- Replaced the split `Current Grid Import Power` and `Current Grid Export Power` entities with a single signed `Current Grid Power` sensor. Import is positive and export is negative.
- Renamed the live production-power entity key from `current_power_consumption` to `current_production_power` to align the entity surface with the observed `get_latest_power` endpoint semantics.

### ✨ New features
- Added restore support for the last two live lifetime-energy samples so lifetime-derived site power sensors can calculate immediately after restart when enough prior live data is available.

### 🐛 Bug fixes
- Fixed lifetime-derived site power sensors so they no longer expose stale or nonsensical startup wattage when only incomplete lifetime-energy history is available.
- Fixed heat-pump power selection so stable HEMS inventories continue ranking alternate device payloads when the previously selected source reports zero or empty samples, avoiding stale low-power picks on delayed backend updates.

### 🔧 Improvements
- Removed stale deprecated split grid-power entities during sensor sync and localized the new `Current Grid Power` label across all supported locales.
- Reduced the cached site lifetime-energy refresh interval from 15 minutes to 5 minutes so energy sensors and lifetime-derived power sensors update more closely to the underlying Enphase interval data.
- Renamed the mislabelled live production-power display name to `Current Production Power`.

### 🔄 Other changes
- None

## v2.3.4 - 2026-03-19

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed derived `Grid Import Power`, `Grid Export Power`, and `Battery Power` site sensors so they wait for the first real lifetime-energy sample before calculating current watts, preventing lifetime totals from being exposed as instantaneous power. Battery Power now also lives under the Enphase Cloud service with the other site aggregate power sensors.

### 🔧 Improvements
- Added regression coverage for the first-real-sample baseline behavior and cloud device association for derived site power sensors.

### 🔄 Other changes
- None

## v2.3.3 - 2026-03-19

### 🚧 Breaking changes
- None

### ✨ New features
- Added derived `Grid Import Power`, `Grid Export Power`, and signed `Battery Power` site sensors based on lifetime energy flows, complementing the existing site solar production power sensor for Home Assistant Energy Dashboard setups.

### 🐛 Bug fixes
- Inferred missing Heat Pump interval metadata and normalized float interval values so runtime telemetry remains available on sites that omit or vary interval metadata. (#389)

### 🔧 Improvements
- Localized the new derived site power sensors across all supported integration locales. (#390)
- Expanded regression coverage for derived site power sensors and Heat Pump interval metadata inference. (#389, #390)

### 🔄 Other changes
- Updated the README feature summary to call out the new derived site power sensors. (#390)

## v2.3.2 - 2026-03-18

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed Heat Pump runtime diagnostics and power sampling edge cases so telemetry stays more reliable across runtime refresh paths. (#385)
- Fixed site-energy import handling and sparse-import fallback behavior, including corrected site power labeling. (#386)

### 🔧 Improvements
- Expanded regression coverage for Heat Pump runtime diagnostics, power sampling, and site-energy import fallback behavior. (#385, #386)

### 🔄 Other changes
- Refreshed the README screenshot gallery with theme-aware light and dark images.
- Updated the API spec notes to match the site-energy import handling changes. (#386)

## v2.3.1 - 2026-03-14

### 🚧 Breaking changes
- None

### ✨ New features
- Added endpoint-level payload health diagnostics so malformed-payload state, stale-cache usage, and recovery details are captured consistently for coordinator, summary, session-history, and EVSE-timeseries refresh paths.

### 🐛 Bug fixes
- Refined heat-pump vitals power selection so HEMS-backed heat-pump telemetry chooses the intended power source more reliably. (#374)
- Fixed battery entity/control gating to rely on battery site-settings support flags instead of stale or incomplete runtime hints. (#375)
- Fixed HEMS heat-pump power refreshes to retry date windows more safely when Enphase rejects specific request dates. (#377)
- Fixed malformed JSON and invalid-shape handling across runtime refresh paths so valid cached charger/site data can be reused temporarily instead of immediately dropping entities unavailable or escalating a single bad endpoint into a full cloud-outage repair.
- Fixed EVSE status payload classification so non-object success payloads are treated as structured payload failures and flow through the same stale-if-error recovery path.

### 🔧 Improvements
- Improved payload troubleshooting by attaching structured, redacted payload-failure signatures to diagnostics and transition-based warning/recovery logs instead of repeatedly logging raw decode failures.
- Improved cloud/site diagnostic attributes to expose payload failure source, endpoint, and stale-data usage for easier field troubleshooting.
- Expanded regression coverage for payload resilience, recovery logging, stale-cache reuse, diagnostics redaction, and endpoint health tracking.
- Hardened HEMS/repository debug logging redaction so payload/debug summaries stay useful without leaking sensitive identifiers. (#379, #382)

### 🔄 Other changes
- Updated repository contribution workflow guidance for Docker formatting checks. (#378)

## v2.3.0 - 2026-03-13

### 🚧 Breaking changes
- None

### ✨ New features
- Added a HEMS support preflight check from the system dashboard summary endpoint so HEMS-backed discovery and heat-pump telemetry can be enabled or skipped earlier and more reliably on a per-site basis. (#365)

### 🐛 Bug fixes
- Fixed startup topology restore and deferred migration handling so restored discovery state, registry migrations, and background startup work no longer race during setup. (#357)
- Fixed HEMS refresh regressions by shortening support-preflight, HEMS inventory, and heat-pump power cache windows so stale or empty HEMS data is not held for several minutes. (#372)
- Fixed EVSE daily-energy timeseries refreshes to request the active local day explicitly via `start_date`, restoring charger energy updates after the topology refactor regression. (#372)

### 🔧 Improvements
- Improved topology performance by separating topology-only updates from state refreshes, caching inventory summary derivations, and avoiding unnecessary registry resyncs when device metadata is unchanged. (#366)
- Expanded regression coverage for startup restore/migrations, HEMS support preflight, topology performance paths, and the HEMS/EVSE regression fixes. (#357, #365, #366, #372)

### 🔄 Other changes
- Documented additional system dashboard endpoints, including summary, master-data, devices-table, devices-tree, status, show-livestream, and range-testing routes. (#358, #359, #360, #361, #362, #364)
- Updated repository maintenance files and GitHub Actions dependencies/configuration. (#368, #369, #370, #371)

## v2.2.3 - 2026-03-13

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed EVSE daily and lifetime timeseries requests to use the required `site_id` query parameter so charger energy fallback endpoints no longer fail with `400 BAD_REQUEST`.

### 🔧 Improvements
- Added regression coverage for the EVSE timeseries `site_id` request parameter handling.

### 🔄 Other changes
- Updated the API spec to document the EVSE timeseries `site_id` requirement and example requests.

## v2.2.2 - 2026-03-13

### 🚧 Breaking changes
- None

### ✨ New features
- Added a cloud current power consumption sensor for site-level live load visibility. (#348)
- Added EVSE timeseries energy fallback paths to preserve charger energy reporting when primary realtime/session sources are incomplete. (#347)

### 🐛 Bug fixes
- Fixed BatteryConfig writes and schedule CRUD flows for EMEA sites, including region-specific auth/header handling. (#340)
- Fixed fast-poll interval fallback handling so charger polling stays aligned with configured intervals. (#350)
- Treated optional system-dashboard and unsupported HEMS endpoints as soft failures instead of allowing them to cascade into misleading payload/auth errors. (#346)
- Hardened system-dashboard diagnostics parsing and fallback handling for Enphase dashboard routes that return unexpected HTML or partial data. (#352)
- Improved auth failure diagnostics by logging the exact request that received a `401` and the stored-credential reauth retry outcome.

### 🔧 Improvements
- Reused recent HEMS inventory payloads when refreshes temporarily fail and surfaced HEMS freshness/staleness details on Heat Pump entities and diagnostics.
- Expanded regression coverage for BatteryConfig EMEA writes, EVSE timeseries fallback behavior, fast-poll fallback handling, optional dashboard/HEMS failures, auth retry logging, and hardened dashboard parsing.

### 🔄 Other changes
- Documented the integration activation checklist and cleaned up API spec notes. (#349)
- Documented the filtered site-device inventory endpoint and refreshed related API docs. (#353)

## v2.2.1 - 2026-03-11

### 🚧 Breaking changes
- None

### ✨ New features
- Added dry-contact settings diagnostics and expanded dry-contact debug visibility. (#342)
- Added system dashboard device diagnostics sourced from Enphase's dashboard endpoints. (#344)

### 🐛 Bug fixes
- Fixed microinverter discovery and tightened site-energy entity gating so unsupported site-energy paths do not surface incorrectly. (#343)
- Fixed Home Assistant reauthentication flow compatibility for cores that expect the standard `reauth_confirm` step, preventing `Invalid flow specified` failures during reauth.
- Hardened unload/update-listener handling so disabled or failed entries do not trigger self-reloads and partial setup states no longer fall into `ConfigEntryState.FAILED_UNLOAD` when unloading.
- Treated optional HEMS HTML/non-JSON fallback pages as endpoint unavailability instead of payload failures to reduce noisy logs and avoid misleading optional-endpoint errors.

### 🔧 Improvements
- Added regression coverage for reauth compatibility, partial-unload handling, optional HEMS non-JSON responses, and related config-entry lifecycle paths.

### 🔄 Other changes
- None

## v2.2.0 - 2026-03-10

### 🚧 Breaking changes
- Raised the minimum supported Home Assistant version to `2024.12.0` to align the integration and development environment with Python 3.13.

### ✨ New features
- Added IQ EV charger firmware-details support, including per-charger firmware update entities.
- Added HEMS-first inventory sourcing for Heat Pump and IQ Energy Router discovery, including support for HEMS-only router entities.

### 🐛 Bug fixes
- Normalized dry-contact device mapping and migrated legacy standalone dry-contact registry entries back to the gateway device.
- Fixed heat-pump power timeseries filtering to prefer documented HEMS `device_uid` values while preserving fallback behavior when metadata is missing.
- Treated EVSE feature flags as advisory hints so runtime-supported charger controls remain available even when cloud feature-flag payloads are stale or misleading.
- Fixed battery reserve and charge-from-grid control availability on EMEA sites by preferring `cfgControl` visibility flags when they are present.

### 🔧 Improvements
- Modernized Python 3.13 compatibility paths by switching to stdlib timeout helpers, tightening runtime dataclasses/serialization, and removing obsolete compatibility branches.
- Expanded diagnostics and API documentation for charger firmware details, EVSE feature flags, HEMS inventory sourcing, and battery control support provenance.
- Added regression coverage for charger firmware updates, HEMS-first inventory, dry-contact normalization, cfgControl-based battery control availability, and Python 3.13 compatibility paths.

### 🔄 Other changes
- None

## v2.1.4 - 2026-03-08

### 🚧 Breaking changes
- None

### ✨ New features
- Added a dedicated `Heat Pump SG Ready Active` binary sensor and clearer SG Ready/contact-state reporting for heat pump sites.

### 🐛 Bug fixes
- Fixed heat pump SG Ready active-state handling when gateway payloads report mixed `Normal` and `Recommended` statuses.
- Pruned historical charger sensor registry entries during setup so legacy charger entities that are no longer created do not linger.
- Fixed EV power estimate clamping for three-phase chargers so power sensors are no longer capped at a single-phase ceiling.

### 🔧 Improvements
- Clarified system controller and dry-contact terminal diagnostic labels/descriptions for heat pump reporting.
- Added regression coverage for SG Ready reporting, stale charger-entity pruning, and phase-aware EV power clamp paths.

### 🔄 Other changes
- Added wiki-published 30-day service-status history with incident summaries, linked the README status badge to the history page, and tightened workflow failure handling for unexpected history fetch/show errors.

## v2.1.3 - 2026-03-05

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Handled malformed/non-JSON cloud payloads as a dedicated payload failure class to avoid unexpected coordinator crashes and preserve predictable backoff/error state.
- Sanitized payload failure reporting so diagnostics/state attributes now store bounded summaries instead of raw response body fragments.
- Fixed reauthentication flow compatibility for Home Assistant cores that invoke `async_step_reauth` without positional entry-data arguments.

### 🔧 Improvements
- Added regression coverage for payload failure classification/sanitization and reauth flow invocation compatibility.

### 🔄 Other changes
- None

## v2.1.2 - 2026-03-03

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Pruned coordinator/session-history runtime caches by active charger serial/day to prevent stale state from persisting across refreshes.
- Hardened unload cleanup so schedule-sync shutdown and runtime cache cleanup run after platform unload succeeds.
- Removed stale schedule-slot switch entities after repeated missing slot refreshes to prevent orphaned toggle entities.

### 🔧 Improvements
- Updated repository URLs after rename across docs, blueprints, manifest metadata, and firmware catalog fetch URLs.

### 🔄 Other changes
- Added regression coverage for runtime cache pruning, unload cleanup, session-history state pruning, and stale schedule-slot cleanup.

## v2.1.1 - 2026-03-02

### 🚧 Breaking changes
- None

### ✨ New features
- Added firmware catalog discovery and firmware update entities for IQ Gateway and Microinverter firmware tracking.
- Added region-specific firmware catalog routing so release metadata resolves correctly across locales/countries.
- Merged dry-contact details into the IQ Gateway diagnostics sensor payload.

### 🐛 Bug fixes
- Fixed Heat Pump power becoming unavailable when HEMS pointers do not align with site payload paths.
- Handled HEMS power `422 date-validation` responses gracefully to preserve sensor availability.
- Fixed Heat Pump sensor labels across all shipped locale files.

### 🔧 Improvements
- Avoid creating Heat Pump and Water Heater site lifetime-energy sensors when those channels are not available for the site.
- Disabled firmware version checks in the integration by default (firmware catalog/update code remains in place for re-enable later).

### 🔄 Other changes
- None

## v2.1.0 - 2026-03-01

### 🚧 Breaking changes
- None

### ✨ New features
- Added Heat Pump device support, including setup/reconfigure selection and dedicated device handling.
- Added IQ Gateway diagnostic sensors for IQ Energy Router monitoring.
- Added lifetime energy sensors for EVSE, Heat Pump, and Water Heater devices with HEMS data support.

### 🐛 Bug fixes
- Fixed Heat Pump reconfigure state handling and device linking behavior.
- Skipped retired IQ Energy Router inventory members when building gateway diagnostics.

### 🔧 Improvements
- Hardened lifetime-energy payload normalization and fallback handling for device lifetime sensors.

### 🔄 Other changes
- Documented IQ Energy Router and Heat Pump HEMS monitoring endpoints and aligned API reference docs.
- Added/expanded regression coverage for lifetime energy sensors, IQ Energy Router diagnostics, Heat Pump onboarding/reconfigure flows, and translation updates.

## v2.0.3 - 2026-02-24

### 🚧 Breaking changes
- None

### ✨ New features
- Added an IQ Gateway `Storm Alert Opt Out` button that bulk-opt-outs all active (non-opted-out) Storm Guard alerts with one request per alert and no-ops when no active alerts exist.

### 🐛 Bug fixes
- Fixed `System Profile Status` getting stuck on `Updating...` after cloud-side profile convergence by reducing stale cache effects during pending profile updates.
- Fixed pending profile resolution by clearing pending state from both storm-guard profile and battery settings payload convergence paths.
- Fixed pending profile matching for `select`-driven system-profile changes to clear once the effective profile matches, even when backend reserve/subtype echo differs.

### 🔧 Improvements
- Improved Storm Alert diagnostic sensor accuracy so `Active` reflects alerts that are not opted out, including robust handling of mixed/legacy alert payload shapes.
- Added pending-profile debug visibility with `pending_requires_exact_settings` on `system_profile_status` attributes.

### 🔄 Other changes
- Added regression coverage for pending-profile cache bypass and convergence clearing paths.

## v2.0.2 - 2026-02-23

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed a startup timeout path where Home Assistant bootstrap could wait on `ScheduleSync.async_start()`; schedule sync startup now runs via background-task APIs so integration setup no longer blocks bootstrap.

### 🔧 Improvements
- Added regression coverage for schedule sync startup task scheduling paths (`config_entry` background task, `hass` background task fallback, and legacy `hass.async_create_task` fallback).

### 🔄 Other changes
- None

## v2.0.1 - 2026-02-23

### 🚧 Breaking changes
- None

### ✨ New features
- Added locale/country-aware nominal-voltage fallback mapping for power-estimation defaults when API voltage is unavailable.

### 🐛 Bug fixes
- Use API `operatingVoltage` to populate `Nominal Voltage (V)` when available, keep it user-editable, and use the configured value in calculations.
- Removed the hardcoded `240V` nominal-voltage fallback.
- Set battery reserve and shutdown minimum from API `veryLowSocMin` (fallback `5%`).
- Fixed stale legacy cloud-error entity placement by migrating legacy cloud-error unique-id variants to the `Enphase Cloud` device.
- Prevent unsupported gateway meter diagnostics from remaining permanently `Unavailable` by pruning unsupported meter entities once inventory is known.

### 🔧 Improvements
- Expanded diagnostics redaction to include site/device identifiers (site IDs, serials, names, and network-identifying fields) in exports.
- Hardened cloud-entity migration logic to sweep and rehome older cloud-diagnostic entity variants.

### 🔄 Other changes
- Added/updated regression tests across voltage fallback, battery reserve/shutdown minimum handling, diagnostics redaction, cloud-entity migration, and gateway meter pruning.
- Maintained 100% coverage for touched integration modules.
- Changes based on feedback from discussion [#297](https://github.com/barneyonline/ha-enphase-ev-charger/discussions/297).

## v2.0.0 - 2026-02-22

### 🚧 Breaking changes
- v2.0 migrates from the legacy `Enphase Site` device anchor to type devices (`Gateway`, `Battery`, `System Controller`, `EV Chargers`, etc.), so existing device-targeted automations/services may need re-selection.
- Device selection moved from per-charger selection to category-based selection.

### ✨ New features
- Added site-level BatteryConfig controls:
  - System Profile controls
  - battery settings (battery mode, charge-from-grid toggles, schedule times, shutdown level)
- Added OTP-gated grid control workflow:
  - services: `request_grid_toggle_otp`, `set_grid_mode`
  - entities: `Request Grid Toggle OTP` button and `Grid Mode` sensor
  - updated OTP helper/script blueprint behavior
- Added type-based device inventory ingestion and type-device diagnostics.
- Added microinverter support:
  - `Microinverters` setup category
  - shared `Microinverters` device
  - per-inverter lifetime sensors
  - site-level microinverter diagnostic sensors
- Added battery telemetry expansion:
  - site-level battery telemetry sensors
  - per-battery status/health/cycle/last-reported diagnostics
  - backup history calendar
- Split cloud diagnostics to a dedicated `Enphase Cloud` device.
- Moved site energy flow sensors to the cloud device and default-enabled them (with migration support).

### 🐛 Bug fixes
- Hardened BatteryConfig/System Profile/Storm Guard writes with clearer, user-facing validation errors (including 401/403 handling).
- Fixed Home Assistant event-loop blocking warning by priming integration version metadata in executor context.
- Fixed duplicate/malformed device model and SKU presentation.
- Fixed microinverter lifetime energy unit display (`kWh`).
- Fixed battery last-reported behavior and recorder handling for noisy timestamp entities.
- Prevented auto-resume from issuing start requests when charger mode is `GREEN_CHARGING`.
- Fixed Safe Mode amps fallback so forced safe-limit behavior applies only while actively charging.

### 🔧 Improvements
- Standardized non-cloud device naming to `IQ <Device>` and stabilized type-device naming/identity behavior.
- Canonicalized gateway/controller/meter typing and migrated legacy site-anchored entities to gateway/type devices.
- Improved Gateway diagnostics with dedicated connectivity, meter, and system-controller entities.
- Improved diagnostics metadata/attribute structure across battery, gateway, cloud, and inverter paths.
- Renamed integration display name to **Enphase Energy** (domain remains `enphase_ev` for compatibility).
- Updated sensor/device placement and pending-state UX:
  - moved `system_profile_status` and `storm_alert` to IQ Gateway
  - moved charger `storm_guard_state` and `charger_authentication` into the Sensor section
  - `system_profile_status` now shows `Updating...` while pending
  - `storm_guard_state` now shows `Updating` while pending

## v2.0.0b6 – 2026-02-22

### 🚧 Breaking changes
- None

### ✨ New features
- Added the `Microinverters` category to initial setup selection so it appears consistently during onboarding (not only in options/reconfigure flows).

### 🐛 Bug fixes
- Fixed Home Assistant event-loop blocking warnings by priming integration version metadata from `manifest.json` in the executor.
- Fixed BatteryConfig control authentication/headers to align with API expectations across System Profile, Charge From Grid, and Storm Guard flows.
- Added explicit 401/403 handling for Storm Guard writes so websocket service calls return actionable validation errors instead of raw client exceptions.
- Fixed duplicate device model/SKU presentation across EV Charger, IQ Battery, and Microinverter devices.
- Fixed microinverter lifetime energy unit display to `kWh` (was incorrectly shown as `MWh`).
- Fixed battery last-reported behavior for site-level diagnostics wiring so update/availability behavior matches other last-reported entities.

### 🔧 Improvements
- Aligned config entry title updates to `Site: <site_id>` for clearer site identification in Home Assistant.
- Increased targeted regression coverage for BatteryConfig auth/error handling, Storm Guard gating, and device info normalization paths.

### 🔄 Other changes
- None

## v2.0.0b4 – 2026-02-22

### 🚧 Breaking changes
- Upgrading from v1.9.1 migrates away from the legacy Enphase Site device anchor to type devices (Gateway, Battery, EV Chargers, etc.). Device-targeted automations/scripts/services bound to the old site device may need re-selection after upgrade.
- Per-charger selection has been replaced by category-based device selection. In v2.0, enabling EV Chargers includes discovered chargers as a group, so setups that previously kept only a subset of chargers may need post-upgrade entity disablement/reconfiguration.

### ✨ New features
- None

### 🐛 Bug fixes
- None

### 🔧 Improvements
- None

### 🔄 Other changes
- None

## v2.0.0b4 – 2026-02-22

### 🚧 Breaking changes
- Upgrading from v1.9.1 migrates away from the legacy Enphase Site device anchor to type devices (Gateway, Battery, EV Chargers, etc.). Device-targeted automations/scripts/services bound to the old site device may need re-selection after upgrade.
- Per-charger selection has been replaced by category-based device selection. In v2.0, enabling EV Chargers includes discovered chargers as a group, so setups that previously kept only a subset of chargers may need post-upgrade entity disablement/reconfiguration.

### ✨ New features
- None

### 🐛 Bug fixes
- None

### 🔧 Improvements
- None

### 🔄 Other changes
- None

## v2.0.0b4 – 2026-02-22

### 🚧 Breaking changes
- Upgrading from v1.9.1 migrates away from the legacy Enphase Site device anchor to type devices (Gateway, Battery, EV Chargers, etc.). Device-targeted automations/scripts/services bound to the old site device may need re-selection after upgrade.
- Per-charger selection has been replaced by category-based device selection. In v2.0, enabling EV Chargers includes discovered chargers as a group, so setups that previously kept only a subset of chargers may need post-upgrade entity disablement/reconfiguration.

### ✨ New features
- None

### 🐛 Bug fixes
- None

### 🔧 Improvements
- None

### 🔄 Other changes
- None

## v2.0.0b5 – 2026-02-22

### 🚧 Breaking changes
- None

### ✨ New features
- Added the `Microinverters` category to initial setup selection so it appears consistently during onboarding (not only in options/reconfigure flows).

### 🐛 Bug fixes
- Fixed Home Assistant event-loop blocking warnings by priming integration version metadata from `manifest.json` in the executor.
- Fixed BatteryConfig control authentication/headers to align with API expectations across System Profile, Charge From Grid, and Storm Guard flows.
- Added explicit 401/403 handling for Storm Guard writes so websocket service calls return actionable validation errors instead of raw client exceptions.
- Fixed duplicate device model/SKU presentation across EV Charger, IQ Battery, and Microinverter devices.
- Fixed microinverter lifetime energy unit display to `kWh` (was incorrectly shown as `MWh`).
- Fixed battery last-reported behavior for site-level diagnostics wiring so update/availability behavior matches other last-reported entities.

### 🔧 Improvements
- Aligned config entry title updates to `Site: <site_id>` for clearer site identification in Home Assistant.
- Increased targeted regression coverage for BatteryConfig auth/error handling, Storm Guard gating, and device info normalization paths.

### 🔄 Other changes
- None

## v2.0.0b4 – 2026-02-22

### 🚧 Breaking changes
- Upgrading from v1.9.1 migrates away from the legacy Enphase Site device anchor to type devices (Gateway, Battery, EV Chargers, etc.). Device-targeted automations/scripts/services bound to the old site device may need re-selection after upgrade.
- Per-charger selection has been replaced by category-based device selection. In v2.0, enabling EV Chargers includes discovered chargers as a group, so setups that previously kept only a subset of chargers may need post-upgrade entity disablement/reconfiguration.

### ✨ New features
- None

### 🐛 Bug fixes
- Fixed missing diagnostic sensor icons, including `Active Microinverters`, `Microinverter Connectivity Status`, `Battery System Profile Status`, and `Grid Control Status`.
- Aligned icon usage across shared/common sensors, including consistent icon treatment for `Last Reported` entities.
- Corrected EV charger model naming to remove malformed/duplicated text (for example `Q EV Charger ...` formatting issues).
- Normalized gateway and microinverter connectivity status values to capitalized display text.
- Fixed battery `Last Reported` behavior by removing the site-level aggregate sensor, retaining per-battery `Last Reported` sensors, and keeping unique-ID battery prefixes.
- Resolved `unknown` state behavior for affected current/last-reported timestamp sensors.
- Excluded all `Last Reported` sensors from recorder history.

### 🔧 Improvements
- Standardized device naming to `IQ <Device>` for all non-cloud devices (for example `IQ Battery`, `IQ EV Charger`, `IQ Microinverters`, `IQ Gateway`) and restored controller device naming to `Gateway`.
- Rounded kWh-based values to 2 decimal places.
- Unified device-info layout across devices to the clean multi-line format used by EV Charger device info.
- Expanded Enphase Cloud device metadata presentation (including service/integration version where available).
- Aligned energy sensor unit presentation to consistent kWh usage.

### 🔄 Other changes
- None

## v2.0.0b3 – 2026-02-21

### 🚧 Breaking changes
- None

### ✨ New features
- Reworked onboarding to a category-based `Select Devices` step that shows integration device groups (instead of charger-only selection), defaults discovered categories to enabled, keeps the shared scan interval control, and stores category selections for runtime entity gating ([#279](https://github.com/barneyonline/ha-enphase-ev-charger/pull/279)).
- Added category-based device toggles in Configure Options, preserved unknown stored type keys, and forced integration entry titles to numeric site IDs across setup/reconfigure/reauth paths ([#280](https://github.com/barneyonline/ha-enphase-ev-charger/pull/280)).
- Split out a dedicated `Enphase Cloud` device and moved cloud diagnostics to it; added a battery `Last Reported` type-level sensor with aggregate reporting attributes ([#283](https://github.com/barneyonline/ha-enphase-ev-charger/pull/283)).
- Moved site energy flow sensors to the cloud device and enabled them by default, with migration support for existing entities ([#286](https://github.com/barneyonline/ha-enphase-ev-charger/pull/286)).
- Switched the grid OTP helper blueprint to a Companion App actionable-notification reply flow and kept runtime OTP prompts in the script blueprint ([#287](https://github.com/barneyonline/ha-enphase-ev-charger/pull/287)).

### 🐛 Bug fixes
- Prevent auto-resume from issuing a start request when a charger is in `GREEN_CHARGING` mode after cloud reconnects or temporary outages ([#276](https://github.com/barneyonline/ha-enphase-ev-charger/pull/276), [issue #274](https://github.com/barneyonline/ha-enphase-ev-charger/issues/274)).
- Fixed `battery_available_energy` metadata by removing the invalid energy `state_class` assignment ([#277](https://github.com/barneyonline/ha-enphase-ev-charger/pull/277)).
- Fixed Safe Mode 8A fallback behavior so `Set Amps` is forced to the safe limit only while charging is actively true; idle states now use configured/fallback setpoints ([#284](https://github.com/barneyonline/ha-enphase-ev-charger/pull/284)).

### 🔧 Improvements
- Expanded service-status checks to cover newer BatteryConfig, diagnostics, and inverter endpoints and added optional locale-aware service-status execution support ([#277](https://github.com/barneyonline/ha-enphase-ev-charger/pull/277)).
- Refreshed type-device metadata generation (serial/model/hardware/software summaries), improved gateway/system-controller naming fallbacks, normalized EVSE model composition, and hardened stale metadata clearing rules ([#281](https://github.com/barneyonline/ha-enphase-ev-charger/pull/281)).
- Stabilized battery entity ordering migrations for charge-from-grid schedule controls, hardened battery health parsing/validation, and simplified battery/microinverter diagnostics attributes ([#282](https://github.com/barneyonline/ha-enphase-ev-charger/pull/282)).
- Refined EV charger status presentation by normalizing status labels and storm-guard naming/category behavior, with `status_raw` retained for migration/debug visibility ([#284](https://github.com/barneyonline/ha-enphase-ev-charger/pull/284)).
- Aligned microinverter diagnostics state evaluation, retired the legacy microinverter inventory sensor, and repurposed reporting-count behavior into user-facing active microinverter reporting ([#285](https://github.com/barneyonline/ha-enphase-ev-charger/pull/285)).

### 🔄 Other changes
- Updated translations (`strings.json` + all locale files) and expanded regression coverage across onboarding/options/device migration/diagnostics flows to preserve 100% coverage on touched modules ([#280](https://github.com/barneyonline/ha-enphase-ev-charger/pull/280), [#283](https://github.com/barneyonline/ha-enphase-ev-charger/pull/283), [#284](https://github.com/barneyonline/ha-enphase-ev-charger/pull/284), [#285](https://github.com/barneyonline/ha-enphase-ev-charger/pull/285)).

## v2.0.0b2 – 2026-02-17

### 🚧 Breaking changes
- None

### ✨ New features
- Added site-level battery telemetry sensors for available energy, available power, and inactive microinverter count on the `Battery` device.
- Added per-battery diagnostic sensors for status, health (SoH), cycle count, and last reported timestamp, with dynamic add/remove lifecycle sync.
- Added site-level microinverter diagnostic sensors (`Microinverter Connectivity Status`, `Microinverter Reporting Count`, and `Microinverter Last Reported`) on the shared `Microinverters` device.

### 🐛 Bug fixes
- Translate battery profile write failures (including HTTP 403/401 responses) into actionable validation errors and enforce read-only user write restrictions.
- Catch and translate System Profile selector write failures into user-facing Home Assistant errors so raw BatteryConfig HTTP exceptions no longer bubble through websocket service calls.
- Preserve the `System Controller` inventory entity during legacy cleanup by retiring only obsolete `meter`/`gateway` inventory unique IDs; avoid deleting the new `type_enpower_inventory` replacement on runtime registry sync.
- Handle system-profile write timeout errors with actionable Home Assistant messages instead of surfacing raw exceptions.
- Normalize battery storage `id` attributes to plain numeric strings without thousands separators.
- Prevent `battery_overall_status` from being misclassified as a per-battery sensor during registry sync, which could previously remove the entity after startup.
- Keep user-customized charge-from-grid schedule time entity IDs intact during migration; only rename known legacy defaults.

### 🔧 Improvements
- Remove the `Gateway Inventory` sensor and replace it with dedicated Gateway diagnostics (`Gateway Connectivity Status`, `Gateway Connected Devices`, and `Gateway Last Reported`) that prioritize actionable health and connectivity summaries.
- Add dedicated `Production Meter` and `Consumption Meter` diagnostic sensors on the shared `Gateway` device, keyed by meter `channel_type`, with meter status as state and full meter payload exposure in attributes.
- Add a dedicated `System Controller` diagnostic inventory sensor on the shared `Gateway` device (state = `statusText`) with flattened per-property attributes from the controller inventory payload.
- Move `grid_control_supported` and `grid_toggle_allowed` attribute display to the `Grid Mode` sensor and remove duplicate copies from `Grid Control Status`.
- Extend Gateway device diagnostics snapshots with normalized connectivity/status/model/firmware summaries and surfaced property-key coverage for easier gap analysis.
- Exclude volatile cloud/gateway timestamp/error-response attributes from recorder history to reduce noise (for example last success/failure/backoff metadata).
- Re-enable the new Gateway replacement diagnostics by default and localize their names via translation keys (`System Controller`, `Production Meter`, `Consumption Meter`, and gateway status sensors).
- Canonicalize meter and system-controller (`enpower`) type identifiers to the gateway (`envoy`) type so separate legacy type devices are no longer created.
- Migrate legacy `Enphase Site <site_id>` entities to the `Gateway` device and prune empty legacy site devices from the registry.
- Stabilize type-device names by removing dynamic count suffixes (for example `Microinverters (16)` -> `Microinverters`) and shift quantity detail to the device sub-name/model summary (for example `IQ7A x16`, `IQ Battery 5P x2`).
- Migrate charge-from-grid schedule time entities to deterministic `from`/`to` IDs and preserve start-then-end ordering under the schedule control.
- Promote primary battery status fields to first-class entities while keeping detailed/raw data in diagnostic attributes and diagnostics payloads.
- Localize newly added battery telemetry labels across all non-English locale files and add translation guard coverage to prevent English fallback regressions.
- Enriched microinverter type inventory and diagnostics with additional inverter API metadata: status-type counts, panel info, firmware and array summaries, latest-reported inverter details, and production-window dates.
- Filter retired microinverters from inverter inventory/status rollups so entity counts and status summaries reflect only active members.

### 🔄 Other changes
- Expanded battery controls/sensors/time-entity test coverage and maintained 100% coverage for touched integration modules.
- Added focused coordinator/sensor/diagnostics regression tests for the new microinverter alignment paths and translation keys.

## v2.0.0b1 – 2026-02-13

### 🚧 Breaking changes
- Replaced the legacy `Enphase Site <site_id>` device anchor with inventory type devices (`Gateway`, `Battery`, `System Controller`, `EV Chargers`, etc.); site-level entities/controls are now attached to type-specific devices and are skipped when that type is not present.

### ✨ New features
- Added site-level BatteryConfig profile controls.
- Added site-level Battery Settings controls (battery mode, charge-from-grid toggles, schedule start/end times, and battery shutdown level) and remapped them under inventory type devices.
- Exposed additional EV charger cloud metadata from the status/summary APIs across existing diagnostic entities (charge mode, last reported, storm alert, battery mode, and system profile status), including schedule context, firmware/network diagnostics, storm alert metadata, and battery site/profile capability flags.
- Added grid-control eligibility endpoint integration (`grid_control_check.json`) and a `Grid Control Status` diagnostic sensor that reports `ready`/`blocked`/`pending` with detailed guard-flag attributes on battery-capable sites.
- Added OTP-gated grid control services: `request_grid_toggle_otp` and `set_grid_mode` with single-site routing (`site_id`/device target), runtime `mode` + `otp` inputs, and explicit validation errors.
- Added a site-level `Request Grid Toggle OTP` button entity and a site-level `Grid Mode` sensor (`on_grid`/`off_grid`/`unknown`).
- Added a single runtime-mode script blueprint at `blueprints/script/enphase_ev/grid_mode_otp.yaml` for Home Assistant dashboards.
- Added `devices.json` inventory ingestion with canonical per-type buckets, frontend-style type naming (`<Label> (<count>)`), and retired-device filtering.
- Added read-only per-type inventory diagnostic sensors (state = active member count; attributes = normalized member details) plus type-device diagnostics snapshots.
- Added onboarding auto-discovery defaults that preselect discovered EV chargers and reconfigure controls that allow enabling/disabling charger devices.
- Added an `Inverters` integration option in setup/reconfigure that enables microinverter discovery for the selected site.
- Added a shared `Microinverters` device with one lifetime-energy sensor per inverter (MWh), including inverter metadata attributes and device-level model/status summaries.
- Added inverter endpoint integration (`inverters.json`, `inverter_status_x`, `inverter_data_x`) with ID-to-serial mapping, site-local date handling, and dynamic add/remove entity lifecycle updates.
- Added battery status endpoint integration (`/pv/settings/<site_id>/battery_status.json`) with per-battery charge sensors, aggregate battery charge/status sensors, dynamic add/remove lifecycle handling, and diagnostics payload capture under the shared `Battery` type device.
- Added a site-level `Backup History` calendar entity on the `Battery` type device, backed by `/app-api/<site_id>/battery_backup_history.json` with normalized outage intervals, coordinator caching, and diagnostics payload capture.

### 🐛 Bug fixes
- None

### 🔧 Improvements
- Improved Battery Settings write handling with optimistic updates, disclaimer auto-stamping when enabling charge-from-grid, and dedicated write lock/debounce safeguards.
- Moved safe-limit diagnostics to the Set Amps sensor (from Connector Status) and expanded Last Session attributes with session authentication metadata.
- Added EVSE lifetime energy flow parsing and exposed EVSE charging kWh as a site-energy sensor attribute.
- Remapped Gateway/Battery entities to their relevant type devices and re-parented per-serial EV charger devices via the `EV Chargers` type device when available.
- Moved `SystemProfileSelect`, `CancelPendingProfileChangeButton`, and `StormGuardSwitch` under the `Gateway` device while keeping battery-setting controls under `Battery`.
- Added runtime registry synchronization so type-device naming/parent relationships stay aligned with refreshed inventory data.
- Added hard grid-control guard enforcement (support + guard flags + OTP format + envoy serial checks), no-OTP persistence behavior, fast-poll refresh kick on successful toggle, and best-effort audit logging via `log_grid_change`.
- Documented the battery status endpoint and payload field reference in the API specification, including anonymized request/response examples and storage-level behavior notes.

### 🔄 Other changes
- Fixed full-suite `pytest tests/components/enphase_ev -q` recursion failures by resetting pytest-socket state during test setup.
- Expanded battery-settings and entity gating tests to keep changed integration modules at 100% coverage.
- Expanded coverage for inventory normalization, retired filtering, unknown type handling, type-device diagnostics, reconfigure empty-selection behavior, and service/device-action resolution with type identifiers.

## v1.9.0 – 2026-02-07

### 🚧 Breaking changes
- None

### ✨ New features
- Added Storm Guard support with a site-level Storm Guard switch, per-charger Storm Guard EV Charge switch, Storm Guard State sensor, and Storm Alert diagnostic sensor.

### 🐛 Bug fixes
- Refresh Storm Guard profile data before toggling settings so state changes use current EVSE preference values.

### 🔧 Improvements
- Normalize Storm Guard and storm alert metadata in coordinator payload handling for consistent sensor/switch availability.

### 🔄 Other changes
- Documented BatteryConfig Storm Guard profile and toggle endpoints in the API specification.

## v1.8.2 – 2026-01-31

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- Prefer session history payload for last session metadata when idle so cost/duration/id fields populate correctly.

### 🔧 Improvements
- None

### 🔄 Other changes
- None

## v1.8.1 – 2026-01-30

### 🚧 Breaking changes
- None

### ✨ New features
- None

### 🐛 Bug fixes
- None

### 🔧 Improvements
- Surface safe-limit state in connector diagnostics and reflect safe-mode amperage overrides in charging amp entities.
- Trigger a fast refresh and immediate state write when charging start fails (e.g., unplugged/not_ready) so HomeKit switches revert quickly.
- Swap site discovery to the Enlighten search API for both the integration and service-status report, with deduped site titles in the picker and updated API documentation.
- Drop the legacy single-charger status endpoint from the integration, service-status checks, and documentation.
- Align session history requests with the Enlighten web API (filter criteria call, username/requestid headers, updated payload shape, and timezone support).

### 🔄 Other changes
- None

## v1.8.0 – 2026-01-29

### 🚧 Breaking changes
- None

### ✨ New features
- Added the green charging “Use Battery for EV Charging” toggle so green-mode sessions can force battery supplementation when supported by the site summary.
- Introduced the charger authentication diagnostic sensor plus the “Auth via App” toggle so Home Assistant surfaces Enphase app/RFID requirements and lets users toggle app auth without leaving HA; start charging now logs a warning (instead of blocking) when authentication is required so the request completes once Enphase auth finishes.

### 🐛 Bug fixes
- None

### 🔧 Improvements
- Handle degraded Enlighten subservices gracefully, marking scheduler/session-history/site-energy/auth-settings availability and treating 550 session-history responses as degraded instead of erroring so sensors fall back to cache when the backend is partially offline.

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
