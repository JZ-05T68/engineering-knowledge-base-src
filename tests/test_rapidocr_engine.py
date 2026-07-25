"""Unit tests for the offline RapidOCR adapter in ``src.rapidocr_engine``.

All tests use fake in-memory ``rapidocr`` modules only: no real models,
no network, no subprocess, and no third-party OCR package is loaded.
"""

from __future__ import annotations

import ast
import hashlib
import sys
import types
from pathlib import Path

import pytest

import src.rapidocr_engine as rapidocr_engine_module
from src.ocr_engine import OcrExecutionError, OcrUnavailable
from src.rapidocr_engine import RapidOcrEngine


class _FakeResult:
    """Minimal stand-in for a RapidOCR result object."""

    def __init__(self, txts: object) -> None:
        self.txts = txts

    def vis(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("vis() 不得被适配器调用")


class _FakeRapidOCR:
    """Fake RapidOCR class recording initialization and calls."""

    def __init__(self, behavior: dict[str, object]) -> None:
        behavior["init_count"] = int(behavior.get("init_count", 0)) + 1
        self.behavior = behavior
        if behavior.get("init_error") is not None:
            raise behavior["init_error"]  # type: ignore[misc]

    def __call__(self, image: object) -> object:
        self.behavior.setdefault("calls", []).append(image)  # type: ignore[union-attr]
        if self.behavior.get("call_error") is not None:
            raise self.behavior["call_error"]  # type: ignore[misc]
        return _FakeResult(self.behavior.get("txts", ("第一行", "第二行")))


def _install_fake_rapidocr(
    monkeypatch: pytest.MonkeyPatch,
    **behavior: object,
) -> dict[str, object]:
    """Register a fake ``rapidocr`` module and return the behavior record."""

    record: dict[str, object] = dict(behavior)
    fake_module = types.ModuleType("rapidocr")

    def factory() -> _FakeRapidOCR:
        return _FakeRapidOCR(record)

    fake_module.RapidOCR = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapidocr", fake_module)
    return record


def _calls(record: dict[str, object]) -> list[object]:
    return record.get("calls", [])  # type: ignore[return-value]


def test_module_has_no_third_party_import_at_top_level() -> None:
    tree = ast.parse(
        Path(rapidocr_engine_module.__file__).read_text(encoding="utf-8")
    )
    checked = 0
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        checked += 1
        assert not any(
            name.split(".")[0] in {"rapidocr", "rapidocr_onnxruntime", "onnxruntime"}
            for name in names
        )
    assert checked > 0


def test_module_source_has_no_network_or_subprocess_usage() -> None:
    source = Path(rapidocr_engine_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "socket", "urllib", "requests", "urlopen", "http"):
        assert forbidden not in source


def test_construction_does_not_initialize_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _install_fake_rapidocr(monkeypatch)
    engine = RapidOcrEngine()
    assert record.get("init_count", 0) == 0
    assert _calls(record) == []
    assert engine._engine is None


def test_first_recognize_initializes_once_and_reuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _install_fake_rapidocr(monkeypatch)
    engine = RapidOcrEngine()
    assert engine.recognize(Path("a.png")) == "第一行\n第二行"
    assert engine.recognize(Path("b.png")) == "第一行\n第二行"
    assert record["init_count"] == 1
    assert len(_calls(record)) == 2


def test_input_path_is_passed_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _install_fake_rapidocr(monkeypatch)
    engine = RapidOcrEngine()
    image_path = Path("data/pages/doc-1/page_0002.png")
    engine.recognize(image_path)
    assert _calls(record) == [image_path]
    assert _calls(record)[0] is image_path


def test_txts_tuple_is_joined_in_reading_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_rapidocr(monkeypatch, txts=("第一行", "第二行"))
    assert RapidOcrEngine().recognize(Path("page.png")) == "第一行\n第二行"


def test_none_blank_and_non_string_items_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_rapidocr(monkeypatch, txts=[" 第一行 ", None, "   ", 123, "第二行", ""])
    assert RapidOcrEngine().recognize(Path("page.png")) == "第一行\n第二行"


def test_txts_none_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_rapidocr(monkeypatch, txts=None)
    assert RapidOcrEngine().recognize(Path("blank.png")) == ""


def test_empty_txts_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_rapidocr(monkeypatch, txts=())
    assert RapidOcrEngine().recognize(Path("blank.png")) == ""


def test_missing_package_maps_to_ocr_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    engine = RapidOcrEngine()
    with pytest.raises(OcrUnavailable) as excinfo:
        engine.recognize(Path("page.png"))
    message = str(excinfo.value)
    assert "初始化失败" in message
    assert "pip" not in message
    assert "page.png" not in message


def test_initialization_failure_maps_to_ocr_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "secret-model-dir"
    _install_fake_rapidocr(monkeypatch, init_error=RuntimeError(f"模型损坏：{secret}"))
    engine = RapidOcrEngine()
    with pytest.raises(OcrUnavailable) as excinfo:
        engine.recognize(tmp_path / "page.png")
    assert str(secret) not in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_recognition_failure_maps_to_ocr_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_rapidocr(monkeypatch, call_error=ValueError("decode failed"))
    engine = RapidOcrEngine()
    with pytest.raises(OcrExecutionError) as excinfo:
        engine.recognize(Path("page.png"))
    assert not isinstance(excinfo.value, OcrUnavailable)
    assert excinfo.value.__cause__ is not None


def test_error_message_never_contains_image_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image_path = tmp_path / "private" / "page_0007.png"
    _install_fake_rapidocr(
        monkeypatch, call_error=RuntimeError(f"无法读取 {image_path}")
    )
    engine = RapidOcrEngine()
    with pytest.raises(OcrExecutionError) as excinfo:
        engine.recognize(image_path)
    message = str(excinfo.value)
    assert str(image_path) not in message
    assert "page_0007" not in message
    assert "private" not in message


def test_keyboard_interrupt_and_system_exit_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for fatal in (KeyboardInterrupt, SystemExit):
        _install_fake_rapidocr(monkeypatch, call_error=fatal("stop"))
        with pytest.raises(fatal):
            RapidOcrEngine().recognize(Path("page.png"))


def test_vis_is_never_called(monkeypatch: pytest.MonkeyPatch) -> None:
    # _FakeResult.vis raises AssertionError if the adapter ever calls it.
    _install_fake_rapidocr(monkeypatch, txts=("文本",))
    assert RapidOcrEngine().recognize(Path("page.png")) == "文本"


def test_input_file_is_not_modified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake-png-bytes")
    before = hashlib.sha256(image_path.read_bytes()).hexdigest()
    _install_fake_rapidocr(monkeypatch)
    RapidOcrEngine().recognize(image_path)
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == before


def test_invalid_result_is_never_stringified_into_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in (123, "整段字符串", {"txts": ("文本",)}):
        _install_fake_rapidocr(monkeypatch, txts=bad)
        with pytest.raises(OcrExecutionError) as excinfo:
            RapidOcrEngine().recognize(Path("page.png"))
        assert str(bad) not in str(excinfo.value)


def test_none_result_and_missing_txts_map_to_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoTxts:
        pass

    for bad_result in (None, _NoTxts()):
        fake_module = types.ModuleType("rapidocr")

        class _Engine:
            def __call__(self, image: object, _result: object = bad_result) -> object:
                return _result

        fake_module.RapidOCR = _Engine  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "rapidocr", fake_module)
        with pytest.raises(OcrExecutionError):
            RapidOcrEngine().recognize(Path("page.png"))
