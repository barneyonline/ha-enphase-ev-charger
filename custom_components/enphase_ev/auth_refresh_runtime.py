"""Refresh Enlighten tokens from stored credentials with cooldown safeguards."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone as _tz
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
    AUTH_BLOCKED_COOLDOWN_S,
    AUTH_REFRESH_REJECTED_COOLDOWN_S,
    AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD,
    AUTH_REFRESH_SUSPENDED_COOLDOWN_S,
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

        if coord._auth_block_active():
            return False

        if coord._auth_refresh_suspended_active():
            return False

        if self.auth_refresh_recent_success_active():
            return True

        if self.auth_refresh_rejected_active():
            return False

        task = getattr(coord, "_auth_refresh_task", None)
        if task is not None and not task.done():
            # Concurrent 401 handlers share the same refresh attempt so Enphase
            # does not see a burst of password logins.
            return await asyncio.shield(task)

        async with coord._refresh_lock:
            if coord._auth_refresh_suspended_active():
                return False

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
        coord._auth_refresh_rejected_count = (
            int(getattr(coord, "_auth_refresh_rejected_count", 0)) + 1
        )
        delay = float(AUTH_REFRESH_REJECTED_COOLDOWN_S)
        coord._auth_refresh_last_success_mono = None
        if coord._auth_refresh_rejected_count >= int(
            AUTH_REFRESH_REJECTED_SUSPEND_THRESHOLD
        ):
            # Repeated credential rejections are treated as durable auth
            # failures and suspended longer than transient service errors.
            coord._auth_refresh_rejected_until = None
            coord._auth_refresh_rejected_ends_utc = None
            try:
                suspended_until = dt_util.utcnow() + timedelta(
                    seconds=AUTH_REFRESH_SUSPENDED_COOLDOWN_S
                )
            except Exception:
                suspended_until = datetime.now(_tz.utc) + timedelta(
                    seconds=AUTH_REFRESH_SUSPENDED_COOLDOWN_S
                )
            coord._note_auth_refresh_suspended(suspended_until=suspended_until)
            _LOGGER.warning(
                "Stored-credential automatic reauthentication has been suspended after %s consecutive rejections; reauthenticate via the integration options",
                coord._auth_refresh_rejected_count,
            )
            return
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

    def note_login_wall_block(self, *, reason: str) -> None:
        """Persist a long auth block after Enphase starts serving the login wall."""

        coord = self.coordinator
        coord._note_auth_blocked(
            blocked_until=dt_util.utcnow() + timedelta(seconds=AUTH_BLOCKED_COOLDOWN_S),
            reason=reason,
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
        coord._clear_auth_refresh_rejection_state()
        coord._auth_refresh_suspended_until_utc = None
        coord._auth_refresh_last_success_mono = time.monotonic()
        coord._clear_auth_block(persist=False)
        coord._tokens = tokens
        coord.client.update_credentials(
            eauth=tokens.access_token,
            cookie=tokens.cookie,
        )
        coord._persist_tokens(tokens)
        return True
