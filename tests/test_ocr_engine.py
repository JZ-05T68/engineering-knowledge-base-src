"""Tests for the local OCR engine contract in ``src.ocr_engine``.

All tests use in-memory fakes only: no real OCR software, models, network
access, system installation, or subprocess is involved.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import src.ocr_engine as ocr_engine_module
from src.ocr_engine import (
    OcrEngine,
    OcrExecutionError,
    OcrUnavailable,
    require_ocr_engine,
)


class FakeOcrEngine:
    """Duck-typed fake engine recording calls and returning fixed text."""

    def __init__(self, result: str = "识别文本") -> None:
        self.result = result
        self.calls: list[Path] = []

    def recognize(self, image_path: Path) -> str:
        self.calls.append(image_path)
        return self.result


class FailingOcrEngine:
    """Fake engine whose recognition attempt fails with a plain error."""

    def recognize(self, image_path: Path) -> str:
        raise OcrExecutionError(f"无法识别图像：{image_path.name}")


def test_fake_engine_satisfies_protocol_and_returns_text() -> None:
    engine = FakeOcrEngine(result="第 3 页内容")
    assert isinstance(engine, OcrEngine)
    assert engine.recognize(Path("page-3.png")) == "第 3 页内容"


def test_missing_engine_raises_ocr_unavailable() -> None:
    with pytest.raises(OcrUnavailable):
        require_ocr_engine(None)


def test_ocr_unavailable_is_distinct_runtime_error() -> None:
    assert issubclass(OcrUnavailable, RuntimeError)
    assert OcrUnavailable is not RuntimeError
    assert not issubclass(OcrExecutionError, OcrUnavailable)
    assert not issubclass(OcrUnavailable, OcrExecutionError)


def test_ocr_unavailable_message_is_non_empty() -> None:
    with pytest.raises(OcrUnavailable) as excinfo:
        require_ocr_engine(None)
    assert str(excinfo.value).strip()


def test_unavailable_path_never_returns_empty_string_silently() -> None:
    try:
        require_ocr_engine(None)
    except OcrUnavailable:
        return
    pytest.fail("无引擎状态必须抛出 OcrUnavailable，不能静默返回空字符串")


def test_empty_recognition_result_is_not_mistaken_for_unavailable() -> None:
    engine = require_ocr_engine(FakeOcrEngine(result=""))
    assert engine.recognize(Path("blank.png")) == ""


def test_execution_failure_is_not_converted_to_unavailable() -> None:
    engine = require_ocr_engine(FailingOcrEngine())
    with pytest.raises(OcrExecutionError):
        engine.recognize(Path("broken.png"))


def test_input_path_is_passed_through_unchanged() -> None:
    engine = FakeOcrEngine()
    image_path = Path("data/pages/doc-1/page-2.png")
    engine.recognize(image_path)
    assert engine.calls == [image_path]
    assert engine.calls[0] is image_path


def test_same_engine_is_reusable_across_calls() -> None:
    engine = FakeOcrEngine(result="重复调用")
    first = engine.recognize(Path("a.png"))
    second = engine.recognize(Path("b.png"))
    assert first == second == "重复调用"
    assert len(engine.calls) == 2


def test_valid_engine_is_returned_as_is() -> None:
    engine = FakeOcrEngine()
    resolved = require_ocr_engine(engine)
    assert resolved is engine


def test_importing_module_loads_no_ocr_third_party_packages() -> None:
    forbidden = {
        "rapidocr",
        "rapidocr_onnxruntime",
        "onnxruntime",
        "paddleocr",
        "pytesseract",
        "easyocr",
    }
    assert forbidden.isdisjoint(sys.modules)


def test_module_source_has_no_network_or_subprocess_usage() -> None:
    source = Path(ocr_engine_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "socket", "urllib", "requests", "http"):
        assert forbidden not in source


def test_import_has_no_filesystem_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    # reload re-executes class definitions inside the shared module object;
    # snapshot and restore so later tests keep the original class identity.
    snapshot = dict(ocr_engine_module.__dict__)
    try:
        importlib.reload(ocr_engine_module)
        assert list(tmp_path.iterdir()) == []
    finally:
        ocr_engine_module.__dict__.clear()
        ocr_engine_module.__dict__.update(snapshot)


def test_module_coimports_with_ocr_policy() -> None:
    import src.ocr_policy  # noqa: F401


def test_runtime_and_document_service_import_without_ocr_packages() -> None:
    import src.document_service  # noqa: F401
    import src.runtime  # noqa: F401
