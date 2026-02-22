# Enphase Energy Cloud API Specification

_This reference consolidates everything the integration has learned from reverse-engineering the Enlighten mobile/web APIs across EV charging, site energy, gateway, battery, and microinverter features._

---

## 1. Overview
- **Base URL:** `https://enlighten.enphaseenergy.com`
- **Auth:** Most endpoints require the Enlighten `e-auth-token` header and the authenticated session `Cookie` header. Some services (notably scheduler and selected control APIs) also require bearer tokens; the integration attaches `Authorization: Bearer <token>` when available.
- **Privacy:** Example identifiers, timestamps, and credentials in this document are anonymized placeholders.
- **Path Variables:**
  - `<site_id>` - numeric site identifier
  - `<sn>` - charger serial number
  - `connectorId` - connector index; currently always `1`
- **Discovery:** `GET /app-api/search_sites.json?searchText=&favourite=false` enumerates the account's accessible sites, returning IDs and display titles for the config flow.

---

### 1.1 Site Discovery (Search API)
```
GET /app-api/search_sites.json?searchText=&favourite=false
```
Returns the sites tied to the authenticated account. The integration extracts `id` as the site identifier and uses `title` as the friendly display name when available.
`searchText` filters results by name/id, while `favourite=false` returns all sites instead of just starred entries.

Example response (anonymized):
```json
{
  "sites": [
    {
      "id": 1234567,
      "path": "/web/1234567?v=3.4.0",
      "title": "Main Site",
      "favourite": false
    }
  ]
}
```

---

### 1.2 Recommended Endpoint Order (System-First)

For integration work and troubleshooting, process endpoints in this order:

1. Authenticate and establish session headers (`6.1`-`6.5`).
2. Discover sites (`1.1`).
3. Load site capabilities and inventory (`2.9`, `2.13`-`2.16`, `5.2`).
4. Load runtime telemetry (`2.1`, `2.2`, `2.7`, `2.8`, `2.10`, `2.11`, `2.14`-`2.16`).
5. Apply site-level controls (`2.12.1`-`2.12.5`, `5.4`-`5.6`).
6. Apply EV charger controls and scheduling (`3.1`-`3.3`, `4.1`-`4.5`).
7. Validate failures, retries, and cloud backoff behavior (`8`, `9`).

### 1.3 Endpoint Families (Quick Layout)

- **Auth and discovery:** `1.1`, `6.1`-`6.5`
- **Site/system inventory and telemetry:** `2.9`-`2.16`
- **EV charger telemetry and metadata:** `2.1`-`2.8`
- **EV charger controls and scheduling:** `3.1`-`3.3`, `4.1`-`4.5`
- **BatteryConfig controls:** `5.1`-`5.6`
- **Cross-cutting references:** `7`, `8`, `9`

### 1.4 Table of Contents

- `1. Overview`
- `2. Core Site and Device Endpoints`
- `3. EV Charger Control Operations`
- `4. EV Scheduler (Charge Mode) API`
- `5. BatteryConfig APIs (System Profile and Battery Controls)`
- `6. Authentication Flow (Shared Across Services)`
- `7. Response Field Reference`
- `8. Error Handling and Rate Limiting`
- `9. Known Variations and Open Questions`
- `10. References`

### 1.5 Endpoint Matrix (High-Level)

| Domain | Method | Endpoint | Auth | Used by integration |
| --- | --- | --- | --- | --- |
| Site discovery | `GET` | `/app-api/search_sites.json` | login session cookies | Yes |
| EV runtime status | `GET` | `/service/evse_controller/<site_id>/ev_chargers/status` | `e-auth-token` + cookies | Yes |
| EV metadata summary | `GET` | `/service/evse_controller/api/v2/<site_id>/ev_chargers/summary` | `e-auth-token` + cookies | Yes |
| Site inventory | `GET` | `/app-api/<site_id>/devices.json` | `e-auth-token` + cookies | Yes |
| Site lifetime energy | `GET` | `/pv/systems/<site_id>/lifetime_energy` | `e-auth-token` + cookies | Yes |
| Homeowner events | `GET` | `/service/events-platform-service/v1.0/<site_id>/events/homeowner` | `e-auth-token` + cookies | Yes |
| Battery backup history | `GET` | `/app-api/<site_id>/battery_backup_history.json` | `e-auth-token` + cookies | Yes |
| Grid eligibility | `GET` | `/app-api/<site_id>/grid_control_check.json` | `e-auth-token` + cookies | Yes |
| Microinverter inventory | `GET` | `/app-api/<site_id>/inverters.json` | `e-auth-token` + cookies | Yes |
| Battery status | `GET` | `/pv/settings/<site_id>/battery_status.json` | `e-auth-token` + cookies | Yes |
| Start charging | `POST` | `/service/evse_controller/<site_id>/ev_chargers/<sn>/start_charging` | `e-auth-token` + cookies | Yes |
| Stop charging | `PUT` | `/service/evse_controller/<site_id>/ev_chargers/<sn>/stop_charging` | `e-auth-token` + cookies | Yes |
| Charge mode preference | `GET/PUT` | `/service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference` | bearer token + session headers | Yes |
| BatteryConfig site settings | `GET` | `/service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` | `e-auth-token` + cookies + `Username` | Yes |
| Login | `POST` | `/login/login.json` | credentials + CSRF/session cookies | Yes |

