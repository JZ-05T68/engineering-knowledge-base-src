"""Tests for the staged, verifiable document deletion service.

Fixtures build real files and real schema v5 rows under ``tmp_path`` only.
Production data and port 8501 are never touched.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from src.database import Database
from src.document_deletion_service import (
    DocumentDeletionError,
    DocumentDeletionService,
)
from src.evidence_basket_service import EvidenceBasketService
from src.models import ImportStatus
from src.note_service import NoteService


def _make_service(
    tmp_path: Path,
) -> tuple[Database, DocumentDeletionService, Path, Path, Path, Path]:
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
    )
    return database, service, data_dir, raw_dir, pages_dir, markdown_dir


def _create_document(
    database: Database,
    raw_dir: Path,
    pages_dir: Path,
    markdown_dir: Path,
    *,
    title: str,
    sha_letter: str,
    page_count: int,
    page_text_token: str = "阀体",
    with_markdown: bool = True,
):
    """Create one document with real PDF/PNG/Markdown files on disk."""

    document = database.create_document(
        title=title,
        filename=f"{title}.pdf",
        source_path=raw_dir / f"{title}.pdf",
        sha256=sha_letter * 64,
        page_count=page_count,
    )
    Path(document.source_path).write_bytes(f"pdf-{title}".encode() * 100)
    pages = []
    for number in range(1, page_count + 1):
        image_path = pages_dir / str(document.id) / f"page_{number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        markdown_path = None
        markdown_content = ""
        if with_markdown:
            markdown_path = markdown_dir / str(document.id) / f"page_{number:04d}.md"
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_content = f"# {title} 第 {number} 页笔记"
            markdown_path.write_text(markdown_content, encoding="utf-8")
        page = database.create_page(
            document_id=document.id,
            page_number=number,
            image_path=image_path,
            extracted_text=f"第 {number} 页 {page_text_token} 回路 {title}",
            markdown_content=markdown_content,
            markdown_path=markdown_path,
        )
        pages.append(page)
    database.update_document_page_count(document.id, page_count)
    return document, pages


def _add_full_notes(note_service: NoteService, document, page) -> None:
    note_service.create_document_note(document.id, "文档级笔记")
    note_service.create_page_note(page.id, "页面级笔记")
    note_service.create_text_selection_note(page.id, "阀体", "选区笔记")
    note_service.create_image_region_note(page.id, 10, 20, 300, 400, "区域笔记")


def _add_associations(database: Database, document, page) -> tuple[int, int]:
    tag = database.create_tag("标签甲")
    project = database.create_project("项目甲")
    database.set_document_tags(document.id, [tag.id])
    database.set_document_projects(document.id, [project.id])
    database.set_page_tags(page.id, [tag.id])
    database.set_page_projects(page.id, [project.id])
    return tag.id, project.id


def _add_import_record(database: Database, document) -> int:
    record = database.create_import_record(document.filename, document.title, document.sha256)
    database.update_import_record(
        record.id,
        status=ImportStatus.COMPLETED,
        document_id=document.id,
        total_pages=document.page_count,
    )
    return record.id


def _count(database: Database, sql: str, parameters: tuple = ()) -> int:
    with sqlite3.connect(database.database_path) as connection:
        return int(connection.execute(sql, parameters).fetchone()[0])


def _residue_counts(database: Database, document_id: int, page_ids: list[int]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in page_ids) or "NULL"
    return {
        "pages": _count(
            database, "SELECT COUNT(*) FROM pages WHERE document_id = ?", (document_id,)
        ),
        "document_notes": _count(
            database, "SELECT COUNT(*) FROM notes WHERE document_id = ?", (document_id,)
        ),
        "page_notes": _count(
            database, f"SELECT COUNT(*) FROM notes WHERE page_id IN ({placeholders})", page_ids
        ),
        "evidence": _count(
            database,
            f"SELECT COUNT(*) FROM evidence_items "
            f"WHERE document_id = ? OR page_id IN ({placeholders})",
            (document_id, *page_ids),
        ),
        "search": _count(
            database,
            f"SELECT COUNT(*) FROM page_search WHERE rowid IN ({placeholders})",
            page_ids,
        ),
        "document_tags": _count(
            database, "SELECT COUNT(*) FROM document_tags WHERE document_id = ?", (document_id,)
        ),
        "page_tags": _count(
            database, f"SELECT COUNT(*) FROM page_tags WHERE page_id IN ({placeholders})", page_ids
        ),
        "project_documents": _count(
            database,
            "SELECT COUNT(*) FROM project_documents WHERE document_id = ?",
            (document_id,),
        ),
        "project_pages": _count(
            database,
            f"SELECT COUNT(*) FROM project_pages WHERE page_id IN ({placeholders})",
            page_ids,
        ),
        "import_refs": _count(
            database, "SELECT COUNT(*) FROM import_records WHERE document_id = ?", (document_id,)
        ),
    }


def _foreign_key_violations(database: Database) -> list:
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        return connection.execute("PRAGMA foreign_key_check").fetchall()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _quarantine_root(data_dir: Path) -> Path:
    return data_dir / ".deletion-quarantine"


def _build_full_library(tmp_path: Path):
    """Two documents; the first carries notes, evidence, tags and a project."""

    database, service, data_dir, raw_dir, pages_dir, markdown_dir = _make_service(tmp_path)
    document, pages = _create_document(
        database, raw_dir, pages_dir, markdown_dir,
        title="甲文档", sha_letter="a", page_count=2,
    )
    note_service = NoteService(database)
    _add_full_notes(note_service, document, pages[0])
    tag_id, project_id = _add_associations(database, document, pages[0])
    basket_service = EvidenceBasketService(database)
    basket_service.add_item(
        document_id=document.id, page_id=pages[0].id, evidence_text="阀体"
    )
    import_record_id = _add_import_record(database, document)
    other, other_pages = _create_document(
        database, raw_dir, pages_dir, markdown_dir,
        title="乙文档", sha_letter="b", page_count=1, page_text_token="齿轮",
    )
    note_service.create_document_note(other.id, "乙文档笔记")
    return {
        "database": database,
        "service": service,
        "data_dir": data_dir,
        "raw_dir": raw_dir,
        "pages_dir": pages_dir,
        "markdown_dir": markdown_dir,
        "document": document,
        "pages": pages,
        "tag_id": tag_id,
        "project_id": project_id,
        "import_record_id": import_record_id,
        "other": other,
        "other_pages": other_pages,
    }


# --- A. preview ---------------------------------------------------------------


def test_preview_missing_document_raises(tmp_path: Path) -> None:
    _, service, *_ = _make_service(tmp_path)
    with pytest.raises(DocumentDeletionError, match="找不到文档"):
        service.preview_document_deletion(9999)


def test_preview_counts_files_and_sizes(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    preview = env["service"].preview_document_deletion(env["document"].id)

    assert preview.document_id == env["document"].id
    assert preview.document_title == "甲文档"
    assert preview.page_count == 2
    assert preview.document_note_count == 1
    assert preview.page_note_count == 1
    assert preview.text_selection_note_count == 1
    assert preview.image_region_note_count == 1
    assert preview.note_count == 4
    assert preview.evidence_item_count == 1
    assert preview.search_record_count == 2
    assert preview.association_count == 4
    assert preview.import_record_count == 1
    assert preview.pdf_file_count == 1
    assert preview.page_image_count == 2
    assert preview.markdown_file_count == 2
    assert len(preview.files) == 5
    expected_size = sum(entry.path.stat().st_size for entry in preview.files)
    assert preview.total_size_bytes == expected_size
    assert preview.missing_files == ()
    assert preview.path_anomalies == ()
    # Only this document's own files are listed, never the other document's.
    other_paths = {
        Path(env["other"].source_path),
        *(page.image_path for page in env["other_pages"]),
    }
    assert other_paths.isdisjoint(entry.path for entry in preview.files)


def test_preview_reports_missing_file(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    missing_png = env["pages"][0].image_path
    missing_png.unlink()

    preview = env["service"].preview_document_deletion(env["document"].id)

    assert preview.missing_files == (missing_png,)
    entry = next(item for item in preview.files if item.path == missing_png)
    assert entry.exists is False
    assert entry.size_bytes is None
    # The missing file contributes nothing to the total size.
    assert preview.total_size_bytes == sum(
        item.size_bytes or 0 for item in preview.files
    )
    assert preview.path_anomalies == ()


# --- B. normal deletion --------------------------------------------------------


def test_no_bare_database_delete_document_api() -> None:
    """Business-level deletion must go through DocumentDeletionService.

    The bare cascading ``Database.delete_document`` API was removed so no
    production code can bypass quarantine, residue checks and confirmation.
    """

    assert not hasattr(Database, "delete_document")


def test_delete_single_page_document(tmp_path: Path) -> None:
    database, service, data_dir, raw_dir, pages_dir, markdown_dir = _make_service(tmp_path)
    document, pages = _create_document(
        database, raw_dir, pages_dir, markdown_dir,
        title="单页文档", sha_letter="c", page_count=1, with_markdown=False,
    )
    import_record_id = _add_import_record(database, document)
    pdf_path = Path(document.source_path)

    result = service.delete_document(document.id, expected_title=document.title)

    assert result.deleted is True
    assert result.document_id == document.id
    assert result.cleanup_warnings == ()
    assert database.get_document(document.id) is None
    assert not pdf_path.exists()
    assert not pages[0].image_path.exists()
    assert not (pages_dir / str(document.id)).exists()
    assert _foreign_key_violations(database) == []
    with sqlite3.connect(database.database_path) as connection:
        row = connection.execute(
            "SELECT document_id FROM import_records WHERE id = ?", (import_record_id,)
        ).fetchone()
    assert row is not None and row[0] is None
    assert not any(_quarantine_root(data_dir).iterdir())


def test_delete_multi_page_document_cascades_everything(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    page_ids = [page.id for page in env["pages"]]
    recorded_files = [Path(document.source_path)]
    recorded_files += [page.image_path for page in env["pages"]]
    recorded_files += [page.markdown_path for page in env["pages"]]
    other_files = [Path(env["other"].source_path)]
    other_files += [page.image_path for page in env["other_pages"]]
    other_files += [page.markdown_path for page in env["other_pages"]]
    other_fingerprints = {path: _sha256(path) for path in other_files}
    assert set(_residue_counts(database, document.id, page_ids).values()) != {0}

    result = env["service"].delete_document(document.id, expected_title=document.title)

    assert result.deleted is True
    assert result.cleanup_warnings == ()
    assert result.preview.page_count == 2
    assert set(_residue_counts(database, document.id, page_ids).values()) == {0}
    assert _foreign_key_violations(database) == []
    for path in recorded_files:
        assert not path.exists(), f"文件未被删除：{path}"
    assert not (env["pages_dir"] / str(document.id)).exists()
    assert not (env["markdown_dir"] / str(document.id)).exists()
    # Tags and projects are shared entities and always survive.
    assert _count(database, "SELECT COUNT(*) FROM tags WHERE id = ?", (env["tag_id"],)) == 1
    assert (
        _count(database, "SELECT COUNT(*) FROM projects WHERE id = ?", (env["project_id"],))
        == 1
    )
    # The import record survives with its document reference nulled.
    with sqlite3.connect(database.database_path) as connection:
        row = connection.execute(
            "SELECT document_id FROM import_records WHERE id = ?",
            (env["import_record_id"],),
        ).fetchone()
    assert row is not None and row[0] is None
    # The other document's rows and file bytes are untouched.
    assert database.get_document(env["other"].id) is not None
    assert len(database.list_pages(env["other"].id)) == 1
    for path, fingerprint in other_fingerprints.items():
        assert path.is_file()
        assert _sha256(path) == fingerprint
    assert not any(_quarantine_root(env["data_dir"]).iterdir())


def test_delete_removes_only_that_documents_agent_readings(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    document = env["document"]
    pages = env["pages"]
    other = env["other"]
    other_pages = env["other_pages"]
    reading_root = env["data_dir"] / "agent-readings"
    documents_root = reading_root / "documents"
    page_root = reading_root / "pages"
    documents_root.mkdir(parents=True)
    page_root.mkdir(parents=True)
    target_paths = [documents_root / f"document_{document.id}.json"] + [
        page_root / f"page_{page.id}.json" for page in pages
    ]
    other_paths = [documents_root / f"document_{other.id}.json"] + [
        page_root / f"page_{page.id}.json" for page in other_pages
    ]
    for path in (*target_paths, *other_paths):
        path.write_text('{"derived": true}\n', encoding="utf-8")

    preview = env["service"].preview_document_deletion(document.id)

    assert set(target_paths) <= {entry.path for entry in preview.files}
    assert set(other_paths).isdisjoint(entry.path for entry in preview.files)
    result = env["service"].delete_document(
        document.id, expected_title=document.title
    )

    assert result.deleted is True
    assert all(not path.exists() for path in target_paths)
    assert all(path.is_file() for path in other_paths)


def test_delete_with_missing_file_still_succeeds(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    missing_png = env["pages"][0].image_path
    missing_png.unlink()

    result = env["service"].delete_document(
        env["document"].id, expected_title=env["document"].title
    )

    assert result.deleted is True
    assert result.preview.missing_files == (missing_png,)
    assert env["database"].get_document(env["document"].id) is None
    assert not Path(env["document"].source_path).exists()


# --- D. fault injection ---------------------------------------------------------


def test_file_move_failure_aborts_without_touching_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    page_ids = [page.id for page in env["pages"]]
    before = _residue_counts(database, document.id, page_ids)

    real_replace = os.replace

    def crashing_replace(src, dst):
        # Manifest writes use os.replace too; only data-file moves fail here.
        if Path(dst).parent.name == "files":
            raise OSError("模拟移动失败")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crashing_replace)
    with pytest.raises(DocumentDeletionError, match="移动文件到隔离目录失败"):
        env["service"].delete_document(document.id, expected_title=document.title)

    assert _residue_counts(database, document.id, page_ids) == before
    assert database.get_document(document.id) is not None
    assert Path(document.source_path).is_file()
    for page in env["pages"]:
        assert page.image_path.is_file()
        assert page.markdown_path.is_file()
    assert not any(_quarantine_root(env["data_dir"]).iterdir())


def test_database_delete_failure_rolls_back_and_restores_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    page_ids = [page.id for page in env["pages"]]
    recorded = [Path(document.source_path)]
    recorded += [page.image_path for page in env["pages"]]
    recorded += [page.markdown_path for page in env["pages"]]
    sizes = {path: path.stat().st_size for path in recorded}

    def failing_delete(self, document_id):
        raise DocumentDeletionError("模拟数据库删除失败")

    monkeypatch.setattr(
        DocumentDeletionService, "_delete_document_records", failing_delete
    )
    with pytest.raises(DocumentDeletionError, match="已全部恢复原位"):
        env["service"].delete_document(document.id, expected_title=document.title)

    assert database.get_document(document.id) is not None
    assert set(_residue_counts(database, document.id, page_ids).values()) != {0}
    for path, size in sizes.items():
        assert path.is_file(), f"文件未恢复：{path}"
        assert path.stat().st_size == size
    assert not any(_quarantine_root(env["data_dir"]).iterdir())


def test_commit_failure_rolls_back_and_restores_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    page_ids = [page.id for page in env["pages"]]
    recorded = [Path(document.source_path)]
    recorded += [page.image_path for page in env["pages"]]
    recorded += [page.markdown_path for page in env["pages"]]
    sizes = {path: path.stat().st_size for path in recorded}

    state = {"delete_executed": False, "commit_failed": False}
    real_connect = sqlite3.connect

    class CommitFailConnection(sqlite3.Connection):
        """Connection whose commit fails exactly once, right after the DELETE."""

        def execute(self, sql, parameters=(), /):
            if isinstance(sql, str) and "DELETE FROM documents" in sql:
                state["delete_executed"] = True
            return super().execute(sql, parameters)

        def commit(self):
            if state["delete_executed"] and not state["commit_failed"]:
                state["commit_failed"] = True
                raise sqlite3.OperationalError("模拟提交失败")
            super().commit()

    def connect_with_factory(*args, **kwargs):
        kwargs["factory"] = CommitFailConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_factory)
    with pytest.raises(DocumentDeletionError, match="已全部恢复原位"):
        env["service"].delete_document(document.id, expected_title=document.title)

    assert database.get_document(document.id) is not None
    assert set(_residue_counts(database, document.id, page_ids).values()) != {0}
    for path, size in sizes.items():
        assert path.is_file(), f"文件未恢复：{path}"
        assert path.stat().st_size == size
    assert not any(_quarantine_root(env["data_dir"]).iterdir())


def test_restore_failure_is_reported_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]

    def failing_delete(self, document_id):
        raise DocumentDeletionError("模拟数据库删除失败")

    real_replace = os.replace

    def flaky_replace(src, dst):
        if ".deletion-quarantine" in Path(dst).parts:
            real_replace(src, dst)
        else:
            raise OSError("模拟恢复失败")

    monkeypatch.setattr(
        DocumentDeletionService, "_delete_document_records", failing_delete
    )
    monkeypatch.setattr(os, "replace", flaky_replace)
    with caplog.at_level("CRITICAL"):
        with pytest.raises(DocumentDeletionError, match="未能恢复原位") as exc_info:
            env["service"].delete_document(document.id, expected_title=document.title)

    message = str(exc_info.value)
    assert "数据库未改动" in message
    assert "隔离目录" in message
    assert database.get_document(document.id) is not None
    quarantine_entries = list(_quarantine_root(env["data_dir"]).rglob("*"))
    quarantine_files = [path for path in quarantine_entries if path.is_file()]
    assert len(quarantine_files) == 6  # 1 PDF + 2 PNG + 2 Markdown + manifest.json
    assert not Path(document.source_path).exists()
    assert any(record.levelname == "CRITICAL" for record in caplog.records)


def test_quarantine_cleanup_failure_reports_warning_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    page_ids = [page.id for page in env["pages"]]

    def failing_rmtree(path, *args, **kwargs):
        raise OSError("模拟清理失败")

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)
    result = env["service"].delete_document(document.id, expected_title=document.title)

    assert result.deleted is True
    assert len(result.cleanup_warnings) == 1
    assert "隔离目录未能清理" in result.cleanup_warnings[0]
    assert database.get_document(document.id) is None
    assert set(_residue_counts(database, document.id, page_ids).values()) == {0}
    quarantine_files = [
        path for path in _quarantine_root(env["data_dir"]).rglob("*") if path.is_file()
    ]
    assert len(quarantine_files) == 6  # 5 个登记文件 + manifest.json


# --- E. path safety ---------------------------------------------------------------


def _corrupt_page_image_path(database: Database, page_id: int, new_path: str) -> None:
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE pages SET image_path = ? WHERE id = ?", (new_path, page_id)
        )


def test_dotdot_path_aborts_deletion(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    outside = env["data_dir"].parent / "secret.png"
    outside.write_bytes(b"secret")
    dodgy = env["pages_dir"] / str(document.id) / ".." / ".." / "secret.png"
    _corrupt_page_image_path(database, env["pages"][0].id, str(dodgy))

    preview = env["service"].preview_document_deletion(document.id)
    assert any(".." in anomaly for anomaly in preview.path_anomalies)
    with pytest.raises(DocumentDeletionError, match="路径异常"):
        env["service"].delete_document(document.id, expected_title=document.title)

    assert outside.read_bytes() == b"secret"
    assert database.get_document(document.id) is not None
    assert Path(document.source_path).is_file()


def test_outside_data_dir_path_aborts_deletion(tmp_path: Path) -> None:
    database, service, data_dir, raw_dir, pages_dir, markdown_dir = _make_service(tmp_path)
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"outside")
    document, pages = _create_document(
        database, raw_dir, pages_dir, markdown_dir,
        title="外部路径", sha_letter="d", page_count=1,
    )
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE documents SET source_path = ? WHERE id = ?",
            (str(outside_pdf), document.id),
        )

    preview = service.preview_document_deletion(document.id)
    assert any("不在其归属目录" in anomaly for anomaly in preview.path_anomalies)
    with pytest.raises(DocumentDeletionError, match="路径异常"):
        service.delete_document(document.id, expected_title=document.title)

    assert outside_pdf.read_bytes() == b"outside"
    assert database.get_document(document.id) is not None
    assert pages[0].image_path.is_file()


def test_data_root_and_directory_paths_abort_deletion(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    _corrupt_page_image_path(database, env["pages"][0].id, str(env["pages_dir"]))

    preview = env["service"].preview_document_deletion(document.id)
    assert any("数据根目录" in anomaly for anomaly in preview.path_anomalies)
    with pytest.raises(DocumentDeletionError, match="路径异常"):
        env["service"].delete_document(document.id, expected_title=document.title)
    assert database.get_document(document.id) is not None

    # A directory recorded where a file is expected is equally refused.
    _corrupt_page_image_path(
        database, env["pages"][0].id, str(env["pages_dir"] / str(document.id))
    )
    preview = env["service"].preview_document_deletion(document.id)
    assert any("不是普通文件" in anomaly for anomaly in preview.path_anomalies)
    with pytest.raises(DocumentDeletionError, match="路径异常"):
        env["service"].delete_document(document.id, expected_title=document.title)
    assert database.get_document(document.id) is not None


def test_symlink_escape_aborts_deletion(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    outside = tmp_path / "outside-target.png"
    outside.write_bytes(b"target")
    link = env["pages_dir"] / str(document.id) / "page_9001.png"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")
    _corrupt_page_image_path(database, env["pages"][0].id, str(link))

    preview = env["service"].preview_document_deletion(document.id)
    assert preview.path_anomalies
    with pytest.raises(DocumentDeletionError, match="路径异常"):
        env["service"].delete_document(document.id, expected_title=document.title)

    assert outside.read_bytes() == b"target"
    assert link.is_symlink()
    assert database.get_document(document.id) is not None


# --- F. expected_title confirmation invariant ---------------------------------


def _create_titled_document(tmp_path: Path):
    """One real document with a Latin-cased title for strict-match tests."""

    database, service, data_dir, raw_dir, pages_dir, markdown_dir = _make_service(tmp_path)
    document, pages = _create_document(
        database, raw_dir, pages_dir, markdown_dir,
        title="Spec-A 规格书", sha_letter="e", page_count=1,
    )
    return database, service, data_dir, document, pages


def test_delete_accepts_exact_expected_title(tmp_path: Path) -> None:
    database, service, _, document, pages = _create_titled_document(tmp_path)

    result = service.delete_document(document.id, expected_title="Spec-A 规格书")

    assert result.deleted is True
    assert database.get_document(document.id) is None
    assert not Path(document.source_path).exists()
    assert not pages[0].image_path.exists()


@pytest.mark.parametrize(
    "expected_title",
    [
        "Spec-B 规格书",
        "spec-a 规格书",
        " Spec-A 规格书",
        "Spec-A 规格书 ",
        "",
    ],
    ids=["错误标题", "仅大小写不同", "前导空格", "尾随空格", "空字符串"],
)
def test_delete_rejects_mismatched_expected_title(
    tmp_path: Path, expected_title: str
) -> None:
    database, service, data_dir, document, pages = _create_titled_document(tmp_path)
    page_ids = [page.id for page in pages]
    recorded = [Path(document.source_path)]
    recorded += [page.image_path for page in pages]
    recorded += [page.markdown_path for page in pages]
    before = _residue_counts(database, document.id, page_ids)

    with pytest.raises(DocumentDeletionError, match="标题确认不匹配"):
        service.delete_document(document.id, expected_title=expected_title)

    # The refusal happens before any side effect: database untouched, every
    # recorded file in place, and no quarantine operation directory created.
    assert database.get_document(document.id) is not None
    assert _residue_counts(database, document.id, page_ids) == before
    for path in recorded:
        assert path.is_file(), f"文件被误动：{path}"
    quarantine_root = _quarantine_root(data_dir)
    assert not quarantine_root.exists() or not any(quarantine_root.iterdir())


def test_delete_requires_expected_title_argument(tmp_path: Path) -> None:
    database, service, _, document, _ = _create_titled_document(tmp_path)

    with pytest.raises(TypeError):
        service.delete_document(document.id)  # type: ignore[call-arg]

    assert database.get_document(document.id) is not None


def test_double_delete_is_refused_without_side_effects(tmp_path: Path) -> None:
    env = _build_full_library(tmp_path)
    database = env["database"]
    service = env["service"]
    document = env["document"]
    other_files = [Path(env["other"].source_path)]
    other_files += [page.image_path for page in env["other_pages"]]
    other_files += [page.markdown_path for page in env["other_pages"]]
    other_fingerprints = {path: _sha256(path) for path in other_files}

    result = service.delete_document(document.id, expected_title=document.title)
    assert result.deleted is True
    import_records_after_first = _count(database, "SELECT COUNT(*) FROM import_records")

    with pytest.raises(DocumentDeletionError, match="找不到文档"):
        service.delete_document(document.id, expected_title=document.title)

    # The repeated call stops at the missing-document check: no new quarantine
    # operation, import records unchanged, other documents' bytes untouched.
    assert not any(_quarantine_root(env["data_dir"]).iterdir())
    assert (
        _count(database, "SELECT COUNT(*) FROM import_records")
        == import_records_after_first
    )
    assert database.get_document(env["other"].id) is not None
    for path, fingerprint in other_fingerprints.items():
        assert path.is_file()
        assert _sha256(path) == fingerprint


def test_delete_uses_execution_time_state(tmp_path: Path) -> None:
    """A stale preview never drives deletion: state is re-read at execution."""

    env = _build_full_library(tmp_path)
    database = env["database"]
    service = env["service"]
    document = env["document"]
    stale_preview = service.preview_document_deletion(document.id)
    assert stale_preview.page_count == 2
    assert stale_preview.page_note_count == 1

    # The underlying state changes after the preview: one more note and one
    # more page (with real files) appear before the deletion executes.
    NoteService(database).create_page_note(env["pages"][0].id, "预览后新增笔记")
    new_image_path = env["pages_dir"] / str(document.id) / "page_0003.png"
    Image.new("RGB", (800, 1200), "white").save(new_image_path)
    new_markdown_path = env["markdown_dir"] / str(document.id) / "page_0003.md"
    new_markdown_path.write_text("# 预览后新增页面", encoding="utf-8")
    new_page = database.create_page(
        document_id=document.id,
        page_number=3,
        image_path=new_image_path,
        extracted_text="第 3 页 预览后新增 阀体",
        markdown_content="# 预览后新增页面",
        markdown_path=new_markdown_path,
    )

    result = service.delete_document(document.id, expected_title=document.title)

    # The result reflects the execution-time state, not the stale preview.
    assert result.deleted is True
    assert result.preview.page_count == 3
    assert result.preview.page_note_count == 2
    page_ids = [page.id for page in env["pages"]] + [new_page.id]
    assert set(_residue_counts(database, document.id, page_ids).values()) == {0}
    assert not new_image_path.exists()
    assert not new_markdown_path.exists()
    assert _foreign_key_violations(database) == []
    assert not any(_quarantine_root(env["data_dir"]).iterdir())


def test_delete_removes_knowledge_object_sources_without_deleting_objects(
    tmp_path: Path,
) -> None:
    """v0.5.2: polymorphic KO sources are cleaned up before the cascading delete."""

    env = _build_full_library(tmp_path)
    database = env["database"]
    document = env["document"]
    page = env["pages"][0]
    note_view = NoteService(database).create_page_note(page.id, "知识来源笔记")
    evidence_item = EvidenceBasketService(database).add_item(
        document_id=document.id, page_id=page.id, evidence_text="第二条证据文本"
    )
    ko = database.create_knowledge_object(kind="fact", title="知识对象", content="内容")
    for source_type, source_id in (
        ("document", document.id),
        ("page", page.id),
        ("note", note_view.note.id),
        ("evidence", evidence_item.id),
    ):
        database.add_knowledge_object_source(
            knowledge_object_id=ko.id, source_type=source_type, source_id=source_id
        )

    preview = env["service"].preview_document_deletion(document.id)
    assert preview.knowledge_object_source_count == 4

    result = env["service"].delete_document(document.id, expected_title=document.title)

    assert result.deleted is True
    assert database.get_document(document.id) is None
    with sqlite3.connect(database.database_path) as connection:
        remaining_sources = connection.execute(
            "SELECT COUNT(*) FROM knowledge_object_sources"
        ).fetchone()[0]
    assert remaining_sources == 0
    # The knowledge object itself survives; only its dangling sources are gone.
    assert database.get_knowledge_object(ko.id) is not None
    assert _foreign_key_violations(database) == []
