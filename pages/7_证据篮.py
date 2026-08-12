"""Manage durable multi-page evidence and export a grounded Markdown package."""

from __future__ import annotations

import logging

import streamlit as st

from src.evidence_basket_service import EvidenceBasketError
from src.models import EvidenceConfirmationStatus, EvidenceType
from src.runtime import application_evidence_basket_service

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="证据篮｜工程知识库 v0.4.0", page_icon="🧺", layout="wide")
st.title("证据篮")
st.caption("持久收集多个页面的具体选区，核对来源后按当前顺序生成 Markdown 证据包。")

try:
    basket_service = application_evidence_basket_service()
    basket = basket_service.default_basket()
    items = basket_service.list_items(basket.id)
except Exception as exc:
    LOGGER.exception("读取证据篮失败")
    st.error(f"读取证据篮失败：{exc}")
    st.stop()

flash = st.session_state.pop("evidence_basket_flash", "")
if flash:
    st.success(flash)

summary = st.columns(3)
summary[0].metric("当前证据", len(items))
summary[1].metric("来源文档", len({item.document_id for item in items}))
summary[2].metric(
    "未复核证据",
    sum(item.review_status.value != "reviewed" for item in items),
)

if not items:
    st.info("证据篮为空。可从“检索资料”或“浏览资料”页面加入具体证据选区。")
    empty_actions = st.columns(2)
    if empty_actions[0].button("🔎 前往检索资料", use_container_width=True):
        st.switch_page("pages/4_检索资料.py")
    if empty_actions[1].button("📖 前往浏览资料", use_container_width=True):
        st.switch_page("pages/3_浏览资料.py")
else:
    for index, item in enumerate(items):
        with st.container(border=True):
            title_column, up_column, down_column, source_column, delete_column = st.columns(
                [4.2, 0.7, 0.7, 1.1, 0.8]
            )
            title_column.markdown(
                f"### {index + 1}. {item.document_title} · 第 {item.page_number} 页"
            )
            title_column.markdown(f":gray-badge[{item.evidence_type.label}]")
            title_column.caption(
                f"原始文件：{item.filename}　|　复核状态：{item.review_status.label}　|　"
                f"可信度：{item.text_kind.label}"
            )
            if up_column.button(
                "↑",
                key=f"evidence_up_{item.id}",
                disabled=index == 0,
                help="上移",
                use_container_width=True,
            ):
                reordered_ids = [candidate.id for candidate in items]
                reordered_ids[index - 1], reordered_ids[index] = (
                    reordered_ids[index],
                    reordered_ids[index - 1],
                )
                try:
                    basket_service.reorder(reordered_ids, basket_id=basket.id)
                except EvidenceBasketError as exc:
                    st.error(f"调整证据顺序失败：{exc}")
                else:
                    st.rerun()
            if down_column.button(
                "↓",
                key=f"evidence_down_{item.id}",
                disabled=index >= len(items) - 1,
                help="下移",
                use_container_width=True,
            ):
                reordered_ids = [candidate.id for candidate in items]
                reordered_ids[index], reordered_ids[index + 1] = (
                    reordered_ids[index + 1],
                    reordered_ids[index],
                )
                try:
                    basket_service.reorder(reordered_ids, basket_id=basket.id)
                except EvidenceBasketError as exc:
                    st.error(f"调整证据顺序失败：{exc}")
                else:
                    st.rerun()
            if source_column.button(
                "返回原始页",
                key=f"evidence_source_{item.id}",
                use_container_width=True,
            ):
                try:
                    source_item = basket_service.validated_item(
                        item.id, basket_id=basket.id
                    )
                except EvidenceBasketError as exc:
                    st.error(f"无法打开来源：{exc}")
                else:
                    source_params = {
                        "document": str(source_item.document_id),
                        "page": str(source_item.page_number),
                        "from_search": "0",
                    }
                    # ``st.switch_page`` does not reliably carry query parameters
                    # between pages. The reader revalidates this one-shot handoff.
                    st.session_state["pending_reader_query_params"] = source_params
                    st.query_params.clear()
                    st.query_params.update(source_params)
                    st.switch_page("pages/3_浏览资料.py")
            if delete_column.button(
                "删除",
                key=f"evidence_delete_{item.id}",
                use_container_width=True,
            ):
                try:
                    basket_service.remove_item(item.id, basket_id=basket.id)
                except EvidenceBasketError as exc:
                    st.error(f"删除证据失败：{exc}")
                else:
                    st.session_state["evidence_basket_flash"] = "已删除一条证据。"
                    st.rerun()

            projects = "、".join(item.projects) if item.projects else "未关联项目"
            tags = "、".join(item.tags) if item.tags else "未添加标签"
            st.caption(f"项目：{projects}　|　标签：{tags}　|　排序位置：{item.position}")
            confirmation_column, confirm_action_column = st.columns([3, 1])
            confirmation_caption = f"确认状态：{item.confirmation_status.label}"
            if item.confirmed_at is not None:
                confirmation_caption += (
                    f"（确认于 {item.confirmed_at.astimezone():%Y-%m-%d %H:%M}）"
                )
            confirmation_column.caption(confirmation_caption)
            is_confirmed = (
                item.confirmation_status is EvidenceConfirmationStatus.CONFIRMED
            )
            if confirm_action_column.button(
                "取消确认" if is_confirmed else "确认证据",
                key=f"evidence_confirm_{item.id}",
                use_container_width=True,
            ):
                try:
                    basket_service.set_confirmation(item.id, not is_confirmed)
                except EvidenceBasketError as exc:
                    st.error(f"更新确认状态失败：{exc}")
                else:
                    st.session_state["evidence_basket_flash"] = (
                        "证据确认状态已更新。"
                    )
                    st.rerun()
            if item.evidence_type is EvidenceType.IMAGE_REGION:
                st.markdown("**图片区域**")
                st.caption(
                    f"区域坐标：({item.region_x0}, {item.region_y0}) - "
                    f"({item.region_x1}, {item.region_y1})　|　"
                    f"锚点图像尺寸：{item.region_image_width} × "
                    f"{item.region_image_height} 像素"
                )
            elif item.evidence_type is EvidenceType.TEXT_SELECTION:
                st.markdown("**证据选区**")
                st.code(item.evidence_text, language=None)
            else:
                st.caption("整页证据引用整个页面，不包含独立选区文本。")
            if item.context:
                with st.expander(item.context_kind.label):
                    st.text(item.context)
            with st.form(f"evidence_note_form_{item.id}"):
                note = st.text_area(
                    "用户备注",
                    value=item.user_note,
                    height=90,
                    key=f"evidence_note_{item.id}",
                )
                if st.form_submit_button("保存备注", use_container_width=True):
                    try:
                        basket_service.update_note(
                            item.id,
                            note,
                            basket_id=basket.id,
                        )
                    except EvidenceBasketError as exc:
                        st.error(f"保存备注失败：{exc}")
                    else:
                        st.session_state["evidence_basket_flash"] = "证据备注已保存。"
                        st.rerun()

