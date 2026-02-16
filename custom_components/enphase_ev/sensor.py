from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter

from .const import DOMAIN, SAFE_LIMIT_AMPS
from .coordinator import EnphaseCoordinator
from .energy import SiteEnergyFlow
from .entity import EnphaseBaseEntity
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0

STATE_NONE = "none"
BATTERY_ENTITY_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_charge_level",
    "_status",
    "_health",
    "_cycle_count",
    "_last_reported",
)


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _type_available(coord: EnphaseCoordinator, type_key: str) -> bool:
    has_type_for_entities = getattr(coord, "has_type_for_entities", None)
    if callable(has_type_for_entities):
        return bool(has_type_for_entities(type_key))
    has_type = getattr(coord, "has_type", None)
    return bool(has_type(type_key)) if callable(has_type) else True


def _grid_control_site_applicable(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    has_enpower = getattr(coord, "battery_has_enpower", None)
    if has_encharge is True or has_enpower is True:
        return True
    if has_encharge is False and has_enpower is False:
        return False
    return _type_available(coord, "encharge")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    known_site_entity_keys: set[str] = set()
    known_serials: set[str] = set()
    known_battery_serials: set[str] = set()
    known_inverter_serials: set[str] = set()
    known_type_keys: set[str] = set()
    battery_registry_pruned = False
    inverter_registry_pruned = False

    def _battery_sensor_unique_id(serial: str, suffix: str) -> str:
        return f"{DOMAIN}_site_{coord.site_id}_battery_{serial}{suffix}"

    def _battery_sensor_unique_ids(serial: str) -> tuple[str, ...]:
        return tuple(
            _battery_sensor_unique_id(serial, suffix)
            for suffix in BATTERY_ENTITY_UNIQUE_SUFFIXES
        )

    def _battery_serial_from_unique_id(unique_id: str) -> str | None:
        unique_prefix = f"{DOMAIN}_site_{coord.site_id}_battery_"
        if not unique_id.startswith(unique_prefix):
            return None
        if unique_id == f"{DOMAIN}_site_{coord.site_id}_battery_overall_status":
            return None
        for suffix in BATTERY_ENTITY_UNIQUE_SUFFIXES:
            if not unique_id.endswith(suffix):
                continue
            serial = unique_id[len(unique_prefix) : -len(suffix)]
            if serial:
                return serial
            return None
        return None

    def _inverter_lifetime_sensor_unique_id(serial: str) -> str:
        return f"{DOMAIN}_inverter_{serial}_lifetime_energy"

    @callback
    def _async_sync_site_entities() -> None:
        site_entities: list[SensorEntity] = []
        site_has_battery = _site_has_battery(coord)
        gateway_available = _type_available(coord, "envoy")
        battery_device_available = _type_available(coord, "encharge")
        microinverter_available = bool(getattr(coord, "include_inverters", True)) and (
            _type_available(coord, "microinverter")
        )

        def _add_site_entity(key: str, entity: SensorEntity) -> None:
            if key in known_site_entity_keys:
                return
            site_entities.append(entity)
            known_site_entity_keys.add(key)

        if gateway_available:
            _add_site_entity("site_last_update", EnphaseSiteLastUpdateSensor(coord))
            _add_site_entity("site_cloud_latency", EnphaseCloudLatencySensor(coord))
            _add_site_entity(
                "site_last_error_code", EnphaseSiteLastErrorCodeSensor(coord)
            )
            _add_site_entity(
                "site_backoff_ends", EnphaseSiteBackoffEndsSensor(coord)
            )
            _add_site_entity(
                "system_controller_inventory",
                EnphaseSystemControllerInventorySensor(coord),
            )
            _add_site_entity(
                "gateway_production_meter",
                EnphaseGatewayProductionMeterSensor(coord),
            )
            _add_site_entity(
                "gateway_consumption_meter",
                EnphaseGatewayConsumptionMeterSensor(coord),
            )
            _add_site_entity(
                "gateway_connectivity_status",
                EnphaseGatewayConnectivityStatusSensor(coord),
            )
            _add_site_entity(
                "gateway_connected_devices",
                EnphaseGatewayConnectedDevicesSensor(coord),
            )
            _add_site_entity(
                "gateway_last_reported",
                EnphaseGatewayLastReportedSensor(coord),
            )
            site_energy_specs: dict[str, tuple[str, str]] = {
                "solar_production": ("site_solar_production", "Site Solar Production"),
                "consumption": ("site_consumption", "Site Consumption"),
                "grid_import": ("site_grid_import", "Site Grid Import"),
                "grid_export": ("site_grid_export", "Site Grid Export"),
                "battery_charge": ("site_battery_charge", "Site Battery Charge"),
                "battery_discharge": ("site_battery_discharge", "Site Battery Discharge"),
            }
            for flow_key, (translation_key, name) in site_energy_specs.items():
                _add_site_entity(
                    f"site_energy_{flow_key}",
                    EnphaseSiteEnergySensor(coord, flow_key, translation_key, name),
                )
        if microinverter_available:
            _add_site_entity(
                "microinverter_connectivity_status",
                EnphaseMicroinverterConnectivityStatusSensor(coord),
            )
            _add_site_entity(
                "microinverter_reporting_count",
                EnphaseMicroinverterReportingCountSensor(coord),
            )
            _add_site_entity(
                "microinverter_last_reported",
                EnphaseMicroinverterLastReportedSensor(coord),
            )
        if _grid_control_site_applicable(coord) and (
            _type_available(coord, "enpower") or _type_available(coord, "envoy")
        ):
            _add_site_entity("grid_mode", EnphaseGridModeSensor(coord))
            _add_site_entity(
                "grid_control_status", EnphaseGridControlStatusSensor(coord)
            )
        if site_has_battery and battery_device_available:
            _add_site_entity("storm_alert", EnphaseStormAlertSensor(coord))
            _add_site_entity("battery_mode", EnphaseBatteryModeSensor(coord))
            _add_site_entity(
                "system_profile_status", EnphaseSystemProfileStatusSensor(coord)
            )
            _add_site_entity(
                "battery_overall_charge", EnphaseBatteryOverallChargeSensor(coord)
            )
            _add_site_entity(
                "battery_overall_status", EnphaseBatteryOverallStatusSensor(coord)
            )
            _add_site_entity(
                "battery_available_energy", EnphaseBatteryAvailableEnergySensor(coord)
            )
            _add_site_entity(
                "battery_available_power", EnphaseBatteryAvailablePowerSensor(coord)
            )
            _add_site_entity(
                "battery_inactive_microinverters",
                EnphaseBatteryInactiveMicroinvertersSensor(coord),
            )
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)

    @callback
    def _async_sync_type_inventory() -> None:
        keys = [
            key
            for key in getattr(coord, "iter_type_keys", lambda: [])()
            if key and key != "envoy" and key not in known_type_keys
        ]
        if not keys:
            return
        type_entities = [EnphaseTypeInventorySensor(coord, key) for key in keys]
        async_add_entities(type_entities, update_before_add=False)
        known_type_keys.update(keys)

    @callback
    def _async_sync_chargers() -> None:
        _async_sync_site_entities()
        _async_sync_batteries()
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        if not serials:
            return
        per_serial_entities = []
        site_has_battery = _site_has_battery(coord)
        for sn in serials:
            per_serial_entities.append(EnphaseEnergyTodaySensor(coord, sn))
            per_serial_entities.append(EnphaseConnectorStatusSensor(coord, sn))
            per_serial_entities.append(EnphaseElectricalPhaseSensor(coord, sn))
            per_serial_entities.append(EnphasePowerSensor(coord, sn))
            per_serial_entities.append(EnphaseChargingLevelSensor(coord, sn))
            per_serial_entities.append(EnphaseLastReportedSensor(coord, sn))
            per_serial_entities.append(EnphaseChargeModeSensor(coord, sn))
            per_serial_entities.append(EnphaseChargerAuthenticationSensor(coord, sn))
            per_serial_entities.append(EnphaseStatusSensor(coord, sn))
            per_serial_entities.append(EnphaseLifetimeEnergySensor(coord, sn))
            if site_has_battery:
                per_serial_entities.append(EnphaseStormGuardStateSensor(coord, sn))
            # The following sensors were removed due to unreliable values in most deployments:
            # Connector Reason, Schedule Type/Start/End, Session Miles, Session Plug timestamps
        if per_serial_entities:
            async_add_entities(per_serial_entities, update_before_add=False)
            known_serials.update(serials)

    @callback
    def _async_sync_batteries() -> None:
        nonlocal battery_registry_pruned
        _async_sync_type_inventory()
        site_has_battery = _site_has_battery(coord)
        if not site_has_battery or not _type_available(coord, "encharge"):
            current_serials: list[str] = []
        else:
            iter_batteries = getattr(coord, "iter_battery_serials", None)
            current_serials = (
                [sn for sn in iter_batteries() if sn] if callable(iter_batteries) else []
            )
        current_set = set(current_serials)

        if not battery_registry_pruned:
            for reg_entry in list(ent_reg.entities.values()):
                entry_domain = getattr(reg_entry, "domain", None)
                if entry_domain is None:
                    entry_domain = reg_entry.entity_id.partition(".")[0]
                if entry_domain != "sensor":
                    continue
                entry_platform = getattr(reg_entry, "platform", None)
                if entry_platform is not None and entry_platform != DOMAIN:
                    continue
                entry_config_id = getattr(reg_entry, "config_entry_id", None)
                if entry_config_id is not None and entry_config_id != entry.entry_id:
                    continue
                unique_id = reg_entry.unique_id or ""
                serial = _battery_serial_from_unique_id(unique_id)
                if serial is None:
                    continue
                if serial in current_set:
                    continue
                ent_reg.async_remove(reg_entry.entity_id)
                known_battery_serials.discard(serial)
            battery_registry_pruned = True

        removed_serials = known_battery_serials - current_set
        for serial in removed_serials:
            for unique_id in _battery_sensor_unique_ids(serial):
                entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
                if entity_id is not None:
                    ent_reg.async_remove(entity_id)
            known_battery_serials.discard(serial)

        known_battery_serials.intersection_update(current_set)

        serials = [sn for sn in current_serials if sn not in known_battery_serials]
        if serials:
            entities: list[SensorEntity] = []
            for sn in serials:
                entities.extend(
                    [
                        EnphaseBatteryStorageChargeSensor(coord, sn),
                        EnphaseBatteryStorageStatusSensor(coord, sn),
                        EnphaseBatteryStorageHealthSensor(coord, sn),
                        EnphaseBatteryStorageCycleCountSensor(coord, sn),
                        EnphaseBatteryStorageLastReportedSensor(coord, sn),
                    ]
                )
            async_add_entities(entities, update_before_add=False)
            known_battery_serials.update(serials)

    @callback
    def _async_sync_inverters() -> None:
        nonlocal inverter_registry_pruned
        _async_sync_type_inventory()
        current_serials = [
            sn for sn in getattr(coord, "iter_inverter_serials", lambda: [])() if sn
        ]
        current_set = set(current_serials)

        if not inverter_registry_pruned:
            unique_prefix = f"{DOMAIN}_inverter_"
            unique_suffix = "_lifetime_energy"
            for reg_entry in list(ent_reg.entities.values()):
                entry_domain = getattr(reg_entry, "domain", None)
                if entry_domain is None:
                    entry_domain = reg_entry.entity_id.partition(".")[0]
                if entry_domain != "sensor":
                    continue
                entry_platform = getattr(reg_entry, "platform", None)
                if entry_platform is not None and entry_platform != DOMAIN:
                    continue
                entry_config_id = getattr(reg_entry, "config_entry_id", None)
                if entry_config_id is not None and entry_config_id != entry.entry_id:
                    continue
                unique_id = reg_entry.unique_id or ""
                if not (
                    unique_id.startswith(unique_prefix) and unique_id.endswith(unique_suffix)
                ):
                    continue
                serial = unique_id[len(unique_prefix) : -len(unique_suffix)]
                if not serial or serial in current_set:
                    continue
                ent_reg.async_remove(reg_entry.entity_id)
                known_inverter_serials.discard(serial)
            inverter_registry_pruned = True

        removed_serials = known_inverter_serials - current_set
        for serial in removed_serials:
            entity_id = ent_reg.async_get_entity_id(
                "sensor", DOMAIN, _inverter_lifetime_sensor_unique_id(serial)
            )
            if entity_id is not None:
                ent_reg.async_remove(entity_id)
            known_inverter_serials.discard(serial)

        known_inverter_serials.intersection_update(current_set)

        serials = [sn for sn in current_serials if sn not in known_inverter_serials]
        if serials:
            entities = [EnphaseInverterLifetimeEnergySensor(coord, sn) for sn in serials]
            async_add_entities(entities, update_before_add=False)
            known_inverter_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    unsubscribe_type = coord.async_add_listener(_async_sync_type_inventory)
    entry.async_on_unload(unsubscribe_type)
    unsubscribe_batteries = coord.async_add_listener(_async_sync_batteries)
    entry.async_on_unload(unsubscribe_batteries)
    unsubscribe_inverters = coord.async_add_listener(_async_sync_inverters)
    entry.async_on_unload(unsubscribe_inverters)
    _async_sync_site_entities()
    _async_sync_type_inventory()
    _async_sync_batteries()
    _async_sync_chargers()
    _async_sync_inverters()


class _BaseEVSensor(EnphaseBaseEntity, SensorEntity):
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str, key: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name_suffix
        self._attr_unique_id = f"{DOMAIN}_{sn}_{key}"

    @property
    def native_value(self):
        return self.data.get(self._key)


class EnphaseElectricalPhaseSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "electrical_phase"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_electrical_phase"

    @staticmethod
    def _friendly_phase_mode(raw) -> tuple[str | None, object | None]:
        if raw is None:
            return None, None
        try:
            normalized = str(raw).strip()
        except Exception:  # noqa: BLE001
            return None, raw
        if not normalized:
            return None, None
        friendly: str | None = None
        try:
            n = int(normalized)
        except Exception:  # noqa: BLE001
            n = None
        if n == 1:
            friendly = "Single Phase"
        elif n == 3:
            friendly = "Three Phase"
        if friendly is None:
            friendly = normalized
        raw_out: object | None = normalized if isinstance(raw, str) else raw
        return friendly, raw_out

    @staticmethod
    def _as_bool(value) -> bool | None:
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def native_value(self):
        friendly, _ = self._friendly_phase_mode(self.data.get("phase_mode"))
        return friendly

    @property
    def extra_state_attributes(self):
        _, phase_raw = self._friendly_phase_mode(self.data.get("phase_mode"))
        return {
            "phase_mode_raw": phase_raw,
            "dlb_enabled": self._as_bool(self.data.get("dlb_enabled")),
            "dlb_active": self._as_bool(self.data.get("dlb_active")),
        }


@dataclass
class _LastSessionRestoreData(ExtraStoredData):
    """Persist last session metrics across restarts."""

    last_session_kwh: float | None
    last_session_wh: float | None
    last_session_start: float | None
    last_session_end: float | None
    session_key: str | None
    last_duration_min: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "last_session_kwh": self.last_session_kwh,
            "last_session_wh": self.last_session_wh,
            "last_session_start": self.last_session_start,
            "last_session_end": self.last_session_end,
            "session_key": self.session_key,
            "last_duration_min": self.last_duration_min,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "_LastSessionRestoreData":
        if not isinstance(data, dict):
            return cls(None, None, None, None, None, None)

        def _as_float(val):
            try:
                return float(val) if val is not None else None
            except Exception:  # noqa: BLE001
                return None

        def _as_int(val):
            try:
                return int(val) if val is not None else None
            except Exception:  # noqa: BLE001
                return None

        session_key = data.get("session_key")
        return cls(
            _as_float(data.get("last_session_kwh")),
            _as_float(data.get("last_session_wh")),
            _as_float(data.get("last_session_start")),
            _as_float(data.get("last_session_end")),
            str(session_key) if session_key is not None else None,
            _as_int(data.get("last_duration_min")),
        )


class EnphaseEnergyTodaySensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):
    """Expose the last charging session's energy as a sensor."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "last_session"
    _HISTORY_ATTR_KEYS = (
        "session_cost",
        "avg_cost_per_kwh",
        "cost_calculated",
        "session_cost_state",
        "manual_override",
        "charge_profile_stack_level",
        "session_id",
        "start",
        "end",
        "active_charge_time_s",
        "session_miles",
        "session_charge_level",
        "session_auth_status",
        "session_auth_type",
        "session_auth_identifier",
        "session_auth_token_present",
    )

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        # Preserve unique_id for continuity even though the semantics changed
        self._attr_unique_id = f"{DOMAIN}_{sn}_energy_today"
        self._attr_name = "Last Session"
        self._last_session_kwh: float | None = None
        self._last_session_wh: float | None = None
        self._last_session_start: float | None = None
        self._last_session_end: float | None = None
        self._last_duration_min: int | None = None
        self._session_key: str | None = None
        self._last_context: dict | None = None
        self._last_context_source: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_extra = await self.async_get_last_extra_data()
        extra_data = _LastSessionRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        self._last_session_kwh = extra_data.last_session_kwh
        self._last_session_wh = extra_data.last_session_wh
        self._last_session_start = extra_data.last_session_start
        self._last_session_end = extra_data.last_session_end
        self._session_key = extra_data.session_key
        self._last_duration_min = extra_data.last_duration_min
        if last_state:
            try:
                restored_val = float(last_state.state)
            except Exception:
                restored_val = None
            if restored_val is not None and restored_val >= 0:
                self._last_session_kwh = restored_val
            attrs = last_state.attributes or {}
            if self._session_key is None and attrs.get("session_key") is not None:
                try:
                    self._session_key = str(attrs["session_key"])
                except Exception:
                    self._session_key = None
            if self._last_duration_min is None and attrs.get("session_duration_min"):
                try:
                    self._last_duration_min = int(attrs.get("session_duration_min"))
                except Exception:
                    self._last_duration_min = None

    @staticmethod
    def _coerce_timestamp(value) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            cleaned = cleaned.replace("[UTC]", "").replace("Z", "+00:00")
            try:
                dt_val = datetime.fromisoformat(cleaned)
            except Exception:
                return None
            if dt_val.tzinfo is None:
                dt_val = dt_val.replace(tzinfo=timezone.utc)
            return dt_val.timestamp()
        return None

    @staticmethod
    def _coerce_energy(session_kwh, session_wh) -> tuple[float | None, float | None]:
        energy_kwh: float | None = None
        energy_wh: float | None = None
        if session_kwh is not None:
            try:
                energy_kwh = round(float(session_kwh), 3)
            except Exception:  # noqa: BLE001
                energy_kwh = None
        if energy_kwh is None and session_wh is not None:
            try:
                wh_val = float(session_wh)
            except Exception:  # noqa: BLE001
                wh_val = None
            if wh_val is not None:
                if wh_val > 200:
                    energy_kwh = round(wh_val / 1000.0, 3)
                    energy_wh = round(wh_val, 3)
                else:
                    energy_kwh = round(wh_val, 3)
                    energy_wh = round(wh_val * 1000.0, 3)
        if energy_kwh is not None and energy_wh is None:
            try:
                energy_wh = round(energy_kwh * 1000.0, 3)
            except Exception:  # noqa: BLE001
                energy_wh = None
        return energy_kwh, energy_wh

    def _extract_realtime_session(self, data: dict) -> dict:
        charging = bool(data.get("charging"))
        energy_kwh, energy_wh = self._coerce_energy(
            data.get("session_kwh"), data.get("session_energy_wh")
        )
        start = self._coerce_timestamp(data.get("session_start"))
        end = self._coerce_timestamp(data.get("session_end"))
        session_key = None
        if start is not None or end is not None:
            session_key = f"{start or 'none'}:{end or 'none'}"
        elif charging:
            session_key = "charging"

        return {
            "energy_kwh": energy_kwh,
            "energy_wh": energy_wh,
            "start": start,
            "end": end,
            "charging": charging,
            "plug_in_at": data.get("session_plug_in_at"),
            "plug_out_at": data.get("session_plug_out_at"),
            "session_charge_level": data.get("session_charge_level"),
            "session_cost": data.get("session_cost"),
            "session_miles": data.get("session_miles"),
            "session_key": session_key,
            "session_id": None,
            "active_charge_time_s": None,
            "avg_cost_per_kwh": None,
            "cost_calculated": None,
            "session_cost_state": None,
            "manual_override": None,
            "charge_profile_stack_level": None,
            "session_auth_status": data.get("session_auth_status"),
            "session_auth_type": data.get("session_auth_type"),
            "session_auth_identifier": data.get("session_auth_identifier"),
            "session_auth_token_present": data.get("session_auth_token_present"),
        }

    def _extract_history_session(self, data: dict) -> dict | None:
        sessions = data.get("energy_today_sessions") or []
        if not sessions:
            return None
        latest = sessions[-1]
        energy_kwh, energy_wh = self._coerce_energy(
            (
                latest.get("energy_kwh_total")
                if latest.get("energy_kwh_total") is not None
                else latest.get("energy_kwh")
            ),
            None,
        )
        start = self._coerce_timestamp(latest.get("start"))
        end = self._coerce_timestamp(latest.get("end"))
        session_id_raw = (
            latest.get("session_id")
            if latest.get("session_id") is not None
            else (
                latest.get("sessionId")
                if latest.get("sessionId") is not None
                else latest.get("id")
            )
        )
        session_key = None
        session_id = None
        if session_id_raw is not None:
            try:
                session_id = str(session_id_raw)
            except Exception:  # noqa: BLE001
                session_id = None
        if session_id is not None:
            session_key = session_id
        elif start is not None or end is not None:
            session_key = f"{start or 'none'}:{end or 'none'}"

        return {
            "energy_kwh": energy_kwh,
            "energy_wh": energy_wh,
            "start": start,
            "end": end,
            "charging": False,
            "plug_in_at": latest.get("start"),
            "plug_out_at": latest.get("end"),
            "session_charge_level": latest.get("session_charge_level"),
            "session_cost": latest.get("session_cost"),
            "session_miles": (
                latest.get("miles_added")
                if latest.get("miles_added") is not None
                else latest.get("range_added")
            ),
            "session_key": session_key,
            "session_id": session_id,
            "active_charge_time_s": latest.get("active_charge_time_s"),
            "avg_cost_per_kwh": latest.get("avg_cost_per_kwh"),
            "cost_calculated": latest.get("cost_calculated"),
            "session_cost_state": latest.get("session_cost_state"),
            "manual_override": latest.get("manual_override"),
            "charge_profile_stack_level": latest.get("charge_profile_stack_level"),
            "session_auth_status": latest.get("auth_status"),
            "session_auth_type": latest.get("auth_type"),
            "session_auth_identifier": latest.get("auth_identifier"),
            "session_auth_token_present": (
                bool(latest.get("auth_token")) if latest.get("auth_token") else False
            ),
        }

    @staticmethod
    def _compute_duration_minutes(
        start: float | None, end: float | None, charging: bool
    ) -> int | None:
        if start is None:
            return None
        if end is None and charging:
            end_ts = dt_util.utcnow().timestamp()
        elif end is None:
            return None
        else:
            end_ts = end
        try:
            duration = int((end_ts - start) / 60)
        except Exception:  # noqa: BLE001
            return None
        return max(0, duration)

    def _pick_session_context(self, data: dict) -> dict | None:
        realtime = self._extract_realtime_session(data)
        history = self._extract_history_session(data)

        has_realtime_energy = realtime and realtime.get("energy_kwh") is not None
        realtime_nonzero = bool(
            has_realtime_energy and (realtime.get("energy_kwh") or 0) > 0
        )
        realtime_idle_zero = bool(
            realtime
            and not realtime.get("charging")
            and (realtime.get("energy_kwh") or 0) == 0
        )
        if realtime and realtime["charging"]:
            self._last_context_source = "realtime"
            return realtime
        if history and history.get("energy_kwh") is not None:
            # When idle, prefer the richer session history payload.
            self._last_context_source = "history"
            return history
        if realtime and realtime_nonzero:
            self._last_context_source = "realtime"
            return realtime
        if has_realtime_energy and not realtime_idle_zero:
            self._last_context_source = "realtime"
            return realtime
        if realtime_idle_zero:
            if history:
                self._last_context_source = "history"
                return history
            self._last_context_source = None
            return None
        if history:
            self._last_context_source = "history"
            return history
        self._last_context_source = None
        return None

    def _merge_history_context(self, context: dict | None) -> dict:
        merged = dict(context or {})
        history = self._extract_history_session(self.data)
        if not history:
            return merged

        def _as_float(value):
            if value is None or isinstance(value, bool):
                return None
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None

        should_merge = self._last_context_source == "history"
        if not should_merge:
            context_key = merged.get("session_key")
            history_key = history.get("session_key")
            should_merge = (
                context_key is not None
                and history_key is not None
                and context_key == history_key
            )
        if not should_merge:
            ctx_start = _as_float(merged.get("start"))
            ctx_end = _as_float(merged.get("end"))
            hist_start = _as_float(history.get("start"))
            hist_end = _as_float(history.get("end"))
            if ctx_start is not None and hist_start is not None:
                if abs(ctx_start - hist_start) <= 1.0:
                    if ctx_end is None or hist_end is None:
                        should_merge = True
                    elif abs(ctx_end - hist_end) <= 1.0:
                        should_merge = True
            elif ctx_end is not None and hist_end is not None:
                if abs(ctx_end - hist_end) <= 1.0:
                    should_merge = True
        if should_merge:
            for key in self._HISTORY_ATTR_KEYS:
                value = history.get(key)
                if value is not None:
                    merged[key] = value
        return merged

    @property
    def native_value(self):
        context = self._pick_session_context(self.data) or {}
        self._last_context = context

        energy_kwh = context.get("energy_kwh")
        energy_wh = context.get("energy_wh")
        start = context.get("start")
        end = context.get("end")
        charging = bool(context.get("charging"))
        session_key = context.get("session_key")
        duration_min = self._compute_duration_minutes(start, end, charging)

        if energy_kwh is not None:
            try:
                energy_kwh = max(0.0, round(float(energy_kwh), 3))
            except Exception:  # noqa: BLE001
                energy_kwh = None
        if energy_wh is not None:
            try:
                energy_wh = max(0.0, round(float(energy_wh), 3))
            except Exception:  # noqa: BLE001
                energy_wh = None
        if energy_kwh is not None and energy_wh is None:
            try:
                energy_wh = round(energy_kwh * 1000.0, 3)
            except Exception:  # noqa: BLE001
                energy_wh = None

        if session_key and session_key != self._session_key:
            self._session_key = session_key
            if energy_kwh is not None:
                self._last_session_kwh = energy_kwh
            if energy_wh is not None or energy_kwh is not None:
                self._last_session_wh = energy_wh or (
                    round(energy_kwh * 1000.0, 3) if energy_kwh is not None else None
                )
            self._last_duration_min = duration_min
            self._last_session_start = start
            self._last_session_end = end
        else:
            if energy_kwh is not None:
                self._last_session_kwh = energy_kwh
            if energy_wh is not None:
                self._last_session_wh = energy_wh
            elif energy_kwh is not None:
                try:
                    self._last_session_wh = round(energy_kwh * 1000.0, 3)
                except Exception:  # noqa: BLE001
                    pass
            if duration_min is not None:
                self._last_duration_min = duration_min
            if start is not None:
                self._last_session_start = start
            if end is not None:
                self._last_session_end = end

        return self._last_session_kwh

    @property
    def extra_state_attributes(self):
        merged_context = self._merge_history_context(self._last_context)
        return self._session_metadata_attributes(
            self.data,
            hass=self.hass,  # type: ignore[arg-type]
            context=merged_context,
            energy_kwh=self._last_session_kwh,
            energy_wh=self._last_session_wh,
            duration_min=self._last_duration_min,
            session_key=self._session_key,
        )

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return _LastSessionRestoreData(
            last_session_kwh=self._last_session_kwh,
            last_session_wh=self._last_session_wh,
            last_session_start=self._last_session_start,
            last_session_end=self._last_session_end,
            session_key=self._session_key,
            last_duration_min=self._last_duration_min,
        )

    @staticmethod
    def _session_metadata_attributes(
        data: dict,
        hass=None,
        *,
        context: dict | None = None,
        energy_kwh: float | None = None,
        energy_wh: float | None = None,
        duration_min: int | None = None,
        session_key: str | None = None,
    ) -> dict[str, object]:
        """Derive session metadata attributes from the coordinator payload."""
        result: dict[str, object] = {}

        def _localize(value):
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)):
                    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                elif isinstance(value, str):
                    cleaned = value.strip()
                    if not cleaned:
                        return None
                    if cleaned.endswith("[UTC]"):
                        cleaned = cleaned[:-5]
                    if cleaned.endswith("Z"):
                        cleaned = cleaned[:-1] + "+00:00"
                    dt = datetime.fromisoformat(cleaned)
                else:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt_util.as_local(dt).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(value):
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "y")
            return None

        def _as_int(value):
            if value is None:
                return None
            try:
                return int(float(value))
            except Exception:  # noqa: BLE001
                return None

        def _as_float(value, *, precision: int | None = None):
            if value is None:
                return None
            try:
                out = float(value)
            except Exception:  # noqa: BLE001
                return None
            if precision is not None:
                try:
                    return round(out, precision)
                except Exception:  # noqa: BLE001
                    return out
            return out

        session_data = context or {}
        plug_in = _localize(
            session_data.get("plug_in_at") or data.get("session_plug_in_at")
        )
        plug_out = _localize(
            session_data.get("plug_out_at") or data.get("session_plug_out_at")
        )
        result["plugged_in_at"] = plug_in
        result["plugged_out_at"] = plug_out

        energy_kwh_val = energy_kwh
        energy_wh_val = energy_wh
        if energy_kwh_val is None or energy_wh_val is None:
            kwh_raw = session_data.get("energy_kwh")
            wh_raw = session_data.get("energy_wh")
            if energy_kwh_val is None and kwh_raw is not None:
                try:
                    energy_kwh_val = round(float(kwh_raw), 3)
                except Exception:  # noqa: BLE001
                    energy_kwh_val = None
            if energy_wh_val is None and wh_raw is not None:
                try:
                    energy_wh_val = round(float(wh_raw), 3)
                except Exception:  # noqa: BLE001
                    energy_wh_val = None
        if energy_kwh_val is None:
            session_kwh = data.get("session_kwh")
            if session_kwh is not None:
                try:
                    energy_kwh_val = round(float(session_kwh), 3)
                except Exception:  # noqa: BLE001
                    energy_kwh_val = None
        if energy_wh_val is None:
            energy_wh_raw = data.get("session_energy_wh")
            if energy_wh_raw is not None:
                try:
                    energy_wh_val = round(float(energy_wh_raw), 3)
                except Exception:  # noqa: BLE001
                    energy_wh_val = None
        if energy_kwh_val is not None and energy_wh_val is None:
            try:
                energy_wh_val = round(energy_kwh_val * 1000.0, 3)
            except Exception:  # noqa: BLE001
                energy_wh_val = None

        result["energy_consumed_wh"] = energy_wh_val
        result["energy_consumed_kwh"] = energy_kwh_val

        session_cost = session_data.get("session_cost", data.get("session_cost"))
        if session_cost is not None:
            try:
                result["session_cost"] = round(float(session_cost), 3)
            except Exception:  # noqa: BLE001
                result["session_cost"] = session_cost
        else:
            result["session_cost"] = None

        session_charge_level = session_data.get(
            "session_charge_level", data.get("session_charge_level")
        )
        if session_charge_level is not None:
            try:
                result["session_charge_level"] = int(session_charge_level)
            except Exception:  # noqa: BLE001
                result["session_charge_level"] = session_charge_level
        else:
            result["session_charge_level"] = None

        range_value = session_data.get("session_miles", data.get("session_miles"))
        preferred_unit = UnitOfLength.MILES
        try:
            if hass is not None and hasattr(hass, "config"):
                units = getattr(hass.config, "units", None)
                if units is not None and hasattr(units, "length_unit"):
                    preferred_unit = units.length_unit  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            preferred_unit = UnitOfLength.MILES
        converted_range = None
        try:
            if range_value is not None:
                range_float = float(range_value)
                target_unit = preferred_unit
                if target_unit and target_unit != UnitOfLength.MILES:
                    converted_range = DistanceConverter.convert(
                        range_float, UnitOfLength.MILES, target_unit
                    )
                else:
                    converted_range = range_float
        except Exception:  # noqa: BLE001
            converted_range = None

        result["range_added"] = (
            round(converted_range, 3) if converted_range is not None else None
        )
        result["session_duration_min"] = duration_min
        session_id = session_data.get("session_id")
        if session_id is not None:
            try:
                result["session_id"] = str(session_id)
            except Exception:  # noqa: BLE001
                result["session_id"] = session_id
        else:
            result["session_id"] = None

        start_at = _localize(session_data.get("start") or data.get("session_start"))
        end_at = _localize(session_data.get("end") or data.get("session_end"))
        result["session_started_at"] = start_at
        result["session_ended_at"] = end_at

        result["active_charge_time_s"] = _as_int(
            session_data.get("active_charge_time_s")
        )
        result["avg_cost_per_kwh"] = _as_float(
            session_data.get("avg_cost_per_kwh"), precision=3
        )
        result["cost_calculated"] = _as_bool(session_data.get("cost_calculated"))
        result["session_cost_state"] = session_data.get("session_cost_state")
        result["manual_override"] = _as_bool(session_data.get("manual_override"))
        result["charge_profile_stack_level"] = _as_int(
            session_data.get("charge_profile_stack_level")
        )
        auth_status_raw = session_data.get("session_auth_status")
        if auth_status_raw is None:
            auth_status_raw = data.get("session_auth_status")
        result["session_auth_status"] = _as_int(auth_status_raw)
        result["session_auth_type"] = (
            session_data.get("session_auth_type")
            if session_data.get("session_auth_type") is not None
            else data.get("session_auth_type")
        )
        result["session_auth_identifier"] = (
            session_data.get("session_auth_identifier")
            if session_data.get("session_auth_identifier") is not None
            else data.get("session_auth_identifier")
        )
        auth_token_flag = session_data.get(
            "session_auth_token_present", data.get("session_auth_token_present")
        )
        result["session_auth_token_present"] = _as_bool(auth_token_flag)

        return result


class EnphaseConnectorStatusSensor(_BaseEVSensor):
    _attr_translation_key = "connector_status"

    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Connector Status", "connector_status")

    @property
    def icon(self) -> str | None:
        v = str(self.data.get("connector_status") or "").upper()
        # Map common connector status values to clearer icons
        mapping = {
            "AVAILABLE": "mdi:ev-station",
            "CHARGING": "mdi:ev-plug-ccs2",
            "PLUGGED": "mdi:ev-plug-type2",
            "CONNECTED": "mdi:ev-plug-type2",
            "DISCONNECTED": "mdi:power-plug-off",
            "UNPLUGGED": "mdi:power-plug-off",
            "FAULTED": "mdi:alert",
            "ERROR": "mdi:alert",
            "OCCUPIED": "mdi:car-electric",
        }
        return mapping.get(v, "mdi:ev-station")

    @property
    def extra_state_attributes(self):
        def _clean(val):
            if val in (None, ""):
                return None
            if isinstance(val, str):
                cleaned = val.strip()
                return cleaned or None
            try:
                text = str(val)
            except Exception:  # noqa: BLE001
                return val
            return text.strip() or None

        def _as_bool(val):
            if val is None:
                return None
            try:
                return bool(val)
            except Exception:  # noqa: BLE001
                return None

        return {
            "status_reason": _clean(self.data.get("connector_reason")),
            "connector_status_info": _clean(self.data.get("connector_status_info")),
            "suspended_by_evse": _as_bool(self.data.get("suspended_by_evse")),
        }


class EnphasePowerSensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_translation_key = "power"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER

    _DEFAULT_WINDOW_S = 300  # 5 minutes
    _MIN_DELTA_KWH = 0.0005  # 0.5 Wh jitter guard
    _RESET_DROP_KWH = 0.25  # minimum backward delta treated as a meter reset
    _STATIC_MAX_WATTS = 19200  # IQ EV Charger 2 max continuous throughput (~80A @ 240V)
    _FALLBACK_OPERATING_V = 240  # Assume 240V split-phase when API omits voltage

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_power"
        self._last_lifetime_kwh: float | None = None
        self._last_energy_ts: float | None = None
        self._last_sample_ts: float | None = None
        self._last_power_w: int = 0
        self._last_window_s: float | None = None
        self._last_method: str = "seeded"
        self._max_throughput_w: int = self._STATIC_MAX_WATTS
        self._max_throughput_unbounded_w: int = self._STATIC_MAX_WATTS
        self._max_throughput_source: str = "static_default"
        self._max_throughput_amps: float | None = None
        self._max_throughput_voltage: float = float(self._FALLBACK_OPERATING_V)
        self._last_reset_at: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state:
            return
        attrs = last_state.attributes or {}
        try:
            if attrs.get("last_lifetime_kwh") is not None:
                self._last_lifetime_kwh = float(attrs.get("last_lifetime_kwh"))
        except Exception:
            self._last_lifetime_kwh = None
        try:
            if attrs.get("last_energy_ts") is not None:
                self._last_energy_ts = float(attrs.get("last_energy_ts"))
        except Exception:
            self._last_energy_ts = None
        try:
            if attrs.get("last_sample_ts") is not None:
                self._last_sample_ts = float(attrs.get("last_sample_ts"))
        except Exception:
            self._last_sample_ts = None
        try:
            self._last_power_w = int(round(float(last_state.state)))
        except Exception:
            try:
                if attrs.get("last_power_w") is not None:
                    self._last_power_w = int(round(float(attrs.get("last_power_w"))))
            except Exception:
                self._last_power_w = 0
        try:
            if attrs.get("last_window_seconds") is not None:
                self._last_window_s = float(attrs.get("last_window_seconds"))
        except Exception:
            self._last_window_s = None
        if attrs.get("method"):
            self._last_method = str(attrs.get("method"))
        try:
            if attrs.get("last_reset_at") is not None:
                self._last_reset_at = float(attrs.get("last_reset_at"))
        except Exception:
            self._last_reset_at = None

        # Legacy restore support (pre-0.7.9 attributes)
        if self._last_lifetime_kwh is None:
            legacy_baseline = attrs.get("baseline_kwh")
            legacy_today = attrs.get("last_energy_today_kwh")
            try:
                if legacy_baseline is not None:
                    legacy_baseline = float(legacy_baseline)
                if legacy_today is not None:
                    legacy_today = float(legacy_today)
            except Exception:
                legacy_baseline = None
                legacy_today = None
            if legacy_baseline is not None and legacy_today is not None:
                self._last_lifetime_kwh = legacy_baseline + legacy_today
                try:
                    if (
                        attrs.get("last_ts") is not None
                        and self._last_energy_ts is None
                    ):
                        self._last_energy_ts = float(attrs.get("last_ts"))
                except Exception:
                    self._last_energy_ts = None
                # Preserve previously reported power when available
                if attrs.get("method") is None:
                    self._last_method = "legacy_restore"

    @staticmethod
    def _parse_timestamp(raw: float | str | None) -> float | None:
        """Normalize Enlighten timestamps to epoch seconds."""
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            val = float(raw)
            if val > 10**12:
                val = val / 1000.0
            return val if val > 0 else None
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return None
            s = s.replace("[UTC]", "").replace("Z", "+00:00")
            try:
                dt_obj = datetime.fromisoformat(s)
            except ValueError:
                return None
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            return dt_obj.timestamp()
        return None

    @staticmethod
    def _as_float(val) -> float | None:
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_actually_charging(data: dict) -> bool:
        status = data.get("connector_status")
        if isinstance(status, str) and status.strip().upper().startswith("SUSPENDED"):
            return False
        if data.get("suspended_by_evse"):
            return False
        return bool(data.get("charging"))

    def _resolve_max_throughput(
        self, data: dict
    ) -> tuple[int, str, float | None, float, int]:
        voltage = self._as_float(data.get("operating_v"))
        if voltage is None or voltage <= 0:
            voltage = float(self._FALLBACK_OPERATING_V)
        candidates = (
            ("session_charge_level", data.get("session_charge_level")),
            ("charging_level", data.get("charging_level")),
            ("max_amp", data.get("max_amp")),
            ("max_current", data.get("max_current")),
        )
        for source, raw in candidates:
            amps = self._as_float(raw)
            if amps is None or amps <= 0:
                continue
            unbounded = int(round(voltage * amps))
            if unbounded <= 0:
                continue
            bounded = min(unbounded, self._STATIC_MAX_WATTS)
            return bounded, source, amps, voltage, unbounded
        return (
            self._STATIC_MAX_WATTS,
            "static_default",
            None,
            voltage,
            self._STATIC_MAX_WATTS,
        )

    @property
    def native_value(self):
        data = self.data
        is_charging = self._is_actually_charging(data)
        (
            max_watts,
            max_source,
            max_amps,
            max_voltage,
            max_unbounded,
        ) = self._resolve_max_throughput(data)
        self._max_throughput_w = max_watts
        self._max_throughput_unbounded_w = max_unbounded
        self._max_throughput_source = max_source
        self._max_throughput_amps = max_amps
        self._max_throughput_voltage = max_voltage
        lifetime = self._as_float(data.get("lifetime_kwh"))
        sample_ts = self._parse_timestamp(data.get("last_reported_at"))
        if sample_ts is None:
            now_dt = dt_util.now()
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            sample_ts = now_dt.astimezone(timezone.utc).timestamp()
        self._last_sample_ts = sample_ts

        if lifetime is None:
            if not is_charging:
                self._last_power_w = 0
                self._last_method = "idle"
                self._last_window_s = None
            return self._last_power_w

        if self._last_lifetime_kwh is None:
            self._last_lifetime_kwh = lifetime
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "seeded"
            self._last_window_s = None
            return 0

        delta_kwh = lifetime - self._last_lifetime_kwh
        if delta_kwh < -self._RESET_DROP_KWH:
            self._last_lifetime_kwh = lifetime
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "lifetime_reset"
            self._last_window_s = None
            self._last_reset_at = sample_ts
            return 0
        if not is_charging:
            self._last_lifetime_kwh = lifetime
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "idle"
            self._last_window_s = None
            return 0
        if delta_kwh <= self._MIN_DELTA_KWH:
            return self._last_power_w

        if self._last_energy_ts is not None and sample_ts > self._last_energy_ts:
            window_s = sample_ts - self._last_energy_ts
        else:
            window_s = self._DEFAULT_WINDOW_S

        watts = (delta_kwh * 3_600_000.0) / window_s
        if watts < 0:
            watts = 0
        if watts > self._max_throughput_w:
            watts = self._max_throughput_w

        self._last_power_w = int(round(watts))
        self._last_method = "lifetime_energy_window"
        self._last_window_s = window_s
        self._last_lifetime_kwh = lifetime
        self._last_energy_ts = sample_ts
        return self._last_power_w

    @property
    def extra_state_attributes(self):
        data = self.data
        actual_charging = self._is_actually_charging(data)
        return {
            "last_lifetime_kwh": self._last_lifetime_kwh,
            "last_energy_ts": self._last_energy_ts,
            "last_sample_ts": self._last_sample_ts,
            "last_power_w": self._last_power_w,
            "last_window_seconds": self._last_window_s,
            "method": self._last_method,
            "charging": bool(data.get("charging")),
            "actual_charging": actual_charging,
            "operating_v": data.get("operating_v") or self._FALLBACK_OPERATING_V,
            "max_throughput_w": self._max_throughput_w,
            "max_throughput_unbounded_w": self._max_throughput_unbounded_w,
            "max_throughput_source": self._max_throughput_source,
            "max_throughput_amps": self._max_throughput_amps,
            "max_throughput_voltage": self._max_throughput_voltage,
            "last_reset_at": self._last_reset_at,
        }


class EnphaseChargingLevelSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "set_amps"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 0

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charging_amps"

    @staticmethod
    def _safe_limit_active(value) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return bool(value)
        try:
            return int(str(value).strip()) != 0
        except Exception:  # noqa: BLE001
            return False

    @property
    def native_value(self):
        data = self.data
        if self._safe_limit_active(data.get("safe_limit_state")):
            return SAFE_LIMIT_AMPS
        lvl = data.get("charging_level")
        if lvl is None:
            # Fall back to coordinator helper which respects charger limits
            return self._coord.pick_start_amps(self._sn)
        try:
            return int(lvl)
        except Exception:
            return self._coord.pick_start_amps(self._sn)

    @staticmethod
    def _coerce_amp(value):
        if value in (None, ""):
            return None
        try:
            return int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        min_amp = self._coerce_amp(self.data.get("min_amp"))
        max_amp = self._coerce_amp(self.data.get("max_amp"))
        max_current = self._coerce_amp(self.data.get("max_current"))
        amp_granularity = self._coerce_amp(self.data.get("amp_granularity"))
        safe_limit_state = self.data.get("safe_limit_state")
        return {
            "min_amp": min_amp,
            "max_amp": max_amp,
            "max_current": max_current,
            "amp_granularity": amp_granularity,
            "safe_limit_state": safe_limit_state,
            "safe_limit_active": self._safe_limit_active(safe_limit_state),
        }


class EnphaseLastReportedSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_last_rpt"

    @property
    def native_value(self):
        from datetime import datetime, timezone

        s = self.data.get("last_reported_at")
        if not s:
            return None
        # Example: 2025-09-07T11:38:31Z[UTC]
        s = str(s).replace("[UTC]", "").replace("Z", "")
        try:
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @property
    def extra_state_attributes(self):
        def _as_int(value):
            if value is None:
                return None
            try:
                return int(str(value).strip())
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(value):
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in ("true", "1", "yes", "y", "enabled", "on"):
                    return True
                if normalized in ("false", "0", "no", "n", "disabled", "off"):
                    return False
            return None

        def _clean_text(value):
            if value in (None, ""):
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        def _localize(value):
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)):
                    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                elif isinstance(value, str):
                    cleaned = value.strip()
                    if not cleaned:
                        return None
                    if cleaned.endswith("[UTC]"):
                        cleaned = cleaned[:-5]
                    if cleaned.endswith("Z"):
                        cleaned = cleaned[:-1] + "+00:00"
                    dt = datetime.fromisoformat(cleaned)
                else:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt_util.as_local(dt).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None

        interval_raw = self.data.get("reporting_interval")
        attrs = {
            "reporting_interval": _as_int(interval_raw),
            "connection": _clean_text(self.data.get("connection")),
            "ip_address": _clean_text(self.data.get("ip_address")),
            "mac_address": _clean_text(self.data.get("mac_address")),
            "network_interface_count": _as_int(self.data.get("network_interface_count")),
            "operating_voltage": _as_int(self.data.get("operating_v")),
            "charger_timezone": _clean_text(self.data.get("charger_timezone")),
            "firmware_version": _clean_text(self.data.get("firmware_version")),
            "system_version": _clean_text(self.data.get("system_version")),
            "application_version": _clean_text(self.data.get("application_version")),
            "software_version": _clean_text(self.data.get("sw_version")),
            "hardware_version": _clean_text(self.data.get("hw_version")),
            "processor_board_version": _clean_text(
                self.data.get("processor_board_version")
            ),
            "power_board_version": _clean_text(self.data.get("power_board_version")),
            "kernel_version": _clean_text(self.data.get("kernel_version")),
            "bootloader_version": _clean_text(self.data.get("bootloader_version")),
            "default_route": _clean_text(self.data.get("default_route")),
            "wifi_config": _clean_text(self.data.get("wifi_config")),
            "cellular_config": _clean_text(self.data.get("cellular_config")),
            "warranty_start_date": _localize(self.data.get("warranty_start_date")),
            "warranty_due_date": _localize(self.data.get("warranty_due_date")),
            "warranty_period_years": _as_int(self.data.get("warranty_period_years")),
            "created_at": _localize(self.data.get("created_at")),
            "breaker_rating": _as_int(self.data.get("breaker_rating")),
            "rated_current": _as_int(self.data.get("rated_current")),
            "grid_type": _as_int(self.data.get("grid_type")),
            "phase_count": _as_int(self.data.get("phase_count")),
            "commissioning_status": _as_int(self.data.get("commissioning_status")),
            "is_connected": _as_bool(self.data.get("is_connected")),
            "is_locally_connected": _as_bool(self.data.get("is_locally_connected")),
            "ho_control": _as_bool(self.data.get("ho_control")),
            "gateway_connection_count": _as_int(
                self.data.get("gateway_connection_count")
            ),
            "gateway_connected_count": _as_int(self.data.get("gateway_connected_count")),
            "functional_validation_state": _as_int(
                self.data.get("functional_validation_state")
            ),
            "functional_validation_updated_at": _localize(
                self.data.get("functional_validation_updated_at")
            ),
        }
        return attrs


class EnphaseChargeModeSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_mode"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charge_mode"

    @property
    def native_value(self):
        d = self.data
        # Prefer scheduler preference when available for consistency with selector
        return d.get("charge_mode_pref") or d.get("charge_mode")

    @property
    def icon(self) -> str | None:
        # Map charge modes to friendly icons
        mode = str(self.native_value or "").upper()
        mapping = {
            "MANUAL_CHARGING": "mdi:flash",
            "IMMEDIATE": "mdi:flash",
            "SCHEDULED_CHARGING": "mdi:calendar-clock",
            "GREEN_CHARGING": "mdi:leaf",
            "IDLE": "mdi:timer-sand-paused",
        }
        return mapping.get(mode, "mdi:car-electric")

    @staticmethod
    def _as_bool(value) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "y", "enabled", "on"):
                return True
            if normalized in ("false", "0", "no", "n", "disabled", "off"):
                return False
        return None

    @property
    def extra_state_attributes(self):
        return {
            "preferred_mode": self.data.get("charge_mode_pref"),
            "effective_mode": self.data.get("charge_mode"),
            "schedule_status": self.data.get("schedule_status"),
            "schedule_type": self.data.get("schedule_type"),
            "schedule_slot_id": self.data.get("schedule_slot_id"),
            "schedule_start": self.data.get("schedule_start"),
            "schedule_end": self.data.get("schedule_end"),
            "schedule_days": self.data.get("schedule_days"),
            "schedule_reminder_enabled": self._as_bool(
                self.data.get("schedule_reminder_enabled")
            ),
            "schedule_reminder_minutes": self.data.get("schedule_reminder_min"),
            "green_battery_supported": self._as_bool(
                self.data.get("green_battery_supported")
            ),
            "green_battery_enabled": self._as_bool(
                self.data.get("green_battery_enabled")
            ),
        }


class EnphaseStormGuardStateSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "storm_guard_state"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_storm_guard_state"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self.data.get("storm_guard_state") is not None

    @property
    def native_value(self):
        raw = self.data.get("storm_guard_state")
        if raw is None:
            return None
        if isinstance(raw, bool):
            return "enabled" if raw else "disabled"
        if isinstance(raw, (int, float)):
            return "enabled" if raw != 0 else "disabled"
        try:
            normalized = str(raw).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        if normalized in ("enabled", "disabled"):
            return normalized
        if normalized in ("true", "1", "yes", "y", "on"):
            return "enabled"
        if normalized in ("false", "0", "no", "n", "off"):
            return "disabled"
        return None


class EnphaseChargerAuthenticationSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charger_authentication"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charger_authentication"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self._coord.auth_settings_available

    @property
    def native_value(self):
        required = self.data.get("auth_required")
        if required is True:
            return "enabled"
        if required is False:
            return "disabled"
        return None

    @staticmethod
    def _as_bool(value) -> bool | None:
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        return {
            "app_auth_enabled": self._as_bool(self.data.get("app_auth_enabled")),
            "rfid_auth_enabled": self._as_bool(self.data.get("rfid_auth_enabled")),
            "app_auth_supported": self._as_bool(self.data.get("app_auth_supported")),
            "rfid_auth_supported": self._as_bool(self.data.get("rfid_auth_supported")),
        }


class EnphaseLifetimeEnergySensor(EnphaseBaseEntity, RestoreSensor):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = "lifetime_energy"
    # Allow tiny jitter of 0.01 kWh (~10 Wh) before treating value as a drop
    _drop_tolerance = 0.01
    # Heuristics for accepting genuine meter resets reported by the API
    _reset_floor_kwh = 5.0
    _reset_drop_threshold_kwh = 0.5
    _reset_ratio = 0.5

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_lifetime_kwh"
        # Track last good value to avoid publishing bad/zero on startup
        self._last_value: float | None = None
        # Apply a one-shot boot filter to ignore an initial 0/None
        self._boot_filter: bool = True
        self._last_reset_value: float | None = None
        self._last_reset_at: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore native value using RestoreSensor helper (restores native_value/unit)
        last = await self.async_get_last_sensor_data()
        if last is None:
            return
        try:
            val = float(last.native_value) if last.native_value is not None else None
        except Exception:
            val = None
        if val is not None and val >= 0:
            self._last_value = val
            self._attr_native_value = val
        try:
            last_state = await self.async_get_last_state()
        except Exception:
            last_state = None
        if last_state is not None:
            attrs = last_state.attributes or {}
            try:
                if attrs.get("last_reset_value") is not None:
                    self._last_reset_value = float(attrs.get("last_reset_value"))
            except Exception:
                self._last_reset_value = None
            reset_at_attr = attrs.get("last_reset_at")
            if isinstance(reset_at_attr, str):
                self._last_reset_at = reset_at_attr

    @property
    def native_value(self):
        raw = self.data.get("lifetime_kwh")
        # Parse and validate
        val: float | None
        try:
            val = float(raw) if raw is not None else None
        except Exception:
            val = None

        # Reject missing or negative samples outright; keep prior value
        if val is None or val < 0:
            return self._last_value

        # Honor boot filter before running drop/reset heuristics so the initial
        # zero sample reported at startup keeps the restored value.
        if self._boot_filter:
            if val == 0 and (self._last_value or 0) > 0:
                return self._last_value
            # First good sample observed; disable boot filter
            self._boot_filter = False

        # Enforce monotonic behaviour – ignore sudden drops beyond tolerance
        if self._last_value is not None:
            if val + self._drop_tolerance < self._last_value:
                drop = self._last_value - val
                if drop >= self._reset_drop_threshold_kwh and (
                    val <= self._reset_floor_kwh
                    or val <= (self._last_value * self._reset_ratio)
                ):
                    self._last_reset_value = val
                    self._last_reset_at = dt_util.utcnow().isoformat()
                    self._boot_filter = False
                else:
                    return self._last_value
            elif val < self._last_value:
                val = self._last_value

        # Accept sample; remember as last good value
        self._last_value = val
        return val

    @property
    def extra_state_attributes(self):
        return {
            "last_reset_value": self._last_reset_value,
            "last_reset_at": self._last_reset_at,
        }


class EnphaseStatusSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "status"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_status"
        from homeassistant.helpers.entity import EntityCategory

        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self.data.get("status")

    @property
    def extra_state_attributes(self):
        def _as_bool(value):
            if value is None:
                return None
            try:
                return bool(value)
            except Exception:  # noqa: BLE001
                return None

        def _localize(value):
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)):
                    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                elif isinstance(value, str):
                    cleaned = value.strip()
                    if not cleaned:
                        return None
                    if cleaned.endswith("[UTC]"):
                        cleaned = cleaned[:-5]
                    if cleaned.endswith("Z"):
                        cleaned = cleaned[:-1] + "+00:00"
                    dt = datetime.fromisoformat(cleaned)
                else:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt_util.as_local(dt).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None

        return {
            "commissioned": _as_bool(self.data.get("commissioned")),
            "charger_problem": _as_bool(self.data.get("faulted")),
            "suspended_by_evse": _as_bool(self.data.get("suspended_by_evse")),
            "offline_since": _localize(self.data.get("offline_since")),
        }


## Removed duplicate Current Amps sensor to avoid confusion with Set Amps


## Removed unreliable sensors: Session Miles


class _TimestampFromIsoSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coord: EnphaseCoordinator, sn: str, key: str, name: str, uniq: str
    ):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = uniq

    @property
    def native_value(self):
        from datetime import datetime, timezone

        s = self.data.get(self._key)
        if not s:
            return None
        s = str(s).replace("[UTC]", "").replace("Z", "")
        try:
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None


## Removed unreliable sensors: Session Plug-in At


## Removed unreliable sensors: Session Plug-out At


class _TimestampFromEpochSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coord: EnphaseCoordinator, sn: str, key: str, name: str, uniq: str
    ):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = uniq

    @property
    def native_value(self):
        from datetime import datetime, timezone

        ts = self.data.get(self._key)
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except Exception:
            return None


## Removed unreliable sensors: Schedule Type


## Removed unreliable sensors: Schedule Start


## Removed unreliable sensors: Schedule End


class EnphaseTypeInventorySensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, type_key: str) -> None:
        super().__init__(coord)
        self._coord = coord
        self._type_key = str(type_key)
        label = self._coord.type_label(self._type_key) or "Device"
        self._attr_name = f"{label} Inventory"
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_type_{self._type_key}_inventory"
        )

    @property
    def available(self) -> bool:
        has_type = getattr(self._coord, "has_type", None)
        return bool(
            super().available
            and (bool(has_type(self._type_key)) if callable(has_type) else True)
        )

    @property
    def native_value(self):
        bucket = self._coord.type_bucket(self._type_key) or {}
        try:
            return int(bucket.get("count", 0))
        except Exception:
            return 0

    @property
    def extra_state_attributes(self):
        bucket = self._coord.type_bucket(self._type_key) or {}
        members = bucket.get("devices")
        attrs = {
            "type_key": self._type_key,
            "type_label": bucket.get("type_label") or self._coord.type_label(self._type_key),
            "device_count": bucket.get("count", 0),
            "devices": members if isinstance(members, list) else [],
        }
        status_counts = bucket.get("status_counts")
        if isinstance(status_counts, dict):
            attrs["status_counts"] = dict(status_counts)
        status_summary = bucket.get("status_summary")
        if isinstance(status_summary, str) and status_summary.strip():
            attrs["status_summary"] = status_summary
        model_counts = bucket.get("model_counts")
        if isinstance(model_counts, dict):
            attrs["model_counts"] = dict(model_counts)
        model_summary = bucket.get("model_summary")
        if isinstance(model_summary, str) and model_summary.strip():
            attrs["model_summary"] = model_summary
        firmware_counts = bucket.get("firmware_counts")
        if isinstance(firmware_counts, dict):
            attrs["firmware_counts"] = dict(firmware_counts)
        firmware_summary = bucket.get("firmware_summary")
        if isinstance(firmware_summary, str) and firmware_summary.strip():
            attrs["firmware_summary"] = firmware_summary
        array_counts = bucket.get("array_counts")
        if isinstance(array_counts, dict):
            attrs["array_counts"] = dict(array_counts)
        array_summary = bucket.get("array_summary")
        if isinstance(array_summary, str) and array_summary.strip():
            attrs["array_summary"] = array_summary
        panel_info = bucket.get("panel_info")
        if isinstance(panel_info, dict):
            attrs["panel_info"] = dict(panel_info)
        status_type_counts = bucket.get("status_type_counts")
        if isinstance(status_type_counts, dict):
            attrs["status_type_counts"] = dict(status_type_counts)
        connectivity_state = bucket.get("connectivity_state")
        if isinstance(connectivity_state, str) and connectivity_state.strip():
            attrs["connectivity_state"] = connectivity_state
        reporting_count = bucket.get("reporting_count")
        if reporting_count is not None:
            attrs["reporting_count"] = reporting_count
        latest_reported_utc = bucket.get("latest_reported_utc")
        if isinstance(latest_reported_utc, str) and latest_reported_utc.strip():
            attrs["latest_reported_utc"] = latest_reported_utc
        latest_reported_device = bucket.get("latest_reported_device")
        if isinstance(latest_reported_device, dict):
            attrs["latest_reported_device"] = dict(latest_reported_device)
        production_start = bucket.get("production_start_date")
        if isinstance(production_start, str) and production_start.strip():
            attrs["production_start_date"] = production_start
        production_end = bucket.get("production_end_date")
        if isinstance(production_end, str) and production_end.strip():
            attrs["production_end_date"] = production_end
        return attrs

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo

        info = self._coord.type_device_info(self._type_key)
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:{self._type_key}")},
            manufacturer="Enphase",
        )


class EnphaseInverterLifetimeEnergySensor(CoordinatorEntity, RestoreSensor):
    """Lifetime production for one inverter under the shared microinverter device."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "MWh"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 6

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._attr_name = f"{self._sn} Lifetime Energy"
        self._attr_unique_id = f"{DOMAIN}_inverter_{self._sn}_lifetime_energy"
        self._last_good_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None:
            return
        try:
            restored = (
                float(last.native_value) if last.native_value is not None else None
            )
        except Exception:  # noqa: BLE001
            restored = None
        if restored is not None and restored >= 0:
            self._last_good_native_value = restored
            self._attr_native_value = restored

    def _snapshot(self) -> dict[str, object] | None:
        getter = getattr(self._coord, "inverter_data", None)
        if not callable(getter):
            return None
        data = getter(self._sn)
        if isinstance(data, dict):
            return data
        return None

    @property
    def available(self) -> bool:
        return bool(super().available and self._snapshot() is not None)

    @property
    def native_value(self):
        data = self._snapshot()
        if not isinstance(data, dict):
            return self._last_good_native_value
        raw_wh = data.get("lifetime_production_wh")
        try:
            value_wh = float(raw_wh) if raw_wh is not None else None
        except (TypeError, ValueError):
            value_wh = None
        if value_wh is None or value_wh < 0:
            return self._last_good_native_value
        value_mwh = round(value_wh / 1_000_000.0, 6)
        if (
            self._last_good_native_value is not None
            and value_mwh < self._last_good_native_value
        ):
            return self._last_good_native_value
        self._last_good_native_value = value_mwh
        return value_mwh

    @property
    def extra_state_attributes(self):
        data = self._snapshot() or {}
        return {
            "serial_number": data.get("serial_number"),
            "inverter_id": data.get("inverter_id"),
            "device_id": data.get("device_id"),
            "inverter_type": data.get("inverter_type"),
            "name": data.get("name"),
            "array_name": data.get("array_name"),
            "sku_id": data.get("sku_id"),
            "part_num": data.get("part_num"),
            "sku": data.get("sku"),
            "status": data.get("status"),
            "status_text": data.get("status_text"),
            "status_code": data.get("status_code"),
            "last_report": data.get("last_report"),
            "fw1": data.get("fw1"),
            "fw2": data.get("fw2"),
            "warranty_end_date": data.get("warranty_end_date"),
            "show_sig_str": data.get("show_sig_str"),
            "emu_version": data.get("emu_version"),
            "issi": data.get("issi"),
            "rssi": data.get("rssi"),
            "lifetime_production_wh": data.get("lifetime_production_wh"),
            "lifetime_query_start_date": data.get("lifetime_query_start_date"),
            "lifetime_query_end_date": data.get("lifetime_query_end_date"),
        }

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo

        type_device_info = getattr(self._coord, "type_device_info", None)
        info = (
            type_device_info("microinverter")
            if callable(type_device_info)
            else None
        )
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:microinverter")},
            manufacturer="Enphase",
            name="Microinverters",
        )


class _EnphaseBatteryStorageBaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: EnphaseCoordinator, serial: str, unique_suffix: str) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_battery_{self._sn}{unique_suffix}"
        )

    def _snapshot(self) -> dict[str, object] | None:
        getter = getattr(self._coord, "battery_storage", None)
        if not callable(getter):
            return None
        payload = getter(self._sn)
        if isinstance(payload, dict):
            return payload
        return None

    @staticmethod
    def _as_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _as_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value).strip())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            if isinstance(value, datetime):
                dt_value = value
            else:
                text = str(value).strip()
                if not text:
                    return None
                text = text.replace("[UTC]", "")
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                dt_value = datetime.fromisoformat(text)
            if dt_value.tzinfo is None:
                return dt_value.replace(tzinfo=timezone.utc)
            return dt_value
        except Exception:  # noqa: BLE001
            return None

    @property
    def available(self) -> bool:
        if not _type_available(self._coord, "encharge"):
            return False
        return bool(super().available and self._snapshot() is not None)

    @property
    def _battery_label(self) -> str:
        snapshot = self._snapshot() or {}
        for key in ("name", "serial_number", "identity"):
            value = snapshot.get(key)
            if value is None:
                continue
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return text
        return self._sn

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo

        type_device_info = getattr(self._coord, "type_device_info", None)
        info = type_device_info("encharge") if callable(type_device_info) else None
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
            name="Battery",
        )


