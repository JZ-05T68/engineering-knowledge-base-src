"""v0.0.8 read-only diagnostics and redacted-report tests."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backup_service import BackupService
from src.database import Database
from src.diagnostic_service import (
    DiagnosticService,
    DiagnosticStatus,
    generate_diagnostic_report,
    redact_path,
)
from src.models import ImportStatus, PageStatus


def _environment(root: Path) -> tuple[DiagnosticService, BackupService, dict[str, Path]]:
    data = root / "data"
    paths = {
        "data": data,
        "raw": data / "raw",
        "pages": data / "pages",
        "markdown": data / "markdown",
        "database": data / "database" / "knowledge.db",
        "backups": root / "backups",
        "logs": root / "logs",
    }
    for key in ("raw", "pages", "markdown", "logs"):
        paths[key].mkdir(parents=True, exist_ok=True)
    pdf = paths["raw"] / "manual.pdf"
    pdf.write_bytes(b"private pdf")
    image = paths["pages"] / "1" / "page_0001.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    markdown = paths["markdown"] / "1" / "page_0001.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text("SUPER_PRIVATE_BODY", encoding="utf-8")
    database = Database(paths["database"])
    document = database.create_document(
        title="私有标题",
        filename="manual.pdf",
        source_path=pdf,
        sha256=hashlib.sha256(b"private pdf").hexdigest(),
    )
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image,
        extracted_text="SUPER_PRIVATE_BODY",
        markdown_content="SUPER_PRIVATE_BODY",
        markdown_path=markdown,
        status=PageStatus.REVIEWED,
    )
    database.update_document_page_count(document.id, 1)
    backup_service = BackupService(
        app_version="0.0.8",
        data_dir=paths["data"],
        raw_dir=paths["raw"],
        pages_dir=paths["pages"],
        markdown_dir=paths["markdown"],
        database_path=paths["database"],
        backups_dir=paths["backups"],
    )
    diagnostic = _diagnostic(root, paths)
    return diagnostic, backup_service, paths


def _diagnostic(
    root: Path,
    paths: dict[str, Path],
    **overrides,
) -> DiagnosticService:
    defaults = {
        "app_version": "0.0.8",
        "project_root": root,
        "data_dir": paths["data"],
        "raw_dir": paths["raw"],
        "pages_dir": paths["pages"],
        "markdown_dir": paths["markdown"],
        "database_path": paths["database"],
        "backups_dir": paths["backups"],
        "logs_dir": paths["logs"],
        "log_path": paths["logs"] / "engineering-kb.log",
        "host": "127.0.0.1",
        "port": 8501,
        "disk_usage": lambda path: SimpleNamespace(free=10 * 1024**3),
        "access_check": lambda path, mode: True,
        "listener_addresses": lambda port: ("127.0.0.1",),
        "port_is_open": lambda port: False,
        "health_check": lambda port: True,
    }
    defaults.update(overrides)
    return DiagnosticService(**defaults)


def _check(snapshot, key: str):
    return next(check for check in snapshot.checks if check.key == key)


def test_normal_diagnostics_with_verified_backup_are_all_normal(tmp_path: Path) -> None:
    diagnostic, backup_service, _ = _environment(tmp_path)
    backup_service.create_backup()

    snapshot = diagnostic.run()

    assert snapshot.overall_status is DiagnosticStatus.NORMAL
    assert snapshot.database_summary is not None
    assert snapshot.database_summary.documents == 1
    assert snapshot.database_summary.pages == 1
    assert snapshot.database_summary.fts == 1
    assert _check(snapshot, "database_integrity").status is DiagnosticStatus.NORMAL
    assert _check(snapshot, "foreign_keys").status is DiagnosticStatus.NORMAL
    assert _check(snapshot, "latest_backup").status is DiagnosticStatus.NORMAL


def test_foreign_key_violation_is_reported_as_error(tmp_path: Path) -> None:
    diagnostic, _, paths = _environment(tmp_path)
    with sqlite3.connect(paths["database"]) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO document_tags(document_id, tag_id, created_at)
            VALUES (999, 999, '2026-07-18T00:00:00+00:00')
            """
        )
        connection.commit()

    snapshot = diagnostic.run()

    assert _check(snapshot, "foreign_keys").status is DiagnosticStatus.ERROR
    assert "2 条" in _check(snapshot, "foreign_keys").summary


