"""Phase 7 embedding persistence / freshness contract tests.

All vectors are fake tuples; no network, no embedding API, no real model
calls. The tests prove the v8 persistence contract: safe SQLite writes,
exact identity lookup, freshness by source fingerprint, in-place upsert,
multi-configuration coexistence, FK cascade, corruption fail-closed, and
backup/restore fidelity.
"""

from __future__ import annotations

import hashlib
import socket
import sqlite3
import struct
from pathlib import Path

import pytest
from PIL import Image

from src.ai.embedding_store import (
    EMBEDDING_VECTOR_FORMAT_VERSION,
    decode_vector,
    encode_vector,
    vector_blob_size,
)
from src.backup_service import BackupService, validate_backup
from src.database import Database, DatabaseError, RecordNotFoundError
from src.document_deletion_service import DocumentDeletionService

TS_MODEL = "fake-embedding-model"
HASH_A = hashlib.sha256("闭环控制原文".encode()).hexdigest()
HASH_B = hashlib.sha256("改动后的文本".encode()).hexdigest()
HASH_C = hashlib.sha256("另一页文本".encode()).hexdigest()


# ---------------------------------------------------------------- serialization
def test_encode_decode_roundtrip_exact_values() -> None:
    vector = (0.5, -0.25, 1.5, 2.0)

    blob = encode_vector(vector, dimensions=4)

    assert blob[0] == EMBEDDING_VECTOR_FORMAT_VERSION
    assert len(blob) == vector_blob_size(4) == 1 + 4 * 4
    assert decode_vector(blob, dimensions=4) == vector


def test_encode_decode_roundtrip_is_byte_stable_for_arbitrary_values() -> None:
    vector = (0.1, 0.2, 0.3, -1.75, 12.3456)

    once = encode_vector(vector, dimensions=5)
    twice = encode_vector(decode_vector(once, dimensions=5), dimensions=5)

    assert once == twice
    assert decode_vector(once, dimensions=5) == pytest.approx(vector, rel=1e-6)


def test_roundtrip_supports_1024_dimensions() -> None:
    vector = tuple((index % 16) / 16.0 for index in range(1024))

    blob = encode_vector(vector, dimensions=1024)

    assert len(blob) == 1 + 4 * 1024
    assert decode_vector(blob, dimensions=1024) == pytest.approx(vector)


def test_encode_accepts_int_components() -> None:
    assert decode_vector(encode_vector((1, 2, 3), dimensions=3), dimensions=3) == (
        1.0,
        2.0,
        3.0,
    )


@pytest.mark.parametrize(
    ("vector", "dimensions"),
    [
        ((), 3),  # 空向量
        ((0.1, 0.2), 3),  # 长度不足
        ((0.1, 0.2, 0.3, 0.4), 3),  # 长度超出
        ((float("nan"), 0.2, 0.3), 3),  # NaN
        ((float("inf"), 0.2, 0.3), 3),  # Inf
        ((-float("inf"), 0.2, 0.3), 3),  # -Inf
        ((1e300, 0.2, 0.3), 3),  # 超出 float32 范围
        (("0.1", 0.2, 0.3), 3),  # 非数值分量
        ((True, 0.2, 0.3), 3),  # 布尔不是数值
    ],
)
def test_encode_rejects_invalid_vectors(
    vector: tuple, dimensions: int
) -> None:
    with pytest.raises(ValueError):
        encode_vector(vector, dimensions=dimensions)


@pytest.mark.parametrize("dimensions", [0, -1, True, 2.5, "4"])
def test_dimensions_must_be_positive_int(dimensions: object) -> None:
    with pytest.raises(ValueError):
        encode_vector((0.1, 0.2), dimensions=dimensions)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        decode_vector(b"\x01" + b"\x00" * 8, dimensions=dimensions)  # type: ignore[arg-type]


def test_decode_rejects_wrong_byte_length() -> None:
    blob = encode_vector((0.5, 0.25, 1.5), dimensions=3)
    with pytest.raises(ValueError):
        decode_vector(blob[:-1], dimensions=3)
    with pytest.raises(ValueError):
        decode_vector(blob + b"\x00", dimensions=3)
    with pytest.raises(ValueError):
        decode_vector(blob, dimensions=4)


def test_decode_rejects_unknown_format_version() -> None:
    blob = encode_vector((0.5, 0.25), dimensions=2)
    tampered = bytes([EMBEDDING_VECTOR_FORMAT_VERSION + 1]) + blob[1:]
    with pytest.raises(ValueError):
        decode_vector(tampered, dimensions=2)


