"""
Pull data types for every column the Construction Plan Forecast node touches.

Source of truth = the SLA DAG on the live KG node (so we never drift from
what build_plan actually reads). Adds the fixed columns + the filter set,
then queries information_schema.columns for the actual PostgreSQL types.

Run:
    python3 -m scripts.inspect_cpf_column_types
    # OR with a sample value per column (slower, one extra COUNT/SELECT per col):
    python3 -m scripts.inspect_cpf_column_types --samples
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

from tools.neo4j_tool import Neo4jTool
from tools.python_sandbox import PythonSandbox

NODE_ID = "cpf-001-construction-plan-forecast"
STAGING = "pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined"
NAS_TABLE = "pwc_macro_staging_schema.stg_nas_planned_outage_activity"

# Columns we add on top of the SLA DAG. Keep in sync with build_plan.
FIXED_COLUMNS = {
    "pj_p_4225_construction_start_finish",
    "ms_1550_construction_start_actual",
    "ms_1555_construction_complete_actual",
    "scoping_package_crane_required",
    "s_site_id",
    "smp_name",
}
FILTER_COLUMNS = {
    "rgn_region", "m_area", "m_market", "construction_gc",
    "por_category", "pj_project_status", "s_site_class",
}
NAS_COLUMNS = {"nas_activity_end_date"}   # lives in the NAS outage table


def collect_columns_from_sla_dag(dag: dict) -> set[str]:
    cols: set[str] = set()
    for project_branch in dag.values():
        for spec in project_branch.values():
            if isinstance(spec, dict):
                for k in ("column", "alt_column", "actual"):
                    v = spec.get(k)
                    if v:
                        cols.add(v)
    return cols


def fetch_sla_dag_from_kg() -> dict:
    nt = Neo4jTool()
    out = nt.run_cypher(
        "MATCH (n:BKGNode {node_id: $nid}) RETURN coalesce(n.kpi_sla_dag, '{}') AS dag",
        {"nid": NODE_ID},
    )
    rows = out.get("records") or []
    if not rows:
        sys.exit(f"node '{NODE_ID}' not in Neo4j — run scripts/load_cpf_node.py first")
    return json.loads(rows[0]["dag"])


def run_inspect(with_samples: bool) -> None:
    dag = fetch_sla_dag_from_kg()
    sla_cols = collect_columns_from_sla_dag(dag)

    print(f"\n[info] SLA DAG references {len(sla_cols)} milestone columns")
    print(f"[info] + {len(FIXED_COLUMNS)} fixed + {len(FILTER_COLUMNS)} filter columns")

    main_columns = sorted(sla_cols | FIXED_COLUMNS | FILTER_COLUMNS) - NAS_COLUMNS
    # NAS table has its own columns
    nas_columns = sorted(c for c in (sla_cols | NAS_COLUMNS) if c in NAS_COLUMNS or c.startswith("nas_"))

    # Build the introspection SQL via the sandbox (so it uses the same PG conn).
    # information_schema is standard PostgreSQL.
    code = f"""
import json
sample_cols_main = {sorted(main_columns)!r}
sample_cols_nas  = {sorted(nas_columns)!r}
schema, main_table = {STAGING!r}.split('.')
_, nas_table = {NAS_TABLE!r}.split('.')

sql_main = (
    "SELECT column_name, data_type, udt_name, is_nullable "
    "FROM information_schema.columns "
    "WHERE table_schema = '" + schema + "' AND table_name = '" + main_table + "' "
    "  AND column_name = ANY(ARRAY[" + ", ".join("'" + c + "'" for c in sample_cols_main) + "]) "
    "ORDER BY column_name"
)
main_meta = execute_query(sql_main) or []

nas_meta = []
if sample_cols_nas:
    sql_nas = (
        "SELECT column_name, data_type, udt_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = '" + schema + "' AND table_name = '" + nas_table + "' "
        "  AND column_name = ANY(ARRAY[" + ", ".join("'" + c + "'" for c in sample_cols_nas) + "]) "
        "ORDER BY column_name"
    )
    nas_meta = execute_query(sql_nas) or []

# Identify columns that exist in the SLA DAG but are NOT present in either table
existing = {{ r['column_name'] for r in main_meta }} | {{ r['column_name'] for r in nas_meta }}
referenced = set(sample_cols_main + sample_cols_nas)
missing = sorted(referenced - existing)

result = {{
    "main_table":         {STAGING!r},
    "nas_table":          {NAS_TABLE!r},
    "main_columns":       main_meta,
    "nas_columns":        nas_meta,
    "missing_columns":    missing,
    "checked_count":      len(referenced),
    "existing_count":     len(existing),
}}
"""
    if with_samples:
        code += """
# One sample non-null value per main-table column (slow — one SELECT per column)
samples = {}
for r in main_meta:
    col = r['column_name']
    try:
        rows = execute_query(
            "SELECT " + col + " AS v FROM " + schema + "." + main_table +
            " WHERE " + col + " IS NOT NULL LIMIT 1"
        ) or []
        samples[col] = rows[0].get('v') if rows else None
    except Exception as e:
        samples[col] = "ERR: " + str(e)[:120]
result['main_samples'] = samples
"""

    sandbox = PythonSandbox()
    out = sandbox.execute(code, 90)
    if out.get("status") != "success":
        sys.exit(f"sandbox error: {out.get('error')}\n{out.get('traceback','')}")
    result = out["result"]

    # Pretty print
    print(f"\n{'='*78}")
    print(f"MAIN TABLE  {result['main_table']}")
    print(f"{'='*78}")
    fmt = "{:<60} {:<22} {:<10} {:<5}"
    print(fmt.format("column", "data_type", "udt_name", "null"))
    print("-" * 100)
    for r in result["main_columns"]:
        print(fmt.format(r["column_name"], r["data_type"], r.get("udt_name", ""), r["is_nullable"]))

    if result["nas_columns"]:
        print(f"\n{'='*78}")
        print(f"NAS TABLE   {result['nas_table']}")
        print(f"{'='*78}")
        print(fmt.format("column", "data_type", "udt_name", "null"))
        print("-" * 100)
        for r in result["nas_columns"]:
            print(fmt.format(r["column_name"], r["data_type"], r.get("udt_name", ""), r["is_nullable"]))

    if result["missing_columns"]:
        print(f"\n[WARN] {len(result['missing_columns'])} column(s) referenced in SLA DAG but NOT in either table:")
        for c in result["missing_columns"]:
            print(f"   - {c}")

    if with_samples and "main_samples" in result:
        print("\n[samples] one non-null value per column:")
        for col, val in result["main_samples"].items():
            print(f"   {col:<60} {val!r}")

    # Roll up by data_type so we can see the spread at a glance
    type_counts: dict[str, int] = {}
    for r in result["main_columns"] + result["nas_columns"]:
        type_counts[r["data_type"]] = type_counts.get(r["data_type"], 0) + 1
    print("\n[summary] data_type distribution across our columns:")
    for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        print(f"   {n:>3}  {t}")

    print(f"\n[total] checked {result['checked_count']} columns, found {result['existing_count']}")


if __name__ == "__main__":
    run_inspect(with_samples="--samples" in sys.argv)
