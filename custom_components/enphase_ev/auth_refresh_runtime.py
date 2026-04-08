"""Stored-credential Enlighten token refresh.

``AuthRefreshRuntime.attempt_auto_refresh`` uses this class's own methods
(``auth_refresh_recent_success_active``, ``auth_refresh_rejected_active``,
``async_run_auto_refresh``, ``clear_auth_refresh_task``) so behavior stays in one
place; tests typically patch ``custom_components.enphase_ev.auth_refresh_runtime``
(``async_authenticate``, ``async_get_clientsession``) or the runtime instance on
the coordinator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import (
    EnlightenAuthInvalidCredentials,
    EnlightenAuthMFARequired,
    EnlightenAuthUnavailable,
    async_authenticate,
)
from .const import (
    AUTH_REFRESH_REJECTED_COOLDOWN_S,
    AUTH_REFRESH_SUCCESS_REUSE_WINDOW_S,
)
from .log_redaction import redact_text

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)


class AuthRefreshRuntime:
    """Stored-credential Enlighten token refresh with coalescing and cooldown."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    async def attempt_auto_refresh(self) -> bool:
        """Attempt to refresh authentication using stored credentials."""

        coord = self.coordinator
        if (
            not coord._email
            or not coord._remember_password
            or not coord._stored_password
        ):
            return False

        if self.auth_refresh_recent_success_active():
            return True

        if self.auth_refresh_rejected_active():
            return False

        task = getattr(coord, "_auth_refresh_task", None)
        if task is not None and not task.done():
            return await asyncio.shield(task)

        async with coord._refresh_lock:
            if self.auth_refresh_rejected_active():
                return False

            if self.auth_refresh_recent_success_active():
                return True

            task = getattr(coord, "_auth_refresh_task", None)
            if task is None or task.done():
                task = asyncio.create_task(self.async_run_auto_refresh())
                coord._auth_refresh_task = task
                task.add_done_callback(self.clear_auth_refresh_task)

        return await asyncio.shield(task)

    def clear_auth_refresh_task(self, task: asyncio.Task[bool]) -> None:
        """Clear the shared auth-refresh task once it completes."""

        coord = self.coordinator
        if getattr(coord, "_auth_refresh_task", None) is task:
            coord._auth_refresh_task = None

    def auth_refresh_rejected_active(self) -> bool:
        """Return True while stored-credential refresh is in cooldown."""

        coord = self.coordinator
        cooldown_until = getattr(coord, "_auth_refresh_rejected_until", None)
        if not isinstance(cooldown_until, (int, float)):
            return False
        if time.monotonic() < float(cooldown_until):
            return True
        coord._auth_refresh_rejected_until = None
        coord._auth_refresh_rejected_ends_utc = None
        return False

    def note_auth_refresh_rejected(self, message: str) -> None:
        """Start a cooldown after stored credentials are rejected."""

        coord = self.coordinator
        delay = float(AUTH_REFRESH_REJECTED_COOLDOWN_S)
        coord._auth_refresh_last_success_mono = None
        coord._auth_refresh_rejected_until = time.monotonic() + delay
        try:
            coord._auth_refresh_rejected_ends_utc = dt_util.utcnow() + timedelta(
                seconds=delay
            )
        except Exception:
            coord._auth_refresh_rejected_ends_utc = None
        _LOGGER.warning(message)

    def auth_refresh_recent_success_active(self) -> bool:
        """Return True when a recent successful refresh can satisfy stale 401s."""

        coord = self.coordinator
        last_success = getattr(coord, "_auth_refresh_last_success_mono", None)
        if not isinstance(last_success, (int, float)):
            return False
        return (time.monotonic() - float(last_success)) <= float(
            AUTH_REFRESH_SUCCESS_REUSE_WINDOW_S
        )

    async def async_run_auto_refresh(self) -> bool:
        """Run one stored-credential refresh attempt for all concurrent waiters."""

        coord = self.coordinator
        session = async_get_clientsession(coord.hass)
        try:
            tokens, _ = await async_authenticate(
                session, coord._email, coord._stored_password
            )
        except EnlightenAuthInvalidCredentials:
            self.note_auth_refresh_rejected(
                "Stored Enlighten credentials were rejected; reauthenticate via the integration options"
            )
            return False
        except EnlightenAuthMFARequired:
            self.note_auth_refresh_rejected(
                "Enphase account requires multi-factor authentication; complete MFA in the browser and reauthenticate"
            )
            return False
        except EnlightenAuthUnavailable:
            _LOGGER.debug(
                "Auth service unavailable while refreshing tokens; will retry later"
            )
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Unexpected error refreshing Enlighten auth: %s",
                redact_text(err),
            )
            return False

        coord._auth_refresh_rejected_until = None
        coord._auth_refresh_rejected_ends_utc = None
        coord._auth_refresh_last_success_mono = time.monotonic()
        coord._tokens = tokens
        coord.client.update_credentials(
            eauth=tokens.access_token,
            cookie=tokens.cookie,
        )
        coord._persist_tokens(tokens)
        return True
