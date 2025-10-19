# OCPP Message responses

- Heartbeat: Success
Request:
'action: enphase_ev.trigger_message
target:
  device_id: <deviceID>
data:
  requested_message: Heartbeat'
Response:
'results:
  - device_id: <deviceID>
    serial: "<serial>"
    site_id: "<siteID>"
    response:
      meta:
        serverTimeStamp: 1759038020185
      data:
        status: accepted
      error: {}'

- BootNotification: Success
As above

- StatusNotification: Success
As above

- FirmwareStatusNotification: Success
As above

- LogStatusNotification: Fail

- MeterValues: Success

- TransactionEvent: Fail

- DisplayMessages: Fail