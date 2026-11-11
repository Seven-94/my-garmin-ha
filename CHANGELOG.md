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
