# Enphase EV Charger 2 (Cloud) — Docs Skeleton

## Introduction
Integrates Enphase IQ EV Chargers via the Enlighten cloud. Provides sensors for charging session data, a control for charging amps, and actions to start/stop charging.

## Supported devices
- Enphase IQ EV Charger (cloud-connected). Regional availability may vary.

## Prerequisites
- Active Enlighten account and logged-in browser session to capture `e-auth-token` and `Cookie` headers.

## Configuration
- UI-based config flow: enter `Site ID`, charger serial(s), `e-auth-token`, and `Cookie`.

## Configuration options
- Scan interval (s)
- Fast/slow poll intervals (s)
- Prefer fast polling while streaming

## Supported functionality
- Sensors: Set Amps, Power, Session Energy, Duration, Connector Status, Cloud Latency, Last Update
- Numbers: Charging Amps (setpoint)
- Buttons: Start/Stop Charging
- Device triggers: Charging started/stopped, Plugged in/unplugged, Faulted
- Services: start_charging, stop_charging, trigger_message

## Data updates
- Polling by default (fast while charging/streaming, slow while idle). Temporary fast window after start/stop.

## Known limitations
- Cloud API may rate limit; polling slows during backoff.

## Troubleshooting
- 401 Unauthorized: refresh headers (Reauthenticate in Options).
- No data: confirm charger is visible in Enlighten and site ID is correct.

## Removing the integration
- Use standard HA removal; no additional steps.

