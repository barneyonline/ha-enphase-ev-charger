# Coordinator & Entities (sketch)

- DataUpdateCoordinator ticks every `scan_interval` seconds.
- `async_update_data` calls `client.get_status()`.
- For each charger in `serials`:
  - pick object from `evChargerData` where `sn == serial`
  - map to `ChargerState` dataclass
  - update entities

- Control entities call `client.start_charging(...)` or `client.stop_charging(...)` then request an immediate refresh.

**Dataclass**
```python
@dataclass
class ChargerState:
    sn: str
    name: str
    connected: bool
    plugged: bool
    charging: bool
    faulted: bool
    connector_status: str
    session_kwh: float | None
    session_start: int | None
```
