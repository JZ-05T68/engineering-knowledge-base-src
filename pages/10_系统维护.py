"""Verified backups, restore prechecks, diagnostics, and redacted reports."""

from __future__ import annotations

import logging
import platform
import shutil
import time
from pathlib import Path

import streamlit as st

from src.backup_service import (
    BackupError,
    list_backup_candidates,
    read_database_summary,
    validate_backup,
)
from src.diagnostic_service import (
    DiagnosticSnapshot,
    DiagnosticStatus,
    generate_diagnostic_report,
)
from src.migrations import SCHEMA_VERSION
from src.runtime import (
    application_backup_service,
    application_diagnostic_service,
    application_settings,
)

LOGGER = logging.getLogger(__name__)

st.set_page_config(
    page_title="系统维护｜工程知识库 v0.3.0",
    page_icon="🛡️",
    layout="wide",
)
st.title("系统维护")
st.caption("完整备份、安全恢复预检、只读诊断和默认脱敏报告；所有操作均在本机完成。")

try:
    settings = application_settings()
    backup_service = application_backup_service()
    database_summary = read_database_summary(settings.database_path)
    disk_free = shutil.disk_usage(settings.data_dir).free
except Exception as exc:
    LOGGER.exception("系统维护页面初始化失败")
    st.error(f"系统维护页面初始化失败：{exc}")
    st.stop()

top_metrics = st.columns(6)
top_metrics[0].metric("应用版本", f"v{settings.app_version}")
top_metrics[1].metric("schema", f"v{database_summary.schema_version}")
top_metrics[2].metric("文档", database_summary.documents)
top_metrics[3].metric("页面", database_summary.pages)
top_metrics[4].metric("FTS", database_summary.fts)
top_metrics[5].metric("证据", database_summary.evidence)

with st.expander("运行环境与正式路径", expanded=False):
    st.write(f"Python：{platform.python_version()}")
    st.write(f"服务地址：{settings.host}:{settings.port}")
    st.write(f"可用磁盘空间：{disk_free / (1024**3):.2f} GB")
    st.code(
        "\n".join(
            (
                f"数据库：{settings.database_path}",
                f"正式数据：{settings.data_dir}",
                f"完整备份：{settings.backups_dir}",
                f"日志：{settings.logs_dir}",
            )
        )
    )

st.divider()
st.header("完整本地备份")
st.caption(
    "备份使用一致性 SQLite 快照，并对原 PDF、页面 PNG、页面 Markdown、恢复配置和数据库"
    "逐文件计算 SHA-256。只有全部验证通过后才会原子完成。"
)
backup_target = Path(
    st.text_input(
        "备份目标目录",
        value=str(settings.backups_dir),
        help="必须位于正式 data 目录之外；不会覆盖同名已有备份。",
    )
)
if st.button("创建并验证完整备份", type="primary", use_container_width=True):
    try:
        result = backup_service.create_backup(backup_target)
    except BackupError as exc:
        LOGGER.warning("完整备份未完成：%s", type(exc).__name__)
        st.error(f"备份失败：{exc}")
    except Exception as exc:
        LOGGER.exception("完整备份发生未预期错误")
        st.error(f"备份失败：{exc}")
    else:
        st.session_state["maintenance_last_backup"] = str(result.backup_path)
        st.success(f"完整备份已创建并验证：{result.backup_path.name}")
        timing = st.columns(2)
        timing[0].metric("创建总耗时", f"{result.creation_seconds:.3f} 秒")
        timing[1].metric("完整验证耗时", f"{result.verification_seconds:.3f} 秒")

candidates = list_backup_candidates(settings.backups_dir)
if not candidates:
    st.info(f"还没有 v{settings.app_version} 完整备份。请先创建第一个备份，再进行恢复预检。")
