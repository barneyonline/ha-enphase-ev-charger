# Cloud API Spec (derived from mobile app captures)

> Base: `https://enlighten.enphaseenergy.com`

All endpoints require:
- Header: `e-auth-token: <EAUTH>`
- Header: `Cookie: <SESSION COOKIES>` (complete cookie string from authenticated web/mobile session)

**Path Variables**
- `<site_id>`: numeric site identifier (e.g., `3381244`)
- `<sn>`: charger serial number (e.g., `482522020944`)
- `connectorId`: usually `1`
- **Site discovery:** `GET /service/evse_controller/sites` (fallbacks: `/api/v1/sites`, `/sites.json`) lists available sites for the authenticated account with `site_id` and optional `name`.

## Endpoints

### 1) EVSE Status (read)
`GET /service/evse_controller/<site_id>/ev_chargers/status`

**200 Response (representative keys):**
```json
{
  "evChargerData": [{
    "sn": "482522020944",
    "name": "Garage EV",
    "connected": true,
    "pluggedIn": true,
    "charging": false,
    "faulted": false,
    "connectorStatusType": "AVAILABLE|CHARGING|FINISHING|SUSPENDED",
    "session_d": { "e_c": 3.52, "cost": 0.84, "sch_kwh": 0.0, "start_time": 1725600000 },
    "sch_d": { "enabled": false }
  }],
  "ts": 1725600123
}
```

### 2) Start Live Stream
`GET /service/evse_controller/<site_id>/ev_chargers/start_live_stream`

**200 Response:**
```json
{ "status": "accepted", "topics": ["evse/<sn>/status"], "duration_s": 60 }
```

### 3) Stop Live Stream
`GET /service/evse_controller/<site_id>/ev_chargers/stop_live_stream`

**200 Response:**
```json
{ "status": "accepted" }
```

### 4) Start Charging (control)
`POST /service/evse_controller/<site_id>/ev_chargers/<sn>/start_charging`
```json
{ "chargingLevel": 32, "connectorId": 1 }
```

**200 Response:**
```json
{ "status": "accepted", "chargingLevel": 32 }
```

### 5) Stop Charging (control)
`PUT /service/evse_controller/<site_id>/ev_chargers/<sn>/stop_charging`

**200 Response:**
```json
{ "status": "accepted" }
```

### 6) Trigger OCPP Message (advanced)
`POST /service/evse_controller/<site_id>/ev_charger/<sn>/trigger_message`
```json
{ "requestedMessage": "MeterValues" }
```
**200 Response:**
```json
{ "status": "accepted" }
```

## Auth
- Use `e-auth-token` and `Cookie`. These are retrieved from a logged-in session.
- The integration should store them in HA’s **secrets** storage and never log their values.
- Provide a button to re‑paste/refresh tokens if 401 occurs.

## Notes
- Rate limit friendly: default **15s** polling on `/status`.
- Live stream endpoints are optional; not required for MVP.
- Treat times as Unix epoch seconds; energy units may be kWh in `session_d` (`e_c`).
