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

### Changed

- The fixture anonymiser also draws the `_AWAKE` stress categories.
