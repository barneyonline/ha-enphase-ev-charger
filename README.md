# Enphase Energy — Home Assistant Custom Integration

<!-- Badges -->
[![Release](https://img.shields.io/github/v/release/barneyonline/ha-enphase-energy?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-enphase-energy/releases)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-enphase-energy)](https://github.com/barneyonline/ha-enphase-energy/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-enphase-energy)](LICENSE)

[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-energy/tests.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-enphase-energy/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/barneyonline/ha-enphase-energy/graph/badge.svg?token=ichJ6LKzFK)](https://codecov.io/gh/barneyonline/ha-enphase-energy)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-energy/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-enphase-energy/actions/workflows/hassfest.yml)

[![Quality Scale](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-enphase-energy%2Fmain%2Fcustom_components%2Fenphase_ev%2Fmanifest.json&query=%24.quality_scale&label=quality%20scale&cacheSeconds=3600)](https://developers.home-assistant.io/docs/integration_quality_scale_index)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)

[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-enphase-energy)](https://github.com/barneyonline/ha-enphase-energy/issues)
![Development Status](https://img.shields.io/badge/development-active-success?style=flat-square)

[![Enphase Service Status](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.svg)](https://github.com/barneyonline/ha-enphase-energy/wiki/Service-Status-History)

Cloud-based Home Assistant integration for Enphase Energy systems.

> [!IMPORTANT]
> This is an unofficial community project. It is not affiliated with, endorsed by, or supported by Enphase Energy.
>
> The integration relies on undocumented Enphase APIs. Those APIs may change or stop working without notice, which can break features until the integration is updated.

## Supported device categories

- IQ Gateway / System Controller entities and controls
- IQ Battery telemetry and BatteryConfig controls (where supported)
- IQ EV Charger controls and session telemetry
- IQ Microinverter connectivity, inventory, and lifetime production telemetry
- Site and cloud energy telemetry (including supported HEMS channels such as Heat Pump and Water Heater lifetime energy)

## Key features

- Guided onboarding for site selection and device-category enablement
- Unified support for EV chargers, gateway, battery, and microinverter entities
- EV charging controls and session telemetry, including charge-mode aware behavior
- Heat-pump runtime status, connectivity, SG-Ready mode, power, and current-day consumption details sourced from HEMS endpoints
- Site and battery energy telemetry, including derived grid-import, grid-export, and battery power sensors for Home Assistant Energy Dashboard use
- Health diagnostics, service-availability tracking, and actionable repair issues
- Broad localization support across all user-facing integration strings

Localized strings cover English (default plus US, Canada, Australia, New Zealand, and Ireland variants), French, German, Spanish, Italian, Dutch, Swedish, Danish, Finnish, Norwegian Bokmal, Polish, Greek, Romanian, Czech, Hungarian, Bulgarian, Latvian, Lithuanian, Estonian, and Brazilian Portuguese.

## Screenshots

Screenshots below are from a mixed Enphase site and show multiple supported device categories.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/setup-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/setup-light.png">
  <img alt="Add integration flow showing category-based device selection (gateway, battery, EV chargers, and microinverters)" src="docs/images/setup-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/devices-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/devices-light.png">
  <img alt="Device overview showing Enphase entities grouped across battery, EV charger, gateway, microinverters, and cloud" src="docs/images/devices-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/gateway-controls-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/gateway-controls-light.png">
  <img alt="Gateway controls card with site operation controls" src="docs/images/gateway-controls-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/battery-controls-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/battery-controls-light.png">
  <img alt="Battery controls card with profile and reserve controls" src="docs/images/battery-controls-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/microinverters-sensors-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/microinverters-sensors-light.png">
  <img alt="Microinverter device sensors with per-inverter lifetime production telemetry" src="docs/images/microinverters-sensors-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/charger-controls-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/charger-controls-light.png">
  <img alt="EV charger controls card with charge mode, amps control, and charge actions" src="docs/images/charger-controls-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/cloud-sensors-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/cloud-sensors-light.png">
  <img alt="Cloud sensor entities with site-level energy and connectivity telemetry" src="docs/images/cloud-sensors-light.png">
</picture>

## Quick install (HACS)

1. HACS -> Integrations -> Enphase Energy
2. Install and restart Home Assistant
3. Add the integration and sign in

Manual install steps: see the wiki Installation page.

## Compatibility

- Minimum supported Home Assistant version is `2024.12.0` (Python `3.13`+).
- In v2.0.0, the integration display name changed to `Enphase Energy`.
- The integration domain remains `enphase_ev`, so existing entity IDs, automations, and scripts do not require migration.
- Users migrating from the core Enphase Envoy integration can optionally use the `Migrate Envoy history` assistant in the integration Options flow to take over compatible Energy-dashboard entity IDs. The assistant archives the migrated Envoy energy entities, swaps the entity IDs, and restores the remaining Envoy entities after the migration. Create a full Home Assistant backup first. Full steps: [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration).

## Authentication

Sign in with your Enlighten credentials; MFA is supported. See the wiki for details.

## Local Docker workflows

The repository now ships two separate Docker workflows:

- `ha-dev` is the pinned Python toolchain used for linting, formatting, tests, coverage, and other contributor checks.
- `ha-runtime` is a manual verification Home Assistant instance based on the official `ghcr.io/home-assistant/home-assistant:stable` image.

Build the contributor container:

```bash
docker compose -f devtools/docker/docker-compose.yml build ha-dev
```

Run the standard checks inside `ha-dev`:

```bash
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "ruff check ."
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "black custom_components/enphase_ev tests/components/enphase_ev"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest -q tests/components/enphase_ev"
```

Start a real Home Assistant instance for UI verification:

```bash
mkdir -p .ha-config
docker compose -f devtools/docker/docker-compose.yml up -d ha-runtime
```

Then open [http://localhost:8123](http://localhost:8123). The runtime container mounts:

- `.ha-config/` to `/config` for local Home Assistant state
- `custom_components/enphase_ev` to `/config/custom_components/enphase_ev` so the running instance uses this checkout

Notes:

- `.ha-config/` is gitignored and intended only for local verification data.
- `ha-runtime` inherits the `TZ` environment variable from your shell and defaults to `UTC` if it is unset.
- The runtime service uses port mapping instead of `network_mode: host` so it works reliably on Docker Desktop for macOS and Windows.
- If you need Linux-only host networking or hardware access for local discovery testing, adjust the runtime service locally with the options from the official Home Assistant Container docs.

## Documentation

Refer to the [Wiki](https://github.com/barneyonline/ha-enphase-energy/wiki), including [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration) for preserving Energy dashboard history when migrating from Enphase Envoy.

## Battery Scheduling Notes

- Battery schedule toggles and limits are exposed as `switch` and `number` entities.
- Battery schedule start and end values are exposed as separate `time` entities.
- In Home Assistant, those `time` entities may need to be added to dashboards manually if you want the schedule window visible on a card.
- Related battery schedule entities also expose the current schedule window and write-pending status as state attributes to make delayed Enphase cloud updates easier to diagnose.
