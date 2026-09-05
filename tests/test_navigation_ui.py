"""Smoke tests for review continuation entries on the home and browser pages."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.config import Settings
from src.database import Database
from src.document_deletion_service import DocumentDeletionService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService


def _local_runtime(tmp_path: Path, monkeypatch) -> tuple[Database, DocumentService]:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="导航入口测试",
        filename="navigation.pdf",
        source_path=tmp_path / "raw" / "navigation.pdf",
        sha256="5" * 64,
    )
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "pages" / "page_0001.png",
    )
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    settings = Settings(
        data_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
        database_dir=tmp_path / "database",
        database_path=tmp_path / "database" / "knowledge.db",
        logs_dir=tmp_path / "logs",
        log_path=tmp_path / "logs" / "test.log",
        runtime_dir=tmp_path / "runtime",
        pid_path=tmp_path / "runtime" / "test.pid.json",
        _env_file=None,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    monkeypatch.setattr(
        runtime,
        "application_document_deletion_service",
        lambda: DocumentDeletionService(
            database=database,
            raw_dir=tmp_path / "raw",
            pages_dir=tmp_path / "pages",
            markdown_dir=tmp_path / "markdown",
            data_dir=tmp_path,
        ),
    )
    monkeypatch.setattr(runtime, "application_settings", lambda: settings)
    return database, service


def _reader_navigation_runtime(
    tmp_path: Path, monkeypatch
) -> tuple[Database, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir(parents=True)
    pages_dir.mkdir(parents=True)

    document = database.create_document(
        title="八页导航测试",
        filename="eight-pages.pdf",
        source_path=raw_dir / "eight-pages.pdf",
        sha256="7" * 64,
    )
    document.source_path.write_bytes(b"pdf")
    for page_number in range(1, 9):
        image_path = pages_dir / str(document.id) / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), "white").save(image_path)
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=f"PAGE {page_number} TOKEN NAV-{page_number:04d}",
        )
    database.update_document_page_count(document.id, 8)

    other_document = database.create_document(
        title="其他文档",
        filename="other.pdf",
        source_path=raw_dir / "other.pdf",
        sha256="8" * 64,
    )
    other_document.source_path.write_bytes(b"pdf")
    other_image_path = pages_dir / str(other_document.id) / "page_0001.png"
    other_image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), "white").save(other_image_path)
    database.create_page(
        document_id=other_document.id,
        page_number=1,
        image_path=other_image_path,
        extracted_text="OTHER DOCUMENT PAGE 1",
    )
    database.update_document_page_count(other_document.id, 1)

    ordered_list_pages = database.list_pages

    def reversed_list_pages(document_id: int):
        return list(reversed(ordered_list_pages(document_id)))

    monkeypatch.setattr(database, "list_pages", reversed_list_pages)
    service = DocumentService(
        database,
        raw_dir,
        pages_dir,
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    monkeypatch.setattr(
        runtime,
        "application_document_deletion_service",
        lambda: DocumentDeletionService(
            database=database,
            raw_dir=raw_dir,
            pages_dir=pages_dir,
            markdown_dir=tmp_path / "markdown",
            data_dir=tmp_path,
        ),
    )
    return database, document.id


def _reader_page_selector(app: AppTest):
    return next(selectbox for selectbox in app.selectbox if selectbox.label == "页码")


def _reader_button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _has_caption(app: AppTest, text: str) -> bool:
    return any(text in caption.value for caption in app.caption)


def test_empty_home_makes_add_document_the_obvious_first_step(
    tmp_path: Path, monkeypatch,
) -> None:
    """A first-time user sees one action and no dashboard concepts before it."""

    database = Database(tmp_path / "database" / "knowledge.db")
    settings = Settings(
        data_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
        database_dir=tmp_path / "database",
        database_path=tmp_path / "database" / "knowledge.db",
        logs_dir=tmp_path / "logs",
        log_path=tmp_path / "logs" / "test.log",
        runtime_dir=tmp_path / "runtime",
        pid_path=tmp_path / "runtime" / "test.pid.json",
        _env_file=None,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_settings", lambda: settings)
    monkeypatch.setattr(runtime, "application_startup_reconciliation", lambda: None)

    home = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=10)

    assert not home.exception
    visible = "\n".join(item.value for item in home.markdown)
    assert "第 1 步：添加资料" in visible
    assert all(text in visible for text in ("添加资料", "让 Agent 阅读", "开始提问"))
    assert "EKB v0.6.0" in visible
    assert not home.text_input
    assert not home.metric
    assert [button.label for button in home.button] == ["添加资料"]


def test_reader_page_selection_synchronizes_position_and_ordinary_navigation(
    tmp_path: Path, monkeypatch
) -> None:
    _, document_id = _reader_navigation_runtime(tmp_path, monkeypatch)
    reader_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(reader_path))
    app.query_params = {"document": str(document_id)}
    app.run(timeout=10)

    assert not app.exception
    assert _reader_page_selector(app).value == 1
    assert app.query_params["page"] == ["1"]
    assert _has_caption(app, "当前文档记录位置：第 1 / 8 页（PDF 页码 1）")
    assert _reader_button(app, "← 普通上一页").disabled
    assert not _reader_button(app, "普通下一页 →").disabled
    assert _has_caption(app, "普通下一页：第 2 页")

    _reader_page_selector(app).set_value(4).run(timeout=10)

    assert not app.exception
    assert _reader_page_selector(app).value == 4
    assert app.query_params["page"] == ["4"]
    assert _has_caption(app, "当前文档记录位置：第 4 / 8 页（PDF 页码 4）")
    assert not _reader_button(app, "← 普通上一页").disabled
    assert not _reader_button(app, "普通下一页 →").disabled
    assert _has_caption(app, "普通上一页：第 3 页")
    assert _has_caption(app, "普通下一页：第 5 页")

    _reader_button(app, "← 普通上一页").click().run(timeout=10)
    assert _reader_page_selector(app).value == 3
    assert app.query_params["page"] == ["3"]
    assert _has_caption(app, "当前文档记录位置：第 3 / 8 页（PDF 页码 3）")

    _reader_page_selector(app).set_value(4).run(timeout=10)
    _reader_button(app, "普通下一页 →").click().run(timeout=10)
    assert _reader_page_selector(app).value == 5
    assert app.query_params["page"] == ["5"]
    assert _has_caption(app, "当前文档记录位置：第 5 / 8 页（PDF 页码 5）")

    _reader_page_selector(app).set_value(8).run(timeout=10)
    assert _reader_page_selector(app).value == 8
    assert app.query_params["page"] == ["8"]
    assert _has_caption(app, "当前文档记录位置：第 8 / 8 页（PDF 页码 8）")
    assert not _reader_button(app, "← 普通上一页").disabled
    assert _reader_button(app, "普通下一页 →").disabled
    assert _has_caption(app, "普通上一页：第 7 页")
    assert not _has_caption(app, "普通下一页：第 1 页")


def test_reader_query_target_uses_same_sorted_document_navigation(
    tmp_path: Path, monkeypatch
) -> None:
    _, document_id = _reader_navigation_runtime(tmp_path, monkeypatch)
    reader_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(reader_path))
    app.query_params = {"document": str(document_id), "page": "4"}
    app.run(timeout=10)

    assert not app.exception
    assert _reader_page_selector(app).value == 4
    assert app.query_params["page"] == ["4"]
    assert _has_caption(app, "当前文档记录位置：第 4 / 8 页（PDF 页码 4）")
    assert _has_caption(app, "普通上一页：第 3 页")
    assert _has_caption(app, "普通下一页：第 5 页")


def test_home_and_browser_show_review_continuation_entry(tmp_path: Path, monkeypatch) -> None:
    _local_runtime(tmp_path, monkeypatch)
    project_root = Path(__file__).parents[1]

    home = AppTest.from_file(str(project_root / "app.py")).run(timeout=10)
    browser_path = next((project_root / "pages").glob("3_*.py"))
    browser = AppTest.from_file(str(browser_path)).run(timeout=10)

    assert not home.exception
    assert not browser.exception
    assert any(button.label == "查看识别结果" for button in home.button)
    assert any(button.label == "继续处理下一待复核页" for button in browser.button)


def test_home_recent_edit_keeps_document_and_page_on_real_switch(
    tmp_path: Path, monkeypatch,
) -> None:
    """Follow the real multipage switch, so clearing URL parameters cannot be masked."""

    database, _ = _local_runtime(tmp_path, monkeypatch)
    page = database.create_page(
        document_id=1, page_number=2, image_path=tmp_path / "pages" / "page_0002.png",
    )
    database.update_page_markdown(page.id, "导航回归测试", tmp_path / "note.md")
    home = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=10)
    home.button(key=f"recent_page_{page.id}").click().run(timeout=10)

    assert not home.exception
    assert home.query_params["document"] == ["1"]
    assert home.query_params["page"] == ["2"]


def test_home_recent_document_replaces_unrelated_query_parameters(
    tmp_path: Path, monkeypatch,
) -> None:
    """Opening a document must not inherit a previous page or search context."""

    _local_runtime(tmp_path, monkeypatch)
    home = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"))
    home.query_params = {"page": "99", "from_search": "1", "search_query": "旧查询"}
    home.run(timeout=10)
    home.button(key="recent_document_1").click().run(timeout=10)

    assert not home.exception
    assert home.query_params == {"document": ["1"], "page": ["1"]}


def test_home_review_entry_preserves_target_page(tmp_path: Path, monkeypatch) -> None:
    """The review target survives Streamlit's default query-parameter reset."""

    database, _ = _local_runtime(tmp_path, monkeypatch)
    target = database.list_review_pages()[0]
    home = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=10)
    _reader_button(home, "查看识别结果").click().run(timeout=10)

    assert not home.exception
    assert home.query_params == {"page_id": [str(target.id)]}


