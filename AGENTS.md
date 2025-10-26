# Agent Workflow Notes

To keep feature branches healthy and avoid CI surprises:

- **Always run hassfest before pushing.** Use the Home Assistant Core tooling (`python -m script.hassfest` from a Core checkout) or an equivalent container so the integration schema stays compliant.
- Run the dockerised test suite: `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest tests/components/enphase_ev"`.
- Finish with the repository pre-commit hooks: `docker-compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"`.
- Run `ruff check` (standalone) because the pre-commit configuration only covers selected files; this ensures lint errors outside the allow-list are caught before pushing.

Capture issues locally, fix them, and re-run the relevant checks before creating a PR.

## Branch & PR Process

1. Create a topic branch from `main`: `git checkout -b feature/<short-description>` (or `bugfix/`, `docs/` as appropriate).
2. Immediately update the `## Unreleased` section in `CHANGELOG.md` with a short, present-tense bullet list describing the planned changes for this branch.
3. Stage and commit your work with focused messages: `git add <files> && git commit -m "<scope>: <summary>"`.
4. Ensure hassfest, pytest, and pre-commit have succeeded locally (see commands above).
5. Push the branch: `git push origin <branch-name>`.
6. Open a pull request against `main` (for example `gh pr create --base main --head <branch>`) and fill out every section of the template, including links to related issues and the commands you ran.
7. Monitor CI and address any feedback or failures before requesting review.

## Release Preparation Checklist

Follow these steps whenever you cut a new release:

1. Create a version branch from `main`, e.g. `git checkout -b version-X.Y.Z`.
2. Review `CHANGELOG.md` and move the relevant `Unreleased` entries under a new `## vX.Y.Z – YYYY-MM-DD` heading using the release-notes template below; leave fresh placeholders under `## Unreleased`.
3. Bump `custom_components/enphase_ev/manifest.json` to the new version.
4. Update `README.md` or other docs if the release introduces user-facing changes that need highlighting.
5. Ensure all tests and checks pass locally (`python -m script.hassfest`, `pytest tests/components/enphase_ev`, `pre-commit run --all-files` or their docker equivalents).
6. Commit the release changes, push the branch, and open a PR referencing any issues or milestones.
7. Tag and publish the release only after the PR merges.

## Release Notes Template

When drafting release notes (CHANGELOG entries, GitHub releases, etc.), follow this structure and include every heading even if a section has “None”:

```
## vX.Y.Z – YYYY-MM-DD

### 🚧 Breaking changes
- …

### ✨ New features
- …

### 🐛 Bug fixes
- …

### 🔧 Improvements
- …

### 🔄 Other changes
- …
```

Document migrations, highlight benefits, reference PR/issue numbers where helpful, keep bullet phrasing concise and user focused, and add a link to the relevant `CHANGELOG.md` section at the end of the release notes.
