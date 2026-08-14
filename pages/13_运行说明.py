"""Show local runtime, storage, and privacy information."""

from __future__ import annotations

import streamlit as st

from src.runtime import application_settings

st.set_page_config(page_title="运行说明｜工程知识库 v0.5.0", page_icon="⚙️", layout="wide")
st.title("设置与运行说明")

settings = application_settings()
st.success(f"服务正常运行：{settings.host}:{settings.port}")
st.caption("健康检查由本机 Streamlit 内置端点 `/_stcore/health` 提供，不包含用户资料。")

st.subheader("本地位置")
st.code(
    "\n".join(
        (
            f"数据目录：{settings.data_dir}",
            f"数据库：{settings.database_path}",
            f"原 PDF：{settings.raw_dir}",
            f"页面图片：{settings.pages_dir}",
            f"Markdown：{settings.markdown_dir}",
            f"日志：{settings.logs_dir}",
            f"运行状态：{settings.runtime_dir}",
        )
    )
)

st.subheader("日常运行")
st.markdown(
    """
- 双击项目根目录的 `启动工程知识库.bat` 启动，浏览器会自动打开。
- 双击 `停止工程知识库.bat` 只停止本项目记录的进程。
- `启用开机自启.bat` 与 `关闭开机自启.bat` 管理当前用户登录后的可选计划任务。
- 服务仅绑定 `127.0.0.1`，不会默认向局域网开放。
"""
)
st.info(
    "v0.5.0 不包含账号、登录、云同步、向量数据库或联网必需功能。"
    "AI 默认为手动模式：未配置 API Key 时全部原有功能离线可用，"
    "可选的 Qwen API 接入默认关闭，且不会在后台自动发起请求。"
)
st.caption(
    "当前数据库为 schema v7。已有 schema v6 数据库升级时会先创建一致性备份，再将既有证据"
    "保留为未确认的文字选区证据，并增加整页、文字选区、图片区域三类证据与人工确认状态。"
    "更早的 schema 会按版本顺序迁移；正式恢复只接受与当前版本兼容且同为 schema v7 的完整备份。"
)
if st.button("🛡️ 打开系统维护、备份与诊断", use_container_width=True):
    st.switch_page("pages/12_系统维护.py")
