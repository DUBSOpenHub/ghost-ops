"""Unit tests for sentinel — no network, stdlib only."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from missions.sentinel import (
    Finding,
    _check_actions_health,
    _check_agent_quality,
    _check_config_sanity,
    _check_daemon_liveness,
    _check_data_freshness,
    _check_file_drift,
    _check_gh_auth,
    _check_secret_alignment,
    _check_venv_health,
    _cron_interval_hours,
    _prune_db,
    run,
)


def arun(coro):
    """Helper: run a coroutine."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _cron_interval_hours
# ---------------------------------------------------------------------------

class TestCronInterval(unittest.TestCase):
    def test_every_hour(self):
        self.assertAlmostEqual(_cron_interval_hours("0 * * * *"), 1.0)

    def test_every_3_hours(self):
        self.assertAlmostEqual(_cron_interval_hours("0 */3 * * *"), 3.0)

    def test_twice_daily(self):
        self.assertAlmostEqual(_cron_interval_hours("0 9,21 * * *"), 12.0)

    def test_three_times_daily(self):
        self.assertAlmostEqual(_cron_interval_hours("0 8,14,20 * * *"), 8.0)

    def test_once_daily(self):
        self.assertAlmostEqual(_cron_interval_hours("30 6 * * *"), 24.0)

    def test_invalid(self):
        self.assertAlmostEqual(_cron_interval_hours("invalid"), 24.0)

    def test_empty(self):
        self.assertAlmostEqual(_cron_interval_hours(""), 24.0)


# ---------------------------------------------------------------------------
# Actions Health (dry run)
# ---------------------------------------------------------------------------

class TestActionsHealthDryRun(unittest.TestCase):
    def test_dry_run_returns_info(self):
        findings = arun(_check_actions_health(
            ["owner/repo1", "owner/repo2"], dry_run=True, thresholds={}
        ))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")
        self.assertIn("2 repos", findings[0].detail)


# ---------------------------------------------------------------------------
# Actions Health (mocked API)
# ---------------------------------------------------------------------------

class TestActionsHealthLive(unittest.TestCase):
    def _make_workflow(self, wf_id=1, name="CI", path=".github/workflows/ci.yml"):
        return {"id": wf_id, "name": name, "path": path}

    def _make_runs(self, conclusions):
        return {"workflow_runs": [
            {"conclusion": c, "created_at": datetime.now(tz=timezone.utc).isoformat()}
            for c in conclusions
        ]}

    @patch("missions.sentinel._gh_api")
    def test_consecutive_failures_flagged(self, mock_api):
        wf = self._make_workflow()
        runs = self._make_runs(["failure", "failure", "failure", "success"])

        async def side_effect(path):
            if "workflows?" in path:
                return {"workflows": [wf]}
            if "/runs?" in path:
                return runs
            if "contents/" in path:
                return {"content": ""}  # no schedule
            if "issues?" in path:
                return []
            return None

        mock_api.side_effect = side_effect
        findings = arun(_check_actions_health(["owner/repo"], False, {"consecutive_failures": 3}))
        critical = [f for f in findings if f.severity == "CRITICAL"]
        self.assertGreaterEqual(len(critical), 1)
        self.assertIn("3 consecutive failures", critical[0].detail)

    @patch("missions.sentinel._gh_api")
    def test_no_failures_clean(self, mock_api):
        wf = self._make_workflow()
        runs = self._make_runs(["success", "success", "success"])

        async def side_effect(path):
            if "workflows?" in path:
                return {"workflows": [wf]}
            if "/runs?" in path:
                return runs
            if "contents/" in path:
                return {"content": ""}
            if "issues?" in path:
                return []
            return None

        mock_api.side_effect = side_effect
        findings = arun(_check_actions_health(["owner/repo"], False, {"consecutive_failures": 3}))
        critical = [f for f in findings if f.severity == "CRITICAL"]
        self.assertEqual(len(critical), 0)


