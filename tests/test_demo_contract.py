"""v0.6.1 demo contract tests: fixture compatibility, mock semantics, boundaries.

Focused regression for the competition demo layer (``src/demo``). The mock
must stay schema-compatible with the frozen public Hosted DTOs, deterministic,
offline, and strictly separated from the real agent/runtime/database paths.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.request
from pathlib import Path

import pytest

from src.demo import (
    FIXTURE_KEYS,
    GENERIC_WARNING,
    NO_EVIDENCE_MESSAGE,
    RESPONSES,
    SOURCES,
    DemoAgentRunResponse,
    DemoRequestError,
    DemoSourceError,
    DemoSourceResponse,
    MockDemoClient,
    load_demo_catalog,
)
from src.demo.catalog import _CITATION_MARKER
from src.demo.export import catalog_to_json
from src.hosted_api.contracts import (
    AgentRunResponse,
    HTTPFailure,
    SourceResponse,
    public_error,
)
from src.models import ContextFingerprintState, ContextItemType
from src.source_metadata import parse_source_id, safe_display_text

SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "src/demo/data/demo_catalog.json"
DEMO_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src/demo"

DEMO_QUESTION_A = "PID 调整时，比例项与积分项分别影响什么？"
DEMO_QUESTION_B = "编码器接线错误导致 PID 震荡的那次问题，最终是怎么定位和解决的？"
DEMO_QUESTION_C = "检查知识对象「伺服驱动调试指南」的来源是否仍然可信。"
DEMO_QUESTION_EMPTY = "知识库里有关于机器学习模型部署的内容吗？"
DEMO_QUESTION_FAILED = "（演示排练）触发一次工具失败的安全状态。"

_FORBIDDEN_IMPORT_PREFIXES = (
    "src.runtime",
    "src.database",
    "src.hosted",
    "src.ai",
    "streamlit",
    "requests",
    "urllib",
    "httpx",
    "socket",
    "http.client",
)

_IMPORT_LINE = re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.MULTILINE)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any demo code path tries to reach network or dotenv."""

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("demo tests forbid network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setenv("EKB_AI_API_KEY", "sentinel-should-never-be-read")


@pytest.fixture
def client() -> MockDemoClient:
    return MockDemoClient()


def _real_fields(response: DemoAgentRunResponse) -> dict[str, object]:
    return {key: getattr(response, key) for key in AgentRunResponse.model_fields}


# ---------------------------------------------------------------------------
# Schema compatibility with the frozen public DTOs
# ---------------------------------------------------------------------------


def test_every_fixture_validates_through_real_agent_run_response() -> None:
    for fixture in RESPONSES:
        mirror = AgentRunResponse(**_real_fields(fixture.response))
        assert mirror.status == fixture.response.status
        assert mirror.answer == fixture.response.answer
        assert mirror.grounded == fixture.response.grounded
        assert mirror.citations == fixture.response.citations
        assert mirror.warnings == fixture.response.warnings
        assert mirror.error == fixture.response.error


def test_every_demo_source_is_a_real_source_response() -> None:
    for source in SOURCES:
        public = source.to_public()
        assert isinstance(public, DemoSourceResponse)
        assert isinstance(public, SourceResponse)
        mirror = SourceResponse(
            stable_id=public.stable_id,
            type=public.type,
            title=public.title,
            label=public.label,
        )
        assert mirror.model_dump(mode="json") == {
            "stable_id": public.stable_id,
            "type": public.type.value,
            "title": public.title,
            "label": public.label,
        }


def test_demo_response_dump_is_a_field_superset_of_real_contract() -> None:
    fixture = next(item for item in RESPONSES if item.key == "success_grounded_a")
    dumped = fixture.response.model_dump(mode="json")
    assert set(AgentRunResponse.model_fields) < set(dumped)
    assert dumped["mode"] == "mock_demo"
    real_fields = {
        "request_id", "status", "answer", "grounded", "citations", "warnings", "error",
    }
    assert real_fields <= set(dumped)


def test_error_payloads_come_from_the_closed_catalog() -> None:
    failed = next(item for item in RESPONSES if item.key == "failed_safe")
    assert failed.response.error is not None
    assert failed.response.error == public_error("tool_failed")
    assert failed.response.error.message == "知识库工具执行失败。"


# ---------------------------------------------------------------------------
# Citation rendering mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", RESPONSES, ids=lambda item: item.key)
def test_citations_detail_mirrors_citations(fixture: object) -> None:
    response = fixture.response  # type: ignore[attr-defined]
    assert len(response.citations_detail) == len(response.citations)
    for index, (citation, detail) in enumerate(
        zip(response.citations, response.citations_detail, strict=True), start=1
    ):
        _, kind, _ = parse_source_id(detail.stable_id)
        assert detail.display_index == index
        assert detail.stable_id == citation
        assert detail.source_type.value == kind
        assert detail.anchor_label.strip()
        assert detail.title == safe_display_text(detail.title)
        assert detail.label == safe_display_text(detail.label)


@pytest.mark.parametrize("fixture", RESPONSES, ids=lambda item: item.key)
def test_grounded_answers_carry_valid_marker_sequence(fixture: object) -> None:
    response = fixture.response  # type: ignore[attr-defined]
    if not (response.status == "completed" and response.grounded):
        pytest.skip("only grounded completed answers carry citation markers")
    numbers = [int(match.group(1)) for match in _CITATION_MARKER.finditer(response.answer)]
    first_seen: list[int] = []
    for number in numbers:
        if number not in first_seen:
            first_seen.append(number)
    assert first_seen == list(range(1, len(response.citations) + 1))


# ---------------------------------------------------------------------------
# Source lookup mapping
# ---------------------------------------------------------------------------


def test_source_lookup_matches_citation_details(client: MockDemoClient) -> None:
    for fixture in RESPONSES:
        for detail in fixture.response.citations_detail:
            source = client.get_source(detail.stable_id)
            assert source.type == detail.source_type
            assert source.title == detail.title
            assert source.label == detail.label


def test_source_lookup_preserves_public_metadata_only(client: MockDemoClient) -> None:
    source = client.get_source(SOURCES[0].stable_id)
    assert source.integrity_state is None
    assert source.demo_note is None
    assert source.type is ContextItemType.PAGE


def test_integrity_scenario_exposes_preset_changed_state(client: MockDemoClient) -> None:
    source = client.get_source(SOURCES[-1].stable_id)
    assert source.type is ContextItemType.KNOWLEDGE_OBJECT
    assert source.integrity_state is ContextFingerprintState.CHANGED
    assert source.demo_note is not None
    assert "不是实时核验结果" in source.demo_note


def test_unknown_and_malformed_source_ids_fail_like_real_http(
    client: MockDemoClient,
) -> None:
    unknown = f"{SOURCES[0].stable_id.rsplit(':', 1)[0]}:999999"
    with pytest.raises(DemoSourceError) as exc:
        client.get_source(unknown)
    assert exc.value.http_status == 404
    assert isinstance(exc.value.failure, HTTPFailure)
    assert exc.value.failure.error == public_error("not_found")

    with pytest.raises(DemoSourceError) as exc:
        client.get_source("NOT-A-UUID:page:1")
    assert exc.value.http_status == 422
    assert exc.value.failure.error == public_error("invalid_source_id")
    # The error envelope never echoes the rejected client input.
    assert "NOT-A-UUID" not in exc.value.failure.model_dump_json()

    with pytest.raises(DemoSourceError) as exc:
        client.get_source("0e6b1de5-0000-4000-8000-000000000001:knowledge_source:1")
    assert exc.value.http_status == 404


# ---------------------------------------------------------------------------
# Success / partial / failed / empty state semantics
# ---------------------------------------------------------------------------


def test_scenario_a_is_grounded_without_warnings(client: MockDemoClient) -> None:
    response = client.run_agent(DEMO_QUESTION_A)
    assert response.status == "completed"
    assert response.grounded is True
    assert response.warnings == ()
    assert response.error is None
    assert len(response.citations) == 2


def test_scenario_b_is_grounded_with_generic_warning(client: MockDemoClient) -> None:
    response = client.run_agent(DEMO_QUESTION_B)
    assert response.status == "completed"
    assert response.grounded is True
    assert response.warnings == (GENERIC_WARNING,)
    assert len(response.citations) == 1


def test_scenario_c_cites_knowledge_object_with_warning(client: MockDemoClient) -> None:
    response = client.run_agent(DEMO_QUESTION_C)
    assert response.grounded is True
    assert response.warnings == (GENERIC_WARNING,)
    _, kind, _ = parse_source_id(response.citations[0])
    assert kind == ContextItemType.KNOWLEDGE_OBJECT.value


def test_empty_scenario_is_honest_no_evidence(client: MockDemoClient) -> None:
    response = client.run_agent(DEMO_QUESTION_EMPTY)
    assert response.status == "completed"
    assert response.grounded is False
    assert response.answer == NO_EVIDENCE_MESSAGE
    assert response.citations == ()
    assert response.error is None


def test_failed_scenario_is_safe_business_failure(client: MockDemoClient) -> None:
    response = client.run_agent(DEMO_QUESTION_FAILED)
    assert response.status == "failed"
    assert response.grounded is False
    assert response.answer == ""
    assert response.citations == ()
    assert response.error is not None
    assert response.error.code == "tool_failed"


# ---------------------------------------------------------------------------
# Mock determinism, guards and mode separation
# ---------------------------------------------------------------------------


def test_same_input_yields_byte_identical_output(client: MockDemoClient) -> None:
    first = client.run_agent(DEMO_QUESTION_A, correlation_id="demo-1")
    second = client.run_agent(DEMO_QUESTION_A, correlation_id="demo-1")
    assert first.model_dump_json() == second.model_dump_json()
    assert first.request_id == second.request_id


def test_unknown_question_falls_back_to_honest_empty_state(
    client: MockDemoClient,
) -> None:
    response = client.run_agent("完全不在题卡里的问题")
    assert response.grounded is False
    assert response.citations == ()
    stripped = client.run_agent(f"  {DEMO_QUESTION_A}  \n")
    assert stripped.model_dump(mode="json")["answer"] == client.run_agent(
        DEMO_QUESTION_A
    ).model_dump(mode="json")["answer"]


def test_request_guards_mirror_real_limits(client: MockDemoClient) -> None:
    with pytest.raises(DemoRequestError) as exc:
        client.run_agent("x" * 120_001)
    assert exc.value.http_status == 422
    assert exc.value.failure.error == public_error("invalid_request")

    with pytest.raises(DemoRequestError):
        client.run_agent(DEMO_QUESTION_A, correlation_id="invalid id!")

    with pytest.raises(DemoRequestError):
        client.run_agent(DEMO_QUESTION_A, correlation_id="x" * 129)

    with pytest.raises(DemoRequestError):
        client.run_agent(None)  # type: ignore[arg-type]


def test_demo_mode_marker_is_constant(client: MockDemoClient) -> None:
    for question in (DEMO_QUESTION_A, DEMO_QUESTION_EMPTY, DEMO_QUESTION_FAILED):
        response = client.run_agent(question)
        assert response.mode == "mock_demo"


# ---------------------------------------------------------------------------
# Preset integrity
# ---------------------------------------------------------------------------


def test_preset_table_has_three_main_two_backup_and_rehearsal(
    client: MockDemoClient,
) -> None:
    presets = client.list_presets()
    roles = [preset.role for preset in presets]
    assert roles.count("main") == 3
    assert roles.count("backup") == 2
    assert roles.count("rehearsal") == 1


def test_preset_expectations_match_fixtures(client: MockDemoClient) -> None:
    for preset in client.list_presets():
        response = client.run_agent(preset.question)
        fixture = load_demo_catalog().response(preset.expected_fixture)
        assert response.citations == preset.expected_cited_sources
        assert response.citations == fixture.response.citations
        expected_warning = (
            (GENERIC_WARNING,) if preset.expected_warning_state == "generic_limitation" else ()
        )
        assert response.warnings == expected_warning


def test_fixture_keys_and_catalog_are_consistent() -> None:
    catalog = load_demo_catalog()
    assert {fixture.key for fixture in catalog.responses} == set(FIXTURE_KEYS)
    for key in (
        "success_grounded_a",
        "partial_warning_b",
        "failed_safe",
        "empty_result",
        "integrity_warning_c",
    ):
        catalog.response(key)


# ---------------------------------------------------------------------------
# Environment / network / database isolation
# ---------------------------------------------------------------------------


def test_demo_results_do_not_depend_on_environment(client: MockDemoClient) -> None:
    baseline = client.run_agent(DEMO_QUESTION_A).model_dump_json()
    monkey_env = {
        "EKB_AI_API_KEY": "sentinel-should-never-be-read",
        "EKB_AI_MODE": "api",
        "EKB_DATA_DIR": "D:/somewhere/else",
        "EKB_RUNTIME_PROFILE": "local",
    }
    for key, value in monkey_env.items():
        os.environ[key] = value
    try:
        assert client.run_agent(DEMO_QUESTION_A).model_dump_json() == baseline
    finally:
        for key in monkey_env:
            os.environ.pop(key, None)


def test_demo_package_never_imports_runtime_database_hosted_or_ai() -> None:
    for module in DEMO_PACKAGE_ROOT.rglob("*.py"):
        source = module.read_text(encoding="utf-8")
        for match in _IMPORT_LINE.finditer(source):
            imported = match.group(1)
            for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                hit = imported == prefix or imported.startswith(prefix + ".")
                assert not hit, f"{module} must not import {imported!r}"


def test_fixtures_stay_free_of_paths_secrets_and_production_data() -> None:
    document = catalog_to_json()
    for marker in ("data/database", "knowledge.db", "api_key", "sk-", ":\\", "Bearer "):
        assert marker not in document
    for source in SOURCES:
        assert safe_display_text(source.title) == source.title
        assert safe_display_text(source.label) == source.label


def test_json_snapshot_matches_code_fixtures() -> None:
    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert committed == catalog_to_json()


def test_snapshot_json_parses_and_matches_dto_shapes() -> None:
    document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert document["mode"] == "mock_demo"
    for payload in document["responses"].values():
        response = DemoAgentRunResponse.model_validate(payload)
        assert response.mode == "mock_demo"
        assert response.request_id == ""
    assert {preset["preset_id"] for preset in document["presets"]} == {
        "A",
        "A2",
        "B",
        "C",
        "EMPTY",
        "REHEARSAL_FAILED",
    }
