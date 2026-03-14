# Agents

## Overview
Ghost Ops is an autonomous agent operations daemon (`ghost_ops.py`) that runs continuously on macOS via `launchd`. It orchestrates three autonomous missions using the GitHub Models API (with Copilot CLI fallback) and an ELO router to direct tasks to the best-performing LLM.

## Available Agents (Missions)

### Portfolio Watchdog
- **Purpose**: Nightly scan of configured GitHub repositories for security regressions, compliance drift, and notable activity. Writes findings to the SQLite state store and triggers alerts.
- **Schedule**: `0 6 * * *` (6 AM daily, configurable in `ghost_ops.toml`)
- **Usage**: `python3 ghost_ops.py --mission portfolio_watchdog --dry-run`
- **Model**: ELO-routed (best-performing model from `~/.copilot/hackathon-elo.json`)

### Inbox Autopilot
- **Purpose**: Hourly triage of new GitHub issues and pull requests. Drafts LLM-generated responses for human review before posting. Reduces response latency on active repos.
- **Schedule**: `0 * * * *` (every hour, configurable)
- **Usage**: `python3 ghost_ops.py --mission inbox_autopilot --dry-run`
- **Model**: ELO-routed

### Fleet Evolution
- **Purpose**: Daily mutation and improvement of underperforming agent prompts. Validates proposed changes with 3-model consensus before deploying winners. Integrates with Grid Medic patterns.
- **Schedule**: `0 3 * * *` (3 AM daily, configurable)
- **Usage**: `python3 ghost_ops.py --mission fleet_evolution --dry-run`
- **Model**: 3-model consensus (ELO-ranked)

## Architecture

```
launchd (survives reboots)
  └── ghost_ops.py
       ├── Scheduler ──── cron-like trigger engine
       ├── ELO Router ─── routes tasks to best-performing models
       ├── LLM Backend ── GitHub Models API → Amplifier CLI fallback
       ├── State Store ── SQLite (ghost_ops.db)
       └── Alert System ─ writes to alerts table
```

## Configuration
- Edit `ghost_ops.toml` to enable/disable missions, set schedules, and specify target repos.
- Install as a persistent macOS daemon: `bash install.sh`
- Dry-run all missions without API calls: `python3 ghost_ops.py --dry-run --once`
- **Requirements**: macOS, Python 3.14+ (pure stdlib — zero pip dependencies), `gh` CLI authenticated.
- ELO scores are read from `~/.copilot/hackathon-elo.json`; agents directory defaults to `~/.copilot/agents`.
