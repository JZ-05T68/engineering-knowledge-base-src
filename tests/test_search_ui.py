"""Streamlit workflow tests for v0.0.4 search cards and exact navigation."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_service import DocumentService
from src.models import PageStatus


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _local_search_runtime(
    tmp_path: Path, monkeypatch
) -> tuple[Database, DocumentService, int, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    source_path = tmp_path / "raw" / "工程 手册.pdf"
    image_path = tmp_path / "pages" / "1" / "page_0002.png"
    source_path.parent.mkdir(parents=True)
    image_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"pdf")
    Image.new("RGB", (2, 2), "white").save(image_path)
    document = database.create_document(
        title="液压泵维护手册",
        filename="工程 手册.pdf",
        source_path=source_path,
        sha256="e" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=2,
        image_path=image_path,
        extracted_text="液压泵异常噪声需要检查吸油管路。",
        status=PageStatus.PENDING,
    )
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    return database, service, document.id, page.id


def test_search_cards_filter_clear_evidence_and_navigation_state(
    tmp_path: Path, monkeypatch
) -> None:
    _, _, document_id, page_id = _local_search_runtime(tmp_path, monkeypatch)
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))
    page_path = next((Path(__file__).parents[1] / "pages").glob("3_*.py"))
    app = AppTest.from_file(str(page_path)).run(timeout=10)

    assert not app.exception
    assert {"检索", "清空筛选"} <= {button.label for button in app.button}
    app.text_input(key="search_query_input").input("液压泵").run()
    _button(app, "检索").click().run()

    assert not app.exception
    assert any("液压泵维护手册 · 第 2 页" in item.value for item in app.markdown)
    assert any("命中字段 / 来源：页面提取文本" in item.value for item in app.caption)
    assert {"打开页面", "生成 / 复制证据包"} <= {
        button.label for button in app.button
    }

    _button(app, "生成 / 复制证据包").click().run()
    assert page_id in app.session_state["evidence_packages"]
    assert "document_id=" in app.session_state["evidence_packages"][page_id]
    assert app.code

    app.multiselect(key="search_status_values").select(PageStatus.REVIEWED.value).run()
    _button(app, "检索").click().run()
    assert any("没有符合关键词与筛选条件" in item.value for item in app.info)
    _button(app, "清空筛选").click().run()
    assert app.session_state["search_status_values"] == []
    assert app.session_state["knowledge_results"]

    _button(app, "打开页面").click().run()
    assert switched[-1] == "pages/2_浏览资料.py"
    assert app.query_params["document"] == [str(document_id)]
    assert app.query_params["page"] == ["2"]
    assert app.query_params["search_query"] == ["液压泵"]
    assert app.session_state["search_result_page"] == 1


def test_reader_uses_exact_query_target_and_returns_to_search(
    tmp_path: Path, monkeypatch
) -> None:
    _, _, document_id, _ = _local_search_runtime(tmp_path, monkeypatch)
    switched: list[str] = []
    monkeypatch.setattr(st, "switch_page", lambda page: switched.append(str(page)))
    page_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    app = AppTest.from_file(str(page_path))
    app.query_params = {
        "document": str(document_id),
        "page": "2",
        "from_search": "1",
        "search_query": "异常噪声",
    }
    app.run(timeout=10)

    assert not app.exception
    assert any("当前页面来自检索：异常噪声" in item.value for item in app.info)
    assert any(metric.label == "状态" for metric in app.metric)
    assert "返回检索结果" in {button.label for button in app.button}
    _button(app, "返回检索结果").click().run()

    assert switched[-1] == "pages/3_检索资料.py"
    assert app.query_params == {}


def test_reader_rejects_missing_page_instead_of_falling_back(
    tmp_path: Path, monkeypatch
) -> None:
    _, _, document_id, _ = _local_search_runtime(tmp_path, monkeypatch)
    page_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    app = AppTest.from_file(str(page_path))
    app.query_params = {"document": str(document_id), "page": "999"}
    app.run(timeout=10)

    assert not app.exception
    assert any("不存在第 999 页" in item.value for item in app.error)
    assert not any(metric.label == "状态" for metric in app.metric)
