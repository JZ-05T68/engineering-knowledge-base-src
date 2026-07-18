"""Show local runtime, storage, and privacy information."""

from __future__ import annotations

import streamlit as st

from src.runtime import application_settings

st.set_page_config(page_title="运行说明｜工程知识库 v0.0.5", page_icon="⚙️", layout="wide")
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
st.info("v0.0.5 不包含账号、登录、云同步、付费 API、向量数据库或联网必需功能。")
