"""Read-only "AI 整理经验" section for the knowledge-memory page (Phase 4).

The user first selects explicit knowledge context, then one click generates a
structured experience candidate through the audited experience-model chain.
The result is a read-only preview: there is deliberately no save, confirm or
write button here.
"""

from __future__ import annotations

import json
import logging

import streamlit as st

from src.ai.experience_model_service import (
    ExperienceModelError,
    ExperienceModelService,
)
from src.ai.provider import AIError, AIUnavailableError
from src.ai.rag_answer_service import MockCompletionProvider
from src.database import Database
from src.knowledge_context import ContextItemProjector, ContextProjectionError
from src.knowledge_context_packager import (
    KnowledgeContextError,
    KnowledgeContextPackager,
)
from src.models import (
    KNOWLEDGE_MEMORY_STABLE_TYPE,
    ContextItem,
    KnowledgeLifecycle,
    KnowledgeMemoryStatus,
    build_stable_id,
)

LOGGER = logging.getLogger(__name__)
EXPERIENCE_TASK_KEY = "experience_task"
EXPERIENCE_SELECTION_KEY = "experience_context_selection"


def _selectable_contexts(database: Database) -> dict[str, tuple[str, int]]:
    """Return {display_label: (kind, local_id)} for active knowledge assets."""

    options: dict[str, tuple[str, int]] = {}
    kb_uuid = database.get_knowledge_base_uuid()
    for knowledge_object in database.list_knowledge_objects(
        lifecycle=KnowledgeLifecycle.ACTIVE
    ):
        stable_id = database.knowledge_object_stable_id(knowledge_object.id)
        title = knowledge_object.title.strip() or f"知识对象 {knowledge_object.id}"
        options[f"知识对象｜{title}｜{stable_id}"] = (
            "knowledge_object",
            knowledge_object.id,
        )
    for entry in database.list_knowledge_memory_entries(
        status=KnowledgeMemoryStatus.ACTIVE
    ):
        stable_id = build_stable_id(
            kb_uuid, KNOWLEDGE_MEMORY_STABLE_TYPE, entry.id
        )
        title = entry.title.strip() or f"知识记忆 {entry.id}"
        options[f"知识记忆｜{title}｜{stable_id}"] = (
            "knowledge_memory",
            entry.id,
        )
    return options


def _project_selected(
    database: Database, selection: list[str]
) -> list[ContextItem]:
    projector = ContextItemProjector(database)
    items: list[ContextItem] = []
    options = _selectable_contexts(database)
    for label in selection:
        kind, local_id = options.get(label, ("", 0))
        try:
            if kind == "knowledge_object":
                items.append(projector.project_knowledge_object(local_id))
            elif kind == "knowledge_memory":
                items.append(projector.project_knowledge_memory(local_id))
        except ContextProjectionError as exc:
            LOGGER.warning("经验整理上下文投影失败，已跳过：%s", exc)
    return items


def _resolve_provider() -> tuple[object, bool]:
    from src.runtime import application_ai_provider

    try:
        provider = application_ai_provider()
    except Exception:
        LOGGER.exception("解析 AI provider 失败，回退到离线演示")
        provider = None
    if provider is None:
        return MockCompletionProvider(), True
    return provider, False