# ---------------------------------------------------------------------------
# Daemon Liveness
# ---------------------------------------------------------------------------

class TestDaemonLiveness(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_check_daemon_liveness({}, True, {}))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    @patch("missions.sentinel._shell")
    def test_missing_db_is_critical(self, mock_shell):
        mock_shell.return_value = (0, "ghost-ops")  # launchd ok
        config = {"ghost_ops": {"db_path": "/nonexistent/path/ghost_ops.db"}, "missions": {}}
        findings = arun(_check_daemon_liveness(config, False, {}))
        critical = [f for f in findings if f.severity == "CRITICAL"]
        self.assertGreaterEqual(len(critical), 1)
        self.assertIn("not found", critical[0].detail)

    @patch("missions.sentinel._shell")
    def test_stale_mission_flagged(self, mock_shell):
        mock_shell.return_value = (0, "ghost-ops")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        try:
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY, mission_id TEXT, started_at DATETIME,
                    finished_at DATETIME, status TEXT
                );
            """)
            # Insert a success from 10 hours ago
            old_time = (datetime.now(tz=timezone.utc) - timedelta(hours=10)).isoformat()
            conn.execute(
                "INSERT INTO runs (mission_id, status, finished_at) VALUES (?, 'success', ?)",
                ("inbox_autopilot", old_time)
            )
            conn.commit()
            conn.close()

            config = {
                "ghost_ops": {"db_path": db_path},
                "missions": {
                    "inbox_autopilot": {"enabled": True, "schedule": "0 * * * *"},
                },
            }
            findings = arun(_check_daemon_liveness(config, False, {"gap_multiplier": 2.5}))
            critical = [f for f in findings if f.severity == "CRITICAL"]
            self.assertGreaterEqual(len(critical), 1)
            self.assertIn("inbox_autopilot", critical[0].detail)
        finally:
            os.unlink(db_path)

    @patch("missions.sentinel._shell")
    def test_launchd_not_loaded(self, mock_shell):
        mock_shell.return_value = (0, "")  # empty = not loaded
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        try:
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY, mission_id TEXT, started_at DATETIME,
                    finished_at DATETIME, status TEXT
                );
            """)
            conn.commit()
            conn.close()
            config = {"ghost_ops": {"db_path": db_path}, "missions": {}}
            findings = arun(_check_daemon_liveness(config, False, {}))
            critical = [f for f in findings if f.severity == "CRITICAL"]
            self.assertGreaterEqual(len(critical), 1)
            self.assertIn("not loaded", critical[0].detail)
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# Data Freshness
# ---------------------------------------------------------------------------