---

## 2. Core Site and Device Endpoints

This section groups both EV charger endpoints and non-EV site/system endpoints exposed by Enlighten service APIs.

### 2.A EV Charger Telemetry and Metadata

### 2.1 Status Snapshot
```
GET /service/evse_controller/<site_id>/ev_chargers/status
```
Returns charger state (plugged, charging, session energy, etc.).

Recent cloud responses wrap the data in `meta`/`data` objects:
```json
{
  "meta": { "serverTimeStamp": 1761456789123 },
  "data": {
    "site": "1234567",
    "tz": "Region/City",
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
            "safeLimitState": 1,
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
Note: the `connectors[]` payload includes `dlbActive` (dynamic load balancing active), `safeLimitState`, and status info fields; preserve `connectors` or at least `dlbActive`/`safeLimitState` when normalizing so DLB safe-mode state is not lost.

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
    "timezone": "Region/City",
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

### 2.5 Session Authentication Settings (App + RFID)
```
POST /service/evse_controller/api/v1/<site_id>/ev_chargers/<sn>/ev_charger_config
Body: [
  { "key": "rfidSessionAuthentication" },
  { "key": "sessionAuthentication" }
]
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Fetches the current authentication requirements for charging sessions.

Example response:
```json
{
  "meta": { "serverTimeStamp": 1760000000000, "rowCount": 2 },
  "data": [
    {
      "key": "rfidSessionAuthentication",
      "value": "disabled",
      "reqValue": "disabled",
      "status": 1
    },
    {
      "key": "sessionAuthentication",
      "value": null,
      "reqValue": null,
      "status": 1
    }
  ],
  "error": {}
}
```

Enable or disable app authentication:
```
PUT /service/evse_controller/api/v1/<site_id>/ev_chargers/<sn>/ev_charger_config
Body: [ { "key": "sessionAuthentication", "value": "enabled" } ]
```

Example response (enable request):
```json
{
  "meta": { "serverTimeStamp": 1760000000000, "rowCount": 1 },
  "data": [
    {
      "key": "sessionAuthentication",
      "value": "disabled",
      "reqValue": "enabled",
      "status": 2
    }
  ],
  "error": {}
}
```

Disable request payload:
```json
[
  { "key": "sessionAuthentication", "value": "disabled" }
]
```

Notes:
- `sessionAuthentication` controls "Auth via App"; `rfidSessionAuthentication` controls RFID auth.
- When either setting is enabled, charging sessions require user authentication before starting.
- Observed: read responses use `status=1`; update responses use `status=2`, with `value` reflecting the prior state and `reqValue` the desired state.
- Observed: `sessionAuthentication` can return `null` when disabled or unset.

### 2.6 Session History (Filter Criteria)
```
GET /service/enho_historical_events_ms/<site_id>/filter_criteria?source=evse&requestId=<uuid>&username=<user_id>
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <session_id>
  requestid: <uuid>
  username: <user_id>
  X-Requested-With: XMLHttpRequest
```
Returns the chargers available for session history lookups (IDs + display names).
Notes:
- `Authorization` uses the Auth MS JWT (from `/tokens` or the `enlighten_manager_token_production` cookie).
- `e-auth-token` should match the JWT `session_id` claim; `username` should match the JWT `user_id` claim.
- `requestid` is a UUID generated per request.

### 2.7 Session History
```
POST /service/enho_historical_events_ms/<site_id>/sessions/<sn>/history
Body: {
  "source": "evse",
  "params": {
    "offset": 0,
    "limit": 20,
    "startDate": "16-10-2025",
    "endDate": "16-10-2025",
    "timezone": "Region/City"
  }
}
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <session_id>
  requestid: <uuid>
  username: <user_id>
  X-Requested-With: XMLHttpRequest
```
Returns a list of recent charging sessions for the requested charger. `startDate`/`endDate` are `DD-MM-YYYY` in the site's local timezone. The response indicates whether more pages are available via `hasMore`.
Notes:
- `Authorization` uses the Auth MS JWT (from `/tokens` or the `enlighten_manager_token_production` cookie).
- `e-auth-token` should match the JWT `session_id` claim; `username` should match the JWT `user_id` claim.
- `requestid` is a UUID generated per request.

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

### 2.8 Lifetime Energy (time-series buckets)
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

### 2.B Site-Level Energy, Inventory, and Events