class EnphaseBatteryStorageChargeSensor(_EnphaseBatteryStorageBaseSensor):
    """Per-battery state-of-charge sensor under the shared battery type device."""

    _attr_translation_key = "battery_storage_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_charge_level")

    @property
    def name(self) -> str:
        return self._battery_label

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        value = snapshot.get("current_charge_pct")
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        return self._snapshot() or {}


class EnphaseBatteryStorageStatusSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_status")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        status_text = snapshot.get("status_text")
        if status_text is not None:
            try:
                cleaned = str(status_text).strip()
            except Exception:  # noqa: BLE001
                cleaned = ""
            if cleaned:
                return cleaned
        status_normalized = snapshot.get("status_normalized")
        if status_normalized is None:
            return None
        try:
            cleaned = str(status_normalized).strip()
        except Exception:  # noqa: BLE001
            return None
        return cleaned or None

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot() or {}
        return {
            "status_normalized": snapshot.get("status_normalized"),
            "status_raw": snapshot.get("status"),
            "status_text": snapshot.get("status_text"),
        }


class EnphaseBatteryStorageHealthSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_health"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_health")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        value = self._as_float(snapshot.get("battery_soh"))
        if value is None:
            return None
        return round(value, 1)


class EnphaseBatteryStorageCycleCountSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_cycle_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_cycle_count")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        return self._as_int(snapshot.get("cycle_count"))


class EnphaseBatteryStorageLastReportedSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_last_reported")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        return self._parse_timestamp(
            snapshot.get("last_reported")
            if snapshot.get("last_reported") is not None
            else snapshot.get("last_report")
        )


_GATEWAY_STATUS_KEYS: tuple[str, ...] = ("statusText", "status")
_GATEWAY_MODEL_KEYS: tuple[str, ...] = ("model", "channel_type", "sku_id")
_GATEWAY_FIRMWARE_KEYS: tuple[str, ...] = ("envoy_sw_version", "sw_version")
_GATEWAY_LAST_REPORT_KEYS: tuple[str, ...] = (
    "last_report",
    "last_reported",
    "lastReportedAt",
)


def _gateway_clean_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _gateway_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "enabled", "enable", "on"):
            return True
        if normalized in ("false", "0", "no", "n", "disabled", "disable", "off"):
            return False
    return None


def _gateway_normalize_status(value: object) -> str:
    text = _gateway_clean_text(value)
    if not text:
        return "unknown"
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("fault", "error", "critical")):
        return "error"
    if "warn" in normalized:
        return "warning"
    if any(
        token in normalized
        for token in ("not_reporting", "offline", "disconnected", "retired")
    ):
        return "not_reporting"
    if any(token in normalized for token in ("normal", "online", "connected", "ok")):
        return "normal"
    return "unknown"


def _gateway_parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    timestamp_seconds: float | None = None
    if isinstance(value, (int, float)):
        try:
            timestamp_seconds = float(value)
        except Exception:  # noqa: BLE001
            timestamp_seconds = None
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            timestamp_seconds = float(cleaned)
        except Exception:
            timestamp_seconds = None
        if timestamp_seconds is None:
            normalized = cleaned.replace("[UTC]", "").replace("Z", "+00:00")
            parsed = dt_util.parse_datetime(normalized)
            if parsed is None:
                try:
                    parsed = datetime.fromisoformat(normalized)
                except Exception:  # noqa: BLE001
                    return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    if timestamp_seconds is None:
        return None
    if timestamp_seconds > 1_000_000_000_000:
        timestamp_seconds /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _gateway_format_counts(counts: dict[str, int]) -> str | None:
    clean: dict[str, int] = {}
    for key, value in (counts or {}).items():
        label = _gateway_clean_text(key)
        if not label:
            continue
        try:
            count = int(value)
        except Exception:  # noqa: BLE001
            continue
        if count <= 0:
            continue
        clean[label] = count
    if not clean:
        return None
    ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} x{count}" for name, count in ordered)


