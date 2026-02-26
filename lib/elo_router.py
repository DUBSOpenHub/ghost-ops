"""ELO Router — reads hackathon ELO scores and returns a ranked model list."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MODELS = ["claude-sonnet-4.6", "claude-opus-4.6", "gpt-5.2"]
_CACHE_TTL = 300  # seconds


class ELORouter:
    """Reads ~/.copilot/hackathon-elo.json and returns ranked model lists."""

    def __init__(self, elo_path: str | None = None, cache_ttl: int = _CACHE_TTL) -> None:
        self._elo_path = Path(os.path.expanduser(elo_path or "~/.copilot/hackathon-elo.json"))
        self._cache_ttl = cache_ttl
        self._cached_ranking: list[str] = []
        self._cache_loaded_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ranked_models(self) -> list[str]:
        """Return models ranked by ELO (highest first), refreshing cache if stale."""
        if self._is_cache_fresh():
            return list(self._cached_ranking)
        return self._load_and_cache()

    def top_model(self) -> str:
        """Return the single best model."""
        return self.ranked_models()[0]

    def fallback_chain(self, exclude: list[str] | None = None) -> list[str]:
        """Return ranked models excluding any that already failed this call."""
        exclude_set = set(exclude or [])
        chain = [m for m in self.ranked_models() if m not in exclude_set]
        # Guarantee at least the defaults are present
        for m in _DEFAULT_MODELS:
            if m not in chain and m not in exclude_set:
                chain.append(m)
        return chain

    def invalidate_cache(self) -> None:
        """Force a reload on the next call."""
        self._cached_ranking = []
        self._cache_loaded_at = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_cache_fresh(self) -> bool:
        return bool(self._cached_ranking) and (time.monotonic() - self._cache_loaded_at) < self._cache_ttl

    def _load_and_cache(self) -> list[str]:
        ranking = self._read_elo_file()
        self._cached_ranking = ranking
        self._cache_loaded_at = time.monotonic()
        return list(ranking)

    def _read_elo_file(self) -> list[str]:
        if not self._elo_path.exists():
            logger.info("ELO file not found at %s; using fallback model list", self._elo_path)
            return list(_DEFAULT_MODELS)

        try:
            raw = self._elo_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read ELO file: %s; using defaults", exc)
            return list(_DEFAULT_MODELS)

        models_dict = data.get("models")
        if not isinstance(models_dict, dict) or not models_dict:
            logger.warning("ELO file has no valid 'models' key; using defaults")
            return list(_DEFAULT_MODELS)

        try:
            ranked = sorted(
                models_dict.keys(),
                key=lambda m: float(models_dict[m].get("elo", 0)),
                reverse=True,
            )
        except (TypeError, AttributeError) as exc:
            logger.warning("Malformed ELO data: %s; using defaults", exc)
            return list(_DEFAULT_MODELS)

        # Ensure defaults are present at the tail for a complete fallback chain
        for m in _DEFAULT_MODELS:
            if m not in ranked:
                ranked.append(m)

        return ranked
