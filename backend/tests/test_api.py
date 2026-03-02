"""Integration tests for API endpoints (async job-based translation)."""

import httpx
import pytest
import respx
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.services.job_manager import _jobs

MOCK_ENDPOINT = "https://my-translator.cognitiveservices.azure.com"
MOCK_STORAGE = "mystorageaccount"
MOCK_CONN_STR = (
    "DefaultEndpointsProtocol=https;"
    f"AccountName={MOCK_STORAGE};"
    "AccountKey=dGVzdGtleQ==;"
    "EndpointSuffix=core.windows.net"
)


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(settings, "azure_translator_endpoint", MOCK_ENDPOINT)
    monkeypatch.setattr(settings, "azure_translator_key", "test-key")
    monkeypatch.setattr(settings, "azure_translator_region", "eastus")
    monkeypatch.setattr(settings, "use_managed_identity", False)
    monkeypatch.setattr(settings, "max_file_size_mb", 1)
    monkeypatch.setattr(settings, "azure_storage_account_name", MOCK_STORAGE)
    monkeypatch.setattr(settings, "azure_storage_connection_string", MOCK_CONN_STR)


@pytest.fixture(autouse=True)
def _clear_jobs():
    """Ensure job store is empty between tests."""
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _mock_blob_service(translated_bytes: bytes = b"translated-content"):
    """Return a mock BlobServiceClient for batch translation tests."""
    mock = MagicMock()
    src = MagicMock()
    src.upload_blob = AsyncMock()
    src.delete_blob = AsyncMock()
    tgt = MagicMock()
    dl = AsyncMock()
    dl.readall = AsyncMock(return_value=translated_bytes)
    tgt.download_blob = AsyncMock(return_value=dl)
    tgt.delete_blob = AsyncMock()

    def get_container(name):
        return src if name == "source" else tgt

    mock.get_container_client = MagicMock(side_effect=get_container)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


# ---------------------------------------------------------------------------
# Health & Languages
# ---------------------------------------------------------------------------


class TestHealthAndLanguages:
    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_languages(self, client):
        resp = client.get("/api/languages")
        assert resp.status_code == 200
        langs = resp.json()["languages"]
        codes = [l["code"] for l in langs]
        assert "en" in codes
        assert "de" in codes
        assert "auto" in codes


# ---------------------------------------------------------------------------
# Formats endpoint
# ---------------------------------------------------------------------------


class TestFormatsEndpoint:
    def test_formats_returns_fallback_when_empty(self, client):
        """When SUPPORTED_FORMATS is empty, /api/formats returns a fallback list."""
        from app.services import translator
        original = list(translator.SUPPORTED_FORMATS)
        translator.SUPPORTED_FORMATS.clear()
        try:
            resp = client.get("/api/formats")
            assert resp.status_code == 200
            data = resp.json()
            assert "formats" in data
            formats = data["formats"]
            extensions = [ext for f in formats for ext in f["fileExtensions"]]
            assert ".pdf" in extensions
            assert ".txt" in extensions
        finally:
            translator.SUPPORTED_FORMATS.extend(original)

    def test_formats_returns_api_data_when_populated(self, client):
        """When SUPPORTED_FORMATS is populated, /api/formats returns it directly."""
        from app.services import translator
        original = list(translator.SUPPORTED_FORMATS)
        sample = [
            {"format": "PlainText", "fileExtensions": [".txt"], "contentTypes": ["text/plain"]},
            {"format": "PDF", "fileExtensions": [".pdf"], "contentTypes": ["application/pdf"]},
        ]
        translator.SUPPORTED_FORMATS.clear()
        translator.SUPPORTED_FORMATS.extend(sample)
        try:
            resp = client.get("/api/formats")
            assert resp.status_code == 200
            data = resp.json()
            assert data["formats"] == sample
        finally:
            translator.SUPPORTED_FORMATS.clear()
            translator.SUPPORTED_FORMATS.extend(original)


# ---------------------------------------------------------------------------
# Translate endpoint – submit
# ---------------------------------------------------------------------------


class TestTranslateSubmit:
    @respx.mock
    def test_submit_returns_202(self, client):
        """POST /api/translate returns 202 with a job_id."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"Hallo")
        )
        resp = client.post(
            "/api/translate",
            files=[("files", ("hello.txt", b"Hello", "text/plain"))],
            data={"source_language": "en", "target_language": "de"},
        )
        assert resp.status_code == 202
        assert "job_id" in resp.json()

    @respx.mock
    def test_multi_file_submit(self, client):
        """Multiple files accepted in one request."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"Translated")
        )
        resp = client.post(
            "/api/translate",
            files=[
                ("files", ("a.txt", b"Hello", "text/plain")),
                ("files", ("b.md", b"# World", "text/markdown")),
            ],
            data={"source_language": "en", "target_language": "fr"},
        )
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Full translate → poll → download flow
# ---------------------------------------------------------------------------


