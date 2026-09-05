"""WP4 synthetic v13 artifact/seed/WAL adversarial tests; no private DB or AI."""

from __future__ import annotations

import builtins
import io
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_hosted_api_readiness import offline as offline  # noqa: F401

import src.database as database_module
import src.hosted.storage as storage_module
import src.hosted.storage_validation as validation
from src.agent import AgentRequest
from src.agent.tools import ToolContext, ToolInput, ToolResultStatus, build_phase1_handlers
from src.ai.provider import AiCallRecord, AiOutputRecord
from src.config import PROJECT_ROOT
from src.database import Database
from src.hosted.application import HostedDependencies
from src.hosted.readiness import HostedReadiness, ReadinessReason, check_hosted_database
from src.hosted.storage import bootstrap_hosted_storage, validate_hosted_sqlite_runtime_policy
from src.hosted.storage_validation import (
    HostedStorageError,
    StorageFailure,
    sha256_file,
    sidecars,
    validate_runtime_database,
    validate_seed_artifact,
)
from src.hosted_api.app import create_hosted_app
from src.hosted_config import HostedSettings, load_hosted_settings, validate_hosted_startup
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.runtime_profile import RuntimeConfigurationError

KB_UUID = "11111111-1111-4111-8111-111111111111"
OTHER_UUID = "22222222-2222-4222-8222-222222222222"
PRIVATE = "TEST_ONLY_PRIVATE_EXCEPTION"


