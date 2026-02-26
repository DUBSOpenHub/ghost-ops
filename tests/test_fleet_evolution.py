"""Unit tests for fleet_evolution — no network, stdlib only."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from missions.fleet_evolution import _get_sample_task, _ab_test_agent, _resolve_consensus


def run(coro):
    """Helper: run a coroutine in a fresh event loop."""
    return asyncio.run(coro)


class TestGetSampleTask(unittest.TestCase):
    """_get_sample_task maps known agent IDs to specific tasks."""

    def test_compliance_inspector(self) -> None:
        task = _get_sample_task("compliance-inspector")
        self.assertIn("compliance", task.lower())
        self.assertIn("DUBSOpenHub/ghost-ops", task)

    def test_security_audit(self) -> None:
        task = _get_sample_task("security-audit")
        self.assertIn("security", task.lower())
        self.assertIn("DUBSOpenHub/ghost-ops", task)

    def test_repo_detective(self) -> None:
        task = _get_sample_task("repo-detective")
        self.assertIn("origin", task.lower())
        self.assertIn("DUBSOpenHub/ghost-ops", task)

    def test_unknown_agent_returns_fallback(self) -> None:
        task = _get_sample_task("some-unknown-agent-xyz")
        self.assertIn("DUBSOpenHub/ghost-ops", task)
        # Fallback should not be empty
        self.assertGreater(len(task), 10)

    def test_different_unknowns_same_fallback(self) -> None:
        t1 = _get_sample_task("agent-a")
        t2 = _get_sample_task("agent-b")
        self.assertEqual(t1, t2)


class TestABTestDryRun(unittest.TestCase):
    """_ab_test_agent returns stub values in dry-run mode."""

    def _make_ctx(self) -> MagicMock:
        ctx = MagicMock()
        ctx.dry_run = True
        return ctx

    def test_dry_run_stub_score_original(self) -> None:
        ctx = self._make_ctx()
        result = run(_ab_test_agent("orig", "mutated", "some task", ctx))
        self.assertAlmostEqual(result["score_original"], 6.0)

    def test_dry_run_stub_score_mutated(self) -> None:
        ctx = self._make_ctx()
        result = run(_ab_test_agent("orig", "mutated", "some task", ctx))
        self.assertAlmostEqual(result["score_mutated"], 7.5)

    def test_dry_run_winner_is_mutated(self) -> None:
        ctx = self._make_ctx()
        result = run(_ab_test_agent("orig", "mutated", "some task", ctx))
        self.assertEqual(result["winner"], "mutated")

    def test_dry_run_task_echoed(self) -> None:
        ctx = self._make_ctx()
        result = run(_ab_test_agent("orig", "mutated", "check this task", ctx))
        self.assertEqual(result["task"], "check this task")

    def test_dry_run_no_llm_calls(self) -> None:
        ctx = self._make_ctx()
        run(_ab_test_agent("orig", "mutated", "task", ctx))
        ctx.llm.complete.assert_not_called()


class TestConsensusWithAB(unittest.TestCase):
    """_resolve_consensus applies stricter threshold when A/B favors original."""

    def test_ab_mutated_winner_normal_threshold_approved(self) -> None:
        # AB winner="mutated" + 2/3 validators → approved (base threshold 2)
        self.assertEqual(_resolve_consensus(2, "mutated", 2), "approved")

    def test_ab_original_winner_stricter_threshold_rejected(self) -> None:
        # AB winner="original" + 2/3 validators → rejected (needs all 3)
        self.assertEqual(_resolve_consensus(2, "original", 2), "rejected")

    def test_ab_original_winner_unanimous_approved(self) -> None:
        # AB winner="original" + 3/3 validators → approved (unanimous override)
        self.assertEqual(_resolve_consensus(3, "original", 2), "approved")

    def test_ab_tie_normal_threshold_approved(self) -> None:
        # AB winner="tie" + 2/3 validators → approved (base threshold 2)
        self.assertEqual(_resolve_consensus(2, "tie", 2), "approved")

    def test_ab_tie_insufficient_rejected(self) -> None:
        # AB winner="tie" + 1/3 validators → rejected
        self.assertEqual(_resolve_consensus(1, "tie", 2), "rejected")

    def test_ab_mutated_winner_insufficient_rejected(self) -> None:
        # AB winner="mutated" but only 1/3 validators → rejected
        self.assertEqual(_resolve_consensus(1, "mutated", 2), "rejected")


if __name__ == "__main__":
    unittest.main()
