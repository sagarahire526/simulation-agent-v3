"""
DB Service — handles all persistence to pwc_simulation_agent_schema.

Writes to three tables:
  • threads               — one row per conversation thread
  • queries               — one row per user query
  • hitl_clarifications   — one row per HITL pause/resume cycle

Design rules:
  - Every function opens and closes its own connection (consistent with
    the rest of the codebase which also uses per-operation connections).
  - DB errors are logged but NEVER raised — DB failures must not block
    the agent from returning a response to the user.
"""
from __future__ import annotations

import json
import logging
import uuid

import threading
from contextlib import contextmanager

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

import config

logger = logging.getLogger(__name__)

_SCHEMA = "pwc_agent_utility_schema"

# ─────────────────────────────────────────────
# Connection pool (lazy-initialised, thread-safe)
# ─────────────────────────────────────────────

_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    """Return the shared connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # double-checked locking
                _pool = ThreadedConnectionPool(
                    minconn=2,
                    maxconn=20,
                    host=config.PG_HOST,
                    port=config.PG_PORT,
                    database=config.PG_DATABASE,
                    user=config.PG_USER,
                    password=config.PG_PASSWORD,
                    connect_timeout=5,
                )
                logger.info("PostgreSQL connection pool created (min=2, max=20)")
    return _pool


def close_pool() -> None:
    """Drain and close every connection in the pool. Called at app shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("PostgreSQL connection pool closed")


@contextmanager
def _pooled_conn():
    """Context manager: borrows a connection and always returns it to the pool."""
    pool = _get_pool()
    conn = pool.getconn()
    if conn is None:
        raise psycopg2.OperationalError(
            "Connection pool exhausted — all connections are in use"
        )
    try:
        # Revive stale connections (DB restart, network timeout, idle kill)
        if conn.closed:
            conn.reset()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def ensure_tables() -> None:
    """
    Create all required tables in pwc_simulation_agent_schema if they do not exist.
    Called once at application startup.
    """
    ddl = f"""
        CREATE TABLE IF NOT EXISTS {_SCHEMA}.simulation_agent_threads (
            thread_id       VARCHAR(100)    PRIMARY KEY,
            user_id         VARCHAR(100)    NOT NULL,
            thread_name     VARCHAR(255),
            created_at      TIMESTAMP       NOT NULL DEFAULT NOW(),
            last_active_at  TIMESTAMP       NOT NULL DEFAULT NOW(),
            status          VARCHAR(20)     NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS {_SCHEMA}.simulation_agent_queries (
            query_id            VARCHAR(100)    PRIMARY KEY,
            thread_id           VARCHAR(100)    NOT NULL
                                    REFERENCES {_SCHEMA}.simulation_agent_threads(thread_id),
            user_id             VARCHAR(100)    NOT NULL,
            original_query      TEXT            NOT NULL,
            refined_query       TEXT,
            routing_decision    VARCHAR(50),
            planning_rationale  JSONB,
            final_response      TEXT,
            algorithm           TEXT,
            graph               JSONB,
            started_at          TIMESTAMP       NOT NULL DEFAULT NOW(),
            completed_at        TIMESTAMP,
            duration_ms         NUMERIC(12, 2),
            status              VARCHAR(20)     NOT NULL DEFAULT 'running',
            traces              JSONB
        );

        CREATE TABLE IF NOT EXISTS {_SCHEMA}.simulation_agent_hitl_clarifications (
            clarification_id    VARCHAR(100)    PRIMARY KEY,
            query_id            VARCHAR(100)    NOT NULL
                                    REFERENCES {_SCHEMA}.simulation_agent_queries(query_id),
            thread_id           VARCHAR(100)    NOT NULL
                                    REFERENCES {_SCHEMA}.simulation_agent_threads(thread_id),
            questions_asked     JSONB           NOT NULL,
            assumptions_offered JSONB           NOT NULL,
            user_answer         TEXT,
            asked_at            TIMESTAMP       NOT NULL DEFAULT NOW(),
            answered_at         TIMESTAMP,
            was_skipped         BOOLEAN         DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS {_SCHEMA}.simulation_agent_feedback (
            feedback_id     VARCHAR(100)    PRIMARY KEY,
            thread_id       VARCHAR(100)    NOT NULL
                                REFERENCES {_SCHEMA}.simulation_agent_threads(thread_id),
            query_id        VARCHAR(100)    NOT NULL
                                REFERENCES {_SCHEMA}.simulation_agent_queries(query_id),
            user_id         VARCHAR(100)    NOT NULL,
            username        VARCHAR(255)    NOT NULL,
            rating          SMALLINT,
            is_positive     BOOLEAN,
            comment         TEXT,
            created_at      TIMESTAMP       NOT NULL DEFAULT NOW()
        );
    """
    # Migration: add graph and traces columns if missing (safe for existing deployments)
    migrate_graph_col = f"""
        ALTER TABLE {_SCHEMA}.simulation_agent_queries
            ADD COLUMN IF NOT EXISTS graph JSONB;
    """
    migrate_traces_col = f"""
        ALTER TABLE {_SCHEMA}.simulation_agent_queries
            ADD COLUMN IF NOT EXISTS traces JSONB;
    """
    migrate_analysis_col = f"""
        ALTER TABLE {_SCHEMA}.simulation_agent_queries
            ADD COLUMN IF NOT EXISTS analysis JSONB;
    """
    migrate_algorithm_col = f"""
        ALTER TABLE {_SCHEMA}.simulation_agent_queries
            ADD COLUMN IF NOT EXISTS algorithm TEXT;
    """
    try:
        with _pooled_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(migrate_graph_col)
                cur.execute(migrate_traces_col)
                cur.execute(migrate_analysis_col)
                cur.execute(migrate_algorithm_col)
        logger.info("pwc_simulation_agent_schema tables verified / created.")
    except Exception as exc:
        logger.error("ensure_tables failed: %s", exc)


