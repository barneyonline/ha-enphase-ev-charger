# Enphase Energy Cloud API Specification

_This reference consolidates observed Enlighten mobile/web APIs across EV charging, site energy, gateway, battery, and microinverter features._

---

## 1. Overview
- **Base URL:** `https://enlighten.enphaseenergy.com`
- **Auth:** Most endpoints require the Enlighten `e-auth-token` header and the authenticated session `Cookie` header. Some services (notably scheduler and selected control APIs) also require `Authorization: Bearer <token>`.
- **Privacy:** Example identifiers, timestamps, and credentials in this document are anonymized placeholders.
- **Path Variables:**
  - `<site_id>` - numeric site identifier
  - `<sn>` - charger serial number
  - `connectorId` - connector index; currently always `1`
- **Discovery:** `GET /app-api/search_sites.json?searchText=&favourite=false` enumerates the account's accessible sites, returning IDs and display titles.

---

### 1.1 Site Discovery (Search API)
```
GET /app-api/search_sites.json?searchText=&favourite=false
```
Returns the sites tied to the authenticated account. `id` is the numeric site identifier and `title` is the display name when present.
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

### 1.2 Endpoint Families (Quick Layout)

- **Auth and discovery:** `1.1`, `6.1`-`6.6`
- **Site/system inventory and telemetry:** `2.9`-`2.21`
- **EV charger telemetry and metadata:** `2.1`-`2.8`
- **EV charger controls and scheduling:** `3.1`-`3.3`, `4.1`-`4.5`
- **BatteryConfig controls:** `5.1`-`5.11`
- **Cross-cutting references:** `7`, `8`, `9`

### 1.3 Table of Contents

- `1. Overview`
- `2. Core Site and Device Endpoints`
- `2.F HEMS (IQ Energy Router / Heat Pump Monitoring)`
- `2.G Mobile/Web Shared Constants`
- `3. EV Charger Control Operations`
- `4. EV Scheduler (Charge Mode) API`
- `5. BatteryConfig APIs (System Profile and Battery Controls)`
- `6. Authentication Flow (Shared Across Services)`
- `7. Response Field Reference`
- `8. Error Handling and Rate Limiting`
- `9. Known Variations and Open Questions`
- `10. References`

### 1.4 Endpoint Matrix (High-Level)

| Domain | Method | Endpoint | Auth | Used by integration |
| --- | --- | --- | --- | --- |
| Site discovery | `GET` | `/app-api/search_sites.json` | login session cookies | Yes |
| JWT token bootstrap | `GET` | `/app-api/jwt_token.json` | authenticated Enlighten session cookies | No |
| JWT token fallback | `GET` | `/service/auth_ms_enho/api/v1/session/token` | session cookies + `_enlighten_4_session` echoed as `e-auth-token` | No |
| Mobile/web shared constants | `GET` | `https://enlighten-mobile-38d22.firebaseio.com/enho_constants.json` | none observed | No (documented from web UI) |
| EV runtime status | `GET` | `/service/evse_controller/<site_id>/ev_chargers/status` | `e-auth-token` + cookies | Yes |
| EV metadata summary | `GET` | `/service/evse_controller/api/v2/<site_id>/ev_chargers/summary` | `e-auth-token` + cookies | Yes |
| EV last-reported timestamps | `GET` | `/service/evse_controller/api/v2/<site_id>/ev_chargers/last_reported_at` | `e-auth-token` + cookies | No (documented from web UI) |
| EV firmware details | `GET` | `/service/evse_management/fwDetails/<site_id>` | `e-auth-token` + cookies | Yes |
| EV feature flags | `GET` | `/service/evse_management/api/v1/config/feature-flags?site_id=<site_id>[&country=<country>]` | `e-auth-token` + cookies | Yes |
| EV daily timeseries | `GET` | `/service/timeseries/evse/timeseries/daily_energy?site_id=<site_id>&source=evse&requestId=<uuid>&start_date=<YYYY-MM-DD>[&username=<user_id>]` | bearer token + session headers | No (documented from runtime traces) |
| EV lifetime timeseries | `GET` | `/service/timeseries/evse/timeseries/lifetime_energy?site_id=<site_id>&source=evse&requestId=<uuid>[&username=<user_id>]` | bearer token + session headers | No (documented from runtime traces) |
| Site inventory | `GET` | `/app-api/<site_id>/devices.json` | `e-auth-token` + cookies | Yes |
| Filtered site-device inventory | `POST` | `/service/site-device/api/v2/devices/list` | `e-auth-token` + cookies | No (documented from web UI) |
| Site live-stream flags | `GET` | `/app-api/<site_id>/show_livestream` | authenticated session cookies | No (documented from web UI) |
| Site latest power | `GET` | `/app-api/<site_id>/get_latest_power` | `e-auth-token` + cookies | Yes |
| Site tariff configuration | `GET` | `/service/tariff/tariff-ms/systems/<site_id>/tariff?include-site-details=true` | bearer token + `e-auth-token` + cookies | No (documented from web UI) |
| System dashboard summary | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/summary` | session cookies (observed); `e-auth-token` unverified | No (documented from web UI) |
| System dashboard master data | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/data/master-data` | session cookies (+ XSRF) | No (documented from web UI) |
| Activation checklist | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/updated_activation_checklist` | `e-auth-token` + cookies | No (documented from web UI) |
| System dashboard devices table | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/devices?range=<range>&filter_columns=<...>&serial_numbers=<...>&type=table&page=<page>&per_page=<n>` | `e-auth-token` + cookies | No (documented from web UI) |
| System dashboard status | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/status` | `e-auth-token` + cookies | No (documented from web UI) |
| System dashboard range testing | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/range_testing` | `e-auth-token` + cookies | No (documented from web UI) |
| System dashboard device tree | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices-tree` | `e-auth-token` + cookies | No (documented from web UI) |
| Standing alarms | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/alarms` | `e-auth-token` + cookies | No (documented from web UI) |
| System dashboard device details | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=<type>` | `e-auth-token` + cookies | No (documented from web UI) |
| Site lifetime energy | `GET` | `/pv/systems/<site_id>/lifetime_energy` | `e-auth-token` + cookies | Yes |
| Homeowner events | `GET` | `/service/events-platform-service/v1.0/<site_id>/events/homeowner` | `e-auth-token` + cookies | Yes |
| Battery backup history | `GET` | `/app-api/<site_id>/battery_backup_history.json` | `e-auth-token` + cookies | Yes |
| Grid eligibility | `GET` | `/app-api/<site_id>/grid_control_check.json` | `e-auth-token` + cookies | Yes |
| Microinverter inventory | `GET` | `/app-api/<site_id>/inverters.json` | `e-auth-token` + cookies | Yes |
| Battery status | `GET` | `/pv/settings/<site_id>/battery_status.json` | `e-auth-token` + cookies | Yes |
| HEMS device inventory | `GET` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices` | Enlighten session cookies | No (documented for roadmap) |
| HEMS heat-pump runtime state | `GET` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/heatpump/<device_uid>/state?timezone=<iana_tz>` | Enlighten session cookies | No (documented from mobile app HAR) |
| HEMS daily device energy consumption | `GET` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/energy-consumption?from=<iso8601>&to=<iso8601>&timezone=<iana_tz>&step=<period>` | Enlighten session cookies | No (documented from mobile app HAR) |
| HEMS power timeseries | `GET` | `/systems/<site_id>/hems_power_timeseries[?device-uid=<device_uid>]` | `e-auth-token` + cookies | No (documented for roadmap) |
| HEMS lifetime consumption | `GET` | `/systems/<site_id>/hems_consumption_lifetime` | `e-auth-token` + cookies | No (documented for roadmap) |
| HEMS live stream toggle | `PUT` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/status` | Enlighten session cookies | No (monitoring stream only) |
| HEMS live vitals toggle | `PUT` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/vitals` | Enlighten session cookies | No (monitoring stream only) |
| Start charging | `POST` | `/service/evse_controller/<site_id>/ev_chargers/<sn>/start_charging` | `e-auth-token` + cookies | Yes |
| Stop charging | `PUT` | `/service/evse_controller/<site_id>/ev_chargers/<sn>/stop_charging` | `e-auth-token` + cookies | Yes |
| Charge mode preference | `GET/PUT` | `/service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference` | bearer token + session headers | Yes |
| BatteryConfig site settings | `GET` | `/service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` | `e-auth-token` + cookies + `Username` | Yes |
| BatteryConfig MQTT authorizer bootstrap | `GET` | `/service/batteryConfig/api/v1/mqttSignedUrl/<site_id>` | `e-auth-token` + cookies + `Username` | No |
| BatteryConfig third-party settings | `GET` | `/service/batteryConfig/api/v1/<site_id>/thirdPartyControlSettings` | `e-auth-token` + cookies + `Username` | No (documented from web UI) |
| BatteryConfig schedules | `GET` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules` | `e-auth-token` + cookies + `Username` | No (documented from web UI) |
| BatteryConfig schedule create | `POST` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules` | `e-auth-token` + cookies + `Username` + XSRF | No |
| BatteryConfig schedule validation | `POST` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/isValid` | `e-auth-token` + cookies + `Username` | No (documented from web UI) |
| BatteryConfig schedule update | `PUT` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>` | `e-auth-token` + cookies + `Username` + XSRF | No (documented from web UI) |
| BatteryConfig schedule legacy delete alias | `POST` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>/delete` | `e-auth-token` + cookies + `Username` + XSRF | No |
| BatteryConfig disclaimer accept | `POST` | `/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/<site_id>` | `e-auth-token` + cookies + `Username` | No (documented from web UI) |
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
Legacy responses may still return the flatter `evChargerData` shape.
The `connectors[]` payload includes `dlbActive`, `safeLimitState`, and connector status fields.

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

### 2.2.1 Last Reported Timestamps
```
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/last_reported_at
```
Returns a compact map of charger serial numbers to their latest report timestamp in epoch milliseconds.

Example response (anonymized):
```json
{
  "meta": {
    "serverTimeStamp": 1770000001000
  },
  "data": {
    "EV0000000000": 1769999900000
  },
  "error": {}
}
```

### 2.2.2 Firmware Details
```
GET /service/evse_management/fwDetails/<site_id>
```
Returns site-scoped EV charger firmware rollout details as an array keyed by `serialNumber`.
Unlike summary v2, the path variable is the site identifier, not the charger serial number.

Example response (anonymized capture):
```json
[
  {
    "serialNumber": "EVSE-SERIAL-0001",
    "siteId": 1234567,
    "upgradeStatus": 5,
    "currentFwVersion": "25.37.1.14",
    "targetFwVersion": "25.37.1.14",
    "lastSuccessfulUpgradeDate": "2025-12-08T22:41:46.568837098Z[UTC]",
    "lastUpdatedAt": "2025-12-08T15:52:59.806385175Z[UTC]",
    "statusDetail": null,
    "isAutoOta": false
  }
]
```

Observed fields:
- `serialNumber`: charger serial number used to join the record to summary/runtime data.
- `siteId`: numeric site identifier echoed by the service.
- `upgradeStatus`: integer firmware-upgrade state code. Semantics are not yet decoded; preserve the raw value.
- `currentFwVersion`: currently installed charger firmware.
- `targetFwVersion`: target charger firmware for update comparison.
- `lastSuccessfulUpgradeDate`: timestamp of the last successful firmware upgrade.
- `lastUpdatedAt`: service timestamp for the current firmware-details record.
- `statusDetail`: optional additional upgrade-state detail; often `null`.
- `isAutoOta`: whether automatic OTA behavior is enabled for the charger.

### 2.2.3 EV Feature Flags
```
GET /service/evse_management/api/v1/config/feature-flags?site_id=<site_id>[&country=<country>]
```
Returns site-wide and per-charger capability flags.

Example response (anonymized capture):
```json
{
  "meta": {
    "serverTimeStamp": "2026-03-08T09:40:02.917+00:00"
  },
  "data": {
    "evse_charging_mode": true,
    "evse_launch_countries": true,
    "evse_charge_range_slider": false,
    "off_peak_schedule": true,
    "evse_phase_switching": true,
    "ev_charging": true,
    "evse_tamper_detection": true,
    "evse_storm_guard": false,
    "iqevse_usebatterynew": false,
    "EVSE-SERIAL-0001": {
      "evse_network_settings": true,
      "evse_ble_control": true,
      "evse_gateway_connectivity": true,
      "dynamic_load_supported": true,
      "evse_authentication": true,
      "iqevse_rfid": true,
      "evse_ocpp_server_settings": true,
      "phase_config_support": true,
      "max_current_config_support": true,
      "plug_and_charge": false
    }
  },
  "error": {}
}
```

Observed structure:
- Top-level booleans under `data` are site-wide capability gates.
- Nested objects keyed by charger serial contain per-device flags.

Observed flags worth preserving:
- Site-level: `evse_charging_mode`, `evse_charge_range_slider`, `off_peak_schedule`, `evse_phase_switching`, `ev_charging`, `evse_tamper_detection`, `evse_storm_guard`, `iqevse_usebatterynew`.
- Per-charger: `dynamic_load_supported`, `evse_authentication`, `iqevse_rfid`, `evse_ocpp_server_settings`, `phase_config_support`, `max_current_config_support`, `plug_and_charge`, `evse_network_settings`, `evse_gateway_connectivity`.

### 2.3 Start Live Stream
```
GET /service/evse_controller/<site_id>/ev_chargers/start_live_stream
```
Initiates a short burst of rapid status updates.

Example response (anonymized):
```json
{
  "meta": {
    "serverTimeStamp": 1770000000000
  },
  "data": {
    "liveStreamTopicList": [
      "v1/evse/prod/live-stream/<stream_key>"
    ],
    "liveStreamDuration": 900
  },
  "error": {}
}
```

Observed behavior:
- The response returns one or more live topic identifiers plus a 15-minute duration.
- A subsequent request to `GET /service/evse_sse/subscribeEvent?key=<site_id>` was observed immediately afterward, but the HAR did not preserve event frames.

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

### 2.7.1 EVSE Timeseries (Daily + Lifetime Energy)
```
GET /service/timeseries/evse/timeseries/daily_energy?site_id=<site_id>&source=evse&requestId=<uuid>&start_date=<YYYY-MM-DD>[&username=<user_id>]
GET /service/timeseries/evse/timeseries/lifetime_energy?site_id=<site_id>&source=evse&requestId=<uuid>[&username=<user_id>]
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <session_id>
  requestid: <uuid>
  username: <user_id>
  X-Requested-With: XMLHttpRequest