def _gateway_inventory_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    bucket = coord.type_bucket("envoy") or {}
    members_raw = bucket.get("devices")
    members = (
        [item for item in members_raw if isinstance(item, dict)]
        if isinstance(members_raw, list)
        else []
    )
    try:
        total_devices = int(bucket.get("count", len(members)))
    except Exception:  # noqa: BLE001
        total_devices = len(members)
    total_devices = max(total_devices, len(members))

    status_counts: dict[str, int] = {
        "normal": 0,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    model_counts: dict[str, int] = {}
    firmware_counts: dict[str, int] = {}
    property_keys: set[str] = set()
    connected_devices = 0
    disconnected_devices = 0
    latest_reported: datetime | None = None
    latest_reported_device: dict[str, object] | None = None
    without_last_report_count = 0

    for member in members:
        property_keys.update(str(key) for key in member.keys())

        status_source = None
        for key in _GATEWAY_STATUS_KEYS:
            if member.get(key) is not None:
                status_source = member.get(key)
                break
        status = _gateway_normalize_status(status_source)
        status_counts[status] = status_counts.get(status, 0) + 1

        connected = _gateway_optional_bool(member.get("connected"))
        if connected is None:
            if status == "normal":
                connected = True
            elif status == "not_reporting":
                connected = False
        if connected is True:
            connected_devices += 1
        elif connected is False:
            disconnected_devices += 1

        model_name = None
        for key in _GATEWAY_MODEL_KEYS:
            model_name = _gateway_clean_text(member.get(key))
            if model_name:
                break
        if model_name:
            model_counts[model_name] = model_counts.get(model_name, 0) + 1

        firmware_version = None
        for key in _GATEWAY_FIRMWARE_KEYS:
            firmware_version = _gateway_clean_text(member.get(key))
            if firmware_version:
                break
        if firmware_version:
            firmware_counts[firmware_version] = firmware_counts.get(firmware_version, 0) + 1

        parsed_last_report = None
        for key in _GATEWAY_LAST_REPORT_KEYS:
            parsed_last_report = _gateway_parse_timestamp(member.get(key))
            if parsed_last_report is not None:
                break
        if parsed_last_report is None:
            without_last_report_count += 1
            continue
        if latest_reported is None or parsed_last_report > latest_reported:
            latest_reported = parsed_last_report
            latest_reported_device = {
                "name": _gateway_clean_text(member.get("name")),
                "serial_number": _gateway_clean_text(member.get("serial_number")),
                "status": _gateway_clean_text(status_source),
            }

    unknown_connection_devices = max(
        0, total_devices - connected_devices - disconnected_devices
    )
    status_summary = (
        f"Normal {status_counts.get('normal', 0)} | "
        f"Warning {status_counts.get('warning', 0)} | "
        f"Error {status_counts.get('error', 0)} | "
        f"Not Reporting {status_counts.get('not_reporting', 0)} | "
        f"Unknown {status_counts.get('unknown', 0)}"
    )
    if total_devices <= 0:
        status_summary = None

    return {
        "total_devices": total_devices,
        "connected_devices": connected_devices,
        "disconnected_devices": disconnected_devices,
        "unknown_connection_devices": unknown_connection_devices,
        "without_last_report_count": without_last_report_count,
        "status_counts": status_counts,
        "status_summary": status_summary,
        "model_counts": model_counts,
        "model_summary": _gateway_format_counts(model_counts),
        "firmware_counts": firmware_counts,
        "firmware_summary": _gateway_format_counts(firmware_counts),
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "property_keys": sorted(property_keys),
    }


def _gateway_connectivity_state(snapshot: dict[str, object]) -> str | None:
    total = int(snapshot.get("total_devices", 0) or 0)
    connected = int(snapshot.get("connected_devices", 0) or 0)
    disconnected = int(snapshot.get("disconnected_devices", 0) or 0)
    unknown = int(snapshot.get("unknown_connection_devices", 0) or 0)
    if total <= 0:
        return None
    if connected >= total:
        return "online"
    if connected == 0 and disconnected > 0:
        return "offline"
    if connected > 0 and connected < total:
        return "degraded"
    if unknown >= total:
        return "unknown"
    return "degraded"


def _microinverter_connectivity_state(snapshot: dict[str, object]) -> str | None:
    total = int(snapshot.get("total_inverters", 0) or 0)
    reporting = int(snapshot.get("reporting_inverters", 0) or 0)
    unknown = int(snapshot.get("unknown_inverters", 0) or 0)
    if total <= 0:
        return None
    if reporting >= total:
        return "online"
    if reporting == 0 and unknown > 0:
        return "unknown"
    if reporting > 0:
        return "degraded"
    return "offline"


def _microinverter_inventory_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    bucket = coord.type_bucket("microinverter") or {}
    members = bucket.get("devices")
    if isinstance(members, list):
        safe_members = [dict(item) for item in members if isinstance(item, dict)]
    else:
        safe_members = []

    status_counts_raw = bucket.get("status_counts")
    status_counts: dict[str, int] = {}
    has_status_counts = isinstance(status_counts_raw, dict)
    if isinstance(status_counts_raw, dict):
        for key in ("total", "normal", "warning", "error", "not_reporting", "unknown"):
            try:
                status_counts[key] = int(status_counts_raw.get(key, 0) or 0)
            except Exception:
                status_counts[key] = 0

    try:
        total_inverters = int(bucket.get("count", len(safe_members)) or 0)
    except Exception:
        total_inverters = len(safe_members)
    if status_counts.get("total", 0) > 0:
        total_inverters = max(total_inverters, int(status_counts.get("total", 0)))

    not_reporting = max(0, int(status_counts.get("not_reporting", 0)))
    unknown = max(0, int(status_counts.get("unknown", 0)))
    if not has_status_counts:
        unknown = total_inverters
    elif (
        total_inverters > 0
        and int(status_counts.get("total", 0) or 0) <= 0
        and max(
            0,
            int(status_counts.get("normal", 0) or 0)
            + int(status_counts.get("warning", 0) or 0)
            + int(status_counts.get("error", 0) or 0)
            + not_reporting
            + unknown,
        )
        == 0
    ):
        unknown = total_inverters
    known_status_total = not_reporting + unknown
    if known_status_total > total_inverters:
        overflow = known_status_total - total_inverters
        unknown = max(0, unknown - overflow)
    reporting = max(0, total_inverters - not_reporting - unknown)

    latest_reported = _gateway_parse_timestamp(
        bucket.get("latest_reported_utc")
        if bucket.get("latest_reported_utc") is not None
        else bucket.get("latest_reported")
    )
    latest_reported_device = (
        dict(bucket.get("latest_reported_device"))
        if isinstance(bucket.get("latest_reported_device"), dict)
        else None
    )
    if latest_reported is None:
        for member in safe_members:
            parsed_last = _gateway_parse_timestamp(member.get("last_report"))
            if parsed_last is None:
                continue
            if latest_reported is None or parsed_last > latest_reported:
                latest_reported = parsed_last
                latest_reported_device = {
                    "serial_number": _gateway_clean_text(member.get("serial_number")),
                    "name": _gateway_clean_text(member.get("name")),
                    "status": _gateway_clean_text(
                        member.get("statusText")
                        if member.get("statusText") is not None
                        else member.get("status")
                    ),
                }

    snapshot: dict[str, object] = {
        "total_inverters": total_inverters,
        "reporting_inverters": reporting,
        "not_reporting_inverters": not_reporting,
        "unknown_inverters": unknown,
        "status_counts": status_counts,
        "status_summary": bucket.get("status_summary"),
        "model_summary": bucket.get("model_summary"),
        "firmware_summary": bucket.get("firmware_summary"),
        "array_summary": bucket.get("array_summary"),
        "panel_info": (
            dict(bucket.get("panel_info")) if isinstance(bucket.get("panel_info"), dict) else None
        ),
        "status_type_counts": (
            dict(bucket.get("status_type_counts"))
            if isinstance(bucket.get("status_type_counts"), dict)
            else None
        ),
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "production_start_date": bucket.get("production_start_date"),
        "production_end_date": bucket.get("production_end_date"),
    }
    connectivity_state = bucket.get("connectivity_state")
    if not isinstance(connectivity_state, str) or not connectivity_state.strip():
        connectivity_state = _microinverter_connectivity_state(snapshot)
    snapshot["connectivity_state"] = connectivity_state
    return snapshot


def _gateway_channel_type_kind(value: object) -> str | None:
    text = _gateway_clean_text(value)
    if not text:
        return None
    normalized = "".join(ch if ch.isalnum() else "_" for ch in text.lower())
    if "production" in normalized or normalized in ("prod", "pv", "solar"):
        return "production"
    if "consumption" in normalized or normalized in ("cons", "load", "site_load"):
        return "consumption"
    return None


_NON_ATTR_CHARS_RE = re.compile(r"[^a-z0-9]+")


def _gateway_attr_key(key: object) -> str | None:
    text = _gateway_clean_text(key)
    if not text:
        return None
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", text)
    normalized = _NON_ATTR_CHARS_RE.sub("_", normalized.lower()).strip("_")
    return normalized or None


def _gateway_flat_member_attributes(
    member: dict[str, object],
    *,
    skip_keys: set[str] | None = None,
) -> dict[str, object]:
    flattened: dict[str, object] = {}
    skip = skip_keys or set()
    for raw_key, raw_value in member.items():
        key = _gateway_attr_key(raw_key)
        if not key or key in skip:
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, (str, int, float, bool)):
            value = raw_value
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
            flattened[key] = value
    return flattened


def _gateway_meter_member(
    coord: EnphaseCoordinator, meter_kind: str
) -> dict[str, object] | None:
    bucket = coord.type_bucket("envoy") or {}
    members = bucket.get("devices")
    if not isinstance(members, list):
        return None
    for member in members:
        if not isinstance(member, dict):
            continue
        kind = _gateway_channel_type_kind(member.get("channel_type"))
        if kind is None:
            name = _gateway_clean_text(member.get("name")) or ""
            if "production" in name.lower():
                kind = "production"
            elif "consumption" in name.lower():
                kind = "consumption"
        if kind == meter_kind:
            return dict(member)
    return None


def _gateway_meter_status_text(member: dict[str, object] | None) -> str | None:
    if not isinstance(member, dict):
        return None
    status_text = _gateway_clean_text(member.get("statusText"))
    if status_text:
        return status_text
    status_raw = _gateway_clean_text(member.get("status"))
    if not status_raw:
        return None
    return status_raw.replace("_", " ").replace("-", " ").title()


def _gateway_meter_last_reported(member: dict[str, object] | None) -> datetime | None:
    if not isinstance(member, dict):
        return None
    for key in _GATEWAY_LAST_REPORT_KEYS:
        parsed = _gateway_parse_timestamp(member.get(key))
        if parsed is not None:
            return parsed
    return None


def _gateway_system_controller_member(
    coord: EnphaseCoordinator,
) -> dict[str, object] | None:
    bucket = coord.type_bucket("envoy") or {}
    members = bucket.get("devices")
    if not isinstance(members, list):
        return None
    for member in members:
        if not isinstance(member, dict):
            continue
        channel_type = (_gateway_clean_text(member.get("channel_type")) or "").lower()
        if channel_type in ("enpower", "system_controller", "systemcontroller"):
            return dict(member)
        name = (_gateway_clean_text(member.get("name")) or "").lower()
        if "system controller" in name:
            return dict(member)
    return None


class _SiteBaseEntity(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {
            "last_success_utc",
            "last_failure_utc",
            "backoff_ends_utc",
            "last_failure_response",
        }
    )

    def __init__(
        self, coord: EnphaseCoordinator, key: str, _name: str, type_key: str = "envoy"
    ):
        super().__init__(coord)
        self._coord = coord
        self._key = key
        self._type_key = type_key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{key}"

    @property
    def available(self) -> bool:
        if not _type_available(self._coord, self._type_key):
            return False
        if self._coord.last_success_utc is not None:
            return True
        return super().available

    def _cloud_diag_attrs(
        self, *, include_last_success: bool = True
    ) -> dict[str, object]:
        attrs: dict[str, object] = {}
        if include_last_success and self._coord.last_success_utc:
            attrs["last_success_utc"] = self._coord.last_success_utc.isoformat()
        if self._coord.last_failure_utc:
            attrs["last_failure_utc"] = self._coord.last_failure_utc.isoformat()
        if self._coord.last_failure_status is not None:
            attrs["last_failure_status"] = self._coord.last_failure_status
        if self._coord.last_failure_description:
            attrs["code_description"] = self._coord.last_failure_description
        if self._coord.last_failure_response:
            attrs["last_failure_response"] = self._coord.last_failure_response
        if self._coord.last_failure_source:
            attrs["last_failure_source"] = self._coord.last_failure_source
        if self._coord.backoff_ends_utc:
            attrs["backoff_ends_utc"] = self._coord.backoff_ends_utc.isoformat()
        return attrs

    def _backoff_remaining_seconds(self) -> int | None:
        ends = self._coord.backoff_ends_utc
        if ends is None:
            return None
        try:
            remaining = (ends - dt_util.utcnow()).total_seconds()
        except Exception:
            return None
        if remaining <= 0:
            return 0
        rounded = int(round(remaining))
        if rounded <= 0:
            return 1
        return rounded

    @property
    def extra_state_attributes(self):
        return self._cloud_diag_attrs()

    @property
    def device_info(self):
        type_device_info = getattr(self._coord, "type_device_info", None)
        info = (
            type_device_info(self._type_key) if callable(type_device_info) else None
        )
        if info is not None:
            return info
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:{self._type_key}")},
            manufacturer="Enphase",
        )