def test_decode_rejects_non_bytes() -> None:
    with pytest.raises(ValueError):
        decode_vector("not-bytes", dimensions=2)  # type: ignore[arg-type]


def test_decode_rejects_non_finite_stored_values() -> None:
    for bad in (float("nan"), float("inf")):
        blob = bytes([EMBEDDING_VECTOR_FORMAT_VERSION]) + struct.pack("<2f", bad, 0.5)
        with pytest.raises(ValueError):
            decode_vector(blob, dimensions=2)


# --------------------------------------------------------------------- fixtures
def _library(tmp_path: Path) -> tuple[Database, int, int]:
    """Create a database with one document and two real pages."""

    database = Database(tmp_path / "knowledge.db")
    document = database.create_document(
        title="液压手册",
        filename="hyd.pdf",
        source_path=tmp_path / "hyd.pdf",
        sha256="d" * 64,
    )
    first = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "page_0001.png",
        extracted_text="闭环控制原文",
    )
    second = database.create_page(
        document_id=document.id,
        page_number=2,
        image_path=tmp_path / "page_0002.png",
        extracted_text="另一页文本",
    )
    return database, first.id, second.id


def _embedding_count(database: Database) -> int:
    with sqlite3.connect(database.database_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM page_embeddings").fetchone()[0]
        )


# ------------------------------------------------------------------ persistence
def test_upsert_and_get_roundtrip(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)

    stored = database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )
    loaded = database.get_page_embedding(
        page_id=page_id, model=TS_MODEL, dimensions=4, config_version=1
    )

    assert loaded is not None
    assert loaded == stored
    assert loaded.page_id == page_id
    assert loaded.source_text_sha256 == HASH_A
    assert loaded.vector == (0.5, -0.25, 1.5, 2.0)
    assert loaded.created_at <= loaded.updated_at


def test_get_page_embedding_returns_none_when_absent(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    assert (
        database.get_page_embedding(
            page_id=page_id, model=TS_MODEL, dimensions=4, config_version=1
        )
        is None
    )


def test_duplicate_upsert_does_not_accumulate_rows(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    payload = {
        "page_id": page_id,
        "source_text_sha256": HASH_A,
        "model": TS_MODEL,
        "dimensions": 4,
        "config_version": 1,
        "vector": (0.5, -0.25, 1.5, 2.0),
    }

    first = database.upsert_page_embedding(**payload)
    second = database.upsert_page_embedding(**payload)

    assert second.id == first.id
    assert _embedding_count(database) == 1


def test_source_hash_change_updates_current_record_in_place(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    original = database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )

    updated = database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_B,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(1.0, 2.0, 3.0, 4.0),
    )

    assert updated.id == original.id
    assert updated.source_text_sha256 == HASH_B
    assert updated.vector == (1.0, 2.0, 3.0, 4.0)
    assert updated.created_at == original.created_at
    assert _embedding_count(database) == 1


# --------------------------------------------------------------------- freshness
def test_fresh_lookup_hits_on_exact_identity(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )

    fresh = database.get_fresh_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
    )

    assert fresh is not None
    assert fresh.vector == (0.5, -0.25, 1.5, 2.0)


def test_stale_source_hash_is_not_returned(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )

    assert (
        database.get_fresh_page_embedding(
            page_id=page_id,
            source_text_sha256=HASH_B,
            model=TS_MODEL,
            dimensions=4,
            config_version=1,
        )
        is None
    )


@pytest.mark.parametrize(
    "override",
    [
        {"model": "other-model"},
        {"dimensions": 8},
        {"config_version": 2},
        {"page_id_offset": 1},
    ],
)
def test_fresh_lookup_rejects_any_identity_mismatch(
    tmp_path: Path, override: dict
) -> None:
    database, page_id, other_page_id = _library(tmp_path)
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )
    query = {
        "page_id": page_id,
        "source_text_sha256": HASH_A,
        "model": TS_MODEL,
        "dimensions": 4,
        "config_version": 1,
    }
    for key, value in override.items():
        if key == "page_id_offset":
            query["page_id"] = other_page_id
        else:
            query[key] = value

    assert database.get_fresh_page_embedding(**query) is None


