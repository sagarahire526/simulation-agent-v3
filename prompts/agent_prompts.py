"""
Backwards-compatible re-export of all agent prompts.

Individual prompt modules:
    prompts.traversal_prompt  — TRAVERSAL_SYSTEM
    prompts.response_prompt   — RESPONSE_SYSTEM

Import directly from those modules for new code.
This file exists so that any legacy `from prompts.agent_prompts import ...`
calls continue to work without modification.
"""
from prompts.traversal_prompt import TRAVERSAL_SYSTEM  # noqa: F401
from prompts.response_prompt import RESPONSE_SYSTEM    # noqa: F401

__all__ = ["TRAVERSAL_SYSTEM", "RESPONSE_SYSTEM"]
