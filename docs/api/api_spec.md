# Enphase EV Cloud API Specification

_This reference consolidates everything the integration has learned from reverse‑engineering the Enlighten mobile/web APIs for the IQ EV Charger 2._

---

## 1. Overview
- **Base URL:** `https://enlighten.enphaseenergy.com`
- **Auth:** All EV endpoints require the Enlighten `e-auth-token` header and the authenticated session `Cookie` header. Most control endpoints also accept Enlighten bearer tokens when provided; the integration automatically attaches `Authorization: Bearer <token>` when available.
- **Privacy:** Example identifiers, timestamps, and credentials in this document are anonymized placeholders.
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

Recent cloud responses wrap the data in `meta`/`data` objects:
```json
{
  "meta": { "serverTimeStamp": 1761456789123 },
  "data": {
    "site": "1234567",
    "tz": "Australia/Melbourne",
    "chargers": [
      {
        "smartEV": { "hasToken": false, "hasEVDetails": false },
        "evManufacturerName": "Example OEM",
        "offGrid": "ON_GRID",
        "sn": "EV9876543210",
        "name": "IQ EV Charger",
        "lst_rpt_at": "2025-10-25T01:12:05Z[UTC]",
        "offlineAt": "2025-10-23T03:00:29.082Z[UTC]",
        "connected": true,
        "auth_token": null,
        "mode": 0,
        "charging": true,
        "pluggedIn": true,
        "faulted": false,
        "commissioned": 1,
        "isEVDetailsSet": true,
        "sch_d": { "status": 0, "info": [] },
        "session_d": {
          "plg_in_at": "2025-10-24T23:57:05.145Z[UTC]",
          "strt_chrg": 1761456500000,
          "plg_out_at": null,
          "e_c": 3542.11,
          "miles": 14.35,
          "session_cost": null,
          "auth_status": -1,
          "auth_type": null,
          "auth_id": null,
          "charge_level": 32
        },
        "connectors": [
          {
            "connectorId": 1,
            "connectorStatusType": "CHARGING",
            "connectorStatusInfo": "EVConnected",
            "connectorStatusReason": "",
            "dlbActive": false,
            "pluggedIn": true
          }
        ]
      }
    ]
  },
  "error": {}
}
```
Legacy responses may still return the flatter `evChargerData` shape. The integration maps the nested structure above into the historic structure internally so downstream consumers always receive an `evChargerData` array with `sn`, `name`, `connected`, `pluggedIn`, `charging`, `faulted`, `connectorStatusType`, and a simplified `session_d` containing `e_c` and `start_time` (derived from `session_d.strt_chrg`).
Note: the `connectors[]` payload includes `dlbActive` (dynamic load balancing active) plus status info fields; preserve `connectors` or at least `dlbActive` when normalizing so DLB state is not lost.

### 2.2 Extended Summary (Metadata)
```
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/summary?filter_retired=true
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/<sn>/summary
```
Provides hardware/software versions, model names, operating voltage, IP addresses, and schedule information.
The list endpoint returns a `data` array; the per-charger endpoint returns a single `data` object and includes `supportsUseBattery`
to indicate whether the green-mode "Use Battery" toggle is supported.

