# Enphase IQ EV Charger 2 – Local Investigation Notes

## Embedded Zigbee Module
- Hardware inspection shows a **Digi XBee3 Zigbee** module inside the charger control board.
- XBee3 highlights (per Digi datasheet):
  - IEEE 802.15.4 / Zigbee 3.0 radio (2.4 GHz)
  - UART interface to host MCU
  - Programmable Micropython support
  - OTA firmware update capability
- Behaviour: module begins advertising/listening immediately after AC power-up or reboot, suggesting the charger polls for Zigbee commands or provisioning messages.
- Action item: sniff local Zigbee traffic during boot to confirm frame structure and potential control endpoints.

## OCPP Support
- Enphase documentation and mobile app logs indicate the charger implements **OCPP 2.0.1**.
- Current integration triggers OCPP messages via the cloud API; local initiation may be possible if direct connectivity (LAN or serial) is exposed.
- Known cloud-triggered messages: `MeterValues`, `StatusNotification`, others TBD.
- TODO: enumerate supported OCPP operations and evaluate whether the charger exposes a local WebSocket or serial bridge for OCPP communications.

## Next Steps
1. Capture Zigbee traffic during charger boot to identify command topics.
2. Investigate whether the gateway proxies OCPP over LAN (e.g., WebSocket) or only via cloud relay.
3. Catalogue OCPP message set (request/response) accepted by the charger.
