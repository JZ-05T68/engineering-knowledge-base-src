"""Unified v0.5.2 release-readiness checks with clear process exit status."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backup_service import (  # noqa: E402
    BackupError,
    BackupService,
    read_database_summary,
    validate_backup,
)
from src.config import (  # noqa: E402
    OFFICIAL_HOST,
    OFFICIAL_PORT,
    OfficialEndpointError,
    Settings,
    get_settings,
)
from src.diagnostic_service import (  # noqa: E402
    is_healthy,
    is_port_open,
    listener_addresses_for_port,
)
from src.migrations import SCHEMA_VERSION  # noqa: E402

EXPECTED_VERSION: Final[str] = "0.5.2"
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bv\d+\.\d+\.\d+\b")
ISOLATION_PORTS: Final[tuple[int, ...]] = tuple(range(8502, 8513))
_RUNTIME_ARTIFACT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:^|/)(?:browser[-_]?acceptance|acceptance[-_]?artifacts?|test[-_]?data)(?:/|$)"
    r"|(?:\.tmp$|\.partial$|\.incomplete$|test[^/]*\.db$)"
)


class CheckStatus(StrEnum):
    """Release-check outcome severity."""

    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One release criterion and its concise result."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    """Complete release-readiness outcome."""

    results: tuple[CheckResult, ...]
    test_count: int = 0
    application_version: str = EXPECTED_VERSION
    schema_version: int = SCHEMA_VERSION
    backup_path: Path | None = None
    duration_seconds: float = 0.0

    @property
    def readiness(self) -> CheckStatus:
        if any(result.status is CheckStatus.FAIL for result in self.results):
            return CheckStatus.FAIL
        return CheckStatus.PASS

    @property
    def exit_code(self) -> int:
        return 1 if self.readiness is CheckStatus.FAIL else 0


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Captured local command result."""

    returncode: int
    output: str


