"""Regression tests for OCR text across search, snippets, and evidence.

These tests pin the source semantics of local OCR drafts:

- OCR text written by ``run_page_ocr`` enters the existing FTS and becomes
  searchable, field-filterable, and snippet-able like any other source;
- content priority stays Markdown > OCR > extracted PDF text;
- OCR-derived evidence remains ``original_material`` but is explicitly
  labelled as an unverified local OCR draft in user-facing exports.

Everything runs against a real temporary SQLite database with real FTS,
real temporary files, and in-memory fake OCR engines only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from src.database import Database
from src.document_service import DocumentService, PageOcrOutcome
from src.evidence_basket_service import (
    DuplicateEvidenceError,
    EvidenceBasketService,
)
from src.evidence_service import (
    OCR_EVIDENCE_WARNING,
    EvidencePackageBuilder,
)
from src.models import (
    EvidenceTextKind,
    Page,
    PageStatus,
    SearchField,
    SearchFilters,
    SearchResult,
)
from src.pdf_service import PdfService
from src.search_service import SearchService


class FakeOcrEngine:
    """Duck-typed fake engine returning a fixed recognition result."""

    def __init__(self, result: str) -> None:
        self.result = result
        self.calls: list[Path] = []

    def recognize(self, image_path: Path) -> str:
        self.calls.append(image_path)
        return self.result


def _make_library(tmp_path: Path) -> tuple[Database, SearchService, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    source_path = tmp_path / "raw" / "manual.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"local pdf bytes")
    document = database.create_document(
        title="液压系统手册",
        filename="manual.pdf",
        source_path=source_path,
        sha256=uuid4().hex * 2,
    )
    database.update_document_page_count(document.id, 10)
    return database, SearchService(database), document.id


def _add_page(
    database: Database,
    tmp_path: Path,
    document_id: int,
    page_number: int,
    *,
    extracted_text: str = "",
    ocr_text: str = "",
    markdown_content: str = "",
) -> tuple[Path, Page]:
    image_path = (
        tmp_path / "pages" / str(document_id) / f"page_{page_number:04d}.png"
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), "white").save(image_path)
    page = database.create_page(
        document_id=document_id,
        page_number=page_number,
        image_path=image_path,
        extracted_text=extracted_text,
        ocr_text=ocr_text,
        markdown_content=markdown_content,
        status=PageStatus.PENDING,
    )
    return image_path, page


def _ocr_service(database: Database, tmp_path: Path, result: str) -> DocumentService:
    return DocumentService(
        database=database,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
        pdf_service=PdfService(minimum_text_length=20),
        ocr_engine=FakeOcrEngine(result=result),
    )


def _run_ocr(
    database: Database, tmp_path: Path, page: Page, result: str
) -> Page:
    service = _ocr_service(database, tmp_path, result)
    outcome = service.run_page_ocr(page.id)
    assert outcome.outcome is PageOcrOutcome.COMPLETED
    return outcome.page


# C1: FTS synchronization ----------------------------------------------------


def test_run_page_ocr_makes_text_searchable(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)

    _run_ocr(database, tmp_path, page, "液压泵额定压力二十一兆帕")

    results = search.search("液压泵")
    assert [result.page_id for result in results] == [page.id]
    assert SearchField.OCR_TEXT in results[0].match_fields
    assert results[0].ocr_text == "液压泵额定压力二十一兆帕"


def test_ocr_search_hits_only_the_current_page(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, first = _add_page(database, tmp_path, document_id, 1)
    _, second = _add_page(database, tmp_path, document_id, 2)
    _run_ocr(database, tmp_path, first, "回转窑温度曲线")
    _run_ocr(database, tmp_path, second, "齿轮箱振动频谱")

    results = search.search("回转窑")

    assert [result.page_id for result in results] == [first.id]
    other = database.get_page(second.id)
    assert other is not None
    assert other.ocr_text == "齿轮箱振动频谱"


def test_empty_ocr_result_produces_no_phantom_hits(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    _run_ocr(database, tmp_path, page, "")

    assert search.search("齿轮箱") == []
    persisted = database.get_page(page.id)
    assert persisted is not None
    assert persisted.processing_status == "ocr_completed"


def test_ocr_update_removes_old_keywords_and_adds_new(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    database.update_page(page.id, ocr_text="旧版本关键词回转窑")
    assert [r.page_id for r in search.search("回转窑")] == [page.id]

    database.update_page(page.id, ocr_text="新版本关键词齿轮箱")

    assert search.search("回转窑") == []
    assert [r.page_id for r in search.search("齿轮箱")] == [page.id]


# C2: field-scoped search ----------------------------------------------------


def test_field_restricted_search_respects_ocr_boundaries(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, page = _add_page(
        database, tmp_path, document_id, 1, extracted_text="普通文本层内容"
    )
    _run_ocr(database, tmp_path, page, "泄压阀整定压力")

    all_fields = search.search("泄压阀")
    assert [result.page_id for result in all_fields] == [page.id]

    ocr_only = search.search(
        "泄压阀", filters=SearchFilters(match_fields=(SearchField.OCR_TEXT,))
    )
    assert [result.page_id for result in ocr_only] == [page.id]

    extracted_only = search.search(
        "泄压阀", filters=SearchFilters(match_fields=(SearchField.EXTRACTED_TEXT,))
    )
    assert extracted_only == []

    markdown_only = search.search(
        "泄压阀", filters=SearchFilters(match_fields=(SearchField.MARKDOWN,))
    )
    assert markdown_only == []

    extracted_hit = search.search(
        "普通文本层", filters=SearchFilters(match_fields=(SearchField.EXTRACTED_TEXT,))
    )
    assert [result.page_id for result in extracted_hit] == [page.id]


# C3: snippets ----------------------------------------------------------------


def test_snippet_is_built_from_the_ocr_match(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    _run_ocr(
        database,
        tmp_path,
        page,
        "第一段无关内容。焊缝探伤结果显示一切正常。第三段无关内容。",
    )

    results = search.search("焊缝探伤")

    assert len(results) == 1
    result = results[0]
    assert result.match_fields == (SearchField.OCR_TEXT,)
    assert "焊缝探伤" in result.snippet
    assert result.snippets
    assert result.snippets[0].field is SearchField.OCR_TEXT
    assert "焊缝探伤" in result.snippets[0].text


def test_ocr_english_and_digits_are_searchable(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    _run_ocr(database, tmp_path, page, "Working pressure 21MPa stable")

    results = search.search("21MPa")

    assert [result.page_id for result in results] == [page.id]
    assert "21MPa" in results[0].snippet


def test_punctuation_only_ocr_forms_no_effective_result(tmp_path: Path) -> None:
    database, search, document_id = _make_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    _run_ocr(database, tmp_path, page, "，。、")

    assert search.search("，。") == []
    assert search.search("压力") == []


# C4: content priority --------------------------------------------------------


def _page_model(
    *, extracted: str = "", ocr: str = "", markdown: str = ""
) -> Page:
    return Page(
        id=1,
        document_id=1,
        page_number=1,
        image_path=Path("page.png"),
        extracted_text=extracted,
        ocr_text=ocr,
        markdown_content=markdown,
        markdown_path=None,
        status=PageStatus.PENDING,
        processing_error="",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


def test_searchable_content_priority_markdown_over_ocr_over_extracted() -> None:
    both = _page_model(
        extracted="文本层", ocr="OCR 初稿", markdown="人工整理稿"
    )
    assert both.searchable_content == "人工整理稿"

    no_markdown = _page_model(extracted="文本层", ocr="OCR 初稿")
    assert no_markdown.searchable_content == "OCR 初稿"

    extracted_only = _page_model(extracted="文本层")
    assert extracted_only.searchable_content == "文本层"

    blank_markdown = _page_model(
        extracted="文本层", ocr="OCR 初稿", markdown="   \n  "
    )
    assert blank_markdown.searchable_content == "OCR 初稿"


def test_search_content_prefers_markdown_then_ocr_then_extracted(
    tmp_path: Path,
) -> None:
    database, search, document_id = _make_library(tmp_path)
    _add_page(
        database,
        tmp_path,
        document_id,
        1,
        extracted_text="文本层包含共通词",
        ocr_text="OCR 稿包含共通词",
        markdown_content="人工稿包含共通词",
    )
    _add_page(
        database,
        tmp_path,
        document_id,
        2,
        extracted_text="文本层包含特有二",
        ocr_text="OCR 稿包含特有二",
        markdown_content="   ",
    )
    _add_page(
        database,
        tmp_path,
        document_id,
        3,
        extracted_text="只有文本层含有特有三",
    )

    first = search.search("共通词")
    assert first[0].content == "人工稿包含共通词"
    assert first[0].match_fields[0] is SearchField.MARKDOWN

    second = search.search("特有二")
    assert second[0].content == "OCR 稿包含特有二"
    assert second[0].match_fields[0] is SearchField.OCR_TEXT

    third = search.search("特有三")
    assert third[0].content == "只有文本层含有特有三"
    assert third[0].match_fields[0] is SearchField.EXTRACTED_TEXT


# C5: evidence basket ---------------------------------------------------------


def _basket_library(
    tmp_path: Path,
) -> tuple[Database, EvidenceBasketService, int]:
    database, _, document_id = _make_library(tmp_path)
    return database, EvidenceBasketService(database), document_id


def test_ocr_evidence_is_original_and_warns_exactly_once(tmp_path: Path) -> None:
    database, basket, document_id = _basket_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    persisted = _run_ocr(database, tmp_path, page, "本地识别出的阀组安装要点")

    item = basket.add_item(
        document_id=document_id,
        page_id=page.id,
        evidence_text="阀组安装要点",
    )

    assert item.text_kind is EvidenceTextKind.ORIGINAL
    assert persisted.ocr_text == "本地识别出的阀组安装要点"
    export = basket.export_markdown()
    assert OCR_EVIDENCE_WARNING in export
    assert export.count(OCR_EVIDENCE_WARNING) == 1
    assert "阀组安装要点" in export
    assert "- 来源定位：" in export
    assert "page\\_id=" in export
    assert "液压系统手册" in export


def test_extracted_text_evidence_has_no_ocr_warning(tmp_path: Path) -> None:
    database, basket, document_id = _basket_library(tmp_path)
    _, page = _add_page(
        database, tmp_path, document_id, 1, extracted_text="泵组需要定期检修"
    )

    item = basket.add_item(
        document_id=document_id,
        page_id=page.id,
        evidence_text="定期检修",
    )

    assert item.text_kind is EvidenceTextKind.ORIGINAL
    export = basket.export_markdown()
    assert OCR_EVIDENCE_WARNING not in export


def test_user_excerpt_has_no_ocr_warning(tmp_path: Path) -> None:
    database, basket, document_id = _basket_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    _run_ocr(database, tmp_path, page, "页面上的识别文字")

    item = basket.add_item(
        document_id=document_id,
        page_id=page.id,
        evidence_text="用户凭记忆写下的摘录",
    )

    assert item.text_kind is EvidenceTextKind.USER_EXCERPT
    export = basket.export_markdown()
    assert OCR_EVIDENCE_WARNING not in export


def test_ocr_warning_not_hidden_by_markdown_or_extracted(tmp_path: Path) -> None:
    database, basket, document_id = _basket_library(tmp_path)
    _, page = _add_page(
        database,
        tmp_path,
        document_id,
        1,
        extracted_text="文本层的其他内容",
        markdown_content="人工笔记已经整理过",
    )
    _run_ocr(database, tmp_path, page, "识别出的密封件规格")

    item = basket.add_item(
        document_id=document_id,
        page_id=page.id,
        evidence_text="密封件规格",
    )

    assert item.text_kind is EvidenceTextKind.ORIGINAL
    export = basket.export_markdown()
    assert export.count(OCR_EVIDENCE_WARNING) == 1
    validated = basket.validated_item(item.id)
    assert validated.from_ocr_text is True


def test_duplicate_detection_is_unaffected(tmp_path: Path) -> None:
    database, basket, document_id = _basket_library(tmp_path)
    _, page = _add_page(database, tmp_path, document_id, 1)
    _run_ocr(database, tmp_path, page, "重复检测用的识别文本")

    basket.add_item(
        document_id=document_id,
        page_id=page.id,
        evidence_text="重复检测用的识别文本",
    )
    with pytest.raises(DuplicateEvidenceError):
        basket.add_item(
            document_id=document_id,
            page_id=page.id,
            evidence_text="重复检测用的识别文本",
        )


# C6: export structure --------------------------------------------------------


def test_multiple_ocr_items_each_warn_once_without_global_claim(
    tmp_path: Path,
) -> None:
    database, basket, document_id = _basket_library(tmp_path)
    _, first = _add_page(database, tmp_path, document_id, 1)
    _, second = _add_page(database, tmp_path, document_id, 2)
    _run_ocr(database, tmp_path, first, "第一页识别内容")
    _run_ocr(database, tmp_path, second, "第二页识别内容")
    basket.add_item(
        document_id=document_id, page_id=first.id, evidence_text="第一页识别内容"
    )
    basket.add_item(
        document_id=document_id, page_id=second.id, evidence_text="第二页识别内容"
    )

    export = basket.export_markdown()

    assert export.count(OCR_EVIDENCE_WARNING) == 2
    assert "## 证据 1" in export
    assert "## 证据 2" in export
    first_item_start = export.index("## 证据 1")
    assert OCR_EVIDENCE_WARNING not in export[:first_item_start]
    assert export.index("### 原始材料") < export.index(OCR_EVIDENCE_WARNING)


def test_warning_text_contains_no_machine_paths() -> None:
    assert "\\" not in OCR_EVIDENCE_WARNING
    assert "C:" not in OCR_EVIDENCE_WARNING
    assert "/" not in OCR_EVIDENCE_WARNING


def _search_result(
    *,
    content: str,
    match_fields: tuple[SearchField, ...],
    extracted_text: str = "",
    ocr_text: str = "",
    markdown_content: str = "",
    snippet: str = "",
) -> SearchResult:
    return SearchResult(
        page_id=1,
        document_id=1,
        document_title="液压系统手册",
        filename="manual.pdf",
        page_number=3,
        image_path=Path("page_0003.png"),
        content=content,
        snippet=snippet,
        rank=1.0,
        status=PageStatus.REVIEWED,
        match_fields=match_fields,
        extracted_text=extracted_text,
        ocr_text=ocr_text,
        markdown_content=markdown_content,
    )


def test_single_package_labels_ocr_source_with_warning() -> None:
    result = _search_result(
        content="识别出的密封件规格",
        match_fields=(SearchField.OCR_TEXT,),
        ocr_text="识别出的密封件规格",
        snippet="密封件规格",
    )

    package = EvidencePackageBuilder().build(result)

    assert "来源：OCR 文本" in package
    assert package.count(OCR_EVIDENCE_WARNING) == 1
    assert "第 3 页" in package


def test_single_package_pdf_layer_source_has_no_ocr_warning() -> None:
    result = _search_result(
        content="泵组需要定期检修",
        match_fields=(SearchField.EXTRACTED_TEXT,),
        extracted_text="泵组需要定期检修",
        ocr_text="同页存在的识别文字",
        snippet="定期检修",
    )

    package = EvidencePackageBuilder().build(result)

    assert "来源：PDF 文本层" in package
    assert OCR_EVIDENCE_WARNING not in package


def test_single_package_markdown_note_does_not_trigger_ocr_warning() -> None:
    result = _search_result(
        content="泵组需要定期检修",
        match_fields=(SearchField.EXTRACTED_TEXT,),
        extracted_text="泵组需要定期检修",
        ocr_text="同页存在的识别文字",
        markdown_content="人工整理的检修要点",
        snippet="定期检修",
    )

    package = EvidencePackageBuilder().build(result)

    assert "## 用户笔记" in package
    assert "人工整理的检修要点" in package
    assert OCR_EVIDENCE_WARNING not in package
