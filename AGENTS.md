# Agent Workflow Notes

To keep feature branches healthy and avoid CI surprises:

- **Always run hassfest before pushing.** Use the Home Assistant Core tooling (`python -m script.hassfest` from a Core checkout) or an equivalent container so the integration schema stays compliant.
- Run the dockerised test suite: `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev"`.
- Finish with the repository pre-commit hooks: `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"`.

Capture issues locally, fix them, and re-run the relevant checks before creating a PR.
