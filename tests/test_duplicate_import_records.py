"""Regression tests for duplicate imports and ``import_records`` accounting.

A repeated import of an already imported file must be a zero-write no-op:
no new document, pages, search rows, files — and no new import record.
These tests run the real ``Database`` / ``PdfService`` / ``DocumentService``
stack against a real generated PDF under ``tmp_path``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pdf_test_helpers import PAGE_COUNT, build_sample_pdf

from src.database import Database
from src.document_service import DocumentService
from src.models import ImportStatus


@pytest.fixture()
def service_env(tmp_path: Path) -> dict[str, object]:
    """Create a real service stack and a sample PDF under ``tmp_path``."""

    database = Database(tmp_path / "data" / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "data" / "raw",
        pages_dir=tmp_path / "data" / "pages",
        markdown_dir=tmp_path / "data" / "markdown",
    )
    pdf_path = build_sample_pdf(tmp_path / "inputs" / "sample.pdf")
    return {
        "database": database,
        "service": service,
        "pdf_bytes": pdf_path.read_bytes(),
        "tmp_path": tmp_path,
    }


def _counts(tmp_path: Path) -> dict[str, int]:
    """Collect database table counts and file counts for zero-delta checks."""

    connection = sqlite3.connect(tmp_path / "data" / "database" / "knowledge.db")
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "pages", "page_search", "import_records")
        }
    finally:
        connection.close()
    counts["raw_files"] = len(list((tmp_path / "data" / "raw").glob("*")))
    counts["png_files"] = len(list((tmp_path / "data" / "pages").rglob("*.png")))
    counts["markdown_files"] = len(
        list((tmp_path / "data" / "markdown").rglob("*.md"))
    )
    return counts


def _import_record_rows(tmp_path: Path) -> list[tuple]:
    """Return full import_records rows so existing records can be diffed."""

    connection = sqlite3.connect(tmp_path / "data" / "database" / "knowledge.db")
    try:
        return connection.execute(
            "SELECT * FROM import_records ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def test_first_import_creates_one_completed_import_record(
    service_env: dict[str, object],
) -> None:
    """Scenario A: first import creates the document and one success record."""

    service = service_env["service"]
    result = service.import_pdf(service_env["pdf_bytes"], "sample.pdf")

    assert result.duplicate is False
    assert result.document.import_status is ImportStatus.COMPLETED
    assert result.document.page_count == PAGE_COUNT
    assert len(result.pages) == PAGE_COUNT

    counts = _counts(service_env["tmp_path"])
    assert counts["documents"] == 1
    assert counts["pages"] == PAGE_COUNT
    assert counts["page_search"] == PAGE_COUNT
    assert counts["raw_files"] == 1
    assert counts["png_files"] == PAGE_COUNT
    assert counts["markdown_files"] == 0

    rows = _import_record_rows(service_env["tmp_path"])
    assert len(rows) == 1
    row = rows[0]
    assert row[4] == ImportStatus.COMPLETED.value
    assert row[5] == result.document.id
    assert row[6] == PAGE_COUNT
    assert row[11] == ""


def test_duplicate_import_creates_no_import_record(
    service_env: dict[str, object],
) -> None:
    """Scenario B: re-importing the same file is a full zero-delta no-op."""

    service = service_env["service"]
    first = service.import_pdf(service_env["pdf_bytes"], "sample.pdf")
    assert first.duplicate is False

    before_counts = _counts(service_env["tmp_path"])
    before_records = _import_record_rows(service_env["tmp_path"])
    assert len(before_records) == 1

    second = service.import_pdf(service_env["pdf_bytes"], "sample.pdf")

    assert second.duplicate is True
    assert second.document.id == first.document.id
    assert second.import_record is None

    after_counts = _counts(service_env["tmp_path"])
    after_records = _import_record_rows(service_env["tmp_path"])

    assert after_counts == before_counts
    assert after_records == before_records
    statuses = {row[4] for row in after_records}
    assert statuses == {ImportStatus.COMPLETED.value}

    # No orphaned pages or search rows may appear after the no-op import.
    connection = sqlite3.connect(
        service_env["tmp_path"] / "data" / "database" / "knowledge.db"
    )
    try:
        orphan_pages = connection.execute(
            "SELECT COUNT(*) FROM pages p "
            "LEFT JOIN documents d ON d.id = p.document_id "
            "WHERE d.id IS NULL"
        ).fetchone()[0]
        assert orphan_pages == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