### 2.9 Device Inventory (Site Hardware Cards)
```
GET /app-api/<site_id>/devices.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns grouped device inventory used by the Enlighten "Devices" views (Gateway, batteries, system controller, relays, meters, EV charger, etc.).

Example response shape (anonymized):
```json
{
  "result": [
    {
      "type": "envoy",
      "devices": [
        {
          "name": "IQ Gateway",
          "serial_number": "GW0000000000",
          "sku_id": "SC100G-M000ROW",
          "connected": true,
          "status": "normal",
          "statusText": "Normal",
          "ip": "192.0.2.10",
          "envoy_sw_version": "D8.X.XXXX",
          "last_report": 1770000000,
          "show_connection_details": true,
          "warranty_end_date": "2030-09-18"
        }
      ]
    },
    {
      "type": "encharge",
      "devices": [
        {
          "name": "IQ Battery 5P",
          "serial_number": "BT0000000001",
          "sku_id": "B05-T02-ROW00-1-2",
          "channel_type": "IQ Battery",
          "status": "normal",
          "last_report": 1770000010,
          "sw_version": "522-00002-01-vX.Y.Z_rel/31.44",
          "warranty_end_date": "2040-09-18"
        }
      ]
    },
    {
      "type": "enpower",
      "devices": [
        {
          "name": "IQ System Controller 3",
          "serial_number": "SC0000000000",
          "sku_id": "SC100G-M000ROW",
          "channel_type": "IQ System Controller",
          "status": "normal",
          "last_report": 1770000020,
          "sw_version": "522-00003-01-vX.Y.Z_rel/31.44",
          "warranty_end_date": "2035-09-18"
        }
      ]
    },
    { "curr_date_site": "2026-02-08" }
  ]
}
```
Observed structure:
- `result[]` is a mixed array containing typed buckets (`{type, devices}`) and metadata objects (for example `curr_date_site`).
- Each bucket's `type` drives the frontend section and card template; `devices[]` may be empty.
- Common device fields: `name`, `serial_number`, `sku_id`, `status`, `statusText`, `last_report`.
- Optional fields vary by type (`ip`, `connected`, `envoy_sw_version`, `channel_type`, `sw_version`, `warranty_end_date`, etc.).

### 2.10 Homeowner Events History
```
GET /service/events-platform-service/v1.0/<site_id>/events/homeowner?next=<cursor>&page_size=<n>&locale=<locale>
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns event feed rows shown in "Events History" with cursor pagination.

Example response shape (anonymized):
```json
{
  "events": [
    {
      "id": 123456789,
      "status": "Info",
      "type": "IQ EV Charger",
      "description": "Charging started on IQ EV Charger (SNo. EV0000000000).",
      "standing": false,
      "event_start_date": 1770000100,
      "event_clear_date": 1770000100,
      "devices_impacted": ["IQ EV Charger (SNo. EV0000000000)"],
      "serial_num": "EV0000000000",
      "recommended_action": "No action is required.",
      "emu_event_id": -1,
      "event_key": "evse_start_charging",
      "event_date": 1770000100,
      "event_type_id": 795,
      "message_params": "Charge Level = --, Mode = Charge Now"
    }
  ],
  "site_timezone": "Region/City",
  "next": "1769999999:EV0000000000:795:-1",
  "page_size": 30,
  "csv_link": "https://enlighten.example/service/events-platform-service/v1.0/<site_id>/events/homeowner?export=true&next=start&page_size=5000"
}
```
Observed structure:
- `events[]` includes Info and Closed rows, with optional `cta`, `message_params`, and `description_key`.
- Date fields (`event_start_date`, `event_clear_date`, `event_date`) are epoch seconds and render in site-local timezone.
- Pagination uses the opaque `next` token returned by each page.
- `description`, `devices_impacted`, and `serial_num` embed serial identifiers; redact these before logging/sharing traces.

### 2.11 Battery Backup History
```
GET /app-api/<site_id>/battery_backup_history.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns backup outage history consumed by the "Backup History" table.

Example response (anonymized):
```json
{
  "total_records": 4,
  "total_backup": 307,
  "histories": [
    {
      "start_time": "2025-10-17T14:38:30+11:00",
      "duration": 121
    },
    {
      "start_time": "2025-10-16T18:30:09+11:00",
      "duration": 74
    }
  ]
}
```
Observed structure:
- `total_records` is the count of backup events.
- `total_backup` is cumulative backup duration in seconds.
- `histories[]` entries contain ISO8601 `start_time` and `duration` (seconds); UI derives `end_time = start_time + duration`.

### 2.C Site Grid Control

### 2.12 Grid Control Check (Eligibility / Guardrails)
```
GET /app-api/<site_id>/grid_control_check.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns booleans used to enable/disable off-grid control actions in the Settings UI.

Example response (anonymized):
```json
{
  "disableGridControl": false,
  "activeDownload": false,
  "sunlightBackupSystemCheck": false,
  "gridOutageCheck": false,
  "userInitiatedGridToggle": false
}
```
Observed structure:
- `disableGridControl=true` indicates the UI should prevent a grid-mode toggle.
- `activeDownload`/`sunlightBackupSystemCheck`/`gridOutageCheck` are guard conditions surfaced by the backend.
- `userInitiatedGridToggle` indicates whether a toggle workflow is already in progress.
- This endpoint does **not** provide the current steady-state grid mode (`On Grid`/`Off Grid`); it only reports whether a mode change is currently allowed or blocked.

