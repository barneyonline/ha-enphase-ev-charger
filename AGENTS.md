# Repository Guidelines

## Project Structure & Module Organization
- Core integration code lives under `custom_components/enphase_ev/` with feature modules such as `sensor.py`, `coordinator.py`, and `config_flow.py`.
- Reusable developer tooling, Docker compose definitions, and pinned dev requirements reside in `devtools/`.
- Automated tests are in `tests/components/enphase_ev/`; mirror the source layout when adding new coverage (e.g., `tests/components/enphase_ev/test_sensor_feature.py`).
- Documentation and project metadata (README, CHANGELOG, quality scale) are in the repository root.

## Build, Test, and Development Commands
- Use the dockerized `ha-dev` environment for **all** linting and tests (ruff, pre-commit, pytest); do not run these locally.
- `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "ruff check ."` — static analysis and import sorting; keep it clean before touching code.
- `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python3 -m pre_commit run --all-files"` — run the full lint/format pipeline exactly as CI.
- `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev -q"` — quick regression against the focused suite.
- `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest"` — authoritative test run inside the pinned dev container (must pass before PR).
- `python3 -m black custom_components/enphase_ev` — apply formatting when Black reports diffs.
- Before pushing any branch, confirm `strings.json` changes are mirrored in every locale under `custom_components/enphase_ev/translations/` so runtime translations stay in sync.
- Use the dockerized `ha-dev` environment for running pytest in this repository to ensure dependencies match CI.

## Coding Style & Naming Conventions
- Python code targets 3.12+ and follows Black defaults (4-space indentation, double quotes).
- Keep imports sorted and deduplicated; rely on Ruff for enforcement.
- Name Home Assistant entities, services, and test fixtures with clear, EV-specific context (e.g., `EnphaseEnergyTodaySensor`, `test_energy_today_rollover`).
- Restrict new dependencies to standard library or Home Assistant core unless justified.

## Testing Guidelines
- Use `pytest` with the HA custom component plugin; prefer descriptive `test_*` names grouped by module.
- Maintain parity between new source files and test modules; include regression tests for bug fixes.
- When mocking API calls, leverage fixtures from existing tests (`random_ids.py`, helper factories) to keep IDs consistent.
- Achieve and preserve 100 % test coverage on all changed files; add targeted tests for new branches and guard conditions.
- Ensure new behavior is covered both in direct unit tests and, when applicable, coordinator integration scenarios.

## Commit & Pull Request Guidelines
- Commit messages follow the repository pattern: concise imperative line (e.g., “Fix Energy Today rollover when session timestamps missing”).
- Squash is not enforced, but keep commits focused and self-testing.
- Use the PR template below when opening a pull request. Fill every section and keep bullet formatting intact.

  ```
  ## Summary
  - <short bullet explaining the first major change>
  - <add more bullets as needed>

  ## Testing
  - <command or checklist entry>
  - <include all linters, pytest invocations, and docker-compose pytest run>
  ```

- Pull requests should reference the branch `fix-*` or `feature/*` naming used in history.
- Include screenshots or log snippets when altering UI or diagnostics.
- Before requesting review, confirm all local quality gates: `ruff check .`, `python3 -m pre_commit run --all-files`, local `pytest`, and the Dockerized `pytest`.
- Never push a branch until `python3 -m pre_commit run --all-files` completes without changes; rerun and commit any formatting/lint fixes first.
- Highlight coverage numbers in the PR description when touching new code to reinforce the 100 % coverage standard.

## GitHub Push Workflow (gh)
- If `git push` hangs, push the branch via the GitHub API using `gh`:
  - Create blobs from local files, build a tree off `main`, and create a commit.
  - Create or update `refs/heads/<branch>` to point at the new commit.
  - Example script pattern (run from repo root):
    - Use `gh api repos/<owner>/<repo>/git/ref/heads/main` to get base SHA.
    - Use `gh api repos/<owner>/<repo>/git/blobs` for each file.
    - Use `gh api repos/<owner>/<repo>/git/trees` to assemble a tree.
    - Use `gh api repos/<owner>/<repo>/git/commits` to create the commit.
    - Use `gh api repos/<owner>/<repo>/git/refs` to create/update the branch ref.

## Best Practice Checks
- Verified sensor rationalisation against Home Assistant developer best practices using Context7 (`/home-assistant/developers.home-assistant`, integration quality scale guidance on dynamic devices and attribute usage).
