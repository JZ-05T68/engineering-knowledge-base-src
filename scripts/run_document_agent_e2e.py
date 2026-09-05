"""Run the real fixed-PDF Agent validation without changing the frontend."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final
from uuid import uuid4

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.local_document import LocalDocumentAgent  # noqa: E402
from src.agent_document_reader import AgentReadingStore  # noqa: E402
from src.ai.provider import (  # noqa: E402
    AIBudgetExceededError,
    AiCallRecord,
    build_production_audited_provider,
)
from src.ai.qwen_client import QwenProvider, urllib_transport  # noqa: E402
from src.config import Settings  # noqa: E402
from src.database import Database  # noqa: E402
from src.document_service import DocumentService  # noqa: E402

VALIDATIONS: Final[tuple[tuple[str, str, int], ...]] = (
    ("EKB-S42 的额定转速是多少？", "1379 rpm", 2),
    ("那次编码器问题最终怎么解决？大约用了多久？", "42 分钟", 5),
    ("E17 表示什么？", "编码器方向不一致", 6),
    ("散热风扇多久检查一次？", "500 小时", 7),
    ("这份资料里的唯一验证标记是什么？", "EKB-VERIFY-9F27K", 8),
)
ABSENT_QUESTION: Final[str] = "EKB-S42 的电机轴承型号是什么？"


class _DatabaseLedger:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, call: AiCallRecord) -> None:
        self._database.insert_ai_call(call)


class _ConfiguredBudgetGuard:
    """Use local configured token limits against the isolated probe DB."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self._database = database
        self._daily = settings.ai_daily_token_budget
        self._monthly = settings.ai_monthly_token_budget

    def ensure_allowed(self, capability: str) -> None:
        now = datetime.now(UTC)
        starts = (
            (self._daily, now.replace(hour=0, minute=0, second=0, microsecond=0)),
            (
                self._monthly,
                now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            ),
        )
        for limit, start in starts:
            if limit > 0 and self._database.total_ai_tokens_since(
                start.isoformat(timespec="microseconds")
            ) >= limit:
                raise AIBudgetExceededError("AI 调用被预算限制拒绝。")


def _provider(settings: Settings, database: Database):
    key = settings.ai_api_key.get_secret_value()
    if settings.ai_mode != "api" or not key:
        raise RuntimeError("请先配置 EKB_AI_MODE=api 和 EKB_AI_API_KEY。")
    qwen = QwenProvider(
        api_key=key,
        llm_model=settings.ai_llm_model,
        llm_model_hard=settings.ai_llm_model_hard,
        embedding_model=settings.ai_embedding_model,
        rerank_model=settings.ai_rerank_model,
        timeout_seconds=settings.ai_timeout_seconds,
        max_extra_attempts=settings.ai_max_extra_attempts,
        enable_thinking=False,
        transport=urllib_transport,
    )
    return build_production_audited_provider(
        qwen,
        default_model=settings.ai_llm_model,
        default_embedding_model=settings.ai_embedding_model,
        source_feature="document_agent_e2e",
        ledger=_DatabaseLedger(database),
        budget_guard=_ConfiguredBudgetGuard(database, settings),
    )


def _citation_pages(database: Database, stable_ids: tuple[str, ...]) -> list[int]:
    pages: list[int] = []
    for stable_id in stable_ids:
        parts = stable_id.rsplit(":", 2)
        if len(parts) != 3 or parts[1] != "page":
            continue
        page = database.get_page(int(parts[2]))
        if page is not None:
            pages.append(page.page_number)
    return pages


def _write_report(path: Path, payload: dict[str, object]) -> None:
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


def _mentions_page(answer: str, page_number: int) -> bool:
    """Accept normal Chinese page labels with or without optional spaces."""

    return re.search(rf"第\s*{page_number}\s*页", answer) is not None


