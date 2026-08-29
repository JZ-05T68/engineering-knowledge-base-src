"""Unified release-check decision tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.release_check as release_check
from scripts.release_check import (
    ACTIVE_MILESTONE_VERSION,
    EXPECTED_VERSION,
    MILESTONE_PAGES,
    PROJECT_ROOT,
    CheckResult,
    CheckStatus,
    ReleaseChecker,
    ReleaseReport,
    agents_staging_check,
    data_pollution_check,
    git_workspace_check,
    listener_check,
    parse_collected_test_count,
    parse_passed_test_count,
    parse_skipped_test_count,
    render_report,
    stopped_listener_check,
    successful_test_count,
    untracked_artifact_check,
    version_consistency_check,
)
from src.backup_service import BackupValidation
from src.config import OfficialEndpointError, Settings


def test_release_gate_targets_current_version() -> None:
    assert EXPECTED_VERSION == "0.6.0"


def test_all_pass_report_returns_zero_and_clear_summary() -> None:
    report = ReleaseReport(
        results=(
            CheckResult("Ruff", CheckStatus.PASS, "通过"),
            CheckResult("Pytest", CheckStatus.PASS, "142 passed"),
        ),
        test_count=142,
        backup_path=Path("backup"),
    )

    assert report.readiness is CheckStatus.PASS
    assert report.exit_code == 0
    rendered = render_report(report)
    assert "Release readiness: PASS" in rendered
    assert "Tests: 142 passed" in rendered


def test_warning_does_not_hide_pass_or_return_failure() -> None:
    report = ReleaseReport(
        results=(
            CheckResult("Git workspace", CheckStatus.WARNING, "仅 AGENTS.md 未提交"),
            CheckResult("Database", CheckStatus.PASS, "ok"),
        ),
        backup_path=Path("backup"),
    )

    assert report.readiness is CheckStatus.PASS
    assert report.exit_code == 0
    assert "[WARNING] Git workspace" in render_report(report)


def test_git_workspace_allows_only_unstaged_agents_change() -> None:
    assert git_workspace_check([" M AGENTS.md"]).status is CheckStatus.WARNING
    assert git_workspace_check(["M  AGENTS.md"]).status is CheckStatus.WARNING
    assert git_workspace_check([" M AGENTS.md", "?? result.txt"]).status is CheckStatus.FAIL


def test_serious_error_returns_nonzero() -> None:
    report = ReleaseReport(
        results=(CheckResult("Database", CheckStatus.FAIL, "corrupt"),)
    )

    assert report.readiness is CheckStatus.FAIL
    assert report.exit_code == 1
    assert "Release readiness: FAIL" in render_report(report)


def test_version_mismatch_fails_readme_changelog_and_page_consistency(
    tmp_path: Path,
) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "README.md").write_text(
        "# Engineering Knowledge Base v0.1.2\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("## v0.1.2\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'st.set_page_config(page_title="工程知识库 v0.1.2")', encoding="utf-8"
    )
    (tmp_path / "pages" / "1_test.py").write_text(
        'st.set_page_config(page_title="测试 v0.1.2 stale v0.0.8")', encoding="utf-8"
    )

    result = version_consistency_check(
        tmp_path,
        app_version="0.1.2",
        app_title="工程知识库 v0.1.2",
    )

    assert result.status is CheckStatus.FAIL
    assert "旧页面版本" in result.detail


def test_non_loopback_or_unhealthy_listener_is_failure() -> None:
    wrong = listener_check("127.0.0.1", 8501, ("0.0.0.0",), True)
    wrong_port = listener_check("127.0.0.1", 49343, ("127.0.0.1",), True)
    unhealthy = listener_check("127.0.0.1", 8501, ("127.0.0.1",), False)
    correct = listener_check("127.0.0.1", 8501, ("127.0.0.1",), True)

    assert wrong.status is CheckStatus.FAIL
    assert wrong_port.status is CheckStatus.FAIL
    assert "正式端点必须为 127.0.0.1:8501" in wrong_port.detail
    assert unhealthy.status is CheckStatus.FAIL
    assert correct.status is CheckStatus.PASS


def test_release_closure_can_require_service_to_be_stopped() -> None:
    stopped = stopped_listener_check("127.0.0.1", 8501, (), False)
    running = stopped_listener_check("127.0.0.1", 8501, ("127.0.0.1",), True)
    wrong_port = stopped_listener_check("127.0.0.1", 8510, (), False)

    assert stopped.status is CheckStatus.PASS
    assert running.status is CheckStatus.FAIL
    assert wrong_port.status is CheckStatus.FAIL


def test_release_closure_revalidates_existing_formal_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "release-v0.1.2-20260721-120000-deadbeef"
    checker = ReleaseChecker(
        Settings(app_title="工程知识库 v0.1.2", app_version="0.1.2", _env_file=None),
        tmp_path,
    )

    monkeypatch.setattr(
        release_check,
        "validate_backup",
        lambda *args, **kwargs: BackupValidation(
            backup_path=backup,
            valid=True,
            errors=(),
            warnings=(),
            manifest={},
            database_summary=None,
            duration_seconds=0.125,
        ),
    )

    result, validated_path = checker._existing_backup_check(backup)
    wrong_name, wrong_path = checker._existing_backup_check(
        tmp_path / "ekb-v0.1.2-20260721"
    )

    assert result.status is CheckStatus.PASS
    assert validated_path == backup.resolve(strict=False)
    assert wrong_name.status is CheckStatus.FAIL
    assert wrong_path is None


def test_release_entrypoint_reports_invalid_formal_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid_settings():
        raise OfficialEndpointError(
            "正式服务端点必须为 127.0.0.1:8501；收到端口 49343。"
        )

    monkeypatch.setattr(release_check, "get_settings", invalid_settings)

    assert release_check.main(["--skip-backup"]) == 2
    output = capsys.readouterr().out
    assert "[FAIL] Formal endpoint configuration" in output
    assert "127.0.0.1:8501" in output


def test_staged_agents_md_is_a_release_failure() -> None:
    assert agents_staging_check([]).status is CheckStatus.PASS
    result = agents_staging_check(["src/config.py", "AGENTS.md"])
    assert result.status is CheckStatus.FAIL
    assert "误暂存" in result.detail


def test_unignored_acceptance_artifact_and_formal_pollution_are_failures(
    tmp_path: Path,
) -> None:
    status = ["?? browser-acceptance/screenshot.png", " M AGENTS.md"]
    assert untracked_artifact_check(status).status is CheckStatus.FAIL

    data = tmp_path / "data"
    artifact = data / "browser-acceptance" / "result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    assert data_pollution_check(data).status is CheckStatus.FAIL


def test_pytest_count_parsers_support_project_quiet_output() -> None:
    collected_output = "tests/test_a.py: 12\ntests/test_b.py: 3\n"
    passed_output = "............... [100%]\n15 passed in 2.01s\n"

    assert parse_collected_test_count(collected_output) == 15
    assert parse_collected_test_count("15 tests collected in 0.2s") == 15
    assert parse_passed_test_count(passed_output) == 15
    assert parse_skipped_test_count("14 passed, 1 skipped in 2.01s") == 1
    assert successful_test_count(
        "............... [100%]", returncode=0, collected=15
    ) == 15
    assert successful_test_count(
        "..............F [100%]", returncode=1, collected=15
    ) == 0


def test_frozen_competition_agent_page_passes_version_consistency() -> None:
    """Audit R-01/TD-02 regression: the gate must accept the frozen tree.

    The v0.6.1 competition Agent page intentionally carries no released
    version in ``page_title`` and displays the milestone line elsewhere; the
    gate must understand that policy instead of failing the whole release.
    """

    settings = Settings(_env_file=None)

    result = version_consistency_check(
        PROJECT_ROOT,
        app_version=settings.app_version,
        app_title=settings.app_title,
    )

    assert result.status is CheckStatus.PASS
    assert "0.5.3" not in result.detail


def test_milestone_page_policy_accepts_sanctioned_milestone_display(
    tmp_path: Path,
) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "README.md").write_text(
        f"# Engineering Knowledge Base v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"## v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        f'st.set_page_config(page_title="工程知识库 v{EXPECTED_VERSION}")',
        encoding="utf-8",
    )
    (tmp_path / "pages" / "0_知识Agent.py").write_text(
        'page_title="知识 Agent · 工程知识库"\n'
        f'PAGE_VERSION_LINE = "v{ACTIVE_MILESTONE_VERSION} · Competition Demo"',
        encoding="utf-8",
    )

    result = version_consistency_check(
        tmp_path,
        app_version=EXPECTED_VERSION,
        app_title=f"工程知识库 v{EXPECTED_VERSION}",
    )

    assert result.status is CheckStatus.PASS


def test_milestone_page_without_milestone_version_is_rejected(
    tmp_path: Path,
) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "README.md").write_text(
        f"# Engineering Knowledge Base v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"## v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        f'st.set_page_config(page_title="工程知识库 v{EXPECTED_VERSION}")',
        encoding="utf-8",
    )
    (tmp_path / "pages" / "0_知识Agent.py").write_text(
        'page_title="知识 Agent · 工程知识库"', encoding="utf-8"
    )

    result = version_consistency_check(
        tmp_path,
        app_version=EXPECTED_VERSION,
        app_title=f"工程知识库 v{EXPECTED_VERSION}",
    )

    assert result.status is CheckStatus.FAIL
    assert f"未包含活动里程碑版本 v{ACTIVE_MILESTONE_VERSION}" in result.detail


def test_ordinary_page_missing_released_version_is_rejected(
    tmp_path: Path,
) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "README.md").write_text(
        f"# Engineering Knowledge Base v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"## v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        f'st.set_page_config(page_title="工程知识库 v{EXPECTED_VERSION}")',
        encoding="utf-8",
    )
    (tmp_path / "pages" / "9_普通页.py").write_text(
        'st.set_page_config(page_title="普通页 · 工程知识库")', encoding="utf-8"
    )

    result = version_consistency_check(
        tmp_path,
        app_version=EXPECTED_VERSION,
        app_title=f"工程知识库 v{EXPECTED_VERSION}",
    )

    assert result.status is CheckStatus.FAIL
    assert "9_普通页.py 未包含当前页面版本" in result.detail


def test_ordinary_page_displaying_milestone_version_is_stale(
    tmp_path: Path,
) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "README.md").write_text(
        f"# Engineering Knowledge Base v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"## v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        f'st.set_page_config(page_title="工程知识库 v{EXPECTED_VERSION}")',
        encoding="utf-8",
    )
    (tmp_path / "pages" / "9_普通页.py").write_text(
        f'st.set_page_config(page_title="普通页 · 工程知识库 v{ACTIVE_MILESTONE_VERSION}")',
        encoding="utf-8",
    )

    result = version_consistency_check(
        tmp_path,
        app_version=EXPECTED_VERSION,
        app_title=f"工程知识库 v{EXPECTED_VERSION}",
    )

    assert result.status is CheckStatus.FAIL
    assert "旧页面版本" in result.detail


def test_stale_app_version_literal_in_page_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "README.md").write_text(
        f"# Engineering Knowledge Base v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"## v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        f'st.set_page_config(page_title="工程知识库 v{EXPECTED_VERSION}")',
        encoding="utf-8",
    )
    (tmp_path / "pages" / "9_普通页.py").write_text(
        f'st.set_page_config(page_title="普通页 · 工程知识库 v{EXPECTED_VERSION}")\n'
        'render_packager(app_version="0.5.3")',
        encoding="utf-8",
    )

    result = version_consistency_check(
        tmp_path,
        app_version=EXPECTED_VERSION,
        app_title=f"工程知识库 v{EXPECTED_VERSION}",
    )

    assert result.status is CheckStatus.FAIL
    assert "硬编码了过期应用版本字面量：0.5.3" in result.detail


def test_gate_does_not_confuse_api_schema_and_display_versions(
    tmp_path: Path,
) -> None:
    """/v0.6 (API path) and schema v12 must never trip the display check."""

    (tmp_path / "pages").mkdir()
    (tmp_path / "README.md").write_text(
        f"# Engineering Knowledge Base v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"## v{EXPECTED_VERSION}\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        f'st.set_page_config(page_title="工程知识库 v{EXPECTED_VERSION}")\n'
        'st.caption("Hosted API: /v0.6 · schema v12 · 引用可点击")',
        encoding="utf-8",
    )
    (tmp_path / "pages" / "9_普通页.py").write_text(
        f'st.set_page_config(page_title="普通页 · 工程知识库 v{EXPECTED_VERSION}")\n'
        'st.caption("POST /v0.6/agent/run 需要 schema v12 数据库")',
        encoding="utf-8",
    )

    result = version_consistency_check(
        tmp_path,
        app_version=EXPECTED_VERSION,
        app_title=f"工程知识库 v{EXPECTED_VERSION}",
    )

    assert result.status is CheckStatus.PASS


def test_milestone_pages_policy_only_covers_the_competition_agent_page() -> None:
    assert MILESTONE_PAGES == frozenset({"0_知识Agent.py"})
    assert ACTIVE_MILESTONE_VERSION == "0.6.1"
    assert EXPECTED_VERSION == "0.6.0"
