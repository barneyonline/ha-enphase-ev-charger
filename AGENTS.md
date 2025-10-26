# Agent Workflow Notes

To keep feature branches healthy and avoid CI surprises:

- Run the dockerised test suite: `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev"`.
- Finish with the repository pre-commit hooks: `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"`.
- Run `ruff check` (standalone) because the pre-commit configuration only covers selected files.
- Ensure every change is covered by automated tests: add or update unit tests for new logic and confirm the relevant suites include the modified paths so Codecov patch coverage stays at 100%.

Capture issues locally, fix them, and re-run the relevant checks before creating a PR.
