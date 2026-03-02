"""Tests for Azure Document Translation service (sync + batch)."""

import httpx
import pytest
import respx
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.translator import (
    SUPPORTED_LANGUAGES,
    CONTENT_TYPES,
    SUPPORTED_FORMATS,
    _FALLBACK_CONTENT_TYPES,
    _GLOBAL_ENDPOINT,
    _check_endpoint,
    _is_sync_format,
    _resolve_content_type,
    translate_document,
    fetch_supported_formats,
    _get_document_translate_url,
    _get_supported_formats_url,
    _get_batch_url,
    _get_headers,
)
from app.config import settings


MOCK_ENDPOINT = "https://my-translator.cognitiveservices.azure.com"
GLOBAL_ENDPOINT = "https://api.cognitive.microsofttranslator.com"
MOCK_STORAGE = "mystorageaccount"
MOCK_CONN_STR = (
    "DefaultEndpointsProtocol=https;"
    f"AccountName={MOCK_STORAGE};"
    "AccountKey=dGVzdGtleQ==;"
    "EndpointSuffix=core.windows.net"
)


# Override settings for tests
@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(settings, "azure_translator_endpoint", MOCK_ENDPOINT)
    monkeypatch.setattr(settings, "azure_translator_key", "test-key-123")
    monkeypatch.setattr(settings, "azure_translator_region", "eastus")
    monkeypatch.setattr(settings, "use_managed_identity", False)
    monkeypatch.setattr(settings, "azure_storage_account_name", MOCK_STORAGE)
    monkeypatch.setattr(settings, "azure_storage_connection_string", MOCK_CONN_STR)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

class TestUrlConstruction:
    def test_document_translate_url(self):
        url = _get_document_translate_url()
        assert url == f"{MOCK_ENDPOINT}/translator/document:translate"

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setattr(settings, "azure_translator_endpoint", f"{MOCK_ENDPOINT}/")
        url = _get_document_translate_url()
        assert url == f"{MOCK_ENDPOINT}/translator/document:translate"

    def test_supported_formats_url(self):
        url = _get_supported_formats_url()
        assert url == f"{MOCK_ENDPOINT}/translator/document/formats"

    def test_supported_formats_url_trailing_slash(self, monkeypatch):
        monkeypatch.setattr(settings, "azure_translator_endpoint", f"{MOCK_ENDPOINT}/")
        url = _get_supported_formats_url()
        assert url == f"{MOCK_ENDPOINT}/translator/document/formats"

    def test_batch_url(self):
        url = _get_batch_url()
        assert url == f"{MOCK_ENDPOINT}/translator/document/batches"


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_key_auth_headers(self):
        headers = _get_headers()
        assert headers["Ocp-Apim-Subscription-Key"] == "test-key-123"
        assert headers["Ocp-Apim-Subscription-Region"] == "eastus"
        assert "Authorization" not in headers

    def test_no_region_if_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "azure_translator_region", "")
        headers = _get_headers()
        assert "Ocp-Apim-Subscription-Region" not in headers


# ---------------------------------------------------------------------------
# translate_document – success
# ---------------------------------------------------------------------------

