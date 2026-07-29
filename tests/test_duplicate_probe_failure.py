"""Regression tests for duplicate-probe failures.

If the duplicate-probe queries (``get_document_by_sha256`` / ``list_pages``)
raise before an import record exists, the import must still leave exactly one
failed import record when the database remains writable, while a confirmed
complete duplicate stays a strict zero-record no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pdf_test_helpers import PAGE_COUNT, build_sample_pdf

from src.database import Database
from src.document_service import DocumentService
from src.models import ImportStatus


class ShaProbeFailureDatabase(Database):
    """Fail only the SHA lookup while keeping all writes fully functional."""

    def get_document_by_sha256(self, sha256: str):  # noqa: ANN001, ANN201
        raise sqlite3.OperationalError("injected sha256 probe failure")


class ListPagesProbeFailureDatabase(Database):
    """Fail ``list_pages`` only when armed, keeping everything else intact."""

    armed = False

    def list_pages(self, document_id: int):  # noqa: ANN201
        if self.armed:
            raise sqlite3.OperationalError("injected list_pages probe failure")
        return super().list_pages(document_id)


def _make_service(database: Database, tmp_path: Path) -> DocumentService:
    return DocumentService(
        database=database,
        raw_dir=tmp_path / "data" / "raw",
        pages_dir=tmp_path / "data" / "pages",
        markdown_dir=tmp_path / "data" / "markdown",
    )


def _counts(tmp_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(tmp_path / "data" / "database" / "knowledge.db")
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "pages", "page_search", "import_records")
        }
        counts["orphan_pages"] = connection.execute(
            "SELECT COUNT(*) FROM pages p "
            "LEFT JOIN documents d ON d.id = p.document_id WHERE d.id IS NULL"
        ).fetchone()[0]
        counts["orphan_page_search"] = connection.execute(
            "SELECT COUNT(*) FROM page_search ps "
            "LEFT JOIN pages p ON p.id = ps.rowid WHERE p.id IS NULL"
        ).fetchone()[0]
        counts["processing_records"] = connection.execute(
            "SELECT COUNT(*) FROM import_records WHERE status = ?",
            (ImportStatus.PROCESSING.value,),
        ).fetchone()[0]
        counts["integrity"] = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        counts["fk_violations"] = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    finally:
        connection.close()
    counts["raw_files"] = len(list((tmp_path / "data" / "raw").glob("*")))
    counts["png_files"] = len(list((tmp_path / "data" / "pages").rglob("*.png")))
    counts["markdown_files"] = len(
        list((tmp_path / "data" / "markdown").rglob("*.md"))
    )
    return counts


def _import_record_rows(tmp_path: Path) -> list[tuple]:
    connection = sqlite3.connect(tmp_path / "data" / "database" / "knowledge.db")
    try:
        return connection.execute(
            "SELECT * FROM import_records ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


@pytest.fixture()
def sample_pdf(tmp_path: Path) -> bytes:
    return build_sample_pdf(tmp_path / "inputs" / "sample.pdf").read_bytes()


def test_sha_probe_failure_leaves_one_failed_import_record(
    tmp_path: Path, sample_pdf: bytes
) -> None:
    """A failing SHA probe must still produce exactly one failed record."""

    database = ShaProbeFailureDatabase(tmp_path / "data" / "database" / "knowledge.db")
    service = _make_service(database, tmp_path)

    with pytest.raises(Exception, match="injected sha256 probe failure"):
        service.import_pdf(sample_pdf, "sample.pdf")

    rows = _import_record_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row[4] == ImportStatus.FAILED.value
    assert row[11] != ""
    assert row[5] is None  # no document exists to associate

    counts = _counts(tmp_path)
    assert counts["documents"] == 0
    assert counts["pages"] == 0
    assert counts["page_search"] == 0
    assert counts["raw_files"] == 0
    assert counts["png_files"] == 0
    assert counts["markdown_files"] == 0
    assert counts["processing_records"] == 0
    assert counts["orphan_pages"] == 0
    assert counts["orphan_page_search"] == 0
    assert counts["integrity"] == "ok"
    assert counts["fk_violations"] == 0


def test_list_pages_probe_failure_leaves_one_failed_import_record(
    tmp_path: Path, sample_pdf: bytes
) -> None:
    """A failing list_pages probe must not look like a complete duplicate."""

    database = ListPagesProbeFailureDatabase(
        tmp_path / "data" / "database" / "knowledge.db"
    )
    service = _make_service(database, tmp_path)

    first = service.import_pdf(sample_pdf, "sample.pdf")
    assert first.duplicate is False
    before_counts = _counts(tmp_path)
    before_records = _import_record_rows(tmp_path)
    assert len(before_records) == 1
    assert before_records[0][4] == ImportStatus.COMPLETED.value

    database.armed = True
    with pytest.raises(Exception, match="injected list_pages probe failure"):
        service.import_pdf(sample_pdf, "sample.pdf")

    after_records = _import_record_rows(tmp_path)
    assert len(after_records) == 2
    assert after_records[0] == before_records[0]  # success record untouched
    failure = after_records[1]
    assert failure[4] == ImportStatus.FAILED.value
    assert failure[11] != ""

    after_counts = _counts(tmp_path)
    assert after_counts["documents"] == before_counts["documents"] == 1
    assert after_counts["pages"] == before_counts["pages"] == PAGE_COUNT
    assert after_counts["page_search"] == before_counts["page_search"] == PAGE_COUNT
    assert after_counts["raw_files"] == before_counts["raw_files"] == 1
    assert after_counts["png_files"] == before_counts["png_files"] == PAGE_COUNT
    assert after_counts["markdown_files"] == before_counts["markdown_files"] == 0
    assert after_counts["processing_records"] == 0
    assert after_counts["orphan_pages"] == 0
    assert after_counts["orphan_page_search"] == 0
    assert after_counts["integrity"] == "ok"
    assert after_counts["fk_violations"] == 0

    # The original document and its pages stay fully intact.
    assert database.get_document_by_sha256(first.document.sha256) is not None
    database.armed = False
    assert len(database.list_pages(first.document.id)) == PAGE_COUNT