@pytest.mark.parametrize(
    ("path_key", "relative", "check_key"),
    [
        ("raw", "manual.pdf", "pdf_files"),
        ("pages", "1/page_0001.png", "page_images"),
        ("markdown", "1/page_0001.md", "markdown_files"),
    ],
)
def test_missing_referenced_files_are_errors(
    tmp_path: Path, path_key: str, relative: str, check_key: str
) -> None:
    diagnostic, _, paths = _environment(tmp_path)
    (paths[path_key] / Path(relative)).unlink()

    snapshot = diagnostic.run()

    check = _check(snapshot, check_key)
    assert check.status is DiagnosticStatus.ERROR
    assert check.details
    assert all("SUPER_PRIVATE_BODY" not in detail for detail in check.details)


def test_orphan_file_is_warning_and_is_never_deleted(tmp_path: Path) -> None:
    diagnostic, _, paths = _environment(tmp_path)
    orphan = paths["pages"] / "orphan.png"
    orphan.write_bytes(b"orphan")

    snapshot = diagnostic.run()

    check = _check(snapshot, "orphan_files")
    assert check.status is DiagnosticStatus.WARNING
    assert "pages/orphan.png" in check.details
    assert orphan.read_bytes() == b"orphan"


def test_read_only_directory_and_low_disk_logic_are_separate_warnings(
    tmp_path: Path,
) -> None:
    _, _, paths = _environment(tmp_path)
    diagnostic = _diagnostic(
        tmp_path,
        paths,
        access_check=lambda path, mode: path != paths["markdown"],
        disk_usage=lambda path: SimpleNamespace(free=128 * 1024**2),
    )

    snapshot = diagnostic.run()

    assert _check(snapshot, "data_access").status is DiagnosticStatus.ERROR
    assert _check(snapshot, "disk_space").status is DiagnosticStatus.WARNING


def test_wrong_listener_and_residual_acceptance_ports_are_detected(
    tmp_path: Path,
) -> None:
    _, _, paths = _environment(tmp_path)
    diagnostic = _diagnostic(
        tmp_path,
        paths,
        host="127.0.0.1",
        listener_addresses=lambda port: ("0.0.0.0",),
        port_is_open=lambda port: port == 8502,
    )

    snapshot = diagnostic.run()

    assert _check(snapshot, "service_listener").status is DiagnosticStatus.ERROR
    assert _check(snapshot, "isolation_ports").status is DiagnosticStatus.WARNING
    assert _check(snapshot, "isolation_ports").details == ("127.0.0.1:8502",)


def test_corrupt_latest_backup_is_an_error(tmp_path: Path) -> None:
    diagnostic, backup_service, _ = _environment(tmp_path)
    result = backup_service.create_backup()
    pdf = next((result.backup_path / "data" / "raw").glob("*.pdf"))
    pdf.write_bytes(b"tampered")

    snapshot = diagnostic.run()

    assert snapshot.latest_backup is not None
    assert not snapshot.latest_backup.valid
    assert _check(snapshot, "latest_backup").status is DiagnosticStatus.ERROR


def test_report_redacts_content_secrets_logs_and_user_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diagnostic, backup_service, paths = _environment(tmp_path)
    backup_service.create_backup()
    database = Database(paths["database"])
    record = database.create_import_record("private.pdf", "私有标题", "f" * 64)
    database.update_import_record(
        record.id,
        status=ImportStatus.FAILED,
        error_message="API_KEY=SHOULD_NOT_LEAK SUPER_PRIVATE_BODY",
    )
    (paths["logs"] / "engineering-kb.log").write_text(
        "2026-07-18 12:00:00,000 ERROR src.import: "
        "password=SHOULD_NOT_LEAK SUPER_PRIVATE_BODY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EKB_API_KEY", "ENV_SECRET_SHOULD_NOT_LEAK")

    snapshot = diagnostic.run()
    report = generate_diagnostic_report(
        snapshot,
        project_root=tmp_path,
        home_dir=tmp_path.parent,
    )

    assert "SUPER_PRIVATE_BODY" not in report
    assert "SHOULD_NOT_LEAK" not in report
    assert "ENV_SECRET_SHOULD_NOT_LEAK" not in report
    assert "private.pdf" not in report
    assert str(tmp_path.parent) not in report
    assert "<PROJECT_ROOT>" in report
    assert "详情已脱敏" in report
    assert "失败或部分完成记录：1" in report


def test_redact_path_never_exposes_absolute_user_home(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "Yang"
    project = home / "engineering-kb"
    database = project / "data" / "database" / "knowledge.db"

    assert redact_path(database, project, home) == (
        "<PROJECT_ROOT>/data/database/knowledge.db"
    )
    assert str(home) not in redact_path(home / "elsewhere.txt", project, home)