@pytest.fixture(autouse=True)
def protect_production(monkeypatch: pytest.MonkeyPatch) -> None:
    protected = {
        (PROJECT_ROOT / "data/database/knowledge.db").resolve(),
        (PROJECT_ROOT / ".env").resolve(),
    }
    original_connect, original_open, original_io = sqlite3.connect, builtins.open, io.open

    def check(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        value = os.fsdecode(value)
        if value.startswith("file:"):
            from urllib.request import url2pathname

            value = url2pathname(value[5:].split("?", 1)[0])
        if value != ":memory:" and Path(value).resolve() in protected:
            pytest.fail("WP4 forbids opening the production DB or real dotenv")

    def connect(path, *args, **kwargs):
        check(path)
        if not str(path).startswith("file:") and str(path) != ":memory:":
            assert not Path(path).resolve().is_relative_to(PROJECT_ROOT), "No repo DB access"
        return original_connect(path, *args, **kwargs)

    def opening(path, *args, **kwargs):
        check(path)
        check_write(path, args, kwargs)
        return original_open(path, *args, **kwargs)

    def io_open(path, *args, **kwargs):
        check(path)
        check_write(path, args, kwargs)
        return original_io(path, *args, **kwargs)

    def check_write(path, args, kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if isinstance(path, (str, bytes, os.PathLike)) and any(flag in mode for flag in "wax+"):
            assert not Path(os.fsdecode(path)).resolve().is_relative_to(PROJECT_ROOT), (
                "No repo writes"
            )

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(builtins, "open", opening)
    monkeypatch.setattr(io, "open", io_open)


@pytest.fixture
def demo(tmp_path: Path) -> SimpleNamespace:
    artifact = tmp_path / "approved.db"
    database = Database(artifact)
    timestamp = "2026-01-01T00:00:00+00:00"
    with closing(sqlite3.connect(artifact)) as db:
        db.execute("UPDATE knowledge_base_meta SET kb_uuid = ?", (KB_UUID,))
        db.execute(
            """INSERT INTO documents(
            id,title,filename,source_path,sha256,page_count,created_at,updated_at)
            VALUES (1,'Public control manual','control.pdf','demo://documents/1',?,1,?,?)""",
            ("a" * 64, timestamp, timestamp),
        )
        db.execute(
            """INSERT INTO pages(id,document_id,page_number,image_path,extracted_text,
            search_extracted_text,status,review_status,created_at,updated_at)
            VALUES (1,1,1,'demo://pages/1','PID motor control','pid motor control',
            'text_extracted','reviewed',?,?)""",
            (timestamp, timestamp),
        )
        db.execute(
            "INSERT INTO evidence_baskets VALUES (1,'Public evidence',?,?)", (timestamp, timestamp)
        )
        db.execute(
            """INSERT INTO evidence_items(id,basket_id,document_id,page_id,document_title,
            filename,page_number,review_status,evidence_text,text_kind,context_kind,
            source_text_sha256,source_locator,selection_sha256,added_at,position,
            confirmation_status,confirmed_at)
            VALUES (1,1,1,1,'Public control manual','control.pdf',1,'reviewed',
            'PID motor control','original_material','system_generated',?,
            'document_id=1; page_id=1; page_number=1',?,?,1,'confirmed',?)""",
            ("b" * 64, "c" * 64, timestamp, timestamp),
        )
        db.commit()
    objects = KnowledgeObjectService(database)
    view = objects.create(
        kind="concept",
        title="PID controller",
        content="PID motor tuning",
        epistemic_basis="source_derived",
        source_links=(("page", 1, "Public page"),),
    )
    memory = KnowledgeMemoryService(database).create_entry(
        kind="experience",
        title="PID motor lesson",
        content="Check controller tuning",
        knowledge_object_id=view.knowledge_object.id,
        document_id=1,
        page_id=1,
    )
    with closing(sqlite3.connect(artifact)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    root = tmp_path / "hosted-data"
    root.mkdir()
    settings = HostedSettings(
        runtime_profile="hosted",
        data_root=root,
        demo_db_artifact=artifact,
        demo_db_sha256=sha256_file(artifact),
        demo_kb_uuid=KB_UUID,
        ai_api_key="TEST_ONLY_FAKE_KEY",
        ai_daily_token_budget=100,
    )
    return SimpleNamespace(
        artifact=artifact,
        settings=settings,
        object_id=view.knowledge_object.id,
        memory_id=memory.id,
        source_id=objects.source_views(view.knowledge_object.id)[0].source.id,
    )


def configured(demo, **changes) -> HostedSettings:
    return HostedSettings(**(demo.settings.model_dump() | changes))


def mutate(path: Path, sql: str, args=()) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, args)
        db.commit()


def expect_artifact_failure(demo) -> HostedStorageError:
    before = demo.artifact.read_bytes()
    with pytest.raises(HostedStorageError) as caught:
        validate_seed_artifact(demo.artifact, sha256_file(demo.artifact), KB_UUID)
    assert demo.artifact.read_bytes() == before
    assert PRIVATE not in str(caught.value) and str(demo.artifact) not in str(caught.value)
    return caught.value


def test_artifact_valid_readonly_unchanged_and_no_sidecars(demo, monkeypatch):
    before = demo.artifact.read_bytes()
    original = sqlite3.connect
    statements = []

    def connection(path, **kwargs):
        assert path.endswith("?mode=ro&immutable=1")
        db = original(path, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", connection)
    validate_seed_artifact(demo.artifact, demo.settings.demo_db_sha256, KB_UUID)
    # SQLite prefixes internal FTS SELECT/PRAGMA trace entries with '-- '.
    assert statements and all(
        sql.lstrip("- ").upper().startswith(("SELECT", "PRAGMA")) for sql in statements
    )
    assert demo.artifact.read_bytes() == before
    assert not any(path.exists() for path in sidecars(demo.artifact))


def test_artifact_wrong_sha_rejected_before_sqlite_and_seed(demo, monkeypatch):
    monkeypatch.setattr(
        sqlite3, "connect", Mock(side_effect=AssertionError("No SQLite before SHA"))
    )
    with pytest.raises(HostedStorageError) as caught:
        bootstrap_hosted_storage(configured(demo, demo_db_sha256="f" * 64))
    assert caught.value.code == StorageFailure.DIGEST
    assert not demo.settings.database_path.exists()


@pytest.mark.parametrize("value", ["", "abc", "f" * 63, "f" * 65, "g" * 64, " " + "a" * 64, 123])
def test_artifact_invalid_sha_configuration(demo, value):
    with pytest.raises(RuntimeConfigurationError):
        configured(demo, demo_db_sha256=value)


@pytest.mark.parametrize(
    "value",
    ["", "bad", "{11111111-1111-4111-8111-111111111111}", "11111111111141118111111111111111", 123],
)
def test_artifact_invalid_uuid_configuration(demo, value):
    with pytest.raises(RuntimeConfigurationError):
        configured(demo, demo_kb_uuid=value)


def test_artifact_config_env_only_and_digest_normalized(demo, monkeypatch):
    monkeypatch.setenv("EKB_DATA_ROOT", str(demo.settings.data_root))
    monkeypatch.setenv("EKB_DEMO_DB_ARTIFACT", str(demo.artifact))
    monkeypatch.setenv("EKB_DEMO_DB_SHA256", demo.settings.demo_db_sha256.upper())
    monkeypatch.setenv("EKB_DEMO_KB_UUID", KB_UUID)
    settings = load_hosted_settings()
    assert settings.demo_db_sha256 == demo.settings.demo_db_sha256
    assert settings.demo_db_artifact == demo.artifact and settings.demo_kb_uuid == KB_UUID


@pytest.mark.parametrize(
    "change",
    [
        "DELETE FROM schema_migrations WHERE version=12",
        "INSERT INTO schema_migrations VALUES (14,'future')",
        "DROP TABLE schema_migrations",
        "DELETE FROM schema_migrations WHERE version=4",
        "DROP TABLE ai_outputs",
        "CREATE TABLE quarantine_state (path TEXT)",
        "ALTER TABLE documents ADD COLUMN private_path TEXT",
    ],
)
def test_artifact_schema_exact_and_known(demo, change):
    mutate(demo.artifact, change)
    expect_artifact_failure(demo)


def test_artifact_corrupt_sqlite(demo):
    demo.artifact.write_bytes(b"synthetic corrupt SQLite")
    expect_artifact_failure(demo)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE knowledge_base_meta SET kb_uuid='22222222-2222-4222-8222-222222222222'",
        "DROP TABLE knowledge_base_meta",
        "DELETE FROM knowledge_base_meta",
    ],
)
def test_artifact_identity_required(demo, sql):
    mutate(demo.artifact, sql)
    expect_artifact_failure(demo)


@pytest.mark.parametrize("table", ["ai_calls", "ai_outputs"])
def test_artifact_rejects_historical_audit(demo, table):
    database = Database(demo.artifact)
    append_audit(database, table)
    with closing(sqlite3.connect(demo.artifact)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    assert expect_artifact_failure(demo).code == StorageFailure.SANITATION


def append_audit(database: Database, table: str) -> None:
    if table == "ai_calls":
        database.insert_ai_call(
            AiCallRecord(
                "TEST_ONLY_CALL",
                "completion",
                "fake",
                "d" * 64,
                1,
                "success",
                "wp4-test",
                total_tokens=2,
            )
        )
    else:
        database.insert_ai_output(
            AiOutputRecord("TEST_ONLY_OUTPUT", "fake", "e" * 64, "imported_answer", "wp4-test")
        )


@pytest.mark.parametrize(
    "value",
    [
        r"C:\private\x.pdf",
        "/home/user/x.pdf",
        r"\\server\private\x.pdf",
        "file:///private/x.pdf",
        "../private/x",
        "relative/path",
        "demo://../private",
        "demo://docs/../private",
        "demo://docs/%2fprivate",
    ],
)
@pytest.mark.parametrize(
    "table,column",
    [("documents", "source_path"), ("pages", "image_path"), ("pages", "markdown_path")],
)
def test_artifact_rejects_filesystem_paths(demo, value, table, column):
    mutate(demo.artifact, f"UPDATE {table} SET {column}=?", (value,))
    assert expect_artifact_failure(demo).code == StorageFailure.SANITATION


def test_artifact_does_not_scan_engineering_prose(demo):
    mutate(
        demo.artifact,
        "UPDATE knowledge_objects SET content=?",
        (r"An example C:\drivers and /usr/bin and file:// are engineering knowledge",),
    )
    validate_seed_artifact(demo.artifact, sha256_file(demo.artifact), KB_UUID)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO import_records(filename,status,started_at) VALUES ('x.pdf','failed','today')",
        "UPDATE documents SET import_error='TEST_ONLY_PRIVATE_EXCEPTION'",
        "UPDATE pages SET processing_error='TEST_ONLY_PRIVATE_EXCEPTION'",
        "UPDATE evidence_items SET filename='/home/user/x.pdf'",
    ],
)
def test_artifact_rejects_local_operational_metadata(demo, sql):
    mutate(demo.artifact, sql)
    assert expect_artifact_failure(demo).code == StorageFailure.SANITATION


