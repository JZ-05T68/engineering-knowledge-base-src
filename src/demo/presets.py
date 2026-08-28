"""Frozen competition demo preset questions (v0.6.1).

Three main judge questions (A/B/C), two backup questions and one rehearsal
preset. The competition must not rely on fully random audience questions:
anything outside these cards deterministically falls back to the honest
no-evidence fixture (``empty_result``).

Each preset records the expected narrative, cited sources, viewer state,
warning state and fallback fixture so rehearsal and tests can verify the
demo story end to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.demo.fixtures import (
    MEMORY_ENCODER,
    OBJECT_SERVO,
    PAGE_PID_1,
    PAGE_PID_2,
    PAGE_VFD_1,
)

PresetRole = Literal["main", "backup", "rehearsal"]


@dataclass(frozen=True, slots=True)
class DemoPreset:
    """One frozen demo question card with its expected demo state."""

    preset_id: str
    role: PresetRole
    scenario: Literal["A_grounded", "B_historical", "C_integrity", "empty", "failed_safe"]
    question: str
    expected_fixture: str
    expected_cited_sources: tuple[str, ...]
    expected_warning_state: Literal["none", "generic_limitation"]
    expected_viewer_state: str
    expected_narrative: str


PRESETS: tuple[DemoPreset, ...] = (
    DemoPreset(
        preset_id="A",
        role="main",
        scenario="A_grounded",
        question="PID 调整时，比例项与积分项分别影响什么？",
        expected_fixture="success_grounded_a",
        expected_cited_sources=(PAGE_PID_1, PAGE_PID_2),
        expected_warning_state="none",
        expected_viewer_state=(
            "grounded 答案 + 2 条来源卡片（调试手册第 12/13 页），"
            "来源抽屉显示标题、页码与 anchor 标签"
        ),
        expected_narrative="提问 → Agent 检索页面资料 → grounded 答案 → 逐条来源检查",
    ),
    DemoPreset(
        preset_id="B",
        role="main",
        scenario="B_historical",
        question="编码器接线错误导致 PID 震荡的那次问题，最终是怎么定位和解决的？",
        expected_fixture="partial_warning_b",
        expected_cited_sources=(MEMORY_ENCODER,),
        expected_warning_state="generic_limitation",
        expected_viewer_state=(
            "grounded 答案 + 1 条知识记忆卡片（root cause / 定位 / 处理 / 复用条件），"
            "顶部显示通用来源限制提示"
        ),
        expected_narrative="历史工程经验复用；同时诚实提示该记录的来源存在限制",
    ),
    DemoPreset(
        preset_id="C",
        role="main",
        scenario="C_integrity",
        question="检查知识对象「伺服驱动调试指南」的来源是否仍然可信。",
        expected_fixture="integrity_warning_c",
        expected_cited_sources=(OBJECT_SERVO,),
        expected_warning_state="generic_limitation",
        expected_viewer_state=(
            "知识对象卡片带完整性状态「已变化（changed）」与演示预置说明，"
            "明确该状态是预置演示数据"
        ),
        expected_narrative="来源完整性检查：不把过期来源静默当作可靠事实",
    ),
    DemoPreset(
        preset_id="A2",
        role="backup",
        scenario="A_grounded",
        question="变频器参数设置有哪些注意事项？",
        expected_fixture="success_grounded_a2",
        expected_cited_sources=(PAGE_VFD_1,),
        expected_warning_state="none",
        expected_viewer_state="grounded 答案 + 1 条页面来源卡片（变频器指南第 5 页）",
        expected_narrative="备用主问题：同场景 A，单来源",
    ),
    DemoPreset(
        preset_id="EMPTY",
        role="backup",
        scenario="empty",
        question="知识库里有关于机器学习模型部署的内容吗？",
        expected_fixture="empty_result",
        expected_cited_sources=(),
        expected_warning_state="none",
        expected_viewer_state="无证据诚实空态：明确说明未找到可支持资料，不显示任何引用",
        expected_narrative="超纲问题 → 诚实无证据，不编造答案（信任叙事）",
    ),
    DemoPreset(
        preset_id="REHEARSAL_FAILED",
        role="rehearsal",
        scenario="failed_safe",
        question="（演示排练）触发一次工具失败的安全状态。",
        expected_fixture="failed_safe",
        expected_cited_sources=(),
        expected_warning_state="none",
        expected_viewer_state=(
            "failed 安全态：封闭错误文案「知识库工具执行失败。」"
            "+ 重试/切模式入口"
        ),
        expected_narrative="排练失败链路；不是评委主叙事题目",
    ),
)