class EnphaseSiteEnergySensor(_SiteBaseEntity, RestoreSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coord: EnphaseCoordinator,
        flow_key: str,
        translation_key: str,
        name: str,
    ) -> None:
        super().__init__(coord, flow_key, name)
        self._flow_key = flow_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{flow_key}"
        self._restored_value: float | None = None
        self._restored_reset_at: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            try:
                restored = (
                    float(last.native_value) if last.native_value is not None else None
                )
            except Exception:  # noqa: BLE001
                restored = None
            if restored is not None and restored >= 0:
                self._restored_value = restored
                self._attr_native_value = restored
        try:
            last_state = await self.async_get_last_state()
        except Exception:  # noqa: BLE001
            last_state = None
        if last_state is not None:
            reset_attr = (last_state.attributes or {}).get("last_reset_at")
            if isinstance(reset_attr, str):
                self._restored_reset_at = reset_attr

    def _flow_data(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        flows = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(self._coord, "site_energy", None)
        ) or {}
        entry = flows.get(self._flow_key)
        if isinstance(entry, SiteEnergyFlow):
            try:
                return entry.__dict__
            except Exception:  # noqa: BLE001
                return {}
        if isinstance(entry, dict):
            return entry
        return {}

    def _current_value(self) -> float | None:
        data = self._flow_data()
        val = data.get("value_kwh")
        if val is None:
            return None
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._current_value() is not None:
            return True
        return self._restored_value is not None

    @property
    def native_value(self):
        current = self._current_value()
        if current is not None:
            return current
        return self._restored_value

    @property
    def extra_state_attributes(self):
        data = self._flow_data()
        last_report_raw = data.get("last_report_date")
        last_report_iso = None
        if isinstance(last_report_raw, datetime):
            last_report_iso = last_report_raw.isoformat()
        elif last_report_raw is not None:
            try:
                last_report_iso = str(last_report_raw)
            except Exception:  # noqa: BLE001
                last_report_iso = None
        attrs = {
            "start_date": data.get("start_date"),
            "last_report_date": last_report_iso,
            "bucket_count": data.get("bucket_count"),
            "source_fields": data.get("fields_used"),
            "source_unit": data.get("source_unit") or "Wh",
        }
        if data.get("interval_minutes") is not None:
            attrs["interval_minutes"] = data.get("interval_minutes")
        reset_at = data.get("last_reset_at") or self._restored_reset_at
        if reset_at:
            attrs["last_reset_at"] = reset_at
        update_pending = data.get("update_pending")
        if update_pending is not None:
            attrs["update_pending"] = bool(update_pending)
        evse_flow = None
        try:
            flows = (
                getattr(getattr(self._coord, "energy", None), "site_energy", None) or {}
            )
            evse_flow = flows.get("evse_charging")
        except Exception:  # noqa: BLE001
            evse_flow = None
        evse_value = None
        if isinstance(evse_flow, SiteEnergyFlow):
            evse_value = evse_flow.value_kwh
        elif isinstance(evse_flow, dict):
            evse_value = evse_flow.get("value_kwh")
        if evse_value is not None:
            try:
                attrs["evse_charging_kwh"] = float(evse_value)
            except Exception:  # noqa: BLE001
                attrs["evse_charging_kwh"] = None
        return attrs


class EnphaseSiteLastUpdateSensor(_SiteBaseEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_successful_update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "last_update", "Last Successful Update")

    @property
    def native_value(self):
        return self._coord.last_success_utc


class EnphaseCloudLatencySensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_latency"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "latency_ms", "Cloud Latency")

    @property
    def native_value(self):
        return self._coord.latency_ms

    @property
    def extra_state_attributes(self):
        return self._cloud_diag_attrs(include_last_success=False)


class EnphaseSiteLastErrorCodeSensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_error_code"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "last_error_code", "Cloud Error Code")

    @property
    def native_value(self):
        failure_ts = self._coord.last_failure_utc
        success_ts = self._coord.last_success_utc
        failure_active = bool(
            failure_ts and (success_ts is None or failure_ts > success_ts)
        )
        if not failure_active:
            return STATE_NONE
        code = self._coord.last_failure_status
        if code is None:
            description = (self._coord.last_failure_description or "").lower()
            if self._coord.last_failure_source == "network":
                dns_tokens = (
                    "dns",
                    "name or service not known",
                    "temporary failure in name resolution",
                    "resolv",
                )
                if any(token in description for token in dns_tokens):
                    return "dns_error"
                return "network_error"
            return STATE_NONE
        return str(code)

    @property
    def extra_state_attributes(self):
        return self._cloud_diag_attrs(include_last_success=False)


class EnphaseSiteBackoffEndsSensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_backoff_ends"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "backoff_ends", "Cloud Backoff Ends")
        self._expiry_cancel: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._ensure_expiry_timer()

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        self._cancel_expiry_timer()

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._ensure_expiry_timer()

    @property
    def native_value(self):
        ends = self._coord.backoff_ends_utc
        if ends is None:
            return None
        try:
            now = dt_util.utcnow()
        except Exception:  # noqa: BLE001
            return None
        if ends <= now:
            return None
        return ends

    @property
    def extra_state_attributes(self):
        return self._cloud_diag_attrs(include_last_success=False)

    @callback
    def _ensure_expiry_timer(self) -> None:
        if self.hass is None:
            return
        ends = self._coord.backoff_ends_utc
        try:
            now = dt_util.utcnow()
        except Exception:  # noqa: BLE001
            self._cancel_expiry_timer()
            return
        if ends is None or ends <= now:
            self._cancel_expiry_timer()
            return
        self._cancel_expiry_timer()
        fire_at = ends + timedelta(seconds=1)
        self._expiry_cancel = async_track_point_in_utc_time(
            self.hass, self._handle_backoff_expired, fire_at
        )

    @callback
    def _handle_backoff_expired(self, _now: datetime) -> None:
        self._cancel_expiry_timer()
        self.async_write_ha_state()

    @callback
    def _cancel_expiry_timer(self) -> None:
        if self._expiry_cancel:
            try:
                self._expiry_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._expiry_cancel = None


class EnphaseSystemControllerInventorySensor(_SiteBaseEntity):
    _attr_translation_key = "system_controller_inventory"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "last_reported_utc",
            "last_reported",
            "last_report",
            "last_reported_at",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "type_enpower_inventory",
            "System Controller",
            type_key="envoy",
        )

    def _member(self) -> dict[str, object] | None:
        return _gateway_system_controller_member(self._coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._member() is not None

    @property
    def native_value(self):
        return _gateway_meter_status_text(self._member())

    @property
    def extra_state_attributes(self):
        member = self._member()
        if not isinstance(member, dict):
            return {}
        last_reported = _gateway_meter_last_reported(member)
        attrs = {
            "name": _gateway_clean_text(member.get("name")) or "System Controller",
            "status_text": _gateway_meter_status_text(member),
            "status_raw": _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            ),
            "connected": _gateway_optional_bool(member.get("connected")),
            "channel_type": _gateway_clean_text(member.get("channel_type")),
            "serial_number": _gateway_clean_text(member.get("serial_number")),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
        }
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "status_text",
                    "status_raw",
                    "connected",
                    "channel_type",
                    "serial_number",
                    "last_reported_utc",
                    "status",
                    "statusText",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                },
            )
        )
        return attrs


class _EnphaseGatewayMeterSensor(_SiteBaseEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "meter_attributes",
            "last_reported_utc",
        }
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        meter_kind: str,
        label: str,
    ) -> None:
        super().__init__(
            coord,
            f"gateway_{meter_kind}_meter",
            label,
            type_key="envoy",
        )
        self._meter_kind = meter_kind

    def _member(self) -> dict[str, object] | None:
        return _gateway_meter_member(self._coord, self._meter_kind)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._member() is not None

    @property
    def native_value(self):
        return _gateway_meter_status_text(self._member())

    @property
    def extra_state_attributes(self):
        member = self._member()
        if not isinstance(member, dict):
            return {}
        last_reported = _gateway_meter_last_reported(member)
        status_text = _gateway_meter_status_text(member)
        attrs: dict[str, object] = {
            "meter_name": _gateway_clean_text(member.get("name")),
            "meter_type": self._meter_kind,
            "channel_type": _gateway_clean_text(member.get("channel_type")),
            "serial_number": _gateway_clean_text(member.get("serial_number")),
            "connected": _gateway_optional_bool(member.get("connected")),
            "status_text": status_text,
            "status_raw": _gateway_clean_text(
                member.get("statusText") if member.get("statusText") is not None else member.get("status")
            ),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
            "ip_address": _gateway_clean_text(
                member.get("ip")
                if member.get("ip") is not None
                else member.get("ip_address")
            ),
            "meter_attributes": dict(member),
        }
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "channel_type",
                    "serial_number",
                    "connected",
                    "status_text",
                    "status_raw",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "ip",
                    "ip_address",
                },
            )
        )
        return attrs


class EnphaseGatewayProductionMeterSensor(_EnphaseGatewayMeterSensor):
    _attr_translation_key = "gateway_production_meter"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "production", "Production Meter")


class EnphaseGatewayConsumptionMeterSensor(_EnphaseGatewayMeterSensor):
    _attr_translation_key = "gateway_consumption_meter"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "consumption", "Consumption Meter")


class EnphaseGatewayConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "latest_reported_utc",
            "latest_reported_device",
            "property_keys",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "gateway_connectivity_status",
            "Gateway Connectivity Status",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _gateway_inventory_snapshot(self._coord)
        if int(snapshot.get("total_devices", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        return _gateway_connectivity_state(_gateway_inventory_snapshot(self._coord))

    @property
    def extra_state_attributes(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        return {
            "total_devices": snapshot.get("total_devices"),
            "connected_devices": snapshot.get("connected_devices"),
            "disconnected_devices": snapshot.get("disconnected_devices"),
            "unknown_connection_devices": snapshot.get("unknown_connection_devices"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "model_summary": snapshot.get("model_summary"),
            "firmware_summary": snapshot.get("firmware_summary"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "property_keys": snapshot.get("property_keys"),
        }


class EnphaseGatewayConnectedDevicesSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_connected_devices"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "gateway_connected_devices",
            "Gateway Connected Devices",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _gateway_inventory_snapshot(self._coord)
        if int(snapshot.get("total_devices", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        if int(snapshot.get("total_devices", 0) or 0) <= 0:
            return None
        return int(snapshot.get("connected_devices", 0) or 0)

    @property
    def extra_state_attributes(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        return {
            "total_devices": snapshot.get("total_devices"),
            "connected_devices": snapshot.get("connected_devices"),
            "disconnected_devices": snapshot.get("disconnected_devices"),
            "unknown_connection_devices": snapshot.get("unknown_connection_devices"),
            "connectivity_state": _gateway_connectivity_state(snapshot),
        }


class EnphaseGatewayLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "gateway_last_reported",
            "Gateway Last Reported",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _gateway_inventory_snapshot(self._coord)
        if snapshot.get("latest_reported") is not None:
            return True
        return int(snapshot.get("total_devices", 0) or 0) > 0

    @property
    def native_value(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        return snapshot.get("latest_reported")

    @property
    def extra_state_attributes(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "without_last_report_count": snapshot.get("without_last_report_count"),
            "total_devices": snapshot.get("total_devices"),
            "status_summary": snapshot.get("status_summary"),
        }


class EnphaseMicroinverterConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "latest_reported_utc",
            "latest_reported_device",
            "panel_info",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "microinverter_connectivity_status",
            "Microinverter Connectivity Status",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        return _microinverter_inventory_snapshot(self._coord).get("connectivity_state")

    @property
    def extra_state_attributes(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return {
            "total_inverters": snapshot.get("total_inverters"),
            "reporting_inverters": snapshot.get("reporting_inverters"),
            "not_reporting_inverters": snapshot.get("not_reporting_inverters"),
            "unknown_inverters": snapshot.get("unknown_inverters"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "model_summary": snapshot.get("model_summary"),
            "firmware_summary": snapshot.get("firmware_summary"),
            "array_summary": snapshot.get("array_summary"),
            "status_type_counts": snapshot.get("status_type_counts"),
            "panel_info": snapshot.get("panel_info"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
        }


class EnphaseMicroinverterReportingCountSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_reporting_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "microinverter_reporting_count",
            "Microinverter Reporting Count",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) <= 0:
            return None
        return int(snapshot.get("reporting_inverters", 0) or 0)

    @property
    def extra_state_attributes(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return {
            "total_inverters": snapshot.get("total_inverters"),
            "reporting_inverters": snapshot.get("reporting_inverters"),
            "not_reporting_inverters": snapshot.get("not_reporting_inverters"),
            "unknown_inverters": snapshot.get("unknown_inverters"),
            "connectivity_state": snapshot.get("connectivity_state"),
            "status_summary": snapshot.get("status_summary"),
        }


class EnphaseMicroinverterLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "microinverter_last_reported",
            "Microinverter Last Reported",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if snapshot.get("latest_reported") is not None:
            return True
        return int(snapshot.get("total_inverters", 0) or 0) > 0

    @property
    def native_value(self):
        return _microinverter_inventory_snapshot(self._coord).get("latest_reported")

    @property
    def extra_state_attributes(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "total_inverters": snapshot.get("total_inverters"),
            "reporting_inverters": snapshot.get("reporting_inverters"),
            "not_reporting_inverters": snapshot.get("not_reporting_inverters"),
            "unknown_inverters": snapshot.get("unknown_inverters"),
            "status_summary": snapshot.get("status_summary"),
            "production_start_date": snapshot.get("production_start_date"),
            "production_end_date": snapshot.get("production_end_date"),
        }


class EnphaseStormAlertSensor(_SiteBaseEntity):
    _attr_translation_key = "storm_alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "storm_alert", "Storm Alert", type_key="encharge")

    @property
    def native_value(self):
        active = self._coord.storm_alert_active
        if active is None:
            return None
        return "active" if active else "inactive"

    @property
    def extra_state_attributes(self):
        alerts = getattr(self._coord, "storm_alerts", None)
        if not isinstance(alerts, list):
            alerts = []
        return {
            "storm_alert_active": self._coord.storm_alert_active,
            "critical_alert_override": getattr(
                self._coord, "storm_alert_critical_override", None
            ),
            "storm_alert_count": len(alerts),
            "storm_alerts": alerts,
        }


class EnphaseBatteryOverallChargeSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_overall_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_overall_charge",
            "Battery Overall Charge",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.battery_aggregate_charge_pct is not None

    @property
    def native_value(self):
        value = self._coord.battery_aggregate_charge_pct
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        summary = self._coord.battery_status_summary
        return {
            "aggregate_status": summary.get("aggregate_status"),
            "aggregate_charge_source": summary.get("aggregate_charge_source"),
            "included_count": summary.get("included_count"),
            "contributing_count": summary.get("contributing_count"),
            "missing_energy_capacity_keys": summary.get("missing_energy_capacity_keys"),
            "excluded_count": summary.get("excluded_count"),
            "available_energy_kwh": summary.get("available_energy_kwh"),
            "max_capacity_kwh": summary.get("max_capacity_kwh"),
            "site_current_charge_pct": summary.get("site_current_charge_pct"),
            "site_available_energy_kwh": summary.get("site_available_energy_kwh"),
            "site_max_capacity_kwh": summary.get("site_max_capacity_kwh"),
            "site_available_power_kw": summary.get("site_available_power_kw"),
            "site_max_power_kw": summary.get("site_max_power_kw"),
            "site_total_micros": summary.get("site_total_micros"),
            "site_active_micros": summary.get("site_active_micros"),
            "site_inactive_micros": summary.get("site_inactive_micros"),
            "site_included_count": summary.get("site_included_count"),
            "site_excluded_count": summary.get("site_excluded_count"),
            "battery_order": summary.get("battery_order"),
        }


class EnphaseBatteryOverallStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_overall_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_overall_status",
            "Battery Overall Status",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.battery_aggregate_status is not None

    @property
    def native_value(self):
        return self._coord.battery_aggregate_status

    @property
    def extra_state_attributes(self):
        summary = self._coord.battery_status_summary
        return {
            "aggregate_charge_pct": summary.get("aggregate_charge_pct"),
            "aggregate_charge_source": summary.get("aggregate_charge_source"),
            "included_count": summary.get("included_count"),
            "contributing_count": summary.get("contributing_count"),
            "missing_energy_capacity_keys": summary.get("missing_energy_capacity_keys"),
            "excluded_count": summary.get("excluded_count"),
            "worst_storage_key": summary.get("worst_storage_key"),
            "worst_status": summary.get("worst_status"),
            "per_battery_status": summary.get("per_battery_status"),
            "per_battery_status_raw": summary.get("per_battery_status_raw"),
            "per_battery_status_text": summary.get("per_battery_status_text"),
            "battery_order": summary.get("battery_order"),
        }


class EnphaseBatteryAvailableEnergySensor(_SiteBaseEntity):
    _attr_translation_key = "battery_available_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_available_energy",
            "Battery Available Energy",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        summary = self._coord.battery_status_summary
        value = summary.get("site_available_energy_kwh")
        if value is None:
            return None
        try:
            return round(float(value), 3)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        summary = self._coord.battery_status_summary
        return {
            "site_max_capacity_kwh": summary.get("site_max_capacity_kwh"),
            "site_current_charge_pct": summary.get("site_current_charge_pct"),
            "included_count": summary.get("site_included_count"),
            "excluded_count": summary.get("site_excluded_count"),
        }


class EnphaseBatteryAvailablePowerSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_available_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_available_power",
            "Battery Available Power",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        summary = self._coord.battery_status_summary
        value = summary.get("site_available_power_kw")
        if value is None:
            return None
        try:
            return round(float(value), 3)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        summary = self._coord.battery_status_summary
        return {"site_max_power_kw": summary.get("site_max_power_kw")}


class EnphaseBatteryInactiveMicroinvertersSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_inactive_microinverters"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_inactive_microinverters",
            "Battery Inactive Microinverters",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        summary = self._coord.battery_status_summary
        value = summary.get("site_inactive_micros")
        if value is None:
            return None
        try:
            return int(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        summary = self._coord.battery_status_summary
        return {
            "site_total_micros": summary.get("site_total_micros"),
            "site_active_micros": summary.get("site_active_micros"),
        }


class EnphaseBatteryModeSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_mode"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "battery_mode", "Battery Mode", type_key="encharge")

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.battery_grid_mode is not None

    @property
    def native_value(self):
        return self._coord.battery_mode_display

    @property
    def extra_state_attributes(self):
        start_time = getattr(self._coord, "battery_charge_from_grid_start_time", None)
        end_time = getattr(self._coord, "battery_charge_from_grid_end_time", None)
        return {
            "mode_raw": self._coord.battery_grid_mode,
            "charge_from_grid_allowed": self._coord.battery_charge_from_grid_allowed,
            "discharge_to_grid_allowed": self._coord.battery_discharge_to_grid_allowed,
            "charge_from_grid_enabled": getattr(
                self._coord, "battery_charge_from_grid_enabled", None
            ),
            "charge_from_grid_schedule_enabled": getattr(
                self._coord, "battery_charge_from_grid_schedule_enabled", None
            ),
            "charge_from_grid_start_time": (
                start_time.isoformat() if start_time is not None else None
            ),
            "charge_from_grid_end_time": (
                end_time.isoformat() if end_time is not None else None
            ),
            "shutdown_level": getattr(self._coord, "battery_shutdown_level", None),
            "shutdown_level_min": getattr(
                self._coord, "battery_shutdown_level_min", None
            ),
            "shutdown_level_max": getattr(
                self._coord, "battery_shutdown_level_max", None
            ),
            "hide_charge_from_grid": getattr(
                self._coord, "_battery_hide_charge_from_grid", None
            ),
            "envoy_supports_vls": getattr(
                self._coord, "_battery_envoy_supports_vls", None
            ),
            "use_battery_for_self_consumption": getattr(
                self._coord, "battery_use_battery_for_self_consumption", None
            ),
        }


class EnphaseGridControlStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "grid_control_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "grid_control_status",
            "Grid Control Status",
            type_key="enpower",
        )

    @property
    def available(self) -> bool:
        if not _grid_control_site_applicable(self._coord):
            return False
        if not (
            _type_available(self._coord, "enpower")
            or _type_available(self._coord, "envoy")
        ):
            return False
        if self._coord.last_success_utc is not None:
            return True
        return bool(getattr(self._coord, "last_update_success", False))

    @property
    def native_value(self):
        if not self._coord.grid_control_supported:
            return None
        if self._coord.grid_toggle_pending:
            return "pending"
        allowed = self._coord.grid_toggle_allowed
        if allowed is True:
            return "ready"
        if allowed is False:
            return "blocked"
        return None

    @property
    def extra_state_attributes(self):
        return {
            "grid_toggle_pending": self._coord.grid_toggle_pending,
            "blocked_reasons": self._coord.grid_toggle_blocked_reasons,
            "disable_grid_control": self._coord.grid_control_disable,
            "active_download": self._coord.grid_control_active_download,
            "sunlight_backup_system_check": self._coord.grid_control_sunlight_backup_system_check,
            "grid_outage_check": self._coord.grid_control_grid_outage_check,
            "user_initiated_grid_toggle": self._coord.grid_control_user_initiated_toggle,
        }

    @property
    def device_info(self):
        type_device_info = getattr(self._coord, "type_device_info", None)
        if callable(type_device_info):
            for type_key in ("enpower", "envoy"):
                info = type_device_info(type_key)
                if info is not None:
                    return info
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class EnphaseGridModeSensor(_SiteBaseEntity):
    _attr_translation_key = "grid_mode"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "grid_mode", "Grid Mode", type_key="enpower")

    @property
    def available(self) -> bool:
        if not _grid_control_site_applicable(self._coord):
            return False
        if not (
            _type_available(self._coord, "enpower")
            or _type_available(self._coord, "envoy")
        ):
            return False
        if self._coord.last_success_utc is not None:
            return True
        return bool(getattr(self._coord, "last_update_success", False))

    @property
    def native_value(self):
        mode = getattr(self._coord, "grid_mode", None)
        if mode in {"on_grid", "off_grid", "unknown"}:
            return mode
        return "unknown"

    @property
    def extra_state_attributes(self):
        return {
            "raw_states": getattr(self._coord, "grid_mode_raw_states", []),
            "grid_control_supported": self._coord.grid_control_supported,
            "grid_toggle_allowed": self._coord.grid_toggle_allowed,
        }

    @property
    def device_info(self):
        type_device_info = getattr(self._coord, "type_device_info", None)
        if callable(type_device_info):
            for type_key in ("enpower", "envoy"):
                info = type_device_info(type_key)
                if info is not None:
                    return info
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class EnphaseSystemProfileStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "system_profile_status"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "system_profile_status",
            "System Profile Status",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._coord.battery_controls_available:
            return True
        return self._coord.battery_profile is not None

    @property
    def native_value(self):
        if self._coord.battery_profile_pending:
            return "pending"
        return self._coord.battery_effective_profile_display

    @property
    def extra_state_attributes(self):
        labels = self._coord.battery_profile_option_labels
        attrs = {
            "effective_profile": self._coord.battery_profile,
            "effective_profile_label": self._coord.battery_effective_profile_display,
            "effective_reserve_percentage": self._coord.battery_effective_backup_percentage,
            "effective_operation_mode_sub_type": self._coord.battery_effective_operation_mode_sub_type,
            "requested_profile": self._coord.battery_pending_profile,
            "requested_profile_label": labels.get(
                self._coord.battery_pending_profile or ""
            ),
            "requested_reserve_percentage": self._coord.battery_pending_backup_percentage,
            "requested_operation_mode_sub_type": self._coord.battery_pending_operation_mode_sub_type,
            "pending": self._coord.battery_profile_pending,
            "pending_requested_at": (
                self._coord.battery_pending_requested_at.isoformat()
                if self._coord.battery_pending_requested_at
                else None
            ),
            "selected_profile": self._coord.battery_selected_profile,
            "selected_profile_label": self._coord.battery_profile_display,
            "selected_reserve_percentage": self._coord.battery_selected_backup_percentage,
            "selected_operation_mode_sub_type": self._coord.battery_selected_operation_mode_sub_type,
            "available_profile_keys": self._coord.battery_profile_option_keys,
            "available_profile_labels": labels,
        }
        attrs["supports_mqtt"] = getattr(self._coord, "battery_supports_mqtt", None)
        attrs["polling_interval_seconds"] = getattr(
            self._coord, "battery_profile_polling_interval", None
        )
        attrs["cfg_control_show"] = getattr(
            self._coord, "battery_cfg_control_show", None
        )
        attrs["cfg_control_enabled"] = getattr(
            self._coord, "battery_cfg_control_enabled", None
        )
        attrs["cfg_control_schedule_supported"] = getattr(
            self._coord, "battery_cfg_control_schedule_supported", None
        )
        attrs["cfg_control_force_schedule_supported"] = getattr(
            self._coord, "battery_cfg_control_force_schedule_supported", None
        )
        attrs["site_show_production"] = getattr(
            self._coord, "battery_show_production", None
        )
        attrs["site_show_consumption"] = getattr(
            self._coord, "battery_show_consumption", None
        )
        attrs["site_show_charge_from_grid"] = getattr(
            self._coord, "_battery_show_charge_from_grid", None
        )
        attrs["site_show_savings_mode"] = getattr(
            self._coord, "_battery_show_savings_mode", None
        )
        attrs["site_show_full_backup"] = getattr(
            self._coord, "_battery_show_full_backup", None
        )
        attrs["site_show_storm_guard"] = getattr(
            self._coord, "battery_show_storm_guard", None
        )
        attrs["site_show_backup_percentage"] = getattr(
            self._coord, "battery_show_battery_backup_percentage", None
        )
        attrs["site_has_encharge"] = getattr(self._coord, "battery_has_encharge", None)
        attrs["site_has_enpower"] = getattr(self._coord, "battery_has_enpower", None)
        attrs["site_charging_modes_enabled"] = getattr(
            self._coord, "battery_is_charging_modes_enabled", None
        )
        attrs["site_country_code"] = getattr(self._coord, "battery_country_code", None)
        attrs["site_region"] = getattr(self._coord, "battery_region", None)
        attrs["site_locale"] = getattr(self._coord, "battery_locale", None)
        attrs["site_timezone"] = getattr(self._coord, "battery_timezone", None)
        attrs["site_user_is_owner"] = getattr(
            self._coord, "battery_user_is_owner", None
        )
        attrs["site_user_is_installer"] = getattr(
            self._coord, "battery_user_is_installer", None
        )
        attrs["site_status_code"] = getattr(
            self._coord, "battery_site_status_code", None
        )
        attrs["site_status_text"] = getattr(
            self._coord, "battery_site_status_text", None
        )
        attrs["site_status_severity"] = getattr(
            self._coord, "battery_site_status_severity", None
        )
        attrs["feature_details"] = getattr(self._coord, "battery_feature_details", {})
        evse_profile = getattr(self._coord, "battery_profile_evse_device", None)
        if isinstance(evse_profile, dict):
            attrs["evse_profile"] = evse_profile
        return attrs
