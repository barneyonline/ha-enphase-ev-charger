
import json
import pathlib
import pytest
import sys

# Register HA test plugin to provide the 'hass' fixture
pytest_plugins = "pytest_homeassistant_custom_component"

@pytest.fixture
def load_fixture():
    def _load(name: str):
        p = pathlib.Path(__file__).parent / "fixtures" / name
        return json.loads(p.read_text())
    return _load

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
