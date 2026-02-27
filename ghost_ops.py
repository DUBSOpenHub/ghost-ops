#!/usr/bin/env python3
"""
ghost_ops.py — Ghost Ops Daemon
Async mission scheduler with ELO-routed LLM backend.
Pure stdlib, Python 3.11+.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure lib/ and missions/ are importable when run directly
_BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(_BASE_DIR))

from lib.elo_router import ELORouter
from lib.llm_backend import LLMBackend
from lib.state import StateStore

import missions.portfolio_watchdog as _portfolio_watchdog
import missions.inbox_autopilot as _inbox_autopilot
import missions.fleet_evolution as _fleet_evolution

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _setup_logging(log_level: str, log_file: Path | None = None) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = _JSONFormatter()
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        print(f"Configuration error: failed to parse {config_path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _validate_config(config: dict[str, Any], config_path: Path) -> None:
    """Validate required config sections; print human-readable error and exit 1 if invalid."""
    if not config_path.exists():
        print(f"Configuration error: {config_path} not found", file=sys.stderr)
        sys.exit(1)
    if not config:
        print(
            f"Configuration error: {config_path} is empty or missing required sections "
            "([ghost_ops], [llm], at least one [missions.*])",
            file=sys.stderr,
        )
        sys.exit(1)
    for section in ("ghost_ops", "llm"):
        if section not in config:
            print(f"Configuration error: missing required [{section}] section in {config_path}", file=sys.stderr)
            sys.exit(1)
    if not config.get("missions"):
        print(f"Configuration error: no missions defined in {config_path}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Cron-like scheduler
# ---------------------------------------------------------------------------

def _parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into (minute, hour, dom, month, dow) sets."""
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression: {expr!r}")

    def _expand(field: str, lo: int, hi: int) -> set[int]:
        if field == "*":
            return set(range(lo, hi + 1))
        result: set[int] = set()
        for part in field.split(","):
            if "/" in part:
                rng, step = part.split("/", 1)
                start, end = (lo, hi) if rng == "*" else map(int, rng.split("-"))
                result.update(range(start, end + 1, int(step)))
            elif "-" in part:
                start, end = map(int, part.split("-"))
                result.update(range(start, end + 1))
            else:
                result.add(int(part))
        return result

    return (
        _expand(fields[0], 0, 59),
        _expand(fields[1], 0, 23),
        _expand(fields[2], 1, 31),
        _expand(fields[3], 1, 12),
        _expand(fields[4], 0, 6),
    )