def test_multiple_model_configurations_coexist(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    configurations = (
        {"model": "fake-emb-a", "dimensions": 4, "config_version": 1},
        {"model": "fake-emb-b", "dimensions": 4, "config_version": 1},
        {"model": "fake-emb-a", "dimensions": 8, "config_version": 1},
        {"model": "fake-emb-a", "dimensions": 4, "config_version": 2},
    )

    for index, config in enumerate(configurations):
        database.upsert_page_embedding(
            page_id=page_id,
            source_text_sha256=HASH_A,
            vector=(float(index + 1),) * config["dimensions"],
            **config,
        )

    assert _embedding_count(database) == 4
    for index, config in enumerate(configurations):
        loaded = database.get_fresh_page_embedding(
            page_id=page_id, source_text_sha256=HASH_A, **config
        )
        assert loaded is not None
        assert loaded.vector == (float(index + 1),) * config["dimensions"]


def test_pages_do_not_share_embedding_rows(tmp_path: Path) -> None:
    database, first_page, second_page = _library(tmp_path)
    database.upsert_page_embedding(
        page_id=first_page,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, 0.5, 0.5, 0.5),
    )
    database.upsert_page_embedding(
        page_id=second_page,
        source_text_sha256=HASH_C,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.25, 0.25, 0.25, 0.25),
    )

    assert _embedding_count(database) == 2
    assert (
        database.get_fresh_page_embedding(
            page_id=first_page,
            source_text_sha256=HASH_C,
            model=TS_MODEL,
            dimensions=4,
            config_version=1,
        )
        is None
    )


# --------------------------------------------------------------------- validation
def test_upsert_rejects_unknown_page(tmp_path: Path) -> None:
    database, _, _ = _library(tmp_path)
    with pytest.raises(RecordNotFoundError):
        database.upsert_page_embedding(
            page_id=999,
            source_text_sha256=HASH_A,
            model=TS_MODEL,
            dimensions=4,
            config_version=1,
            vector=(0.5, 0.5, 0.5, 0.5),
        )


def test_fk_failure_leaves_store_untouched_and_recoverable(tmp_path: Path) -> None:
    """公开 API 下 IntegrityError 仅对应 page 缺失，且不污染后续写入。"""

    database, page_id, _ = _library(tmp_path)
    with pytest.raises(RecordNotFoundError):
        database.upsert_page_embedding(
            page_id=999,
            source_text_sha256=HASH_A,
            model=TS_MODEL,
            dimensions=4,
            config_version=1,
            vector=(0.5, 0.5, 0.5, 0.5),
        )
    assert _embedding_count(database) == 0

    stored = database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, 0.5, 0.5, 0.5),
    )
    assert stored.page_id == page_id
    assert _embedding_count(database) == 1


@pytest.mark.parametrize(
    "field",
    [
        {"source_text_sha256": "not-a-hash"},
        {"source_text_sha256": ""},
        {"model": "   "},
        {"dimensions": 0},
        {"dimensions": -4},
        {"config_version": 0},
        {"page_id": 0},
        {"page_id": -3},
    ],
)
def test_upsert_rejects_invalid_identity_fields(
    tmp_path: Path, field: dict
) -> None:
    database, page_id, _ = _library(tmp_path)
    payload = {
        "page_id": page_id,
        "source_text_sha256": HASH_A,
        "model": TS_MODEL,
        "dimensions": 4,
        "config_version": 1,
        "vector": (0.5, -0.25, 1.5, 2.0),
    }
    payload.update(field)
    with pytest.raises(ValueError):
        database.upsert_page_embedding(**payload)
    assert _embedding_count(database) == 0


def test_upsert_rejects_non_finite_vector(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    with pytest.raises(ValueError):
        database.upsert_page_embedding(
            page_id=page_id,
            source_text_sha256=HASH_A,
            model=TS_MODEL,
            dimensions=3,
            config_version=1,
            vector=(0.5, float("nan"), 0.25),
        )
    assert _embedding_count(database) == 0


def test_corrupted_stored_blob_fails_closed(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )
    # 长度合法但格式版本字节未知（绕过 CHECK 的纯长度校验，模拟真实损坏）
    corrupted = sqlite3.Binary(bytes([EMBEDDING_VECTOR_FORMAT_VERSION + 1]) + b"\x00" * 16)
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE page_embeddings SET vector = ?", (corrupted,)
        )

    with pytest.raises(DatabaseError):
        database.get_page_embedding(
            page_id=page_id, model=TS_MODEL, dimensions=4, config_version=1
        )
    with pytest.raises(DatabaseError):
        database.get_fresh_page_embedding(
            page_id=page_id,
            source_text_sha256=HASH_A,
            model=TS_MODEL,
            dimensions=4,
            config_version=1,
        )