def recheck_report(source: Path, output: Path) -> dict[str, object]:
    """Re-evaluate a saved real-model report with the current validator."""

    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), list):
        raise RuntimeError("旧报告格式不正确。")
    expected_by_question = {
        question: (expected, page) for question, expected, page in VALIDATIONS
    }
    for item in payload["answers"]:
        if not isinstance(item, dict):
            raise RuntimeError("旧报告答案格式不正确。")
        question = item.get("question")
        answer = item.get("answer")
        model = item.get("model")
        citation_pages = item.get("citation_pages")
        if (
            not isinstance(question, str)
            or not isinstance(answer, str)
            or not isinstance(citation_pages, list)
        ):
            raise RuntimeError("旧报告缺少可复核字段。")
        if question in expected_by_question:
            expected, page = expected_by_question[question]
            item["passed"] = bool(
                model
                and expected in answer
                and _mentions_page(answer, page)
                and page in citation_pages
            )
        elif question == ABSENT_QUESTION:
            item["passed"] = bool(
                model
                and any(
                    marker in answer
                    for marker in ("没有", "未提供", "未找到", "信息不足", "无法确定")
                )
            )
        else:
            raise RuntimeError(f"旧报告包含未知问题：{question}")
        item["validation_version"] = 2
    payload["passed"] = all(bool(item.get("passed")) for item in payload["answers"])
    payload["rechecked_from"] = str(source.resolve())
    _write_report(output, payload)
    return payload


def run(pdf: Path, output: Path) -> dict[str, object]:
    """Execute import, page reading and all fixed grounded questions."""

    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        raise RuntimeError("测试 PDF 不存在或格式不正确。")
    settings = Settings()
    with TemporaryDirectory(prefix="ekb-document-agent-e2e-") as temporary:
        root = Path(temporary)
        database = Database(root / "database" / "knowledge.db")
        document_service = DocumentService(
            database=database,
            raw_dir=root / "raw",
            pages_dir=root / "pages",
            markdown_dir=root / "markdown",
        )
        imported = document_service.import_pdf(
            pdf.read_bytes(), pdf.name, title=pdf.stem
        )
        provider = _provider(settings, database)
        agent = LocalDocumentAgent(
            database=database,
            provider=provider,
            readings=AgentReadingStore(root / "agent-readings"),
            model=settings.ai_llm_model_hard,
        )
        progress: list[dict[str, int]] = []

        def update(current: int, total: int) -> None:
            progress.append({"current": current, "total": total})
            print(f"正在读取 {current} / {total} 页", flush=True)

        reading = agent.read_document(
            imported.document.id,
            progress_callback=update,
        )
        print("Agent 已读完，现在可以提问", flush=True)
        answers: list[dict[str, object]] = []
        for question, expected, expected_page in VALIDATIONS:
            response = agent.ask(question)
            citation_pages = _citation_pages(database, response.citations)
            passed = (
                response.status == "completed"
                and response.grounded
                and expected in response.answer
                and _mentions_page(response.answer, expected_page)
                and expected_page in citation_pages
            )
            answers.append(
                {
                    "question": question,
                    "answer": response.answer,
                    "model": response.model,
                    "status": response.status,
                    "grounded": response.grounded,
                    "citation_pages": citation_pages,
                    "passed": passed,
                }
            )
            print(f"{'PASS' if passed else 'FAIL'}：{question}", flush=True)

        absent = agent.ask(ABSENT_QUESTION)
        absent_pages = _citation_pages(database, absent.citations)
        honest = any(
            marker in absent.answer
            for marker in ("没有", "未提供", "未找到", "信息不足", "无法确定")
        )
        absent_passed = absent.status == "completed" and honest
        answers.append(
            {
                "question": ABSENT_QUESTION,
                "answer": absent.answer,
                "model": absent.model,
                "status": absent.status,
                "grounded": absent.grounded,
                "citation_pages": absent_pages,
                "passed": absent_passed,
            }
        )
        print(f"{'PASS' if absent_passed else 'FAIL'}：资料外问题不编造", flush=True)
        report: dict[str, object] = {
            "pdf_name": pdf.name,
            "pdf_sha256": imported.document.sha256,
            "page_count": imported.document.page_count,
            "reading": asdict(reading),
            "progress": progress,
            "answers": answers,
            "passed": all(bool(item["passed"]) for item in answers),
        }
    _write_report(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", type=Path)
    source.add_argument("--recheck-report", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "agent-document-e2e-report.json",
    )
    args = parser.parse_args()
    try:
        if args.recheck_report is not None:
            report = recheck_report(
                args.recheck_report.resolve(), args.output.resolve()
            )
        else:
            report = run(args.pdf.resolve(), args.output.resolve())
    except Exception as exc:
        print(f"E2E FAIL：{exc}", file=sys.stderr)
        return 1
    print(f"报告：{args.output.resolve()}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
