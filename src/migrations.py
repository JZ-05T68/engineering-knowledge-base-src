"""Transactional SQLite schema migrations with automatic pre-upgrade backups."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 11

# 仅用于 Phase 2B-V 失败注入验证；生产运行时恒为 None，永不触发。
_V10_INJECTION_POINT: str | None = None
# 仅用于 Phase 3B 失败注入验证；生产运行时恒为 None，永不触发。
_V11_INJECTION_POINT: str | None = None


def _inject_v10_failure(point: str) -> None:
    """Raise inside ``_apply_version_ten`` when ``point`` matches the test hook."""

    if _V10_INJECTION_POINT == point:
        raise MigrationError(f"v10 迁移失败注入点：{point}")


def _inject_v11_failure(point: str) -> None:
    """Raise inside ``_apply_version_eleven`` when ``point`` matches the test hook."""

    if _V11_INJECTION_POINT == point:
        raise MigrationError(f"v11 迁移失败注入点：{point}")


class MigrationError(RuntimeError):
    """Raised when the database cannot be backed up or migrated safely."""


def migrate_database(database_path: Path) -> Path | None:
    """Migrate ``database_path`` to the latest schema and return any backup path.

    Existing non-empty databases are backed up with SQLite's online backup API
    before a version-changing write. Migration v2 itself is one transaction, so
    an error leaves the source database at its previous schema version.
    """

    database_path.parent.mkdir(parents=True, exist_ok=True)
    current_version = _read_schema_version(database_path)
    if current_version > SCHEMA_VERSION:
        raise MigrationError(
            f"数据库版本 {current_version} 高于程序支持的 {SCHEMA_VERSION}，请升级程序。"
        )

    backup_path: Path | None = None
    needs_backup = (
        database_path.exists()
        and database_path.stat().st_size > 0
        and current_version < SCHEMA_VERSION
    )
    if needs_backup:
        backup_path = backup_database(database_path, current_version)

    connection = sqlite3.connect(database_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        current_version = _connection_schema_version(connection)
        if current_version < 1:
            _apply_version_one(connection)
            current_version = 1
        if current_version < 2:
            _apply_version_two(connection)
            current_version = 2
        if current_version < 3:
            _apply_version_three(connection)
            current_version = 3
        if current_version < 4:
            _apply_version_four(connection)
        if current_version < 5:
            _apply_version_five(connection)
        if current_version < 6:
            _apply_version_six(connection)
        if current_version < 7:
            _apply_version_seven(connection)
        if current_version < 8:
            _apply_version_eight(connection)
        if current_version < 9:
            _apply_version_nine(connection)
        if current_version < 10:
            _apply_version_ten(connection)
        if current_version < 11:
            _apply_version_eleven(connection)
            current_version = 11
        connection.execute("PRAGMA foreign_keys = ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError(f"迁移后数据库完整性检查失败：{integrity}")
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_violations:
            raise MigrationError(
                f"迁移后发现 {len(foreign_key_violations)} 条外键违规记录"
            )
    except Exception as exc:
        connection.rollback()
        LOGGER.exception("数据库迁移失败，原数据库和迁移前备份均已保留")
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            f"数据库迁移失败：{exc}。原数据库与迁移前备份均已保留。"
        ) from exc
    finally:
        connection.close()
    return backup_path


def backup_database(database_path: Path, version: int | None = None) -> Path:
    """Create and verify a consistent SQLite backup without modifying the source."""

    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    version_label = f"v{version}" if version else "legacy"
    backup_path = backup_dir / f"{database_path.stem}.{version_label}.{timestamp}.db"
    try:
        with closing(sqlite3.connect(database_path)) as source, closing(
            sqlite3.connect(backup_path)
        ) as destination:
            source.backup(destination)
            destination.commit()
        with closing(sqlite3.connect(backup_path)) as verification:
            integrity = verification.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            backup_path.unlink(missing_ok=True)
            raise MigrationError(f"数据库备份完整性检查失败：{integrity}")
    except Exception as exc:
        backup_path.unlink(missing_ok=True)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"无法创建迁移前数据库备份：{exc}") from exc
    LOGGER.info("已创建数据库迁移前备份：%s", backup_path)
    return backup_path


def _read_schema_version(database_path: Path) -> int:
    if not database_path.exists() or database_path.stat().st_size == 0:
        return 0
    try:
        connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if table is None:
                return 0
            return _connection_schema_version(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise MigrationError(f"无法读取现有数据库版本：{exc}") from exc


def _connection_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    return int(row[0])


def _apply_version_one(connection: sqlite3.Connection) -> None:
    """Install the historical v0.0.1 schema for a fresh database."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            filename TEXT NOT NULL CHECK (length(trim(filename)) > 0),
            source_path TEXT NOT NULL CHECK (length(trim(source_path)) > 0),
            sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
            page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            page_number INTEGER NOT NULL CHECK (page_number > 0),
            image_path TEXT NOT NULL CHECK (length(trim(image_path)) > 0),
            extracted_text TEXT NOT NULL DEFAULT '',
            markdown_content TEXT NOT NULL DEFAULT '',
            markdown_path TEXT,
            status TEXT NOT NULL CHECK (status IN ('ready', 'pending')),
            search_extracted_text TEXT NOT NULL DEFAULT '',
            search_markdown_content TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (document_id, page_number)
        );
        CREATE INDEX IF NOT EXISTS idx_pages_document_page ON pages(document_id, page_number);
        CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);
        CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(
            search_extracted_text,
            search_markdown_content,
            content='pages',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS pages_fts_insert AFTER INSERT ON pages BEGIN
            INSERT INTO page_search(rowid, search_extracted_text, search_markdown_content)
            VALUES (new.id, new.search_extracted_text, new.search_markdown_content);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_fts_delete AFTER DELETE ON pages BEGIN
            INSERT INTO page_search(
                page_search, rowid, search_extracted_text, search_markdown_content
            ) VALUES (
                'delete', old.id, old.search_extracted_text, old.search_markdown_content
            );
        END;
        CREATE TRIGGER IF NOT EXISTS pages_fts_update AFTER UPDATE ON pages BEGIN
            INSERT INTO page_search(
                page_search, rowid, search_extracted_text, search_markdown_content
            ) VALUES (
                'delete', old.id, old.search_extracted_text, old.search_markdown_content
            );
            INSERT INTO page_search(rowid, search_extracted_text, search_markdown_content)
            VALUES (new.id, new.search_extracted_text, new.search_markdown_content);
        END;
        """
    )
    connection.execute("INSERT INTO page_search(page_search) VALUES ('rebuild')")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)", (_utc_now(),)
    )
    connection.commit()


def _apply_version_two(connection: sqlite3.Connection) -> None:
    """Add v0.0.2 status, organization, import history, and search structures."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_document_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
        document_columns = {
            "import_status": "TEXT NOT NULL DEFAULT 'completed'",
            "processed_page_count": "INTEGER NOT NULL DEFAULT 0",
            "text_page_count": "INTEGER NOT NULL DEFAULT 0",
            "review_page_count": "INTEGER NOT NULL DEFAULT 0",
            "import_error": "TEXT NOT NULL DEFAULT ''",
            "imported_at": "TEXT",
        }
        for name, definition in document_columns.items():
            if name not in existing_document_columns:
                connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
        connection.execute(
            """
            UPDATE documents SET
                processed_page_count = CASE
                    WHEN processed_page_count = 0 THEN page_count ELSE processed_page_count END,
                text_page_count = CASE
                    WHEN text_page_count = 0 THEN (
                        SELECT COUNT(*) FROM pages
                        WHERE pages.document_id = documents.id AND status = 'ready'
                    ) ELSE text_page_count END,
                review_page_count = CASE
                    WHEN review_page_count = 0 THEN (
                        SELECT COUNT(*) FROM pages
                        WHERE pages.document_id = documents.id AND status = 'pending'
                    ) ELSE review_page_count END,
                imported_at = COALESCE(imported_at, created_at)
            """
        )

        for trigger in ("pages_fts_insert", "pages_fts_delete", "pages_fts_update"):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP TABLE IF EXISTS page_search")
        connection.execute(
            """
            CREATE TABLE pages_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                image_path TEXT NOT NULL CHECK (length(trim(image_path)) > 0),
                extracted_text TEXT NOT NULL DEFAULT '',
                ocr_text TEXT NOT NULL DEFAULT '',
                markdown_content TEXT NOT NULL DEFAULT '',
                markdown_path TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'text_extracted', 'ocr_completed', 'pending_review',
                    'manually_reviewed', 'failed'
                )),
                processing_error TEXT NOT NULL DEFAULT '',
                search_extracted_text TEXT NOT NULL DEFAULT '',
                search_ocr_text TEXT NOT NULL DEFAULT '',
                search_markdown_content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (document_id, page_number)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO pages_v2(
                id, document_id, page_number, image_path, extracted_text, ocr_text,
                markdown_content, markdown_path, status, processing_error,
                search_extracted_text, search_ocr_text, search_markdown_content,
                created_at, updated_at
            )
            SELECT
                id, document_id, page_number, image_path, extracted_text, '',
                markdown_content, markdown_path,
                CASE
                    WHEN length(trim(markdown_content)) > 0 THEN 'manually_reviewed'
                    WHEN status = 'ready' THEN 'text_extracted'
                    ELSE 'pending_review'
                END,
                '', search_extracted_text, '', search_markdown_content,
                created_at, updated_at
            FROM pages
            """
        )
        connection.execute("DROP TABLE pages")
        connection.execute("ALTER TABLE pages_v2 RENAME TO pages")
        connection.execute(
            "CREATE INDEX idx_pages_document_page ON pages(document_id, page_number)"
        )
        connection.execute("CREATE INDEX idx_pages_status ON pages(status)")
        connection.execute("CREATE INDEX idx_documents_import_status ON documents(import_status)")
        _create_v2_tables(connection)
        _create_v2_fts(connection)
        connection.execute("INSERT INTO page_search(page_search) VALUES ('rebuild')")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)", (_utc_now(),)
        )
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v2")
    except Exception:
        connection.rollback()
        raise