```
Returns EV charger daily or lifetime energy keyed by charger serial.

Notes:
- The request parameter must be `site_id`. Requests using `siteId` were rejected by Enphase with `400 BAD_REQUEST` and the message `Required request parameter 'site_id' ... is not present`.
- The daily endpoint also requires `start_date` in `YYYY-MM-DD`. Omitting it produced `400 BAD_REQUEST` with `Required request parameter 'start_date' ... is not present`.
- `Authorization` uses the Auth MS JWT (from `/tokens` or the `enlighten_manager_token_production` cookie).
- `e-auth-token` should match the JWT `session_id` claim; `username` should match the JWT `user_id` claim when present.
- `requestId` / `requestid` is a UUID generated per request.

Example daily request:
```
GET /service/timeseries/evse/timeseries/daily_energy?site_id=1234567&source=evse&requestId=<uuid>&start_date=2026-03-13&username=2999024
```

Example lifetime request:
```
GET /service/timeseries/evse/timeseries/lifetime_energy?site_id=1234567&source=evse&requestId=<uuid>&username=2999024
```

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
Returns grouped device inventory for the site (Gateway, batteries, system controller, relays, meters, EV charger, etc.).

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
Additional observed buckets (anonymized excerpt):
```json
{
  "result": [
    {
      "type": "meter",
      "devices": [
        {
          "name": "IQ Envoy",
          "serial_number": "GW0000000000EIM1",
          "sku_id": null,
          "channel_type": "Localized production meter label",
          "status": "normal",
          "statusText": "Normal",
          "last_report": 1772183700
        }
      ]
    },
    {
      "type": "dryContactLoads",
      "devices": [
        {
          "name": "NC1",
          "load_name": "Aux Load 1",
          "load_type": "others",
          "status": "normal",
          "statusText": "Normal",
          "last_report": 1772183578
        }
      ]
    },
    {
      "type": "hemsDevices",
      "devices": [
        {
          "gateway": [{ "device-type": "IQ_ENERGY_ROUTER", "device-uid": "<site_id>_IQ_ENERGY_ROUTER_1" }],
          "heat-pump": [{ "device-type": "HEAT_PUMP", "device-uid": "<site_id>_HEAT_PUMP_1" }],
          "evse": [],
          "water-heater": []
        }
      ]
    },
    { "curr_date_site": "2026-02-27" }
  ]
}
```
Observed structure:
- `result[]` is a mixed array containing typed buckets (`{type, devices}`) and metadata objects (for example `curr_date_site`).
- Each bucket's `type` identifies the device family; `devices[]` may be empty.
- Common device fields: `name`, `serial_number`, `sku_id`, `status`, `statusText`, `last_report`.
- Optional fields vary by type (`ip`, `connected`, `envoy_sw_version`, `channel_type`, `sw_version`, `warranty_end_date`, `load_name`, `load_type`, etc.).
- `channel_type` labels may be localized by site locale (for example French meter labels).
- Some sites include a nested `type: "hemsDevices"` bucket in `/devices.json`, reusing the hierarchical HEMS shape documented in `2.17`.

### 2.9.1 Filtered Site-Device Inventory
```
POST /service/site-device/api/v2/devices/list
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
```
Returns a filtered device list for the system dashboard and device-management views. The request body carries the site identifier plus device-family filters and requested extra fields.

Example request body (anonymized capture):
```json
{
  "site_id": "1234567",
  "filters": {
    "include_retired": false,
    "include_sub_device": false,
    "core_device_types": ["IQ_AIR"],
    "extra_fields": ["WARRANTY", "STATUS"]
  }
}
```

Example response (anonymized capture):
```json
{
  "type": "device-details",
  "timestamp": "2026-03-09T05:46:35.782815934Z[UTC]",
  "data": {
    "devices": []
  }
}
```

Observed request fields:
- `site_id`: numeric site identifier passed in the JSON body rather than the URL path.
- `filters.include_retired`: includes retired devices when `true`.
- `filters.include_sub_device`: includes nested or subordinate devices when `true`.
- `filters.core_device_types`: array of requested device-family codes, observed with `IQ_AIR`.
- `filters.extra_fields`: optional extra metadata groups to hydrate in the response, observed with `WARRANTY` and `STATUS`.

Observed response fields:
- `type`: envelope discriminator, observed as `device-details`.
- `timestamp`: server-side generation timestamp.
- `data.devices`: array of matching devices; may be empty when no devices match the filter set.

Notes:
- This endpoint complements `/app-api/<site_id>/devices.json` by allowing the web UI to request a narrow device subset instead of the full site inventory.
- Additional `core_device_types` values were not present in this capture; preserve unknown codes verbatim until more examples are collected.

### 2.9.2 Live Stream Capability Flags
```
GET /app-api/<site_id>/show_livestream
```
Returns booleans indicating live site status and live vitals availability.

Example response (anonymized capture):
```json
{
  "live_status": true,
  "live_vitals": true
}
```

Observed request fields:
- Path parameter `site_id`: numeric Enlighten site identifier in the URL path.
- Method: `GET`.
- Query/body: none observed.
- `Accept: application/json`.
- Browser capture authenticated with Enlighten session cookies; no explicit `e-auth-token` header was present in the observed request.
- `Referer` was the site summary page: `/app/system_dashboard/sites/<site_id>/summary`.

Observed response fields:
- `live_status`: boolean gate for live site-status streaming availability.
- `live_vitals`: boolean gate for live vitals/telemetry streaming availability.

Notes:
- The raw browser trace included session cookies, a JWT-bearing cookie, user identifiers, and an exact site ID; those values are intentionally omitted here and replaced with placeholders.
- This endpoint appears to be a lightweight capability check used before the UI enables live monitoring flows.
- It complements the HEMS live-stream toggle endpoints documented in `2.F`, but does not itself start a stream or return a stream topic/key.

### 2.9.3 Latest Site Power
```
GET /app-api/<site_id>/get_latest_power
```
Returns the latest observed site power sample.

Example response (anonymized capture):
```json
{
  "latest_power": {
    "value": 752,
    "units": "W",
    "precision": 0,
    "time": 1773207600
  }
}
```

Observed fields:
- `value`: current site production power in watts.
- `units`: reported unit string, observed as `W`.
- `precision`: reported precision hint for the sample.
- `time`: Unix timestamp in seconds for the sampled value.

Notes:
- Requires the standard authenticated Enlighten session headers (`e-auth-token` plus cookies).
- The payload is nested under `latest_power`; treat a missing or non-numeric `value` as no sample rather than coercing to `0`.
- Observed timestamps are epoch seconds rather than milliseconds.

### 2.9.4 System Dashboard Summary Flags
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/summary
```
Returns high-level capability and region flags used by the system dashboard summary view.

Headers:
  Accept: application/json
  Cookie: <authenticated Enlighten session cookies>
  Referer: https://enlighten.enphaseenergy.com/app/system_dashboard/sites/<site_id>/summary

Example response (anonymized capture):
```json
{
  "is_ensemble": true,
  "is_ensemble3": true,
  "is_ensemble3_na": false,
  "is_ensemble3_row": true,
  "is_nem3": false,
  "is_dt": false,
  "currency_unit": "CUR",
  "currency_symbol": "$",
  "geo": "REGION",
  "country_code": "XX",
  "is_hems": false
}
```

Observed structure:
- The endpoint returns a flat JSON object; there is no top-level `data`, `meta`, or `error` envelope.
- `is_ensemble`, `is_ensemble3`, `is_ensemble3_na`, and `is_ensemble3_row` are site capability flags related to Ensemble / IQ Battery platform support. Preserve raw booleans because variant naming is product-specific.
- `is_nem3` appears to indicate whether the site is configured for a NEM 3 tariff/export regime. This interpretation is inferred from the field name and should be treated as provisional.
- `is_dt` is another site capability/configuration flag surfaced by the dashboard, but its exact meaning was not confirmed from this capture.
- `currency_unit`, `currency_symbol`, `geo`, and `country_code` provide region and localization metadata for downstream UI formatting.
- `is_hems` was observed on sites also exposing IQ Energy Router / heat-pump endpoints.
- `currency_*`, `geo`, and `country_code` are region-dependent.

