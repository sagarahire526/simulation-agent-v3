"""
SSE Manager — bridges sync LangGraph thread execution to async SSE event stream.

Two primitives per in-flight query:
  asyncio.Queue  (keyed by query_id)  — sync thread pushes events;
                                         async SSE generator reads and yields them.
  threading.Event (keyed by thread_id) — sync stream thread blocks on .wait();
                                          the resume endpoint calls .set() to unblock.
"""
from __future__ import annotations

import asyncio
import threading
import logging

logger = logging.getLogger(__name__)


class SSEManager:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._loops:  dict[str, asyncio.AbstractEventLoop] = {}
        self._hitl_events:  dict[str, threading.Event] = {}
        self._resume_answers: dict[str, str] = {}

    # ── Stream registration ───────────────────────────────────────────────────

    def register(self, query_id: str, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        """
        Create and store an asyncio.Queue + event-loop reference for this query.
        Must be called from the async context (event loop thread) before the
        executor thread starts.
        Returns the queue so the SSE generator can await items from it.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._queues[query_id] = q
        self._loops[query_id]  = loop
        return q

    # ── Sync → async event bridge ─────────────────────────────────────────────

    def put_sync(self, query_id: str, event_name: str, data: dict) -> None:
        """
        Push an SSE event from a thread executor into the async queue.
        Uses run_coroutine_threadsafe so queue.put() is scheduled safely on
        the event loop rather than called directly from the worker thread.
        """
        q    = self._queues.get(query_id)
        loop = self._loops.get(query_id)
        if q is None or loop is None:
            logger.warning("put_sync: no queue/loop registered for query_id=%s", query_id)
            return
        asyncio.run_coroutine_threadsafe(
            q.put({"event": event_name, "data": data}),
            loop,
        )

    # ── HITL pause / resume ───────────────────────────────────────────────────

    def create_hitl_event(self, thread_id: str) -> threading.Event:
        """
        Create a threading.Event for the HITL pause.
        The stream thread calls .wait() on it; the resume endpoint calls .set().
        """
        event = threading.Event()
        self._hitl_events[thread_id] = event
        return event

    def signal_resume(self, thread_id: str, answer: str) -> bool:
        """
        Unblock the waiting stream thread with the user's clarification answer.
        Returns True if a stream was waiting, False otherwise.
        """
        event = self._hitl_events.get(thread_id)
        if event is None:
            return False
        self._resume_answers[thread_id] = answer
        event.set()
        return True

    def get_resume_answer(self, thread_id: str) -> str:
        """Retrieve (and remove) the answer stored by signal_resume."""
        return self._resume_answers.pop(thread_id, "Accept stated assumptions")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self, query_id: str, thread_id: str) -> None:
        """Release all resources associated with a completed stream."""
        self._queues.pop(query_id, None)
        self._loops.pop(query_id, None)
        self._hitl_events.pop(thread_id, None)
        self._resume_answers.pop(thread_id, None)


# Module-level singleton shared by graph.py and sse_simulate.py
sse_manager = SSEManager()