def test_artifact_foreign_key_violations(demo):
    mutate(demo.artifact, "UPDATE pages SET document_id=999")
    assert expect_artifact_failure(demo).code == StorageFailure.INTEGRITY


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_artifact_rejects_sidecar_dependency(demo, suffix):
    Path(str(demo.artifact) + suffix).write_bytes(b"synthetic sidecar")
    assert expect_artifact_failure(demo).code == StorageFailure.ARTIFACT


def test_artifact_symlink_rejected_before_open(demo, monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == demo.artifact or original(path))
    assert expect_artifact_failure(demo).code == StorageFailure.ARTIFACT


@pytest.mark.parametrize("ancestor", [False, True])
def test_artifact_windows_reparse_point_rejected_before_open(demo, monkeypatch, ancestor):
    blocked = demo.artifact.parent if ancestor else demo.artifact
    original = Path.lstat

    def lstat(path):
        if path == blocked:
            return SimpleNamespace(
                st_mode=original(path).st_mode,
                st_file_attributes=validation.stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(
        validation, "sha256_file", Mock(side_effect=AssertionError("No input open"))
    )
    with pytest.raises(HostedStorageError) as caught:
        validate_seed_artifact(demo.artifact, demo.settings.demo_db_sha256, KB_UUID)
    assert caught.value.code == StorageFailure.ARTIFACT


def test_artifact_hardlink_alias_rejected(demo, tmp_path):
    alias = tmp_path / "alias.db"
    os.link(demo.artifact, alias)
    with pytest.raises(HostedStorageError):
        validate_seed_artifact(alias, demo.settings.demo_db_sha256, KB_UUID)


def test_artifact_production_path_rejected_without_open(demo):
    with pytest.raises(HostedStorageError) as caught:
        validate_seed_artifact(PROJECT_ROOT / "data/database/knowledge.db", "0" * 64, KB_UUID)
    assert caught.value.code == StorageFailure.ARTIFACT


@pytest.mark.parametrize(
    "folder,allowed",
    [
        ("data", False),
        ("logs", False),
        ("backups", False),
        ("runtime", False),
        ("deploy/demo", True),
    ],
)
def test_artifact_private_roots_vs_packaging_area(demo, tmp_path, monkeypatch, folder, allowed):
    source_root = tmp_path / "synthetic-code-root"
    path = source_root / folder / "demo.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(demo.artifact.read_bytes())
    monkeypatch.setattr(validation, "PROJECT_ROOT", source_root)
    if allowed:
        validate_seed_artifact(path, demo.settings.demo_db_sha256, KB_UUID)
    else:
        with pytest.raises(HostedStorageError):
            validate_seed_artifact(path, demo.settings.demo_db_sha256, KB_UUID)


def test_seed_valid_wal_directories_and_read_services(demo):
    before = demo.artifact.read_bytes()
    storage = bootstrap_hosted_storage(demo.settings)
    try:
        assert storage.database.last_backup_path is None
        assert storage.kb_uuid == KB_UUID and storage.database_path == demo.settings.database_path
        assert storage.readiness_reason(storage.database_path) is None
        assert {path.name for path in demo.settings.data_root.iterdir()} == {"database", "logs"}
        assert {path.name for path in demo.settings.database_dir.iterdir()} <= {
            "knowledge.db",
            "knowledge.db-wal",
            "knowledge.db-shm",
        }
        validate_hosted_startup(demo.settings)
        handlers = build_phase1_handlers(storage.database)
        args = {
            "page_search": {"query": "motor"},
            "knowledge_search": {"query": "PID"},
            "get_knowledge_object": {"stable_id": f"{KB_UUID}:knowledge_object:{demo.object_id}"},
            "get_knowledge_memory": {"stable_id": f"{KB_UUID}:knowledge_memory:{demo.memory_id}"},
            "inspect_provenance": {"stable_id": f"{KB_UUID}:knowledge_object:{demo.object_id}"},
            "inspect_source_integrity": {
                "stable_id": f"{KB_UUID}:knowledge_source:{demo.source_id}"
            },
            "get_evidence": {"stable_id": f"{KB_UUID}:evidence:1"},
        }
        for name, arguments in args.items():
            result = handlers[name](ToolInput(name, arguments), ToolContext())
            assert result.status in {ToolResultStatus.SUCCESS, ToolResultStatus.PARTIAL}, (
                name,
                result,
            )
        assert demo.artifact.read_bytes() == before
    finally:
        storage.close()


@pytest.mark.parametrize("field", ["demo_db_artifact", "demo_db_sha256", "demo_kb_uuid"])
def test_seed_missing_config_never_creates_empty_db(demo, monkeypatch, field):
    forbidden = Mock(side_effect=AssertionError("No initialization without seed"))
    monkeypatch.setattr(database_module, "migrate_database", forbidden)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(configured(demo, **{field: None}))
    assert not demo.settings.database_path.exists()
    forbidden.assert_not_called()


def test_seed_invalid_corpus_no_target_or_temporary(demo):
    mutate(demo.artifact, "UPDATE pages SET image_path='/private/x.png'")
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(configured(demo, demo_db_sha256=sha256_file(demo.artifact)))
    assert list(demo.settings.database_dir.iterdir()) == []


def test_seed_temp_validation_failure_cleans_temp(demo, monkeypatch):
    original = storage_module._copy_artifact

    def corrupt(*args):
        path = original(*args)
        path.write_bytes(b"synthetic bad copied bytes")
        return path

    monkeypatch.setattr(storage_module, "_copy_artifact", corrupt)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert list(demo.settings.database_dir.iterdir()) == []


def test_seed_publish_is_same_directory_atomic_and_no_overwrite(demo, monkeypatch):
    original = storage_module._publish_seed
    observations = []

    def racing(temporary, target):
        assert temporary.parent == target.parent == demo.settings.database_dir
        observations.append(temporary.read_bytes())
        target.write_bytes(b"OTHER_CALLER_TARGET")
        original(temporary, target)

    monkeypatch.setattr(storage_module, "_publish_seed", racing)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert demo.settings.database_path.read_bytes() == b"OTHER_CALLER_TARGET"
    assert observations == [demo.artifact.read_bytes()]
    assert list(demo.settings.database_dir.iterdir()) == [demo.settings.database_path]


@pytest.mark.parametrize("phase", ["copy", "publish", "initialize"])
def test_seed_io_failures_clean_owned_files(demo, monkeypatch, phase):
    function = {
        "copy": "_copy_artifact",
        "publish": "_publish_seed",
        "initialize": "_initialize_runtime",
    }[phase]
    monkeypatch.setattr(storage_module, function, Mock(side_effect=OSError(PRIVATE)))
    with pytest.raises(HostedStorageError) as caught:
        bootstrap_hosted_storage(demo.settings)
    assert PRIVATE not in str(caught.value)
    assert list(demo.settings.database_dir.iterdir()) == []


def test_runtime_reuses_target_with_audit_without_artifact_or_hash_equality(demo, monkeypatch):
    first = bootstrap_hosted_storage(demo.settings)
    append_audit(first.database, "ai_calls")
    append_audit(first.database, "ai_outputs")
    first.close()
    before = demo.settings.database_path.stat()
    monkeypatch.setattr(
        storage_module, "validate_seed_artifact", Mock(side_effect=AssertionError("No reseed"))
    )
    second = bootstrap_hosted_storage(configured(demo, demo_db_artifact=None, demo_db_sha256=None))
    try:
        assert len(second.database.list_ai_calls()) == len(second.database.list_ai_outputs()) == 1
        validate_runtime_database(second.database_path, KB_UUID)
        after = second.database_path.stat()
        # WAL checkpointing may change main-file bytes; identity/rows must persist.
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        assert sha256_file(second.database_path) != demo.settings.demo_db_sha256
    finally:
        second.close()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE knowledge_base_meta SET kb_uuid='22222222-2222-4222-8222-222222222222'",
        "DELETE FROM schema_migrations WHERE version=12",
        "INSERT INTO schema_migrations VALUES (14,'future')",
        "DROP TABLE knowledge_base_meta",
    ],
)
def test_runtime_invalid_existing_unchanged_before_migration(demo, monkeypatch, sql):
    demo.settings.database_dir.mkdir()
    target = demo.settings.database_path
    target.write_bytes(demo.artifact.read_bytes())
    mutate(target, sql)
    before = target.read_bytes()
    migrate = Mock(side_effect=AssertionError("Wrong DB must not reach migration"))
    monkeypatch.setattr(database_module, "migrate_database", migrate)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert target.read_bytes() == before
    migrate.assert_not_called()
    assert list(target.parent.iterdir()) == [target]


