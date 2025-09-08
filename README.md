# Enphase EV Charger 2 (Cloud) — Home Assistant Custom Integration

**Version:** 0.6.0 (beta)

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
- **e-auth-token header**: From a logged-in Enlighten session.  
- **Cookie header**: The full cookie string from the same session.  

> Paste the exact values captured from your browser/app session. If you receive a 401 later, re-open the options and paste refreshed headers.

<details>
  <summary>How to capture e-auth-token and Cookie in Chrome</summary>

1. Open Chrome and sign in to https://enlighten.enphaseenergy.com/.
2. Press `Cmd+Opt+I` (macOS) or `Ctrl+Shift+I` (Windows/Linux) to open DevTools.
3. Go to the **Network** tab and enable **Preserve log**.
4. Refresh the page. Filter for `status` or `ivp` (or requests to `enphaseenergy.com`).
5. Click any API request (e.g., a call that returns site/charger status).
6. Under **Headers → Request Headers**, copy the values for:
   - `e-auth-token`
   - `cookie` (copy the entire cookie string)
7. Optionally, you can find the cookie under **Application → Storage → Cookies → enphaseenergy.com**.

</details>

<details>
  <summary>How to capture e-auth-token and Cookie in Firefox</summary>

1. Open Firefox and sign in to https://enlighten.enphaseenergy.com/.
2. Open DevTools with `Cmd+Opt+I` (macOS) or `Ctrl+Shift+I` → **Network**.
3. Refresh the page. Use the filter for `status` or `ivp`.
4. Click an API request and look under **Headers → Request Headers**.
5. Copy the values for:
   - `e-auth-token`
   - `cookie` (entire string)
6. Cookies are also viewable under **Storage → Cookies → enphaseenergy.com**.

</details>

<details>
  <summary>How to capture e-auth-token and Cookie in Safari</summary>

1. Enable the Develop menu: Safari → Settings → **Advanced** → check **Show features for web developers** / **Show Develop menu**.
2. Sign in to https://enlighten.enphaseenergy.com/.
3. Open Web Inspector: Develop → **Show Web Inspector** (or `Cmd+Opt+I`) → **Network**.
4. Refresh the page and select an API request (look for calls returning site/charger status).
5. Under the request **Headers**, copy the values for:
   - `e-auth-token`
   - `cookie` (entire string)
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

**Services**
- `enphase_ev.start_charging` — fields: `device_id`, optional `charging_level` (A), optional `connector_id` (default 1)
- `enphase_ev.stop_charging` — fields: `device_id`
- `enphase_ev.trigger_message` — fields: `device_id`, `requested_message`
- `enphase_ev.clear_reauth_issue` — optional `site_id`; manually clears the reauth repair issue

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
- Fast while streaming: Off by default; prefer enabling only when explicitly starting cloud live stream.

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

### Breaking changes (since early betas)

- “Charging Amps” sensor is now displayed as “Set Amps”.
- Removed unreliable sensors: Connector Reason, Schedule (type/start/end), Session Miles, Session Plug‑in/out.
