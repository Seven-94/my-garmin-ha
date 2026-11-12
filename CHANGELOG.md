# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Hydration sensors, disabled by default: sweat loss and fluid intake during
  activities, refreshed every hour and after each watch sync, with the time of
  the last entry as an attribute. Intake and goal stay on the daily summary.

### Fixed

- The release zip stores its files uncompressed, so a local build of a tag
  gives the same bytes as the published `my_garmin.zip`.

## [0.1.0] - 2026-11-12

### Added

- Project setup: tooling, CI, contribution and security guidelines.
- Integration skeleton `custom_components/my_garmin` (manifest only).
- Asynchronous client for the Garmin Connect Personal API: API key to token
  exchange with in-memory renewal, one retry on a revoked token, typed errors.
- Scripts to capture raw API responses and turn them into anonymised fixtures.
- Config flow: API key and its expiry date, re-authentication and
  reconfiguration, one Garmin account per Home Assistant instance.
- Daily summary sensors, refreshed every 10 minutes: steps, step goal,
  distance, calories, intensity minutes, floors, heart rate, stress, Body
  Battery, respiration, SpO2 and last sync.
- Sleep sensors (duration, stages, score, quality, bedtime, wake-up time),
  refreshed after each watch sync; last night stays shown until the next one
  is recorded.
- Every other daily summary field: activity time, floors descended and goals,
  calorie details (remaining calories can be negative), wheelchair pushes,
  7-day and 2-minute heart rates, abnormal heart rate alerts, maximum stress,
  stress category, durations and shares, Body Battery charged and drained,
  respiration and SpO2 details, hydration and wellness distance. Niche ones are
  disabled by default.
- Jet lag binary sensor, with the trip name as an attribute.
- Every other sleep field: naps (with their times as an attribute),
  unmeasurable sleep, awakenings, sleep stress and heart rate, sleep
  respiration and SpO2, breathing disruptions, sleep need for the current and
  next night with the recommended bedtime, sleep alignment, score component
  ratings and stage shares. Score feedback codes are attributes of the sleep
  score.
- Heart rate variability (HRV) sensors, refreshed after each watch sync: last
  night average, 7-day average, last night 5-minute high (disabled by default)
  and HRV status, with the baseline bounds and the feedback code as attributes.
  Devices without HRV get unknown values, without errors or log entries.
- Weight sensors for the latest weigh-in, refreshed after each watch sync and
  every hour: weight, weight change, muscle mass, BMI, body fat and body water;
  bone mass, visceral fat, metabolic age and physique rating are disabled by
  default. Garmin sends masses in grams although its spec says kilograms: both
  are handled. Weight goal, with its low and high bounds disabled by default.
- Last activity sensor: the activity name, with its type, start and end times
  and every other summary field as attributes (social data and GPS positions
  are left out). Refreshed after each watch sync and every hour.
- New activity event, fired once per activity recorded while Home Assistant
  runs; activities already known at start-up fire nothing. Activity names are
  never logged.
- API key expiry: a diagnostic date sensor, and a repair issue a week before
  the date the user entered (warning), turning into an error once it has
  passed. Reconfiguring with a new key and date removes it.
- Watch sync overdue problem sensor: on when the last watch sync is older than
  a threshold; the last sync seen is kept across midnight and restarts.
- Options: sync overdue threshold (24 h by default) and daily summary refresh
  interval (10 min by default); saving them reloads the integration.
- Diagnostics for bug reports: the entry and each coordinator's state, with
  identifiers, names, URLs, GPS positions and free text redacted; health
  measurements and times are replaced by their shape (type and digits);
  menstrual and nutrition data are left out.
- Fitness age sensors, refreshed every six hours and after each watch sync:
  fitness age, with its components and last update as attributes; achievable
  and previous fitness age (previous one disabled by default); and the reason
  there is no fitness age as a diagnostic sensor. The chronological age is
  never read.
- Heart rate zone sensors, diagnostic and refreshed every six hours: maximum
  and lactate threshold heart rates, resting heart rate, floors of zones 1 to
  5 and training method, for the DEFAULT sport (the first sport when there is
  none). Lactate threshold and training method are disabled by default; the
  zones of the other sports are attributes.
- SpO2 and acclimation sensors: seven-day average and average during sleep,
  refreshed after each watch sync and every two hours. Average, lowest and
  latest SpO2 (same values as the daily summary), average during tomorrow's
  sleep and average altitude are disabled by default. Devices without SpO2 get
  unknown values, without errors.
- Abnormal heart rate alert thresholds (high and low), diagnostic sensors
  refreshed every six hours. An alert that is off, or a day without data, shows
  unknown.
- `hacs.json`, and CI jobs that validate the integration with hassfest and
  the HACS action.
- Integration icon, shipped in `custom_components/my_garmin/brand/` for Home
  Assistant and HACS.
- Release workflow: pushing a `vX.Y.Z` tag that matches the manifest version
  publishes a GitHub release with `my_garmin.zip`, installable from HACS, and
  the changelog section as notes. Installation steps in the README.

### Changed

- The fixture anonymiser also draws the `_AWAKE` stress categories.

[Unreleased]: https://github.com/Seven-94/my-garmin-ha/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Seven-94/my-garmin-ha/releases/tag/v0.1.0
