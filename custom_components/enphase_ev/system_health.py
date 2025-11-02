from __future__ import annotations

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import BASE_URL, DOMAIN
from .coordinator import EnphaseCoordinator


@callback
def async_register(
    hass: HomeAssistant, register: system_health.RegisterSystemHealth
) -> None:
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant):
    # Report simple reachability and entry/site info
    entries = hass.config_entries.async_entries(DOMAIN)
    hass_data = hass.data.get(DOMAIN, {})
    site_infos: list[dict[str, object]] = []

    for entry in entries:
        entry_site_id = entry.data.get("site_id")
        entry_data = hass_data.get(entry.entry_id, {})
        coord: EnphaseCoordinator | None = entry_data.get("coordinator")
        if coord and hasattr(coord, "collect_site_metrics"):
            metrics = coord.collect_site_metrics()
        else:
            backoff_until = getattr(coord, "_backoff_until", None) if coord else None
            metrics = {
                "site_id": entry_site_id,
                "last_success": (
                    coord.last_success_utc.isoformat()
                    if coord and coord.last_success_utc
                    else None
                ),
                "latency_ms": coord.latency_ms if coord else None,
                "last_error": getattr(coord, "_last_error", None)
                if coord
                else None,
                "backoff_active": bool(backoff_until and backoff_until > 0),
                "network_errors": getattr(coord, "_network_errors", None)
                if coord
                else None,
                "http_errors": getattr(coord, "_http_errors", None)
                if coord
                else None,
                "phase_timings": coord.phase_timings if coord else {},
                "session_cache_ttl_s": getattr(
                    coord, "_session_history_cache_ttl", None
                )
                if coord
                else None,
            }
        if metrics.get("site_id") is None and entry_site_id is not None:
            metrics["site_id"] = entry_site_id
        site_infos.append(metrics)

    primary = site_infos[0] if site_infos else {}
    return {
        "site_count": len(site_infos),
        "site_id": primary.get("site_id"),
        "site_name": primary.get("site_name"),
        "site_ids": [info.get("site_id") for info in site_infos if info.get("site_id")],
        "site_names": [info.get("site_name") for info in site_infos if info.get("site_name")],
        "can_reach_server": system_health.async_check_can_reach_url(hass, BASE_URL),
        "last_success": primary.get("last_success"),
        "latency_ms": primary.get("latency_ms"),
        "last_error": primary.get("last_error"),
        "last_failure_status": primary.get("last_failure_status"),
        "last_failure_description": primary.get("last_failure_description"),
        "backoff_active": primary.get("backoff_active"),
        "network_errors": primary.get("network_errors"),
        "http_errors": primary.get("http_errors"),
        "phase_timings": primary.get("phase_timings", {}),
        "session_cache_ttl_s": primary.get("session_cache_ttl_s"),
        "sites": site_infos,
    }