else:
    st.caption(f"已发现 {len(candidates)} 个带 manifest 的备份目录。")
    candidate_by_name = {candidate.name: candidate for candidate in candidates}
    selected_name = st.selectbox("选择备份进行完整预检", options=list(candidate_by_name))
    selected_backup = candidate_by_name[selected_name]
    if st.button("验证所选备份（只读）", use_container_width=True):
        validation = validate_backup(
            selected_backup,
            expected_app_version=settings.app_version,
            expected_schema_version=SCHEMA_VERSION,
        )
        st.session_state["maintenance_backup_validation"] = validation

    validation = st.session_state.get("maintenance_backup_validation")
    if validation is not None and validation.backup_path == selected_backup:
        if validation.valid and validation.manifest is not None:
            statistics = validation.manifest["statistics"]
            st.success("备份 manifest、文件哈希、数据库完整性、外键和文件引用全部通过。")
            restore_metrics = st.columns(4)
            restore_metrics[0].metric("备份时间", validation.manifest["created_at"])
            restore_metrics[1].metric("文档", statistics["documents"])
            restore_metrics[2].metric("页面", statistics["pages"])
            restore_metrics[3].metric("FTS", statistics["fts"])
            st.caption(f"完整预检耗时：{validation.duration_seconds:.3f} 秒")
            st.warning(
                "为避免 Streamlit 占用数据库，本页面不会在线覆盖正式资料。正式恢复必须先停止服务，"
                "再由独立脚本执行；脚本会重新验证并自动创建恢复前完整备份。"
            )
            command = (
                ".\\.venv\\Scripts\\python.exe scripts\\restore_backup.py "
                f'--backup "{selected_backup}" --confirm RESTORE'
            )
            st.code(command, language="powershell")
            st.markdown(
                "1. 双击 `停止工程知识库.bat`。  \n"
                "2. 在项目根目录 PowerShell 执行上面的恢复命令。  \n"
                "3. 恢复成功后双击 `启动工程知识库.bat`，再运行一次只读诊断。"
            )
        else:
            st.error("备份验证失败，禁止强制恢复。")
            for error in validation.errors:
                st.write(f"- {error}")

st.divider()
st.header("一键只读诊断")
st.caption("诊断不会删除、重建、修改或自动修复正式数据库和用户文件。")
if st.button("运行完整只读诊断", use_container_width=True):
    try:
        snapshot = application_diagnostic_service().run()
    except Exception as exc:
        LOGGER.exception("只读诊断失败")
        st.error(f"只读诊断失败：{exc}")
    else:
        st.session_state["maintenance_diagnostic_snapshot"] = snapshot

snapshot = st.session_state.get("maintenance_diagnostic_snapshot")
if isinstance(snapshot, DiagnosticSnapshot):
    status_method = {
        DiagnosticStatus.NORMAL: st.success,
        DiagnosticStatus.WARNING: st.warning,
        DiagnosticStatus.ERROR: st.error,
    }[snapshot.overall_status]
    status_method(
        f"诊断完成：总体{snapshot.overall_status.label}，耗时 {snapshot.duration_seconds:.3f} 秒。"
    )
    for check in snapshot.checks:
        icon = {
            DiagnosticStatus.NORMAL: "✅",
            DiagnosticStatus.WARNING: "⚠️",
            DiagnosticStatus.ERROR: "❌",
        }[check.status]
        with st.expander(f"{icon} {check.title}｜{check.status.label}"):
            st.write(check.summary)
            for detail in check.details:
                st.write(f"- {detail}")

    report_started = time.perf_counter()
    report = generate_diagnostic_report(
        snapshot,
        project_root=Path(__file__).resolve().parents[1],
    )
    report_seconds = time.perf_counter() - report_started
    st.download_button(
        "下载脱敏诊断报告（Markdown）",
        data=report.encode("utf-8"),
        file_name=(
            "engineering-kb-diagnostic-"
            f"{snapshot.generated_at.strftime('%Y%m%d-%H%M%S')}.md"
        ),
        mime="text/markdown",
        use_container_width=True,
    )
    st.caption(
        f"报告生成耗时：{report_seconds:.3f} 秒。报告不包含 PDF、Markdown、笔记、证据正文、"
        "环境变量值、API Key、代理凭据或完整用户目录。"
    )
else:
    st.info("尚未运行诊断。点击上方按钮检查数据库、文件、路径、磁盘、监听地址和最近备份。")

with st.expander("备份格式说明"):
    st.code(
        """<backup>/
├── manifest.json
├── config/settings.json
└── data/
    ├── database/knowledge.db
    ├── raw/
    ├── pages/
    └── markdown/"""
    )
    st.write(
        "日志、Python 缓存、测试数据库、浏览器验收目录、临时文件、Git 目录、SQLite WAL/SHM "
        "和无法用于恢复的运行时缓存不会纳入完整备份。"
    )
