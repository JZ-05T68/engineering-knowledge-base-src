"""P1 regression tests: recovering interrupted raw-PDF source saving.

Covers the S3B-P1 finding: a killed import wrote the uploaded PDF directly to
the formal raw path (``open(..., "xb")`` + one ``write``), so a process kill
left a partial file at the content-addressed target.  The old
``_choose_raw_path`` then sidestepped the residue with ``_1``/``_2`` suffixes,
leaving a permanent, unreferenced partial file in the managed raw directory
after every kill.  The fix saves through a verified same-directory temp file
plus ``os.replace``, reuses only hash-identical files, and rebuilds zero-byte
or hash-mismatching residue of the same target in place.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pdf_test_helpers import assert_decodable_png, build_sample_pdf

import src.document_service as document_service_module
from src.backup_service import BackupService, validate_backup
from src.database import Database
from src.diagnostic_service import DiagnosticService, DiagnosticStatus
from src.document_service import TEMP_RAW_INFIX, DocumentImportError, DocumentService
from src.migrations import SCHEMA_VERSION
from src.models import ImportStatus
from src.pdf_service import PdfService

PAGE_COUNT = 10


def _make_service(tmp_path: Path) -> tuple[Database, DocumentService]:
    database = Database(tmp_path / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
    )
    return database, service


def _raw_target(service: DocumentService, content: bytes, filename: str) -> Path:
    sha256 = hashlib.sha256(content).hexdigest()
    return service._choose_raw_path(sha256, filename)


def _raw_pdfs(raw_dir: Path) -> list[Path]:
    return sorted(raw_dir.glob("*.pdf"))


def _raw_temps(raw_dir: Path) -> list[Path]:
    return sorted(raw_dir.glob(f"*{TEMP_RAW_INFIX}*"))


def _fts_count(database: Database) -> int:
    with sqlite3.connect(database.database_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM page_search").fetchone()[0])


def _duplicate_page_rows(database: Database) -> list[tuple[int, int, int]]:
    with sqlite3.connect(database.database_path) as connection:
        return connection.execute(
            """
            SELECT document_id, page_number, COUNT(*) AS copies
            FROM pages GROUP BY document_id, page_number HAVING copies > 1
            """
        ).fetchall()


def test_fresh_import_saves_verified_raw_and_completes(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()

    result = service.import_pdf(content, source.name, title="样例")

    assert result.document.import_status is ImportStatus.COMPLETED
    sha256 = hashlib.sha256(content).hexdigest()
    target = tmp_path / "raw" / f"{sha256}_sample.pdf"
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert Path(result.document.source_path) == target
    assert PdfService.calculate_sha256(target) == sha256
    assert _raw_temps(tmp_path / "raw") == []
    assert result.diagnostics.failed_page_numbers == ()
    pages_dir = tmp_path / "pages" / str(result.document.id)
    pngs = sorted(pages_dir.glob("*.png"))
    assert len(pngs) == PAGE_COUNT
    for png in pngs:
        assert_decodable_png(png)


def test_valid_existing_raw_is_reused_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    target = _raw_target(service, content, source.name)
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    original_mtime = target.stat().st_mtime_ns
    # Any attempt to rewrite the reusable file explodes the import.
    def forbidden_write(temp_path: Path, data: bytes) -> None:
        raise AssertionError(f"不得重写可复用的正式原文件：{temp_path}")

    monkeypatch.setattr(document_service_module, "_write_pdf_content", forbidden_write)

    result = service.import_pdf(content, source.name, title="样例")

    assert result.document.import_status is ImportStatus.COMPLETED
    assert target.read_bytes() == content
    assert target.stat().st_mtime_ns == original_mtime
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert _raw_temps(tmp_path / "raw") == []
    assert database.dashboard_stats().documents == 1


def test_zero_byte_raw_is_rebuilt_automatically(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    target = _raw_target(service, content, source.name)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")  # killed run residue: created, never written

    result = service.import_pdf(content, source.name, title="样例")

    assert result.document.import_status is ImportStatus.COMPLETED
    assert target.read_bytes() == content
    assert PdfService.calculate_sha256(target) == hashlib.sha256(content).hexdigest()
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert _raw_temps(tmp_path / "raw") == []


def test_hash_mismatch_raw_is_rebuilt_in_place(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    target = _raw_target(service, content, source.name)
    target.parent.mkdir(parents=True)
    target.write_bytes(content[: len(content) // 3])  # partial interrupted save

    result = service.import_pdf(content, source.name, title="样例")

    assert result.document.import_status is ImportStatus.COMPLETED
    assert target.read_bytes() == content
    # No sidestepped ``_1`` copy and no leftover partial file.
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert _raw_temps(tmp_path / "raw") == []
    assert database.dashboard_stats().documents == 1


def test_stale_temp_of_same_target_does_not_block_retry(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    target = _raw_target(service, content, source.name)
    target.parent.mkdir(parents=True)
    stale_one = target.with_name(f"{target.name}{TEMP_RAW_INFIX}deadbeef01")
    stale_two = target.with_name(f"{target.name}{TEMP_RAW_INFIX}deadbeef02")
    stale_one.write_bytes(content[:100])
    stale_two.write_bytes(b"")

    result = service.import_pdf(content, source.name, title="样例")

    assert result.document.import_status is ImportStatus.COMPLETED
    assert not stale_one.exists()
    assert not stale_two.exists()
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert _raw_temps(tmp_path / "raw") == []


def test_temp_cleanup_only_touches_current_target(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    target = _raw_target(service, content, source.name)
    target.parent.mkdir(parents=True)
    own_stale = target.with_name(f"{target.name}{TEMP_RAW_INFIX}deadbeef01")
    own_stale.write_bytes(b"partial")
    other_hash = hashlib.sha256(b"another pdf").hexdigest()
    other_temp = target.parent / f"{other_hash}_other.pdf{TEMP_RAW_INFIX}deadbeef99"
    other_temp.write_bytes(b"someone else's interrupted save")

    result = service.import_pdf(content, source.name, title="样例")

    assert result.document.import_status is ImportStatus.COMPLETED
    assert not own_stale.exists()
    assert other_temp.read_bytes() == b"someone else's interrupted save"


def test_temp_write_failure_leaves_no_formal_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()

    def crashing_write(temp_path: Path, data: bytes) -> None:
        temp_path.write_bytes(data[: len(data) // 2])
        raise OSError("simulated write crash")

    monkeypatch.setattr(document_service_module, "_write_pdf_content", crashing_write)

    with pytest.raises(DocumentImportError):
        service.import_pdf(content, source.name, title="样例")

    assert _raw_pdfs(tmp_path / "raw") == []
    assert _raw_temps(tmp_path / "raw") == []
    assert database.dashboard_stats().documents == 0
    records = database.list_import_records()
    assert len(records) == 1
    assert records[0].status is ImportStatus.FAILED
    assert records[0].error_message != ""


def test_failed_rebuild_preserves_existing_formal_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    target = _raw_target(service, content, source.name)
    target.parent.mkdir(parents=True)
    residue = content[: len(content) // 3]
    target.write_bytes(residue)

    def crashing_write(temp_path: Path, data: bytes) -> None:
        raise OSError("simulated write crash")

    monkeypatch.setattr(document_service_module, "_write_pdf_content", crashing_write)

    with pytest.raises(DocumentImportError):
        service.import_pdf(content, source.name, title="样例")

    # Never deleted first, never replaced by unverified bytes.
    assert target.read_bytes() == residue
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert _raw_temps(tmp_path / "raw") == []


def test_temp_hash_mismatch_skips_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    real_calculate = PdfService.calculate_sha256

    def lying_calculate(file_path: Path | str, chunk_size: int = 1024 * 1024) -> str:
        if TEMP_RAW_INFIX in Path(file_path).name:
            return "0" * 64
        return real_calculate(file_path, chunk_size)

    monkeypatch.setattr(PdfService, "calculate_sha256", staticmethod(lying_calculate))

    with pytest.raises(DocumentImportError, match="校验失败"):
        service.import_pdf(content, source.name, title="样例")

    assert _raw_pdfs(tmp_path / "raw") == []
    assert _raw_temps(tmp_path / "raw") == []
    assert database.dashboard_stats().documents == 0


def test_catchable_save_failure_marks_records_failed_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()

    def crashing_write(temp_path: Path, data: bytes) -> None:
        raise OSError("simulated write crash")

    monkeypatch.setattr(document_service_module, "_write_pdf_content", crashing_write)
    with pytest.raises(DocumentImportError):
        service.import_pdf(content, source.name, title="样例")

    failed_records = database.list_import_records()
    assert len(failed_records) == 1
    assert failed_records[0].status is ImportStatus.FAILED
    assert failed_records[0].error_message != ""
    assert database.dashboard_stats().documents == 0

    monkeypatch.undo()
    retry = service.import_pdf(content, source.name, title="样例")

    assert retry.document.import_status is ImportStatus.COMPLETED
    assert database.dashboard_stats().documents == 1
    assert _raw_pdfs(tmp_path / "raw") == [_raw_target(service, content, source.name)]
    assert _raw_temps(tmp_path / "raw") == []


def test_retry_after_interrupted_save_recovers_consistent_counts(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    target = _raw_target(service, content, source.name)
    target.parent.mkdir(parents=True)
    # Killed first attempt: partial formal file, stale temp, stuck record.
    target.write_bytes(content[: len(content) // 3])
    stale = target.with_name(f"{target.name}{TEMP_RAW_INFIX}deadbeef01")
    stale.write_bytes(content[:100])
    stuck = database.create_import_record(source.name, "样例", sha256)
    database.update_import_record(stuck.id, status=ImportStatus.PROCESSING)

    retry = service.import_pdf(content, source.name, title="样例")

    assert retry.document.import_status is ImportStatus.COMPLETED
    assert retry.import_record is not None
    assert retry.import_record.status is ImportStatus.COMPLETED
    stats = database.dashboard_stats()
    assert stats.documents == 1
    assert stats.pages == PAGE_COUNT
    assert _fts_count(database) == PAGE_COUNT
    assert _duplicate_page_rows(database) == []
    assert PdfService.calculate_sha256(target) == sha256
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert _raw_temps(tmp_path / "raw") == []
    pages_dir = tmp_path / "pages" / str(retry.document.id)
    pngs = sorted(pages_dir.glob("*.png"))
    assert len(pngs) == PAGE_COUNT
    for png in pngs:
        assert_decodable_png(png)


def test_repeated_import_stays_single_document_without_rewrite(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path)
    source = build_sample_pdf(tmp_path / "sample.pdf")
    content = source.read_bytes()

    first = service.import_pdf(content, source.name, title="样例")
    target = Path(first.document.source_path)
    original_mtime = target.stat().st_mtime_ns
    second = service.import_pdf(content, source.name, title="样例")

    assert second.duplicate is True
    assert second.document.id == first.document.id
    assert database.dashboard_stats().documents == 1
    assert target.read_bytes() == content
    assert target.stat().st_mtime_ns == original_mtime
    assert _raw_pdfs(tmp_path / "raw") == [target]
    assert _raw_temps(tmp_path / "raw") == []


def test_raw_temp_files_are_not_treated_as_formal_assets(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=data_dir / "raw",
        pages_dir=data_dir / "pages",
        markdown_dir=data_dir / "markdown",
    )
    source = build_sample_pdf(tmp_path / "sample.pdf")
    result = service.import_pdf(source.read_bytes(), source.name, title="样例")
    formal = Path(result.document.source_path)
    # A killed later run left this temp next to the valid formal file.
    temp = formal.with_name(f"{formal.name}{TEMP_RAW_INFIX}deadbeef99")
    temp.write_bytes(b"interrupted leftover")
    assert temp.suffix != ".pdf"

    backup_service = BackupService(
        app_version="0.2.2",
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        pages_dir=data_dir / "pages",
        markdown_dir=data_dir / "markdown",
        database_path=data_dir / "database" / "knowledge.db",
        backups_dir=tmp_path / "backups",
    )
    backup = backup_service.create_backup()
    validation = validate_backup(
        backup.backup_path,
        expected_app_version="0.2.2",
        expected_schema_version=SCHEMA_VERSION,
    )
    assert validation.valid, validation.errors

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    diagnostic = DiagnosticService(
        app_version="0.2.2",
        project_root=tmp_path,
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        pages_dir=data_dir / "pages",
        markdown_dir=data_dir / "markdown",
        database_path=data_dir / "database" / "knowledge.db",
        backups_dir=tmp_path / "backups",
        logs_dir=logs_dir,
        log_path=logs_dir / "engineering-kb.log",
        host="127.0.0.1",
        port=8501,
        disk_usage=lambda path: SimpleNamespace(free=10 * 1024**3),
        access_check=lambda path, mode: True,
        listener_addresses=lambda port: ("127.0.0.1",),
        port_is_open=lambda port: False,
        health_check=lambda port: True,
    )
    snapshot = diagnostic.run()
    pdf_check = next(check for check in snapshot.checks if check.key == "pdf_files")
    assert pdf_check.status is DiagnosticStatus.NORMAL

    service.delete_document(result.document.id, confirmed=True)
    assert not formal.exists()
    # The leftover temp is not the document's file: delete leaves it alone.
    assert temp.is_file()