def _apply_version_three(connection: sqlite3.Connection) -> None:
    """Add the v0.0.3 manual-review lifecycle without losing v2 process state.

    The historical ``status`` column is retained as the page-processing result.
    ``review_status`` becomes the canonical user workflow state. Because v0.0.2
    automatically marked any saved Markdown as manually reviewed, non-empty
    legacy notes are conservatively migrated to ``draft`` rather than claiming
    that a human explicitly confirmed them.
    """

    try:
        connection.execute("BEGIN IMMEDIATE")
        for trigger in ("pages_fts_insert", "pages_fts_delete", "pages_fts_update"):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP TABLE IF EXISTS page_search")
        connection.execute(
            """
            CREATE TABLE pages_v3 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                image_path TEXT NOT NULL CHECK (length(trim(image_path)) > 0),
                extracted_text TEXT NOT NULL DEFAULT '',
                ocr_text TEXT NOT NULL DEFAULT '',
                markdown_content TEXT NOT NULL DEFAULT '',
                markdown_path TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'text_extracted', 'ocr_completed', 'pending_review',
                    'manually_reviewed', 'failed'
                )),
                review_status TEXT NOT NULL CHECK (review_status IN (
                    'pending', 'draft', 'reviewed', 'skipped', 'failed'
                )),
                processing_error TEXT NOT NULL DEFAULT '',
                search_extracted_text TEXT NOT NULL DEFAULT '',
                search_ocr_text TEXT NOT NULL DEFAULT '',
                search_markdown_content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                note_updated_at TEXT,
                reviewed_at TEXT,
                last_viewed_at TEXT,
                UNIQUE (document_id, page_number)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO pages_v3(
                id, document_id, page_number, image_path, extracted_text, ocr_text,
                markdown_content, markdown_path, status, review_status,
                processing_error, search_extracted_text, search_ocr_text,
                search_markdown_content, created_at, updated_at, note_updated_at,
                reviewed_at, last_viewed_at
            )
            SELECT
                id, document_id, page_number, image_path, extracted_text, ocr_text,
                markdown_content, markdown_path, status,
                CASE
                    WHEN status = 'failed' AND length(trim(markdown_content)) = 0
                        THEN 'failed'
                    WHEN length(trim(markdown_content)) > 0 THEN 'draft'
                    WHEN status = 'manually_reviewed' THEN 'reviewed'
                    ELSE 'pending'
                END,
                processing_error, search_extracted_text, search_ocr_text,
                search_markdown_content, created_at, updated_at,
                CASE WHEN length(trim(markdown_content)) > 0 THEN updated_at ELSE NULL END,
                CASE
                    WHEN status = 'manually_reviewed'
                         AND length(trim(markdown_content)) = 0
                    THEN updated_at ELSE NULL
                END,
                NULL
            FROM pages
            """
        )
        connection.execute("DROP TABLE pages")
        connection.execute("ALTER TABLE pages_v3 RENAME TO pages")
        connection.execute(
            "CREATE INDEX idx_pages_document_page ON pages(document_id, page_number)"
        )
        connection.execute("CREATE INDEX idx_pages_status ON pages(status)")
        connection.execute(
            "CREATE INDEX idx_pages_review_status "
            "ON pages(review_status, document_id, page_number)"
        )
        _create_v2_fts(connection)
        connection.execute("INSERT INTO page_search(page_search) VALUES ('rebuild')")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (3, ?)",
            (_utc_now(),),
        )
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v3")
    except Exception:
        connection.rollback()
        raise


