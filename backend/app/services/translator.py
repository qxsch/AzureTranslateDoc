"""
Azure AI Translator – Document Translation API (hybrid sync + batch).

Uses a dual approach:
- **Synchronous API** for text formats (TXT, HTML, CSV, …) – fast, no storage.
- **Batch API** for binary formats (PDF, DOCX, PPTX, …) – via Azure Blob Storage.

The synchronous single-document endpoint only accepts text/* content types.
Binary formats such as PDF require the batch Document Translation API which
operates through Azure Blob Storage.

API references
  Sync:  POST {endpoint}/translator/document:translate
  Batch: POST {endpoint}/translator/document/batches
"""

import asyncio
import logging
import mimetypes
import re
import uuid
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient

from ..config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GLOBAL_ENDPOINT = "api.cognitive.microsofttranslator.com"
_API_VERSION = "2024-05-01"
_MAX_API_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

# Blob Storage container names
_SOURCE_CONTAINER = "source"
_TARGET_CONTAINER = "target"

# Batch polling configuration
_BATCH_POLL_INITIAL_INTERVAL = 2.0   # seconds
_BATCH_POLL_MAX_INTERVAL = 10.0      # seconds
_BATCH_POLL_MAX_WAIT = 300.0         # 5 minutes

# ---------------------------------------------------------------------------
# Supported languages & formats
# ---------------------------------------------------------------------------

SUPPORTED_LANGUAGES = {
    "auto": "Auto-detect",
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "zh-Hans": "Chinese (Simplified)",
    "ja": "Japanese",
}

# Hardcoded fallback – used when the API is unreachable at startup.
_FALLBACK_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

# Populated at startup by fetch_supported_formats()
CONTENT_TYPES: dict[str, str] = dict(_FALLBACK_CONTENT_TYPES)
SUPPORTED_FORMATS: list[dict[str, Any]] = []

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_credential = None


def _get_credential():
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _get_headers() -> dict[str, str]:
    """Authentication headers for the Translator API."""
    headers: dict[str, str] = {}

    if settings.use_managed_identity:
        credential = _get_credential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        headers["Authorization"] = f"Bearer {token.token}"
    else:
        headers["Ocp-Apim-Subscription-Key"] = settings.azure_translator_key

    if settings.azure_translator_region:
        headers["Ocp-Apim-Subscription-Region"] = settings.azure_translator_region

    return headers


# ---------------------------------------------------------------------------
# Endpoint helpers
# ---------------------------------------------------------------------------


def _check_endpoint() -> None:
    """Warn loudly if the endpoint is the global one (no Document Translation)."""
    ep = settings.azure_translator_endpoint
    if _GLOBAL_ENDPOINT in ep:
        logger.error(
            "\n"
            "============================================================\n"
            "  ENDPOINT ERROR\n"
            "  The Document Translation API requires a CUSTOM DOMAIN\n"
            "  endpoint, but the configured endpoint is the global one:\n"
            "    %s\n"
            "\n"
            "  Expected format: https://<name>.cognitiveservices.azure.com\n"
            "\n"
            "  Fix your AZURE_TRANSLATOR_ENDPOINT environment variable\n"
            "  or .env file, then restart.\n"
            "============================================================",
            ep,
        )


def _get_document_translate_url() -> str:
    """Build the synchronous document translation URL."""
    endpoint = settings.azure_translator_endpoint.rstrip("/")
    return f"{endpoint}/translator/document:translate"


def _get_supported_formats_url() -> str:
    """Build the URL for the supported-formats endpoint."""
    endpoint = settings.azure_translator_endpoint.rstrip("/")
    return f"{endpoint}/translator/document/formats"


def _get_batch_url() -> str:
    """Build the batch document translation URL."""
    endpoint = settings.azure_translator_endpoint.rstrip("/")
    return f"{endpoint}/translator/document/batches"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