def test_runtime_rejects_private_recheck_path_but_allows_audit(demo):
    storage = bootstrap_hosted_storage(demo.settings)
    try:
        append_audit(storage.database, "ai_outputs")
        mutate(storage.database_path, "UPDATE ai_outputs SET recheck_path='/private/output.md'")
        with pytest.raises(HostedStorageError):
            validate_runtime_database(storage.database_path, KB_UUID)
    finally:
        storage.close()


@pytest.mark.parametrize("existing", [False, True])
def test_wal_migration_failure_fails_closed_without_original_deletion(
    demo, monkeypatch, caplog, existing
):
    if existing:
        demo.settings.database_dir.mkdir()
        demo.settings.database_path.write_bytes(demo.artifact.read_bytes())
    monkeypatch.setattr(
        database_module, "migrate_database", Mock(side_effect=RuntimeError(PRIVATE))
    )
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert demo.settings.database_path.exists() == existing
    if existing:
        assert demo.settings.database_path.read_bytes() == demo.artifact.read_bytes()
    assert PRIVATE not in caplog.text


def test_wal_actual_mode_checked_not_assumed(demo, monkeypatch):
    # A fake no-op migration returns no backup but fails to initialize WAL.
    monkeypatch.setattr(database_module, "migrate_database", lambda path: None)
    with pytest.raises(HostedStorageError) as caught:
        bootstrap_hosted_storage(demo.settings)
    assert caught.value.code == StorageFailure.WAL
    assert not demo.settings.database_path.exists()