### 2.12.1 Grid Toggle OTP (Send / Resend)
```
GET /app-api/<site_id>/grid_toggle_otp.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Triggers delivery of the 4-digit OTP used to authorize a manual grid toggle.

Example response (anonymized):
```json
{
  "success": "email sent successfully"
}
```
Observed behavior:
- Called after the user confirms either `Go Off Grid` or `Go On Grid`.
- Also called again when the user taps `Resend` in the OTP modal.

### 2.12.2 Grid Toggle OTP Verification
```
POST /app-api/grid_toggle_otp.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Content-Type: application/x-www-form-urlencoded; charset=UTF-8
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  Origin: https://enlighten.enphaseenergy.com
  X-Requested-With: XMLHttpRequest
Body (form):
  otp=<4_digit_code>
  site_id=<site_id>
```
Validates the OTP before the grid relay command is accepted.

Example response (anonymized):
```json
{
  "valid": true
}
```
Observed behavior:
- `valid=true` is required before `/pv/settings/grid_state.json` is invoked.

### 2.12.3 Grid State Change Command
```
POST /pv/settings/grid_state.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Content-Type: application/x-www-form-urlencoded; charset=UTF-8
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  Origin: https://enlighten.enphaseenergy.com
  X-Requested-With: XMLHttpRequest
Body (form):
  envoy_serial_number=<envoy_serial>
  state=<state_code>
```
Queues the actual grid relay transition after OTP validation.

State mapping observed in captures:
- `state=1` requests `Go Off Grid` (UI shows `Disconnecting from Grid...`).
- `state=2` requests `Go On Grid` (UI shows `Connecting to Grid...`).

Example response (anonymized):
```json
{
  "request_id": "req_xxxxxxxxxxxxxxxxxxxxxxxx",
  "context_ids": [
    1700000000000000
  ]
}
```

### 2.12.4 Grid Change Audit Log
```
POST /pv/settings/log_grid_change.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Content-Type: application/x-www-form-urlencoded; charset=UTF-8
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  Origin: https://enlighten.enphaseenergy.com
  X-Requested-With: XMLHttpRequest
Body (form):
  envoy_serial_number=<envoy_serial>
  old_state=<relay_state_before>
  new_state=<relay_state_after>
```
Records relay-state transitions after the grid toggle request.

Example response (anonymized):
```json
{
  "status": "Grid Change Logged",
  "old_state": "OPER_RELAY_CLOSED",
  "new_state": "OPER_RELAY_OFFGRID_AC_GRID_PRESENT"
}
```
Observed state pairs:
- Off-grid transition: `OPER_RELAY_CLOSED` -> `OPER_RELAY_OFFGRID_AC_GRID_PRESENT`
- On-grid transition: `OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD` -> `OPER_RELAY_CLOSED`

### 2.12.5 Off-Grid Status Context
```
GET /app-api/<site_id>/off_grid_due_to_grid_outage
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns grid-outage and regional contact metadata used by the Grid Control card when deciding whether reconnect options should be shown.

Example response (anonymized):
```json
{
  "continent_code": "XX",
  "country_code": "YY",
  "isd_code": "+00",
  "phone": "<support_phone>",
  "is_sunlight_backup": false,
  "is_grid_outage": false,
  "show_grid_connect": true,
  "has_battery": true
}
```

### 2.12.6 Grid Toggle UI-to-API Sequence
Both directions use a confirmation + OTP gate before the relay command is sent.

Off-grid (`System is On Grid` -> `System is Off Grid`):
1. User taps `Go Off Grid` toggle and confirms warning dialog.
2. Client calls `GET /app-api/<site_id>/grid_toggle_otp.json`.
3. User enters OTP; client calls `POST /app-api/grid_toggle_otp.json`.
4. If `{"valid": true}`, client calls `POST /pv/settings/grid_state.json` with `state=1`.
5. UI shows `Disconnecting from Grid...` until backend state settles.
6. Client logs transition via `POST /pv/settings/log_grid_change.json`.

On-grid (`System is Off Grid` -> `System is On Grid`):
1. User taps `Go On Grid` toggle and confirms reconnect dialog.
2. Client calls `GET /app-api/<site_id>/grid_toggle_otp.json`.
3. User enters OTP; client calls `POST /app-api/grid_toggle_otp.json`.
4. If `{"valid": true}`, client calls `POST /pv/settings/grid_state.json` with `state=2`.
5. UI shows `Connecting to Grid...` until backend state settles.
6. Client may query `GET /app-api/<site_id>/off_grid_due_to_grid_outage` and logs transition via `POST /pv/settings/log_grid_change.json`.

### 2.D Microinverter APIs