def test_sidebar_uses_direct_links_instead_of_source_page_rerun_buttons(
    tmp_path: Path, monkeypatch,
) -> None:
    """Navigation must send a destination hash directly, with no button-trigger rerun."""

    _local_runtime(tmp_path, monkeypatch)
    home = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=10)

    assert not home.exception
    assert not home.sidebar.button
    links = {link.label: link.proto for link in home.sidebar.get("page_link")}
    assert len(links) == 9
    assert links["首页"].page == ""
    assert links["首页"].disabled
    for label, route in (
        ("问问 Agent", "知识Agent"), ("添加资料", "导入资料"),
        ("我的资料", "我的资料"), ("查看识别结果", "待整理页面"),
        ("我保存过的内容", "知识记忆"),
    ):
        assert links[label].page == route
        assert links[label].page_script_hash
        assert not links[label].external
        assert not links[label].disabled
    assert len({link.page_script_hash for link in links.values()}) == 9


def test_direct_reader_entry_keeps_native_sidebar_destinations(
    tmp_path: Path, monkeypatch,
) -> None:
    """Opening a subpage directly must still target the real home and import pages."""

    _local_runtime(tmp_path, monkeypatch)
    reader_path = next((Path(__file__).parents[1] / "pages").glob("17_*.py"))
    reader = AppTest.from_file(str(reader_path)).run(timeout=10)

    assert not reader.exception
    links = {link.label: link.proto for link in reader.sidebar.get("page_link")}
    assert links["首页"].page == ""
    assert not links["首页"].disabled
    assert links["我的资料"].disabled
    assert links["添加资料"].page == "导入资料"


