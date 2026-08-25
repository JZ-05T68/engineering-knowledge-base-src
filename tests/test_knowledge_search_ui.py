"""Phase 3D knowledge-scope search UI tests.

Covers the scope switcher, URL state round-trip, knowledge-scope service
dispatch, result-card semantics (type / title / snippet / status / stable_id /
provenance), empty results and Chinese / English / mixed queries. Page-scope
zero-regression is asserted here and by the existing search test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.evidence_basket_service import EvidenceBasketService
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.knowledge_search_service import KnowledgeSearchService
from src.knowledge_search_ui import provenance_labels, status_badge
from src.models import (
    KnowledgeEpistemicBasis,
    KnowledgeSearchResult,
    KnowledgeSearchResultType,
    SearchScope,
)
from src.search_state import (
    SearchPageState,
    parse_search_state,
    search_state_query_params,
)

SEARCH_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("4_*.py")))


def _make_database(tmp_path: Path) -> Database:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    return Database(database_dir / "knowledge.db")


def _seed_library(database: Database, tmp_path: Path) -> dict[str, int]:
    source_path = tmp_path / "data" / "raw" / "manual.pdf"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"pdf")
    image_path = tmp_path / "data" / "pages" / "1" / "page_0001.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), "white").save(image_path)
    document = database.create_document(
        title="液压泵维护手册",
        filename="manual.pdf",
        source_path=source_path,
        sha256="a" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="液压泵异常噪声需要检查吸油管路。",
    )
    database.update_document_page_count(document.id, 1)
    return {"document": document.id, "page": page.id}


def _seed_knowledge(database: Database, ids: dict[str, int]) -> dict[str, int]:
    objects = KnowledgeObjectService(database)
    memories = KnowledgeMemoryService(database)
    chinese = objects.create(
        kind="concept",
        title="汽蚀概念",
        content="泵汽蚀发生在入口压力低于汽化压力时。",
        epistemic_basis=KnowledgeEpistemicBasis.SOURCE_DERIVED,
    )
    objects.link_source(
        chinese.knowledge_object.id,
        source_type="page",
        source_id=ids["page"],
        source_note="关键页",
    )
    memory = memories.create_entry(
        kind="experience",
        title="汽蚀维修经验",
        content="现场处理泵汽蚀时先检查入口过滤器。",
        knowledge_object_id=chinese.knowledge_object.id,
        document_id=ids["document"],
        page_id=ids["page"],
    )
    english = objects.create(
        kind="fact",
        title="Cavitation fact",
        content="cavitation damage appears on impeller surfaces",
        epistemic_basis=KnowledgeEpistemicBasis.DIRECT_OBSERVATION,
    )
    mixed = objects.create(
        kind="principle",
        title="PID 整定经验",
        content="PID 参数整定与 tuning 的工程经验",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE,
    )
    return {
        "object": chinese.knowledge_object.id,
        "memory": memory.id,
        "english": english.knowledge_object.id,
        "mixed": mixed.knowledge_object.id,
    }


class _CoverageStub:
    """Zero-cost coverage summary; the knowledge scope never reads embeddings."""

    def coverage_summary(self):
        return type(
            "Summary",
            (),
            {"indexable": 0, "indexed": 0, "missing": 0, "stale": 0},
        )()


class _RecordingKnowledgeSearchService:
    """Records every knowledge-scope search; returns an empty result set."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int = 20):
        self.calls.append((query, limit))
        return ()


def _ui_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    knowledge_service: object | None = None,
) -> tuple[Database, dict[str, int]]:
    database = _make_database(tmp_path)
    ids = _seed_library(database, tmp_path)
    if knowledge_service is None:
        knowledge_service = KnowledgeSearchService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    monkeypatch.setattr(runtime, "application_coverage_service", lambda: _CoverageStub())
    monkeypatch.setattr(
        runtime, "application_knowledge_search_service", lambda: knowledge_service
    )
    return database, ids


