"""Read-only diagnostics and privacy-preserving support reports."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from src.backup_service import (
    BackupError,
    DatabaseSummary,
    _is_link_like,
    list_backup_candidates,
    read_database_summary,
    validate_backup,
)
from src.migrations import SCHEMA_VERSION

_LOW_DISK_BYTES: Final[int] = 1024 * 1024 * 1024
_ISOLATION_PORTS: Final[tuple[int, ...]] = (*range(8502, 8510), 8511, 8512)
_LOG_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[^ ]*\s+[^ ]+)\s+"
    r"(?P<level>WARNING|ERROR|CRITICAL)\s+(?P<logger>[^:]+):"
)


class DiagnosticStatus(StrEnum):
    """Severity used by every independent diagnostic check."""

    NORMAL = "normal"
    WARNING = "warning"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            self.NORMAL: "正常",
            self.WARNING: "警告",
            self.ERROR: "错误",
        }[self]


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One read-only check and privacy-safe issue details."""

    key: str
    title: str
    status: DiagnosticStatus
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackupDiagnostic:
    """Latest backup information retained by the diagnostic snapshot."""

    path: Path
    created_at: str
    valid: bool
    validation_seconds: float
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    """Complete read-only state used by the UI and redacted report."""

    generated_at: datetime
    app_version: str
    schema_version: int
    python_version: str
    operating_system: str
    database_path: Path
    data_dir: Path
    logs_dir: Path
    service_address: str
    database_summary: DatabaseSummary | None
    evidence_count: int
    disk_free_bytes: int
    latest_backup: BackupDiagnostic | None
    checks: tuple[DiagnosticCheck, ...]
    import_failure_count: int
    latest_import_failure_at: str | None
    log_events: tuple[str, ...]
    git_revision: str | None
    duration_seconds: float

    @property
    def overall_status(self) -> DiagnosticStatus:
        """Return the highest check severity."""

        statuses = {check.status for check in self.checks}
        if DiagnosticStatus.ERROR in statuses:
            return DiagnosticStatus.ERROR
        if DiagnosticStatus.WARNING in statuses:
            return DiagnosticStatus.WARNING
        return DiagnosticStatus.NORMAL