class TestTranslateDocument:
    """Tests for text-based documents routed through the sync API."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_translate_txt(self):
        """Plain text document → sync API returns translated bytes."""
        translated_bytes = b"Hallo Welt"
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=translated_bytes)
        )
        result = await translate_document(
            b"Hello world", "hello.txt", ".txt", "en", "de"
        )
        assert result == translated_bytes

    @pytest.mark.asyncio
    @respx.mock
    async def test_translate_md(self):
        """Markdown → sync API returns translated markdown bytes."""
        translated_bytes = b"# Titre\n\nBonjour"
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=translated_bytes)
        )
        result = await translate_document(
            b"# Title\n\nHello", "readme.md", ".md", "en", "fr"
        )
        assert result == translated_bytes


# ---------------------------------------------------------------------------
# Format routing
# ---------------------------------------------------------------------------


class TestFormatRouting:
    def test_text_plain_is_sync(self):
        assert _is_sync_format("text/plain") is True

    def test_text_html_is_sync(self):
        assert _is_sync_format("text/html") is True

    def test_application_pdf_is_batch(self):
        assert _is_sync_format("application/pdf") is False

    def test_application_docx_is_batch(self):
        assert _is_sync_format(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ) is False

    def test_resolve_content_type_txt(self):
        assert _resolve_content_type("hello.txt", ".txt") == "text/plain"

    def test_resolve_content_type_pdf(self):
        assert _resolve_content_type("doc.pdf", ".pdf") == "application/pdf"


# ---------------------------------------------------------------------------
# Batch translation (PDF, DOCX via Blob Storage)
# ---------------------------------------------------------------------------


def _mock_blob_service():
    """Return a mock BlobServiceClient for testing batch translation."""
    mock_blob_client = MagicMock()

    # Source container mock
    src_container = MagicMock()
    src_container.upload_blob = AsyncMock()
    src_container.delete_blob = AsyncMock()

    # Target container mock
    tgt_container = MagicMock()
    tgt_download = AsyncMock()
    tgt_download.readall = AsyncMock(return_value=b"%PDF-1.4 translated")
    tgt_container.download_blob = AsyncMock(return_value=tgt_download)
    tgt_container.delete_blob = AsyncMock()

    def get_container(name):
        if name == "source":
            return src_container
        return tgt_container

    mock_blob_client.get_container_client = MagicMock(side_effect=get_container)
    mock_blob_client.__aenter__ = AsyncMock(return_value=mock_blob_client)
    mock_blob_client.__aexit__ = AsyncMock(return_value=None)

    return mock_blob_client


class TestBatchTranslation:
    """Tests for binary documents routed through the batch API."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_translate_pdf_via_batch(self):
        """PDF → batch API → poll → download from blob."""
        translated = b"%PDF-1.4 translated"
        operation_url = f"{MOCK_ENDPOINT}/translator/document/batches/abc123"

        # Mock the batch start
        respx.post(f"{MOCK_ENDPOINT}/translator/document/batches").mock(
            return_value=httpx.Response(
                202,
                headers={"Operation-Location": operation_url},
            )
        )
        # Mock the poll (immediately succeeds)
        respx.get(operation_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "Succeeded",
                    "summary": {"total": 1, "failed": 0, "success": 1},
                },
            )
        )

        mock_blob = _mock_blob_service()
        with patch(
            "app.services.translator._create_blob_service_client",
            return_value=mock_blob,
        ):
            result = await translate_document(
                b"%PDF-1.4 original", "report.pdf", ".pdf", "en", "de"
            )

        assert result == translated
        # Verify upload was called
        src = mock_blob.get_container_client("source")
        src.upload_blob.assert_awaited_once()

    @pytest.mark.asyncio
    @respx.mock
    async def test_translate_docx_via_batch(self):
        """DOCX → batch API via blob storage."""
        operation_url = f"{MOCK_ENDPOINT}/translator/document/batches/def456"

        respx.post(f"{MOCK_ENDPOINT}/translator/document/batches").mock(
            return_value=httpx.Response(
                202,
                headers={"Operation-Location": operation_url},
            )
        )
        respx.get(operation_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "Succeeded",
                    "summary": {"total": 1, "failed": 0, "success": 1},
                },
            )
        )

        mock_blob = _mock_blob_service()
        with patch(
            "app.services.translator._create_blob_service_client",
            return_value=mock_blob,
        ):
            result = await translate_document(
                b"PK original", "doc.docx", ".docx", "en", "fr"
            )

        assert result == b"%PDF-1.4 translated"  # from mock

    @pytest.mark.asyncio
    @respx.mock
    async def test_batch_api_error_raises(self):
        """Non-202 from batch start raises RuntimeError."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document/batches").mock(
            return_value=httpx.Response(400, text="Bad Request")
        )

        mock_blob = _mock_blob_service()
        with patch(
            "app.services.translator._create_blob_service_client",
            return_value=mock_blob,
        ):
            with pytest.raises(RuntimeError, match="400"):
                await translate_document(
                    b"%PDF-1.4", "doc.pdf", ".pdf", "en", "de"
                )

    @pytest.mark.asyncio
    @respx.mock
    async def test_batch_poll_failure_raises(self):
        """Batch with Failed status raises RuntimeError."""
        operation_url = f"{MOCK_ENDPOINT}/translator/document/batches/fail1"

        respx.post(f"{MOCK_ENDPOINT}/translator/document/batches").mock(
            return_value=httpx.Response(
                202,
                headers={"Operation-Location": operation_url},
            )
        )
        respx.get(operation_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "Failed",
                    "error": {"code": "InternalError", "message": "Something broke"},
                },
            )
        )

        mock_blob = _mock_blob_service()
        with patch(
            "app.services.translator._create_blob_service_client",
            return_value=mock_blob,
        ):
            with pytest.raises(RuntimeError, match="Something broke"):
                await translate_document(
                    b"%PDF-1.4", "doc.pdf", ".pdf", "en", "de"
                )

    @pytest.mark.asyncio
    @respx.mock
    async def test_batch_cleans_up_blobs(self):
        """Blobs are deleted after successful batch translation."""
        operation_url = f"{MOCK_ENDPOINT}/translator/document/batches/clean1"

        respx.post(f"{MOCK_ENDPOINT}/translator/document/batches").mock(
            return_value=httpx.Response(
                202,
                headers={"Operation-Location": operation_url},
            )
        )
        respx.get(operation_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "Succeeded",
                    "summary": {"total": 1, "failed": 0, "success": 1},
                },
            )
        )

        mock_blob = _mock_blob_service()
        with patch(
            "app.services.translator._create_blob_service_client",
            return_value=mock_blob,
        ):
            await translate_document(
                b"%PDF-1.4", "doc.pdf", ".pdf", "en", "de"
            )

        # Both source and target blobs should have been deleted
        src = mock_blob.get_container_client("source")
        tgt = mock_blob.get_container_client("target")
        src.delete_blob.assert_awaited_once()
        tgt.delete_blob.assert_awaited_once()


# ---------------------------------------------------------------------------
# translate_document – query parameters
# ---------------------------------------------------------------------------

class TestQueryParams:
    @pytest.mark.asyncio
    @respx.mock
    async def test_explicit_source_language(self):
        """source_language passed as sourceLanguage query param."""
        route = respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"translated")
        )
        await translate_document(b"Hello", "f.txt", ".txt", "en", "de")
        request = route.calls[0].request
        assert "sourceLanguage=en" in str(request.url)
        assert "targetLanguage=de" in str(request.url)

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_detect_omits_source(self):
        """When source is 'auto', sourceLanguage param is not sent."""
        route = respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"translated")
        )
        await translate_document(b"Hello", "f.txt", ".txt", "auto", "fr")
        request = route.calls[0].request
        assert "sourceLanguage" not in str(request.url)
        assert "targetLanguage=fr" in str(request.url)

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_version_sent(self):
        """api-version=2024-05-01 is always included."""
        route = respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"ok")
        )
        await translate_document(b"Hello", "f.txt", ".txt", "en", "de")
        request = route.calls[0].request
        assert "api-version=2024-05-01" in str(request.url)


# ---------------------------------------------------------------------------
# translate_document – multipart upload
# ---------------------------------------------------------------------------

class TestMultipart:
    @pytest.mark.asyncio
    @respx.mock
    async def test_sends_multipart_form(self):
        """Text file is sent as multipart/form-data with 'document' field."""
        route = respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"translated")
        )
        await translate_document(b"Hello", "hello.txt", ".txt", "en", "de")
        request = route.calls[0].request
        content_type = request.headers.get("content-type", "")
        assert "multipart/form-data" in content_type


# ---------------------------------------------------------------------------
# translate_document – error handling (sync path)
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Sync API error handling for text files."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_runtime_error(self):
        """Unauthorized → RuntimeError with status code."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(RuntimeError, match="401"):
            await translate_document(b"Hello", "f.txt", ".txt", "en", "de")

    @pytest.mark.asyncio
    @respx.mock
    async def test_400_raises_runtime_error(self):
        """Bad request → RuntimeError with detail."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(400, text="Bad request: unsupported format")
        )
        with pytest.raises(RuntimeError, match="400"):
            await translate_document(b"Hello", "f.txt", ".txt", "en", "de")

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_raises_runtime_error(self):
        """Server error → RuntimeError."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(500, text="Internal server error")
        )
        with pytest.raises(RuntimeError, match="500"):
            await translate_document(b"Hello", "f.txt", ".txt", "en", "de")


