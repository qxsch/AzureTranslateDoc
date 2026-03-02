"""In-memory translation job manager.

Manages async translation jobs: each job can contain one or more files.
Jobs are stored in memory and auto-purged after ``_JOB_TTL`` seconds.

When ``enhance_accuracy`` is enabled, each file goes through a three-stage
pipeline:
  1. Pass 1 – standard translation
  2. Text extraction + LLM glossary generation
  3. Pass 2 – re-translation with the glossary attached
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from .glossary_generator import generate_glossary, is_available as llm_available
from .text_extractor import extract_text
from .translator import SUPPORTED_LANGUAGES, translate_document

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_JOB_TTL = 3600  # purge completed jobs after 1 hour
_jobs: dict[str, "TranslationJob"] = {}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileResult:
    """Tracks a single file within a translation job."""

    index: int
    original_name: str
    output_name: str
    file_extension: str
    status: str = "pending"         # pending | translating | enhancing | completed | error
    error: str | None = None
    substatus: str = ""             # pass1 | extracting | glossary | pass2 (for enhanced)

    # Stored in memory; cleared when the job is purged.
    _input_bytes: bytes = field(default=b"", repr=False)
    _result_bytes: bytes | None = field(default=None, repr=False)


@dataclass
class TranslationJob:
    """Tracks a multi-file translation job."""

    id: str
    status: str                     # pending | processing | completed | error
    source_lang: str
    target_lang: str
    target_lang_name: str
    enhance_accuracy: bool = False
    files: list[FileResult] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def _language_suffix(lang_code: str) -> str:
    """Derive a filename-safe suffix from a language code.

    Examples::

        "de"      → "german"
        "zh-Hans" → "chinese-simplified"
    """
    name = SUPPORTED_LANGUAGES.get(lang_code, lang_code)
    s = name.lower().replace("(", "").replace(")", "").strip()
    return s.replace("  ", " ").replace(" ", "-")


def output_filename(original: str, lang_code: str) -> str:
    """Build an output filename with a language suffix.

    Examples::

        output_filename("myFile.pdf", "de")    → "myFile_german.pdf"
        output_filename("document.docx", "fr") → "document_french.docx"
    """
    suffix = _language_suffix(lang_code)
    dot = original.rfind(".")
    if dot > 0:
        return f"{original[:dot]}_{suffix}{original[dot:]}"
    return f"{original}_{suffix}"


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------


def _purge_expired() -> None:
    """Remove jobs older than ``_JOB_TTL``."""
    now = time.time()
    expired = [jid for jid, j in _jobs.items() if now - j.created_at > _JOB_TTL]
    for jid in expired:
        del _jobs[jid]
    if expired:
        logger.info("Purged %d expired job(s)", len(expired))


def create_job(
    files: list[tuple[str, str, bytes]],   # (filename, extension, content)
    source_lang: str,
    target_lang: str,
    enhance_accuracy: bool = False,
) -> TranslationJob:
    """Create a new translation job (not yet started)."""
    _purge_expired()

    job_id = uuid.uuid4().hex[:12]
    lang_name = SUPPORTED_LANGUAGES.get(target_lang, target_lang)

    file_results = []
    for i, (fname, ext, content) in enumerate(files):
        file_results.append(FileResult(
            index=i,
            original_name=fname,
            output_name=output_filename(fname, target_lang),
            file_extension=ext,
            _input_bytes=content,
        ))

    job = TranslationJob(
        id=job_id,
        status="pending",
        source_lang=source_lang,
        target_lang=target_lang,
        target_lang_name=lang_name,
        enhance_accuracy=enhance_accuracy,
        files=file_results,
    )
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> TranslationJob | None:
    """Retrieve a job by ID, or ``None`` if not found / expired."""
    _purge_expired()
    return _jobs.get(job_id)


def get_file_result(job_id: str, file_index: int) -> FileResult | None:
    """Get a specific file result from a job."""
    job = get_job(job_id)
    if not job or file_index < 0 or file_index >= len(job.files):
        return None
    return job.files[file_index]


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------


async def _translate_file(
    file: FileResult,
    source_lang: str,
    target_lang: str,
) -> None:
    """Translate a single file within a job (called concurrently)."""
    file.status = "translating"
    try:
        result = await translate_document(
            file_bytes=file._input_bytes,
            filename=file.original_name,
            file_extension=file.file_extension,
            source_lang=source_lang,
            target_lang=target_lang,
        )
        file._result_bytes = result
        file.status = "completed"
        # Free input bytes to save memory
        file._input_bytes = b""
    except Exception as exc:
        file.status = "error"
        file.error = str(exc)
        logger.error("Translation failed for %s: %s", file.original_name, exc)


async def _translate_file_enhanced(
    file: FileResult,
    source_lang: str,
    target_lang: str,
) -> None:
    """Three-stage enhanced translation for a single file.

    1. Translate normally (pass 1)
    2. Extract text from source + pass-1 result → LLM glossary
    3. Re-translate with the glossary (pass 2)

    Falls back to the pass-1 result if any enhancement step fails.
    """
    file.status = "enhancing"
    original_bytes = file._input_bytes
    pass1_result = None

    try:
        # ── Pass 1: standard translation ──
        file.substatus = "pass1"
        logger.info("Enhanced [%s] pass 1: translating…", file.original_name)
        pass1_result = await translate_document(
            file_bytes=original_bytes,
            filename=file.original_name,
            file_extension=file.file_extension,
            source_lang=source_lang,
            target_lang=target_lang,
        )

        # ── Extract text ──
        file.substatus = "extracting"
        logger.info("Enhanced [%s]: extracting text…", file.original_name)
        source_text = extract_text(original_bytes, file.file_extension)
        translated_text = extract_text(pass1_result, file.file_extension)

        if not source_text.strip():
            logger.warning(
                "Enhanced [%s]: could not extract source text, "
                "returning pass-1 result.", file.original_name,
            )
            file._result_bytes = pass1_result
            file.status = "completed"
            file.substatus = ""
            file._input_bytes = b""
            return

        # ── LLM glossary generation ──
        file.substatus = "glossary"
        logger.info("Enhanced [%s]: generating glossary…", file.original_name)

        source_lang_name = SUPPORTED_LANGUAGES.get(source_lang, source_lang)
        target_lang_name = SUPPORTED_LANGUAGES.get(target_lang, target_lang)
        glossary_bytes = generate_glossary(
            source_text=source_text,
            translated_text=translated_text,
            source_lang=source_lang_name,
            target_lang=target_lang_name,
        )

        if not glossary_bytes:
            logger.warning(
                "Enhanced [%s]: glossary generation returned empty, "
                "returning pass-1 result.", file.original_name,
            )
            file._result_bytes = pass1_result
            file.status = "completed"
            file.substatus = ""
            file._input_bytes = b""
            return

        # ── Pass 2: re-translate with glossary ──
        file.substatus = "pass2"
        logger.info(
            "Enhanced [%s] pass 2: re-translating with glossary "
            "(%d bytes)…", file.original_name, len(glossary_bytes),
        )
        pass2_result = await translate_document(
            file_bytes=original_bytes,
            filename=file.original_name,
            file_extension=file.file_extension,
            source_lang=source_lang,
            target_lang=target_lang,
            glossary_bytes=glossary_bytes,
        )

        file._result_bytes = pass2_result
        file.status = "completed"
        file.substatus = ""
        file._input_bytes = b""
        logger.info("Enhanced [%s]: done.", file.original_name)

    except Exception as exc:
        logger.error(
            "Enhanced translation failed for %s at substatus '%s': %s",
            file.original_name, file.substatus, exc,
        )
        # If we have a pass-1 result, return that as fallback
        if pass1_result is not None:
            logger.info(
                "Using pass-1 fallback for %s.", file.original_name,
            )
            file._result_bytes = pass1_result
            file.status = "completed"
            file.error = f"Enhanced mode partially failed ({file.substatus}); standard translation returned."
        else:
            file.status = "error"
            file.error = str(exc)
        file.substatus = ""
        file._input_bytes = b""


async def process_job(job: TranslationJob) -> None:
    """Process all files in a job concurrently."""
    job.status = "processing"

    if job.enhance_accuracy and llm_available():
        tasks = [
            _translate_file_enhanced(f, job.source_lang, job.target_lang)
            for f in job.files
        ]
    else:
        if job.enhance_accuracy and not llm_available():
            logger.warning(
                "Job %s requested enhanced accuracy but no LLM is configured. "
                "Falling back to standard translation.", job.id,
            )
        tasks = [
            _translate_file(f, job.source_lang, job.target_lang)
            for f in job.files
        ]
    await asyncio.gather(*tasks, return_exceptions=True)

    # Determine overall status
    has_error = any(f.status == "error" for f in job.files)
    job.status = "completed" if not has_error else "completed_with_errors"

    logger.info(
        "Job %s finished: %s (%d files, %d completed, %d errors)",
        job.id,
        job.status,
        len(job.files),
        sum(1 for f in job.files if f.status == "completed"),
        sum(1 for f in job.files if f.status == "error"),
    )