class DiagnosticService:
    """Inspect formal local data without repairing or writing it."""

    def __init__(
        self,
        *,
        app_version: str,
        project_root: Path,
        data_dir: Path,
        raw_dir: Path,
        pages_dir: Path,
        markdown_dir: Path,
        database_path: Path,
        backups_dir: Path,
        logs_dir: Path,
        log_path: Path,
        host: str,
        port: int,
        disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
        access_check: Callable[[Path, int], bool] = os.access,
        listener_addresses: Callable[[int], tuple[str, ...]] | None = None,
        port_is_open: Callable[[int], bool] | None = None,
        health_check: Callable[[int], bool] | None = None,
        low_disk_bytes: int = _LOW_DISK_BYTES,
    ) -> None:
        self.app_version = app_version.removeprefix("v")
        self.project_root = project_root.resolve(strict=False)
        self.data_dir = data_dir.resolve(strict=False)
        self.raw_dir = raw_dir.resolve(strict=False)
        self.pages_dir = pages_dir.resolve(strict=False)
        self.markdown_dir = markdown_dir.resolve(strict=False)
        self.database_path = database_path.resolve(strict=False)
        self.backups_dir = backups_dir.resolve(strict=False)
        self.logs_dir = logs_dir.resolve(strict=False)
        self.log_path = log_path.resolve(strict=False)
        self.host = host
        self.port = int(port)
        self.disk_usage = disk_usage
        self.access_check = access_check
        self.listener_addresses = listener_addresses or listener_addresses_for_port
        self.port_is_open = port_is_open or is_port_open
        self.health_check = health_check or is_healthy
        self.low_disk_bytes = int(low_disk_bytes)

    def run(self) -> DiagnosticSnapshot:
        """Run all checks read-only and return a reusable snapshot."""

        started = time.perf_counter()
        checks: list[DiagnosticCheck] = []
        summary: DatabaseSummary | None = None
        evidence_count = 0
        import_failure_count = 0
        latest_import_failure_at: str | None = None
        document_paths: tuple[str, ...] = ()
        page_paths: tuple[tuple[str, str | None, str], ...] = ()

        try:
            summary = read_database_summary(self.database_path)
        except BackupError as exc:
            checks.append(
                DiagnosticCheck(
                    "database_integrity",
                    "数据库完整性",
                    DiagnosticStatus.ERROR,
                    str(exc),
                )
            )
            checks.append(
                DiagnosticCheck(
                    "foreign_keys",
                    "外键完整性",
                    DiagnosticStatus.ERROR,
                    "数据库不可读，无法执行外键检查。",
                )
            )
        else:
            integrity_status = (
                DiagnosticStatus.NORMAL
                if summary.integrity_check == "ok"
                else DiagnosticStatus.ERROR
            )
            checks.append(
                DiagnosticCheck(
                    "database_integrity",
                    "数据库完整性",
                    integrity_status,
                    f"PRAGMA integrity_check：{summary.integrity_check}",
                )
            )
            foreign_status = (
                DiagnosticStatus.NORMAL
                if summary.foreign_key_violations == 0
                else DiagnosticStatus.ERROR
            )
            checks.append(
                DiagnosticCheck(
                    "foreign_keys",
                    "外键完整性",
                    foreign_status,
                    f"外键违规 {summary.foreign_key_violations} 条。",
                )
            )
            evidence_count = summary.evidence
            try:
                (
                    document_paths,
                    page_paths,
                    count_issues,
                    import_failure_count,
                    latest_import_failure_at,
                ) = self._database_details()
            except sqlite3.Error as exc:
                checks.append(
                    DiagnosticCheck(
                        "count_consistency",
                        "数据数量一致性",
                        DiagnosticStatus.ERROR,
                        f"无法读取统计明细：{type(exc).__name__}",
                    )
                )
            else:
                if summary.schema_version != SCHEMA_VERSION:
                    count_issues = (
                        *count_issues,
                        f"schema 为 v{summary.schema_version}，程序要求 v{SCHEMA_VERSION}",
                    )
                if summary.fts != summary.pages:
                    count_issues = (
                        *count_issues,
                        f"FTS {summary.fts} 条与页面 {summary.pages} 条不一致",
                    )
                checks.append(
                    DiagnosticCheck(
                        "count_consistency",
                        "数据数量一致性",
                        DiagnosticStatus.NORMAL
                        if not count_issues
                        else DiagnosticStatus.ERROR,
                        (
                            f"文档 {summary.documents}、页面 {summary.pages}、"
                            f"FTS {summary.fts}、证据 {summary.evidence}。"
                            if not count_issues
                            else f"发现 {len(count_issues)} 项数量异常。"
                        ),
                        count_issues,
                    )
                )

        pdf_missing = self._missing_paths(document_paths, self.raw_dir, "raw")
        checks.append(_file_check("pdf_files", "原始 PDF", pdf_missing, "原始 PDF"))
        image_paths = tuple(row[0] for row in page_paths)
        png_missing = self._missing_paths(image_paths, self.pages_dir, "pages")
        checks.append(_file_check("page_images", "页面 PNG", png_missing, "页面 PNG"))
        markdown_paths = tuple(row[1] for row in page_paths if row[1])
        markdown_missing = list(
            self._missing_paths(markdown_paths, self.markdown_dir, "markdown")
        )
        markdown_missing.extend(
            "数据库页面记录存在笔记正文但未记录 Markdown 路径"
            for _, markdown_path, markdown_content in page_paths
            if markdown_content.strip() and not markdown_path
        )
        checks.append(
            _file_check(
                "markdown_files",
                "页面 Markdown",
                tuple(markdown_missing),
                "页面 Markdown",
            )
        )

        orphan_details, link_details = self._orphan_files(
            document_paths=document_paths,
            image_paths=image_paths,
            markdown_paths=markdown_paths,
        )
        if link_details:
            orphan_details = (*orphan_details, *link_details)
            orphan_status = DiagnosticStatus.ERROR
        elif orphan_details:
            orphan_status = DiagnosticStatus.WARNING
        else:
            orphan_status = DiagnosticStatus.NORMAL
        checks.append(
            DiagnosticCheck(
                "orphan_files",
                "孤立文件与符号链接",
                orphan_status,
                "未发现明显孤立文件或符号链接。"
                if not orphan_details
                else f"发现 {len(orphan_details)} 个需人工核对的文件项。",
                orphan_details,
            )
        )

        path_details = self._path_configuration_issues()
        checks.append(
            DiagnosticCheck(
                "configuration_paths",
                "关键配置路径",
                DiagnosticStatus.NORMAL if not path_details else DiagnosticStatus.ERROR,
                "关键配置路径有效且位于正式 data 目录内。"
                if not path_details
                else f"发现 {len(path_details)} 项路径配置错误。",
                path_details,
            )
        )

        access_details = tuple(
            _relative_or_token(path, self.data_dir)
            for path in (
                self.data_dir,
                self.raw_dir,
                self.pages_dir,
                self.markdown_dir,
                self.database_path.parent,
            )
            if not path.exists()
            or not self.access_check(path, os.R_OK)
            or not self.access_check(path, os.W_OK)
        )
        checks.append(
            DiagnosticCheck(
                "data_access",
                "正式数据目录读写权限",
                DiagnosticStatus.NORMAL if not access_details else DiagnosticStatus.ERROR,
                "关键数据目录可读写（未创建探测文件）。"
                if not access_details
                else f"有 {len(access_details)} 个关键路径不可读写。",
                access_details,
            )
        )

        try:
            disk_free = int(self.disk_usage(self.data_dir).free)
        except OSError:
            disk_free = 0
        disk_status = (
            DiagnosticStatus.ERROR
            if disk_free <= 0
            else DiagnosticStatus.WARNING
            if disk_free < self.low_disk_bytes
            else DiagnosticStatus.NORMAL
        )
        checks.append(
            DiagnosticCheck(
                "disk_space",
                "可用磁盘空间",
                disk_status,
                f"可用空间 {_format_bytes(disk_free)}；"
                f"告警阈值 {_format_bytes(self.low_disk_bytes)}。",
            )
        )

        addresses = self.listener_addresses(self.port)
        healthy = self.health_check(self.port)
        bad_addresses = tuple(address for address in addresses if address != "127.0.0.1")
        if self.host != "127.0.0.1" or bad_addresses:
            listener_status = DiagnosticStatus.ERROR
            listener_summary = "监听地址不是严格的 127.0.0.1。"
        elif not healthy or "127.0.0.1" not in addresses:
            listener_status = DiagnosticStatus.WARNING
            listener_summary = "配置为 127.0.0.1，但未确认正式健康监听。"
        else:
            listener_status = DiagnosticStatus.NORMAL
            listener_summary = f"正式服务健康监听 127.0.0.1:{self.port}。"
        checks.append(
            DiagnosticCheck(
                "service_listener",
                "正式服务与监听",
                listener_status,
                listener_summary,
                tuple(f"监听地址：{address}:{self.port}" for address in bad_addresses),
            )
        )

        residual_ports = tuple(
            port for port in _ISOLATION_PORTS if port != self.port and self.port_is_open(port)
        )
        checks.append(
            DiagnosticCheck(
                "isolation_ports",
                "隔离验收服务",
                DiagnosticStatus.NORMAL
                if not residual_ports
                else DiagnosticStatus.WARNING,
                "未发现常用隔离验收端口残留。"
                if not residual_ports
                else f"发现 {len(residual_ports)} 个常用隔离端口仍开放。",
                tuple(f"127.0.0.1:{port}" for port in residual_ports),
            )
        )

        latest_backup, backup_check = self._latest_backup_check()
        checks.append(backup_check)
        log_events = read_redacted_log_events(self.logs_dir)
        git_revision = safe_git_revision(self.project_root)
        return DiagnosticSnapshot(
            generated_at=datetime.now(UTC),
            app_version=self.app_version,
            schema_version=summary.schema_version if summary else 0,
            python_version=platform.python_version(),
            operating_system=f"{platform.system()} {platform.release()} ({platform.machine()})",
            database_path=self.database_path,
            data_dir=self.data_dir,
            logs_dir=self.logs_dir,
            service_address=f"{self.host}:{self.port}",
            database_summary=summary,
            evidence_count=evidence_count,
            disk_free_bytes=disk_free,
            latest_backup=latest_backup,
            checks=tuple(checks),
            import_failure_count=import_failure_count,
            latest_import_failure_at=latest_import_failure_at,
            log_events=log_events,
            git_revision=git_revision,
            duration_seconds=time.perf_counter() - started,
        )

    def _database_details(
        self,
    ) -> tuple[
        tuple[str, ...],
        tuple[tuple[str, str | None, str], ...],
        tuple[str, ...],
        int,
        str | None,
    ]:
        uri = f"file:{self.database_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            documents = tuple(
                str(row[0])
                for row in connection.execute("SELECT source_path FROM documents")
            )
            pages = tuple(
                (str(row[0]), str(row[1]) if row[1] else None, str(row[2]))
                for row in connection.execute(
                    "SELECT image_path, markdown_path, markdown_content FROM pages"
                )
            )
            mismatch_rows = connection.execute(
                """
                SELECT d.id, d.page_count, COUNT(p.id)
                FROM documents d LEFT JOIN pages p ON p.document_id = d.id
                GROUP BY d.id HAVING d.page_count != COUNT(p.id)
                """
            ).fetchall()
            orphan_fts = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM page_search f
                    LEFT JOIN pages p ON p.id = f.rowid WHERE p.id IS NULL
                    """
                ).fetchone()[0]
            )
            missing_fts = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM pages p
                    LEFT JOIN page_search f ON f.rowid = p.id WHERE f.rowid IS NULL
                    """
                ).fetchone()[0]
            )
            failed = connection.execute(
                """
                SELECT COUNT(*), MAX(started_at) FROM import_records
                WHERE status IN ('failed', 'partially_completed')
                """
            ).fetchone()
        count_issues = tuple(
            f"文档记录 {document_id} 声明 {declared} 页，实际 {actual} 页"
            for document_id, declared, actual in mismatch_rows
        )
        if orphan_fts:
            count_issues = (*count_issues, f"FTS 存在 {orphan_fts} 条孤立记录")
        if missing_fts:
            count_issues = (*count_issues, f"有 {missing_fts} 个页面缺少 FTS 记录")
        return documents, pages, count_issues, int(failed[0]), failed[1]

    def _missing_paths(
        self, values: Iterable[str], expected_root: Path, label: str
    ) -> tuple[str, ...]:
        missing: list[str] = []
        for raw_value in values:
            value = Path(raw_value)
            if not value.is_absolute():
                value = self.project_root / value
            if _is_link_like(value):
                missing.append(f"{label}/{value.name}（符号链接或重解析点）")
                continue
            resolved = value.resolve(strict=False)
            try:
                relative = resolved.relative_to(expected_root)
            except ValueError:
                missing.append(f"<{label.upper()}_OUTSIDE>/{value.name}")
                continue
            if not resolved.is_file() or resolved.is_symlink():
                missing.append(f"{label}/{relative.as_posix()}")
        return tuple(sorted(set(missing)))

    def _orphan_files(
        self,
        *,
        document_paths: Iterable[str],
        image_paths: Iterable[str],
        markdown_paths: Iterable[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        referenced = {
            _normalized_recorded_path(value, self.project_root)
            for value in (*document_paths, *image_paths, *markdown_paths)
        }
        orphaned: list[str] = []
        links: list[str] = []
        for label, root in (
            ("raw", self.raw_dir),
            ("pages", self.pages_dir),
            ("markdown", self.markdown_dir),
        ):
            if _is_link_like(root):
                links.append(f"符号链接或重解析点：{label}/")
                continue
            for path, is_link in _walk_diagnostic_files(root):
                relative = f"{label}/{path.relative_to(root).as_posix()}"
                if is_link:
                    links.append(f"符号链接或重解析点：{relative}")
                elif path.name != ".gitkeep" and path.resolve(strict=False) not in referenced:
                    orphaned.append(relative)
        return tuple(sorted(orphaned)), tuple(sorted(links))

    def _path_configuration_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        for label, path in (
            ("raw", self.raw_dir),
            ("pages", self.pages_dir),
            ("markdown", self.markdown_dir),
            ("database", self.database_path),
        ):
            try:
                path.relative_to(self.data_dir)
            except ValueError:
                issues.append(f"{label} 路径不在正式 data 目录内")
        if self.database_path.name != "knowledge.db":
            issues.append("正式数据库文件名不是 knowledge.db")
        return tuple(issues)

    def _latest_backup_check(
        self,
    ) -> tuple[BackupDiagnostic | None, DiagnosticCheck]:
        candidates = list_backup_candidates(self.backups_dir)
        if not candidates:
            return None, DiagnosticCheck(
                "latest_backup",
                "最近完整备份",
                DiagnosticStatus.WARNING,
                "尚未创建 v0.0.8 完整备份。",
            )
        latest = candidates[0]
        validation = validate_backup(
            latest,
            expected_app_version=self.app_version,
            expected_schema_version=SCHEMA_VERSION,
        )
        created_at = "未知"
        if validation.manifest is not None:
            created_at = str(validation.manifest.get("created_at", "未知"))
        diagnostic = BackupDiagnostic(
            path=latest,
            created_at=created_at,
            valid=validation.valid,
            validation_seconds=validation.duration_seconds,
            errors=validation.errors,
        )
        if validation.valid:
            check = DiagnosticCheck(
                "latest_backup",
                "最近完整备份",
                DiagnosticStatus.NORMAL,
                f"最近备份完整，创建于 {created_at}。",
            )
        else:
            check = DiagnosticCheck(
                "latest_backup",
                "最近完整备份",
                DiagnosticStatus.ERROR,
                "最近备份验证失败，不可用于恢复。",
                tuple(_redact_free_text(error) for error in validation.errors),
            )
        return diagnostic, check


def generate_diagnostic_report(
    snapshot: DiagnosticSnapshot,
    *,
    project_root: Path,
    home_dir: Path | None = None,
) -> str:
    """Generate Markdown without document, note, evidence, secret, or home-path data."""

    started = time.perf_counter()
    del started  # The caller measures generation time around this pure function.
    home = (home_dir or Path.home()).resolve(strict=False)
    root = project_root.resolve(strict=False)
    summary = snapshot.database_summary
    lines = [
        "# 工程知识库脱敏诊断报告",
        "",
        f"- 报告生成时间：{snapshot.generated_at.isoformat(timespec='seconds')}",
        f"- 应用版本：v{snapshot.app_version}",
        f"- schema 版本：v{snapshot.schema_version}",
        f"- Python：{snapshot.python_version}",
        f"- 操作系统：{snapshot.operating_system}",
        f"- 数据库：{redact_path(snapshot.database_path, root, home)}",
        f"- 正式数据目录：{redact_path(snapshot.data_dir, root, home)}",
        f"- 日志目录：{redact_path(snapshot.logs_dir, root, home)}",
        f"- 服务地址：{snapshot.service_address}",
        f"- 诊断结果：{snapshot.overall_status.label}",
        "",
        "## 正式数据统计",
        "",
    ]
    if summary is None:
        lines.append("数据库不可读，无法取得正式统计。")
    else:
        lines.extend(
            [
                f"- 文档：{summary.documents}",
                f"- 页面：{summary.pages}",
                f"- FTS：{summary.fts}",
                f"- 证据：{summary.evidence}",
                f"- 项目：{summary.projects}",
                f"- 标签：{summary.tags}",
                f"- 数据库完整性：{summary.integrity_check}",
                f"- 外键违规：{summary.foreign_key_violations}",
            ]
        )
    lines.extend(["", "## 诊断检查", ""])
    for check in snapshot.checks:
        lines.append(f"- [{check.status.label}] {check.title}：{_redact_free_text(check.summary)}")
        for detail in check.details[:20]:
            lines.append(f"  - {_redact_free_text(detail)}")
    lines.extend(["", "## 最近备份", ""])
    if snapshot.latest_backup is None:
        lines.append("- 尚无完整备份。")
    else:
        lines.extend(
            [
                f"- 创建时间：{snapshot.latest_backup.created_at}",
                f"- 状态：{'完整' if snapshot.latest_backup.valid else '验证失败'}",
                f"- 路径：{redact_path(snapshot.latest_backup.path, root, home)}",
            ]
        )
    lines.extend(
        [
            "",
            "## 最近导入失败摘要",
            "",
            f"- 失败或部分完成记录：{snapshot.import_failure_count}",
            f"- 最近时间：{snapshot.latest_import_failure_at or '无'}",
            "- 文件名、标题、正文和错误正文均未导出。",
            "",
            "## 最近日志警告与错误摘要",
            "",
        ]
    )
    if snapshot.log_events:
        lines.extend(f"- {event}" for event in snapshot.log_events)
    else:
        lines.append("- 未发现可安全摘要的 WARNING / ERROR / CRITICAL 事件。")
    lines.extend(
        [
            "",
            "## 运行版本",
            "",
            f"- Git 提交：{snapshot.git_revision or '不可用'}",
            f"- 诊断耗时：{snapshot.duration_seconds:.3f} 秒",
            "",
            "> 本报告默认脱敏，不包含 PDF、Markdown、笔记、证据正文、环境变量值、"
            "API Key、代理凭据或完整用户目录。",
            "",
        ]
    )
    return "\n".join(lines)


def redact_path(path: Path, project_root: Path, home_dir: Path) -> str:
    """Replace project and user-home prefixes with stable placeholders."""

    resolved = path.resolve(strict=False)
    for base, token in (
        (project_root.resolve(strict=False), "<PROJECT_ROOT>"),
        (home_dir.resolve(strict=False), "<USER_HOME>"),
    ):
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            continue
        suffix = relative.as_posix()
        return token if not suffix else f"{token}/{suffix}"
    if resolved.is_absolute():
        return f"<ABSOLUTE_PATH>/{resolved.name}"
    return resolved.as_posix()


def read_redacted_log_events(logs_dir: Path, limit: int = 20) -> tuple[str, ...]:
    """Return only timestamp, severity, and logger—not private log messages."""

    if not logs_dir.is_dir() or logs_dir.is_symlink():
        return ()
    events: list[str] = []
    candidates = sorted(
        (
            path
            for path in logs_dir.iterdir()
            if path.is_file() and not path.is_symlink() and ".log" in path.name
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in candidates[:8]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines[-500:]):
            match = _LOG_PATTERN.match(line)
            if match:
                events.append(
                    f"{match.group('timestamp')} {match.group('level')} "
                    f"{match.group('logger')}：<详情已脱敏>"
                )
                if len(events) >= limit:
                    return tuple(events)
    return tuple(events)


def safe_git_revision(project_root: Path) -> str | None:
    """Return only a commit hash, never repository paths or environment data."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        return revision.lower()
    return None


def listener_addresses_for_port(port: int) -> tuple[str, ...]:
    """Read TCP listener addresses using the platform's local netstat command."""

    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    addresses: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0].upper() != "TCP":
            continue
        local = fields[1]
        state = fields[3].upper() if len(fields) >= 4 else ""
        if state != "LISTENING":
            continue
        address, separator, raw_port = local.rpartition(":")
        if not separator or raw_port != str(port):
            continue
        addresses.add(address.strip("[]"))
    return tuple(sorted(addresses))


def is_healthy(port: int, timeout: float = 1.0) -> bool:
    """Check only Streamlit's privacy-safe loopback health endpoint."""

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/_stcore/health", timeout=timeout
        ) as response:
            return response.status == 200 and response.read(32).strip() == b"ok"
    except (OSError, urllib.error.URLError):
        return False


def is_port_open(port: int) -> bool:
    """Return whether a loopback TCP port accepts a connection."""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _file_check(
    key: str, title: str, missing: tuple[str, ...], label: str
) -> DiagnosticCheck:
    return DiagnosticCheck(
        key,
        title,
        DiagnosticStatus.NORMAL if not missing else DiagnosticStatus.ERROR,
        f"数据库引用的{label}均存在。"
        if not missing
        else f"数据库引用中缺少 {len(missing)} 个{label}。",
        missing,
    )


def _walk_diagnostic_files(root: Path) -> Iterable[tuple[Path, bool]]:
    if not root.is_dir() or _is_link_like(root):
        return
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in list(directories):
            candidate = current_path / directory
            if _is_link_like(candidate):
                directories.remove(directory)
                yield candidate, True
        for filename in filenames:
            candidate = current_path / filename
            yield candidate, _is_link_like(candidate)


def _normalized_recorded_path(value: str, project_root: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve(strict=False)


def _relative_or_token(path: Path, data_dir: Path) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(data_dir.resolve(strict=False))
    except ValueError:
        return f"<OUTSIDE_DATA>/{path.name}"
    return "data" if not relative.parts else f"data/{relative.as_posix()}"


def _redact_free_text(value: str) -> str:
    """Remove obvious credential-like assignments from already bounded summaries."""

    sanitized = re.sub(
        r"(?i)(api[_-]?key|password|passwd|proxy[_-]?(?:user|password)|token)\s*[=:]\s*\S+",
        r"\1=<REDACTED>",
        value,
    )
    return sanitized.replace("\x00", "")[:1000]


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"
