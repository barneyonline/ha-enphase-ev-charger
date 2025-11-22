from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfLength, UnitOfPower
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

PARALLEL_UPDATES = 0

STATE_NONE = "none"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # Site-level diagnostic sensors
    site_entities = [
        EnphaseSiteLastUpdateSensor(coord),
        EnphaseCloudLatencySensor(coord),
        EnphaseSiteLastErrorCodeSensor(coord),
        EnphaseSiteBackoffEndsSensor(coord),
    ]
    async_add_entities(site_entities, update_before_add=False)

    known_serials: set[str] = set()

    @callback
    def _async_sync_chargers() -> None:
        serials = [
            sn for sn in coord.iter_serials() if sn and sn not in known_serials
        ]
        if not serials:
            return
        per_serial_entities = []
        for sn in serials:
            per_serial_entities.append(EnphaseEnergyTodaySensor(coord, sn))
            per_serial_entities.append(EnphaseConnectorStatusSensor(coord, sn))
            per_serial_entities.append(EnphasePowerSensor(coord, sn))
            per_serial_entities.append(EnphaseChargingLevelSensor(coord, sn))
            per_serial_entities.append(EnphaseLastReportedSensor(coord, sn))
            per_serial_entities.append(EnphaseChargeModeSensor(coord, sn))
            per_serial_entities.append(EnphaseStatusSensor(coord, sn))
            per_serial_entities.append(EnphaseLifetimeEnergySensor(coord, sn))
            # The following sensors were removed due to unreliable values in most deployments:
            # Connector Reason, Schedule Type/Start/End, Session Miles, Session Plug timestamps
        if per_serial_entities:
            async_add_entities(per_serial_entities, update_before_add=False)
            known_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class _BaseEVSensor(EnphaseBaseEntity, SensorEntity):
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str, key: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name_suffix
        self._attr_unique_id = f"{DOMAIN}_{sn}_{key}"

    @property
    def native_value(self):
        return self.data.get(self._key)


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
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "last_session"

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
        }

    def _extract_history_session(self, data: dict) -> dict | None:
        sessions = data.get("energy_today_sessions") or []
        if not sessions:
            return None
        latest = sessions[-1]
        energy_kwh, energy_wh = self._coerce_energy(
            latest.get("energy_kwh_total") or latest.get("energy_kwh"), None
        )
        start = self._coerce_timestamp(latest.get("start"))
        end = self._coerce_timestamp(latest.get("end"))
        session_key_raw = latest.get("session_id") or latest.get("sessionId")
        session_key = None
        if session_key_raw is not None:
            try:
                session_key = str(session_key_raw)
            except Exception:  # noqa: BLE001
                session_key = None
        if session_key is None and (start is not None or end is not None):
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
            "session_miles": latest.get("miles_added")
            if latest.get("miles_added") is not None
            else latest.get("range_added"),
            "session_key": session_key,
        }

    @staticmethod
    def _compute_duration_minutes(start: float | None, end: float | None, charging: bool) -> int | None:
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

        if realtime and (realtime["charging"] or realtime.get("energy_kwh") is not None):
            return realtime
        if history and history.get("energy_kwh") is not None:
            return history
        return realtime or history

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
        return self._session_metadata_attributes(
            self.data,
            hass=self.hass,  # type: ignore[arg-type]
            context=self._last_context,
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

        session_data = context or {}
        plug_in = _localize(session_data.get("plug_in_at") or data.get("session_plug_in_at"))
        plug_out = _localize(session_data.get("plug_out_at") or data.get("session_plug_out_at"))
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
        reason = self.data.get("connector_reason")
        if reason in (None, ""):
            return {}
        try:
            reason_str = str(reason)
        except Exception:  # noqa: BLE001
            reason_str = reason
        return {"status_reason": reason_str}


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
            if not bool(data.get("charging")):
                self._last_power_w = 0
                self._last_method = "idle"
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
        if delta_kwh <= self._MIN_DELTA_KWH:
            if not bool(data.get("charging")):
                self._last_power_w = 0
                self._last_method = "idle"
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
        return {
            "last_lifetime_kwh": self._last_lifetime_kwh,
            "last_energy_ts": self._last_energy_ts,
            "last_sample_ts": self._last_sample_ts,
            "last_power_w": self._last_power_w,
            "last_window_seconds": self._last_window_s,
            "method": self._last_method,
            "charging": bool(data.get("charging")),
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
    _attr_native_unit_of_measurement = "A"
    _attr_suggested_display_precision = 0

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charging_amps"

    @property
    def native_value(self):
        data = self.data
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
        return {
            "min_amp": min_amp,
            "max_amp": max_amp,
            "max_current": max_current,
        }


class EnphaseLastReportedSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
        interval_raw = self.data.get("reporting_interval")
        interval = None
        if interval_raw is not None:
            try:
                interval = int(str(interval_raw).strip())
            except Exception:  # noqa: BLE001
                interval = None
        return {"reporting_interval": interval}


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


class EnphaseLifetimeEnergySensor(EnphaseBaseEntity, RestoreSensor):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
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
        if self._boot_filter and val == 0 and (self._last_value or 0) > 0:
            return self._last_value

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

        # One-shot boot filter: ignore an initial None/0 which some backends
        # briefly emit at startup. Fall back to restored last value.
        if self._boot_filter:
            if val == 0 and (self._last_value or 0) > 0:
                return self._last_value
            # First good sample observed; disable boot filter
            self._boot_filter = False

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

        return {
            "commissioned": _as_bool(self.data.get("commissioned")),
            "charger_problem": _as_bool(self.data.get("faulted")),
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


class _SiteBaseEntity(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: EnphaseCoordinator, key: str, name: str):
        super().__init__(coord)
        self._coord = coord
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{key}"

    @property
    def available(self) -> bool:
        if self._coord.last_success_utc is not None:
            return True
        return super().available

    def _cloud_diag_attrs(self) -> dict[str, object]:
        attrs: dict[str, object] = {}
        if self._coord.last_success_utc:
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
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"site:{self._coord.site_id}")},
            manufacturer="Enphase",
            model="Enlighten Cloud",
            name=f"Enphase Site {self._coord.site_id}",
            translation_key="enphase_site",
            translation_placeholders={"site_id": str(self._coord.site_id)},
        )


class EnphaseSiteLastUpdateSensor(_SiteBaseEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_successful_update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "last_update", "Last Successful Update")

    @property
    def native_value(self):
        return self._coord.last_success_utc


class EnphaseCloudLatencySensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_latency"
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "latency_ms", "Cloud Latency")

    @property
    def native_value(self):
        return self._coord.latency_ms


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
            failure_ts
            and (success_ts is None or failure_ts > success_ts)
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
