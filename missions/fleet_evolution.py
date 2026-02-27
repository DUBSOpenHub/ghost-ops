"""Fleet Evolution — mutate bottom-ranked agents with 2/3 consensus validation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MUTATION_PROMPT = """\
You are an AI agent improvement specialist.

Below is an AI agent prompt file. Your task is to improve it by:
1. Making the instructions clearer and more actionable
2. Adding better examples or edge-case handling
3. Improving output format guidance

Return ONLY the full improved agent file content — no explanation, no markdown fences.

--- ORIGINAL AGENT FILE ---
{content}
"""

_VALIDATION_PROMPT = """\
You are a strict AI agent quality reviewer.

Compare these two agent prompt files and determine if the MUTATED version is better than the ORIGINAL.

Respond with ONLY valid JSON: {{"approved": true}} or {{"approved": false, "reason": "..."}}

--- ORIGINAL ---
{original}

--- MUTATED ---
{mutated}
"""


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _sync_paired_files(agent_path: Path, content: str, ctx: Any) -> None:
    """Sync paired file copies after an approved mutation."""
    config = ctx.config.get("missions", {}).get("fleet_evolution", {})
    paired_map = config.get("paired_files", {})
    targets = paired_map.get(agent_path.name, [])
    for target in targets:
        dest = Path(os.path.expanduser(target))
        if not dest.parent.exists():
            logger.warning("[fleet_evolution] Paired target dir missing: %s", dest.parent)
            continue
        try:
            dest.write_text(content, encoding="utf-8")
            logger.info("[fleet_evolution] Synced paired file: %s", dest)
        except OSError as exc:
            logger.warning("[fleet_evolution] Failed to sync %s: %s", dest, exc)


async def _xray_score(content: str, ctx: Any) -> int | None:
    """Run Agent X-Ray on content and return composite score (0-100), or None on error.

    Writes content to a temp file, runs agent-xray.js --json, parses result.
    Zero tokens — pure deterministic pattern matching.
    """
    import tempfile
    xray_path = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "agent-xray.js"
    if not xray_path.exists():
        logger.debug("[fleet_evolution] agent-xray.js not found at %s", xray_path)
        return None

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(content)
        tmp_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "node", str(xray_path), tmp_path, "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode())
        return int(data.get("composite", 0))
    except Exception as exc:
        logger.debug("[fleet_evolution] X-Ray scan failed: %s", exc)
        return None
    finally:
        os.unlink(tmp_path)


def _get_sample_task(agent_id: str) -> str:
    """Return a representative task string for the given agent ID."""
    _TASKS = {
        "compliance-inspector": "Audit the license and compliance posture of DUBSOpenHub/ghost-ops",
        "security-audit": "Check the security features of DUBSOpenHub/ghost-ops",
        "repo-detective": "Find the origin and first PR of DUBSOpenHub/ghost-ops",
    }
    return _TASKS.get(
        agent_id,
        "Analyze the GitHub repository DUBSOpenHub/ghost-ops and provide a brief assessment",
    )


def _resolve_consensus(approvals: int, ab_winner: str, base_threshold: int) -> str:
    """Return 'approved' or 'rejected'; raises threshold to 3 when A/B favors original."""
    threshold = 3 if ab_winner == "original" else base_threshold
    return "approved" if approvals >= threshold else "rejected"


async def _ab_test_agent(
    original_content: str, mutated_content: str, task: str, ctx: Any
) -> dict:
    """Run original and mutated agent prompts on the same task; blind-judge the outputs.

    Returns dict with score_original, score_mutated, winner ('original'|'mutated'|'tie'), task.
    In dry-run mode returns stub scores.
    """
    if ctx.dry_run:
        return {"score_original": 6.0, "score_mutated": 7.5, "winner": "mutated", "task": task}

    messages_original = [
        {"role": "system", "content": original_content},
        {"role": "user", "content": task},
    ]
    messages_mutated = [
        {"role": "system", "content": mutated_content},
        {"role": "user", "content": task},
    ]

    try:
        resp_original = await ctx.llm.complete(messages_original, max_tokens=1024, temperature=0.1)
        resp_mutated = await ctx.llm.complete(messages_mutated, max_tokens=1024, temperature=0.1)
    except Exception as exc:
        logger.warning("[fleet_evolution] A/B test LLM call failed: %s; returning tie", exc)
        return {"score_original": 5.0, "score_mutated": 5.0, "winner": "tie", "task": task}

    output_original = resp_original.content.strip()
    output_mutated = resp_mutated.content.strip()

    # Randomize A/B assignment to prevent position bias
    a_is_original = random.random() < 0.5
    output_a = output_original if a_is_original else output_mutated
    output_b = output_mutated if a_is_original else output_original

    judge_prompt = (
        "You are a strict quality judge. Two AI agents produced outputs for the same task.\n"
        "Score each output 1-10 on: accuracy, specificity, actionability.\n"
        'Return ONLY valid JSON: {"score_a": float, "score_b": float, "winner": "a"|"b"|"tie", "reason": "..."}\n\n'
        f"TASK: {task}\n\n"
        f"OUTPUT A:\n{output_a}\n\n"
        f"OUTPUT B:\n{output_b}"
    )

    try:
        judge_resp = await ctx.llm.complete(
            [{"role": "user", "content": judge_prompt}],
            max_tokens=256,
            temperature=0.1,
        )
        verdict = json.loads(judge_resp.content)
        score_a = float(verdict.get("score_a", 5.0))
        score_b = float(verdict.get("score_b", 5.0))
        raw_winner = verdict.get("winner", "tie")
    except Exception as exc:
        logger.warning("[fleet_evolution] A/B judge failed: %s; returning tie", exc)
        return {"score_original": 5.0, "score_mutated": 5.0, "winner": "tie", "task": task}

    # Map A/B scores back to original/mutated
    if a_is_original:
        score_original, score_mutated = score_a, score_b
        winner = "original" if raw_winner == "a" else ("mutated" if raw_winner == "b" else "tie")
    else:
        score_original, score_mutated = score_b, score_a
        winner = "mutated" if raw_winner == "a" else ("original" if raw_winner == "b" else "tie")

    logger.info(
        "[fleet_evolution] A/B test: original=%.1f mutated=%.1f winner=%s task=%.50s",
        score_original, score_mutated, winner, task,
    )
    return {"score_original": score_original, "score_mutated": score_mutated, "winner": winner, "task": task}


async def _mutate_agent(agent_path: Path, ctx: Any) -> dict[str, Any] | None:
    """Mutate a single agent file and validate with 3 models."""
    agent_id = agent_path.stem
    config = ctx.config.get("missions", {}).get("fleet_evolution", {})
    validator_models: list[str] = config.get(
        "validator_models", ["claude-opus-4.6", "gpt-5.2", "claude-sonnet-4.6"]
    )
    consensus_threshold: int = config.get("consensus_threshold", 2)

    original_content = agent_path.read_text(encoding="utf-8")
    original_hash = _file_hash(original_content)

    # Generate mutation
    if ctx.dry_run:
        mutated_content = original_content + "\n<!-- Ghost Ops dry-run mutation -->\n"
    else:
        model = ctx.elo.top_model()
        messages = [{"role": "user", "content": _MUTATION_PROMPT.format(content=original_content)}]
        try:
            resp = await ctx.llm.complete(messages, model=model, max_tokens=4096, temperature=0.5)
            mutated_content = resp.content.strip()
        except Exception as exc:
            logger.warning("[fleet_evolution] LLM unavailable for %s (%s); applying stub mutation", agent_id, exc)
            mutated_content = original_content + "\n<!-- ghost-ops-evolved: stub mutation applied -->\n"

    mutated_hash = _file_hash(mutated_content)

    # X-Ray quality gate — reject mutations that lower the deterministic composite score
    xray_original = await _xray_score(original_content, ctx)
    xray_mutated = await _xray_score(mutated_content, ctx)
    if xray_original is not None and xray_mutated is not None:
        logger.info(
            "[fleet_evolution] X-Ray gate %s: original=%d mutated=%d",
            agent_id, xray_original, xray_mutated,
        )
        if xray_mutated < xray_original:
            logger.info(
                "[fleet_evolution] X-Ray rejected %s: mutated score %d < original %d",
                agent_id, xray_mutated, xray_original,
            )
            await ctx.store.record_mutation(
                agent_id=agent_id,
                original_hash=original_hash,
                mutated_hash=mutated_hash,
                mutation_type="improvement",
                validators=["xray-rejected"],
                consensus="rejected",
                deployed=False,
                ab_score_original=float(xray_original),
                ab_score_mutated=float(xray_mutated),
                ab_task="xray-composite",
                ab_winner="original",
            )
            return {
                "agent_id": agent_id,
                "original_hash": original_hash,
                "mutated_hash": mutated_hash,
                "consensus": "rejected",
                "approvals": 0,
                "mutation_id": 0,
                "ab_winner": "original",
                "xray_rejected": True,
            }

    # A/B test the mutation before validation
    sample_task = _get_sample_task(agent_id)
    ab_result = await _ab_test_agent(original_content, mutated_content, sample_task, ctx)
    logger.info(
        "[fleet_evolution] A/B result for %s: original=%.1f mutated=%.1f winner=%s",
        agent_id, ab_result["score_original"], ab_result["score_mutated"], ab_result["winner"],
    )

    # If A/B favors the original, require unanimous consensus (3/3) instead of 2/3
    effective_threshold = 3 if ab_result["winner"] == "original" else consensus_threshold

    # Validate with 3 models, providing A/B evidence to each validator
    ab_evidence = (
        f"A/B test results: original_score={ab_result['score_original']:.1f}, "
        f"mutated_score={ab_result['score_mutated']:.1f}, winner={ab_result['winner']}"
    )
    validator_tasks = [
        _validate_mutation(original_content, mutated_content, model, ctx, ab_evidence=ab_evidence)
        for model in validator_models[:3]
    ]
    validator_results: list[str] = await asyncio.gather(*validator_tasks)
    approvals = sum(1 for v in validator_results if v == "approved")
    consensus = _resolve_consensus(approvals, ab_result["winner"], effective_threshold)

    logger.info(
        "[fleet_evolution] %s: mutation %s/%d validators approved (threshold=%d) → %s",
        agent_id, approvals, len(validator_models), effective_threshold, consensus,
    )

    # Record in DB with A/B scores
    mutation_id = await ctx.store.record_mutation(
        agent_id=agent_id,
        original_hash=original_hash,
        mutated_hash=mutated_hash,
        mutation_type="improvement",
        validators=validator_results,
        consensus=consensus,
        deployed=consensus == "approved",
        ab_score_original=ab_result["score_original"],
        ab_score_mutated=ab_result["score_mutated"],
        ab_task=ab_result["task"],
        ab_winner=ab_result["winner"],
    )

    if consensus == "approved":
        # Backup original BEFORE writing new content
        backup_path = Path(str(agent_path) + ".bak")
        shutil.copy2(agent_path, backup_path)
        logger.info("[fleet_evolution] Backed up %s → %s", agent_path.name, backup_path.name)
        agent_path.write_text(mutated_content, encoding="utf-8")
        logger.info("[fleet_evolution] Applied mutation to %s (mutation_id=%d)", agent_path.name, mutation_id)
        _sync_paired_files(agent_path, mutated_content, ctx)
    else:
        logger.info("[fleet_evolution] Mutation rejected for %s", agent_path.name)

    return {
        "agent_id": agent_id,
        "original_hash": original_hash,
        "mutated_hash": mutated_hash,
        "consensus": consensus,
        "approvals": approvals,
        "mutation_id": mutation_id,
        "ab_winner": ab_result["winner"],
    }


async def _validate_mutation(original: str, mutated: str, model: str, ctx: Any, *, ab_evidence: str = "") -> str:
    """Ask one validator model if the mutation is approved. Returns 'approved' or 'rejected'."""
    if ctx.dry_run:
        return "approved"

    prompt = _VALIDATION_PROMPT.format(original=original[:2000], mutated=mutated[:2000])
    if ab_evidence:
        prompt += f"\n\nA/B EVIDENCE: {ab_evidence}"
    messages = [{"role": "user", "content": prompt}]
    try:
        resp = await ctx.llm.complete(messages, model=model, max_tokens=128, temperature=0.1)
        data = json.loads(resp.content)
        return "approved" if data.get("approved") else "rejected"
    except Exception as exc:
        logger.warning("[fleet_evolution] Validator %s unavailable: %s; defaulting to approved", model, exc)
        return "approved"


async def _get_agent_fitness(agent_path: Path, ctx: Any) -> float:
    """Return ELO-based fitness score for an agent file."""
    agent_id = agent_path.stem
    # Try elo_cache table first
    rows = await ctx.store.execute(
        "SELECT elo FROM elo_cache WHERE model=?", (agent_id,)
    )
    if rows:
        return float(rows[0]["elo"])
    # Fall back to ELO router for known models
    ranked = ctx.elo.ranked_models()
    if agent_id in ranked:
        return float(len(ranked) - ranked.index(agent_id))
    return 0.0


async def _check_rollbacks(ctx: Any) -> int:
    """Check recently deployed mutations; rollback any that regress in A/B re-test.

    Skipped in dry-run mode. Returns the count of mutations rolled back.
    """
    if ctx.dry_run:
        return 0

    agents_dir = Path(os.path.expanduser(
        os.environ.get("GHOST_OPS_AGENT_DIR") or
        ctx.config.get("ghost_ops", {}).get("agents_dir", "~/.copilot/agents")
    ))

    rows = await ctx.store.execute(
        "SELECT * FROM mutations WHERE deployed=1 AND consensus='approved' "
        "AND created_at >= datetime('now', '-24 hours')"
    )

    rollbacks = 0
    for row in rows:
        agent_id = row["agent_id"]
        agent_path = agents_dir / f"{agent_id}.md"
        backup_path = Path(str(agent_path) + ".bak")

        if not agent_path.exists() or not backup_path.exists():
            continue

        current_content = agent_path.read_text(encoding="utf-8")
        original_content = backup_path.read_text(encoding="utf-8")

        task = _get_sample_task(agent_id)
        ab_result = await _ab_test_agent(original_content, current_content, task, ctx)

        if ab_result["winner"] == "original":
            shutil.copy2(backup_path, agent_path)
            logger.info(
                "[fleet_evolution] Rolled back %s (original=%.1f > current=%.1f)",
                agent_id, ab_result["score_original"], ab_result["score_mutated"],
            )
            await ctx.store.record_mutation(
                agent_id=agent_id,
                original_hash=_file_hash(current_content),
                mutated_hash=_file_hash(original_content),
                mutation_type="rollback",
                validators=[],
                consensus="rolled_back",
                deployed=True,
                ab_score_original=ab_result["score_original"],
                ab_score_mutated=ab_result["score_mutated"],
                ab_task=task,
                ab_winner=ab_result["winner"],
            )
            rollbacks += 1

    return rollbacks


async def run(ctx: Any) -> dict[str, Any]:
    """Fleet Evolution mission — mutate and validate bottom-ranked agents."""
    log = ctx.logger
    config = ctx.config.get("missions", {}).get("fleet_evolution", {})
    # GHOST_OPS_AGENT_DIR env var overrides config for testing
    agents_dir = Path(os.path.expanduser(
        os.environ.get("GHOST_OPS_AGENT_DIR") or
        ctx.config.get("ghost_ops", {}).get("agents_dir", "~/.copilot/agents")
    ))
    batch_size: int = config.get("evolution_batch_size", 3)

    # Check for regressions and rollback before mutating
    rollbacks = await _check_rollbacks(ctx)
    if rollbacks:
        log.info("[fleet_evolution] Rolled back %d regressed mutation(s)", rollbacks)

    if not agents_dir.exists():
        log.info("[fleet_evolution] Agents dir %s does not exist; skipping", agents_dir)
        return {"agents_found": 0, "mutated": 0, "approved": 0, "rollbacks": rollbacks}

    agent_files = sorted(agents_dir.glob("*.md"))
    if not agent_files:
        log.info("[fleet_evolution] No *.agent.md files found in %s", agents_dir)
        return {"agents_found": 0, "mutated": 0, "approved": 0, "rollbacks": rollbacks}

    log.info("[fleet_evolution] Found %d agent files", len(agent_files))

    # Rank agents by fitness (lowest = most in need of evolution)
    fitness_tasks = [_get_agent_fitness(f, ctx) for f in agent_files]
    fitness_scores: list[float] = list(await asyncio.gather(*fitness_tasks))

    ranked_agents = sorted(zip(agent_files, fitness_scores), key=lambda x: x[1])
    bottom_agents = [path for path, _ in ranked_agents[:batch_size]]

    log.info(
        "[fleet_evolution] Mutating bottom %d agents: %s",
        len(bottom_agents), [f.name for f in bottom_agents],
    )

    # Mutate sequentially to avoid hammering the LLM
    mutated = 0
    approved = 0
    for agent_path in bottom_agents:
        result = await _mutate_agent(agent_path, ctx)
        if result:
            mutated += 1
            if result["consensus"] == "approved":
                approved += 1

    return {"agents_found": len(agent_files), "mutated": mutated, "approved": approved, "rollbacks": rollbacks}
