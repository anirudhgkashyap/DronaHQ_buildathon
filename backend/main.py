"""Atlas SDR – FastAPI application entrypoint.

Lifespan:
  startup  → init_models() creates/migrates tables, starts background scheduler
  shutdown → scheduler cancelled cleanly

Routers are mounted at /api/v1/* to match the frontend's documented contract.
CORS is wide-open for the hackathon demo; tighten origins= in production.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_models, session_scope
from app.api.errors import install_error_handlers
from app.api import (
    campaigns,
    platform,
    approvals,
    prospects,
    conversations,
    prompts,
    agents_cfg,
    knowledge,
    metrics,
)
from app.orchestrator import scheduler as sched
from app import models  # noqa: F401 — ensures models are registered

logging.basicConfig(
    level=logging.DEBUG if getattr(settings, "DEBUG", False) else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────
    logger.info("Atlas SDR starting — environment: %s", settings.DATABASE_URL[:40])
    await init_models()
    logger.info("Database tables ready")

    # Auto-seed on first boot (idempotent — skips if data already present)
    try:
        import sqlalchemy
        from app.seed_minimal import seed as do_seed
        async with session_scope() as s:
            row = await s.execute(
                sqlalchemy.text("SELECT COUNT(*) FROM campaigns")
            )
            count = row.scalar()
        if count == 0:
            logger.info("Empty database detected — seeding demo data…")
            await do_seed(reset=False)
            logger.info("Seed complete")
        else:
            logger.info("Database already has %d campaign(s) — skipping seed", count)
    except Exception as exc:
        logger.warning("Auto-seed skipped: %s", exc)

    sched.start()
    logger.info(
        "Scheduler %s (tick every %ss)",
        "started" if settings.SCHEDULER_ENABLED else "disabled by config",
        settings.SCHEDULER_TICK_SECONDS,
    )

    yield  # application is live

    # ── shutdown ─────────────────────────────────────────────────────────
    await sched.stop()
    logger.info("Atlas SDR shutdown complete")


app = FastAPI(
    title="Atlas SDR API",
    description=(
        "Autonomous B2B Sales Development Representative — "
        "backend for the DronaHQ AI Buildathon demo."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
# Wide open for the hackathon demo.  Restrict in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Error handlers ────────────────────────────────────────────────────────────
install_error_handlers(app)

# ── Routers ───────────────────────────────────────────────────────────────────
PREFIX = "/api/v1"

app.include_router(campaigns.router,    prefix=PREFIX)
app.include_router(platform.router,     prefix=PREFIX)
app.include_router(approvals.router,    prefix=PREFIX)
app.include_router(prospects.router,    prefix=PREFIX)
app.include_router(conversations.router, prefix=PREFIX)
app.include_router(prompts.router,      prefix=PREFIX)
app.include_router(agents_cfg.router,   prefix=PREFIX)
app.include_router(knowledge.router,    prefix=PREFIX)
app.include_router(metrics.router,      prefix=PREFIX)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "scheduler": sched.state.as_dict(),
        "dry_run": settings.CHANNELS_DRY_RUN,
    }


# ── Frontend (static SPA) ───────────────────────────────────────────────────────
# Serves the plain-JS frontend from the sibling `front_end/` directory so the
# whole app — API + UI — is one Render service on one URL. This must be the
# LAST thing mounted: StaticFiles at "/" is a catch-all and would otherwise
# shadow the /api/v1/* and /health routes registered above.
#
# `front_end/js/config.js` already defaults API_BASE_URL to `/api/v1`, which is
# correct as long as the frontend is served from this same origin (as it is
# here). If the frontend is instead deployed separately (its own static host),
# set `window.ATLAS_API_URL` to this backend's full URL before config.js loads.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "front_end"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at %s — serving API only", _FRONTEND_DIR)


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=getattr(settings, "DEBUG", False),
        log_level="debug" if getattr(settings, "DEBUG", False) else "info",
    )