def test_wal_unexpected_migration_backup_removed_and_bootstrap_fails(demo, monkeypatch):
    def unexpected(path):
        backup = path.parent / "backups" / "knowledge.v11.synthetic.db"
        backup.parent.mkdir()
        backup.write_bytes(b"SYNTHETIC_UNEXPECTED_BACKUP")
        return backup

    monkeypatch.setattr(database_module, "migrate_database", unexpected)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert list(demo.settings.database_dir.iterdir()) == []


@pytest.mark.parametrize(
    "process,worker",
    [
        (1, 1),
        (2, 1),
        (1, 2),
        (0, 1),
        (1, 0),
        (-1, 1),
        (1, -1),
        (True, 1),
        (1, True),
        (1.0, 1),
        (1, "1"),
    ],
)
def test_worker_policy_exact(process, worker):
    if type(process) is int and type(worker) is int and process == worker == 1:
        validate_hosted_sqlite_runtime_policy(process_count=process, worker_count=worker)
    else:
        with pytest.raises(HostedStorageError):
            validate_hosted_sqlite_runtime_policy(process_count=process, worker_count=worker)


def test_worker_invalid_policy_precedes_storage_mutation(demo):
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings, worker_count=2)
    assert list(demo.settings.data_root.iterdir()) == []


