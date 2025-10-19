# Entities & Services

## Entities (per charger)
- `binary_sensor.enphase_ev_<sn>_plugged_in` — on/off
- `binary_sensor.enphase_ev_<sn>_charging` — on/off
- `binary_sensor.enphase_ev_<sn>_faulted` — on/off
- `sensor.enphase_ev_<sn>_power` — W (if provided; else None)
- `sensor.enphase_ev_<sn>_session_energy` — kWh (`session_d.e_c`)
- `sensor.enphase_ev_<sn>_connector_status` — enum: AVAILABLE/CHARGING/etc.
- `sensor.enphase_ev_<sn>_charging_level` — A (from last command or status if present)
- `sensor.enphase_ev_<sn>_session_duration` — minutes (derived from start_time)
- `button.enphase_ev_<sn>_start_charging`
- `button.enphase_ev_<sn>_stop_charging`
- `number.enphase_ev_<sn>_charging_amps` — min/max as per charger capabilities (default 6..40)

## Services

### `enphase_ev.start_charging`
```yaml
target:
  device_id: <device>
data:
  charging_level: 32
  connector_id: 1
```

### `enphase_ev.stop_charging`
```yaml
target:
  device_id: <device>
```

### `enphase_ev.trigger_message`
```yaml
target:
  device_id: <device>
data:
  requested_message: "MeterValues"
```

### `enphase_ev.start_live_stream` / `enphase_ev.stop_live_stream`
```yaml
target:
  entity_id: sensor.enphase_ev_<sn>_power
```
