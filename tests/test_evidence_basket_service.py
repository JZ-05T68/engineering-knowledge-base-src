"""Tests for durable, source-linked v0.0.5 evidence baskets."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.evidence_basket_service import (
    DuplicateEvidenceError,
    EvidenceBasketError,
    EvidenceBasketService,
    EvidenceSourceError,
    evidence_text_html,
)
from src.models import EvidenceTextKind, PageStatus


def _library(tmp_path: Path) -> tuple[Database, EvidenceBasketService, int, list[int]]:
    database = Database(tmp_path / "database" / "knowledge.db")
    source_path = tmp_path / "raw" / "液压手册.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"local pdf")
    document = database.create_document(
        title="液压系统手册",
        filename="液压手册.pdf",
        source_path=source_path,
        sha256="a" * 64,
    )
    page_ids: list[int] = []
    texts = (
        "液压泵需要定期检查压力和温度。<b>禁止超压</b> [原始]。",
        "阀组安装后应执行泄漏测试。",
        "",
    )
    for page_number, text in enumerate(texts, start=1):
        image_path = tmp_path / "pages" / "1" / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), "white").save(image_path)
        page = database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=text,
            status=PageStatus.REVIEWED if page_number == 1 else PageStatus.PENDING,
        )
        page_ids.append(page.id)
    database.update_document_page_count(document.id, len(texts))
    tag = database.create_tag("维护")
    project = database.create_project("泵站改造")
    database.set_page_tags(page_ids[0], [tag.id])
    database.set_page_projects(page_ids[0], [project.id])
    return database, EvidenceBasketService(database), document.id, page_ids


def test_create_add_multiple_prevent_duplicate_and_restore_after_restart(
    tmp_path: Path,
) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    default = service.default_basket()
    extra = service.create_basket("故障分析")

    first = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵需要定期检查压力和温度。",
        user_note="现场复核",
    )
    second = service.add_item(
        document_id=document_id,
        page_id=page_ids[1],
        evidence_text="阀组安装后应执行泄漏测试。",
    )

    assert [basket.name for basket in service.list_baskets()] == ["默认证据篮", "故障分析"]
    assert (first.basket_id, first.position, second.position) == (default.id, 1, 2)
    assert first.text_kind is EvidenceTextKind.ORIGINAL
    assert first.tags == ("维护",) and first.projects == ("泵站改造",)
    assert "液压泵需要定期检查" in first.context
    assert service.contains(page_ids[0], "液压泵需要定期检查压力和温度。")
    assert service.list_items(extra.id) == []

    with pytest.raises(DuplicateEvidenceError, match="重复"):
        service.add_item(
            document_id=document_id,
            page_id=page_ids[0],
            evidence_text="  液压泵需要定期检查压力和温度。  ",
        )

    reopened = EvidenceBasketService(Database(database.database_path))
    assert [item.id for item in reopened.list_items()] == [first.id, second.id]
    assert reopened.list_items()[0].user_note == "现场复核"


def test_user_excerpt_classification_chinese_special_chars_and_empty_selection(
    tmp_path: Path,
) -> None:
    _, service, document_id, page_ids = _library(tmp_path)

    matched = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="<b>禁止超压</b> [原始]",
    )
    excerpt = service.add_item(
        document_id=document_id,
        page_id=page_ids[2],
        evidence_text="用户根据页面图像整理的内容",
    )

    assert matched.text_kind is EvidenceTextKind.ORIGINAL
    assert excerpt.text_kind is EvidenceTextKind.USER_EXCERPT
    assert "未经原文匹配确认" in excerpt.text_kind.label
    assert evidence_text_html('<script>x</script> & "quote"') == (
        "&lt;script&gt;x&lt;/script&gt; &amp; &quot;quote&quot;"
    )
    with pytest.raises(EvidenceBasketError, match="不能为空"):
        service.add_item(
            document_id=document_id,
            page_id=page_ids[0],
            evidence_text=" \n ",
        )


def test_note_reorder_delete_clear_and_parameterized_user_text(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    first = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵需要定期检查压力和温度。",
    )
    second = service.add_item(
        document_id=document_id,
        page_id=page_ids[1],
        evidence_text="阀组安装后应执行泄漏测试。",
    )
    injection = "'); DROP TABLE documents; --"

    updated = service.update_note(first.id, injection)
    reordered = service.reorder([second.id, first.id])

    assert updated.user_note == injection
    assert [item.id for item in reordered] == [second.id, first.id]
    assert [item.position for item in reordered] == [1, 2]
    assert database.get_document(document_id) is not None
    with pytest.raises(EvidenceBasketError, match="4000"):
        service.update_note(first.id, "x" * 4001)

    service.remove_item(second.id)
    assert [(item.id, item.position) for item in service.list_items()] == [(first.id, 1)]
    assert service.clear() == 1
    assert service.list_items() == []


def test_missing_mismatched_and_changed_sources_stop_safely(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    with pytest.raises(EvidenceSourceError, match="文档记录不存在"):
        service.add_item(document_id=999, page_id=page_ids[0], evidence_text="证据")
    with pytest.raises(EvidenceSourceError, match="页面记录不存在"):
        service.add_item(document_id=document_id, page_id=999, evidence_text="证据")
    other_source = tmp_path / "raw" / "其他手册.pdf"
    other_source.write_bytes(b"other pdf")
    other_document = database.create_document(
        title="其他手册",
        filename="其他手册.pdf",
        source_path=other_source,
        sha256="b" * 64,
    )
    with pytest.raises(EvidenceSourceError, match="所属文档不一致"):
        service.add_item(
            document_id=other_document.id,
            page_id=page_ids[0],
            evidence_text="证据",
        )

    first = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵需要定期检查压力和温度。",
    )
    database.update_page(page_ids[0], extracted_text="原始文本后来变化")
    with pytest.raises(EvidenceSourceError, match="文本已发生变化"):
        service.validated_items()

    # Simulate legacy/corrupt external deletion with FK enforcement bypassed.
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM pages WHERE id = ?", (page_ids[0],))
    assert service.list_items()[0].id == first.id
    with pytest.raises(EvidenceSourceError, match="页面记录不存在"):
        service.validated_items()


def test_missing_files_and_document_cascade_are_explicit(tmp_path: Path) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    page = database.get_page(page_ids[0])
    assert page is not None
    page.image_path.unlink()
    with pytest.raises(EvidenceSourceError, match="页面图像缺失"):
        service.add_item(
            document_id=document_id,
            page_id=page_ids[0],
            evidence_text="液压泵",
        )

    # Restore the image, add evidence, then verify normal FK cascade removes it.
    Image.new("RGB", (2, 2), "white").save(page.image_path)
    service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵",
    )
    database.delete_document(document_id)
    assert service.list_items() == []
