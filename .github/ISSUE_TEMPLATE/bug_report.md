---
name: 🐛 Bug Report
about: Report a bug in Ghost Ops
title: "[Bug] "
labels: bug
assignees: ''
---

## Description

<!-- A clear and concise description of what the bug is. -->

## Which mission failed?

- [ ] `portfolio_watchdog`
- [ ] `inbox_autopilot`
- [ ] `fleet_evolution`
- [ ] Scheduler / daemon core
- [ ] Other: <!-- specify -->

## Mode

- [ ] Normal
- [ ] Dry-run (`--dry-run`)

## Steps to Reproduce

1. 
2. 
3. 

## Expected Behavior

<!-- What should have happened? -->

## Actual Behavior

<!-- What actually happened? Include error messages or log output. -->

## Relevant `ghost_ops.db` State

<!-- If applicable, include relevant rows from the SQLite database. -->

```sql
-- e.g. SELECT * FROM missions WHERE name = '...';
```

## Logs

<!-- Paste relevant output from logs/ directory. -->

```
```

## Environment

- **OS**: <!-- e.g. macOS 15.3 -->
- **Python version**: <!-- e.g. 3.14.0a5 -->
- **`gh` CLI version**: <!-- e.g. 2.65.0 -->
- **Ghost Ops version**: <!-- e.g. 0.1.0 -->
