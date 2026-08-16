"""Guard tests for the controlled real query probe entry point. No network ever.

Every test uses fake providers and temp databases only; the real
``QwenProvider`` constructor is replaced by a recording factory and the
production database path is redirected to a temp file. The real API key is
never read, printed, or sent anywhere.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import scripts.ai_real_query_probe as probe_script
from src.ai.page_indexer import EMBEDDING_CONFIG_VERSION, prepare_page_text
from src.ai.provider import EmbeddingResult
from src.config import Settings
from src.database import Database

MODEL = "qwen3.7-text-embedding"
DIMS = 1024
CONFIG = EMBEDDING_CONFIG_VERSION
FAKE_KEY = "sk-phase10c-step3-guard-test-key"
PAGE_TEXT = "定时器框图：PSC 预分频器、CNT 计数器、ARR 自动重装载寄存器。"


def _settings(database_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "data_dir": database_path.parent.parent,
        "raw_dir": database_path.parent.parent / "raw",
        "pages_dir": database_path.parent.parent / "pages",
        "markdown_dir": database_path.parent.parent / "markdown",
        "database_dir": database_path.parent,
        "database_path": database_path,
        "backups_dir": database_path.parent.parent.parent / "backups",
        "logs_dir": database_path.parent.parent.parent / "logs",
        "log_path": database_path.parent.parent.parent / "logs" / "test.log",
        "runtime_dir": database_path.parent.parent.parent / "runtime",
        "pid_path": database_path.parent.parent.parent / "runtime" / "pid.json",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _library(database_path: Path, *, with_embedding: bool) -> tuple[Database, int]:
    """Create one document with one page; optionally store a fresh embedding."""

    database = Database(database_path)
    document = database.create_document(
        title="STM32入门",
        filename="stm32.pdf",
        source_path=database_path.parent / "stm32.pdf",
        sha256="e" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=18,
        image_path=database_path.parent / "page_0018.png",
        extracted_text=PAGE_TEXT,
    )
    if with_embedding:
        prepared = prepare_page_text(page.searchable_content)
        assert prepared is not None
        database.upsert_page_embedding(
            page_id=page.id,
            source_text_sha256=prepared.sha256,
            model=MODEL,
            dimensions=DIMS,
            config_version=CONFIG,
            vector=(0.5,) * DIMS,
        )
    return database, page.id


class FakeQwenProvider:
    """Recording fake standing in for the real Qwen adapter."""

    constructed: list[dict[str, object]] = []
    calls: list[tuple[tuple[str, ...], str | None, int | None]] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).constructed.append(kwargs)

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        type(self).calls.append((tuple(texts), model, dimensions))
        return EmbeddingResult(
            embeddings=tuple((0.5,) * (dimensions or DIMS) for _ in texts),
            model=model or MODEL,
        )


@pytest.fixture(autouse=True)
def _fake_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeQwenProvider.constructed = []
    FakeQwenProvider.calls = []
    monkeypatch.setattr(probe_script, "QwenProvider", FakeQwenProvider)


@pytest.fixture(autouse=True)
def _fake_production_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the production snapshot to an isolated temp database."""

    production_path = tmp_path / "prod" / "data" / "database" / "knowledge.db"
    Database(production_path)  # schema only, zero embeddings
    monkeypatch.setattr(
        probe_script, "_production_database_path", lambda: production_path
    )
    return production_path


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(probe_script, "get_settings", lambda: settings)


def _patch_staging(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(probe_script, "staging_settings", lambda: settings)


# ------------------------------------------------------------------ constants
def test_query_is_the_approved_constant() -> None:
    """The approved query is hard-coded; the probe can never widen itself."""

    assert probe_script.APPROVED_QUERY == "定时器预分频器和自动重装载寄存器的作用"
    assert probe_script.REAL_EXTRA_ATTEMPTS == 0


# ---------------------------------------------------------------- dry-run path
def test_default_is_dry_run_with_zero_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _, page_id = _library(database_path, with_embedding=True)
    _patch_settings(monkeypatch, _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY))

    exit_code = probe_script.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert f"exact query: {probe_script.APPROVED_QUERY!r}" in out
    assert "stored page embeddings: 1" in out
    assert f"page_id={page_id}" in out
    assert "fresh=YES" in out
    assert "production page_embeddings: 0" in out
    assert "planned real HTTP requests: 1" in out
    assert "planned max_extra_attempts: 0" in out
    assert "planned DB writes: 0" in out
    assert FakeQwenProvider.constructed == []  # dry-run 不装配真实 provider
    assert FakeQwenProvider.calls == []


