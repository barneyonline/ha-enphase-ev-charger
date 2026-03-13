# Repository Guidelines

## Project Structure & Module Organization
- Integration code lives in `custom_components/enphase_ev/`; the main coordination and API logic is concentrated in `coordinator.py`, `api.py`, `sensor.py`, `config_flow.py`, and the platform modules (`button.py`, `number.py`, `select.py`, `switch.py`, `update.py`).
- Tests live in `tests/components/enphase_ev/`; keep new coverage close to the behavior being changed and reuse `conftest.py`, `random_ids.py`, fixture JSON, and snapshot files where possible.
- User-facing strings are maintained in `custom_components/enphase_ev/strings.json` and mirrored in `custom_components/enphase_ev/translations/*.json`.
- Contributor documentation is in `README.md`, `CONTRIBUTING.md`, and `CHANGELOG.md`; API research and reference material are under `docs/api/`; maintenance scripts are under `scripts/`; the pinned Docker dev environment is under `devtools/docker/`.

## Build, Test, and Development Commands
- Prefer the pinned Docker environment for agent-driven validation:
- Use the pinned Docker environment for all linting, formatting, coverage, and test commands. Do not use a local virtualenv in this repository.
  - `docker compose -f devtools/docker/docker-compose.yml build ha-dev`
  - `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "<command>"`
- Run the repository’s standard checks before finalizing changes:
  - `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "ruff check ."`
  - `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "black custom_components/enphase_ev"`
  - `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest -q tests/components/enphase_ev"`
  - `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python scripts/validate_quality_scale.py"`
  - `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"`
- Keep changed Python modules at 100% targeted coverage:
  - `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python -m coverage erase && python -m coverage run -m pytest tests/components/enphase_ev -q && python -m coverage report -m --include=<comma-separated-paths> --fail-under=100"`
- When running pytest in Docker, target the on-disk test directory in this checkout: `docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest -q tests/components/enphase_ev"`.

## Coding Style & Naming Conventions
- Follow Home Assistant integration patterns and keep dependencies limited to the standard library or Home Assistant unless there is a strong reason otherwise.
- Target Home Assistant `2024.12.0+` behavior and Python `3.13` syntax/runtime expectations.
- Keep external I/O async and avoid blocking the event loop; use executor jobs only for genuinely blocking work.
- Keep code Black-formatted, Ruff-clean, and consistent with lazy logging and clear type hints.
- Keep `try` blocks narrow and raise the most specific Home Assistant exception available.
- For transient setup failures, raise `ConfigEntryNotReady` from `async_setup_entry`; use `ConfigEntryAuthFailed` for expired or invalid credentials so Home Assistant can start reauth.
- Name entities, services, and fixtures with explicit Enphase domain context.

## Testing Guidelines
- Maintain 100% coverage for every touched file; cover new branches and guard paths, not just happy paths.
- Pair source changes with matching tests in `tests/components/enphase_ev/`, including coordinator coverage when behavior is driven through refresh/update flows.
- Reuse existing helper fixtures and deterministic IDs when mocking API payloads.
- Prefer tests of Home Assistant integration behavior over tests of mock internals; assert outbound API calls directly for service actions.
- For diagnostics changes, test that sensitive fields remain redacted and that the diagnostics output matches the expected payload.
- Translation or repair-issue changes should include regression coverage when applicable, including `tests/components/enphase_ev/test_service_translations.py`.

## Translations, Docs, and Changelog
- Any user-facing string change must update `custom_components/enphase_ev/strings.json` and every locale file under `custom_components/enphase_ev/translations/`.
- Do not leave new non-English locale entries in English.
- If translation or manifest changes need `hassfest` validation, follow `CONTRIBUTING.md` for the local workflow; CI also runs hassfest for this repository.
- Update `README.md` or `docs/` when supported features, setup, or behavior change.
- Update `CHANGELOG.md` for user-facing changes.

## Commit & Pull Request Guidelines
- Keep commits focused and use concise imperative summaries.
- Repository contributor docs use branch names like `feature/...`, `bugfix/...`, or `docs/...`.
- Before opening or updating a PR, run the relevant quality gates above and fill out `.github/pull_request_template.md`.
- Include exact commands run in the PR testing section and call out coverage for touched modules.
- Include diagnostics when changing repairs or diagnostics output.

### PR Creation Workflow (Required)
Follow this exact sequence to create a PR correctly:

1. Ensure your branch is current and clean:
   - `git fetch origin`
   - `git status --short` (must be clean before final push)
2. Run quality gates in Docker and fix any failures before commit:
   - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "ruff check ."`
   - `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "black <changed-python-files>"` and include `tests/components/enphase_ev/...` for changed test files. This must be run before every push to `origin`.
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
