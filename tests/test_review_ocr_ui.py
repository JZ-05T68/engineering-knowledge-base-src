"""Tests for the single-page OCR controls on the review page.

Pure feedback mapping is unit-tested directly; widget behavior is tested
through Streamlit AppTest against a real temporary SQLite database with
fake in-memory OCR engines only — no real OCR, network, or formal data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_service import DocumentService, PageOcrOutcome
from src.models import PageStatus
from src.ocr_engine import OcrExecutionError
from src.ocr_ui import (
    OCR_DRAFT_HINT,
    page_ocr_feedback,
    page_ocr_unavailable_feedback,
)


class _FakeOcrEngine:
    """Fake engine returning fixed text and recording calls."""

    def __init__(self, result: str = "识别出的泵体参数") -> None:
        self.result = result
        self.calls: list[Path] = []

    def recognize(self, image_path: Path) -> str:
        self.calls.append(image_path)
        return self.result


class _FailingOcrEngine:
    """Fake engine failing with a message containing a local path."""

    def __init__(self, message: str) -> None:
        self.message = message

    def recognize(self, image_path: Path) -> str:
        raise OcrExecutionError(self.message)


def _build_review_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_engine: object | None = None,
    ocr_text: str = "",
    extracted_text: str = "",
) -> tuple[AppTest, Database, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="单页 OCR 界面测试",
        filename="ocr-ui.pdf",
        source_path=tmp_path / "raw" / "ocr-ui.pdf",
        sha256="5" * 64,
    )
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir(parents=True)
    image_path = pages_dir / "page_0001.png"
    Image.new("RGB", (40, 20), "white").save(image_path)
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
    )
    if extracted_text or ocr_text:
        database.update_page(
            page.id,
            extracted_text=extracted_text or None,
            ocr_text=ocr_text or None,
        )
    service = DocumentService(
        database,
        tmp_path / "raw",
        pages_dir,
        tmp_path / "markdown",
        ocr_engine=ocr_engine,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    app_path = next((Path(__file__).parents[1] / "pages").glob("4_*.py"))
    app = AppTest.from_file(str(app_path)).run(timeout=10)
    return app, database, page.id


# Pure feedback mapping -------------------------------------------------------


def test_completed_with_text_maps_to_success() -> None:
    assert page_ocr_feedback(PageOcrOutcome.COMPLETED, "识别文本") == (
        "success",
        "本页 OCR 已完成。",
    )


def test_completed_without_text_says_no_valid_text() -> None:
    level, message = page_ocr_feedback(PageOcrOutcome.COMPLETED, "  ")
    assert level == "info"
    assert "已执行完成" in message
    assert "未识别到有效文字" in message


def test_not_eligible_maps_to_info_not_error() -> None:
    level, message = page_ocr_feedback(PageOcrOutcome.NOT_ELIGIBLE, "")
    assert level == "info"
    assert "不符合 OCR 条件" in message


def test_failed_maps_to_error_without_paths() -> None:
    level, message = page_ocr_feedback(PageOcrOutcome.FAILED, "")
    assert level == "error"
    assert "执行失败" in message
    assert "未被修改" in message
    assert "/" not in message and "\\" not in message


def test_unavailable_maps_to_warning() -> None:
    level, message = page_ocr_unavailable_feedback()
    assert level == "warning"
    assert "不可用" in message
    assert "依赖已完整安装" in message
    assert "/" not in message and "\\" not in message


# Widget behavior --------------------------------------------------------------


def test_page_with_ocr_text_shows_readonly_draft_without_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _FakeOcrEngine()
    app, database, page_id = _build_review_app(
        tmp_path, monkeypatch, ocr_engine=engine, ocr_text="本地识别初稿文字"
    )

    assert not app.exception
    assert any("未经人工核验" in item.value for item in app.markdown)
    assert any(OCR_DRAFT_HINT in item.value for item in app.caption)
    draft = app.text_area(key=f"review_ocr_draft_{page_id}")
    assert draft.disabled is True
    assert draft.value == "本地识别初稿文字"
    labels = {button.label for button in app.button}
    assert "执行本地 OCR" not in labels
    assert not any("批量" in label and "OCR" in label for label in labels)
    assert "全部页面 OCR" not in labels
    assert engine.calls == []


def test_page_without_ocr_text_runs_single_page_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _FakeOcrEngine(result="识别出的泵体参数")
    app, database, page_id = _build_review_app(tmp_path, monkeypatch, ocr_engine=engine)

    assert not app.exception
    button = next(b for b in app.button if b.label == "执行本地 OCR")
    assert button.key == f"review_run_ocr_{page_id}"
    assert sum(1 for b in app.button if "OCR" in b.label) == 1

    button.click().run()

    assert not app.exception
    assert any("本页 OCR 已完成。" == item.value for item in app.success)
    assert engine.calls and len(engine.calls) == 1
    persisted = database.get_page(page_id)
    assert persisted is not None
    assert persisted.ocr_text == "识别出的泵体参数"
    assert persisted.processing_status == "ocr_completed"
    assert persisted.markdown_content == ""
    assert persisted.status is PageStatus.PENDING
    draft = app.text_area(key=f"review_ocr_draft_{page_id}")
    assert draft.disabled is True
    assert draft.value == "识别出的泵体参数"


def test_not_eligible_page_gets_info_and_engine_is_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _FakeOcrEngine()
    app, database, page_id = _build_review_app(
        tmp_path,
        monkeypatch,
        ocr_engine=engine,
        extracted_text="工程正文内容" * 10,
    )

    next(b for b in app.button if b.label == "执行本地 OCR").click().run()

    assert not app.exception
    assert any("不符合 OCR 条件" in item.value for item in app.info)
    assert engine.calls == []
    persisted = database.get_page(page_id)
    assert persisted is not None
    assert persisted.ocr_text == ""
    assert persisted.status is PageStatus.PENDING


def test_unavailable_engine_gets_warning_and_page_stays_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database, page_id = _build_review_app(tmp_path, monkeypatch, ocr_engine=None)

    next(b for b in app.button if b.label == "执行本地 OCR").click().run()

    assert not app.exception
    assert any("本地 OCR 引擎不可用" in item.value for item in app.warning)
    assert any(item.label == "保存草稿" for item in app.button)
    persisted = database.get_page(page_id)
    assert persisted is not None
    assert persisted.ocr_text == ""
    assert persisted.processing_error == ""


def test_failed_ocr_gets_error_and_never_shows_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_dir = tmp_path / "secret"
    engine = _FailingOcrEngine(f"path={secret_dir}/page.png")
    app, database, page_id = _build_review_app(tmp_path, monkeypatch, ocr_engine=engine)

    next(b for b in app.button if b.label == "执行本地 OCR").click().run()

    assert not app.exception
    assert any("本页 OCR 执行失败" in item.value for item in app.error)
    persisted = database.get_page(page_id)
    assert persisted is not None
    assert persisted.processing_error.startswith("OCR：")
    assert "[本地路径]" in persisted.processing_error
    assert persisted.markdown_content == ""
    assert persisted.status is PageStatus.PENDING
    visible_text = "\n".join(
        item.value
        for group in (app.error, app.warning, app.info, app.success, app.markdown)
        for item in group
    )
    assert str(secret_dir) not in visible_text
    assert "secret" not in visible_text
