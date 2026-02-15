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
- Before pushing any branch, run a targeted coverage check for touched modules with `coverage.py` (the `ha-dev` image does not provide `pytest-cov`) and fix any gaps (example for `api.py` + `config_flow.py`):
  - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python3 -m coverage erase && python3 -m coverage run -m pytest tests/components/enphase_ev -q && python3 -m coverage report -m --include=custom_components/enphase_ev/api.py,custom_components/enphase_ev/config_flow.py --fail-under=100"`
- `python3 -m black custom_components/enphase_ev` — apply formatting when Black reports diffs.
- Before pushing any branch, confirm `strings.json` changes are mirrored in every locale under `custom_components/enphase_ev/translations/` so runtime translations stay in sync.
- Any change that adds or modifies user-facing strings (entities, services, issues, repairs, diagnostics labels, config/options flow text) must update `custom_components/enphase_ev/strings.json` and every file in `custom_components/enphase_ev/translations/` in the designated locale language.
- Do not leave newly added keys in English for non-English locale files; translate them (do not rely on English fallback).
- Translation sync is mandatory and blocking: if `strings.json` changes, the same PR must include matching updates to all locale files and any needed translation regression tests before merge.
- After translation updates, run `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev/test_service_translations.py -q"` and fix any failures before push.
- Use the dockerized `ha-dev` environment for running pytest in this repository to ensure dependencies match CI.

## Coding Style & Naming Conventions
- Python code targets 3.12+ and follows Black defaults (4-space indentation, double quotes).
- Keep imports sorted and deduplicated; rely on Ruff for enforcement.
- Name Home Assistant entities, services, and test fixtures with clear, EV-specific context (e.g., `EnphaseEnergyTodaySensor`, `test_energy_today_rollover`).
- Restrict new dependencies to standard library or Home Assistant core unless justified.
- For BatteryConfig/system-profile features, keep profile keys canonical (`self-consumption`, `cost_savings`, `backup_only`) and preserve unknown regional profile keys as passthrough values.
- Keep `showStormGuard` out of BatteryConfig profile-option logic; it belongs to Storm Guard controls.

## Testing Guidelines
- Use `pytest` with the HA custom component plugin; prefer descriptive `test_*` names grouped by module.
- Maintain parity between new source files and test modules; include regression tests for bug fixes.
- When mocking API calls, leverage fixtures from existing tests (`random_ids.py`, helper factories) to keep IDs consistent.
- Achieve and preserve 100 % test coverage on all changed files; add targeted tests for new branches and guard conditions.
- Ensure new behavior is covered both in direct unit tests and, when applicable, coordinator integration scenarios.
- BatteryConfig profile-control changes must include coordinator tests for: site-settings flag parsing, unknown profile passthrough, pending/apply/cancel lifecycle, and write lock/debounce safeguards.
- BatteryConfig diagnostics updates must include tests that assert payload snapshots are present and sensitive fields remain redacted.
- Repair issue additions (new `issues.*` keys) must include translation coverage in `strings.json` and all locale files.

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

### PR Creation Workflow (Required)
Follow this exact sequence to create a PR correctly:

1. Ensure your branch is current and clean:
   - `git fetch origin`
   - `git status --short` (must be clean before final push)
2. Run quality gates in Docker and fix any failures before commit:
   - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "ruff check ."`
   - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python3 -m pre_commit run --all-files"`
   - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python3 -m coverage erase && python3 -m coverage run -m pytest tests/components/enphase_ev -q && python3 -m coverage report -m --include=<touched-module-paths-comma-separated> --fail-under=100"`
   - If `strings.json` changed: update every locale file under `custom_components/enphase_ev/translations/` and verify non-English values are localized (no English fallback for new keys).
   - If translations changed: `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev/test_service_translations.py -q"`
   - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev -q"`
   - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest"`
3. Commit with an imperative message and keep scope focused:
   - `git add -A && git commit -m "<imperative summary>"`
4. Push branch to origin:
   - `git push -u origin <branch-name>`
5. Create PR with `gh` using a body file (do not inline Markdown with backticks in shell):
   - `cat > /tmp/pr_body.md <<'EOF'`
   - Include `## Summary` and `## Testing` sections with exact commands run
   - `EOF`
   - `gh pr create --base main --head <branch-name> --title "<PR title>" --body-file /tmp/pr_body.md`
6. Verify the PR metadata after creation:
   - `gh pr view --json number,url,headRefName,baseRefName,title`
7. If a PR already exists for the branch, update it instead of creating a duplicate:
   - `gh pr edit --title "<updated title>" --body-file /tmp/pr_body.md`

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
