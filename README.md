# Enphase EV Charger 2 (Cloud) — Home Assistant Custom Integration

**Version:** 0.1.0 • **Updated:** 2025-09-07T09:56:20Z

This custom integration surfaces the **Enphase IQ EV Charger 2** in Home Assistant using the same **Enlighten cloud** endpoints used by the Enphase mobile app.

> ⚠️ Local-only access to EV endpoints is **role-gated** on IQ Gateway firmware 7.6.175. The charger surfaces locally under `/ivp/pdm/*` or `/ivp/peb/*` only with **installer** scope. This integration therefore uses the **cloud API** until owner-scope local endpoints are available.

## Installation

1. Copy the `custom_components/enphase_ev/` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings → Devices & Services → + Add Integration → Enphase EV Charger 2 (Cloud)**.

Alternatively, YAML (advanced):
```yaml
enphase_ev:
  site_id: "9990001"
  serials: ["499900000001"]
  e_auth_token: "!secret enphase_eauth"
  cookie: "!secret enphase_cookie"
  scan_interval: 15
```

## Required Inputs

- **Site ID**: Numeric site identifier (e.g., `9990001`).  
- **Serials**: One or more charger serial numbers (e.g., `499900000001`).  
- **e-auth-token** header value**: From a logged-in Enlighten session.  
- **Cookie** header value**: The full cookie string from the same session.  

> Paste the exact values captured from your browser/app session. If you receive a 401 later, re-open the options and paste refreshed headers.

## Entities & Services

**Site diagnostics**
- Sensors: Last Successful Update (timestamp), Cloud Latency (ms)
- Binary: Cloud Reachable (on/off)

**Entities (per charger)**
- Binary sensors: Plugged-In, Charging, Faulted
- Sensors: Power (W), Session Energy (kWh), Connector Status, Charging Level (A), Session Duration (min)
- Number: Charging Amps setpoint (UI control)
- Buttons: Start Charging, Stop Charging

**Services**
- `enphase_ev.start_charging` — fields: `device_id`, optional `charging_level` (A), optional `connector_id` (default 1)
- `enphase_ev.stop_charging` — fields: `device_id`
- `enphase_ev.trigger_message` — fields: `device_id`, `requested_message`
- `enphase_ev.clear_reauth_issue` — optional `site_id`; manually clears the reauth repair issue

## Privacy & Rate Limits

- Credentials are stored in HA’s config entries and redacted from diagnostics.
- The integration polls `/status` every 15 seconds by default (configurable).  
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
- Install dev deps: `pip install -U pytest pytest-asyncio pytest-homeassistant-custom-component homeassistant`
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
