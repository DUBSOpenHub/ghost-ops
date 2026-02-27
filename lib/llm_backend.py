"""LLM Backend — GitHub Models API primary, Amplifier CLI fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_STUB_RESPONSE = {
    "content": "DRY-RUN STUB RESPONSE — no real LLM call made.",
    "model": "stub",
    "tokens_in": 0,
    "tokens_out": 0,
}


@dataclass
class LLMResponse:
    content: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LLMBackend:
    """Wraps GitHub Models API and Amplifier CLI with dry-run support."""

    def __init__(
        self,
        default_model: str = "claude-sonnet-4.6",
        dry_run: bool = False,
        primary: str = "github-models",
        fallback: str = "amplifier",
    ) -> None:
        self.default_model = default_model
        self.dry_run = dry_run
        self._primary = primary
        self._fallback = fallback
        self._total_tokens_in: int = 0
        self._total_tokens_out: int = 0

    # ------------------------------------------------------------------
    # Token accounting
    # ------------------------------------------------------------------

    @property
    def total_tokens_in(self) -> int:
        return self._total_tokens_in

    @property
    def total_tokens_out(self) -> int:
        return self._total_tokens_out

    def reset_token_counters(self) -> tuple[int, int]:
        """Reset counters and return (tokens_in, tokens_out) accumulated so far."""
        tin, tout = self._total_tokens_in, self._total_tokens_out
        self._total_tokens_in = 0
        self._total_tokens_out = 0
        return tin, tout

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Run a chat completion, returning LLMResponse."""
        if self.dry_run:
            return LLMResponse(**_STUB_RESPONSE)

        target_model = model or self.default_model
        errors: list[str] = []

        # Primary: GitHub Models
        try:
            resp = await self._github_models(messages, target_model, max_tokens, temperature)
            self._total_tokens_in += resp.tokens_in
            self._total_tokens_out += resp.tokens_out
            return resp
        except Exception as exc:
            errors.append(f"github-models: {exc}")
            logger.warning("Primary LLM failed (%s), trying fallback", exc)

        # Fallback: Amplifier
        try:
            resp = await self._amplifier(messages, target_model)
            self._total_tokens_in += resp.tokens_in
            self._total_tokens_out += resp.tokens_out
            return resp
        except Exception as exc:
            errors.append(f"amplifier: {exc}")
            logger.error("All LLM backends failed: %s", "; ".join(errors))
            raise RuntimeError(f"All LLM backends failed: {'; '.join(errors)}") from exc

    # ------------------------------------------------------------------
    # GitHub Models via `gh api`
    # ------------------------------------------------------------------

    async def _github_models(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        payload = json.dumps(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        proc = await asyncio.create_subprocess_exec(
            "gh", "api",
            "--method", "POST",
            "-H", "Accept: application/vnd.github+json",
            "/models/chat/completions",
            "--input", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=payload.encode())

        if proc.returncode != 0:
            raise RuntimeError(f"gh api exit {proc.returncode}: {stderr.decode().strip()}")

        data = json.loads(stdout.decode())
        choice = data["choices"][0]["message"]
        usage = data.get("usage", {})
        return LLMResponse(
            content=choice.get("content", ""),
            model=data.get("model", model),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            raw=data,
        )

    # ------------------------------------------------------------------
    # Amplifier CLI fallback
    # ------------------------------------------------------------------

    async def _amplifier(
        self,
        messages: list[dict[str, str]],
        model: str,
    ) -> LLMResponse:
        prompt_text = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages
        )
        amplifier_dir = os.path.expanduser("~/amplifier")
        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "amplifier",
            "--model", model,
            "--",
            prompt_text,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=amplifier_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            raise RuntimeError(f"amplifier exit {proc.returncode}: {stderr.decode().strip()}")

        content = stdout.decode().strip()
        return LLMResponse(content=content, model=model)
