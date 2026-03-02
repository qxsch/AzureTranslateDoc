"""Tests for the in-memory translation job manager."""

import pytest
from unittest.mock import AsyncMock, patch
import time

from app.services.job_manager import (
    FileResult,
    TranslationJob,
    _language_suffix,
    _jobs,
    create_job,
    get_file_result,
    get_job,
    output_filename,
    process_job,
    _JOB_TTL,
)
from app.config import settings


MOCK_ENDPOINT = "https://my-translator.cognitiveservices.azure.com"


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(settings, "azure_translator_endpoint", MOCK_ENDPOINT)
    monkeypatch.setattr(settings, "azure_translator_key", "test-key")
    monkeypatch.setattr(settings, "azure_translator_region", "eastus")
    monkeypatch.setattr(settings, "use_managed_identity", False)


@pytest.fixture(autouse=True)
def _clear_jobs():
    """Ensure job store is empty between tests."""
    _jobs.clear()
    yield
    _jobs.clear()


# ---------------------------------------------------------------------------
# Output filename
# ---------------------------------------------------------------------------


class TestOutputFilename:
    def test_german_pdf(self):
        assert output_filename("myFile.pdf", "de") == "myFile_german.pdf"

    def test_french_docx(self):
        assert output_filename("document.docx", "fr") == "document_french.docx"

    def test_spanish_txt(self):
        assert output_filename("notes.txt", "es") == "notes_spanish.txt"

    def test_chinese_simplified(self):
        assert output_filename("report.pdf", "zh-Hans") == "report_chinese-simplified.pdf"

    def test_japanese_md(self):
        assert output_filename("readme.md", "ja") == "readme_japanese.md"

    def test_no_extension(self):
        assert output_filename("README", "de") == "README_german"

    def test_multiple_dots(self):
        assert output_filename("my.report.v2.pdf", "fr") == "my.report.v2_french.pdf"


class TestLanguageSuffix:
    def test_simple_name(self):
        assert _language_suffix("de") == "german"

    def test_parenthesised_name(self):
        assert _language_suffix("zh-Hans") == "chinese-simplified"

    def test_unknown_code(self):
        assert _language_suffix("zz") == "zz"


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------


class TestCreateJob:
    def test_creates_job_with_files(self):
        job = create_job(
            [("hello.txt", ".txt", b"Hello"), ("doc.pdf", ".pdf", b"%PDF")],
            "en", "de",
        )
        assert job.id
        assert job.status == "pending"
        assert len(job.files) == 2
        assert job.files[0].original_name == "hello.txt"
        assert job.files[0].output_name == "hello_german.txt"
        assert job.files[1].output_name == "doc_german.pdf"

    def test_job_stored(self):
        job = create_job([("f.txt", ".txt", b"hi")], "en", "fr")
        assert get_job(job.id) is job

    def test_unknown_job_returns_none(self):
        assert get_job("nonexistent") is None


class TestGetFileResult:
    def test_returns_file(self):
        job = create_job([("a.txt", ".txt", b"A"), ("b.txt", ".txt", b"B")], "en", "de")
        fr = get_file_result(job.id, 1)
        assert fr is not None
        assert fr.original_name == "b.txt"

    def test_out_of_range(self):
        job = create_job([("a.txt", ".txt", b"A")], "en", "de")
        assert get_file_result(job.id, 5) is None

    def test_invalid_job(self):
        assert get_file_result("nope", 0) is None


class TestPurgeExpired:
    def test_old_jobs_purged(self):
        job = create_job([("f.txt", ".txt", b"hi")], "en", "de")
        job.created_at = time.time() - _JOB_TTL - 1
        # Creating another job triggers purge
        create_job([("g.txt", ".txt", b"bye")], "en", "fr")
        assert get_job(job.id) is None


# ---------------------------------------------------------------------------
# process_job
# ---------------------------------------------------------------------------


class TestProcessJob:
    @pytest.mark.asyncio
    async def test_all_files_translated(self):
        job = create_job(
            [("a.txt", ".txt", b"A"), ("b.txt", ".txt", b"B")],
            "en", "de",
        )
        with patch(
            "app.services.job_manager.translate_document",
            new_callable=AsyncMock,
            return_value=b"Translated",
        ):
            await process_job(job)

        assert job.status == "completed"
        assert all(f.status == "completed" for f in job.files)
        assert all(f._result_bytes == b"Translated" for f in job.files)

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        job = create_job(
            [("good.txt", ".txt", b"OK"), ("bad.txt", ".txt", b"FAIL")],
            "en", "de",
        )

        call_count = 0

        async def mock_translate(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs["filename"] == "bad.txt":
                raise RuntimeError("Boom")
            return b"Translated"

        with patch(
            "app.services.job_manager.translate_document",
            side_effect=mock_translate,
        ):
            await process_job(job)

        assert job.status == "completed_with_errors"
        assert job.files[0].status == "completed"
        assert job.files[1].status == "error"
        assert "Boom" in job.files[1].error

    @pytest.mark.asyncio
    async def test_input_bytes_freed_after_success(self):
        job = create_job([("f.txt", ".txt", b"Hello")], "en", "de")
        with patch(
            "app.services.job_manager.translate_document",
            new_callable=AsyncMock,
            return_value=b"Hallo",
        ):
            await process_job(job)

        assert job.files[0]._input_bytes == b""
        assert job.files[0]._result_bytes == b"Hallo"
