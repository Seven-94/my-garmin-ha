# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- Heart rate zone sensors, diagnostic and refreshed every six hours: maximum
  and lactate threshold heart rates, resting heart rate, floors of zones 1 to
  5 and training method, for the DEFAULT sport (the first sport when there is
  none). Lactate threshold and training method are disabled by default; the
  zones of the other sports are attributes.

### Changed

- The fixture anonymiser also draws the `_AWAKE` stress categories.