class ReleaseChecker:
    """Run release criteria without modifying formal data except one verified backup."""

    def __init__(self, settings: Settings, project_root: Path = PROJECT_ROOT) -> None:
        self.settings = settings
        self.project_root = project_root.resolve(strict=False)

    def run(
        self,
        *,
        create_backup: bool = True,
        existing_backup: Path | None = None,
        expect_service_stopped: bool = False,
    ) -> ReleaseReport:
        """Run all checks and return a non-ambiguous report."""

        if create_backup and existing_backup is not None:
            raise ValueError("不能同时创建新备份和复验既有备份")

        started = time.perf_counter()
        results: list[CheckResult] = []
        results.append(
            version_consistency_check(
                self.project_root,
                app_version=self.settings.app_version,
                app_title=self.settings.app_title,
            )
        )

        # Validate the formal service as a release preflight. The full test suite
        # exercises service lifecycle paths and may stop the process; release
        # readiness requires a successful startup and health check, not that the
        # same process remain alive after the long-running test phase.
        addresses = listener_addresses_for_port(self.settings.port)
        listener_result = (
            stopped_listener_check(
                self.settings.host,
                self.settings.port,
                addresses,
                is_healthy(self.settings.port),
            )
            if expect_service_stopped
            else listener_check(
                self.settings.host,
                self.settings.port,
                addresses,
                is_healthy(self.settings.port),
            )
        )
        results.append(listener_result)

        ruff = _run_command(
            [str(self._python()), "-m", "ruff", "check", "."],
            self.project_root,
            timeout=180,
        )
        results.append(
            CheckResult(
                "Ruff",
                CheckStatus.PASS if ruff.returncode == 0 else CheckStatus.FAIL,
                "通过" if ruff.returncode == 0 else _last_output(ruff.output),
            )
        )

        collection = _run_command(
            [str(self._python()), "-m", "pytest", "--collect-only", "-q"],
            self.project_root,
            timeout=180,
        )
        collected = parse_collected_test_count(collection.output)
        collection_passed = collection.returncode == 0 and collected > 0
        results.append(
            CheckResult(
                "Pytest collection",
                CheckStatus.PASS if collection_passed else CheckStatus.FAIL,
                f"收集 {collected} 项" if collection_passed else _last_output(collection.output),
            )
        )

        pytest = _run_command(
            [str(self._python()), "-m", "pytest", "-q"],
            self.project_root,
            timeout=1_200,
        )
        passed = successful_test_count(
            pytest.output,
            returncode=pytest.returncode,
            collected=collected,
        )
        skipped = parse_skipped_test_count(pytest.output)
        tests_completed = (
            pytest.returncode == 0
            and passed > 0
            and passed + skipped == collected
        )
        test_status = (
            CheckStatus.PASS
            if tests_completed and skipped == 0
            else CheckStatus.WARNING
            if tests_completed
            else CheckStatus.FAIL
        )
        results.append(
            CheckResult(
                "Pytest",
                test_status,
                f"{passed} passed"
                if tests_completed and skipped == 0
                else f"{passed} passed, {skipped} skipped"
                if tests_completed
                else f"通过 {passed}/{collected}；{_last_output(pytest.output)}",
            )
        )

        try:
            database = read_database_summary(self.settings.database_path)
        except BackupError as exc:
            results.extend(
                (
                    CheckResult("Database integrity", CheckStatus.FAIL, str(exc)),
                    CheckResult("Foreign keys", CheckStatus.FAIL, "数据库不可读"),
                    CheckResult("Formal counts", CheckStatus.FAIL, "数据库不可读"),
                )
            )
            schema_version = 0
        else:
            schema_version = database.schema_version
            results.append(
                CheckResult(
                    "Database integrity",
                    CheckStatus.PASS
                    if database.integrity_check == "ok"
                    else CheckStatus.FAIL,
                    database.integrity_check,
                )
            )
            results.append(
                CheckResult(
                    "Foreign keys",
                    CheckStatus.PASS
                    if database.foreign_key_violations == 0
                    else CheckStatus.FAIL,
                    f"{database.foreign_key_violations} violations",
                )
            )
            count_ok = database.pages == database.fts and schema_version == SCHEMA_VERSION
            results.append(
                CheckResult(
                    "Formal counts",
                    CheckStatus.PASS if count_ok else CheckStatus.FAIL,
                    f"documents={database.documents}, pages={database.pages}, "
                    f"fts={database.fts}, evidence={database.evidence}",
                )
            )
            results.append(schema_v7_invariants_check(self.settings.database_path))
            results.append(schema_v8_invariants_check(self.settings.database_path))

        results.append(data_pollution_check(self.settings.data_dir))

        residual = tuple(
            port
            for port in ISOLATION_PORTS
            if port != self.settings.port and is_port_open(port)
        )
        results.append(
            CheckResult(
                "Isolation ports",
                CheckStatus.PASS if not residual else CheckStatus.FAIL,
                "全部关闭"
                if not residual
                else "仍开放：" + ", ".join(str(port) for port in residual),
            )
        )

        git_status = _git_lines(
            self.project_root,
            ["status", "--porcelain=v1", "--untracked-files=all"],
        )
        results.append(git_workspace_check(git_status))
        staged = _git_lines(self.project_root, ["diff", "--cached", "--name-only"])
        results.append(agents_staging_check(staged))
        results.append(untracked_artifact_check(git_status))
        results.append(self._path_access_check())

        backup_path: Path | None = None
        if existing_backup is not None:
            backup_result = self._existing_backup_check(existing_backup)
            results.append(backup_result[0])
            backup_path = backup_result[1]
        elif create_backup:
            backup_result = self._backup_check()
            results.append(backup_result[0])
            backup_path = backup_result[1]
        else:
            results.append(
                CheckResult(
                    "Backup verification",
                    CheckStatus.WARNING,
                    "按命令参数跳过；正式发布不得跳过",
                )
            )

        return ReleaseReport(
            results=tuple(results),
            test_count=passed,
            application_version=self.settings.app_version,
            schema_version=schema_version,
            backup_path=backup_path,
            duration_seconds=time.perf_counter() - started,
        )

    def _python(self) -> Path:
        candidate = self.project_root / ".venv" / "Scripts" / "python.exe"
        return candidate if candidate.is_file() else Path(sys.executable)

    def _path_access_check(self) -> CheckResult:
        paths = (
            self.settings.data_dir,
            self.settings.raw_dir,
            self.settings.pages_dir,
            self.settings.markdown_dir,
            self.settings.database_dir,
            self.settings.backups_dir,
            self.settings.logs_dir,
        )
        failed: list[str] = []
        for path in paths:
            if not path.is_dir() or not os.access(path, os.R_OK | os.W_OK):
                failed.append(path.name)
                continue
            probe = path / f".release-write-probe-{uuid.uuid4().hex}"
            try:
                probe.write_bytes(b"")
                probe.unlink()
            except OSError:
                failed.append(path.name)
                probe.unlink(missing_ok=True)
        return CheckResult(
            "Critical paths",
            CheckStatus.PASS if not failed else CheckStatus.FAIL,
            "关键目录可读写" if not failed else "不可读写：" + ", ".join(failed),
        )

    def _backup_check(self) -> tuple[CheckResult, Path | None]:
        service = BackupService(
            app_version=self.settings.app_version,
            data_dir=self.settings.data_dir,
            raw_dir=self.settings.raw_dir,
            pages_dir=self.settings.pages_dir,
            markdown_dir=self.settings.markdown_dir,
            database_path=self.settings.database_path,
            backups_dir=self.settings.backups_dir,
            host=self.settings.host,
            port=self.settings.port,
            minimum_text_length=self.settings.minimum_text_length,
            pdf_render_dpi=self.settings.pdf_render_dpi,
        )
        try:
            backup = service.create_backup(
                backup_name=(
                    f"release-v{self.settings.app_version}-"
                    f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
                )
            )
            validation = validate_backup(
                backup.backup_path,
                expected_app_version=self.settings.app_version,
                expected_schema_version=SCHEMA_VERSION,
            )
        except BackupError as exc:
            return CheckResult("Backup verification", CheckStatus.FAIL, str(exc)), None
        if not validation.valid:
            return (
                CheckResult(
                    "Backup verification",
                    CheckStatus.FAIL,
                    "；".join(validation.errors),
                ),
                backup.backup_path,
            )
        return (
            CheckResult(
                "Backup verification",
                CheckStatus.PASS,
                f"创建并复验通过（{backup.creation_seconds:.3f}s）",
            ),
            backup.backup_path,
        )

    def _existing_backup_check(
        self, backup_path: Path
    ) -> tuple[CheckResult, Path | None]:
        """Revalidate one already-created formal release backup without copying data."""

        resolved = backup_path.resolve(strict=False)
        expected_prefix = f"release-v{self.settings.app_version}-"
        if not resolved.name.startswith(expected_prefix):
            return (
                CheckResult(
                    "Backup verification",
                    CheckStatus.FAIL,
                    f"正式发布备份目录必须以 {expected_prefix} 开头",
                ),
                None,
            )
        validation = validate_backup(
            resolved,
            expected_app_version=self.settings.app_version,
            expected_schema_version=SCHEMA_VERSION,
        )
        if not validation.valid:
            return (
                CheckResult(
                    "Backup verification",
                    CheckStatus.FAIL,
                    "；".join(validation.errors),
                ),
                None,
            )
        return (
            CheckResult(
                "Backup verification",
                CheckStatus.PASS,
                f"既有正式发布备份复验通过（{validation.duration_seconds:.3f}s）",
            ),
            resolved,
        )


