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

# Per-table top_k overrides — increase KPI & QA for richer context,
# keep simulation low (scenarios are large, only best match matters).
_TABLE_TOP_K: dict[str, int] = {
    "kpi":           5,
    "question_bank": 5,
    "simulation":    1,
    "keywords":      5,
}

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
            # print(f"RESULTS ARE AS FOLLOWS: {results}")
            return results

        except requests.exceptions.ConnectionError as exc:
            print(
                f"⚠ Semantic search [{table}]: Cannot reach {self._base_url} — {exc}"
            )
            logger.warning(
                "Semantic search [%s]: Cannot reach %s — %s",
                table, self._base_url, exc,
            )
        except requests.exceptions.Timeout:
            print(f"⚠ Semantic search [{table}]: Timed out after {_REQUEST_TIMEOUT}s")
            logger.warning(
                "Semantic search [%s]: Request timed out after %ds", table, _REQUEST_TIMEOUT
            )
        except requests.exceptions.HTTPError as exc:
            print(f"⚠ Semantic search [{table}]: HTTP error — {exc}")
            logger.warning("Semantic search [%s]: HTTP error — %s", table, exc)
        except Exception as exc:
            print(f"⚠ Semantic search [{table}]: Unexpected error — {exc}")
            logger.warning("Semantic search [%s]: Unexpected error — %s", table, exc)

        return []

    # ── Keywords API call ────────────────────────────────────────────────────

    def _search_keywords(self, query: str, top_k: int = _TABLE_TOP_K["keywords"]) -> list[dict]:
        """
        Call the semantic keywords search API endpoint.
        Returns results in the same shape as _search (list of dicts with
        content + similarity_score), or an empty list on error.
        """
        url = f"{self._base_url}/api/v1/semantic/keywords/search"
        payload = {"query": query, "top_k": top_k}

        try:
            resp = self._session.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            results: list[dict] = resp.json().get("results", [])
            logger.info(
                "Semantic search [keywords]: %d result(s) for query: %.80s",
                len(results), query,
            )
            return results

        except requests.exceptions.ConnectionError as exc:
            print(f"⚠ Semantic search [keywords]: Cannot reach {self._base_url} — {exc}")
            logger.warning("Semantic search [keywords]: Cannot reach %s — %s", self._base_url, exc)
        except requests.exceptions.Timeout:
            print(f"⚠ Semantic search [keywords]: Timed out after {_REQUEST_TIMEOUT}s")
            logger.warning("Semantic search [keywords]: Request timed out after %ds", _REQUEST_TIMEOUT)
        except requests.exceptions.HTTPError as exc:
            print(f"⚠ Semantic search [keywords]: HTTP error — {exc}")
            logger.warning("Semantic search [keywords]: HTTP error — %s", exc)
        except Exception as exc:
            print(f"⚠ Semantic search [keywords]: Unexpected error — {exc}")
            logger.warning("Semantic search [keywords]: Unexpected error — %s", exc)

        return []

    # ── High-level: query all tables ───────────────────────────────────────

    def get_all_context(
        self,
        query: str,
        top_k: int | None = None,
    ) -> dict[str, list[dict]]:
        """
        Query kpi, question_bank, simulation tables and semantic_keywords
        concurrently.

        Uses per-table top_k from _TABLE_TOP_K by default.  Pass an explicit
        top_k to override all tables uniformly.

        Returns:
            {
                "kpi":           [...],
                "question_bank": [...],
                "simulation":    [...],
                "keywords":      [...],
            }
        Each list contains result dicts (may be empty on error).
        """
        from concurrent.futures import ThreadPoolExecutor

        workers = len(_TABLES) + 1  # +1 for keywords search
        with ThreadPoolExecutor(max_workers=workers) as executor:
            # External API searches (per-table top_k)
            futures: dict[str, Any] = {
                table: executor.submit(
                    self._search, query, table,
                    top_k if top_k is not None else _TABLE_TOP_K.get(table, _DEFAULT_TOP_K),
                )
                for table in _TABLES
            }
            # Local PostgreSQL keyword embedding search
            futures["keywords"] = executor.submit(
                self._search_keywords, query, top_k=10,
            )

        return {key: fut.result() for key, fut in futures.items()}

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
        kw_results  = context.get("keywords", [])

        if not any([kpi_results, qb_results, sim_results, kw_results]):
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

        # ── Semantic Keywords section ──
        if kw_results:
            lines.append("### Matched Domain Keywords")
            lines.append(
                "These keywords from the domain knowledge base matched the query. "
                "Use `mapped_table_columns` for correct table/column references "
                "and `logic` for computation guidance."
            )
            lines.append("")
            for r in kw_results:
                content: dict[str, Any] = r.get("content") or {}
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                kw_name = content.get("keyword_name", r.get("keyword_name", "?"))
                kw_id = content.get("keyword_id", r.get("id", "?"))
                lines.append(f"**{kw_name}** (ID: {kw_id}, similarity: {score})")
                if content.get("keyword_description"):
                    lines.append(f"  - **Description**: {content['keyword_description']}")
                if content.get("mapped_table_columns"):
                    lines.append(f"  - **Tables/Columns**: {content['mapped_table_columns']}")
                if content.get("logic"):
                    lines.append(f"  - **Logic**: {content['logic']}")
                if content.get("synonyms"):
                    lines.append(f"  - **Synonyms**: {content['synonyms']}")
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
