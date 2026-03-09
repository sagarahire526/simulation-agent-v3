"""
Semantic search service — calls the internal PM Copilot semantic search API
to retrieve relevant context from KPI, question_bank, and simulation tables.

API endpoint: POST /api/v1/semantic/search
Note: Only accessible within the company network.

Request body:
    { "query": str, "table": "kpi"|"question_bank"|"simulation", "top_k": int }

Response (200):
    {
        "query": str,
        "total_results": int,
        "results": [
            { "table": str, "id": int, "content": {...}, "similarity_score": float }
        ]
    }
"""
from __future__ import annotations

import logging
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_TABLES = ("kpi", "question_bank", "simulation")
_DEFAULT_TOP_K = 2
_REQUEST_TIMEOUT = 15  # seconds

# Known structured keys inside the simulation table's content dict
_SIMULATION_CONTENT_KEYS: dict[str, str] = {
    "scenario":                "Scenario Description",
    "data_phase_questions":    "Data Phase Questions",
    "data_phase_steps":        "Data Phase Steps",
    "calculation_phase_steps": "Calculation Phase Steps",
    "simulator_phase_steps":   "Simulator Phase Steps",
    "simulation_methodology":  "Simulation Methodology",
}


class SemanticService:
    """
    Client for the internal PM Copilot semantic search API.

    Queries kpi, question_bank, and simulation tables and formats
    the results as structured context strings for the traversal and
    response agents. Gracefully degrades when the API is unreachable
    (e.g., outside the company network).
    """

    def __init__(self):
        self._base_url = config.SEMANTIC_SEARCH_URL.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "accept": "application/json",
            "Content-Type": "application/json",
        })

    # ── Low-level API call ─────────────────────────────────────────────────

    def _search(self, query: str, table: str, top_k: int = _DEFAULT_TOP_K) -> list[dict]:
        """
        Call the semantic search API for a single table.
        Returns an empty list on any error so the agent can proceed without context.
        """
        url = f"{self._base_url}/api/v1/semantic/search"
        payload = {"query": query, "table": table, "top_k": top_k}

        try:
            resp = self._session.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            results: list[dict] = resp.json().get("results", [])
            logger.info(
                "Semantic search [%s]: %d result(s) for query: %.80s",
                table, len(results), query,
            )
            return results

        except requests.exceptions.ConnectionError:
            logger.warning(
                "Semantic search [%s]: Cannot reach %s — are you on the company network?",
                table, self._base_url,
            )
        except requests.exceptions.Timeout:
            logger.warning(
                "Semantic search [%s]: Request timed out after %ds", table, _REQUEST_TIMEOUT
            )
        except requests.exceptions.HTTPError as exc:
            logger.warning("Semantic search [%s]: HTTP error — %s", table, exc)
        except Exception as exc:
            logger.warning("Semantic search [%s]: Unexpected error — %s", table, exc)

        return []

    # ── High-level: query all tables ───────────────────────────────────────

    def get_all_context(
        self,
        query: str,
        top_k: int = _DEFAULT_TOP_K,
    ) -> dict[str, list[dict]]:
        """
        Query kpi, question_bank, and simulation tables concurrently.

        Returns:
            {
                "kpi":           [...],
                "question_bank": [...],
                "simulation":    [...],
            }
        Each list contains result dicts from the API (may be empty on error).
        """
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(_TABLES)) as executor:
            futures = {
                table: executor.submit(self._search, query, table, top_k)
                for table in _TABLES
            }
        return {table: fut.result() for table, fut in futures.items()}

    # ── Context formatting ─────────────────────────────────────────────────

    def format_traversal_context(self, context: dict[str, list[dict]]) -> str:
        """
        Format all semantic search results into a structured context block
        to be injected into the Traversal Agent's system prompt.

        Sections: KPI context → Question Bank examples → Simulation scenarios.
        Returns an empty string when no results are available.
        """
        kpi_results = context.get("kpi", [])
        qb_results  = context.get("question_bank", [])
        sim_results = context.get("simulation", [])

        if not any([kpi_results, qb_results, sim_results]):
            return ""

        lines: list[str] = [
            "## Semantic Context (from Internal Knowledge Base)",
            "The following was retrieved via semantic similarity search against "
            "the user's query. Use it to guide your data retrieval strategy.",
            "",
        ]

        # ── KPI section ──
        if kpi_results:
            lines.append("### Relevant KPIs")
            for r in kpi_results:
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                lines.append(f"**KPI #{r.get('id', '?')}** (similarity: {score})")
                for k, v in (r.get("content") or {}).items():
                    if v:
                        lines.append(f"  - **{k}**: {v}")
                lines.append("")

        # ── Question Bank section ──
        if qb_results:
            lines.append("### Relevant Questions from Knowledge Base")
            lines.append(
                "These pre-answered questions are semantically similar to the user's query. "
                "Use them to understand expected data shape and calculations."
            )
            lines.append("")
            for r in qb_results:
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                lines.append(f"**Q&A #{r.get('id', '?')}** (similarity: {score})")
                for k, v in (r.get("content") or {}).items():
                    if v:
                        lines.append(f"  - **{k}**: {v}")
                lines.append("")

        # ── Simulation Scenario section ──
        if sim_results:
            lines.append("### Matched Simulation Scenarios")
            lines.append(
                "These pre-defined scenarios closely match the query. "
                "Follow the Data Phase Questions/Steps as your primary retrieval roadmap "
                "before exploring freely."
            )
            lines.append("")
            for i, r in enumerate(sim_results, 1):
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                lines.append(
                    f"**Scenario {i} — ID {r.get('id', '?')}** (similarity: {score})"
                )
                content: dict[str, Any] = r.get("content") or {}
                rendered: set[str] = set()

                # Render known structured keys in a logical order
                for key, label in _SIMULATION_CONTENT_KEYS.items():
                    val = content.get(key)
                    if not val:
                        continue
                    rendered.add(key)
                    if isinstance(val, list):
                        lines.append(f"  **{label}**:")
                        for item in val:
                            if str(item).strip():
                                lines.append(f"    - {item}")
                    else:
                        lines.append(f"  **{label}**: {val}")

                # Any remaining keys not in the known set
                for k, v in content.items():
                    if k not in rendered and v:
                        lines.append(f"  **{k}**: {v}")

                lines.append("")

        lines.append("─" * 60)
        return "\n".join(lines)

    def format_simulation_guidance(self, context: dict[str, list[dict]]) -> str:
        """
        Extract simulation guidance (calculation steps, simulator steps, methodology)
        from the best-matched simulation scenario result.

        This is passed to the Response Agent so it knows how to structure
        calculations and the final output. Returns empty string if no match.
        """
        sim_results = context.get("simulation", [])
        if not sim_results:
            return ""

        best    = sim_results[0]  # highest similarity
        content = best.get("content") or {}
        score   = f"{best.get('similarity_score', 0) * 100:.1f}%"

        lines: list[str] = [
            "## Matched Scenario — Simulation Guidance (Reference Only)",
            f"*Scenario ID {best.get('id', '?')} · Similarity {score}*",
            f"*Scenario: {content.get('scenario', 'N/A')}*",
            "",
        ]

        calc_steps: list = content.get("calculation_phase_steps", [])
        if calc_steps:
            lines.append("### Calculation Phase Steps")
            lines.append("*(Adapt to what was actually retrieved)*")
            for step in calc_steps:
                if str(step).strip():
                    lines.append(f"- {step}")
            lines.append("")

        sim_steps: list = content.get("simulator_phase_steps", [])
        if sim_steps:
            lines.append("### Simulator Phase Steps")
            for step in sim_steps:
                if str(step).strip():
                    lines.append(f"- {step}")
            lines.append("")

        methodology: str = content.get("simulation_methodology", "")
        if methodology:
            lines.append("### Expected Output Methodology")
            lines.append(methodology)
            lines.append("")

        return "\n".join(lines)