st.divider()
st.subheader("生成多页面 Markdown 证据包")
package_title = st.text_input("证据包标题", value=basket.name)
if st.button(
    "生成证据包",
    type="primary",
    disabled=not items,
    use_container_width=True,
):
    try:
        st.session_state["basket_markdown_package"] = basket_service.export_markdown(
            basket_id=basket.id,
            title=package_title,
        )
    except EvidenceBasketError as exc:
        st.error(f"无法生成证据包：{exc}")
    except Exception as exc:
        LOGGER.exception("生成多页面证据包失败")
        st.error(f"生成多页面证据包失败：{exc}")

package = st.session_state.get("basket_markdown_package", "")
if package:
    st.code(package, language="markdown")
    st.download_button(
        "保存 Markdown 文件",
        data=package.encode("utf-8"),
        file_name="engineering-evidence-package.md",
        mime="text/markdown",
        use_container_width=True,
    )

with st.expander("清空证据篮"):
    st.warning("清空只删除当前证据篮条目，不删除原 PDF、页面图像、页面记录或用户笔记。")
    confirm_clear = st.checkbox("我确认清空当前证据篮", key="confirm_clear_basket")
    if st.button(
        "清空证据篮",
        disabled=not confirm_clear or not items,
        use_container_width=True,
    ):
        try:
            removed_count = basket_service.clear(basket_id=basket.id)
        except EvidenceBasketError as exc:
            st.error(f"清空证据篮失败：{exc}")
        else:
            st.session_state.pop("basket_markdown_package", None)
            st.session_state["evidence_basket_flash"] = f"已清空 {removed_count} 条证据。"
            st.rerun()