```json
{
  "data": [
    {
      "serialNumber": "EV1234567890",
      "displayName": "Sample Charger",
      "modelName": "IQ-EVSE-SAMPLE",
      "supportsUseBattery": true,
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

Example per-charger response (anonymized):
```json
{
  "meta": {
    "serverTimeStamp": 1760000000000
  },
  "data": {
    "lastReportedAt": "2025-01-25T09:09:01.943Z[UTC]",
    "supportsUseBattery": true,
    "chargeLevelDetails": {
      "min": "6",
      "max": "32",
      "granularity": "1",
      "defaultChargeLevel": "disabled"
    },
    "displayName": "IQ EV Charger",
    "timezone": "Australia/Example",
    "warrantyDueDate": "2030-01-01T00:00:00.000000000Z[UTC]",
    "isConnected": true,
    "wifiConfig": "connectionStatus=1, wifiMode=client, SSID=ExampleSSID, status=connected",
    "hoControl": true,
    "processorBoardVersion": "2.0.713.0",
    "activeConnection": "wifi",
    "operatingVoltage": "230",
    "defaultRoute": "interface=mlan0, ip_address=192.0.2.1",
    "wiringConfiguration": {
      "L1": "L1"
    },
    "dlbEnabled": 1,
    "systemVersion": "25.37.1.14",
    "createdAt": "2025-01-01T00:00:00.000000000Z[UTC]",
    "maxCurrent": 32,
    "warrantyStartDate": "2025-01-01T00:00:00.000000000Z[UTC]",
    "warrantyPeriod": 5,
    "bootloaderVersion": "2024.04",
    "gridType": 2,
    "hoControlScope": [],
    "sku": "IQ-EVSE-EXAMPLE-0000",
    "firmwareVersion": "25.37.1.14",
    "cellularConfig": "signalStrength=0, status=disconnected, network=, info=",
    "applicationVersion": "25.37.1.5",
    "reportingInterval": 300,
    "serialNumber": "EV000000000000",
    "commissioningStatus": 1,
    "phaseMode": 1,
    "gatewayConnectivityDetails": [
      {
        "gwSerialNum": "GW0000000000",
        "gwConnStatus": 0,
        "gwConnFailureReason": 0,
        "lastConnTime": 1760000000000
      }
    ],
    "rmaDetails": null,
    "networkConfig": "[\n\"netmask=255.255.255.0,mac_addr=00:11:22:33:44:55,interface_name=eth0,connectionStatus=0,ipaddr=192.0.2.10,bootproto=dhcp,gateway=192.0.2.1\",\n\"netmask=255.255.255.0,mac_addr=00:11:22:33:44:66,interface_name=mlan0,connectionStatus=1,ipaddr=192.0.2.11,bootproto=dhcp,gateway=192.0.2.1\"\n]",
    "breakerRating": 32,
    "modelName": "IQ-EVSE-EXAMPLE",
    "ratedCurrent": "32",
    "isLocallyConnected": true,
    "kernelVersion": "6.6.23-lts-next-gb2f1b3288874",
    "siteId": 1234567,
    "powerBoardVersion": "25.28.9.0",
    "partNumber": "865-02030 09",
    "isRetired": false,
    "functionalValDetails": {
      "lastUpdatedTimestamp": 1700000000000,
      "state": 1
    },
    "status": "NORMAL",
    "phaseCount": 1
  },
  "error": {}
}
```

### 2.3 Start Live Stream
```
GET /service/evse_controller/<site_id>/ev_chargers/start_live_stream
```
Initiates a short burst of rapid status updates.
```json
{ "status": "accepted", "topics": ["evse/<sn>/status"], "duration_s": 900 }
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

