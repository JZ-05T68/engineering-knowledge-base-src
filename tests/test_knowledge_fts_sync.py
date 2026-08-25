"""Phase 3C runtime shadow-column synchronization tests.

Proves that every knowledge write path keeps the v11 FTS shadow columns in
sync with the business fields inside one transaction: create, content/title
update, memory field updates, empty optional fields, delete cleanup, default
status filtering and revision isolation. Page-search behavior is not touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.database import Database
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService


@pytest.fixture()
def services(tmp_path: Path) -> tuple[Database, KnowledgeObjectService, KnowledgeMemoryService]:
    database = Database(tmp_path / "knowledge.db")
    return database, KnowledgeObjectService(database), KnowledgeMemoryService(database)


def _fts_rows(database: Database, table: str, match_expression: str) -> set[int]:
    with sqlite3.connect(database.database_path) as connection:
        rows = connection.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH ?",
            (match_expression,),
        ).fetchall()
    return {int(row[0]) for row in rows}


def _create_object(service: KnowledgeObjectService, title: str, content: str) -> int:
    return service.create(
        kind="concept",
        title=title,
        content=content,
        epistemic_basis="source_derived",
    ).knowledge_object.id


# --- create / update synchronization ----------------------------------------


def test_create_knowledge_object_is_searchable_immediately(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "液压系统故障分析", "齿轮泵压力脉动分析")

    assert _fts_rows(database, "knowledge_object_search", "液压") == {object_id}
    assert _fts_rows(database, "knowledge_object_search", "齿轮泵") == {object_id}


def test_update_knowledge_object_content_syncs_old_terms_out_and_new_terms_in(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "AlphaTitle", "oldterm content")
    assert _fts_rows(database, "knowledge_object_search", "oldterm") == {object_id}

    objects.update_content(object_id, title="BetaTitle", content="newterm content")

    assert _fts_rows(database, "knowledge_object_search", "oldterm") == set()
    assert _fts_rows(database, "knowledge_object_search", "newterm") == {object_id}
    assert _fts_rows(database, "knowledge_object_search", "betatitle") == {object_id}
    assert _fts_rows(database, "knowledge_object_search", "alphatitle") == set()


def test_memory_four_fields_are_searchable(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, _, memories = services
    entry = memories.create_entry(
        kind="experience",
        title="调试经验",
        content="复位电路问题",
        root_cause="电源上电时序错误",
        lesson="增加去耦电容",
    )

    assert _fts_rows(database, "knowledge_memory_search", "search_title:调试") == {entry.id}
    assert _fts_rows(database, "knowledge_memory_search", "search_content:复位") == {entry.id}
    assert (
        _fts_rows(database, "knowledge_memory_search", "search_root_cause:时序")
        == {entry.id}
    )
    assert _fts_rows(database, "knowledge_memory_search", "search_lesson:电容") == {entry.id}


def test_update_memory_fields_syncs_shadow_columns(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, _, memories = services
    entry = memories.create_entry(
        kind="decision", title="OldTitle", content="oldmemory"
    )
    assert _fts_rows(database, "knowledge_memory_search", "oldmemory") == {entry.id}

    memories.update_entry(
        entry.id, title="NewTitle", content="newmemory", lesson="lessonword"
    )

    assert _fts_rows(database, "knowledge_memory_search", "oldmemory") == set()
    assert _fts_rows(database, "knowledge_memory_search", "newmemory") == {entry.id}
    assert _fts_rows(database, "knowledge_memory_search", "newtitle") == {entry.id}
    assert _fts_rows(database, "knowledge_memory_search", "lessonword") == {entry.id}


# --- language coverage ------------------------------------------------------


def test_chinese_knowledge_object_sync(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "中文标题", "中文内容关键词")
    assert _fts_rows(database, "knowledge_object_search", "中文") == {object_id}
    assert _fts_rows(database, "knowledge_object_search", "关键词") == {object_id}


def test_english_knowledge_object_sync(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "Cavitation analysis", "pump cavitation check")
    assert _fts_rows(database, "knowledge_object_search", "cavitation") == {object_id}
    assert _fts_rows(database, "knowledge_object_search", "pump") == {object_id}


def test_mixed_language_knowledge_object_sync(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "定时器 Timer 预分频器", "PWM prescaler 配置")
    assert _fts_rows(database, "knowledge_object_search", "timer") == {object_id}
    assert _fts_rows(database, "knowledge_object_search", "定时器") == {object_id}
    assert _fts_rows(database, "knowledge_object_search", "prescaler") == {object_id}


def test_empty_memory_optional_fields_sync(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, _, memories = services
    entry = memories.create_entry(kind="decision", title="Only title")
    with sqlite3.connect(database.database_path) as connection:
        row = connection.execute(
            "SELECT search_title, search_content, search_root_cause, search_lesson"
            " FROM knowledge_memory_entries WHERE id = ?",
            (entry.id,),
        ).fetchone()
    assert row == ("only title", "", "", "")
    assert _fts_rows(database, "knowledge_memory_search", "only") == {entry.id}


# --- delete semantics -------------------------------------------------------


def test_delete_knowledge_object_cleans_fts_and_keeps_revisions(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "待删除对象", "删除关键词")
    objects.update_content(object_id, content="删除关键词 修订")
    with sqlite3.connect(database.database_path) as connection:
        revision_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_object_revisions WHERE knowledge_object_id = ?",
            (object_id,),
        ).fetchone()[0]
    assert revision_count >= 2

    objects.delete(object_id)

    with sqlite3.connect(database.database_path) as connection:
        assert _fts_rows(database, "knowledge_object_search", "删除关键词") == set()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_object_revisions"
                " WHERE knowledge_object_id = ?",
                (object_id,),
            ).fetchone()[0]
            == revision_count
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_objects WHERE id = ?", (object_id,)
            ).fetchone()[0]
            == 0
        )


def test_delete_memory_cleans_fts(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, _, memories = services
    entry = memories.create_entry(kind="experience", title="待删除记忆", content="记忆关键词")

    memories.delete_entry(entry.id)

    assert _fts_rows(database, "knowledge_memory_search", "记忆关键词") == set()


# --- default status filtering -----------------------------------------------


def test_archived_knowledge_object_excluded_by_default_and_included_on_demand(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "归档对象", "archiveword")
    objects.archive(object_id)

    default = database.search_knowledge('"archiveword"')
    included = database.search_knowledge('"archiveword"', include_archived=True)

    assert [result.id for result in default] == []
    assert [result.id for result in included] == [object_id]


def test_superseded_knowledge_object_excluded_by_default_and_included_on_demand(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    old_id = _create_object(objects, "旧对象", "supersededword")
    new_id = _create_object(objects, "新对象", "new object content")
    objects.supersede(old_id, new_id)

    default = database.search_knowledge('"supersededword"')
    included = database.search_knowledge('"supersededword"', include_superseded=True)

    assert [result.id for result in default] == []
    assert [result.id for result in included] == [old_id]


def test_archived_memory_excluded_by_default_and_included_on_demand(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, _, memories = services
    entry = memories.create_entry(kind="experience", title="归档记忆", content="archivedmemory")
    memories.set_status(entry.id, status="archived")

    default = database.search_knowledge('"archivedmemory"')
    included = database.search_knowledge('"archivedmemory"', include_archived=True)

    assert [result.id for result in default] == []
    assert [result.id for result in included] == [entry.id]


def test_revision_content_never_appears_in_search(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "修订对象", "oldrevword")
    objects.update_content(object_id, content="newrevword")

    assert _fts_rows(database, "knowledge_object_search", "oldrevword") == set()
    assert _fts_rows(database, "knowledge_object_search", "newrevword") == {object_id}


# --- atomicity --------------------------------------------------------------


def test_failed_create_writes_nothing(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    with pytest.raises(ValueError):
        objects.create(
            kind="concept",
            title="超长标题" * 100,
            content="内容",
            epistemic_basis="source_derived",
        )
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM knowledge_object_search").fetchone()[0]
            == 0
        )


def test_failed_update_keeps_previous_content_searchable(
    services: tuple[Database, KnowledgeObjectService, KnowledgeMemoryService],
) -> None:
    database, objects, _ = services
    object_id = _create_object(objects, "原子对象", "atomicold")
    with pytest.raises(ValueError):
        objects.update_content(object_id, content="x" * 20001)

    assert _fts_rows(database, "knowledge_object_search", "atomicold") == {object_id}
