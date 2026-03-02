"""Translation API routes.

Provides an async job-based workflow:
  1. ``POST /api/translate``  – accept one or more files, returns a job ID (202)
  2. ``GET  /api/jobs/{id}``  – poll for per-file status
  3. ``GET  /api/jobs/{id}/files/{i}/download`` – download a translated file
"""

from pathlib import Path
from typing import List

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import Response

from ..config import settings
from ..services.job_manager import (
    create_job,
    get_file_result,
    get_job,
    process_job,
)
from ..services.translator import (
    CONTENT_TYPES,
    SUPPORTED_FORMATS,
    SUPPORTED_LANGUAGES,
)

router = APIRouter()


def _get_extension(filename: str) -> str:
    return Path(filename).suffix.lower()


# ---------------------------------------------------------------------------
# Read-only endpoints
# ---------------------------------------------------------------------------


@router.get("/languages")
async def get_languages():
    """Return the list of supported languages."""
    return {
        "languages": [
            {"code": code, "name": name}
            for code, name in SUPPORTED_LANGUAGES.items()
        ]
    }


@router.get("/health")
async def health():
    return {"status": "healthy"}


@router.get("/formats")
async def get_formats():
    """Return the list of supported document formats."""
    if SUPPORTED_FORMATS:
        return {"formats": SUPPORTED_FORMATS}

    # Build a minimal response from the fallback CONTENT_TYPES dict
    fallback = []
    for ext, ct in CONTENT_TYPES.items():
        fallback.append({
            "format": ext.lstrip(".").upper(),
            "fileExtensions": [ext],
            "contentTypes": [ct],
        })
    return {"formats": fallback}


# ---------------------------------------------------------------------------
# Translation job endpoints
# ---------------------------------------------------------------------------


@router.post("/translate", status_code=202)
async def translate_endpoint(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    source_language: str = Form("auto"),
    target_language: str = Form("en"),
    enhance_accuracy: bool = Form(False),
):
    """Accept one or more files and start an async translation job.

    Returns ``202`` with ``{"job_id": "…"}``.  The frontend should poll
    ``GET /api/jobs/{job_id}`` until the status is ``completed``.
    """
    # --- language validation ---
    if source_language not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source language: {source_language}",
        )
    if target_language not in SUPPORTED_LANGUAGES or target_language == "auto":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported target language: {target_language}",
        )

    # --- per-file validation ---
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    file_data: list[tuple[str, str, bytes]] = []

    for f in files:
        if not f.filename:
            raise HTTPException(status_code=400, detail="No filename provided.")

        ext = _get_extension(f.filename)
        if ext not in CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported file type '{ext}' ({f.filename}). "
                    f"Supported: {', '.join(sorted(CONTENT_TYPES.keys()))}"
                ),
            )

        content = await f.read()
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File '{f.filename}' too large. "
                    f"Maximum size: {settings.max_file_size_mb} MB"
                ),
            )
        file_data.append((f.filename, ext, content))

    # --- create job & schedule background processing ---
    job = create_job(file_data, source_language, target_language, enhance_accuracy)
    background_tasks.add_task(process_job, job)

    return {"job_id": job.id}


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Poll for translation job status."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    return {
        "job_id": job.id,
        "status": job.status,
        "source_language": job.source_lang,
        "target_language": job.target_lang,
        "target_language_name": job.target_lang_name,
        "enhance_accuracy": job.enhance_accuracy,
        "files": [
            {
                "index": f.index,
                "original_name": f.original_name,
                "output_name": f.output_name,
                "status": f.status,
                "substatus": f.substatus,
                "error": f.error,
            }
            for f in job.files
        ],
    }


@router.get("/jobs/{job_id}/files/{file_index}/download")
async def download_file(job_id: str, file_index: int):
    """Download a specific translated file."""
    fr = get_file_result(job_id, file_index)
    if not fr:
        raise HTTPException(status_code=404, detail="File not found.")

    if fr.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"File not ready. Status: {fr.status}",
        )
    if not fr._result_bytes:
        raise HTTPException(
            status_code=410,
            detail="Translation result no longer available.",
        )

    return Response(
        content=fr._result_bytes,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{fr.output_name}"'
        },
    )