def _markdown_values(app: AppTest) -> list[str]:
    return [item.value for item in app.markdown]


def _caption_values(app: AppTest) -> list[str]:
    return [item.value for item in app.caption]


# --- scope switching and URL state ------------------------------------------


def test_default_open_is_page_scope(tmp_path: Path, monkeypatch) -> None:
    _ui_runtime(tmp_path, monkeypatch)
    app = AppTest.from_file(SEARCH_PAGE).run(timeout=15)

    assert not app.exception
    assert app.radio(key="search_scope_value").value == SearchScope.PAGE.value
    assert "scope" not in app.query_params
    assert app.button(key="apply_search_filters").label == "搜索 / 应用筛选"


def test_scope_knowledge_url_enters_knowledge_ui(
    tmp_path: Path, monkeypatch
) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    _seed_knowledge(database, ids)
    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": "汽蚀"}
    app.run(timeout=15)

    assert not app.exception
    assert app.radio(key="search_scope_value").value == SearchScope.KNOWLEDGE.value
    assert app.query_params["scope"] == [SearchScope.KNOWLEDGE.value]
    assert app.button(key="apply_search_filters").label == "搜索个人知识"
    assert any("个人知识检索结果" in item.value for item in app.subheader)
    assert not any(toggle.key == "search_filters_open" for toggle in app.toggle)


def test_page_scope_search_results_unchanged(tmp_path: Path, monkeypatch) -> None:
    _ui_runtime(tmp_path, monkeypatch)
    app = AppTest.from_file(SEARCH_PAGE).run(timeout=15)
    app.text_input(key="search_query_input").input("液压泵").run()
    app.button(key="apply_search_filters").click().run(timeout=15)

    assert not app.exception
    assert any("液压泵维护手册 · 第 1 页" in value for value in _markdown_values(app))
    assert not any("知识对象 ·" in value for value in _markdown_values(app))
    assert not any("知识记忆 ·" in value for value in _markdown_values(app))
    assert app.radio(key="search_scope_value").value == SearchScope.PAGE.value
    assert "scope" not in app.query_params


def test_knowledge_scope_calls_knowledge_search_service(
    tmp_path: Path, monkeypatch
) -> None:
    fake = _RecordingKnowledgeSearchService()
    _ui_runtime(tmp_path, monkeypatch, knowledge_service=fake)

    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": "液压"}
    app.run(timeout=15)
    assert fake.calls == [("液压", 50)]

    # A page-scope search must never touch the knowledge service.
    page_app = AppTest.from_file(SEARCH_PAGE).run(timeout=15)
    page_app.text_input(key="search_query_input").input("液压").run()
    page_app.button(key="apply_search_filters").click().run(timeout=15)
    assert fake.calls == [("液压", 50)]


# --- result cards ------------------------------------------------------------


def test_knowledge_object_result_display(tmp_path: Path, monkeypatch) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    _seed_knowledge(database, ids)
    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": "汽蚀概念"}
    app.run(timeout=15)

    assert not app.exception
    assert any("知识对象 · 汽蚀概念" in value for value in _markdown_values(app))
    assert any("状态徽标：`ACTIVE` 现行" in value for value in _markdown_values(app))
    assert any("来源锚点：页面 #" in value for value in _caption_values(app))


def test_knowledge_memory_result_display(tmp_path: Path, monkeypatch) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    _seed_knowledge(database, ids)
    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": "现场处理"}
    app.run(timeout=15)

    assert not app.exception
    assert any("知识记忆 · 汽蚀维修经验" in value for value in _markdown_values(app))
    anchors = [value for value in _caption_values(app) if "来源锚点" in value]
    assert any(
        "知识对象 #" in value and "文档 #" in value and "页面 #" in value
        for value in anchors
    )


