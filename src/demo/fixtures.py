"""Deterministic competition demo fixtures (v0.6.1, Mode 2).

All content below is synthetic, sanitized demo material written for the
competition narrative. It is NOT the maintainer-approved actual corpus
(v0.6.0 RC-02/03 remain pending) and must never be presented as live model
output: every response carries ``mode="mock_demo"``.

Boundaries:

- no network, no AI provider, no database, no environment reads;
- stable-id vocabulary reuses ``build_stable_id`` and ``ContextItemType``;
- titles/labels are path-free so ``safe_display_text`` keeps them visible;
- warnings mirror the real HTTP projection: the generic limitation message
  only, never raw internal warning text;
- integrity states use the real ``ContextFingerprintState`` vocabulary and
  are preset fixture facts, not live verification results.

Response fixtures are templates with an empty ``request_id``; the mock client
stamps the deterministic request id at call time (``MockDemoClient``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.demo.contracts import DEMO_MODE, DemoAgentRunResponse, DemoCitation, DemoSourceResponse
from src.hosted_api.contracts import public_error
from src.models import ContextFingerprintState, ContextItemType, build_stable_id
from src.source_metadata import safe_display_text

DEMO_KB_UUID = "0e6b1de5-0000-4000-8000-000000000001"

# The real HTTP projection collapses all warnings into this closed message.
GENERIC_WARNING = "来源存在限制，请核对引用资料。"

# Exact no-evidence message of the real Final Answer Stage (fail-closed copy).
NO_EVIDENCE_MESSAGE = "没有在当前知识库中找到可支持该问题的资料。"


def _sid(object_type: str, local_id: int) -> str:
    return build_stable_id(DEMO_KB_UUID, object_type, local_id)


@dataclass(frozen=True, slots=True)
class DemoSourceFixture:
    """One demo source: public metadata plus preset viewer state."""

    stable_id: str
    source_type: ContextItemType
    title: str | None
    label: str | None
    anchor_label: str
    integrity_state: ContextFingerprintState | None = None
    demo_note: str | None = None

    def to_public(self) -> DemoSourceResponse:
        return DemoSourceResponse(
            stable_id=self.stable_id,
            type=self.source_type,
            title=self.title,
            label=self.label,
            integrity_state=self.integrity_state,
            demo_note=self.demo_note,
        )


@dataclass(frozen=True, slots=True)
class DemoResponseFixture:
    """One deterministic mock agent response, keyed for preset lookup."""

    key: str
    response: DemoAgentRunResponse


PAGE_PID_1 = _sid("page", 1)
PAGE_PID_2 = _sid("page", 2)
PAGE_VFD_1 = _sid("page", 3)
MEMORY_ENCODER = _sid("knowledge_memory", 1)
OBJECT_SERVO = _sid("knowledge_object", 1)

SOURCES: tuple[DemoSourceFixture, ...] = (
    DemoSourceFixture(
        stable_id=PAGE_PID_1,
        source_type=ContextItemType.PAGE,
        title="PID 控制器调试手册",
        label="第 12 页",
        anchor_label="PID 控制器调试手册 · 第 12 页",
    ),
    DemoSourceFixture(
        stable_id=PAGE_PID_2,
        source_type=ContextItemType.PAGE,
        title="PID 控制器调试手册",
        label="第 13 页",
        anchor_label="PID 控制器调试手册 · 第 13 页",
    ),
    DemoSourceFixture(
        stable_id=PAGE_VFD_1,
        source_type=ContextItemType.PAGE,
        title="变频器参数设置指南",
        label="第 5 页",
        anchor_label="变频器参数设置指南 · 第 5 页",
    ),
    DemoSourceFixture(
        stable_id=MEMORY_ENCODER,
        source_type=ContextItemType.KNOWLEDGE_MEMORY,
        title="编码器接线错误导致 PID 震荡的问题解决记录",
        label="问题解决",
        anchor_label="编码器接线错误导致 PID 震荡的问题解决记录",
    ),
    DemoSourceFixture(
        stable_id=OBJECT_SERVO,
        source_type=ContextItemType.KNOWLEDGE_OBJECT,
        title="伺服驱动调试指南",
        label="原理",
        anchor_label="完整性检查目标",
        integrity_state=ContextFingerprintState.CHANGED,
        demo_note=(
            "演示预置状态：该对象登记的 2 个来源中有 1 个在指纹捕获后发生了变化"
            "（changed）。该状态来自演示数据，不是实时核验结果。"
        ),
    ),
)

FIXTURE_KEYS = (
    "success_grounded_a",
    "success_grounded_a2",
    "partial_warning_b",
    "integrity_warning_c",
    "empty_result",
    "failed_safe",
)


def _fixture(
    key: str,
    *,
    answer: str,
    citation_ids: tuple[str, ...],
    grounded: bool,
    warnings: tuple[str, ...] = (),
    status: Literal["completed", "failed"] = "completed",
    error_code: str | None = None,
) -> DemoResponseFixture:
    """Build one fixture; construction validates through the real DTO fields."""

    sources = {source.stable_id: source for source in SOURCES}
    details = tuple(
        DemoCitation(
            display_index=index,
            stable_id=stable_id,
            anchor_label=sources[stable_id].anchor_label,
            source_type=sources[stable_id].source_type,
            title=safe_display_text(sources[stable_id].title),
            label=safe_display_text(sources[stable_id].label),
        )
        for index, stable_id in enumerate(citation_ids, start=1)
    )
    response = DemoAgentRunResponse(
        request_id="",
        status=status,
        answer=answer,
        grounded=grounded,
        citations=citation_ids,
        warnings=warnings,
        error=public_error(error_code) if error_code is not None else None,
        mode=DEMO_MODE,
        citations_detail=details,
    )
    return DemoResponseFixture(key=key, response=response)


RESPONSES: tuple[DemoResponseFixture, ...] = (
    _fixture(
        "success_grounded_a",
        answer=(
            "比例项（P）决定系统对当前偏差的响应强度：增益偏大会引起震荡，"
            "偏小则响应迟缓【来源 #1】。积分项（I）用于消除稳态误差，"
            "但积分作用过强会带来超调和积分饱和，需要与抗积分饱和措施配合整定"
            "【来源 #2】。\n\n以上结论仅基于当前知识库引用的页面内容；"
            "请通过来源卡片核对原始页面。"
        ),
        citation_ids=(PAGE_PID_1, PAGE_PID_2),
        grounded=True,
    ),
    _fixture(
        "success_grounded_a2",
        answer=(
            "知识库中的变频器参数设置指南指出：修改参数前应记录原始值，"
            "逐项调整并在调整后进行试运行验证；不同品牌变频器的参数编号与含义"
            "不同，不能凭记忆直接套用【来源 #1】。\n\n请通过来源卡片核对该页原文。"
        ),
        citation_ids=(PAGE_VFD_1,),
        grounded=True,
    ),
    _fixture(
        "partial_warning_b",
        answer=(
            "根据知识记忆中的问题解决记录：该次 PID 震荡的根因是伺服编码器接线"
            "错误导致速度反馈异常；定位方式是分段排查反馈回路，并在驱动器侧对比"
            "实际转速与指令转速；处理方式是重新按接线图压接编码器电缆并更换受损"
            "屏蔽层，处理后震荡消失。该结论仅在同样的接线规范下可复用【来源 #1】。"
        ),
        citation_ids=(MEMORY_ENCODER,),
        grounded=True,
        warnings=(GENERIC_WARNING,),
    ),
    _fixture(
        "integrity_warning_c",
        answer=(
            "该知识对象目前登记 2 个来源，其中 1 个来源的完整性状态为"
            "「已变化（changed）」：来源内容在该对象捕获指纹之后发生了变化。"
            "EKB 不会把过期来源静默当作可靠事实，因此该对象的内容应结合最新"
            "原文重新核对后再采信【来源 #1】。"
        ),
        citation_ids=(OBJECT_SERVO,),
        grounded=True,
        warnings=(GENERIC_WARNING,),
    ),
    _fixture(
        "empty_result",
        answer=NO_EVIDENCE_MESSAGE,
        citation_ids=(),
        grounded=False,
    ),
    _fixture(
        "failed_safe",
        answer="",
        citation_ids=(),
        grounded=False,
        status="failed",
        error_code="tool_failed",
    ),
)
