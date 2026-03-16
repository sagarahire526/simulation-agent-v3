"""
v1 router — aggregates all v1 endpoint routers under the /v1 prefix.
"""
from fastapi import APIRouter

from api.v1.endpoints import health, simulate, bkg, sandbox, semantic, threads, sse_simulate

router = APIRouter(prefix="/v1")


router.include_router(sse_simulate.router)
router.include_router(threads.router)
router.include_router(sandbox.router)
router.include_router(health.router)
router.include_router(semantic.router)
router.include_router(simulate.router)