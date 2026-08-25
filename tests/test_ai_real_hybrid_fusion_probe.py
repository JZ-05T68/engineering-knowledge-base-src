"""Offline tests for the Phase 10D controlled real hybrid fusion probe.

These tests never touch the network, never call Qwen, and never require a
real API key. They cover the probe's guard rails, RRF math verification,
call accounting, DB snapshot behaviour and the both/vector-only fusion
shapes that the real experiment observed.

The frozen query and classification map are asserted so that any accidental
rewrite of the experiment contract fails loudly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import scripts.ai_real_hybrid_fusion_probe as probe
from src.ai.hybrid_search import HybridSearchOutcome, HybridSearchResult
from src.config import Settings
from src.models import PageStatus, SearchResult

APPROVED_QUERY: str = '"定时器"'
RRF_K: int = 60


@pytest.fixture(autouse=True)
def _isolate_probe_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run path opens a real database; it must never touch production."""

    database_path = tmp_path / "probe" / "data" / "database" / "knowledge.db"
    settings = Settings(
        _env_file=None,
        data_dir=database_path.parent.parent,
        raw_dir=database_path.parent.parent / "raw",
        pages_dir=database_path.parent.parent / "pages",
        markdown_dir=database_path.parent.parent / "markdown",
        database_dir=database_path.parent,
        database_path=database_path,
        backups_dir=tmp_path / "backups",
        logs_dir=tmp_path / "logs",
        log_path=tmp_path / "logs" / "probe-test.log",
        runtime_dir=tmp_path / "runtime",
        pid_path=tmp_path / "runtime" / "probe-test.pid.json",
    )
    monkeypatch.setattr(probe, "get_settings", lambda: settings)
    monkeypatch.setattr(probe, "staging_settings", lambda: settings)


def _search_result(page_id: int) -> SearchResult:
    """Build a minimal citation-complete SearchResult for report tests."""

    return SearchResult(
        page_id=page_id,
        document_id=1,
        document_title="STM32入门(标准库)(新78页版)",
        filename="STM32入门(标准库)(新78页版).pdf",
        page_number=page_id,
        image_path=Path(f"data/pages/1/page_{page_id:04d}.png"),
        content="",
        snippet="",
        rank=0.0,
        status=PageStatus.PENDING,
    )


def _fused(
    page_id: int,
    lexical_rank: int | None,
    vector_rank: int | None,
    *,
    fused_score: float | None = None,
) -> HybridSearchResult:
    """Build one fused result; by default fused_score follows the RRF formula."""

    if fused_score is None:
        _, _, fused_score = probe._rrf_expectation(lexical_rank, vector_rank)
    return HybridSearchResult(
        result=_search_result(page_id),
        fused_score=fused_score,
        lexical_rank=lexical_rank,
        vector_rank=vector_rank,
    )


class TestFrozenContract:
    def test_approved_query_frozen(self) -> None:
        assert probe.APPROVED_QUERY == APPROVED_QUERY

    def test_classification_map_complete(self) -> None:
        assert probe.CLASSIFICATION == {
            18: "strong positive",
            17: "near-positive",
            19: "hard negative",
            5: "clear negative",
            40: "clear negative",
        }

    def test_rrf_k_is_60(self) -> None:
        assert probe.RRF_K == RRF_K
        assert probe.REAL_EXTRA_ATTEMPTS == 0


class TestPaidCallGuards:
    """Guard rails must reject real calls before any DB read or network I/O."""

    def test_paid_call_without_staging_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = probe.main(["--confirm-paid-call"])
        assert exit_code == 3
        output = capsys.readouterr().out
        assert "GUARD FAIL" in output
        assert "未发起任何网络请求" in output

    def test_plain_run_is_dry_run_plan_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        # No --confirm-paid-call: plan-only path, zero API, never reaches provider.
        exit_code = probe.main([])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "dry-run" in output or "Phase 10D" in output


