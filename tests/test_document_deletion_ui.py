"""UI tests for the document deletion expander on the browser page (AppTest).

Fixtures use temporary databases and synthetic files only. Production data
and port 8501 are never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import (
    DocumentDeletionError,
    DocumentDeletionService,
)
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.note_service import NoteService
from src.search_service import SearchService

BROWSE_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("2_*.py")))


def _create_document(
    database: Database,
    raw_dir: Path,
    pages_dir: Path,
    markdown_dir: Path,
    *,
    title: str,
    sha_letter: str,
    page_count: int,
    page_text_token: str,
):
    document = database.create_document(
        title=title,
        filename=f"{title}.pdf",
        source_path=raw_dir / f"{title}.pdf",
        sha256=sha_letter * 64,
        page_count=page_count,
    )
    Path(document.source_path).write_bytes(f"pdf-{title}".encode() * 50)
    pages = []
    for number in range(1, page_count + 1):
        image_path = pages_dir / str(document.id) / f"page_{number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        markdown_path = markdown_dir / str(document.id) / f"page_{number:04d}.md"
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_content = f"# {title} 第 {number} 页笔记"
        markdown_path.write_text(markdown_content, encoding="utf-8")
        pages.append(
            database.create_page(
                document_id=document.id,
                page_number=number,
                image_path=image_path,
                extracted_text=f"第 {number} 页 {page_text_token} 回路 {title}",
                markdown_content=markdown_content,
                markdown_path=markdown_path,
            )
        )
    database.update_document_page_count(document.id, page_count)
    return document, pages


def _build_app(tmp_path: Path, monkeypatch, *, document_count: int = 2):
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    pages_dir = data_dir / "pages"
    markdown_dir = data_dir / "markdown"
    for directory in (raw_dir, pages_dir, markdown_dir):
        directory.mkdir(parents=True, exist_ok=True)
    database = Database(data_dir / "database" / "knowledge.db")
    deletion_service = DocumentDeletionService(
        database=database,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
        data_dir=data_dir,
    )
    document_service = DocumentService(database, raw_dir, pages_dir, markdown_dir)

    document, pages = _create_document(
        database, raw_dir, pages_dir, markdown_dir,
        title="甲文档", sha_letter="a", page_count=2, page_text_token="阀体",
    )
    note_service = NoteService(database)
    note_service.create_document_note(document.id, "甲文档笔记")
    note_service.create_page_note(pages[0].id, "甲页面笔记")
    note_service.create_text_selection_note(pages[0].id, "阀体", "甲选区笔记")
    note_service.create_image_region_note(pages[0].id, 10, 20, 300, 400, "甲区域笔记")
    tag = database.create_tag("标签甲")
    project = database.create_project("项目甲")
    database.set_document_tags(document.id, [tag.id])
    database.set_document_projects(document.id, [project.id])
    database.set_page_tags(pages[0].id, [tag.id])
    database.set_page_projects(pages[0].id, [project.id])
    record = database.create_import_record(document.filename, document.title, document.sha256)
    database.update_import_record(record.id, status="completed", document_id=document.id)

    other = None
    if document_count == 2:
        other, _ = _create_document(
            database, raw_dir, pages_dir, markdown_dir,
            title="乙文档", sha_letter="b", page_count=1, page_text_token="齿轮",
        )

    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: document_service)
    monkeypatch.setattr(
        runtime, "application_document_deletion_service", lambda: deletion_service
    )
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    app = AppTest.from_file(BROWSE_PAGE).run(timeout=30)
    return app, database, deletion_service, document, other, data_dir


def _button(app: AppTest, key: str):
    matches = [button for button in app.button if button.key == key]
    assert matches, f"找不到按钮 {key}"
    return matches[0]


def _select_document(app: AppTest, document_id: int) -> None:
    selectbox = next(sb for sb in app.selectbox if sb.label == "选择文档")
    selectbox.set_value(document_id).run()


def _confirm_deletion(app: AppTest, document) -> None:
    app.checkbox(key=f"doc_delete_confirm_{document.id}").check().run()
    app.text_input(key=f"doc_delete_title_{document.id}").input(document.title).run()


def _captions(app: AppTest) -> list[str]:
    return [caption.value for caption in app.caption]


# --- preview display ---------------------------------------------------------


def test_preview_counts_and_warning_displayed(tmp_path: Path, monkeypatch) -> None:
    app, _, _, document, _, _ = _build_app(tmp_path, monkeypatch)
    assert not app.exception
    _select_document(app, document.id)

    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["页面"] == "2"
    assert metrics["结构化笔记"] == "4"
    assert metrics["证据项"] == "0"
    assert metrics["搜索记录"] == "2"
    captions = _captions(app)
    assert any(
        "文档级 1 条" in value and "图片区域 1 条" in value for value in captions
    )
    assert any("标签与项目关联 4 条" in value for value in captions)
    assert any("导入记录 1 条" in value for value in captions)
    assert any(
        "PDF 1 个" in value and "页面图片 2 个" in value and "Markdown 2 个" in value
        for value in captions
    )
    assert any("此操作不可撤销" in warning.value for warning in app.warning)


def test_missing_file_and_anomaly_are_displayed(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document, _, _ = _build_app(tmp_path, monkeypatch)
    missing_png = database.list_pages(document.id)[0].image_path
    missing_png.unlink()
    app.run(timeout=30)
    _select_document(app, document.id)
    assert any(
        "缺失" in warning.value and str(missing_png) in warning.value
        for warning in app.warning
    )


# --- two-step confirmation -----------------------------------------------------


def test_execute_button_requires_checkbox_and_title(tmp_path: Path, monkeypatch) -> None:
    app, _, _, document, _, _ = _build_app(tmp_path, monkeypatch)
    _select_document(app, document.id)
    key = f"doc_delete_execute_{document.id}"

    assert _button(app, key).disabled
    app.checkbox(key=f"doc_delete_confirm_{document.id}").check().run()
    assert _button(app, key).disabled
    app.text_input(key=f"doc_delete_title_{document.id}").input("错误的标题").run()
    assert _button(app, key).disabled
    app.text_input(key=f"doc_delete_title_{document.id}").input(document.title).run()
    assert not _button(app, key).disabled


# --- execution -------------------------------------------------------------------


def test_successful_deletion_updates_page_and_cleans_state(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, _, document, other, _ = _build_app(tmp_path, monkeypatch)
    _select_document(app, document.id)
    _confirm_deletion(app, document)
    _button(app, f"doc_delete_execute_{document.id}").click().run()

    assert not app.exception
    assert any("已永久删除导入文件“甲文档”" in success.value for success in app.success)
    assert database.get_document(document.id) is None
    selectbox = next(sb for sb in app.selectbox if sb.label == "选择文档")
    option_labels = [str(option) for option in selectbox.options]
    assert not any("甲文档" in label for label in option_labels)
    assert any("乙文档" in label for label in option_labels)
    assert f"doc_delete_confirm_{document.id}" not in app.session_state
    assert f"doc_delete_title_{document.id}" not in app.session_state
    assert app.query_params.get("document") == [str(other.id)]
    # Deleted notes no longer appear in the structured-notes list.
    note_service = NoteService(database)
    assert all(
        item.document_id != document.id
        for item in note_service.list_note_summaries(limit=100)
    )
    # Search no longer returns the deleted document's pages.
    results = SearchService(database).search("阀体")
    assert all(result.document_id != document.id for result in results)
    # The other document still opens normally in a fresh session. (AppTest
    # cannot re-serialize the keyless document selectbox after its options
    # changed mid-session, so a fresh run stands in for the next visit.)
    fresh = AppTest.from_file(BROWSE_PAGE).run(timeout=30)
    assert not fresh.exception
    assert any(markdown.value == "### 乙文档" for markdown in fresh.markdown)


def test_deleting_last_document_falls_back_to_empty_state(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, _, document, _, _ = _build_app(
        tmp_path, monkeypatch, document_count=1
    )
    _confirm_deletion(app, document)
    _button(app, f"doc_delete_execute_{document.id}").click().run()

    assert not app.exception
    assert any("已永久删除" in success.value for success in app.success)
    assert database.get_document(document.id) is None
    assert any("还没有可浏览的文档" in info.value for info in app.info)
    assert len(app.query_params) == 0


def test_failed_deletion_shows_error_without_fake_success(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, deletion_service, document, _, _ = _build_app(tmp_path, monkeypatch)

    def failing_delete(document_id):
        raise DocumentDeletionError("模拟删除失败")

    monkeypatch.setattr(deletion_service, "delete_document", failing_delete)
    _select_document(app, document.id)
    _confirm_deletion(app, document)
    _button(app, f"doc_delete_execute_{document.id}").click().run()

    assert any(
        "删除失败" in error.value and "模拟删除失败" in error.value
        for error in app.error
    )
    assert not any("已永久删除" in success.value for success in app.success)
    assert database.get_document(document.id) is not None


def test_cleanup_warnings_are_displayed(tmp_path: Path, monkeypatch) -> None:
    app, database, _, document, _, data_dir = _build_app(tmp_path, monkeypatch)

    def failing_rmtree(path, *args, **kwargs):
        raise OSError("模拟清理失败")

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)
    _select_document(app, document.id)
    _confirm_deletion(app, document)
    _button(app, f"doc_delete_execute_{document.id}").click().run()

    assert not app.exception
    assert any("已永久删除" in success.value for success in app.success)
    assert any("隔离目录未能清理" in warning.value for warning in app.warning)
    assert database.get_document(document.id) is None
    quarantine_files = [
        path
        for path in (data_dir / ".deletion-quarantine").rglob("*")
        if path.is_file()
    ]
    assert quarantine_files


# --- entry-point discipline ------------------------------------------------------


def test_no_batch_deletion_entry(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _, _, _ = _build_app(tmp_path, monkeypatch)
    assert not app.exception
    button_labels = [button.label for button in app.button]
    assert not any("全部删除" in label for label in button_labels)
    expander_labels = [expander.label for expander in app.expander]
    assert expander_labels.count("删除导入文件") == 1
    assert "危险操作：删除文档" not in expander_labels