### 2.13 Microinverter Inventory (Legacy Site View)
```
GET /app-api/<site_id>/inverters.json?limit=<n>&offset=<n>&search=<query>
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns the microinverter list previously used by the Enlighten site "Microinverters" UI cards.

Example response shape (anonymized):
```json
{
  "total": 16,
  "not_reporting": 0,
  "inverters": [
    {
      "name": "IQ7A",
      "array_name": "North",
      "serial_number": "12XXXXXXXXXX",
      "sku_id": "IQ7A-72-E-ACM-INT",
      "status": "normal",
      "statusText": "Normal",
      "part_num": "800-01714-r02",
      "sku": 3733,
      "last_report": 1770623834,
      "fw1": "520-00082-r01-v04.30.32",
      "fw2": "540-00169-r01-v04.30.12",
      "warranty_end_date": "2032-08-10"
    },
    {
      "name": "IQ7A",
      "array_name": "West",
      "serial_number": "12XXXXXXXXYY",
      "sku_id": "IQ7A-72-E-ACM-INT",
      "status": "normal",
      "statusText": "Normal",
      "part_num": "800-01714-r02",
      "sku": 3733,
      "last_report": 1770624076,
      "fw1": "520-00082-r01-v04.30.32",
      "fw2": "540-00169-r01-v04.30.12",
      "warranty_end_date": "2032-08-10"
    }
  ],
  "error_count": 0,
  "warning_count": 0,
  "normal_count": 16,
  "panel_info": {
    "pv_module_manufacturer": null,
    "model_name": null,
    "stc_rating": null
  }
}
```
Observed structure:
- Pagination uses `limit` and `offset`; `search` filters by serial/model text and can be blank.
- `total` is the full match count, independent of current page size.
- `inverters[]` card fields include model (`name`), array grouping (`array_name`), serial, firmware (`fw1`/`fw2`), and warranty date.
- Status rollups are provided as counters (`error_count`, `warning_count`, `normal_count`) plus `not_reporting`.
- `last_report` is epoch seconds and maps to the "Last reported" timestamp shown in the UI.

### 2.14 Inverter Production by Date Range
```
GET /systems/<site_id>/inverter_data_x/energy.json?start_date=<YYYY-MM-DD>&end_date=<YYYY-MM-DD>
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns per-inverter production totals for the requested date window. The response is keyed by inverter ID and used by the layout energy views.

Example response (single-day range, anonymized):
```json
{
  "production": {
    "67XXXXXXXX13": 556,
    "67XXXXXXXX14": 536,
    "67XXXXXXXX15": 543,
    "67XXXXXXXX16": 545
  },
  "start_date": "2026-02-09",
  "end_date": "2026-02-09"
}
```

Example response (lifetime range, anonymized):
```json
{
  "production": {
    "67XXXXXXXX13": 1884125,
    "67XXXXXXXX14": 1848279,
    "67XXXXXXXX15": 2092602,
    "67XXXXXXXX16": 2104793
  },
  "start_date": "2022-08-10",
  "end_date": "2026-02-09"
}
```
Observed structure:
- `start_date` and `end_date` are inclusive date bounds in `YYYY-MM-DD`.
- `production` is a dictionary of `<inverter_id> -> <energy_value>` for the selected window.
- Inverter keys are stable numeric IDs represented as strings in JSON.
- Values are energy totals in Wh for the requested period (single-day windows produce small daily totals; long windows return cumulative totals).

### 2.15 Inverter Status Map (ID to Serial/Device Mapping)
```
GET /systems/<site_id>/inverter_status_x.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns a device status map keyed by inverter ID. This endpoint provides the join between `inverter_data_x.production` keys and physical inverter serial numbers.

Example response shape (anonymized):
```json
{
  "67XXXXXXXX13": {
    "serialNum": "12XXXXXXXX62",
    "statusCode": "normal",
    "status": "Normal",
    "deviceId": 62XXXX42,
    "issi": { "sig_str": 0, "level": 0 },
    "rssi": { "sig_str": 0, "level": 0 },
    "emu_version": "8.3.5232",
    "show_sig_str": false,
    "type": "IQ7A"
  },
  "67XXXXXXXX14": {
    "serialNum": "12XXXXXXXX44",
    "statusCode": "normal",
    "status": "Normal",
    "deviceId": 62XXXX43,
    "issi": { "sig_str": 0, "level": 0 },
    "rssi": { "sig_str": 0, "level": 0 },
    "emu_version": "8.3.5232",
    "show_sig_str": false,
    "type": "IQ7A"
  }
}
```
Observed structure:
- Top-level object keys are inverter IDs and align with keys in `inverter_data_x.production`.
- Each entry includes `serialNum` and `deviceId`, allowing deterministic joins to `/app-api/<site_id>/inverters.json`.
- Payload may include non-microinverter device types on mixed systems (for example battery PCU entries); filter by serial/type when building microinverter entities.

### 2.E Site Battery Runtime Status

### 2.16 Battery Status (Site Battery Card)
```
GET /pv/settings/<site_id>/battery_status.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...   # authenticated Enlighten web session cookies
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns the battery card payload used in Enlighten web/app for site-level and per-battery SoC, power, and status details.