class TestDataFreshness(unittest.TestCase):
    def test_dry_run(self):
        entries = [{"repo": "o/r", "path": "f.json", "max_age_hours": 48}]
        findings = arun(_check_data_freshness(entries, True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    @patch("missions.sentinel._gh_api")
    def test_stale_file_flagged(self, mock_api):
        old_date = (datetime.now(tz=timezone.utc) - timedelta(hours=100)).isoformat()

        async def side_effect(path):
            return [{"commit": {"committer": {"date": old_date}}}]

        mock_api.side_effect = side_effect
        entries = [{"repo": "o/r", "path": "data.json", "max_age_hours": 48}]
        findings = arun(_check_data_freshness(entries, False))
        self.assertGreaterEqual(len(findings), 1)
        self.assertIn("100", findings[0].detail)

    @patch("missions.sentinel._gh_api")
    def test_fresh_file_clean(self, mock_api):
        fresh_date = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()

        async def side_effect(path):
            return [{"commit": {"committer": {"date": fresh_date}}}]

        mock_api.side_effect = side_effect
        entries = [{"repo": "o/r", "path": "data.json", "max_age_hours": 48}]
        findings = arun(_check_data_freshness(entries, False))
        self.assertEqual(len(findings), 0)


# ---------------------------------------------------------------------------
# Config Sanity
# ---------------------------------------------------------------------------

class TestConfigSanity(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_check_config_sanity({}, "", True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    def test_no_missions_warns(self):
        findings = arun(_check_config_sanity({"ghost_ops": {"db_path": "/fake"}}, "", False))
        warns = [f for f in findings if f.severity == "WARN" and "No missions" in f.detail]
        self.assertGreaterEqual(len(warns), 1)

    def test_token_tracking_broken(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        try:
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY, mission_id TEXT, status TEXT,
                    finished_at DATETIME, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0
                );
            """)
            for _ in range(5):
                conn.execute(
                    "INSERT INTO runs (mission_id, status, finished_at, tokens_in, tokens_out) "
                    "VALUES ('fleet_evolution', 'success', datetime('now'), 0, 0)"
                )
            conn.commit()
            conn.close()

            config = {"ghost_ops": {"db_path": db_path}, "missions": {"fleet_evolution": {}}}
            findings = arun(_check_config_sanity(config, "", False))
            warns = [f for f in findings if "token" in f.detail.lower()]
            self.assertGreaterEqual(len(warns), 1)
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# File Drift
# ---------------------------------------------------------------------------

class TestFileDrift(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_check_file_drift({}, True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    def test_matching_files_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir) / "agents"
            agents_dir.mkdir()
            paired_dir = Path(tmpdir) / "paired"
            paired_dir.mkdir()

            (agents_dir / "test.agent.md").write_text("same content")
            (paired_dir / "SKILL.md").write_text("same content")

            config = {
                "ghost_ops": {"agents_dir": str(agents_dir)},
                "missions": {
                    "fleet_evolution": {
                        "paired_files": {
                            "test.agent.md": [str(paired_dir / "SKILL.md")]
                        }
                    }
                },
            }
            findings = arun(_check_file_drift(config, False))
            self.assertEqual(len(findings), 0)

    def test_drifted_files_flagged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir) / "agents"
            agents_dir.mkdir()
            paired_dir = Path(tmpdir) / "paired"
            paired_dir.mkdir()

            (agents_dir / "test.agent.md").write_text("version A")
            (paired_dir / "SKILL.md").write_text("version B")

            config = {
                "ghost_ops": {"agents_dir": str(agents_dir)},
                "missions": {
                    "fleet_evolution": {
                        "paired_files": {
                            "test.agent.md": [str(paired_dir / "SKILL.md")]
                        }
                    }
                },
            }
            findings = arun(_check_file_drift(config, False))
            warns = [f for f in findings if f.severity == "WARN" and "Drift" in f.detail]
            self.assertGreaterEqual(len(warns), 1)

    def test_missing_agent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir) / "agents"
            agents_dir.mkdir()

            config = {
                "ghost_ops": {"agents_dir": str(agents_dir)},
                "missions": {
                    "fleet_evolution": {
                        "paired_files": {
                            "nonexistent.md": ["/tmp/whatever"]
                        }
                    }
                },
            }
            findings = arun(_check_file_drift(config, False))
            self.assertGreaterEqual(len(findings), 1)

    def test_broken_symlink_is_critical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir) / "agents"
            agents_dir.mkdir()
            (agents_dir / "test.agent.md").write_text("content")

            symlink = Path(tmpdir) / "broken_link"
            symlink.symlink_to("/nonexistent/target/file.md")

            config = {
                "ghost_ops": {"agents_dir": str(agents_dir)},
                "missions": {
                    "fleet_evolution": {
                        "paired_files": {
                            "test.agent.md": [str(symlink)]
                        }
                    }
                },
            }
            findings = arun(_check_file_drift(config, False))
            critical = [f for f in findings if f.severity == "CRITICAL"]
            self.assertGreaterEqual(len(critical), 1)
            self.assertIn("Broken symlink", critical[0].detail)


# ---------------------------------------------------------------------------
# Secret Alignment
# ---------------------------------------------------------------------------

class TestSecretAlignment(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_check_secret_alignment(["o/r"], True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")


# ---------------------------------------------------------------------------
# Full mission (dry run)
# ---------------------------------------------------------------------------

class TestSentinelRun(unittest.TestCase):
    def test_dry_run_returns_summary(self):
        ctx = MagicMock()
        ctx.dry_run = True
        ctx.logger = MagicMock()
        ctx.store = MagicMock()
        ctx.store.write_alert = AsyncMock()
        ctx.config = {
            "ghost_ops": {"db_path": ":memory:", "agents_dir": "/tmp/agents"},
            "missions": {
                "sentinel": {
                    "repos": ["o/r1"],
                    "thresholds": {},
                    "data_freshness": [],
                },
            },
        }
        result = arun(run(ctx))
        self.assertEqual(result["checks_run"], 10)
        self.assertIn("findings", result)
        self.assertGreaterEqual(result["info"], 1)


# ---------------------------------------------------------------------------
# gh Auth Pre-flight
# ---------------------------------------------------------------------------

class TestGhAuth(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_check_gh_auth(True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    @patch("missions.sentinel._gh_cli")
    def test_auth_failure_is_critical(self, mock_cli):
        async def side_effect(*args):
            return (1, "", "not logged in")
        mock_cli.side_effect = side_effect
        findings = arun(_check_gh_auth(False))
        critical = [f for f in findings if f.severity == "CRITICAL"]
        self.assertGreaterEqual(len(critical), 1)
        self.assertIn("not authenticated", critical[0].detail)

    @patch("missions.sentinel._gh_cli")
    def test_auth_success_clean(self, mock_cli):
        async def side_effect(*args):
            return (0, "Logged in to github.com as user (token has repo scope)", "")
        mock_cli.side_effect = side_effect
        findings = arun(_check_gh_auth(False))
        critical = [f for f in findings if f.severity == "CRITICAL"]
        self.assertEqual(len(critical), 0)


# ---------------------------------------------------------------------------
# DB Pruning
# ---------------------------------------------------------------------------

class TestDbPruning(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_prune_db({}, True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    def test_prunes_old_runs(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        try:
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY, mission_id TEXT, status TEXT,
                    finished_at DATETIME
                );
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY, severity TEXT, source TEXT,
                    title TEXT, acknowledged BOOLEAN DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Insert old run (100 days ago)
            conn.execute(
                "INSERT INTO runs (mission_id, status, finished_at) "
                "VALUES ('test', 'success', datetime('now', '-100 days'))"
            )
            # Insert recent run
            conn.execute(
                "INSERT INTO runs (mission_id, status, finished_at) "
                "VALUES ('test', 'success', datetime('now', '-1 day'))"
            )
            conn.commit()

            count_before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            self.assertEqual(count_before, 2)
            conn.close()

            config = {"ghost_ops": {"db_path": db_path}}
            findings = arun(_prune_db(config, False))

            conn = sqlite3.connect(db_path)
            count_after = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            self.assertEqual(count_after, 1)  # old one pruned
            conn.close()
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# Agent Quality
# ---------------------------------------------------------------------------

class TestAgentQuality(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_check_agent_quality({}, True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    def test_no_xray_skips_silently(self):
        config = {"ghost_ops": {"agents_dir": "/nonexistent"}}
        findings = arun(_check_agent_quality(config, False))
        self.assertEqual(len(findings), 0)


# ---------------------------------------------------------------------------
# Venv Health
# ---------------------------------------------------------------------------

class TestVenvHealth(unittest.TestCase):
    def test_dry_run(self):
        findings = arun(_check_venv_health({}, True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "INFO")

    def test_missing_venv_warns(self):
        config = {"missions": {"sentinel": {"venvs": [
            {"path": "/nonexistent/venv", "imports": ["pydantic"]}
        ]}}}
        findings = arun(_check_venv_health(config, False))
        warns = [f for f in findings if f.severity == "WARN"]
        self.assertGreaterEqual(len(warns), 1)
        self.assertIn("not found", warns[0].detail)


if __name__ == "__main__":
    unittest.main()
