"""Knowledge export service (v0.5.3 Phase 6A).

Produces one deterministic, verified, self-contained knowledge export package:
manifest + lossless JSON entities + one Markdown file per object/memory +
SHA-256 file inventory. The export is business data only: it never includes
the AI call ledger, embedding vectors, prompts, answers, experience
candidates, API keys or runtime caches.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.backup_service import sha256_file
from src.database import Database
from src.migrations import SCHEMA_VERSION
from src.models import (
    KNOWLEDGE_MEMORY_STABLE_TYPE,
    KnowledgeObject,
    KnowledgeObjectSource,
    KnowledgeRelation,
    KnowledgeRevision,
    build_stable_id,
)

LOGGER = logging.getLogger(__name__)

KNOWLEDGE_EXPORT_FORMAT = "engineering-knowledge-base-knowledge-export"
KNOWLEDGE_EXPORT_FORMAT_VERSION = 1
_TARGET_TABLES = {
    "document": "documents",
    "page": "pages",
    "note": "notes",
    "evidence": "evidence_items",
}


class KnowledgeExportError(RuntimeError):
    """Raised when a knowledge export cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class KnowledgeExportResult:
    export_path: Path
    manifest: dict[str, object]
    object_count: int
    memory_count: int
    file_count: int


class KnowledgeExportService:
    """Read-only knowledge export with staging, validation and atomic publish."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def export(
        self,
        target_dir: Path,
        *,
        app_version: str,
        export_name: str | None = None,
    ) -> KnowledgeExportResult:
        root = target_dir.expanduser().resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        destination = root / (export_name or f"knowledge-export-{timestamp}")
        if destination.exists():
            raise KnowledgeExportError(f"导出目标已存在，不会覆盖：{destination}")
        staging = root / f".knowledge-export-{timestamp}.incomplete-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=False)
            payload = self._collect_payload(app_version)
            records: list[dict[str, object]] = []
            for name, value in payload["documents"].items():
                records.append(
                    self._write_json(staging, f"{name}.json", value)
                )
            markdown_dir = staging / "markdown"
            markdown_dir.mkdir()
            for stable_id, text in payload["markdown"].items():
                filename = f"{stable_id.replace(':', '_')}.md"
                records.append(self._write_text(staging, f"markdown/{filename}", text))
            files = self._build_file_inventory(staging, records)
            files.append(self._write_json(staging, "files.json", files))
            files.sort(key=lambda item: str(item["path"]))
            manifest = self._manifest(payload, files)
            self._write_json(staging, "manifest.json", manifest)
            self._validate_inventory(
                staging, files, {str(record["path"]) for record in files}
            )
            os.replace(staging, destination)
        except Exception as exc:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, KnowledgeExportError):
                raise
            raise KnowledgeExportError(f"知识导出失败：{exc}") from exc
        return KnowledgeExportResult(
            export_path=destination,
            manifest=manifest,
            object_count=int(payload["counts"]["knowledge_objects"]),
            memory_count=int(payload["counts"]["knowledge_memory_entries"]),
            file_count=len(files),
        )

    def _collect_payload(self, app_version: str) -> dict[str, object]:
        database = self._database
        kb_uuid = database.get_knowledge_base_uuid()
        objects = sorted(
            self._all_objects(database),
            key=lambda item: database.knowledge_object_stable_id(item.id),
        )
        memories = sorted(
            self._all_memories(database),
            key=lambda item: build_stable_id(
                kb_uuid, KNOWLEDGE_MEMORY_STABLE_TYPE, item.id
            ),
        )
        object_records = [self._object_record(item, kb_uuid) for item in objects]
        source_records: list[dict[str, object]] = []
        relation_records: list[dict[str, object]] = []
        revision_records: list[dict[str, object]] = []
        for item in objects:
            stable_id = database.knowledge_object_stable_id(item.id)
            for source in database.list_knowledge_object_sources(item.id):
                source_records.append(self._source_record(source, stable_id))
            for relation in database.list_knowledge_relations(item.id):
                relation_records.append(
                    self._relation_record(relation, database, kb_uuid)
                )
            for revision in database.list_knowledge_revisions(item.id):
                revision_records.append(self._revision_record(revision))
        relation_records = self._dedupe_records(relation_records, "id")
        revision_records = self._dedupe_records(revision_records, "id")
        deleted_revisions = self._deleted_object_revisions(kb_uuid, object_records)
        revision_records.extend(deleted_revisions)
        revision_records.sort(
            key=lambda item: (
                str(item["knowledge_object_id"]),
                int(item["revision_number"]),
                int(item["id"]),
            )
        )
        memory_records = [self._memory_record(item, kb_uuid) for item in memories]
        markdown: dict[str, str] = {}
        for record in object_records:
            markdown[str(record["stable_id"])] = self._object_markdown(record)
        for record in memory_records:
            markdown[str(record["stable_id"])] = self._memory_markdown(record)
        missing_refs: list[str] = []
        for source in source_records:
            if source["state_at_export"] == "missing":
                missing_refs.append(
                    f"source:{source['source_type']}:{source['source_id']}"
                )
        warnings: list[str] = []
        if missing_refs:
            warnings.append(
                f"{len(missing_refs)} 条来源引用当前不可用，仍保留引用与快照。"
            )
        return {
            "documents": {
                "knowledge_objects": object_records,
                "knowledge_object_sources": source_records,
                "knowledge_relations": relation_records,
                "knowledge_memory_entries": memory_records,
                "knowledge_object_revisions": revision_records,
            },
            "markdown": markdown,
            "counts": {
                "knowledge_objects": len(object_records),
                "knowledge_object_sources": len(source_records),
                "knowledge_relations": len(relation_records),
                "knowledge_memory_entries": len(memory_records),
                "knowledge_object_revisions": len(revision_records),
            },
            "warnings": warnings,
            "missing_references": missing_refs,
            "app_version": app_version,
            "kb_uuid": kb_uuid,
        }

    def _all_objects(self, database: Database) -> list[KnowledgeObject]:
        objects: list[KnowledgeObject] = []
        offset = 0
        while True:
            batch = database.list_knowledge_objects(limit=500, offset=offset)
            objects.extend(batch)
            offset += len(batch)
            if len(batch) < 500:
                break
        return objects

    def _all_memories(self, database: Database) -> list:
        memories: list = []
        offset = 0
        while True:
            batch = database.list_knowledge_memory_entries(limit=500, offset=offset)
            memories.extend(batch)
            offset += len(batch)
            if len(batch) < 500:
                break
        return memories

    def _object_record(
        self, item: KnowledgeObject, kb_uuid: str
    ) -> dict[str, object]:
        stable_id = self._database.knowledge_object_stable_id(item.id)
        return {
            "id": item.id,
            "stable_id": stable_id,
            "kind": item.kind.value,
            "authorship": item.authorship.value,
            "epistemic_basis": item.epistemic_basis.value,
            "title": item.title,
            "content": item.content,
            "importance": item.importance.value,
            "lifecycle": item.lifecycle.value,
            "confirmation_status": item.confirmation_status.value,
            "confirmed_revision": item.confirmed_revision,
            "current_revision": item.current_revision,
            "confirmed_at": _iso(item.confirmed_at),
            "superseded_by": (
                self._database.knowledge_object_stable_id(item.superseded_by_ko_id)
                if item.superseded_by_ko_id is not None
                else None
            ),
            "superseded_by_ko_id": item.superseded_by_ko_id,
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }

    def _memory_record(self, item, kb_uuid: str) -> dict[str, object]:
        return {
            "id": item.id,
            "stable_id": build_stable_id(
                kb_uuid, KNOWLEDGE_MEMORY_STABLE_TYPE, item.id
            ),
            "kind": item.kind.value,
            "title": item.title,
            "content": item.content,
            "root_cause": item.root_cause,
            "lesson": item.lesson,
            "outcome": item.outcome,
            "context_conditions": item.context_conditions,
            "knowledge_object_id": item.knowledge_object_id,
            "document_id": item.document_id,
            "page_id": item.page_id,
            "status": item.status.value,
            "content_revision": item.content_revision,
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }

    def _source_record(
        self, item: KnowledgeObjectSource, object_stable_id: str
    ) -> dict[str, object]:
        return {
            "id": item.id,
            "knowledge_object_id": item.knowledge_object_id,
            "object_stable_id": object_stable_id,
            "source_type": item.source_type.value,
            "source_id": item.source_id,
            "source_note": item.source_note,
            "source_fingerprint": item.source_fingerprint,
            "fingerprint_version": item.fingerprint_version,
            "captured_at": _iso(item.captured_at),
            "state_at_export": self._source_state(
                item.source_type.value, item.source_id
            ),
            "created_at": _iso(item.created_at),
        }

    def _relation_record(
        self, item: KnowledgeRelation, database: Database, kb_uuid: str
    ) -> dict[str, object]:
        return {
            "id": item.id,
            "relation_type": item.relation_type.value,
            "description": item.description,
            "source_ko_id": item.source_ko_id,
            "target_ko_id": item.target_ko_id,
            "source_stable_id": database.knowledge_object_stable_id(
                item.source_ko_id
            ),
            "target_stable_id": database.knowledge_object_stable_id(
                item.target_ko_id
            ),
            "created_at": _iso(item.created_at),
        }

    def _revision_record(self, item: KnowledgeRevision) -> dict[str, object]:
        return {
            "id": item.id,
            "knowledge_object_id": item.knowledge_object_id,
            "object_local_id_snapshot": item.object_local_id_snapshot,
            "object_stable_id_snapshot": item.object_stable_id_snapshot,
            "object_title_snapshot": item.object_title_snapshot,
            "object_kind_snapshot": item.object_kind_snapshot,
            "revision_number": item.revision_number,
            "event_type": item.event_type.value,
            "before_title": item.before_title,
            "after_title": item.after_title,
            "before_content": item.before_content,
            "after_content": item.after_content,
            "before_lifecycle": item.before_lifecycle,
            "after_lifecycle": item.after_lifecycle,
            "before_confirmation": item.before_confirmation,
            "after_confirmation": item.after_confirmation,
            "superseded_by_before": item.superseded_by_before,
            "superseded_by_after": item.superseded_by_after,
            "source_ref": item.source_ref,
            "payload_version": item.payload_version,
            "object_deleted": False,
        }

    def _deleted_object_revisions(
        self, kb_uuid: str, object_records: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        existing_ids = {int(record["id"]) for record in object_records}
        deleted: list[dict[str, object]] = []
        with self._database._connection() as connection:  # noqa: SLF001 - read only
            rows = connection.execute(
                "SELECT knowledge_object_id FROM knowledge_object_revisions "
                "GROUP BY knowledge_object_id ORDER BY knowledge_object_id"
            ).fetchall()
        for row in rows:
            object_id = int(row["knowledge_object_id"])
            if object_id in existing_ids:
                continue
            for revision in self._database.list_knowledge_revisions(object_id):
                record = self._revision_record(revision)
                record["object_deleted"] = True
                if not record.get("object_stable_id_snapshot"):
                    record["object_stable_id_snapshot"] = build_stable_id(
                        kb_uuid, "knowledge_object", object_id
                    )
                deleted.append(record)
        return deleted

    def _source_state(self, source_type: str, source_id: int) -> str:
        table = _TARGET_TABLES.get(source_type)
        if table is None:
            return "unknown"
        try:
            with self._database._connection() as connection:  # noqa: SLF001
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE id = ?", (source_id,)
                ).fetchone()
        except Exception as exc:
            LOGGER.warning("来源状态检查失败：%s", exc)
            return "unknown"
        return "available" if row is not None else "missing"

    def _manifest(
        self, payload: dict[str, object], files: list[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "export_format": KNOWLEDGE_EXPORT_FORMAT,
            "knowledge_export_format_version": KNOWLEDGE_EXPORT_FORMAT_VERSION,
            "application_version": str(payload["app_version"]),
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kb_uuid": str(payload["kb_uuid"]),
            "counts": payload["counts"],
            "warnings": payload["warnings"],
            "missing_references": payload["missing_references"],
            "excluded": [],
            "privacy": (
                "不包含 API Key、模型提示词正文、模型输出正文、"
                "Experience Model 会话状态、向量索引数据或 AI 调用台账。"
            ),
            "files": files,
        }

    def _write_json(
        self, root: Path, name: str, value: object
    ) -> dict[str, object]:
        path = root / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return _record(path, root)

    def _write_text(
        self, root: Path, name: str, value: str
    ) -> dict[str, object]:
        path = root / name
        path.write_text(value, encoding="utf-8")
        return _record(path, root)

    def _build_file_inventory(
        self, root: Path, records: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        files = sorted(records, key=lambda item: str(item["path"]))
        return files

    def _validate_inventory(
        self,
        root: Path,
        files: list[dict[str, object]],
        expected_paths: set[str],
    ) -> None:
        seen: set[str] = set()
        for record in files:
            path = str(record["path"])
            if path in seen:
                raise KnowledgeExportError(f"文件清单重复：{path}")
            seen.add(path)
            target = root / path
            if not target.is_file():
                raise KnowledgeExportError(f"导出文件缺失：{path}")
            if target.stat().st_size != int(record["size"]):
                raise KnowledgeExportError(f"导出文件大小不一致：{path}")
            if sha256_file(target) != str(record["sha256"]):
                raise KnowledgeExportError(f"导出文件哈希不一致：{path}")
        if seen != expected_paths:
            raise KnowledgeExportError("文件清单与声明文件不一致。")

    def _object_markdown(self, record: dict[str, object]) -> str:
        lines = [
            f"# {record['title']}",
            "",
            f"- stable_id：{record['stable_id']}",
            f"- kind：{record['kind']}",
            f"- authorship：{record['authorship']}",
            f"- epistemic_basis：{record['epistemic_basis']}",
            f"- importance：{record['importance']}",
            f"- lifecycle：{record['lifecycle']}",
            f"- confirmation_status：{record['confirmation_status']}",
            f"- superseded_by：{record['superseded_by'] or '无'}",
            "",
            "## 正文",
            "",
            str(record["content"]),
            "",
        ]
        return "\n".join(lines)

    def _memory_markdown(self, record: dict[str, object]) -> str:
        lines = [
            f"# {record['title']}",
            "",
            f"- stable_id：{record['stable_id']}",
            f"- kind：{record['kind']}",
            f"- status：{record['status']}",
            f"- content_revision：{record['content_revision']}",
            "",
            "## 内容",
            "",
            str(record["content"]),
            "",
            f"## 根因\n\n{record['root_cause'] or '（无）'}\n",
            f"## 经验教训\n\n{record['lesson'] or '（无）'}\n",
            f"## 结果\n\n{record['outcome'] or '（无）'}\n",
            f"## 适用条件\n\n{record['context_conditions'] or '（无）'}\n",
        ]
        return "\n".join(lines)

    def _dedupe_records(
        self, records: list[dict[str, object]], key: str
    ) -> list[dict[str, object]]:
        seen: set[object] = set()
        result: list[dict[str, object]] = []
        for record in records:
            value = record[key]
            if value in seen:
                continue
            seen.add(value)
            result.append(record)
        return result


def _record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None