async def fetch_supported_formats(
    *,
    max_retries: int = 3,
    backoff_base: float = 2.0,
) -> list[dict[str, Any]]:
    """Fetch supported document formats from the Document Translation API.

    Retries with exponential back-off on transient failures.  On success the
    module-level ``CONTENT_TYPES`` and ``SUPPORTED_FORMATS`` are updated
    so the rest of the application can use them.

    Returns the raw list of format dicts from the API ``value`` array.
    """
    url = _get_supported_formats_url()
    headers = _get_headers()
    params = {"api-version": _API_VERSION, "type": "document"}

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers, params=params)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Formats API returned {response.status_code}: "
                    f"{response.text[:300]}"
                )
            data = response.json()
            formats: list[dict[str, Any]] = data.get("value", [])

            new_ct: dict[str, str] = {}
            for fmt in formats:
                extensions = fmt.get("fileExtensions", [])
                content_types = fmt.get("contentTypes", [])
                ct = (content_types[0].lower() if content_types
                      else "application/octet-stream")
                for ext in extensions:
                    ext_norm = ext.strip().lower()
                    if not ext_norm.startswith("."):
                        ext_norm = f".{ext_norm}"
                    new_ct[ext_norm] = ct

            CONTENT_TYPES.clear()
            CONTENT_TYPES.update(new_ct)
            SUPPORTED_FORMATS.clear()
            SUPPORTED_FORMATS.extend(formats)
            logger.info(
                "Loaded %d supported document formats from API (%d extensions).",
                len(formats),
                len(new_ct),
            )
            return formats

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_retries:
                wait = backoff_base ** attempt
                logger.warning(
                    "Attempt %d/%d to fetch supported formats failed (%s). "
                    "Retrying in %.1fs …",
                    attempt,
                    max_retries,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "All %d attempts to fetch supported formats failed. "
                    "Using hardcoded fallback. Last error: %s",
                    max_retries,
                    exc,
                )

    # Keep the hardcoded fallback already in CONTENT_TYPES
    CONTENT_TYPES.clear()
    CONTENT_TYPES.update(_FALLBACK_CONTENT_TYPES)
    SUPPORTED_FORMATS.clear()
    return []


def _resolve_content_type(filename: str, file_extension: str) -> str:
    """Determine the MIME content type for a file."""
    file_extension = file_extension.lower()
    guessed_type, _ = mimetypes.guess_type(filename)
    return (
        guessed_type
        or CONTENT_TYPES.get(file_extension)
        or "application/octet-stream"
    )


def _is_sync_format(content_type: str) -> bool:
    """Return True if the content type works with the synchronous API."""
    return content_type.startswith("text/")


# ---------------------------------------------------------------------------
# Blob Storage helpers
# ---------------------------------------------------------------------------


def _get_storage_account_name() -> str:
    """Resolve the storage account name from settings."""
    if settings.azure_storage_account_name:
        return settings.azure_storage_account_name
    if settings.azure_storage_connection_string:
        parts = dict(
            part.split("=", 1)
            for part in settings.azure_storage_connection_string.split(";")
            if "=" in part
        )
        return parts.get("AccountName", "")
    raise RuntimeError(
        "No storage configuration. "
        "Set AZURE_STORAGE_ACCOUNT_NAME or AZURE_STORAGE_CONNECTION_STRING."
    )


def _create_blob_service_client() -> BlobServiceClient:
    """Create an async BlobServiceClient."""
    if settings.azure_storage_connection_string:
        return BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string
        )
    if not settings.azure_storage_account_name:
        raise RuntimeError(
            "No storage configuration. "
            "Set AZURE_STORAGE_ACCOUNT_NAME or AZURE_STORAGE_CONNECTION_STRING."
        )
    account_url = (
        f"https://{settings.azure_storage_account_name}.blob.core.windows.net"
    )
    return BlobServiceClient(account_url, credential=_get_credential())


def _build_blob_url(container: str, blob_name: str) -> str:
    """Build a plain blob URL (no SAS – Translator uses its own identity)."""
    account_name = _get_storage_account_name()
    return (
        f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}"
    )