### 2.6 Lifetime Energy (time‑series buckets)
```
GET /pv/systems/<site_id>/lifetime_energy
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: BP-XSRF-Token=<token>; XSRF-TOKEN=<token>; ...   # normal Enlighten session cookies
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns aggregated Wh buckets for production/consumption and related flows. Cloud responses present arrays of equal length representing historical intervals (15 min or daily depending on site configuration).

Example shape (values truncated/obfuscated):
```json
{
  "system_id": 1234567,
  "start_date": "2023-08-10",
  "last_report_date": 1765442709,
  "update_pending": false,
  "production": [12000, 8300, 9000, 26000, ...],
  "consumption": [7100, 13400, 15800, 14100, ...],
  "solar_home": [2700, 3300, 5400, 6000, ...],
  "solar_grid": [8300, 4400, 2600, 18600, ...],
  "grid_home": [4200, 9800, 10700, 7700, ...],
  "import": [null, null, ...],
  "export": [null, null, ...],
  "charge": [null, null, ...],
  "discharge": [null, null, ...],
  "solar_battery": [null, null, ...],
  "battery_home": [null, null, ...],
  "battery_grid": [null, null, ...],
  "grid_battery": [null, null, ...],
  "evse": [0, 0, ...],
  "heatpump": [],
  "water_heater": []
}
```
Notes:
- `start_date` marks the earliest bucket; `last_report_date` is an epoch seconds cursor.
- Arrays are long; empty arrays imply the site lacks that flow type (for example `heatpump`).
- When present, `evse` values report charging energy attributed to the EVSE.

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

> **Official API parity:** Enphase’s published EV Charger Control API (v4) exposes the same behaviour at `POST /api/v4/systems/{system_id}/ev_charger/{serial_no}/start_charging`, returning HTTP 202 with `{"message": "Request sent successfully"}`. The partner spec also documents the validation messages we have observed in practice (for example: invalid `system_id`/`serial_no`, `connectorId` must be greater than zero, and the requested charging level must stay within 0‑100). While our integration continues to target the Enlighten UI endpoints above, these public details confirm the backend error semantics.

### 3.2 Stop Charging
```
PUT /service/evse_controller/<site_id>/ev_chargers/<sn>/stop_charging
```
Fallbacks: `POST`, singular path `/ev_charger/`.
```json
{ "status": "accepted" }
```

The v4 control API mirrors this stop request and reports success with the same HTTP 202 / `{"message": "Request sent successfully"}` envelope, reinforcing that a 202 response from the cloud simply means the command has been queued.

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

### 4.3 Green Charging Settings (Battery Support)
```
GET /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site_id>/<sn>/settings
Headers: Authorization: Bearer <token>
```
Response:
```json
{
  "meta": {
    "serverTimeStamp": "2025-01-01T00:00:00.000+00:00",
    "rowCount": 1
  },
  "data": [
    {
      "chargerSettingName": "USE_BATTERY_FOR_SELF_CONSUMPTION",
      "enabled": true,
      "value": null
    }
  ],
  "error": {}
}
```

```
PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site_id>/<sn>/settings
Headers: Authorization: Bearer <token>
Body: {
  "chargerSettingList": [
    {
      "chargerSettingName": "USE_BATTERY_FOR_SELF_CONSUMPTION",
      "enabled": true,
      "value": null,
      "loader": false
    }
  ]
}
```
Response:
```json
{
  "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00" },
  "data": {
    "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00" },
    "data": null,
    "error": {}
  },
  "error": {}
}
```
Notes:
- `USE_BATTERY_FOR_SELF_CONSUMPTION` backs the UI toggle "Use battery for EV charging" shown in Green mode.
- Setting `enabled=false` disables battery supplementation; `value` remains `null`.
- The web UI sends `loader=false`; the API accepts payloads without the `loader` key.

### 4.4 List Schedules
```
GET /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site_id>/<sn>/schedules
Headers: Authorization: Bearer <token>
```
Response:
```json
{
  "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00" },
  "data": {
    "config": {
      "isOffPeakEligible": true,
      "scheduleSyncStatus": "synced",
      "isModeCancellable": true,
      "pendingModesOffGrid": false,
      "pendingSchedulesOffGrid": false
    },
    "slots": [
      {
        "id": "<site_id>:<sn>:<uuid>",
        "startTime": "23:00",
        "endTime": "06:00",
        "chargingLevel": 32,
        "chargingLevelAmp": 32,
        "scheduleType": "CUSTOM",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "remindTime": 10,
        "remindFlag": false,
        "enabled": true,
        "recurringKind": "Recurring",
        "chargeLevelType": "Weekly",
        "sourceType": "SYSTEM",
        "reminderTimeUtc": null,
        "serializedDays": null
      },
      {
        "id": "<site_id>:<sn>:<uuid>",
        "startTime": null,
        "endTime": null,
        "chargingLevel": null,
        "chargingLevelAmp": null,
        "scheduleType": "OFF_PEAK",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "remindTime": 10,
        "remindFlag": false,
        "enabled": false,
        "recurringKind": null,
        "chargeLevelType": null,
        "sourceType": "SYSTEM",
        "reminderTimeUtc": null,
        "serializedDays": null
      }
    ]
  },
  "error": {}
}
```
Notes:
- `scheduleType=OFF_PEAK` typically has null `startTime`/`endTime`.
- `days` uses 1=Monday through 7=Sunday.
- `remindFlag` toggles reminders and `remindTime` is minutes before `startTime`.
- Observed: `recurringKind` and `chargeLevelType` may be `null` even for `CUSTOM` slots.
- Observed: `chargingLevel`/`chargingLevelAmp` can be populated for `OFF_PEAK` schedules even when `startTime`/`endTime` are null.
- Observed: `remindTime` may be present even when `remindFlag` is `false`.
- Observed: `reminderTimeUtc` is `HH:MM` when `remindFlag=true`, otherwise null.
- Observed: editing a schedule time in Enlighten auto-enables the slot and populates `reminderTimeUtc`.

### 4.5 Update Schedules
```
PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site_id>/<sn>/schedules
Headers: Authorization: Bearer <token>
Body: {
  "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00", "rowCount": 2 },
  "data": [ <slot>, <slot> ]
}
```
Notes:
- Send the full list of slots; omitted slots may be deleted server-side.
- Preserve unchanged fields like `sourceType`, `recurringKind`, `chargeLevelType`.
- Observed: frontend PATCH requests may include `chargingLevel=100` and `chargingLevelAmp=null` for `CUSTOM` schedules; subsequent GETs may normalize back to `32/32`.
- Observed: frontend PATCH requests include a top-level `"error": {}` field; the API accepts PATCH payloads without it.
- Integration behavior: PATCH payloads are normalized to known slot fields only, ids are coerced to strings, booleans/ints are coerced, and `OFF_PEAK` days default to `[1..7]` if missing.
- Integration behavior: when a schedule helper change updates time blocks, the integration auto-enables the slot to mirror Enlighten's edit behavior.

---

## 5. Authentication Flow

### 5.1 Login (Enlighten Web)
```
POST https://enlighten.enphaseenergy.com/login/login.json
```
This endpoint authenticates credentials and either completes login immediately or initiates an MFA challenge. MFA status is inferred from the response shape and cookie changes (there is no explicit flag).

MFA required response (credentials accepted, OTP pending):
```json
{
  "success": true,
  "isBlocked": false
}
```
Indicators:
- `session_id` and `manager_token` are absent from the JSON.
- `Set-Cookie` refreshes `login_otp_nonce` (short expiry).
- `_enlighten_4_session` is not replaced with an authenticated session yet.

MFA not required response (fully authenticated):
```json
{
  "message": "success",
  "session_id": "<session_id>",
  "manager_token": "<jwt>",
  "is_consumer": true,
  "system_id": "<system_id>",
  "redirect_url": ""
}
```
Indicators:
- `session_id` and `manager_token` are present.
- `Set-Cookie` issues a new authenticated `_enlighten_4_session`.

Any other response shape (e.g., `success: false` or `isBlocked: true`) should be treated as invalid credentials or a changed API contract.

### 5.2 MFA OTP Validation
```
POST https://enlighten.enphaseenergy.com/app-api/validate_login_otp
Content-Type: application/x-www-form-urlencoded
```
Requires the pre-MFA session cookies from the login step (`_enlighten_4_session`, `login_otp_nonce`, XSRF cookies, `email`). Body parameters are base64-encoded:

```
email=<base64_email>
otp=<base64_otp>
xhrFields[withCredentials]=true
```

Success response (authenticated):
```json
{
  "message": "success",
  "session_id": "<session_id>",
  "manager_token": "<jwt>",
  "is_consumer": true,
  "system_id": "<system_id>",
  "redirect_url": "",
  "isValidMobileNumber": true
}
```
Indicators:
- `Set-Cookie` replaces `_enlighten_4_session` with the authenticated session.
- `session_id` and `manager_token` are now available for API access.

Invalid OTP response:
```json
{
  "isValid": false,
  "isBlocked": false
}
```

Blocked (defensive case):
```json
{
  "isValid": false,
  "isBlocked": true
}
```

### 5.3 MFA OTP Resend
```
POST https://enlighten.enphaseenergy.com/app-api/generate_mfa_login_otp
Content-Type: application/x-www-form-urlencoded
```
Body:
```
locale=en
```

Success response (OTP queued):
```json
{
  "success": true,
  "isBlocked": false
}
```
The server rotates `login_otp_nonce` via `Set-Cookie` but does not return `session_id` or `manager_token`.

### 5.4 Access Token
Some sites issue a JWT-like access token via `https://entrez.enphaseenergy.com/access_token`. The integration decodes the `exp` claim to know when to refresh.

