from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from .routers import auth, users, partners, cameras, zones, admin, sources, occupancy, forecasts
from .database import engine
from .db_models import Base

# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ParkTrack API",
    version="1.0.0",
    description="Smart Parking API — MVP",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://swagger.parktrack.live",
        "https://labeler.parktrack.live",
        "https://parktrack.live",
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ---------------------------------------------------------------------------
# Роутеры. Все пути идут под /api/v1 согласно контракту.
# ---------------------------------------------------------------------------

PREFIX = "/api/v1"

app.include_router(auth.router,     prefix=PREFIX)
app.include_router(users.router,    prefix=PREFIX)
app.include_router(partners.router, prefix=PREFIX)
app.include_router(cameras.router,  prefix=PREFIX)
app.include_router(zones.router,    prefix=PREFIX)
app.include_router(admin.router,    prefix=PREFIX)
app.include_router(sources.router,  prefix=PREFIX)
app.include_router(occupancy.router,prefix=PREFIX)
app.include_router(forecasts.router,prefix=PREFIX)


# ---------------------------------------------------------------------------
# System эндпоинты (без prefix — они были и раньше)
# ---------------------------------------------------------------------------

from .database import get_db
from sqlalchemy.orm import Session
from fastapi import Depends
from typing import Annotated


@app.get("/api/v1/health", tags=["System"])
def health(db: Annotated[Session, Depends(get_db)]):
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        db_status = "connected"
        api_status = "healthy"
    except Exception:
        db_status = "disconnected"
        api_status = "degraded"

        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": api_status, "database": db_status})
    return {"status": api_status, "database": db_status}


@app.get("/api/v1/version", tags=["System"])
def version():
    return {"api_version": "1.0"}
