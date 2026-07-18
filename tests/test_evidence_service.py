"""Tests for traceable, copyable v0.0.4 evidence packages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.evidence_service import EvidencePackageBuilder
from src.models import PageStatus, SearchField, SearchResult

FIXED_TIME = datetime(2026, 7, 18, 16, 30, tzinfo=timezone(timedelta(hours=8)))


def _result(tmp_path: Path, *, status: PageStatus = PageStatus.REVIEWED) -> SearchResult:
    return SearchResult(
        page_id=17,
        document_id=4,
        document_title="液压设备维护手册",
        filename="液压 系统 manual.pdf",
        page_number=12,
        image_path=tmp_path / "页面 图像" / "第 12 页.png",
        content="液压泵出现异常噪声时，应检查吸油管路。",
        snippet="液压泵出现异常噪声时，应检查吸油管路。",
        rank=-2.0,
        status=status,
        match_type="页面提取文本、用户 Markdown 笔记",
        tags=("液压", "维护"),
        projects=("泵站改造",),
        match_fields=(SearchField.EXTRACTED_TEXT, SearchField.MARKDOWN),
        document_source_path=tmp_path / "原始 资料" / "液压 系统 manual.pdf",
        document_sha256="a" * 64,
        extracted_text="液压泵出现异常噪声时，应检查吸油管路和油液状态。",
        ocr_text="",
        markdown_content="# 用户复核笔记\n\n已在现场确认吸油管松动。",
    )


def test_complete_evidence_package_contains_traceability_and_clear_boundaries(
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)

    package = EvidencePackageBuilder().build(result, generated_at=FIXED_TIME)

    assert "文档标题：液压设备维护手册" in package
    assert "原始文件名：液压 系统 manual.pdf" in package
    assert "页码：第 12 页" in package
    assert "页面复核状态：人工复核完成（reviewed）" in package
    assert "所属项目：泵站改造" in package
    assert "标签：液压、维护" in package
    assert "## 命中片段" in package
    assert "## 原始材料内容" in package
    assert "## 用户笔记" in package
    assert package.index("## 原始材料内容") < package.index("## 用户笔记")
    assert "document_id=4; page_id=17; page_number=12" in package
    assert "document_sha256=" + "a" * 64 in package
    assert FIXED_TIME.isoformat(timespec="seconds") in package


def test_paths_with_spaces_and_chinese_are_absolute_and_not_fake_links(
    tmp_path: Path,
) -> None:
    package = EvidencePackageBuilder().build(
        _result(tmp_path), generated_at=FIXED_TIME
    )

    assert str((tmp_path / "原始 资料" / "液压 系统 manual.pdf").resolve()) in package
    assert str((tmp_path / "页面 图像" / "第 12 页.png").resolve()) in package
    assert "http://" not in package and "https://" not in package
    assert "ekb://" not in package


def test_missing_optional_metadata_and_note_do_not_create_empty_sections(
    tmp_path: Path,
) -> None:
    base = _result(tmp_path)
    result = SearchResult(
        page_id=base.page_id,
        document_id=base.document_id,
        document_title="",
        filename="",
        page_number=base.page_number,
        image_path=base.image_path,
        content="",
        snippet="",
        rank=base.rank,
        status=PageStatus.PENDING,
        extracted_text="",
        ocr_text="",
        markdown_content="",
    )

    package = EvidencePackageBuilder().build(result, generated_at=FIXED_TIME)

    assert "未命名文档" in package
    assert "未记录原始文件名" in package
    assert "所属项目：" not in package
    assert "标签：" not in package
    assert "## 用户笔记" not in package
    assert "请核对页面图像" in package
    assert "尚未处于“人工复核完成”状态" in package


@pytest.mark.parametrize(
    ("status", "warned"),
    [
        (PageStatus.PENDING, True),
        (PageStatus.DRAFT, True),
        (PageStatus.REVIEWED, False),
        (PageStatus.SKIPPED, True),
        (PageStatus.FAILED, True),
    ],
)
def test_every_review_status_is_labeled_and_unreviewed_text_is_warned(
    tmp_path: Path, status: PageStatus, warned: bool
) -> None:
    package = EvidencePackageBuilder().build(
        _result(tmp_path, status=status), generated_at=FIXED_TIME
    )

    assert f"{status.label}（{status.value}）" in package
    assert ("尚未处于“人工复核完成”状态" in package) is warned


def test_selected_chinese_excerpt_is_used_and_output_is_stable(tmp_path: Path) -> None:
    builder = EvidencePackageBuilder()
    result = _result(tmp_path)

    first = builder.build(
        result,
        selected_excerpt="用户选中的中文证据片段。",
        generated_at=FIXED_TIME,
    )
    second = builder.build(
        result,
        selected_excerpt="用户选中的中文证据片段。",
        generated_at=FIXED_TIME,
    )

    assert "用户选中的中文证据片段。" in first
    assert first == second
