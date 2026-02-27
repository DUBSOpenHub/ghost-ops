# 👻 Ghost Ops

**Your agents clock in when you clock out.**

Ghost Ops is an autonomous agent operations daemon that runs 24/7 on macOS via `launchd`. Three missions, one brain.

## What It Does

| Mission | Schedule | Function |
|---------|----------|----------|
| **Portfolio Watchdog** | Nightly | Scans your GitHub repos for security regressions, compliance drift, and activity |
| **Inbox Autopilot** | Hourly | Triages new issues and PRs with LLM-drafted responses (human reviews before posting) |
| **Fleet Evolution** | Daily | Mutates underperforming agent prompts, validates with 3-model consensus, deploys winners (see [Grid Medic](#grid-medic)) |

## Architecture

```
launchd (survives reboots)
  └── ghost_ops.py
       ├── Scheduler ─────── cron-like trigger engine
       ├── ELO Router ────── routes tasks to best-performing models
       ├── LLM Backend ───── GitHub Models API → Amplifier CLI fallback
       ├── Mission: Portfolio Watchdog
       ├── Mission: Inbox Autopilot
       ├── Mission: Fleet Evolution
       ├── State Store ────── SQLite ghost_ops.db
       └── Alert System ──── writes to alerts table
```

## Quick Start

```bash
# Clone
git clone https://github.com/DUBSOpenHub/ghost-ops.git
cd ghost-ops

# Test (no API calls)
python3 ghost_ops.py --dry-run --once

# Run a single mission
python3 ghost_ops.py --mission portfolio_watchdog --dry-run

# Install as daemon
bash install.sh
```

## Requirements

- **macOS** (launchd)
- **Python 3.14+** (pure stdlib — zero pip dependencies)
- **GitHub CLI** (`gh`) — authenticated

## Configuration

Edit `ghost_ops.toml`:

```toml
[ghost_ops]
log_level = "INFO"
db_path = "ghost_ops.db"
elo_path = "~/.copilot/hackathon-elo.json"
agents_dir = "~/.copilot/agents"

[missions.portfolio_watchdog]
enabled = true
schedule = "0 6 * * *"    # 6 AM daily
repos = ["owner/repo1", "owner/repo2"]

[missions.inbox_autopilot]
enabled = true
schedule = "0 * * * *"    # every hour

[missions.fleet_evolution]
enabled = true
schedule = "0 3 * * *"    # 3 AM daily
```

## Key Design Decisions

- **Pure Python stdlib** — no pip dependencies, no venv, no version drift
- **LLM calls via `gh api`** — reuses existing GitHub auth, no API key management
- **ELO-routed model selection** — your hackathon data picks the best model per task
- **Drafts only by default** — Inbox Autopilot never auto-posts without explicit config
- **Backup before mutate** — Fleet Evolution always backs up agent files first
- **Dry-run everything** — every mission supports `--dry-run` for safe testing

## Project Structure

```
ghost-ops/
├── ghost_ops.py          # Main daemon
├── ghost_ops.toml        # Configuration
├── install.sh            # One-command installer
├── com.dubsopenhub.ghost-ops.plist
├── lib/
│   ├── elo_router.py     # ELO-based model selection
│   ├── llm_backend.py    # LLM API abstraction
│   └── state.py          # SQLite state store (6 tables)
├── missions/
│   ├── portfolio_watchdog.py
│   ├── inbox_autopilot.py
│   └── fleet_evolution.py
└── tests/
    ├── test_elo_router.py
    └── test_state.py
```

## Grid Medic

Fleet Evolution is the daemon implementation of the **[Grid Medic](https://github.com/DUBSOpenHub/ghost-ops/blob/main/missions/fleet_evolution.py)** 🚑 pattern — a self-healing meta-agent that continuously monitors, repairs, and improves the agent fleet.

| Capability | How It Works |
|---|---|
| **Fitness scoring** | Ranks agents by ELO performance data; lowest-fitness agents evolve first |
| **LLM-powered mutation** | Generates improved agent prompts via configurable models |
| **Multi-model consensus** | 2-of-3 validator models must approve before any change deploys |
| **A/B testing** | Runs original vs. mutated prompts on the same task; blind-judges the outputs |
| **Auto-rollback** | Re-tests deployed mutations within 24 hours; rolls back any that regress |
| **Agent X-Ray integration** | Scores agent files with deterministic pattern matching (zero tokens) |
| **Paired-file sync** | Keeps co-located copies (e.g., skill files) in sync after approved mutations |

When used interactively via Copilot CLI, Grid Medic is available as a standalone agent (`grid-medic`) that can diagnose, improve, and validate individual agents on demand. Fleet Evolution runs the same logic autonomously on a schedule.

## Testing

```bash
# Unit tests (30 tests, no network required)
python3 -m unittest discover -s tests -v

# Dry-run smoke test
python3 ghost_ops.py --dry-run --once
```

## License

MIT
