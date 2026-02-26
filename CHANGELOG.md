# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Live A/B testing in Fleet Evolution — runs original and mutated agents on same task, blind-judges output quality
- A/B scores tracked in mutations table (ab_score_original, ab_score_mutated, ab_task, ab_winner)
- Auto-rollback — reverts deployed mutations that regress in subsequent A/B tests
- Stricter consensus: if A/B test favors original, all 3 validators must approve (not just 2/3)

## [0.1.0] - 2026-02-26

### Added

- Three autonomous missions: Portfolio Watchdog, Inbox Autopilot, Fleet Evolution
- ELO-routed model selection from hackathon data
- GitHub Models API + Amplifier CLI fallback LLM backend
- SQLite state store with 6 tables
- macOS launchd integration (KeepAlive, RunAtLoad)
- Dry-run mode for all missions
- Single-mission execution via --mission flag
- Graceful SIGTERM shutdown
- One-command installer (install.sh)
- Unit test suite (30 tests)