def version_consistency_check(
    project_root: Path, *, app_version: str, app_title: str
) -> CheckResult:
    """Check runtime, README, CHANGELOG, and every Streamlit page title."""

    expected = EXPECTED_VERSION
    issues: list[str] = []
    if app_version != expected:
        issues.append(f"配置版本为 {app_version}")
    if f"v{expected}" not in app_title:
        issues.append("应用标题版本不一致")
    readme = _safe_read(project_root / "README.md")
    changelog = _safe_read(project_root / "CHANGELOG.md")
    if not readme.startswith(f"# Engineering Knowledge Base v{expected}"):
        issues.append("README 标题版本不一致")
    if f"## v{expected}" not in changelog:
        issues.append("CHANGELOG 缺少当前版本章节")
    page_files = [project_root / "app.py", *sorted((project_root / "pages").glob("*.py"))]
    for page in page_files:
        content = _safe_read(page)
        display_text = "\n".join(
            line
            for line in content.splitlines()
            if "page_title=" in line or "st.title(" in line
        )
        display_versions = set(_VERSION_PATTERN.findall(display_text))
        if f"v{expected}" not in display_versions:
            issues.append(f"{page.name} 未包含当前页面版本")
        stale_versions = sorted(
            version
            for version in display_versions
            if version != f"v{expected}"
        )
        if stale_versions:
            issues.append(
                f"{page.name} 仍包含旧页面版本：{', '.join(stale_versions)}"
            )
    return CheckResult(
        "Version consistency",
        CheckStatus.PASS if not issues else CheckStatus.FAIL,
        f"v{expected} 全部一致" if not issues else "；".join(issues),
    )


