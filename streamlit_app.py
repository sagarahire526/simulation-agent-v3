"""
Streamlit chatbot UI for the Simulation Agent.

Run backend first:  uvicorn main:app --reload --port 8000
Run UI:             streamlit run streamlit_app.py
"""
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
    st.session_state.messages = []          # [{role, content, meta}]
if "health_checked" not in st.session_state:
    st.session_state.health_checked = False
if "health_data" not in st.session_state:
    st.session_state.health_data = None


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
        json={"query": query},
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎯 Simulation Agent")
    st.caption("Powered by LangGraph · Neo4j · PostgreSQL · OpenAI")
    st.divider()

    # Auto-check health once per session
    if not st.session_state.health_checked:
        st.session_state.health_data = _fetch_health()
        st.session_state.health_checked = True

    h = st.session_state.health_data

    st.markdown("**Service Status**")
    if h and "error" not in h:
        services = h.get("services", {})

        neo4j   = services.get("neo4j",    {})
        pg      = services.get("postgres", {})
        openai  = services.get("openai",   {})

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
        st.rerun()

    st.divider()



# ── Main chat area ────────────────────────────────────────────────────────────
st.markdown("## Simulation Agent")
st.caption("Ask a question and the agent will explore the knowledge graph to answer it.")

# Render existing chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

        # For assistant messages, show optional detail expanders
        if msg["role"] == "assistant" and msg.get("meta"):
            meta = msg["meta"]

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

            parts = []
            if meta.get("traversal_steps"):
                parts.append(f"{meta['traversal_steps']} tool call(s)")
            if meta.get("elapsed_s") is not None:
                parts.append(f"answered in {meta['elapsed_s']}s")
            if parts:
                st.caption(" · ".join(parts))


# ── Chat input ────────────────────────────────────────────────────────────────
if prompt := st.chat_input("Ask about site delivery, crews, prerequisites, schedules…"):

    # Add user message to history and render it immediately
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Call the simulation API and stream the response
    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Thinking…_")

        try:
            with st.spinner(""):
                t0 = time.perf_counter()
                data = _run_simulation(prompt)
                elapsed_s = round(time.perf_counter() - t0, 1)

            final_response = data.get("final_response", "").strip()
            if not final_response:
                final_response = "_The agent did not produce a response. Check the execution log._"

            placeholder.markdown(final_response)

            meta = {
                "errors":          data.get("errors", []),
                "data_summary":    data.get("data_summary", {}),
                "calculations":    data.get("calculations", ""),
                "traversal_steps": data.get("traversal_steps", 0),
                "elapsed_s":       elapsed_s,
            }

            if meta["errors"]:
                with st.expander("⚠️ Errors", expanded=True):
                    for err in meta["errors"]:
                        st.warning(err)

            if meta["data_summary"]:
                with st.expander("📊 Data Summary"):
                    st.json(meta["data_summary"])

            if meta["calculations"]:
                with st.expander("🔢 Calculation Trace"):
                    st.code(meta["calculations"], language="text")

            parts = []
            if meta["traversal_steps"]:
                parts.append(f"{meta['traversal_steps']} tool call(s)")
            if meta.get("elapsed_s") is not None:
                parts.append(f"answered in {meta['elapsed_s']}s")
            if parts:
                st.caption(" · ".join(parts))

        except requests.HTTPError as e:
            error_text = f"API error ({e.response.status_code}): {e.response.text}"
            placeholder.error(error_text)
            final_response = error_text
            meta = {}

        except Exception as e:
            error_text = f"Could not reach the API: {e}"
            placeholder.error(error_text)
            final_response = error_text
            meta = {}

    # Persist assistant message with metadata
    st.session_state.messages.append({
        "role": "assistant",
        "content": final_response,
        "meta": meta,
    })
