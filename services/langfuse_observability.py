"""
Langfuse observability — builds the tracing config handed to every LLM call.

Mirrors the reporting-agent's `langfuse_observability` module (same helper names,
same tag convention) so both services report into one Langfuse project and the
same mental model ports across systems.

Wiring is two steps:
  1. The request entrypoint calls `set_request_context(thread_id, user_id, query_id)`
     ONCE (services/simulation_service.py, api/v1/endpoints/sse_simulate.py).
  2. Every LLM call site passes `config=handler_for(<AGENT>)`. Call sites that
     already build their own config (the ReAct traversal agent) merge instead:
     `config=merge_handler(base_config, TRAVERSAL_AGENT)`.

Trace grouping:
  • `langfuse_session_id` = thread_id — the conversation. Every turn, including
    HITL resumes, lands in the same Langfuse session.
  • `langfuse_user_id`    = user_id   — per-user filtering / cost attribution.
  • `query_id` rides along as trace metadata so a Langfuse trace can be joined
    back to its row in pwc_simulation_agent_schema.

Everything degrades to a no-op when the LANGFUSE_* credentials are absent:
`handler_for` returns None, `merge_handler` returns the base config untouched,
and no call site changes behaviour. That keeps local dev and any air-gapped
deployment running exactly as before.

Env vars:
    LANGFUSE_PUBLIC_KEY   pk-lf-...
    LANGFUSE_SECRET_KEY   sk-lf-...
    LANGFUSE_HOST         https://cloud.langfuse.com  (or the self-hosted URL)
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Primary tag (the product flow) ────────────────────────────────────────────
FLOW_SIMULATION_AGENT = "simulation-agent"

# ── Secondary tags — one per LLM call site ────────────────────────────────────
QUERY_REFINER      = "query-refiner"
ORCHESTRATOR       = "orchestrator"
PLANNER            = "planner"
TRAVERSAL_AGENT    = "traversal-agent"
RESPONSE_AGENT     = "response-agent"
ALGORITHM_NARRATOR = "algorithm-narrator"
CHART_GENERATOR    = "chart-generator"
SCENARIO_SELECTOR  = "scenario-selector"
SCENARIO_PARAMS    = "scenario-params"

# ── Per-request context ───────────────────────────────────────────────────────
# Set once at the request entrypoint and read anywhere downstream via
# `handler_for(agent)`, so thread_id / user_id / query_id never have to be
# threaded through node signatures or SimulationState.
#
# ContextVars propagate into the planner's parallel sub-traversals because
# agents/planner.py submits work through `contextvars.copy_context()`. Any NEW
# raw `threading.Thread` that makes an LLM call must copy the context the same
# way, otherwise `handler_for` sees an empty context and that call goes untraced.
_CURRENT_THREAD_ID: ContextVar[str] = ContextVar("langfuse_thread_id", default="")
_CURRENT_USER_ID:   ContextVar[str] = ContextVar("langfuse_user_id",   default="")
_CURRENT_QUERY_ID:  ContextVar[str] = ContextVar("langfuse_query_id",  default="")


def set_request_context(
    thread_id: str,
    user_id: str | None = None,
    query_id: str | None = None,
) -> None:
    """Bind the request's thread_id + user_id + query_id to the current context.
    Call this once at the top of every request-processing entrypoint."""
    _CURRENT_THREAD_ID.set(thread_id or "")
    _CURRENT_USER_ID.set(user_id or "")
    _CURRENT_QUERY_ID.set(query_id or "")


def current_thread_id() -> str: return _CURRENT_THREAD_ID.get()
def current_user_id() -> str:   return _CURRENT_USER_ID.get()
def current_query_id() -> str:  return _CURRENT_QUERY_ID.get()


def _check_credentials() -> bool:
    """Log credential status at startup so missing config is immediately visible."""
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host   = os.environ.get("LANGFUSE_HOST", "")
    if public and secret and host:
        # Privacy: never log any portion of the key — log host + status only.
        logger.info("Langfuse configured — host: %s | credentials: present", host)
        return True
    missing_count = sum(1 for v in (public, secret, host) if not v)
    logger.warning(
        "Langfuse NOT configured — %d credential(s) missing. Tracing disabled.",
        missing_count,
    )
    return False


_LANGFUSE_ENABLED = _check_credentials()


def is_enabled() -> bool:
    """True when LANGFUSE_* credentials were present at import time."""
    return _LANGFUSE_ENABLED


def get_handler(
    thread_id: str,
    flow: str,
    agent: str,
    user_id: str | None = None,
    query_id: str | None = None,
) -> dict | None:
    """
    Return a LangChain/LangGraph-compatible config dict with Langfuse tracing (v4 API).

    Langfuse v4: CallbackHandler() takes no constructor args. Session, user and
    tags travel in the `metadata` block of the LangChain config dict:
        config = {"callbacks": [...], "metadata": {"langfuse_session_id": ...}}

    Tags carry both the product flow and the specific agent/chain as secondary tag.

    Returns None when Langfuse is not configured so callers can safely do
        llm.invoke(messages, config=handler_for(AGENT))
    (LangChain treats config=None as "no config").
    """
    if not _LANGFUSE_ENABLED:
        return None
    try:
        from langfuse.langchain import CallbackHandler
        handler = CallbackHandler()
        config = {
            "callbacks": [handler],
            # `run_name` is LangChain's RunnableConfig field that controls the
            # outer span / trace root name. Without it, a direct `llm.invoke(…,
            # config=…)` shows up in Langfuse as "ChatOpenAI" (the runnable's
            # class name) instead of the intended agent tag. create_react_agent
            # honours it too, so this covers both single-invoke and agent sites.
            "run_name": agent,
            "metadata": {
                "langfuse_session_id": thread_id,
                "langfuse_user_id": user_id or "",
                "langfuse_tags": [flow, agent],
                "langfuse_trace_name": agent,
                # Plain metadata — joins a Langfuse trace to its DB row.
                "query_id": query_id or "",
            },
        }
        logger.info(
            "Langfuse config built   | agent: %-20s | flow: %-18s | thread: %s | user: %s",
            agent,
            flow,
            thread_id,
            user_id or "anonymous",
        )
        return config
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse config creation failed (%s) — tracing disabled for this call", exc)
        return None


def handler_for(agent: str, flow: str = FLOW_SIMULATION_AGENT) -> dict | None:
    """Convenience wrapper — pulls thread_id / user_id / query_id from the request
    context set by `set_request_context()` and returns a langfuse config ready to
    be passed straight into `llm.invoke(messages, config=...)`.

    Returns None when Langfuse is off OR no context has been set (scripts and
    background jobs without a thread_id are simply not traced instead of erroring)."""
    thread_id = current_thread_id()
    if not thread_id:
        return None
    return get_handler(
        thread_id=thread_id,
        flow=flow,
        agent=agent,
        user_id=current_user_id() or None,
        query_id=current_query_id() or None,
    )


def merge_handler(
    base: dict,
    agent: str,
    flow: str = FLOW_SIMULATION_AGENT,
) -> dict:
    """
    Merge the Langfuse config into a config the caller already needs.

    Used by the ReAct traversal agent, which passes `recursion_limit` and its own
    debug callback. Callback lists are concatenated (base callbacks first) rather
    than overwritten; every other Langfuse key is added on top.

    Returns `base` unchanged when Langfuse is off or no request context is set.
    """
    langfuse_config = handler_for(agent, flow)
    if not langfuse_config:
        return base

    merged = {**base, **{k: v for k, v in langfuse_config.items() if k != "callbacks"}}
    merged["callbacks"] = [*base.get("callbacks", []), *langfuse_config["callbacks"]]
    return merged


def flush() -> None:
    """
    Force-flush buffered spans. Langfuse batches in a background thread, so call
    this on application shutdown to avoid losing the last few traces when the
    process is killed (containers, uvicorn reload). No-op when tracing is off.
    """
    if not _LANGFUSE_ENABLED:
        return
    try:
        from langfuse import get_client
        get_client().flush()
        logger.info("Langfuse spans flushed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse flush failed (%s)", exc)