class TestFullFlow:
    @respx.mock
    def test_txt_full_flow(self, client):
        """TXT: submit → poll (completed) → download with language suffix."""
        translated = b"Hallo Welt"
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=translated)
        )

        # 1. Submit
        resp = client.post(
            "/api/translate",
            files=[("files", ("hello.txt", b"Hello world", "text/plain"))],
            data={"source_language": "en", "target_language": "de"},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # 2. Poll — BackgroundTasks already completed in TestClient
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["files"][0]["status"] == "completed"
        assert data["files"][0]["output_name"] == "hello_german.txt"

        # 3. Download
        resp = client.get(f"/api/jobs/{job_id}/files/0/download")
        assert resp.status_code == 200
        assert resp.content == translated
        assert "hello_german.txt" in resp.headers["content-disposition"]

    @respx.mock
    def test_pdf_full_flow(self, client):
        """PDF: submit → batch API → poll → download."""
        fake_pdf = b"%PDF-1.4 translated"
        operation_url = f"{MOCK_ENDPOINT}/translator/document/batches/test1"

        respx.post(f"{MOCK_ENDPOINT}/translator/document/batches").mock(
            return_value=httpx.Response(
                202, headers={"Operation-Location": operation_url}
            )
        )
        respx.get(operation_url).mock(
            return_value=httpx.Response(
                200,
                json={"status": "Succeeded", "summary": {"total": 1, "failed": 0, "success": 1}},
            )
        )

        mock_blob = _mock_blob_service(fake_pdf)
        with patch(
            "app.services.translator._create_blob_service_client",
            return_value=mock_blob,
        ):
            resp = client.post(
                "/api/translate",
                files=[("files", ("report.pdf", b"%PDF-1.4 original", "application/pdf"))],
                data={"source_language": "en", "target_language": "de"},
            )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        resp = client.get(f"/api/jobs/{job_id}")
        data = resp.json()
        assert data["status"] == "completed"
        assert data["files"][0]["output_name"] == "report_german.pdf"

        resp = client.get(f"/api/jobs/{job_id}/files/0/download")
        assert resp.status_code == 200
        assert resp.content == fake_pdf

    @respx.mock
    def test_multi_file_flow(self, client):
        """Two text files translated with correct language suffixes."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"Traduit")
        )

        resp = client.post(
            "/api/translate",
            files=[
                ("files", ("doc1.txt", b"Hello", "text/plain")),
                ("files", ("doc2.md", b"# World", "text/markdown")),
            ],
            data={"source_language": "en", "target_language": "fr"},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        resp = client.get(f"/api/jobs/{job_id}")
        data = resp.json()
        assert data["status"] == "completed"
        assert len(data["files"]) == 2
        assert data["files"][0]["output_name"] == "doc1_french.txt"
        assert data["files"][1]["output_name"] == "doc2_french.md"

    @respx.mock
    def test_md_full_flow(self, client):
        """Markdown: submit → poll → download."""
        translated = b"# Titre\n\nBonjour"
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=translated)
        )

        resp = client.post(
            "/api/translate",
            files=[("files", ("readme.md", b"# Title\n\nHello", "text/markdown"))],
            data={"source_language": "en", "target_language": "fr"},
        )
        job_id = resp.json()["job_id"]

        resp = client.get(f"/api/jobs/{job_id}/files/0/download")
        assert resp.status_code == 200
        assert resp.content == translated

    @respx.mock
    def test_auto_detect_source(self, client):
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(200, content=b"Bonjour")
        )
        resp = client.post(
            "/api/translate",
            files=[("files", ("hi.txt", b"Hello", "text/plain"))],
            data={"source_language": "auto", "target_language": "fr"},
        )
        assert resp.status_code == 202

    @respx.mock
    def test_translation_error_visible_in_job(self, client):
        """When the translator raises, the file shows error status."""
        respx.post(f"{MOCK_ENDPOINT}/translator/document:translate").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        resp = client.post(
            "/api/translate",
            files=[("files", ("f.txt", b"Hello", "text/plain"))],
            data={"source_language": "en", "target_language": "de"},
        )
        job_id = resp.json()["job_id"]

        resp = client.get(f"/api/jobs/{job_id}")
        data = resp.json()
        assert data["status"] == "completed_with_errors"
        assert data["files"][0]["status"] == "error"
        assert data["files"][0]["error"] is not None


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------


class TestJobEndpoints:
    def test_unknown_job_returns_404(self, client):
        resp = client.get("/api/jobs/nonexistent")
        assert resp.status_code == 404

    @respx.mock
    def test_download_before_complete(self, client):
        """Downloading a file that hasn't completed returns 409."""
        # Create a job but don't process it
        from app.services.job_manager import create_job
        job = create_job([("f.txt", ".txt", b"hi")], "en", "de")

        resp = client.get(f"/api/jobs/{job.id}/files/0/download")
        assert resp.status_code == 409

    def test_download_invalid_file_index(self, client):
        from app.services.job_manager import create_job
        job = create_job([("f.txt", ".txt", b"hi")], "en", "de")

        resp = client.get(f"/api/jobs/{job.id}/files/99/download")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unsupported_file_type(self, client):
        resp = client.post(
            "/api/translate",
            files=[("files", ("image.png", b"\x89PNG", "image/png"))],
            data={"source_language": "en", "target_language": "de"},
        )
        assert resp.status_code == 400
        assert "Unsupported" in resp.json()["detail"]

    def test_file_too_large(self, client):
        big = b"x" * (2 * 1024 * 1024)  # 2 MB > 1 MB limit
        resp = client.post(
            "/api/translate",
            files=[("files", ("big.txt", big, "text/plain"))],
            data={"source_language": "en", "target_language": "de"},
        )
        assert resp.status_code == 413

    def test_invalid_source_language(self, client):
        resp = client.post(
            "/api/translate",
            files=[("files", ("f.txt", b"hi", "text/plain"))],
            data={"source_language": "xx", "target_language": "de"},
        )
        assert resp.status_code == 400

    def test_invalid_target_language(self, client):
        resp = client.post(
            "/api/translate",
            files=[("files", ("f.txt", b"hi", "text/plain"))],
            data={"source_language": "en", "target_language": "auto"},
        )
        assert resp.status_code == 400

    def test_missing_file(self, client):
        resp = client.post(
            "/api/translate",
            data={"source_language": "en", "target_language": "de"},
        )
        assert resp.status_code == 422  # FastAPI validation error