def schema_v7_invariants_check(database_path: Path) -> CheckResult:
    """Read-only v0.5.0 structural invariants of the formal database."""

    try:
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as connection:
            evidence_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(evidence_items)")
            }
            preference_rows = connection.execute(
                "SELECT COUNT(*) FROM note_display_preferences"
            ).fetchone()[0]
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND tbl_name = 'evidence_items'"
                )
            }
            evidence_types = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT evidence_type FROM evidence_items"
                )
            }
            confirmation_states = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT confirmation_status FROM evidence_items"
                )
            }
    except (OSError, sqlite3.Error) as exc:
        return CheckResult("Schema v7 invariants", CheckStatus.FAIL, str(exc))
    issues: list[str] = []
    required_columns = {
        "evidence_type",
        "confirmation_status",
        "confirmed_at",
        "region_image_sha256",
        "region_image_width",
        "region_image_height",
        "region_x0",
        "region_y0",
        "region_x1",
        "region_y1",
    }
    missing_columns = sorted(required_columns - evidence_columns)
    if missing_columns:
        issues.append(f"evidence_items 缺少列：{missing_columns}")
    if preference_rows != 1:
        issues.append(f"note_display_preferences 行数为 {preference_rows}")
    required_indexes = {"idx_evidence_items_page", "idx_evidence_items_document"}
    missing_indexes = sorted(required_indexes - indexes)
    if missing_indexes:
        issues.append(f"evidence_items 缺少索引：{missing_indexes}")
    if not evidence_types <= {"page", "text_selection", "image_region"}:
        issues.append(f"存在非法 evidence_type 值：{sorted(evidence_types)}")
    if not confirmation_states <= {"unconfirmed", "confirmed"}:
        issues.append(
            f"存在非法 confirmation_status 值：{sorted(confirmation_states)}"
        )
    return CheckResult(
        "Schema v7 invariants",
        CheckStatus.PASS if not issues else CheckStatus.FAIL,
        "证据类型 / 区域锚点 / 人工确认 / 索引 全部在位"
        if not issues
        else "；".join(issues),
    )


def schema_v8_invariants_check(database_path: Path) -> CheckResult:
    """Read-only Phase 7 embedding-persistence invariants of the formal database."""

    try:
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as connection:
            table_row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                " AND name = 'page_embeddings'"
            ).fetchone()
            embedding_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(page_embeddings)")
            }
            foreign_keys = [
                (row[2], row[3], row[4], row[6])
                for row in connection.execute("PRAGMA foreign_key_list(page_embeddings)")
            ]
            unique_shapes = set()
            for index in connection.execute(
                "PRAGMA index_list(page_embeddings)"
            ).fetchall():
                if not int(index[2]):
                    continue
                unique_shapes.add(
                    tuple(
                        column[2]
                        for column in connection.execute(
                            f"PRAGMA index_info({index[1]})"
                        ).fetchall()
                    )
                )
    except (OSError, sqlite3.Error) as exc:
        return CheckResult("Schema v8 invariants", CheckStatus.FAIL, str(exc))
    issues: list[str] = []
    if table_row is None:
        issues.append("缺少 page_embeddings 表")
    required_columns = {
        "id",
        "page_id",
        "source_text_sha256",
        "model",
        "dimensions",
        "config_version",
        "vector",
        "created_at",
        "updated_at",
    }
    missing_columns = sorted(required_columns - embedding_columns)
    if missing_columns:
        issues.append(f"page_embeddings 缺少列：{missing_columns}")
    if ("pages", "page_id", "id", "CASCADE") not in foreign_keys:
        issues.append("page_embeddings 缺少 pages(id) ON DELETE CASCADE 外键")
    if ("page_id", "model", "dimensions", "config_version") not in unique_shapes:
        issues.append("page_embeddings 缺少配置唯一约束索引")
    return CheckResult(
        "Schema v8 invariants",
        CheckStatus.PASS if not issues else CheckStatus.FAIL,
        "page_embeddings 表 / 外键级联 / 配置唯一约束 全部在位"
        if not issues
        else "；".join(issues),
    )


