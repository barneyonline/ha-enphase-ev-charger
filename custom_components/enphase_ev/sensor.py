
from __future__ import annotations

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = []
    # Site-level diagnostic sensors
    entities.append(EnphaseSiteLastUpdateSensor(coord))
    entities.append(EnphaseCloudLatencySensor(coord))
    serials = list(coord.serials or coord.data.keys())
    for sn in serials:
        # Daily energy derived from lifetime meter; monotonic within a day
        entities.append(EnphaseEnergyTodaySensor(coord, sn))
        entities.append(EnphaseConnectorStatusSensor(coord, sn))
        entities.append(EnphasePowerSensor(coord, sn))
        entities.append(EnphaseChargingLevelSensor(coord, sn))
        entities.append(EnphaseSessionDurationSensor(coord, sn))
        entities.append(EnphaseLastReportedSensor(coord, sn))
        entities.append(EnphaseChargeModeSensor(coord, sn))
        entities.append(EnphaseMaxCurrentSensor(coord, sn))
        entities.append(EnphaseMinAmpSensor(coord, sn))
        entities.append(EnphaseMaxAmpSensor(coord, sn))
        entities.append(EnphasePhaseModeSensor(coord, sn))
        entities.append(EnphaseStatusSensor(coord, sn))
        entities.append(EnphaseLifetimeEnergySensor(coord, sn))
        # The following sensors were removed due to unreliable values in most deployments:
        # Connector Reason, Schedule Type/Start/End, Session Miles, Session Plug timestamps
    async_add_entities(entities)

class _BaseEVSensor(EnphaseBaseEntity, SensorEntity):
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str, key: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name_suffix
        self._attr_unique_id = f"{DOMAIN}_{sn}_{key}"

    @property
    def native_value(self):
        d = (self._coord.data or {}).get(self._sn) or {}
        return d.get(self._key)

class EnphaseEnergyTodaySensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"
    # Daily total that resets at midnight; monotonic within a day
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "energy_today"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_energy_today"
        self._baseline_kwh: float | None = None
        self._baseline_day: str | None = None  # YYYY-MM-DD in local time
        self._last_value: float | None = None
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
        if not last_state:
            return
        try:
            last_attrs = last_state.attributes or {}
            last_baseline = last_attrs.get("baseline_kwh")
            last_day = last_attrs.get("baseline_day")
            today = dt_util.now().strftime("%Y-%m-%d")
            # Only restore baseline if it's the same local day
            if last_baseline is not None and last_day == today:
                self._baseline_kwh = float(last_baseline)
                self._baseline_day = str(last_day)
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
        d = (self._coord.data or {}).get(self._sn) or {}
        total = d.get("lifetime_kwh")
        if total is None:
            return None
        try:
            total_f = float(total)
        except Exception:
            return None
        self._ensure_baseline(total_f)
        # Compute today's energy as the delta from baseline; never below 0
        val = max(0.0, round(total_f - (self._baseline_kwh or 0.0), 3))
        # Guard against occasional jitter causing tiny negative dips
        if self._last_value is not None and val + 0.005 < self._last_value:
            val = self._last_value
        self._last_value = val
        return val

    @property
    def extra_state_attributes(self):
        return {
            "baseline_kwh": self._baseline_kwh,
            "baseline_day": self._baseline_day,
        }

class EnphaseConnectorStatusSensor(_BaseEVSensor):
    _attr_translation_key = "connector_status"
    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Connector Status", "connector_status")
        from homeassistant.helpers.entity import EntityCategory
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
    @property
    def icon(self) -> str | None:
        d = (self._coord.data or {}).get(self._sn) or {}
        v = str(d.get("connector_status") or "").upper()
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

class EnphasePowerSensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_translation_key = "power"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_power"
        self._baseline_kwh: float | None = None
        self._baseline_day: str | None = None
        self._last_energy_kwh: float | None = None
        self._last_ts: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state:
            return
        attrs = last_state.attributes or {}
        try:
            self._baseline_kwh = float(attrs.get("baseline_kwh")) if attrs.get("baseline_kwh") is not None else None
        except Exception:
            self._baseline_kwh = None
        self._baseline_day = attrs.get("baseline_day")
        try:
            last_energy_attr = attrs.get("last_energy_today_kwh")
            self._last_energy_kwh = float(last_energy_attr) if last_energy_attr is not None else None
        except Exception:
            self._last_energy_kwh = None
        try:
            self._last_ts = float(attrs.get("last_ts")) if attrs.get("last_ts") is not None else None
        except Exception:
            self._last_ts = None

    def _ensure_energy_baseline(self, lifetime_kwh: float) -> None:
        # Mirror daily baseline logic used by Energy Today sensor
        now_local = dt_util.now()
        day_str = now_local.strftime("%Y-%m-%d")
        if self._baseline_day != day_str or self._baseline_kwh is None:
            self._baseline_day = day_str
            self._baseline_kwh = float(lifetime_kwh)
            # Reset last sample at day rollover
            self._last_energy_kwh = 0.0
            self._last_ts = None

    @property
    def native_value(self):
        # Derive average power from the rate of change of today's energy
        d = (self._coord.data or {}).get(self._sn) or {}
        lifetime = d.get("lifetime_kwh")
        if lifetime is None:
            return 0
        try:
            lifetime_f = float(lifetime)
        except Exception:
            return 0
        self._ensure_energy_baseline(lifetime_f)
        energy_today = max(0.0, float(lifetime_f - (self._baseline_kwh or 0.0)))
        now_ts = dt_util.now().timestamp()
        if self._last_ts is None or self._last_energy_kwh is None:
            # Seed and report 0 on first sample
            self._last_ts = now_ts
            self._last_energy_kwh = energy_today
            return 0
        dt_s = max(0.0, now_ts - self._last_ts)
        delta_kwh = energy_today - self._last_energy_kwh
        # Update lasts for next cycle
        self._last_ts = now_ts
        self._last_energy_kwh = energy_today
        # Handle day reset or jitter
        if dt_s <= 0.0 or delta_kwh <= 0.0:
            return 0
        # kWh per second -> W: kWh * 3600000 / s
        watts = int(round(delta_kwh * 3_600_000.0 / dt_s))
        # Avoid tiny noise
        if watts < 0:
            watts = 0
        return watts

    @property
    def extra_state_attributes(self):
        d = (self._coord.data or {}).get(self._sn) or {}
        return {
            "baseline_kwh": self._baseline_kwh,
            "baseline_day": self._baseline_day,
            "last_energy_today_kwh": self._last_energy_kwh,
            "last_ts": self._last_ts,
            "operating_v": d.get("operating_v") or 230,
            "method": "derived_from_energy_today",
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
        d = (self._coord.data or {}).get(self._sn) or {}
        lvl = d.get("charging_level")
        if lvl is None:
            # Fall back to last set amps; if unknown, prefer 32A default
            return int(self._coord.last_set_amps.get(self._sn) or 32)
        try:
            return int(lvl)
        except Exception:
            return int(self._coord.last_set_amps.get(self._sn) or 32)

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
        d = (self._coord.data or {}).get(self._sn) or {}
        start = d.get("session_start")
        if not start:
            return 0
        try:
            start_i = int(start)
        except Exception:
            return 0
        # Prefer a fixed end recorded by coordinator after stop; else if charging,
        # compute duration to now; otherwise return 0
        end = d.get("session_end")
        charging = bool(d.get("charging"))
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
        d = (self._coord.data or {}).get(self._sn) or {}
        s = d.get("last_reported_at")
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
        d = (self._coord.data or {}).get(self._sn) or {}
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

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_lifetime_kwh"
        # Track last good value to avoid publishing bad/zero on startup
        self._last_value: float | None = None
        # Apply a one-shot boot filter to ignore an initial 0/None
        self._boot_filter: bool = True

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

    @property
    def native_value(self):
        d = (self._coord.data or {}).get(self._sn) or {}
        raw = d.get("lifetime_kwh")
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
                return self._last_value
            if val < self._last_value:
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
        d = (self._coord.data or {}).get(self._sn) or {}
        return d.get("max_current")

class EnphaseMinAmpSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "min_amp"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_min_amp"
    @property
    def native_value(self):
        d = (self._coord.data or {}).get(self._sn) or {}
        return d.get("min_amp")

class EnphaseMaxAmpSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "max_amp"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_max_amp"
    @property
    def native_value(self):
        d = (self._coord.data or {}).get(self._sn) or {}
        return d.get("max_amp")

class EnphasePhaseModeSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "phase_mode"
    _attr_icon = "mdi:transmission-tower"
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_phase_mode"
    @property
    def native_value(self):
        d = (self._coord.data or {}).get(self._sn) or {}
        v = d.get("phase_mode")
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
        d = (self._coord.data or {}).get(self._sn) or {}
        return d.get("status")


## Removed duplicate Current Amps sensor to avoid confusion with Set Amps


## Removed unreliable sensors: Session Miles


class _TimestampFromIsoSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: EnphaseCoordinator, sn: str, key: str, name: str, uniq: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = uniq

    @property
    def native_value(self):
        from datetime import datetime, timezone
        d = (self._coord.data or {}).get(self._sn) or {}
        s = d.get(self._key)
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

    def __init__(self, coord: EnphaseCoordinator, sn: str, key: str, name: str, uniq: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = uniq

    @property
    def native_value(self):
        from datetime import datetime, timezone
        d = (self._coord.data or {}).get(self._sn) or {}
        ts = d.get(self._key)
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
