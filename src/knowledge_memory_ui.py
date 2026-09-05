"""Plain-language UI for content the user explicitly chose to save.

v0.7 Phase 1 boundary: raw saved Q&A (``kind='raw_qa'``) is always shown as
"保存的问答" — a verbatim question + agent answer copy the user kept. It is
never presented as user experience. Structured experiences are shown as
"整理好的经验". Deleted entries move to a simple "最近删除" list with restore
and explicit permanent-delete actions; nothing is destroyed in one step.

v0.7.0 Experience Capture: an opened raw Q&A offers "整理成经验" — one explicit
AI draft (reuse of the audited ExperienceModelService chain), full user
editing, then an explicit confirm step whose displayed content is exactly what
is committed (session content-hash guard). The AI's root-cause judgment only
becomes "已确认" through a separate user checkbox; saving alone never confirms
it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import streamlit as st

from src.ai.experience_model_service import ExperienceModelService
from src.database import Database
from src.knowledge_memory_service import (
    KnowledgeMemoryEntryNotFoundError,
    KnowledgeMemoryService,
)
from src.models import (
    KnowledgeMemoryEntry,
    MemoryCitation,
    MemoryCitationSnapshotError,
    parse_memory_citations,
)

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 100
_OPEN_ENTRY_KEY = "saved_content_open_entry_id"
_DELETE_ENTRY_KEY = "saved_content_delete_entry_id"
_PURGE_ENTRY_KEY = "saved_content_purge_entry_id"
_EXPERIENCE_DRAFT_SOURCE_KEY = "saved_experience_draft_source_id"
_EXPERIENCE_DRAFT_KEY = "saved_experience_draft"
_EXPERIENCE_DRAFT_HASH_KEY = "saved_experience_draft_hash"

_DRAFT_FIELD_LABELS: tuple[tuple[str, str, int], ...] = (
    ("title", "标题", 200),
    ("problem", "遇到的问题", 4000),
    ("context", "适用背景 / 条件", 4000),
    ("action", "处理方式", 4000),
    ("result", "结果", 4000),
    ("root_cause", "最终原因", 4000),
    ("lesson", "经验教训", 4000),
    ("applicability", "适用范围", 4000),
    ("limitations", "限制 / 不确定性", 4000),
)


@dataclass(frozen=True, slots=True)
class ExperienceDraft:
    """One AI-generated, not-yet-confirmed experience draft (session only)."""

    fields: dict[str, str]
    is_mock: bool


def render_knowledge_memory_page(
    service: KnowledgeMemoryService, *, database: Database | None = None
) -> None:
    """Render saved items in two plain sections plus the restore area."""

    flash = st.session_state.pop("saved_content_flash", "")
    if flash:
        st.success(flash)
    entries = service.list(limit=PAGE_SIZE)
    total = service.count()
    if total == 0:
        st.info(
            "你还没有保存过内容。向 Agent 提问后，可以手动保存有用的问答，"
            "再把有用的问答整理成经验。"
        )
    else:
        raw_qa_entries = [
            entry for entry in entries if entry.kind.value == "raw_qa"
        ]
        experience_entries = [
            entry for entry in entries if entry.kind.value != "raw_qa"
        ]
        st.caption(f"共保存了 {total} 条内容")
        st.markdown("### 保存的问答")
        if raw_qa_entries:
            for entry in raw_qa_entries:
                _render_entry(service, entry, database=database)
        else:
            st.caption(
                "还没有保存过问答。在“问问 Agent”里回答完成后，"
                "点击“保存这次问答”就会出现在这里。"
            )
        st.markdown("### 整理好的经验")
        if experience_entries:
            for entry in experience_entries:
                _render_entry(service, entry, database=database)
        else:
            st.caption(
                "打开上面任意一条保存的问答，点击“整理成经验”，"
                "就能把有用的一问一答整理成经验。"
            )
    _render_deleted_section(service)


def _render_entry(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    *,
    database: Database | None,
) -> None:
    """Render one compact card with date, type, title, preview and actions."""

    opened = st.session_state.get(_OPEN_ENTRY_KEY) == entry.id
    pending_delete = st.session_state.get(_DELETE_ENTRY_KEY) == entry.id
    with st.container(border=True):
        st.caption(
            f"{_friendly_date(entry.created_at)} · {_kind_label(entry)}"
        )
        st.markdown(f"**{_display_title(entry, database)}**")
        preview = _content_preview(entry.content)
        if preview:
            st.write(f"“{preview}”")
        action_columns = st.columns([1, 1])
        if action_columns[0].button(
            "收起" if opened else "查看",
            key=f"saved_view_{entry.id}",
            use_container_width=True,
        ):
            st.session_state[_OPEN_ENTRY_KEY] = None if opened else entry.id
            st.rerun()
        if action_columns[1].button(
            "删除", key=f"saved_delete_{entry.id}", use_container_width=True
        ):
            st.session_state[_DELETE_ENTRY_KEY] = entry.id
            st.rerun()

        if opened:
            _render_entry_detail(service, entry, database=database)

        if pending_delete:
            st.warning(
                "确定删除这条内容吗？删除后可以在“最近删除”里恢复，"
                "原始资料不受影响。"
            )
            confirm_column, cancel_column = st.columns([1, 1])
            if confirm_column.button(
                "确认删除",
                key=f"saved_confirm_delete_{entry.id}",
                type="primary",
                use_container_width=True,
            ):
                _delete_entry(service, entry.id)
            if cancel_column.button(
                "取消", key=f"saved_cancel_delete_{entry.id}", use_container_width=True
            ):
                st.session_state.pop(_DELETE_ENTRY_KEY, None)
                st.rerun()


def _render_entry_detail(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    *,
    database: Database | None,
) -> None:
    """Render the expanded view: full copy, citation history and notes."""

    st.markdown("#### 保存的完整内容")
    st.write(entry.content or "（没有正文）")
    if entry.root_cause.strip():
        st.markdown(f"**原因**：{entry.root_cause}")
    if entry.lesson.strip():
        st.markdown(f"**以后可以这样做**：{entry.lesson}")
    if database is not None:
        _render_citation_history(entry, database)
    if entry.kind.value == "raw_qa":
        _render_promote_section(service, entry, database=database)
    else:
        _render_source_link(service, entry)


def _render_promote_section(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    *,
    database: Database | None,
) -> None:
    """Offer the explicit raw-QA → experience draft flow for one saved Q&A."""

    st.divider()
    draft_active = st.session_state.get(_EXPERIENCE_DRAFT_SOURCE_KEY) == entry.id
    if not draft_active:
        st.caption(
            "这条问答只是一个副本。如果它对你有长期价值，可以整理成一条经验。"
        )
        if st.button(
            "整理成经验",
            key=f"saved_promote_{entry.id}",
            use_container_width=True,
        ):
            _generate_experience_draft(service, entry, database=database)
        return
    draft = st.session_state.get(_EXPERIENCE_DRAFT_KEY)
    if not isinstance(draft, ExperienceDraft):
        st.session_state.pop(_EXPERIENCE_DRAFT_SOURCE_KEY, None)
        st.rerun()
        return
    _render_experience_draft_form(service, entry, draft)


def _generate_experience_draft(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    *,
    database: Database | None,
) -> None:
    """Produce one AI experience draft from the saved Q&A, or explain failures.

    Reuses the audited ExperienceModelService chain (the same one as the
    knowledge page); when AI is unavailable the deterministic offline draft is
    used and clearly labelled, so the page never silently pretends.
    """

    if database is None:
        st.error("当前无法读取知识库，暂时不能整理经验。")
        return
    question = _raw_qa_question(entry.content)
    task = f"把这条用户保存的问答整理成一条结构化的个人经验：{question}"
    try:
        from src import __version__
        from src.knowledge_context import ContextItemProjector
        from src.knowledge_context_packager import KnowledgeContextPackager

        item = ContextItemProjector(database).project_knowledge_memory(entry.id)
        package = KnowledgeContextPackager(
            kb_uuid=database.get_knowledge_base_uuid(), app_version=__version__
        ).build([item], question=task)
    except Exception as exc:
        LOGGER.exception("整理经验的上下文构建失败")
        st.warning(f"这次没有生成整理草稿：{exc}")
        return

    from src.ai.provider import AIError, AIUnavailableError
    from src.ai.rag_answer_service import MockCompletionProvider
    from src.runtime import application_experience_model_service

    try:
        output = application_experience_model_service().generate(task, package)
    except AIUnavailableError:
        output = ExperienceModelService(MockCompletionProvider()).generate(
            task, package
        )
    except AIError as exc:
        st.error(f"AI 服务调用失败，这次没有生成整理草稿：{exc}")
        return
    except Exception as exc:  # noqa: BLE001 - UI 边界兜底，保持页面可用
        LOGGER.exception("AI 整理经验失败")
        st.error(f"AI 整理经验失败：{exc}")
        return
    is_mock = bool(output.is_mock)
    candidate = output.candidate
    fields = {
        "title": candidate.title,
        "problem": candidate.problem,
        "context": candidate.context,
        "action": candidate.action,
        "result": candidate.result,
        "root_cause": candidate.root_cause,
        "lesson": candidate.lesson,
        "applicability": candidate.applicability,
        "limitations": candidate.limitations,
    }
    st.session_state[_EXPERIENCE_DRAFT_SOURCE_KEY] = entry.id
    st.session_state[_EXPERIENCE_DRAFT_KEY] = ExperienceDraft(
        fields=fields, is_mock=is_mock
    )
    st.rerun()


def _render_experience_draft_form(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    draft: ExperienceDraft,
) -> None:
    """Render the editable draft and the display-to-commit confirm step."""

    st.markdown("##### 整理成经验（草稿）")
    if draft.is_mock:
        st.warning(
            "当前没有可用的 AI 服务，这份草稿由离线演示生成，请自行修改后再保存。"
        )
    else:
        st.caption("这是 AI 根据这条问答整理的草稿。请逐项检查并修改，确认无误后再保存。")
    fields: dict[str, str] = {}
    for field, label, max_chars in _DRAFT_FIELD_LABELS:
        if field == "title":
            fields[field] = st.text_input(
                label,
                value=draft.fields.get(field, ""),
                max_chars=max_chars,
                key=f"saved_exp_draft_{entry.id}_{field}",
            )
        else:
            fields[field] = st.text_area(
                label,
                value=draft.fields.get(field, ""),
                max_chars=max_chars,
                height=72,
                key=f"saved_exp_draft_{entry.id}_{field}",
            )
    root_cause_confirmed = False
    if fields["root_cause"].strip():
        root_cause_confirmed = st.checkbox(
            "我确认“最终原因”的判断与我的实际情况相符",
            key=f"saved_exp_draft_{entry.id}_confirmed",
            help=(
                "最终原因最初来自 AI 整理，只有你在这里明确勾选，"
                "保存后才会标记为已确认。"
            ),
        )

    assembled = _assemble_experience_content(fields)
    st.session_state[_EXPERIENCE_DRAFT_HASH_KEY] = _draft_hash(
        assembled, root_cause_confirmed
    )
    with st.expander("确认前请核对：将要保存的完整内容", expanded=True):
        st.write(assembled or "（还没有内容）")
        st.markdown(f"**最终原因**：{fields['root_cause'].strip() or '（未填写）'}")
        st.markdown(f"**经验教训**：{fields['lesson'].strip() or '（未填写）'}")

    confirm_column, cancel_column = st.columns([1, 1])
    if confirm_column.button(
        "确认保存为经验",
        key=f"saved_exp_confirm_{entry.id}",
        type="primary",
        use_container_width=True,
    ):
        if st.session_state.get(_EXPERIENCE_DRAFT_HASH_KEY) != _draft_hash(
            assembled, root_cause_confirmed
        ):
            st.warning("内容刚刚发生变化，请重新核对后再保存。")
            return
        _save_experience_draft(service, entry, fields, root_cause_confirmed)
    if cancel_column.button(
        "放弃整理", key=f"saved_exp_cancel_{entry.id}", use_container_width=True
    ):
        _clear_experience_draft()
        st.rerun()


def _draft_hash(assembled: str, root_cause_confirmed: bool) -> str:
    """Return the display-to-commit integrity hash of one draft render."""

    return hashlib.sha256(
        (assembled + "|" + str(root_cause_confirmed)).encode("utf-8")
    ).hexdigest()


def _save_experience_draft(
    service: KnowledgeMemoryService,
    entry: KnowledgeMemoryEntry,
    fields: dict[str, str],
    root_cause_confirmed: bool,
) -> None:
    """Commit the confirmed draft; the displayed content is what is stored."""

    try:
        service.promote_raw_qa_to_experience(
            entry.id,
            title=fields["title"],
            content=_assemble_experience_content(fields),
            root_cause=fields["root_cause"],
            lesson=fields["lesson"],
            outcome=fields["result"],
            context_conditions=fields["applicability"],
            root_cause_confirmed=root_cause_confirmed,
        )
    except Exception as exc:
        LOGGER.exception("保存整理经验失败")
        st.error(f"这次没有保存成功：{exc}")
        return
    _clear_experience_draft()
    st.session_state["saved_content_flash"] = (
        "整理好的经验已保存，可在“整理好的经验”里查看。"
    )
    st.rerun()


def _assemble_experience_content(fields: dict[str, str]) -> str:
    """Compose the exact narrative content that will be stored and displayed.

    Only non-empty sections are included, so the confirmation preview and the
    committed content are the same string by construction.
    """

    sections = (
        ("遇到的问题", fields.get("problem", "")),
        ("适用背景", fields.get("context", "")),
        ("处理方式", fields.get("action", "")),
        ("结果", fields.get("result", "")),
        ("适用范围", fields.get("applicability", "")),
        ("限制与不确定性", fields.get("limitations", "")),
    )
    return "\n\n".join(
        f"{heading}：{value.strip()}" for heading, value in sections if value.strip()
    )


def _raw_qa_question(content: str) -> str:
    """Extract the saved question from the verbatim raw-QA copy."""

    text = content.strip()
    if text.startswith("问题："):
        text = text[len("问题：") :]
    if "Agent 回答：" in text:
        text = text.split("Agent 回答：", 1)[0]
    return text.strip() or "（这条问答没有可识别的问题）"


def _render_source_link(
    service: KnowledgeMemoryService, entry: KnowledgeMemoryEntry
) -> None:
    """Show the raw-QA provenance of one structured experience."""

    if entry.source_entry_id is None:
        return
    st.divider()
    source_title = entry.source_title or f"第 {entry.source_entry_id} 条保存内容"
    source = service.get(entry.source_entry_id)
    if source is None:
        st.caption(
            f"来自保存的问答：{source_title}（该问答已被删除，原始副本不再可见。）"
        )
        return
    st.caption(f"来自保存的问答：{source_title}")
    if st.button(
        "查看原始问答", key=f"saved_open_source_{entry.id}", use_container_width=True
    ):
        st.session_state[_OPEN_ENTRY_KEY] = source.id
        st.rerun()


def _render_citation_history(entry: KnowledgeMemoryEntry, database: Database) -> None:
    """Show what the saved answer cited, honoring deleted source material.

    The snapshot is frozen at save time, so a citation survives the deletion
    of its document as an honest historical note — never as a link that
    pretends the material is still openable.
    """

    try:
        citations = parse_memory_citations(entry.citation_snapshot)
    except MemoryCitationSnapshotError:
        st.caption("保存时记录的引用信息无法解析，已跳过显示。")
        return
    if not citations:
        return
    st.markdown("##### 保存时引用的原始资料")
    for citation in citations:
        st.write(_citation_line(citation, database))
        _render_open_original_page(citation, database)


def _render_open_original_page(citation: MemoryCitation, database: Database) -> None:
    """Offer one click back to the original page when the material still exists.

    Deleted material stays an honest historical note: only a live document and
    page get an open button, never a dead link.
    """

    if citation.document_id is None or citation.page_id is None:
        return
    if (
        database.get_document(citation.document_id) is None
        or database.get_page(citation.page_id) is None
    ):
        return
    if st.button(
        "打开原页",
        key=f"saved_open_page_{citation.page_id}",
        use_container_width=True,
    ):
        st.query_params["document"] = str(citation.document_id)
        st.query_params["page"] = str(citation.page_number)
        st.switch_page("pages/17_我的资料.py")


def _citation_line(citation: MemoryCitation, database: Database) -> str:
    """Return one plain-language citation line with live availability."""

    location = f"第 {citation.page_number} 页" if citation.page_number else ""
    document = (
        database.get_document(citation.document_id)
        if citation.document_id is not None
        else None
    )
    page_exists = (
        citation.page_id is None or database.get_page(citation.page_id) is not None
    )
    title = citation.document_title or "一份已删除的资料"
    if document is not None and page_exists:
        return f"《{title}》{location}".strip()
    if location:
        return f"曾引用《{title}》（{location}），但原资料现已不可用。"
    return f"曾引用《{title}》，但原资料现已不可用。"


def _render_deleted_section(service: KnowledgeMemoryService) -> None:
    """Render the simple restore area for tombstoned entries."""

    try:
        deleted_entries = service.list_deleted(limit=PAGE_SIZE)
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("读取最近删除失败")
        return
    if not deleted_entries:
        return
    with st.expander(f"最近删除（{len(deleted_entries)} 条）"):
        st.caption("删除的内容先放在这里；确认不再需要时才永久删除。")
        for entry in deleted_entries:
            _render_deleted_entry(service, entry)


def _render_deleted_entry(
    service: KnowledgeMemoryService, entry: KnowledgeMemoryEntry
) -> None:
    """Render one tombstoned entry with restore and explicit purge actions."""

    pending_purge = st.session_state.get(_PURGE_ENTRY_KEY) == entry.id
    with st.container(border=True):
        st.caption(
            f"{_friendly_date(entry.created_at)} · {_kind_label(entry)}"
        )
        st.write(entry.title)
        action_columns = st.columns([1, 1])
        if action_columns[0].button(
            "恢复",
            key=f"saved_restore_{entry.id}",
            use_container_width=True,
        ):
            _restore_entry(service, entry.id)
        if action_columns[1].button(
            "永久删除", key=f"saved_purge_{entry.id}", use_container_width=True
        ):
            st.session_state[_PURGE_ENTRY_KEY] = entry.id
            st.rerun()
        if pending_purge:
            st.error(
                "永久删除后无法恢复。原始资料不受影响，但这份保存内容会彻底消失。"
            )
            confirm_column, cancel_column = st.columns([1, 1])
            if confirm_column.button(
                "确认永久删除",
                key=f"saved_purge_confirm_{entry.id}",
                type="primary",
                use_container_width=True,
            ):
                _purge_entry(service, entry.id)
            if cancel_column.button(
                "取消", key=f"saved_purge_cancel_{entry.id}", use_container_width=True
            ):
                st.session_state.pop(_PURGE_ENTRY_KEY, None)
                st.rerun()


def _delete_entry(service: KnowledgeMemoryService, entry_id: int) -> None:
    """Move one saved item to the restore area (soft delete)."""

    try:
        service.delete_entry(entry_id)
    except KnowledgeMemoryEntryNotFoundError as exc:
        st.error(str(exc))
    else:
        st.session_state.pop(_DELETE_ENTRY_KEY, None)
        if st.session_state.get(_OPEN_ENTRY_KEY) == entry_id:
            st.session_state.pop(_OPEN_ENTRY_KEY, None)
        if st.session_state.get(_EXPERIENCE_DRAFT_SOURCE_KEY) == entry_id:
            _clear_experience_draft()
        st.session_state["saved_content_flash"] = "这条内容已删除，可在“最近删除”里恢复。"
        st.rerun()


def _clear_experience_draft() -> None:
    """Drop any in-progress experience draft state."""

    st.session_state.pop(_EXPERIENCE_DRAFT_SOURCE_KEY, None)
    st.session_state.pop(_EXPERIENCE_DRAFT_KEY, None)
    st.session_state.pop(_EXPERIENCE_DRAFT_HASH_KEY, None)


def _restore_entry(service: KnowledgeMemoryService, entry_id: int) -> None:
    """Restore one tombstoned entry back into the saved list."""

    try:
        service.restore_entry(entry_id)
    except Exception as exc:
        LOGGER.exception("恢复保存内容失败")
        st.error(f"恢复失败：{exc}")
    else:
        st.session_state["saved_content_flash"] = "这条内容已恢复。"
        st.rerun()


def _purge_entry(service: KnowledgeMemoryService, entry_id: int) -> None:
    """Permanently delete one already-deleted entry."""

    try:
        service.purge_entry(entry_id)
    except Exception as exc:
        LOGGER.exception("永久删除保存内容失败")
        st.error(f"永久删除失败：{exc}")
    else:
        st.session_state.pop(_PURGE_ENTRY_KEY, None)
        st.session_state["saved_content_flash"] = "这条内容已永久删除。"
        st.rerun()


def _kind_label(entry: KnowledgeMemoryEntry) -> str:
    """Return the plain-language record type shown on every card.

    Raw saved Q&A must never be labelled as experience; its fixed label is
    "保存的问答".
    """

    if entry.kind.value == "raw_qa":
        return "保存的问答"
    return entry.kind.label


def _friendly_date(value) -> str:
    """Return a short date a non-technical user can scan quickly."""

    local_value = value.astimezone() if value.tzinfo is not None else value
    return f"{local_value.month} 月 {local_value.day} 日"


def _display_title(entry: KnowledgeMemoryEntry, database: Database | None) -> str:
    """Raw Q&A keeps the document-conversation title; experiences show their
    user-confirmed title."""

    if (
        entry.kind.value == "raw_qa"
        and database is not None
        and entry.document_id is not None
    ):
        document = database.get_document(entry.document_id)
        if document is not None:
            return f"关于 {document.title} 的讨论"
    return entry.title


def _content_preview(content: str, *, limit: int = 140) -> str:
    """Prefer the saved Agent answer and keep the card preview compact."""

    text = content.strip()
    if "Agent 回答：" in text:
        text = text.split("Agent 回答：", 1)[1].strip()
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"
