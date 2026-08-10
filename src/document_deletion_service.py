"""Staged, verifiable deletion of one imported document and its own files.

The deletion never deletes anything in place. Recorded files are first
moved into a per-operation quarantine directory
(``.deletion-quarantine/op-<uuid>/``) with same-volume atomic renames —
but only after an atomically written ``manifest.json`` (operation id,
document id, and every planned file with its original path, quarantine
path, size and SHA-256) is reliably on disk, so an interrupted operation
can always be settled later by :mod:`src.deletion_recovery`. Only after
the single-transaction database delete (including per-table residue checks
and ``PRAGMA foreign_key_check``) has committed is the quarantine removed
permanently. Any failure before the commit rolls the database back and
moves every quarantined file back to its original location, verifying
presence and size. Files not recorded as belonging to the document are
never touched, and projects/tags are shared entities that always survive.
Follows the established project discipline: connections go through
``Database._connection()``, all user-facing errors are explicit Chinese
messages, and no exception is ever swallowed silently.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from src.database import Database, DatabaseError
from src.deletion_recovery import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    QUARANTINE_DIR_NAME,
    sha256_file,
    write_json_atomic,
)
from src.models import (
    DocumentDeletionFile,
    DocumentDeletionPreview,
    DocumentDeletionResult,
)

LOGGER = logging.getLogger(__name__)


class DocumentDeletionError(DatabaseError):
    """A document deletion was refused, aborted, or only partially recoverable."""


class DocumentDeletionService:
    """Preview and execute the complete, staged deletion of one document."""

    def __init__(
        self,
        *,
        database: Database,
        raw_dir: Path,
        pages_dir: Path,
        markdown_dir: Path,
        data_dir: Path,
        app_version: str = "",
    ) -> None:
        self._database = database
        self._raw_dir = Path(raw_dir)
        self._pages_dir = Path(pages_dir)
        self._markdown_dir = Path(markdown_dir)
        self._data_dir = Path(data_dir)
        self._app_version = app_version
        self._quarantine_root = self._data_dir / QUARANTINE_DIR_NAME

    # ------------------------------------------------------------- preview
    def preview_document_deletion(self, document_id: int) -> DocumentDeletionPreview:
        """Build a read-only deletion impact summary without writing anything.

        Raises :class:`DocumentDeletionError` when the document does not
        exist. Every recorded file path is validated against its owning root
        directory; suspicious paths are reported in ``path_anomalies`` and
        abort any later deletion instead of being followed.
        """

        document = self._database.get_document(document_id)
        if document is None:
            raise DocumentDeletionError(f"找不到文档：{document_id}")
        pages = self._database.list_pages(document_id)
        page_ids = [page.id for page in pages]
        placeholders = ",".join("?" for _ in page_ids) or "NULL"

        with self._database._connection() as connection:
            note_counts = {"document": 0, "page": 0, "text_selection": 0, "image_region": 0}
            row = connection.execute(
                "SELECT COUNT(*) FROM notes WHERE document_id = ?", (document_id,)
            ).fetchone()
            note_counts["document"] = int(row[0])
            for row in connection.execute(
                f"SELECT note_type, COUNT(*) FROM notes "
                f"WHERE page_id IN ({placeholders}) GROUP BY note_type",
                page_ids,
            ).fetchall():
                note_counts[str(row[0])] = int(row[1])
            evidence_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM evidence_items "
                    f"WHERE document_id = ? OR page_id IN ({placeholders})",
                    (document_id, *page_ids),
                ).fetchone()[0]
            )
            search_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM page_search WHERE rowid IN ({placeholders})",
                    page_ids,
                ).fetchone()[0]
            )
            association_count = sum(
                int(connection.execute(query, parameters).fetchone()[0])
                for query, parameters in (
                    ("SELECT COUNT(*) FROM document_tags WHERE document_id = ?", (document_id,)),
                    (
                        f"SELECT COUNT(*) FROM page_tags WHERE page_id IN ({placeholders})",
                        page_ids,
                    ),
                    (
                        "SELECT COUNT(*) FROM project_documents WHERE document_id = ?",
                        (document_id,),
                    ),
                    (
                        f"SELECT COUNT(*) FROM project_pages WHERE page_id IN ({placeholders})",
                        page_ids,
                    ),
                )
            )
            import_record_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM import_records WHERE document_id = ?",
                    (document_id,),
                ).fetchone()[0]
            )

        files, missing_files, path_anomalies, total_size = self._collect_files(
            document_id=document_id,
            source_path=document.source_path,
            image_paths=[page.image_path for page in pages],
            markdown_paths=[
                page.markdown_path for page in pages if page.markdown_path is not None
            ],
        )
        return DocumentDeletionPreview(
            document_id=document.id,
            document_title=document.title,
            page_count=len(pages),
            document_note_count=note_counts["document"],
            page_note_count=note_counts["page"],
            text_selection_note_count=note_counts["text_selection"],
            image_region_note_count=note_counts["image_region"],
            evidence_item_count=evidence_count,
            search_record_count=search_count,
            association_count=association_count,
            import_record_count=import_record_count,
            files=files,
            total_size_bytes=total_size,
            missing_files=missing_files,
            path_anomalies=path_anomalies,
        )

    # ------------------------------------------------------------ deletion
    def delete_document(self, document_id: int) -> DocumentDeletionResult:
        """Delete one document in verifiable stages, never faking success.

        Stage 1 re-validates the document and every recorded path; any
        anomaly aborts before anything is touched. Stage 2 writes the
        per-operation quarantine manifest (every planned file with its
        SHA-256) atomically — before the first file moves, so a crash at
        any later point is recoverable by :mod:`src.deletion_recovery`.
        Stage 3 moves recorded files into the operation directory with
        atomic same-volume renames. Stage 4 runs the single cascading
        ``DELETE`` plus per-table residue checks in one transaction,
        rolling back on any surprise. Stage 5-6 permanently remove the
        quarantine only after the commit.
        """

        preview = self.preview_document_deletion(document_id)
        if preview.path_anomalies:
            raise DocumentDeletionError(
                "检测到路径异常，已中止删除，未改动任何数据："
                + "；".join(preview.path_anomalies)
            )

        operation_id = uuid4().hex
        quarantine_dir = self._quarantine_root / f"op-{operation_id}"
        files_dir = quarantine_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=False)

        movable = [entry for entry in preview.files if entry.exists]
        manifest_files = []
        for index, entry in enumerate(movable):
            manifest_files.append(
                {
                    "original_path": str(entry.path),
                    "quarantine_path": str(files_dir / f"{index:04d}-{entry.path.name}"),
                    "size_bytes": entry.size_bytes,
                    "sha256": sha256_file(entry.path),
                }
            )
        manifest_path = quarantine_dir / MANIFEST_NAME
        manifest = {
            "version": MANIFEST_VERSION,
            "operation_id": operation_id,
            "document_id": document_id,
            "document_title": preview.document_title,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "app_version": self._app_version,
            # Diagnostic only; recovery decisions never read this field.
            "phase": "prepared",
            "files": manifest_files,
        }
        try:
            write_json_atomic(manifest_path, manifest)
        except OSError as exc:
            try:
                shutil.rmtree(quarantine_dir)
            except OSError:
                LOGGER.warning("中止后隔离目录未能移除，已保留：%s", quarantine_dir)
            raise DocumentDeletionError(
                f"写入删除操作 manifest 失败，已中止删除，未改动任何数据：{exc}"
            ) from exc

        moved: list[tuple[Path, Path, int | None]] = []
        try:
            for entry in manifest_files:
                original = Path(entry["original_path"])
                destination = Path(entry["quarantine_path"])
                os.replace(original, destination)
                moved.append((original, destination, entry["size_bytes"]))
        except OSError as exc:
            self._abort_with_restore(
                moved,
                quarantine_dir,
                f"移动文件到隔离目录失败，已中止删除：{entry['original_path']}（{exc}）",
            )
        self._update_manifest_phase(manifest_path, manifest, "quarantined")

        try:
            self._delete_document_records(document_id)
        except Exception as exc:
            self._abort_with_restore(
                moved,
                quarantine_dir,
                f"删除文档数据库记录失败，已回滚数据库：{exc}",
            )
        self._update_manifest_phase(manifest_path, manifest, "db_committed")

        cleanup_warnings: list[str] = []
        try:
            shutil.rmtree(quarantine_dir)
        except OSError as exc:
            warning = f"删除已完成，但隔离目录未能清理，已保留：{quarantine_dir}（{exc}）"
            LOGGER.warning("隔离目录清理失败：%s（%s）", quarantine_dir, exc)
            cleanup_warnings.append(warning)
        for directory in (
            self._pages_dir / str(document_id),
            self._markdown_dir / str(document_id),
        ):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                warning = f"删除已完成，但目录非空（含未登记文件），已保留：{directory}"
                LOGGER.warning("文档目录非空，已保留：%s", directory)
                cleanup_warnings.append(warning)

        LOGGER.info(
            "导入文件已删除：document_id=%s title=%s files=%s warnings=%s",
            document_id,
            preview.document_title,
            len(movable),
            len(cleanup_warnings),
        )
        return DocumentDeletionResult(
            document_id=preview.document_id,
            document_title=preview.document_title,
            preview=preview,
            deleted=True,
            cleanup_warnings=tuple(cleanup_warnings),
        )

    def _update_manifest_phase(
        self, manifest_path: Path, manifest: dict, phase: str
    ) -> None:
        """Best-effort diagnostic phase update; never fails the deletion.

        The phase field is diagnostic-only — crash recovery decides purely
        from the database and the filesystem — so a failed update is logged
        and otherwise ignored. The atomic rewrite guarantees the manifest
        on disk is always either the old or the new complete JSON.
        """

        try:
            write_json_atomic(manifest_path, {**manifest, "phase": phase})
        except OSError as exc:
            LOGGER.warning("更新删除操作 manifest 阶段标记失败：%s（%s）", manifest_path, exc)

    # ------------------------------------------------------- database stage
    def _delete_document_records(self, document_id: int) -> None:
        """Run the cascading delete and verify zero residue in one transaction.

        Mirrors :meth:`Database.delete_document` (one ``DELETE`` relying on
        the verified schema v5 cascades and the FTS cleanup trigger), then
        checks every affected table inside the same connection so any
        surprise rolls the whole transaction back before commit.
        """

        with self._database._connection() as connection:
            page_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM pages WHERE document_id = ?", (document_id,)
                ).fetchall()
            ]
            placeholders = ",".join("?" for _ in page_ids) or "NULL"
            cursor = connection.execute(
                "DELETE FROM documents WHERE id = ?", (document_id,)
            )
            if cursor.rowcount == 0:
                raise DocumentDeletionError(f"找不到文档：{document_id}")
            residue_checks = (
                ("页面记录", "SELECT COUNT(*) FROM pages WHERE document_id = ?", (document_id,)),
                (
                    "文档级笔记",
                    "SELECT COUNT(*) FROM notes WHERE document_id = ?",
                    (document_id,),
                ),
                (
                    "页面笔记",
                    f"SELECT COUNT(*) FROM notes WHERE page_id IN ({placeholders})",
                    page_ids,
                ),
                (
                    "证据项",
                    f"SELECT COUNT(*) FROM evidence_items "
                    f"WHERE document_id = ? OR page_id IN ({placeholders})",
                    (document_id, *page_ids),
                ),
                (
                    "搜索记录",
                    f"SELECT COUNT(*) FROM page_search WHERE rowid IN ({placeholders})",
                    page_ids,
                ),
                (
                    "文档标签关联",
                    "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
                    (document_id,),
                ),
                (
                    "页面标签关联",
                    f"SELECT COUNT(*) FROM page_tags WHERE page_id IN ({placeholders})",
                    page_ids,
                ),
                (
                    "文档项目关联",
                    "SELECT COUNT(*) FROM project_documents WHERE document_id = ?",
                    (document_id,),
                ),
                (
                    "页面项目关联",
                    f"SELECT COUNT(*) FROM project_pages WHERE page_id IN ({placeholders})",
                    page_ids,
                ),
                # Import records survive with document_id set to NULL
                # (ON DELETE SET NULL); none may keep referencing the document.
                (
                    "导入记录引用",
                    "SELECT COUNT(*) FROM import_records WHERE document_id = ?",
                    (document_id,),
                ),
            )
            failures = [
                label
                for label, query, parameters in residue_checks
                if int(connection.execute(query, parameters).fetchone()[0]) != 0
            ]
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                failures.append(f"外键一致性检查（{len(violations)} 条违规）")
            if failures:
                raise DocumentDeletionError(
                    "删除后校验发现残留数据：" + "、".join(failures)
                )

    # ----------------------------------------------------------- file stage
    def _collect_files(
        self,
        *,
        document_id: int,
        source_path: Path,
        image_paths: list[Path],
        markdown_paths: list[Path],
    ) -> tuple[tuple[DocumentDeletionFile, ...], tuple[Path, ...], tuple[str, ...], int]:
        """Validate and measure every recorded file exactly once.

        Paths come only from database records and are validated against
        their owning root. Duplicated records (same resolved path) collapse
        to one entry so nothing is measured or moved twice.
        """

        candidates = [
            (Path(source_path), "pdf", self._raw_dir),
            *(
                (Path(path), "page_image", self._pages_dir / str(document_id))
                for path in image_paths
            ),
            *(
                (Path(path), "markdown", self._markdown_dir / str(document_id))
                for path in markdown_paths
            ),
        ]
        entries: list[DocumentDeletionFile] = []
        missing: list[Path] = []
        anomalies: list[str] = []
        total_size = 0
        seen: set[Path] = set()
        for path, kind, root in candidates:
            anomaly, resolved = self._validate_recorded_path(path, root)
            if anomaly is not None:
                anomalies.append(anomaly)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if path.is_file():
                size = path.stat().st_size
                total_size += size
                entries.append(
                    DocumentDeletionFile(path=path, kind=kind, exists=True, size_bytes=size)
                )
            else:
                missing.append(path)
                entries.append(
                    DocumentDeletionFile(path=path, kind=kind, exists=False, size_bytes=None)
                )
        return tuple(entries), tuple(missing), tuple(anomalies), total_size

    def _validate_recorded_path(
        self, path: Path, root: Path
    ) -> tuple[str | None, Path]:
        """Validate one database-recorded path against its owning root.

        Returns ``(anomaly_message, resolved_path)``; the message is ``None``
        only when the path stays inside ``root`` after resolution, is not a
        data root itself, contains no ``..`` segment, is not a symlink, and
        is not a directory masquerading as a file. No user input is ever
        used to build these paths; deletion aborts on any anomaly instead
        of following the path.
        """

        resolved = path.resolve()
        managed_roots = (
            self._raw_dir.resolve(),
            self._pages_dir.resolve(),
            self._markdown_dir.resolve(),
            self._data_dir.resolve(),
        )
        if ".." in path.parts:
            return f"路径包含“..”段：{path}", resolved
        if resolved in managed_roots:
            return f"路径指向数据根目录本身：{path}", resolved
        root_resolved = root.resolve()
        if not resolved.is_relative_to(root_resolved):
            return f"路径不在其归属目录（{root_resolved}）内：{path}", resolved
        if path.is_symlink():
            return f"路径是符号链接：{path}", resolved
        if path.exists() and not path.is_file():
            return f"路径不是普通文件：{path}", resolved
        return None, resolved

    def _restore_moved_files(
        self, moved: list[tuple[Path, Path, int | None]]
    ) -> list[str]:
        """Move every quarantined file back and verify presence and size."""

        failures: list[str] = []
        for original, quarantined, size_bytes in reversed(moved):
            try:
                os.replace(quarantined, original)
            except OSError as exc:
                failures.append(f"{original}（移回失败：{exc}）")
                continue
            if not original.is_file():
                failures.append(f"{original}（恢复后文件不存在）")
            elif size_bytes is not None and original.stat().st_size != size_bytes:
                failures.append(f"{original}（恢复后大小不一致）")
        return failures

    def _abort_with_restore(
        self,
        moved: list[tuple[Path, Path, int | None]],
        quarantine_dir: Path,
        reason: str,
    ) -> None:
        """Restore quarantined files after an abort and raise honestly.

        Restoration failures are logged as critical and spelled out in the
        raised message together with the quarantine location; the database
        is already rolled back at this point and the message says so.
        """

        restore_failures = self._restore_moved_files(moved)
        if restore_failures:
            LOGGER.critical(
                "删除中止且部分文件未能恢复：quarantine=%s failures=%s",
                quarantine_dir,
                restore_failures,
            )
            raise DocumentDeletionError(
                f"{reason}。数据库未改动，但以下文件未能恢复原位："
                + "；".join(restore_failures)
                + f"。这些文件保留在隔离目录：{quarantine_dir}"
            )
        try:
            shutil.rmtree(quarantine_dir)
        except OSError:
            LOGGER.warning("中止后隔离目录未能移除，已保留：%s", quarantine_dir)
        raise DocumentDeletionError(f"{reason}。数据库未改动，文件已全部恢复原位。")