def _result(**overrides: object) -> KnowledgeSearchResult:
    defaults: dict[str, object] = {
        "result_type": KnowledgeSearchResultType.KNOWLEDGE_OBJECT,
        "id": 1,
        "stable_id": "kb:test:knowledge_object:1",
        "title": "t",
        "content": "c",
        "status": "active",
        "status_label": "现行",
    }
    defaults.update(overrides)
    return KnowledgeSearchResult(**defaults)  # type: ignore[arg-type]


def test_status_badge_covers_lifecycle_values() -> None:
    assert status_badge(_result(status="active", status_label="现行")) == "`ACTIVE` 现行"
    assert (
        status_badge(_result(status="archived", status_label="已归档"))
        == "`ARCHIVED` 已归档"
    )
    assert (
        status_badge(_result(status="superseded", status_label="已替代"))
        == "`SUPERSEDED` 已替代"
    )


def test_provenance_labels_for_ko_and_memory() -> None:
    ko = _result(source_anchors=(("page", 7), ("document", 3)))
    assert provenance_labels(ko) == ("页面 #7", "文档 #3")

    memory = _result(
        result_type=KnowledgeSearchResultType.KNOWLEDGE_MEMORY,
        knowledge_object_id=5,
        document_id=2,
        page_id=9,
    )
    assert provenance_labels(memory) == ("知识对象 #5", "文档 #2", "页面 #9")


def test_stable_id_is_displayed(tmp_path: Path, monkeypatch) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    _seed_knowledge(database, ids)
    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": "汽蚀概念"}
    app.run(timeout=15)

    assert not app.exception
    assert any(
        "稳定标识：" in value and ":knowledge_object:" in value
        for value in _caption_values(app)
    )


# --- URL state restore and legacy compatibility ------------------------------


def test_url_state_restore_and_legacy_defaults() -> None:
    assert parse_search_state({}).scope is SearchScope.PAGE
    assert parse_search_state({"scope": "knowledge", "q": "x"}).scope is (
        SearchScope.KNOWLEDGE
    )
    knowledge_state = SearchPageState(query="x", scope=SearchScope.KNOWLEDGE)
    assert search_state_query_params(knowledge_state)["scope"] == "knowledge"
    assert "scope" not in search_state_query_params(SearchPageState(query="x"))


def test_knowledge_scope_restores_from_url(tmp_path: Path, monkeypatch) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    _seed_knowledge(database, ids)
    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": "汽蚀"}
    app.run(timeout=15)
    assert any("个人知识检索结果" in item.value for item in app.subheader)

    restored = AppTest.from_file(SEARCH_PAGE)
    restored.query_params = dict(app.query_params)
    restored.run(timeout=15)

    assert not restored.exception
    assert restored.radio(key="search_scope_value").value == SearchScope.KNOWLEDGE.value
    assert any("个人知识检索结果" in item.value for item in restored.subheader)


# --- empty results and query language coverage -------------------------------


def test_knowledge_scope_empty_result(tmp_path: Path, monkeypatch) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    _seed_knowledge(database, ids)
    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": "zzzznotfound"}
    app.run(timeout=15)

    assert not app.exception
    assert any("没有找到匹配的个人知识" in item.value for item in app.info)


@pytest.mark.parametrize(
    ("query", "expected_heading"),
    [
        ("汽蚀", "知识对象 · 汽蚀概念"),
        ("cavitation", "知识对象 · Cavitation fact"),
        ("PID 整定", "知识对象 · PID 整定经验"),
    ],
)
def test_knowledge_scope_queries_chinese_english_and_mixed(
    tmp_path: Path, monkeypatch, query: str, expected_heading: str
) -> None:
    database, ids = _ui_runtime(tmp_path, monkeypatch)
    _seed_knowledge(database, ids)
    app = AppTest.from_file(SEARCH_PAGE)
    app.query_params = {"scope": "knowledge", "q": query}
    app.run(timeout=15)

    assert not app.exception
    assert any(expected_heading in value for value in _markdown_values(app))