Notes:
- The captured browser request succeeded with authenticated session cookies and did not include an `e-auth-token` header. Whether non-browser clients can omit `e-auth-token` for this endpoint remains unverified.
- The original trace contained live cookies, a site ID, account identifiers, and a client-facing IP address; those values are intentionally replaced with placeholders in this document.

### 2.9.4.a Activation Checklist
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/updated_activation_checklist
```
Returns the commissioning and activation checklist shown in the system dashboard for battery / controller capable sites.

Example response (anonymized capture):
```json
[
  {
    "label": "IQ Battery(s) Entered",
    "done": "18/09/2025 04:27 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ System Controller Entered",
    "done": "18/09/2025 04:34 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Comms Kit Detected",
    "done": "18/09/2025 04:35 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Cell Modem Connectivity",
    "done": "18/09/2025 04:35 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "WiFi/Ethernet Connectivity",
    "done": "18/09/2025 04:35 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ Battery(s) Provisioned",
    "done": "18/09/2025 04:36 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ System Controller Provisioned",
    "done": "18/09/2025 04:36 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Production CT Enabled",
    "done": "10/08/2022 12:42 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Consumption CT Enabled",
    "done": "10/08/2022 12:42 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Consumption CT - Load with Solar Production (Net-Consumption)",
    "done": "10/08/2022 12:42 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Battery Profile Set",
    "done": null,
    "color": "AMBER"
  },
  {
    "label": "Tariff Set",
    "done": "03/09/2025 06:32 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ System Controller FW Upgrade",
    "done": "22/01/2026 08:02 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ Battery FW Upgrade",
    "done": "21/01/2026 08:37 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Functional Validation Done",
    "done": "18/09/2025 04:44 PM TZ",
    "color": "GREEN"
  }
]
```

Observed structure:
- The endpoint returns a plain array; there is no top-level `data` envelope.
- `label` is a localized string, not a stable enum. Expect wording differences across locales and Enphase revisions.
- `done` is either `null` or a pre-formatted site-local timestamp string that already includes a timezone abbreviation.
- `color` is an uppercase status token observed as `GREEN` and `AMBER`; preserve unknown values rather than coercing them.

### 2.9.4.b System Dashboard Master Data Catalog
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/data/master-data
```
Returns the reference catalogs used by the system dashboard UI for device pickers, parameter filters, activity-type labels, and installer/user selectors.
Unlike the runtime/status endpoints, this payload is mostly lookup metadata rather than live telemetry.

Example response (anonymized capture):
```json
{
  "devices": [
    {
      "name": "Microinverter",
      "serial_num": "INV0000000001"
    },
    {
      "name": "IQ System Controller",
      "serial_num": "SC0000000001"
    },
    {
      "name": "IQ Battery PCU",
      "serial_num": "PCU0000000001"
    },
    {
      "name": "Production Meter",
      "serial_num": "GW0000000001EIM1"
    },
    {
      "name": "Gateway",
      "serial_num": "GW0000000001"
    }
  ],
  "parameters": [
    {
      "id": "ac_frequency",
      "name": "AC Frequency"
    },
    {
      "id": "energy_consumed",
      "name": "Energy Consumed"
    },
    {
      "id": "state_of_charge",
      "name": "State of Charge"
    },
    {
      "id": "temperature",
      "name": "Temperature"
    }
  ],
  "ranges": [
    {
      "id": "today",
      "name": "Today"
    },
    {
      "id": "past_7_days",
      "name": "Past 7 Days"
    },
    {
      "id": "month_to_date",
      "name": "Month to Date"
    },
    {
      "id": "custom",
      "name": "Custom"
    }
  ],
  "activity_types": [
    {
      "id": "owner_details_entered",
      "name": "Owner Details Entered"
    },
    {
      "id": "envoy_upgrade",
      "name": "Gateway Upgrade"
    },
    {
      "id": "evse_maintenance_success",
      "name": "Evse maintenance success"
    },
    {
      "id": "FW upgrade complete",
      "name": "Fw upgrade complete"
    }
  ],
  "users": [
    {
      "id": "installer.one@example.invalid",
      "name": "installer.one@example.invalid"
    },
    {
      "id": "installer.two@example.invalid",
      "name": "installer.two@example.invalid"
    }
  ]
}
```

Observed structure:
- The response is a plain object with five top-level arrays: `devices`, `parameters`, `ranges`, `activity_types`, and `users`.
- `devices` is a flat site inventory keyed by display `name` and `serial_num`. Observed names included microinverters, IQ Battery PCUs, IQ Batteries, IQ System Controller, meters, and the gateway.
- `parameters` exposes stable metric/filter IDs such as `ac_frequency`, `energy_consumed`, `power`, `state_of_charge`, and `temperature`.
- `ranges` enumerates the built-in dashboard date filters. Observed values were `today`, `past_7_days`, `month_to_date`, and `custom`.
- `activity_types` is a large catalog of commissioning, provisioning, maintenance, and firmware event identifiers mapped to human-readable labels.
- `users` contained email-address identifiers in the captured response; treat this array as personally identifiable data and anonymize or redact it in logs and documentation.

Notes:
- The browser capture used an authenticated same-origin Enlighten session with XSRF/session cookies; no bearer token was observed on this request.
- `activity_types.id` values are not normalized. The sample contained mixed casing, embedded spaces, and duplicate-looking variants, so clients should preserve the raw string rather than coercing it.
- Meter `serial_num` values may derive from the gateway serial with suffixes such as `EIM1` and `EIM2`.
- Because the payload is catalog-like and changed infrequently in the capture, it is a better candidate for caching than the live status endpoints.

### 2.9.4.c System Dashboard Devices Table
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/devices?range=today&start_date=<iso8601>&end_date=<iso8601>&filter_columns=<csv>&serial_numbers=<csv>&type=table&page=<page>&per_page=<n>
```
Returns the paginated device inventory table shown in the commissioning/system-dashboard UI.

Observed query parameters:
- `range`: observed as `today`.
- `start_date`, `end_date`: site-local ISO-8601 timestamps with offset.
- `filter_columns`: comma-separated column list controlling which fields the table returns.
- `serial_numbers`: comma-separated inventory scope. The capture included true serials plus synthetic group tokens such as `PcuDevice`.
- `type`: observed as `table`.
- `page`, `per_page`: pagination controls; captured values were `1..3` and `15`.
- `serial_number`, `device_type`, `hw_version`, `sw_version`, `last_report`: optional UI filter inputs; when unused, the web UI still sent them as empty strings on later pages.

Example response (anonymized):
```json
{
  "total_devices": 39,
  "page": "2",
  "per_page": "15",
  "devices": [
    {
      "device_type": "Gateway",
      "serial_number": "GW0000000000",
      "device_link": "https://enlighten.example/systems/<site_id>/envoys/200001",
      "device_status": "Normal",
      "sw_version": "D8.3.5228.250724 (abcdef)",
      "hw_version": "-",
      "created_at": "2026/03/01 12:04:44 +1100 (TZ)",
      "soc": "N/A",
      "delta_soc": "N/A",
      "plc_comm": 5,
      "profile": "Regional Grid Profile",
      "last_report": "2026/03/09 16:59:08 +1100 (TZ)",
      "time_since_last_report": "1 minute",
      "operation_mode": "N/A",
      "enc_serial_number": null,
      "enc_serial_number_link": null,
      "dmir_version": "-",
      "devimg_version": "500-00005-r01-v01.02.537 (abcdef)",
      "essimg_version": "500-00020-r01-v31.44.11 (abcdef)",
      "app_version": "500-00002-r01-v08.03.5228 (abcdef)",
      "ibl_fw_version": "N/A",
      "swift_asic_fw": "N/A"
    },
    {
      "device_type": "IQ Battery",
      "serial_number": "BAT0000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/300001",
      "device_status": "Normal",
      "sw_version": "522-00002-01-v3.0.8557_rel/31.44",
      "hw_version": "892-00030-r83",
      "created_at": "2025/09/18 16:35:47 +1000 (TZ)",
      "soc": 98,
      "delta_soc": 0,
      "plc_comm": 5,
      "rssi_dbm": 0,
      "profile": "N/A",
      "last_report": "2026/03/09 16:53:26 +1100 (TZ)",
      "time_since_last_report": "9 minutes",
      "operation_mode": "Multi-mode On Grid, Discharging",
      "enc_serial_number": null,
      "enc_serial_number_link": null,
      "dmir_version": "546-00002-01-v01",
      "devimg_version": null,
      "essimg_version": null,
      "app_version": "3.0.8557_rel/31.44",
      "ibl_fw_version": "3.1.813-abcdef",
      "swift_asic_fw": "001.002.1.7.2"
    },
    {
      "device_type": "IQ Battery PCU",
      "serial_number": "PCU0000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/inverters/310001",
      "device_status": "Normal",
      "sw_version": "521-00008-r00-v4.63.1-D63",
      "hw_version": "880-01691-r44",
      "created_at": "2025/09/18 16:56:35 +1000 (TZ)",
      "soc": "N/A",
      "delta_soc": "N/A",
      "plc_comm": "N/A",
      "profile": "N/A",
      "last_report": "2026/03/09 16:56:26 +1100 (TZ)",
      "time_since_last_report": "6 minutes",
      "operation_mode": "N/A",
      "enc_serial_number": "BAT0000000001",
      "enc_serial_number_link": "https://enlighten.example/systems/<site_id>/ac_batteries/300001",
      "dmir_version": "549-00057-r00-v4.63.1-D63",
      "devimg_version": null,
      "essimg_version": null,
      "app_version": null,
      "ibl_fw_version": "N/A",
      "swift_asic_fw": "N/A"
    },
    {
      "device_type": "IQ System Controller E3 Control Board",
      "serial_number": "CTRLBOARD0001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/310000",
      "device_status": "Normal",
      "sw_version": "522-00003-01-v2.7.7054_rel/31.44",
      "hw_version": "880-01323-r04",
      "created_at": "2025/09/18 16:56:34 +1000 (TZ)",
      "soc": "N/A",
      "delta_soc": "N/A",
      "plc_comm": "N/A",
      "profile": null,
      "last_report": "-",
      "time_since_last_report": "-",
      "operation_mode": "N/A",
      "enc_serial_number": null,
      "enc_serial_number_link": null,
      "dmir_version": null,
      "devimg_version": null,
      "essimg_version": null,
      "app_version": null,
      "ibl_fw_version": "N/A",
      "swift_asic_fw": "N/A"
    }
  ],
  "csv_link": "https://enlighten.example/admin/sites/<site_id>/site_devices_csv?...",
  "show_feoc_dom": false
}
```

Observed structure:
- The payload is a single page, with `total_devices` describing the full filtered result count.
- `devices[]` is heterogeneous. The row schema varies by `device_type`; battery rows expose numeric `soc`/`delta_soc`, PCU and BMCC rows add `enc_serial_number`, and controller daughterboards can report `"-"` for `last_report` and `time_since_last_report`.
- Observed `device_type` values included `Gateway`, `Cellular Modem`, `Microinverter`, `Production Meter`, `Consumption Meter`, `IQ Battery`, `IQ Battery PCU`, `IQ Battery BMCC`, `IQ System Controller`, `IQ System Controller E3 Control Board`, and `IQ System Controller Startup PCBA`.
- `device_status`, `profile`, `operation_mode`, and `time_since_last_report` are display-oriented strings and may be localized.
- Missing values use a mix of `null`, `"N/A"`, and `"-"` depending on the field and device family.
- `device_link`, `enc_serial_number_link`, and `csv_link` are direct dashboard URLs. They should be treated as sensitive because they embed site-specific identifiers.
- `show_feoc_dom` was observed as a boolean feature flag (`false` in the capture); semantics are still unclear.

### 2.9.5 System Dashboard Status Overview
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/status
```
Returns the compact site overview shown in the system dashboard header.

