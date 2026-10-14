from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import literal, select

from app.db.session import SessionLocal

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> JSONResponse:
    """The process answers; used by the container health check. The API is built to keep
    serving (buffered writes, open streams) through a database outage, so that outage must
    not make the container unhealthy and get it restarted with its buffers."""
    return JSONResponse({"status": "ok"})


@router.get("/health")
async def health() -> JSONResponse:
    """Readiness: liveness plus a database round trip."""
    try:
        async with SessionLocal() as session:
            await session.scalar(select(literal(1)))
    except Exception:  # noqa: BLE001 - report, do not raise
        return JSONResponse({"status": "degraded", "database": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})
