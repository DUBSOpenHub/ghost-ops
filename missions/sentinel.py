"""Sentinel — proactive health check across all repos and agents.

Catches failure classes that portfolio_watchdog doesn't:
  1. GitHub Actions workflow failures (consecutive, stale crons, issue spam)
  2. Daemon liveness (mission gaps, fail_count spikes)
  3. Data freshness (tracked files going stale)
  4. Config sanity (toml parsing, Python path, token tracking, venv health)
  5. File sync / drift (paired_files hash comparison, symlink integrity)
  6. Workflow-to-secret alignment (referenced secrets exist)
  7. gh CLI auth pre-flight (catches dead tokens before any API calls)
  8. DB pruning (auto-cleanup of old runs and alerts)
  9. Agent quality gate (flags xray score regressions after fleet_evolution)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Rate-limit: max concurrent gh api calls
_API_SEMAPHORE = asyncio.Semaphore(3)
_HEARTBEAT_PATH = Path(os.path.expanduser("~/.ghost-ops-heartbeat"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _gh_api(path: str) -> dict[str, Any] | list[Any] | None:
    """Run `gh api <path>` with rate-limit throttling. Returns parsed JSON or None."""
    async with _API_SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            "gh", "api", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            # Detect rate limiting
            if "rate limit" in err.lower() or "403" in err:
                logger.warning("gh api %s rate limited, backing off", path)
                await asyncio.sleep(30)
                return None
            logger.debug("gh api %s failed: %s", path, err)
            return None
        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError:
            return None


async def _gh_cli(*args: str) -> tuple[int, str, str]:
    """Run an arbitrary gh CLI command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def _shell(cmd: str) -> tuple[int, str]:
    """Run a shell command, return (returncode, stdout)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode()


@dataclass
class Finding:
    severity: str  # CRITICAL, WARN, INFO
    check: str
    repo: str | None
    detail: str


# ---------------------------------------------------------------------------
# Check 1: GitHub Actions Health
# ---------------------------------------------------------------------------

async def _check_actions_health(
    repos: list[str], dry_run: bool, thresholds: dict[str, Any]
) -> list[Finding]:
    """Flag repos with consecutive workflow failures, stale crons, or issue spam."""
    findings: list[Finding] = []
    consecutive_threshold = thresholds.get("consecutive_failures", 3)
    stale_hours = thresholds.get("stale_cron_hours", 48)
    spam_threshold = thresholds.get("issue_spam_count", 5)

    if dry_run:
        return [Finding("INFO", "actions_health", None, f"Dry run: would check {len(repos)} repos")]

    for repo in repos:
        # Get workflows
        workflows_data = await _gh_api(f"repos/{repo}/actions/workflows?per_page=30")
        if not isinstance(workflows_data, dict):
            continue
        workflows = workflows_data.get("workflows", [])

        for wf in workflows:
            wf_id = wf.get("id")
            wf_name = wf.get("name", "unknown")
            wf_path = wf.get("path", "")

            # Get last N runs for this workflow
            runs_data = await _gh_api(
                f"repos/{repo}/actions/workflows/{wf_id}/runs"
                f"?per_page={consecutive_threshold + 2}&status=completed"
            )
            if not isinstance(runs_data, dict):
                continue
            runs = runs_data.get("workflow_runs", [])

            if not runs:
                continue

            # Check consecutive failures
            consecutive_fails = 0
            for r in runs:
                if r.get("conclusion") == "failure":
                    consecutive_fails += 1
                else:
                    break

            if consecutive_fails >= consecutive_threshold:
                findings.append(Finding(
                    "CRITICAL", "actions_health", repo,
                    f"Workflow '{wf_name}' has {consecutive_fails} consecutive failures",
                ))

            # Check stale cron (has schedule trigger but hasn't run recently)
            # Read workflow file to detect cron schedule
            wf_file = await _gh_api(f"repos/{repo}/contents/{wf_path}")
            has_schedule = False
            if isinstance(wf_file, dict) and wf_file.get("content"):
                try:
                    content = base64.b64decode(wf_file["content"]).decode()
                    has_schedule = "schedule:" in content or "cron:" in content
                except Exception:
                    pass

            if has_schedule and runs:
                last_run_str = runs[0].get("created_at", "")
                try:
                    last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
                    hours_ago = (datetime.now(tz=timezone.utc) - last_run).total_seconds() / 3600
                    if hours_ago > stale_hours:
                        findings.append(Finding(
                            "WARN", "actions_health", repo,
                            f"Scheduled workflow '{wf_name}' last ran {hours_ago:.0f}h ago (>{stale_hours}h)",
                        ))
                except (ValueError, TypeError):
                    pass

        # Check issue spam: auto-created issues in last 24h
        issues = await _gh_api(
            f"repos/{repo}/issues?state=open&per_page=100"
            f"&since={(datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0)).isoformat()}"
        )
        if isinstance(issues, list):
            bot_issues = [
                i for i in issues
                if i.get("user", {}).get("type") == "Bot"
                or "[bot]" in i.get("user", {}).get("login", "")
                or "workflow" in i.get("title", "").lower()
                or "failed" in i.get("title", "").lower()
            ]
            if len(bot_issues) >= spam_threshold:
                findings.append(Finding(
                    "WARN", "actions_health", repo,
                    f"{len(bot_issues)} auto-created issues today (possible failure spam)",
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 2: Daemon Liveness
# ---------------------------------------------------------------------------

async def _check_daemon_liveness(
    config: dict[str, Any], dry_run: bool, thresholds: dict[str, Any]
) -> list[Finding]:
    """Check that the ghost-ops daemon missions are running on schedule."""
    findings: list[Finding] = []
    gap_multiplier = thresholds.get("gap_multiplier", 2.5)

    if dry_run:
        return [Finding("INFO", "daemon_liveness", None, "Dry run: would check daemon liveness")]

    db_path = os.path.expanduser(
        config.get("ghost_ops", {}).get("db_path", "ghost_ops.db")
    )
    if not Path(db_path).exists():
        findings.append(Finding(
            "CRITICAL", "daemon_liveness", None,
            f"Ghost-ops database not found: {db_path}",
        ))
        return findings

    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    missions_cfg = config.get("missions", {})
    for mission_id, mcfg in missions_cfg.items():
        if mission_id == "sentinel":
            continue  # don't check ourselves
        if not mcfg.get("enabled", True):
            continue

        schedule = mcfg.get("schedule", "")
        # Calculate expected interval from cron
        expected_hours = _cron_interval_hours(schedule)
        max_gap_hours = expected_hours * gap_multiplier

        # Last successful run
        row = conn.execute(
            "SELECT finished_at FROM runs WHERE mission_id=? AND status='success' "
            "ORDER BY finished_at DESC LIMIT 1",
            (mission_id,),
        ).fetchone()

        if row and row["finished_at"]:
            last_success = datetime.fromisoformat(row["finished_at"]).replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(tz=timezone.utc) - last_success).total_seconds() / 3600
            if hours_since > max_gap_hours:
                findings.append(Finding(
                    "CRITICAL", "daemon_liveness", None,
                    f"Mission '{mission_id}' last succeeded {hours_since:.1f}h ago "
                    f"(expected every {expected_hours:.1f}h, threshold {max_gap_hours:.1f}h)",
                ))
        else:
            findings.append(Finding(
                "WARN", "daemon_liveness", None,
                f"Mission '{mission_id}' has never completed successfully",
            ))

        # Check for recent failures
        fail_rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM runs WHERE mission_id=? AND status='failed' "
            "AND started_at > datetime('now', '-24 hours')",
            (mission_id,),
        ).fetchone()
        if fail_rows and fail_rows["cnt"] > 0:
            findings.append(Finding(
                "WARN", "daemon_liveness", None,
                f"Mission '{mission_id}' has {fail_rows['cnt']} failures in the last 24h",
            ))

    # Check launchd service is loaded
    rc, stdout = await _shell("launchctl list 2>/dev/null | grep ghost-ops || true")
    if "ghost-ops" not in stdout:
        findings.append(Finding(
            "CRITICAL", "daemon_liveness", None,
            "launchd service 'ghost-ops' is not loaded",
        ))

    conn.close()
    return findings


def _cron_interval_hours(expr: str) -> float:
    """Estimate the interval between runs from a cron expression."""
    try:
        fields = expr.strip().split()
        if len(fields) != 5:
            return 24.0

        minute_f, hour_f = fields[0], fields[1]

        # "0 */3 * * *" → every 3 hours
        if "/" in hour_f:
            step = int(hour_f.split("/")[1])
            return float(step)

        # "0 * * * *" → every hour
        if hour_f == "*" and minute_f != "*":
            return 1.0

        # "0 9,21 * * *" → 2x/day → 12h
        if "," in hour_f:
            hours = [int(h) for h in hour_f.split(",")]
            if len(hours) >= 2:
                return 24.0 / len(hours)

        # Single specific hour → once/day
        return 24.0
    except Exception:
        return 24.0


# ---------------------------------------------------------------------------
# Check 3: Data Freshness
# ---------------------------------------------------------------------------

async def _check_data_freshness(
    entries: list[dict[str, Any]], dry_run: bool
) -> list[Finding]:
    """Check that tracked data files haven't gone stale."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "data_freshness", None, f"Dry run: would check {len(entries)} files")]

    for entry in entries:
        repo = entry.get("repo", "")
        file_path = entry.get("path", "")
        max_age_hours = entry.get("max_age_hours", 48)

        if not repo or not file_path:
            continue

        # Get last commit for this file
        commits = await _gh_api(
            f"repos/{repo}/commits?path={file_path}&per_page=1"
        )
        if not isinstance(commits, list) or not commits:
            findings.append(Finding(
                "WARN", "data_freshness", repo,
                f"Could not fetch commit history for {file_path}",
            ))
            continue

        try:
            commit_date_str = commits[0]["commit"]["committer"]["date"]
            commit_date = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))
            hours_ago = (datetime.now(tz=timezone.utc) - commit_date).total_seconds() / 3600
            if hours_ago > max_age_hours:
                findings.append(Finding(
                    "WARN", "data_freshness", repo,
                    f"{file_path} last updated {hours_ago:.0f}h ago (>{max_age_hours}h)",
                ))
        except (KeyError, ValueError) as e:
            findings.append(Finding(
                "WARN", "data_freshness", repo,
                f"Could not parse commit date for {file_path}: {e}",
            ))

    return findings


