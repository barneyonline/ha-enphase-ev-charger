from __future__ import annotations

import re
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector

from .const import (
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SITE_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
)

# Do not import the API client at module import time to avoid unexpected errors during flow load

class EnphaseEVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        # Show form directly to collect required inputs instead of external step.
        errors = {}
        if user_input is not None:
            # Optional: allow pasting 'Copy as cURL' from DevTools and parse automatically
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
            # Normalize serials (accept CSV or newline-separated string) to list[str]
            if isinstance(user_input.get(CONF_SERIALS), str):
                serials_text = user_input[CONF_SERIALS]
                user_input[CONF_SERIALS] = [p.strip() for p in re.split(r"[,\n]+", serials_text) if p.strip()]

            validated = await self._validate_and_create(user_input, errors)
            if validated:
                return validated
        schema = vol.Schema({
            vol.Required(CONF_SITE_ID): str,
            vol.Required(CONF_SERIALS): selector({"text": {"multiline": False}}),
            vol.Required(CONF_EAUTH): selector({"text": {"multiline": False}}),
            # Cookie as multiline text for easier paste
            vol.Required(CONF_COOKIE): selector({"text": {"multiline": True}}),
            vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
            vol.Optional("curl"): selector({"text": {"multiline": True}}),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # Legacy external-step handlers retained for compatibility; unused now.
    async def async_step_browser(self, user_input=None):
        return self.async_external_step_done(next_step_id="user")

    async def _validate_and_create(self, user_input, errors):
        try:
            # Local imports to reduce risk of import-time errors
            import aiohttp

            from .api import EnphaseEVClient

            session = async_get_clientsession(self.hass)
            client = EnphaseEVClient(
                session,
                user_input[CONF_SITE_ID],
                user_input[CONF_EAUTH],
                user_input[CONF_COOKIE],
            )
            await client.status()
        except Exception as ex:
            try:
                from .api import Unauthorized as _Unauthorized
            except Exception:  # noqa: BLE001
                _Unauthorized = None  # type: ignore[assignment]
            try:
                import aiohttp
                aio_err = isinstance(ex, aiohttp.ClientError)
            except Exception:  # noqa: BLE001
                aio_err = False

            if _Unauthorized and isinstance(ex, _Unauthorized):
                errors["base"] = "invalid_auth"
            elif aio_err:
                errors["base"] = "cannot_connect"
            else:
                errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(user_input[CONF_SITE_ID])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Enphase EV {user_input[CONF_SITE_ID]}", data=user_input)
        # Indicate to caller to re-render the form with errors
        return None

    def _parse_curl(self, curl: str):
        try:
            # Extract URL
            m_url = re.search(r"curl\s+'([^']+)'|curl\s+\"([^\"]+)\"|curl\s+(https?://\S+)", curl)
            url = next(
                g
                for g in (
                    m_url.group(1) if m_url else None,
                    m_url.group(2) if m_url else None,
                    m_url.group(3) if m_url else None,
                )
                if g
            )  # type: ignore
            # Extract headers
            headers = {}
            for m in re.finditer(r"-H\s+'([^:]+):\s*([^']*)'|-H\s+\"([^:]+):\s*([^\"]*)\"", curl):
                key = m.group(1) or m.group(3)
                val = m.group(2) or m.group(4)
                if key and val:
                    headers[key.strip()] = val.strip()
            eauth = headers.get("e-auth-token")
            cookie = headers.get("Cookie")
            # Site ID from URL path
            path = urlparse(url).path
            m_site = re.search(r"/evse_controller/(\d+)/", path) or re.search(r"/pv/systems/(\d+)/", path)
            site_id = m_site.group(1) if m_site else None
            if site_id and eauth and cookie:
                return {CONF_SITE_ID: site_id, CONF_EAUTH: eauth, CONF_COOKIE: cookie}
        except Exception:
            return None
        return None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()

    async def async_step_reconfigure(self, user_input=None):
        """Handle reconfiguration of the integration in-place.

        Allows updating site settings and credentials without removing the entry.
        """
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            # Optional: parse cURL to fill headers/site automatically
            curl = user_input.get("curl")
            if curl:
                parsed = self._parse_curl(curl)
                if parsed:
                    user_input[CONF_SITE_ID] = parsed[CONF_SITE_ID]
                    user_input[CONF_EAUTH] = parsed[CONF_EAUTH]
                    user_input[CONF_COOKIE] = parsed[CONF_COOKIE]
                else:
                    errors["base"] = "invalid_auth"

            # Normalize serials to list[str]
            if isinstance(user_input.get(CONF_SERIALS), str):
                serials_text = user_input[CONF_SERIALS]
                user_input[CONF_SERIALS] = [p.strip() for p in re.split(r"[,\n]+", serials_text) if p.strip()]

            # Validate provided credentials by probing /status
            try:
                import aiohttp

                from .api import EnphaseEVClient
                from .api import Unauthorized as _Unauthorized  # type: ignore[attr-defined]
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
                if "_Unauthorized" in locals() and isinstance(ex, _Unauthorized):
                    errors["base"] = "invalid_auth"
                elif aio_err:
                    errors["base"] = "cannot_connect"
                else:
                    errors["base"] = "unknown"
            else:
                # Ensure the unique id (site id) matches this entry; abort if wrong account
                await self.async_set_unique_id(user_input[CONF_SITE_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                data_updates = {
                    CONF_SITE_ID: user_input[CONF_SITE_ID],
                    CONF_SERIALS: user_input.get(CONF_SERIALS, entry.data.get(CONF_SERIALS, [])),
                    CONF_EAUTH: user_input[CONF_EAUTH],
                    CONF_COOKIE: user_input[CONF_COOKIE],
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                    ),
                }
                # Use helper to update + reload + abort with success message when available
                if hasattr(self, "async_update_reload_and_abort"):
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates=data_updates,
                    )
                # Fallback for older cores
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **data_updates})
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        # Build form with current values prefilled
        serials_val = entry.data.get(CONF_SERIALS) or []
        if isinstance(serials_val, (list, tuple)):
            serials_text = ", ".join(map(str, serials_val))
        else:
            serials_text = str(serials_val or "")
        schema = vol.Schema(
            {
                vol.Required(CONF_SITE_ID, default=entry.data.get(CONF_SITE_ID, "")): str,
                vol.Required(CONF_SERIALS, default=serials_text): selector({"text": {"multiline": False}}),
                vol.Required(CONF_EAUTH, default=""): selector({"text": {"multiline": False}}),
                vol.Required(CONF_COOKIE, default=""): selector({"text": {"multiline": True}}),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): int,
                vol.Optional("curl"): selector({"text": {"multiline": True}}),
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data):
        """Start reauth flow when credentials are invalid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            # Validate new headers
            try:
                import aiohttp

                from .api import EnphaseEVClient

                session = async_get_clientsession(self.hass)
                client = EnphaseEVClient(
                    session,
                    self._reauth_entry.data[CONF_SITE_ID],
                    user_input[CONF_EAUTH],
                    user_input[CONF_COOKIE],
                )
                await client.status()
            except Exception as ex:
                try:
                    from .api import Unauthorized as _Unauthorized
                except Exception:  # noqa: BLE001
                    _Unauthorized = None  # type: ignore[assignment]
                try:
                    import aiohttp
                    aio_err = isinstance(ex, aiohttp.ClientError)
                except Exception:  # noqa: BLE001
                    aio_err = False

                if _Unauthorized and isinstance(ex, _Unauthorized):
                    errors["base"] = "invalid_auth"
                elif aio_err:
                    errors["base"] = "cannot_connect"
                else:
                    errors["base"] = "unknown"
            else:
                # Update entry with refreshed tokens
                new_data = {
                    **self._reauth_entry.data,
                    CONF_EAUTH: user_input[CONF_EAUTH],
                    CONF_COOKIE: user_input[CONF_COOKIE],
                }
                self.hass.config_entries.async_update_entry(self._reauth_entry, data=new_data)
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema({
            vol.Required(CONF_EAUTH): selector({"text": {"multiline": False}}),
            vol.Required(CONF_COOKIE): selector({"text": {"multiline": True}}),
        })
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

class OptionsFlowHandler(OptionsFlow):

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        base_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.data.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
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
                    default=self.config_entry.options.get(OPT_FAST_WHILE_STREAMING, False),
                 ): bool,
                vol.Optional(
                    OPT_API_TIMEOUT,
                    default=self.config_entry.options.get(OPT_API_TIMEOUT, 15),
                ): int,
                vol.Optional(
                    OPT_NOMINAL_VOLTAGE,
                    default=self.config_entry.options.get(OPT_NOMINAL_VOLTAGE, 240),
                ): int,
            }
        )
        schema = self.add_suggested_values_to_schema(base_schema, self.config_entry.options)
        return self.async_show_form(step_id="init", data_schema=schema)