def test_simple_reader_exposes_page_correction_without_advanced_jargon(
    tmp_path: Path, monkeypatch,
) -> None:
    _local_runtime(tmp_path, monkeypatch)
    page_path = next((Path(__file__).parents[1] / "pages").glob("17_*.py"))
    page = AppTest.from_file(str(page_path)).run(timeout=10)

    assert not page.exception
    text = "\n".join(item.value for item in (*page.caption, *page.subheader))
    labels = {button.label for button in page.button}
    assert "修改这一页的文字" in text
    assert "保存修改并让 Agent 重读" in labels
    assert "证据篮" not in text
    assert "知识对象" not in text
    control_labels = labels | {item.label for item in page.text_area}
    assert not any("Markdown" in label for label in control_labels)


def test_browser_warns_when_database_note_has_no_markdown_file(
    tmp_path: Path, monkeypatch
) -> None:
    database, _ = _local_runtime(tmp_path, monkeypatch)
    page = database.list_pages(1)[0]
    database.update_page_markdown(
        page.id,
        "# 数据库仍有笔记",
        tmp_path / "markdown" / "1" / "page_0001.md",
    )

    browser_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    browser = AppTest.from_file(str(browser_path)).run(timeout=10)

    assert not browser.exception
    assert any(
        "对应 Markdown 文件缺失或未记录" in warning.value
        for warning in browser.warning
    )
    assert any(metric.label == "导入已处理页" for metric in browser.metric)


def test_browser_disables_markdown_clear_entry_and_keeps_editing_available(
    tmp_path: Path, monkeypatch
) -> None:
    class ClearTrackingDocumentService(DocumentService):
        clear_calls = 0

        def clear_page_markdown(self, document_id: int, page_number: int):
            del document_id, page_number
            self.clear_calls += 1
            raise AssertionError("正常页面交互不得调用 Markdown 清空协议")

    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="Markdown 安全入口测试",
        filename="markdown-safety.pdf",
        source_path=tmp_path / "raw" / "markdown-safety.pdf",
        sha256="6" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "pages" / "page_0001.png",
    )
    markdown_path = tmp_path / "markdown" / str(document.id) / "page_0001.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("# 原有笔记", encoding="utf-8")
    database.update_page_markdown(page.id, "# 原有笔记", markdown_path)
    service = ClearTrackingDocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    monkeypatch.setattr(
        runtime,
        "application_document_deletion_service",
        lambda: DocumentDeletionService(
            database=database,
            raw_dir=tmp_path / "raw",
            pages_dir=tmp_path / "pages",
            markdown_dir=tmp_path / "markdown",
            data_dir=tmp_path,
        ),
    )

    browser_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    browser = AppTest.from_file(str(browser_path)).run(timeout=10)

    assert not browser.exception
    assert "确认清空" not in {checkbox.label for checkbox in browser.checkbox}
    assert "清空笔记" not in {button.label for button in browser.button}
    assert any(element.value == "# 原有笔记" for element in browser.markdown)

    browser.text_area(key=f"markdown_editor_{page.id}_current").input(
        "# 更新后的笔记"
    ).run(timeout=10)
    next(button for button in browser.button if button.label == "保存笔记").click().run(
        timeout=10
    )

    updated_page = database.get_page(page.id)
    assert updated_page is not None
    assert updated_page.markdown_content == "# 更新后的笔记"
    assert markdown_path.read_text(encoding="utf-8") == "# 更新后的笔记"
    assert service.clear_calls == 0
    assert not browser.exception
