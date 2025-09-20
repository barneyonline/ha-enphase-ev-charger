
import asyncio
import json
import os
import pathlib
import sys

import pytest

try:
    import pytest_asyncio  # noqa: F401
except Exception:  # pragma: no cover - plugin optional
    PYTEST_ASYNCIO_AVAILABLE = False
    pytest_plugins = ()
else:
    PYTEST_ASYNCIO_AVAILABLE = True
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1":
        pytest_plugins = ("pytest_asyncio",)
    else:  # respect explicit disable while allowing fallback logic below
        pytest_plugins = ()

@pytest.fixture
def load_fixture():
    def _load(name: str):
        p = pathlib.Path(__file__).parent / "fixtures" / name
        return json.loads(p.read_text())
    return _load



@pytest.fixture
def hass():
    class DummyHass:
        def __init__(self):
            class DummyFlow:
                def async_progress_by_handler(
                    self,
                    handler,
                    include_uninitialized=False,
                    **kwargs,
                ):
                    return []

            class DummyConfigEntries:
                def __init__(self):
                    self.flow = DummyFlow()
                    self._updated = []
                    self._reloaded = []

                def async_get_entry(self, entry_id):
                    return None

                def async_update_entry(self, *args, **kwargs):
                    self._updated.append((args, kwargs))

                async def async_reload(self, *args, **kwargs):
                    self._reloaded.append((args, kwargs))

                def async_start_reauth(self, hass):
                    return None

                def async_entries(self, handler):
                    return []

                async def async_schedule_reload(self, entry):
                    self._reloaded.append(((entry,), {}))
                    return None

                def async_entry_for_domain_unique_id(self, domain, unique_id):
                    return None

                # Helpers for assertions
                @property
                def updated_entries(self):
                    return list(self._updated)

                @property
                def reloaded_entries(self):
                    return list(self._reloaded)

            self.config_entries = DummyConfigEntries()
            self.data = {}

        async def async_create_task(self, coro):
            return await coro

    return DummyHass()

# Ensure repository root is on sys.path for imports like 'custom_components.enphase_ev.*'
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def suppress_frame_usage(monkeypatch):
    """Silence frame helper enforcement when running unit tests without HA core."""
    try:
        from homeassistant.helpers import frame as ha_frame
    except Exception:
        return
    monkeypatch.setattr(
        ha_frame,
        "report_usage",
        lambda *args, **kwargs: None,
        raising=False,
    )


@pytest.fixture(autouse=True)
def stub_issue_registry(monkeypatch):
    try:
        from homeassistant.helpers import issue_registry as ha_issue
    except Exception:
        return

    class DummyIssueRegistry:
        def async_delete_issue(self, *args, **kwargs):
            return None

        def async_create_issue(self, *args, **kwargs):
            return None

    dummy = DummyIssueRegistry()
    monkeypatch.setattr(ha_issue, "async_get", lambda hass: dummy, raising=False)
    monkeypatch.setattr(ha_issue, "async_delete_issue", lambda hass, domain, issue_id: None, raising=False)
    monkeypatch.setattr(
        ha_issue,
        "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: None,
        raising=False,
    )


class _DummyResponse:
    def __init__(self, *, json_data=None, status=200):
        self._json = json_data or {}
        self.status = status
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json

    def raise_for_status(self):
        return None


class _DummySession:
    def __init__(self):
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return _DummyResponse()


@pytest.fixture(autouse=True)
def stub_client_session(monkeypatch):
    session = _DummySession()
    monkeypatch.setattr(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        lambda hass, **kwargs: session,
        raising=False,
    )
    return session


@pytest.fixture(autouse=True)
def ensure_event_loop():
    if PYTEST_ASYNCIO_AVAILABLE and os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1":
        yield
        return
    try:
        asyncio.get_running_loop()
        yield
        return
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        asyncio.set_event_loop(None)
        loop.close()


if not PYTEST_ASYNCIO_AVAILABLE or os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1":

    def pytest_configure(config):
        config.addinivalue_line(
            "markers",
            "asyncio: execute the test coroutine using a dedicated event loop",
        )

    def _wrap_async(func):
        def _sync_wrapper(*args, **kwargs):
            return asyncio.run(func(*args, **kwargs))

        return _sync_wrapper

    def pytest_collection_modifyitems(items):
        for item in items:
            if item.get_closest_marker("asyncio") and asyncio.iscoroutinefunction(item.obj):
                item.obj = _wrap_async(item.obj)
