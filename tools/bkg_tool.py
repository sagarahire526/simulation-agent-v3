# bkg_tool.py
import re
import json
from neo4j import GraphDatabase
import config


class BKGTool:
    """
    Agent's understanding layer — backed by Neo4j.

    Labels in Neo4j:
      - ConceptNode  → business entities  (node_id property)
      - MetricNode   → computed metrics   (metric_id property)

    Relationships are typed (HAS_PROJECT, LOCATED_IN, MEASURES, etc.)

    No separate alias table in your schema — aliases are handled in-memory
    from a hardcoded map (GC → GeneralContractor, etc.) since your Cypher
    script has no BKGAlias nodes.
    """

    # Hardcoded aliases since your ingestion script has no alias nodes
    STATIC_ALIASES = {
        "GC": "GeneralContractor",
        "gc": "GeneralContractor",
        "generalcontractor": "GeneralContractor",
        "NAS": "NASSession",
        "nas": "NASSession",
        "IX": "Integration",
        "ix": "Integration",
        "CX": "ConstructionProgress",
        "cx": "TowerConstruction",
        "BOM": "BillOfMaterials",
        "bom": "BillOfMaterials",
        "NTP": "NTP",
        "SSV": "Acceptance",
        "COP": "Acceptance",
    }

    def __init__(self):
        self._driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        )
        self._driver.verify_connectivity()
        self.aliases = self.STATIC_ALIASES.copy()

        # ── Startup diagnostics ──
        concept_count = self._run("MATCH (n:ConceptNode) RETURN count(n) AS cnt")[0]["cnt"]
        metric_count  = self._run("MATCH (m:MetricNode)  RETURN count(m) AS cnt")[0]["cnt"]
        self._node_count = concept_count + metric_count

        # Print actual property keys from first node so we can catch mismatches
        sample = self._run("MATCH (n:ConceptNode) RETURN keys(n) AS k, n.node_id AS nid LIMIT 1")
        if sample:
            print(f"✓ Neo4j: Found {concept_count+metric_count} Nodes!")
        else:
            print("✗ Neo4j: ConceptNode label exists but NO NODES FOUND — check your database name")
            print(f"  Connected to: {config.NEO4J_URI}")

    @property
    def nodes(self):
        """Compatibility shim for health check: returns count as len-able."""
        return range(self._node_count)

    def close(self):
        self._driver.close()

    def resolve_id(self, raw_id: str) -> str:
        """Resolve alias to canonical node_id. Returns as-is if already canonical."""
        return self.aliases.get(raw_id, self.aliases.get(raw_id.upper(), raw_id))

    def query(self, request: dict) -> dict:
        mode = request.get("mode")
        try:
            if mode == "get_node":
                return self._get_node(request.get("node_id", ""))
            elif mode == "find_relevant":
                return self._find_relevant(request.get("question", ""))
            elif mode == "traverse":
                return self._traverse(
                    request.get("start", ""),
                    request.get("depth", 2),
                    request.get("rel_type"),
                )
            elif mode == "diagnostic":
                return self._get_diagnostic(request.get("metric_id", ""))
            elif mode == "schema":
                return self._get_schema(request.get("table_name"))
            else:
                return {"error": f"Unknown mode: {mode}"}
        except Exception as e:
            return {"error": str(e)}

    # ── Private helpers ──────────────────────────────────────────────────────

    def _run(self, cypher: str, **params) -> list:
        with self._driver.session(database=config.NEO4J_DATABASE) as session:
            result = session.run(cypher, **params)
            return [r.data() for r in result]

    def _node_props_to_dict(self, props: dict) -> dict:
        """
        Clean up raw Neo4j property bag.
        - Parse JSON strings back to dicts/lists where applicable.
        - Strip Neo4j internal keys.
        """
        out = {}
        for k, v in props.items():
            if isinstance(v, str) and v.startswith(('{', '[')):
                try:
                    v = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    pass
            out[k] = v
        return out

    # ── Mode: get_node ───────────────────────────────────────────────────────

    def _get_node(self, raw_id: str) -> dict:
        node_id = self.resolve_id(raw_id)

        # Try ConceptNode first
        rows = self._run(
            "MATCH (n:ConceptNode {node_id: $nid}) RETURN properties(n) AS props",
            nid=node_id,
        )
        if rows:
            node = self._node_props_to_dict(rows[0]["props"])

            # Outgoing relationships
            out_rows = self._run(
                """
                MATCH (n:ConceptNode {node_id: $nid})-[r]->(t)
                RETURN type(r) AS relationship,
                       coalesce(t.node_id, t.metric_id) AS target,
                       properties(r) AS rel_props
                """,
                nid=node_id,
            )
            node["outgoing"] = [
                {
                    "relationship": r["relationship"],
                    "target": r["target"],
                    **{k: v for k, v in (r["rel_props"] or {}).items()},
                }
                for r in out_rows
            ]

            # Incoming relationships
            in_rows = self._run(
                """
                MATCH (s)-[r]->(n:ConceptNode {node_id: $nid})
                RETURN type(r) AS relationship,
                       coalesce(s.node_id, s.metric_id) AS source,
                       properties(r) AS rel_props
                """,
                nid=node_id,
            )
            node["incoming"] = [
                {
                    "relationship": r["relationship"],
                    "source": r["source"],
                    **{k: v for k, v in (r["rel_props"] or {}).items()},
                }
                for r in in_rows
            ]
            return node

        # Try MetricNode
        rows = self._run(
            "MATCH (m:MetricNode {metric_id: $mid}) RETURN properties(m) AS props",
            mid=node_id,
        )
        if rows:
            return self._node_props_to_dict(rows[0]["props"])

        return {
            "error": (
                f"'{raw_id}' not found (resolved to '{node_id}'). "
                "Try find_relevant to search by keyword."
            )
        }

    # ── Mode: find_relevant ──────────────────────────────────────────────────

    def _find_relevant(self, question: str) -> dict:
        """
        Keyword search over ConceptNode and MetricNode properties.
        Uses CONTAINS matching — no fulltext index required.
        Scores by how many words match across key fields.
        """
        q_words = list(set(re.findall(r'\w+', question.lower())))
        if not q_words:
            return {"relevant_nodes": [], "relevant_metrics": []}

        # ── ConceptNodes ──
        node_rows = self._run(
            """
            MATCH (n:ConceptNode)
            WHERE any(w IN $words
                WHERE toLower(coalesce(n.node_id, ''))    CONTAINS w
                   OR toLower(coalesce(n.definition, '')) CONTAINS w
                   OR toLower(coalesce(n.name, ''))       CONTAINS w
                   OR toLower(coalesce(n.domain, ''))     CONTAINS w
                   OR any(attr IN coalesce(n.attributes, [])
                          WHERE toLower(attr) CONTAINS w)
            )
            RETURN
                n.node_id       AS node_id,
                n.layer         AS layer,
                n.domain        AS domain,
                n.definition    AS definition,
                n.name          AS name,
                n.primary_table AS primary_table,
                coalesce(n.is_stub, false) AS is_stub
            LIMIT 8
            """,
            words=q_words,
        )

        node_results = []
        for r in node_rows:
            nid = r["node_id"]

            # Score: count how many q_words appear across key text fields
            text = " ".join(filter(None, [
                str(r.get("node_id") or ""),
                str(r.get("definition") or ""),
                str(r.get("name") or ""),
                str(r.get("domain") or ""),
            ])).lower()
            score = sum(1 for w in q_words if w in text)

            # Neighbor preview
            neighbors = self._run(
                """
                MATCH (n:ConceptNode {node_id: $nid})-[r]->(t)
                RETURN coalesce(t.node_id, t.metric_id) AS target
                LIMIT 5
                """,
                nid=nid,
            )

            node_results.append({
                "node_id": nid,
                "name": r.get("name"),
                "layer": r.get("layer"),
                "domain": r.get("domain"),
                "definition": (r.get("definition") or "")[:300],
                "primary_table": r.get("primary_table"),
                "is_stub": r.get("is_stub", False),
                "relevance_score": score,
                "neighbors_out": [n["target"] for n in neighbors],
                "neighbors_in": [],
            })

        # Sort by score descending
        node_results.sort(key=lambda x: x["relevance_score"], reverse=True)

        # ── MetricNodes ──
        metric_rows = self._run(
            """
            MATCH (m:MetricNode)
            WHERE any(w IN $words
                WHERE toLower(coalesce(m.metric_id, ''))  CONTAINS w
                   OR toLower(coalesce(m.definition, '')) CONTAINS w
                   OR toLower(coalesce(m.name, ''))       CONTAINS w
                   OR toLower(coalesce(m.domain, ''))     CONTAINS w
                   OR any(ref IN coalesce(m.references_nodes, [])
                          WHERE toLower(ref) CONTAINS w)
            )
            RETURN
                m.metric_id        AS metric_id,
                m.domain           AS domain,
                m.definition       AS definition,
                m.name             AS name,
                m.references_nodes AS references_nodes
            LIMIT 5
            """,
            words=q_words,
        )

        metric_results = []
        for r in metric_rows:
            text = " ".join(filter(None, [
                str(r.get("metric_id") or ""),
                str(r.get("definition") or ""),
                str(r.get("name") or ""),
                str(r.get("domain") or ""),
            ])).lower()
            score = sum(1 for w in q_words if w in text)

            metric_results.append({
                "metric_id": r["metric_id"],
                "name": r.get("name"),
                "domain": r.get("domain"),
                "definition": (r.get("definition") or "")[:300],
                "references_nodes": r.get("references_nodes") or [],
                "has_diagnostic": True,
                "relevance_score": score,
            })

        metric_results.sort(key=lambda x: x["relevance_score"], reverse=True)

        return {
            "relevant_nodes": node_results,
            "relevant_metrics": metric_results,
        }

    # ── Mode: traverse ───────────────────────────────────────────────────────

    def _traverse(self, raw_start: str, depth: int = 2, rel_type: str = None) -> dict:
        node_id = self.resolve_id(raw_start)
        depth = min(max(depth, 1), 4)

        rel_pattern = f"[r:{rel_type}*1..{depth}]" if rel_type else f"[r*1..{depth}]"

        exists = self._run(
            """
            MATCH (n)
            WHERE (n:ConceptNode AND n.node_id = $nid)
               OR (n:MetricNode  AND n.metric_id = $nid)
            RETURN n LIMIT 1
            """,
            nid=node_id,
        )
        if not exists:
            return {"error": f"Node '{raw_start}' (resolved: '{node_id}') not found"}

        rows = self._run(
            f"""
            MATCH (start)-{rel_pattern}->(end)
            WHERE (start:ConceptNode AND start.node_id = $nid)
               OR (start:MetricNode  AND start.metric_id = $nid)
            WITH start, r, end
            UNWIND r AS rel
            WITH startNode(rel) AS src, rel, endNode(rel) AS tgt
            RETURN
                coalesce(src.node_id, src.metric_id) AS from_node,
                type(rel)                             AS relationship,
                coalesce(tgt.node_id, tgt.metric_id) AS to_node,
                coalesce(rel.join_column, '')         AS join_sql,
                coalesce(tgt.domain, '')              AS domain,
                coalesce(tgt.definition, '')          AS definition,
                tgt.primary_table                     AS primary_table,
                tgt.base_query                        AS base_query,
                tgt.filters                           AS filters,
                coalesce(tgt.is_stub, false)          AS is_stub
            LIMIT 20
            """,
            nid=node_id,
        )

        paths = []
        discovered = {}
        for r in rows:
            if not r.get("to_node"):
                continue
            paths.append({
                "from": r["from_node"],
                "relationship": r["relationship"],
                "to": r["to_node"],
                "join_sql": r.get("join_sql", ""),
            })
            tgt = r["to_node"]
            if tgt not in discovered:
                discovered[tgt] = {
                    "node_id": tgt,
                    "domain": r.get("domain"),
                    "definition": (r.get("definition") or "")[:200],
                    "primary_table": r.get("primary_table"),
                    "base_query": r.get("base_query"),
                    "filters": r.get("filters"),
                    "is_stub": r.get("is_stub", False),
                }

        return {"paths": paths, "discovered_nodes": discovered}

    # ── Mode: diagnostic ─────────────────────────────────────────────────────

    def _get_diagnostic(self, raw_id: str) -> dict:
        metric_id = self.resolve_id(raw_id)

        rows = self._run(
            "MATCH (m:MetricNode {metric_id: $mid}) RETURN properties(m) AS props",
            mid=metric_id,
        )
        if rows:
            m = self._node_props_to_dict(rows[0]["props"])

            dt_rows = self._run(
                """
                MATCH (m:MetricNode {metric_id: $mid})-[r:DIAGNOSTIC_TRAVERSES]->(t)
                RETURN
                    coalesce(r.step, 0)       AS step,
                    coalesce(r.condition, '') AS condition,
                    t.metric_id               AS traverse_to_metric,
                    properties(t)             AS target_props
                ORDER BY r.step
                """,
                mid=metric_id,
            )

            diagnostic_tree = []
            for dt in dt_rows:
                tgt_props = self._node_props_to_dict(dt["target_props"] or {})
                diagnostic_tree.append({
                    "step": dt["step"],
                    "condition": dt["condition"],
                    "traverse_to": [dt["traverse_to_metric"]],
                    "description": tgt_props.get("definition", "")[:200],
                    "_target_summaries": {
                        dt["traverse_to_metric"]: {
                            "primary_table": tgt_props.get("primary_table"),
                            "base_query": tgt_props.get("base_query"),
                            "filters": tgt_props.get("filters"),
                        }
                    }
                })

            formulas = {
                k: v for k, v in m.items()
                if k.startswith("formula_sql_")
            }

            return {
                "metric_id": metric_id,
                "definition": m.get("definition"),
                "references_nodes": m.get("references_nodes", []),
                "formulas": formulas,
                "thresholds": m.get("thresholds", {}),
                "unit": m.get("unit"),
                "diagnostic_tree": diagnostic_tree,
            }

        related = self._run(
            """
            MATCH (m:MetricNode)
            WHERE $nid IN coalesce(m.references_nodes, [])
            RETURN m.metric_id AS metric_id, m.definition AS definition
            """,
            nid=metric_id,
        )
        if related:
            return {
                "note": f"'{metric_id}' is a ConceptNode, not a MetricNode. Metrics referencing it:",
                "related_metrics": [
                    {
                        "metric_id": r["metric_id"],
                        "definition": (r.get("definition") or "")[:200],
                        "has_diagnostic": True,
                    }
                    for r in related
                ],
            }

        return {"error": f"Metric '{raw_id}' not found"}

    # ── Mode: schema ─────────────────────────────────────────────────────────

    def _get_schema(self, table_name: str = None) -> dict:
        """
        Your schema lives as properties on ConceptNodes (primary_table, column_map, etc.)
        rather than dedicated BKGTable nodes.
        This reconstructs a schema view from ConceptNode properties.
        """
        if table_name:
            rows = self._run(
                """
                MATCH (n:ConceptNode)
                WHERE n.primary_table = $tname
                RETURN
                    n.node_id       AS node_id,
                    n.name          AS name,
                    n.primary_table AS table_name,
                    n.primary_key   AS primary_key,
                    n.grain         AS grain,
                    n.column_map    AS column_map,
                    n.attributes    AS columns,
                    n.base_query    AS base_query,
                    n.notes         AS notes
                """,
                tname=table_name,
            )
            if not rows:
                return {"error": f"No ConceptNode found with primary_table='{table_name}'"}
            return {
                "table_name": table_name,
                "nodes": [self._node_props_to_dict(r) for r in rows],
            }

        rows = self._run(
            """
            MATCH (n:ConceptNode)
            WHERE n.primary_table IS NOT NULL
            RETURN DISTINCT
                n.primary_table AS table_name,
                collect(n.node_id) AS used_by_nodes,
                collect(n.primary_key)[0] AS sample_primary_key,
                collect(n.grain)[0] AS sample_grain
            ORDER BY table_name
            """
        )
        return {
            "tables": rows,
            "note": (
                "Schema is derived from ConceptNode.primary_table properties. "
                "Use get_node mode for full column_map and base_query per node."
            ),
        }
