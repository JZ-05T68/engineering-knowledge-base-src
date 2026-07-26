"""Tests for the deterministic v0.2.3 scale PDF generator.

All PDFs are small (<20 pages), built inside pytest's ``tmp_path`` and removed
automatically.  CLI-level tests invoke the script through ``sys.executable``
(the project virtual environment) so the real argparse exit codes are covered.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from scripts.generate_scale_pdf import (
    SPECIAL_PAGE_TYPES,
    FormalPathError,
    build_scale_pdf,
    parse_special_pages,
    preset_special_pages,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_SCRIPT = PROJECT_ROOT / "scripts" / "generate_scale_pdf.py"

MIXED_SPEC = "blank:2,short:3,rot90:4,rot180:5,rot270:6,wide:7,image:8"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )


def _page_texts(pdf_path: Path) -> list[str]:
    document = pymupdf.open(pdf_path)
    try:
        return [page.get_text() for page in document]
    finally:
        document.close()


def test_single_normal_page_contains_unique_token(tmp_path: Path) -> None:
    result = build_scale_pdf(tmp_path / "one.pdf", pages=1, document_id="DOC-A")

    assert result.pages == 1
    assert result.document_id == "DOC-A"
    assert result.size_bytes > 0
    assert len(result.sha256) == 64
    assert "SCALE DOC-A PAGE 1 TOKEN DOC-A-000001" in _page_texts(result.path)[0]


def test_document_id_defaults_to_output_stem(tmp_path: Path) -> None:
    result = build_scale_pdf(tmp_path / "scale-demo 01.pdf", pages=1)

    assert result.document_id == "SCALE-DEMO-01"
    assert "SCALE SCALE-DEMO-01 PAGE 1" in _page_texts(result.path)[0]


def test_mixed_special_pages_count_and_rules(tmp_path: Path) -> None:
    result = build_scale_pdf(
        tmp_path / "mixed.pdf",
        pages=10,
        document_id="MIX",
        special_pages=parse_special_pages(MIXED_SPEC),
    )

    document = pymupdf.open(result.path)
    try:
        assert document.page_count == 10
        pages = list(document)
        assert pages[1].get_text().strip() == ""  # blank
        assert 0 < len(pages[2].get_text().strip()) < 20  # short
        assert pages[3].rotation == 90
        assert pages[4].rotation == 180
        assert pages[5].rotation == 270
        assert pages[6].rect.width > pages[6].rect.height  # wide
        assert pages[7].get_text().strip() == ""  # image-only
        assert len(pages[7].get_images(full=True)) >= 1
        for index in (0, 8, 9):  # normal pages keep their unique tokens
            assert f"TOKEN MIX-{index + 1:06d}" in pages[index].get_text()
    finally:
        document.close()


def test_same_arguments_produce_identical_page_text(tmp_path: Path) -> None:
    first = build_scale_pdf(
        tmp_path / "a.pdf", pages=6, document_id="DET",
        special_pages=parse_special_pages("short:2,rot90:4"),
    )
    second = build_scale_pdf(
        tmp_path / "b.pdf", pages=6, document_id="DET",
        special_pages=parse_special_pages("short:2,rot90:4"),
    )

    assert _page_texts(first.path) == _page_texts(second.path)


def test_no_unique_text_removes_token(tmp_path: Path) -> None:
    result = build_scale_pdf(
        tmp_path / "plain.pdf", pages=2, document_id="PLAIN", unique_text=False
    )

    text = _page_texts(result.path)[0]
    assert "SCALE PLAIN PAGE 1" in text
    assert "TOKEN" not in text


def test_preset_covers_every_special_type() -> None:
    preset = preset_special_pages(50)

    assert set(preset.values()) == set(SPECIAL_PAGE_TYPES)
    assert all(1 <= page <= 50 for page in preset)


@pytest.mark.parametrize(
    "spec",
    ["unknown:3", "blank:0", "blank:xyz", "blank", "blank:2,short:2"],
)
def test_parse_special_pages_rejects_invalid_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_special_pages(spec)


def test_existing_output_is_kept_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "kept.pdf"
    build_scale_pdf(target, pages=1)

    with pytest.raises(FileExistsError):
        build_scale_pdf(target, pages=1)
    rebuilt = build_scale_pdf(target, pages=2, overwrite=True)
    assert rebuilt.pages == 2


def test_formal_data_directory_is_rejected() -> None:
    with pytest.raises(FormalPathError):
        build_scale_pdf(
            Path("D:/Projects/engineering-kb/data/raw/scale.pdf"), pages=1
        )


def test_special_page_beyond_page_count_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="超出总页数"):
        build_scale_pdf(
            tmp_path / "oob.pdf", pages=3,
            special_pages=parse_special_pages("blank:9"),
        )


def test_cli_generates_pdf_and_prints_summary(tmp_path: Path) -> None:
    target = tmp_path / "cli" / "cli-3.pdf"

    completed = _run_cli(
        "--output", str(target), "--pages", "3", "--special-pages", "blank:2"
    )

    assert completed.returncode == 0, completed.stderr
    assert target.is_file()
    assert len(_page_texts(target)) == 3
    assert "Pages: 3" in completed.stdout
    assert "SHA-256:" in completed.stdout


@pytest.mark.parametrize("bad_pages", ["0", "-5", "abc"])
def test_cli_rejects_invalid_page_counts(tmp_path: Path, bad_pages: str) -> None:
    completed = _run_cli("--output", str(tmp_path / "bad.pdf"), "--pages", bad_pages)

    assert completed.returncode != 0
    assert not (tmp_path / "bad.pdf").exists()


def test_cli_refuses_existing_output_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "exists.pdf"
    assert _run_cli("--output", str(target), "--pages", "1").returncode == 0

    refused = _run_cli("--output", str(target), "--pages", "1")
    assert refused.returncode != 0

    allowed = _run_cli("--output", str(target), "--pages", "1", "--overwrite")
    assert allowed.returncode == 0, allowed.stderr


def test_cli_special_preset_smoke(tmp_path: Path) -> None:
    target = tmp_path / "preset.pdf"

    completed = _run_cli("--output", str(target), "--pages", "14", "--special-preset")

    assert completed.returncode == 0, completed.stderr
    assert len(_page_texts(target)) == 14
