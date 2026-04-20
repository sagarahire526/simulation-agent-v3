# Frontend Handoff: Planner HITL (Step Review)

## What Changed

The planner phase now **pauses before executing** so the user can review, edit, reorder, remove, or add analysis steps. Previously, steps were auto-executed immediately.

---

## New SSE Event Flow

### Before (old)
```
stream_started → query_refiner_complete → orchestrator_complete → schema_complete
  → planner_plan_ready → planner_step_complete (x N) → planner_complete
  → response_complete → complete
```

### After (new)
```
stream_started → query_refiner_complete → orchestrator_complete → schema_complete
  → planner_plan_ready → planner_plan_complete
  → planner_hitl_start        ← NEW: pause here, show step editor
  [user edits steps, frontend calls POST /stream/resume/planner]
  → planner_hitl_complete      ← NEW: user approved, execution begins
  → planner_step_complete (x N) → planner_execute_complete
  → response_complete → complete
```

### With Both HITLs (query was incomplete + simulation route)
```
stream_started → query_refiner_complete → hitl_start
  [user provides clarification via POST /stream/resume]
  → hitl_complete → orchestrator_complete → schema_complete
  → planner_plan_ready → planner_plan_complete → planner_hitl_start
  [user reviews steps via POST /stream/resume/planner]
  → planner_hitl_complete → planner_step_complete (x N) → ...
  → complete
```

### Traversal route (no planner, unchanged)
```
stream_started → ... → schema_complete → traversal_complete → response_complete → complete
```

---

## New SSE Events

### `planner_hitl_start`

Fired when the planner has produced steps and is waiting for user approval.