def test_readiness_is_observer_no_connections_or_file_writes_and_sees_live_wal(demo, monkeypatch):
    storage = bootstrap_hosted_storage(demo.settings)
    try:
        readiness = HostedReadiness(demo.settings, storage.readiness_reason)
        client = TestClient(
            create_hosted_app(
                settings=demo.settings,
                dependencies=HostedDependencies(readiness, Mock(), Mock(), AgentRequest, Mock()),
            )
        )
        before = {
            path: path.read_bytes() for path in demo.settings.data_root.rglob("*") if path.is_file()
        }
        with monkeypatch.context() as guard:
            guard.setattr(
                sqlite3,
                "connect",
                Mock(side_effect=AssertionError("Readiness cannot open/bootstrap DB")),
            )
            guard.setattr(
                storage_module,
                "bootstrap_hosted_storage",
                Mock(side_effect=AssertionError("No bootstrap")),
            )
            guard.setattr(Path, "mkdir", Mock(side_effect=AssertionError("No readiness mkdir")))
            guard.setattr(
                "src.hosted_config.NamedTemporaryFile",
                Mock(side_effect=AssertionError("No readiness write probe")),
            )
            for _ in range(3):
                assert client.get("/ready").status_code == 200
        assert before == {
            path: path.read_bytes() for path in demo.settings.data_root.rglob("*") if path.is_file()
        }
        # The observer must read committed WAL, not an immutable stale main file.
        mutate(storage.database_path, "UPDATE knowledge_base_meta SET kb_uuid=?", (OTHER_UUID,))
        response = client.get("/ready")
        assert response.status_code == 503 and response.json()["reasons"] == ["storage_invalid"]
        assert str(storage.database_path) not in response.text and KB_UUID not in response.text
    finally:
        storage.close()
    assert storage.readiness_reason(storage.database_path) == ReadinessReason.STORAGE_INVALID


