from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfLength, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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
            # Daily energy derived from lifetime meter; monotonic within a day
            per_serial_entities.append(EnphaseEnergyTodaySensor(coord, sn))
            per_serial_entities.append(EnphaseConnectorStatusSensor(coord, sn))
            per_serial_entities.append(EnphaseConnectionSensor(coord, sn))
            per_serial_entities.append(EnphaseIpAddressSensor(coord, sn))
            per_serial_entities.append(EnphaseReportingIntervalSensor(coord, sn))
            per_serial_entities.append(EnphaseDynamicLoadBalancingSensor(coord, sn))
            per_serial_entities.append(EnphasePowerSensor(coord, sn))
            per_serial_entities.append(EnphaseChargingLevelSensor(coord, sn))
            per_serial_entities.append(EnphaseSessionDurationSensor(coord, sn))
            per_serial_entities.append(EnphaseLastReportedSensor(coord, sn))
            per_serial_entities.append(EnphaseChargeModeSensor(coord, sn))
            per_serial_entities.append(EnphaseMaxCurrentSensor(coord, sn))
            per_serial_entities.append(EnphaseMinAmpSensor(coord, sn))
            per_serial_entities.append(EnphaseMaxAmpSensor(coord, sn))
            per_serial_entities.append(EnphasePhaseModeSensor(coord, sn))
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
class _EnergyTodayRestoreData(ExtraStoredData):
    """Persist internal tracking for Energy Today sensor without exposing attributes."""

    baseline_kwh: float | None
    baseline_day: str | None
    last_total_kwh: float | None
    last_reset_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "baseline_kwh": self.baseline_kwh,
            "baseline_day": self.baseline_day,
            "last_total_kwh": self.last_total_kwh,
            "last_reset_at": self.last_reset_at,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "_EnergyTodayRestoreData":
        if not isinstance(data, dict):
            return cls(None, None, None, None)
        baseline = data.get("baseline_kwh")
        baseline_day = data.get("baseline_day")
        last_total = data.get("last_total_kwh")
        last_reset = data.get("last_reset_at")
        try:
            baseline_f = float(baseline) if baseline is not None else None
        except Exception:  # noqa: BLE001
            baseline_f = None
        try:
            last_total_f = float(last_total) if last_total is not None else None
        except Exception:  # noqa: BLE001
            last_total_f = None
        baseline_day_str = str(baseline_day) if baseline_day is not None else None
        last_reset_str = str(last_reset) if last_reset is not None else None
        return cls(baseline_f, baseline_day_str, last_total_f, last_reset_str)


class EnphaseEnergyTodaySensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    # Daily total that resets at midnight; monotonic within a day
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "energy_today"
    # Treat large backward jumps as meter resets instead of jitter
    _reset_drop_threshold_kwh = 5.0
    _reset_floor_kwh = 5.0
    _session_reset_drop_threshold_kwh = 0.05

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_energy_today"
        self._baseline_kwh: float | None = None
        self._baseline_day: str | None = None  # YYYY-MM-DD in local time
        self._last_value: float | None = None
        self._last_total: float | None = None
        self._last_reset_at: str | None = None
        self._attr_name = "Energy Today"

    def _ensure_baseline(self, total_kwh: float) -> None:
        now_local = dt_util.now()
        day_str = now_local.strftime("%Y-%m-%d")
        if self._baseline_day != day_str or self._baseline_kwh is None:
            self._baseline_day = day_str
            self._baseline_kwh = float(total_kwh)
            self._last_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_extra = await self.async_get_last_extra_data()
        extra_data = _EnergyTodayRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        if extra_data.last_total_kwh is not None:
            self._last_total = extra_data.last_total_kwh
        if extra_data.last_reset_at is not None:
            self._last_reset_at = extra_data.last_reset_at
        if not last_state:
            return
        try:
            last_attrs = last_state.attributes or {}
            last_baseline = last_attrs.get("baseline_kwh")
            last_day = last_attrs.get("baseline_day")
            today = dt_util.now().strftime("%Y-%m-%d")
            baseline_restored = False
            if self._last_total is None and last_attrs.get("last_total_kwh") is not None:
                try:
                    self._last_total = float(last_attrs["last_total_kwh"])
                except Exception:
                    self._last_total = None
            if self._last_reset_at is None:
                reset_attr = last_attrs.get("last_reset_at")
                if isinstance(reset_attr, str):
                    self._last_reset_at = reset_attr
            # Only restore baseline if it's the same local day
            if self._baseline_kwh is None:
                candidate_baseline = extra_data.baseline_kwh
                candidate_day = extra_data.baseline_day
                if candidate_baseline is not None and candidate_day == today:
                    self._baseline_kwh = candidate_baseline
                    self._baseline_day = candidate_day
                    baseline_restored = True
            if (
                self._baseline_kwh is None
                and last_baseline is not None
                and last_day == today
            ):
                self._baseline_kwh = float(last_baseline)
                self._baseline_day = str(last_day)
                baseline_restored = True
            if baseline_restored:
                # Keep continuity by restoring last numeric value when valid
                try:
                    self._last_value = float(last_state.state)
                except Exception:
                    self._last_value = None
        except Exception:
            # On any parsing issue, skip restore; baseline will be re-established
            return

    @property
    def native_value(self):
        data = self.data
        lifetime_val = self._value_from_lifetime(data)
        status_val = self._value_from_status(data)
        if status_val is not None:
            return status_val
        session_val = self._value_from_sessions(data)
        if session_val is not None:
            return session_val
        return lifetime_val

    def _value_from_status(self, data) -> float | None:
        energy_kwh = data.get("session_kwh")
        val: float | None = None
        if energy_kwh is not None:
            try:
                val = float(energy_kwh)
            except Exception:  # noqa: BLE001
                val = None
        if val is None:
            energy_wh = data.get("session_energy_wh")
            if energy_wh is None:
                return None
            try:
                energy_wh_f = float(energy_wh)
            except Exception:  # noqa: BLE001
                return None
            try:
                val = energy_wh_f / 1000.0 if energy_wh_f > 200 else energy_wh_f
            except Exception:  # noqa: BLE001
                val = None
        if val is None:
            return None
        val = max(0.0, round(val, 3))
        # Treat significant drops as a reset (new session/day) and capture metadata
        if self._last_value is not None and val + 0.005 < self._last_value:
            drop = self._last_value - val
            if drop >= self._session_reset_drop_threshold_kwh or val <= 0.05:
                # Legitimate reset (session completed or new day)
                self._last_reset_at = dt_util.utcnow().isoformat()
            else:
                # Avoid small backwards jitter from API rounding
                val = self._last_value
        self._last_value = val
        return val

    def _value_from_lifetime(self, data) -> float | None:
        total = data.get("lifetime_kwh")
        if total is None:
            return None
        try:
            total_f = float(total)
        except Exception:
            return None
        self._ensure_baseline(total_f)
        if (
            self._last_total is not None
            and total_f + 0.05 < self._last_total
            and (
                (self._last_total - total_f) >= self._reset_drop_threshold_kwh
                or total_f <= self._reset_floor_kwh
            )
        ):
            now_local = dt_util.now()
            self._baseline_kwh = float(total_f)
            self._baseline_day = now_local.strftime("%Y-%m-%d")
            self._last_reset_at = dt_util.utcnow().isoformat()
            self._last_value = 0.0
        val = max(0.0, round(total_f - (self._baseline_kwh or 0.0), 3))
        if self._last_value is not None and val + 0.005 < self._last_value:
            val = self._last_value
        self._last_value = val
        self._last_total = total_f
        return val

    def _value_from_sessions(self, data) -> float | None:
        sessions = data.get("energy_today_sessions")
        if not sessions:
            return None
        total = data.get("energy_today_sessions_kwh")
        if total is None:
            return None
        try:
            val = max(0.0, round(float(total), 3))
        except Exception:
            return None
        if self._last_value is not None and val + 0.005 < self._last_value:
            drop = self._last_value - val
            if drop >= self._session_reset_drop_threshold_kwh or val <= 0.05:
                self._last_reset_at = dt_util.utcnow().isoformat()
            else:
                val = self._last_value
        self._last_value = val
        return val

    @property
    def extra_state_attributes(self):
        return self._session_metadata_attributes(
            self.data, hass=self.hass  # type: ignore[arg-type]
        )

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return _EnergyTodayRestoreData(
            baseline_kwh=self._baseline_kwh,
            baseline_day=self._baseline_day,
            last_total_kwh=self._last_total,
            last_reset_at=self._last_reset_at,
        )

    @staticmethod
    def _session_metadata_attributes(data: dict, hass=None) -> dict[str, object]:
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

        plug_in = _localize(data.get("session_plug_in_at"))
        plug_out = _localize(data.get("session_plug_out_at"))
        result["plugged_in_at"] = plug_in
        result["plugged_out_at"] = plug_out

        energy_kwh_val: float | None = None
        energy_wh_val: float | None = None
        session_kwh = data.get("session_kwh")
        if session_kwh is not None:
            try:
                energy_kwh_val = round(float(session_kwh), 3)
            except Exception:  # noqa: BLE001
                energy_kwh_val = None
        energy_wh_raw = data.get("session_energy_wh")
        if energy_wh_raw is not None:
            try:
                energy_wh_f = float(energy_wh_raw)
            except Exception:  # noqa: BLE001
                energy_wh_f = None
            if energy_wh_f is not None:
                if energy_kwh_val is None:
                    if energy_wh_f > 200:
                        energy_kwh_val = round(energy_wh_f / 1000.0, 3)
                        energy_wh_val = round(energy_wh_f, 3)
                    else:
                        energy_kwh_val = round(energy_wh_f, 3)
                        energy_wh_val = round(energy_wh_f * 1000.0, 3)
                else:
                    energy_wh_val = round(energy_kwh_val * 1000.0, 3)
        if energy_kwh_val is not None and energy_wh_val is None:
            energy_wh_val = round(energy_kwh_val * 1000.0, 3)
        if energy_wh_val is not None:
            result["energy_consumed_wh"] = energy_wh_val
        else:
            result["energy_consumed_wh"] = None
        if energy_kwh_val is not None:
            result["energy_consumed_kwh"] = energy_kwh_val
        else:
            result["energy_consumed_kwh"] = None

        session_cost = data.get("session_cost")
        if session_cost is not None:
            try:
                result["session_cost"] = round(float(session_cost), 3)
            except Exception:  # noqa: BLE001
                result["session_cost"] = session_cost
        else:
            result["session_cost"] = None

        session_charge_level = data.get("session_charge_level")
        if session_charge_level is not None:
            try:
                result["session_charge_level"] = int(session_charge_level)
            except Exception:  # noqa: BLE001
                result["session_charge_level"] = session_charge_level
        else:
            result["session_charge_level"] = None

        range_value = data.get("session_miles")
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

        if converted_range is not None:
            try:
                result["range_added"] = round(converted_range, 3)
            except Exception:  # noqa: BLE001
                result["range_added"] = converted_range
        else:
            result["range_added"] = None

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


