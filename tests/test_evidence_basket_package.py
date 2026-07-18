"""Tests for ordered, multi-page Markdown evidence packages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.evidence_basket_service import (
    EmptyEvidenceBasketError,
    EvidenceBasketService,
    EvidenceSourceError,
)
from src.evidence_service import EvidenceBasketPackageBuilder, EvidencePackageError
from src.models import EvidenceContextKind, EvidenceTextKind, PageStatus

FIXED_TIME = datetime(2026, 7, 18, 19, 30, tzinfo=timezone(timedelta(hours=8)))


def _document(
    database: Database,
    tmp_path: Path,
    *,
    suffix: str,
    title: str,
    statuses: tuple[PageStatus, ...],
) -> tuple[int, list[int]]:
    filename = f"{title}.pdf"
    source_path = tmp_path / "raw sources" / filename
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"local pdf")
    document = database.create_document(
        title=title,
        filename=filename,
        source_path=source_path,
        sha256=suffix * 64,
    )
    page_ids: list[int] = []
    for page_number, status in enumerate(statuses, start=1):
        image_path = (
            tmp_path / "page images" / str(document.id) / f"page_{page_number:04d}.png"
        )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), "white").save(image_path)
        page = database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=f"{title} 第 {page_number} 页原始工程结论。",
            status=status,
        )
        page_ids.append(page.id)
    database.update_document_page_count(document.id, len(statuses))
    return document.id, page_ids


def test_multi_document_multi_page_package_preserves_order_and_boundaries(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    first_document, first_pages = _document(
        database,
        tmp_path,
        suffix="1",
        title="液压 #维护手册",
        statuses=(PageStatus.REVIEWED, PageStatus.DRAFT),
    )
    second_document, second_pages = _document(
        database,
        tmp_path,
        suffix="2",
        title="阀组手册",
        statuses=(PageStatus.PENDING,),
    )
    service = EvidenceBasketService(database)
    first = service.add_item(
        document_id=first_document,
        page_id=first_pages[0],
        evidence_text="液压 #维护手册 第 1 页原始工程结论。",
        user_note="# 用户标题\n```python\nunsafe = True\n```",
    )
    second = service.add_item(
        document_id=first_document,
        page_id=first_pages[1],
        evidence_text="## 用户整理的未匹配结论",
        context="用户补充的上下文",
    )
    third = service.add_item(
        document_id=second_document,
        page_id=second_pages[0],
        evidence_text="阀组手册 第 1 页原始工程结论。",
    )
    service.reorder([third.id, first.id, second.id])

    package = service.export_markdown(
        title="泵站 #故障证据",
        generated_at=FIXED_TIME,
    )

    assert "# 泵站 \\#故障证据" in package
    assert "证据条数：3" in package and "涉及文档数：2" in package
    assert "包含 2 条未处于“人工复核完成”" in package
    assert package.index("## 证据 1：阀组手册") < package.index(
        "## 证据 2：液压 \\#维护手册"
    )
    assert package.index("## 证据 2：液压 \\#维护手册") < package.index(
        "## 证据 3：液压 \\#维护手册"
    )
    assert "### 原始材料" in package
    assert "### 用户摘录" in package
    assert "### 用户笔记" in package
    assert "### 系统生成的上下文 / 摘要" in package
    assert "### 用户提供的上下文" in package
    assert "    # 用户标题" in package
    assert "    ## 用户整理的未匹配结论" in package
    assert "\n## 用户整理的未匹配结论" not in package
    assert str((tmp_path / "raw sources" / "阀组手册.pdf").resolve()) in package
    assert FIXED_TIME.isoformat(timespec="seconds") in package

    validated = service.validated_items()
    assert validated[1].text_kind is EvidenceTextKind.ORIGINAL
    assert validated[1].context_kind is EvidenceContextKind.SYSTEM_GENERATED
    assert validated[2].text_kind is EvidenceTextKind.USER_EXCERPT
    assert validated[2].context_kind is EvidenceContextKind.USER_PROVIDED


def test_all_five_review_statuses_are_labeled_with_individual_warnings(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    statuses = tuple(PageStatus)
    document_id, page_ids = _document(
        database,
        tmp_path,
        suffix="3",
        title="五态资料",
        statuses=statuses,
    )
    service = EvidenceBasketService(database)
    for page_number, page_id in enumerate(page_ids, start=1):
        service.add_item(
            document_id=document_id,
            page_id=page_id,
            evidence_text=f"五态资料 第 {page_number} 页原始工程结论。",
        )

    package = service.export_markdown(generated_at=FIXED_TIME)

    for status in statuses:
        assert f"{status.label}（{status.value}）" in package
    assert "包含 4 条未处于“人工复核完成”" in package
    assert package.count("本条复核警告") == 4


def test_empty_basket_and_missing_source_fail_without_partial_export(tmp_path: Path) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    service = EvidenceBasketService(database)
    with pytest.raises(EmptyEvidenceBasketError, match="为空"):
        service.export_markdown()

    document_id, page_ids = _document(
        database,
        tmp_path,
        suffix="4",
        title="来源检查",
        statuses=(PageStatus.REVIEWED,),
    )
    service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="来源检查 第 1 页原始工程结论。",
    )
    page = database.get_page(page_ids[0])
    assert page is not None
    page.image_path.unlink()
    with pytest.raises(EvidenceSourceError, match="页面图像缺失"):
        service.export_markdown()


def test_builder_rejects_cross_basket_items(tmp_path: Path) -> None:
    database = Database(tmp_path / "database" / "knowledge.db")
    document_id, page_ids = _document(
        database,
        tmp_path,
        suffix="5",
        title="篮子一致性",
        statuses=(PageStatus.REVIEWED,),
    )
    service = EvidenceBasketService(database)
    item = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="篮子一致性 第 1 页原始工程结论。",
    )
    other = service.create_basket("另一个篮子")

    with pytest.raises(EvidencePackageError, match="不一致"):
        EvidenceBasketPackageBuilder().build(other, [item], generated_at=FIXED_TIME)