def _exec(sql: str, params: tuple) -> None:
    """Execute a single DML statement. Logs and swallows all DB errors."""
    try:
        with _pooled_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
    except Exception as exc:
        logger.error("DB write failed — %.80s | error=%s", sql, exc)


def _fetch_one(sql: str, params: tuple):
    """Fetch a single scalar value. Returns None on error or no result."""
    try:
        with _pooled_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.error("DB read failed — %.80s | error=%s", sql, exc)
        return None


def _fetch_rows(sql: str, params: tuple) -> list[dict]:
    """Fetch all rows as a list of dicts. Returns [] on error or no results."""
    try:
        with _pooled_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("DB read failed — %.80s | error=%s", sql, exc)
        return []


def _fetch_row(sql: str, params: tuple) -> dict | None:
    """Fetch a single row as a dict. Returns None on error or no result."""
    rows = _fetch_rows(sql, params)
    return rows[0] if rows else None


# ─────────────────────────────────────────────
# threads
# ─────────────────────────────────────────────

def upsert_thread(thread_id: str, user_id: str, thread_name: str | None = None) -> None:
    """Create thread if new; on conflict refresh last_active_at and set status=active."""
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.simulation_agent_threads
            (thread_id, user_id, thread_name, created_at, last_active_at, status)
        VALUES (%s, %s, %s, NOW(), NOW(), 'active')
        ON CONFLICT (thread_id)
        DO UPDATE SET last_active_at = NOW(), status = 'active'
        """,
        (thread_id, user_id, thread_name),
    )


def auto_name_thread(thread_id: str, query: str) -> None:
    """Set thread_name to the first user query (truncated) if it's currently NULL."""
    name = query.strip()[:250]
    if not name:
        return
    _exec(
        f"UPDATE {_SCHEMA}.simulation_agent_threads SET thread_name = %s "
        f"WHERE thread_id = %s AND thread_name IS NULL",
        (name, thread_id),
    )


def touch_thread(thread_id: str) -> None:
    """Refresh last_active_at on resume (user_id already stored from initial call)."""
    _exec(
        f"UPDATE {_SCHEMA}.simulation_agent_threads SET last_active_at = NOW() WHERE thread_id = %s",
        (thread_id,),
    )


# ─────────────────────────────────────────────
# queries
# ─────────────────────────────────────────────

