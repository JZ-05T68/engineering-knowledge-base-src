"""v0.0.8 complete-backup validation and guarded-restore tests."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

import pytest

import src.backup_service as backup_module
from src.backup_service import BackupError, BackupService, validate_backup
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.models import PageStatus


def _service(root: Path, *, version: str = "0.0.8") -> BackupService:
    data = root / "data"
    return BackupService(
        app_version=version,
        data_dir=data,
        raw_dir=data / "raw",
        pages_dir=data / "pages",
        markdown_dir=data / "markdown",
        database_path=data / "database" / "knowledge.db",
        backups_dir=root / "backups",
    )


def _library(
    root: Path, *, marker: bytes = b"source library", version: str = "0.0.8"
) -> BackupService:
    service = _service(root, version=version)
    for directory in (
        service.raw_dir,
        service.pages_dir,
        service.markdown_dir,
        service.database_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    pdf = service.raw_dir / "manual.pdf"
    pdf.write_bytes(marker)
    image = service.pages_dir / "1" / "page_0001.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png:" + marker)
    markdown = service.markdown_dir / "1" / "page_0001.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text("# 私有笔记\n\n闭环控制。", encoding="utf-8")
    database = Database(service.database_path)
    document = database.create_document(
        title="控制手册",
        filename="manual.pdf",
        source_path=pdf,
        sha256=hashlib.sha256(marker).hexdigest(),
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image,
        extracted_text="闭环控制原文",
        markdown_content="# 私有笔记\n\n闭环控制。",
        markdown_path=markdown,
        status=PageStatus.REVIEWED,
    )
    database.update_document_page_count(document.id, 1)
    tag = database.create_tag("控制")
    project = database.create_project("小车", "本地项目")
    database.set_document_tags(document.id, [tag.id])
    database.set_page_projects(page.id, [project.id])
    EvidenceBasketService(database).add_item(
        document_id=document.id,
        page_id=page.id,
        evidence_text="闭环控制原文",
        user_note="证据备注",
    )
    return service


def _copy_backup(source: Path, target: Path) -> Path:
    shutil.copytree(source, target)
    return target


def test_empty_database_backup_has_complete_manifest_and_zero_file_counts(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    Database(service.database_path)

    result = service.create_backup()
    validation = validate_backup(
        result.backup_path, expected_app_version="0.0.8"
    )

    assert validation.valid
    assert result.manifest["complete"] is True
    assert result.manifest["statistics"]["documents"] == 0
    assert result.manifest["statistics"]["pages"] == 0
    assert result.manifest["statistics"]["fts"] == 0
    assert result.manifest["directories"] == {
        "raw": {"files": 0, "bytes": 0},
        "pages": {"files": 0, "bytes": 0},
        "markdown": {"files": 0, "bytes": 0},
    }
    assert (result.backup_path / "data" / "raw").is_dir()
    assert (result.backup_path / "data" / "pages").is_dir()
    assert (result.backup_path / "data" / "markdown").is_dir()


def test_normal_backup_captures_database_assets_hashes_and_all_metadata(
    tmp_path: Path,
) -> None:
    service = _library(tmp_path)

    result = service.create_backup()
    validation = validate_backup(
        result.backup_path, expected_app_version="v0.0.8"
    )

    assert validation.valid
    assert validation.database_summary is not None
    assert validation.database_summary.integrity_check == "ok"
    assert validation.database_summary.foreign_key_violations == 0
    assert validation.database_summary.evidence == 1
    assert result.manifest["schema_version"] == 4
    assert result.manifest["statistics"] == {
        "documents": 1,
        "pages": 1,
        "fts": 1,
        "evidence": 1,
        "projects": 1,
        "tags": 1,
    }
    assert result.manifest["directories"]["raw"]["files"] == 1
    assert result.manifest["directories"]["pages"]["files"] == 1
    assert result.manifest["directories"]["markdown"]["files"] == 1
    assert all(len(record["sha256"]) == 64 for record in result.manifest["files"])


def test_backup_excludes_logs_tests_caches_and_database_sidecars(tmp_path: Path) -> None:
    service = _library(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "private.log").write_text("private", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test.db").write_bytes(b"test")
    (tmp_path / "browser-acceptance").mkdir()
    (tmp_path / "browser-acceptance" / "shot.png").write_bytes(b"shot")
    service.database_path.with_name("knowledge.db-wal").write_bytes(b"runtime")

    result = service.create_backup()
    paths = {record["path"] for record in result.manifest["files"]}

    assert not any("logs" in path for path in paths)
    assert not any("tests" in path for path in paths)
    assert not any("browser" in path for path in paths)
    assert not any(path.endswith("-wal") or path.endswith("-shm") for path in paths)


def test_existing_target_and_copy_failure_never_leave_recoverable_partial_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _library(tmp_path)
    existing = service.backups_dir / "fixed"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupError, match="已存在"):
        service.create_backup(backup_name="fixed")
    assert marker.read_text(encoding="utf-8") == "keep"

    original_copy = backup_module._copy_regular_file

    def fail_on_pdf(source: Path, target: Path) -> None:
        if source.suffix == ".pdf":
            raise OSError("simulated copy interruption")
        original_copy(source, target)

    monkeypatch.setattr(backup_module, "_copy_regular_file", fail_on_pdf)
    with pytest.raises(BackupError, match="创建完整备份失败"):
        service.create_backup(backup_name="interrupted")
    assert not (service.backups_dir / "interrupted").exists()
    assert not list(service.backups_dir.glob(".interrupted.incomplete-*"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("remove_manifest", "缺少 manifest"),
        ("bad_json", "有效 JSON"),
        ("missing_file", "关键文件缺失|缺少文件"),
        ("hash_mismatch", "哈希不一致"),
        ("path_traversal", "不安全路径|非法相对路径"),
    ],
)
def test_invalid_or_incomplete_backups_are_rejected(
    tmp_path: Path, mutation: str, message: str
) -> None:
    service = _library(tmp_path / "source")
    good = service.create_backup().backup_path
    damaged = _copy_backup(good, tmp_path / mutation)
    manifest_path = damaged / "manifest.json"
    if mutation == "remove_manifest":
        manifest_path.unlink()
    elif mutation == "bad_json":
        manifest_path.write_text("{broken", encoding="utf-8")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_record = next(record for record in manifest["files"] if record["kind"] == "raw")
        raw_file = damaged / Path(*PurePosixPath(raw_record["path"]).parts)
        if mutation == "missing_file":
            raw_file.unlink()
        elif mutation == "hash_mismatch":
            raw_file.write_bytes(b"x" * raw_file.stat().st_size)
        else:
            raw_record["path"] = "../escape.pdf"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
    validation = validate_backup(damaged, expected_app_version="0.0.8")
    assert not validation.valid
    assert any(re.search(message, error) for error in validation.errors)


def test_backup_root_windows_reparse_point_is_rejected_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _library(tmp_path / "source")
    good = service.create_backup().backup_path
    original_check = backup_module._is_windows_reparse_point

    monkeypatch.setattr(
        backup_module,
        "_is_windows_reparse_point",
        lambda path: path == good or original_check(path),
    )

    validation = validate_backup(good, expected_app_version="0.0.8")

    assert not validation.valid
    assert "重解析点" in validation.errors[0]


def test_backup_service_rejects_link_like_configured_root_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    original_check = backup_module._is_windows_reparse_point
    monkeypatch.setattr(
        backup_module,
        "_is_windows_reparse_point",
        lambda path: path == backups_dir or original_check(path),
    )

    with pytest.raises(BackupError, match="备份目录.*重解析点"):
        _service(tmp_path)


def test_schema_and_application_version_incompatibility_are_rejected(
    tmp_path: Path,
) -> None:
    service = _library(tmp_path / "source")
    good = service.create_backup().backup_path
    incompatible = _copy_backup(good, tmp_path / "incompatible")
    manifest_path = incompatible / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    schema_validation = validate_backup(
        incompatible, expected_app_version="0.0.8"
    )
    version_validation = validate_backup(good, expected_app_version="0.1.0")

    assert not schema_validation.valid
    assert "不兼容" in schema_validation.errors[0]
    assert not version_validation.valid
    assert "应用版本不兼容" in version_validation.errors[0]


def test_patch_upgrade_accepts_older_same_minor_backup_but_rejects_future_patch(
    tmp_path: Path,
) -> None:
    v010_backup = _library(tmp_path / "v010", version="0.1.0").create_backup().backup_path
    v012_backup = _library(tmp_path / "v012", version="0.1.2").create_backup().backup_path

    compatible = validate_backup(v010_backup, expected_app_version="0.1.1")
    future = validate_backup(v012_backup, expected_app_version="0.1.1")

    assert compatible.valid
    assert not future.valid
    assert "应用版本不兼容" in future.errors[0]


def test_patch_upgrade_can_restore_older_same_minor_backup(tmp_path: Path) -> None:
    source = _library(tmp_path / "source", marker=b"v0.1.0", version="0.1.0")
    backup = source.create_backup().backup_path
    target = _library(tmp_path / "target", marker=b"v0.1.1", version="0.1.1")

    result = target.restore_backup(backup, service_is_running=lambda: False)

    assert result.database_summary.schema_version == 4
    assert (target.raw_dir / "manual.pdf").read_bytes() == b"v0.1.0"
    assert result.pre_restore_backup is not None


def test_restore_rebases_paths_preserves_all_counts_and_creates_prebackup(
    tmp_path: Path,
) -> None:
    source = _library(tmp_path / "source", marker=b"source")
    backup = source.create_backup().backup_path
    target = _library(tmp_path / "target", marker=b"old target")

    result = target.restore_backup(backup, service_is_running=lambda: False)

    assert result.pre_restore_backup is not None
    assert validate_backup(
        result.pre_restore_backup, expected_app_version="0.0.8"
    ).valid
    assert result.database_summary.documents == 1
    assert result.database_summary.pages == 1
    assert result.database_summary.fts == 1
    assert result.database_summary.evidence == 1
    assert (target.raw_dir / "manual.pdf").read_bytes() == b"source"
    assert (target.pages_dir / "1" / "page_0001.png").read_bytes() == b"png:source"
    assert (target.markdown_dir / "1" / "page_0001.md").read_text(
        encoding="utf-8"
    ).startswith("# 私有笔记")
    with sqlite3.connect(target.database_path) as connection:
        source_path = Path(connection.execute("SELECT source_path FROM documents").fetchone()[0])
        image_path, markdown_path = connection.execute(
            "SELECT image_path, markdown_path FROM pages"
        ).fetchone()
    assert source_path == target.raw_dir / "manual.pdf"
    assert Path(image_path) == target.pages_dir / "1" / "page_0001.png"
    assert Path(markdown_path) == target.markdown_dir / "1" / "page_0001.md"


def test_restore_refuses_running_service_and_corrupt_backup_without_touching_target(
    tmp_path: Path,
) -> None:
    source = _library(tmp_path / "source", marker=b"source")
    backup = source.create_backup().backup_path
    target = _library(tmp_path / "target", marker=b"old target")
    before = target.database_path.read_bytes()

    with pytest.raises(BackupError, match="仍在运行"):
        target.restore_backup(backup, service_is_running=lambda: True)
    assert target.database_path.read_bytes() == before

    damaged = _copy_backup(backup, tmp_path / "damaged")
    database_record = next(
        record
        for record in json.loads((damaged / "manifest.json").read_text(encoding="utf-8"))[
            "files"
        ]
        if record["kind"] == "database"
    )
    (damaged / Path(*PurePosixPath(database_record["path"]).parts)).write_bytes(b"broken")
    with pytest.raises(BackupError, match="无效备份"):
        target.restore_backup(damaged, service_is_running=lambda: False)
    assert target.database_path.read_bytes() == before


def test_restore_postcheck_failure_rolls_back_original_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _library(tmp_path / "source", marker=b"source")
    backup = source.create_backup().backup_path
    target = _library(tmp_path / "target", marker=b"old target")
    before_database = target.database_path.read_bytes()
    before_pdf = (target.raw_dir / "manual.pdf").read_bytes()

    def fail_postcheck(*args, **kwargs) -> None:
        del args, kwargs
        raise BackupError("simulated post-restore failure")

    monkeypatch.setattr(target, "_verify_restored_assets", fail_postcheck)
    with pytest.raises(BackupError, match="post-restore failure"):
        target.restore_backup(backup, service_is_running=lambda: False)

    assert target.database_path.read_bytes() == before_database
    assert (target.raw_dir / "manual.pdf").read_bytes() == before_pdf
    assert list(target.backups_dir.glob("pre-restore-*"))
