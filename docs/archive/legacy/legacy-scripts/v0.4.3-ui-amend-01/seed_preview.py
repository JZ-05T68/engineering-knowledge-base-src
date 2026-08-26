"""UI-AMEND-01 隔离预览种子：合成 3 页 PDF，走正式 import/notes 服务写入隔离库。

仅用于 D:/ekb-isolated/v0.4.3-ui-amend-01 隔离实例，不进入 Git，不触碰正式数据。
"""

from __future__ import annotations

import fitz

from src.config import Settings
from src.database import Database
from src.document_service import DocumentService
from src.note_service import NoteService

LEVELS = ("primary", "secondary", "normal")
LABELS = {"primary": "重点", "secondary": "次重点", "normal": "一般"}


def main() -> None:
    settings = Settings()
    settings.ensure_directories()
    database = Database(settings.database_path)
    documents = DocumentService(
        database, settings.raw_dir, settings.pages_dir, settings.markdown_dir
    )
    pdf = fitz.open()
    for index in range(3):
        page = pdf.new_page()
        page.insert_text(
            (72, 72),
            f"UI-AMEND-01 演示页面 {index + 1} 液压系统 阀体 回路 压力",
        )
    result = documents.import_pdf(
        pdf.tobytes(), "ui-amend-01-demo.pdf", title="UI-AMEND-01 演示资料"
    )
    notes = NoteService(database)
    for page, level in zip(result.pages, LEVELS, strict=True):
        notes.create_page_note(
            page.id, f"{LABELS[level]}演示笔记：新默认配色预览", importance=level
        )
    preferences = notes.get_display_preferences()
    print("document_id=", result.document.id)
    print(
        "prefs=",
        preferences.color_primary,
        preferences.color_secondary,
        preferences.color_normal,
    )


if __name__ == "__main__":
    main()
