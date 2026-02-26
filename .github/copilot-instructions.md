# Ghost Ops — Copilot Instructions

## What is Ghost Ops?

Ghost Ops is a **Python 3.14 asyncio daemon** that runs three autonomous
missions on a configurable schedule. It uses an **ELO-routed model selection**
system (seeded from hackathon tournament data) and persists all state in a
**SQLite database** (`ghost_ops.db`).

## File Map

| Path | Purpose |
|------|---------|
| `ghost_ops.py` | Main daemon entry point, scheduler, CLI argument parsing |
| `ghost_ops.toml` | Configuration file — source of truth for all behavior |
| `lib/llm.py` | LLM backend (GitHub Models API + Amplifier CLI fallback) |
| `lib/elo.py` | ELO rating engine for model routing |
| `lib/db.py` | SQLite state store (6 tables) |
| `lib/gh.py` | `gh` CLI wrapper for GitHub API calls |
| `missions/portfolio_watchdog.py` | Mission: monitors repos for security/health signals |
| `missions/inbox_autopilot.py` | Mission: triages and drafts responses for issues/PRs |
| `missions/fleet_evolution.py` | Mission: evolves agent prompts via tournament data |
| `tests/` | Unit test suite (30 tests) |
| `install.sh` | One-command installer for macOS launchd |
| `com.dubsopenhub.ghost-ops.plist` | macOS launchd service definition |

## Non-Negotiables

When editing this codebase, these rules are **never** violated:

1. **Pure stdlib** — No third-party packages. Only Python standard library.
2. **Backup before mutate** — Every file write must create a backup first.
3. **Dry-run always** — Every mission must work in `--dry-run` mode with zero
   side effects.
4. **No auto-post by default** — Draft mode is the default. Live posting
   requires explicit config opt-in.
5. **Config is source of truth** — All tunables live in `ghost_ops.toml`.
   Hard-coded values are bugs.

## PR Requirements

- All 30 unit tests pass: `python3 -m unittest discover -s tests -v`
- Dry-run smoke test passes: `python3 ghost_ops.py --dry-run --once`
- CHANGELOG.md updated for user-facing changes