### 5.5 Headers Required by API Client
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
| `connectorStatusType` | ENUM: `AVAILABLE`, `CHARGING`, `FINISHING`, `SUSPENDED`, `SUSPENDED_EV`, `SUSPENDED_EVSE`, `FAULTED` |
| `connectorStatusReason` | Additional enum reason (e.g., `INSUFFICIENT_SOLAR`) |
| `session_d.e_c` | Session energy (Wh if >200, else kWh) |
| `session_d.start_time` | Epoch seconds when session started |
| `chargeLevelDetails.min/max` | Min/max allowed amps |
| `maxCurrent` | Hardware max amp rating |
| `operatingVoltage` | Nominal voltage per summary v2 |
| `dlbEnabled` | Dynamic Load Balancing flag |
| `supportsUseBattery` | Summary v2 flag for green-mode "Use Battery" support |
| `networkConfig` | Interfaces with IP/fallback metadata |
| `firmwareVersion` | Charger firmware |
| `processorBoardVersion` | Hardware version |

Additional metrics documented in the official `/api/v4/.../telemetry` endpoint align with the time-series payloads we have observed (for example `consumption` arrays of Wh values paired with `end_at` epoch timestamps for each 15‑minute bucket). Treat those fields as alternate labels for the same energy-per-interval data returned by the Enlighten UI endpoints.

