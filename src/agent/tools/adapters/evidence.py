"""``get_evidence`` read-only Tool Adapter (v0.6.0 Phase 1C).

Thin adapter over the existing read-only :class:`EvidenceBasketService.get_item`.
It resolves a canonical ``evidence`` stable-id, reads the existing EvidenceItem,
and projects the confirmation / source / region metadata into a structured
ToolResult. Unconfirmed evidence remains readable but is explicitly surfaced as
PARTIAL with a warning.
"""

from __future__ import annotations

from datetime import datetime

from src.agent.tools.adapters._common import (
    AdapterInputError,
    failed_result,
    internal_failure_result,
    parse_stable_id,
    partial_result,
    reject_unknown_arguments,
    require_text,
    success_result,
)
from src.agent.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolInput,
    ToolReference,
    ToolResult,
    ToolSideEffect,
)
from src.evidence_basket_service import EvidenceBasketService
from src.models import (
    EVIDENCE_STABLE_TYPE,
    PAGE_STABLE_TYPE,
    EvidenceConfirmationStatus,
    EvidenceItem,
    build_stable_id,
)

ALLOWED_ARGUMENTS = frozenset({"stable_id"})
MAX_STABLE_ID_LENGTH = 300

GET_EVIDENCE_DEFINITION = ToolDefinition(
    name="get_evidence",
    description="按 stable_id 读取一条已存在的 Evidence 及其确认/来源元数据。",
    side_effect=ToolSideEffect.READ_ONLY,
    input_schema={
        "stable_id": {
            "type": "string",
            "required": True,
            "description": "证据 stable_id，格式 <kb_uuid>:evidence:<id>",
        },
    },
    timeout_seconds=30.0,
)


class GetEvidenceAdapter:
    """Execute ``get_evidence`` through EvidenceBasketService.get_item."""

    tool_name = "get_evidence"

    def __init__(self, evidence_service: EvidenceBasketService, *, kb_uuid: str) -> None:
        self._service = evidence_service
        self._kb_uuid = kb_uuid

    def __call__(self, tool_input: ToolInput, context: ToolContext) -> ToolResult:
        try:
            reject_unknown_arguments(tool_input.arguments, ALLOWED_ARGUMENTS)
            stable_id = require_text(
                tool_input.arguments, "stable_id", max_length=MAX_STABLE_ID_LENGTH
            )
            kb_uuid, object_type, local_id = parse_stable_id(stable_id)
            if kb_uuid != self._kb_uuid:
                return failed_result(
                    self.tool_name,
                    ToolErrorCode.NOT_FOUND,
                    "证据不属于当前知识库",
                )
            if object_type != EVIDENCE_STABLE_TYPE:
                raise AdapterInputError(
                    f"stable_id 类型必须是 {EVIDENCE_STABLE_TYPE}"
                )
            item = self._service.get_item(local_id)
        except AdapterInputError as exc:
            return failed_result(
                self.tool_name, ToolErrorCode.INVALID_INPUT, exc.message
            )
        except Exception as exc:
            return internal_failure_result(
                self.tool_name, exc, safe_message="读取证据失败"
            )
        if item is None:
            return failed_result(
                self.tool_name,
                ToolErrorCode.NOT_FOUND,
                "证据不存在",
            )
        return self._to_result(stable_id, item)

    def _to_result(self, stable_id: str, item: EvidenceItem) -> ToolResult:
        data = {
            "stable_id": stable_id,
            "id": item.id,
            "evidence_type": item.evidence_type.value,
            "confirmation_status": item.confirmation_status.value,
            "confirmed": item.confirmation_status is EvidenceConfirmationStatus.CONFIRMED,
            "text_kind": item.text_kind.value,
            "from_ocr_text": item.from_ocr_text,
            "document_id": item.document_id,
            "document_title": item.document_title,
            "filename": item.filename,
            "page_id": item.page_id,
            "page_number": item.page_number,
            "evidence_text": item.evidence_text,
            "user_note": item.user_note,
            "context": item.context,
            "source_text_sha256": item.source_text_sha256,
            "source_locator": item.source_locator,
            "added_at": _iso_or_none(item.added_at),
            "position": item.position,
            "region_metadata": _region_metadata(item),
        }
        references: list[ToolReference] = [
            ToolReference(stable_id=stable_id, anchor_label=item.document_title)
        ]
        references.append(
            ToolReference(
                stable_id=build_stable_id(
                    self._kb_uuid, PAGE_STABLE_TYPE, item.page_id
                ),
                anchor_label=f"页面 {item.page_number}",
            )
        )
        warnings: list[str] = []
        if item.confirmation_status is EvidenceConfirmationStatus.UNCONFIRMED:
            warnings.append("该证据尚未人工确认，不能视为已确认来源。")
        if warnings:
            return partial_result(
                self.tool_name,
                data,
                warnings=tuple(warnings),
                references=tuple(references),
            )
        return success_result(
            self.tool_name, data, references=tuple(references)
        )


def _region_metadata(item: EvidenceItem) -> dict[str, object]:
    return {
        "image_sha256": item.region_image_sha256,
        "image_width": item.region_image_width,
        "image_height": item.region_image_height,
        "x0": item.region_x0,
        "y0": item.region_y0,
        "x1": item.region_x1,
        "y1": item.region_y1,
    }


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = ["GET_EVIDENCE_DEFINITION", "GetEvidenceAdapter"]
