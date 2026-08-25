"""Tests for the knowledge export service (v0.5.3 Phase 6A)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.backup_service import sha256_file
from src.database import Database
from src.knowledge_export_service import (
    KNOWLEDGE_EXPORT_FORMAT_VERSION,
    KnowledgeExportError,
    KnowledgeExportService,
)
from src.knowledge_object_service import KnowledgeObjectService
from src.models import KnowledgeRelationType


def _seeded_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "knowledge.db")
    source_pdf = tmp_path / "manual.pdf"
    source_pdf.write_bytes(b"%PDF-1.4")
    document = database.create_document(
        title="手册",
        filename="manual.pdf",
        source_path=source_pdf,
        sha256=sha256_file(source_pdf),
        page_count=1,
    )
    image = tmp_path / "page-1.png"
    image.write_bytes(b"\x89PNG")
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=image,
        extracted_text="编码器 A/B 相接反。",
    )
    service = KnowledgeObjectService(database)
    first = service.create(
        kind="experience", title="经验甲", content="内容甲",
        epistemic_basis="personal_experience",
    ).knowledge_object
    second = service.create(
        kind="fact", title="事实乙", content="内容乙",
        epistemic_basis="source_derived",
    ).knowledge_object
    service.link_source(first.id, source_type="page", source_id=page.id)
    service.add_relation(first.id, second.id, relation_type=KnowledgeRelationType.SUPPORTS)
    service.supersede(second.id, first.id)
    database.create_knowledge_memory_entry(
        kind="experience", title="记忆甲", content="记忆内容", document_id=document.id,
        page_id=page.id,
    )
    return database


def test_empty_knowledge_export(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    result = KnowledgeExportService(database).export(tmp_path / "out", app_version="0.5.2")

    assert result.object_count == 0
    manifest = json.loads((result.export_path / "manifest.json").read_text("utf-8"))
    assert manifest["knowledge_export_format_version"] == KNOWLEDGE_EXPORT_FORMAT_VERSION
    assert manifest["counts"]["knowledge_objects"] == 0


def test_full_export_structure_and_lossless_json(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path)
    result = KnowledgeExportService(database).export(tmp_path / "out", app_version="0.5.2")
    root = result.export_path

    for name in (
        "manifest.json",
        "knowledge_objects.json",
        "knowledge_object_sources.json",
        "knowledge_relations.json",
        "knowledge_memory_entries.json",
        "knowledge_object_revisions.json",
        "files.json",
    ):
        assert (root / name).is_file(), name
    objects = json.loads((root / "knowledge_objects.json").read_text("utf-8"))
    relations = json.loads((root / "knowledge_relations.json").read_text("utf-8"))
    memories = json.loads((root / "knowledge_memory_entries.json").read_text("utf-8"))

    assert len(objects) == 2
    by_title = {item["title"]: item for item in objects}
    assert by_title["事实乙"]["superseded_by"] == by_title["经验甲"]["stable_id"]
    assert relations[0]["source_stable_id"] == by_title["经验甲"]["stable_id"]
    assert relations[0]["target_stable_id"] == by_title["事实乙"]["stable_id"]
    assert memories[0]["kind"] == "experience"
    assert memories[0]["stable_id"].startswith(database.get_knowledge_base_uuid())
    markdown_files = sorted(path.name for path in (root / "markdown").glob("*.md"))
    assert len(markdown_files) == 3
    inventory = json.loads((root / "files.json").read_text("utf-8"))
    for record in inventory:
        target = root / record["path"]
        assert sha256_file(target) == record["sha256"]


def test_export_contains_no_forbidden_data(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path)
    result = KnowledgeExportService(database).export(tmp_path / "out", app_version="0.5.2")

    text = "\n".join(
        path.read_text("utf-8", errors="ignore")
        for path in result.export_path.rglob("*")
        if path.is_file()
    ).lower()
    for forbidden in ("ai_calls", "embedding", "api_key", "prompt", "回答"):
        assert forbidden not in text


def test_export_is_deterministic_in_entity_order(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path)
    first = KnowledgeExportService(database).export(tmp_path / "a", app_version="0.5.2")
    second = KnowledgeExportService(database).export(tmp_path / "b", app_version="0.5.2")

    for name in (
        "knowledge_objects.json",
        "knowledge_relations.json",
        "knowledge_memory_entries.json",
        "knowledge_object_revisions.json",
    ):
        assert (first.export_path / name).read_text("utf-8") == (
            second.export_path / name
        ).read_text("utf-8")


def test_output_conflict_is_rejected(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path)
    service = KnowledgeExportService(database)
    service.export(tmp_path / "out", app_version="0.5.2", export_name="dup")

    with pytest.raises(KnowledgeExportError, match="不会覆盖"):
        service.export(tmp_path / "out", app_version="0.5.2", export_name="dup")
