# OCPP Trigger Messages

The `enphase_ev.trigger_message` service asks the Enphase cloud EVSE endpoint to request a specific OCPP message from a charger.

Supported `requested_message` values:

- `Heartbeat`
- `MeterValues`
- `StatusNotification`
- `BootNotification`
- `DiagnosticsStatusNotification`
- `FirmwareStatusNotification`

Advanced messages require `confirm_advanced: true`:

- `BootNotification`
- `DiagnosticsStatusNotification`
- `FirmwareStatusNotification`

Example:

```yaml
action: enphase_ev.trigger_message
target:
  device_id: <deviceID>
data:
  requested_message: MeterValues
```

Advanced example:

```yaml
action: enphase_ev.trigger_message
target:
  device_id: <deviceID>
data:
  requested_message: DiagnosticsStatusNotification
  confirm_advanced: true
```

Example response:

```yaml
results:
  - device_id: <deviceID>
    serial: "<serial>"
    site_id: "<siteID>"
    response:
      meta:
        serverTimeStamp: 1759038020185
      data:
        status: accepted
      error: {}
```

Previously observed unsupported message names include `LogStatusNotification`, `TransactionEvent`, and `DisplayMessages`. Unsupported or malformed values are rejected before any cloud request is sent.
