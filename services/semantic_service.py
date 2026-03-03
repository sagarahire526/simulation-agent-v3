"""
Semantic similarity service for matching user questions to pre-defined scenarios.

Uses OpenAI text-embedding-3-small to create embeddings and cosine similarity
to find matching scenarios from pwc_semantic_information_schema.semantics_simulation.

Threshold: 70% cosine similarity (configurable via SIMILARITY_THRESHOLD).
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import psycopg2

import config

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
SIMILARITY_THRESHOLD = 0.70  # 70% threshold
_TABLE = "pwc_semantic_information_schema.semantics_simulation"


class SemanticService:
    """
    Handles embedding creation and semantic similarity search against the
    scenario knowledge base stored in PostgreSQL.
    """

    def __init__(self):
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._openai_client = None

    # ── OpenAI client ──────────────────────────────────────────────────────

    def _get_client(self):
        if self._openai_client is None:
            from openai import OpenAI
            self._openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
        return self._openai_client

    # ── PostgreSQL connection ──────────────────────────────────────────────

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(
                host=config.PG_HOST,
                port=config.PG_PORT,
                database=config.PG_DATABASE,
                user=config.PG_USER,
                password=config.PG_PASSWORD,
            )
        return self._conn

    # ── Embedding ──────────────────────────────────────────────────────────

    def create_embedding(self, text: str) -> list[float]:
        """Create an embedding vector for the given text using text-embedding-3-small."""
        response = self._get_client().embeddings.create(
            model=EMBEDDING_MODEL,
            input=text.strip(),
        )
        return response.data[0].embedding

    # ── Cosine similarity ──────────────────────────────────────────────────

    @staticmethod
    def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
        """Compute cosine similarity between two vectors. Returns value in [0, 1]."""
        a = np.array(vec1, dtype=np.float64)
        b = np.array(vec2, dtype=np.float64)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    # ── Semantic search ────────────────────────────────────────────────────

    def search_similar_scenarios(
        self,
        question: str,
        threshold: float = SIMILARITY_THRESHOLD,
    ) -> list[dict]:
        """
        Search for scenarios similar to the given question.

        Embeds the question, compares against all stored scenario embeddings
        using cosine similarity, and returns rows with similarity >= threshold.

        Returns:
            List of matching scenario dicts sorted by similarity (highest first).
            Each dict contains: scenario_id, scenario, data_phase_steps,
            data_phase_questions, calculation_phase_steps, simulator_phase_steps,
            simulation_methodology, similarity_score.
        """
        try:
            query_embedding = self.create_embedding(question)
        except Exception as e:
            logger.error("Failed to create embedding for question: %s", e)
            return []

        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(f"""
                SELECT scenario_id, scenario,
                       data_phase_steps, data_phase_questions,
                       calculation_phase_steps, simulator_phase_steps,
                       simulation_methodology, embedding
                FROM {_TABLE}
                WHERE embedding IS NOT NULL
            """)
            rows = cur.fetchall()
            cur.close()
        except Exception as e:
            logger.error("Failed to fetch scenarios from DB: %s", e)
            return []

        matches = []
        for row in rows:
            stored_emb = row[7]
            if not stored_emb:
                continue
            sim = self._cosine_similarity(query_embedding, list(stored_emb))
            if sim >= threshold:
                matches.append({
                    "scenario_id": row[0],
                    "scenario": row[1] or "",
                    "data_phase_steps": row[2] or [],
                    "data_phase_questions": row[3] or [],
                    "calculation_phase_steps": row[4] or [],
                    "simulator_phase_steps": row[5] or [],
                    "simulation_methodology": row[6] or "",
                    "similarity_score": round(sim, 4),
                })

        matches.sort(key=lambda x: x["similarity_score"], reverse=True)

        logger.info(
            "Semantic search: %d/%d scenarios above %.0f%% threshold for query: %.80s",
            len(matches), len(rows), threshold * 100, question,
        )
        return matches

    # ── Context formatting ─────────────────────────────────────────────────

    def format_scenario_context(self, scenarios: list[dict]) -> str:
        """
        Format matched scenario rows into a structured context string
        to be injected into the traversal agent's system prompt.
        """
        if not scenarios:
            return ""

        lines = [
            "## Matched Scenario Guidance (from Semantic Knowledge Base)",
            "",
            "The following pre-defined scenario(s) closely match the user's question.",
            "Use the Data Phase Steps and Questions below to guide your data retrieval,",
            "the Calculation Phase Steps to plan computations, and the Simulation",
            "Methodology to structure your final output.",
            "",
        ]

        for i, s in enumerate(scenarios, 1):
            lines.append(
                f"### Scenario {i} — ID {s['scenario_id']} "
                f"(Similarity: {s['similarity_score'] * 100:.1f}%)"
            )
            lines.append(f"**Scenario**: {s['scenario']}")
            lines.append("")

            if s["data_phase_questions"]:
                lines.append("**Data Phase Questions** (key questions to answer in order):")
                for q in s["data_phase_questions"]:
                    if q and q.strip():
                        lines.append(f"  - {q.strip()}")
                lines.append("")

            if s["data_phase_steps"]:
                lines.append("**Data Phase Steps** (how to retrieve the required data):")
                for step in s["data_phase_steps"]:
                    if step and step.strip():
                        lines.append(f"  - {step.strip()}")
                lines.append("")

            if s["calculation_phase_steps"]:
                lines.append("**Calculation Phase Steps** (computations to perform):")
                for step in s["calculation_phase_steps"]:
                    if step and step.strip():
                        lines.append(f"  - {step.strip()}")
                lines.append("")

            if s["simulator_phase_steps"]:
                lines.append("**Simulator Phase Steps** (simulation plan):")
                for step in s["simulator_phase_steps"]:
                    if step and step.strip():
                        lines.append(f"  - {step.strip()}")
                lines.append("")

            if s["simulation_methodology"]:
                lines.append(
                    f"**Expected Output Format / Methodology**: {s['simulation_methodology']}"
                )
                lines.append("")

            lines.append("─" * 60)
            lines.append("")

        return "\n".join(lines)

    def format_simulation_guidance(self, scenarios: list[dict]) -> str:
        """
        Format the simulation-side guidance from the best-matched scenario:
        Calculation Phase Steps, Simulator Phase Steps, and Simulation Methodology.

        This is passed to the Response Agent (not the Traversal Agent) so it
        understands how to structure calculations and the final output view.
        Uses only the top-scoring match.
        """
        if not scenarios:
            return ""

        s = scenarios[3]  # best match only
        lines = [
            "## Matched Scenario — Simulation Guidance (Reference Only)",
            f"*Scenario ID {s['scenario_id']} · Similarity {s['similarity_score'] * 100:.1f}%*",
            f"*Scenario: {s['scenario']}*",
            "",
        ]

        if s["calculation_phase_steps"]:
            lines.append("### Calculation Phase Steps")
            lines.append("*(How the data should be computed — adapt to what was actually retrieved)*")
            for step in s["calculation_phase_steps"]:
                if step and step.strip():
                    lines.append(f"- {step.strip()}")
            lines.append("")

        if s["simulator_phase_steps"]:
            lines.append("### Simulator Phase Steps")
            lines.append("*(The simulation plan this type of query typically follows)*")
            for step in s["simulator_phase_steps"]:
                if step and step.strip():
                    lines.append(f"- {step.strip()}")
            lines.append("")

        if s["simulation_methodology"]:
            lines.append("### Expected Output Methodology")
            lines.append("*(The format and structure the final response should follow)*")
            lines.append(s["simulation_methodology"])
            lines.append("")

        return "\n".join(lines)
