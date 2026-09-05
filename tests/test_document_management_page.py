"""AppTest coverage for the standalone document management page (13_文档管理).

Also pins the browser page (2_浏览资料) to its new role: the deletion
confirmation chain no longer lives there, only a migration hint and a
navigation entry. Fixtures use temporary databases and synthetic files only.
Production data and port 8501 are never touched.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService

PAGES_DIR = Path(__file__).parents[1] / "pages"
MANAGE_PAGE = str(next(PAGES_DIR.glob("11_*.py")))
BROWSE_PAGE = str(next(PAGES_DIR.glob("3_*.py")))


def _create_document(
    database: Database,
    raw_dir: Path,
    pages_dir: Path,
    markdown_dir: Path,
    *,
    title: str,
    sha_letter: str,
    page_count: int,
):
    """Create one document with real PDF/PNG/Markdown files on disk."""

    document = database.create_document(
        title=title,
        filename=f"{title}.pdf",
        source_path=raw_dir / f"{title}.pdf",
        sha256=sha_letter * 64,
        page_count=page_count,
    )
    Path(document.source_path).write_bytes(f"pdf-{title}".encode() * 50)
    for number in range(1, page_count + 1):
        image_path = pages_dir / str(document.id) / f"page_{number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (800, 1200), "white").save(image_path)
        markdown_path = markdown_dir / str(document.id) / f"page_{number:04d}.md"
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(f"# {title} 第 {number} 页笔记", encoding="utf-8")
        database.create_page(
            document_id=document.id,
            page_number=number,
            image_path=image_path,
            extracted_text=f"第 {number} 页 阀体 回路 {title}",
            markdown_content=f"# {title} 第 {number} 页笔记",
            markdown_path=markdown_path,
        )
    database.update_document_page_count(document.id, page_count)
    return document


def _make_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    pages_dir = data_dir / "pages"
    markdown_dir = data_dir / "markdown"
    for directory in (raw_dir, pages_dir, markdown_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return data_dir, raw_dir, pages_dir, markdown_dir


def _build_manage_app(tmp_path: Path, monkeypatch, *, with_documents: bool = True):
    data_dir, raw_dir, pages_dir, markdown_dir = _make_dirs(tmp_path)
    database = Database(data_dir / "database" / "knowledge.db")
    deletion_service = DocumentDeletionService(
        database=database,
        raw_dir=raw_dir,
        pages_dir=pages_dir,
        markdown_dir=markdown_dir,
        data_dir=data_dir,
    )
    document = other = None
    if with_documents:
        document = _create_document(
            database, raw_dir, pages_dir, markdown_dir,
            title="甲文档", sha_letter="a", page_count=2,
        )
        other = _create_document(
            database, raw_dir, pages_dir, markdown_dir,
            title="乙文档", sha_letter="b", page_count=1,
        )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_document_deletion_service", lambda: deletion_service
    )
    app = AppTest.from_file(MANAGE_PAGE).run(timeout=30)
    return app, database, document, other


def _build_browse_app(tmp_path: Path, monkeypatch):
    data_dir, raw_dir, pages_dir, markdown_dir = _make_dirs(tmp_path)
    database = Database(data_dir / "database" / "knowledge.db")
    document_service = DocumentService(database, raw_dir, pages_dir, markdown_dir)
    document = _create_document(
        database, raw_dir, pages_dir, markdown_dir,
        title="甲文档", sha_letter="a", page_count=1,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: document_service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    app = AppTest.from_file(BROWSE_PAGE).run(timeout=30)
    return app, database, document


def _select_document(app: AppTest, document_id: int) -> None:
    selectbox = next(sb for sb in app.selectbox if sb.label == "选择文档")
    selectbox.set_value(document_id).run()


# --- document list -------------------------------------------------------------


def test_document_list_shows_key_fields(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _ = _build_manage_app(tmp_path, monkeypatch)

    assert not app.exception
    table = app.dataframe[0].value
    assert list(table.columns) == ["标题", "原始文件名", "页数", "状态", "导入时间"]
    rows = table.set_index("标题")
    assert rows.loc["甲文档", "原始文件名"] == "甲文档.pdf"
    assert rows.loc["甲文档", "页数"] == 2
    assert rows.loc["甲文档", "状态"] == "导入完成"
    assert rows.loc["乙文档", "页数"] == 1


def test_empty_state_without_documents(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _ = _build_manage_app(tmp_path, monkeypatch, with_documents=False)

    assert not app.exception
    assert any("还没有已导入的文档" in info.value for info in app.info)
    assert not app.selectbox
    # Empty data has no document actions; shared sidebar navigation stays available.
    assert not app.main.button
    assert any(link.label == "首页" for link in app.sidebar.get("page_link"))


# --- target selection ----------------------------------------------------------


def test_selection_binds_the_exact_document(tmp_path: Path, monkeypatch) -> None:
    app, _, document, other = _build_manage_app(tmp_path, monkeypatch)

    _select_document(app, other.id)

    assert app.session_state["doc_manage_selected_document_id"] == other.id
    title_input = app.text_input(key=f"doc_delete_title_{other.id}")
    assert f"请输入文档标题“{other.title}”以确认删除" == title_input.label
    # No confirmation widgets of the unselected document are rendered.
    text_input_keys = [item.key for item in app.text_input]
    assert f"doc_delete_title_{document.id}" not in text_input_keys


def test_wrong_title_keeps_deletion_disabled(tmp_path: Path, monkeypatch) -> None:
    app, database, document, _ = _build_manage_app(tmp_path, monkeypatch)
    _select_document(app, document.id)

    app.checkbox(key=f"doc_delete_confirm_{document.id}").check().run()
    app.text_input(key=f"doc_delete_title_{document.id}").input("错误的标题").run()

    execute = next(
        button
        for button in app.button
        if button.key == f"doc_delete_execute_{document.id}"
    )
    assert execute.disabled
    assert database.get_document(document.id) is not None


# --- full deletion flow --------------------------------------------------------


def test_full_confirmation_chain_deletes_document(tmp_path: Path, monkeypatch) -> None:
    app, database, document, other = _build_manage_app(tmp_path, monkeypatch)
    page_id = database.list_pages(document.id)[0].id
    EvidenceBasketService(database).add_item(
        document_id=document.id, page_id=page_id, evidence_text="阀体"
    )
    _select_document(app, document.id)
    execute_key = f"doc_delete_execute_{document.id}"

    app.checkbox(key=f"doc_delete_confirm_{document.id}").check().run()
    app.text_input(key=f"doc_delete_title_{document.id}").input(document.title).run()
    # Evidence items demand their own independent confirmation.
    assert next(b for b in app.button if b.key == execute_key).disabled
    app.checkbox(key=f"doc_delete_evidence_{document.id}").check().run()
    next(b for b in app.button if b.key == execute_key).click().run()

    assert not app.exception
    assert any(
        "已从当前知识库删除" in s.value and "甲文档" in s.value for s in app.success
    )
    assert database.get_document(document.id) is None
    assert database.get_document(other.id) is not None


def test_list_refreshes_and_selection_is_not_stale_after_deletion(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, document, other = _build_manage_app(tmp_path, monkeypatch)
    _select_document(app, document.id)
    app.checkbox(key=f"doc_delete_confirm_{document.id}").check().run()
    app.text_input(key=f"doc_delete_title_{document.id}").input(document.title).run()
    next(
        b for b in app.button if b.key == f"doc_delete_execute_{document.id}"
    ).click().run()

    assert not app.exception
    # The list refreshes and no deleted identity lingers in the selection.
    titles = app.dataframe[0].value["标题"].tolist()
    assert titles == ["乙文档"]
    selectbox = next(sb for sb in app.selectbox if sb.label == "选择文档")
    assert len(selectbox.options) == 1
    assert "乙文档" in str(selectbox.options[0])
    assert selectbox.value == other.id
    assert app.session_state["doc_manage_selected_document_id"] == other.id
    assert f"doc_delete_confirm_{document.id}" not in app.session_state
    assert f"doc_delete_title_{document.id}" not in app.session_state
    # The surviving document is untouched and still opens its own chain.
    assert database.get_document(other.id) is not None
    assert len(database.list_pages(other.id)) == 1
    assert Path(other.source_path).is_file()


# --- browser page convergence ----------------------------------------------------


def test_browse_page_no_longer_renders_deletion_chain(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, _ = _build_browse_app(tmp_path, monkeypatch)

    assert not app.exception
    text_input_keys = [item.key or "" for item in app.text_input]
    assert not any(key.startswith("doc_delete_title_") for key in text_input_keys)
    button_labels = [button.label for button in app.button]
    assert "永久删除此导入文档及关联数据" not in button_labels
    captions = [caption.value for caption in app.caption]
    assert any("已迁移至「文档管理」" in value for value in captions)
    assert "前往「文档管理」" in button_labels