---

## 7. Error Handling & Rate Limiting
- HTTP 401 — credentials expired; request reauth.
- HTTP 400/404/409/422 during control operations — charger not ready/not plugged; treated as no-ops.
- Rate limiting presents as HTTP 429; the integration backs off and logs the event.
- Recommended polling interval: 30 s (configurable). Live stream can be used for short bursts (15 min)

### 7.1 Cloud status codes (from the official v4 control API)
Enphase’s public “EV Charger Control” reference (https://developer-v4.enphase.com/docs.html) documents the same backend actions behind a `/api/v4/systems/{system_id}/ev_charger/{serial_no}/…` surface. Although we do not call that REST layer directly, the status codes it lists match the JSON payloads we have seen bubble out of the Enlighten UI endpoints. The most relevant responses are:

| HTTP | Status / message | Meaning |
| --- | --- | --- |
| 400 | `Bad request` (`INVALID_SYSTEM_ID`, `Connector Id must be greater than 0`, `Charging level should be in the range [0-100]`) | Input validation failures for site, serial, connector, or requested amperage. |
| 401 | `Not Authorized` | Missing or expired authentication (bearer token or cookie). |
| 403 | `Forbidden` | Authenticated user lacks access to the target site. |
| 405 | `Method not allowed` | Endpoint does not accept the verb being sent (e.g. POST vs PUT). |
| 466 | `UNSUPPORTED_ENVOY` | Envoy must be online and running firmware ≥ 6.0.0 before live actions are accepted. |
| 468 | `INVALID_SYSTEM_ID` | Site ID does not exist or is not mapped to the authenticated account. |
| 472 | `LIVE_STREAM_NOT_SUPPORTED` | Site hardware mix cannot participate in the live polling burst. |
| 473 | `IQ_GATEWAY_NOT_REPORTING` | Backend cannot reach the site’s gateway, so commands and live data are rejected. |
| 550/551 | `SERVICE_UNREACHABLE` | Generic transient fault on the cloud side; retry later. |
| 552 | `CONNECTION_NOT_ESTABLISHED` | Command was queued but the service could not connect downstream to the charger. |

When these conditions occur against the `/service/evse_controller/...` paths, we receive an analogous JSON envelope (often with `"status": "error"` and the same `message`/`details`). Treat 4xx codes as actionable validation problems and 5xx codes as retryable faults.

---

## 8. Known Variations & Open Questions
- Some deployments omit `displayName` from `/status`; summary v2 is needed for friendly names.
- Session energy units vary; integration normalizes values >200 as Wh ➜ kWh.
- Local LAN endpoints (`/ivp/pdm/*`, `/ivp/peb/*`) exist but require installer permissions; not currently accessible with owner accounts.

---

## 9. References
- Reverse-engineered from Enlighten mobile app network traces (2024–2025).
- Implemented in `custom_components/enphase_ev/api.py` and `coordinator.py`.