Example response (anonymized):
```json
{
  "name": "Example Account",
  "status": "normal",
  "statusText": "Normal",
  "battery_mode": "Self - Consumption",
  "soc": "97%",
  "storm_guard": "Disabled",
  "storage_setpoint": 22,
  "pv_setpoint": 100,
  "reserved_soc": 20,
  "backup_type": "Partial Home Backup",
  "timezone": "Region/City",
  "isIqcp": false,
  "items": [
    {
      "name": "Admin View",
      "link": "https://enlighten.example/admin/sites/<site_id>"
    },
    {
      "name": "MyEnlighten View",
      "link": "https://enlighten.example/web/<site_id>?v=3.4.0"
    },
    {
      "name": "Enlighten Mobile",
      "link": "https://enlighten.example/mobile/<site_id>?v=3.4.0"
    },
    {
      "name": "Enlighten Manager",
      "link": "https://enlighten.example/systems/<site_id>"
    }
  ]
}
```

Observed structure:
- The response is a flat object with no `meta`/`data` envelope.
- `name` contains account-holder text and should be treated as sensitive.
- `status` is a normalized lowercase state token, while `statusText` is the localized display label shown in the UI.
- `battery_mode`, `storm_guard`, and `backup_type` are localized display strings, not stable enums; preserve spacing and punctuation as returned.
- `soc` is a string percentage, not a numeric ratio.
- `storage_setpoint`, `pv_setpoint`, and `reserved_soc` are integer tuning values. `storage_setpoint` has been observed as both positive and negative; semantics are still unclear.
- `items[]` contains dashboard shortcut labels and fully qualified site URLs for other Enlighten surfaces.
- Observed request auth was session-cookie based from the dashboard web app; no explicit `e-auth-token` header was present in the captured request.

### 2.9.5.a System Dashboard Range Testing
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/range_testing
```
Returns the current dashboard-visible range-test state for the site.

Example response (anonymized):
```json
{
  "tested_on": null,
  "is_success": false,
  "range_test": []
}
```

Observed structure:
- No query parameters or request body were observed; the endpoint used the same authenticated dashboard session headers as other `api_internal/dashboard` calls.
- `tested_on` is nullable. The captured response returned `null`, so the exact timestamp format for completed tests is not yet confirmed.
- `is_success` is a boolean flag indicating the recorded outcome of the range test.
- `range_test` is an array. The captured response returned an empty array when no test result was available.

Inference:
- Based on the path and field names, this appears to expose a site-level range-test or commissioning validation result used by the system dashboard UI.
- The concrete schema of items inside `range_test` remains unknown because the observed capture contained no entries.

### 2.9.6 System Dashboard Device Tree
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices-tree
```
Returns a flattened parent/child topology for the site and attached devices.

Example response (anonymized):
```json
{
  "data": [
    {
      "id": 1234567,
      "name": "System",
      "serial_number": 1234567,
      "status": "normal",
      "type": "Site"
    },
    {
      "id": 210000001,
      "name": "Gateway",
      "serial_number": "GW0000000000",
      "status": "Normal",
      "type": "Envoy",
      "parent_id": 1234567
    },
    {
      "id": 210000002,
      "name": "Cellular Modem",
      "serial_number": "MD0000000000",
      "status": "activated",
      "type": "CellularModem",
      "parent_id": 210000001
    },
    {
      "id": 210000101,
      "name": "Microinverter",
      "serial_number": "MI0000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "PcuDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000102,
      "name": "Microinverter",
      "serial_number": "MI0000000002",
      "status": "Normal",
      "sub_status": "",
      "type": "PcuDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000201,
      "name": "Production Meter",
      "serial_number": "GW0000000000EIM1",
      "status": "Normal",
      "sub_status": "",
      "type": "EimDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000202,
      "name": "Consumption Meter",
      "serial_number": "GW0000000000EIM2",
      "status": "Normal",
      "sub_status": "",
      "type": "EimDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000301,
      "name": "IQ System Controller",
      "serial_number": "SC0000000000",
      "status": "Normal",
      "sub_status": "",
      "type": "Enpower",
      "parent_id": 210000001
    },
    {
      "id": 210000302,
      "name": "E3 Control Board",
      "serial_number": "SCB000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EnpowerE3ControlBoard",
      "parent_id": 210000301
    },
    {
      "id": 210000303,
      "name": "IQ System Controller Startup PCBA",
      "serial_number": "SCP000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EnpowerStartupPcba",
      "parent_id": 210000301
    },
    {
      "id": 210000401,
      "name": "IQ Battery",
      "serial_number": "BAT000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "Encharge",
      "parent_id": 210000001
    },
    {
      "id": 210000402,
      "name": "IQ Battery BMCC",
      "serial_number": "BATB00000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EnchargeE3ControlBoard",
      "parent_id": 210000401
    },
    {
      "id": 210000403,
      "name": "IQ Battery PCU",
      "serial_number": "BATP00000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EncPcuDevice",
      "parent_id": 210000401
    },
    {
      "id": 210000404,
      "name": "IQ Battery PCU",
      "serial_number": "BATP00000002",
      "status": "Normal",
      "sub_status": "",
      "type": "EncPcuDevice",
      "parent_id": 210000401
    }
  ]
}
```

Observed structure:
- The payload is a flat array; hierarchy is reconstructed via `parent_id`.
- The site root record has no `parent_id`; child records reference either the site root or another device node.
- `serial_number` is usually a string, but the site root was observed echoing the numeric site identifier.
- `status` and `sub_status` are human-readable strings. The same payload used both `normal` and `Normal`, so casing should not be treated as stable.
- Repeated hardware such as microinverters and battery PCUs appears as one record per physical unit, often with generic `name` values.

Observed fields:
- `id`: numeric device or site node identifier. This is the value referenced by `parent_id`.
- `name`: dashboard label for the node. It is often generic (`"Microinverter"`, `"IQ Battery PCU"`) rather than user-customized.
- `serial_number`: device serial or synthetic meter identifier. Meter entries may suffix the gateway serial with `EIM1` or `EIM2`.
- `status`: display-oriented device state. Preserve raw text rather than coercing it to an enum.
- `sub_status`: optional secondary status string; often empty for healthy devices.
- `type`: backend device-class key used by system-dashboard pages and the related `devices_details?type=<type>` endpoint.
- `parent_id`: foreign key to the containing node. Missing on the root `Site` record.

Observed `type` values from the capture:
- `Site`: root system node.
- `Envoy`: gateway / communications hub.
- `CellularModem`: optional modem attached to the gateway.
- `PcuDevice`: microinverter node.
- `EimDevice`: production or consumption meter.
- `Enpower`: IQ System Controller.
- `EnpowerE3ControlBoard`: system-controller control board child.
- `EnpowerStartupPcba`: system-controller startup board child.
- `Encharge`: IQ Battery node.
- `EnchargeE3ControlBoard`: battery BMCC/control-board child.
- `EncPcuDevice`: battery PCU child.

### 2.9.7 Standing Alarms
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/alarms?range=today&filter_columns=<...>&type=table&page=<page>&per_page=<n>
```
Returns the table backing the "Standing Alarms" view in the system dashboard.

Example response (anonymized):
```json
{
  "total": 2,
  "alarms": [
    {
      "id": "<site_id>.440.1770000000000",
      "severity": 4,
      "type": "Gateway",
      "serial_num": "GW0000000000",
      "device_link": "https://enlighten.example/systems/<site_id>/envoys/200001",
      "description": "Aggregate battery low state of charge",
      "first_set": "2026/03/08 09:41:28 +0100 (CET)",
      "force_clearable": true
    }
  ],
  "page": "1",
  "per_page": "200",
  "csv_link": "https://enlighten.example/admin/sites/<site_id>/standing_alarms?...",
  "disable_force_clear": false,
  "availablePages": {
    "next_pointers": {},
    "last_offset": 0
  }
}
```

Observed structure:
- `first_set` is already formatted as a site-local string rather than epoch time.
- `serial_num` may contain a true serial number or aggregate text such as `"2 Devices"`.
- `force_clearable` and `disable_force_clear` appear to indicate whether manual clear actions are allowed.

### 2.9.8 System Dashboard Device Details by Type
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=envoys
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=encharges
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=enpowers
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=meters
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=modems
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=inverters
```
Returns per-family detail cards for the system dashboard device modal.

Observed query parameter:
- `type` selects the device family. Observed values: `envoys`, `encharges`, `enpowers`, `meters`, `modems`, `inverters`.

Example response for `type=envoys` (anonymized):
```json
{
  "envoys": [
    {
      "id": 200001,
      "name": "IQ Gateway",
      "serial_number": "GW0000000000",
      "device_link": "https://enlighten.example/systems/<site_id>/envoys/200001",
      "sku_id": "SC100G-M000ROW",
      "connected": true,
      "status": "normal",
      "statusText": "Normal",
      "ip": "192.0.2.10",
      "ap_mode": true,
      "envoy_sw_version": "D8.3.5167.250527",
      "last_report": "2026/03/08 09:59:12 +0100 (CET)",
      "connection_details": {
        "cellular": false,
        "wifi": null,
        "ethernet": true,
        "interface_ip": {
          "wifi": null,
          "ethernet": "192.0.2.10"
        }
      }
    }
  ]
}
```

Example response for `type=encharges` (anonymized):
```json
{
  "encharges": [
    {
      "id": 300001,
      "name": "IQ Battery 5P",
      "serial_number": "BAT0000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/300001",
      "sku_id": "B05-T02-ROW00-1-2",
      "channel_type": "IQ Battery",
      "phase": "L1(A)",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:38:49 +1100 (AEDT)",
      "sw_version": "546-00002-01-v01",
      "total": 2,
      "not_reporting": 0,
      "rssi_subghz": 0,
      "rssi_24ghz": 5,
      "rssi_dbm": 0,
      "soc": "98%",
      "operation_mode": "Multi-mode On Grid, Discharging",
      "led_status": 13,
      "app_version": "3.0.8557_rel/31.44",
      "alarm_id": null
    }
  ]
}
```

Example response for `type=enpowers` (anonymized):
```json
{
  "enpowers": [
    {
      "id": 310001,
      "name": "IQ System Controller",
      "serial_number": "CTRL000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/310001",
      "sku_id": "SC100G-M230ROW",
      "channel_type": "IQ System Controller",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:42:43 +1100 (AEDT)",
      "sw_version": "546-00003-01-v01",
      "rssi_subghz": 0,
      "rssi_24ghz": 5,
      "rssi_dbm": 0,
      "operation_mode": "Grid Connected - IQ Batteries Connected",
      "app_version": "2.7.7054_rel/31.44",
      "earth": "TN-C-S"
    }
  ]
}
```

