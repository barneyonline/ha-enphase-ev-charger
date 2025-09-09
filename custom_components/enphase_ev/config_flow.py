
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import aiohttp
from homeassistant.config_entries import OptionsFlowWithReload
import re
from urllib.parse import urlparse

from .const import (
    DOMAIN,
    CONF_SITE_ID,
    CONF_SERIALS,
    CONF_EAUTH,
    CONF_COOKIE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    OPT_FAST_POLL_INTERVAL,
    OPT_SLOW_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
)
from .api import EnphaseEVClient, Unauthorized

class EnphaseEVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        # Only support browser sign-in. Open Enlighten in a new tab; user returns and pastes headers.
        return self.async_external_step(step_id="browser", url="https://enlighten.enphaseenergy.com/")

    async def async_step_browser(self, user_input=None):
        # After user clicked and viewed the login, return to a final step to paste values
        return self.async_external_step_done(next_step_id="finish")

    async def async_step_finish(self, user_input=None):
        # Final step after browser external step; show the headers form and validate
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
            validated = await self._validate_and_create(user_input, errors)
            if validated:
                return validated
        schema = vol.Schema({
            vol.Required(CONF_SITE_ID): str,
            vol.Required(CONF_SERIALS): [str],
            vol.Required(CONF_EAUTH): str,
            vol.Required(CONF_COOKIE): str,
            vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
            vol.Optional("curl"): str,
        })
        return self.async_show_form(step_id="finish", data_schema=schema, errors=errors)

    async def _validate_and_create(self, user_input, errors):
        try:
            session = async_get_clientsession(self.hass)
            client = EnphaseEVClient(
                session,
                user_input[CONF_SITE_ID],
                user_input[CONF_EAUTH],
                user_input[CONF_COOKIE],
            )
            await client.status()
        except Unauthorized:
            errors["base"] = "invalid_auth"
        except aiohttp.ClientError:
            errors["base"] = "cannot_connect"
        except Exception:
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
            url = next(g for g in (m_url.group(1) if m_url else None, m_url.group(2) if m_url else None, m_url.group(3) if m_url else None) if g)  # type: ignore
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
        return OptionsFlowHandler(config_entry)

    async def async_step_reauth(self, entry_data):
        """Start reauth flow when credentials are invalid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            # Validate new headers
            try:
                session = async_get_clientsession(self.hass)
                client = EnphaseEVClient(
                    session,
                    self._reauth_entry.data[CONF_SITE_ID],
                    user_input[CONF_EAUTH],
                    user_input[CONF_COOKIE],
                )
                await client.status()
            except Unauthorized:
                errors["base"] = "invalid_auth"
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
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
            vol.Required(CONF_EAUTH): str,
            vol.Required(CONF_COOKIE): str,
        })
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

class OptionsFlowHandler(OptionsFlowWithReload):
    def __init__(self, entry):
        self.entry = entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=self.entry.data.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                ): int,
                vol.Optional(
                    OPT_FAST_POLL_INTERVAL,
                    default=self.entry.options.get(OPT_FAST_POLL_INTERVAL, 10),
                ): int,
                vol.Optional(
                    OPT_SLOW_POLL_INTERVAL,
                    default=self.entry.options.get(OPT_SLOW_POLL_INTERVAL, 30),
                ): int,
                vol.Optional(
                    OPT_FAST_WHILE_STREAMING,
                    default=self.entry.options.get(OPT_FAST_WHILE_STREAMING, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
