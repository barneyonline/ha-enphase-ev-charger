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
    first = entries[0] if entries else None
    site_id = first.data.get("site_id") if first else None
    coord: EnphaseCoordinator | None = None
    if entries:
        entry_data = hass.data.get(DOMAIN, {}).get(entries[0].entry_id, {})
        coord = entry_data.get("coordinator")

    return {
        "site_id": site_id,
        "can_reach_server": system_health.async_check_can_reach_url(hass, BASE_URL),
        "last_success": (
            coord.last_success_utc.isoformat()
            if coord and coord.last_success_utc
            else None
        ),
        "latency_ms": coord.latency_ms if coord else None,
        "last_error": getattr(coord, "_last_error", None) if coord else None,
        "backoff_active": bool(
            getattr(coord, "_backoff_until", None) and coord._backoff_until > 0
        ),
        "network_errors": getattr(coord, "_network_errors", None) if coord else None,
        "http_errors": getattr(coord, "_http_errors", None) if coord else None,
        "phase_timings": coord.phase_timings if coord else {},
        "session_cache_ttl_s": (
            getattr(coord, "_session_history_cache_ttl", None) if coord else None
        ),
    }
