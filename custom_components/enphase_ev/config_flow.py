from __future__ import annotations

import inspect
import re
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
    EnlightenAuthMFARequired,
    EnlightenAuthUnavailable,
    EnphaseEVClient,
    Unauthorized,
    async_authenticate,
    async_fetch_chargers,
)
from .const import (
    AUTH_MODE_LOGIN,
    AUTH_MODE_MANUAL,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_MODE,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
)


class EnphaseEVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._auth_tokens: AuthTokens | None = None
        self._sites: dict[str, str | None] = {}
        self._selected_site_id: str | None = None
        self._chargers: list[tuple[str, str | None]] = []
        self._chargers_loaded = False
        self._email: str | None = None
        self._remember_password = False
        self._password: str | None = None
        self._manual_mode = False
        self._reconfigure_entry: ConfigEntry | None = None
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("manual_auth"):
                self._manual_mode = True
                return await self.async_step_manual()

            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            remember = bool(user_input.get(CONF_REMEMBER_PASSWORD, False))

            session = async_get_clientsession(self.hass)
            try:
                tokens, sites = await async_authenticate(session, email, password)
            except EnlightenAuthInvalidCredentials:
                errors["base"] = "invalid_auth"
            except EnlightenAuthMFARequired:
                errors["base"] = "mfa_required"
            except EnlightenAuthUnavailable:
                errors["base"] = "service_unavailable"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                self._auth_tokens = tokens
                self._sites = {site.site_id: site.name for site in sites}
                self._email = email
                self._remember_password = remember
                self._password = password if remember else None

                if self._reconfigure_entry:
                    current_site = self._reconfigure_entry.data.get(CONF_SITE_ID)
                    if current_site:
                        self._selected_site_id = str(current_site)

                if len(self._sites) == 1 and not self._reconfigure_entry:
                    self._selected_site_id = next(iter(self._sites))
                    return await self.async_step_devices()
                return await self.async_step_site()

        defaults = {
            CONF_EMAIL: self._email or "",
            CONF_REMEMBER_PASSWORD: self._remember_password,
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=defaults[CONF_EMAIL]): selector({"text": {"type": "email"}}),
                vol.Required(CONF_PASSWORD): selector({"text": {"type": "password"}}),
                vol.Optional(CONF_REMEMBER_PASSWORD, default=defaults[CONF_REMEMBER_PASSWORD]): bool,
                vol.Optional("manual_auth", default=False): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            site_id = user_input.get(CONF_SITE_ID)
            if site_id:
                self._selected_site_id = str(site_id)
                if self._selected_site_id not in self._sites:
                    self._sites[self._selected_site_id] = None
                return await self.async_step_devices()
            errors["base"] = "site_required"

        options = [
            {
                "value": site_id,
                "label": f"{name} ({site_id})" if name else site_id,
            }
            for site_id, name in self._sites.items()
        ]

        if options:
            schema = vol.Schema(
                {
                    vol.Required(CONF_SITE_ID): selector(
                        {"select": {"options": options, "multiple": False}}
                    )
                }
            )
        else:
            schema = vol.Schema({vol.Required(CONF_SITE_ID): str})

        return self.async_show_form(step_id="site", data_schema=schema, errors=errors)

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if not self._chargers_loaded:
            await self._ensure_chargers()

        if user_input is not None:
            serials = user_input.get(CONF_SERIALS)
            scan_interval = int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
            selected = self._normalize_serials(serials)
            if selected:
                return await self._finalize_login_entry(selected, scan_interval)
            errors["base"] = "serials_required"

        default_scan = self._default_scan_interval()

        if self._chargers:
            options = [
                {"value": serial, "label": name or serial}
                for serial, name in self._chargers
            ]
            schema = vol.Schema(
                {
                    vol.Required(CONF_SERIALS): selector(
                        {"select": {"options": options, "multiple": True}}
                    ),
                    vol.Optional(CONF_SCAN_INTERVAL, default=default_scan): int,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_SERIALS): selector({"text": {"multiline": True}}),
                    vol.Optional(CONF_SCAN_INTERVAL, default=default_scan): int,
                }
            )

        return self.async_show_form(step_id="devices", data_schema=schema, errors=errors)

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            curl = user_input.get("curl")
            if curl:
                parsed = self._parse_curl(curl)
                if not parsed:
                    errors["base"] = "invalid_auth"
                else:
                    user_input = {
                        CONF_SITE_ID: parsed[CONF_SITE_ID],
                        CONF_SERIALS: user_input.get(CONF_SERIALS) or [],
                        CONF_EAUTH: parsed[CONF_EAUTH],
                        CONF_COOKIE: parsed[CONF_COOKIE],
                        CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    }
            if errors:
                pass
            else:
                result = await self._validate_manual_input(user_input, errors)
                if result:
                    return result

        defaults = {
            CONF_SITE_ID: "",
            CONF_SERIALS: "",
            CONF_EAUTH: "",
            CONF_COOKIE: "",
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        }
        entry = self._reconfigure_entry or self._reauth_entry
        if entry:
            defaults[CONF_SITE_ID] = entry.data.get(CONF_SITE_ID, "")
            serials_val = entry.data.get(CONF_SERIALS) or []
            if isinstance(serials_val, (list, tuple)):
                defaults[CONF_SERIALS] = ", ".join(map(str, serials_val))
            else:
                defaults[CONF_SERIALS] = str(serials_val or "")
            defaults[CONF_SCAN_INTERVAL] = int(entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

        schema = vol.Schema(
            {
                vol.Required(CONF_SITE_ID, default=defaults[CONF_SITE_ID]): str,
                vol.Required(CONF_SERIALS, default=defaults[CONF_SERIALS]): selector({"text": {"multiline": False}}),
                vol.Required(CONF_EAUTH, default=defaults[CONF_EAUTH]): selector({"text": {"multiline": False}}),
                vol.Required(CONF_COOKIE, default=defaults[CONF_COOKIE]): selector({"text": {"multiline": True}}),
                vol.Optional(CONF_SCAN_INTERVAL, default=defaults[CONF_SCAN_INTERVAL]): int,
                vol.Optional("curl"): selector({"text": {"multiline": True}}),
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    async def _finalize_login_entry(self, serials: list[str], scan_interval: int) -> FlowResult:
        if not self._auth_tokens or not self._selected_site_id:
            return self.async_abort(reason="unknown")

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
            CONF_AUTH_MODE: AUTH_MODE_LOGIN,
            CONF_REMEMBER_PASSWORD: self._remember_password,
            CONF_EMAIL: self._email,
        }
        if self._remember_password and self._password:
            data[CONF_PASSWORD] = self._password
        else:
            data.pop(CONF_PASSWORD, None)

        await self.async_set_unique_id(self._selected_site_id)

        if self._reconfigure_entry:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            merged = dict(self._reconfigure_entry.data)
            for key, value in data.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            if not self._remember_password:
                merged.pop(CONF_PASSWORD, None)
                if hasattr(self, "async_update_reload_and_abort"):
                    result = self.async_update_reload_and_abort(
                        self._reconfigure_entry,
                        data_updates=merged,
                    )
                    if inspect.isawaitable(result):
                        return await result
                    return result
            self.hass.config_entries.async_update_entry(self._reconfigure_entry, data=merged)
            await self.hass.config_entries.async_reload(self._reconfigure_entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        self._abort_if_unique_id_configured()
        title = site_name or f"Enphase EV {self._selected_site_id}"
        return self.async_create_entry(title=title, data=data)

    async def _ensure_chargers(self) -> None:
        if self._chargers_loaded:
            return
        if not self._auth_tokens or not self._selected_site_id:
            self._chargers_loaded = True
            return
        session = async_get_clientsession(self.hass)
        chargers = await async_fetch_chargers(session, self._selected_site_id, self._auth_tokens)
        self._chargers = [(c.serial, c.name) for c in chargers]
        self._chargers_loaded = True

    async def _validate_manual_input(self, user_input: dict[str, Any], errors: dict[str, str]) -> FlowResult | None:
        try:
            import aiohttp  # pragma: no cover - imported lazily for tests

            session = async_get_clientsession(self.hass)
            client = EnphaseEVClient(
                session,
                user_input[CONF_SITE_ID],
                user_input[CONF_EAUTH],
                user_input[CONF_COOKIE],
            )
            await client.status()
        except Exception as ex:  # noqa: BLE001
            try:
                import aiohttp

                aio_err = isinstance(ex, aiohttp.ClientError)
            except Exception:  # noqa: BLE001
                aio_err = False
            if isinstance(ex, Unauthorized):
                errors["base"] = "invalid_auth"
            elif aio_err:
                errors["base"] = "cannot_connect"
            else:
                errors["base"] = "unknown"
            return None

        serials = self._normalize_serials(user_input[CONF_SERIALS])
        scan_interval = int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        await self.async_set_unique_id(user_input[CONF_SITE_ID])
        if self._reconfigure_entry:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            merged = {**self._reconfigure_entry.data}
            merged.update(
                {
                    CONF_SITE_ID: user_input[CONF_SITE_ID],
                    CONF_SERIALS: serials,
                    CONF_EAUTH: user_input[CONF_EAUTH],
                    CONF_COOKIE: user_input[CONF_COOKIE],
                    CONF_SCAN_INTERVAL: scan_interval,
                    CONF_AUTH_MODE: AUTH_MODE_MANUAL,
                }
            )
            if hasattr(self, "async_update_reload_and_abort"):
                result = self.async_update_reload_and_abort(
                    self._reconfigure_entry,
                    data_updates=merged,
                )
                if inspect.isawaitable(result):
                    return await result
                return result
            self.hass.config_entries.async_update_entry(self._reconfigure_entry, data=merged)
            await self.hass.config_entries.async_reload(self._reconfigure_entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        self._abort_if_unique_id_configured()
        data = {
            CONF_SITE_ID: user_input[CONF_SITE_ID],
            CONF_SERIALS: serials,
            CONF_EAUTH: user_input[CONF_EAUTH],
            CONF_COOKIE: user_input[CONF_COOKIE],
            CONF_SCAN_INTERVAL: scan_interval,
            CONF_AUTH_MODE: AUTH_MODE_MANUAL,
        }
        return self.async_create_entry(title=f"Enphase EV {user_input[CONF_SITE_ID]}", data=data)

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

    def _default_scan_interval(self) -> int:
        if self._reconfigure_entry:
            return int(self._reconfigure_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        return DEFAULT_SCAN_INTERVAL

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

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._reconfigure_entry = self._get_reconfigure_entry()
        if not self._reconfigure_entry:
            return self.async_abort(reason="unknown")
        is_manual = self._reconfigure_entry.data.get(CONF_AUTH_MODE) == AUTH_MODE_MANUAL
        has_email = bool(self._reconfigure_entry.data.get(CONF_EMAIL))
        if is_manual or not has_email:
            return await self.async_step_manual()
        self._email = self._reconfigure_entry.data.get(CONF_EMAIL)
        self._remember_password = bool(self._reconfigure_entry.data.get(CONF_REMEMBER_PASSWORD))
        if self._remember_password:
            self._password = self._reconfigure_entry.data.get(CONF_PASSWORD)
        return await self.async_step_user()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        self._reconfigure_entry = self._reauth_entry
        if not self._reauth_entry:
            return self.async_abort(reason="unknown")
        is_manual = self._reauth_entry.data.get(CONF_AUTH_MODE) == AUTH_MODE_MANUAL
        has_email = bool(self._reauth_entry.data.get(CONF_EMAIL))
        if is_manual or not has_email:
            return await self.async_step_manual()
        self._email = self._reauth_entry.data.get(CONF_EMAIL)
        self._remember_password = bool(self._reauth_entry.data.get(CONF_REMEMBER_PASSWORD))
        if self._remember_password:
            self._password = self._reauth_entry.data.get(CONF_PASSWORD)
        return await self.async_step_user()

    def _parse_curl(self, curl: str) -> dict[str, str] | None:
        try:
            m_url = re.search(r"curl\s+'([^']+)'|curl\s+\"([^\"]+)\"|curl\s+(https?://\S+)", curl)
            url = next(
                g
                for g in (
                    m_url.group(1) if m_url else None,
                    m_url.group(2) if m_url else None,
                    m_url.group(3) if m_url else None,
                )
                if g
            )
            headers = {}
            for m in re.finditer(r"-H\s+'([^:]+):\s*([^']*)'|-H\s+\"([^:]+):\s*([^\"]*)\"", curl):
                key = m.group(1) or m.group(3)
                val = m.group(2) or m.group(4)
                if key and val:
                    headers[key.strip()] = val.strip()
            eauth = headers.get("e-auth-token")
            cookie = headers.get("Cookie")
            from urllib.parse import urlparse

            path = urlparse(url).path
            m_site = re.search(r"/evse_controller/(\d+)/", path) or re.search(r"/pv/systems/(\d+)/", path)
            site_id = m_site.group(1) if m_site else None
            if site_id and eauth and cookie:
                return {CONF_SITE_ID: site_id, CONF_EAUTH: eauth, CONF_COOKIE: cookie}
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        try:
            super().__init__(config_entry)
        except TypeError:
            # Older cores lacked the config_entry parameter; fall back to manual assignment.
            super().__init__()
            self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            if user_input.pop("forget_password", False):
                data = dict(self.config_entry.data)
                data.pop(CONF_PASSWORD, None)
                data[CONF_REMEMBER_PASSWORD] = False
                self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            if user_input.pop("reauth", False):
                start_reauth = getattr(self.config_entry, "async_start_reauth", None)
                if start_reauth is not None:
                    result = start_reauth(self.hass)
                    if inspect.isawaitable(result):
                        await result
            return self.async_create_entry(data=user_input)

        base_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): int,
                vol.Optional(
                    OPT_FAST_POLL_INTERVAL,
                    default=self.config_entry.options.get(OPT_FAST_POLL_INTERVAL, 10),
                ): int,
                vol.Optional(
                    OPT_SLOW_POLL_INTERVAL,
                    default=self.config_entry.options.get(OPT_SLOW_POLL_INTERVAL, 30),
                ): int,
                vol.Optional(
                    OPT_FAST_WHILE_STREAMING,
                    default=self.config_entry.options.get(OPT_FAST_WHILE_STREAMING, True),
                ): bool,
                vol.Optional(
                    OPT_API_TIMEOUT,
                    default=self.config_entry.options.get(OPT_API_TIMEOUT, 15),
                ): int,
                vol.Optional(
                    OPT_NOMINAL_VOLTAGE,
                    default=self.config_entry.options.get(OPT_NOMINAL_VOLTAGE, 240),
                ): int,
                vol.Optional("reauth", default=False): bool,
                vol.Optional("forget_password", default=False): bool,
            }
        )
        schema = self.add_suggested_values_to_schema(base_schema, self.config_entry.options)
        return self.async_show_form(step_id="init", data_schema=schema)
