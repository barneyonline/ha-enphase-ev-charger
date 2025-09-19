# Enphase EV Charger 2 (Cloud) — Home Assistant Custom Integration

<!-- Badges -->
[![Release](https://img.shields.io/github/v/release/barneyonline/ha-enphase-ev-charger?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-enphase-ev-charger/releases)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-enphase-ev-charger)](https://github.com/barneyonline/ha-enphase-ev-charger/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-enphase-ev-charger)](LICENSE)

[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-ev-charger/tests.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-enphase-ev-charger/actions/workflows/tests.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-ev-charger/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-enphase-ev-charger/actions/workflows/hassfest.yml)
[![Quality Scale Check](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-ev-charger/quality_scale.yml?branch=main&label=quality%20scale%20check)](https://github.com/barneyonline/ha-enphase-ev-charger/actions/workflows/quality_scale.yml)

[![Quality Scale](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-enphase-ev-charger%2Fmain%2Fcustom_components%2Fenphase_ev%2Fmanifest.json&query=%24.quality_scale&label=quality%20scale&cacheSeconds=3600)](https://developers.home-assistant.io/docs/integration_quality_scale_index)
[![Integration Version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-enphase-ev-charger%2Fmain%2Fcustom_components%2Fenphase_ev%2Fmanifest.json&query=%24.version&label=integration%20version&cacheSeconds=3600)](custom_components/enphase_ev/manifest.json)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

[![Downloads](https://img.shields.io/github/downloads/barneyonline/ha-enphase-ev-charger/total)](https://github.com/barneyonline/ha-enphase-ev-charger/releases)
[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-enphase-ev-charger)](https://github.com/barneyonline/ha-enphase-ev-charger/issues)

This custom integration surfaces the **Enphase IQ EV Charger 2** in Home Assistant using the same **Enlighten cloud** endpoints used by the Enphase mobile app.

> ⚠️ Local-only access to EV endpoints is **role-gated** on IQ Gateway firmware 7.6.175. The charger surfaces locally under `/ivp/pdm/*` or `/ivp/peb/*` only with **installer** scope. This integration therefore uses the **cloud API** until owner-scope local endpoints are available.

## Installation

Recommended: HACS
1. In Home Assistant, open **HACS → Integrations**.
2. Click the three‑dot menu → **Custom repositories**.
3. Add `https://github.com/barneyonline/ha-enphase-ev-charger` with category **Integration**.
4. In HACS, search for and open **Enphase EV Charger 2 (Cloud)**, then click **Download/Install**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → + Add Integration → Enphase EV Charger 2 (Cloud)** and follow the prompts.

Alternative: Manual copy
1. Copy the `custom_components/enphase_ev/` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings → Devices & Services → + Add Integration → Enphase EV Charger 2 (Cloud)**.

Alternatively, YAML (advanced):
```yaml
enphase_ev:
  site_id: "5527819"
  serials: ["483591047321"]
  e_auth_token: "!secret enphase_eauth"
  cookie: "!secret enphase_cookie"
  scan_interval: 15
```

## Required Inputs

- **Site ID**: Numeric site identifier (e.g., `5527819`).  
- **Serials**: One or more charger serial numbers (e.g., `483591047321`). The serial is printed under the charger's face plate.  
- **e-auth-token header**: From a logged-in Enlighten session, referenced in the Request body as `Authorization`.  
- **Cookie header**: The full cookie string from the same session.  

> Paste the exact values captured from your browser/app session. If you receive a 401 later, re-open the options and paste refreshed headers.

<details>
  <summary>How to capture e-auth-token and Cookie in Chrome</summary>

1. Open Chrome and sign in to https://enlighten.enphaseenergy.com/.
2. Press `Cmd+Opt+I` (macOS) or `Ctrl+Shift+I` (Windows/Linux) to open DevTools.
3. Go to the **Network** tab and enable **Preserve log**.
4. Refresh the page. Filter for your 'site-ID` (e.g. `5527819`).
   - One will contain the `Authorization` and the other will contain the `Cookie`
5. Under **Headers → Request Headers**, copy the values for:
   - `Authorization` = `e-auth-token`
   - `Cookie` = `cookie` (copy the entire cookie string)
6. Optionally, you can find the cookie under **Application → Storage → Cookies → enphaseenergy.com**.

</details>

<details>
  <summary>How to capture e-auth-token and Cookie in Firefox</summary>

1. Open Firefox and sign in to https://enlighten.enphaseenergy.com/.
2. Open DevTools with `Cmd+Opt+I` (macOS) or `Ctrl+Shift+I` → **Network**.
3. Refresh the page. Filter for your 'site-ID` (e.g. `5527819`).
   - One will contain the `Authorization` and the other will contain the `Cookie`
4. Under **Headers → Request Headers**, copy the values for:
   - `Authorization` = `e-auth-token`
   - `Cookie` = `cookie` (copy the entire cookie string)
5. Cookies are also viewable under **Storage → Cookies → enphaseenergy.com**.

</details>

<details>
  <summary>How to capture e-auth-token and Cookie in Safari</summary>

1. Enable the Develop menu: Safari → Settings → **Advanced** → check **Show features for web developers** / **Show Develop menu**.
2. Sign in to https://enlighten.enphaseenergy.com/.
3. Open Web Inspector: Develop → **Show Web Inspector** (or `Cmd+Opt+I`) → **Network**.
4. Refresh the page. Filter for your 'site-ID` (e.g. `5527819`).
   - One will contain the `Authorization` and the other will contain the `Cookie`
5. Under **Headers → Request Headers**, copy the values for:
   - `Authorization` = `e-auth-token`
   - `Cookie` = `cookie` (copy the entire cookie string)
6. You can also view cookies under the **Storage** tab for the domain.

</details>

## Entities & Services

Site diagnostics
- Sensors: Last Successful Update (timestamp), Cloud Latency (ms)
- Binary: Cloud Reachable (on/off)

Per‑charger entities
- Switch: Charging (on/off)
- Buttons: Start Charging, Stop Charging
- Select: Charge Mode (Manual, Scheduled, Green) — uses the scheduler preference
- Number: Charging Amps (setpoint only; does not start charging)
- Sensors:
  - Power (W) — maps multiple keys and estimates from amps×voltage when missing
  - Session Energy (kWh) — normalized if the API reports Wh
  - Session Duration (min) — increases only while charging; freezes after stop
  - Set Amps (A) — current setpoint (falls back to last set amps if unknown)
  - Min/Max Amp (A)
  - Charge Mode — reflects scheduler preference
  - Phase Mode — 1→Single Phase, 3→Three Phase
  - Status — cloud summary status
  - Connector Status — AVAILABLE/CHARGING/etc. (diagnostic)

Removed (unreliable across deployments): Connector Reason, Schedule Type/Start/End, Session Miles, Session Plug‑in/out timestamps.

**Services (Actions)**
- Action: `enphase_ev.start_charging`
  - Description: Start charging on the selected charger.
  - Fields:
    - `device_id` (required)
    - `charging_level` (optional, A; 6–40)
    - `connector_id` (optional; usually 1)
- Action: `enphase_ev.stop_charging`
  - Description: Stop charging on the selected charger.
  - Fields:
    - `device_id` (required)
- Action: `enphase_ev.trigger_message`
  - Description: Request the charger to send an OCPP message.
  - Fields:
    - `device_id` (required)
    - `requested_message` (required; e.g., `MeterValues`)
- Action: `enphase_ev.clear_reauth_issue`
  - Description: Clear the integration’s reauthentication issue notification.
  - Fields:
    - `site_id` (optional)
- Action: `enphase_ev.start_live_stream`
  - Description: Request faster cloud status updates for a short period.
- Action: `enphase_ev.stop_live_stream`
  - Description: Stop the cloud live stream request.

## Privacy & Rate Limits

- Credentials are stored in HA’s config entries and redacted from diagnostics.
- The integration polls `/status` every 30 seconds by default (configurable).  
- Avoids login; uses your provided session headers.

## Future Local Path

When Enphase exposes owner-scope EV endpoints locally, we can add a local client and prefer it automatically. For now, local `/ivp/pdm/*` and `/ivp/peb/*` returned 401 in discovery.

---

### Troubleshooting

- **401 Unauthorized**: Refresh `e-auth-token` and `Cookie` headers from an active session.  
- **No entities**: Check that your serial is present in `/status` response (`evChargerData`), and matches the configured serial.  
- **Rate limiting**: Increase `scan_interval` to 30s or more.

### Development

- Python 3.13 recommended. Create and activate: `python3.13 -m venv .venv && source .venv/bin/activate`
- Install dev deps: `pip install -U pytest pytest-asyncio pytest-homeassistant-custom-component homeassistant ruff black`
- Lint: `ruff check .`
- Format: `black custom_components/enphase_ev`
- Run tests: `pytest -q`

### Options

- Polling intervals: Configure slow (idle) and fast (charging) intervals. The integration auto‑switches and also uses a short fast window after Start/Stop to reflect changes faster.
- API timeout: Default 15s (Options → API timeout).
- Nominal voltage: Default 240 V; used to estimate power from amps when the API omits power.
 - Fast while streaming: On by default; prefers faster polling while an explicit cloud live stream is active.

### System Health & Diagnostics

- System Health (Settings → System → Repairs → System Health):
  - Site ID: your configured site identifier
  - Can reach server: live reachability to Enlighten cloud
  - Last successful update: timestamp of most recent poll
  - Cloud latency: round‑trip time for the last status request
- Diagnostics: Downloaded JSON excludes sensitive headers (`e-auth-token`, `Cookie`) and other secrets.

### Energy Dashboard

- Use the `Lifetime Energy` sensor for the Energy Dashboard.
  - Go to Settings → Dashboards → Energy → Add consumption.
  - Select `sensor.<charger>_lifetime_energy` (device class energy, state_class total_increasing).
- This tracks the charger’s lifetime kWh reported by Enlighten.

### Behavior notes

- Charging Amps (number) stores your desired setpoint but does not start charging. The Start button, Charging switch, or start service will use that stored setpoint (default 32 A).
- Start/Stop actions treat benign 4xx responses (e.g., unplugged/not active) as no‑ops to avoid errors in HA.
- The Charge Mode select works with the scheduler API and reflects the service’s active mode.

### Reconfigure

- You can reconfigure the integration (update site ID, serials, or session headers) without removing it.
- Go to Settings → Devices & Services → Integrations → Enphase EV Charger 2 (Cloud) → Reconfigure.
- Paste refreshed `e-auth-token` and `Cookie` headers; optionally paste a cURL to auto‑fill.

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
