"""Phase 2B-V adversarial verification for the schema v10 knowledge foundation.

The mandatory verification areas are exercised here with temporary databases
only. This file never opens the checkout production database.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid as uuid_module
from pathlib import Path

import pytest
from test_schema9_knowledge_foundation import _create_v9_database

from src import migrations as migrations_module
from src.database import Database
from src.knowledge_object_service import (
    KnowledgeObjectService,
    KnowledgeObjectValidationError,
    KnowledgeSourceLinkError,
)
from src.migrations import MigrationError, _read_schema_version, migrate_database
from src.models import (
    KNOWLEDGE_OBJECT_STABLE_TYPE,
    KnowledgeAuthorship,
    KnowledgeEpistemicBasis,
    KnowledgeRevisionEventType,
    build_stable_id,
)

V9_INJECTION_POINTS = (
    "v10_meta",
    "v10_objects_copy",
    "v10_before_drop_rename",
    "v10_legacy_events",
    "v10_baselines",
    "v10_memory_copy",
    "v10_version_record",
    "v10_before_commit",
)

TS = "2026-08-01T00:00:00+00:00"


# ---------------------------------------------------------------- v9 fixture
def _build_v9_with_special_rows(database_path: Path) -> None:
    """Build the shared v9 fixture and append Unicode / special rows."""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    _create_v9_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO knowledge_objects(kind, title, content, importance, status,"
            " created_at, updated_at)"
            " VALUES ('principle', '特殊＃泵阀①Ω🎯', '特殊内容<&>\"#', 'secondary',"
            " 'draft', ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content,"
            " document_id, page_id, created_at, updated_at)"
            " VALUES ('decision', '决策记忆@特殊', '决策正文🎯', 1, 1, ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO knowledge_memory_entries(kind, title, content,"
            " knowledge_object_id, created_at, updated_at)"
            " VALUES ('knowledge_change', '知识更新：特殊＃对象', '日志@特殊', 4, ?, ?)",
            (TS, TS),
        )
        connection.commit()


def _v9_state_snapshot(database_path: Path) -> dict[str, object]:
    """Capture a complete, comparable snapshot of the v9 database."""

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        snapshot: dict[str, object] = {
            "tables": sorted(tables),
            "version": _read_schema_version(database_path),
            "schema_migrations": connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall(),
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall(),
            "knowledge_objects": connection.execute(
                "SELECT * FROM knowledge_objects ORDER BY id"
            ).fetchall(),
            "knowledge_object_sources": connection.execute(
                "SELECT * FROM knowledge_object_sources ORDER BY id"
            ).fetchall(),
            "knowledge_relations": connection.execute(
                "SELECT * FROM knowledge_relations ORDER BY id"
            ).fetchall(),
            "knowledge_memory_entries": connection.execute(
                "SELECT * FROM knowledge_memory_entries ORDER BY id"
            ).fetchall(),
            "documents": connection.execute(
                "SELECT * FROM documents ORDER BY id"
            ).fetchall(),
            "pages": connection.execute("SELECT * FROM pages ORDER BY id").fetchall(),
            "notes": connection.execute("SELECT * FROM notes ORDER BY id").fetchall(),
            "evidence_items": connection.execute(
                "SELECT * FROM evidence_items ORDER BY id"
            ).fetchall(),
            "evidence_baskets": connection.execute(
                "SELECT * FROM evidence_baskets ORDER BY id"
            ).fetchall(),
            "import_records": connection.execute(
                "SELECT * FROM import_records ORDER BY id"
            ).fetchall(),
        }
    return snapshot


def _assert_v9_intact(
    database_path: Path, snapshot: dict[str, object]
) -> None:
    """Assert every captured v9 invariant survived a failed migration."""

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == set(snapshot["tables"]), tables
        assert _read_schema_version(database_path) == snapshot["version"] == 9
        assert connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall() == snapshot["schema_migrations"]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT * FROM knowledge_objects ORDER BY id"
        ).fetchall() == snapshot["knowledge_objects"]
        assert connection.execute(
            "SELECT * FROM knowledge_object_sources ORDER BY id"
        ).fetchall() == snapshot["knowledge_object_sources"]
        assert connection.execute(
            "SELECT * FROM knowledge_relations ORDER BY id"
        ).fetchall() == snapshot["knowledge_relations"]
        assert connection.execute(
            "SELECT * FROM knowledge_memory_entries ORDER BY id"
        ).fetchall() == snapshot["knowledge_memory_entries"]
        for table in (
            "documents",
            "pages",
            "notes",
            "evidence_items",
            "evidence_baskets",
            "import_records",
        ):
            assert connection.execute(
                f"SELECT * FROM {table} ORDER BY id"
            ).fetchall() == snapshot[table]
        # No half-written v10 structures may survive the rollback.
        for table_name in sorted(tables):
            assert "_v10" not in table_name and "_new" not in table_name, table_name
            assert "_old" not in table_name, table_name
        assert "knowledge_base_meta" not in tables
        assert "knowledge_object_revisions" not in tables


# ------------------------------------------------------- mandatory area one
@pytest.mark.parametrize("injection_point", V9_INJECTION_POINTS)
def test_v10_migration_failure_injection_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injection_point: str
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _build_v9_with_special_rows(database_path)
    snapshot = _v9_state_snapshot(database_path)

    monkeypatch.setattr(
        migrations_module, "_V10_INJECTION_POINT", injection_point
    )
    with pytest.raises(MigrationError, match=injection_point):
        migrate_database(database_path)
    monkeypatch.setattr(migrations_module, "_V10_INJECTION_POINT", None)

    _assert_v9_intact(database_path, snapshot)

    # A re-run of the migration must succeed once the injection is cleared.
    migrate_database(database_path)
    assert _read_schema_version(database_path) == 13


# ------------------------------------------------------- mandatory area two
def _backup_integrity(backup_path: Path) -> tuple[str, int, int]:
    with sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True) as c:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        fk = c.execute("PRAGMA foreign_key_check").fetchall()
        version = c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    return integrity, len(fk), int(version)


def test_non_empty_migration_creates_verified_v9_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _build_v9_with_special_rows(database_path)
    snapshot = _v9_state_snapshot(database_path)
    pre_sha256 = hashlib.sha256(database_path.read_bytes()).hexdigest()

    backup_path = migrate_database(database_path)

    assert backup_path is not None
    assert backup_path.parent == database_path.parent / "backups"
    assert backup_path.name.startswith("knowledge.v9.")
    assert backup_path.name.endswith(".db")
    assert backup_path.exists()

    integrity, fk_count, version = _backup_integrity(backup_path)
    assert integrity == "ok"
    assert fk_count == 0
    assert version == 9

    # The backup must be the pre-migration v9 database, not a copy of v10.
    with sqlite3.connect(
        f"file:{backup_path.as_posix()}?mode=ro", uri=True
    ) as connection:
        backup_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "knowledge_base_meta" not in backup_tables
        assert "knowledge_object_revisions" not in backup_tables
        assert connection.execute(
            "SELECT * FROM knowledge_objects ORDER BY id"
        ).fetchall() == snapshot["knowledge_objects"]
        assert connection.execute(
            "SELECT * FROM knowledge_object_sources ORDER BY id"
        ).fetchall() == snapshot["knowledge_object_sources"]
        assert connection.execute(
            "SELECT * FROM knowledge_relations ORDER BY id"
        ).fetchall() == snapshot["knowledge_relations"]
        assert connection.execute(
            "SELECT * FROM knowledge_memory_entries ORDER BY id"
        ).fetchall() == snapshot["knowledge_memory_entries"]
    post_sha256 = hashlib.sha256(database_path.read_bytes()).hexdigest()
    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert backup_sha256 != post_sha256  # 备份不是迁移后的副本
    assert _read_schema_version(database_path) == 13
    assert pre_sha256  # 记录迁移前强指纹（报告引用）

    # 重复启动（无待迁移版本）不得再覆盖/新增备份。
    backups_before = sorted((backup_path.parent).glob("knowledge.v9.*.db"))
    assert migrate_database(database_path) is None
    backups_after = sorted((backup_path.parent).glob("knowledge.v9.*.db"))
    assert backups_before == backups_after


def test_failed_migration_keeps_pre_migration_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _build_v9_with_special_rows(database_path)

    import src.migrations as migrations_module

    original = migrations_module._V10_INJECTION_POINT
    migrations_module._V10_INJECTION_POINT = "v10_objects_copy"
    try:
        with pytest.raises(MigrationError):
            migrate_database(database_path)
    finally:
        migrations_module._V10_INJECTION_POINT = original

    backups = list((database_path.parent / "backups").glob("knowledge.v9.*.db"))
    assert len(backups) == 1
    integrity, fk_count, version = _backup_integrity(backups[0])
    assert integrity == "ok"
    assert fk_count == 0
    assert version == 9


# ------------------------------------------------------- mandatory area three
def _preserved_tables_digest(database_path: Path) -> str:
    """SHA-256 over schema-only-untouched core tables (stable across v9→v10)."""

    tables = (
        "documents",
        "pages",
        "notes",
        "evidence_items",
        "evidence_baskets",
        "import_records",
    )
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as c:
        parts: list[str] = []
        for table in tables:
            rows = c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            parts.append(f"{table}=" + repr(rows))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _rows_as_dicts(
    database_path: Path, table: str, rows: list[tuple[object, ...]]
) -> list[dict[str, object]]:
    with sqlite3.connect(database_path) as connection:
        columns = [
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        ]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def test_core_data_fingerprint_preserved_and_mapped(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _build_v9_with_special_rows(database_path)
    pre_snapshot = _v9_state_snapshot(database_path)
    pre_digest = _preserved_tables_digest(database_path)

    old_objects = {
        int(row["id"]): row
        for row in _rows_as_dicts(
            database_path,
            "knowledge_objects",
            pre_snapshot["knowledge_objects"],  # type: ignore[arg-type]
        )
    }
    old_memory = _rows_as_dicts(
        database_path,
        "knowledge_memory_entries",
        pre_snapshot["knowledge_memory_entries"],  # type: ignore[arg-type]
    )
    old_changes = [row for row in old_memory if row["kind"] == "knowledge_change"]
    old_user_memory = [row for row in old_memory if row["kind"] != "knowledge_change"]

    migrate_database(database_path)

    assert _preserved_tables_digest(database_path) == pre_digest

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        objects = connection.execute(
            "SELECT * FROM knowledge_objects ORDER BY id"
        ).fetchall()
        sources = connection.execute(
            "SELECT * FROM knowledge_object_sources ORDER BY id"
        ).fetchall()
        relations = connection.execute(
            "SELECT * FROM knowledge_relations ORDER BY id"
        ).fetchall()
        memory = connection.execute(
            "SELECT * FROM knowledge_memory_entries ORDER BY id"
        ).fetchall()
        revisions = connection.execute(
            "SELECT * FROM knowledge_object_revisions ORDER BY id"
        ).fetchall()

    # 1) 原样保留字段逐项一致 + 正式映射。
    assert {int(row["id"]) for row in objects} == set(old_objects)
    assert len(objects) == len(old_objects)  # 无行丢失 / 无重复
    for row in objects:
        old = old_objects[int(row["id"])]
        assert row["kind"] == old["kind"]
        assert row["title"] == old["title"]
        assert row["content"] == old["content"]
        assert row["importance"] == old["importance"]
        assert row["created_at"] == old["created_at"]
        assert row["updated_at"] == old["updated_at"]
        assert row["authorship"] == "user"
        assert row["epistemic_basis"] == "unknown_legacy"
        expected_lifecycle = (
            "archived" if old["status"] == "archived" else "active"
        )
        assert row["lifecycle"] == expected_lifecycle
        expected_confirmation = (
            "confirmed" if old["status"] == "reviewed" else "unconfirmed"
        )
        assert row["confirmation_status"] == expected_confirmation

    # 2) 来源、关系逐项一致；fingerprint 三列按 NULL/1/迁移时点回填。
    assert len(sources) == len(pre_snapshot["knowledge_object_sources"])  # type: ignore[arg-type]
    for row in sources:
        assert row["source_fingerprint"] is None
        assert row["fingerprint_version"] == 1
        assert row["captured_at"]
    assert len(relations) == len(pre_snapshot["knowledge_relations"])  # type: ignore[arg-type]

    # 3) 用户 Memory ID 集合一致；knowledge_change 全部迁出。
    assert {int(row["id"]) for row in memory} == {
        int(row["id"]) for row in old_user_memory
    }
    assert all(row["status"] == "active" for row in memory)

    # 4) legacy_event 数量 == 旧 knowledge_change 数量；每对象恰一个 baseline。
    legacy_events = [
        row for row in revisions if row["event_type"] == "legacy_event"
    ]
    baselines = [row for row in revisions if row["event_type"] == "legacy_baseline"]
    assert len(legacy_events) == len(old_changes)
    assert len(baselines) == len(old_objects)
    for object_id in old_objects:
        change_count = sum(
            1
            for row in old_changes
            if row["knowledge_object_id"] == object_id
        )
        object_baselines = [
            row for row in baselines if row["knowledge_object_id"] == object_id
        ]
        assert len(object_baselines) == 1
        assert object_baselines[0]["revision_number"] == change_count + 1

    # 5) Unicode 与特殊字符逐字节一致（规范化后同值）。
    special = next(row for row in objects if int(row["id"]) == 4)
    assert special["title"] == "特殊＃泵阀①Ω🎯"
    assert special["content"] == "特殊内容<&>\"#"
    orphan_events = [
        row for row in legacy_events if row["knowledge_object_id"] is None
    ]
    assert len(orphan_events) == 1


# ------------------------------------------------------- mandatory area four
@pytest.fixture()
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "knowledge.db")


@pytest.fixture()
def service(database: Database) -> KnowledgeObjectService:
    return KnowledgeObjectService(database)


def _seed_document_and_page(database: Database) -> tuple[int, int]:
    document = database.create_document(
        title="测试手册",
        filename="manual.pdf",
        source_path="data/raw/manual.pdf",
        sha256="a" * 64,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path="data/pages/1/page_0001.png",
        extracted_text="页面文本",
    )
    return document.id, page.id


def test_unknown_legacy_is_migration_only(
    tmp_path: Path, database: Database, service: KnowledgeObjectService
) -> None:
    # v9→v10 可以写入 unknown_legacy。
    migrated_path = tmp_path / "migrated" / "knowledge.db"
    _build_v9_with_special_rows(migrated_path)
    migrate_database(migrated_path)
    migrated = Database(migrated_path)
    assert any(
        item.epistemic_basis is KnowledgeEpistemicBasis.UNKNOWN_LEGACY
        for item in migrated.list_knowledge_objects()
    )

    # 正常用户创建拒绝 unknown_legacy，且零副作用。
    with pytest.raises(KnowledgeObjectValidationError, match="未知"):
        service.create(
            kind="concept",
            title="概念",
            content="内容",
            epistemic_basis=KnowledgeEpistemicBasis.UNKNOWN_LEGACY,
        )
    assert database.count_knowledge_objects() == 0

    # 更新不得改回 unknown_legacy。
    view = service.create(
        kind="concept",
        title="概念",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_JUDGMENT,
    )
    with pytest.raises(KnowledgeObjectValidationError, match="未知"):
        service.update_epistemic_basis(
            view.knowledge_object.id,
            epistemic_basis=KnowledgeEpistemicBasis.UNKNOWN_LEGACY,
        )
    assert (
        service.get(view.knowledge_object.id).epistemic_basis
        is KnowledgeEpistemicBasis.PERSONAL_JUDGMENT
    )

    # 用户可以显式修订为合法 basis；修订后不会无故恢复 unknown。
    revised = service.update_epistemic_basis(
        view.knowledge_object.id,
        epistemic_basis=KnowledgeEpistemicBasis.SOURCE_DERIVED,
    )
    assert (
        revised.knowledge_object.epistemic_basis
        is KnowledgeEpistemicBasis.SOURCE_DERIVED
    )
    assert (
        service.get(view.knowledge_object.id).epistemic_basis
        is KnowledgeEpistemicBasis.SOURCE_DERIVED
    )


def test_ui_never_offers_unknown_legacy() -> None:
    from src.knowledge_object_ui import _BASIS_OPTIONS

    assert KnowledgeEpistemicBasis.UNKNOWN_LEGACY not in _BASIS_OPTIONS
    legal = set(KnowledgeEpistemicBasis) - {
        KnowledgeEpistemicBasis.UNKNOWN_LEGACY
    }
    assert set(_BASIS_OPTIONS) == legal


def test_ai_authorship_schema_compatible_but_writes_rejected(
    database: Database, service: KnowledgeObjectService
) -> None:
    # 数据库层允许未来 ai 枚举（schema 兼容），但 v0.5.2 业务写入拒绝。
    with pytest.raises(ValueError, match="AI"):
        database.create_knowledge_object(
            kind="fact",
            title="AI 草稿",
            content="内容",
            authorship=KnowledgeAuthorship.AI,
            epistemic_basis=KnowledgeEpistemicBasis.SOURCE_DERIVED,
        )
    assert database.count_knowledge_objects() == 0

    view = service.create(
        kind="fact",
        title="用户事实",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.SOURCE_DERIVED,
    )
    assert view.knowledge_object.authorship is KnowledgeAuthorship.USER

    # 原始 SQL 可以写入 ai（保留未来兼容能力），当前业务路径从未使用。
    with database._connection() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO knowledge_objects(kind, authorship, epistemic_basis,"
            " title, content, importance, lifecycle, superseded_by_ko_id,"
            " confirmation_status, confirmed_at, confirmed_revision,"
            " current_revision, created_at, updated_at)"
            " VALUES ('fact', 'ai', 'source_derived', 'AI 原始写入', '内容',"
            " 'normal', 'active', NULL, 'unconfirmed', NULL, NULL, 1, ?, ?)",
            (TS, TS),
        )
        connection.commit()
    assert database.count_knowledge_objects() == 2


# ------------------------------------------------------- mandatory area five
def test_kb_uuid_invariants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "knowledge.db"
    database = Database(path)
    first_uuid = database.get_knowledge_base_uuid()
    parsed = uuid_module.UUID(first_uuid)
    assert parsed.version == 4
    assert str(parsed) == first_uuid
    assert len(first_uuid) == 36

    with database._connection() as connection:  # noqa: SLF001
        rows = connection.execute("SELECT id, kb_uuid FROM knowledge_base_meta").fetchall()
        assert [tuple(row) for row in rows] == [(1, first_uuid)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_base_meta(id, kb_uuid, created_at)"
                " VALUES (2, 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', ?)",
                (TS,),
            )
        connection.rollback()

    # 重复打开 / 重复迁移不重新生成。
    assert Database(path).get_knowledge_base_uuid() == first_uuid
    assert migrate_database(path) is None
    assert Database(path).get_knowledge_base_uuid() == first_uuid

    # 失败迁移不留半生成 UUID（meta 表整体不存在）。
    failed_path = tmp_path / "failed" / "knowledge.db"
    _build_v9_with_special_rows(failed_path)
    original = migrations_module._V10_INJECTION_POINT
    migrations_module._V10_INJECTION_POINT = "v10_meta"
    try:
        with pytest.raises(MigrationError):
            migrate_database(failed_path)
    finally:
        migrations_module._V10_INJECTION_POINT = original
    with sqlite3.connect(failed_path) as connection:
        meta = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='knowledge_base_meta'"
        ).fetchone()
        assert meta is None

    # 备份复制后 UUID 保持一致。
    copy_path = tmp_path / "copy" / "knowledge.db"
    copy_path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as source, sqlite3.connect(copy_path) as dest:
        source.backup(dest)
        dest.commit()
    assert Database(copy_path).get_knowledge_base_uuid() == first_uuid

    # 两个独立新库 UUID 不同。
    other_uuid = Database(tmp_path / "other.db").get_knowledge_base_uuid()
    assert other_uuid != first_uuid

    # stable ID 确定性、跨库差异与非法输入拒绝。
    stable_a = build_stable_id(first_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, 7)
    assert stable_a == build_stable_id(first_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, 7)
    assert stable_a == f"{first_uuid}:knowledge_object:7"
    assert build_stable_id(other_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, 7) != stable_a
    with pytest.raises(ValueError, match="对象类型"):
        build_stable_id(first_uuid, "document", 7)
    for bad_local_id in (0, -1, True, "x"):
        with pytest.raises(ValueError, match="正整数"):
            build_stable_id(first_uuid, KNOWLEDGE_OBJECT_STABLE_TYPE, bad_local_id)


# ------------------------------------------------------- mandatory area six
def test_revision_three_sequence_model(
    database: Database, service: KnowledgeObjectService
) -> None:
    view = service.create(
        kind="concept",
        title="概念",
        content="v1",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_JUDGMENT,
    )
    object_id = view.knowledge_object.id
    assert view.knowledge_object.current_revision == 1
    assert view.knowledge_object.confirmed_revision is None

    service.confirm(object_id)  # event 2, 不伪造正文版本
    service.confirm(object_id)  # 幂等
    assert service.get(object_id).current_revision == 1
    assert service.get(object_id).confirmed_revision == 1

    service.unconfirm(object_id)  # event 3
    service.unconfirm(object_id)  # 幂等
    assert service.get(object_id).current_revision == 1

    updated = service.update_content(object_id, content="v2")  # event 4
    assert updated.knowledge_object.current_revision == 4
    service.confirm(object_id)  # event 5
    assert service.get(object_id).confirmed_revision == 4

    service.archive(object_id)  # event 6
    assert service.get(object_id).current_revision == 4
    service.unarchive(object_id)  # event 7
    assert service.get(object_id).current_revision == 4

    successor = service.create(
        kind="concept",
        title="后继",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_JUDGMENT,
    )
    service.supersede(object_id, successor.knowledge_object.id)  # event 8
    assert service.get(object_id).current_revision == 4

    _, page_id = _seed_document_and_page(database)
    service.link_source(object_id, source_type="page", source_id=page_id)  # event 9
    assert service.get(object_id).current_revision == 4
    source = database.list_knowledge_object_sources(object_id)[0]
    service.unlink_source(source.id)  # event 10
    assert service.get(object_id).current_revision == 4

    revisions = database.list_knowledge_revisions(object_id)
    numbers = [item.revision_number for item in revisions]
    assert numbers == list(range(1, 11))  # 严格递增且不重复
    assert [item.event_type for item in revisions] == [
        KnowledgeRevisionEventType.CREATED,
        KnowledgeRevisionEventType.CONFIRMATION_CHANGED,
        KnowledgeRevisionEventType.CONFIRMATION_CHANGED,
        KnowledgeRevisionEventType.CONTENT_UPDATED,
        KnowledgeRevisionEventType.CONFIRMATION_CHANGED,
        KnowledgeRevisionEventType.LIFECYCLE_CHANGED,
        KnowledgeRevisionEventType.LIFECYCLE_CHANGED,
        KnowledgeRevisionEventType.SUPERSESSION_CHANGED,
        KnowledgeRevisionEventType.SOURCE_LINKED,
        KnowledgeRevisionEventType.SOURCE_UNLINKED,
    ]
    # confirmed_revision 始终指向真实存在的内容版本（event 4 是正文修订）。
    content_events = [
        item for item in revisions if item.revision_number in (1, 4)
    ]
    assert len(content_events) == 2


def test_revision_failure_does_not_skip_numbers(
    database: Database, service: KnowledgeObjectService, monkeypatch
) -> None:
    view = service.create(
        kind="concept",
        title="概念",
        content="v1",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_JUDGMENT,
    )
    service.confirm(view.knowledge_object.id)
    service.update_content(view.knowledge_object.id, content="v2")
    assert service.get(view.knowledge_object.id).current_revision == 3

    def _fail(*args, **kwargs):
        raise RuntimeError("revision 写入失败")

    monkeypatch.setattr(service, "_insert_revision", _fail)
    with pytest.raises(RuntimeError):
        service.update_content(view.knowledge_object.id, content="v3-不应落库")
    monkeypatch.undo()

    assert service.get(view.knowledge_object.id).content == "v2"
    assert service.get(view.knowledge_object.id).current_revision == 3
    assert len(database.list_knowledge_revisions(view.knowledge_object.id)) == 3

    updated = service.update_content(view.knowledge_object.id, content="v4")
    assert updated.knowledge_object.current_revision == 4  # 不跳号
    assert [item.revision_number for item in
            database.list_knowledge_revisions(view.knowledge_object.id)] == [1, 2, 3, 4]


def test_legacy_and_formal_revisions_never_collide(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledge.db"
    _build_v9_with_special_rows(database_path)
    migrate_database(database_path)
    database = Database(database_path)
    service = KnowledgeObjectService(database)

    service.update_content(1, content="迁移后新内容")

    revisions = database.list_knowledge_revisions(1)
    assert [item.revision_number for item in revisions] == [1, 2, 3, 4]
    assert [item.event_type for item in revisions] == [
        KnowledgeRevisionEventType.LEGACY_EVENT,
        KnowledgeRevisionEventType.LEGACY_EVENT,
        KnowledgeRevisionEventType.LEGACY_BASELINE,
        KnowledgeRevisionEventType.CONTENT_UPDATED,
    ]


# ------------------------------------------------------- mandatory area seven
def test_source_link_revision_atomicity(
    database: Database, service: KnowledgeObjectService, monkeypatch
) -> None:
    view = service.create(
        kind="fact",
        title="事实",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.SOURCE_DERIVED,
    )
    object_id = view.knowledge_object.id
    _, page_id = _seed_document_and_page(database)

    # Source 写入成功但 Revision 写入失败 → 整体回滚。
    monkeypatch.setattr(service, "_insert_revision",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("revision 失败")))
    with pytest.raises(RuntimeError):
        service.link_source(object_id, source_type="page", source_id=page_id)
    monkeypatch.undo()
    assert database.list_knowledge_object_sources(object_id) == []
    assert len(database.list_knowledge_revisions(object_id)) == 1
    assert service.get(object_id).current_revision == 1

    # 正常 link 成功。
    service.link_source(object_id, source_type="page", source_id=page_id)
    assert len(database.list_knowledge_object_sources(object_id)) == 1

    # 重复 link：明确拒绝且零副作用。
    with pytest.raises(KnowledgeSourceLinkError):
        service.link_source(object_id, source_type="page", source_id=page_id)
    assert len(database.list_knowledge_object_sources(object_id)) == 1
    assert len(database.list_knowledge_revisions(object_id)) == 2

    # unlink 后 Revision 失败 → 来源保持已链接，不出现“已解绑但无记录”。
    source_id = database.list_knowledge_object_sources(object_id)[0].id
    monkeypatch.setattr(service, "_insert_revision",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("revision 失败")))
    with pytest.raises(RuntimeError):
        service.unlink_source(source_id)
    monkeypatch.undo()
    assert len(database.list_knowledge_object_sources(object_id)) == 1
    assert len(database.list_knowledge_revisions(object_id)) == 2

    # 正常 unlink 成功；重复 unlink 明确拒绝。
    service.unlink_source(source_id)
    assert database.list_knowledge_object_sources(object_id) == []
    with pytest.raises(KnowledgeSourceLinkError, match="不存在"):
        service.unlink_source(source_id)

    # 非法来源目标在写入前即被拒绝，且不影响 current/confirmation/lifecycle。
    before = service.get(object_id)
    with pytest.raises(KnowledgeSourceLinkError, match="不存在"):
        service.link_source(object_id, source_type="page", source_id=9999)
    after = service.get(object_id)
    assert after.current_revision == before.current_revision
    assert after.confirmation_status is before.confirmation_status
    assert after.lifecycle is before.lifecycle
    assert database.list_knowledge_object_sources(object_id) == []
