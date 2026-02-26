# 🛠️ Contributing to Ghost Ops

Thank you for your interest in contributing to Ghost Ops! This guide will help
you get started.

## Core Principles

Before making any changes, understand the non-negotiable design principles:

1. **Backup before mutate is sacred** — Every write operation must create a
   backup first. No exceptions.
2. **Config is source of truth** — `ghost_ops.toml` drives all behavior.
   Hard-coded values are bugs.
3. **Pure stdlib** — Ghost Ops uses only the Python standard library. No
   third-party packages.
4. **Dry-run everything** — Every mission must support `--dry-run` mode and
   produce identical logs without side effects.

## Getting Started

### Prerequisites

- Python 3.14+
- `gh` CLI (authenticated)
- macOS (for launchd integration)

### Development Workflow

1. **Fork** the repository and clone your fork.
2. **Create a branch** from `main`:
   ```bash
   git checkout -b ghost-ops/<topic>
   ```
3. **Make your changes** following the core principles above.
4. **Run the test suite**:
   ```bash
   python3 -m unittest discover -s tests -v
   ```
5. **Run a dry-run smoke test**:
   ```bash
   python3 ghost_ops.py --dry-run --once
   ```
6. **Commit** with a clear, descriptive message.
7. **Open a Pull Request** against `main`.

## Modifying Missions

Missions live in the `missions/` directory. When modifying a mission:

- **Change one mission at a time** — keep PRs focused.
- **Test in dry-run mode** before testing with live side effects.
- **Verify backup logic** — if your mission writes files, ensure backups are
  created before any mutation.
- **Update the CHANGELOG** with your changes.

## Code Style

- Follow existing patterns in the codebase.
- Keep functions small and focused.
- Use type hints where practical.
- Docstrings for public functions.

## Reporting Bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md) and include:
- Which mission failed
- Whether you were in normal or dry-run mode
- Relevant logs from `logs/`

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
