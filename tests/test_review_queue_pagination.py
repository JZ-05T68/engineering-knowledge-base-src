"""Database-level pagination tests for the manual-review queue."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.database import Database
from src.models import PageStatus, ReviewQueueSort


def _create_document(database: Database, root: Path, index: int):
    return database.create_document(
        title=f"分页文档 {index}",
        filename=f"pagination-{index}.pdf",
        source_path=root / f"pagination-{index}.pdf",
        sha256=f"{index:x}" * 64,
    )


def _create_pages(
    database: Database,
    root: Path,
    document_id: int,
    count: int,
    *,
    status: PageStatus = PageStatus.PENDING,
) -> list[int]:
    return [
        database.create_page(
            document_id=document_id,
            page_number=page_number,
            image_path=root / f"{document_id}-{page_number}.png",
            extracted_text=f"第 {page_number} 页",
            status=status,
        ).id
        for page_number in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    ("total", "requested_batch", "expected_size", "expected_batches"),
    [
        (0, 1, 0, 0),
        (7, 1, 7, 1),
        (20, 1, 20, 1),
        (21, 1, 20, 2),
        (45, 3, 5, 3),
    ],
)
def test_review_queue_page_sizes_and_totals(
    tmp_path: Path,
    total: int,
    requested_batch: int,
    expected_size: int,
    expected_batches: int,
) -> None:
    database = Database(tmp_path / f"queue-{total}.db")
    document = _create_document(database, tmp_path, 1)
    _create_pages(database, tmp_path, document.id, total)

    result = database.paginate_review_pages(
        document.id,
        batch_number=requested_batch,
        batch_size=20,
    )

    assert len(result.pages) == expected_size
    assert result.total_pages == total
    assert result.batch_size == 20
    assert result.batch_number == requested_batch
    assert result.total_batches == expected_batches
    assert result.requested_batch_number == requested_batch
    assert not result.corrected
    assert result.query.document_id == document.id
    assert result.query.batch_number == requested_batch
    assert result.visible_page_ids == tuple(page.id for page in result.pages)


def test_review_queue_document_and_status_filters_are_consistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "filters.db")
    first = _create_document(database, tmp_path, 1)
    second = _create_document(database, tmp_path, 2)
    expected_first: list[int] = []
    for page_number, status in enumerate(PageStatus, start=1):
        page = database.create_page(
            document_id=first.id,
            page_number=page_number,
            image_path=tmp_path / f"first-{page_number}.png",
            status=status,
        )
        if status in {PageStatus.PENDING, PageStatus.DRAFT, PageStatus.FAILED}:
            expected_first.append(page.id)
    second_draft = database.create_page(
        document_id=second.id,
        page_number=1,
        image_path=tmp_path / "second-1.png",
        status=PageStatus.DRAFT,
    )

    first_default = database.paginate_review_pages(first.id)
    draft_only = database.paginate_review_pages(statuses=(PageStatus.DRAFT,))

    assert first_default.visible_page_ids == tuple(expected_first)
    assert first_default.total_pages == len(expected_first)
    assert draft_only.visible_page_ids == (
        database.get_page_by_number(first.id, 2).id,  # type: ignore[union-attr]
        second_draft.id,
    )
    assert draft_only.query.statuses == (PageStatus.DRAFT,)


def test_review_queue_batches_have_no_duplicates_or_omissions(tmp_path: Path) -> None:
    database = Database(tmp_path / "stable.db")
    first = _create_document(database, tmp_path, 1)
    second = _create_document(database, tmp_path, 2)
    expected_ids = [
        *_create_pages(database, tmp_path, first.id, 23),
        *_create_pages(database, tmp_path, second.id, 22),
    ]

    results = [
        database.paginate_review_pages(batch_number=batch_number, batch_size=20)
        for batch_number in (1, 2, 3)
    ]
    actual_ids = [page_id for result in results for page_id in result.visible_page_ids]

    assert actual_ids == expected_ids
    assert len(actual_ids) == len(set(actual_ids)) == 45
    assert all(result.query.sort is ReviewQueueSort.DOCUMENT_PAGE for result in results)


@pytest.mark.parametrize(
    ("requested_batch", "expected_batch", "expected_page_numbers"),
    [(0, 1, list(range(1, 21))), (99, 2, [21])],
)
def test_review_queue_out_of_range_batch_is_safely_corrected(
    tmp_path: Path,
    requested_batch: int,
    expected_batch: int,
    expected_page_numbers: list[int],
) -> None:
    database = Database(tmp_path / f"correct-{requested_batch}.db")
    document = _create_document(database, tmp_path, 1)
    _create_pages(database, tmp_path, document.id, 21)

    result = database.paginate_review_pages(
        document.id,
        batch_number=requested_batch,
    )

    assert result.corrected
    assert result.requested_batch_number == requested_batch
    assert result.batch_number == expected_batch
    assert result.query.batch_number == expected_batch
    assert [page.page_number for page in result.pages] == expected_page_numbers


def test_review_queue_uses_parameterized_count_and_bounded_data_queries(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(tmp_path / "queries.db")
    document = _create_document(database, tmp_path, 1)
    _create_pages(database, tmp_path, document.id, 25)
    statements: list[tuple[str, tuple[object, ...]]] = []
    original_connection = database._connection

    class RecordingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, query: str, parameters: Sequence[object] = ()):
            statements.append((query, tuple(parameters)))
            return self.connection.execute(query, parameters)

    @contextmanager
    def recording_connection() -> Iterator[RecordingConnection]:
        with original_connection() as connection:
            yield RecordingConnection(connection)

    monkeypatch.setattr(database, "_connection", recording_connection)

    result = database.paginate_review_pages(
        document.id,
        statuses=(PageStatus.FAILED, PageStatus.PENDING),
        batch_number=2,
        batch_size=20,
    )

    selects = [
        (" ".join(query.split()), parameters)
        for query, parameters in statements
        if query.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 2
    count_query, count_parameters = selects[0]
    data_query, data_parameters = selects[1]
    count_where = count_query.split(" WHERE ", 1)[1]
    data_where = data_query.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0]
    assert count_where == data_where
    assert "review_status IN (?,?)" in count_query
    assert "document_id = ?" in count_query
    assert "ORDER BY document_id ASC, page_number ASC, id ASC" in data_query
    assert "LIMIT ? OFFSET ?" in data_query
    assert count_parameters == (
        PageStatus.PENDING.value,
        PageStatus.FAILED.value,
        document.id,
    )
    assert data_parameters == (*count_parameters, 20, 20)
    assert result.total_pages == 25
    assert len(result.pages) == 5


def test_review_queue_rejects_non_whitelisted_sort(tmp_path: Path) -> None:
    database = Database(tmp_path / "sort.db")
    document = _create_document(database, tmp_path, 1)
    _create_pages(database, tmp_path, document.id, 1)

    with pytest.raises(ValueError):
        database.paginate_review_pages(sort="document_page; DROP TABLE pages")

    assert database.get_page_by_number(document.id, 1) is not None