Example response (anonymized):
```json
{
  "current_charge": "48%",
  "available_energy": 4.75,
  "max_capacity": 10,
  "available_power": 7.68,
  "max_power": 7.68,
  "total_micros": 12,
  "show_battery_banner": false,
  "active_micros": 12,
  "inactive_micros": 0,
  "inactive_micros_sn": [],
  "included_count": 2,
  "excluded_count": 0,
  "storages": [
    {
      "id": 100000001,
      "serial_number": "BT0000000001",
      "current_charge": "48%",
      "available_energy": 2.40,
      "max_capacity": 5,
      "led_status": 17,
      "excluded": false,
      "error_code": null,
      "error_text": null,
      "available_power": 3.84,
      "max_power": 3.84,
      "total_micros": 6,
      "active_micros": 6,
      "inactive_micros": 0,
      "inactive_micros_sn": [],
      "event_start_date": null,
      "event_description": null,
      "event_recommendation": null,
      "statusText": "Normal",
      "status": "normal",
      "last_report": 1770000001,
      "cycle_count": 115,
      "battery_mode": "Self-Consumption",
      "rated_power": 3840,
      "battery_phase_count": 1,
      "is_flex_phase": false,
      "battery_soh": "100%"
    },
    {
      "id": 100000002,
      "serial_number": "BT0000000002",
      "current_charge": "47%",
      "available_energy": 2.35,
      "max_capacity": 5,
      "led_status": 17,
      "excluded": false,
      "error_code": null,
      "error_text": null,
      "available_power": 3.84,
      "max_power": 3.84,
      "total_micros": 6,
      "active_micros": 6,
      "inactive_micros": 0,
      "inactive_micros_sn": [],
      "event_start_date": null,
      "event_description": null,
      "event_recommendation": null,
      "statusText": "Normal",
      "status": "normal",
      "last_report": 1770000011,
      "cycle_count": 115,
      "battery_mode": "Self-Consumption",
      "rated_power": 3840,
      "battery_phase_count": 1,
      "is_flex_phase": false,
      "battery_soh": "100%"
    }
  ]
}
```
Observed structure:
- Top-level metrics summarize combined battery behavior (`current_charge`, energy/power totals, microinverter counts).
- `storages[]` contains one object per battery with SoC, power, status, reporting timestamp, and event/error metadata.
- `excluded=true` marks batteries excluded from active fleet calculations in the UI; included/excluded counters are exposed at the top level.
- Percentage fields (`current_charge`, `battery_soh`) are string percentages in observed payloads.
- Status appears as normalized code (`status`, for example `normal`) plus a display label (`statusText`, for example `Normal`).

---

## 3. EV Charger Control Operations

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

## 4. EV Scheduler (Charge Mode) API

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

## 5. BatteryConfig APIs (System Profile and Battery Controls)

The Enlighten battery profile web UI (`https://battery-profile-ui.enphaseenergy.com/`) loads system profile and EV charging mode cards (Storm Guard, Self-Consumption, Savings, Full Backup) via the BatteryConfig service.

Observed shared requirements:
- `e-auth-token` header plus the authenticated `Cookie` jar.
- `Username: <user_id>` header matching the Enlighten user ID.
- Browser-style `Origin`/`Referer` set to the battery profile UI host.

### 5.1 MQTT Signed URL / Authorizer Bootstrap
```
GET /service/batteryConfig/api/v1/mqttSignedUrl/<site_id>
```
Returns an AWS IoT custom authorizer payload used to open a short-lived MQTT stream for live updates.

Example response (anonymized):
```json
{
  "topic": "v1/server/response-stream/<stream_id>",
  "stream_duration": 900,
  "aws_iot_endpoint": "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com",
  "aws_authorizer": "aws-lambda-authoriser-prod",
  "aws_token_key": "enph_token",
  "aws_token_value": "<session_id>",
  "aws_digest": "<base64_signature>"
}
```

### 5.2 Site Settings
```
GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>
```
Provides feature flags and UI gating for the battery profile experience.

Example response (anonymized):
```json
{
  "type": "site-settings",
  "timestamp": "<timestamp>",
  "data": {
    "showProduction": true,
    "showConsumption": true,
    "hasEncharge": true,
    "hasEnpower": true,
    "countryCode": "XX",
    "region": "XX",
    "locale": "en-XX",
    "timezone": "Region/City",
    "showChargeFromGrid": true,
    "showSavingsMode": true,
    "showStormGuard": true,
    "showFullBackup": true,
    "showBatteryBackupPercentage": true,
    "isChargingModesEnabled": true,
    "batteryGridMode": "ImportExport",
    "featureDetails": {
      "HEMS_EV_Custom_Schedule": true,
      "Disable_Storm_Guard_Grid_Charging": false
    },
    "userDetails": {
      "isOwner": true,
      "isInstaller": false,
      "email": "u******r@example.com"
    },
    "siteStatus": {
      "code": "normal",
      "text": "Normal",
      "severity": "warning"
    }
  }
}
```