def _apply_version_four(connection: sqlite3.Connection) -> None:
    """Add durable evidence baskets without rewriting page or FTS data."""

    fingerprint = _core_data_fingerprint(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE evidence_baskets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK (
                    length(trim(name)) > 0 AND length(name) <= 100
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE evidence_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                basket_id INTEGER NOT NULL
                    REFERENCES evidence_baskets(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                page_id INTEGER NOT NULL
                    REFERENCES pages(id) ON DELETE CASCADE,
                document_title TEXT NOT NULL CHECK (length(trim(document_title)) > 0),
                filename TEXT NOT NULL CHECK (length(trim(filename)) > 0),
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                review_status TEXT NOT NULL CHECK (review_status IN (
                    'pending', 'draft', 'reviewed', 'skipped', 'failed'
                )),
                projects_json TEXT NOT NULL DEFAULT '[]',
                tags_json TEXT NOT NULL DEFAULT '[]',
                evidence_text TEXT NOT NULL CHECK (length(trim(evidence_text)) > 0),
                text_kind TEXT NOT NULL CHECK (text_kind IN (
                    'original_material', 'user_excerpt'
                )),
                context TEXT NOT NULL DEFAULT '',
                context_kind TEXT NOT NULL CHECK (context_kind IN (
                    'system_generated', 'user_provided'
                )),
                user_note TEXT NOT NULL DEFAULT '' CHECK (length(user_note) <= 4000),
                source_text_sha256 TEXT NOT NULL CHECK (length(source_text_sha256) = 64),
                source_locator TEXT NOT NULL CHECK (length(trim(source_locator)) > 0),
                selection_sha256 TEXT NOT NULL CHECK (length(selection_sha256) = 64),
                added_at TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position > 0),
                UNIQUE (basket_id, page_id, selection_sha256),
                UNIQUE (basket_id, position)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_evidence_items_page ON evidence_items(page_id)"
        )
        connection.execute(
            "CREATE INDEX idx_evidence_items_document ON evidence_items(document_id)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (4, ?)",
            (_utc_now(),),
        )
        if _core_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v4 迁移改变了现有文档、页面或 FTS 数据")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v4")
    except Exception:
        connection.rollback()
        raise