def listener_check(
    configured_host: str,
    port: int,
    addresses: tuple[str, ...],
    healthy: bool,
) -> CheckResult:
    """Require the healthy, fixed formal endpoint on IPv4 loopback."""

    endpoint_ok = configured_host == OFFICIAL_HOST and port == OFFICIAL_PORT
    listener_ok = addresses == (OFFICIAL_HOST,) and healthy
    valid = endpoint_ok and listener_ok
    detail = (
        f"{OFFICIAL_HOST}:{OFFICIAL_PORT} / health ok"
        if valid
        else (
            f"正式端点必须为 {OFFICIAL_HOST}:{OFFICIAL_PORT}；"
            f"configured={configured_host}:{port}, listeners={addresses}, healthy={healthy}"
        )
    )
    return CheckResult(
        "Service listener", CheckStatus.PASS if valid else CheckStatus.FAIL, detail
    )


def stopped_listener_check(
    configured_host: str,
    port: int,
    addresses: tuple[str, ...],
    healthy: bool,
) -> CheckResult:
    """Require the formal endpoint configuration while the service is stopped."""

    endpoint_ok = configured_host == OFFICIAL_HOST and port == OFFICIAL_PORT
    stopped = not addresses and not healthy
    valid = endpoint_ok and stopped
    detail = (
        f"{OFFICIAL_HOST}:{OFFICIAL_PORT} 已停止，等待正式运行验收"
        if valid
        else (
            f"正式端点必须为 {OFFICIAL_HOST}:{OFFICIAL_PORT} 且发布收口阶段必须停止；"
            f"configured={configured_host}:{port}, listeners={addresses}, healthy={healthy}"
        )
    )
    return CheckResult(
        "Service stopped state", CheckStatus.PASS if valid else CheckStatus.FAIL, detail
    )


def agents_staging_check(staged_paths: list[str]) -> CheckResult:
    """Fail if the user-owned AGENTS.md change is staged."""

    staged_agents = [path for path in staged_paths if Path(path).name.casefold() == "agents.md"]
    return CheckResult(
        "AGENTS.md staging",
        CheckStatus.PASS if not staged_agents else CheckStatus.FAIL,
        "未暂存" if not staged_agents else "AGENTS.md 被误暂存",
    )


def git_workspace_check(status_lines: list[str]) -> CheckResult:
    """Allow only the known unstaged AGENTS.md user modification."""

    meaningful = [line for line in status_lines if line.strip()]
    non_agents = [
        line
        for line in meaningful
        if Path(line[3:].strip().strip('"')).name.casefold() != "agents.md"
    ]
    if non_agents:
        return CheckResult(
            "Git workspace",
            CheckStatus.FAIL,
            f"存在 {len(non_agents)} 项非 AGENTS.md 改动或未追踪文件",
        )
    if meaningful:
        return CheckResult(
            "Git workspace",
            CheckStatus.WARNING,
            "仅保留未暂存的 AGENTS.md 用户修改",
        )
    return CheckResult("Git workspace", CheckStatus.PASS, "工作区干净")


def untracked_artifact_check(status_lines: list[str]) -> CheckResult:
    """Reject visible browser/test/runtime acceptance artifacts."""

    untracked = [line[3:].strip().strip('"') for line in status_lines if line.startswith("?? ")]
    artifacts = [path for path in untracked if _RUNTIME_ARTIFACT_PATTERN.search(path)]
    return CheckResult(
        "Untracked runtime artifacts",
        CheckStatus.PASS if not artifacts else CheckStatus.FAIL,
        "未发现" if not artifacts else "发现：" + ", ".join(artifacts[:10]),
    )


