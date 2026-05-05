"""Versioned deterministic reports for review-response workflows.

The workflow owns these user-visible report shapes so final output cannot drift
with delegate prose ordering.  Delegate output is accepted only as structured
inputs that can be normalized into the versioned schemas below.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import json
from typing import Any

from .review_response_state import ReportSnapshot, ReviewResponseWorkflowState, ViolationRecord


REPORT_SCHEMA_VERSION = 1


class ReportValidationError(ValueError):
    """Raised when workflow report input cannot satisfy the fixed schema."""


class ReviewResponseReportType(StrEnum):
    """Versioned report surfaces for the targeted review-response workflow."""

    STEP = "review_response.step_report"
    COMPLETION = "review_response.completion_report"


@dataclass(frozen=True)
class DelegateResultReportInput:
    """Normalized subset of a delegate result used by deterministic reports."""

    succeeded: bool
    files_modified: list[str] = field(default_factory=list)
    decisions_requiring_confirmation: list[str] = field(default_factory=list)
    pre_finalization_status: str = "pre-finalization"
    next_recommended_step: str = "Continue workflow."
    verification_summary: str = "not reported"
    raw_summary: str = ""

    def __post_init__(self) -> None:
        _require_bool(self.succeeded, "succeeded")
        _require_str_list(self.files_modified, "files_modified")
        _require_str_list(
            self.decisions_requiring_confirmation,
            "decisions_requiring_confirmation",
        )
        _require_non_empty(self.pre_finalization_status, "pre_finalization_status")
        _require_non_empty(self.next_recommended_step, "next_recommended_step")
        _require_non_empty(self.verification_summary, "verification_summary")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewResponseStepReport:
    """Schema for one workflow step completion report."""

    pr_number: int
    phase: str
    delegate_result: DelegateResultReportInput
    schema_version: int = REPORT_SCHEMA_VERSION
    report_type: ReviewResponseReportType = ReviewResponseReportType.STEP

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        _require_positive_int(self.pr_number, "pr_number")
        _require_non_empty(self.phase, "phase")
        if not isinstance(self.delegate_result, DelegateResultReportInput):
            raise ReportValidationError("delegate_result must be normalized before rendering")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["report_type"] = self.report_type.value
        return payload

    def to_snapshot(self, report_id: str) -> ReportSnapshot:
        return ReportSnapshot(report_id=report_id, phase=self.phase, data=self.to_dict())


@dataclass(frozen=True)
class ReviewResponseCompletionReport:
    """Schema for final or early-exit workflow completion reports."""

    pr_number: int
    unresolved_review_threads: int
    ci_status: str
    working_tree: str
    local_verification: str
    completion_status: str
    thread_resolution: str = "not applicable"
    schema_version: int = REPORT_SCHEMA_VERSION
    report_type: ReviewResponseReportType = ReviewResponseReportType.COMPLETION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        _require_positive_int(self.pr_number, "pr_number")
        _require_non_negative_int(self.unresolved_review_threads, "unresolved_review_threads")
        _require_non_empty(self.ci_status, "ci_status")
        _require_non_empty(self.working_tree, "working_tree")
        _require_non_empty(self.local_verification, "local_verification")
        _require_non_empty(self.completion_status, "completion_status")
        _require_non_empty(self.thread_resolution, "thread_resolution")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["report_type"] = self.report_type.value
        return payload

    def to_snapshot(self, report_id: str) -> ReportSnapshot:
        return ReportSnapshot(report_id=report_id, phase="completion", data=self.to_dict())


def normalize_delegate_result_for_report(result: Any) -> DelegateResultReportInput:
    """Normalize delegate output into typed report input or reject it.

    Accepted shapes are dictionaries, JSON objects, or the parent-visible
    ``delegate_task`` shape ``{"results": [...], "total_duration_seconds": ...}``.
    Freeform strings and structurally invalid values are rejected so they cannot
    become authoritative report prose.
    """

    payload = _parse_delegate_payload(result)
    source = _select_delegate_result(payload)

    succeeded = _coerce_success(source)
    files_modified = _string_list_from_any(
        _first_present(source, "files_modified", "modified_files", "files"),
        field_name="files_modified",
    )
    decisions = _string_list_from_any(
        _first_present(
            source,
            "decisions_requiring_confirmation",
            "decisions",
            "requires_confirmation",
        ),
        field_name="decisions_requiring_confirmation",
    )
    pre_finalization_status = _string_from_any(
        _first_present(source, "pre_finalization_status", "finalization_status", "status"),
        default="pre-finalization",
    )
    next_step = _string_from_any(
        _first_present(source, "next_recommended_step", "next_step", "recommendation"),
        default="Continue workflow.",
    )
    verification = _string_from_any(
        _first_present(source, "verification_summary", "local_verification", "verification"),
        default="not reported",
    )
    raw_summary = _string_from_any(_first_present(source, "summary", "final_response"), default="")

    return DelegateResultReportInput(
        succeeded=succeeded,
        files_modified=files_modified,
        decisions_requiring_confirmation=decisions,
        pre_finalization_status=pre_finalization_status,
        next_recommended_step=next_step,
        verification_summary=verification,
        raw_summary=raw_summary,
    )


def normalize_delegate_result_or_record_violation(
    result: Any,
    state: ReviewResponseWorkflowState,
    *,
    phase: str,
) -> DelegateResultReportInput:
    """Normalize delegate output, recording a machine-readable violation on failure."""

    try:
        return normalize_delegate_result_for_report(result)
    except ReportValidationError as exc:
        state.violations.append(
            ViolationRecord(
                code="malformed_delegate_report_output",
                message="Delegate output could not be normalized into the workflow report schema.",
                phase=str(phase or ""),
                details={"error": str(exc)},
            )
        )
        raise


def render_step_report(report: ReviewResponseStepReport) -> str:
    """Render a deterministic post-completion step report."""

    report.__post_init__()
    delegate = report.delegate_result
    return "\n".join(
        [
            f"PR #{report.pr_number} workflow step report",
            "",
            "## Schema",
            f"- Version: {report.schema_version}",
            f"- Type: {report.report_type.value}",
            "",
            "## Step",
            f"- Phase: {report.phase}",
            f"- Delegate status: {'succeeded' if delegate.succeeded else 'failed'}",
            "",
            "## Modified files",
            *_render_list(delegate.files_modified, empty="none reported"),
            "",
            "## Decisions requiring confirmation",
            *_render_list(delegate.decisions_requiring_confirmation, empty="none"),
            "",
            "## Workflow status",
            f"- Pre-finalization state: {delegate.pre_finalization_status}",
            f"- Local verification: {delegate.verification_summary}",
            f"- Next recommended step: {delegate.next_recommended_step}",
        ]
    )


def render_completion_report(report: ReviewResponseCompletionReport) -> str:
    """Render a deterministic final completion report."""

    report.__post_init__()
    return "\n".join(
        [
            f"PR #{report.pr_number} review-response complete — {report.completion_status}.",
            "",
            "## Schema",
            f"- Version: {report.schema_version}",
            f"- Type: {report.report_type.value}",
            "",
            "## Required status",
            f"- Unresolved review threads: {report.unresolved_review_threads}",
            f"- CI status: {report.ci_status}",
            f"- Working tree: {report.working_tree}",
            f"- Local verification: {report.local_verification}",
            "",
            "## Review thread resolution",
            f"- Resolution status: {report.thread_resolution}",
        ]
    )


def render_clean_pr_early_exit_report(report: ReviewResponseCompletionReport) -> str:
    """Render the exact clean-PR early-exit shape required by the skill."""

    report.__post_init__()
    if report.unresolved_review_threads != 0:
        raise ReportValidationError("clean PR early-exit report requires zero unresolved review threads")
    if report.working_tree != "clean (no changes made)":
        raise ReportValidationError("clean PR early-exit report requires a clean unchanged working tree")
    return "\n".join(
        [
            f"PR #{report.pr_number} review-response complete — no action required.",
            "",
            f"- Unresolved review threads: {report.unresolved_review_threads}",
            f"- CI status: {report.ci_status}",
            f"- Working tree: {report.working_tree}",
            f"- Local verification: {report.local_verification}",
        ]
    )


def _parse_delegate_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ReportValidationError("delegate output must be structured JSON, not freeform prose") from exc
    if not isinstance(result, dict):
        raise ReportValidationError("delegate output must be a JSON object")
    return result


def _select_delegate_result(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    if isinstance(results, list):
        if not results:
            raise ReportValidationError("delegate results list is empty")
        first = results[0]
        if not isinstance(first, dict):
            raise ReportValidationError("delegate results entries must be objects")
        return first
    return payload


def _coerce_success(source: dict[str, Any]) -> bool:
    for key in ("succeeded", "success", "ok"):
        if key in source:
            value = source[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "success", "succeeded", "ok", "passed"}:
                    return True
                if lowered in {"false", "failed", "error", "not ok"}:
                    return False
            raise ReportValidationError(f"{key} must be boolean-like")
    status = str(source.get("status") or "").strip().lower()
    if status in {"success", "succeeded", "ok", "passed", "clean"}:
        return True
    if status in {"failed", "failure", "error", "blocked"}:
        return False
    raise ReportValidationError("delegate output missing required success status")


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _string_list_from_any(value: Any, *, field_name: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        raise ReportValidationError(f"{field_name} must be a list of strings")
    output = [str(item).strip() for item in value if str(item).strip()]
    return output


def _string_from_any(value: Any, *, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _render_list(values: list[str], *, empty: str) -> list[str]:
    if not values:
        return [f"- {empty}"]
    return [f"- {value}" for value in values]


def _require_schema_version(value: int) -> None:
    if value != REPORT_SCHEMA_VERSION:
        raise ReportValidationError(f"unsupported report schema version: {value}")


def _require_bool(value: Any, field_name: str) -> None:
    if not isinstance(value, bool):
        raise ReportValidationError(f"{field_name} must be a bool")


def _require_positive_int(value: Any, field_name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ReportValidationError(f"{field_name} must be a positive integer")


def _require_non_negative_int(value: Any, field_name: str) -> None:
    if not isinstance(value, int) or value < 0:
        raise ReportValidationError(f"{field_name} must be a non-negative integer")


def _require_non_empty(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReportValidationError(f"{field_name} is required")


def _require_str_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ReportValidationError(f"{field_name} must be a list of non-empty strings")
