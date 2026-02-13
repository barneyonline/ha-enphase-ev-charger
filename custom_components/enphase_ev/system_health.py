from __future__ import annotations

from datetime import datetime

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import BASE_URL, DOMAIN
from .runtime_data import get_runtime_data


@callback
def async_register(
    hass: HomeAssistant, register: system_health.RegisterSystemHealth
) -> None:
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant):
    # Report simple reachability and entry/site info
    entries = hass.config_entries.async_entries(DOMAIN)
    site_infos: list[dict[str, object]] = []

    for entry in entries:
        entry_site_id = entry.data.get("site_id")
        coord = None
        try:
            coord = get_runtime_data(hass, entry).coordinator
        except Exception:
            pass

        if coord and hasattr(coord, "collect_site_metrics"):
            metrics = coord.collect_site_metrics()
        else:
            session_ttl = None
            if coord:
                session_manager = getattr(coord, "session_history", None)
                if session_manager is not None:
                    session_ttl = getattr(session_manager, "cache_ttl", None)
                if session_ttl is None:
                    session_ttl = getattr(coord, "_session_history_cache_ttl", None)
            backoff_until = getattr(coord, "_backoff_until", None) if coord else None
            scheduler_backoff_until = (
                getattr(coord, "_scheduler_backoff_until", None) if coord else None
            )
            scheduler_backoff_ends = (
                getattr(coord, "_scheduler_backoff_ends_utc", None) if coord else None
            )
            if isinstance(scheduler_backoff_ends, datetime):
                scheduler_backoff_ends = scheduler_backoff_ends.isoformat()
            auth_backoff_ends = (
                getattr(coord, "_auth_settings_backoff_ends_utc", None)
                if coord
                else None
            )
            if isinstance(auth_backoff_ends, datetime):
                auth_backoff_ends = auth_backoff_ends.isoformat()
            session_manager = getattr(coord, "session_history", None) if coord else None
            session_backoff_ends = (
                getattr(session_manager, "service_backoff_ends_utc", None)
                if session_manager
                else None
            )
            if isinstance(session_backoff_ends, datetime):
                session_backoff_ends = session_backoff_ends.isoformat()
            energy_manager = getattr(coord, "energy", None) if coord else None
            energy_backoff_ends = (
                getattr(energy_manager, "service_backoff_ends_utc", None)
                if energy_manager
                else None
            )
            if isinstance(energy_backoff_ends, datetime):
                energy_backoff_ends = energy_backoff_ends.isoformat()
            metrics = {
                "site_id": entry_site_id,
                "last_success": (
                    coord.last_success_utc.isoformat()
                    if coord and coord.last_success_utc
                    else None
                ),
                "latency_ms": coord.latency_ms if coord else None,
                "last_error": getattr(coord, "_last_error", None) if coord else None,
                "backoff_active": bool(backoff_until and backoff_until > 0),
                "network_errors": (
                    getattr(coord, "_network_errors", None) if coord else None
                ),
                "http_errors": getattr(coord, "_http_errors", None) if coord else None,
                "phase_timings": coord.phase_timings if coord else {},
                "session_cache_ttl_s": session_ttl,
                "scheduler_available": (
                    getattr(coord, "scheduler_available", None) if coord else None
                ),
                "scheduler_last_error": (
                    getattr(coord, "scheduler_last_error", None) if coord else None
                ),
                "scheduler_failures": (
                    getattr(coord, "_scheduler_failures", None) if coord else None
                ),
                "scheduler_backoff_active": bool(
                    scheduler_backoff_until and scheduler_backoff_until > 0
                ),
                "scheduler_backoff_ends_utc": scheduler_backoff_ends,
                "auth_settings_available": (
                    getattr(coord, "auth_settings_available", None) if coord else None
                ),
                "auth_settings_last_error": (
                    getattr(coord, "auth_settings_last_error", None) if coord else None
                ),
                "auth_settings_failures": (
                    getattr(coord, "_auth_settings_failures", None) if coord else None
                ),
                "auth_settings_backoff_active": (
                    bool(getattr(coord, "_auth_settings_backoff_until", None))
                    if coord
                    else None
                ),
                "auth_settings_backoff_ends_utc": auth_backoff_ends,
                "session_history_available": (
                    getattr(session_manager, "service_available", None)
                    if coord
                    else None
                ),
                "session_history_last_error": (
                    getattr(session_manager, "service_last_error", None)
                    if coord
                    else None
                ),
                "session_history_failures": (
                    getattr(session_manager, "service_failures", None)
                    if coord
                    else None
                ),
                "session_history_backoff_active": (
                    getattr(
                        session_manager,
                        "service_backoff_active",
                        None,
                    )
                    if coord
                    else None
                ),
                "session_history_backoff_ends_utc": session_backoff_ends,
                "site_energy_available": (
                    getattr(energy_manager, "service_available", None)
                    if coord
                    else None
                ),
                "site_energy_last_error": (
                    getattr(energy_manager, "service_last_error", None)
                    if coord
                    else None
                ),
                "site_energy_failures": (
                    getattr(energy_manager, "service_failures", None) if coord else None
                ),
                "site_energy_backoff_active": (
                    getattr(energy_manager, "service_backoff_active", None)
                    if coord
                    else None
                ),
                "site_energy_backoff_ends_utc": energy_backoff_ends,
            }
        if metrics.get("site_id") is None and entry_site_id is not None:
            metrics["site_id"] = entry_site_id
        site_infos.append(metrics)

    primary = site_infos[0] if site_infos else {}
    can_reach_server = await system_health.async_check_can_reach_url(hass, BASE_URL)
    return {
        "site_count": len(site_infos),
        "site_id": primary.get("site_id"),
        "site_name": primary.get("site_name"),
        "site_ids": [info.get("site_id") for info in site_infos if info.get("site_id")],
        "site_names": [
            info.get("site_name") for info in site_infos if info.get("site_name")
        ],
        "can_reach_server": can_reach_server,
        "last_success": primary.get("last_success"),
        "latency_ms": primary.get("latency_ms"),
        "last_error": primary.get("last_error"),
        "scheduler_available": primary.get("scheduler_available"),
        "scheduler_last_error": primary.get("scheduler_last_error"),
        "scheduler_failures": primary.get("scheduler_failures"),
        "scheduler_backoff_active": primary.get("scheduler_backoff_active"),
        "scheduler_backoff_ends_utc": primary.get("scheduler_backoff_ends_utc"),
        "auth_settings_available": primary.get("auth_settings_available"),
        "auth_settings_last_error": primary.get("auth_settings_last_error"),
        "auth_settings_failures": primary.get("auth_settings_failures"),
        "auth_settings_backoff_active": primary.get("auth_settings_backoff_active"),
        "auth_settings_backoff_ends_utc": primary.get("auth_settings_backoff_ends_utc"),
        "session_history_available": primary.get("session_history_available"),
        "session_history_last_error": primary.get("session_history_last_error"),
        "session_history_failures": primary.get("session_history_failures"),
        "session_history_backoff_active": primary.get("session_history_backoff_active"),
        "session_history_backoff_ends_utc": primary.get(
            "session_history_backoff_ends_utc"
        ),
        "site_energy_available": primary.get("site_energy_available"),
        "site_energy_last_error": primary.get("site_energy_last_error"),
        "site_energy_failures": primary.get("site_energy_failures"),
        "site_energy_backoff_active": primary.get("site_energy_backoff_active"),
        "site_energy_backoff_ends_utc": primary.get("site_energy_backoff_ends_utc"),
        "last_failure_status": primary.get("last_failure_status"),
        "last_failure_description": primary.get("last_failure_description"),
        "backoff_active": primary.get("backoff_active"),
        "network_errors": primary.get("network_errors"),
        "http_errors": primary.get("http_errors"),
        "phase_timings": primary.get("phase_timings", {}),
        "session_cache_ttl_s": primary.get("session_cache_ttl_s"),
        "sites": site_infos,
    }
