# Repository Guidelines

## Project Structure & Module Organization
- `custom_components/enphase_ev/`: Home Assistant integration code
  - Core: `api.py`, `coordinator.py`, `const.py`, `__init__.py`
  - Entities: `sensor.py`, `binary_sensor.py`, `number.py`, `button.py`
  - Config/Meta: `manifest.json`, `config_flow.py`, `services.yaml`, `diagnostics.py`
- `tests_enphase_ev/`: Pytest suite with `fixtures/` JSON payloads
- `enphase_ev_custom_docs/`: Design notes and API references
- Root: `README.md`, `LICENSE`

## Build, Test, and Development Commands
- Build dev container: `docker-compose build ha-dev`
- Run tests (inside container): `docker-compose run --rm ha-dev pytest -q tests_enphase_ev`
- Lint/format (inside container): `docker-compose run --rm ha-dev ruff check .` • `docker-compose run --rm ha-dev black custom_components/enphase_ev`
- Pre-commit (required before pushing): `docker-compose run --rm ha-dev bash -lc "cd /workspace && git config --global --add safe.directory /workspace && pip install pre-commit >/tmp/install.log && pre-commit run --all-files"`
- Interactive shell with HA deps preinstalled: `docker-compose run --rm --service-ports ha-dev bash`
- Optional HA core dev: when inside the container, use `async_get_clientsession(hass)` for HTTP and `DataUpdateCoordinator` for polling.

## Coding Style & Naming Conventions
- Python: match current HA supported version (currently 3.12); 4‑space indentation; type hints.
- Names: modules `snake_case.py`; classes `PascalCase`; constants `UPPER_SNAKE`.
- HA patterns: async I/O only; share HTTP via `async_get_clientsession(hass)`; use `DataUpdateCoordinator` and `CoordinatorEntity`.
- Entities: prefer `_attr_has_entity_name = True` and `DeviceInfo`; avoid embedding serials in display names.
- Logging via `_LOGGER`; never log secrets or full responses.

## Testing Guidelines
- Framework: `pytest` with `pytest-homeassistant-custom-component` when interacting with HA helpers.
- Location: `tests_enphase_ev/` with `test_*.py`; fixtures under `tests_enphase_ev/fixtures/`.
- Coverage: API URL building, data mapping, and coordinator update behavior. Add fixtures for new endpoints.
- Run tests via Docker (`docker-compose run --rm ha-dev pytest -q`); keep tests isolated and async-safe (`@pytest.mark.asyncio`).

## Commit & Pull Request Guidelines
- Commits: imperative + scoped (e.g., `api:`, `sensor:`). Example: `sensor: add connector status sensor`.
- PRs: description, linked issue, test results, and screenshots or logs when behavior changes.
- Gate: tests green; `ruff`/`black` clean; keep changes focused.

## Security & Configuration Tips
- Never commit real `e-auth-token` or `Cookie`; use fixtures only.
- Redact credentials in diagnostics (see `diagnostics.py`); avoid logging headers or bodies.
- Respect rate limits; keep conservative defaults in `const.py`.

## Manifest & Services (HA specifics)
- `manifest.json`: include `version`, `domain`, `name`, `codeowners`, `documentation`, `issue_tracker`, `requirements`, `iot_class`, and `integration_type` (likely `hub`). Set `config_flow: true` if using a UI flow.
- `services.yaml`: describe services for the UI; register service handlers in code (async, validated with `voluptuous`).