# ------------------------------------------------------------------------ cascade
def test_page_delete_cascades_embeddings_via_real_fk(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )
    assert _embedding_count(database) == 1

    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("DELETE FROM pages WHERE id = ?", (page_id,))

    assert _embedding_count(database) == 0


def test_document_deletion_service_cascades_embeddings(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    pages_dir = data_dir / "pages"
    markdown_dir = data_dir / "markdown"
    for directory in (raw_dir, pages_dir, markdown_dir):
        directory.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "database" / "knowledge.db")
    document = database.create_document(
        title="液压手册",
        filename="hyd.pdf",
        source_path=raw_dir / "hyd.pdf",
        sha256="e" * 64,
        page_count=1,
    )
    Path(document.source_path).write_bytes(b"pdf-hyd" * 100)
    image_path = pages_dir / str(document.id) / "page_0001.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 1200), "white").save(image_path)
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="闭环控制原文",
    )
    database.upsert_page_embedding(
        page_id=page.id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )
    service = DocumentDeletionService(
        database=database,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
        data_dir=data_dir,
    )

    result = service.delete_document(document.id, expected_title=document.title)

    assert result.deleted is True
    assert _embedding_count(database) == 0
    with sqlite3.connect(database.database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# ------------------------------------------------------- lexical non-interaction
def test_lexical_search_is_unaffected_by_embeddings(tmp_path: Path) -> None:
    database, page_id, _ = _library(tmp_path)
    before = database.search("闭环控制", terms=("闭环控制",))
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )

    after = database.search("闭环控制", terms=("闭环控制",))

    assert [result.page_id for result in after] == [
        result.page_id for result in before
    ]
    assert page_id in {result.page_id for result in after}
    assert _embedding_count(database) == 1


# ---------------------------------------------------------------- backup/restore
def _backup_service(root: Path) -> BackupService:
    data = root / "data"
    return BackupService(
        app_version="0.5.0",
        data_dir=data,
        raw_dir=data / "raw",
        pages_dir=data / "pages",
        markdown_dir=data / "markdown",
        database_path=data / "database" / "knowledge.db",
        backups_dir=root / "backups",
    )


def test_backup_and_restore_preserve_embedding_records(tmp_path: Path) -> None:
    source = _backup_service(tmp_path / "source")
    for directory in (
        source.raw_dir,
        source.pages_dir,
        source.markdown_dir,
        source.database_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    database = Database(source.database_path)
    pdf_path = source.raw_dir / "hyd.pdf"
    pdf_path.write_bytes(b"pdf-hyd" * 100)
    image_path = source.pages_dir / "page_0001.png"
    Image.new("RGB", (12, 16), "white").save(image_path)
    document = database.create_document(
        title="液压手册",
        filename="hyd.pdf",
        source_path=pdf_path,
        sha256=hashlib.sha256(b"pdf-hyd" * 100).hexdigest(),
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="闭环控制原文",
    )
    stored = database.upsert_page_embedding(
        page_id=page.id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )

    result = source.create_backup()
    assert validate_backup(result.backup_path, expected_app_version="0.5.0").valid

    target = _backup_service(tmp_path / "target")
    for directory in (
        target.raw_dir,
        target.pages_dir,
        target.markdown_dir,
        target.database_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    Database(target.database_path)
    restore = target.restore_backup(
        result.backup_path, service_is_running=lambda: False
    )
    assert restore.database_summary.schema_version == 9

    restored = Database(target.database_path).get_page_embedding(
        page_id=page.id, model=TS_MODEL, dimensions=4, config_version=1
    )
    assert restored is not None
    assert restored == stored
    assert restored.vector == (0.5, -0.25, 1.5, 2.0)


# ------------------------------------------------------------------- offline proof
def test_embedding_persistence_runs_fully_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("embedding persistence 禁止任何网络访问")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    database, page_id, _ = _library(tmp_path)
    database.upsert_page_embedding(
        page_id=page_id,
        source_text_sha256=HASH_A,
        model=TS_MODEL,
        dimensions=4,
        config_version=1,
        vector=(0.5, -0.25, 1.5, 2.0),
    )
    assert (
        database.get_fresh_page_embedding(
            page_id=page_id,
            source_text_sha256=HASH_A,
            model=TS_MODEL,
            dimensions=4,
            config_version=1,
        )
        is not None
    )