def create_query(
    query_id: str,
    thread_id: str,
    user_id: str,
    original_query: str,
) -> None:
    """Insert a new query row with status=running at the moment of receipt."""
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.simulation_agent_queries
            (query_id, thread_id, user_id, original_query, started_at, status)
        VALUES (%s, %s, %s, %s, NOW(), 'running')
        """,
        (query_id, thread_id, user_id, original_query),
    )


def update_query_paused(query_id: str) -> None:
    """Mark query as paused while waiting for HITL clarification."""
    _exec(
        f"UPDATE {_SCHEMA}.simulation_agent_queries SET status = 'paused' WHERE query_id = %s",
        (query_id,),
    )


def update_query_complete(
    query_id: str,
    refined_query: str,
    routing_decision: str,
    planner_steps: list[str],
    final_response: str,
    duration_ms: float,
    graph_data: dict | None = None,
    traces: dict | None = None,
    analysis: dict | None = None,
    algorithm: str | None = None,
) -> None:
    """
    Finalize a completed query.
    planning_rationale is stored as a JSON array of the planner steps.
    graph is stored as a Highcharts-compatible chart JSON object.
    traces is stored as a JSONB object with full execution trace.
    analysis is stored as a JSONB object with semantic search headings.
    algorithm is the plain-text step-by-step execution narrative produced
    by the fast-tier LLM in parallel with the response agent.
    """
    planning_rationale = json.dumps(planner_steps) if planner_steps else None
    graph_json = json.dumps(graph_data) if graph_data else None
    traces_json = json.dumps(traces) if traces else None
    analysis_json = json.dumps(analysis) if analysis else None
    algorithm_text = algorithm or None
    _exec(
        f"""
        UPDATE {_SCHEMA}.simulation_agent_queries SET
            refined_query      = %s,
            routing_decision   = %s,
            planning_rationale = %s,
            final_response     = %s,
            algorithm          = %s,
            graph              = %s,
            traces             = %s,
            analysis           = %s,
            completed_at       = NOW(),
            duration_ms        = %s,
            status             = 'complete'
        WHERE query_id = %s
        """,
        (
            refined_query,
            routing_decision,
            planning_rationale,
            final_response,
            algorithm_text,
            graph_json,
            traces_json,
            analysis_json,
            duration_ms,
            query_id,
        ),
    )


def update_query_error(query_id: str, duration_ms: float) -> None:
    """Mark query as errored with the elapsed duration."""
    _exec(
        f"""
        UPDATE {_SCHEMA}.simulation_agent_queries SET
            completed_at = NOW(),
            duration_ms  = %s,
            status       = 'error'
        WHERE query_id = %s
        """,
        (duration_ms, query_id),
    )


def get_graph_by_query_id(query_id: str) -> dict | None:
    """Return the graph JSONB column for a given query, or None."""
    return _fetch_row(
        f"SELECT query_id, original_query, graph FROM {_SCHEMA}.simulation_agent_queries WHERE query_id = %s",
        (query_id,),
    )


def get_paused_query_id(thread_id: str) -> str | None:
    """Return the query_id of the most recent paused query for this thread."""
    return _fetch_one(
        f"""
        SELECT query_id FROM {_SCHEMA}.simulation_agent_queries
        WHERE thread_id = %s AND status = 'paused'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (thread_id,),
    )


# ─────────────────────────────────────────────
# hitl_clarifications
# ─────────────────────────────────────────────

def create_hitl_clarification(
    query_id: str,
    thread_id: str,
    questions_asked: list[str],
    assumptions_offered: list[str],
) -> None:
    """Record the clarification questions shown to the user."""
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.simulation_agent_hitl_clarifications
            (clarification_id, query_id, thread_id,
             questions_asked, assumptions_offered, asked_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        """,
        (
            str(uuid.uuid4()),
            query_id,
            thread_id,
            json.dumps(questions_asked),
            json.dumps(assumptions_offered),
        ),
    )


def update_hitl_answered(
    query_id: str,
    user_answer: str,
    was_skipped: bool,
) -> None:
    """Record the user's response to the clarification questions."""
    _exec(
        f"""
        UPDATE {_SCHEMA}.simulation_agent_hitl_clarifications SET
            user_answer = %s,
            answered_at = NOW(),
            was_skipped = %s
        WHERE query_id = %s
        """,
        (user_answer, was_skipped, query_id),
    )


# ─────────────────────────────────────────────
# Read queries (used by threads endpoints)
# ─────────────────────────────────────────────

def get_threads_by_user(user_id: str) -> list[dict]:
    """Return all threads for a user, most recent first, with query count."""
    return _fetch_rows(
        f"""
        SELECT
            t.thread_id,
            t.user_id,
            t.thread_name,
            t.created_at,
            t.last_active_at,
            t.status,
            COUNT(q.query_id) AS total_queries
        FROM {_SCHEMA}.simulation_agent_threads t
        LEFT JOIN {_SCHEMA}.simulation_agent_queries q ON q.thread_id = t.thread_id
        WHERE t.user_id = %s
        GROUP BY t.thread_id
        ORDER BY t.last_active_at DESC
        """,
        (user_id,),
    )


def get_thread(thread_id: str) -> dict | None:
    """Return a single thread's metadata plus query count."""
    return _fetch_row(
        f"""
        SELECT
            t.thread_id,
            t.user_id,
            t.thread_name,
            t.created_at,
            t.last_active_at,
            t.status,
            COUNT(q.query_id) AS total_queries
        FROM {_SCHEMA}.simulation_agent_threads t
        LEFT JOIN {_SCHEMA}.simulation_agent_queries q ON q.thread_id = t.thread_id
        WHERE t.thread_id = %s
        GROUP BY t.thread_id
        """,
        (thread_id,),
    )


def delete_thread(thread_id: str) -> bool:
    """
    Delete a thread and all its child rows.
    Deletes in FK order: hitl_clarifications → queries → threads.
    Returns True if the thread existed and was deleted.
    """
    try:
        with _pooled_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.simulation_agent_feedback WHERE thread_id = %s",
                    (thread_id,),
                )
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.simulation_agent_hitl_clarifications WHERE thread_id = %s",
                    (thread_id,),
                )
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.simulation_agent_queries WHERE thread_id = %s",
                    (thread_id,),
                )
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.simulation_agent_threads WHERE thread_id = %s",
                    (thread_id,),
                )
                return cur.rowcount > 0
    except Exception as exc:
        logger.error("delete_thread failed — thread=%s | error=%s", thread_id, exc)
        return False


