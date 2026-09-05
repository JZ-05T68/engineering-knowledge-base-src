"""Focused tests for the one-click document-reading page."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st

import src.runtime as runtime
from src.models import PageStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMPORT_PAGE = PROJECT_ROOT / "pages" / "1_导入资料.py"


class _Progress:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def progress(self, *args: Any, **kwargs: Any) -> None:
        self.labels.append(str(kwargs.get("text", "")))

    def empty(self) -> None:
        return None


def test_one_click_import_reads_every_page_and_hides_technical_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = tuple(
        SimpleNamespace(id=index, page_number=index, status=PageStatus.PENDING)
        for index in range(1, 4)
    )
    result = SimpleNamespace(
        document=SimpleNamespace(id=7, title="隔离资料"),
        pages=pages,
        duplicate=False,
        import_record=None,
    )

    class Upload:
        name = "隔离资料.docx"
        size = 1024

        @staticmethod
        def getvalue() -> bytes:
            return b"office-document"

    imported: list[dict[str, Any]] = []
    ocr_pages: list[int] = []
    read_documents: list[int] = []
    button_labels: list[str] = []
    success_messages: list[str] = []
    progress = _Progress()

    class Service:
        @staticmethod
        def import_document(**kwargs: Any):
            imported.append(kwargs)
            return result

        @staticmethod
        def run_page_ocr(page_id: int) -> None:
            ocr_pages.append(page_id)

    class AgentClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def read_document(self, document_id: int, **kwargs: Any) -> None:
            read_documents.append(document_id)
            callback = kwargs["progress_callback"]
            for position in range(1, 4):
                callback(position, 3)

    def button(label: str, **kwargs: Any) -> bool:
        del kwargs
        button_labels.append(label)
        return label == "让 Agent 读这份资料"

    class Column:
        @staticmethod
        def button(label: str, **kwargs: Any) -> bool:
            return button(label, **kwargs)

    monkeypatch.setattr(runtime, "application_document_service", lambda: Service())
    monkeypatch.setattr(
        runtime,
        "application_settings",
        lambda: SimpleNamespace(
            agent_readings_dir=Path("readings"), ai_llm_model_hard="qwen3.8"
        ),
    )
    monkeypatch.setattr(runtime, "application_database", lambda: object())
    monkeypatch.setattr(runtime, "application_ai_provider", lambda: object())
    monkeypatch.setattr(
        "src.agent.local_client.LocalDocumentAgentClient", AgentClient
    )
    monkeypatch.setattr(st, "set_page_config", lambda **kwargs: None)
    monkeypatch.setattr(st, "title", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "caption", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: Upload())
    monkeypatch.setattr(st, "button", button)
    monkeypatch.setattr(st, "columns", lambda count: [Column() for _ in range(count)])
    monkeypatch.setattr(st, "progress", lambda *args, **kwargs: progress)
    monkeypatch.setattr(
        st,
        "success",
        lambda message, *args, **kwargs: success_messages.append(str(message)),
    )
    monkeypatch.setattr(st, "error", lambda *args, **kwargs: None)

    runpy.run_path(str(IMPORT_PAGE), run_name="__main__")

    assert len(imported) == 1
    assert imported[0]["filename"] == "隔离资料.docx"
    assert "progress_callback" not in imported[0]
    assert ocr_pages == [1, 2, 3]
    assert read_documents == [7]
    assert "让 Agent 读这份资料" in button_labels
    assert "去问 Agent" in button_labels
    assert "查看识别结果（可选）" in button_labels
    assert progress.labels[-1] == "已读完 3 / 3 页"
    assert success_messages[-1] == "资料已经读完，可以去问 Agent 了。"
    assert progress.labels == [
        "正在读取 1 / 3 页",
        "正在读取 2 / 3 页",
        "正在读取 3 / 3 页",
        "已读完 3 / 3 页",
    ]
    visible_text = "\n".join(button_labels + progress.labels + success_messages)
    for hidden_term in ("embedding", "chunk", "RAG", "索引"):
        assert hidden_term not in visible_text


def test_uploader_accepts_pdf_word_and_powerpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploader_options: dict[str, Any] = {}

    def file_uploader(*args: Any, **kwargs: Any) -> None:
        del args
        uploader_options.update(kwargs)
        return None

    monkeypatch.setattr(st, "set_page_config", lambda **kwargs: None)
    monkeypatch.setattr(st, "title", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "caption", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "file_uploader", file_uploader)
    monkeypatch.setattr(st, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(st, "stop", lambda: (_ for _ in ()).throw(SystemExit))

    with pytest.raises(SystemExit):
        runpy.run_path(str(IMPORT_PAGE), run_name="__main__")

    assert uploader_options["type"] == ["pdf", "doc", "docx", "ppt", "pptx"]
