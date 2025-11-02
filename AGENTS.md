# Repository Guidelines

## Project Structure & Module Organization
- Core integration code lives under `custom_components/enphase_ev/` with feature modules such as `sensor.py`, `coordinator.py`, and `config_flow.py`.
- Reusable developer tooling, Docker compose definitions, and pinned dev requirements reside in `devtools/`.
- Automated tests are in `tests/components/enphase_ev/`; mirror the source layout when adding new coverage (e.g., `tests/components/enphase_ev/test_sensor_feature.py`).
- Documentation and project metadata (README, CHANGELOG, quality scale) are in the repository root.

## Build, Test, and Development Commands
- `pytest tests/components/enphase_ev -q` — run focused unit tests with minimal output.
- `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest"` — execute the full test matrix inside the maintained dev container.
- `ruff check .` — static analysis and import sorting; use `ruff check . --fix` for autofixable issues.
- `python3 -m black custom_components/enphase_ev` — apply code formatting to Python sources.
- `python3 -m pre_commit run --all-files` — run the lint+format hooks exactly as CI expects.

## Coding Style & Naming Conventions
- Python code targets 3.12+ and follows Black defaults (4-space indentation, double quotes).
- Keep imports sorted and deduplicated; rely on Ruff for enforcement.
- Name Home Assistant entities, services, and test fixtures with clear, EV-specific context (e.g., `EnphaseEnergyTodaySensor`, `test_energy_today_rollover`).
- Restrict new dependencies to standard library or Home Assistant core unless justified.

## Testing Guidelines
- Use `pytest` with the HA custom component plugin; prefer descriptive `test_*` function names grouped by module.
- Maintain parity between new source files and test modules; include regression tests for bug fixes.
- When mocking API calls, leverage fixtures from existing tests (`random_ids.py`, helper factories) to keep IDs consistent.
- Ensure new behavior is covered both in direct unit tests and, when applicable, coordinator integration scenarios.

## Commit & Pull Request Guidelines
- Commit messages follow the repository pattern: concise imperative line (e.g., “Fix Energy Today rollover when session timestamps missing”).
- Squash is not enforced, but keep commits focused and self-testing.
- Pull requests should reference the branch `fix-*` or `feature/*` naming used in history, describe the change, list test commands, and include screenshots/log snippets when altering UI or diagnostics.
- Confirm PRs pass `pre-commit` and Dockerized test runs before requesting review.