class EnphaseConnectionSensor(_BaseEVSensor):
    _attr_translation_key = "connection"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "Connection", "connection")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        raw = super().native_value
        if raw is None:
            return None
        val = str(raw).strip()
        return val or None


class EnphaseIpAddressSensor(_BaseEVSensor):
    _attr_translation_key = "ip_address"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "IP Address", "ip_address")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        raw = super().native_value
        if raw is None:
            return None
        val = str(raw).strip()
        return val or None


class EnphaseReportingIntervalSensor(_BaseEVSensor):
    _attr_translation_key = "reporting_interval"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "Reporting Interval", "reporting_interval")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        raw = super().native_value
        if raw is None:
            return None
        try:
            return int(raw)
        except Exception:
            try:
                return int(str(raw).strip())
            except Exception:
                return None


class EnphaseDynamicLoadBalancingSensor(_BaseEVSensor):
    _attr_translation_key = "dlb_status"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "Dynamic Load Balancing", "dlb_enabled")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        raw = super().native_value
        if raw is None:
            return None
        return "enabled" if bool(raw) else "disabled"

    @property
    def icon(self) -> str | None:
        raw = super().native_value
        if raw is None:
            return "mdi:lightning-bolt-outline"
        return "mdi:lightning-bolt" if bool(raw) else "mdi:lightning-bolt-outline"


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


class EnphaseSessionDurationSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_translation_key = "session_duration"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_session_duration"

    @property
    def native_value(self):
        data = self.data
        start = data.get("session_start")
        if not start:
            return 0
        try:
            start_i = int(start)
        except Exception:
            return 0
        # Prefer a fixed end recorded by coordinator after stop; else if charging,
        # compute duration to now; otherwise return 0
        end = data.get("session_end")
        charging = bool(data.get("charging"))
        if isinstance(end, (int, float)):
            end_i = int(end)
        elif charging:
            from datetime import datetime, timezone

            end_i = int(datetime.now(timezone.utc).timestamp())
        else:
            return 0
        minutes = max(0, int((end_i - start_i) / 60))
        return minutes


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


class EnphaseMaxCurrentSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "max_amp"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_max_current"

    @property
    def native_value(self):
        return self.data.get("max_current")


class EnphaseMinAmpSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "min_amp"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_min_amp"

    @property
    def native_value(self):
        return self.data.get("min_amp")


class EnphaseMaxAmpSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "max_amp"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_max_amp"

    @property
    def native_value(self):
        return self.data.get("max_amp")


class EnphasePhaseModeSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "phase_mode"
    _attr_icon = "mdi:transmission-tower"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_phase_mode"

    @property
    def native_value(self):
        v = self.data.get("phase_mode")
        if v is None:
            return None
        # Map numeric phase indicators to friendly text
        try:
            n = int(v)
            if n == 1:
                return "Single Phase"
            if n == 3:
                return "Three Phase"
        except Exception:
            pass
        return v


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
            return STATE_NONE
        return str(code)


class EnphaseSiteBackoffEndsSensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_backoff_ends"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "backoff_ends", "Cloud Backoff Ends")

    @property
    def native_value(self):
        if self._coord.backoff_ends_utc is None:
            return STATE_NONE
        try:
            return self._coord.backoff_ends_utc.isoformat()
        except Exception:
            return STATE_NONE
