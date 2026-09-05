"""Run the real QW3.8 conflict checklist against the formal local library.

The two PDFs must already be imported and fully read by the Agent. This probe
does not import, edit, or delete formal documents; it asks the checklist
questions, records the grounded answers, and verifies cited document pages.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.local_client import LocalDocumentAgentClient  # noqa: E402
from src.agent_document_reader import AgentReadingStore  # noqa: E402
from src.runtime import (  # noqa: E402
    application_ai_provider,
    application_database,
    application_settings,
)
from src.source_metadata import InvalidSourceId, parse_source_id  # noqa: E402


@dataclass(frozen=True, slots=True)
class Check:
    question: str
    required_fragments: tuple[str, ...]
    required_locations: tuple[tuple[str, int], ...]
    conflict: bool = False
    no_answer: bool = False


CHECKS = (
    Check(
        "EKB-S42 的额定转速是多少？",
        ("1379", "1426", "R2", "R3-C"),
        (("EKB测试PDF", 2), ("EKB冲突测试资料_R3-C", 2)),
        conflict=True,
    ),
    Check(
        "两份资料中的额定转速是否一致？",
        ("1379", "1426"),
        (("EKB测试PDF", 2), ("EKB冲突测试资料_R3-C", 2)),
        conflict=True,
    ),
    Check(
        "低速来回振荡的根因到底是什么？",
        ("A/B", "DIR_INV", "1"),
        (("EKB测试PDF", 4), ("EKB冲突测试资料_R3-C", 3)),
        conflict=True,
    ),
    Check(
        "这次排障到底用了多久？",
        ("42", "58"),
        (("EKB测试PDF", 5), ("EKB冲突测试资料_R3-C", 3)),
        conflict=True,
    ),
    Check(
        "E17 是什么意思？",
        ("编码器方向不一致", "编码器信号完整性异常", "3.2", "3.3"),
        (("EKB测试PDF", 6), ("EKB冲突测试资料_R3-C", 4)),
        conflict=True,
    ),
    Check(
        "在 DemoSuite 3.3 中，E17 表示什么？",
        ("编码器信号完整性异常",),
        (("EKB冲突测试资料_R3-C", 4),),
    ),
    Check(
        "散热风扇多久检查一次？",
        ("500", "300", "高粉尘"),
        (("EKB测试PDF", 7), ("EKB冲突测试资料_R3-C", 5)),
        conflict=True,
    ),
    Check(
        "冲突资料里的唯一验证标记是什么？",
        ("EKB-CONFLICT-4C81M",),
        (("EKB冲突测试资料_R3-C", 7),),
    ),
    Check(
        "两份资料各自的唯一验证标记是什么？",
        ("EKB-VERIFY-9F27K", "EKB-CONFLICT-4C81M"),
        (("EKB测试PDF", 8), ("EKB冲突测试资料_R3-C", 7)),
    ),
    Check(
        "EKB-S42 的电机轴承型号是什么？",
        (),
        (),
        no_answer=True,
    ),
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _locations(stable_ids: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    database = application_database()
    values: list[tuple[str, int]] = []
    for stable_id in stable_ids:
        try:
            _, kind, local_id = parse_source_id(stable_id)
        except InvalidSourceId:
            continue
        if kind != "page":
            continue
        page = database.get_page(local_id)
        if page is None:
            continue
        document = database.get_document(page.document_id)
        if document is not None:
            values.append((document.title, page.page_number))
    return tuple(values)


def _location_present(
    actual: tuple[tuple[str, int], ...], expected: tuple[str, int]
) -> bool:
    expected_title, expected_page = expected
    return any(
        expected_title in title and page_number == expected_page
        for title, page_number in actual
    )


def _evaluate(check: Check, answer: str, locations: tuple[tuple[str, int], ...]) -> bool:
    if check.no_answer:
        return any(
            marker in answer
            for marker in ("没有提供", "未提供", "未找到", "信息不足", "无法确定")
        )
    fragments_ok = all(fragment in answer for fragment in check.required_fragments)
    locations_ok = all(
        _location_present(locations, expected) for expected in check.required_locations
    )
    conflict_ok = not check.conflict or any(
        marker in answer for marker in ("不同", "差异", "冲突", "不一致")
    )
    return fragments_ok and locations_ok and conflict_ok


def run(output: Path) -> dict[str, object]:
    settings = application_settings()
    database = application_database()
    expected_titles = {"EKB测试PDF", "EKB冲突测试资料_R3-C"}
    documents = tuple(database.list_documents())
    available_titles = {document.title for document in documents}
    missing = sorted(expected_titles - available_titles)
    if missing:
        raise RuntimeError("正式资料库缺少测试资料：" + "、".join(missing))
    reading_store = AgentReadingStore(settings.agent_readings_dir)
    unread = [
        document.title
        for document in documents
        if document.title in expected_titles
        and not (
            (state := reading_store.document_state(document.id))
            and state.completed
            and state.model == settings.ai_llm_model_hard
        )
    ]
    if unread:
        raise RuntimeError("以下资料尚未让 Agent 读完：" + "、".join(unread))
    client = LocalDocumentAgentClient(
        database=database,
        provider=application_ai_provider(),
        readings=reading_store,
        model=settings.ai_llm_model_hard,
    )
    results: list[dict[str, object]] = []
    for index, check in enumerate(CHECKS, start=1):
        before_call_ids = {
            call.call_uuid for call in database.list_ai_calls(limit=500)
        }
        response = client.run_agent(check.question)
        new_calls = tuple(
            call
            for call in database.list_ai_calls(limit=500)
            if call.call_uuid not in before_call_ids
        )
        model_calls = tuple(
            call
            for call in new_calls
            if call.source_feature in {"agent_decision", "agent_final_answer"}
        )
        model_verified = bool(model_calls) and all(
            call.model == settings.ai_llm_model_hard and call.status == "success"
            for call in model_calls
        )
        locations = _locations(response.citations)
        passed = (
            response.status == "completed"
            and model_verified
            and _evaluate(check, response.answer, locations)
        )
        results.append(
            {
                "number": index,
                "question": check.question,
                "answer": response.answer,
                "status": response.status,
                "error_code": response.error.code if response.error else None,
                "error_message": response.error.message if response.error else None,
                "model": settings.ai_llm_model_hard,
                "model_verified_from_ai_ledger": model_verified,
                "ai_call_features": [call.source_feature for call in model_calls],
                "grounded": response.grounded,
                "citation_locations": [
                    {"document": title, "page": page}
                    for title, page in locations
                ],
                "passed": passed,
            }
        )
        print(f"{'PASS' if passed else 'FAIL'} {index}/10：{check.question}", flush=True)
    report: dict[str, object] = {
        "model": settings.ai_llm_model_hard,
        "documents": [
            {
                "title": document.title,
                "sha256": document.sha256,
                "page_count": document.page_count,
            }
            for document in documents
            if document.title in expected_titles
        ],
        "checks": results,
        "passed": all(bool(item["passed"]) for item in results),
    }
    _write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "conflict-agent-audit-report.json",
    )
    args = parser.parse_args()
    try:
        report = run(args.output.resolve())
    except Exception as exc:
        print(f"CONFLICT AUDIT FAIL：{exc}", file=sys.stderr)
        return 1
    print(f"报告：{args.output.resolve()}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