### 5.3 Profile Details (System + EVSE)
```
GET /service/batteryConfig/api/v1/profile/<site_id>?source=enho&userId=<user_id>&locale=<locale>
```
Returns the active system profile plus embedded EVSE configuration used to render the EV charging card.

Example response (anonymized):
```json
{
  "type": "profile-details",
  "timestamp": "<timestamp>",
  "data": {
    "supportsMqtt": true,
    "pollingInterval": 60,
    "profile": "self-consumption",
    "operationModeSubType": "prioritize-energy",
    "batteryBackupPercentage": 20,
    "stormGuardState": "disabled",
    "acceptedStormGuardDisclaimer": false,
    "devices": {
      "iqEvse": [
        {
          "uuid": "<evse_uuid>",
          "deviceName": "IQ EV Charger",
          "profile": "self-consumption",
          "profileConfig": "full",
          "enable": false,
          "status": -1,
          "chargeMode": "MANUAL",
          "chargeModeStatus": "COMPLETED",
          "updatedAt": "<epoch_seconds>"
        }
      ]
    },
    "cfgControl": {
      "show": true,
      "enabled": true,
      "scheduleSupported": true,
      "forceScheduleSupported": true
    },
    "evseStormEnabled": false
  }
}
```

### 5.4 System Profile Updates (Site Profile)
```
PUT /service/batteryConfig/api/v1/profile/<site_id>?userId=<user_id>
Headers: X-XSRF-Token: <token>
```
Updates the system profile (Self-Consumption, Savings, Full Backup) and reserve percentage. The UI uses this to apply profile changes and EV charging mode selections.

Example payloads observed:
```json
{ "profile": "self-consumption", "batteryBackupPercentage": 10 }
```

```json
{
  "profile": "cost_savings",
  "operationModeSubType": "prioritize-energy",
  "batteryBackupPercentage": 20,
  "devices": [
    {
      "uuid": "<evse_uuid>",
      "chargeMode": "MANUAL",
      "deviceType": "iqEvse",
      "enable": false
    }
  ]
}
```

```json
{
  "profile": "backup_only",
  "batteryBackupPercentage": 100,
  "devices": [
    {
      "uuid": "<evse_uuid>",
      "chargeMode": "MANUAL",
      "deviceType": "iqEvse",
      "enable": false
    }
  ]
}
```

Response:
```json
{ "message": "success" }
```

Notes:
- The reserve slider enforces a 10% minimum (Self-Consumption) and 100% for Full Backup. The Savings profile uses a reserve slider plus a "Use battery after peak hours" toggle; `operationModeSubType` appears to track this state (only `prioritize-energy` observed so far).
- After saving a mode change, the UI shows a pending state until the profile takes effect. During this window, the user can cancel the request.

```
PUT /service/batteryConfig/api/v1/cancel/profile/<site_id>?userId=<user_id>
Headers: X-XSRF-Token: <token>
Body: {}
```
Cancels a pending profile change. The request body is an empty JSON object.

Example response:
```json
{ "message": "success" }
```

### 5.5 Battery Settings (Battery Details)
```
GET /service/batteryConfig/api/v1/batterySettings/<site_id>?source=enho&userId=<user_id>
```
Returns battery configuration details for the Battery page (battery mode, charge-from-grid settings, shutdown level).

Example response (anonymized):
```json
{
  "type": "battery-details",
  "timestamp": "<timestamp>",
  "data": {
    "profile": "self-consumption",
    "batteryBackupPercentage": 20,
    "stormGuardState": "disabled",
    "hideChargeFromGrid": false,
    "envoySupportsVls": true,
    "chargeBeginTime": 120,
    "chargeEndTime": 300,
    "batteryGridMode": "ImportExport",
    "veryLowSoc": 15,
    "veryLowSocMin": 10,
    "veryLowSocMax": 25,
    "chargeFromGrid": true,
    "chargeFromGridScheduleEnabled": true,
    "acceptedItcDisclaimer": "<timestamp>",
    "devices": {
      "iqEvse": { "useBatteryFrSelfConsumption": true }
    }
  }
}
```

```
PUT /service/batteryConfig/api/v1/batterySettings/<site_id>?userId=<user_id>
Headers: X-XSRF-Token: <token>
```
Updates battery settings. The UI sends partial payloads to change individual controls.

Example payloads observed:
```json
{ "chargeFromGrid": false }
```

```json
{
  "chargeFromGrid": true,
  "acceptedItcDisclaimer": "<timestamp>",
  "chargeBeginTime": 120,
  "chargeEndTime": 300,
  "chargeFromGridScheduleEnabled": true
}
```

```json
{ "veryLowSoc": 15 }
```

Response:
```json
{ "message": "success" }
```