Example response for `type=meters` (anonymized):
```json
{
  "meters": [
    {
      "id": 320001,
      "name": "IQ Gateway",
      "serial_number": "GW0000000000EIM1",
      "device_link": "https://enlighten.example/systems/<site_id>/meters/320001",
      "sku_id": null,
      "channel_type": "Production Meter",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:40:00 +1100 (AEDT)",
      "meter_state": "Enabled",
      "config_type": "Production",
      "meter_type": "Production"
    },
    {
      "id": 320002,
      "name": "IQ Gateway",
      "serial_number": "GW0000000000EIM2",
      "device_link": "https://enlighten.example/systems/<site_id>/meters/320002",
      "sku_id": null,
      "channel_type": "Consumption Meter",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:40:00 +1100 (AEDT)",
      "meter_state": "Enabled",
      "config_type": "Net (Load with Solar)",
      "meter_type": "Consumption"
    }
  ]
}
```

Example response for `type=modems` (anonymized):
```json
{
  "modems": [
    {
      "id": 330001,
      "serial_number": "89010000000000000000",
      "device_status": "Normal",
      "status": "activated",
      "statusText": "Activated",
      "last_report": "2026/03/09 16:44:25 +1100 (AEDT)",
      "sw_version": "EG25GLGDR07A03M1G_01.200.01.200",
      "signal": 4,
      "rssi": 18,
      "bit_error_rate": "99 (Unknown)",
      "plan_end": "2030/09/17",
      "part_number_with_sku": "865-02038-r03 (CELLMODEM-07-INT-05-CM)",
      "device_link": "https://enlighten.example/systems/<site_id>/devices?status=active#cellular_modems"
    }
  ]
}
```

Example response for `type=inverters` (anonymized):
```json
{
  "inverters": {
    "total": 16,
    "not_reporting": 0,
    "plc_comm": 5,
    "items": [
      {
        "name": "IQ7A Microinverters",
        "count": 16
      }
    ],
    "device_link": "https://enlighten.example/systems/<site_id>/devices?status=active"
  }
}
```

Observed structure:
- The top-level key matches the requested `type`.
- `envoys`, `encharges`, `enpowers`, `meters`, and `modems` return arrays of device cards.
- `inverters` returns a summary object with `total`, `not_reporting`, `plc_comm`, `items[]`, and `device_link` rather than per-device rows.
- Fields are family-specific and often localized (`statusText`, `channel_type`, meter labels, modem plan text).
- `encharges` and `enpowers` both expose RF/link-health fields (`rssi_subghz`, `rssi_24ghz`, `rssi_dbm`) plus family-specific operating-state strings.
- `meters` expose configuration labels (`config_type`, `meter_type`) rather than firmware/network details.
- `modems` expose provisioning state (`status`, `plan_end`, `part_number_with_sku`) instead of the gateway-style network payload.
- These endpoints expose sensitive infrastructure details such as IP addresses, MAC-derived identifiers, and direct dashboard links; redact aggressively before sharing traces.

### 2.9.8.a Site Tariff Configuration
```
GET /service/tariff/tariff-ms/systems/<site_id>/tariff?include-site-details=true
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: <authenticated Enlighten session cookies>
  e-auth-token: <session_id>
  X-Requested-With: XMLHttpRequest
```
Returns the tariff profile used for site cost calculations and tariff-aware battery / EV charging UI flows.

Example response (anonymized; representative values only):
```json
{
  "site_id": 1234567,
  "country": "XX",
  "currency": "$",
  "zipcode": "9999",
  "hasAcb": false,
  "chargeFromGrid": true,
  "chargeBeginTime": 120,
  "chargeEndTime": 300,
  "showBatteryConfig": true,
  "hideChargeFromGrid": true,
  "supports_mqtt": true,
  "calibrationProgress": false,
  "purchase": {
    "typeKind": "single",
    "typeId": "tou",
    "hasNetMetering": false,
    "source": "manual",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {
                "id": "off-peak",
                "startTime": "",
                "endTime": "",
                "rate": "0.18",
                "type": "off-peak",
                "rateComponents": []
              },
              {
                "id": "peak-1",
                "startTime": "840",
                "endTime": "1260",
                "rate": "0.31",
                "type": "peak",
                "rateComponents": []
              }
            ],
            "updatedValue": "",
            "must_charge_start": "0",
            "must_charge_duration": "0",
            "must_charge_mode": "CP"
          }
        ]
      }
    ]
  },
  "buyback": {
    "typeKind": "single",
    "typeId": "tou",
    "source": "netFit",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {
                "id": "off-peak",
                "startTime": "",
                "endTime": "",
                "rate": "0.02",
                "type": "off-peak",
                "rateComponents": []
              },
              {
                "id": "peak-1",
                "startTime": "960",
                "endTime": "1320",
                "rate": "0.06",
                "type": "peak",
                "rateComponents": []
              }
            ],
            "updatedValue": ""
          }
        ]
      }
    ],
    "exportPlan": "netFit"
  },
  "nemVersion": "",
  "installDate": "2024-01-15",
  "showDTQuestion": false,
  "dtCustomChargeEnabled": true
}
```

Observed request fields:
- Path parameter `site_id`: numeric site identifier embedded in the `/systems/<site_id>/...` path.
- Query parameter `include-site-details`: observed as `true`; when set, the response includes top-level site metadata such as `country`, `currency`, `zipcode`, `installDate`, and battery-related capability flags.

Observed response structure:
- The response is a flat JSON object with no `data` / `meta` wrapper.
- `purchase` describes import tariffs and `buyback` describes export compensation. Both use the same nested `seasons[] -> days[] -> periods[]` shape.
- `periods[].startTime` and `periods[].endTime` are stringified minutes-after-midnight values (for example `"840"` = 14:00 local time). Empty strings were observed for all-day/default periods.
- `periods[].rate` is returned as a string, not a numeric JSON value.
- `days[].days` uses numeric weekday values; the capture showed `[1, 2, 3, 4, 5, 6, 7]` for an every-day schedule.
- `purchase.days[].must_charge_start`, `must_charge_duration`, and `must_charge_mode` appear to be EV/battery charge-policy hints attached to the import tariff definition.
- Top-level `chargeBeginTime` / `chargeEndTime` are integer minutes after midnight and align with the charge-from-grid window exposed by the BatteryConfig APIs.
- Top-level flags such as `showBatteryConfig`, `hideChargeFromGrid`, `supports_mqtt`, `calibrationProgress`, and `dtCustomChargeEnabled` appear to gate related UI behavior.

Notes:
- The observed browser request included live bearer tokens, cookies, site identifiers, postcode data, and account-linked metadata. Those values are intentionally replaced here with placeholders or representative examples.
- `Authorization: Bearer <jwt>` was present in the capture, unlike many read-only site endpoints that rely on cookies plus `e-auth-token` alone.
- `source` values under `purchase` / `buyback` are backend-origin labels (`manual`, `netFit` in the capture) and should be preserved verbatim until more variants are observed.

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
- Heat-pump / HEMS captures also showed stable `event_key` values for SG Ready mode transitions and connectivity problems, including:
  - `hems_sgready_mode_changed_to_2` for "normal mode"
  - `hems_sgready_mode_changed_to_3` for "recommended consumption"
  - `hems_sgready_relay_offline` when the heat pump / SG Ready relay stopped communicating
  - `hems_energy_meter_offline` when the HEMS energy meter stopped communicating
  - `hems_iqer_MQTT_offline` when the IQ Energy Router stopped forwarding connected-device data to Enphase Cloud

Inference:
- The event-history feed provides stable SG Ready transition keys even when the human-readable descriptions are localized, so it is useful for documenting `MODE_2` versus `MODE_3` semantics without relying on translated text.

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
- `histories[]` entries contain ISO8601 `start_time` and `duration` (seconds); `end_time` can be derived as `start_time + duration`.

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
Returns booleans describing off-grid control eligibility and in-progress state.

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
- `disableGridControl=true` indicates a grid-mode toggle is blocked.
- `activeDownload`/`sunlightBackupSystemCheck`/`gridOutageCheck` are guard conditions surfaced by the backend.
- `userInitiatedGridToggle` indicates whether a toggle workflow is already in progress.
- This endpoint does **not** provide the current steady-state grid mode (`On Grid`/`Off Grid`).

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
- `state=1` requests transition to off-grid mode.
- `state=2` requests transition to on-grid mode.

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
Returns grid-outage state and regional contact metadata.

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

### 2.12.6 Grid Toggle Request Sequence
Both directions use a confirmation + OTP gate before the relay command is sent.

Off-grid (`System is On Grid` -> `System is Off Grid`):
1. User taps `Go Off Grid` toggle and confirms warning dialog.
2. Client calls `GET /app-api/<site_id>/grid_toggle_otp.json`.
3. User enters OTP; client calls `POST /app-api/grid_toggle_otp.json`.
4. If `{"valid": true}`, client calls `POST /pv/settings/grid_state.json` with `state=1`.
5. Backend transition proceeds after the command is accepted.
6. Client logs transition via `POST /pv/settings/log_grid_change.json`.

On-grid (`System is Off Grid` -> `System is On Grid`):
1. User taps `Go On Grid` toggle and confirms reconnect dialog.
2. Client calls `GET /app-api/<site_id>/grid_toggle_otp.json`.
3. User enters OTP; client calls `POST /app-api/grid_toggle_otp.json`.
4. If `{"valid": true}`, client calls `POST /pv/settings/grid_state.json` with `state=2`.
5. Backend transition proceeds after the command is accepted.
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
Returns the microinverter list for the site.

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
- `last_report` is epoch seconds.

### 2.14 Inverter Production by Date Range
```
GET /systems/<site_id>/inverter_data_x/energy.json?start_date=<YYYY-MM-DD>&end_date=<YYYY-MM-DD>
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns per-inverter production totals for the requested date window. The response is keyed by inverter ID.

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
- `excluded=true` marks batteries excluded from active fleet calculations; included/excluded counters are exposed at the top level.
- Percentage fields (`current_charge`, `battery_soh`) are string percentages in observed payloads.
- Status appears as normalized code (`status`, for example `normal`) plus a display label (`statusText`, for example `Normal`).
- `led_status` is the raw battery LED/runtime status code. The integration currently interprets `12` as charging, `13` as discharging, `14` as idle, `15` as idle, and `17` as idle; any other value is treated as unknown runtime state.

Observed battery LED legend:
- Rapidly Flashing Yellow: Starting up / establishing communications
- Red Double Flash: Error. Refer to `Troubleshooting`
- Solid Yellow: Not operating due to high temperature
- Solid Blue or Green: Idle. Colour transitions between blue and green as battery charge changes. Check Live State for charge status.
- Soft Pulse Blue: Discharging
- Soft Pulse Green: Charging
- Soft Pulse Yellow: Sleep mode
- Red Triple Flashes: DC switch OFF
- Red One-Second Flash: Rapid Shutdown mode
- Off: Not operating. Refer `Troubleshooting`

### 2.F HEMS (IQ Energy Router / Heat Pump Monitoring)

The endpoints below are read-oriented HEMS/IQ Energy Router APIs observed in Enlighten web captures for sites with paired router + heat-pump accessories.

### 2.17 HEMS Device Inventory (Router + Heat Pump Stack)
```
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices
Headers:
  Accept: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