class TestRRFMath:
    def test_both_branches_accumulate(self) -> None:
        lex_contrib, vec_contrib, expected = probe._rrf_expectation(1, 2)
        assert lex_contrib == pytest.approx(1.0 / (RRF_K + 1))
        assert vec_contrib == pytest.approx(1.0 / (RRF_K + 2))
        assert expected == pytest.approx(lex_contrib + vec_contrib)

    def test_vector_only_has_zero_lexical(self) -> None:
        lex_contrib, vec_contrib, expected = probe._rrf_expectation(None, 4)
        assert lex_contrib == 0.0
        assert vec_contrib == pytest.approx(1.0 / (RRF_K + 4))
        assert expected == pytest.approx(vec_contrib)

    def test_lexical_only_has_zero_vector(self) -> None:
        lex_contrib, vec_contrib, expected = probe._rrf_expectation(2, None)
        assert vec_contrib == 0.0
        assert lex_contrib == pytest.approx(1.0 / (RRF_K + 2))
        assert expected == pytest.approx(lex_contrib)

    def test_no_ranks_yields_zero_total(self) -> None:
        assert probe._rrf_expectation(None, None) == (0.0, 0.0, 0.0)

    def test_matches_real_fused_values(self) -> None:
        # Real Phase 10D observations: page 18 (lex 1, vec 2) etc.
        _, _, total = probe._rrf_expectation(1, 2)
        assert total == pytest.approx(0.032522, abs=1e-6)


class TestHybridReportRRFVerification:
    def test_mismatch_detected(self) -> None:
        outcome = HybridSearchOutcome(
            results=(
                _fused(18, 1, 2, fused_score=0.999),  # wrong on purpose
            ),
            vector_status="ok",
        )
        mismatches = probe._print_hybrid_report(None, outcome)  # type: ignore[arg-type]
        assert len(mismatches) == 1
        assert "page_id=18" in mismatches[0]

    def test_math_correct_is_not_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        outcome = HybridSearchOutcome(
            results=(
                _fused(18, 1, 2),   # both
                _fused(19, 3, 1),   # both, reversed vector rank
                _fused(5, None, 4),  # vector-only
            ),
            vector_status="ok",
        )
        mismatches = probe._print_hybrid_report(None, outcome)  # type: ignore[arg-type]
        assert mismatches == []
        output = capsys.readouterr().out
        assert "RRF_MATH=PASS" in output
        assert "lex_contrib=0.000000" in output  # vector-only lexical contribution is 0
        assert "lex_contrib=0.016393" in output  # page 18 lexical contribution

    def test_report_does_not_need_database(self) -> None:
        # _print_hybrid_report only prints provenance fields already carried by
        # the fused results; it must never touch a database.
        outcome = HybridSearchOutcome(
            results=(_fused(40, None, 5),),
            vector_status="ok",
        )
        assert probe._print_hybrid_report(None, outcome) == []  # type: ignore[arg-type]


class TestCallAccounting:
    def test_recording_provider_counts_calls(self) -> None:
        class Inner:
            def __init__(self) -> None:
                self.seen: list[tuple[str, ...]] = []

            def embed(
                self,
                texts: tuple[str, ...],
                *,
                model: str | None = None,
                dimensions: int | None = None,
            ):
                self.seen.append(texts)
                return type(
                    "Result", (), {"embeddings": [[0.0] * 1024], "model": model, "usage": None}
                )()

        inner = Inner()
        recording = probe._RecordingProvider(inner)  # type: ignore[arg-type]
        cached = probe._CachedQueryProvider(recording)
        assert len(recording.calls) == 0
        cached.embed(("x",), model="m", dimensions=1024)
        cached.embed(("x",), model="m", dimensions=1024)  # cache hit, no second HTTP
        assert len(recording.calls) == 1
        assert len(inner.seen) == 1


class TestDatabaseSnapshot:
    def test_missing_path(self) -> None:
        assert probe._database_snapshot(Path("no/such/file.db")) == (-1, "MISSING")

    def test_snapshot_reads_count_and_sha256(self, tmp_path: Path) -> None:
        db_path = tmp_path / "probe.db"
        connection = sqlite3.connect(db_path)
        connection.execute("CREATE TABLE page_embeddings (id INTEGER PRIMARY KEY, page_id INTEGER)")
        connection.execute("INSERT INTO page_embeddings (page_id) VALUES (18)")
        connection.commit()
        connection.close()
        count, digest = probe._database_snapshot(db_path)
        assert count == 1
        assert len(digest) == 64

    def test_query_embedding_non_persistence_shape(self, tmp_path: Path) -> None:
        # The real run treats any count/sha256 change as a stop condition;
        # verify the snapshot is byte-stable across two reads of the same file.
        db_path = tmp_path / "stable.db"
        connection = sqlite3.connect(db_path)
        connection.execute(
            "CREATE TABLE page_embeddings (id INTEGER PRIMARY KEY, page_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO page_embeddings (page_id) VALUES (5), (17), (18), (19), (40)"
        )
        connection.commit()
        connection.close()
        assert probe._database_snapshot(db_path) == probe._database_snapshot(db_path)
