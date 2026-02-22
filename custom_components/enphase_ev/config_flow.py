from __future__ import annotations

import inspect
import logging
import re
import time
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector

from .api import (
    AuthTokens,
    EnlightenAuthInvalidCredentials,
    EnlightenAuthInvalidOTP,
    EnlightenAuthMFARequired,
    EnlightenAuthOTPBlocked,
    EnlightenAuthUnavailable,
    async_authenticate,
    async_fetch_devices_inventory,
    async_fetch_chargers,
    async_resend_login_otp,
    async_validate_login_otp,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_INCLUDE_INVERTERS,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    CONF_SITE_ONLY,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    OPT_SCHEDULE_SYNC_ENABLED,
)
from .device_types import (
    ONBOARDING_SUPPORTED_TYPE_KEYS,
    active_type_serials_from_inventory,
    active_type_keys_from_inventory,
    normalize_type_key,
)

_LOGGER = logging.getLogger(__name__)

MFA_RESEND_DELAY_SECONDS = 30
CONF_OTP = "otp"
CONF_RESEND_CODE = "resend_code"
CONF_TYPE_ENVOY = "type_envoy"
CONF_TYPE_ENCHARGE = "type_encharge"
CONF_TYPE_IQEVSE = "type_iqevse"
CONF_TYPE_MICROINVERTER = "type_microinverter"

_TYPE_FIELD_BY_KEY: dict[str, str] = {
    "envoy": CONF_TYPE_ENVOY,
    "encharge": CONF_TYPE_ENCHARGE,
    "iqevse": CONF_TYPE_IQEVSE,
    "microinverter": CONF_TYPE_MICROINVERTER,
}


class EnphaseEVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._auth_tokens: AuthTokens | None = None
        self._sites: dict[str, str | None] = {}
        self._selected_site_id: str | None = None
        self._chargers: list[tuple[str, str | None]] = []
        self._chargers_loaded = False
        self._available_type_keys: list[str] = []
        self._inventory_iqevse_serials: list[str] = []
        self._type_keys_loaded = False
        self._inventory_unknown = False
        self._email: str | None = None
        self._remember_password = False
        self._password: str | None = None
        self._reconfigure_entry: ConfigEntry | None = None
        self._reauth_entry: ConfigEntry | None = None
        self._site_only = False
        self._include_inverters = True
        self._mfa_tokens: AuthTokens | None = None
        self._mfa_resend_available_at: float | None = None
        self._mfa_code_sent = False
        self._pending_user_errors: dict[str, str] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is None and self._pending_user_errors:
            errors = self._pending_user_errors
            self._pending_user_errors = None

        if user_input is not None:
            self._pending_user_errors = None
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            remember = bool(user_input.get(CONF_REMEMBER_PASSWORD, False))
            self._clear_mfa()

            session = async_get_clientsession(self.hass)
            try:
                tokens, sites = await async_authenticate(session, email, password)
            except EnlightenAuthInvalidCredentials:
                errors["base"] = "invalid_auth"
            except EnlightenAuthMFARequired as err:
                self._email = email
                self._remember_password = remember
                self._password = password if remember else None
                if isinstance(err.tokens, AuthTokens) and err.tokens.raw_cookies:
                    self._start_mfa(err.tokens)
                    return await self.async_step_mfa()
                errors["base"] = "mfa_required"
            except EnlightenAuthUnavailable:
                errors["base"] = "service_unavailable"
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Unexpected error during Enlighten authentication: %s", err
                )
                errors["base"] = "unknown"
            else:
                self._email = email
                self._remember_password = remember
                self._password = password if remember else None
                return await self._handle_auth_success(tokens, sites)

        defaults = {
            CONF_EMAIL: self._email or "",
            CONF_REMEMBER_PASSWORD: self._remember_password,
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=defaults[CONF_EMAIL]): selector(
                    {"text": {"type": "email"}}
                ),
                vol.Required(CONF_PASSWORD): selector({"text": {"type": "password"}}),
                vol.Optional(
                    CONF_REMEMBER_PASSWORD, default=defaults[CONF_REMEMBER_PASSWORD]
                ): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _start_mfa(self, tokens: AuthTokens) -> None:
        self._mfa_tokens = tokens
        self._mfa_resend_available_at = None
        self._mfa_code_sent = False

    def _clear_mfa(self) -> None:
        self._mfa_tokens = None
        self._mfa_resend_available_at = None
        self._mfa_code_sent = False

    def _mfa_can_resend(self) -> bool:
        if self._mfa_resend_available_at is None:
            return True
        return time.monotonic() >= self._mfa_resend_available_at

    async def _handle_auth_success(
        self, tokens: AuthTokens, sites: list[Any]
    ) -> FlowResult:
        self._auth_tokens = tokens
        self._sites = {site.site_id: site.name for site in sites}

        if self._reconfigure_entry:
            current_site = self._reconfigure_entry.data.get(CONF_SITE_ID)
            if current_site:
                current_site_id = str(current_site)
                if self._selected_site_id != current_site_id:
                    self._reset_discovery_cache()
                self._selected_site_id = current_site_id

        if len(self._sites) == 1 and not self._reconfigure_entry:
            selected_site = next(iter(self._sites))
            if self._selected_site_id != selected_site:
                self._reset_discovery_cache()
            self._selected_site_id = selected_site
            return await self.async_step_devices()
        return await self.async_step_site()

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if not self._mfa_tokens or not self._mfa_tokens.raw_cookies or not self._email:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}

        if user_input is None and not self._mfa_code_sent:
            errors = await self._send_mfa_code()
            self._mfa_code_sent = True
            if errors.get("base") == "invalid_auth":
                return await self._restart_login_with_error("invalid_auth")

        if user_input is not None:
            resend = bool(user_input.get(CONF_RESEND_CODE, False))
            otp = str(user_input.get(CONF_OTP, "")).strip()

            session = async_get_clientsession(self.hass)

            if resend:
                if not self._mfa_can_resend():
                    errors["base"] = "resend_wait"
                else:
                    errors = await self._send_mfa_code()
                    if errors.get("base") == "invalid_auth":
                        return await self._restart_login_with_error("invalid_auth")
            else:
                if not otp:
                    errors["base"] = "otp_required"
                else:
                    try:
                        tokens, sites = await async_validate_login_otp(
                            session,
                            self._email,
                            otp,
                            self._mfa_tokens.raw_cookies,
                        )
                    except EnlightenAuthInvalidOTP:
                        _LOGGER.warning("MFA code rejected by Enlighten")
                        errors["base"] = "otp_invalid"
                    except EnlightenAuthOTPBlocked:
                        _LOGGER.warning("MFA validation blocked by Enlighten")
                        errors["base"] = "otp_blocked"
                    except EnlightenAuthInvalidCredentials:
                        return await self._restart_login_with_error("invalid_auth")
                    except EnlightenAuthUnavailable:
                        _LOGGER.warning(
                            "Enlighten MFA validation temporarily unavailable"
                        )
                        errors["base"] = "service_unavailable"
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "Unexpected error during Enlighten MFA validation: %s", err
                        )
                        errors["base"] = "unknown"
                    else:
                        self._clear_mfa()
                        return await self._handle_auth_success(tokens, sites)

        schema = vol.Schema(
            {
                vol.Optional(CONF_OTP, default=""): selector(
                    {"text": {"type": "text"}}
                ),
                vol.Optional(CONF_RESEND_CODE, default=False): bool,
            }
        )
        return self.async_show_form(step_id="mfa", data_schema=schema, errors=errors)

    async def _send_mfa_code(self) -> dict[str, str]:
        if not self._mfa_tokens or not self._mfa_tokens.raw_cookies:
            return {"base": "unknown"}
        session = async_get_clientsession(self.hass)
        try:
            updated = await async_resend_login_otp(
                session, self._mfa_tokens.raw_cookies
            )
        except EnlightenAuthOTPBlocked:
            _LOGGER.warning("Enlighten MFA resend blocked")
            return {"base": "otp_blocked"}
        except EnlightenAuthInvalidCredentials:
            return {"base": "invalid_auth"}
        except EnlightenAuthUnavailable:
            _LOGGER.warning("Enlighten MFA resend temporarily unavailable")
            return {"base": "service_unavailable"}
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Unexpected error during Enlighten MFA resend: %s", err)
            return {"base": "unknown"}

        self._mfa_tokens = updated
        self._mfa_resend_available_at = time.monotonic() + MFA_RESEND_DELAY_SECONDS
        return {}

    async def _restart_login_with_error(self, error: str) -> FlowResult:
        _LOGGER.warning("Enlighten MFA session no longer valid; restarting login flow")
        self._clear_mfa()
        self._pending_user_errors = {"base": error}
        return await self.async_step_user()

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if self._reconfigure_entry and self._selected_site_id and user_input is None:
            # Keep the existing site locked during reconfigure; skip the picker UX.
            return await self.async_step_devices()

        errors: dict[str, str] = {}

        if user_input is not None:
            site_id_raw = user_input.get(CONF_SITE_ID)
            site_id = str(site_id_raw).strip() if site_id_raw is not None else ""
            if site_id:
                if not site_id.isdigit():
                    errors["base"] = "site_invalid"
                else:
                    if self._selected_site_id != site_id:
                        self._reset_discovery_cache()
                    self._selected_site_id = site_id
                    if self._selected_site_id not in self._sites:
                        self._sites[self._selected_site_id] = None
                    return await self.async_step_devices()
            else:
                errors["base"] = "site_required"

        options = [
            {
                "value": site_id,
                "label": f"{name} ({site_id})" if name else site_id,
            }
            for site_id, name in self._sites.items()
        ]

        if options:
            default_site_id = None
            if self._selected_site_id and self._selected_site_id in self._sites:
                default_site_id = self._selected_site_id
            else:
                default_site_id = options[0]["value"]
            schema = vol.Schema(
                {
                    vol.Required(CONF_SITE_ID, default=default_site_id): selector(
                        {"select": {"options": options, "multiple": False}}
                    )
                }
            )
        else:
            schema = vol.Schema({vol.Required(CONF_SITE_ID): str})

        return self.async_show_form(step_id="site", data_schema=schema, errors=errors)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        await self._ensure_device_selection_data()
        discovered_serials = self._discovered_serials()
        available_type_keys = self._available_type_keys_for_form(discovered_serials)
        default_selected_type_keys = self._default_selected_type_keys(
            available_type_keys
        )
        if (
            "microinverter" in _TYPE_FIELD_BY_KEY
            and "microinverter" not in available_type_keys
        ):
            available_type_keys.append("microinverter")

        if user_input is not None:
            selected_type_keys = self._selected_type_keys_from_user_input(
                user_input,
                available_type_keys,
                default_selected_type_keys=default_selected_type_keys,
            )
            selected_type_keys = self._merged_selected_type_keys_for_unknown_inventory(
                selected_type_keys, visible_type_keys=available_type_keys
            )
            scan_interval = int(
                user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            )
            selected_serials = []
            if "iqevse" in selected_type_keys:
                selected_serials = self._selected_iqevse_serials(discovered_serials)
            include_inverters = "microinverter" in selected_type_keys
            site_only_selected = "iqevse" not in selected_type_keys
            self._site_only = site_only_selected
            self._include_inverters = include_inverters
            return await self._finalize_login_entry(
                selected_serials,
                scan_interval,
                site_only_selected,
                include_inverters=include_inverters,
                selected_type_keys=selected_type_keys,
            )

        default_scan = self._default_scan_interval()
        schema_fields: dict[vol.Marker, object] = {}
        for type_key in available_type_keys:
            field_key = _TYPE_FIELD_BY_KEY[type_key]
            schema_fields[
                vol.Optional(field_key, default=type_key in default_selected_type_keys)
            ] = bool
        schema_fields[vol.Optional(CONF_SCAN_INTERVAL, default=default_scan)] = int
        errors: dict[str, str] = {}
        if self._inventory_unknown:
            errors["base"] = "service_unavailable"
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    async def _finalize_login_entry(
        self,
        serials: list[str],
        scan_interval: int,
        site_only: bool = False,
        *,
        include_inverters: bool = True,
        selected_type_keys: list[str] | None = None,
    ) -> FlowResult:
        if not self._auth_tokens or not self._selected_site_id:
            return self.async_abort(reason="unknown")

        if selected_type_keys is None:
            selected_type_keys = self._legacy_selected_type_keys(
                serials,
                include_inverters,
                site_only=site_only,
            )

        site_name = self._sites.get(self._selected_site_id)
        data = {
            CONF_SITE_ID: self._selected_site_id,
            CONF_SITE_NAME: site_name,
            CONF_SERIALS: serials,
            CONF_SCAN_INTERVAL: scan_interval,
            CONF_COOKIE: self._auth_tokens.cookie,
            CONF_EAUTH: self._auth_tokens.access_token,
            CONF_ACCESS_TOKEN: self._auth_tokens.access_token,
            CONF_SESSION_ID: self._auth_tokens.session_id,
            CONF_TOKEN_EXPIRES_AT: self._auth_tokens.token_expires_at,
            CONF_REMEMBER_PASSWORD: self._remember_password,
            CONF_EMAIL: self._email,
            CONF_SITE_ONLY: bool(site_only),
            CONF_INCLUDE_INVERTERS: bool(include_inverters),
            CONF_SELECTED_TYPE_KEYS: list(dict.fromkeys(selected_type_keys)),
        }
        if self._remember_password and self._password:
            data[CONF_PASSWORD] = self._password
        else:
            data.pop(CONF_PASSWORD, None)

        await self.async_set_unique_id(self._selected_site_id)

        if self._reconfigure_entry:
            reason = (
                "reauth_successful" if self._reauth_entry else "reconfigure_successful"
            )
            current_site_id_raw = (
                self._reconfigure_entry.unique_id
                or self._reconfigure_entry.data.get(CONF_SITE_ID)
            )
            current_site_id = (
                str(current_site_id_raw) if current_site_id_raw is not None else None
            )
            desired_site_id = self._selected_site_id
            if (
                current_site_id
                and desired_site_id
                and current_site_id != desired_site_id
            ):
                current_site_name = self._reconfigure_entry.data.get(CONF_SITE_NAME)
                desired_site_name = self._sites.get(desired_site_id)
                configured_label = (
                    f"{current_site_name} ({current_site_id})"
                    if current_site_name and current_site_id
                    else current_site_name or current_site_id or "current site"
                )
                requested_label = (
                    f"{desired_site_name} ({desired_site_id})"
                    if desired_site_name and desired_site_id
                    else desired_site_name or desired_site_id or "selected site"
                )
                return self.async_abort(
                    reason="wrong_account",
                    description_placeholders={
                        "configured_label": configured_label,
                        "requested_label": requested_label,
                    },
                )

            self._abort_if_unique_id_mismatch(reason="wrong_account")
            desired_title = str(self._selected_site_id)
            if self._reconfigure_entry.title != desired_title:
                self.hass.config_entries.async_update_entry(
                    self._reconfigure_entry, title=desired_title
                )
            merged = dict(self._reconfigure_entry.data)
            for key, value in data.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            if not self._remember_password:
                merged.pop(CONF_PASSWORD, None)
                if hasattr(self, "async_update_reload_and_abort"):
                    kwargs = {"data_updates": merged}
                    if (
                        "reason"
                        in inspect.signature(
                            self.async_update_reload_and_abort
                        ).parameters
                    ):
                        kwargs["reason"] = reason
                    result = self.async_update_reload_and_abort(
                        self._reconfigure_entry, **kwargs
                    )
                    if inspect.isawaitable(result):
                        return await result
                    return result
            self.hass.config_entries.async_update_entry(
                self._reconfigure_entry, data=merged
            )
            await self.hass.config_entries.async_reload(
                self._reconfigure_entry.entry_id
            )
            return self.async_abort(reason=reason)

        self._abort_if_unique_id_configured()
        title = str(self._selected_site_id)
        return self.async_create_entry(title=title, data=data)

    async def _ensure_chargers(self) -> None:
        if self._chargers_loaded:
            return
        if not self._auth_tokens or not self._selected_site_id:
            self._chargers_loaded = True
            return
        session = async_get_clientsession(self.hass)
        chargers = await async_fetch_chargers(
            session, self._selected_site_id, self._auth_tokens
        )
        self._chargers = [(c.serial, c.name) for c in chargers]
        self._chargers_loaded = True

    async def _ensure_available_type_keys(self) -> None:
        if self._type_keys_loaded:
            return
        self._type_keys_loaded = True
        self._inventory_unknown = False
        if not self._auth_tokens or not self._selected_site_id:
            self._available_type_keys = []
            self._inventory_iqevse_serials = []
            return
        session = async_get_clientsession(self.hass)
        payload = await async_fetch_devices_inventory(
            session, self._selected_site_id, self._auth_tokens
        )
        if payload is None:
            self._inventory_unknown = True
            self._available_type_keys = []
            self._inventory_iqevse_serials = []
            return
        self._inventory_iqevse_serials = active_type_serials_from_inventory(
            payload, type_key="iqevse"
        )
        self._available_type_keys = [
            key
            for key in active_type_keys_from_inventory(
                payload,
                allowed_type_keys=ONBOARDING_SUPPORTED_TYPE_KEYS,
            )
            if key in _TYPE_FIELD_BY_KEY
        ]

    async def _ensure_device_selection_data(self) -> None:
        if not self._chargers_loaded:
            await self._ensure_chargers()
        if not self._type_keys_loaded:
            await self._ensure_available_type_keys()

    def _reset_discovery_cache(self) -> None:
        self._chargers = []
        self._chargers_loaded = False
        self._available_type_keys = []
        self._inventory_iqevse_serials = []
        self._type_keys_loaded = False
        self._inventory_unknown = False

    def _normalize_serials(self, value: Any) -> list[str]:
        if isinstance(value, list):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        serials = []
        for item in iterable:
            itm = str(item).strip()
            if itm and itm not in serials:
                serials.append(itm)
        return serials

    def _discovered_serials(self) -> list[str]:
        return [serial for serial, _name in self._chargers if serial]

    def _selected_iqevse_serials(self, discovered_serials: list[str]) -> list[str]:
        serials: list[str] = []
        for source in (
            discovered_serials,
            self._inventory_iqevse_serials,
            self._stored_configured_serials(),
        ):
            for serial in source:
                if serial and serial not in serials:
                    serials.append(serial)
        return serials

    def _available_type_keys_for_form(self, discovered_serials: list[str]) -> list[str]:
        available = list(self._available_type_keys)
        if self._inventory_unknown:
            available.extend(
                self._fallback_type_keys_for_unknown_inventory(discovered_serials)
            )
        if discovered_serials and "iqevse" not in available:
            available.append("iqevse")
        ordered: list[str] = []
        for type_key in ONBOARDING_SUPPORTED_TYPE_KEYS:
            if type_key in available and type_key in _TYPE_FIELD_BY_KEY:
                ordered.append(type_key)
        return ordered

    def _default_include_inverters(self) -> bool:
        if self._reconfigure_entry:
            return bool(self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True))
        return bool(self._include_inverters)

    def _default_scan_interval(self) -> int:
        if self._reconfigure_entry:
            return int(
                self._reconfigure_entry.data.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                )
            )
        return DEFAULT_SCAN_INTERVAL

    def _normalize_type_keys(self, value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        out: list[str] = []
        for item in iterable:
            normalized = normalize_type_key(item)
            if (
                normalized
                and normalized in _TYPE_FIELD_BY_KEY
                and normalized not in out
            ):
                out.append(normalized)
        return out

    def _default_selected_type_keys(self, available_type_keys: list[str]) -> list[str]:
        if (
            self._reconfigure_entry
            and CONF_SELECTED_TYPE_KEYS in self._reconfigure_entry.data
        ):
            configured = self._normalize_type_keys(
                self._reconfigure_entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
            )
            return [key for key in available_type_keys if key in configured]

        selected = set(available_type_keys)
        if self._reconfigure_entry:
            configured_serials = self._normalize_serials(
                self._reconfigure_entry.data.get(CONF_SERIALS, [])
            )
            if not configured_serials or bool(
                self._reconfigure_entry.data.get(CONF_SITE_ONLY, False)
            ):
                selected.discard("iqevse")
            if not bool(self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True)):
                selected.discard("microinverter")
        else:
            if self._site_only:
                selected.discard("iqevse")
            if not self._include_inverters:
                selected.discard("microinverter")
        return [key for key in available_type_keys if key in selected]

    def _selected_type_keys_from_user_input(
        self,
        user_input: dict[str, Any],
        available_type_keys: list[str],
        *,
        default_selected_type_keys: list[str],
    ) -> list[str]:
        selected: list[str] = []
        for type_key in available_type_keys:
            field_key = _TYPE_FIELD_BY_KEY.get(type_key)
            if not field_key:
                continue
            enabled = bool(
                user_input.get(field_key, type_key in default_selected_type_keys)
            )
            if enabled:
                selected.append(type_key)
        return selected

    def _legacy_selected_type_keys(
        self,
        serials: list[str],
        include_inverters: bool,
        *,
        site_only: bool = False,
    ) -> list[str]:
        discovered_serials = self._discovered_serials()
        available_type_keys = self._available_type_keys_for_form(discovered_serials)
        if available_type_keys:
            selected = set(available_type_keys)
            if site_only or not serials:
                selected.discard("iqevse")
            if not include_inverters:
                selected.discard("microinverter")
            return [key for key in available_type_keys if key in selected]

        selected = ["envoy", "encharge"]
        if serials and not site_only:
            selected.append("iqevse")
        if include_inverters:
            selected.append("microinverter")
        return selected

    def _stored_selected_type_keys(self) -> list[str]:
        if not self._reconfigure_entry:
            return []
        if CONF_SELECTED_TYPE_KEYS in self._reconfigure_entry.data:
            return self._normalize_type_keys(
                self._reconfigure_entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
            )
        return self._legacy_selected_type_keys(
            self._normalize_serials(self._reconfigure_entry.data.get(CONF_SERIALS, [])),
            bool(self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True)),
            site_only=bool(self._reconfigure_entry.data.get(CONF_SITE_ONLY, False)),
        )

    def _stored_configured_serials(self) -> list[str]:
        if not self._reconfigure_entry:
            return []
        return self._normalize_serials(
            self._reconfigure_entry.data.get(CONF_SERIALS, [])
        )

    def _fallback_type_keys_for_unknown_inventory(
        self, discovered_serials: list[str]
    ) -> list[str]:
        selected = self._stored_selected_type_keys()
        if selected:
            return selected
        fallback = ["envoy", "encharge"]
        if discovered_serials:
            fallback.append("iqevse")
        if self._default_include_inverters():
            fallback.append("microinverter")
        return fallback

    def _merged_selected_type_keys_for_unknown_inventory(
        self, selected_type_keys: list[str], *, visible_type_keys: list[str]
    ) -> list[str]:
        if not self._inventory_unknown:
            return selected_type_keys
        stored_selected = set(self._stored_selected_type_keys())
        visible = set(visible_type_keys)
        merged = list(selected_type_keys)
        for key in stored_selected:
            if key not in visible and key not in merged and key in _TYPE_FIELD_BY_KEY:
                merged.append(key)
        return merged

    def _get_reconfigure_entry(self) -> ConfigEntry | None:
        if hasattr(super(), "_get_reconfigure_entry"):
            try:
                return super()._get_reconfigure_entry()  # type: ignore[misc]
            except Exception:
                pass
        entry_id = self.context.get("entry_id") if hasattr(self, "context") else None
        if entry_id and self.hass:
            return self.hass.config_entries.async_get_entry(entry_id)
        current = self._async_current_entries()
        return current[0] if current else None

    def _abort_if_unique_id_mismatch(self, *, reason: str) -> None:
        from homeassistant.data_entry_flow import AbortFlow

        try:
            super()._abort_if_unique_id_mismatch(reason=reason)  # type: ignore[misc]
        except AbortFlow:
            raise
        except AttributeError:
            pass
        except Exception:
            # Parent helpers may rely on HA internals unavailable in our tests; fall back below.
            pass
        entry = self._get_reconfigure_entry()
        if not entry:
            return
        current_uid = entry.unique_id or entry.data.get(CONF_SITE_ID)
        desired_uid = getattr(self, "unique_id", None)
        if current_uid and desired_uid and current_uid != desired_uid:
            from homeassistant.data_entry_flow import AbortFlow

            raise AbortFlow(reason)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._reconfigure_entry = self._get_reconfigure_entry()
        if not self._reconfigure_entry:
            return self.async_abort(reason="unknown")
        has_email = bool(self._reconfigure_entry.data.get(CONF_EMAIL))
        if not has_email:
            return self.async_abort(reason="manual_mode_removed")
        self._email = self._reconfigure_entry.data.get(CONF_EMAIL)
        self._remember_password = bool(
            self._reconfigure_entry.data.get(CONF_REMEMBER_PASSWORD)
        )
        self._site_only = bool(self._reconfigure_entry.data.get(CONF_SITE_ONLY, False))
        self._include_inverters = bool(
            self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True)
        )
        if self._remember_password:
            self._password = self._reconfigure_entry.data.get(CONF_PASSWORD)
        return await self.async_step_user()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id")
        )
        self._reconfigure_entry = self._reauth_entry
        if not self._reauth_entry:
            return self.async_abort(reason="unknown")
        has_email = bool(self._reauth_entry.data.get(CONF_EMAIL))
        if not has_email:
            return self.async_abort(reason="manual_mode_removed")
        self._email = self._reauth_entry.data.get(CONF_EMAIL)
        self._remember_password = bool(
            self._reauth_entry.data.get(CONF_REMEMBER_PASSWORD)
        )
        self._site_only = bool(self._reauth_entry.data.get(CONF_SITE_ONLY, False))
        self._include_inverters = bool(
            self._reauth_entry.data.get(CONF_INCLUDE_INVERTERS, True)
        )
        if self._remember_password:
            self._password = self._reauth_entry.data.get(CONF_PASSWORD)
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        try:
            super().__init__(config_entry)
        except TypeError:
            # Older cores lacked the config_entry parameter; fall back to parameterless init.
            super().__init__()
        self._entry = config_entry

    @staticmethod
    def _normalize_serials(value: Any) -> list[str]:
        if isinstance(value, list):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        serials: list[str] = []
        for item in iterable:
            serial = str(item).strip()
            if serial and serial not in serials:
                serials.append(serial)
        return serials

    @staticmethod
    def _normalize_type_keys(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        selected: list[str] = []
        for item in iterable:
            key = normalize_type_key(item)
            if key and key in _TYPE_FIELD_BY_KEY and key not in selected:
                selected.append(key)
        return selected

    @staticmethod
    def _normalize_any_type_keys(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        selected: list[str] = []
        for item in iterable:
            key = normalize_type_key(item)
            if key and key not in selected:
                selected.append(key)
        return selected

    def _legacy_selected_type_keys(
        self,
        serials: list[str],
        include_inverters: bool,
        *,
        site_only: bool = False,
    ) -> list[str]:
        selected = ["envoy", "encharge"]
        if serials and not site_only:
            selected.append("iqevse")
        if include_inverters:
            selected.append("microinverter")
        return selected

    def _stored_selected_type_keys(self) -> list[str]:
        if CONF_SELECTED_TYPE_KEYS in self._entry.data:
            return self._normalize_any_type_keys(
                self._entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
            )
        return self._legacy_selected_type_keys(
            self._normalize_serials(self._entry.data.get(CONF_SERIALS, [])),
            bool(self._entry.data.get(CONF_INCLUDE_INVERTERS, True)),
            site_only=bool(self._entry.data.get(CONF_SITE_ONLY, False)),
        )

    def _default_selected_type_keys(self) -> list[str]:
        selected = set(self._stored_selected_type_keys())
        return [key for key in ONBOARDING_SUPPORTED_TYPE_KEYS if key in selected]

    def _build_schema(self) -> vol.Schema:
        default_selected_type_keys = self._default_selected_type_keys()
        schema_fields: dict[vol.Marker, object] = {}
        for type_key in ONBOARDING_SUPPORTED_TYPE_KEYS:
            field_key = _TYPE_FIELD_BY_KEY.get(type_key)
            if field_key is None:
                continue
            schema_fields[
                vol.Optional(field_key, default=type_key in default_selected_type_keys)
            ] = bool
        schema_fields.update(
            {
                vol.Optional(
                    OPT_FAST_POLL_INTERVAL,
                    default=self._entry.options.get(
                        OPT_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_INTERVAL
                    ),
                ): int,
                vol.Optional(
                    OPT_SLOW_POLL_INTERVAL,
                    default=self._entry.options.get(
                        OPT_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL
                    ),
                ): int,
                vol.Optional(
                    OPT_FAST_WHILE_STREAMING,
                    default=self._entry.options.get(OPT_FAST_WHILE_STREAMING, True),
                ): bool,
                vol.Optional(
                    OPT_API_TIMEOUT,
                    default=self._entry.options.get(OPT_API_TIMEOUT, 15),
                ): int,
                vol.Optional(
                    OPT_NOMINAL_VOLTAGE,
                    default=self._entry.options.get(OPT_NOMINAL_VOLTAGE, 240),
                ): int,
                vol.Optional(
                    OPT_SESSION_HISTORY_INTERVAL,
                    default=self._entry.options.get(
                        OPT_SESSION_HISTORY_INTERVAL,
                        DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
                    ),
                ): int,
                vol.Optional(
                    OPT_SCHEDULE_SYNC_ENABLED,
                    default=self._entry.options.get(OPT_SCHEDULE_SYNC_ENABLED, False),
                ): bool,
                vol.Optional("reauth", default=False): bool,
                vol.Optional("forget_password", default=False): bool,
            }
        )
        base_schema = vol.Schema(schema_fields)
        return self.add_suggested_values_to_schema(base_schema, self._entry.options)

    async def _discover_iqevse_serials(self) -> list[str]:
        site_id = str(self._entry.data.get(CONF_SITE_ID, "")).strip()
        if not site_id:
            return []

        tokens = AuthTokens(
            cookie=str(self._entry.data.get(CONF_COOKIE, "") or ""),
            session_id=self._entry.data.get(CONF_SESSION_ID),
            access_token=self._entry.data.get(CONF_EAUTH)
            or self._entry.data.get(CONF_ACCESS_TOKEN),
            token_expires_at=self._entry.data.get(CONF_TOKEN_EXPIRES_AT),
        )
        session = async_get_clientsession(self.hass)

        chargers = await async_fetch_chargers(session, site_id, tokens)
        discovered: list[str] = []
        for charger in chargers:
            if charger.serial:
                serial = str(charger.serial).strip()
                if serial and serial not in discovered:
                    discovered.append(serial)
        if discovered:
            return discovered

        payload = await async_fetch_devices_inventory(session, site_id, tokens)
        if payload is None:
            return []
        return self._normalize_serials(
            active_type_serials_from_inventory(payload, type_key="iqevse")
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        schema = self._build_schema()
        if user_input is not None:
            option_data = dict(user_input)
            forget_password = bool(option_data.pop("forget_password", False))
            reauth = bool(option_data.pop("reauth", False))
            selected_type_keys: list[str] = []
            default_selected_type_keys = self._default_selected_type_keys()
            for type_key in ONBOARDING_SUPPORTED_TYPE_KEYS:
                field_key = _TYPE_FIELD_BY_KEY.get(type_key)
                if field_key is None:
                    continue
                if bool(
                    option_data.pop(field_key, type_key in default_selected_type_keys)
                ):
                    selected_type_keys.append(type_key)

            stored_selected_type_keys = self._stored_selected_type_keys()
            for type_key in stored_selected_type_keys:
                if (
                    type_key not in ONBOARDING_SUPPORTED_TYPE_KEYS
                    and type_key not in selected_type_keys
                ):
                    selected_type_keys.append(type_key)

            serials = self._normalize_serials(self._entry.data.get(CONF_SERIALS, []))
            site_only = "iqevse" not in selected_type_keys
            include_inverters = "microinverter" in selected_type_keys
            if site_only:
                serials = []
            elif not serials and not (forget_password or reauth):
                serials = await self._discover_iqevse_serials()
                if not serials:
                    error_schema = self.add_suggested_values_to_schema(
                        self._build_schema(), user_input
                    )
                    return self.async_show_form(
                        step_id="init",
                        data_schema=error_schema,
                        errors={"base": "serials_required"},
                    )

            new_data = dict(self._entry.data)
            new_data[CONF_SELECTED_TYPE_KEYS] = selected_type_keys
            new_data[CONF_SITE_ONLY] = site_only
            new_data[CONF_INCLUDE_INVERTERS] = include_inverters
            new_data[CONF_SERIALS] = serials

            if forget_password:
                new_data.pop(CONF_PASSWORD, None)
                new_data[CONF_REMEMBER_PASSWORD] = False

            if reauth:
                start_reauth = getattr(self._entry, "async_start_reauth", None)
                if start_reauth is not None:
                    result = start_reauth(self.hass)
                    if inspect.isawaitable(result):
                        await result

            option_data.pop(CONF_SCAN_INTERVAL, None)
            option_data.pop(CONF_SITE_ONLY, None)

            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            return self.async_create_entry(data=option_data)

        return self.async_show_form(step_id="init", data_schema=schema)
