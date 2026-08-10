"""Tests for deletion-quarantine manifests and crash reconciliation.

Fixtures build real files and real schema v6 rows under ``tmp_path`` only.
Production data and port 8501 are never touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.deletion_recovery import (
    QUARANTINE_DIR_NAME,
    STATUS_ATTENTION,
    STATUS_COMPLETED,
    STATUS_RESTORED,
    reconcile_quarantine,
    sha256_file,
    write_json_atomic,
)
from src.document_deletion_service import (
    DocumentDeletionError,
    DocumentDeletionService,
)


def _make_env(tmp_path: Path):
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    pages_dir = data_dir / "pages"
    markdown_dir = data_dir / "markdown"
    for directory in (raw_dir, pages_dir, markdown_dir):
        directory.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "database" / "knowledge.db")
    service = DocumentDeletionService(
        database=database,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
        data_dir=data_dir,
        app_version="0.3.2-test",
    )
    return database, service, data_dir, raw_dir, pages_dir, markdown_dir


def _reconcile(database: Database, data_dir: Path, raw_dir, pages_dir, markdown_dir):
    return reconcile_quarantine(
        database=database,
        data_dir=data_dir,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
    )


def _create_document(database, raw_dir, pages_dir, *, title: str, sha_letter: str):
    document = database.create_document(
        title=title,
        filename=f"{title}.pdf",
        source_path=raw_dir / f"{title}.pdf",
        sha256=sha_letter * 64,
        page_count=1,
    )
    Path(document.source_path).write_bytes(f"pdf-{title}".encode() * 100)
    image_path = pages_dir / str(document.id) / "page_0001.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 1200), "white").save(image_path)
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text=f"第 1 页 阀体 回路 {title}",
    )
    database.update_document_page_count(document.id, 1)
    return document, page


def _delete_document_row(database: Database, document_id: int) -> None:
    """Simulate a committed deletion: remove the row with cascades active."""

    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def _forge_operation(
    data_dir: Path,
    document,
    files: list[Path],
    *,
    moved: set[int],
    operation_id: str = "a1",
) -> tuple[Path, dict]:
    """Fabricate a crashed deletion: manifest plus some files already moved."""

    operation_dir = data_dir / QUARANTINE_DIR_NAME / f"op-{operation_id}"
    files_dir = operation_dir / "files"
    files_dir.mkdir(parents=True)
    entries = []
    for index, path in enumerate(files):
        entries.append(
            {
                "original_path": str(path),
                "quarantine_path": str(files_dir / f"{index:04d}-{path.name}"),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "version": 1,
        "operation_id": operation_id,
        "document_id": document.id,
        "document_title": document.title,
        "created_at": "2026-08-10T00:00:00",
        "app_version": "0.3.2-test",
        "phase": "quarantined",
        "files": entries,
    }
    write_json_atomic(operation_dir / "manifest.json", manifest)
    for index in sorted(moved):
        os.replace(
            Path(entries[index]["original_path"]),
            Path(entries[index]["quarantine_path"]),
        )
    return operation_dir, manifest


# --- manifest discipline -------------------------------------------------------


def test_manifest_written_before_first_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    observed: dict[str, object] = {}
    real_replace = os.replace

    def observing_replace(src, dst):
        destination = Path(dst)
        if (
            not observed
            and ".deletion-quarantine" in destination.parts
            and destination.parent.name == "files"
        ):
            manifests = list((data_dir / QUARANTINE_DIR_NAME).rglob("manifest.json"))
            assert len(manifests) == 1, "首个文件移动前 manifest 必须已落盘"
            observed["manifest"] = json.loads(manifests[0].read_text(encoding="utf-8"))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", observing_replace)
    result = service.delete_document(document.id)

    assert result.deleted is True
    manifest = observed["manifest"]
    assert manifest["version"] == 1
    assert manifest["document_id"] == document.id
    assert manifest["document_title"] == "甲"
    assert manifest["app_version"] == "0.3.2-test"
    assert len(manifest["files"]) == 2
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64
        assert Path(entry["quarantine_path"]).parent.name == "files"


def test_write_json_atomic_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    write_json_atomic(target, {"version": 1, "files": []})
    write_json_atomic(target, {"version": 1, "files": [{"x": 1}]})

    assert json.loads(target.read_text(encoding="utf-8"))["files"] == [{"x": 1}]
    assert [path.name for path in tmp_path.iterdir()] == ["manifest.json"]


def test_reconcile_without_quarantine_root_is_silent(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)
    assert report.operations == ()
    assert not report.has_attention


# --- Case 1: document still exists -> restore -----------------------------------


def test_crash_before_any_move_restores_nothing_and_cleans_up(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved=set())

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert len(report.operations) == 1
    assert report.operations[0].status == STATUS_RESTORED
    assert not operation_dir.exists()
    assert Path(document.source_path).is_file()
    assert page.image_path.is_file()


def test_crash_after_first_move_restores_everything(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    fingerprints = {path: sha256_file(path) for path in files}
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0})
    assert not Path(document.source_path).exists()

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_RESTORED
    assert not operation_dir.exists()
    for path, fingerprint in fingerprints.items():
        assert path.is_file()
        assert sha256_file(path) == fingerprint


def test_crash_after_partial_move_restores_everything(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    # A second page gives us three recorded files to move.
    second_image = pages_dir / str(document.id) / "page_0002.png"
    Image.new("RGB", (800, 1200), "white").save(second_image)
    second_page = database.create_page(
        document_id=document.id,
        page_number=2,
        image_path=second_image,
        extracted_text="第 2 页 齿轮",
    )
    files = [Path(document.source_path), page.image_path, second_page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0, 2})

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_RESTORED
    assert not operation_dir.exists()
    for path in files:
        assert path.is_file()


def test_all_quarantined_with_document_present_restores_fully(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0, 1})

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_RESTORED
    assert not operation_dir.exists()
    assert Path(document.source_path).is_file()
    assert page.image_path.is_file()


# --- Case 2: document gone -> finish the committed deletion ----------------------


def test_committed_deletion_destroys_quarantine(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0, 1})
    _delete_document_row(database, document.id)

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_COMPLETED
    assert not operation_dir.exists()


def test_crash_during_quarantine_destruction_is_finished(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, manifest = _forge_operation(
        data_dir, document, files, moved={0, 1}
    )
    _delete_document_row(database, document.id)
    # The destruction died after removing the first quarantined file.
    Path(manifest["files"][0]["quarantine_path"]).unlink()

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_COMPLETED
    assert not operation_dir.exists()


def test_reappeared_original_is_never_touched(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0, 1})
    _delete_document_row(database, document.id)
    # The user (or another tool) put a different file back at the original path.
    Path(document.source_path).write_bytes(b"user-put-this-back")

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_COMPLETED
    assert "未触碰" in report.operations[0].detail
    assert not operation_dir.exists()
    assert Path(document.source_path).read_bytes() == b"user-put-this-back"


# --- fail-closed cases ------------------------------------------------------------


def test_corrupt_manifest_preserves_everything(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0, 1})
    (operation_dir / "manifest.json").write_text("{ 这不是 JSON", encoding="utf-8")

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    operation = report.operations[0]
    assert operation.status == STATUS_ATTENTION
    assert "manifest" in operation.detail
    assert operation_dir.is_dir()
    assert (operation_dir / "files" / "0000-甲.pdf").is_file()


def test_missing_manifest_preserves_everything(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0, 1})
    (operation_dir / "manifest.json").unlink()

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    operation = report.operations[0]
    assert operation.status == STATUS_ATTENTION
    assert operation.document_id is None
    assert operation_dir.is_dir()
    assert (operation_dir / "files" / "0000-甲.pdf").is_file()


def test_legacy_v031_quarantine_dir_is_fail_closed(tmp_path: Path) -> None:
    """v0.3.1-era directories carry no manifest and must never be auto-cleared."""

    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    legacy = data_dir / QUARANTINE_DIR_NAME / "7-20260810-120000-000001"
    legacy.mkdir(parents=True)
    (legacy / "0000-some.pdf").write_bytes(b"orphan")

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_ATTENTION
    assert (legacy / "0000-some.pdf").is_file()


def test_illegal_manifest_path_is_fail_closed(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, manifest = _forge_operation(data_dir, document, files, moved={0, 1})
    manifest["files"][0]["original_path"] = str(tmp_path / "outside.pdf")
    write_json_atomic(operation_dir / "manifest.json", manifest)

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    operation = report.operations[0]
    assert operation.status == STATUS_ATTENTION
    assert "受管数据目录" in operation.detail
    assert (operation_dir / "files" / "0000-甲.pdf").is_file()


def test_same_content_at_both_paths_removes_only_quarantine_copy(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, manifest = _forge_operation(data_dir, document, files, moved={0})
    # A copy with identical content reappeared at the original path.
    original = Path(manifest["files"][0]["original_path"])
    original.write_bytes((operation_dir / "files" / "0000-甲.pdf").read_bytes())

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert report.operations[0].status == STATUS_RESTORED
    assert not operation_dir.exists()
    assert sha256_file(original) == manifest["files"][0]["sha256"]


def test_conflicting_original_is_fail_closed(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, manifest = _forge_operation(data_dir, document, files, moved={0})
    original = Path(manifest["files"][0]["original_path"])
    original.write_bytes(b"different-content")

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    operation = report.operations[0]
    assert operation.status == STATUS_ATTENTION
    assert "未覆盖" in operation.detail
    assert original.read_bytes() == b"different-content"
    assert (operation_dir / "files" / "0000-甲.pdf").is_file()


def test_file_missing_on_both_sides_is_fail_closed(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0})
    (operation_dir / "files" / "0000-甲.pdf").unlink()

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    operation = report.operations[0]
    assert operation.status == STATUS_ATTENTION
    assert "均缺失" in operation.detail
    assert operation_dir.is_dir()


def test_quarantine_hash_mismatch_is_fail_closed(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    operation_dir, _ = _forge_operation(data_dir, document, files, moved={0})
    (operation_dir / "files" / "0000-甲.pdf").write_bytes(b"tampered")

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    operation = report.operations[0]
    assert operation.status == STATUS_ATTENTION
    assert "校验" in operation.detail or "不一致" in operation.detail
    assert (operation_dir / "files" / "0000-甲.pdf").read_bytes() == b"tampered"


# --- idempotency and multi-operation behavior --------------------------------------


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")
    files = [Path(document.source_path), page.image_path]
    _forge_operation(data_dir, document, files, moved={0, 1})

    first = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)
    second = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    assert first.operations[0].status == STATUS_RESTORED
    assert second.operations == ()
    assert Path(document.source_path).is_file()


def test_multiple_unfinished_operations_are_settled_independently(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    first, first_page = _create_document(
        database, raw_dir, pages_dir, title="甲", sha_letter="a"
    )
    second, second_page = _create_document(
        database, raw_dir, pages_dir, title="乙", sha_letter="b"
    )
    _forge_operation(
        data_dir, first, [Path(first.source_path), first_page.image_path],
        moved={0, 1}, operation_id="a1",
    )
    _forge_operation(
        data_dir, second, [Path(second.source_path), second_page.image_path],
        moved={0, 1}, operation_id="b2",
    )
    _delete_document_row(database, second.id)

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    by_id = {operation.operation_id: operation for operation in report.operations}
    assert by_id["op-a1"].status == STATUS_RESTORED
    assert by_id["op-b2"].status == STATUS_COMPLETED
    assert not report.has_attention
    assert Path(first.source_path).is_file()
    assert not Path(second.source_path).exists()


def test_one_broken_operation_does_not_block_a_valid_one(tmp_path: Path) -> None:
    database, _, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    first, first_page = _create_document(
        database, raw_dir, pages_dir, title="甲", sha_letter="a"
    )
    second, second_page = _create_document(
        database, raw_dir, pages_dir, title="乙", sha_letter="b"
    )
    broken_dir, _ = _forge_operation(
        data_dir, first, [Path(first.source_path), first_page.image_path],
        moved={0, 1}, operation_id="a1",
    )
    (broken_dir / "manifest.json").write_text("{ 损坏", encoding="utf-8")
    _forge_operation(
        data_dir, second, [Path(second.source_path), second_page.image_path],
        moved={0, 1}, operation_id="b2",
    )

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)

    by_id = {operation.operation_id: operation for operation in report.operations}
    assert by_id["op-a1"].status == STATUS_ATTENTION
    assert by_id["op-b2"].status == STATUS_RESTORED
    assert report.has_attention
    assert (broken_dir / "files" / "0000-甲.pdf").is_file()
    assert Path(second.source_path).is_file()


def test_aborted_in_process_deletion_leaves_no_operation(tmp_path: Path) -> None:
    """In-process aborts restore immediately; reconciliation finds nothing."""

    database, service, data_dir, raw_dir, pages_dir, markdown_dir = _make_env(tmp_path)
    document, page = _create_document(database, raw_dir, pages_dir, title="甲", sha_letter="a")

    def failing_delete(self, document_id):
        raise DocumentDeletionError("模拟数据库删除失败")

    original = DocumentDeletionService._delete_document_records
    DocumentDeletionService._delete_document_records = failing_delete
    try:
        with pytest.raises(DocumentDeletionError, match="已全部恢复原位"):
            service.delete_document(document.id)
    finally:
        DocumentDeletionService._delete_document_records = original

    report = _reconcile(database, data_dir, raw_dir, pages_dir, markdown_dir)
    assert report.operations == ()
    assert Path(document.source_path).is_file()
    assert page.image_path.is_file()
