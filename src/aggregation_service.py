"""Read-only cross-document knowledge aggregation queries.

This service answers one question: "along an existing organization axis
(project / tag / importance), which notes and evidence have I accumulated
across multiple documents?" It is deliberately not a search box, not a
flat notes list, and not an evidence-basket copy — see
``docs/design-v0.3.2.md`` §16-§17 for the frozen semantics:

- Axis matching reuses the effective association semantics the page search
  layer already ships: a page-level entry matches when its page is directly
  associated OR when its document is directly associated; document-level
  notes only match document-level associations.
- Evidence items carry no importance; they only appear when the importance
  filter is "all" (``None``). No importance is ever fabricated for them.
- Results are computed live from the existing tables — no materialized
  aggregation table, no cache — so a document deleted through
  ``DocumentDeletionService`` disappears from every aggregation naturally.
- One knowledge entry appears exactly once regardless of how many
  association paths reach it; axis matching uses IN/EXISTS, never JOIN
  fan-out.

Query discipline: two aggregation queries (count + page) plus at most four
small batched association lookups per page — no per-item queries. The
service never touches the filesystem; aggregation facts come from the
database only.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.database import Database, DatabaseError
from src.models import (
    AggregationItem,
    AggregationResult,
    AggregationSourceKind,
    NoteImportance,
    NoteType,
)

LOGGER = logging.getLogger(__name__)

LIST_LIMIT_MAX = 500
LIST_LIMIT_DEFAULT = 100


class AggregationError(DatabaseError):
    """An aggregation query was refused or could not be built."""


_NOTES_SELECT = """
        SELECT
            'note' AS source_kind,
            notes.id AS source_id,
            documents.id AS document_id,
            documents.title AS document_title,
            notes.page_id AS page_id,
            pages.page_number AS page_number,
            notes.note_type AS note_type,
            notes.importance AS importance,
            notes.personal_note AS content,
            '' AS user_note,
            NULL AS basket_id,
            notes.updated_at AS sort_ts,
            CASE notes.importance
                WHEN 'primary' THEN 0
                WHEN 'secondary' THEN 1
                ELSE 2
            END AS importance_rank
        FROM notes
        LEFT JOIN pages ON notes.page_id = pages.id
        JOIN documents
            ON documents.id = COALESCE(notes.document_id, pages.document_id)
        {notes_where}
"""

_EVIDENCE_SELECT = """
        SELECT
            'evidence' AS source_kind,
            evidence_items.id AS source_id,
            documents.id AS document_id,
            documents.title AS document_title,
            evidence_items.page_id AS page_id,
            pages.page_number AS page_number,
            NULL AS note_type,
            NULL AS importance,
            evidence_items.evidence_text AS content,
            evidence_items.user_note AS user_note,
            evidence_items.basket_id AS basket_id,
            evidence_items.added_at AS sort_ts,
            3 AS importance_rank
        FROM evidence_items
        JOIN documents ON documents.id = evidence_items.document_id
        JOIN pages ON pages.id = evidence_items.page_id
        {evidence_where}
"""

_ORDER_BY = """
    ORDER BY
        importance_rank ASC,
        sort_ts DESC,
        document_id ASC,
        COALESCE(page_number, 0) ASC,
        source_kind ASC,
        source_id ASC