# ---------------------------------------------------------------------------
# Content types mapping
# ---------------------------------------------------------------------------

class TestContentTypes:
    def test_pdf_content_type(self):
        assert CONTENT_TYPES[".pdf"] == "application/pdf"

    def test_docx_content_type(self):
        assert ".docx" in CONTENT_TYPES

    def test_txt_content_type(self):
        assert CONTENT_TYPES[".txt"] == "text/plain"

    def test_md_content_type(self):
        assert CONTENT_TYPES[".md"] == "text/markdown"


# ---------------------------------------------------------------------------
# Supported languages
# ---------------------------------------------------------------------------

class TestLanguages:
    def test_supported_languages(self):
        assert "en" in SUPPORTED_LANGUAGES
        assert "de" in SUPPORTED_LANGUAGES
        assert "auto" in SUPPORTED_LANGUAGES
        assert "zh-Hans" in SUPPORTED_LANGUAGES


# ---------------------------------------------------------------------------
# Endpoint validation
# ---------------------------------------------------------------------------

class TestEndpointValidation:
    def test_check_endpoint_warns_on_global(self, monkeypatch, caplog):
        """_check_endpoint() logs an error when the global endpoint is used."""
        monkeypatch.setattr(settings, "azure_translator_endpoint", GLOBAL_ENDPOINT)
        import logging
        with caplog.at_level(logging.ERROR):
            _check_endpoint()
        assert "ENDPOINT ERROR" in caplog.text
        assert "custom-domain" in caplog.text.lower() or "CUSTOM DOMAIN" in caplog.text

    def test_check_endpoint_silent_on_custom(self, caplog):
        """_check_endpoint() does NOT log when a custom endpoint is used."""
        import logging
        with caplog.at_level(logging.ERROR):
            _check_endpoint()
        assert "ENDPOINT ERROR" not in caplog.text

    @pytest.mark.asyncio
    async def test_translate_raises_on_global_endpoint(self, monkeypatch):
        """translate_document raises RuntimeError when global endpoint is configured."""
        monkeypatch.setattr(settings, "azure_translator_endpoint", GLOBAL_ENDPOINT)
        with pytest.raises(RuntimeError, match="custom-domain endpoint"):
            await translate_document(b"Hello", "hi.txt", ".txt", "en", "de")