# ---------------------------------------------------------------------------
# Synchronous (single-document) translation  –  text/* only
# ---------------------------------------------------------------------------


async def _translate_sync(
    file_bytes: bytes,
    filename: str,
    file_extension: str,
    source_lang: str,
    target_lang: str,
    content_type: str,
) -> bytes:
    """Translate a text-based document via the synchronous API."""

    url = _get_document_translate_url()
    headers = _get_headers()

    params: dict[str, str] = {
        "api-version": _API_VERSION,
        "targetLanguage": target_lang,
    }
    if source_lang and source_lang != "auto":
        params["sourceLanguage"] = source_lang

    # Sanitise filename for multipart upload
    stem, ext = (filename.rsplit(".", 1) + [""])[:2]
    safe_stem = re.sub(r"[^\w. -]", "_", stem)
    safe_filename = f"{safe_stem}.{ext}" if ext else safe_stem

    logger.info(
        "Sync translation: POST %s  (file=%s [%s], %d bytes, %s, %s → %s)",
        url, filename, safe_filename, len(file_bytes),
        content_type, source_lang, target_lang,
    )

    multipart_files = {"document": (safe_filename, file_bytes, content_type)}

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            url, params=params, headers=headers, files=multipart_files,
        )

    if response.status_code != 200:
        detail = response.text[:500] if response.text else f"HTTP {response.status_code}"
        logger.error(
            "Sync translation error %s for %s: %s",
            response.status_code, filename, detail,
        )
        hint = ""
        if response.status_code == 404:
            hint = (
                " — Use a custom-domain endpoint "
                "(https://<name>.cognitiveservices.azure.com)."
            )
        elif response.status_code == 401:
            hint = " — Check that AZURE_TRANSLATOR_KEY is correct."
        elif response.status_code == 403:
            hint = " — The key may lack Document Translation permission."
        raise RuntimeError(
            f"Sync Translation API returned {response.status_code}: {detail}{hint}"
        )

    logger.info(
        "Sync translation OK: %s (%d → %d bytes)",
        filename, len(file_bytes), len(response.content),
    )
    return response.content


# ---------------------------------------------------------------------------
# Batch (asynchronous) translation  –  binary formats (PDF, DOCX, …)
# ---------------------------------------------------------------------------


async def _translate_batch(
    file_bytes: bytes,
    filename: str,
    file_extension: str,
    source_lang: str,
    target_lang: str,
    content_type: str,
) -> bytes:
    """Translate a binary document via the batch API and Blob Storage."""

    job_id = uuid.uuid4().hex[:12]
    source_blob = f"{job_id}/{filename}"
    target_blob = f"{job_id}/{filename}"

    logger.info(
        "Batch translation: %s (%d bytes, %s, %s → %s, job=%s)",
        filename, len(file_bytes), content_type,
        source_lang, target_lang, job_id,
    )

    async with _create_blob_service_client() as blob_client:
        try:
            # 1. Upload source file to Blob Storage
            src_container = blob_client.get_container_client(_SOURCE_CONTAINER)
            await src_container.upload_blob(
                source_blob,
                file_bytes,
                content_settings=ContentSettings(content_type=content_type),
                overwrite=True,
            )
            logger.debug("Uploaded source blob: %s/%s", _SOURCE_CONTAINER, source_blob)

            # 2. Build blob URLs (Translator accesses via its system identity)
            source_url = _build_blob_url(_SOURCE_CONTAINER, source_blob)
            target_url = _build_blob_url(_TARGET_CONTAINER, target_blob)

            # 3. Start batch translation
            batch_url = _get_batch_url()
            headers = _get_headers()
            headers["Content-Type"] = "application/json"

            body: dict[str, Any] = {
                "inputs": [
                    {
                        "storageType": "File",
                        "source": {"sourceUrl": source_url},
                        "targets": [
                            {"targetUrl": target_url, "language": target_lang}
                        ],
                    }
                ]
            }
            if source_lang and source_lang != "auto":
                body["inputs"][0]["source"]["language"] = source_lang

            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.post(
                    batch_url,
                    headers=headers,
                    json=body,
                    params={"api-version": _API_VERSION},
                )

            if resp.status_code != 202:
                detail = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
                raise RuntimeError(
                    f"Batch Translation API returned {resp.status_code}: {detail}"
                )

            operation_url = resp.headers.get("Operation-Location")
            if not operation_url:
                raise RuntimeError(
                    "Batch API did not return an Operation-Location header"
                )

            logger.info("Batch started: %s", operation_url)

            # 4. Poll until translation finishes
            await _poll_batch(operation_url)

            # 5. Download translated file from target container
            tgt_container = blob_client.get_container_client(_TARGET_CONTAINER)
            download = await tgt_container.download_blob(target_blob)
            translated = await download.readall()

            logger.info(
                "Batch translation OK: %s (%d → %d bytes, job=%s)",
                filename, len(file_bytes), len(translated), job_id,
            )
            return translated

        finally:
            # 6. Clean up blobs (best-effort; lifecycle policy is safety net)
            for ctr, blob in [
                (_SOURCE_CONTAINER, source_blob),
                (_TARGET_CONTAINER, target_blob)
            ]:
                try:
                    await blob_client.get_container_client(ctr).delete_blob(blob)
                    logger.debug("Deleted blob %s/%s", ctr, blob)
                except Exception:
                    logger.debug(
                        "Could not delete %s/%s (lifecycle policy will clean up)",
                        ctr, blob,
                    )


