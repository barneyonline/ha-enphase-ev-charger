# Integration Design

## Domain
`enphase_ev`

## Config
Two modes:

### Option A: UI Flow
- Inputs:
  - `site_id` (string)
  - `serials` (list of charger serials)
  - `e_auth_token` (string, required)
  - `cookie` (string, required)
  - `scan_interval` (seconds, default 15)
- Validation: perform a quick `/status` probe

### Option B: YAML (for power users)
```yaml
enphase_ev:
  site_id: "3381244"
  serials: ["482522020944"]
  e_auth_token: "!secret enphase_eauth"
  cookie: "!secret enphase_cookie"
  scan_interval: 15
```

## Files (custom_components/enphase_ev/)
- `__init__.py` — set up domain, start coordinator(s)
- `manifest.json` — requirements, codeowners
- `config_flow.py` — UI config
- `api.py` — HTTP client for cloud endpoints
- `coordinator.py` — DataUpdateCoordinator
- `const.py` — constants (domain, defaults)
- `sensor.py` — sensors (power, energy, amps, session duration)
- `binary_sensor.py` — charging, plugged, faulted
- `number.py` — charging current setpoint
- `button.py` — start/stop charging buttons
- `services.yaml` — start_charging/stop_charging/trigger_message
- `diagnostics.py` — redact headers
- `translations/` — basic en.json
- `tests/` — pytest fixtures for API responses

## Update Model
- Poll `/status` every `scan_interval` (15s default).
- Extract the object for each serial (`sn`).
- Compute derived values: session_duration, connector_status mapping.
- For controls, call start/stop endpoints and refresh.

## Error Handling
- On 401: set `reauth_required=True` and surface a repair flow.
- On 429/5xx: backoff (exponential), keep entities available with last_state.
- Network errors: log once; set `available=False` until next success.

## Device Info
- Identifiers: `("enphase_ev", "<sn>")`
- Manufacturer: `Enphase`
- Model: `IQ EV Charger 2`
- Name from payload (`name`) or `"Enphase EV <sn_suffix>"`
