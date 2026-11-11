# my-garmin-ha

Home Assistant custom integration for the official Garmin Connect Personal API.

> **Status: in development.** No release is published yet.

## Principles

- **Official API only**: authentication with a personal API key. No Garmin
  username, password or MFA.
- **Read-only**: nothing is ever written to the Garmin account.
- **Few requests**: the daily summary is polled regularly; detailed data is
  fetched only after a new watch sync.
- **One key per user**: each user enters their own key in Home Assistant. No key
  is shipped, stored in this repository or shared.

## Requirements

- Home Assistant 2026.11 or later (Python 3.14)
- A Garmin Connect account and a personal API key, created at
  <https://connect.garmin.com/app/settings/personalApiToken>

## Installation

Instructions (HACS custom repository) will come with the first release.

## Development

Requirements: [uv](https://docs.astral.sh/uv/) 0.12.x.

```bash
uv sync --all-groups      # install the project and dev tools
uv run prek install       # install the Git hooks (once per clone)

uv run ruff check .       # lint
uv run ruff format .      # format
uv run ty check .         # type check
uv run pytest             # tests with coverage (fails below 80 %)
```

Git hooks run on `git commit` (ruff, detect-secrets, zizmor, file checks) and on
`git push` (bandit, pip-audit). Dependabot proposes dependency and action updates
every week.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Security

Please report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Disclaimer

This project is not affiliated with or endorsed by Garmin Ltd. "Garmin" and
"Garmin Connect" are trademarks of Garmin Ltd. or its subsidiaries.

## License

[MIT](LICENSE)
