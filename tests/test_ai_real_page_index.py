"""Guard tests for the controlled real page index entry point. No network ever.

Every test uses fake providers and temp databases only; the real
``QwenProvider`` constructor is replaced by a recording factory. The real
API key is never read, printed, or sent anywhere.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import scripts.ai_real_page_index as index_script
from src.ai.page_indexer import EMBEDDING_CONFIG_VERSION, PageEmbeddingIndexer
from src.ai.provider import EmbeddingResult
from src.config import Settings
from src.database import Database

MODEL = "qwen3.7-text-embedding"
DIMS = 1024
CONFIG = EMBEDDING_CONFIG_VERSION
FAKE_KEY = "sk-phase10-guard-test-key"


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


def _library(database_path: Path, texts: list[str]) -> tuple[Database, list[int]]:
    database = Database(database_path)
    document = database.create_document(
        title="液压手册",
        filename="hyd.pdf",
        source_path=database_path.parent / "hyd.pdf",
        sha256="d" * 64,
    )
    ids = []
    for number, text in enumerate(texts, start=1):
        page = database.create_page(
            document_id=document.id,
            page_number=number,
            image_path=database_path.parent / f"page_{number:04d}.png",
            extracted_text=text,
        )
        ids.append(page.id)
    return database, ids


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
    monkeypatch.setattr(index_script, "QwenProvider", FakeQwenProvider)


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(index_script, "get_settings", lambda: settings)


def _patch_staging(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(index_script, "staging_settings", lambda: settings)


# ---------------------------------------------------------------- dry-run path
def test_default_is_dry_run_with_zero_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, ["文本一", "文本二"])
    _patch_settings(monkeypatch, _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY))

    exit_code = index_script.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "total pages: 2" in out
    assert "missing: 2" in out
    assert "planned real HTTP requests: 1" in out
    assert FakeQwenProvider.constructed == []  # dry-run 不装配真实 provider
    assert FakeQwenProvider.calls == []


def test_dry_run_reports_existing_rows_and_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    database, ids = _library(database_path, ["文本一", "文本二"])
    indexer_db = Database(database_path)
    PageEmbeddingIndexer(
        database=indexer_db,
        embedding=FakeQwenProvider(),
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
        page_ids=[ids[0]],
    ).index_pages()
    FakeQwenProvider.constructed = []  # 重置 setup 记录，只观察 dry-run
    FakeQwenProvider.calls = []
    _patch_settings(monkeypatch, _settings(database_path))

    exit_code = index_script.main(["--dry-run"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "reused(fresh): 1" in out
    assert "missing: 1" in out
    assert "existing embedding rows: 1" in out
    assert FakeQwenProvider.calls == []


def test_dry_run_empty_library_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    Database(database_path)
    _patch_settings(monkeypatch, _settings(database_path))

    assert index_script.main([]) == 0
    assert "NO-OP" in capsys.readouterr().out


# ------------------------------------------------------------------- selection
def test_selection_is_capped_at_five_and_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _, ids = _library(database_path, [f"第 {n} 页正文" for n in range(1, 9)])
    _patch_settings(monkeypatch, _settings(database_path))

    assert index_script.main([]) == 0
    out = capsys.readouterr().out

    selected = [line for line in out.splitlines() if line.strip().startswith("page_id=")]
    assert len(selected) == 5
    assert [int(line.split()[0].split("=")[1]) for line in selected] == ids[:5]
    assert "planned embedding inputs: 5" in out
    for text in (f"第 {n} 页正文" for n in range(1, 9)):
        assert text not in out  # 报告只含 metadata，不含正文


def test_selection_reports_state_and_skips_sensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    database, ids = _library(
        database_path, ["正常页面", "包含 password 的页面", "另一正常页面"]
    )
    # 制造一页 STALE：先写入 hash 不匹配的 embedding
    database.upsert_page_embedding(
        page_id=ids[0],
        source_text_sha256="0" * 64,
        model=MODEL,
        dimensions=DIMS,
        config_version=CONFIG,
        vector=(0.5,) * DIMS,
    )
    _patch_settings(monkeypatch, _settings(database_path))

    assert index_script.main([]) == 0
    out = capsys.readouterr().out

    assert "state=STALE" in out
    assert "state=MISSING" in out
    assert f"sensitive-skipped page_ids（仅启发式，不上传）: [{ids[1]}]" in out
    selected = [line for line in out.splitlines() if line.strip().startswith("page_id=")]
    selected_ids = [int(line.split()[0].split("=")[1]) for line in selected]
    assert ids[1] not in selected_ids


# ------------------------------------------------------------------ paid guards
def test_paid_without_staging_is_refused_before_any_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, ["文本一"])
    monkeypatch.setattr(
        index_script,
        "get_settings",
        lambda: pytest.fail("staging 护栏必须先于配置读取触发"),
    )

    exit_code = index_script.main(["--confirm-paid-call"])

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
    _library(database_path, ["文本一"])
    _patch_staging(monkeypatch, _settings(database_path))  # manual, no key

    exit_code = index_script.main(["--confirm-paid-call", "--staging"])

    assert exit_code == 3
    assert "GUARD FAIL" in capsys.readouterr().out
    assert FakeQwenProvider.calls == []


def test_api_key_never_appears_in_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, ["文本一"])
    settings = _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY)
    _patch_staging(monkeypatch, settings)

    index_script.main(["--staging"])
    index_script.main(["--confirm-paid-call", "--staging"])

    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    assert "API Key present: YES" in out


# ------------------------------------------------------------- paid path (fake)
def test_paid_run_single_batch_zero_retry_and_reuse_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _, ids = _library(database_path, [f"第 {n} 页正文" for n in range(1, 6)])
    settings = _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY)
    _patch_staging(monkeypatch, settings)

    exit_code = index_script.main(["--confirm-paid-call", "--staging"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "CALL PASS" in out
    # 恰好一次调用、单 batch、5 条输入、model/dimensions 正确
    assert len(FakeQwenProvider.calls) == 1
    texts, model, dimensions = FakeQwenProvider.calls[0]
    assert len(texts) == 5
    assert model == MODEL
    assert dimensions == DIMS
    # retry = 0
    assert FakeQwenProvider.constructed[0]["max_extra_attempts"] == 0
    # 写入验证 + post-index dry-run 显示全部 reused
    assert "indexed: 5" in out
    assert "post-index dry-run: total=5 reused=5 to_generate=0" in out
    database = Database(database_path)
    for page_id in ids:
        stored = database.get_page_embedding(
            page_id=page_id, model=MODEL, dimensions=DIMS, config_version=CONFIG
        )
        assert stored is not None
        assert len(stored.vector) == DIMS

    # 再次付费路径：全部 fresh → NO-OP，0 调用
    FakeQwenProvider.calls = []
    assert index_script.main(["--confirm-paid-call", "--staging"]) == 0
    assert "NO-OP" in capsys.readouterr().out
    assert FakeQwenProvider.calls == []


def test_paid_path_runs_offline_with_fake_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("测试禁止真实网络访问")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    _library(database_path, ["文本一"])
    _patch_staging(
        monkeypatch, _settings(database_path, ai_mode="api", ai_api_key=FAKE_KEY)
    )

    assert index_script.main(["--confirm-paid-call", "--staging"]) == 0


def test_indexer_page_ids_allowlist_is_validated(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "database" / "knowledge.db"
    database, ids = _library(database_path, ["文本一"])
    with pytest.raises(ValueError):
        PageEmbeddingIndexer(
            database=database,
            embedding=FakeQwenProvider(),
            model=MODEL,
            dimensions=DIMS,
            config_version=CONFIG,
            page_ids=[ids[0], 999],
        ).index_pages()
    with pytest.raises(ValueError):
        PageEmbeddingIndexer(
            database=database,
            embedding=FakeQwenProvider(),
            model=MODEL,
            dimensions=DIMS,
            config_version=CONFIG,
            page_ids=[0],
        )