"""

# link tables per axis: (document-level link, page-level link, axis column)
_AXIS_TABLES = {
    "project": ("project_documents", "project_pages", "project_id"),
    "tag": ("document_tags", "page_tags", "tag_id"),
}


class AggregationService:
    """Read-only cross-document aggregation over notes and evidence items."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ---------------------------------------------------------- public API
    def aggregate_library(
        self,
        *,
        importance: NoteImportance | str | None = None,
        note_type: NoteType | str | None = None,
        document_id: int | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> AggregationResult:
        """Aggregate knowledge across the whole library (no axis filter)."""

        return self._aggregate(
            axis=None,
            axis_id=None,
            importance=importance,
            note_type=note_type,
            document_id=document_id,
            limit=limit,
            offset=offset,
        )

    def aggregate_by_project(
        self,
        project_id: int,
        *,
        importance: NoteImportance | str | None = None,
        note_type: NoteType | str | None = None,
        document_id: int | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> AggregationResult:
        """Aggregate knowledge associated with one project.

        Raises :class:`AggregationError` when the project does not exist.
        """

        self._require_axis("projects", project_id, "项目")
        return self._aggregate(
            axis="project",
            axis_id=project_id,
            importance=importance,
            note_type=note_type,
            document_id=document_id,
            limit=limit,
            offset=offset,
        )

    def aggregate_by_tag(
        self,
        tag_id: int,
        *,
        importance: NoteImportance | str | None = None,
        note_type: NoteType | str | None = None,
        document_id: int | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        offset: int = 0,
    ) -> AggregationResult:
        """Aggregate knowledge associated with one tag.

        Raises :class:`AggregationError` when the tag does not exist.
        """

        self._require_axis("tags", tag_id, "标签")
        return self._aggregate(
            axis="tag",
            axis_id=tag_id,
            importance=importance,
            note_type=note_type,
            document_id=document_id,
            limit=limit,
            offset=offset,
        )

    # ------------------------------------------------------------- engine
    def _require_axis(self, table: str, axis_id: int, label: str) -> None:
        if isinstance(axis_id, bool) or not isinstance(axis_id, int) or axis_id <= 0:
            raise AggregationError(f"{label} id 必须是正整数：{axis_id!r}")
        with self._database._connection() as connection:
            found = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE id = ?", (axis_id,)
            ).fetchone()[0]
        if not found:
            raise AggregationError(f"找不到{label}：{axis_id}")

    def _aggregate(
        self,
        *,
        axis: str | None,
        axis_id: int | None,
        importance: NoteImportance | str | None,
        note_type: NoteType | str | None,
        document_id: int | None,
        limit: int,
        offset: int,
    ) -> AggregationResult:
        resolved_importance = self._resolve_importance(importance)
        resolved_note_type = self._resolve_note_type(note_type)
        self._validate_pagination(limit, offset)

        notes_conditions: list[str] = []
        notes_parameters: list[object] = []
        evidence_conditions: list[str] = []
        evidence_parameters: list[object] = []

        if axis is not None:
            document_link, page_link, axis_column = _AXIS_TABLES[axis]
            notes_conditions.append(
                f"""(
                    notes.document_id IN (
                        SELECT document_id FROM {document_link}
                        WHERE {axis_column} = ?
                    )
                    OR notes.page_id IN (
                        SELECT page_id FROM {page_link} WHERE {axis_column} = ?
                        UNION
                        SELECT axis_pages.id FROM pages AS axis_pages
                        JOIN {document_link} AS axis_link
                            ON axis_link.document_id = axis_pages.document_id
                        WHERE axis_link.{axis_column} = ?
                    )
                )"""
            )
            notes_parameters.extend((axis_id, axis_id, axis_id))
            evidence_conditions.append(
                f"""(
                    evidence_items.page_id IN (
                        SELECT page_id FROM {page_link} WHERE {axis_column} = ?
                    )
                    OR evidence_items.document_id IN (
                        SELECT document_id FROM {document_link}
                        WHERE {axis_column} = ?
                    )
                )"""
            )
            evidence_parameters.extend((axis_id, axis_id))
        if resolved_importance is not None:
            notes_conditions.append("notes.importance = ?")
            notes_parameters.append(resolved_importance.value)
        if resolved_note_type is not None:
            notes_conditions.append("notes.note_type = ?")
            notes_parameters.append(resolved_note_type.value)
        if document_id is not None:
            notes_conditions.append("documents.id = ?")
            notes_parameters.append(document_id)
            evidence_conditions.append("documents.id = ?")
            evidence_parameters.append(document_id)

        # Evidence carries no importance and no note type: any filter on
        # those dimensions restricts the result to notes only.
        include_evidence = resolved_importance is None and resolved_note_type is None
        notes_select = _NOTES_SELECT.format(
            notes_where=(
                "WHERE " + " AND ".join(notes_conditions) if notes_conditions else ""
            )
        )
        branches = [notes_select]
        if include_evidence:
            branches.append(
                _EVIDENCE_SELECT.format(
                    evidence_where=(
                        "WHERE " + " AND ".join(evidence_conditions)
                        if evidence_conditions
                        else ""
                    )
                )
            )
        union_sql = "SELECT * FROM (" + " UNION ALL ".join(branches) + ")" + _ORDER_BY
        parameters = tuple(notes_parameters)
        if include_evidence:
            parameters += tuple(evidence_parameters)

        with self._database._connection() as connection:
            count_row = connection.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(source_kind = 'note'), 0), "
                "COALESCE(SUM(source_kind = 'evidence'), 0) "
                f"FROM ({union_sql})",
                parameters,
            ).fetchone()
            page_rows = connection.execute(
                f"{union_sql} LIMIT ? OFFSET ?", (*parameters, limit, offset)
            ).fetchall()
            items = self._attach_associations(connection, page_rows)
        return AggregationResult(
            items=tuple(items),
            total_count=int(count_row[0]),
            note_count=int(count_row[1]),
            evidence_count=int(count_row[2]),
            limit=limit,
            offset=offset,
        )

    def _attach_associations(self, connection, rows: list) -> list[AggregationItem]:
        """Attach effective tag/project names with batched lookups (no N+1)."""

        document_ids = sorted({int(row[2]) for row in rows})
        page_ids = sorted({int(row[4]) for row in rows if row[4] is not None})
        document_tags = self._association_names(
            connection, "document_tags", "document_id", document_ids, "tags", "tag_id"
        )
        page_tags = self._association_names(
            connection, "page_tags", "page_id", page_ids, "tags", "tag_id"
        )
        document_projects = self._association_names(
            connection,
            "project_documents",
            "document_id",
            document_ids,
            "projects",
            "project_id",
        )
        page_projects = self._association_names(
            connection, "project_pages", "page_id", page_ids, "projects", "project_id"
        )
        items: list[AggregationItem] = []
        for row in rows:
            document_id = int(row[2])
            page_id = int(row[4]) if row[4] is not None else None
            tags = set(document_tags.get(document_id, ()))
            projects = set(document_projects.get(document_id, ()))
            if page_id is not None:
                tags.update(page_tags.get(page_id, ()))
                projects.update(page_projects.get(page_id, ()))
            items.append(
                AggregationItem(
                    source_kind=AggregationSourceKind(row[0]),
                    source_id=int(row[1]),
                    document_id=document_id,
                    document_title=str(row[3]),
                    page_id=page_id,
                    page_number=int(row[5]) if row[5] is not None else None,
                    note_type=NoteType(row[6]) if row[6] is not None else None,
                    importance=NoteImportance(row[7]) if row[7] is not None else None,
                    content=str(row[8]),
                    user_note=str(row[9]),
                    basket_id=int(row[10]) if row[10] is not None else None,
                    tags=tuple(sorted(tags)),
                    projects=tuple(sorted(projects)),
                    sort_timestamp=datetime.fromisoformat(str(row[11])),
                )
            )
        return items

    @staticmethod
    def _association_names(
        connection,
        link_table: str,
        link_column: str,
        entity_ids: list[int],
        name_table: str,
        name_column: str,
    ) -> dict[int, tuple[str, ...]]:
        if not entity_ids:
            return {}
        placeholders = ",".join("?" for _ in entity_ids)
        rows = connection.execute(
            f"SELECT link.{link_column}, names.name FROM {link_table} AS link "
            f"JOIN {name_table} AS names ON names.id = link.{name_column} "
            f"WHERE link.{link_column} IN ({placeholders})",
            entity_ids,
        ).fetchall()
        names: dict[int, list[str]] = {}
        for entity_id, name in rows:
            names.setdefault(int(entity_id), []).append(str(name))
        return {key: tuple(value) for key, value in names.items()}

    # --------------------------------------------------------- validation
    @staticmethod
    def _resolve_importance(
        importance: NoteImportance | str | None,
    ) -> NoteImportance | None:
        if importance is None:
            return None
        try:
            return NoteImportance(importance)
        except ValueError as exc:
            raise AggregationError(f"未知重要性等级：{importance}") from exc

    @staticmethod
    def _resolve_note_type(note_type: NoteType | str | None) -> NoteType | None:
        if note_type is None:
            return None
        try:
            return NoteType(note_type)
        except ValueError as exc:
            raise AggregationError(f"未知笔记类型：{note_type}") from exc

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= LIST_LIMIT_MAX
        ):
            raise AggregationError(f"limit 必须是 1～{LIST_LIMIT_MAX} 的整数")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise AggregationError("offset 必须是非负整数")
