"""Tests for the isolated single-page OCR workflow in ``DocumentService``.

All tests use a real temporary SQLite database, real temporary PNG files,
and in-memory fake engines only: no real OCR software, models, network, or
subprocess is involved, and no repository sample files are touched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from src.database import Database, DatabaseError
from src.document_service import (
    DocumentImportError,
    DocumentService,
    PageOcrOutcome,
)
from src.models import Page, PageStatus
from src.ocr_engine import OcrExecutionError, OcrUnavailable
from src.pdf_service import PdfService

LONG_TEXT = "工程正文内容" * 10
PNG_BYTES = b"fake-png-bytes"


class FakeOcrEngine:
    """Duck-typed fake engine recording calls and returning a fixed value."""

    def __init__(self, result: object = "识别文本") -> None:
        self.result = result
        self.calls: list[Path] = []

    def recognize(self, image_path: Path) -> str:
        self.calls.append(image_path)
        return self.result  # type: ignore[return-value]


class FailingOcrEngine:
    """Fake engine whose recognition always raises ``OcrExecutionError``."""

    def __init__(self, message: str = "无法识别该页图像") -> None:
        self.message = message
        self.calls: list[Path] = []

    def recognize(self, image_path: Path) -> str:
        self.calls.append(image_path)
        raise OcrExecutionError(self.message)


def _make_service(
    tmp_path: Path,
    *,
    ocr_engine: object | None = None,
    minimum_text_length: int = 20,
) -> tuple[Database, DocumentService]:
    database = Database(tmp_path / "ocr-test.db")
    service = DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
        pdf_service=PdfService(minimum_text_length=minimum_text_length),
        ocr_engine=ocr_engine,  # type: ignore[arg-type]
    )
    return database, service


def _create_page(
    database: Database,
    tmp_path: Path,
    *,
    extracted_text: str = "",
    ocr_text: str = "",
    markdown_content: str = "",
    status: PageStatus = PageStatus.PENDING,
    processing_error: str = "",
    image_bytes: bytes | None = PNG_BYTES,
    page_number: int = 1,
) -> tuple[Path, Page]:
    document = database.create_document(
        title="工程手册",
        filename="manual.pdf",
        source_path=tmp_path / "raw" / "manual.pdf",
        sha256=uuid4().hex * 2,
    )
    image_path = (
        tmp_path / "pages" / str(document.id) / f"page_{page_number:04d}.png"
    )
    if image_bytes is not None:
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image_bytes)
    page = database.create_page(
        document_id=document.id,
        page_number=page_number,
        image_path=image_path,
        extracted_text=extracted_text,
        ocr_text=ocr_text,
        markdown_content=markdown_content,
        status=status,
        processing_error=processing_error,
    )
    return image_path, page


# Success path ---------------------------------------------------------------


def test_eligible_page_runs_engine_once_and_persists_result(tmp_path: Path) -> None:
    engine = FakeOcrEngine(result="第 1 页识别文本")
    database, service = _make_service(tmp_path, ocr_engine=engine)
    image_path, page = _create_page(database, tmp_path)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    assert engine.calls == [image_path]
    assert engine.calls[0] == image_path
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.ocr_text == "第 1 页识别文本"
    assert persisted.processing_status == "ocr_completed"
    assert result.page.ocr_text == "第 1 页识别文本"


def test_success_keeps_extracted_text_markdown_and_review_status(
    tmp_path: Path,
) -> None:
    engine = FakeOcrEngine(result="识别结果")
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(
        database,
        tmp_path,
        extracted_text="短文本",
        markdown_content="人工笔记",
        status=PageStatus.REVIEWED,
    )

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.extracted_text == "短文本"
    assert persisted.markdown_content == "人工笔记"
    assert persisted.markdown_path == page.markdown_path
    assert persisted.status is PageStatus.REVIEWED
    assert persisted.image_path == page.image_path
    assert persisted.document_id == page.document_id
    assert persisted.page_number == page.page_number


def test_success_clears_only_previous_ocr_error(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=FakeOcrEngine())
    _, page = _create_page(database, tmp_path, processing_error="OCR：旧识别失败")

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.processing_error == ""


def test_success_preserves_unrelated_processing_error(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=FakeOcrEngine())
    _, page = _create_page(database, tmp_path, processing_error="页面渲染失败")

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.ocr_text == "识别文本"
    assert persisted.processing_error == "页面渲染失败"


def test_success_preserves_non_ocr_part_of_mixed_error(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=FakeOcrEngine())
    _, page = _create_page(
        database, tmp_path, processing_error="页面渲染失败；OCR：旧识别失败"
    )

    service.run_page_ocr(page.id)

    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.processing_error == "页面渲染失败"


def test_png_file_is_not_modified(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=FakeOcrEngine())
    image_path, page = _create_page(database, tmp_path)
    before = hashlib.sha256(image_path.read_bytes()).hexdigest()

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == before
    assert image_path.stat().st_size == len(PNG_BYTES)


# Empty recognition result ---------------------------------------------------


def test_empty_result_is_completed_not_unavailable(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=FakeOcrEngine(result=""))
    _, page = _create_page(database, tmp_path)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.ocr_text == ""
    assert persisted.processing_status == "ocr_completed"
    assert persisted.processing_error == ""


def test_empty_result_does_not_touch_manual_content(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=FakeOcrEngine(result=""))
    _, page = _create_page(
        database,
        tmp_path,
        extracted_text="短文本",
        markdown_content="人工笔记",
        status=PageStatus.REVIEWED,
    )

    service.run_page_ocr(page.id)

    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.extracted_text == "短文本"
    assert persisted.markdown_content == "人工笔记"
    assert persisted.status is PageStatus.REVIEWED


# Ineligible pages -----------------------------------------------------------


def test_sufficient_extracted_text_skips_engine(tmp_path: Path) -> None:
    engine = FakeOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path, extracted_text=LONG_TEXT)
    before = database.get_page(page.id)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.NOT_ELIGIBLE
    assert engine.calls == []
    assert database.get_page(page.id) == before


def test_existing_ocr_text_skips_engine(tmp_path: Path) -> None:
    engine = FakeOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path, ocr_text="已有 OCR 文本")
    before = database.get_page(page.id)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.NOT_ELIGIBLE
    assert engine.calls == []
    assert database.get_page(page.id) == before


def test_missing_png_skips_engine(tmp_path: Path) -> None:
    engine = FakeOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    image_path, page = _create_page(database, tmp_path)
    image_path.unlink()
    before = database.get_page(page.id)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.NOT_ELIGIBLE
    assert engine.calls == []
    assert database.get_page(page.id) == before


def test_zero_byte_png_skips_engine(tmp_path: Path) -> None:
    engine = FakeOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path, image_bytes=b"")
    before = database.get_page(page.id)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.NOT_ELIGIBLE
    assert engine.calls == []
    assert database.get_page(page.id) == before


def test_empty_image_path_skips_engine(tmp_path: Path) -> None:
    engine = FakeOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path)
    page = database.update_page(page.id, image_path="")
    before = database.get_page(page.id)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.NOT_ELIGIBLE
    assert engine.calls == []
    assert database.get_page(page.id) == before


def test_markdown_present_still_allows_ocr(tmp_path: Path) -> None:
    engine = FakeOcrEngine(result="识别结果")
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path, markdown_content="人工笔记")

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    assert len(engine.calls) == 1
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.markdown_content == "人工笔记"


def test_skipped_status_does_not_block_eligibility(tmp_path: Path) -> None:
    engine = FakeOcrEngine(result="识别结果")
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path, status=PageStatus.SKIPPED)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.COMPLETED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.status is PageStatus.SKIPPED


def test_ineligible_page_does_not_require_engine(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=None)
    _, page = _create_page(database, tmp_path, extracted_text=LONG_TEXT)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.NOT_ELIGIBLE


# Engine unavailable ---------------------------------------------------------


def test_missing_engine_raises_unavailable_without_writes(tmp_path: Path) -> None:
    database, service = _make_service(tmp_path, ocr_engine=None)
    _, page = _create_page(database, tmp_path)
    before = database.get_page(page.id)

    with pytest.raises(OcrUnavailable):
        service.run_page_ocr(page.id)

    after = database.get_page(page.id)
    assert after == before
    assert after is not None
    assert "OCR" not in after.processing_error


# Per-page execution failure -------------------------------------------------


def test_execution_error_isolated_to_current_page(tmp_path: Path) -> None:
    engine = FailingOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    image_path, first_page = _create_page(
        database, tmp_path, extracted_text="第一页短文本"
    )
    other_document = database.create_document(
        title="另一手册",
        filename="other.pdf",
        source_path=tmp_path / "raw" / "other.pdf",
        sha256=uuid4().hex * 2,
    )
    other_page = database.create_page(
        document_id=other_document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="另一页短文本",
    )
    other_before = database.get_page(other_page.id)

    result = service.run_page_ocr(first_page.id)

    assert result.outcome is PageOcrOutcome.FAILED
    assert len(engine.calls) == 1
    persisted = database.get_page(first_page.id)
    assert persisted is not None
    assert persisted.ocr_text == ""
    assert persisted.processing_status != "ocr_completed"
    assert persisted.processing_error.startswith("OCR：")
    assert "无法识别该页图像" in persisted.processing_error
    assert persisted.extracted_text == "第一页短文本"
    assert persisted.markdown_content == ""
    assert persisted.status is PageStatus.PENDING
    assert database.get_page(other_page.id) == other_before


def test_execution_failure_replaces_previous_ocr_error(tmp_path: Path) -> None:
    engine = FailingOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path)
    database.update_page(page.id, processing_error="OCR：上一次失败")

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.FAILED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.ocr_text == ""
    assert persisted.processing_error.startswith("OCR：")
    assert "无法识别该页图像" in persisted.processing_error
    assert "上一次失败" not in persisted.processing_error


def test_execution_failure_preserves_unrelated_error(tmp_path: Path) -> None:
    engine = FailingOcrEngine()
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path, processing_error="页面渲染失败")

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.FAILED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert "页面渲染失败" in persisted.processing_error
    assert "OCR：" in persisted.processing_error


# Contract and error protection ----------------------------------------------


def test_non_string_engine_result_treated_as_failure(tmp_path: Path) -> None:
    engine = FakeOcrEngine(result=None)
    database, service = _make_service(tmp_path, ocr_engine=engine)
    _, page = _create_page(database, tmp_path)

    result = service.run_page_ocr(page.id)

    assert result.outcome is PageOcrOutcome.FAILED
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.ocr_text == ""
    assert persisted.processing_status != "ocr_completed"
    assert persisted.processing_error.startswith("OCR：")


def test_database_error_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service = _make_service(tmp_path, ocr_engine=FakeOcrEngine())
    _, page = _create_page(database, tmp_path)

    def failing_update(*args: object, **kwargs: object) -> object:
        raise DatabaseError("模拟数据库写入失败")

    monkeypatch.setattr(database, "update_page", failing_update)

    with pytest.raises(DatabaseError):
        service.run_page_ocr(page.id)


def test_missing_page_uses_existing_not_found_semantics(tmp_path: Path) -> None:
    engine = FakeOcrEngine()
    _, service = _make_service(tmp_path, ocr_engine=engine)

    with pytest.raises(DocumentImportError):
        service.run_page_ocr(999999)

    assert engine.calls == []
