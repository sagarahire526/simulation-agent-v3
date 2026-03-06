"""
Streamlit chatbot UI for the Simulation Agent.

Run backend first:  uvicorn main:app --reload --port 8000
Run UI:             streamlit run streamlit_app.py

HITL flow:
  - When the agent needs clarification, a special card is shown with questions.
  - The user answers in a dedicated form; the answer is sent to /simulate/resume.
  - thread_id is stored in session state to link requests within one conversation.
"""
import uuid
import time

import streamlit as st
import requests

API_BASE = "http://localhost:8000/api/v1"

st.set_page_config(
    page_title="Simulation Agent",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state init ────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []
if "health_checked" not in st.session_state:
    st.session_state.health_checked = False
if "health_data" not in st.session_state:
    st.session_state.health_data = None
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
if "pending_clarification" not in st.session_state:
    st.session_state.pending_clarification = None  # Set when HITL is paused
if "user_id" not in st.session_state:
    st.session_state.user_id = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _status_badge(status: str) -> str:
    return "🟢" if status == "connected" else "🔴"


def _fetch_health() -> dict | None:
    try:
        r = requests.get(f"{API_BASE}/health", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _run_simulation(query: str) -> dict:
    r = requests.post(
        f"{API_BASE}/simulate",
        json={
            "user_id": st.session_state.user_id,
            "query": query,
            "thread_id": st.session_state.thread_id,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


def _resume_simulation(clarification: str) -> dict:
    r = requests.post(
        f"{API_BASE}/simulate/resume",
        json={
            "thread_id": st.session_state.thread_id,
            "clarification": clarification,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


def _render_response_meta(meta: dict):
    """Render expandable detail sections for an assistant response."""
    if meta.get("errors"):
        with st.expander("⚠️ Errors", expanded=True):
            for err in meta["errors"]:
                st.warning(err)

    if meta.get("data_summary"):
        with st.expander("📊 Data Summary"):
            st.json(meta["data_summary"])

    if meta.get("calculations"):
        with st.expander("🔢 Calculation Trace"):
            st.code(meta["calculations"], language="text")

    if meta.get("planner_steps"):
        label = f"📋 Analysis Plan — {len(meta['planner_steps'])} steps (business intent)"
        with st.expander(label, expanded=False):
            if meta.get("planning_rationale"):
                st.info(f"**Why these steps?** {meta['planning_rationale']}", icon="💡")
            for i, step in enumerate(meta["planner_steps"], 1):
                # Strip "Sub-query N: " prefix for clean business-intent display
                display = step.split(": ", 1)[1] if ": " in step else step
                st.markdown(f"**Step {i}:** {display}")

    parts = []
    if meta.get("traversal_steps"):
        parts.append(f"{meta['traversal_steps']} tool call(s)")
    if meta.get("routing_decision"):
        parts.append(f"route: {meta['routing_decision']}")
    if meta.get("elapsed_s") is not None:
        parts.append(f"answered in {meta['elapsed_s']}s")
    if parts:
        st.caption(" · ".join(parts))


def _handle_api_response(data: dict, elapsed_s: float):
    """
    Process an API response — either a final answer or a clarification request.
    Renders UI and updates session state.
    """
    status = data.get("status", "complete")

    if status == "clarification_needed":
        clarification = data.get("clarification", {})
        st.session_state.pending_clarification = clarification

        questions = clarification.get("questions", [])
        assumptions = clarification.get("assumptions_if_skipped", [])
        message_txt = clarification.get("message", "Please clarify your query.")

        lines = [f"**{message_txt}**\n"]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q}")
        if assumptions:
            lines.append("\n*If you skip, I'll assume:*")
            for a in assumptions:
                lines.append(f"- {a}")
        content = "\n".join(lines)
        st.markdown(content)

        st.session_state.messages.append({
            "role": "assistant",
            "content": content,
            "meta": {"is_clarification": True},
        })
        return

    # ── Final response ─────────────────────────────────────────────────────
    st.session_state.pending_clarification = None

    final_response = data.get("final_response", "").strip()
    if not final_response:
        final_response = "_The agent did not produce a response. Check the execution log._"

    st.markdown(final_response)

    meta = {
        "errors": data.get("errors", []),
        "data_summary": data.get("data_summary", {}),
        "calculations": data.get("calculations", ""),
        "traversal_steps": data.get("traversal_steps", 0),
        "elapsed_s": elapsed_s,
        "routing_decision": data.get("routing_decision", ""),
        "planning_rationale": data.get("planning_rationale", ""),
        "planner_steps": data.get("planner_steps", []),
        "is_clarification": False,
    }
    _render_response_meta(meta)

    st.session_state.messages.append({
        "role": "assistant",
        "content": final_response,
        "meta": meta,
    })

    # Reset thread for the next independent query
    st.session_state.thread_id = str(uuid.uuid4())


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎯 Simulation Agent")
    st.caption("Powered by LangGraph · Neo4j · PostgreSQL · OpenAI")
    st.divider()

    st.markdown("**User ID**")
    user_id_input = st.text_input(
        "User ID",
        value=st.session_state.user_id,
        placeholder="e.g. user-001",
        label_visibility="collapsed",
    )
    if user_id_input != st.session_state.user_id:
        st.session_state.user_id = user_id_input
    st.divider()

    if not st.session_state.health_checked:
        st.session_state.health_data = _fetch_health()
        st.session_state.health_checked = True

    h = st.session_state.health_data

    st.markdown("**Service Status**")
    if h and "error" not in h:
        services = h.get("services", {})
        neo4j  = services.get("neo4j", {})
        pg     = services.get("postgres", {})
        openai = services.get("openai", {})

        st.markdown(
            f"{_status_badge(neo4j.get('status',''))} **Neo4j** &nbsp; "
            f"`{neo4j.get('latency_ms', '—')} ms`"
        )
        st.caption(neo4j.get("detail", ""))
        st.markdown(
            f"{_status_badge(pg.get('status',''))} **PostgreSQL** &nbsp; "
            f"`{pg.get('latency_ms', '—')} ms`"
        )
        st.caption(pg.get("detail", ""))
        st.markdown(
            f"{_status_badge(openai.get('status',''))} **OpenAI** &nbsp; "
            f"`{openai.get('latency_ms', '—')} ms`"
        )
        st.caption(openai.get("detail", ""))

        overall = h.get("status", "degraded")
        if overall == "ok":
            st.success("All services connected", icon="✅")
        else:
            st.warning("One or more services unavailable", icon="⚠️")
    elif h and "error" in h:
        st.error(f"API unreachable: {h['error']}", icon="🔴")
    else:
        st.info("Checking services…")

    if st.button("🔄 Refresh status", use_container_width=True):
        st.session_state.health_data = _fetch_health()
        st.rerun()

    st.divider()

    if st.button("🗑 Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.pending_clarification = None
        st.session_state.thread_id = str(uuid.uuid4())
        st.rerun()

    st.divider()


# ── Main chat area ────────────────────────────────────────────────────────────
st.markdown("## Simulation Agent")
st.caption("Ask a question and the agent will explore the knowledge graph to answer it.")

# Render existing chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("meta") and not msg["meta"].get("is_clarification"):
            _render_response_meta(msg["meta"])


# ── HITL clarification input ──────────────────────────────────────────────────
if st.session_state.pending_clarification:
    st.info("The agent needs more detail before running the simulation.", icon="💬")

    with st.form("clarification_form", clear_on_submit=True):
        clarification_text = st.text_area(
            "Your answer (or leave blank to accept stated assumptions):",
            placeholder="e.g. Chicago market, target 300 sites, next 2 weeks",
            height=80,
        )
        submitted = st.form_submit_button("Submit & Continue")

    if submitted:
        answer = clarification_text.strip() or "Accept stated assumptions"
        st.session_state.messages.append({"role": "user", "content": answer})

        with st.chat_message("assistant"):
            placeholder = st.empty()
            placeholder.markdown("_Resuming simulation…_")
            try:
                with st.spinner(""):
                    t0 = time.perf_counter()
                    data = _resume_simulation(answer)
                    elapsed_s = round(time.perf_counter() - t0, 1)
                placeholder.empty()
                _handle_api_response(data, elapsed_s)
            except requests.HTTPError as e:
                placeholder.error(f"API error ({e.response.status_code}): {e.response.text}")
            except Exception as e:
                placeholder.error(f"Could not reach the API: {e}")


# ── Normal chat input ─────────────────────────────────────────────────────────
elif prompt := st.chat_input(
    "Ask about site delivery, crews, prerequisites, schedules…",
    disabled=not st.session_state.user_id,
):

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Thinking…_")

        try:
            with st.spinner(""):
                t0 = time.perf_counter()
                data = _run_simulation(prompt)
                elapsed_s = round(time.perf_counter() - t0, 1)
            placeholder.empty()
            _handle_api_response(data, elapsed_s)

        except requests.HTTPError as e:
            error_text = f"API error ({e.response.status_code}): {e.response.text}"
            placeholder.error(error_text)
            st.session_state.messages.append({"role": "assistant", "content": error_text, "meta": {}})

        except Exception as e:
            error_text = f"Could not reach the API: {e}"
            placeholder.error(error_text)
            st.session_state.messages.append({"role": "assistant", "content": error_text, "meta": {}})
