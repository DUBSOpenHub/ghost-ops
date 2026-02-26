# 🔒 Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | ✅ Yes             |

## Reporting a Vulnerability

We take the security of Ghost Ops seriously. If you discover a security
vulnerability, please report it responsibly.

### How to Report

1. **Preferred**: Use [GitHub Security Advisories](https://github.com/DUBSOpenHub/ghost-ops/security/advisories/new)
   to report vulnerabilities privately.
2. **Alternative**: Email **security@dubsopenhub.com** with details of the
   vulnerability.

**Please do NOT open a public issue for security vulnerabilities.**

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### In Scope

- Daemon privilege escalation
- Agent file manipulation safety (backup bypass, unauthorized writes)
- SQLite injection via crafted config or state data
- `gh` CLI command injection through mission parameters
- LLM prompt injection affecting mission behavior

### Out of Scope

- Vulnerabilities in the GitHub Models API
- Vulnerabilities in GitHub Copilot CLI
- Vulnerabilities in underlying LLMs (GPT, Claude, Gemini, etc.)
- Issues requiring physical access to the machine

### Response Timeline

| Action          | Timeframe |
| --------------- | --------- |
| Acknowledgment  | 24 hours  |
| Assessment      | 72 hours  |
| Fix (if needed) | Best effort, depends on severity |

## Security Best Practices for Users

- Keep `ghost_ops.toml` permissions restricted (`chmod 600`).
- Review dry-run output before enabling live mode.
- Regularly update to the latest supported version.
