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

### With HACS (recommended)

1. In Home Assistant, open **HACS**, then the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/Seven-94/my-garmin-ha` with the type **Integration**.
3. Search for **My Garmin** in HACS, open it and select **Download**.
4. Restart Home Assistant.
5. Go to **Settings** → **Devices & services** → **Add integration**, search for
   **My Garmin** and enter your personal API key and its expiry date.

HACS installs the `my_garmin.zip` file attached to each
[GitHub release](https://github.com/Seven-94/my-garmin-ha/releases).

### Manually

1. Download `my_garmin.zip` from the latest
   [release](https://github.com/Seven-94/my-garmin-ha/releases).
2. Extract it into `<config>/custom_components/my_garmin/`, where `<config>` is
   the folder that holds `configuration.yaml`.
3. Restart Home Assistant, then add the integration as in step 5 above.

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

### Test fixtures

Tests never call Garmin: they read anonymised copies of real responses in
`tests/fixtures/`. To refresh them with your own account:

```bash
uv run python -m scripts.capture      # asks for your API key (hidden input)
uv run python -m scripts.anonymise    # captures/ -> tests/fixtures/
```

`captures/` holds the raw responses, with personal data: it is ignored by Git.
The anonymiser renumbers identifiers, replaces names, e-mails, URLs, GPS
coordinates and demographic fields, shifts every date by a random number of
weeks, moves the times of sleep, activities, latest readings and weigh-ins by
a random 1 to 4 hours, replaces every measurement with a synthetic value of
the same format and draws stress and sleep categories at random: no real
health data or daily habit is committed. It writes nothing if an original
value remains. Review the fixtures and `captures/keys.txt` before committing.

## Security

Please report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Disclaimer

This project is not affiliated with or endorsed by Garmin Ltd. "Garmin" and
"Garmin Connect" are trademarks of Garmin Ltd. or its subsidiaries.

## License

[MIT](LICENSE)