def render_experience_section(database: Database) -> None:
    """Render the explicit, on-demand experience-candidate preview section."""

    st.divider()
    st.subheader("AI 整理经验")
    st.caption(
        "基于你本次明确选择的知识上下文，按需生成结构化经验候选。"
        "候选仅为 AI 整理结果，不会写入知识库。"
    )
    options = _selectable_contexts(database)
    if not options:
        st.info("当前没有可选择的现行知识对象或知识记忆。")
        return
    selection = st.multiselect(
        "选择知识上下文（必须先选择）",
        options=list(options),
        key=EXPERIENCE_SELECTION_KEY,
    )
    task = st.text_area(
        "整理任务（可选，默认按上下文整理）",
        key=EXPERIENCE_TASK_KEY,
        height=80,
        placeholder="例如：总结这次编码器接线问题的处理经验",
    )
    if st.button(
        "AI 整理经验",
        type="primary",
        use_container_width=True,
        key="experience_generate",
        disabled=not selection,
    ):
        items = _project_selected(database, selection)
        if not items:
            st.warning("所选知识均已失效或被排除，无法整理。")
            return
        packager = KnowledgeContextPackager(
            kb_uuid=database.get_knowledge_base_uuid(), app_version="0.5.3"
        )
        try:
            package = packager.build(items, question=task)
        except KnowledgeContextError as exc:
            st.warning(str(exc))
            return
        provider, is_mock = _resolve_provider()
        try:
            output = ExperienceModelService(provider).generate(task, package)
        except ExperienceModelError as exc:
            st.warning(str(exc))
            return
        except AIUnavailableError as exc:
            st.warning(f"AI 能力不可用（本页浏览与编辑不受影响）：{exc}")
            return
        except AIError as exc:
            st.error(f"AI 服务调用失败（本页浏览与编辑不受影响）：{exc}")
            return
        except Exception as exc:
            LOGGER.exception("AI 整理经验失败")
            st.error(f"AI 整理经验失败：{exc}")
            return
        st.session_state["experience_output"] = output
        st.session_state["experience_is_mock"] = is_mock
        st.session_state["experience_context_items"] = {
            item.stable_id: item for item in package.items
        }

    output = st.session_state.get("experience_output")
    if not output:
        return
    context_items = dict(st.session_state.get("experience_context_items", {}))
    if st.session_state.get("experience_is_mock"):
        st.warning("离线演示生成，未调用真实模型。")
    st.markdown("#### 结构化经验候选（只读预览）")
    candidate = output.candidate
    st.markdown(f"**标题**：{candidate.title}")
    st.markdown(f"**遇到的问题**：{candidate.problem or '（不足以判断）'}")
    st.markdown(f"**适用背景 / 条件**：{candidate.context or '（不足以判断）'}")
    st.markdown(f"**处理方式**：{candidate.action or '（不足以判断）'}")
    st.markdown(f"**结果**：{candidate.result or '（不足以判断）'}")
    st.markdown(f"**最终原因**：{candidate.root_cause or '（不足以判断）'}")
    st.markdown(f"**经验教训**：{candidate.lesson or '（不足以判断）'}")
    st.markdown(f"**适用范围**：{candidate.applicability or '（不足以判断）'}")
    st.markdown(f"**限制 / 不确定性**：{candidate.limitations or '（未说明）'}")
    with st.expander("查看引用与来源信息", expanded=True):
        st.markdown("**本次使用的知识数量与 stable_id**")
        st.caption(f"共 {len(context_items)} 项")
        for stable_id, item in context_items.items():
            anchors = "、".join(
                f"{anchor.anchor_type}:{anchor.anchor_id}"
                for anchor in item.source_anchors
            )
            st.caption(
                f"{item.type.value}｜{stable_id}｜来源：{anchors or '无'}"
            )
        st.markdown("**候选实际引用（已校验）**")
        for stable_id in candidate.citations:
            item = context_items.get(stable_id)
            if item is None:
                st.caption(f"{stable_id}（不在本次上下文中）")
                continue
            anchors = "、".join(
                f"{anchor.anchor_type}:{anchor.anchor_id}"
                for anchor in item.source_anchors
            )
            st.caption(f"{item.type.value}｜{stable_id}｜来源：{anchors or '无'}")
        if output.warnings:
            st.warning("；".join(output.warnings))
    st.caption(
        f"provider：{output.provider}｜model：{output.model}｜"
        f"生成时间：{output.generated_at}"
    )
    st.code(
        json.dumps(
            {
                "title": candidate.title,
                "problem": candidate.problem,
                "context": candidate.context,
                "action": candidate.action,
                "result": candidate.result,
                "root_cause": candidate.root_cause,
                "lesson": candidate.lesson,
                "applicability": candidate.applicability,
                "limitations": candidate.limitations,
                "citations": list(candidate.citations),
            },
            ensure_ascii=False,
            indent=2,
        ),
        language="json",
    )
