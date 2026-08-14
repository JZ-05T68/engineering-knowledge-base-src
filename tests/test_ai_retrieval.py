"""Tests for the offline hybrid retrieval fusion kernel. No network, no DB."""

from __future__ import annotations

import pytest

from src.ai.retrieval import (
    DEFAULT_RRF_K,
    HybridHit,
    RankedHit,
    VectorRecallSource,
    rrf_fuse,
)


def _hits(*pairs: tuple[int, int]) -> tuple[RankedHit, ...]:
    return tuple(RankedHit(page_id=page_id, rank=rank) for page_id, rank in pairs)


class _FakeVectorRecall:
    def recall(self, query: str, *, limit: int) -> tuple[RankedHit, ...]:
        return _hits((7, 1), (3, 2))[:limit]


def test_basic_fusion_unions_and_accumulates_dual_contributions() -> None:
    lexical = _hits((1, 1), (2, 2), (3, 3))  # A, B, C
    vector = _hits((3, 1), (1, 2), (4, 3))  # C, A, D
    k = DEFAULT_RRF_K

    fused = rrf_fuse(lexical, vector)

    assert {hit.page_id for hit in fused} == {1, 2, 3, 4}
    by_id = {hit.page_id: hit for hit in fused}

    # A(1): lexical rank 1 + vector rank 2 — both contributions accumulate.
    assert by_id[1].lexical_rank == 1
    assert by_id[1].vector_rank == 2
    assert by_id[1].fused_score == pytest.approx(1.0 / (k + 1) + 1.0 / (k + 2))
    # C(3): lexical rank 3 + vector rank 1.
    assert by_id[3].fused_score == pytest.approx(1.0 / (k + 3) + 1.0 / (k + 1))
    # Single-source candidates keep exactly one contribution.
    assert by_id[2].fused_score == pytest.approx(1.0 / (k + 2))
    assert by_id[2].vector_rank is None
    assert by_id[4].fused_score == pytest.approx(1.0 / (k + 3))
    assert by_id[4].lexical_rank is None

    # Ordering: A (1/61+1/62 ≈ 0.03252) > C (1/63+1/61 ≈ 0.03227) > B > D.
    assert [hit.page_id for hit in fused] == [1, 3, 2, 4]


def test_lexical_only_candidate_is_retained() -> None:
    fused = rrf_fuse(_hits((5, 1)), _hits((9, 1)))

    by_id = {hit.page_id: hit for hit in fused}
    assert by_id[5].lexical_rank == 1
    assert by_id[5].vector_rank is None


def test_vector_only_candidate_breaks_through_like_recall_gate() -> None:
    """A page absent from lexical recall must survive fusion."""

    fused = rrf_fuse(_hits((1, 1), (2, 2)), _hits((99, 1)))

    by_id = {hit.page_id: hit for hit in fused}
    assert 99 in by_id
    assert by_id[99].lexical_rank is None
    assert by_id[99].vector_rank == 1
    # Vector rank 1 (1/61) beats lexical rank 1 (1/61)? Exact tie → best_rank tie
    # → page_id ascending puts 1 before 99.
    assert [hit.page_id for hit in fused] == [1, 99, 2]


def test_lexical_only_degrades_to_lexical_order() -> None:
    fused = rrf_fuse(_hits((10, 1), (20, 2), (30, 3)), ())

    assert [hit.page_id for hit in fused] == [10, 20, 30]
    assert all(hit.vector_rank is None for hit in fused)


def test_vector_only_degrades_to_vector_order() -> None:
    fused = rrf_fuse((), _hits((30, 1), (10, 2), (20, 3)))

    assert [hit.page_id for hit in fused] == [30, 10, 20]
    assert all(hit.lexical_rank is None for hit in fused)


def test_both_empty_returns_empty_tuple() -> None:
    assert rrf_fuse((), ()) == ()


def test_duplicate_page_within_one_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="重复"):
        rrf_fuse(_hits((1, 1), (1, 2)), ())
    with pytest.raises(ValueError, match="重复"):
        rrf_fuse((), _hits((2, 1), (2, 2)))


def test_same_page_in_both_sources_is_not_a_duplicate() -> None:
    fused = rrf_fuse(_hits((1, 1)), _hits((1, 1)))

    assert len(fused) == 1
    assert fused[0].source_count == 2


def test_invalid_ranks_are_rejected() -> None:
    with pytest.raises(ValueError, match="rank"):
        rrf_fuse(_hits((1, 0)), ())
    with pytest.raises(ValueError, match="rank"):
        rrf_fuse((), _hits((1, -3)))


def test_invalid_page_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="page_id"):
        rrf_fuse(_hits((0, 1)), ())
    with pytest.raises(ValueError, match="page_id"):
        rrf_fuse(_hits((-1, 1)), ())


def test_invalid_k_is_rejected() -> None:
    for bad_k in (0, -1):
        with pytest.raises(ValueError, match="k"):
            rrf_fuse(_hits((1, 1)), (), k=bad_k)


def test_exact_tie_is_deterministic_and_source_neutral() -> None:
    """Same fused score + same best_rank → page_id ascending, no source bias."""

    lexical = _hits((2, 1))
    vector = _hits((1, 1))

    fused = rrf_fuse(lexical, vector)

    assert [hit.page_id for hit in fused] == [1, 2]
    assert fused[0].fused_score == pytest.approx(fused[1].fused_score)
    assert fused[0].best_rank == fused[1].best_rank == 1


def test_input_order_permutation_never_changes_output() -> None:
    lexical = _hits((1, 1), (2, 2), (3, 3), (4, 4))
    vector = _hits((3, 1), (1, 2), (5, 3))
    expected = rrf_fuse(lexical, vector)

    assert rrf_fuse(tuple(reversed(lexical)), tuple(reversed(vector))) == expected


def test_k_semantics_follow_the_formula() -> None:
    hit = _hits((7, 1))

    assert rrf_fuse(hit, (), k=60)[0].fused_score == pytest.approx(1.0 / 61)
    assert rrf_fuse(hit, (), k=1)[0].fused_score == pytest.approx(1.0 / 2)
    assert rrf_fuse(hit, (), k=1000)[0].fused_score == pytest.approx(1.0 / 1001)


def test_small_k_sharpens_top_rank_dominance() -> None:
    lexical = _hits((1, 1), (2, 2))

    small_k = rrf_fuse(lexical, (), k=1)
    large_k = rrf_fuse(lexical, (), k=10000)
    gap_small = small_k[0].fused_score - small_k[1].fused_score
    gap_large = large_k[0].fused_score - large_k[1].fused_score

    assert gap_small > gap_large


def test_hybrid_hit_explainability_surface() -> None:
    hit = HybridHit(page_id=9, fused_score=0.03, lexical_rank=2, vector_rank=1)

    assert hit.best_rank == 1
    assert hit.source_count == 2
    single = HybridHit(page_id=9, fused_score=0.01, lexical_rank=None, vector_rank=3)
    assert single.best_rank == 3
    assert single.source_count == 1


def test_vector_recall_source_protocol_accepts_fakes_only() -> None:
    assert isinstance(_FakeVectorRecall(), VectorRecallSource)
    assert _FakeVectorRecall().recall("q", limit=1) == _hits((7, 1))
    assert not isinstance(object(), VectorRecallSource)


def test_non_hit_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="RankedHit"):
        rrf_fuse([(1, 1)], ())  # type: ignore[list-item]
