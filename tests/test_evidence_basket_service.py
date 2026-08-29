"""Tests for durable, source-linked v0.0.5 evidence baskets."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.evidence_basket_service import (
    DEFAULT_BASKET_NAME,
    DuplicateEvidenceError,
    EvidenceBasketError,
    EvidenceBasketService,
    EvidenceSourceError,
    _EvidenceRepository,
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


def _create_knowledge_object(database: Database, title: str) -> int:
    return database.create_knowledge_object(
        kind="fact", title=title, content="来源生命周期回归测试"
    ).id


def _link_source(
    database: Database, knowledge_object_id: int, source_type: str, source_id: int
) -> None:
    database.add_knowledge_object_source(
        knowledge_object_id=knowledge_object_id,
        source_type=source_type,
        source_id=source_id,
    )


def _knowledge_source_rows(database: Database) -> list[tuple[int, str, int]]:
    with sqlite3.connect(database.database_path) as connection:
        return [
            (int(row[0]), str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT knowledge_object_id, source_type, source_id "
                "FROM knowledge_object_sources "
                "ORDER BY knowledge_object_id, source_type, source_id"
            ).fetchall()
        ]


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


def test_same_page_distinct_selections_coexist_while_normalized_duplicate_is_rejected(
    tmp_path: Path,
) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    page_id = page_ids[0]

    first = service.add_item(
        document_id=document_id,
        page_id=page_id,
        evidence_text="液压泵需要定期检查压力和温度。",
    )
    second = service.add_item(
        document_id=document_id,
        page_id=page_id,
        evidence_text="禁止超压",
    )

    assert first.page_id == second.page_id == page_id
    assert first.id != second.id
    assert [item.evidence_text for item in service.list_items()] == [
        "液压泵需要定期检查压力和温度。",
        "禁止超压",
    ]
    with sqlite3.connect(database.database_path) as connection:
        stored = connection.execute(
            """
            SELECT page_id, selection_sha256
            FROM evidence_items
            ORDER BY position
            """
        ).fetchall()
    assert [row[0] for row in stored] == [page_id, page_id]
    assert len({row[1] for row in stored}) == 2

    with pytest.raises(DuplicateEvidenceError, match="重复"):
        service.add_item(
            document_id=document_id,
            page_id=page_id,
            evidence_text="  液压泵需要定期检查压力和温度。  ",
        )

    assert [item.id for item in service.list_items()] == [first.id, second.id]


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


def test_remove_item_cleans_all_ko_links_and_preserves_unrelated_sources(
    tmp_path: Path,
) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    default = service.default_basket()
    other_basket = service.create_basket("其他篮")
    target = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵需要定期检查压力和温度。",
    )
    same_basket = service.add_item(
        document_id=document_id,
        page_id=page_ids[1],
        evidence_text="阀组安装后应执行泄漏测试。",
    )
    cross_basket = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="禁止超压",
        basket_id=other_basket.id,
    )
    first_ko = _create_knowledge_object(database, "对象一")
    second_ko = _create_knowledge_object(database, "对象二")
    _link_source(database, first_ko, "evidence", target.id)
    _link_source(database, second_ko, "evidence", target.id)
    _link_source(database, first_ko, "evidence", same_basket.id)
    _link_source(database, first_ko, "evidence", cross_basket.id)
    # document/page ids intentionally collide numerically with target.id.
    _link_source(database, first_ko, "document", target.id)
    _link_source(database, first_ko, "page", target.id)

    service.remove_item(target.id, basket_id=default.id)

    assert service.get_item(target.id) is None
    assert [(item.id, item.position) for item in service.list_items(default.id)] == [
        (same_basket.id, 1)
    ]
    assert [(item.id, item.position) for item in service.list_items(other_basket.id)] == [
        (cross_basket.id, 1)
    ]
    assert database.get_knowledge_object(first_ko) is not None
    assert database.get_knowledge_object(second_ko) is not None
    assert _knowledge_source_rows(database) == [
        (first_ko, "document", target.id),
        (first_ko, "evidence", same_basket.id),
        (first_ko, "evidence", cross_basket.id),
        (first_ko, "page", target.id),
    ]
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_object_sources "
            "WHERE source_type = 'evidence' AND source_id = ?",
            (target.id,),
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("link_second", [False, True])
def test_clear_cleans_exact_basket_links_and_preserves_cross_basket(
    tmp_path: Path, link_second: bool
) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    default = service.default_basket()
    other_basket = service.create_basket("保留篮")
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
    other = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="禁止超压",
        basket_id=other_basket.id,
    )
    knowledge_object = _create_knowledge_object(database, "清空篮测试")
    _link_source(database, knowledge_object, "evidence", first.id)
    if link_second:
        _link_source(database, knowledge_object, "evidence", second.id)
    _link_source(database, knowledge_object, "evidence", other.id)

    assert service.clear(basket_id=default.id) == 2

    assert service.list_items(default.id) == []
    assert [(item.id, item.position) for item in service.list_items(other_basket.id)] == [
        (other.id, 1)
    ]
    assert service.clear(basket_id=default.id) == 0
    assert _knowledge_source_rows(database) == [
        (knowledge_object, "evidence", other.id)
    ]
    with sqlite3.connect(database.database_path) as connection:
        for deleted_id in (first.id, second.id):
            assert connection.execute(
                "SELECT COUNT(*) FROM knowledge_object_sources "
                "WHERE source_type = 'evidence' AND source_id = ?",
                (deleted_id,),
            ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


class _FailBeforeEvidenceDeleteConnection:
    """Raise after KO-link cleanup reaches the evidence row deletion."""

    def __init__(self, delegate: sqlite3.Connection) -> None:
        self._delegate = delegate

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("DELETE FROM evidence_items"):
            raise sqlite3.OperationalError("simulated evidence delete failure")
        return self._delegate.execute(sql, parameters)


def test_remove_item_rolls_back_source_cleanup_and_position_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, document_id, page_ids = _library(tmp_path)
    default = service.default_basket()
    target = service.add_item(
        document_id=document_id,
        page_id=page_ids[0],
        evidence_text="液压泵需要定期检查压力和温度。",
    )
    remaining = service.add_item(
        document_id=document_id,
        page_id=page_ids[1],
        evidence_text="阀组安装后应执行泄漏测试。",
    )
    knowledge_object = _create_knowledge_object(database, "回滚测试")
    _link_source(database, knowledge_object, "evidence", target.id)
    before_items = [(item.id, item.position) for item in service.list_items(default.id)]
    before_basket = next(
        basket for basket in service.list_baskets() if basket.id == default.id
    )
    original_connection = _EvidenceRepository._connection

    @contextmanager
    def failing_connection(
        self: _EvidenceRepository,
    ) -> Iterator[_FailBeforeEvidenceDeleteConnection]:
        with original_connection(self) as connection:
            yield _FailBeforeEvidenceDeleteConnection(connection)

    monkeypatch.setattr(_EvidenceRepository, "_connection", failing_connection)
    with pytest.raises(sqlite3.OperationalError, match="simulated evidence delete failure"):
        service.remove_item(target.id, basket_id=default.id)
    monkeypatch.undo()

    assert service.get_item(target.id) is not None
    assert service.get_item(remaining.id) is not None
    assert [(item.id, item.position) for item in service.list_items(default.id)] == before_items
    after_basket = next(
        basket for basket in service.list_baskets() if basket.id == default.id
    )
    assert after_basket.updated_at == before_basket.updated_at
    assert _knowledge_source_rows(database) == [
        (knowledge_object, "evidence", target.id)
    ]
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


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
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    assert service.list_items() == []


class _SelectSyncConnection:
    """Delegate parking the name-based basket lookup until both racers checked."""

    def __init__(self, delegate: sqlite3.Connection, barrier: threading.Barrier) -> None:
        self._delegate = delegate
        self._barrier = barrier

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        cursor = self._delegate.execute(sql, parameters)
        if sql.startswith("SELECT * FROM evidence_baskets WHERE name"):
            try:
                self._barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass  # Lock winner parked alone; proceed after the timeout.
        return cursor


def test_default_basket_concurrent_first_access_keeps_single_basket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both threads complete the existence check before either may insert; a
    # missing write lock then deterministically creates two default baskets.
    database = Database(tmp_path / "database" / "knowledge.db")
    service = EvidenceBasketService(database)
    barrier = threading.Barrier(2)
    original_connection = _EvidenceRepository._connection

    @contextmanager
    def synchronized_connection(
        self: _EvidenceRepository,
    ) -> Iterator[_SelectSyncConnection]:
        with original_connection(self) as connection:
            yield _SelectSyncConnection(connection, barrier)

    monkeypatch.setattr(_EvidenceRepository, "_connection", synchronized_connection)

    basket_ids: list[int] = []
    errors: list[Exception] = []

    def first_access() -> None:
        try:
            basket_ids.append(service.default_basket().id)
        except Exception as exc:  # surfaced by the assertions below
            errors.append(exc)

    threads = [threading.Thread(target=first_access) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(set(basket_ids)) == 1
    with sqlite3.connect(database.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM evidence_baskets WHERE name = ?",
            (DEFAULT_BASKET_NAME,),
        ).fetchone()[0]
    assert count == 1
