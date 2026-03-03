"""
Sandbox endpoint — POST /api/v1/sandbox/execute

Delegates to sandbox_service; handles HTTP error mapping only.
"""
from fastapi import APIRouter, HTTPException

import services.sandbox_service as sandbox_svc
from api.v1.schemas import SandboxRequest

router = APIRouter(tags=["Sandbox"])


@router.post("/sandbox/execute")
def sandbox_execute(req: SandboxRequest):
    """
    Execute Python code in the PostgreSQL-backed sandbox.

    **Available in namespace:** `conn` (psycopg2 read-only), `pd`, `np`, `go`, `px`, `json`, `session`

    Set a `result` dict in your code — its values are returned.
    `pd.DataFrame` values are automatically serialised to records.

    **Example:**
    ```python
    df = pd.read_sql("SELECT table_name FROM information_schema.tables LIMIT 5", conn)
    result = {"tables": df.to_dict(orient="records")}
    ```
    """
    try:
        return sandbox_svc.execute(req.code, req.timeout_seconds)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
