"""FastAPI application, request correlation, and health endpoints."""

import logging
import re
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from codechef_task.database import get_db_session
from codechef_task.logging_config import configure_logging, request_id_context
from codechef_task.checkout_routes import router as checkout_router
from codechef_task.inventory_routes import router as inventory_router
from codechef_task.reservation_routes import router as reservation_router

configure_logging()
logger = logging.getLogger("codechef_task.request")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)

app = FastAPI(title="The Last Seat", version="0.1.0")


@app.middleware("http")
async def correlate_and_log_request(request: Request, call_next):
    """Attach a bounded request ID, log one access record, and log uncaught errors."""
    incoming_id = request.headers.get("X-Request-ID", "")
    request_id = (
        incoming_id
        if REQUEST_ID_PATTERN.fullmatch(incoming_id)
        else str(uuid4())
    )
    request.state.request_id = request_id
    token = request_id_context.set(request_id)
    started = time.perf_counter()
    response = None
    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled exception while processing request")
            response = JSONResponse(
                status_code=500, content={"detail": "Internal server error"}
            )
        response.headers["X-Request-ID"] = request_id
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "http.request method=%s path=%s status=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    finally:
        request_id_context.reset(token)


app.include_router(reservation_router)
app.include_router(inventory_router)
app.include_router(checkout_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Report that the API process is responding."""
    return {"status": "healthy"}


@app.get("/health/db")
async def database_health(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Check database connectivity without revealing connection details."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("Database health check failed")
        raise HTTPException(
            status_code=503,
            detail={"status": "unhealthy", "database": "unavailable"},
        ) from None
    return {"status": "healthy", "database": "available"}
