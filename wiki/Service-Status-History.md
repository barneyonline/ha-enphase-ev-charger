# Service Status History

- Current status: **Fully Operational**
- Last updated: `2026-04-12 14:08 UTC`
- Failed checks in latest run: `1`
- Latest failed checks: battery_config
- Retained hourly samples: `577`
- Incident windows in last 30 days: `6`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-03-13T14:08:51, 0ms
    Window end :vert, window-end, 2026-04-12T14:08:51, 0ms
    section Down
    Down 1 (2026-03-19 2056 UTC) :crit, down-1, 2026-03-19T20:56:20, 145m
    Down 2 (2026-03-25 0943 UTC) :crit, down-2, 2026-03-25T09:43:28, 111m
    Down 3 (2026-04-01 0145 UTC) :crit, down-3, 2026-04-01T01:45:09, 60m
    section Degraded
    Degraded 1 (2026-04-04 1635 UTC) :active, degraded-1, 2026-04-04T16:35:17, 60m
    Degraded 2 (2026-04-04 2030 UTC) :active, degraded-2, 2026-04-04T20:30:40, 60m
    Degraded 3 (2026-04-06 0742 UTC) :active, degraded-3, 2026-04-06T07:42:09, 85m
```

## Incident Summary

| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |
| --- | --- | --- | --- | --- |
| Down | 2026-03-19 20:56 UTC | Unknown after last seen 2026-03-19 23:21 UTC | Observed 2h 25m | charger_status, scheduler_charge_mode, scheduler_green_settings, scheduler_schedules |
| Down | 2026-03-25 09:43 UTC | 2026-03-25 11:34 UTC | 1h 51m | charger_status, scheduler_charge_mode, scheduler_green_settings, scheduler_schedules |
| Down | 2026-04-01 01:45 UTC | Unknown after last seen 2026-04-01 01:45 UTC | Observed 0m | battery_config, evse_runtime, evse_scheduler |
| Degraded | 2026-04-04 16:35 UTC | 2026-04-04 17:28 UTC | 53m | battery_config, evse_scheduler, inventory, site_energy |
| Degraded | 2026-04-04 20:30 UTC | 2026-04-04 21:30 UTC | 59m | battery_config, evse_scheduler |
| Degraded | 2026-04-06 07:42 UTC | 2026-04-06 09:08 UTC | 1h 25m | battery_config, session_history |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

