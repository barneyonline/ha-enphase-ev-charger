# Enphase EV Cloud API Specification

_This reference consolidates everything the integration has learned from reverse‑engineering the Enlighten mobile/web APIs for the IQ EV Charger 2._

---

## 1. Overview
- **Base URL:** `https://enlighten.enphaseenergy.com`
- **Auth:** All EV endpoints require the Enlighten `e-auth-token` header and the authenticated session `Cookie` header. Most control endpoints also accept Enlighten bearer tokens when provided; the integration automatically attaches `Authorization: Bearer <token>` when available.
- **Path Variables:**
  - `<site_id>` — numeric site identifier
  - `<sn>` — charger serial number
  - `connectorId` — connector index; currently always `1`
- **Discovery:** `GET /service/evse_controller/sites` (fallbacks: `/api/v1/sites`, `/sites.json`) enumerates the account's accessible sites, returning `site_id` and optional `name` fields that the config flow can surface without manual entry.

---

## 2. Core EV Controller Endpoints

### 2.1 Status Snapshot
```
GET /service/evse_controller/<site_id>/ev_chargers/status
```
Returns charger state (plugged, charging, session energy, etc.). Some deployments still respond to `/ev_charger/status`; the integration falls back automatically.

Example payload:
```json
{
  "evChargerData": [
    {
      "sn": "EV1234567890",
      "name": "Sample Charger",
      "connected": true,
      "pluggedIn": true,
      "charging": false,
      "faulted": false,
      "connectorStatusType": "AVAILABLE",
      "connectorStatusReason": "INSUFFICIENT_SOLAR",
      "chargingLevel": 32,
      "session_d": {
        "e_c": 3.52,
        "start_time": 1700000000,
        "plg_in_at": 1699999900,
        "plg_out_at": null
      },
      "sch_d": {
        "enabled": false,
        "mode": "IMMEDIATE"
      }
    }
  ],
  "ts": 1700000123
}
```

### 2.2 Extended Summary (Metadata)
```
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/summary?filter_retired=true
```
Provides hardware/software versions, model names, operating voltage, IP addresses, and schedule information.

```json
{
  "data": [
    {
      "serialNumber": "EV1234567890",
      "displayName": "Sample Charger",
      "modelName": "IQ-EVSE-SAMPLE",
      "maxCurrent": 32,
      "chargeLevelDetails": { "min": "6", "max": "32", "granularity": "1" },
      "dlbEnabled": 1,
      "networkConfig": "[...]",          // JSON or CSV-like string of interfaces
      "lastReportedAt": "2025-01-15T12:34:56.000Z[UTC]",
      "operatingVoltage": 240,
      "firmwareVersion": "25.XX.Y.Z",
      "processorBoardVersion": "A.B.C"
    }
  ]
}
```

### 2.3 Start Live Stream
```
GET /service/evse_controller/<site_id>/ev_chargers/start_live_stream
```
Initiates a short burst of rapid status updates.
```json
{ "status": "accepted", "topics": ["evse/<sn>/status"], "duration_s": 60 }
```

### 2.4 Stop Live Stream
```
GET /service/evse_controller/<site_id>/ev_chargers/stop_live_stream
```
Ends the fast polling window.
```json
{ "status": "accepted" }
```

