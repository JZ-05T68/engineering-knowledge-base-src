"""Phase 2C targeted tests for the source fingerprint state machine (ADR-03)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.database import Database
from src.knowledge_object_service import (
    KnowledgeObjectService,
    KnowledgeSourceLinkError,
)
from src.models import (
    KnowledgeEpistemicBasis,
    KnowledgeSourceAggregateState,
    KnowledgeSourceStatus,
    aggregate_source_state,
)
from src.source_fingerprint import FINGERPRINT_VERSION, compute_source_fingerprint

TS = "2026-08-01T00:00:00+00:00"


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "knowledge.db")


@pytest.fixture()
def service(database: Database) -> KnowledgeObjectService:
    return KnowledgeObjectService(database)


def _create_document_and_pages(database: Database, texts: list[str]) -> list[int]:
    document = database.create_document(
        title="液压手册",
        filename="hyd.pdf",
        source_path="data/raw/hyd.pdf",
        sha256="a" * 64,
        page_count=len(texts),
    )
    page_ids: list[int] = []
    for number, text in enumerate(texts, start=1):
        page = database.create_page(
            document_id=document.id,
            page_number=number,
            image_path=f"data/pages/1/page_{number:04d}.png",
            extracted_text=text,
        )
        page_ids.append(page.id)
    return page_ids


def _create_object(service: KnowledgeObjectService) -> int:
    view = service.create(
        kind="fact",
        title="事实",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.SOURCE_DERIVED,
    )
    return view.knowledge_object.id


def _create_note(database: Database, page_id: int) -> int:
    with database._connection() as connection:  # noqa: SLF001
        cursor = connection.execute(
            "INSERT INTO notes(note_type, page_id, personal_note, created_at,"
            " updated_at) VALUES ('page', ?, '笔记正文', ?, ?)",
            (page_id, TS, TS),
        )
        connection.commit()
        return int(cursor.lastrowid)


def _create_evidence(database: Database, page_id: int) -> int:
    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO evidence_baskets(name, created_at, updated_at)"
            " VALUES ('默认证据篮', ?, ?)",
            (TS, TS),
        )
        cursor = connection.execute(
            "INSERT INTO evidence_items(basket_id, document_id, page_id,"
            " document_title, filename, page_number, review_status, projects_json,"
            " tags_json, evidence_type, evidence_text, text_kind, context,"
            " context_kind, user_note, source_text_sha256, source_locator,"
            " selection_sha256, confirmation_status, confirmed_at, added_at,"
            " position)"
            " VALUES (1, 1, ?, '液压手册', 'hyd.pdf', 1, 'reviewed', '[]', '[]',"
            " 'text_selection', '证据正文', 'user_excerpt', '', 'system_generated',"
            " '', ?, 'document_id=1; page_id=1', ?, 'unconfirmed', NULL, ?, 1)",
            (page_id, "b" * 64, "c" * 64, TS),
        )
        connection.commit()
        return int(cursor.lastrowid)


# ------------------------------------------------------------ canonical inputs
def test_page_fingerprint_ignores_markdown_but_tracks_text(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, ["页面文本"])[0]
    object_id = _create_object(service)
    service.link_source(object_id, source_type="page", source_id=page_id)

    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE pages SET markdown_content = '新 markdown' WHERE id = ?",
            (page_id,),
        )
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.VALID

    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE pages SET extracted_text = '改过的文本' WHERE id = ?", (page_id,)
        )
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.CHANGED


def test_document_fingerprint_tracks_sha256_only(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, ["文本"])[0]
    object_id = _create_object(service)
    with database._connection() as connection:  # noqa: SLF001
        document_id = connection.execute(
            "SELECT document_id FROM pages WHERE id = ?", (page_id,)
        ).fetchone()["document_id"]
        connection.execute(
            "UPDATE documents SET title = '改名' WHERE id = ?", (document_id,)
        )
        connection.commit()
    service.link_source(object_id, source_type="document", source_id=document_id)
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.VALID

    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE documents SET sha256 = ? WHERE id = ?", ("d" * 64, document_id)
        )
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.CHANGED


def test_evidence_fingerprint_excludes_confirmation_status(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, ["文本"])[0]
    evidence_id = _create_evidence(database, page_id)
    object_id = _create_object(service)
    service.link_source(object_id, source_type="evidence", source_id=evidence_id)

    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE evidence_items SET confirmation_status = 'confirmed',"
            " confirmed_at = ? WHERE id = ?",
            (TS, evidence_id),
        )
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.VALID

    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE evidence_items SET evidence_text = '证据改过' WHERE id = ?",
            (evidence_id,),
        )
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.CHANGED


# ------------------------------------------------------------ link-time capture
def test_link_captures_fingerprint_snapshot(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, ["页面文本"])[0]
    object_id = _create_object(service)
    view = service.link_source(object_id, source_type="page", source_id=page_id)

    source = view.source
    assert source.source_fingerprint is not None
    assert len(source.source_fingerprint) == 64
    assert int(source.source_fingerprint, 16) >= 0
    assert source.fingerprint_version == FINGERPRINT_VERSION == 1
    assert source.captured_at is not None
    with database._connection() as connection:  # noqa: SLF001
        expected = compute_source_fingerprint(connection, "page", page_id)
    assert source.source_fingerprint == expected


def test_empty_text_page_link_rejected_and_atomic(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, [""])[0]
    object_id = _create_object(service)

    with pytest.raises(KnowledgeSourceLinkError, match="文本层"):
        service.link_source(object_id, source_type="page", source_id=page_id)

    assert database.list_knowledge_object_sources(object_id) == []
    assert len(database.list_knowledge_revisions(object_id)) == 1


# ------------------------------------------------------------ read path states
def test_read_path_states_and_no_write_back(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, ["页面文本"])[0]
    object_id = _create_object(service)
    service.link_source(object_id, source_type="page", source_id=page_id)
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.VALID

    # 历史 NULL 指纹 → UNKNOWN。
    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE knowledge_object_sources SET source_fingerprint = NULL"
            " WHERE knowledge_object_id = ?",
            (object_id,),
        )
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.UNKNOWN

    # 读路径不得写库：整行前后一致。
    with database._connection() as connection:  # noqa: SLF001
        before = connection.execute(
            "SELECT * FROM knowledge_object_sources WHERE knowledge_object_id = ?",
            (object_id,),
        ).fetchall()
    service.source_views(object_id)
    service.source_health(object_id)
    with database._connection() as connection:  # noqa: SLF001
        after = connection.execute(
            "SELECT * FROM knowledge_object_sources WHERE knowledge_object_id = ?",
            (object_id,),
        ).fetchall()
    assert before == after

    # 目标删除 → MISSING。
    with database._connection() as connection:  # noqa: SLF001
        connection.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.MISSING


# ------------------------------------------------------------ aggregate truth table
@pytest.mark.parametrize(
    ("valid", "changed", "missing", "unknown", "expected"),
    [
        (0, 0, 0, 0, KnowledgeSourceAggregateState.UNSOURCED),
        (1, 0, 0, 0, KnowledgeSourceAggregateState.VALID),
        (1, 1, 0, 0, KnowledgeSourceAggregateState.PARTIALLY_VALID),
        (1, 0, 1, 0, KnowledgeSourceAggregateState.PARTIALLY_VALID),
        (1, 0, 0, 1, KnowledgeSourceAggregateState.PARTIALLY_VALID),
        (1, 1, 1, 0, KnowledgeSourceAggregateState.PARTIALLY_VALID),
        (1, 1, 0, 1, KnowledgeSourceAggregateState.PARTIALLY_VALID),
        (1, 0, 1, 1, KnowledgeSourceAggregateState.PARTIALLY_VALID),
        (0, 1, 0, 0, KnowledgeSourceAggregateState.CHANGED),
        (0, 1, 1, 1, KnowledgeSourceAggregateState.CHANGED),
        (0, 0, 1, 1, KnowledgeSourceAggregateState.MISSING),
        (0, 0, 0, 1, KnowledgeSourceAggregateState.UNKNOWN),
    ],
)
def test_aggregate_source_state_truth_table(
    valid: int, changed: int, missing: int, unknown: int,
    expected: KnowledgeSourceAggregateState,
) -> None:
    assert aggregate_source_state(valid, changed, missing, unknown) is expected


def test_source_health_counts_and_evidence_sufficiency(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_ids = _create_document_and_pages(database, ["文本一", "文本二"])
    object_id = _create_object(service)
    assert service.source_health(object_id).state is KnowledgeSourceAggregateState.UNSOURCED

    service.link_source(object_id, source_type="page", source_id=page_ids[0])
    service.link_source(object_id, source_type="page", source_id=page_ids[1])
    health = service.source_health(object_id)
    assert health.state is KnowledgeSourceAggregateState.VALID
    assert health.valid_count == 2 and health.changed_count == 0
    assert health.evidence_sufficient

    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE pages SET extracted_text = '已修改' WHERE id = ?",
            (page_ids[1],),
        )
        connection.commit()
    health = service.source_health(object_id)
    assert health.state is KnowledgeSourceAggregateState.PARTIALLY_VALID
    assert health.valid_count == 1 and health.changed_count == 1

    evidence_id = _create_evidence(database, page_ids[0])
    service.link_source(object_id, source_type="evidence", source_id=evidence_id)
    health = service.source_health(object_id)
    assert health.evidence_unconfirmed_count == 1
    assert health.state is KnowledgeSourceAggregateState.PARTIALLY_VALID
    assert not health.evidence_sufficient

    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE evidence_items SET confirmation_status = 'confirmed',"
            " confirmed_at = ? WHERE id = ?",
            (TS, evidence_id),
        )
        connection.commit()
    health = service.source_health(object_id)
    assert health.evidence_unconfirmed_count == 0


# ------------------------------------------------------------ recapture
def test_recapture_transitions_and_noop(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, ["页面文本"])[0]
    object_id = _create_object(service)
    service.link_source(object_id, source_type="page", source_id=page_id)
    source_id = database.list_knowledge_object_sources(object_id)[0].id

    # 同指纹重采 = no-op，不更新 captured_at。
    before = database.get_knowledge_object_source(source_id)
    assert before is not None
    service.recapture_source_fingerprint(source_id)
    after = database.get_knowledge_object_source(source_id)
    assert after is not None
    assert after.captured_at == before.captured_at

    # 内容变化 → CHANGED；重采后 → VALID，captured_at 更新，version 不变。
    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE pages SET extracted_text = '改过的文本' WHERE id = ?",
            (page_id,),
        )
        connection.commit()
    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.CHANGED
    recaptured = service.recapture_source_fingerprint(source_id)
    assert recaptured.status is KnowledgeSourceStatus.VALID
    refreshed = database.get_knowledge_object_source(source_id)
    assert refreshed is not None
    assert refreshed.captured_at != before.captured_at
    assert refreshed.fingerprint_version == 1
    # recapture 不产生 revision 事件。
    assert len(database.list_knowledge_revisions(object_id)) == 2  # created + linked

    # 目标删除 → 重采报错。
    with database._connection() as connection:  # noqa: SLF001
        connection.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        connection.commit()
    with pytest.raises(KnowledgeSourceLinkError, match="来源"):
        service.recapture_source_fingerprint(source_id)


def test_recapture_legacy_unknown_source(
    database: Database, service: KnowledgeObjectService
) -> None:
    page_id = _create_document_and_pages(database, ["页面文本"])[0]
    object_id = _create_object(service)
    with database._connection() as connection:  # noqa: SLF001
        cursor = connection.execute(
            "INSERT INTO knowledge_object_sources(knowledge_object_id, source_type,"
            " source_id, source_note, source_fingerprint, fingerprint_version,"
            " captured_at, created_at)"
            " VALUES (?, 'page', ?, '历史来源', NULL, 1, ?, ?)",
            (object_id, page_id, TS, TS),
        )
        connection.commit()
        source_id = int(cursor.lastrowid)

    assert service.source_views(object_id)[0].status is KnowledgeSourceStatus.UNKNOWN
    recaptured = service.recapture_source_fingerprint(source_id)
    assert recaptured.status is KnowledgeSourceStatus.VALID
    refreshed = database.get_knowledge_object_source(source_id)
    assert refreshed is not None
    assert refreshed.source_fingerprint is not None
    assert len(refreshed.source_fingerprint) == 64
