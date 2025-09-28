# Enphase IQ EV Charger – Local Endpoints (Discovery Notes)

_The endpoints below were observed on IQ Gateway firmware 7.6.175 while inspecting the EV/managed load services. They currently require installer‑level permissions; owner sessions receive **401 Unauthorized**. These notes consolidate the paths so we can revisit once Enphase enables local access for owners._

## Base
- Local gateway origin (over HTTPS): `https://envoy.local` or `https://<gateway_ip>`
- Certificates are self-signed. Clients must either trust the gateway certificate or disable verification during discovery.
- Authentication: uses Enlighten session cookies with installer role. Owner cookies fail today.

## Primary EV Paths
| Method | Path | Purpose | Notes |
| --- | --- | --- | --- |
| `GET` | `/ivp/pdm/charger/<sn>/status` | Assumed: mirrors the cloud `status` API (plugged, charging, session stats). | 401 for owner tokens; expected JSON when installer token present. |
| `GET` | `/ivp/pdm/charger/<sn>/summary` | Assumed: charger metadata (model, firmware, voltage) similar to cloud summary v2. | Observed via mobile app logs when using installer credentials. |
| `POST` | `/ivp/pdm/charger/<sn>/start_charging` | Assumed: starts charging / sets amps using the same payload as cloud. | Requires installer role; responds with `status: accepted` on success. |
| `POST` | `/ivp/pdm/charger/<sn>/stop_charging` | Assumed: stops active charging session. | Same semantics as cloud stop endpoint. |
| `POST` | `/ivp/pdm/charger/<sn>/trigger_message` | Assumed: initiates the same OCPP message payloads as the cloud API. | Mirrors cloud implementation. |

## Managed Load / Scheduler
| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/ivp/peb/charger/<sn>/schedule` | Returns scheduler preferences (manual/green/scheduled). |
| `PUT` | `/ivp/peb/charger/<sn>/schedule` | Assumed: updates schedule or charge mode using same payload as cloud scheduler API. |

## /ivp/peb Namespace Observations
- Local endpoints under `/ivp/peb/ev_charger`, `/ivp/peb/ev_chargers`, `/ivp/peb/evse`, `/ivp/peb/energy_router`, and `/ivp/peb/managed_loads` respond with **401 Unauthorized** when using owner credentials.
- Examples include `/ivp/peb/ev_charger/<sn>/status`, `/metrics`, `/power`, `/sessions`, `/start_live_stream`, `/stop_live_stream`, `/summary`, etc.
- Presence of these endpoints (no 404) confirms the charger exposes full EVSE surfaces locally, but access is restricted to higher roles (installer).

## Discovery Checklist
1. Query `/ivp/pdm/chargers` to enumerate available chargers (requires elevated auth).
2. For each serial, probe `/ivp/pdm/charger/<sn>/status` to confirm role access.

_Note: Current integration does not attempt local endpoints; discovery steps are for future research._


## Observed Network Footprint

### mDNS Advertisement Details
- Hostname: `iq-evse-<serial>.local` (mDNS).
- Service type: `_workstation._tcp.local`; points to the discard port (9).
- SRV record resolves to charger LAN IP (e.g., `192.168.1.189`) but no user-facing service.
- SSH (port 22) reachable but secured; service banner hidden.
- No HTTP/HTTPS services detected via port scans.
- mDNS advertises `iq-evse-<serial>.local` on `_workstation._tcp` (port 9 discard service).

### Mobile App Connectivity Notes
- Enphase mobile app has been observed controlling the charger on LAN even without a gateway, implying a private encrypted channel.
- Likely uses a mutual-auth TCP/TLS or UDP protocol on a non-standard port; only bundled clients are accepted.
- These unpublished endpoints were not exposed during nmap scanning and need further analysis.

## Open Questions
- Are there separate tokens for installer vs owner, or simply different cookie scopes?
- Does TLS client auth play a role for local EV endpoints?
- Can we obtain a local access token via Enlighten OAuth (similar to solar APIs)?

_These endpoints remain informational until Enphase grants owner-level access. The integration continues to rely on cloud APIs until then._


## Additional Namespaces
- `/ivp/ensemble/*`: returns empty arrays (e.g., `{"serial_nums":[]}`); inventory not exposed for owner accounts.
- `/ivp/ocpp/*`: endpoints exist but respond with `{"error":"404 - Not Found"}` to owner tokens, indicating role gating.
- `/ivp/pdm/*`: full charger namespace (charger, chargers, connectors, devices, eir) present; GET returns 401, OPTIONS responds 204, confirming endpoints exist but require elevated credentials.


