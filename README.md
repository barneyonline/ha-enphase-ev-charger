# Enphase EV Charger 2 (Cloud) — Home Assistant Custom Integration

<!-- Badges -->
[![Release](https://img.shields.io/github/v/release/barneyonline/ha-enphase-ev-charger?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-enphase-ev-charger/releases)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-enphase-ev-charger)](https://github.com/barneyonline/ha-enphase-ev-charger/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-enphase-ev-charger)](LICENSE)

[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-ev-charger/tests.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-enphase-ev-charger/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/barneyonline/ha-enphase-ev-charger/graph/badge.svg?token=ichJ6LKzFK)](https://codecov.io/gh/barneyonline/ha-enphase-ev-charger)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-ev-charger/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-enphase-ev-charger/actions/workflows/hassfest.yml)

[![Quality Scale](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-enphase-ev-charger%2Fmain%2Fcustom_components%2Fenphase_ev%2Fmanifest.json&query=%24.quality_scale&label=quality%20scale&cacheSeconds=3600)](https://developers.home-assistant.io/docs/integration_quality_scale_index)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)

[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-enphase-ev-charger)](https://github.com/barneyonline/ha-enphase-ev-charger/issues)

This custom integration surfaces the **Enphase IQ EV Charger 2** in Home Assistant using the same **Enlighten cloud** endpoints used by the Enphase mobile app and adds:

- Start/stop charging directly from Home Assistant while respecting your Manual/Scheduled/Green charge mode preferences
- Set and persist the charger’s current limit, auto-clamped to the charger’s supported amp range
- View plugged-in and charging state in real time, plus a charger-problem flag exposed as a status attribute
- Track live power plus last-session energy and duration without daily resets
- Inspect connection diagnostics (active interface, IP address, reporting interval) via connectivity attributes

All strings (config flow, entities, diagnostics, and options) are localized in English, French, German, Spanish, and Brazilian Portuguese.

## Screenshots

![Controls card showing charge mode, amps slider, and start/stop buttons](docs/images/controls.png)

![Sensors card with live session metrics and energy statistics](docs/images/sensors.png)

![Diagnostic card with connection status, connector state, and IP address](docs/images/diagnostic.png)

## Installation

Recommended: HACS
1. In Home Assistant, open **HACS → Integrations**.
2. Search for **Enphase EV Charger 2 (Cloud)**.
3. Open the integration listing and click **Download/Install**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → + Add Integration → Enphase EV Charger 2 (Cloud)** and follow the prompts.

Alternative: Manual copy
1. Download the latest release asset (`enphase_ev.zip`) from [GitHub Releases](https://github.com/barneyonline/ha-enphase-ev-charger/releases) and extract it.
2. Copy the extracted `custom_components/enphase_ev/` folder into your Home Assistant `config/custom_components/` directory.
3. Restart Home Assistant.
4. Add the integration via **Settings → Devices & Services → + Add Integration → Enphase EV Charger 2 (Cloud)**.

## Authentication

**Preferred: Sign in with Enlighten credentials**

1. In Home Assistant, go to **Settings → Devices & Services → + Add Integration** and pick **Enphase EV Charger 2 (Cloud)**.
2. Enter the Enlighten email address and password that you use at https://enlighten.enphaseenergy.com/.
3. (Optional) Enable **Remember password** if you want Home Assistant to re-use it for future re-authentications.
4. After login, select your site and tick the chargers you want to add, then finish the flow.

If the login form reports that multi-factor authentication is required, complete the challenge in a browser and retry once the account is verified. Manual header capture is no longer supported.

## Entities & Services

| Entity Type | Description |
| --- | --- |
| Site sensors | Last Successful Update timestamp, Cloud Latency in milliseconds, Cloud Error Code (with descriptive context), and a Cloud Backoff Ends timestamp so you can see exactly when active retry windows clear. |
| Site binary sensor | Cloud Reachable indicator (on/off) with attributes for the last success, last failure (status, description, response), and any active backoff window. |
| Site lifetime energy sensors | Disabled by default; expose lifetime Grid Import/Export, Solar Production, Battery Charge, and Battery Discharge totals for the Energy Dashboard. |
| Switch | Per-charger charging control (on/off) that honors the configured charge mode and stays in sync even if a session is already active. |
| Button | Start Charging and Stop Charging actions for each charger that enforce the active Manual/Scheduled/Green preference before calling the cloud API. |
| Select | Charge Mode selector (Manual, Scheduled, Green) backed by the cloud scheduler. |
| Number | Charging Amps setpoint (auto-clamped to the charger’s min/max) without initiating a session. |
| Charger binary sensors | Plugged In, Charging, and Connected states for each charger (Connected includes connection/phase/IP/DLB attributes). |
| Sensor (charging metrics) | Last Session energy (with duration/cost/range attributes), Lifetime Energy, Power, Set Amps (with charger amp limits), Charge Mode, and Status (with commissioned + charger problem attributes). |
| Sensor (diagnostics) | Connector Status (with pause reason) and Last Reported timestamp sourced from the cloud API. |

Sites without chargers can still be added in **site-only** mode to keep the site device and lifetime energy sensors active.

**Services (Actions)**

| Action | Description | Fields |
| --- | --- | --- |
| `enphase_ev.start_charging` | Start charging for the charger(s) selected via the service target (supports multiple devices) while preserving the charger’s Manual/Scheduled/Green mode. | Advanced fields: `charging_level` (optional A; defaults to the stored/last session amps and is clamped to the charger limits), `connector_id` (optional; defaults to 1) |
| `enphase_ev.stop_charging` | Stop charging on the charger(s) selected via the service target. | None |
| `enphase_ev.trigger_message` | Request the selected charger(s) to send an OCPP message and return the cloud response. | `requested_message` (required; e.g. `MeterValues`). Advanced: `site_id` (optional override) |
| `enphase_ev.clear_reauth_issue` | Clear the integration’s reauthentication repair for the chosen site device(s). | `site_id` (optional override) |
| `enphase_ev.start_live_stream` | Request faster cloud status updates for a short period. | Advanced fields: `site_id` (optional; stream a specific site) |
| `enphase_ev.stop_live_stream` | Stop the cloud live stream request. | Advanced fields: `site_id` (optional; stop streaming for a specific site) |

- The `Last Session` sensor exposes localized session metadata attributes (plug-in/out timestamps, energy consumed in kWh/Wh, duration, cost, charge level, and range added) and preserves the latest completed/active session totals across days and restarts.

## Privacy & Rate Limits

- Credentials are stored in HA’s config entries and redacted from diagnostics.
- The integration polls `/status` every 30 seconds by default (configurable).  
- Uses the Enlighten login flow to obtain session headers and refreshes them automatically when the password is stored.

## Future Local Path

> ⚠️ Local-only access to EV endpoints is **role-gated** on IQ Gateway firmware 7.6.175. The charger surfaces locally under `/ivp/pdm/*` or `/ivp/peb/*` only with **installer** scope. This integration therefore uses the **cloud API** until owner-scope local endpoints are available.

When Enphase exposes owner-scope EV endpoints locally, we can add a local client and prefer it automatically. For now, local `/ivp/pdm/*` and `/ivp/peb/*` returned 401 in discovery.

---

### Troubleshooting

- **401 Unauthorized**: Open the integration options and choose **Start reauthentication** to refresh credentials.  
- **No entities**: Check that your serial is present in `/status` response (`evChargerData`), and matches the configured serial.  
- **Rate limiting**: Increase `scan_interval` to 30s or more.
- **wrong_account**: The reconfigure flow stays tied to the site that was originally configured. Remove and re-add the integration if you need to link a different site/account.

### Documentation

- API reference notes: `docs/api/`
- Screenshot assets for the README: `docs/images/`

### Development

- Python 3.13 recommended. Create and activate: `python3.13 -m venv .venv && source .venv/bin/activate`
- Install dev deps: `pip install -U pytest pytest-asyncio pytest-homeassistant-custom-component homeassistant ruff black`
- Lint: `ruff check .`
- Format: `black custom_components/enphase_ev`
- Run tests: `pytest tests/components/enphase_ev -q`

### Dockerised Dev Environment

The repository also includes a ready-to-use Docker setup under `devtools/docker/` for reproducible testing:

```bash
# Build the dev image
docker compose -f devtools/docker/docker-compose.yml build ha-dev

# Run the full test suite
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev"

# Run pre-commit hooks
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"
```

### Options

- Polling intervals: Configure slow (idle) and fast (charging) intervals. The integration auto‑switches and also uses a short fast window after Start/Stop to reflect changes faster.
- API timeout: Default 15s (Options → API timeout).
- Nominal voltage: Default 240 V; used to estimate power from amps when the API omits power.
- Fast while streaming: On by default; prefers faster polling while an explicit cloud live stream is active.
- Site-only mode: Skip charger polling when your site has no chargers; keeps the site device and lifetime energy sensors active.

### System Health & Diagnostics

- System Health (Settings → System → Repairs → System Health):
  - Site ID: your configured site identifier
  - Can reach server: live reachability to Enlighten cloud
  - Last successful update: timestamp of most recent poll
  - Cloud latency: round‑trip time for the last status request
- Diagnostics: Downloaded JSON excludes sensitive headers (`e-auth-token`, `Cookie`) and other secrets.

### Energy Dashboard

- Site-level energy sensors (disabled by default) map directly to Energy Dashboard slots:
  - Grid Consumption → `Site Grid Import`
  - Return to Grid → `Site Grid Export`
  - Solar Production → `Site Solar Production`
  - Battery Charge / Discharge → `Site Battery Charge` and `Site Battery Discharge`
- These sensors live on the Site device with `device_class: energy`, `state_class: total_increasing`, and kWh units, and they track lifetime totals with reset guards.
- The charger `Lifetime Energy` sensor remains available for per-charger consumption tracking if you prefer.

### Behaviours

| Connector Status | Meaning |
| --- | --- |
| AVAILABLE | Charger is idle and ready; no vehicle is drawing power. The integration treats this as the non-charging baseline. |
| CHARGING | Energy is flowing to the EV; the session is marked as active. |
| FINISHING | Charger is tapering a completed session while the vehicle remains plugged in; still considered active until the plug is removed. |
| SUSPENDED | Firmware-reported pause while the session remains logically active (for example, balancing or awaiting confirmation). Charging remains true so automations stay active. |
| SUSPENDED_EV | The vehicle requested a pause (typical OCPP behaviour). Because power can resume without a new session, Home Assistant continues to show an “active” charging posture. |
| SUSPENDED_EVSE | The charger itself paused delivery (load management, scheduling, insufficient solar, etc.). The coordinator records `suspended_by_evse = True` and flips `charging` to false so dashboards show a paused session. |
| FAULTED | Hardware or safety fault; user action or service intervention is required. The connector status sensor maps this to an alert icon for visibility. |

- The Connector Status sensor now exposes a `Status Reason` attribute mirroring Enlighten's `connectorStatusReason` value (for example, `INSUFFICIENT_SOLAR`) so automations can react to the underlying pause cause.

- Charging Amps (number) stores your desired setpoint but does not start charging. The Start button, Charging switch, or start service will reuse that stored/last session value, clamp it to the charger’s supported range, and fall back to 32 A when the backend provides no hints. When you adjust the number during an active session, the integration automatically pauses charging, waits ~30 seconds, and restarts with the new amps so the updated limit sticks.
- Start/Stop actions now require the EV to be plugged in; unplugged requests raise a translated validation error so the UI tells the user to connect before trying again.
- Use the `binary_sensor.<charger>_plugged_in` entity in conditional cards or automation conditions to hide/disable start controls until the vehicle is connected.
- Backend responses that report the charger as “not ready” or “not plugged” are treated as benign no-ops without optimistic state changes, keeping Home Assistant in sync with the hardware.
- Charging state tracks the backend `charging` flag while EVSE-side suspensions (`SUSPENDED_EVSE`) are treated as paused; when you previously requested charging, the integration automatically re-sends the start command after reconnecting so cloud dropouts and Home Assistant restarts resume charging without manual intervention.
- The Charge Mode select works with the scheduler API and reflects the service’s active mode.
- When you start charging from the switch, buttons, or start service, the integration enforces the active charge mode: Manual sends the explicit amps payload, Scheduled ensures the scheduler stays enabled, and Green omits the charging level so solar-only behaviour is preserved.

### Reconfigure

- You can reconfigure the integration (switch sites, update charger selection, or refresh credentials) without removing it.
- Go to Settings → Devices & Services → Integrations → Enphase EV Charger 2 (Cloud) → Reconfigure, then sign in with your Enlighten credentials.
- The wizard skips the site selector when reconfiguring and will abort with `wrong_account` if you try to switch to a different site; remove and add the integration again to change sites.
- Stored passwords pre-fill automatically; otherwise you will be asked to provide them during the flow.

### Supported devices

- Supported
  - Enphase IQ EV Charger 2 variants (single-connector), as exposed via Enlighten cloud.
- Unsupported / not tested
  - Earlier charger generations or models not exposed by the Enlighten EV endpoints.
  - Multi-connector or region-specific variants not returning compatible status/summary payloads.

### Removing the integration

- Go to Settings → Devices & Services → Integrations.
- Locate “Enphase EV Charger 2 (Cloud)” and choose “Delete” to remove the integration and its devices.
- If installed via HACS, you may also remove the repository entry from HACS after removal.
