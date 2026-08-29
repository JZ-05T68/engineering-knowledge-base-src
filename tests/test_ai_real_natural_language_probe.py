"""Offline tests for the Phase 10E-C real natural-language hybrid probe.

These tests never touch the network, never call Qwen, and never require a
real API key. They freeze the experiment contract (query, model, dimensions,
expected embedding IDs) and prove the probe's guard rails and RRF math before
any real call is authorized.
"""

from __future__ import annotations

import scripts.ai_real_natural_language_probe as probe


def test_approved_query_frozen() -> None:
    assert probe.APPROVED_QUERY == "定时器预分频器和自动重装载寄存器的作用"


def test_model_and_dimensions_frozen() -> None:
    assert probe.QUERY_MODEL == "qwen3.7-text-embedding"
    assert probe.QUERY_DIMENSIONS == 1024
    assert probe.QUERY_CONFIG_VERSION == 1
    assert probe.RRF_K == 60


def test_expected_embedding_ids_frozen() -> None:
    assert probe.EXPECTED_EMBEDDING_IDS == (5, 17, 18, 19, 40)


def test_paid_call_without_staging_refused(capsys) -> None:
    exit_code = probe.main(["--confirm-paid-call"])
    assert exit_code == 3
    assert "未发起任何网络请求" in capsys.readouterr().out


def test_staging_flag_is_forced_at_import() -> None:
    # The probe must establish staging context in a fresh process before any
    # runtime resolves. Proven in a subprocess WITHOUT the flag pre-set: the
    # suite-wide isolation fixture sanitizes ambient EKB_* state, and a
    # collection-time import of this module must not leak into ordinary tests.
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import os, scripts.ai_real_natural_language_probe as probe; "
        "print(os.environ.get('EKB_STAGING_INSTANCE'))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == "1"


def test_staging_database_path_is_staging_only() -> None:
    assert "staging-data" in str(probe.STAGING_DB_PATH)
    assert str(probe.PRODUCTION_DB_PATH) == str(
        probe.PROJECT_ROOT / "data" / "database" / "knowledge.db"
    )
    assert probe.STAGING_DB_PATH != probe.PRODUCTION_DB_PATH


class TestRRFMath:
    def test_both_branches_accumulate(self) -> None:
        lex_c, vec_c, total = probe._rrf_expectation(1, 2)
        assert lex_c == 1.0 / 61.0
        assert vec_c == 1.0 / 62.0
        assert total == lex_c + vec_c

    def test_single_source_zero_contribution(self) -> None:
        lex_c, vec_c, total = probe._rrf_expectation(None, 4)
        assert lex_c == 0.0
        assert vec_c == 1.0 / 64.0
        assert total == vec_c

    def test_no_ranks_zero(self) -> None:
        assert probe._rrf_expectation(None, None) == (0.0, 0.0, 0.0)