def get_messages_by_thread(thread_id: str) -> list[dict]:
    """Return all queries for a thread ordered by started_at ascending."""
    return _fetch_rows(
        f"""
        SELECT
            query_id,
            thread_id,
            user_id,
            original_query,
            refined_query,
            routing_decision,
            planning_rationale,
            final_response,
            algorithm,
            graph,
            analysis,
            traces,
            started_at,
            completed_at,
            duration_ms,
            status
        FROM {_SCHEMA}.simulation_agent_queries
        WHERE thread_id = %s
        ORDER BY started_at ASC
        """,
        (thread_id,),
    )


# ─────────────────────────────────────────────
# feedback
# ─────────────────────────────────────────────

def create_feedback(
    feedback_id: str,
    thread_id: str,
    query_id: str,
    user_id: str,
    username: str,
    rating: int | None = None,
    is_positive: bool | None = None,
    comment: str | None = None,
) -> None:
    """Insert a feedback entry linked to a specific query turn."""
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.simulation_agent_feedback
            (feedback_id, thread_id, query_id, user_id, username,
             rating, is_positive, comment, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """,
        (feedback_id, thread_id, query_id, user_id, username, rating, is_positive, comment),
    )


def get_feedback_for_query(query_id: str) -> list[dict]:
    """Return all feedback entries for a specific query turn."""
    return _fetch_rows(
        f"""
        SELECT feedback_id, thread_id, query_id, user_id, username,
               rating, is_positive, comment, created_at
        FROM {_SCHEMA}.simulation_agent_feedback
        WHERE query_id = %s
        ORDER BY created_at ASC
        """,
        (query_id,),
    )


def get_feedback_for_thread(thread_id: str) -> list[dict]:
    """Return all feedback entries for a given thread."""
    return _fetch_rows(
        f"""
        SELECT feedback_id, thread_id, query_id, user_id, username,
               rating, is_positive, comment, created_at
        FROM {_SCHEMA}.simulation_agent_feedback
        WHERE thread_id = %s
        ORDER BY created_at ASC
        """,
        (thread_id,),
    )


def get_feedback_stats(thread_id: str | None = None) -> dict:
    """Return aggregate feedback stats (count, avg rating, thumbs up/down)."""
    where = "WHERE thread_id = %s" if thread_id else ""
    params = (thread_id,) if thread_id else ()
    sql = f"""
        SELECT
            COUNT(*) AS total,
            ROUND(AVG(rating)::numeric, 2) AS avg_rating,
            COUNT(*) FILTER (WHERE is_positive = true) AS thumbs_up,
            COUNT(*) FILTER (WHERE is_positive = false) AS thumbs_down
        FROM {_SCHEMA}.simulation_agent_feedback
        {where}
    """
    row = _fetch_row(sql, params)
    return row if row else {"total": 0, "avg_rating": None, "thumbs_up": 0, "thumbs_down": 0}


def get_pending_clarification(thread_id: str) -> dict | None:
    """
    Return the pending clarification for a paused thread, or None if not paused.
    Joins queries + hitl_clarifications to get full context in one call.
    """
    return _fetch_row(
        f"""
        SELECT
            hc.clarification_id,
            hc.query_id,
            hc.questions_asked,
            hc.assumptions_offered,
            hc.asked_at
        FROM {_SCHEMA}.simulation_agent_hitl_clarifications hc
        JOIN {_SCHEMA}.simulation_agent_queries q ON q.query_id = hc.query_id
        WHERE q.thread_id = %s
          AND q.status    = 'paused'
          AND hc.answered_at IS NULL
        ORDER BY hc.asked_at DESC
        LIMIT 1
        """,
        (thread_id,),
    )