# ---------------------------------------------------------------------------
# Check 4: Config Sanity
# ---------------------------------------------------------------------------

async def _check_config_sanity(
    config: dict[str, Any], config_path: str, dry_run: bool
) -> list[Finding]:
    """Verify ghost_ops.toml, Python path, and token tracking integrity."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "config_sanity", None, "Dry run: would check config sanity")]

    # 4a. Verify toml parses (it already did if we got here, but check for empty sections)
    if not config.get("missions"):
        findings.append(Finding("WARN", "config_sanity", None, "No missions defined in config"))

    # 4b. Check plist Python path
    plist_paths = list(Path(os.path.expanduser("~/Library/LaunchAgents")).glob("*ghost*ops*"))
    for plist_path in plist_paths:
        try:
            content = plist_path.read_text()
            python_match = re.search(r"<string>(/[^<]*python[^<]*)</string>", content)
            if python_match:
                python_path = python_match.group(1)
                if not Path(python_path).exists():
                    findings.append(Finding(
                        "CRITICAL", "config_sanity", None,
                        f"Plist Python path does not exist: {python_path}",
                    ))
                else:
                    # Check for tomllib
                    rc, _ = await _shell(f"{python_path} -c 'import tomllib' 2>&1")
                    if rc != 0:
                        findings.append(Finding(
                            "CRITICAL", "config_sanity", None,
                            f"Plist Python ({python_path}) lacks tomllib module",
                        ))
        except Exception as e:
            findings.append(Finding(
                "WARN", "config_sanity", None,
                f"Could not read plist {plist_path}: {e}",
            ))

    # 4c. Token tracking integrity: LLM missions should have non-zero tokens
    db_path = os.path.expanduser(
        config.get("ghost_ops", {}).get("db_path", "ghost_ops.db")
    )
    if Path(db_path).exists():
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # fleet_evolution uses LLM — check last 5 runs have non-zero tokens
        rows = conn.execute(
            "SELECT tokens_in, tokens_out FROM runs "
            "WHERE mission_id='fleet_evolution' AND status='success' "
            "ORDER BY finished_at DESC LIMIT 5"
        ).fetchall()

        if len(rows) >= 3:
            zero_token_runs = sum(1 for r in rows if r["tokens_in"] == 0 and r["tokens_out"] == 0)
            if zero_token_runs == len(rows):
                findings.append(Finding(
                    "WARN", "config_sanity", None,
                    f"All {len(rows)} recent fleet_evolution runs show 0 tokens — tracking may be broken",
                ))

        conn.close()

    return findings


# ---------------------------------------------------------------------------
# Check 5: File Sync / Drift Detection
# ---------------------------------------------------------------------------

async def _check_file_drift(
    config: dict[str, Any], dry_run: bool
) -> list[Finding]:
    """Check paired_files registry for hash mismatches and broken symlinks."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "file_drift", None, "Dry run: would check file drift")]

    paired = (
        config.get("missions", {})
        .get("fleet_evolution", {})
        .get("paired_files", {})
    )
    if not paired:
        return findings

    agents_dir = os.path.expanduser(
        config.get("ghost_ops", {}).get("agents_dir", "~/.copilot/agents")
    )

    for agent_file, counterparts in paired.items():
        source_path = Path(agents_dir) / agent_file
        if not source_path.exists():
            findings.append(Finding(
                "WARN", "file_drift", None,
                f"Agent file not found: {source_path}",
            ))
            continue

        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()[:16]

        for cp in counterparts:
            cp_path = Path(os.path.expanduser(cp))

            # Check symlink integrity
            if cp_path.is_symlink():
                target = cp_path.resolve()
                if not target.exists():
                    findings.append(Finding(
                        "CRITICAL", "file_drift", None,
                        f"Broken symlink: {cp_path} → {target}",
                    ))
                    continue

            if not cp_path.exists():
                findings.append(Finding(
                    "WARN", "file_drift", None,
                    f"Paired file not found: {cp_path}",
                ))
                continue

            cp_hash = hashlib.sha256(cp_path.read_bytes()).hexdigest()[:16]
            if source_hash != cp_hash:
                findings.append(Finding(
                    "WARN", "file_drift", None,
                    f"Drift detected: {agent_file} ({source_hash}) != {cp} ({cp_hash})",
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 6: Workflow-to-Secret Alignment
# ---------------------------------------------------------------------------

async def _check_secret_alignment(
    repos: list[str], dry_run: bool
) -> list[Finding]:
    """Check that workflow-referenced secrets actually exist in the repo."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "secret_alignment", None, f"Dry run: would check {len(repos)} repos")]

    for repo in repos:
        # Get list of secrets for this repo
        rc, stdout, _ = await _gh_cli("secret", "list", "-R", repo, "--json", "name")
        if rc != 0:
            continue  # might not have permission, skip
        try:
            secrets_list = json.loads(stdout)
            secret_names = {s["name"] for s in secrets_list}
        except (json.JSONDecodeError, KeyError):
            continue

        # Get workflows and scan for secret references
        workflows_data = await _gh_api(f"repos/{repo}/actions/workflows?per_page=30")
        if not isinstance(workflows_data, dict):
            continue

        for wf in workflows_data.get("workflows", []):
            wf_path = wf.get("path", "")
            wf_name = wf.get("name", "unknown")
            if not wf_path:
                continue

            wf_file = await _gh_api(f"repos/{repo}/contents/{wf_path}")
            if not isinstance(wf_file, dict) or not wf_file.get("content"):
                continue

            try:
                content = base64.b64decode(wf_file["content"]).decode()
            except Exception:
                continue

            # Find all ${{ secrets.X }} references
            referenced = set(re.findall(r"\$\{\{\s*secrets\.(\w+)\s*\}\}", content))

            # Check for fallback patterns like: ${{ secrets.X || 'default' }}
            # These are safe — only flag secrets without defaults
            safe_refs = set(re.findall(
                r"\$\{\{\s*secrets\.(\w+)\s*\|\|", content
            ))
            unprotected = referenced - safe_refs - secret_names

            # Filter out GITHUB_TOKEN (always available)
            unprotected.discard("GITHUB_TOKEN")

            if unprotected:
                findings.append(Finding(
                    "WARN", "secret_alignment", repo,
                    f"Workflow '{wf_name}' references unset secrets without defaults: "
                    f"{', '.join(sorted(unprotected))}",
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 7: gh CLI Auth Pre-flight
# ---------------------------------------------------------------------------

async def _check_gh_auth(dry_run: bool) -> list[Finding]:
    """Verify gh CLI is authenticated before any API calls."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "gh_auth", None, "Dry run: would check gh auth")]

    rc, stdout, stderr = await _gh_cli("auth", "status", "--hostname", "github.com")
    if rc != 0:
        findings.append(Finding(
            "CRITICAL", "gh_auth", None,
            f"gh CLI not authenticated — all API-dependent missions will fail. "
            f"Run 'gh auth login' to fix. Error: {stderr.strip()[:200]}",
        ))
    else:
        # Check token scopes include repo
        if "repo" not in stdout.lower() and "repo" not in stderr.lower():
            findings.append(Finding(
                "WARN", "gh_auth", None,
                "gh CLI token may lack 'repo' scope — some API calls could fail",
            ))

    return findings


# ---------------------------------------------------------------------------
# Check 8: DB Pruning (auto-cleanup)
# ---------------------------------------------------------------------------

async def _prune_db(
    config: dict[str, Any], dry_run: bool
) -> list[Finding]:
    """Prune old runs and acknowledged alerts to prevent unbounded DB growth."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "db_pruning", None, "Dry run: would prune old DB rows")]

    db_path = os.path.expanduser(
        config.get("ghost_ops", {}).get("db_path", "ghost_ops.db")
    )
    if not Path(db_path).exists():
        return findings

    conn = sqlite3.connect(db_path)

    # Prune runs older than 90 days
    cur = conn.execute(
        "DELETE FROM runs WHERE finished_at < datetime('now', '-90 days')"
    )
    pruned_runs = cur.rowcount

    # Prune acknowledged alerts older than 30 days
    cur = conn.execute(
        "DELETE FROM alerts WHERE acknowledged = 1 AND created_at < datetime('now', '-30 days')"
    )
    pruned_alerts = cur.rowcount

    # Prune unacknowledged alerts older than 90 days (stale noise)
    cur = conn.execute(
        "DELETE FROM alerts WHERE created_at < datetime('now', '-90 days')"
    )
    pruned_stale_alerts = cur.rowcount

    conn.commit()

    # Report DB size
    db_size_mb = Path(db_path).stat().st_size / (1024 * 1024)
    if db_size_mb > 50:
        findings.append(Finding(
            "WARN", "db_pruning", None,
            f"ghost_ops.db is {db_size_mb:.1f} MB — consider VACUUM",
        ))

    total_pruned = pruned_runs + pruned_alerts + pruned_stale_alerts
    if total_pruned > 0:
        findings.append(Finding(
            "INFO", "db_pruning", None,
            f"Pruned {pruned_runs} old runs, {pruned_alerts + pruned_stale_alerts} old alerts",
        ))

    conn.close()
    return findings


# ---------------------------------------------------------------------------
# Check 9: Agent Quality Gate
# ---------------------------------------------------------------------------

async def _check_agent_quality(
    config: dict[str, Any], dry_run: bool
) -> list[Finding]:
    """Check if any agent xray scores have dropped below threshold."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "agent_quality", None, "Dry run: would check agent quality")]

    # Run agent-xray if available
    xray_path = Path(os.path.expanduser("~/ghost-ops/agent-xray.js"))
    if not xray_path.exists():
        # Try alternate location
        xray_path = Path(os.path.expanduser("~/dev/agent-xray/agent-xray.js"))
    if not xray_path.exists():
        return findings  # xray not available, skip silently

    agents_dir = os.path.expanduser(
        config.get("ghost_ops", {}).get("agents_dir", "~/.copilot/agents")
    )
    if not Path(agents_dir).exists():
        return findings

    # Scan each agent file
    quality_threshold = 40
    agent_files = list(Path(agents_dir).glob("*.agent.md"))

    for agent_file in agent_files:
        rc, stdout = await _shell(f"node {xray_path} {agent_file} --json 2>/dev/null")
        if rc != 0:
            continue
        try:
            result = json.loads(stdout)
            score = result.get("composite", result.get("score", 100))
            if score < quality_threshold:
                findings.append(Finding(
                    "WARN", "agent_quality", None,
                    f"Agent '{agent_file.name}' xray score is {score} (below {quality_threshold})",
                ))
        except (json.JSONDecodeError, KeyError):
            continue

    return findings


# ---------------------------------------------------------------------------
# Check 10: Python venv health (oss-radar)
# ---------------------------------------------------------------------------

async def _check_venv_health(
    config: dict[str, Any], dry_run: bool
) -> list[Finding]:
    """Verify oss-radar venv is intact and can import critical packages."""
    findings: list[Finding] = []

    if dry_run:
        return [Finding("INFO", "venv_health", None, "Dry run: would check venv health")]

    venv_paths = config.get("missions", {}).get("sentinel", {}).get("venvs", [])
    # Default: check oss-radar venv
    if not venv_paths:
        venv_paths = [{"path": "~/ghost-ops/oss-radar/.venv", "imports": ["pydantic", "typer"]}]

    for entry in venv_paths:
        venv_dir = Path(os.path.expanduser(entry.get("path", "")))
        imports = entry.get("imports", [])

        python = venv_dir / "bin" / "python"
        if not python.exists():
            findings.append(Finding(
                "WARN", "venv_health", None,
                f"Venv Python not found: {python}",
            ))
            continue

        for module in imports:
            rc, _ = await _shell(f"{python} -c 'import {module}' 2>&1")
            if rc != 0:
                findings.append(Finding(
                    "CRITICAL", "venv_health", None,
                    f"Venv {venv_dir.name} cannot import '{module}' — likely broken by Python upgrade",
                ))

    return findings

# ---------------------------------------------------------------------------
# Mission entry point
# ---------------------------------------------------------------------------

async def run(ctx: Any) -> dict[str, Any]:
    """Sentinel mission — proactive health check across all repos and agents."""
    log = ctx.logger
    store = ctx.store
    full_config = ctx.config
    sentinel_cfg = full_config.get("missions", {}).get("sentinel", {})

    repos: list[str] = sentinel_cfg.get("repos", [])
    thresholds: dict[str, Any] = sentinel_cfg.get("thresholds", {})
    data_freshness_entries: list[dict[str, Any]] = sentinel_cfg.get("data_freshness", [])

    config_path = os.path.expanduser(
        full_config.get("ghost_ops", {}).get("_config_path", "~/ghost-ops/ghost_ops.toml")
    )

    log.info("[sentinel] Starting health check across %d repos", len(repos))

    # Pre-flight: check gh auth first — if dead, skip API-dependent checks
    auth_findings = await _check_gh_auth(ctx.dry_run)
    auth_dead = any(f.severity == "CRITICAL" for f in auth_findings)

    if auth_dead and not ctx.dry_run:
        log.warning("[sentinel] gh auth is dead — skipping API-dependent checks")
        # Only run local checks that don't need gh
        results = await asyncio.gather(
            _check_daemon_liveness(full_config, ctx.dry_run, thresholds),
            _check_config_sanity(full_config, config_path, ctx.dry_run),
            _check_file_drift(full_config, ctx.dry_run),
            _prune_db(full_config, ctx.dry_run),
            _check_agent_quality(full_config, ctx.dry_run),
            _check_venv_health(full_config, ctx.dry_run),
            return_exceptions=True,
        )
        check_names = [
            "daemon_liveness", "config_sanity", "file_drift",
            "db_pruning", "agent_quality", "venv_health",
        ]
    else:
        # Full check suite
        results = await asyncio.gather(
            _check_actions_health(repos, ctx.dry_run, thresholds),
            _check_daemon_liveness(full_config, ctx.dry_run, thresholds),
            _check_data_freshness(data_freshness_entries, ctx.dry_run),
            _check_config_sanity(full_config, config_path, ctx.dry_run),
            _check_file_drift(full_config, ctx.dry_run),
            _check_secret_alignment(repos, ctx.dry_run),
            _prune_db(full_config, ctx.dry_run),
            _check_agent_quality(full_config, ctx.dry_run),
            _check_venv_health(full_config, ctx.dry_run),
            return_exceptions=True,
        )
        check_names = [
            "actions_health", "daemon_liveness", "data_freshness",
            "config_sanity", "file_drift", "secret_alignment",
            "db_pruning", "agent_quality", "venv_health",
        ]

    all_findings: list[Finding] = list(auth_findings)
    for name, result in zip(check_names, results):
        if isinstance(result, BaseException):
            log.warning("[sentinel] Check '%s' raised: %s", name, result)
            all_findings.append(Finding(
                "WARN", name, None, f"Check failed with error: {result}",
            ))
        else:
            all_findings.extend(result)

    # Write findings to alerts table
    for f in all_findings:
        if f.severity in ("CRITICAL", "WARN"):
            await store.write_alert(
                severity=f.severity,
                source=f"sentinel/{f.check}",
                title=f.detail,
                repo=f.repo,
            )

    critical = sum(1 for f in all_findings if f.severity == "CRITICAL")
    warnings = sum(1 for f in all_findings if f.severity == "WARN")
    info = sum(1 for f in all_findings if f.severity == "INFO")

    # Write heartbeat file so external watchdog can verify sentinel is alive
    if not ctx.dry_run:
        try:
            _HEARTBEAT_PATH.write_text(datetime.now(tz=timezone.utc).isoformat())
        except OSError:
            pass

    total_checks = len(check_names) + 1  # +1 for gh_auth

    summary = {
        "checks_run": total_checks,
        "total_findings": len(all_findings),
        "critical": critical,
        "warnings": warnings,
        "info": info,
        "findings": [
            {"severity": f.severity, "check": f.check, "repo": f.repo, "detail": f.detail}
            for f in all_findings
        ],
    }

    if critical > 0:
        log.warning("[sentinel] ⚠️ %d CRITICAL, %d WARN, %d INFO findings", critical, warnings, info)
        # Create a GitHub issue digest so the user gets a notification
        notify_repo = sentinel_cfg.get("notify_repo", "DUBSOpenHub/ghost-ops")
        if not ctx.dry_run:
            await _create_digest_issue(notify_repo, all_findings, log)
    else:
        log.info("[sentinel] ✅ %d WARN, %d INFO findings (0 critical)", warnings, info)

    return summary


# ---------------------------------------------------------------------------
# Notification: deduped GitHub issue digest
# ---------------------------------------------------------------------------

async def _create_digest_issue(
    repo: str, findings: list[Finding], log: logging.Logger
) -> None:
    """Create a single daily digest issue on the notify repo (deduped by date)."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    title = f"🚨 Sentinel Health Check — {today}"

    # Check if today's issue already exists
    rc, stdout, _ = await _gh_cli(
        "issue", "list", "-R", repo, "--search", f'"{title}" in:title',
        "--state", "open", "--json", "number", "--limit", "1",
    )
    if rc == 0:
        try:
            existing = json.loads(stdout)
            if existing:
                log.info("[sentinel] Digest issue already exists (#%d), skipping", existing[0]["number"])
                return
        except (json.JSONDecodeError, KeyError):
            pass

    # Build issue body
    critical = [f for f in findings if f.severity == "CRITICAL"]
    warns = [f for f in findings if f.severity == "WARN"]

    body_lines = [
        "## Sentinel found issues that need attention\n",
        f"**{len(critical)} critical, {len(warns)} warnings** across {len(set(f.repo for f in findings if f.repo))} repos\n",
    ]

    if critical:
        body_lines.append("### 🔴 Critical\n")
        for f in critical:
            repo_tag = f" `{f.repo}`" if f.repo else ""
            body_lines.append(f"- **[{f.check}]**{repo_tag} {f.detail}")

    if warns:
        body_lines.append("\n### ⚠️ Warnings\n")
        for f in warns:
            repo_tag = f" `{f.repo}`" if f.repo else ""
            body_lines.append(f"- **[{f.check}]**{repo_tag} {f.detail}")

    body_lines.append("\n---\n*Auto-generated by Ghost Ops sentinel mission*")
    body = "\n".join(body_lines)

    rc, stdout, stderr = await _gh_cli(
        "issue", "create", "-R", repo,
        "--title", title,
        "--body", body,
        "--label", "sentinel",
    )
    if rc == 0:
        log.info("[sentinel] Created digest issue: %s", stdout.strip())
    else:
        # Label might not exist yet — create it and retry
        if "label" in stderr.lower():
            await _gh_cli("label", "create", "-R", repo, "sentinel",
                          "--color", "d93f0b", "--description", "Auto-generated sentinel health check alerts")
            rc2, stdout2, _ = await _gh_cli(
                "issue", "create", "-R", repo,
                "--title", title, "--body", body, "--label", "sentinel",
            )
            if rc2 == 0:
                log.info("[sentinel] Created digest issue (after label creation): %s", stdout2.strip())
            else:
                log.warning("[sentinel] Failed to create digest issue after retry")
        else:
            log.warning("[sentinel] Failed to create digest issue: %s", stderr.strip())
