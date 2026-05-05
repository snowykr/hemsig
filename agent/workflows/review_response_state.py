"""Typed persisted state for targeted review-response workflows.

The store is intentionally narrow: it owns only runtime workflow state for the
review-response hardening work and keeps that state outside LLM-visible session
history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
import logging
from pathlib import Path
import re
from typing import Any

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STATE_WORKFLOW_IDS = frozenset({"review_response", "omx_delegation"})


class ReviewResponsePhase(StrEnum):
    """Known high-level phases for the targeted review-response workflow."""

    ACTIVATED = "activated"
    DUPLICATE_SESSION_CHECK = "duplicate_session_check"
    ACTIONABLE_STATE_CHECK = "actionable_state_check"
    PR_ANALYSIS = "pr_analysis"
    EARLY_EXIT_GATE = "early_exit_gate"
    CHANGED_REVIEW = "changed_review"
    FIX_LOOP = "fix_loop"
    VERIFICATION = "verification"
    FINAL_APPROVAL_GATE = "final_approval_gate"
    FINALIZATION = "finalization"
    THREAD_RESOLUTION = "thread_resolution"
    FINAL_REPORT = "final_report"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass
class BackgroundHandle:
    """Persisted reference to a background process or delegated task handle."""

    handle_id: str
    kind: str = ""
    status: str = "unknown"
    task_id: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReportSnapshot:
    """Structured snapshot of a phase/report artifact."""

    report_id: str
    phase: str
    generated_at: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ViolationRecord:
    """Machine-readable workflow policy violation record."""

    code: str
    message: str
    phase: str = ""
    created_at: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalFlags:
    """Approval gates tracked outside conversation history."""

    ready_for_final_report: bool = False
    final_report_approved: bool = False
    human_approval_required: bool = False


@dataclass
class FinalizationFlags:
    """Finalization status tracked by the workflow runtime."""

    finalized: bool = False
    aborted: bool = False
    completion_report_emitted: bool = False


@dataclass
class LoopGateState:
    """Deterministic fix/review loop gates for review-response workflows."""

    loop_count: int = 0
    consecutive_no_edit_passes: int = 0
    consecutive_no_progress_passes: int = 0
    requires_fix_pass: bool = False
    requires_verification_pass: bool = False
    latest_verification_had_edits: bool = False
    latest_verification_had_new_issues: bool = False
    latest_verification_signature: str = ""
    approval_decision: str = "pending"
    deny_reason: str = ""
    terminal_state: str = "active"
    terminal_reason: str = ""


@dataclass
class ReviewResponseWorkflowState:
    """Persisted runtime state for one targeted workflow session."""

    session_id: str
    workflow_id: str = "review_response"
    schema_version: int = SCHEMA_VERSION
    phase: str = ReviewResponsePhase.ACTIVATED.value
    loop_counters: dict[str, int] = field(default_factory=dict)
    delegated_task_ids: list[str] = field(default_factory=list)
    last_delegated_step: str = ""
    background_handles: list[BackgroundHandle] = field(default_factory=list)
    report_snapshots: list[ReportSnapshot] = field(default_factory=list)
    approvals: ApprovalFlags = field(default_factory=ApprovalFlags)
    finalization: FinalizationFlags = field(default_factory=FinalizationFlags)
    loop_gates: LoopGateState = field(default_factory=LoopGateState)
    structured_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    violations: list[ViolationRecord] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def default(cls, session_id: str, workflow_id: str = "review_response") -> "ReviewResponseWorkflowState":
        now = _utc_now_iso()
        return cls(
            session_id=str(session_id or ""),
            workflow_id=str(workflow_id or "review_response"),
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        session_id: str,
        workflow_id: str = "review_response",
    ) -> "ReviewResponseWorkflowState":
        if not isinstance(payload, dict):
            return cls.default(session_id, workflow_id)

        state = cls.default(
            str(payload.get("session_id") or session_id or ""),
            str(payload.get("workflow_id") or workflow_id or "review_response"),
        )
        state.schema_version = _coerce_int(payload.get("schema_version"), SCHEMA_VERSION)
        state.phase = _coerce_phase(payload.get("phase"))
        state.loop_counters = _coerce_int_map(payload.get("loop_counters"))
        state.delegated_task_ids = _coerce_str_list(payload.get("delegated_task_ids"))
        state.last_delegated_step = str(payload.get("last_delegated_step") or "")
        state.background_handles = [
            BackgroundHandle(
                handle_id=str(item.get("handle_id") or item.get("id") or ""),
                kind=str(item.get("kind") or ""),
                status=str(item.get("status") or "unknown"),
                task_id=str(item.get("task_id") or ""),
                created_at=str(item.get("created_at") or ""),
                metadata=_coerce_dict(item.get("metadata")),
            )
            for item in _coerce_dict_list(payload.get("background_handles"))
        ]
        state.report_snapshots = [
            ReportSnapshot(
                report_id=str(item.get("report_id") or item.get("id") or ""),
                phase=str(item.get("phase") or ""),
                generated_at=str(item.get("generated_at") or ""),
                data=_coerce_dict(item.get("data")),
            )
            for item in _coerce_dict_list(payload.get("report_snapshots"))
        ]
        approvals = _coerce_dict(payload.get("approvals"))
        state.approvals = ApprovalFlags(
            ready_for_final_report=bool(approvals.get("ready_for_final_report", False)),
            final_report_approved=bool(approvals.get("final_report_approved", False)),
            human_approval_required=bool(approvals.get("human_approval_required", False)),
        )
        finalization = _coerce_dict(payload.get("finalization"))
        state.finalization = FinalizationFlags(
            finalized=bool(finalization.get("finalized", False)),
            aborted=bool(finalization.get("aborted", False)),
            completion_report_emitted=bool(finalization.get("completion_report_emitted", False)),
        )
        loop_gates = _coerce_dict(payload.get("loop_gates"))
        state.loop_gates = LoopGateState(
            loop_count=_coerce_int(loop_gates.get("loop_count"), 0),
            consecutive_no_edit_passes=_coerce_int(loop_gates.get("consecutive_no_edit_passes"), 0),
            consecutive_no_progress_passes=_coerce_int(loop_gates.get("consecutive_no_progress_passes"), 0),
            requires_fix_pass=bool(loop_gates.get("requires_fix_pass", False)),
            requires_verification_pass=bool(loop_gates.get("requires_verification_pass", False)),
            latest_verification_had_edits=bool(loop_gates.get("latest_verification_had_edits", False)),
            latest_verification_had_new_issues=bool(loop_gates.get("latest_verification_had_new_issues", False)),
            latest_verification_signature=str(loop_gates.get("latest_verification_signature") or ""),
            approval_decision=str(loop_gates.get("approval_decision") or "pending"),
            deny_reason=str(loop_gates.get("deny_reason") or ""),
            terminal_state=str(loop_gates.get("terminal_state") or "active"),
            terminal_reason=str(loop_gates.get("terminal_reason") or ""),
        )
        state.structured_results = {
            _coerce_phase(key): _coerce_dict(value)
            for key, value in _coerce_dict(payload.get("structured_results")).items()
            if _coerce_phase(key) != ReviewResponsePhase.ACTIVATED.value
        }
        state.violations = [
            ViolationRecord(
                code=str(item.get("code") or ""),
                message=str(item.get("message") or ""),
                phase=str(item.get("phase") or ""),
                created_at=str(item.get("created_at") or ""),
                details=_coerce_dict(item.get("details")),
            )
            for item in _coerce_dict_list(payload.get("violations"))
        ]
        state.created_at = str(payload.get("created_at") or state.created_at)
        state.updated_at = str(payload.get("updated_at") or state.updated_at)
        return state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def touch(self) -> None:
        self.updated_at = _utc_now_iso()


class ReviewResponseWorkflowStateStore:
    """Profile-safe JSON store for targeted workflow state."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or get_hermes_home() / "workflow_state" / "review_response"

    def path_for_session(self, session_id: str) -> Path:
        safe_session_id = _safe_session_id(session_id)
        return self.base_dir / f"{safe_session_id}.json"

    def default_state(self, session_id: str, workflow_id: str = "review_response") -> ReviewResponseWorkflowState:
        return ReviewResponseWorkflowState.default(session_id, workflow_id)

    def load(self, session_id: str, workflow_id: str = "review_response") -> ReviewResponseWorkflowState:
        path = self.path_for_session(session_id)
        if not path.exists():
            return self.default_state(session_id, workflow_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Ignoring corrupt workflow state at %s: %s", path, exc)
            return self.default_state(session_id, workflow_id)
        if not isinstance(payload, dict):
            return self.default_state(session_id, workflow_id)
        return ReviewResponseWorkflowState.from_dict(
            payload,
            session_id=session_id,
            workflow_id=workflow_id,
        )

    def save(self, state: ReviewResponseWorkflowState) -> Path:
        state.touch()
        path = self.path_for_session(state.session_id)
        atomic_json_write(path, state.to_dict(), sort_keys=True)
        return path

    def reset(self, session_id: str) -> None:
        path = self.path_for_session(session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Failed to reset workflow state at %s: %s", path, exc)


_TERMINAL_BACKGROUND_PHASE_ORDER: dict[str, str] = {
    ReviewResponsePhase.PR_ANALYSIS.value: ReviewResponsePhase.CHANGED_REVIEW.value,
    ReviewResponsePhase.CHANGED_REVIEW.value: ReviewResponsePhase.FIX_LOOP.value,
    ReviewResponsePhase.FIX_LOOP.value: ReviewResponsePhase.VERIFICATION.value,
    ReviewResponsePhase.VERIFICATION.value: ReviewResponsePhase.FINAL_REPORT.value,
}

_BACKGROUND_COMPLETION_RE = re.compile(
    r"\[IMPORTANT:\s*Background process\s+(?P<handle_id>\S+)\s+completed\s+"
    r"\(exit code\s+(?P<exit_code>-?\d+|None)\)\.\s*\n"
    r"Command:\s*(?P<command>.*?)\nOutput:\n(?P<output>.*)\]",
    re.DOTALL,
)


def record_workflow_background_handle(
    state: ReviewResponseWorkflowState | None,
    *,
    handle_id: str,
    kind: str,
    task_id: str = "",
    workflow_phase: str = "",
    delegation_type: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Record or refresh a persisted background/delegation handle.

    This helper only stores durable handle metadata. It does not start work,
    poll work, or bypass the existing terminal/delegate execution paths.
    """

    if state is None:
        return False
    normalized_handle_id = str(handle_id or "").strip()
    if not normalized_handle_id:
        return False

    phase = _coerce_phase(workflow_phase or state.phase)
    next_phase = _next_background_phase(phase)
    merged_metadata = _coerce_dict(metadata)
    merged_metadata.update(
        {
            "workflow_phase": phase,
            "delegation_type": str(delegation_type or ""),
        }
    )
    if next_phase:
        merged_metadata["next_phase"] = next_phase

    handle = _find_background_handle(state, normalized_handle_id)
    if handle is None:
        state.background_handles.append(
            BackgroundHandle(
                handle_id=normalized_handle_id,
                kind=str(kind or ""),
                status="running",
                task_id=str(task_id or ""),
                created_at=_utc_now_iso(),
                metadata=merged_metadata,
            )
        )
        state.last_delegated_step = phase
        return True

    changed = False
    for attr, value in {
        "kind": str(kind or handle.kind or ""),
        "task_id": str(task_id or handle.task_id or ""),
    }.items():
        if getattr(handle, attr) != value:
            setattr(handle, attr, value)
            changed = True
    if handle.status in {"", "unknown"}:
        handle.status = "running"
        changed = True
    if not handle.created_at:
        handle.created_at = _utc_now_iso()
        changed = True
    if handle.metadata != {**handle.metadata, **merged_metadata}:
        handle.metadata.update(merged_metadata)
        changed = True
    if changed:
        state.last_delegated_step = phase
    return changed


def extract_background_completion_from_message(message: str) -> dict[str, Any] | None:
    """Parse the existing watcher completion notification shape, if present."""

    match = _BACKGROUND_COMPLETION_RE.search(str(message or ""))
    if not match:
        return None
    raw_exit = match.group("exit_code")
    exit_code = None if raw_exit == "None" else _coerce_int(raw_exit, 0)
    return {
        "handle_id": match.group("handle_id"),
        "exit_code": exit_code,
        "command": match.group("command"),
        "output": match.group("output"),
        "source": "watcher_message",
    }


def ingest_workflow_background_completion(
    state: ReviewResponseWorkflowState | None,
    *,
    handle_id: str,
    exit_code: int | None = None,
    output: str = "",
    command: str = "",
    source: str = "",
) -> bool:
    """Persist a background completion and advance when no structured gate exists."""

    if state is None:
        return False
    normalized_handle_id = str(handle_id or "").strip()
    if not normalized_handle_id:
        return False

    handle = _find_background_handle(state, normalized_handle_id)
    if handle is None:
        handle = BackgroundHandle(
            handle_id=normalized_handle_id,
            kind="terminal",
            status="unknown",
            created_at=_utc_now_iso(),
            metadata={"workflow_phase": state.phase},
        )
        state.background_handles.append(handle)

    completion: dict[str, Any] = {
        "exit_code": exit_code,
        "output": str(output or ""),
        "command": str(command or ""),
        "source": str(source or ""),
        "completed_at": _utc_now_iso(),
    }
    workflow_phase = _coerce_phase(handle.metadata.get("workflow_phase") or state.phase)
    delegation_type = str(handle.metadata.get("delegation_type") or "")
    structured_result = extract_structured_completion_result(
        output,
        workflow_phase=workflow_phase,
        delegation_type=delegation_type,
        exit_code=exit_code,
    )
    if structured_result:
        completion["structured_result"] = structured_result
        state.structured_results[workflow_phase] = structured_result

    next_status = "completed" if exit_code in (0, None) else "failed"
    changed = handle.status != next_status or handle.metadata.get("completion") != completion
    handle.status = next_status
    handle.metadata["completion"] = completion
    if structured_result and handle.metadata.get("structured_result") != structured_result:
        handle.metadata["structured_result"] = structured_result
        changed = True

    if next_status == "completed" and not structured_result:
        next_phase = str(handle.metadata.get("next_phase") or "")
        if next_phase and state.phase in {workflow_phase, ReviewResponsePhase.ACTIVATED.value}:
            state.phase = _coerce_phase(next_phase)
            changed = True
    return changed


def extract_structured_completion_result(
    output: Any,
    *,
    workflow_phase: str = "",
    delegation_type: str = "",
    exit_code: int | None = None,
) -> dict[str, Any]:
    """Extract gate-driving phase result data from delegate completion output.

    This accepts structured JSON emitted directly by a delegate or following a
    marker such as ``WORKFLOW_DELEGATE_RESULT=``.  Freeform output remains
    non-authoritative and falls back to legacy phase-boundary behavior.
    """

    payload = _parse_structured_payload(output)
    if not payload:
        return {}
    source = _select_structured_source(payload)
    phase = _coerce_phase(_first_present(source, "workflow_phase", "phase") or workflow_phase)
    delegation = str(_first_present(source, "delegation_type", "type") or delegation_type or "")
    completed = _coerce_optional_bool(_first_present(source, "completed", "done"))
    if completed is None:
        completed = bool(exit_code in (0, None) and _coerce_successish(source))

    if phase == ReviewResponsePhase.PR_ANALYSIS.value or delegation == "pr-analysis":
        result = {
            "result_kind": "pr_analysis",
            "completed": completed,
            "unresolved_review_threads": _coerce_optional_int(_first_present(source, "unresolved_review_threads", "unresolved_threads")),
            "failing_ci": _coerce_optional_bool(_first_present(source, "failing_ci", "ci_failing")),
            "modifications_made": _coerce_optional_bool(
                _first_present(source, "modifications_made", "made_changes", "files_modified", "modified_files")
            ),
            "working_tree_clean": _coerce_optional_bool(_first_present(source, "working_tree_clean", "clean_working_tree")),
            "root_cause_no_valid_issues": _coerce_optional_bool(
                _first_present(source, "root_cause_no_valid_issues", "no_valid_issues", "no_action_required")
            ),
            "ci_status": _string_from_any(_first_present(source, "ci_status"), default="not reported"),
            "local_verification": _string_from_any(
                _first_present(source, "local_verification", "verification_summary", "verification"),
                default="not reported",
            ),
        }
        return {key: value for key, value in result.items() if value is not None}

    if phase == ReviewResponsePhase.CHANGED_REVIEW.value or delegation == "changed-review":
        blocking_findings = _string_list_from_any(
            _first_present(source, "blocking_findings", "findings", "issues"),
        )
        result = {
            "result_kind": "changed_review",
            "completed": completed,
            "succeeded": bool(_coerce_successish(source)),
            "blocking_findings": blocking_findings,
            "non_blocking_watch_items": _string_list_from_any(
                _first_present(source, "non_blocking_watch_items", "watch_items", "warnings"),
            ),
            "made_changes": bool(_coerce_optional_bool(_first_present(source, "made_changes", "modifications_made", "files_modified", "modified_files"))),
            "requires_human_confirmation": bool(_coerce_optional_bool(_first_present(source, "requires_human_confirmation", "requires_confirmation"))),
            "clean_recommendation": bool(_coerce_optional_bool(_first_present(source, "clean_recommendation", "approved_for_finalization", "clean"))),
            "review_findings": _string_from_any(
                _first_present(source, "review_findings", "summary", "final_response"),
                default="",
            ),
        }
        return result

    return {}


def reconcile_workflow_background_handles(state: ReviewResponseWorkflowState | None) -> bool:
    """Reconcile persisted handles against the existing process registry once.

    This is a deterministic resume check, not a polling loop. It lets a resumed
    or restarted agent consume handles that the existing registry already knows
    are finished.
    """

    if state is None:
        return False
    try:
        from tools.process_registry import process_registry
    except Exception:
        return False

    changed = False
    for handle in list(state.background_handles):
        if handle.kind and handle.kind != "terminal":
            continue
        if handle.status in {"completed", "failed"}:
            continue
        session = process_registry.get(handle.handle_id)
        if session is None:
            continue
        if getattr(session, "exited", False):
            changed = ingest_workflow_background_completion(
                state,
                handle_id=handle.handle_id,
                exit_code=getattr(session, "exit_code", None),
                output=getattr(session, "output_buffer", "") or "",
                command=getattr(session, "command", "") or "",
                source="process_registry_reconcile",
            ) or changed
    return changed


def _find_background_handle(
    state: ReviewResponseWorkflowState,
    handle_id: str,
) -> BackgroundHandle | None:
    for handle in state.background_handles:
        if handle.handle_id == handle_id:
            return handle
    return None


def _next_background_phase(phase: str) -> str:
    return _TERMINAL_BACKGROUND_PHASE_ORDER.get(str(phase or ""), "")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_session_id(session_id: str) -> str:
    text = str(session_id or "").strip()
    if not text:
        return "unknown"
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_phase(value: Any) -> str:
    text = str(value or "").strip()
    if text in {phase.value for phase in ReviewResponsePhase}:
        return text
    return ReviewResponsePhase.ACTIVATED.value


def _coerce_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _coerce_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _coerce_int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _coerce_int(raw, 0) for key, raw in value.items()}


def _parse_structured_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    for candidate in (text, _marked_json_text(text), _first_json_object_text(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _marked_json_text(text: str) -> str:
    marker = re.search(r"(?:WORKFLOW_DELEGATE_RESULT|WORKFLOW_PHASE_RESULT|STRUCTURED_RESULT)=(\{.*\})", text, re.DOTALL)
    return marker.group(1).strip() if marker else ""


def _first_json_object_text(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _select_structured_source(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("pr_analysis", "changed_review", "result", "delegate_result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            merged = dict(nested)
            for metadata_key in ("workflow_phase", "phase", "delegation_type", "type"):
                if metadata_key in payload and metadata_key not in merged:
                    merged[metadata_key] = payload[metadata_key]
            return merged
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return dict(results[0])
    return payload


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "success", "succeeded", "ok", "passed", "clean"}:
        return True
    if text in {"false", "no", "n", "0", "failed", "failure", "error", "blocked", "dirty"}:
        return False
    return None


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_successish(source: dict[str, Any]) -> bool:
    for key in ("succeeded", "success", "ok", "clean_recommendation", "clean"):
        value = _coerce_optional_bool(source.get(key))
        if value is not None:
            return value
    status = str(source.get("status") or "").strip().lower()
    return status in {"completed", "success", "succeeded", "ok", "passed", "clean"}


def _string_list_from_any(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return [str(value).strip()] if str(value).strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def _string_from_any(value: Any, *, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default