### 2.5 Session History
```
POST /service/enho_historical_events_ms/<site_id>/sessions/<sn>/history
Body: {
  "startDate": "16-10-2025",
  "endDate": "16-10-2025",
  "offset": 0,
  "limit": 20
}
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns a list of recent charging sessions for the requested charger. `startDate`/`endDate` are `DD-MM-YYYY` in the site's local timezone. The response indicates whether more pages are available via `hasMore`.

Example response:
```json
{
  "source": "evse",
  "timestamp": "2025-10-16T08:45:14.230924038Z",
  "data": {
    "result": [
      {
        "id": "123456789012:1700000001",
        "sessionId": 1700000001,
        "startTime": "2025-10-16T00:02:08.826Z[UTC]",
        "endTime": "2025-10-16T04:39:50.618Z[UTC]",
        "authType": null,
        "authIdentifier": null,
        "authToken": null,
        "aggEnergyValue": 29.94,
        "activeChargeTime": 15284,
        "milesAdded": 120.7,
        "sessionCost": 0.77,
        "costCalculated": true,
        "manualOverridden": true,
        "avgCostPerUnitEnergy": 0.03,
        "sessionCostState": "COST_CALCULATED",
        "chargeProfileStackLevel": 4
      }
    ],
    "hasMore": true,
    "startDate": "10-08-2022",
    "endDate": "16-10-2025",
    "offset": 0,
    "limit": 20
  }
}
```
Fields of interest:
- `aggEnergyValue` — energy delivered in kWh for the session.
- `activeChargeTime` — session duration in seconds while actively charging.
- `milesAdded` — range added in miles (region-specific; may be `null`).
- `sessionCost`/`avgCostPerUnitEnergy` — cost metadata when tariffs are configured.
- `authType`/`authIdentifier`/`authToken` — authentication metadata recorded by Enlighten (often `null` for residential accounts).
- `sessionCostState` — cost calculation status such as `COST_CALCULATED`.

---

## 3. Control Operations

The Enlighten backend is inconsistent across regions; the integration tries multiple variants until one succeeds. All payloads shown below are the canonical request. If a 409/422 response is returned (charger unplugged/not ready), the integration treats it as a benign no-op.

### 3.1 Start Charging / Set Amps
```
POST /service/evse_controller/<site_id>/ev_chargers/<sn>/start_charging
Body: { "chargingLevel": 32, "connectorId": 1 }
```
Fallback variants observed:
- `PUT` instead of `POST`
- Path `/ev_charger/` (singular)
- Payload keys `charging_level` / `connector_id`
- No body (uses last stored level)

Typical response:
```json
{ "status": "accepted", "chargingLevel": 32 }
```

### 3.2 Stop Charging
```
PUT /service/evse_controller/<site_id>/ev_chargers/<sn>/stop_charging
```
Fallbacks: `POST`, singular path `/ev_charger/`.
```json
{ "status": "accepted" }
```

### 3.3 Trigger OCPP Message
```
POST /service/evse_controller/<site_id>/ev_charger/<sn>/trigger_message
Body: { "requestedMessage": "MeterValues" }
```
Replies vary by backend. Common shape:
```json
{
  "status": "accepted",
  "message": "MeterValues",
  "details": {
    "initiatedAt": "2025-01-15T12:34:56.000Z",
    "trackingId": "TICKET-XYZ123"
  }
}
```

---

## 4. Scheduler (Charge Mode) API

Separate Enlighten service requiring bearer tokens in addition to the cookie headers.

### 4.1 Read Preferred Charge Mode
```
GET /service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference
Headers: Authorization: Bearer <token>
```
Response:
```json
{
  "data": {
    "modes": {
      "manualCharging": { "enabled": true, "chargingMode": "MANUAL_CHARGING" },
      "scheduledCharging": { "enabled": false },
      "greenCharging": { "enabled": false }
    }
  }
}
```

### 4.2 Set Charge Mode
```
PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference
Body: { "mode": "MANUAL_CHARGING" }
Headers: Authorization: Bearer <token>
```
Success response mirrors the GET payload.

---

## 5. Authentication Flow

### 5.1 Login (Enlighten OAuth)
1. POST to `https://entrez.enphaseenergy.com/login` with email/password.
2. Handle MFA challenge (`/login/mfa/verify`) when required.
3. Retrieve cookies and session ID from the response.

### 5.2 Access Token
Some sites issue a JWT-like access token via `https://entrez.enphaseenergy.com/access_token`. The integration decodes the `exp` claim to know when to refresh.

### 5.3 Headers Required by API Client
- `e-auth-token: <token>`
- `Cookie: <serialized cookie jar>` (must include session cookies like `_enlighten_session`, `X-Requested-With`, etc.)
- When available: `Authorization: Bearer <token>`
- Common defaults also send:
  - `Referer: https://enlighten.enphaseenergy.com/`
  - `X-Requested-With: XMLHttpRequest`

The integration reuses tokens until expiry or a 401 is encountered, then prompts reauthentication.

---

## 6. Response Field Reference

| Field | Description |
| --- | --- |
| `connected` | Charger cloud connection status |
| `pluggedIn` | Vehicle plugged state |
| `charging` | Active charging session |
| `faulted` | Fault present |
| `connectorStatusType` | ENUM: `AVAILABLE`, `CHARGING`, `FINISHING`, `SUSPENDED`, `FAULTED` |
| `connectorStatusReason` | Additional enum reason (e.g., `INSUFFICIENT_SOLAR`) |
| `session_d.e_c` | Session energy (Wh if >200, else kWh) |
| `session_d.start_time` | Epoch seconds when session started |
| `chargeLevelDetails.min/max` | Min/max allowed amps |
| `maxCurrent` | Hardware max amp rating |
| `operatingVoltage` | Nominal voltage per summary v2 |
| `dlbEnabled` | Dynamic Load Balancing flag |
| `networkConfig` | Interfaces with IP/fallback metadata |
| `firmwareVersion` | Charger firmware |
| `processorBoardVersion` | Hardware version |

---

## 7. Error Handling & Rate Limiting
- HTTP 401 — credentials expired; request reauth.
- HTTP 400/404/409/422 during control operations — charger not ready/not plugged; treated as no-ops.
- Rate limiting presents as HTTP 429; the integration backs off and logs the event.
- Recommended polling interval: 30 s (configurable). Live stream can be used for short bursts (60 s)

---

## 8. Known Variations & Open Questions
- Some deployments omit `displayName` from `/status`; summary v2 is needed for friendly names.
- Session energy units vary; integration normalizes values >200 as Wh ➜ kWh.
- Local LAN endpoints (`/ivp/pdm/*`, `/ivp/peb/*`) exist but require installer permissions; not currently accessible with owner accounts.

---

## 9. References
- Reverse-engineered from Enlighten mobile app network traces (2024–2025).
- Implemented in `custom_components/enphase_ev/api.py` and `coordinator.py`.