```
Returns grouped HEMS devices, including IQ Energy Router and connected heat-pump ecosystem devices.

Example response shape (anonymized):
```json
{
  "status": "success",
  "result": {
    "devices": [
      {
        "gateway": [
          {
            "name": "IQ Energy Router_1",
            "device-type": "IQ_ENERGY_ROUTER",
            "device-uid": "<site_id>_IQ_ENERGY_ROUTER_1",
            "uid": "ROUTER-UID-001",
            "make": "RouterVendor",
            "model": "RouterModel",
            "status": "normal",
            "statusText": "Normal",
            "pairing-status": "PAIRED",
            "last-report": 1770000000,
            "hems-device-id": "hd_router_001",
            "hems-device-facet-id": "hf_router_001",
            "iqer-uid": "iqer_uid_001",
            "created-at": "2026-02-01T10:00:00Z"
          },
          {
            "name": "IQ Gateway_1",
            "device-type": "IQ_GATEWAY",
            "device-uid": "<site_id>_IQ_GATEWAY_1",
            "uid": "GW-UID-001",
            "status": "normal",
            "statusText": "Normal",
            "pairing-status": "PAIRED",
            "last-report": 1770000001
          }
        ],
        "heat-pump": [
          {
            "name": "SG Ready Gateway_1",
            "device-type": "SG_READY_GATEWAY",
            "device-uid": "<site_id>_SG_READY_GATEWAY_1",
            "uid": "SGR-UID-001",
            "make": "GatewayVendor",
            "model": "GatewayModel",
            "status": "normal",
            "statusText": "Normal",
            "pairing-status": "PAIRED",
            "last-report": 1770000002
          },
          {
            "name": "Energy Meter_1",
            "device-type": "ENERGY_METER",
            "device-uid": "<site_id>_ENERGY_METER_1",
            "uid": "METER-UID-001",
            "make": "MeterVendor",
            "model": "MeterModel",
            "firmware-version": "X.Y.Z",
            "status": "normal",
            "statusText": "Normal",
            "pairing-status": "PAIRED",
            "last-report": 1770000003
          },
          {
            "name": "Heat Pump_1",
            "device-type": "HEAT_PUMP",
            "device-uid": "<site_id>_HEAT_PUMP_1",
            "make": "HeatPumpVendor",
            "model": "HeatPumpModel",
            "status": "normal",
            "statusText": "Normal",
            "pairing-status": "PAIRED",
            "fvt-time": 1770000000
          }
        ],
        "evse": [],
        "water-heater": []
      }
    ]
  }
}
```
Observed structure:
- Device grouping is hierarchical (`gateway`, `heat-pump`, `evse`, `water-heater`) rather than the flat `type/devices` shape used by `/app-api/<site_id>/devices.json`.
- `device-uid` is a stable HEMS identifier reused across timeseries filters and related detail requests.
- For the captured site, device types present were `IQ_ENERGY_ROUTER`, `IQ_GATEWAY`, `SG_READY_GATEWAY`, `ENERGY_METER`, and `HEAT_PUMP`.

### 2.17.1 HEMS Heat Pump Runtime State (Per Heat Pump UID)
```
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/heatpump/<device_uid>/state?timezone=<iana_tz>
Headers:
  Accept: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
```
Returns app-facing runtime state for a single heat-pump device UID.

Example response from an "off / not running" mobile-app capture (anonymized only by site/device placeholders):
```json
{
  "type": "hems-heatpump-details",
  "timestamp": "2026-03-20T07:53:00.982568365Z",
  "data": {
    "device-uid": "<site_id>_HEAT_PUMP_1",
    "heatpump-status": "IDLE",
    "vpp-sgready-mode-override": "NONE",
    "sg-ready-mode": "MODE_2",
    "last-report-at": "2026-03-20T07:51:59.643Z"
  }
}
```

Example response from a "heating / SG Ready on" mobile-app capture (anonymized):
```json
{
  "type": "hems-heatpump-details",
  "timestamp": "2026-03-20T08:19:17.945447902Z",
  "data": {
    "device-uid": "<site_id>_HEAT_PUMP_1",
    "heatpump-status": "RUNNING",
    "vpp-sgready-mode-override": "NONE",
    "sg-ready-mode": "MODE_3",
    "last-report-at": "2026-03-20T08:18:59.604Z"
  }
}
```

Observed structure:
- `type` was `hems-heatpump-details` in all captured responses.
- `heatpump-status` represented runtime state and was observed as `IDLE` when the heat pump was off and `RUNNING` while it was actively heating.
- `sg-ready-mode` and `vpp-sgready-mode-override` appear alongside runtime state and may explain SG Ready behavior independently of health/status reporting.
- `sg-ready-mode` was observed as `MODE_2` when the app/event history described the heat pump as being in normal mode, and as `MODE_3` when it was heating with recommended-consumption / SG Ready on.
- `vpp-sgready-mode-override` remained `NONE` in both idle and running captures; other values are still undocumented.
- `last-report-at` is an ISO-8601 timestamp and is more precise than the sparse `fvt-time`/`last-report` fields seen on some inventory members.

Inference:
- This endpoint is a better candidate for the user-visible "running vs not running" state than `hems-devices.statusText`, which appears to describe device health/reporting (`Normal`, `Warning`, etc.) rather than runtime activity.

### 2.17.2 HEMS Daily Device Energy Consumption
```
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/energy-consumption?from=<iso8601>&to=<iso8601>&timezone=<iana_tz>&step=P1D
Headers:
  Accept: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
```
Returns per-device daily energy-consumption buckets for HEMS-managed loads.

Example response from the same "off / not running" capture (anonymized):
```json
{
  "type": "hems-device-details",
  "timestamp": "2026-03-20T07:53:00.739143826Z",
  "data": {
    "heat-pump": [
      {
        "device-uid": "<site_id>_HEAT_PUMP_1",
        "device-name": "Waermepumpe",
        "consumption": [
          {
            "solar": 0.0,
            "battery": 0.0,
            "grid": 0.0,
            "details": [47.0]
          }
        ]
      }
    ],
    "evse": [],
    "water-heater": []
  }
}
```

Observed structure:
- `type` was `hems-device-details` in the captured responses.
- Results are grouped by HEMS family (`heat-pump`, `evse`, `water-heater`).
- The `consumption[]` item exposes source-split totals (`solar`, `battery`, `grid`) plus a `details[]` numeric array.
- In active-heating captures the `details[]` value increased across polls (`201.0`, `211.0`, `220.0`, `230.0`) while `/heatpump/<device_uid>/state` reported `heatpump-status: RUNNING`.

Inference:
- In the "not running" capture, `details: [47.0]` was still present even though the runtime endpoint reported `heatpump-status: IDLE`, so this endpoint should be treated as daily aggregate consumption, not instantaneous on/off state.

### 2.18 HEMS Power Timeseries (Heat Pump Consumption)
```
GET /systems/<site_id>/hems_power_timeseries
GET /systems/<site_id>/hems_power_timeseries?device-uid=<site_id>_HEAT_PUMP_1
Headers:
  Accept: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns heat-pump consumption series as fixed-interval power buckets.

Example response shape (anonymized):
```json
{
  "heat_pump_consumption": [4.0, 0.0, 0.0, 4.0, 0.0, 120.0, 564.0, 0.0],
  "start_date": 1771628400
}
```
Observed structure:
- `heat_pump_consumption` is a numeric array (captured samples length `672`).
- `start_date` is an epoch-seconds anchor for the first bucket.
- The variant with `device-uid` returned the same key shape as the unfiltered call.

### 2.19 HEMS Lifetime Consumption (Heat Pump / EV / Water Heater)
```
GET /systems/<site_id>/hems_consumption_lifetime
Headers:
  Accept: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns long-window consumption flows; shape is compatible with site lifetime arrays and includes HEMS channels.

Example response shape (anonymized):
```json
{
  "system_id": 1234567,
  "start_date": "2025-01-01",
  "last_report_date": 1772108054,
  "update_pending": false,
  "production": [12000, 8300, 9000],
  "consumption": [7100, 13400, 15800],
  "evse": [0.0, 0.0, 1320.4],
  "heatpump": [0.0, 0.0, 412.0],
  "water_heater": [0.0, 0.0, 0.0]
}
```
Observed structure:
- `heatpump` can be populated even when generic `/pv/systems/<site_id>/lifetime_energy` previously returned it as empty.
- Values are numeric bucket totals; arrays align by index across flow types.

### 2.20 HEMS Device Event/Graph Views (Per Device UID)
```
GET /systems/<site_id>/iq_er/<device_uid>/events
GET /systems/<site_id>/iq_er/<device_uid>/events.json
GET /systems/<site_id>/heat_pump/<device_uid>/events
GET /systems/<site_id>/heat_pump/<device_uid>/events.json
GET /systems/<site_id>/heat_pump/<device_uid>/graphs
```
Observed usage:
- Observed when opening IQ Energy Router and heat-pump detail pages.
- Observed responses were HTTP `200` for router, SG Ready gateway, and energy meter device UIDs.
- In captured traces these were read-only page/data fetches; no corresponding control endpoint was observed for heat-pump actuation.

### 2.21 HEMS Live Stream Status Toggle (Monitoring Transport)
```
PUT https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/status
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
Body:
  {"livestream-enabled": true}
```
Controls HEMS live data streaming state, used for monitoring refresh behavior.

Observed behavior:
- Captured write payload was `{"livestream-enabled": true}`.
- A later mobile-app capture also showed `{"livestream-enabled": false}` returning `"data": {"enable": false}`.
- No heat-pump mode/setpoint/relay control payloads were observed in the same session; this endpoint appears transport-oriented rather than device-actuation control.

### 2.21.1 HEMS Live Stream Vitals Toggle
```
PUT https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/vitals
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
Body:
  {"livestream-enabled": true}
