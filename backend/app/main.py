"""FastAPI application entry-point."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routers.translate import router as translate_router
from .services.translator import fetch_supported_formats

# Configure logging – honour LOG_LEVEL env var (default INFO).
# Always set the app.services.translator logger to DEBUG so multipart
# diagnostics are visible when troubleshooting.
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(levelname)s:%(name)s:%(message)s",
)
logging.getLogger("app.services.translator").setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # --- startup ---
    logger.info("Fetching supported document formats from Azure …")
    await fetch_supported_formats()
    yield
    # --- shutdown ---


app = FastAPI(
    title="TranslateDoc",
    description="Translate documents using Azure AI Translator",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS – allow the Vite dev server during local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes (registered before the static-file catch-all)
app.include_router(translate_router, prefix="/api")

# Serve the built frontend when running in production (Docker)
_static = Path(__file__).resolve().parent.parent / "static"
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
