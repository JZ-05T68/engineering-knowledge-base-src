"""Local OCR engine contract and no-engine fallback semantics.

This module defines the minimal boundary between the application and a
local OCR engine:

- ``OcrEngine`` is the protocol a concrete local engine adapter must
  satisfy;
- ``OcrUnavailable`` expresses that no usable local engine exists (not
  installed, runtime dependency missing, or initialization failed);
- ``OcrExecutionError`` expresses that an available engine failed to
  recognize one specific image;
- ``require_ocr_engine`` turns an absent engine (``None``) into a stable
  ``OcrUnavailable`` instead of ``AttributeError`` or silent empty text.

The module intentionally performs no I/O at import time: it loads no
models, scans no disks, touches no network, reads no database, and
imports no third-party OCR package. Concrete adapters (for example a
future RapidOCR adapter) live outside this module and implement the
``OcrEngine`` protocol.

Eligibility decisions stay in ``src.ocr_policy``; this module only
describes the engine call boundary and its failure semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "OcrEngine",
    "OcrExecutionError",
    "OcrUnavailable",
    "require_ocr_engine",
]


@runtime_checkable
class OcrEngine(Protocol):
    """Minimal contract of a local OCR engine adapter.

    The engine receives the path of one local rendered page image and
    returns the recognized raw text. It does not return Markdown, page
    status, confidence matrices or coordinates; it does not rotate, crop
    or enhance the image; it never mutates the input file, never touches
    the database, and never decides whether a page is eligible for OCR.
    """

    def recognize(self, image_path: Path) -> str:
        """Return the raw text recognized from one local page image.

        Implementations raise ``OcrUnavailable`` when the engine itself
        cannot run at all, and ``OcrExecutionError`` when this particular
        recognition attempt fails. An empty string simply means the image
        yielded no text and is not an error.
        """
        ...


class OcrUnavailable(RuntimeError):
    """No usable local OCR engine exists or it cannot finish initializing.

    This covers a missing engine package, missing local runtime
    dependencies, or initialization failure. It is deliberately distinct
    from per-image recognition failures so callers can tell "no engine"
    apart from "this page failed". Messages carry no API keys, remote
    service references, or download instructions.
    """


class OcrExecutionError(RuntimeError):
    """An available OCR engine failed to recognize one specific image."""


def require_ocr_engine(engine: OcrEngine | None) -> OcrEngine:
    """Return the given engine, or raise ``OcrUnavailable`` when absent.

    Callers that receive an optional engine use this to convert the
    no-engine state into one explicit, typed failure instead of an
    ``AttributeError`` on ``None`` or a silently empty result. A real
    engine instance is returned unchanged — never wrapped or replaced.
    """

    if engine is None:
        raise OcrUnavailable("本地 OCR 引擎不可用：尚未安装本地 OCR 引擎，或引擎初始化失败。")
    return engine