```
Controls the live "vitals" transport.

Example response (anonymized):
```json
{
  "type": "hems-site-details",
  "timestamp": "2026-03-08T09:07:19.756777654Z",
  "data": {
    "enable": true
  }
}
```

Observed behavior:
- This endpoint mirrors the same write payload as `/live-stream/status` but returns a small acknowledgement envelope.
- A later capture also showed disable semantics: request body `{"livestream-enabled": false}` returned `"data": {"enable": false}`.
- Treat the endpoint as transport control, not device actuation.

### 2.G Mobile/Web Shared Constants

### 2.22 Shared Constants Payload
```
GET https://enlighten-mobile-38d22.firebaseio.com/enho_constants.json
```
Returns a global JSON document consumed by the Enphase mobile/web clients for feature constants, localized support/store links,
minimum supported app versions, SKU catalogs, and similar static configuration.

Auth observations:
- Captured request did not include cookies or a bearer token.
- The observed `e-auth-token` header value was literal `null`, suggesting the endpoint is public or at least not session-bound.
- Response was HTTP `200 OK` with `Content-Type: application/json; charset=utf-8`.

Sanitization notes:
- Request metadata that is user- or environment-specific (IP address, browser user-agent, language preferences, exact response date) is intentionally omitted here.
- Internal company/account identifiers that do not affect integration behavior are redacted from examples.

Example response excerpt (anonymized):
```json
{
  "AI_SAVINGS_DATA": {
    "AI_SAVINGS_METRICS_LOWER_LIMIT": 0.1,
    "AI_SAVINGS_METRICS_UPPER_LIMIT": 0.1,
    "NEGATIVE_SAVINGS_LEARN_MORE_LINK": {
      "US": {
        "en": "https://support.enphase.com/s/article/why-is-my-electricity-bill-in-ai-optimization-profile-higher-than-expected"
      }
    }
  },
  "CONNECTIVITY_DATA": {
    "ENV_SPECIAL_CHARACTERS": ["#", "$", "&", "%", "£", "+", "=", "\"", "\\", "€"],
    "MIN_ESW_FOR_ENCODING": "D8.3.5314"
  },
  "ENPHASE_STORE": {
    "US": {
      "en": "https://store.enphase.com/storefront/en-us"
    }
  },
  "ENSTORE_CONSTANTS": {
    "ENPHASE_CARE_MAINTAINER_ID": {
      "production": [
        {
          "company_id": "<redacted>"
        }
      ]
    },
    "ENPHASE_CARE_SKUs": {
      "ANNUAL_SUBSCRIPTION": ["ENPH-CARE-ANNUAL-SOLAR", "ENPH-CARE"],
      "PLUS_SUBSCRIPTION": ["ENPH-CARE-TEN-SOLAR"]
    },
    "ONE_MIN_TELEMETRY_SKU": "ONE-MIN-TELEMETRY"
  },
  "IQCP_DATA": {
    "APP_VERSION": "4.1.0",
    "COMMAND_RETRIES": 3,
    "FW_VERSION": "2.0.0",
    "ITK_MIN_APP_VERSION_RED_COMMISSIONING": "4.8.4"
  }
}
```

Notable field groups observed:
- `AI_SAVINGS_DATA`: thresholds plus country/language-specific support article links.
- `CONNECTIVITY_DATA`: app-side validation constants for special characters and minimum software support.
- `ENPHASE_STORE` and `ENSTORE_CONSTANTS`: localized storefront links, SKU lists, titles, media base URLs, and feature toggles.
- `IQCP_DATA`: balcony solar / IQCP app and firmware compatibility values plus device naming strings.

Integration relevance:
- No charger telemetry, per-site configuration, or account-specific state was present in the captured payload.
- The document appears useful as a reference for feature-gating and catalog discovery, but not for live EV charger control/state.
- Because the payload is shared/global and not site-scoped, any future use in the integration should treat it as cacheable static metadata.

---

## 3. EV Charger Control Operations

Observed request variants differ across regions. All payloads shown below are the canonical request.

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

Observed AI Optimization variant:
```json
{
  "meta": {
    "serverTimeStamp": "<timestamp>"
  },
  "data": {
    "config": {
      "profile": "ai_optimisation",
      "modeSyncStatus": "synced",
      "stormActive": false,
      "isModeCancellable": true,
      "pendingModesOffGrid": false,
      "pendingSchedulesOffGrid": false
    },
    "modes": {
      "smartCharging": {
        "chargingMode": "SMART_CHARGING",
        "enabled": true,
        "showSettings": false,
        "isHidden": false,
        "isDisabledByStorm": false,
        "goalsEnabled": 0,
        "defaultGoalAdded": false
      },
      "scheduledCharging": {
        "chargingMode": "SCHEDULED_CHARGING",
        "enabled": false,
        "showSettings": false,
        "isHidden": false,
        "isDisabledByStorm": false,
        "schedulesEnabled": 0
      },
      "manualCharging": {
        "chargingMode": "MANUAL_CHARGING",
        "enabled": false,
        "showSettings": false,
        "isHidden": false,
        "isDisabledByStorm": false
      }
    }
  },
  "error": {}
}
```

### 4.2 Set Charge Mode
```
PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference
Body: { "mode": "MANUAL_CHARGING" }
Headers: Authorization: Bearer <token>
```
Success response mirrors the GET payload.

Observed mode values include `MANUAL_CHARGING`, `SCHEDULED_CHARGING`,
`GREEN_CHARGING`, and `SMART_CHARGING`.

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
- `USE_BATTERY_FOR_SELF_CONSUMPTION` was the observed setting name for battery supplementation in Green mode.
- Setting `enabled=false` disables battery supplementation; `value` remains `null`.
- Captured requests sent `loader=false`; the API accepts payloads without the `loader` key.

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
- Observed: captured PATCH requests may include `chargingLevel=100` and `chargingLevelAmp=null` for `CUSTOM` schedules; subsequent GETs may normalize back to `32/32`.
- Observed: captured PATCH requests include a top-level `"error": {}` field; the API accepts PATCH payloads without it.
---

## 5. BatteryConfig APIs (System Profile and Battery Controls)

The BatteryConfig service exposes system profile and EV charging mode endpoints.

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

Observed usage:
- MQTT WebSocket URL is `wss://<aws_iot_endpoint>/mqtt`.
- MQTT authentication is carried in the MQTT `username` field rather than the WebSocket query string:
  `?x-amz-customauthorizer-name=<aws_authorizer>&<aws_token_key>=<aws_token_value>&site-id=<site_id>&x-amz-customauthorizer-signature=<urlencoded aws_digest>&env=production`
- The integration uses MQTT 3.1.1 (`protocolVersion: 4`), no password, and `Origin: https://battery-profile-ui.enphaseenergy.com`.
- The returned `topic` is treated as the response stream subscription topic before issuing the matching battery-settings `PUT`.

### 5.2 Site Settings
```
GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>
```
Provides feature and availability flags for the battery profile service.

Example response (anonymized):
```json
{
  "type": "site-settings",
  "timestamp": "<timestamp>",
  "data": {
    "showProduction": true,
    "showConsumption": true,
    "hasEncharge": true,
    "hasAcb": false,
    "hasGenerator": false,
    "hasEnpower": true,
    "countryCode": "AU",
    "region": "AU",
    "locale": "en-AU",
    "ownerOrHostMaskedEmail": "u*******r@example.com",
    "timezone": "Australia/Melbourne",
    "showChargeFromGrid": false,
    "isEnsemble": true,
    "hasOjasDevice": false,
    "showSavingsMode": true,
    "showAiOptiSavingsMode": true,
    "siteStage": 5,
    "isEmea": false,
    "isDTSupported": false,
    "isIQGWScheduleSupported": true,
    "isDTEnabled": false,
    "isDTSite": false,
    "restrictCfg": false,
    "isHemsActivationPending": true,
    "isTariffNEM3": false,
    "tariffTypeId": {
      "importType": "tou",
      "exportType": "tou"
    },
    "isHemsAuthPending": false,
    "isHemsSite": false,
    "isNEM3Supported": false,
    "isNEM3Site": false,
    "calibrationProgress": false,
    "acceptedGICDisclaimer": false,
    "featureDetails": {
      "98daf27c08a25a0a": false,
      "3951a76025fc7d6c": false,
      "HEMS_EV_Custom_Schedule": true,
      "a3f9c1e7b82d4a6f": false,
      "554ed34b3f489c08": true,
      "a56e30d62ecc443f": true,
      "f3a9b7c4d681e25f": true,
      "Disable_Storm_Guard_Grid_Charging": false,
      "714ed62b9f489c98": true,
      "2e6W81086bd123F8": false,
      "ae9fc99447fa52a0": true,
      "734ed62b3f489c98": true,
      "b7def45a12d67e3f": false,
      "af356d446af0e3b4": false,
      "dtpfu43qwqxr5327": false,
      "b707fae2750a4965": true,
      "2e6W81086cd123F9": false,
      "671b7d2591d1adc1": true,
      "pm221c316d32ad3e": false,
      "00eb0092160e4279": false,
      "61df86665efcb677": false,
      "a8cbe12f09b34c1d": true,
      "534ed33b3f489c98": true,
      "66f7a67f71be52f7": true
    },
    "showStormGuard": true,
    "showCriticalAlertsOverride": false,
    "showVLS": true,
    "isIQ8site": false,
    "showFullBackup": true,
    "showBatteryBackupPercentage": true,
    "isCollarPresent": false,
    "isCollarInstalled": false,
    "userDetails": {
      "isHost": false,
      "isOwner": true,
      "isInstaller": false,
      "isMaintainer": false,
      "email": "u******r@example.com",
      "isDemoUser": false
    },
    "siteStatus": {
      "code": "normal",
      "text": "Normal",
      "severity": "warning"
    },
    "isChargingModesEnabled": true,
    "isUseBatteryForEVSESupported": false,
    "batteryGridMode": "ImportExport",
    "batteryLimitSupport": false,
    "isHemsOptScheduleSupported": true,
    "isChangePending": false,
    "isThermostatSmartModeAllowed": false
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
Updates the system profile and reserve percentage. Observed profile keys include
`self-consumption`, `cost_savings`, `backup_only`, and `ai_optimisation`.

Example payloads observed:
```json
{ "profile": "self-consumption", "batteryBackupPercentage": 10 }
```

```json
{
  "profile": "self-consumption",
  "batteryBackupPercentage": 20,
  "devices": [
    {
      "uuid": "<evse_uuid>",
      "profileConfig": "full",
      "chargeMode": "SMART",
      "deviceType": "iqEvse",
      "enable": false
    }
  ]
}
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

```json
{
  "profile": "ai_optimisation",
  "batteryBackupPercentage": 10,
  "operationModeSubType": "prioritize-energy",
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
- The AI Optimization flow uses the profile key `ai_optimisation` and also sends `operationModeSubType: "prioritize-energy"` with the EVSE device payload.
- Switching away from AI Optimization may still include an EVSE `devices` payload. In the observed AI -> Self-Consumption transition, the EVSE payload included `profileConfig: "full"` and `chargeMode: "SMART"`.
- After saving a mode change, the profile may remain in a pending state until the change takes effect. During this window, a separate cancel endpoint can be used.

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
Updates battery settings. Captured requests used partial payloads to change individual controls.

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
- Captured writes used `acceptedItcDisclaimer: true`, while subsequent reads returned a timestamp string; the backend normalizes the acknowledgement state internally.
- `veryLowSoc` drives the "Battery shutdown level" slider, clamped between `veryLowSocMin` and `veryLowSocMax`.
- Two equivalent write variants were observed:
  - REST-only flows use `PUT /batterySettings/<site_id>?source=enho&userId=<user_id>`.
  - MQTT-backed RBD flows on `supportsMqtt=true` systems use `PUT /batterySettings/<site_id>?userId=<user_id>` after opening the MQTT response stream.

### 5.6 Storm Guard Alert Status, Opt-Out, and Toggle
```
GET /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
```
Returns Storm Guard alert state and critical alert override status.

Example response with no active alerts (anonymized):
```json
{
  "criticalAlertsOverride": true,
  "stormAlerts": [],
  "criticalAlertActive": false
}
```

Example response with an active non-critical alert (anonymized):
```json
{
  "criticalAlertsOverride": true,
  "stormAlerts": [
    {
      "id": "<alert_id>",
      "name": "Severe Weather",
      "source": "<weather_provider>",
      "status": "active",
      "startTime": 1771895761000,
      "endTime": 1771920000000,
      "critical": false
    }
  ],
  "criticalAlertActive": false
}
```

```
PUT /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
Headers: X-XSRF-Token: <token>
Body: {
  "stormAlerts": [
    { "id": "<alert_id>", "name": "Severe Weather", "status": "opted-out" }
  ]
}
```
Opts out of a specific active Storm Guard alert.

Example response:
```json
{ "message": "success" }
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
- `evseStormEnabled` controls EV charging behavior during Storm Guard alerts.
- Alert opt-out uses `PUT /stormGuard/<site_id>/stormAlert` with `status: "opted-out"` per alert ID.
- Observed behavior: if that opt-out removes the last active Storm Alert and Storm Guard remains enabled, the system profile exits storm-driven Full Backup and returns to the normal configured profile.
- Once enabled, the profile automatically switches to Full Backup during severe weather alerts and reserves full battery capacity.

