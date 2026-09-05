"""Word/PowerPoint ingestion through the existing page-level PDF pipeline."""

from __future__ import annotations

import hashlib
from pathlib import Path

import fitz
import pytest

from src.database import Database
from src.document_service import DocumentImportError, DocumentService


class _PdfWritingConverter:
    def __init__(self) -> None:
        self.sources: list[Path] = []

    def convert(self, source_path: Path | str, output_path: Path | str) -> Path:
        source = Path(source_path)
        output = Path(output_path)
        self.sources.append(source)
        document = fitz.open()
        for page_number in range(1, 3):
            page = document.new_page()
            page.insert_text(
                (72, 72),
                f"Office import page {page_number}",
                fontsize=12,
            )
        document.save(output)
        document.close()
        return output


def _service(tmp_path: Path, converter: _PdfWritingConverter) -> DocumentService:
    return DocumentService(
        database=Database(tmp_path / "data" / "database" / "knowledge.db"),
        raw_dir=tmp_path / "data" / "raw",
        pages_dir=tmp_path / "data" / "pages",
        markdown_dir=tmp_path / "data" / "markdown",
        office_converter=converter,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("filename", ["学习计划.docx", "课堂讲义.doc", "项目介绍.pptx", "答辩.ppt"])
def test_office_document_is_preserved_and_imported_as_pages(
    tmp_path: Path, filename: str
) -> None:
    converter = _PdfWritingConverter()
    service = _service(tmp_path, converter)
    content = f"local original for {filename}".encode()
    progress: list[tuple[int, int]] = []

    result = service.import_document(
        content,
        filename,
        progress_callback=lambda current, total: progress.append((current, total)),
    )

    assert result.document.title == Path(filename).stem
    assert result.document.page_count == 2
    assert [page.page_number for page in result.pages] == [1, 2]
    assert progress == [(1, 2), (2, 2)]
    assert len(converter.sources) == 1
    original = converter.sources[0]
    assert original.suffix.lower() == Path(filename).suffix.lower()
    assert original.parent.name == "originals"
    assert original.read_bytes() == content
    assert original.name.startswith(hashlib.sha256(content).hexdigest())


def test_import_document_rejects_unknown_file_type(tmp_path: Path) -> None:
    service = _service(tmp_path, _PdfWritingConverter())

    with pytest.raises(DocumentImportError, match="支持 PDF、Word 和 PowerPoint"):
        service.import_document(b"data", "notes.txt")
