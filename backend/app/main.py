"""
Application entrypoint.

Creates and configures the FastAPI application instance. Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.data_sources import router as data_sources_router
from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.tasks import router as tasks_router
from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Application startup",
        extra={"app_env": settings.app_env, "app_name": settings.app_name},
    )
    yield
    logger.info("Application shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.app_debug,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(data_sources_router)
    app.include_router(tasks_router)
    app.include_router(internal_router)

    return app


app = create_app()