async def _poll_batch(operation_url: str) -> None:
    """Poll a batch translation operation until it succeeds or fails."""
    headers = _get_headers()
    elapsed = 0.0
    interval = _BATCH_POLL_INITIAL_INTERVAL

    while elapsed < _BATCH_POLL_MAX_WAIT:
        await asyncio.sleep(interval)
        elapsed += interval

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(operation_url, headers=headers)

        if resp.status_code != 200:
            raise RuntimeError(
                f"Batch status poll returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        data = resp.json()
        status = data.get("status", "Unknown")

        logger.debug("Batch poll: status=%s (%.0fs elapsed)", status, elapsed)

        if status == "Succeeded":
            summary = data.get("summary", {})
            if summary.get("failed", 0) > 0:
                raise RuntimeError(
                    f"Batch completed but {summary['failed']} document(s) failed. "
                    f"Summary: {summary}"
                )
            return

        if status in ("Failed", "Cancelled", "ValidationFailed"):
            error = data.get("error", {})
            msg = error.get("message", status)
            raise RuntimeError(f"Batch translation {status}: {msg}")

        # Gradual back-off
        interval = min(interval * 1.5, _BATCH_POLL_MAX_INTERVAL)

    raise RuntimeError(
        f"Batch translation timed out after {_BATCH_POLL_MAX_WAIT:.0f}s"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def translate_document(
    file_bytes: bytes,
    filename: str,
    file_extension: str,
    source_lang: str,
    target_lang: str,
) -> bytes:
    """
    Translate a document, routing to the appropriate API:

    * **text/** formats  → fast synchronous single-document API
    * everything else (PDF, DOCX, …) → batch API via Blob Storage
    """
    _check_endpoint()
    if _GLOBAL_ENDPOINT in settings.azure_translator_endpoint:
        raise RuntimeError(
            "Document Translation requires a custom-domain endpoint "
            "(https://<name>.cognitiveservices.azure.com), but "
            f"AZURE_TRANSLATOR_ENDPOINT is set to the global endpoint: "
            f"{settings.azure_translator_endpoint}  — "
            "Please update your .env file or environment variable and restart."
        )

    content_type = _resolve_content_type(filename, file_extension)

    if _is_sync_format(content_type):
        return await _translate_sync(
            file_bytes, filename, file_extension,
            source_lang, target_lang, content_type,
        )

    return await _translate_batch(
        file_bytes, filename, file_extension,
        source_lang, target_lang, content_type,
    )
