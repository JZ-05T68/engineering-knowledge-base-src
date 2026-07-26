"""Tests for the T25 terminal-state invariant checker (small libraries only).

Every test builds a tiny isolated library (at most 8 generated pages) inside
pytest ``tmp_path`` roots through the real production import pipeline, then
runs ``scripts/check_scale_invariants`` against it.  Positive tests prove a
clean library passes all 28 invariants; negative tests prove each violated
invariant family is actually detected (the checker never repairs anything).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import check_scale_invariants as invariants
from scripts.generate_scale_pdf import build_scale_pdf
from src.database import Database
from src.document_service import DocumentService

DOCUMENT_ID = "T25DOC"


def _import_library(tmp_path: Path, pages: int = 6) -> tuple[Path, Path]:
    """Import one small generated PDF into an isolated probe-style root."""

    root = tmp_path / "case"
    for relative in (
        "data/raw",
        "data/pages",
        "data/markdown",
        "data/database",
        "backups",
        "logs",
        "runtime",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    pdf = build_scale_pdf(tmp_path / f"t25-{pages}.pdf", pages=pages, document_id=DOCUMENT_ID)
    database = Database(root / "data" / "database" / "knowledge.db")
    service = DocumentService(
        database=database,
        raw_dir=root / "data" / "raw",
        pages_dir=root / "data" / "pages",
        markdown_dir=root / "data" / "markdown",
    )
    with pdf.path.open("rb") as stream:
        result = service.import_pdf(stream, pdf.path.name)
    assert not result.duplicate
    assert str(result.document.import_status) == "completed"
    return root, pdf.path


def _run(root: Path, pdf: Path, *extra: str, expect_pages: int = 6) -> int:
    return invariants.main(
        [
            "--root",
            str(root),
            "--expect-pages",
            str(expect_pages),
            "--document-id",
            DOCUMENT_ID,
            "--source-pdf",
            str(pdf),
            "--sample-interval",
            "3",
            "--json",
            str(root / "t25.json"),
            *extra,
        ]
    )


def _failed_keys(root: Path) -> set[str]:
    payload = json.loads((root / "t25.json").read_text(encoding="utf-8"))
    return {check["key"] for check in payload["checks"] if check["status"] == "FAIL"}


def _update_database(root: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(root / "data" / "database" / "knowledge.db") as connection:
        connection.execute(statement, parameters)


def test_clean_library_passes_all_28_invariants(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)

    assert _run(root, pdf) == 0

    payload = json.loads((root / "t25.json").read_text(encoding="utf-8"))
    assert payload["overall"] == "PASS"
    assert len(payload["checks"]) == 28
    assert all(check["status"] == "PASS" for check in payload["checks"])
    assert payload["environment"]["python"]
    assert payload["environment"]["sqlite"]


def test_missing_page_png_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    victim = next((root / "data" / "pages").rglob("page_0002.png"))
    victim.unlink()

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_15_referenced_pngs_decodable" in failed
    assert "t25_16_png_count" in failed
    assert "t25_17_page_png_one_to_one" in failed
    assert "t25_23_no_missing_files" in failed


def test_zero_byte_png_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    victim = next((root / "data" / "pages").rglob("page_0004.png"))
    victim.write_bytes(b"")

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_15_referenced_pngs_decodable" in failed
    assert "t25_18_no_zero_byte_files" in failed
    assert "t25_19_no_undecodable_pngs" in failed


def test_undecodable_png_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    victim = next((root / "data" / "pages").rglob("page_0005.png"))
    victim.write_bytes(b"\x89PNG-not-a-real-png-payload")

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_19_no_undecodable_pngs" in failed
    assert "t25_18_no_zero_byte_files" not in failed


def test_temp_residue_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    residue = next((root / "data" / "pages").rglob("page_0001.png"))
    residue.with_name("page_0001.png.tmp-deadbeef").write_bytes(b"partial")

    assert _run(root, pdf) == 1
    assert _failed_keys(root) == {"t25_20_no_temp_residue"}


def test_side_step_raw_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    raw_file = next((root / "data" / "raw").glob("*.pdf"))
    side_step = raw_file.with_name(raw_file.stem + "_1.pdf")
    side_step.write_bytes(raw_file.read_bytes())

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_21_no_side_step_raw" in failed
    assert "t25_22_no_orphan_files" in failed


def test_orphan_raw_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    orphan = build_scale_pdf(tmp_path / "orphan.pdf", pages=2, document_id="ORPHAN")
    target = root / "data" / "raw" / orphan.path.name
    target.write_bytes(orphan.path.read_bytes())

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_22_no_orphan_files" in failed
    assert "t25_21_no_side_step_raw" not in failed


def test_wrong_expected_pages_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)

    assert _run(root, pdf, expect_pages=7) == 1
    failed = _failed_keys(root)
    assert "t25_04_pages_count" in failed
    assert "t25_16_png_count" in failed


def test_processing_document_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    _update_database(root, "UPDATE documents SET import_status = 'processing'")

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_09_documents_completed" in failed
    assert "t25_10_no_unexpected_processing" in failed


def test_interrupted_record_requires_explicit_allowance(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    _update_database(
        root,
        "INSERT INTO import_records(filename, title, sha256, status, started_at)"
        " VALUES ('killed.pdf', 'killed', 'deadbeef', 'processing', '2026-07-26T00:00:00')",
    )

    assert _run(root, pdf) == 1
    assert "t25_10_no_unexpected_processing" in _failed_keys(root)

    assert _run(root, pdf, "--allow-processing-records", "1") == 0


def test_failed_document_state_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    _update_database(root, "UPDATE documents SET import_status = 'partially_completed'")

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_09_documents_completed" in failed
    assert "t25_11_no_failed_states" in failed


def test_content_tamper_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    _update_database(root, "UPDATE pages SET extracted_text = '' WHERE page_number = 3")

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_25_page_tokens" in failed
    assert "t25_26_sampled_text_matches" in failed


def test_raw_hash_mismatch_is_detected(tmp_path: Path) -> None:
    root, pdf = _import_library(tmp_path)
    raw_file = next((root / "data" / "raw").glob("*.pdf"))
    other = build_scale_pdf(tmp_path / "other.pdf", pages=3, document_id="OTHER")
    raw_file.write_bytes(other.path.read_bytes())

    assert _run(root, pdf) == 1
    failed = _failed_keys(root)
    assert "t25_14_raw_sha256_matches" in failed


def test_formal_root_is_rejected(tmp_path: Path) -> None:
    exit_code = invariants.main(
        [
            "--root",
            "D:/Projects/engineering-kb",
            "--expect-pages",
            "6",
            "--document-id",
            DOCUMENT_ID,
            "--source-pdf",
            str(tmp_path / "x.pdf"),
        ]
    )
    assert exit_code == 2


def test_sample_page_numbers_covers_first_last_and_interval() -> None:
    assert invariants.sample_page_numbers(10, 4) == (1, 4, 8, 10)
    assert invariants.sample_page_numbers(1, 50) == (1,)
    with pytest.raises(invariants.InvariantUsageError):
        invariants.sample_page_numbers(0, 4)
