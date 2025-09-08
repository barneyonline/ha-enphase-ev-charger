# Enphase EV Charger 2 (Cloud) — Home Assistant Custom Integration

**Version:** Beta (unstable)

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

**Site diagnostics**
- Sensors: Last Successful Update (timestamp), Cloud Latency (ms)
- Binary: Cloud Reachable (on/off)

**Entities (per charger)**
- Binary sensors: Plugged-In, Charging, Faulted
- Sensors: Power (W), Session Energy (kWh), Connector Status, Charging Level (A), Session Duration (min)
- Number: Charging Amps setpoint (UI control)
- Buttons: Start Charging, Stop Charging
- Select: Charge Mode (Manual, Scheduled, Green)

### Entities Overview

Controls (user actions)

| Entity | Type | Description | Example |
|---|---|---|---|
| Start Charging | Button | Sends a cloud request to begin charging. | — |
| Stop Charging | Button | Sends a cloud request to stop charging. | — |
| Charge Mode | Select | Sets the charger mode via scheduler API (MANUAL_CHARGING, SCHEDULED_CHARGING, GREEN_CHARGING). | GREEN_CHARGING |

Core sensors (read-only state)

| Entity | Key | Description | Example |
|---|---|---|---|
| Power | `power_w` | Instantaneous charger power (W). Unknown reported as 0. | 0 W |
| Charging Level | `charging_level` | Current setpoint from the cloud; falls back to last command if absent. | 32 A |
| Session Energy | `session_kwh` | Energy delivered in current session (kWh), state_class=total. | 47.17 kWh |
| Session Duration | derived | Minutes since session start when charging. | 85 min |
| Last Reported At | `last_reported_at` | Cloud timestamp of last charger report (displayed in local time). | 2025‑09‑08 12:55:30 |
| Session Miles | `session_miles` | Distance-equivalent for current session (if provided). | 0.22 mi |
| Session Plug‑in At | `session_plug_in_at` | Timestamp when the vehicle was plugged in. | 2025‑09‑07 17:21:18 |
| Session Plug‑out At | `session_plug_out_at` | Timestamp when the vehicle was unplugged. | 2025‑09‑07 20:48:53 |
| Schedule Type | `schedule_type` | Current schedule type (or schedule status fallback). | greencharging |
| Schedule Start | `schedule_start` | Start time of the active schedule window. | 2025‑09‑08 17:00:00 |
| Schedule End | `schedule_end` | End time of the active schedule window. | 2025‑09‑08 21:00:00 |
| Charge Mode (sensor) | `charge_mode` | Current mode reported/derived from scheduler/status. | MANUAL_CHARGING |
| Charging Amps | `max_current` | Read-only max current capability from summary v2. | 32 A |
| Min Amp | `min_amp` | Minimum allowable current from summary v2 `chargeLevelDetails.min`. | 6 A |
| Max Amp | `max_amp` | Maximum allowable current from summary v2 `chargeLevelDetails.max`. | 32 A |
| Phase Mode | `phase_mode` | Reported phase mode (from summary v2). | 1 |
| Status | `status` | Reported charger status string (from summary v2). | NORMAL |

Binary sensors

| Entity | Key | Description | Example |
|---|---|---|---|
| Plugged In | `plugged` | Vehicle connector is plugged into the charger. | Off |
| Charging | `charging` | Charger is actively charging. | Off |
| Faulted | `faulted` | Charger reports a fault (diagnostic). | Off |
| Connected | `connected` | Charger is connected to cloud (diagnostic). | On |
| DLB Active | `dlb_active` | Dynamic load balancing is active (diagnostic). | Off |
| Commissioned | `commissioned` | Commissioning status (On when 1, Off when 0) (diagnostic). | On |
| Cloud Reachable | site | Indicates recent successful cloud communication (diagnostic). | On |

Connector details

| Entity | Key | Description | Example |
|---|---|---|---|
| Connector Status | `connector_status` | AVAILABLE/CHARGING/FINISHING/SUSPENDED (diagnostic). | AVAILABLE |
| Connector Reason | `connector_reason` | Reason string from the cloud (e.g., INSUFFICIENT_SOLAR) (diagnostic). | INSUFFICIENT_SOLAR |

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

- Polling intervals: Configure a slow poll (idle) and a fast poll (active charging). The integration automatically switches based on charger state.
- Fast while streaming: Off by default. Cloud live stream is time‑limited (~15 min) and should be used sparingly. Enable only if you explicitly start the live stream service and want faster updates during that window.

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

