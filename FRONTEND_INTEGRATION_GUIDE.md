# Frontend Integration Guide

This guide covers every API endpoint the frontend needs — standard HTTP endpoints for threads and simulation, and the SSE stream endpoint for real-time progress events.

**Base URL**: `http://localhost:8000/api/v1`
**Authentication**: None (user identity is passed as `user_id` in request body / query param)

---

## Table of Contents

1. [Standard Simulation (HTTP)](#1-standard-simulation-http)
2. [SSE Streaming Simulation](#2-sse-streaming-simulation)
3. [SSE Event Reference](#3-sse-event-reference)
4. [HITL — Human-in-the-Loop](#4-hitl--human-in-the-loop)
5. [Thread Management](#5-thread-management)
6. [Health Check](#6-health-check)
7. [TypeScript Types](#7-typescript-types)
8. [Integration Patterns](#8-integration-patterns)

---

## 1. Standard Simulation (HTTP)

Use this when you want a simple request/response cycle with no streaming. Good for background jobs or simple integrations.

### POST `/simulate`

Start a new simulation query.

**Request**
```json
{
  "user_id": "user-001",
  "query": "Share me the weekly plan for Chicago market to complete 100 sites in next 3 weeks",
  "thread_id": "session-abc-123"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `user_id` | string | yes | Your user identifier |
| `query` | string | yes | Natural-language PM question |
| `thread_id` | string | no | Pass to continue an existing conversation thread. Generated server-side if omitted |

**Response — complete**
```json
{
  "status": "complete",
  "thread_id": "session-abc-123",
  "final_response": "### Simulation Result: Chicago Weekly Rollout Plan\n...",
  "routing_decision": "simulation",
  "planner_steps": ["Sub-query 1: ...", "Sub-query 2: ..."],
  "planning_rationale": "...",
  "traversal_steps": 14,
  "data_summary": {},
  "calculations": "",
  "errors": [],
  "clarification": null,
  "messages": []
}
```

**Response — clarification needed (HITL)**
```json
{
  "status": "clarification_needed",
  "thread_id": "session-abc-123",
  "final_response": "",
  "clarification": {
    "type": "clarification_needed",
    "original_query": "Can we complete the sites?",
    "questions": [
      "Which market or region are these sites in?",
      "What is the target completion timeframe?"
    ],
    "assumptions_if_skipped": [
      "All active markets will be included",
      "Target timeframe will default to end of current quarter"
    ],
    "message": "Your query needs a bit more detail to run a precise simulation. Please answer the questions below (or press Enter to accept assumptions):"
  },
  ...
}
```

When `status === "clarification_needed"`, show the `clarification.questions` to the user, then call `/simulate/resume`.

---

### POST `/simulate/resume`

Resume a simulation that paused for HITL clarification.

**Request**
```json
{
  "thread_id": "session-abc-123",
  "clarification": "Chicago market, target is 300 sites by end of next week"
}
```

To accept the agent's stated assumptions without answering, send:
```json
{
  "thread_id": "session-abc-123",
  "clarification": "Accept stated assumptions"
}
```

**Response**: Same shape as `/simulate` response with `status: "complete"`.

---

## 2. SSE Streaming Simulation

Use this to show real-time progress as the agent works through each step. The connection is a standard `text/event-stream` — use the browser's native `EventSource` API.

### GET `/simulate/stream`

**Query parameters**

| Param | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | URL-encoded natural-language query |
| `user_id` | string | yes | User identifier |
| `thread_id` | string | no | Pass to continue a conversation thread; auto-generated if omitted |

**Example**
```
GET /api/v1/simulate/stream?query=Weekly+plan+for+Chicago+market+100+sites&user_id=user-001&thread_id=session-abc-123
Accept: text/event-stream
```

**JavaScript (EventSource)**
```javascript
const params = new URLSearchParams({
  query: "Share me the weekly plan for Chicago market to complete 100 sites in next 3 weeks",
  user_id: "user-001",
  thread_id: "session-abc-123",  // optional — omit to auto-generate
});

const evtSource = new EventSource(`/api/v1/simulate/stream?${params}`);

// First event always — contains the thread_id (critical for resume)
evtSource.addEventListener("stream_started", (e) => {
  const { query_id, thread_id } = JSON.parse(e.data);
  // Store thread_id — you'll need it if a hitl_start event arrives
  localStorage.setItem("active_thread_id", thread_id);
});

// Agent progress events
evtSource.addEventListener("query_refiner_complete", (e) => {
  const { refined_query } = JSON.parse(e.data);
  showProgress("Query refined", refined_query);
});

evtSource.addEventListener("orchestrator_complete", (e) => {
  const { routing_decision } = JSON.parse(e.data);
  showProgress("Routing", routing_decision);
});

evtSource.addEventListener("schema_complete", (e) => {
  showProgress("Schema loaded");
});

evtSource.addEventListener("planner_complete", (e) => {
  const { planner_steps } = JSON.parse(e.data);  // string[]
  showProgress(`Plan created — ${planner_steps.length} steps`);
});

evtSource.addEventListener("traversal_complete", (e) => {
  const { traversal_steps } = JSON.parse(e.data);  // number
  showProgress(`Data gathered — ${traversal_steps} tool calls`);
});

evtSource.addEventListener("response_complete", (e) => {
  const { final_response } = JSON.parse(e.data);
  showProgress("Response generated");
});

// HITL events
evtSource.addEventListener("hitl_start", (e) => {
  const payload = JSON.parse(e.data);
  // payload contains: questions[], assumptions_if_skipped[], message, original_query
  showClarificationUI(payload);
  // DO NOT close the EventSource — keep it open for hitl_complete
});

evtSource.addEventListener("hitl_complete", (e) => {
  const { answer } = JSON.parse(e.data);
  hideClarificationUI();
  showProgress("Resuming simulation...");
});

// Final event — simulation complete
evtSource.addEventListener("complete", (e) => {
  const { final_response, routing_decision, planner_steps } = JSON.parse(e.data);
  evtSource.close();
  renderFinalResponse(final_response);
});

// Error
evtSource.addEventListener("error", (e) => {
  if (e.data) {
    const { message } = JSON.parse(e.data);
    showError(message);
  }
  evtSource.close();
});

// Connection-level error (network drop)
evtSource.onerror = () => {
  if (evtSource.readyState === EventSource.CLOSED) {
    showError("Connection lost");
  }
};
```

---

### POST `/simulate/stream/resume`

Resume a paused SSE stream after the user answers a HITL clarification. The SSE connection **must still be open** when you call this.

**Request**
```json
{
  "thread_id": "session-abc-123",
  "clarification": "Chicago market, 300 sites, end of next week"
}
```

To accept assumptions:
```json
{
  "thread_id": "session-abc-123",
  "clarification": "Accept stated assumptions"
}
```

**Response** (200 OK)
```json
{
  "status": "resumed",
  "thread_id": "session-abc-123"
}
```

**Error** (404) — returned if the SSE stream is no longer open:
```json
{
  "detail": "No active stream found for thread 'session-abc-123'. The stream may have already timed out or completed."
}
```

**JavaScript (resume call)**
```javascript
async function submitClarification(answer) {
  const threadId = localStorage.getItem("active_thread_id");
  const res = await fetch("/api/v1/simulate/stream/resume", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: threadId,
      clarification: answer || "Accept stated assumptions",
    }),
  });
  if (!res.ok) {
    const err = await res.json();
    showError(err.detail);
  }
}
```

---

## 3. SSE Event Reference

All events follow the standard `text/event-stream` format:
```
event: <event_name>
data: <json_string>

```

### Full event sequence — no HITL

```
stream_started          → always first; contains thread_id and query_id
query_refiner_complete  → query accepted / refined
orchestrator_complete   → routing decision made
schema_complete         → KG schema loaded
planner_complete        → (simulation route only) sub-query plan created
traversal_complete      → (simulation route: fires after each parallel step)
response_complete       → final response text generated
complete                → simulation finished; contains full final_response
```

### Full event sequence — with HITL

```
stream_started
query_refiner_complete  → (fires after clarification is merged)
hitl_start              → SHOW CLARIFICATION UI; keep SSE connection open
--- user calls POST /simulate/stream/resume ---
hitl_complete           → clarification received; hide UI
orchestrator_complete
schema_complete
planner_complete        (or traversal_complete for simple queries)
traversal_complete
response_complete
complete
```

### Event payloads

| Event | Data fields |
|---|---|
| `stream_started` | `{ query_id: string, thread_id: string }` |
| `query_refiner_complete` | `{ refined_query: string }` |
| `orchestrator_complete` | `{ routing_decision: "greeting" \| "traversal" \| "simulation" }` |
| `schema_complete` | `{}` |
| `planner_complete` | `{ planner_steps: string[] }` |
| `traversal_complete` | `{ traversal_steps: number }` |
| `response_complete` | `{ final_response: string }` |
| `hitl_start` | `{ type: string, original_query: string, questions: string[], assumptions_if_skipped: string[], message: string }` |
| `hitl_complete` | `{ answer: string }` |
| `complete` | `{ final_response: string, routing_decision: string, planner_steps: string[] }` |
| `error` | `{ message: string }` |

---

## 4. HITL — Human-in-the-Loop

The Query Refiner agent may pause the simulation to ask for missing scope information (market, timeframe, or target volume).

### Detecting HITL

**Standard HTTP**: `response.status === "clarification_needed"`
**SSE**: `event: hitl_start` received on the open stream

### What to show the user

```javascript
// From hitl_start event data (SSE) or clarification field (HTTP)
{
  message: "Your query needs a bit more detail...",
  questions: [
    "Which market or region are these sites in?",
    "What is the target timeframe?"
  ],
  assumptions_if_skipped: [
    "All active markets included",
    "Target: end of current quarter"
  ]
}
```

Show:
1. `message` as a header
2. `questions` as a numbered list for the user to answer
3. `assumptions_if_skipped` as a "If you skip, I'll assume:" note
4. A text area for the answer
5. A "Submit" button and a "Accept Assumptions" button

### Resuming after HITL

| Mode | How to resume |
|---|---|
| Standard HTTP | `POST /simulate/resume` with `{ thread_id, clarification }` |
| SSE | `POST /simulate/stream/resume` with `{ thread_id, clarification }` — keep EventSource open |

### Page refresh recovery

If the user refreshes mid-HITL, use this endpoint to detect the pending state:

**GET** `/threads/{thread_id}/clarification`

```json
// Thread is paused waiting for clarification
{
  "is_paused": true,
  "clarification_id": "uuid",
  "query_id": "uuid",
  "questions_asked": ["Which market?", "What timeframe?"],
  "assumptions_offered": ["All markets", "End of quarter"],
  "asked_at": "2025-01-15T10:30:00"
}

// Thread is not paused
{
  "is_paused": false
}
```

Use this on app load or route change to restore the HITL UI if needed.

---

## 5. Thread Management

Threads group related queries for a user. One thread = one conversation session.

### GET `/threads?user_id=<user_id>`

List all threads for a user, most recent first.

**Response**
```json
[
  {
    "thread_id": "session-abc-123",
    "user_id": "user-001",
    "created_at": "2025-01-15T09:00:00",
    "last_active_at": "2025-01-15T11:30:00",
    "status": "active",
    "total_queries": 5
  }
]
```

---

### GET `/threads/{thread_id}`

Get metadata for a single thread.

**Response**: Same shape as one item from the list above. Returns 404 if not found.

---

### DELETE `/threads/{thread_id}`

Permanently delete a thread and all its queries and HITL records.

**Response**: 204 No Content. Returns 404 if not found.

---

### GET `/threads/{thread_id}/messages`

Get all queries within a thread in chronological order (oldest first).

**Response**
```json
[
  {
    "query_id": "uuid",
    "thread_id": "session-abc-123",
    "user_id": "user-001",
    "original_query": "How many sites are ready in Chicago?",
    "refined_query": "How many sites in Chicago have all prerequisites cleared?",
    "routing_decision": "traversal",
    "planning_rationale": null,
    "final_response": "### Simulation Result...",
    "started_at": "2025-01-15T10:00:00",
    "completed_at": "2025-01-15T10:00:45",
    "duration_ms": 45200.0,
    "status": "complete"
  }
]
```

| `status` value | Meaning |
|---|---|
| `running` | Query is currently in progress |
| `paused` | Query is paused waiting for HITL clarification |
| `complete` | Query finished successfully |
| `error` | Query encountered an error |

---

### GET `/threads/{thread_id}/clarification`

Check whether a thread is currently paused waiting for HITL input. Use this on page load to detect and restore a pending clarification state.

**Response**: See [HITL section](#4-hitl--human-in-the-loop) above.

---

## 6. Health Check

### GET `/health`

**Response**
```json
{
  "status": "ok",
  "services": {
    "neo4j": {
      "status": "connected",
      "latency_ms": 12,
      "detail": "nokia_syn_v1 — 5 node labels"
    },
    "postgres": {
      "status": "connected",
      "latency_ms": 3,
      "detail": "pwc_simulation_agent_schema"
    },
    "openai": {
      "status": "connected",
      "latency_ms": 210,
      "detail": "gpt-4o"
    }
  }
}
```

`status` is `"ok"` when all services are connected, `"degraded"` otherwise.

---

## 7. TypeScript Types

```typescript
// ── Simulate ─────────────────────────────────────────────────────────────────

interface SimulateRequest {
  user_id: string;
  query: string;
  thread_id?: string;
}

interface ClarificationPayload {
  type: string;
  original_query: string;
  questions: string[];
  assumptions_if_skipped: string[];
  message: string;
}

interface SimulateResponse {
  status: "complete" | "clarification_needed";
  thread_id: string;
  final_response: string;
  routing_decision: "greeting" | "traversal" | "simulation" | "";
  planner_steps: string[];
  planning_rationale: string;
  traversal_steps: number;
  data_summary: Record<string, unknown>;
  calculations: string;
  errors: string[];
  clarification: ClarificationPayload | null;
  messages: Record<string, unknown>[];
}

interface ResumeRequest {
  thread_id: string;
  clarification: string;
}

// ── SSE ───────────────────────────────────────────────────────────────────────

interface StreamStartedEvent {
  query_id: string;
  thread_id: string;
}

interface QueryRefinerCompleteEvent {
  refined_query: string;
}

interface OrchestratorCompleteEvent {
  routing_decision: "greeting" | "traversal" | "simulation";
}

interface PlannerCompleteEvent {
  planner_steps: string[];
}

interface TraversalCompleteEvent {
  traversal_steps: number;
}

interface ResponseCompleteEvent {
  final_response: string;
}

interface HitlStartEvent {
  type: string;
  original_query: string;
  questions: string[];
  assumptions_if_skipped: string[];
  message: string;
}

interface HitlCompleteEvent {
  answer: string;
}

interface StreamCompleteEvent {
  final_response: string;
  routing_decision: string;
  planner_steps: string[];
}

interface StreamErrorEvent {
  message: string;
}

// ── Threads ───────────────────────────────────────────────────────────────────

interface ThreadSummary {
  thread_id: string;
  user_id: string;
  created_at: string;
  last_active_at: string;
  status: "active";
  total_queries: number;
}

interface MessageRecord {
  query_id: string;
  thread_id: string;
  user_id: string;
  original_query: string;
  refined_query: string | null;
  routing_decision: string | null;
  planning_rationale: string[] | null;
  final_response: string | null;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  status: "running" | "paused" | "complete" | "error";
}

interface ClarificationStatus {
  is_paused: boolean;
  clarification_id?: string;
  query_id?: string;
  questions_asked?: string[];
  assumptions_offered?: string[];
  asked_at?: string;
}
```

---

## 8. Integration Patterns

### Standard request/response (simple integration)

```
1. POST /simulate { user_id, query, thread_id? }
   → { status: "clarification_needed" }  OR  { status: "complete" }
2. If clarification_needed: show questions → POST /simulate/resume
3. Store thread_id for conversation continuity
```

### SSE streaming (recommended — real-time progress)

```
1. GET /simulate/stream?query=...&user_id=...&thread_id=...
2. Listen for events → update progress UI step by step
3. If hitl_start: show clarification UI (keep EventSource open)
   → POST /simulate/stream/resume
4. On complete: render final_response, close EventSource
```

### Thread ID management

- Generate a new `thread_id` (UUID) at the start of each new conversation
- Pass the same `thread_id` for follow-up messages in the same conversation
- The `stream_started` event always echoes back the `thread_id` — use this if you let the server generate it
- Store `thread_id` persistently (localStorage / session) to support page refresh recovery

### Page refresh mid-HITL

```
1. On app load: GET /threads/{thread_id}/clarification
2. If is_paused === true: restore HITL UI with questions_asked and assumptions_offered
3. User answers → POST /simulate/resume (NOT /simulate/stream/resume — the stream is gone after refresh)
```

### Error handling

| HTTP Status | Meaning |
|---|---|
| 400 | Bad request — empty query or clarification |
| 404 | Thread not found, or no active SSE stream for resume |
| 422 | Missing required field (user_id, query, etc.) |
| 500 | Internal server error — check backend logs |

For SSE, errors arrive as `event: error` with `{ message: string }`. The stream closes after an error event — close your `EventSource` in the handler.

---

## Quick Reference

```
Standard:
  POST   /api/v1/simulate                      → run simulation
  POST   /api/v1/simulate/resume               → HITL resume

SSE:
  GET    /api/v1/simulate/stream               → stream simulation events
  POST   /api/v1/simulate/stream/resume        → resume paused SSE stream

Threads:
  GET    /api/v1/threads?user_id=X             → list user's threads
  GET    /api/v1/threads/:id                   → thread metadata
  DELETE /api/v1/threads/:id                   → delete thread
  GET    /api/v1/threads/:id/messages          → query history
  GET    /api/v1/threads/:id/clarification     → HITL pause status

Utility:
  GET    /api/v1/health                        → service status
```
