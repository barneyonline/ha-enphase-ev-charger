# Contributing to Enphase Energy

Thanks for helping improve this Home Assistant custom integration! We follow Home Assistant and HACS standards to keep the project healthy and dependable. Please read through this guide before opening a pull request.

## Code of Conduct

By participating you agree to uphold the [Home Assistant Code of Conduct](https://www.home-assistant.io/code_of_conduct/). Be respectful and inclusive when interacting with the community.

## How to Help

- Report reproducible bugs and attach diagnostics or logs where possible.
- Suggest enhancements or improvements to documentation.
- Contribute code, tests, translations, or quality scale compliance work.
- Review open pull requests and share constructive feedback.

Before starting large features, open an issue or discussion so we can agree on scope and fit.

## Development Workflow

1. **Fork and clone** the repository.
2. **Create a feature branch** (`feature/...`, `bugfix/...`, or `docs/...`) from the latest `main`.
3. **Install dependencies** in a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r devtools/docker/requirements-dev.txt
   ```
   The requirements mirror our CI jobs, which currently target Python 3.12 and 3.13.
4. **Develop and test** your changes.
5. **Commit with clear messages** and push your branch.
6. **Open a pull request** using the template. Fill in every section and link any related issues.

### Optional Docker workflow

We provide a reproducible environment under `devtools/docker/`:

```bash
docker compose -f devtools/docker/docker-compose.yml build ha-dev
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"
```

## Coding Standards and Tooling

Home Assistant integrations must follow the [core development guidelines](https://developers.home-assistant.io/docs/development_guidelines/). Key points:

- Use modern Python syntax (f-strings, type hints) and keep logging format strings lazy.
- Interact with external services via dedicated client libraries or well-structured helpers.
- Use Google-style docstrings when detailed documentation is needed.
- Keep YAML and documentation formatting consistent with the [Home Assistant style guide](https://developers.home-assistant.io/docs/documenting/yaml-style-guide/).

This repository relies on the following checks. Please run them locally before pushing:

```bash
ruff check .
black custom_components/enphase_ev
pytest -q tests/components/enphase_ev
python scripts/validate_quality_scale.py
pre-commit run --all-files
```

> hassfest validation runs automatically in CI via [`home-assistant/actions/hassfest`](https://github.com/home-assistant/actions/tree/master/hassfest). If you need to run it locally, clone the Home Assistant Core repository and execute `python -m script.hassfest` from your integration checkout.

> Tip: `pre-commit` helps maintain formatting by running the configured hooks automatically. Install it with `pip install pre-commit` and enable with `pre-commit install`.

## Translations

- Place new or updated translations under `custom_components/enphase_ev/translations/<language>.json`.
- Follow Home Assistant’s translation conventions (language files use native phrasing and mirror English keys). When adding a new language, ensure the manifest `version` and documentation reflect the addition and include both the English and native language names where applicable.
- Keep JSON alphabetised and valid UTF-8 (ASCII is preferred unless the language requires accented characters).

## Documentation and Changelog

- Update `README.md` when behaviour, options, or supported features change. Include new configuration parameters using the recommended `configuration_basic` formatting when appropriate.
- Record user-facing changes in `CHANGELOG.md` under an `Unreleased` entry (or add a new version section when preparing a release).

## Tests

- Add or update tests in `tests/components/enphase_ev/` for new functionality or bug fixes.
- Ensure pytest remains fast and deterministic; prefer fixtures over network calls.
- If adding significant functionality, mention how you verified it in the PR description and consider attaching diagnostics captured via Home Assistant’s download tools.

## Pull Request Expectations

- Keep pull requests focused. Separate refactors from functional changes.
- Rebase on top of the latest `main` before requesting review to avoid merge conflicts.
- Ensure GitHub Actions workflows (`tests`, `hassfest`, `Validate`, and `Quality Scale`) pass. They mirror the commands listed above and help maintain HACS compliance (`hacs/action` requires the repository to meet manifest, structure, and metadata checks).
- For UI or translation changes, include screenshots or highlight impacted strings.

## Release Process

Maintainers cut releases by updating the manifest version, changelog, and tagging the release. For beta tags (for example `v2.0.0b4`), publish the GitHub release as a **pre-release** so default HACS users stay on the latest stable line (currently `v1.9.1`) unless they explicitly enable beta releases. Contributors generally do not publish releases directly, but you can help by keeping entries in `CHANGELOG.md` clear and actionable.

## Getting Help

Open a discussion or issue if you are blocked. When in doubt, reference:

- [Home Assistant developer documentation](https://developers.home-assistant.io/) for integration patterns and quality scale rules.
- [HACS documentation](https://hacs.xyz/docs/) for repository requirements, including manifests and validation expectations.

Thank you for contributing!