def _cron_matches(expr: str, dt: datetime) -> bool:
    """Return True if dt matches the cron expression."""
    try:
        mins, hours, doms, months, dows = _parse_cron(expr)
        return (
            dt.minute in mins
            and dt.hour in hours
            and dt.day in doms
            and dt.month in months
            and dt.isoweekday() % 7 in dows  # isoweekday: Mon=1..Sun=7; cron: Sun=0..Sat=6
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Mission context
# ---------------------------------------------------------------------------

@dataclass
class MissionContext:
    mission_id: str
    run_id: int
    config: dict[str, Any]
    store: StateStore
    llm: LLMBackend
    elo: ELORouter
    logger: logging.Logger
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Mission registry
# ---------------------------------------------------------------------------

_MISSION_MODULES = {
    "portfolio_watchdog": _portfolio_watchdog,
    "inbox_autopilot": _inbox_autopilot,
    "fleet_evolution": _fleet_evolution,
}

# Default cron schedules (overridden by config)
_DEFAULT_SCHEDULES = {
    "portfolio_watchdog": "0 6 * * *",   # nightly 6 AM
    "inbox_autopilot":    "0 * * * *",   # every hour
    "fleet_evolution":    "0 3 * * *",   # daily 3 AM
}


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class GhostOps:
    def __init__(self, config: dict[str, Any], dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self._shutdown = asyncio.Event()
        self.log = logging.getLogger("ghost_ops")

        ghost_cfg = config.get("ghost_ops", {})
        llm_cfg = config.get("llm", {})

        db_path = os.path.expanduser(ghost_cfg.get("db_path", "~/ghost-ops/ghost_ops.db"))
        elo_path = ghost_cfg.get("elo_path", "~/.copilot/hackathon-elo.json")

        self.store = StateStore(db_path)
        self.elo = ELORouter(elo_path=elo_path)
        self.llm = LLMBackend(
            default_model=llm_cfg.get("default_model", "claude-sonnet-4.6"),
            dry_run=dry_run,
            primary=llm_cfg.get("primary", "github-models"),
            fallback=llm_cfg.get("fallback", "amplifier"),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, once: bool = False, mission: str | None = None) -> None:
        """Open store, register missions, run scheduler loop."""
        # Install signal handlers first so SIGTERM is always handled gracefully
        if not once:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._handle_shutdown)

        self.store.open()
        self.log.info("Ghost Ops starting (dry_run=%s)", self.dry_run)

        # Trigger ELO load at startup so fallback is surfaced in logs immediately
        self.elo.ranked_models()

        # Register missions in DB
        for mission_id, module in _MISSION_MODULES.items():
            schedule = self._mission_schedule(mission_id)
            await self.store.upsert_mission(mission_id, schedule)

        if once:
            if mission:
                await self._run_single_mission(mission)
            else:
                await self._run_all_missions()
            self.store.close()
            return

        await self._scheduler_loop()
        await self._drain(timeout=30)
        self.log.info("Ghost Ops shutdown complete")
        self.store.close()
        self.log.info("Ghost Ops stopped cleanly")

    def _handle_shutdown(self) -> None:
        self.log.info("Shutdown signal received")
        self._shutdown.set()

    async def _drain(self, timeout: int = 30) -> None:
        """Wait for running tasks to complete, up to timeout seconds."""
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        self.log.info("Draining %d tasks (timeout=%ds)...", len(pending), timeout)
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            self.log.warning("Cancelling task that did not finish: %s", task.get_name())
            task.cancel()

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        self.log.info("Scheduler loop started")
        _last_tick: datetime | None = None

        while not self._shutdown.is_set():
            now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)

            # Avoid running the same minute twice
            if _last_tick == now:
                await asyncio.sleep(10)
                continue
            _last_tick = now

            # Dispatch any missions that match this minute
            for mission_id in _MISSION_MODULES:
                if not self._mission_enabled(mission_id):
                    continue
                schedule = self._mission_schedule(mission_id)
                if _cron_matches(schedule, now):
                    asyncio.create_task(
                        self._run_mission(mission_id),
                        name=f"mission.{mission_id}",
                    )

            # Sleep until the next minute boundary
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Mission execution
    # ------------------------------------------------------------------

    async def _run_all_missions(self) -> None:
        """Run all enabled missions once (--once / --dry-run smoke test)."""
        enabled = [mid for mid in _MISSION_MODULES if self._mission_enabled(mid)]
        self.log.info("Running all missions once: %s", enabled)
        async with asyncio.TaskGroup() as tg:
            for mission_id in enabled:
                tg.create_task(self._run_mission(mission_id), name=f"mission.{mission_id}")

    async def _run_single_mission(self, mission_id: str) -> None:
        """Run exactly one mission by name (used with --mission flag)."""
        if mission_id not in _MISSION_MODULES:
            self.log.error("Unknown mission: %s. Available: %s", mission_id, list(_MISSION_MODULES))
            return
        self.log.info("Running single mission: %s", mission_id)
        await self._run_mission(mission_id)

    async def _run_mission(self, mission_id: str) -> None:
        log = logging.getLogger(f"ghost_ops.{mission_id}")
        module = _MISSION_MODULES[mission_id]
        run_id = await self.store.record_run_start(mission_id)
        ctx = MissionContext(
            mission_id=mission_id,
            run_id=run_id,
            config=self.config,
            store=self.store,
            llm=self.llm,
            elo=self.elo,
            logger=log,
            dry_run=self.dry_run,
        )
        log.info("Mission started (run_id=%d)", run_id)
        self.llm.reset_token_counters()
        try:
            results = await module.run(ctx)
            tokens_in, tokens_out = self.llm.reset_token_counters()
            await self.store.record_run_finish(
                run_id, mission_id, "success",
                tokens_in=tokens_in, tokens_out=tokens_out,
                results=results,
            )
            log.info("Mission completed (run_id=%d) tokens_in=%d tokens_out=%d results=%s",
                     run_id, tokens_in, tokens_out, results)
        except Exception as exc:
            tokens_in, tokens_out = self.llm.reset_token_counters()
            log.exception("Mission failed (run_id=%d): %s", run_id, exc)
            await self.store.record_run_finish(
                run_id, mission_id, "failed",
                tokens_in=tokens_in, tokens_out=tokens_out,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _mission_enabled(self, mission_id: str) -> bool:
        return bool(
            self.config.get("missions", {}).get(mission_id, {}).get("enabled", True)
        )

    def _mission_schedule(self, mission_id: str) -> str:
        return (
            self.config.get("missions", {})
            .get(mission_id, {})
            .get("schedule", _DEFAULT_SCHEDULES.get(mission_id, "0 6 * * *"))
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ghost Ops — autonomous GitHub mission daemon")
    p.add_argument(
        "--config",
        default=str(_BASE_DIR / "ghost_ops.toml"),
        help="Path to ghost_ops.toml (default: %(default)s)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all missions with stub LLM — no real API calls",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run all enabled missions once then exit (implied by --dry-run)",
    )
    p.add_argument(
        "--mission",
        default=None,
        metavar="NAME",
        help="Run only this named mission once then exit "
             "(portfolio_watchdog|inbox_autopilot|fleet_evolution)",
    )
    p.add_argument(
        "--log-level",
        default=None,
        help="Override log level (DEBUG|INFO|WARNING|ERROR)",
    )
    return p.parse_args()


def main() -> None:
    # Force line-buffered stdout so logs are visible in real time when piped
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, Exception):
        pass

    args = _parse_args()
    config_path = Path(args.config)
    config = _load_config(config_path)
    _validate_config(config, config_path)

    ghost_cfg = config.get("ghost_ops", {})
    log_level = args.log_level or ghost_cfg.get("log_level", "INFO")

    # Log to file when not in --dry-run / --once mode
    log_file: Path | None = None
    if not (args.dry_run or args.once):
        log_dir = Path(os.path.expanduser(ghost_cfg.get("db_path", "~/ghost-ops/ghost_ops.db"))).parent / "logs"
        log_file = log_dir / "ghost_ops.log"

    _setup_logging(log_level, log_file)

    daemon = GhostOps(config=config, dry_run=args.dry_run)
    once = args.once or args.dry_run or bool(args.mission)

    try:
        asyncio.run(daemon.start(once=once, mission=args.mission))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
