# Enphase EV Charger 2 (Cloud) — Home Assistant Custom Integration

[![Release](https://img.shields.io/github/v/release/barneyonline/ha-enphase-ev-charger?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-enphase-ev-charger/releases)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)
[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-ev-charger/tests.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-enphase-ev-charger/actions/workflows/tests.yml)
[![License](https://img.shields.io/github/license/barneyonline/ha-enphase-ev-charger)](LICENSE)
[![Quality Scale](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-enphase-ev-charger%2Fmain%2Fcustom_components%2Fenphase_ev%2Fmanifest.json&query=%24.quality_scale&label=quality%20scale&cacheSeconds=3600)](https://developers.home-assistant.io/docs/integration_quality_scale_index)

Cloud-based Home Assistant integration for the Enphase IQ EV Charger 2 using the same Enlighten endpoints as the Enphase mobile app.

## Key features

- Start/stop charging while respecting Manual/Scheduled/Green charge modes
- Set and persist charger current limits with automatic clamping
- Live plugged-in/charging state plus charger-problem status
- Power and last-session energy metrics without daily resets
- Connection diagnostics (interface, IP address, reporting cadence)

Localized strings cover English (default plus US, Canada, Australia, New Zealand, and Ireland variants), French, German, Spanish, Italian, Dutch, Swedish, Danish, Finnish, Norwegian Bokmal, Polish, Greek, Romanian, Czech, Hungarian, Bulgarian, Latvian, Lithuanian, Estonian, and Brazilian Portuguese.

## Screenshots

![Controls card showing charge mode, amps slider, and start/stop buttons](docs/images/controls.png)

![Sensors card with live session metrics and energy statistics](docs/images/sensors.png)

![Diagnostic card with connection status, connector state, and IP address](docs/images/diagnostic.png)

## Quick install (HACS)

1. HACS -> Integrations -> Enphase EV Charger 2 (Cloud)
2. Install and restart Home Assistant
3. Add the integration and sign in

Manual install steps: see the wiki Installation page.

## Authentication

Sign in with your Enlighten credentials; MFA is supported. See the wiki for details.

## Documentation

- Wiki home: https://github.com/barneyonline/ha-enphase-ev-charger/wiki
- Installation: https://github.com/barneyonline/ha-enphase-ev-charger/wiki/Installation
- Authentication: https://github.com/barneyonline/ha-enphase-ev-charger/wiki/Authentication
- Entities and Services: https://github.com/barneyonline/ha-enphase-ev-charger/wiki/Entities-and-Services
- Troubleshooting: https://github.com/barneyonline/ha-enphase-ev-charger/wiki/Troubleshooting
- Technical Reference: https://github.com/barneyonline/ha-enphase-ev-charger/wiki/Technical-Reference