Notes:
- `batteryGridMode` matches the Battery Mode card ("ImportExport" renders as "Import and Export") and is controlled by interconnection settings.
- `chargeFromGrid` backs the "Charge battery from the grid" toggle. Enabling it shows a disclaimer dialog; the confirmation sets `acceptedItcDisclaimer` and unlocks the schedule controls.
- The schedule checkbox ("Also up to 100% during this schedule") is represented by `chargeFromGridScheduleEnabled`; `chargeBeginTime`/`chargeEndTime` are minutes after midnight (local).
- When the schedule is enabled, the status payload reports `chargeFromGridScheduleEnabled: true` and `cfgControl.forceScheduleOpted: true`.
- `veryLowSoc` drives the "Battery shutdown level" slider, clamped between `veryLowSocMin` and `veryLowSocMax`.

### 5.6 Storm Guard Status + Toggle
```
GET /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
```
Returns Storm Guard alert state and critical alert override status.

Example response (anonymized):
```json
{
  "criticalAlertsOverride": true,
  "stormAlerts": [],
  "criticalAlertActive": false
}
```

```
PUT /service/batteryConfig/api/v1/stormGuard/toggle/<site_id>?userId=<user_id>
Headers: X-XSRF-Token: <token>
Body: {
  "stormGuardState": "enabled",
  "evseStormEnabled": true
}
```
Updates the Storm Guard toggle and the EV charging checkbox shown in the Storm Guard modal.

Example responses:
```json
{ "message": "success" }
```

Notes:
- `stormGuardState` accepts `enabled` or `disabled`.
- `evseStormEnabled` controls the EV Charging option ("Charges EV to 100% when Storm Alert is On"); the UI warns this may cause grid import costs.
- The web UI prompts with a confirmation dialog before enabling Storm Guard; once enabled, the profile automatically switches to Full Backup during severe weather alerts and reserves full battery capacity.

---

## 6. Authentication Flow (Shared Across Services)

### 6.1 Login (Enlighten Web)
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

### 6.2 MFA OTP Validation
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

### 6.3 MFA OTP Resend
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

### 6.4 Access Token
Some sites issue a JWT-like access token via `https://entrez.enphaseenergy.com/access_token`. The integration decodes the `exp` claim to know when to refresh.

### 6.5 Headers Required by API Client
- `e-auth-token: <token>`
- `Cookie: <serialized cookie jar>` (must include session cookies like `_enlighten_session`, `X-Requested-With`, etc.)
- When available: `Authorization: Bearer <token>`
- Common defaults also send:
  - `Referer: https://enlighten.enphaseenergy.com/`
  - `X-Requested-With: XMLHttpRequest`

The integration reuses tokens until expiry or a 401 is encountered, then prompts reauthentication.

---

## 7. Response Field Reference

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
| `safeLimitState` | DLB safe-mode indicator within `connectors[]`. Observed: `1` when DLB is enabled and the charger cannot reach the gateway, forcing a safe 8A limit. |
| `supportsUseBattery` | Summary v2 flag for green-mode "Use Battery" support |
| `networkConfig` | Interfaces with IP/fallback metadata |
| `firmwareVersion` | Charger firmware |
| `processorBoardVersion` | Hardware version |
| `current_charge` | Site battery state-of-charge percentage string (for example `"48%"`) |
| `available_energy` / `max_capacity` | Site battery available/maximum capacity in kWh |
| `available_power` / `max_power` | Site battery instantaneous/maximum power in kW |
| `storages[].serial_number` | Battery serial identifier |
| `storages[].excluded` | Battery inclusion flag used by the UI fleet card logic |
| `storages[].status` / `storages[].statusText` | Battery status code + display label |
| `storages[].last_report` | Epoch seconds for latest battery telemetry |
| `storages[].battery_soh` | Battery state-of-health percentage string |
| `included_count` / `excluded_count` | Active vs excluded battery counts in the payload |

Additional metrics documented in the official `/api/v4/.../telemetry` endpoint align with the time-series payloads we have observed (for example `consumption` arrays of Wh values paired with `end_at` epoch timestamps for each 15‑minute bucket). Treat those fields as alternate labels for the same energy-per-interval data returned by the Enlighten UI endpoints.

---

## 8. Error Handling & Rate Limiting
- HTTP 401 — credentials expired; request reauth.
- HTTP 400/404/409/422 during control operations — charger not ready/not plugged; treated as no-ops.
- Rate limiting presents as HTTP 429; the integration backs off and logs the event.
- Recommended polling interval: 30 s (configurable). Live stream can be used for short bursts (15 min)

### 8.1 Cloud status codes (from the official v4 control API)
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

## 9. Known Variations & Open Questions
- Some deployments omit `displayName` from `/status`; summary v2 is needed for friendly names.
- Session energy units vary; integration normalizes values >200 as Wh ➜ kWh.
- Local LAN endpoints (`/ivp/pdm/*`, `/ivp/peb/*`) exist but require installer permissions; not currently accessible with owner accounts.

---

## 10. References
- Reverse-engineered from Enlighten mobile/web network traces (2024–2026).
- Implemented in `custom_components/enphase_ev/api.py` and `coordinator.py`.
