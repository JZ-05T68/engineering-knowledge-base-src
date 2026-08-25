"""AppTest UI tests for the read-only AI call ledger dashboard (Phase 5)."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.ai.provider import AiCallRecord
from src.ai_ledger_service import AILedgerService
from src.database import Database

LEDGER_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("16_*.py")))
KB_UUID = "12345678-1234-1234-1234-123456789abc"


def _make_database(tmp_path: Path) -> Database:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    return Database(database_dir / "knowledge.db")


def _record(
    index: int,
    *,
    capability: str = "completion",
    source_feature: str = "rag_answer",
    status: str = "success",
    model: str = "qwen3.7-plus",
    target_refs: tuple[str, ...] = (),
) -> AiCallRecord:
    return AiCallRecord(
        call_uuid=f"{index:032d}",
        capability=capability,
        model=model,
        prompt_sha256="a" * 64,
        input_chars=10,
        status=status,
        source_feature=source_feature,
        target_refs=target_refs,
        created_at=f"2026-08-25T10:00:{index:02d}",
    )


def _stub(database: Database, monkeypatch) -> AILedgerService:
    service = AILedgerService(database)
    monkeypatch.setattr(runtime, "application_ai_ledger_service", lambda: service)
    monkeypatch.setattr(
        runtime, "application_ai_provider", lambda: (_ for _ in ()).throw(
            AssertionError("台账页面不得调用 AI Provider")
        )
    )
    return service


def test_empty_ledger_shows_empty_state(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    _stub(database, monkeypatch)

    app = AppTest.from_file(LEDGER_PAGE).run(timeout=30)

    assert not app.exception
    assert any("没有任何 AI 调用记录" in info.value for info in app.info)


def test_records_show_status_and_features(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    database.insert_ai_call(_record(1, source_feature="rag_answer"))
    database.insert_ai_call(
        _record(2, source_feature="experience_model", status="error")
    )
    database.insert_ai_call(
        _record(3, capability="embedding", source_feature="page_index")
    )
    _stub(database, monkeypatch)

    app = AppTest.from_file(LEDGER_PAGE).run(timeout=30)

    assert not app.exception
    captions = [item.value for item in app.caption]
    assert any("rag_answer" in value for value in captions)
    assert any("experience_model" in value for value in captions)
    assert any("成功" in value for value in captions)
    assert any("失败" in value for value in captions)
    assert any("Mock 离线演示不写入台账" in value for value in captions)


def test_filter_widgets_exist(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    _stub(database, monkeypatch)

    app = AppTest.from_file(LEDGER_PAGE).run(timeout=30)

    for key in (
        "ledger_source_feature",
        "ledger_capability",
        "ledger_status",
        "ledger_model",
        "ledger_sort",
    ):
        assert any(item.key == key for item in app.selectbox)


def test_target_refs_are_collapsed_and_missing_targets_do_not_crash(
    tmp_path: Path, monkeypatch
) -> None:
    database = _make_database(tmp_path)
    database.insert_ai_call(
        _record(
            1,
            target_refs=(
                f"{KB_UUID}:knowledge_object:1",
                f"{KB_UUID}:knowledge_object:999",
            ),
        )
    )
    _stub(database, monkeypatch)

    app = AppTest.from_file(LEDGER_PAGE).run(timeout=30)

    assert not app.exception
    assert any(
        "target_refs" in expander.label for expander in app.expander
    )
    captions = [item.value for item in app.caption]
    assert any("目标当前不可用" in value for value in captions)


def test_page_load_and_rerun_never_call_provider(
    tmp_path: Path, monkeypatch
) -> None:
    database = _make_database(tmp_path)
    database.insert_ai_call(_record(1))
    _stub(database, monkeypatch)

    app = AppTest.from_file(LEDGER_PAGE).run(timeout=30)
    app.run()

    assert not app.exception


def test_query_exception_is_isolated_to_page(tmp_path: Path, monkeypatch) -> None:
    class _BrokenService:
        def stats(self):
            raise RuntimeError("台账查询失败")

    monkeypatch.setattr(
        runtime, "application_ai_ledger_service", lambda: _BrokenService()
    )

    app = AppTest.from_file(LEDGER_PAGE).run(timeout=30)

    assert not app.exception
    assert any("读取 AI 调用台账失败" in error.value for error in app.error)


def test_no_modify_delete_replay_clear_or_export_buttons(
    tmp_path: Path, monkeypatch
) -> None:
    database = _make_database(tmp_path)
    database.insert_ai_call(_record(1))
    _stub(database, monkeypatch)

    app = AppTest.from_file(LEDGER_PAGE).run(timeout=30)

    labels = [button.label for button in app.button]
    assert all(
        word not in label
        for label in labels
        for word in ("删除", "清空", "导出", "重放", "修改", "重新调用")
    )
