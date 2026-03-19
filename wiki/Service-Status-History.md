# Service Status History

- Current status: **Down**
- Last updated: `2026-03-19 20:56 UTC`
- Failed checks in latest run: `4`
- Latest failed checks: charger_status, scheduler_charge_mode, scheduler_green_settings, scheduler_schedules
- Retained hourly samples: `257`
- Incident windows in last 30 days: `2`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-02-17T20:56:20, 0ms
    Window end :vert, window-end, 2026-03-19T20:56:20, 0ms
    section Down
    Down 1 (2026-03-19 2056 UTC) :crit, down-1, 2026-03-19T20:56:20, 60m
    section Degraded
    Degraded 1 (2026-03-12 1650 UTC) :active, degraded-1, 2026-03-12T16:50:43, 60m
```

## Incident Summary

| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |
| --- | --- | --- | --- | --- |
| Degraded | 2026-03-12 16:50 UTC | 2026-03-12 17:42 UTC | 51m | site_discovery_1 |
| Down | 2026-03-19 20:56 UTC | Ongoing (last seen 2026-03-19 20:56 UTC) | Observed at latest check | charger_status, scheduler_charge_mode, scheduler_green_settings, scheduler_schedules |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

