"""
Feedback Analytics Dashboard — Streamlit page for the Simulation Agent.

Run:
    streamlit run streamlit_feedback_dashboard.py
"""
import streamlit as st
import pandas as pd
import psycopg2
from datetime import datetime, timedelta

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Feedback Dashboard | Simulation Agent",
    page_icon="📊",
    layout="wide",
)

# ── DB connection ────────────────────────────────────────────────────────────

SCHEMA = "pwc_agent_utility_schema"


@st.cache_resource
def get_connection():
    """Return a persistent psycopg2 connection (cached across reruns)."""
    import dotenv, os
    dotenv.load_dotenv()
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5433"),
        database=os.getenv("PG_DATABASE", "nokia_syn_v1"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASSWORD", "postgres"),
        connect_timeout=5,
    )


def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Execute a read query and return a DataFrame."""
    conn = get_connection()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        # reconnect on stale connection
        st.cache_resource.clear()
        conn = get_connection()
        return pd.read_sql_query(sql, conn, params=params)


# ── Data loaders ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_all_feedback() -> pd.DataFrame:
    return run_query(f"""
        SELECT
            f.feedback_id, f.thread_id, f.query_id, f.user_id, f.username,
            f.rating, f.is_positive, f.comment, f.created_at,
            q.original_query, q.final_response, q.routing_decision
        FROM {SCHEMA}.simulation_agent_feedback f
        LEFT JOIN {SCHEMA}.simulation_agent_queries q ON q.query_id = f.query_id
        ORDER BY f.created_at DESC
    """)


@st.cache_data(ttl=30)
def load_stats() -> dict:
    df = run_query(f"""
        SELECT
            COUNT(*) AS total,
            ROUND(AVG(rating)::numeric, 2) AS avg_rating,
            COUNT(*) FILTER (WHERE is_positive = true) AS thumbs_up,
            COUNT(*) FILTER (WHERE is_positive = false) AS thumbs_down
        FROM {SCHEMA}.simulation_agent_feedback
    """)
    return df.iloc[0].to_dict() if len(df) else {
        "total": 0, "avg_rating": None, "thumbs_up": 0, "thumbs_down": 0,
    }


@st.cache_data(ttl=30)
def load_daily_trend() -> pd.DataFrame:
    return run_query(f"""
        SELECT
            DATE(created_at) AS date,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE is_positive = true) AS thumbs_up,
            COUNT(*) FILTER (WHERE is_positive = false) AS thumbs_down,
            ROUND(AVG(rating)::numeric, 2) AS avg_rating
        FROM {SCHEMA}.simulation_agent_feedback
        GROUP BY DATE(created_at)
        ORDER BY date
    """)


@st.cache_data(ttl=30)
def load_user_breakdown() -> pd.DataFrame:
    return run_query(f"""
        SELECT
            username,
            COUNT(*) AS total_feedback,
            ROUND(AVG(rating)::numeric, 2) AS avg_rating,
            COUNT(*) FILTER (WHERE is_positive = true) AS thumbs_up,
            COUNT(*) FILTER (WHERE is_positive = false) AS thumbs_down
        FROM {SCHEMA}.simulation_agent_feedback
        GROUP BY username
        ORDER BY total_feedback DESC
    """)


@st.cache_data(ttl=30)
def load_route_breakdown() -> pd.DataFrame:
    return run_query(f"""
        SELECT
            COALESCE(q.routing_decision, 'unknown') AS route,
            COUNT(*) AS total_feedback,
            ROUND(AVG(f.rating)::numeric, 2) AS avg_rating,
            COUNT(*) FILTER (WHERE f.is_positive = true) AS thumbs_up,
            COUNT(*) FILTER (WHERE f.is_positive = false) AS thumbs_down
        FROM {SCHEMA}.simulation_agent_feedback f
        LEFT JOIN {SCHEMA}.simulation_agent_queries q ON q.query_id = f.query_id
        GROUP BY q.routing_decision
        ORDER BY total_feedback DESC
    """)


@st.cache_data(ttl=30)
def load_rating_distribution() -> pd.DataFrame:
    return run_query(f"""
        SELECT rating, COUNT(*) AS count
        FROM {SCHEMA}.simulation_agent_feedback
        WHERE rating IS NOT NULL
        GROUP BY rating
        ORDER BY rating
    """)


# ── Dashboard UI ─────────────────────────────────────────────────────────────

st.title("Simulation Agent — Feedback Dashboard")

# Refresh button
if st.button("Refresh Data"):
    st.cache_data.clear()
    st.rerun()

try:
    stats = load_stats()
    df = load_all_feedback()
except Exception as e:
    st.error(f"Could not connect to database: {e}")
    st.stop()

# ── KPI Cards ────────────────────────────────────────────────────────────────

st.markdown("---")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Feedback", int(stats["total"]))
col2.metric("Avg Rating", stats["avg_rating"] if stats["avg_rating"] else "N/A")
col3.metric("Thumbs Up", int(stats["thumbs_up"]))
col4.metric("Thumbs Down", int(stats["thumbs_down"]))

if stats["total"] > 0:
    thumbs_total = int(stats["thumbs_up"]) + int(stats["thumbs_down"])
    if thumbs_total > 0:
        satisfaction = round(int(stats["thumbs_up"]) / thumbs_total * 100, 1)
        st.progress(satisfaction / 100, text=f"Satisfaction Rate: {satisfaction}%")

st.markdown("---")

# ── Charts Row ───────────────────────────────────────────────────────────────

chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.subheader("Daily Feedback Trend")
    daily = load_daily_trend()
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"])
        chart_data = daily.set_index("date")[["thumbs_up", "thumbs_down"]]
        st.bar_chart(chart_data, color=["#4CAF50", "#F44336"])
    else:
        st.info("No feedback data yet.")

with chart_col2:
    st.subheader("Rating Distribution")
    rating_dist = load_rating_distribution()
    if not rating_dist.empty:
        rating_dist["rating"] = rating_dist["rating"].astype(str)
        st.bar_chart(rating_dist.set_index("rating")["count"])
    else:
        st.info("No rated feedback yet.")

st.markdown("---")

# ── Breakdown Tables ─────────────────────────────────────────────────────────

breakdown_col1, breakdown_col2 = st.columns(2)

with breakdown_col1:
    st.subheader("Feedback by User")
    user_df = load_user_breakdown()
    if not user_df.empty:
        st.dataframe(user_df, use_container_width=True, hide_index=True)
    else:
        st.info("No feedback data yet.")

with breakdown_col2:
    st.subheader("Feedback by Route")
    route_df = load_route_breakdown()
    if not route_df.empty:
        st.dataframe(route_df, use_container_width=True, hide_index=True)
    else:
        st.info("No feedback data yet.")

st.markdown("---")

# ── Filters & Detailed Table ─────────────────────────────────────────────────

st.subheader("All Feedback (Detailed)")

if not df.empty:
    # Filters row
    filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)

    with filter_col1:
        users = ["All"] + sorted(df["username"].dropna().unique().tolist())
        selected_user = st.selectbox("Filter by User", users)

    with filter_col2:
        sentiment_options = ["All", "Positive", "Negative", "Unrated"]
        selected_sentiment = st.selectbox("Filter by Sentiment", sentiment_options)

    with filter_col3:
        routes = ["All"] + sorted(df["routing_decision"].dropna().unique().tolist())
        selected_route = st.selectbox("Filter by Route", routes)

    with filter_col4:
        comments_only = st.checkbox("Only with comments", value=False)

    # Apply filters
    filtered = df.copy()
    if selected_user != "All":
        filtered = filtered[filtered["username"] == selected_user]
    if selected_sentiment == "Positive":
        filtered = filtered[filtered["is_positive"] == True]
    elif selected_sentiment == "Negative":
        filtered = filtered[filtered["is_positive"] == False]
    elif selected_sentiment == "Unrated":
        filtered = filtered[filtered["is_positive"].isna()]
    if selected_route != "All":
        filtered = filtered[filtered["routing_decision"] == selected_route]
    if comments_only:
        filtered = filtered[filtered["comment"].notna() & (filtered["comment"] != "")]

    st.caption(f"Showing {len(filtered)} of {len(df)} feedback entries")

    # Display table
    display_cols = [
        "created_at", "username", "rating", "is_positive", "comment",
        "original_query", "routing_decision", "thread_id", "query_id",
    ]
    existing_cols = [c for c in display_cols if c in filtered.columns]
    st.dataframe(
        filtered[existing_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "created_at": st.column_config.DatetimeColumn("Time", format="DD/MM/YYYY HH:mm"),
            "username": "User",
            "rating": st.column_config.NumberColumn("Rating", format="%d ⭐"),
            "is_positive": st.column_config.CheckboxColumn("Thumbs Up"),
            "comment": st.column_config.TextColumn("Comment", width="large"),
            "original_query": st.column_config.TextColumn("Query", width="large"),
            "routing_decision": "Route",
            "thread_id": st.column_config.TextColumn("Thread ID", width="small"),
            "query_id": st.column_config.TextColumn("Query ID", width="small"),
        },
    )

    # ── Expandable: View full response for each feedback ─────────────────────

    st.markdown("---")
    st.subheader("Feedback Detail View")
    st.caption("Select a feedback entry to view the full query and agent response.")

    if not filtered.empty:
        selected_idx = st.selectbox(
            "Select feedback entry",
            range(len(filtered)),
            format_func=lambda i: (
                f"{filtered.iloc[i]['created_at']} | "
                f"{filtered.iloc[i]['username']} | "
                f"{'👍' if filtered.iloc[i]['is_positive'] == True else '👎' if filtered.iloc[i]['is_positive'] == False else '—'} | "
                f"{str(filtered.iloc[i]['original_query'])[:60]}..."
            ),
        )

        entry = filtered.iloc[selected_idx]

        detail_col1, detail_col2 = st.columns(2)

        with detail_col1:
            st.markdown("**User Query:**")
            st.text_area("query", value=str(entry.get("original_query", "")), height=120, disabled=True, label_visibility="collapsed")

        with detail_col2:
            st.markdown("**Agent Response:**")
            st.text_area("response", value=str(entry.get("final_response", "")), height=120, disabled=True, label_visibility="collapsed")

        meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
        meta_col1.metric("Rating", entry.get("rating") or "N/A")
        meta_col2.metric("Sentiment", "Positive" if entry.get("is_positive") == True else "Negative" if entry.get("is_positive") == False else "N/A")
        meta_col3.metric("Route", entry.get("routing_decision") or "N/A")
        meta_col4.metric("User", entry.get("username") or "N/A")

        if entry.get("comment"):
            st.markdown("**User Comment:**")
            st.info(entry["comment"])

else:
    st.info("No feedback entries found. Feedback will appear here once users start submitting.")
