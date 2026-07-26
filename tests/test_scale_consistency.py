"""Tests for the read-only v0.2.3 scale consistency checker.

Every test builds a tiny isolated library under ``tmp_path`` (real SQLite
database via ``src.database.Database``, a real small PDF from
``pdf_test_helpers`` and real PNG/Markdown files).  Nothing touches the
formal data directory and all artifacts are cleaned up by pytest.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pdf_test_helpers import build_sample_pdf

from scripts.check_scale_consistency import (
    CheckStatus,
    ScaleCheck,
    main,
    overall_status,
    run_checks,
)
from src.database import Database
from src.models import ImportStatus

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"scale-test-payload"


def _build_library(
    root: Path,
    *,
    page_count: int = 3,
    import_status: ImportStatus = ImportStatus.COMPLETED,
) -> tuple[Path, Path]:
    """Create one small consistent library and return (database, pages_dir)."""

    raw_dir = root / "data" / "raw"
    pages_dir = root / "data" / "pages"
    markdown_dir = root / "data" / "markdown"
    raw_dir.mkdir(parents=True)
    pages_dir.mkdir(parents=True)
    markdown_dir.mkdir(parents=True)
    pdf_path = build_sample_pdf(raw_dir / "sample.pdf")
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    database = Database(root / "data" / "database" / "knowledge.db")
    document = database.create_document(
        title="容量一致性样例",
        filename="sample.pdf",
        source_path=pdf_path,
        sha256=digest,
        page_count=page_count,
        import_status=import_status,
    )
    for page_number in range(1, page_count + 1):
        image_path = pages_dir / str(document.id) / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(PNG_BYTES)
        markdown_path = None
        markdown_content = ""
        if page_number == 1:
            markdown_path = markdown_dir / str(document.id) / "page_0001.md"
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_content = "# 第一页笔记"
            markdown_path.write_text(markdown_content, encoding="utf-8")
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=f"第 {page_number} 页文本",
            markdown_content=markdown_content,
            markdown_path=markdown_path,
        )
    return database.database_path, pages_dir


def _create_duplicate_page_database(database_path: Path, png_dir: Path) -> None:
    """Build a minimal DB whose pages table lacks the UNIQUE page constraint.

    The production schema enforces ``UNIQUE (document_id, page_number)``, so a
    duplicate-page fixture can only exist in a hand-built database (as could
    be produced by an external bulk-import experiment).
    """

    png_dir.mkdir(parents=True, exist_ok=True)
    image = png_dir / "page_0001.png"
    image.write_bytes(PNG_BYTES)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (4, '2026-07-26T00:00:00+00:00');
            CREATE TABLE documents(
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                source_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                import_status TEXT NOT NULL DEFAULT 'completed'
            );
            CREATE TABLE pages(
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                markdown_path TEXT
            );
            CREATE TABLE page_search(search_extracted_text);
            CREATE TABLE evidence_items(id INTEGER PRIMARY KEY);
            CREATE TABLE projects(id INTEGER PRIMARY KEY);
            CREATE TABLE tags(id INTEGER PRIMARY KEY);
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, title, filename, source_path, sha256, page_count)
            VALUES (1, '重复页码', 'dup.pdf', ?, ?, 2)
            """,
            (str(image), "e" * 64),
        )
        for row_id in (1, 2):
            connection.execute(
                """
                INSERT INTO pages(id, document_id, page_number, image_path)
                VALUES (?, 1, 1, ?)
                """,
                (row_id, str(image)),
            )
            connection.execute(
                "INSERT INTO page_search(rowid, search_extracted_text) VALUES (?, 'x')",
                (row_id,),
            )


def _check_by_key(checks: tuple[ScaleCheck, ...], key: str) -> ScaleCheck:
    return next(check for check in checks if check.key == key)


def test_empty_database_passes(tmp_path: Path) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()

    checks = run_checks(database.database_path, pages_dir)

    assert overall_status(checks) is CheckStatus.PASS
    assert all(check.status is CheckStatus.PASS for check in checks)


