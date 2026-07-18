"""Safe, URL-backed state helpers for the local search workflow."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol
from urllib.parse import parse_qs, urlencode

from src.models import (
    PageStatus,
    SearchField,
    SearchFilters,
    SearchSort,
    SearchViewMode,
)

MAX_QUERY_CHARS = 500
MAX_RETURN_STATE_CHARS = 8_000
MAX_FILTER_VALUES = 100
_TRUE_VALUES = {"1", "true", "yes", "on"}
_STATE_KEYS = {
    "q",
    "search_query",
    "documents",
    "document_ids",
    "document",
    "projects",
    "project_ids",
    "tags",
    "tag_ids",
    "statuses",
    "status",
    "fields",
    "match_fields",
    "has_note",
    "basket",
    "evidence_basket_id",
    "sort",
    "limit",
    "result_page",
    "filters_open",
    "view",
    "expanded_document",
    "preview_page",
    "focus_result",
}


class QueryParams(Protocol):
    """Small query-parameter surface used by Streamlit and unit tests."""

    def get(self, key: str, default: object = None) -> object: ...


@dataclass(frozen=True, slots=True)
class SearchPageState:
    """Canonical search state that can survive refreshes and page navigation."""

    query: str = ""
    filters: SearchFilters = SearchFilters()
    sort: SearchSort = SearchSort.RELEVANCE
    limit: int = 50
    result_page: int = 1
    filters_open: bool = False
    view_mode: SearchViewMode = SearchViewMode.PAGE
    expanded_document_id: int | None = None
    preview_page_id: int | None = None
    focus_result: int | None = None

    def with_first_page(self, **changes: object) -> SearchPageState:
        """Return a changed state whose result pagination restarts at page one."""

        return replace(self, result_page=1, **changes)


@dataclass(frozen=True, slots=True)
class ActiveFilter:
    """One removable, human-readable condition shown above search results."""

    kind: str
    value: int | str | None
    label: str


def parse_search_state(params: QueryParams | Mapping[str, object]) -> SearchPageState:
    """Parse known URL parameters and safely ignore missing or illegal values."""

    query = _first(params, "q", "search_query")[:MAX_QUERY_CHARS]
    documents = _positive_ids(_values(params, "documents", "document_ids", "document"))
    projects = _positive_ids(_values(params, "projects", "project_ids"))
    tags = _positive_ids(_values(params, "tags", "tag_ids"))
    statuses = _enum_values(
        PageStatus,
        _values(params, "statuses", "status"),
    )
    fields = _enum_values(
        SearchField,
        _values(params, "fields", "match_fields"),
    )
    basket_id = _positive_int(_first(params, "basket", "evidence_basket_id"))
    try:
        sort = SearchSort(_first(params, "sort") or SearchSort.RELEVANCE.value)
    except ValueError:
        sort = SearchSort.RELEVANCE
    limit = _bounded_int(_first(params, "limit"), default=50, minimum=10, maximum=100)
    limit = max(10, min(((limit + 5) // 10) * 10, 100))
    result_page = _bounded_int(
        _first(params, "result_page"), default=1, minimum=1, maximum=100_000
    )
    try:
        view_mode = SearchViewMode(_first(params, "view") or SearchViewMode.PAGE.value)
    except ValueError:
        view_mode = SearchViewMode.PAGE
    expanded_document_id = _positive_int(_first(params, "expanded_document"))
    preview_page_id = _positive_int(_first(params, "preview_page"))
    focus_result = _bounded_optional_int(
        _first(params, "focus_result"), minimum=1, maximum=100_000
    )
    return SearchPageState(
        query=query,
        filters=SearchFilters(
            document_ids=documents,
            project_ids=projects,
            tag_ids=tags,
            statuses=statuses,
            match_fields=fields,
            has_note=_first(params, "has_note").casefold() in _TRUE_VALUES,
            evidence_basket_id=basket_id,
        ),
        sort=sort,
        limit=limit,
        result_page=result_page,
        filters_open=_first(params, "filters_open").casefold() in _TRUE_VALUES,
        view_mode=view_mode,
        expanded_document_id=expanded_document_id,
        preview_page_id=preview_page_id,
        focus_result=focus_result,
    )


def search_state_query_params(state: SearchPageState) -> dict[str, str]:
    """Serialize state into compact canonical URL parameters."""

    params: dict[str, str] = {"q": state.query[:MAX_QUERY_CHARS]}
    filters = state.filters
    if filters.document_ids:
        params["documents"] = _join_values(filters.document_ids)
    if filters.project_ids:
        params["projects"] = _join_values(filters.project_ids)
    if filters.tag_ids:
        params["tags"] = _join_values(filters.tag_ids)
    if filters.statuses:
        params["statuses"] = _join_values(value.value for value in filters.statuses)
    if filters.match_fields:
        params["fields"] = _join_values(value.value for value in filters.match_fields)
    if filters.has_note:
        params["has_note"] = "1"
    if filters.evidence_basket_id is not None:
        params["basket"] = str(filters.evidence_basket_id)
    if state.sort is not SearchSort.RELEVANCE:
        params["sort"] = state.sort.value
    if state.limit != 50:
        params["limit"] = str(state.limit)
    if state.result_page != 1:
        params["result_page"] = str(state.result_page)
    if state.filters_open:
        params["filters_open"] = "1"
    if state.view_mode is not SearchViewMode.PAGE:
        params["view"] = state.view_mode.value
    if state.expanded_document_id is not None:
        params["expanded_document"] = str(state.expanded_document_id)
    if state.preview_page_id is not None:
        params["preview_page"] = str(state.preview_page_id)
    if state.focus_result is not None:
        params["focus_result"] = str(state.focus_result)
    return params


def encode_return_state(state: SearchPageState) -> str:
    """Encode a bounded return target for the reader page."""

    encoded = urlencode(search_state_query_params(state))
    return encoded[:MAX_RETURN_STATE_CHARS]


def decode_return_state(value: str) -> SearchPageState:
    """Decode a reader return target using the same whitelist as normal URLs."""

    bounded = value[:MAX_RETURN_STATE_CHARS]
    parsed = {key: items for key, items in parse_qs(bounded).items()}
    return parse_search_state(parsed)


def has_search_state_params(params: Mapping[str, object] | QueryParams) -> bool:
    """Return whether a URL contains a current or legacy search-state key."""

    try:
        keys = set(params)  # type: ignore[arg-type]
    except TypeError:
        return bool(_first(params, *_STATE_KEYS))
    return bool(keys & _STATE_KEYS)


def clear_search_filters(
    state: SearchPageState, *, keep_query: bool = True
) -> SearchPageState:
    """Clear formal filters while optionally retaining the search query."""

    return replace(
        state,
        query=state.query if keep_query else "",
        filters=SearchFilters(),
        result_page=1,
        expanded_document_id=None,
        preview_page_id=None,
        focus_result=None,
    )


def remove_search_filter(
    state: SearchPageState, kind: str, value: int | str | None
) -> SearchPageState:
    """Remove one whitelisted filter value without changing other state."""

    filters = state.filters
    if kind == "document":
        filters = replace(filters, document_ids=_without(filters.document_ids, value))
    elif kind == "project":
        filters = replace(filters, project_ids=_without(filters.project_ids, value))
    elif kind == "tag":
        filters = replace(filters, tag_ids=_without(filters.tag_ids, value))
    elif kind == "status":
        filters = replace(
            filters,
            statuses=tuple(item for item in filters.statuses if item.value != value),
        )
    elif kind == "field":
        filters = replace(
            filters,
            match_fields=tuple(
                item for item in filters.match_fields if item.value != value
            ),
        )
    elif kind == "has_note":
        filters = replace(filters, has_note=False)
    elif kind == "basket":
        filters = replace(filters, evidence_basket_id=None)
    else:
        return state
    return replace(
        state,
        filters=filters,
        result_page=1,
        expanded_document_id=None,
        preview_page_id=None,
        focus_result=None,
    )


def active_filter_labels(
    state: SearchPageState,
    *,
    document_names: Mapping[int, str] = {},
    project_names: Mapping[int, str] = {},
    tag_names: Mapping[int, str] = {},
) -> tuple[ActiveFilter, ...]:
    """Return removable labels, including stable fallbacks for stale metadata IDs."""

    filters = state.filters
    labels: list[ActiveFilter] = []
    labels.extend(
        ActiveFilter("document", value, f"文档：{document_names.get(value, f'#{value}')}")
        for value in filters.document_ids
    )
    labels.extend(
        ActiveFilter("project", value, f"项目：{project_names.get(value, f'#{value}')}")
        for value in filters.project_ids
    )
    labels.extend(
        ActiveFilter("tag", value, f"标签：{tag_names.get(value, f'#{value}')}")
        for value in filters.tag_ids
    )
    labels.extend(
        ActiveFilter("status", value.value, f"状态：{value.label}")
        for value in filters.statuses
    )
    labels.extend(
        ActiveFilter("field", value.value, f"搜索范围：{value.label}")
        for value in filters.match_fields
    )
    if filters.has_note:
        labels.append(ActiveFilter("has_note", None, "其他：有笔记"))
    if filters.evidence_basket_id is not None:
        labels.append(ActiveFilter("basket", None, "其他：当前证据篮"))
    return tuple(labels)


def filter_named_options(
    options: Mapping[int, str], query: str, *, selected_ids: Sequence[int] = ()
) -> tuple[int, ...]:
    """Filter local option names with Unicode normalization and no regex/HTML use."""

    needle = _normalize_lookup(query)
    selected = set(selected_ids)
    return tuple(
        option_id
        for option_id, name in options.items()
        if option_id in selected or not needle or needle in _normalize_lookup(name)
    )


def _values(params: QueryParams | Mapping[str, object], *keys: str) -> tuple[str, ...]:
    for key in keys:
        raw_values: object = ()
        get_all = getattr(params, "get_all", None)
        if callable(get_all):
            raw_values = get_all(key)
        if not raw_values:
            raw_values = params.get(key, ())
        if raw_values in (None, "", (), []):
            continue
        if isinstance(raw_values, str):
            values: Sequence[object] = (raw_values,)
        elif isinstance(raw_values, Sequence):
            values = raw_values
        else:
            values = (raw_values,)
        split_values: list[str] = []
        for raw_value in values:
            split_values.extend(str(raw_value).split(","))
        return tuple(value.strip() for value in split_values if value.strip())
    return ()


def _first(params: QueryParams | Mapping[str, object], *keys: str) -> str:
    values = _values(params, *keys)
    return values[0] if values else ""


def _positive_ids(values: Sequence[str]) -> tuple[int, ...]:
    result: list[int] = []
    for value in values[:MAX_FILTER_VALUES]:
        parsed = _positive_int(value)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return tuple(result)


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounded_int(value: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _bounded_optional_int(value: str, *, minimum: int, maximum: int) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _enum_values(enum_type: type, values: Sequence[str]) -> tuple:
    result: list[object] = []
    for value in values[:MAX_FILTER_VALUES]:
        try:
            parsed = enum_type(value)
        except ValueError:
            continue
        if parsed not in result:
            result.append(parsed)
    return tuple(result)


def _join_values(values: Sequence[object]) -> str:
    return ",".join(str(value) for value in values)


def _without(values: Sequence[int], removed: int | str | None) -> tuple[int, ...]:
    try:
        parsed = int(removed)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return tuple(values)
    return tuple(value for value in values if value != parsed)


def _normalize_lookup(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


__all__ = [
    "ActiveFilter",
    "SearchPageState",
    "active_filter_labels",
    "clear_search_filters",
    "decode_return_state",
    "encode_return_state",
    "filter_named_options",
    "has_search_state_params",
    "parse_search_state",
    "remove_search_filter",
    "search_state_query_params",
]
