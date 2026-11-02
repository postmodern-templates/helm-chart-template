# Features

## Core

### Support for Multiple CI/CD Platforms

- GitHub **Recommended**
    - Creating repos under both users and orgs is supported
- Azure DevOps **Deprecated**
    - See [Azure DevOps Limitations](azure-devops.md) for the features it lacks and why it's being phased out

### Support for Public (Open-Source) and Private (Closed-Source) Repositories

Both public (open-source) and private (closed-source) repos are supported. See [`project_visibility`](template-questions.md#project_visibility-public-private) for what differs.

### Support for Existing Projects

The template can create a new project or apply to an existing repo. See [Applying to an Existing Project](../getting-started/existing-project.md).

### Support for Template Updates

[Copier](https://github.com/copier-org/copier) keeps projects in sync with their parent template. A scheduled GitHub Actions workflow/Azure DevOps pipeline checks for new template releases, and `mise run copier-update` applies them. See [Updating from the Parent Template](../maintaining/updating-from-template.md).

### Project Lifecycle

A project's [`lifecycle`](template-questions.md#lifecycle-pre-alpha-alpha-beta-stable-inactive) (Pre-Alpha, Alpha, Beta, Stable, or Inactive) drives a managed notice at the top of the README. Lifecycle only moves forward (Pre-Alpha to Alpha to Beta to Stable); Inactive is reachable from any state. Change it with `mise run set-lifecycle`.

### AI Agent Instructions

Optionally (via [`agent_instructions`](template-questions.md#agent_instructions-yes-no)), an `AGENTS.md` describes the project's conventions for AI coding agents.

### Code Coverage Reporting

Optionally (via [`code_coverage`](template-questions.md#code_coverage-yes-no)), PR validation publishes a Cobertura `coverage.xml` from the repo root to [Codecov](https://about.codecov.io/) on GitHub, or to Azure Pipelines' built-in coverage reporting on Azure DevOps. Your own test command produces the file.

## Repository Management

- Creation of new repos and branches
- Repo settings and branch protection (on GitHub, applied from `.github/settings.yml` by [gh-settings](https://github.com/noirbizarre/gh-settings)); see [Manual Repo Settings](manual-repo-settings.md)
- On Azure DevOps, a build validation policy requiring PR Validation to pass before a PR completes, with a Project Administrator override
- Creation of useful non-default issue labels: `awaiting pr` and `blocked`
- On public GitHub repos, [CodeQL](https://codeql.github.com/) code scanning and private vulnerability reporting are enabled
- Contributor management and crediting via [All Contributors](https://allcontributors.org/)
- Dependency updates via [Renovate](https://www.mend.io/renovate/); the [GitHub Marketplace
  App](https://github.com/marketplace/renovate/) on GitHub, a self-hosted scheduled pipeline on
  Azure DevOps
- `mise run renovate-lookup`, a local Renovate dry run that shows which dependencies it detects before you push
- A monthly repo health check that link-checks the whole repo
- On GitHub, each scheduled workflow re-enables itself on each scheduled run, so GitHub doesn't disable it after 60 days without repo activity
- Notifications via [Apprise](https://github.com/caronc/apprise) (Slack, Teams, email, and many more) when a template update is available, plus on Azure DevOps when Renovate opens PRs and when a docs PR merges or needs attention; `mise run provision-secrets` sets the secrets these need (see [Secrets and Tokens](token-permissions.md))

## Documentation

Documentation is set up with [Zensical](https://zensical.org/), the successor to the end-of-life [mkdocs-material](https://squidfunk.github.io/mkdocs-material/).

- Multiple choices of documentation target:
    - GitHub Pages: built and versioned for each release, keeping earlier versions
    - A `docs-site` folder in the repo: rendered HTML for offline use, rebuilt from the latest `main`
- Local preview via `mise run docs-server`
- Starter pages for prerequisites, releasing, updating from the parent template, secrets, and manual repo settings

## Code Management and Formatting

- Repo-local dev tool installation via [mise](https://github.com/jdx/mise), with tool caches kept under `.cache/`; see [Development](../maintaining/development.md)
- Linting and formatting on every commit via [prek](https://prek.j178.dev/):
    - Commit message linting via [committed](https://github.com/crate-ci/committed)
    - GitHub Actions linting via [actionlint](https://github.com/rhysd/actionlint)
    - `.github/settings.yml` validation via [gh-settings](https://github.com/noirbizarre/gh-settings) (GitHub only); PR Validation also reminds you to apply a changed file after merging
    - Link checking via [lychee](https://github.com/lycheeverse/lychee)
    - Markdown linting via [rumdl](https://github.com/rvben/rumdl)
    - Shell script linting via [ShellCheck](https://github.com/koalaman/shellcheck)
    - Spell checking via [typos](https://github.com/crate-ci/typos)
    - TOML linting and formatting via [Tombi](https://github.com/tombi-toml/tombi)
    - YAML linting via [ryl](https://github.com/owenlamont/ryl)
    - General hygiene: merge conflict markers, trailing whitespace, final newlines, byte order marks, LF line endings, and filename case conflicts

## CI/CD

- PR validation: the PR title must be a [Conventional Commit](https://www.conventionalcommits.org/), and prek runs against every file the PR changes
- Version number calculation, changelog updating, releasing, and tagging via
  [Knope](https://knope.tech/): each push to `main` updates a release PR, and merging it publishes the release; see [Releasing](../maintaining/releasing.md)
- A manually triggered prerelease (alpha, beta, or rc) workflow/pipeline
- On GitHub, third-party actions are pinned to commit SHAs, and Renovate keeps those pins current

## Support Files

- `.gitignore`, set to ignore macOS `.DS_Store` files, Windows `Thumbs.db` files, and tool caches
- `CHANGELOG.md`, maintained by Knope
- `AGENTS.md`, instructions for AI coding agents
- `CODE_OF_CONDUCT.md`, derived from [The Contributor Covenant](https://www.contributor-covenant.org/) (public projects only)
- `CONTRIBUTING.md`, a guide for first-time contributors (public projects only)
- Issue forms for bug reports, feature requests, and questions, plus a chooser link for private vulnerability reports (public projects only)
- A pull request template reminding contributors that PR titles must be Conventional Commits (public projects only)
- `LICENSE`, a copy of the [MIT License](https://choosealicense.com/licenses/mit/) (public projects only)
- `README.md`, laid out for a product
- `SECURITY.md`, pointing vulnerability reports to GitHub's private reporting (public projects only)

## Editor Configuration

[Visual Studio Code](https://code.visualstudio.com/) settings and extension recommendations are included.