def data_pollution_check(data_dir: Path) -> CheckResult:
    """Reject obvious test and browser artifacts inside formal data."""

    if not data_dir.is_dir():
        return CheckResult("Formal data pollution", CheckStatus.FAIL, "正式 data 目录不存在")
    suspicious: list[str] = []
    for path in data_dir.rglob("*"):
        if path.is_symlink():
            suspicious.append(path.relative_to(data_dir).as_posix())
            continue
        relative = path.relative_to(data_dir).as_posix()
        if _RUNTIME_ARTIFACT_PATTERN.search(relative):
            suspicious.append(relative)
    return CheckResult(
        "Formal data pollution",
        CheckStatus.PASS if not suspicious else CheckStatus.FAIL,
        "未发现测试、验收或临时产物"
        if not suspicious
        else "发现：" + ", ".join(suspicious[:10]),
    )


def parse_collected_test_count(output: str) -> int:
    """Parse both quiet per-file collection and standard pytest summaries."""

    summary = re.search(r"(\d+) tests? collected", output)
    if summary:
        return int(summary.group(1))
    return sum(
        int(match.group(1))
        for match in re.finditer(r"(?m)^.+\.py:\s*(\d+)\s*$", output)
    )


def parse_passed_test_count(output: str) -> int:
    match = re.search(r"(\d+) passed", output)
    return int(match.group(1)) if match else 0


def parse_skipped_test_count(output: str) -> int:
    """Parse the skipped count from the standard pytest summary."""

    match = re.search(r"(\d+) skipped", output)
    return int(match.group(1)) if match else 0


def successful_test_count(output: str, *, returncode: int, collected: int) -> int:
    """Resolve a passing count even when an extra-quiet pytest omits its summary."""

    parsed = parse_passed_test_count(output)
    if parsed:
        return parsed
    return collected if returncode == 0 and collected > 0 else 0


def render_report(report: ReleaseReport) -> str:
    """Render a concise PASS/WARNING/FAIL release summary."""

    lines = [f"[{result.status}] {result.name}: {result.detail}" for result in report.results]
    lines.extend(
        [
            "",
            f"Release readiness: {report.readiness}",
            f"Application: v{report.application_version}",
            f"Schema: v{report.schema_version}",
            f"Tests: {report.test_count} passed",
            f"Backup verification: {'passed' if report.backup_path else 'not completed'}",
            f"Duration: {report.duration_seconds:.3f}s",
        ]
    )
    return "\n".join(lines)


def _run_command(command: list[str], cwd: Path, *, timeout: int) -> CommandOutcome:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandOutcome(127, f"{type(exc).__name__}: {exc}")
    # Keep leading whitespace because Git porcelain uses it to distinguish an
    # unstaged modification (" M") from a staged modification ("M ").
    output = "\n".join(value for value in (result.stdout, result.stderr) if value).rstrip()
    return CommandOutcome(result.returncode, output)


def _git_lines(project_root: Path, arguments: list[str]) -> list[str]:
    outcome = _run_command(["git", *arguments], project_root, timeout=10)
    if outcome.returncode != 0:
        return [f"!! git command failed: {_last_output(outcome.output)}"]
    return [line for line in outcome.output.splitlines() if line.strip()]


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _last_output(value: str, maximum: int = 300) -> str:
    normalized = " ".join(value.split())
    return normalized[-maximum:] if normalized else "无输出"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="工程知识库 v0.5.2 统一发布检查")
    backup_group = parser.add_mutually_exclusive_group()
    backup_group.add_argument(
        "--skip-backup",
        action="store_true",
        help="仅供开发诊断；正式发布检查不得跳过经过验证的备份",
    )
    backup_group.add_argument(
        "--existing-backup",
        type=Path,
        help="复验指定的既有正式发布备份，不再创建重复备份",
    )
    parser.add_argument(
        "--expect-service-stopped",
        action="store_true",
        help="发布提交和 tag 收口阶段要求 127.0.0.1:8501 尚未监听",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except OfficialEndpointError as exc:
        print(f"[FAIL] Formal endpoint configuration: {exc}")
        return 2
    settings.ensure_directories()
    report = ReleaseChecker(settings).run(
        create_backup=not arguments.skip_backup and arguments.existing_backup is None,
        existing_backup=arguments.existing_backup,
        expect_service_stopped=arguments.expect_service_stopped,
    )
    print(render_report(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
