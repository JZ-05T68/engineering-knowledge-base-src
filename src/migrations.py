"""Transactional SQLite schema migrations with automatic pre-upgrade backups."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 5


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