# ---------------------------------------------------------------------------
# Error hints
# ---------------------------------------------------------------------------

class TestErrorHints:
    @pytest.mark.asyncio
    @respx.mock
    async def test_404_hint(self):
        """404 error includes endpoint hint."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(404, text="Not Found")
        )
        with pytest.raises(RuntimeError, match="custom-domain"):
            await translate_document(b"Hello", "f.txt", ".txt", "en", "de")

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_hint(self):
        """401 error includes key hint."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(RuntimeError, match="AZURE_TRANSLATOR_KEY"):
            await translate_document(b"Hello", "f.txt", ".txt", "en", "de")

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_hint(self):
        """403 error includes permission hint."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(403, text="Forbidden")
        )
        with pytest.raises(RuntimeError, match="permission"):
            await translate_document(b"Hello", "f.txt", ".txt", "en", "de")


# ---------------------------------------------------------------------------
# fetch_supported_formats
# ---------------------------------------------------------------------------

class TestFetchSupportedFormats:
    FORMATS_URL = f"{MOCK_ENDPOINT}/translator/document/formats"
    SAMPLE_RESPONSE = {
        "value": [
            {
                "format": "PlainText",
                "fileExtensions": [".txt"],
                "contentTypes": ["text/plain"],
            },
            {
                "format": "PortableDocumentFormat",
                "fileExtensions": [".pdf"],
                "contentTypes": ["application/pdf"],
            },
        ]
    }

    @pytest.mark.asyncio
    @respx.mock
    async def test_success_populates_content_types(self):
        """Successful fetch updates CONTENT_TYPES and SUPPORTED_FORMATS."""
        respx.get(self.FORMATS_URL).mock(
            return_value=httpx.Response(200, json=self.SAMPLE_RESPONSE)
        )
        result = await fetch_supported_formats(max_retries=1)
        assert len(result) == 2
        assert CONTENT_TYPES[".txt"] == "text/plain"
        assert CONTENT_TYPES[".pdf"] == "application/pdf"
        assert len(SUPPORTED_FORMATS) == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_failure_falls_back(self):
        """When all retries fail, CONTENT_TYPES reverts to fallback."""
        respx.get(self.FORMATS_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        result = await fetch_supported_formats(max_retries=1, backoff_base=0.01)
        assert result == []
        # Fallback content types should be restored
        assert CONTENT_TYPES == dict(_FALLBACK_CONTENT_TYPES)
        assert SUPPORTED_FORMATS == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_retry_succeeds_on_second_attempt(self):
        """fetch retries and succeeds on the second attempt."""
        route = respx.get(self.FORMATS_URL)
        route.side_effect = [
            httpx.Response(500, text="Error"),
            httpx.Response(200, json=self.SAMPLE_RESPONSE),
        ]
        result = await fetch_supported_formats(max_retries=2, backoff_base=0.01)
        assert len(result) == 2
        assert route.call_count == 2