def _apply_version_five(connection: sqlite3.Connection) -> None:
    """Add the unified structured-notes table without touching existing data.

    v0.3.0 structured notes: one table, four ``note_type`` values. Ownership is
    mutually exclusive (document notes reference documents; page-scoped notes
    reference pages only). Anchor fields are type-exclusive and validated by
    CHECK constraints so the database itself rejects malformed combinations.
    """

    fingerprint = _core_data_fingerprint(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_type TEXT NOT NULL CHECK (note_type IN (
                    'document', 'page', 'text_selection', 'image_region'
                )),
                document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
                page_id INTEGER REFERENCES pages(id) ON DELETE CASCADE,

                personal_note TEXT NOT NULL CHECK (
                    length(personal_note) BETWEEN 1 AND 20000
                ),

                source_kind TEXT CHECK (source_kind IS NULL
                    OR source_kind IN ('pdf_text', 'ocr_text')),
                source_page_text_sha256 TEXT CHECK (source_page_text_sha256 IS NULL
                    OR length(source_page_text_sha256) = 64),
                source_excerpt_snapshot TEXT CHECK (source_excerpt_snapshot IS NULL
                    OR length(source_excerpt_snapshot) BETWEEN 1 AND 20000),
                selection_start INTEGER,
                selection_end INTEGER,
                user_excerpt TEXT CHECK (user_excerpt IS NULL
                    OR length(user_excerpt) BETWEEN 1 AND 20000),

                region_image_sha256 TEXT CHECK (region_image_sha256 IS NULL
                    OR length(region_image_sha256) = 64),
                region_image_width INTEGER,
                region_image_height INTEGER,
                region_x0 INTEGER,
                region_y0 INTEGER,
                region_x1 INTEGER,
                region_y1 INTEGER,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                CHECK (
                    note_type = 'document'
                    AND document_id IS NOT NULL
                    AND page_id IS NULL
                OR
                    note_type IN ('page', 'text_selection', 'image_region')
                    AND document_id IS NULL
                    AND page_id IS NOT NULL
                ),

                CHECK (
                    note_type IN ('document', 'page')
                    AND source_kind IS NULL
                    AND source_page_text_sha256 IS NULL
                    AND source_excerpt_snapshot IS NULL
                    AND selection_start IS NULL AND selection_end IS NULL
                    AND user_excerpt IS NULL
                    AND region_image_sha256 IS NULL
                    AND region_image_width IS NULL AND region_image_height IS NULL
                    AND region_x0 IS NULL AND region_y0 IS NULL
                    AND region_x1 IS NULL AND region_y1 IS NULL
                OR
                    note_type = 'text_selection'
                    AND source_kind IS NOT NULL
                    AND source_page_text_sha256 IS NOT NULL
                    AND source_excerpt_snapshot IS NOT NULL
                    AND selection_start IS NOT NULL AND selection_start >= 0
                    AND selection_end IS NOT NULL AND selection_end > selection_start
                    AND length(source_excerpt_snapshot)
                        = selection_end - selection_start
                    AND user_excerpt IS NOT NULL
                    AND region_image_sha256 IS NULL
                    AND region_image_width IS NULL AND region_image_height IS NULL
                    AND region_x0 IS NULL AND region_y0 IS NULL
                    AND region_x1 IS NULL AND region_y1 IS NULL
                OR
                    note_type = 'image_region'
                    AND region_image_sha256 IS NOT NULL
                    AND region_image_width IS NOT NULL AND region_image_width > 0
                    AND region_image_height IS NOT NULL AND region_image_height > 0
                    AND region_x0 IS NOT NULL AND region_x0 >= 0
                    AND region_y0 IS NOT NULL AND region_y0 >= 0
                    AND region_x1 IS NOT NULL AND region_x1 > region_x0
                    AND region_y1 IS NOT NULL AND region_y1 > region_y0
                    AND region_x1 <= region_image_width
                    AND region_y1 <= region_image_height
                    AND source_kind IS NULL
                    AND source_page_text_sha256 IS NULL
                    AND source_excerpt_snapshot IS NULL
                    AND selection_start IS NULL AND selection_end IS NULL
                    AND user_excerpt IS NULL
                )
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_notes_document ON notes(document_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_notes_page ON notes(page_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_notes_type ON notes(note_type, updated_at DESC)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (5, ?)",
            (_utc_now(),),
        )
        if _core_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v5 迁移改变了现有文档、页面或 FTS 数据")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v5")
    except Exception:
        connection.rollback()
        raise


def _apply_version_six(connection: sqlite3.Connection) -> None:
    """Add note importance and display preferences without touching existing data.

    v0.3.1 (frozen design): one additive column on ``notes`` (constant default
    'normal', so legacy rows need no rewrite), one single-row preferences table
    and one index. No other schema objects are introduced.
    """

    fingerprint = _core_data_fingerprint(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            ALTER TABLE notes ADD COLUMN importance TEXT NOT NULL DEFAULT 'normal'
                CHECK (importance IN ('primary', 'secondary', 'normal'))
            """
        )
        connection.execute(
            """
            CREATE TABLE note_display_preferences (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                color_primary TEXT NOT NULL DEFAULT '#c0392b'
                    CHECK (color_primary GLOB '#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
                color_secondary TEXT NOT NULL DEFAULT '#2563eb' CHECK (
                    color_secondary GLOB '#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
                ),
                color_normal TEXT NOT NULL DEFAULT '#000000'
                    CHECK (color_normal GLOB '#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO note_display_preferences (id, updated_at) VALUES (1, ?)",
            (_utc_now(),),
        )
        connection.execute(
            "CREATE INDEX idx_notes_importance ON notes(importance, updated_at DESC)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (6, ?)",
            (_utc_now(),),
        )
        if _core_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v6 迁移改变了现有文档、页面或 FTS 数据")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v6")
    except Exception:
        connection.rollback()
        raise


def _apply_version_seven(connection: sqlite3.Connection) -> None:
    """Rebuild evidence_items with typed evidence and manual confirmation state.

    v0.4.0 (slice 1-3): ``evidence_items`` is rebuilt create-copy-drop-rename
    to carry ``evidence_type`` (page / text_selection / image_region), the
    image-region anchor columns (same CHECK semantics as the notes table) and
    the manual confirmation pair (``confirmation_status`` / ``confirmed_at``).
    Every legacy row is preserved and maps to ``evidence_type='text_selection'``,
    ``confirmation_status='unconfirmed'``, ``confirmed_at=NULL`` and all-NULL
    region columns. The core fingerprint does not cover this table, so the row
    count and id set are verified explicitly inside the same transaction.
    """

    fingerprint = _core_data_fingerprint(connection)
    legacy_item_ids = _evidence_item_ids(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE evidence_items_v7 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                basket_id INTEGER NOT NULL
                    REFERENCES evidence_baskets(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                page_id INTEGER NOT NULL
                    REFERENCES pages(id) ON DELETE CASCADE,
                document_title TEXT NOT NULL CHECK (length(trim(document_title)) > 0),
                filename TEXT NOT NULL CHECK (length(trim(filename)) > 0),
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                review_status TEXT NOT NULL CHECK (review_status IN (
                    'pending', 'draft', 'reviewed', 'skipped', 'failed'
                )),
                projects_json TEXT NOT NULL DEFAULT '[]',
                tags_json TEXT NOT NULL DEFAULT '[]',
                evidence_type TEXT NOT NULL DEFAULT 'text_selection'
                    CHECK (evidence_type IN ('page', 'text_selection', 'image_region')),
                evidence_text TEXT NOT NULL DEFAULT '' CHECK (
                    evidence_type IN ('page', 'image_region')
                    OR length(trim(evidence_text)) > 0
                ),
                text_kind TEXT NOT NULL CHECK (text_kind IN (
                    'original_material', 'user_excerpt'
                )),
                context TEXT NOT NULL DEFAULT '',
                context_kind TEXT NOT NULL CHECK (context_kind IN (
                    'system_generated', 'user_provided'
                )),
                user_note TEXT NOT NULL DEFAULT '' CHECK (length(user_note) <= 4000),
                region_image_sha256 TEXT CHECK (region_image_sha256 IS NULL
                    OR length(region_image_sha256) = 64),
                region_image_width INTEGER,
                region_image_height INTEGER,
                region_x0 INTEGER,
                region_y0 INTEGER,
                region_x1 INTEGER,
                region_y1 INTEGER,
                source_text_sha256 TEXT NOT NULL CHECK (length(source_text_sha256) = 64),
                source_locator TEXT NOT NULL CHECK (length(trim(source_locator)) > 0),
                selection_sha256 TEXT NOT NULL CHECK (length(selection_sha256) = 64),
                confirmation_status TEXT NOT NULL DEFAULT 'unconfirmed'
                    CHECK (confirmation_status IN ('unconfirmed', 'confirmed')),
                confirmed_at TEXT,
                added_at TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position > 0),
                UNIQUE (basket_id, page_id, selection_sha256),
                UNIQUE (basket_id, position),
                CHECK (
                    confirmation_status = 'confirmed' AND confirmed_at IS NOT NULL
                    OR confirmation_status = 'unconfirmed' AND confirmed_at IS NULL
                ),
                CHECK (
                    evidence_type = 'image_region'
                    AND region_image_sha256 IS NOT NULL
                    AND region_image_width IS NOT NULL AND region_image_width > 0
                    AND region_image_height IS NOT NULL AND region_image_height > 0
                    AND region_x0 IS NOT NULL AND region_x0 >= 0
                    AND region_y0 IS NOT NULL AND region_y0 >= 0
                    AND region_x1 IS NOT NULL AND region_x1 > region_x0
                    AND region_y1 IS NOT NULL AND region_y1 > region_y0
                    AND region_x1 <= region_image_width
                    AND region_y1 <= region_image_height
                OR
                    evidence_type IN ('page', 'text_selection')
                    AND region_image_sha256 IS NULL
                    AND region_image_width IS NULL AND region_image_height IS NULL
                    AND region_x0 IS NULL AND region_y0 IS NULL
                    AND region_x1 IS NULL AND region_y1 IS NULL
                )
            )
            """
        )
        connection.execute(
            """
            INSERT INTO evidence_items_v7(
                id, basket_id, document_id, page_id, document_title, filename,
                page_number, review_status, projects_json, tags_json,
                evidence_type, evidence_text, text_kind, context, context_kind,
                user_note, region_image_sha256, region_image_width,
                region_image_height, region_x0, region_y0, region_x1, region_y1,
                source_text_sha256, source_locator, selection_sha256,
                confirmation_status, confirmed_at, added_at, position
            )
            SELECT
                id, basket_id, document_id, page_id, document_title, filename,
                page_number, review_status, projects_json, tags_json,
                'text_selection', evidence_text, text_kind, context, context_kind,
                user_note, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                source_text_sha256, source_locator, selection_sha256,
                'unconfirmed', NULL, added_at, position
            FROM evidence_items
            """
        )
        connection.execute("DROP TABLE evidence_items")
        connection.execute("ALTER TABLE evidence_items_v7 RENAME TO evidence_items")
        connection.execute(
            "CREATE INDEX idx_evidence_items_page ON evidence_items(page_id)"
        )
        connection.execute(
            "CREATE INDEX idx_evidence_items_document ON evidence_items(document_id)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (7, ?)",
            (_utc_now(),),
        )
        if _core_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v7 迁移改变了现有文档、页面或 FTS 数据")
        if _evidence_item_ids(connection) != legacy_item_ids:
            raise MigrationError("schema v7 迁移未能完整保留原有证据条目")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v7")
    except Exception:
        connection.rollback()
        raise


def _apply_version_eight(connection: sqlite3.Connection) -> None:
    """Add the page_embeddings persistence table without touching existing data.

    v0.5.0 Phase 7: page-level embedding persistence. One additive table,
    no rebuild of any existing table. The current embedding of a page is
    uniquely keyed by ``(page_id, model, dimensions, config_version)`` so a
    re-embedding after a text change updates the row in place instead of
    accumulating stale vectors; ``source_text_sha256`` is the freshness
    fingerprint of the embedded text. Rows cascade away with their page.
    """

    fingerprint = _core_data_fingerprint(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE page_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                source_text_sha256 TEXT NOT NULL CHECK (
                    length(source_text_sha256) = 64
                ),
                model TEXT NOT NULL CHECK (length(trim(model)) > 0),
                dimensions INTEGER NOT NULL CHECK (dimensions > 0),
                config_version INTEGER NOT NULL CHECK (config_version > 0),
                vector BLOB NOT NULL CHECK (length(vector) = 1 + 4 * dimensions),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (page_id, model, dimensions, config_version)
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (8, ?)",
            (_utc_now(),),
        )
        if _core_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v8 迁移改变了现有文档、页面或 FTS 数据")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v8")
    except Exception:
        connection.rollback()
        raise


def _apply_version_nine(connection: sqlite3.Connection) -> None:
    """Add the v0.5.2 knowledge-foundation tables without touching existing data.

    Four additive tables, no rebuild of any existing table:

    - ``knowledge_objects``: the durable, source-linked knowledge asset;
    - ``knowledge_object_sources``: polymorphic source-traceability links
      (target existence is enforced by the service layer, not a foreign key);
    - ``knowledge_relations``: typed directed links between objects;
    - ``knowledge_memory_entries``: user-authored memory plus the automatic
      append-only ``knowledge_change`` log.

    All foreign keys that can be declared in SQLite are declared; document and
    page links use ``ON DELETE SET NULL`` so deleting source material never
    destroys memory entries. The core fingerprint still covers only the v1-v8
    tables, so this migration must be a pure addition.
    """

    fingerprint = _core_data_fingerprint(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE knowledge_objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN (
                    'concept', 'fact', 'principle', 'experience',
                    'problem', 'decision'
                )),
                title TEXT NOT NULL CHECK (
                    length(trim(title)) BETWEEN 1 AND 200
                ),
                content TEXT NOT NULL CHECK (
                    length(content) BETWEEN 1 AND 20000
                ),
                importance TEXT NOT NULL DEFAULT 'normal'
                    CHECK (importance IN ('primary', 'secondary', 'normal')),
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'reviewed', 'archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_objects_kind "
            "ON knowledge_objects(kind, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_objects_importance "
            "ON knowledge_objects(importance, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_objects_status "
            "ON knowledge_objects(status, updated_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE knowledge_object_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_object_id INTEGER NOT NULL
                    REFERENCES knowledge_objects(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL CHECK (
                    source_type IN ('document', 'page', 'note', 'evidence')
                ),
                source_id INTEGER NOT NULL,
                source_note TEXT NOT NULL DEFAULT ''
                    CHECK (length(source_note) <= 500),
                created_at TEXT NOT NULL,
                UNIQUE (knowledge_object_id, source_type, source_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_object_sources_target "
            "ON knowledge_object_sources(source_type, source_id)"
        )
        connection.execute(
            """
            CREATE TABLE knowledge_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_ko_id INTEGER NOT NULL
                    REFERENCES knowledge_objects(id) ON DELETE CASCADE,
                target_ko_id INTEGER NOT NULL
                    REFERENCES knowledge_objects(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL CHECK (relation_type IN (
                    'relates_to', 'derived_from', 'supports',
                    'contradicts', 'example_of', 'requires'
                )),
                description TEXT NOT NULL DEFAULT ''
                    CHECK (length(description) <= 1000),
                created_at TEXT NOT NULL,
                UNIQUE (source_ko_id, target_ko_id, relation_type),
                CHECK (source_ko_id <> target_ko_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_relations_source "
            "ON knowledge_relations(source_ko_id, relation_type)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_relations_target "
            "ON knowledge_relations(target_ko_id, relation_type)"
        )
        connection.execute(
            """
            CREATE TABLE knowledge_memory_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN (
                    'problem_solving', 'experience', 'decision',
                    'knowledge_change'
                )),
                title TEXT NOT NULL CHECK (
                    length(trim(title)) BETWEEN 1 AND 200
                ),
                content TEXT NOT NULL DEFAULT ''
                    CHECK (length(content) <= 20000),
                root_cause TEXT NOT NULL DEFAULT ''
                    CHECK (length(root_cause) <= 4000),
                lesson TEXT NOT NULL DEFAULT ''
                    CHECK (length(lesson) <= 4000),
                knowledge_object_id INTEGER
                    REFERENCES knowledge_objects(id) ON DELETE SET NULL,
                document_id INTEGER
                    REFERENCES documents(id) ON DELETE SET NULL,
                page_id INTEGER
                    REFERENCES pages(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_memory_kind "
            "ON knowledge_memory_entries(kind, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_memory_ko "
            "ON knowledge_memory_entries(knowledge_object_id, updated_at DESC)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (9, ?)",
            (_utc_now(),),
        )
        if _core_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v9 迁移改变了现有文档、页面或 FTS 数据")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v9")
    except Exception:
        connection.rollback()
        raise


def _apply_version_ten(connection: sqlite3.Connection) -> None:
    """Rebuild the knowledge schema onto the v0.5.2 Phase 2B orthogonal model.

    v10 changes (ADR-01/02/04/05/07 of the Phase 2A-R1 decision document):

    - ``knowledge_base_meta``: single-row table with a locally generated UUID v4
      used to build stable IDs (``<kb_uuid>:<object_type>:<local_id>``);
    - ``knowledge_objects`` is rebuilt: the compressed ``status``/``reviewed_at``
      pair is replaced by orthogonal ``lifecycle``/``confirmation_*`` fields plus
      ``authorship``/``epistemic_basis``/``current_revision`` and the
      ``superseded_by_ko_id`` successor pointer (ON DELETE RESTRICT). Migrated
      rows keep ``authorship='user'`` and ``epistemic_basis='unknown_legacy'`` —
      no origin inference is performed on legacy data;
    - ``knowledge_object_sources`` gains fingerprint columns; legacy rows keep
      ``source_fingerprint=NULL`` (the fingerprint state machine is Phase 2C);
    - ``knowledge_memory_entries`` is rebuilt to hold user-authored memory only
      (``knowledge_change`` kind removed, ``status`` added);
    - ``knowledge_object_revisions`` is created as an append-only history table
      with stable identity snapshots and no foreign key, so deleting a
      knowledge object never modifies a revision row. Every legacy
      ``knowledge_change`` row is migrated as a ``legacy_event`` (no fabricated
      before/after) and every v9 object receives one ``legacy_baseline``
      revision representing its full content at migration time.
    """

    fingerprint = _core_data_fingerprint(connection)
    migration_timestamp = _utc_now()
    kb_uuid = str(uuid4())
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE knowledge_base_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                kb_uuid TEXT NOT NULL UNIQUE CHECK (length(kb_uuid) = 36),
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO knowledge_base_meta(id, kb_uuid, created_at) VALUES (1, ?, ?)",
            (kb_uuid, migration_timestamp),
        )
        _inject_v10_failure("v10_meta")

        connection.execute(
            """
            CREATE TABLE knowledge_objects_v10 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN (
                    'concept', 'fact', 'principle', 'experience',
                    'problem', 'decision'
                )),
                authorship TEXT NOT NULL DEFAULT 'user'
                    CHECK (authorship IN ('user', 'ai')),
                epistemic_basis TEXT NOT NULL DEFAULT 'unknown_legacy'
                    CHECK (epistemic_basis IN (
                        'source_derived', 'personal_experience',
                        'personal_judgment', 'direct_observation',
                        'decision_record', 'problem_definition',
                        'unknown_legacy'
                    )),
                title TEXT NOT NULL CHECK (
                    length(trim(title)) BETWEEN 1 AND 200
                ),
                content TEXT NOT NULL CHECK (
                    length(content) BETWEEN 1 AND 20000
                ),
                importance TEXT NOT NULL DEFAULT 'normal'
                    CHECK (importance IN ('primary', 'secondary', 'normal')),
                lifecycle TEXT NOT NULL DEFAULT 'active'
                    CHECK (lifecycle IN ('active', 'superseded', 'archived')),
                superseded_by_ko_id INTEGER
                    REFERENCES knowledge_objects_v10(id) ON DELETE RESTRICT,
                confirmation_status TEXT NOT NULL DEFAULT 'unconfirmed'
                    CHECK (confirmation_status IN ('unconfirmed', 'confirmed')),
                confirmed_at TEXT,
                confirmed_revision INTEGER,
                current_revision INTEGER NOT NULL DEFAULT 1
                    CHECK (current_revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    confirmation_status = 'confirmed'
                    AND confirmed_at IS NOT NULL
                    AND confirmed_revision IS NOT NULL
                OR
                    confirmation_status = 'unconfirmed'
                    AND confirmed_at IS NULL
                ),
                CHECK (
                    confirmed_revision IS NULL
                    OR confirmed_revision <= current_revision
                ),
                CHECK (
                    lifecycle IN ('active', 'archived')
                    AND superseded_by_ko_id IS NULL
                OR
                    lifecycle = 'superseded'
                    AND superseded_by_ko_id IS NOT NULL
                )
            )
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_objects_v10 (
                id, kind, authorship, epistemic_basis, title, content, importance,
                lifecycle, superseded_by_ko_id, confirmation_status, confirmed_at,
                confirmed_revision, current_revision, created_at, updated_at
            )
            SELECT
                id, kind, 'user', 'unknown_legacy', title, content, importance,
                CASE status WHEN 'archived' THEN 'archived' ELSE 'active' END,
                NULL,
                CASE status WHEN 'reviewed' THEN 'confirmed' ELSE 'unconfirmed' END,
                reviewed_at,
                CASE status WHEN 'reviewed' THEN (
                    SELECT COUNT(*) FROM knowledge_memory_entries
                    WHERE kind = 'knowledge_change'
                      AND knowledge_object_id = knowledge_objects.id
                ) + 1 ELSE NULL END,
                (
                    SELECT COUNT(*) FROM knowledge_memory_entries
                    WHERE kind = 'knowledge_change'
                      AND knowledge_object_id = knowledge_objects.id
                ) + 1,
                created_at, updated_at
            FROM knowledge_objects
            """
        )
        _inject_v10_failure("v10_objects_copy")
        _inject_v10_failure("v10_before_drop_rename")
        connection.execute("DROP TABLE knowledge_objects")
        connection.execute("ALTER TABLE knowledge_objects_v10 RENAME TO knowledge_objects")
        connection.execute(
            "CREATE INDEX idx_knowledge_objects_kind "
            "ON knowledge_objects(kind, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_objects_importance "
            "ON knowledge_objects(importance, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_objects_lifecycle "
            "ON knowledge_objects(lifecycle, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_objects_superseded_by "
            "ON knowledge_objects(superseded_by_ko_id)"
        )

        connection.execute(
            """
            CREATE TABLE knowledge_object_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_object_id INTEGER,
                object_local_id_snapshot INTEGER,
                object_stable_id_snapshot TEXT,
                object_title_snapshot TEXT NOT NULL,
                object_kind_snapshot TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN (
                    'legacy_baseline', 'legacy_event', 'created',
                    'content_updated', 'confirmation_changed',
                    'lifecycle_changed', 'supersession_changed',
                    'source_linked', 'source_unlinked'
                )),
                before_title TEXT,
                after_title TEXT,
                before_content TEXT,
                after_content TEXT,
                before_lifecycle TEXT,
                after_lifecycle TEXT,
                before_confirmation TEXT,
                after_confirmation TEXT,
                superseded_by_before INTEGER,
                superseded_by_after INTEGER,
                source_ref TEXT,
                payload_version INTEGER NOT NULL DEFAULT 1
                    CHECK (payload_version >= 1),
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (knowledge_object_id, revision_number)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_object_revisions_object "
            "ON knowledge_object_revisions(knowledge_object_id, revision_number)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_object_revisions_stable "
            "ON knowledge_object_revisions(object_stable_id_snapshot)"
        )

        object_rows = connection.execute(
            "SELECT id, kind, title, content, lifecycle, confirmation_status "
            "FROM knowledge_objects ORDER BY id"
        ).fetchall()
        for object_row in object_rows:
            object_id = int(object_row["id"])
            stable_id = f"{kb_uuid}:knowledge_object:{object_id}"
            legacy_rows = connection.execute(
                "SELECT id, title, content, created_at FROM knowledge_memory_entries "
                "WHERE kind = 'knowledge_change' AND knowledge_object_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (object_id,),
            ).fetchall()
            for number, legacy_row in enumerate(legacy_rows, start=1):
                connection.execute(
                    """
                    INSERT INTO knowledge_object_revisions (
                        knowledge_object_id, object_local_id_snapshot,
                        object_stable_id_snapshot, object_title_snapshot,
                        object_kind_snapshot, revision_number, event_type,
                        detail, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'legacy_event', ?, ?)
                    """,
                    (
                        object_id,
                        object_id,
                        stable_id,
                        str(object_row["title"]),
                        str(object_row["kind"]),
                        number,
                        f"{legacy_row['title']}\n{legacy_row['content']}",
                        str(legacy_row["created_at"]),
                    ),
                )
                _inject_v10_failure("v10_legacy_events")
            baseline_number = len(legacy_rows) + 1
            connection.execute(
                """
                INSERT INTO knowledge_object_revisions (
                    knowledge_object_id, object_local_id_snapshot,
                    object_stable_id_snapshot, object_title_snapshot,
                    object_kind_snapshot, revision_number, event_type,
                    after_title, after_content, after_lifecycle,
                    after_confirmation, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'legacy_baseline', ?, ?, ?, ?, ?, ?)
                """,
                (
                    object_id,
                    object_id,
                    stable_id,
                    str(object_row["title"]),
                    str(object_row["kind"]),
                    baseline_number,
                    str(object_row["title"]),
                    str(object_row["content"]),
                    str(object_row["lifecycle"]),
                    str(object_row["confirmation_status"]),
                    "迁移基线：v9→v10 迁移时点完整内容快照",
                    migration_timestamp,
                ),
            )
            _inject_v10_failure("v10_baselines")
        orphan_rows = connection.execute(
            "SELECT id, title, content, created_at FROM knowledge_memory_entries "
            "WHERE kind = 'knowledge_change' AND knowledge_object_id IS NULL "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
        for orphan_row in orphan_rows:
            connection.execute(
                """
                INSERT INTO knowledge_object_revisions (
                    object_title_snapshot, object_kind_snapshot,
                    revision_number, event_type, detail, created_at
                ) VALUES (?, 'unknown', 0, 'legacy_event', ?, ?)
                """,
                (
                    _legacy_change_title_snapshot(str(orphan_row["title"])),
                    f"{orphan_row['title']}\n{orphan_row['content']}",
                    str(orphan_row["created_at"]),
                ),
            )

        connection.execute(
            """
            CREATE TABLE knowledge_memory_entries_v10 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN (
                    'problem_solving', 'experience', 'decision'
                )),
                title TEXT NOT NULL CHECK (
                    length(trim(title)) BETWEEN 1 AND 200
                ),
                content TEXT NOT NULL DEFAULT ''
                    CHECK (length(content) <= 20000),
                root_cause TEXT NOT NULL DEFAULT ''
                    CHECK (length(root_cause) <= 4000),
                lesson TEXT NOT NULL DEFAULT ''
                    CHECK (length(lesson) <= 4000),
                knowledge_object_id INTEGER
                    REFERENCES knowledge_objects(id) ON DELETE SET NULL,
                document_id INTEGER
                    REFERENCES documents(id) ON DELETE SET NULL,
                page_id INTEGER
                    REFERENCES pages(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_memory_entries_v10 (
                id, kind, title, content, root_cause, lesson,
                knowledge_object_id, document_id, page_id, status,
                created_at, updated_at
            )
            SELECT
                id, kind, title, content, root_cause, lesson,
                knowledge_object_id, document_id, page_id, 'active',
                created_at, updated_at
            FROM knowledge_memory_entries
            WHERE kind IN ('problem_solving', 'experience', 'decision')
            """
        )
        _inject_v10_failure("v10_memory_copy")
        connection.execute("DROP TABLE knowledge_memory_entries")
        connection.execute(
            "ALTER TABLE knowledge_memory_entries_v10 RENAME TO knowledge_memory_entries"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_memory_kind "
            "ON knowledge_memory_entries(kind, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_memory_ko "
            "ON knowledge_memory_entries(knowledge_object_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_memory_status "
            "ON knowledge_memory_entries(status, updated_at DESC)"
        )

        connection.execute(
            "ALTER TABLE knowledge_object_sources ADD COLUMN source_fingerprint TEXT"
        )
        connection.execute(
            "ALTER TABLE knowledge_object_sources "
            "ADD COLUMN fingerprint_version INTEGER NOT NULL DEFAULT 1"
        )
        connection.execute(
            "ALTER TABLE knowledge_object_sources "
            f"ADD COLUMN captured_at TEXT NOT NULL DEFAULT '{migration_timestamp}'"
        )

        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (10, ?)",
            (migration_timestamp,),
        )
        _inject_v10_failure("v10_version_record")
        if _core_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v10 迁移改变了现有文档、页面或 FTS 数据")
        _inject_v10_failure("v10_before_commit")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v10")
    except Exception:
        connection.rollback()
        raise


def _apply_version_eleven(connection: sqlite3.Connection) -> None:
    """Add the v0.5.2 knowledge FTS layer without touching existing data.

    v11 changes (Phase 3 retrieval contract, ADR-06 supplement):

    - ``knowledge_objects`` and ``knowledge_memory_entries`` gain tokenized
      shadow columns through ``ALTER TABLE ADD COLUMN`` only — the existing
      tables are never dropped or rebuilt;
    - every legacy row is backfilled with the exact page-FTS canonical
      tokenization (``src.database._tokenize_for_fts``, imported lazily to
      avoid a top-level circular import), so the tokenizer stays a single
      source of truth and page retrieval semantics are untouched;
    - two external-content FTS5 tables and their six sync triggers are
      created, then rebuilt from the shadow columns;
    - ``knowledge_object_revisions`` never gets an FTS index.
    """

    fingerprint = _knowledge_data_fingerprint(connection)
    migration_timestamp = _utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE knowledge_objects "
            "ADD COLUMN search_title TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE knowledge_objects "
            "ADD COLUMN search_content TEXT NOT NULL DEFAULT ''"
        )
        _inject_v11_failure("v11_ko_columns")
        connection.execute(
            "ALTER TABLE knowledge_memory_entries "
            "ADD COLUMN search_title TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE knowledge_memory_entries "
            "ADD COLUMN search_content TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE knowledge_memory_entries "
            "ADD COLUMN search_root_cause TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE knowledge_memory_entries "
            "ADD COLUMN search_lesson TEXT NOT NULL DEFAULT ''"
        )
        _inject_v11_failure("v11_memory_columns")

        _backfill_knowledge_shadow_columns(connection)
        _inject_v11_failure("v11_ko_backfill")
        _backfill_memory_shadow_columns(connection)
        _inject_v11_failure("v11_memory_backfill")

        connection.execute(
            """
            CREATE VIRTUAL TABLE knowledge_object_search USING fts5(
                search_title,
                search_content,
                content='knowledge_objects',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        _inject_v11_failure("v11_ko_fts")
        connection.execute(
            """
            CREATE TRIGGER knowledge_objects_fts_insert
            AFTER INSERT ON knowledge_objects BEGIN
                INSERT INTO knowledge_object_search(rowid, search_title, search_content)
                VALUES (new.id, new.search_title, new.search_content);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER knowledge_objects_fts_delete
            AFTER DELETE ON knowledge_objects BEGIN
                INSERT INTO knowledge_object_search(
                    knowledge_object_search, rowid, search_title, search_content
                ) VALUES (
                    'delete', old.id, old.search_title, old.search_content
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER knowledge_objects_fts_update
            AFTER UPDATE ON knowledge_objects BEGIN
                INSERT INTO knowledge_object_search(
                    knowledge_object_search, rowid, search_title, search_content
                ) VALUES (
                    'delete', old.id, old.search_title, old.search_content
                );
                INSERT INTO knowledge_object_search(rowid, search_title, search_content)
                VALUES (new.id, new.search_title, new.search_content);
            END
            """
        )
        _inject_v11_failure("v11_ko_triggers")
        connection.execute(
            """
            CREATE VIRTUAL TABLE knowledge_memory_search USING fts5(
                search_title,
                search_content,
                search_root_cause,
                search_lesson,
                content='knowledge_memory_entries',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        _inject_v11_failure("v11_memory_fts")
        connection.execute(
            """
            CREATE TRIGGER knowledge_memory_fts_insert
            AFTER INSERT ON knowledge_memory_entries BEGIN
                INSERT INTO knowledge_memory_search(
                    rowid, search_title, search_content, search_root_cause, search_lesson
                ) VALUES (
                    new.id, new.search_title, new.search_content,
                    new.search_root_cause, new.search_lesson
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER knowledge_memory_fts_delete
            AFTER DELETE ON knowledge_memory_entries BEGIN
                INSERT INTO knowledge_memory_search(
                    knowledge_memory_search, rowid, search_title, search_content,
                    search_root_cause, search_lesson
                ) VALUES (
                    'delete', old.id, old.search_title, old.search_content,
                    old.search_root_cause, old.search_lesson
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER knowledge_memory_fts_update
            AFTER UPDATE ON knowledge_memory_entries BEGIN
                INSERT INTO knowledge_memory_search(
                    knowledge_memory_search, rowid, search_title, search_content,
                    search_root_cause, search_lesson
                ) VALUES (
                    'delete', old.id, old.search_title, old.search_content,
                    old.search_root_cause, old.search_lesson
                );
                INSERT INTO knowledge_memory_search(
                    rowid, search_title, search_content, search_root_cause, search_lesson
                ) VALUES (
                    new.id, new.search_title, new.search_content,
                    new.search_root_cause, new.search_lesson
                );
            END
            """
        )
        _inject_v11_failure("v11_memory_triggers")

        connection.execute(
            "INSERT INTO knowledge_object_search(knowledge_object_search) VALUES ('rebuild')"
        )
        connection.execute(
            "INSERT INTO knowledge_memory_search(knowledge_memory_search) VALUES ('rebuild')"
        )
        _inject_v11_failure("v11_rebuild")

        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (11, ?)",
            (migration_timestamp,),
        )
        _inject_v11_failure("v11_version_record")
        if _knowledge_data_fingerprint(connection) != fingerprint:
            raise MigrationError("schema v11 迁移改变了现有知识对象、记忆、来源、关系或修订数据")
        _inject_v11_failure("v11_before_commit")
        connection.commit()
        LOGGER.info("数据库已迁移到 schema v11")
    except Exception:
        connection.rollback()
        raise


def _backfill_knowledge_shadow_columns(connection: sqlite3.Connection) -> None:
    """Tokenize every existing knowledge object into its v11 shadow columns."""

    # Deferred import keeps the canonical page-FTS tokenizer a single source of
    # truth while avoiding a top-level circular import (src.database imports
    # this module). No page-retrieval semantics are changed.
    from src.database import _tokenize_for_fts  # noqa: PLC0415

    rows = connection.execute(
        "SELECT id, title, content FROM knowledge_objects ORDER BY id"
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE knowledge_objects SET search_title = ?, search_content = ? WHERE id = ?",
            (
                _tokenize_for_fts(str(row[1])),
                _tokenize_for_fts(str(row[2])),
                int(row[0]),
            ),
        )


def _backfill_memory_shadow_columns(connection: sqlite3.Connection) -> None:
    """Tokenize every existing memory entry into its v11 shadow columns."""

    # Deferred import mirrors ``_backfill_knowledge_shadow_columns``.
    from src.database import _tokenize_for_fts  # noqa: PLC0415

    rows = connection.execute(
        "SELECT id, title, content, root_cause, lesson "
        "FROM knowledge_memory_entries ORDER BY id"
    ).fetchall()
    for row in rows:
        connection.execute(
            """
            UPDATE knowledge_memory_entries SET
                search_title = ?, search_content = ?,
                search_root_cause = ?, search_lesson = ?
            WHERE id = ?
            """,
            (
                _tokenize_for_fts(str(row[1])),
                _tokenize_for_fts(str(row[2])),
                _tokenize_for_fts(str(row[3])),
                _tokenize_for_fts(str(row[4])),
                int(row[0]),
            ),
        )


def _knowledge_data_fingerprint(connection: sqlite3.Connection) -> tuple[object, ...]:
    """Return raw Knowledge Foundation invariants that v11 must preserve.

    Shadow columns are deliberately excluded: they are v11-derived fields and
    their backfill must never be mistaken for raw-data mutation. Row tuples
    are compared exactly, which is stronger than a content hash.
    """

    knowledge_object_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM knowledge_objects ORDER BY id"
        ).fetchall()
    )
    knowledge_object_rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT id, kind, authorship, epistemic_basis, title, content,"
            " importance, lifecycle, superseded_by_ko_id, confirmation_status,"
            " confirmed_at, confirmed_revision, current_revision, created_at,"
            " updated_at FROM knowledge_objects ORDER BY id"
        ).fetchall()
    )
    memory_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM knowledge_memory_entries ORDER BY id"
        ).fetchall()
    )
    memory_rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT id, kind, title, content, root_cause, lesson,"
            " knowledge_object_id, document_id, page_id, status, created_at,"
            " updated_at FROM knowledge_memory_entries ORDER BY id"
        ).fetchall()
    )
    source_count = int(
        connection.execute("SELECT COUNT(*) FROM knowledge_object_sources").fetchone()[0]
    )
    relation_count = int(
        connection.execute("SELECT COUNT(*) FROM knowledge_relations").fetchone()[0]
    )
    revision_rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT * FROM knowledge_object_revisions ORDER BY id"
        ).fetchall()
    )
    return (
        knowledge_object_ids,
        knowledge_object_rows,
        memory_ids,
        memory_rows,
        source_count,
        relation_count,
        revision_rows,
    )


