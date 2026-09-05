"""v0.7.2 page_visual_search engineering tests."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import src.agent.tools.bootstrap as bootstrap
from src.agent.tools.adapters.page_visual import PageVisualAdapter
from src.agent.tools.contracts import ToolContext, ToolInput
from src.database import Database
from src.search_service import SearchService


@dataclass
class _VisionResult:
    text: str
    model: str = "vision-test"
    usage: object = None
    finish_reason: str = "stop"
    retry_count: int = 0


class _StubVision:
    """Records the prompt; returns a fixed visual fact sheet."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def complete_vision(self, prompt: str, image_png_base64: str, **kwargs):
        self.calls.append((prompt, image_png_base64))
        return _VisionResult(text=self.text)


def _database_with_visual_page(tmp_path: Path, *, image_text: str) -> tuple[Database, Path]:
    database_dir = tmp_path / "db"
    database_dir.mkdir(parents=True, exist_ok=True)
    database = Database(database_dir / "knowledge.db")
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir(exist_ok=True)
    # 1x1 transparent PNG with the marker bytes we can verify are forwarded.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    image_path = pages_dir / "page_0001.png"
    image_path.write_bytes(png)
    document = database.create_document(
        title="视觉测试手册",
        filename="visual.pdf",
        source_path="data/raw/visual.pdf",
        sha256="a" * 64,
        page_count=1,
    )
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image_path,
        extracted_text="图 1：电机额定参数表（数值见图）。",
    )
    return database, pages_dir, png


def test_visual_tool_registers_only_with_vision_capability() -> None:
    defs = bootstrap.phase1_tool_definitions()
    assert all(d.name != "page_visual_search" for d in defs)
    defs_vision = bootstrap.phase1_tool_definitions(include_visual=True)
    assert any(d.name == "page_visual_search" for d in defs_vision)


def test_visual_tool_reads_image_and_returns_page_reference(tmp_path: Path) -> None:
    database, pages_dir, png = _database_with_visual_page(tmp_path, image_text="x")
    stub = _StubVision("表格读数：额定转速 1379 rpm。")
    adapter = PageVisualAdapter(
        SearchService(database),
        kb_uuid=database.get_knowledge_base_uuid(),
        vision_provider=stub,
        pages_dir=pages_dir,
        vision_model="vision-test",
    )
    result = adapter(
        ToolInput(
            tool_name="page_visual_search",
            arguments={"query": "额定 参数表"},
        ),
        ToolContext(run_id="t"),
    )
    assert result.status.value == "success"
    assert result.data["total"] == 1
    item = result.data["results"][0]
    assert "1379" in item["visual_facts"]
    assert item["source"] == "page_image"
    # The real image bytes were forwarded to the vision provider.
    assert base64.b64encode(png).decode("ascii") == stub.calls[0][1]
    # The page reference keeps visual answers traceable.
    assert result.references and "page" in result.references[0].stable_id


def test_visual_tool_confesses_blur_instead_of_fabricating(tmp_path: Path) -> None:
    database, pages_dir, _ = _database_with_visual_page(tmp_path, image_text="x")
    stub = _StubVision("图片模糊，无法可靠读取该页表格数值。")
    adapter = PageVisualAdapter(
        SearchService(database),
        kb_uuid=database.get_knowledge_base_uuid(),
        vision_provider=stub,
        pages_dir=pages_dir,
    )
    result = adapter(
        ToolInput(
            tool_name="page_visual_search",
            arguments={"query": "参数表"},
        ),
        ToolContext(run_id="t"),
    )
    assert result.status.value == "success"
    assert result.data["total"] == 1
    assert "图片模糊" in result.data["results"][0]["visual_facts"]


def test_visual_tool_without_hits_returns_empty(tmp_path: Path) -> None:
    database, pages_dir, _ = _database_with_visual_page(tmp_path, image_text="x")
    stub = _StubVision("无内容")
    adapter = PageVisualAdapter(
        SearchService(database),
        kb_uuid=database.get_knowledge_base_uuid(),
        vision_provider=stub,
        pages_dir=pages_dir,
    )
    result = adapter(
        ToolInput(
            tool_name="page_visual_search",
            arguments={"query": "完全无关的主题"},
        ),
        ToolContext(run_id="t"),
    )
    assert result.status.value == "empty"


def test_decision_prompt_has_visual_rule() -> None:
    from src.agent.decision import prompt as decision_prompt
    from src.agent.tools.adapters.page_search import PAGE_SEARCH_DEFINITION
    from src.agent.tools.adapters.page_visual import (
        PAGE_VISUAL_SEARCH_DEFINITION,
    )

    built = decision_prompt.build_decision_prompt(
        "图表问题", (PAGE_SEARCH_DEFINITION, PAGE_VISUAL_SEARCH_DEFINITION)
    )
    assert "page_visual_search" in built
    assert "禁止编造数值" in built