def test_readiness_formal_identity_requires_bootstrap_observer(demo):
    demo.settings.database_dir.mkdir()
    demo.settings.database_path.write_bytes(demo.artifact.read_bytes())
    assert HostedReadiness(demo.settings).check().reasons == (ReadinessReason.STORAGE_INVALID,)


def test_readiness_fallback_never_ignores_live_wal(demo):
    storage = bootstrap_hosted_storage(demo.settings)
    try:
        assert check_hosted_database(storage.database_path) == ReadinessReason.DATABASE_UNAVAILABLE
    finally:
        storage.close()


def test_seed_directory_symlink_rejected_before_create(demo, monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path == demo.settings.database_dir or original(path)
    )
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert list(demo.settings.data_root.iterdir()) == []


def test_seed_artifact_changed_during_copy_is_rejected(demo, monkeypatch):
    original = storage_module._copy_artifact

    def changed(artifact, directory):
        temporary = original(artifact, directory)
        mutate(artifact, "UPDATE knowledge_objects SET content='changed source'")
        return temporary

    monkeypatch.setattr(storage_module, "_copy_artifact", changed)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert list(demo.settings.database_dir.iterdir()) == []


def test_seed_copy_flush_failure_cleans_partial_temporary(demo, monkeypatch):
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError(PRIVATE)))
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert list(demo.settings.database_dir.iterdir()) == []


def test_seed_failure_cleanup_does_not_remove_replacement_target(demo, monkeypatch):
    def replacement(settings):
        original = settings.database_path.with_name("original-owned.db")
        settings.database_path.rename(original)
        settings.database_path.write_bytes(b"OTHER_CALLER_REPLACEMENT")
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(storage_module, "_initialize_runtime", replacement)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert demo.settings.database_path.read_bytes() == b"OTHER_CALLER_REPLACEMENT"


