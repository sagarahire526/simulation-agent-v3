"""
LLM Provider — centralised factory for ChatOpenAI instances.

Three model tiers (configured via env vars):
    fast    — lightweight routing/classification (orchestrator, query_refiner)
    default — core reasoning with tool use (traversal, planner)
    heavy   — high-quality synthesis (response)

Supports two modes (auto-detected from env vars):
    1. Standard OpenAI  — set OPENAI_API_KEY
    2. Custom gateway    — set LLM_BASE_URL + LLM_API_KEY_HEADER + LLM_API_KEY_VALUE
       (e.g., Nokia LLM Gateway with custom auth headers)

Usage:
    from services.llm_provider import LLMProvider

    llm = LLMProvider.get_llm("fast")
    llm = LLMProvider.get_llm("default")
    llm = LLMProvider.get_llm("heavy")
"""
from __future__ import annotations

import os
import logging

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ── Model tier defaults ──────────────────────────────────────────────────────
_MODEL_FAST    = os.getenv("LLM_MODEL_FAST",    "gpt-4o-mini")
_MODEL_DEFAULT = os.getenv("LLM_MODEL_DEFAULT", "gpt-4o")
_MODEL_HEAVY   = os.getenv("LLM_MODEL_HEAVY",   "gpt-5")

_TIER_MAP: dict[str, str] = {
    "fast":    _MODEL_FAST,
    "default": _MODEL_DEFAULT,
    "heavy":   _MODEL_HEAVY,
}

# ── Per-tier default kwargs (merged into ChatOpenAI, caller kwargs take precedence) ──
_TIER_KWARGS: dict[str, dict] = {
    "heavy": {"reasoning_effort": "low"},
}

# ── Gateway config (optional) ────────────────────────────────────────────────
_BASE_URL          = os.getenv("LLM_BASE_URL")           # e.g. https://llmgateway-qa-api.nokia.com/v1.2/
_API_KEY_HEADER    = os.getenv("LLM_API_KEY_HEADER")     # e.g. api-key
_API_KEY_VALUE     = os.getenv("LLM_API_KEY_VALUE")      # e.g. eyJ...
_WORKSPACE         = os.getenv("LLM_WORKSPACE")          # e.g. VR1857RolloutAgentDevSpace

_USE_GATEWAY = bool(_BASE_URL and _API_KEY_HEADER and _API_KEY_VALUE)


class LLMProvider:
    """
    Centralised LLM factory.

    Call LLMProvider.get_llm(tier) to get a ChatOpenAI instance configured
    for the requested model tier. Each call creates a fresh instance so
    callers can customise temperature / max_tokens independently.
    """

    @staticmethod
    def get_llm(
        tier: str = "default",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        **kwargs,
    ) -> ChatOpenAI:
        """
        Return a ChatOpenAI instance for the given tier.

        Args:
            tier:        "fast" | "default" | "heavy" (or an explicit model name)
            temperature: LLM temperature
            max_tokens:  max output tokens
            **kwargs:    forwarded to ChatOpenAI (e.g. reasoning_effort)
        """
        model = _TIER_MAP.get(tier, tier)  # fall back to treating tier as model name

        # Merge tier-level defaults (caller kwargs override)
        tier_defaults = _TIER_KWARGS.get(tier, {})
        merged_kwargs = {**tier_defaults, **kwargs}

        if _USE_GATEWAY:
            headers: dict[str, str] = {_API_KEY_HEADER: _API_KEY_VALUE}
            if _WORKSPACE:
                headers["workspacename"] = _WORKSPACE

            return ChatOpenAI(
                api_key="NONE",
                model=model,
                base_url=_BASE_URL,
                default_headers=headers,
                temperature=temperature,
                max_tokens=max_tokens,
                **merged_kwargs,
            )

        # Standard OpenAI
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **merged_kwargs,
        )
