"""Unified release-check decision tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.release_check as release_check
from scripts.release_check import (
    EXPECTED_VERSION,
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
    render_report,
    stopped_listener_check,
    successful_test_count,
    untracked_artifact_check,
    version_consistency_check,
)
from src.backup_service import BackupValidation
from src.config import OfficialEndpointError, Settings


def test_release_gate_targets_v024() -> None:
    assert EXPECTED_VERSION == "0.3.3"


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
        Settings(app_title="工程知识库 v0.1.2", app_version="0.1.2"),
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
    assert successful_test_count(
        "............... [100%]", returncode=0, collected=15
    ) == 15
    assert successful_test_count(
        "..............F [100%]", returncode=1, collected=15
    ) == 0
