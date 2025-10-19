# Enphase EV Charger 2 — Home Assistant Custom Integration (Cloud)

**Status:** design brief for an agent to implement a working custom integration using the **Enlighten cloud** EVSE endpoints.  
**Updated:** 2025-09-07T09:56:20Z

This integration surfaces **Enphase IQ EV Charger 2** telemetry and controls in Home Assistant using the same endpoints the mobile app calls.

## Why cloud (not local)?
On IQ Gateway firmware **7.6.175** with an **owner** token, EV/managed‑load endpoints under `/ivp/pdm/*` and `/ivp/peb/*` return **401** (role‑gated). The only reliable route today is the **Enlighten cloud** endpoints used by the mobile app.

## Capabilities
- Read charger status (plugged, charging, faulted, power, energy, session)
- Start/Stop charging; set charging level (amps)
- Optional: start/stop a cloud “live stream” for faster updates
- Expose HA entities: sensors, binary sensors, number (amps), buttons/services

## Deliverables
- A working **custom_component** folder `custom_components/enphase_ev/`
- Config Flow: setup via YAML or UI (paste **site_id**, **charger SN**, and **auth headers** captured from session)
- Client library wrapping the cloud endpoints
- Update coordinator with polling (default 15s; fallback 30s)
- Entities + services + diagnostics
- Robust error handling & retry; rate‑limit friendly
- Unit tests with recorded fixtures
- Documentation (`README`, examples)
