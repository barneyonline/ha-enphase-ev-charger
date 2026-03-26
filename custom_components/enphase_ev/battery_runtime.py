from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_PROFILE_DEFAULT_RESERVE,
    BATTERY_PROFILE_LABELS,
    SAVINGS_OPERATION_MODE_SUBTYPE,
)

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator


class BatteryRuntime:
    """Battery profile selection and pending-state helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    @staticmethod
    def normalize_battery_profile_key(value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return normalized or None

    @staticmethod
    def battery_profile_label(profile: str | None) -> str | None:
        if not profile:
            return None
        if profile in BATTERY_PROFILE_LABELS:
            return BATTERY_PROFILE_LABELS[profile]
        try:
            return str(profile).replace("_", " ").replace("-", " ").title()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_pending_sub_type(
        coordinator: EnphaseCoordinator, profile: str, sub_type: str | None
    ) -> str | None:
        if profile != "cost_savings":
            return None
        return coordinator._normalize_battery_sub_type(sub_type)

    def clear_battery_pending(self) -> None:
        coord = self.coordinator
        coord._battery_pending_profile = None
        coord._battery_pending_reserve = None
        coord._battery_pending_sub_type = None
        coord._battery_pending_requested_at = None
        coord._battery_pending_require_exact_settings = True
        coord._sync_battery_profile_pending_issue()

    def set_battery_pending(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None,
        require_exact_settings: bool = True,
    ) -> None:
        coord = self.coordinator
        coord._battery_pending_profile = profile
        coord._battery_pending_reserve = reserve
        coord._battery_pending_sub_type = self._normalize_pending_sub_type(
            coord, profile, sub_type
        )
        coord._battery_pending_requested_at = dt_util.utcnow()
        coord._battery_pending_require_exact_settings = bool(require_exact_settings)
        coord._sync_battery_profile_pending_issue()

    def effective_profile_matches_pending(self) -> bool:
        coord = self.coordinator
        pending_profile = getattr(coord, "_battery_pending_profile", None)
        if not pending_profile:
            return False
        if getattr(coord, "_battery_profile", None) != pending_profile:
            return False
        if not getattr(coord, "_battery_pending_require_exact_settings", True):
            return True
        pending_reserve = getattr(coord, "_battery_pending_reserve", None)
        if (
            pending_reserve is not None
            and getattr(coord, "_battery_backup_percentage", None) != pending_reserve
        ):
            return False
        if pending_profile != "cost_savings":
            return True
        pending_subtype = coord._normalize_battery_sub_type(
            getattr(coord, "_battery_pending_sub_type", None)
        )
        effective_subtype = coord._normalize_battery_sub_type(
            getattr(coord, "_battery_operation_mode_sub_type", None)
        )
        if pending_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
            return effective_subtype == SAVINGS_OPERATION_MODE_SUBTYPE
        if pending_subtype is None:
            return effective_subtype != SAVINGS_OPERATION_MODE_SUBTYPE
        return pending_subtype == effective_subtype

    def remember_battery_reserve(
        self, profile: str | None, reserve: int | None
    ) -> None:
        if not profile or reserve is None:
            return
        normalized = self.normalize_battery_profile_key(profile)
        if not normalized or normalized not in BATTERY_PROFILE_DEFAULT_RESERVE:
            return
        self.coordinator._battery_profile_reserve_memory[normalized] = int(reserve)

    def target_reserve_for_profile(self, profile: str) -> int:
        remembered = self.coordinator._battery_profile_reserve_memory.get(profile)
        if remembered is not None:
            return self.coordinator._normalize_battery_reserve_for_profile(
                profile, remembered
            )
        default = BATTERY_PROFILE_DEFAULT_RESERVE.get(profile, 20)
        return self.coordinator._normalize_battery_reserve_for_profile(profile, default)

    def current_savings_sub_type(self) -> str | None:
        selected_subtype = self.coordinator.battery_selected_operation_mode_sub_type
        if selected_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
            return SAVINGS_OPERATION_MODE_SUBTYPE
        return None
