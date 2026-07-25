"""Offline RapidOCR adapter implementing the local OCR engine contract.

This module is the only place where the third-party ``rapidocr`` package
is touched. It stays fully local and offline: recognition runs against
the default models bundled inside the installed package, with no network
access, no model downloader, no API key, and no external processes.

Lazy initialization keeps application startup cheap:

- importing this module loads no third-party OCR package and no models;
- constructing :class:`RapidOcrEngine` performs no heavy work;
- the first :meth:`RapidOcrEngine.recognize` call imports ``rapidocr``
  and builds the engine once, and the instance is then reused.

Failure semantics follow ``src.ocr_engine``: a missing package or a
failed initialization raises ``OcrUnavailable``; a per-image recognition
failure raises ``OcrExecutionError``. Both use fixed, sanitized Chinese
messages that never contain local paths, tracebacks, or download
instructions; the original exception is kept only as ``__cause__`` for
programmatic debugging and is never persisted or displayed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.ocr_engine import OcrExecutionError, OcrUnavailable

__all__ = ["RapidOcrEngine"]

LOGGER = logging.getLogger(__name__)

_ENGINE_UNAVAILABLE_MESSAGE = "本地 OCR 引擎初始化失败，请检查项目 OCR 依赖是否完整安装。"
_RECOGNITION_FAILED_MESSAGE = "本地 OCR 识别当前页面失败；原始资料和人工内容未被修改。"
_INVALID_RESULT_MESSAGE = "本地 OCR 引擎返回了无法解释的结果。"


class RapidOcrEngine:
    """Lazy offline adapter around the locally installed RapidOCR engine.

    The adapter receives the path of one local rendered page image and
    returns only the recognized text lines joined by ``\\n`` in reading
    order. It never mutates or copies the input file, never writes
    visualization images, and never returns coordinates, confidence
    scores, or Markdown. An empty string means the page yielded no valid
    text and is not an error.
    """

    def __init__(self) -> None:
        self._engine: Any | None = None

    def recognize(self, image_path: Path) -> str:
        """Return reading-order text recognized from one local page image.

        Raises ``OcrUnavailable`` when the local engine cannot be set up
        at all, and ``OcrExecutionError`` when this particular image
        fails or the engine returns an unexplainable result object.
        ``KeyboardInterrupt`` and ``SystemExit`` always propagate.
        """

        engine = self._ensure_engine()
        try:
            result = engine(image_path)
        except Exception as exc:
            raise OcrExecutionError(_RECOGNITION_FAILED_MESSAGE) from exc
        return self._extract_text(result)

    def _ensure_engine(self) -> Any:
        """Import and build the RapidOCR engine once, then reuse it."""

        if self._engine is not None:
            return self._engine
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise OcrUnavailable(_ENGINE_UNAVAILABLE_MESSAGE) from exc
        try:
            self._engine = RapidOCR()
        except Exception as exc:
            raise OcrUnavailable(_ENGINE_UNAVAILABLE_MESSAGE) from exc
        LOGGER.info("本地 OCR 引擎初始化完成。")
        return self._engine

    @staticmethod
    def _extract_text(result: Any) -> str:
        """Turn one RapidOCR result object into plain multi-line text.

        Only ``result.txts`` is read. ``None`` or empty ``txts`` yields
        an empty string; ``None``, blank, or non-string items are
        skipped. A missing ``txts`` attribute or a non-sequence ``txts``
        violates the expected output contract and raises
        ``OcrExecutionError`` — the foreign object is never stringified
        into page content.
        """

        if result is None or not hasattr(result, "txts"):
            raise OcrExecutionError(_INVALID_RESULT_MESSAGE)
        txts = result.txts
        if txts is None:
            return ""
        if not isinstance(txts, (list, tuple)):
            raise OcrExecutionError(_INVALID_RESULT_MESSAGE)
        lines = [item.strip() for item in txts if isinstance(item, str) and item.strip()]
        return "\n".join(lines)
