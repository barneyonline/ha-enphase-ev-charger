# Enphase IQ EV Charger – Local Endpoints (Discovery Notes)

_The endpoints below were observed on IQ Gateway firmware 7.6.175 while inspecting the EV/managed load services. They currently require installer‑level permissions; owner sessions receive **401 Unauthorized**. These notes consolidate the paths so we can revisit once Enphase enables local access for owners._

## Base
- Local gateway origin (over HTTPS): `https://envoy.local` or `https://<gateway_ip>`
- Certificates are self-signed. Clients must either trust the gateway certificate or disable verification during discovery.
- Authentication: uses Enlighten session cookies with installer role. Owner cookies fail today.

## Primary EV Paths
| Method | Path | Purpose | Notes |
| --- | --- | --- | --- |
| `GET` | `/ivp/pdm/charger/<sn>/status` | Returns the same payload as the cloud `status` API (plugged, charging, session stats). | 401 for owner tokens; expected JSON when installer token present. |
| `GET` | `/ivp/pdm/charger/<sn>/summary` | Summarised charger metadata (model, firmware, voltage). Mirrors cloud summary v2 keys. | Observed via mobile app logs when using installer credentials. |
| `POST` | `/ivp/pdm/charger/<sn>/start_charging` | Starts charging / sets amps. Body matches cloud `chargingLevel` payloads. | Requires installer role; responds with `status: accepted` on success. |
| `POST` | `/ivp/pdm/charger/<sn>/stop_charging` | Stops active charging session. | Same semantics as cloud stop endpoint. |
| `POST` | `/ivp/pdm/charger/<sn>/trigger_message` | Triggers OCPP message. | Mirrors cloud implementation. |

## Managed Load / Scheduler
| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/ivp/peb/charger/<sn>/schedule` | Returns scheduler preferences (manual/green/scheduled). |
| `PUT` | `/ivp/peb/charger/<sn>/schedule` | Updates schedule or charge mode. Payload mirrors the Enlighten scheduler API. |

## Discovery Checklist
1. Query `/ivp/pdm/chargers` to enumerate available chargers.
2. For each serial, call `/ivp/pdm/charger/<sn>/status`.
3. If responses succeed, prefer local URLs in the integration; otherwise fall back to cloud.
4. For owner accounts, retry when Enphase releases firmware enabling owner role access.

## Open Questions
- Are there separate tokens for installer vs owner, or simply different cookie scopes?
- Does TLS client auth play a role for local EV endpoints?
- Can we obtain a local access token via Enlighten OAuth (similar to solar APIs)?

_These endpoints remain informational until Enphase grants owner-level access. The integration continues to rely on cloud APIs until then._