def test_small_consistent_library_passes(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(tmp_path)

    checks = run_checks(database_path, pages_dir)

    assert overall_status(checks) is CheckStatus.PASS
    assert len(checks) == 13
    assert _check_by_key(checks, "documents_count").summary.startswith("documents：1")


def test_missing_page_png_fails(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(tmp_path)
    victim = next(pages_dir.rglob("*.png"))
    victim.unlink()

    checks = run_checks(database_path, pages_dir)

    page_images = _check_by_key(checks, "page_images")
    assert page_images.status is CheckStatus.FAIL
    assert overall_status(checks) is CheckStatus.FAIL


def test_orphan_page_png_is_reported_without_failing(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(tmp_path)
    orphan = pages_dir / "orphan.png"
    orphan.write_bytes(PNG_BYTES)

    checks = run_checks(database_path, pages_dir)

    orphan_check = _check_by_key(checks, "orphan_page_files")
    assert orphan_check.status is CheckStatus.WARN
    assert any("orphan.png" in detail for detail in orphan_check.details)
    assert orphan.is_file()  # reported only, never deleted
    assert overall_status(checks) is CheckStatus.WARN


def test_document_page_count_mismatch_fails(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE documents SET page_count = page_count + 1")

    checks = run_checks(database_path, pages_dir)

    assert _check_by_key(checks, "document_page_counts").status is CheckStatus.FAIL


def test_duplicate_page_number_fails(tmp_path: Path) -> None:
    database_path = tmp_path / "dup" / "knowledge.db"
    pages_dir = tmp_path / "dup" / "pages"
    _create_duplicate_page_database(database_path, pages_dir)

    checks = run_checks(database_path, pages_dir)

    duplicate = _check_by_key(checks, "duplicate_page_numbers")
    assert duplicate.status is CheckStatus.FAIL
    assert overall_status(checks) is CheckStatus.FAIL


def test_processing_document_fails_unless_allowed(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(
        tmp_path, import_status=ImportStatus.PROCESSING
    )

    strict = run_checks(database_path, pages_dir)
    relaxed = run_checks(database_path, pages_dir, allow_processing=True)

    assert _check_by_key(strict, "processing_documents").status is CheckStatus.FAIL
    assert _check_by_key(relaxed, "processing_documents").status is CheckStatus.WARN


def test_checker_leaves_database_bytes_unchanged(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(tmp_path)
    before = database_path.read_bytes()

    exit_code = main(
        ["--database", str(database_path), "--pages-dir", str(pages_dir)]
    )

    assert exit_code == 0
    assert database_path.read_bytes() == before
    # SQLite may create transient -shm/-wal sidecars when a WAL-mode database
    # is opened read-only (same as production read_database_summary); the
    # database file content above is the read-only guarantee.


def test_cli_writes_machine_readable_json(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(tmp_path)
    json_path = tmp_path / "out" / "consistency.json"

    exit_code = main(
        [
            "--database", str(database_path),
            "--pages-dir", str(pages_dir),
            "--json", str(json_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["overall"] == "PASS"
    keys = {check["key"] for check in payload["checks"]}
    assert "database_integrity" in keys
    assert "duplicate_sha256" in keys


def test_cli_returns_nonzero_on_failure(tmp_path: Path) -> None:
    database_path, pages_dir = _build_library(tmp_path)
    next(pages_dir.rglob("*.png")).unlink()

    exit_code = main(
        ["--database", str(database_path), "--pages-dir", str(pages_dir)]
    )

    assert exit_code == 1


def test_cli_requires_explicit_paths() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_cli_refuses_formal_data_directory(tmp_path: Path) -> None:
    exit_code = main(
        [
            "--database", "D:/Projects/engineering-kb/data/database/knowledge.db",
            "--pages-dir", str(tmp_path),
        ]
    )

    assert exit_code == 2


def test_missing_targets_are_usage_errors(tmp_path: Path) -> None:
    exit_code = main(
        [
            "--database", str(tmp_path / "missing.db"),
            "--pages-dir", str(tmp_path / "missing-pages"),
        ]
    )

    assert exit_code == 2
