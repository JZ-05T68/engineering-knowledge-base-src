"""``page_visual_search`` read-only Tool Adapter (v0.7.2 Visual Understanding).

One bounded step: locate pages with the existing lexical search, then read
the top page image with the vision model and return the visually-derived
facts together with the page reference, so every visual answer stays
traceable to the original page.

Boundaries:

- the vision prompt demands verbatim chart/table reading and explicit
  "看不清" confessions; fabrication is refused at the prompt level and the
  raw model text is returned unedited;
- at most ``limit`` (1..2) pages are read per call — bounded paid work;
- without a vision-capable provider the tool is simply not registered.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from src.agent.tools.adapters._common import (
    AdapterInputError,
    empty_result,
    failed_result,
    internal_failure_result,
    optional_int,
    reject_unknown_arguments,
    require_text,
    success_result,
)
from src.agent.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolInput,
    ToolReference,
    ToolResult,
    ToolSideEffect,
)
from src.models import PAGE_STABLE_TYPE, build_stable_id
from src.search_service import SearchService

LOGGER = logging.getLogger(__name__)

MAX_LIMIT = 2
DEFAULT_LIMIT = 1
MAX_QUERY_LENGTH = 500

ALLOWED_ARGUMENTS = frozenset({"query", "limit"})

_VISION_PROMPT = (
    "你在帮用户读取一页资料图片中的视觉信息（表格、柱状图、折线图、饼图、"
    "散点图、流程图、框图或页面上的关键视觉事实）。\n"
    "要求：\n"
    "1. 只描述图片中真实可见的内容；所有数值必须来自图中，禁止编造。\n"
    "2. 如果图片模糊、太小或某处无法辨认，必须明确说明"
    "“图片模糊，无法可靠读取该处”，禁止猜测。\n"
    "3. 表格逐行转写关键行；图表说明坐标轴含义并读出关键数据点。\n"
    "4. 用简体中文要点输出。\n"
)

PAGE_VISUAL_SEARCH_DEFINITION = ToolDefinition(
    name="page_visual_search",
    description=(
        "读取资料页面图片中的视觉信息：表格数值、柱状图/折线图/饼图数据点、"
        "流程图与框图结构。用于答案在图里而不在正文里的问题。"
        "图片模糊时会如实说明看不清，不会编造数值。"
    ),
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "query": {
            "type": "string",
            "required": True,
            "description": "与目标图表/表格相关的关键词，原样保留名词和数字",
        },
        "limit": {
            "type": "integer",
            "default": DEFAULT_LIMIT,
            "min": 1,
            "max": MAX_LIMIT,
            "description": "最多读取几页图片",
        },
    },
    timeout_seconds=60.0,
)


class PageVisualAdapter:
    """Locate pages lexically, then read the page image with vision."""

    tool_name = "page_visual_search"

    def __init__(
        self,
        search_service: SearchService,
        *,
        kb_uuid: str,
        vision_provider: object,
        pages_dir: Path,
        vision_model: str | None = None,
    ) -> None:
        self._service = search_service
        self._kb_uuid = kb_uuid
        self._provider = vision_provider
        self._pages_dir = Path(pages_dir)
        self._vision_model = vision_model

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        try:
            reject_unknown_arguments(tool_input.arguments, ALLOWED_ARGUMENTS)
            query = require_text(
                tool_input.arguments, "query", max_length=MAX_QUERY_LENGTH
            )
            limit = optional_int(
                tool_input.arguments,
                "limit",
                default=DEFAULT_LIMIT,
                min_value=1,
                max_value=MAX_LIMIT,
            )
        except AdapterInputError as exc:
            return failed_result(
                self.tool_name, ToolErrorCode.INVALID_INPUT, exc.message
            )
        try:
            hits = self._service.search(query, limit=limit)
        except Exception as exc:
            return internal_failure_result(
                self.tool_name, exc, safe_message="页面检索执行失败"
            )
        if not hits:
            return empty_result(
                self.tool_name,
                data={"query": query, "limit": limit, "total": 0, "results": []},
            )
        results: list[dict[str, object]] = []
        references: list[ToolReference] = []
        failures: list[str] = []
        for hit in hits:
            try:
                visual_text = self._read_page_image(hit.image_path)
            except Exception:
                failures.append(f"第 {hit.page_number} 页图片读取失败")
                LOGGER.warning(
                    "视觉读取失败：page_id=%s", hit.page_id, exc_info=True
                )
                continue
            if not visual_text.strip():
                failures.append(f"第 {hit.page_number} 页没有可辨认的视觉内容")
                continue
            results.append(
                {
                    "page_id": hit.page_id,
                    "document_id": hit.document_id,
                    "document_title": hit.document_title,
                    "page_number": hit.page_number,
                    # "content" is the projection key the Final Answer mapper
                    # reads; "visual_facts" keeps the explicit meaning.
                    "content": visual_text.strip(),
                    "visual_facts": visual_text.strip(),
                    "source": "page_image",
                }
            )
            references.append(
                ToolReference(
                    stable_id=build_stable_id(
                        self._kb_uuid, PAGE_STABLE_TYPE, hit.page_id
                    ),
                    anchor_label=f"第 {hit.page_number} 页（图片读取）",
                )
            )
        if not results:
            if failures:
                return success_result(
                    self.tool_name,
                    {
                        "query": query,
                        "total": 0,
                        "results": [],
                        "notes": failures,
                        "note": (
                            "图片内容无法可靠读取；如需回答请提供更清晰的资料。"
                        ),
                    },
                    warnings=tuple(failures),
                )
            return empty_result(
                self.tool_name,
                data={"query": query, "limit": limit, "total": 0, "results": []},
            )
        data: dict[str, object] = {
            "query": query,
            "limit": limit,
            "total": len(results),
            "results": results,
        }
        if failures:
            data["notes"] = failures
        return success_result(self.tool_name, data, references=tuple(references))

    def _read_page_image(self, image_path: Path | str) -> str:
        path = Path(image_path)
        if not path.exists():
            candidate = self._pages_dir / path
            if candidate.exists():
                path = candidate
        png_bytes = path.read_bytes()
        encoded = base64.b64encode(png_bytes).decode("ascii")
        wrapper = getattr(self._provider, "complete_vision", None)
        if wrapper is None:
            from src.ai.provider import AIUnavailableError

            raise AIUnavailableError("当前 AI 提供方不支持视觉读取")
        result = wrapper(
            _VISION_PROMPT,
            encoded,
            model=self._vision_model,
            source_feature="page_visual_search",
        )
        return result.text


__all__ = ["PAGE_VISUAL_SEARCH_DEFINITION", "PageVisualAdapter"]