def _legacy_change_title_snapshot(title: str) -> str:
    """Extract the object-title portion from a legacy ``知识XX：标题`` log title."""

    if "：" in title:
        return title.rsplit("：", 1)[1].strip() or title
    return title


def _evidence_item_ids(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Return the ordered evidence item id set; equality implies equal counts."""

    return tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM evidence_items ORDER BY id"
        ).fetchall()
    )


def _core_data_fingerprint(connection: sqlite3.Connection) -> tuple[object, ...]:
    """Return invariants that schema-only migrations must preserve exactly."""

    document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    page_count = connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    fts_count = connection.execute("SELECT COUNT(*) FROM page_search").fetchone()[0]
    statuses = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT review_status, COUNT(*) FROM pages "
            "GROUP BY review_status ORDER BY review_status"
        ).fetchall()
    )
    document_paths = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT id, source_path FROM documents ORDER BY id"
        ).fetchall()
    )
    page_paths = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT id, image_path FROM pages ORDER BY id"
        ).fetchall()
    )
    return (
        document_count,
        page_count,
        fts_count,
        statuses,
        document_paths,
        page_paths,
    )


def _create_v2_tables(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE import_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
            total_pages INTEGER NOT NULL DEFAULT 0,
            processed_pages INTEGER NOT NULL DEFAULT 0,
            text_pages INTEGER NOT NULL DEFAULT 0,
            review_pages INTEGER NOT NULL DEFAULT 0,
            failed_pages INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """,
        "CREATE INDEX idx_import_records_started ON import_records(started_at DESC)",
        "CREATE INDEX idx_import_records_status ON import_records(status)",
        """
        CREATE TABLE tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            normalized_name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE document_tags (
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (document_id, tag_id)
        )
        """,
        "CREATE INDEX idx_document_tags_tag ON document_tags(tag_id, document_id)",
        """
        CREATE TABLE page_tags (
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (page_id, tag_id)
        )
        """,
        "CREATE INDEX idx_page_tags_tag ON page_tags(tag_id, page_id)",
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            normalized_name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE project_documents (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, document_id)
        )
        """,
        "CREATE INDEX idx_project_documents_document ON project_documents(document_id, project_id)",
        """
        CREATE TABLE project_pages (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, page_id)
        )
        """,
        "CREATE INDEX idx_project_pages_page ON project_pages(page_id, project_id)",
    )
    for statement in statements:
        connection.execute(statement)


def _create_v2_fts(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE VIRTUAL TABLE page_search USING fts5(
            search_extracted_text,
            search_ocr_text,
            search_markdown_content,
            content='pages',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER pages_fts_insert AFTER INSERT ON pages BEGIN
            INSERT INTO page_search(
                rowid, search_extracted_text, search_ocr_text, search_markdown_content
            ) VALUES (
                new.id, new.search_extracted_text, new.search_ocr_text,
                new.search_markdown_content
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER pages_fts_delete AFTER DELETE ON pages BEGIN
            INSERT INTO page_search(
                page_search, rowid, search_extracted_text, search_ocr_text,
                search_markdown_content
            ) VALUES (
                'delete', old.id, old.search_extracted_text, old.search_ocr_text,
                old.search_markdown_content
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER pages_fts_update AFTER UPDATE ON pages BEGIN
            INSERT INTO page_search(
                page_search, rowid, search_extracted_text, search_ocr_text,
                search_markdown_content
            ) VALUES (
                'delete', old.id, old.search_extracted_text, old.search_ocr_text,
                old.search_markdown_content
            );
            INSERT INTO page_search(
                rowid, search_extracted_text, search_ocr_text, search_markdown_content
            ) VALUES (
                new.id, new.search_extracted_text, new.search_ocr_text,
                new.search_markdown_content
            );
        END
        """
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = ["MigrationError", "SCHEMA_VERSION", "backup_database", "migrate_database"]
