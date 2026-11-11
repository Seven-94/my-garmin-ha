# Contributing

Thanks for your interest! This is a small, solo-maintained project.

## Before you start

- For anything bigger than a small fix, open an issue first to discuss it.
- Security issues: see [SECURITY.md](SECURITY.md), never a public issue.

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/) 0.12.x.

```bash
uv sync --all-groups
uv run prek install
```

## Workflow

1. Fork the repository and create a branch from `main`
   (`feat/<topic>`, `fix/<topic>`, `docs/<topic>`…).
2. Make focused changes, with tests.
3. Run the checks:
   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run ty check .
   uv run pytest
   ```
4. Commit using [Conventional Commits](https://www.conventionalcommits.org/).
5. Open a pull request and fill in the template.

## Rules

- Tests never call the Garmin API. Fixtures must be anonymised.
- Never include API keys, tokens or personal data in code, tests, logs or commits.
- Do not weaken lint rules, tests or CI checks to make them pass.

## Use of AI tools

AI-assisted contributions are welcome if you have reviewed, understood and
tested every change, and you state which tool you used in the pull request.
Agent instructions for this repository are in [AGENTS.md](AGENTS.md).
