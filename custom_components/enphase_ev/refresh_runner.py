from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone as _tz
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_CHARGE_LEVEL_SETTING,
    DOMAIN,
    PHASE_SWITCH_CONFIG_SETTING,
)
from .log_redaction import redact_site_id, redact_text
from .refresh_plan import RefreshPlan, bind_refresh_plan, warmup_plan

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)


class CoordinatorRefreshRunner:
    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    async def _async_run_refresh_call(
        self,
        timing_key: str,
        log_label: str,
        callback_factory: Callable[[], object],
    ) -> tuple[str, float | None]:
        started = time.monotonic()
        try:
            result = callback_factory()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Skipping %s refresh for site %s: %s",
                log_label,
                redact_site_id(self.coordinator.site_id),
                redact_text(err, site_ids=(self.coordinator.site_id,)),
            )
        return timing_key, round(time.monotonic() - started, 3)

    async def _async_run_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        calls: tuple[tuple[str, str, Callable[[], object]], ...],
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if defer_topology:
            self.coordinator._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            results = await asyncio.gather(
                *(
                    self._async_run_refresh_call(
                        timing_key, log_label, callback_factory
                    )
                    for timing_key, log_label, callback_factory in calls
                )
            )
        finally:
            if defer_topology:
                self.coordinator._end_topology_refresh_batch()

        for timing_key, duration in results:
            if duration is not None:
                phase_timings[timing_key] = duration
        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def _async_run_ordered_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        calls: tuple[tuple[str, str, Callable[[], object]], ...],
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if defer_topology:
            self.coordinator._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            for timing_key, log_label, callback_factory in calls:
                key, duration = await self._async_run_refresh_call(
                    timing_key,
                    log_label,
                    callback_factory,
                )
                if duration is not None:
                    phase_timings[key] = duration
        finally:
            if defer_topology:
                self.coordinator._end_topology_refresh_batch()

        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def _async_run_staged_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        parallel_calls: tuple[tuple[str, str, Callable[[], object]], ...] = (),
        ordered_calls: tuple[tuple[str, str, Callable[[], object]], ...] = (),
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if not parallel_calls and not ordered_calls:
            if stage_key is not None:
                phase_timings[f"{stage_key}_s"] = 0.0
            return

        if defer_topology:
            self.coordinator._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            if parallel_calls:
                await self._async_run_refresh_calls(
                    phase_timings,
                    calls=parallel_calls,
                )
            if ordered_calls:
                await self._async_run_ordered_refresh_calls(
                    phase_timings,
                    calls=ordered_calls,
                )
        finally:
            if defer_topology:
                self.coordinator._end_topology_refresh_batch()

        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def _async_run_refresh_plan(
        self,
        phase_timings: dict[str, float],
        *,
        plan: RefreshPlan,
    ) -> None:
        bound_plan = bind_refresh_plan(self.coordinator, plan)
        for stage in bound_plan.stages:
            await self._async_run_staged_refresh_calls(
                phase_timings,
                stage_key=stage.stage_key,
                defer_topology=stage.defer_topology,
                parallel_calls=stage.parallel_calls,
                ordered_calls=stage.ordered_calls,
            )

    async def _async_startup_warmup_runner(self) -> None:
        coord = self.coordinator
        warmup_timings: dict[str, float] = {}
        coord._warmup_in_progress = True
        coord._warmup_last_error = None
        warmup_data = (
            {sn: dict(payload) for sn, payload in coord.data.items()}
            if isinstance(coord.data, dict)
            else {}
        )
        try:
            await self._async_run_refresh_plan(
                warmup_timings,
                plan=warmup_plan(warmup_data),
            )
            if warmup_data:
                coord.async_set_updated_data(warmup_data)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            coord._warmup_last_error = (
                redact_text(err, site_ids=(coord.site_id,)) or err.__class__.__name__
            )
            _LOGGER.debug(
                "Startup warmup failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
                exc_info=True,
            )
        finally:
            coord._warmup_in_progress = False
            coord._warmup_phase_timings = warmup_timings
            coord.discovery_snapshot.schedule_save()

    async def async_start_startup_warmup(self) -> None:
        coord = self.coordinator
        if coord._warmup_task is not None and not coord._warmup_task.done():
            return
        try:
            coord._warmup_task = coord.hass.async_create_task(
                self._async_startup_warmup_runner(),
                name=f"{DOMAIN}_warmup_{coord.site_id}",
            )
        except TypeError:
            coord._warmup_task = coord.hass.async_create_task(
                self._async_startup_warmup_runner()
            )

    async def _async_refresh_site_energy_for_warmup(self) -> None:
        coord = self.coordinator
        await coord.energy._async_refresh_site_energy()
        coord.discovery_snapshot.sync_site_energy_discovery_state()
        coord._sync_site_energy_issue()

    async def _async_refresh_evse_timeseries_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        coord = self.coordinator
        try:
            day_local = dt_util.as_local(dt_util.now())
        except Exception:
            day_local = datetime.now(tz=_tz.utc)
        await coord.evse_timeseries.async_refresh(day_local=day_local)
        target = working_data
        if target is None and isinstance(coord.data, dict) and coord.data:
            target = {sn: dict(payload) for sn, payload in coord.data.items()}
        if target:
            coord.evse_timeseries.merge_charger_payloads(target, day_local=day_local)
            if working_data is None:
                coord.async_set_updated_data(target)

    async def _async_refresh_session_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        coord = self.coordinator
        target = working_data if working_data is not None else coord.data
        if not isinstance(target, dict) or not target:
            return
        try:
            day_ref = dt_util.as_local(dt_util.now())
        except Exception:
            day_ref = datetime.now(tz=_tz.utc)
        updates = await coord._async_enrich_sessions(
            target.keys(),
            day_ref,
            in_background=False,
        )
        if not updates:
            return
        merged = (
            target
            if working_data is not None
            else {sn: dict(payload) for sn, payload in target.items()}
        )
        for sn, sessions in updates.items():
            payload = merged.get(sn)
            if payload is None:
                continue
            payload["energy_today_sessions"] = sessions
            payload["energy_today_sessions_kwh"] = coord._sum_session_energy(sessions)
        if working_data is None:
            coord.async_set_updated_data(merged)
        coord._sync_session_history_issue()

    async def _async_refresh_secondary_evse_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        coord = self.coordinator
        target = working_data if working_data is not None else coord.data
        if not isinstance(target, dict) or not target:
            return
        serials = [sn for sn in coord.iter_serials() if sn]
        if not serials:
            return
        charge_modes = await coord.evse_runtime.async_resolve_charge_modes(serials)
        green_settings = await coord.evse_runtime.async_resolve_green_battery_settings(
            serials
        )
        auth_settings = await coord.evse_runtime.async_resolve_auth_settings(serials)
        charger_config = await coord.evse_runtime.async_resolve_charger_config(
            serials,
            keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
        )
        merged = (
            target
            if working_data is not None
            else {sn: dict(payload) for sn, payload in target.items()}
        )
        for sn in serials:
            payload = merged.get(sn)
            if payload is None:
                continue
            charge_mode_resolution = charge_modes.get(sn)
            charge_mode_value, charge_mode_source = coord._charge_mode_resolution_parts(
                charge_mode_resolution
            )
            if charge_mode_value:
                payload["charge_mode_pref"] = charge_mode_value
                if charge_mode_source is not None:
                    payload["charge_mode_pref_source"] = charge_mode_source
            if green_settings.get(sn) is not None:
                enabled, supported = green_settings[sn]
                payload["green_battery_supported"] = supported
                if supported:
                    payload["green_battery_enabled"] = enabled
            if auth_settings.get(sn) is not None:
                (
                    app_enabled,
                    rfid_enabled,
                    app_supported,
                    rfid_supported,
                ) = auth_settings[sn]
                payload["app_auth_supported"] = app_supported
                payload["rfid_auth_supported"] = rfid_supported
                payload["app_auth_enabled"] = app_enabled
                payload["rfid_auth_enabled"] = rfid_enabled
                if app_supported or rfid_supported:
                    values = [
                        value
                        for value in (app_enabled, rfid_enabled)
                        if value is not None
                    ]
                    payload["auth_required"] = any(values) if values else None
            config_values = charger_config.get(sn)
            if isinstance(config_values, dict):
                if PHASE_SWITCH_CONFIG_SETTING in config_values:
                    payload["phase_switch_config"] = config_values[
                        PHASE_SWITCH_CONFIG_SETTING
                    ]
                if DEFAULT_CHARGE_LEVEL_SETTING in config_values:
                    payload["default_charge_level"] = config_values[
                        DEFAULT_CHARGE_LEVEL_SETTING
                    ]
        if working_data is None:
            coord.async_set_updated_data(merged)
