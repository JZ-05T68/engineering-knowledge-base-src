"""Tests for the embedding experiment math and paid-call guards. No network."""

from __future__ import annotations

import math

import pytest

import scripts.ai_embedding_experiment as experiment
from src.ai.provider import EmbeddingUsage
from src.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- retrieval math (fixed fake vectors) ---


def test_cosine_similarity_correctness() -> None:
    assert experiment.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert experiment.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert experiment.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert experiment.cosine_similarity([3.0, 4.0], [3.0, 4.0]) == pytest.approx(1.0)


def test_cosine_similarity_rejects_zero_vector() -> None:
    with pytest.raises(ValueError, match="零向量"):
        experiment.cosine_similarity([0.0, 0.0], [1.0, 0.0])


def test_cosine_similarity_rejects_mismatched_dimensions() -> None:
    with pytest.raises(ValueError, match="维度不一致"):
        experiment.cosine_similarity([1.0], [1.0, 0.0])


def test_cosine_similarity_rejects_empty_vectors() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        experiment.cosine_similarity([], [])


def test_rank_documents_orders_by_descending_similarity() -> None:
    query = [1.0, 0.0]
    documents = [
        [0.0, 1.0],  # D0: orthogonal
        [1.0, 0.0],  # D1: identical
        [math.sqrt(0.5), math.sqrt(0.5)],  # D2: 45 degrees
    ]

    ranking = experiment.rank_documents(query, documents)

    assert [index for index, _ in ranking] == [1, 2, 0]
    assert ranking[0][1] == pytest.approx(1.0)
    assert ranking[1][1] == pytest.approx(math.sqrt(0.5))
    assert ranking[2][1] == pytest.approx(0.0)


def test_rank_documents_exact_ties_keep_input_order() -> None:
    query = [1.0, 0.0]
    documents = [[1.0, 0.0], [2.0, 0.0], [0.5, 0.0]]  # all identical direction

    ranking = experiment.rank_documents(query, documents)

    assert [index for index, _ in ranking] == [0, 1, 2]
    assert all(score == pytest.approx(1.0) for _, score in ranking)


def test_experiment_batch_is_exactly_the_fixed_synthetic_texts() -> None:
    assert len(experiment.BATCH_TEXTS) == 8
    assert experiment.BATCH_TEXTS[:2] == experiment.QUERIES
    assert experiment.BATCH_TEXTS[2:] == experiment.DOCUMENTS
    assert experiment.EXPECTED_TOP1 == {0: 0, 1: 5}
    assert experiment.EXPERIMENT_DIMENSIONS == 1024
    assert experiment.EXPERIMENT_EXTRA_ATTEMPTS == 0


# --- paid-call guards ---


def test_without_confirm_flag_refuses_and_never_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        experiment, "run_experiment", lambda s: pytest.fail("不应触达真实调用")
    )
    monkeypatch.setattr(
        experiment, "get_settings", lambda: pytest.fail("未确认时不应读取配置")
    )

    exit_code = experiment.main([])

    assert exit_code == 2
    assert "SKIPPED" in capsys.readouterr().out


def test_manual_mode_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        experiment, "run_experiment", lambda s: pytest.fail("manual 模式不应调用")
    )

    assert experiment.main(["--confirm-paid-call"]) == 3


def test_missing_key_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(experiment, "get_settings", lambda: _settings(ai_mode="api"))
    monkeypatch.setattr(
        experiment, "run_experiment", lambda s: pytest.fail("无 Key 不应调用")
    )

    assert experiment.main(["--confirm-paid-call"]) == 3
    out = capsys.readouterr().out
    assert "EKB_AI_API_KEY is not configured" in out
    assert "API Key present: NO" in out


def test_wrong_embedding_model_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        ai_mode="api",
        ai_api_key="sk-x",
        ai_embedding_model="qwen3.7-plus",
    )
    monkeypatch.setattr(experiment, "get_settings", lambda: settings)
    monkeypatch.setattr(
        experiment, "run_experiment", lambda s: pytest.fail("模型不符不应调用")
    )

    assert experiment.main(["--confirm-paid-call"]) == 3


def test_all_guards_pass_delegates_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(ai_mode="api", ai_api_key="sk-experiment-guard-test")
    monkeypatch.setattr(experiment, "get_settings", lambda: settings)
    calls: list[Settings] = []
    monkeypatch.setattr(experiment, "run_experiment", lambda s: calls.append(s) or 0)

    exit_code = experiment.main(["--confirm-paid-call"])

    assert exit_code == 0
    assert calls == [settings]
    out = capsys.readouterr().out
    assert "API Key present: YES" in out
    assert "estimated maximum real HTTP requests: 1" in out
    assert "sk-experiment-guard-test" not in out


def test_embedding_usage_is_frozen_and_completion_free() -> None:
    usage = EmbeddingUsage(prompt_tokens=5, total_tokens=5)

    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        usage.total_tokens = 0  # type: ignore[misc]
    assert not hasattr(usage, "completion_tokens")
