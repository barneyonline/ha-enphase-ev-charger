# Test Plan

## Fixtures
- `status_idle.json` — pluggedIn true, charging false
- `status_charging.json` — pluggedIn true, charging true, e_c increasing
- `start_charging_accepted.json`
- `stop_charging_accepted.json`

## Unit Tests
- Client: parses status, handles 401, raises on 5xx
- Coordinator: maps `evChargerData` to entity states; handles missing serial
- Entities: services invoke client and refresh
- Config flow: validates headers by pinging `/status`

## Integration Test
- Record + replay `aiohttp` responses (pytest + respx/aioresponses)
- Ensure entities appear with correct device info and states