### 5.7 Third-Party Control Settings
```
GET /service/batteryConfig/api/v1/<site_id>/thirdPartyControlSettings
```
Returns additional battery-profile control settings.

Example response (anonymized):
```json
{
  "type": "third-party-control-settings",
  "timestamp": "2026-03-08T09:40:03.684055096Z",
  "data": {}
}
```

Observed structure:
- The captured site returned an empty `data` object, so field semantics remain unknown.

### 5.8 Battery Schedules
```
GET /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules
```
Returns the charge-from-grid / day-schedule configuration backing the Battery page.

Example response (anonymized):
```json
{
  "type": "BATTERY_SCHEDULES_CONFIG",
  "cfg": {
    "scheduleStatus": "active",
    "count": 1,
    "details": [
      {
        "createdBy": "<user_id>",
        "updatedBy": "<user_id>",
        "createdAt": "1769455003182",
        "updatedAt": "1769455103104",
        "scheduleId": "<schedule_uuid>",
        "timezone": "Region/City",
        "startTime": "20:00",
        "endTime": "05:00",
        "limit": 100,
        "scheduleType": "CFG",
        "scheduleStatus": "active",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "isDeleted": false,
        "isEnabled": false
      }
    ]
  },
  "dtg": {
    "scheduleStatus": "active",
    "count": 0
  },
  "rbd": {
    "scheduleStatus": "active",
    "count": 0
  },
  "anySchedulePending": false
}
```

Observed structure:
- `cfg`, `dtg`, and `rbd` are separate schedule families; only `cfg` contained `details[]` in the capture.
- The captured `days` field used numeric weekday values, but the exact weekday-to-number mapping was not explicit in the trace.
- `scheduleStatus` and per-entry `isEnabled` are separate flags; preserve both rather than collapsing them.

### 5.8.1 Create Battery Schedule
```
POST /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules
Headers: X-XSRF-Token: <token>
Body: {
  "timezone": "Region/City",
  "startTime": "20:00",
  "endTime": "05:00",
  "scheduleType": "CFG",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "limit": 100,
  "isEnabled": true
}
```
Creates a new battery schedule entry. The same endpoint is used for CFG, DTG, and RBD schedule creation and schedule restore flows.

Observed behavior:
- `scheduleType` is sent uppercase (`CFG`, `DTG`, `RBD`).
- `limit` is included for charge-oriented schedules and omitted for pure RBD recreate flows.
- `isEnabled` is optional on create; some clients rely on the backend default when omitted.
- `startTime` and `endTime` are `HH:MM` strings, while `days` uses the same numeric weekday array returned by `GET /schedules`.

### 5.8.2 Delete / Soft-Delete Variants
Two delete patterns were observed:

```
PUT /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>
Headers: X-XSRF-Token: <token>
Body: {
  "scheduleType": "CFG",
  "startTime": "20:00",
  "endTime": "05:00",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "timezone": "Region/City",
  "isEnabled": true,
  "isDeleted": true
}
```

```
POST /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>/delete
Headers: X-XSRF-Token: <token>
Body: {}
```

Observed behavior:
- Soft-delete is supported through the canonical `PUT /schedules/<schedule_id>` resource, sometimes with the full schedule echoed back and sometimes with only `{ "isDeleted": true }`.
- A `/delete` alias also exists. This path is useful as a compatibility note, but it was not present in the newer browser traces captured for this repository.

### 5.9 Battery Schedule Validation
```
POST /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/isValid
Body: { "scheduleType": "cfg", "forceScheduleOpted": true }
```
Performs server-side validation before enabling a battery schedule.

Example response (anonymized):
```json
{
  "isValid": true
}
```

Observed behavior:
- The validation call appeared immediately before enabling charge-from-grid scheduling.
- Only the lowercase request value `scheduleType: "cfg"` was observed, even though the stored schedule object used uppercase `scheduleType: "CFG"`.

### 5.10 Update Battery Schedule (In-Place PUT)
```
PUT /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>
Body: {
  "timezone": "Europe/Lisbon",
  "startTime": "02:00",
  "endTime": "08:00",
  "limit": 61,
  "scheduleType": "CFG",
  "days": [1, 2, 3, 4, 5, 6, 7]
}
```
Updates an existing battery schedule in place.  This is the endpoint used by
the Enlighten battery profile UI when the user modifies a CFG schedule.

**Headers** (same as other batteryConfig calls):
- `e-auth-token`: JWT session token
- `Username`: Enphase user ID
- `XSRF-TOKEN` / `BP-XSRF-Token`: Anti-CSRF tokens from cookies
- `x-xsrf-token`: XSRF token echoed in request header

Example response (anonymized):
```json
{
  "createdBy": "<user_id>",
  "updatedBy": "<user_id>",
  "createdAt": "1773435236162",
  "updatedAt": "1774227351728",
  "scheduleId": "<uuid>",
  "timezone": "Europe/Lisbon",
  "startTime": "02:00",
  "endTime": "08:00",
  "limit": 61,
  "scheduleType": "CFG",
  "scheduleStatus": "pending",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "isDeleted": false,
  "isEnabled": true
}
```

Observed behavior:
- The response includes a `scheduleStatus` field that transitions from `"pending"` to `"active"` once the Envoy acknowledges the change.
- While a schedule is pending, subsequent PUT requests are accepted by the cloud but may block at the integration level to prevent conflicting updates.
- `startTime` and `endTime` use `HH:MM` format (24-hour, no seconds).
- `limit` is the maximum SoC percentage (5-100).
- `days` is a 1-indexed day-of-week array (1=Monday through 7=Sunday).
- This replaces the delete+create pattern previously used for schedule modifications.

### 5.11 ITC Disclaimer Acknowledgement
```
POST /service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/<site_id>
Body: { "disclaimer-type": "itc" }
```
Records the regulatory disclaimer acknowledgement required before enabling charge-from-grid on eligible sites.

Example response (anonymized):
```json
{
  "message": "success"
}
```

Observed behavior:
- This call preceded `PUT /batterySettings/<site_id>` in the captured sequence.
- A subsequent battery-settings update used `acceptedItcDisclaimer: true`; later `GET /batterySettings/<site_id>` returned `acceptedItcDisclaimer` as a timestamp string, so the backend normalizes the acknowledgement internally.

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

Other observed response shapes include `success: false` and `isBlocked: true`.

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

### 6.4 JWT Session Token Retrieval
Two JWT retrieval paths were observed:

Primary path:
```
GET https://enlighten.enphaseenergy.com/app-api/jwt_token.json
```

Observed response:
```json
{
  "token": "<jwt>"
}
```

Fallback path:
```
GET https://enlighten.enphaseenergy.com/service/auth_ms_enho/api/v1/session/token
Headers:
  e-auth-token: <_enlighten_4_session cookie value>
  X-Requested-With: XMLHttpRequest
```

Observed response:
```json
{
  "token": "<jwt>"
}
```

Observed behavior:
- `jwt_token.json` is the simpler path and is the preferred retrieval path when available.
- `auth_ms_enho/api/v1/session/token` is a useful fallback when the primary endpoint fails but the authenticated `_enlighten_4_session` cookie is present.
- Clients can decode the JWT locally and refresh it roughly one hour before expiry.

### 6.5 Access Token
Some sites issue a JWT-like access token via `https://entrez.enphaseenergy.com/access_token`.

### 6.6 Headers Required by API Client
- `e-auth-token: <token>`
- `Cookie: <serialized cookie jar>` (must include session cookies like `_enlighten_session`, `X-Requested-With`, etc.)
- When available: `Authorization: Bearer <token>`
- Common defaults also send:
  - `Referer: https://enlighten.enphaseenergy.com/`
  - `X-Requested-With: XMLHttpRequest`

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
| `storages[].excluded` | Battery inclusion flag |
| `storages[].led_status` | Raw battery LED/runtime status code |
| `storages[].status` / `storages[].statusText` | Battery status code + display label |
| `storages[].last_report` | Epoch seconds for latest battery telemetry |
| `storages[].battery_soh` | Battery state-of-health percentage string |
| `included_count` / `excluded_count` | Active vs excluded battery counts in the payload |
| `device-uid` | Stable HEMS device identifier |
| `device-type` (HEMS) | HEMS device taxonomy values seen in captures: `IQ_ENERGY_ROUTER`, `IQ_GATEWAY`, `SG_READY_GATEWAY`, `ENERGY_METER`, `HEAT_PUMP` |
| `pairing-status` (HEMS) | Pairing state label (for example `PAIRED`) for router-attached ecosystem devices |
| `hems-device-id` / `hems-device-facet-id` | HEMS backend identifiers present on router/heat-pump stack members in `hems-devices` payloads |
| `heatpump-status` | App-facing heat-pump runtime state from `/heatpump/<device_uid>/state`; observed values: `IDLE`, `RUNNING` |
| `sg-ready-mode` | SG Ready operating mode label from `/heatpump/<device_uid>/state`; observed values: `MODE_2` (normal), `MODE_3` (recommended consumption / SG Ready on) |
| `vpp-sgready-mode-override` | SG Ready override label from `/heatpump/<device_uid>/state`; observed off-state value: `NONE` |
| `last-report-at` | ISO-8601 last telemetry timestamp from `/heatpump/<device_uid>/state` |

---

## 8. Error Handling & Rate Limiting
- HTTP 401 — credentials expired or invalid.
- HTTP 400/404/409/422 during control operations — validation failure or charger not ready/not plugged.
- Rate limiting presents as HTTP 429.

### 8.1 Cloud status codes (from the official v4 control API)
Enphase’s public “EV Charger Control” reference (https://developer-v4.enphase.com/docs.html) documents the same backend actions behind a `/api/v4/systems/{system_id}/ev_charger/{serial_no}/…` surface. The status codes it lists match the JSON payloads observed on the cloud endpoints documented here. The most relevant responses are:

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

When these conditions occur against the `/service/evse_controller/...` paths, the response often uses an analogous JSON envelope with `"status": "error"` and the same `message`/`details`.

---

## 9. Known Variations & Open Questions
- Some deployments omit `displayName` from `/status`; summary v2 is needed for friendly names.
- Session energy units vary; values greater than `200` were observed as Wh while smaller values may be reported in kWh.
- Local LAN endpoints (`/ivp/pdm/*`, `/ivp/peb/*`) exist but require installer permissions; not currently accessible with owner accounts.
- `/service/batteryConfig/api/v1/<site_id>/thirdPartyControlSettings` returned an empty `data` object in the captured site, so its schema and feature flags remain unresolved.
- Captured cloud traces for the heat-pump stack were read-only (inventory/events/timeseries plus stream toggle); write-path behavior remains unresolved.

---

## 10. References
- Reverse-engineered from Enlighten mobile/web network traces (2024–2026).