```json
{
  "type": "planner_review",
  "steps": [
    "Sub-query 1: Retrieve completed and not-completed site counts by region using Workfront KPI",
    "Sub-query 2: Retrieve GC capacity data for CENTRAL and SOUTH regions",
    "Sub-query 3: Compute historical run rate per region for last 12 weeks"
  ],
  "display_steps": [
    "Retrieve completed and not-completed site counts by region using Workfront KPI",
    "Retrieve GC capacity data for CENTRAL and SOUTH regions",
    "Compute historical run rate per region for last 12 weeks"
  ],
  "rationale": "The simulation requires baseline counts, capacity data, and run rates to model crew reallocation impact.",
  "message": "Review the planned analysis steps below. You may approve as-is, edit step text, remove, reorder, or add steps."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | `string` | Always `"planner_review"` |
| `steps` | `string[]` | Raw step strings (prefixed with `Sub-query N: ...`) — **these are what you send back** |
| `display_steps` | `string[]` | Clean display text (prefix stripped) — use for UI rendering |
| `rationale` | `string` | Why the planner chose these steps |
| `message` | `string` | Instruction text to show the user |

### `planner_hitl_complete`

Fired after the user approves/modifies steps and execution is about to begin.

```json
{
  "steps": ["Sub-query 1: ...", "Sub-query 2: ..."]
}
```

---

## Changed SSE Events

| Event | Change |
|-------|--------|
| `planner_complete` | **Removed.** Replaced by `planner_plan_complete` and `planner_execute_complete` |
| `planner_plan_complete` | **New.** Fires after the LLM produces the plan (before HITL pause). Data: `{"planner_steps": [...]}` |
| `planner_execute_complete` | **New.** Fires after all traversal steps finish. Data: `{"traversal_steps": <total_tool_calls>}` |
| `planner_step_complete` | **Unchanged.** Still fires per-step during execution. |
| `planner_plan_ready` | **Unchanged.** Still fires with step list and rationale. |

---

## New API Endpoints

### `POST /api/v1/simulate/stream/resume/planner`

Resume after planner HITL (SSE streaming path).

**Request:**
```json
{
  "thread_id": "abc-123-def",
  "planner_steps": [
    "Sub-query 1: Retrieve completed and not-completed site counts by region",
    "Sub-query 2: Retrieve GC capacity data for CENTRAL and SOUTH regions"
  ]
}
```

**Response:**
```json
{
  "status": "resumed",
  "thread_id": "abc-123-def"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `thread_id` | `string` | Yes | From `stream_started` event |
| `planner_steps` | `string[]` | Yes | The approved/modified steps. User can edit text, remove, reorder, or add new steps. |

### `POST /api/v1/simulate/resume/planner`

Resume after planner HITL (non-SSE path).

Same request body as above. Returns full `SimulateResponse`.

---

## Existing Endpoints (unchanged)

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/simulate/stream/resume` | Resume after query_refiner HITL (body: `{thread_id, clarification}`) |
| `POST /api/v1/simulate/resume` | Resume after query_refiner HITL, non-SSE (body: `{thread_id, clarification}`) |

---

## Frontend Implementation Guide

### 1. Detect the HITL type

```javascript
eventSource.addEventListener('hitl_start', (e) => {
  // Query refiner HITL — show clarification dialog (existing)
  const data = JSON.parse(e.data);
  showClarificationDialog(data.questions, data.assumptions_if_skipped);
});

eventSource.addEventListener('planner_hitl_start', (e) => {
  // Planner HITL — show step editor (NEW)
  const data = JSON.parse(e.data);
  showStepEditor(data.steps, data.display_steps, data.rationale, data.message);
});
```

### 2. Build the step editor UI

Show an editable list where each item is a step from `display_steps`. Allow:
- **Edit**: modify step text inline
- **Remove**: delete a step (X button)
- **Reorder**: drag-and-drop
- **Add**: button to add a new step

Show `rationale` as context above the list.
Show `message` as instruction text.

### 3. Submit approved steps

When user clicks "Approve" or "Execute":

```javascript
// Re-prefix steps if user edited display_steps
const approvedSteps = editedSteps.map((text, i) => `Sub-query ${i + 1}: ${text}`);

await fetch('/api/v1/simulate/stream/resume/planner', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    thread_id: threadId,
    planner_steps: approvedSteps,
  }),
});
```

**Important:** Send back the full step strings with `Sub-query N:` prefix. If the user didn't modify anything, send the original `steps` array from the event data (not `display_steps`).

### 4. Approve as-is (no changes)

Just send the original `steps` array unchanged:

```javascript
await fetch('/api/v1/simulate/stream/resume/planner', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    thread_id: threadId,
    planner_steps: originalSteps,  // from planner_hitl_start event
  }),
});
```

### 5. Track execution after approval

After resuming, the same existing events fire:

```javascript
eventSource.addEventListener('planner_step_complete', (e) => {
  const data = JSON.parse(e.data);
  // data.step_index, data.step_total, data.step_query, data.status, data.error
  updateStepProgress(data.step_index, data.status);
});

eventSource.addEventListener('planner_execute_complete', (e) => {
  // All steps done, response agent is synthesizing
});
```

---

## SimulateResponse Changes (non-SSE path)

New possible `status` value: `"plan_review_needed"`

```json
{
  "status": "plan_review_needed",
  "final_response": "",
  "thread_id": "abc-123",
  "errors": [],
  "routing_decision": "simulation",
  "planner_steps": ["Sub-query 1: ...", "Sub-query 2: ..."],
  "planner_review": {
    "type": "planner_review",
    "steps": ["Sub-query 1: ...", "Sub-query 2: ..."],
    "display_steps": ["...", "..."],
    "rationale": "...",
    "message": "Review the planned analysis steps below..."
  }
}
```

| Status | Meaning | Resume endpoint |
|--------|---------|-----------------|
| `"complete"` | Done, has `final_response` | — |
| `"clarification_needed"` | Query refiner paused | `POST /simulate/resume` |
| `"plan_review_needed"` | Planner paused for step review | `POST /simulate/resume/planner` |