def test_seed_cleanup_io_failure_remains_sanitized(demo, monkeypatch):
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == demo.settings.database_path:
            raise OSError(PRIVATE)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(
        storage_module, "_initialize_runtime", Mock(side_effect=RuntimeError(PRIVATE))
    )
    with pytest.raises(HostedStorageError) as caught:
        bootstrap_hosted_storage(demo.settings)
    assert PRIVATE not in str(caught.value)


def test_wal_legacy_migration_exception_logs_sanitized_only_for_bootstrap(
    demo, monkeypatch, caplog
):
    logger = logging.getLogger("src.migrations")
    filters = list(logger.filters)

    def failing(path):
        try:
            raise RuntimeError(PRIVATE)
        except RuntimeError:
            logger.exception("legacy raw failure %s", path)
            raise

    monkeypatch.setattr(database_module, "migrate_database", failing)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert PRIVATE not in caplog.text and str(demo.settings.database_path) not in caplog.text
    assert "storage_initialization_failed" in caplog.text
    assert logger.filters == filters


def test_wal_existing_backup_directory_never_deleted(demo, monkeypatch):
    demo.settings.database_dir.mkdir()
    demo.settings.database_path.write_bytes(demo.artifact.read_bytes())
    backup_dir = demo.settings.database_dir / "backups"
    backup_dir.mkdir()
    preserved = backup_dir / "operator-owned.db"
    preserved.write_bytes(b"OPERATOR_OWNED")
    migrate = Mock(side_effect=AssertionError("Unexpected backup namespace"))
    monkeypatch.setattr(database_module, "migrate_database", migrate)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert preserved.read_bytes() == b"OPERATOR_OWNED"
    migrate.assert_not_called()


def test_artifact_integrity_result_must_be_ok(demo, monkeypatch):
    original = sqlite3.connect

    class CorruptResult:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql == "PRAGMA integrity_check":
                return Mock(fetchall=lambda: [("TEST_ONLY_CORRUPTION",)])
            return self.db.execute(sql, *args)

        def close(self):
            self.db.close()

    monkeypatch.setattr(
        sqlite3, "connect", lambda *args, **kwargs: CorruptResult(original(*args, **kwargs))
    )
    assert expect_artifact_failure(demo).code == StorageFailure.INTEGRITY


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_wal_missing_target_with_orphan_sidecar_rejected(demo, suffix):
    demo.settings.database_dir.mkdir()
    orphan = Path(str(demo.settings.database_path) + suffix)
    orphan.write_bytes(b"OPERATOR_OWNED_ORPHAN")
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert orphan.read_bytes() == b"OPERATOR_OWNED_ORPHAN"
    assert not demo.settings.database_path.exists()


def test_readiness_wrong_path_or_replaced_file_identity_fails_closed(demo, monkeypatch):
    storage = bootstrap_hosted_storage(demo.settings)
    try:
        assert (
            storage.readiness_reason(demo.settings.data_root / "other.db")
            == ReadinessReason.STORAGE_INVALID
        )
        assert storage.readiness_reason(storage.database_path) is None
        original = Path.stat

        def replaced(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == storage.database_path:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_nlink=info.st_nlink,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino + 1,
                )
            return info

        monkeypatch.setattr(Path, "stat", replaced)
        assert storage.readiness_reason(storage.database_path) == ReadinessReason.STORAGE_INVALID
    finally:
        storage.close()


def test_seed_interrupted_temp_unlink_still_cleans_new_target(demo, monkeypatch):
    original = Path.unlink
    injected = False

    def fail_once(path, *args, **kwargs):
        nonlocal injected
        if path.name.startswith(".ekb-seed-") and not injected:
            injected = True
            raise OSError(PRIVATE)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    with pytest.raises(HostedStorageError):
        bootstrap_hosted_storage(demo.settings)
    assert injected and list(demo.settings.database_dir.iterdir()) == []