def test_dry_run_without_stored_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, with_embedding=False)
    _patch_settings(monkeypatch, _settings(database_path))

    assert probe_script.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "stored page embeddings: 0" in out
    assert "（无存储向量）" in out
    assert FakeQwenProvider.calls == []


# ------------------------------------------------------------------ paid guards
def test_paid_without_staging_is_refused_before_any_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, with_embedding=True)
    monkeypatch.setattr(
        probe_script,
        "get_settings",
        lambda: pytest.fail("staging 护栏必须先于配置读取触发"),
    )

    exit_code = probe_script.main(["--confirm-paid-call"])

    out = capsys.readouterr().out
    assert exit_code == 3
    assert "仅允许在隔离的 staging 实例执行" in out
    assert "未发起任何网络请求" in out
    assert FakeQwenProvider.constructed == []
    assert FakeQwenProvider.calls == []


def test_paid_requires_ready_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, with_embedding=True)
    _patch_staging(monkeypatch, _settings(database_path))  # manual, no key

    exit_code = probe_script.main(["--confirm-paid-call", "--staging"])

    assert exit_code == 3
    assert "GUARD FAIL" in capsys.readouterr().out
    assert FakeQwenProvider.calls == []


def test_api_key_never_appears_in_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, with_embedding=True)
    settings = _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY)
    _patch_staging(monkeypatch, settings)

    probe_script.main(["--staging"])
    probe_script.main(["--confirm-paid-call", "--staging"])

    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    assert "API Key present: YES" in out


# ------------------------------------------------------------- paid path (fake)
def test_paid_run_single_http_call_readonly_and_full_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _fake_production_db: Path,
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _, page_id = _library(database_path, with_embedding=True)
    settings = _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY)
    _patch_staging(monkeypatch, settings)
    staging_bytes_before = database_path.read_bytes()
    production_bytes_before = _fake_production_db.read_bytes()

    exit_code = probe_script.main(["--confirm-paid-call", "--staging"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "CALL PASS" in out
    # 恰好一次真实 provider 调用：单 query、model/dimensions 正确、retry=0
    assert "real provider embed calls: 1" in out
    assert len(FakeQwenProvider.calls) == 1
    texts, model, dimensions = FakeQwenProvider.calls[0]
    assert texts == (probe_script.APPROVED_QUERY,)
    assert model == MODEL
    assert dimensions == DIMS
    assert FakeQwenProvider.constructed[0]["max_extra_attempts"] == 0
    # vector recall 独立报告
    assert "query embedding dimensions: 1024" in out
    assert "vector candidate count: 1" in out
    assert f"vector rank 1: page_id={page_id} cosine_similarity=1.000000" in out
    # hybrid + hydration/citation
    assert "vector_status: ok" in out
    assert f"page_id={page_id}" in out
    assert "vector_rank=1" in out
    assert "citation: title='STM32入门'" in out
    assert "filename='stm32.pdf'" in out
    assert "page=18" in out
    # 双库前后快照：数量不变、文件内容不变（只读路径）
    assert "staging page_embeddings: 1" in out
    assert "production page_embeddings: 0" in out
    assert "全部护栏通过" in out
    assert database_path.read_bytes() == staging_bytes_before
    assert _fake_production_db.read_bytes() == production_bytes_before
    # 无新增 page embedding，无 query embedding 持久化
    database = Database(database_path)
    rows = database.list_page_embeddings(model=MODEL, dimensions=DIMS, config_version=CONFIG)
    assert len(rows) == 1
    assert rows[0].page_id == page_id


def test_paid_with_no_stored_embeddings_reports_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, with_embedding=False)
    _patch_staging(monkeypatch, _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY))

    exit_code = probe_script.main(["--confirm-paid-call", "--staging"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "vector candidate count: 0" in out
    assert "vector_status: empty" in out
    assert len(FakeQwenProvider.calls) == 1  # query embedding 仍然恰好一次


def test_paid_path_runs_offline_with_fake_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("测试禁止真实网络访问")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, with_embedding=True)
    _patch_staging(
        monkeypatch, _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY)
    )

    assert probe_script.main(["--confirm-paid-call", "--staging"]) == 0
