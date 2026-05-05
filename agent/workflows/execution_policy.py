"""Workflow-owned delegate/terminal execution argument policy.

The helpers in this module are intentionally narrow: they only apply after a
targeted workflow has been activated in runtime state.  They do not execute
tools and do not replace the normal approval, plugin, logging, or permission
paths; callers rewrite/block arguments before handing them to those existing
paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .review_response_state import (
    ReviewResponsePhase,
    ReviewResponseWorkflowState,
    ViolationRecord,
)


ENFORCED_WORKFLOW_IDS = frozenset({"review_response", "omx_delegation"})
ENFORCED_TOOL_NAMES = frozenset({"delegate_task", "terminal"})
_PROMPT_FILE_KEYS = ("prompt_file", "prompt_file_path")
_PROMPT_FILE_DELIVERY_VALUES = frozenset({"file", "prompt_file", "prompt-file"})


@dataclass(frozen=True)
class WorkflowToolPolicyResult:
    """Result of applying workflow execution-shape policy."""

    args: dict[str, Any]
    error: str | None = None
    rewrites: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.error is not None


def enforce_workflow_tool_policy(
    *,
    workflow_id: str | None,
    tool_name: str,
    args: dict[str, Any],
    state: ReviewResponseWorkflowState | None = None,
) -> WorkflowToolPolicyResult:
    """Rewrite or block workflow-owned delegate/terminal tool arguments.

    Policy:
    - Applies only to targeted workflow IDs and only to delegate_task or
      workflow-owned terminal delegation launches.
    - Blocks prompt-file execution mode for workflow-owned delegations.
    - Blocks delegate_task because the tool is synchronous and cannot satisfy
      the background completion-notification contract.
    - Rewrites absent or boolean-false terminal background/notify flags to True.
    - Blocks non-boolean explicit conflicts fail-closed rather than guessing.
    """

    if workflow_id not in ENFORCED_WORKFLOW_IDS or tool_name not in ENFORCED_TOOL_NAMES:
        return WorkflowToolPolicyResult(args=dict(args))

    next_args = dict(args)

    if tool_name == "delegate_task" and not _is_workflow_owned_delegate_task(next_args):
        return WorkflowToolPolicyResult(args=next_args)

    prompt_file_error = _prompt_file_policy_error(tool_name, next_args)
    if prompt_file_error:
        _record_violation(
            state,
            code="workflow_prompt_file_mode_blocked",
            message=prompt_file_error,
            tool_name=tool_name,
            args=next_args,
        )
        return WorkflowToolPolicyResult(args=next_args, error=prompt_file_error)

    if tool_name == "delegate_task":
        message = (
            "Workflow-owned delegate_task execution is blocked because delegate_task "
            "runs synchronously; launch workflow delegations through terminal with "
            "background=true and notify_on_complete=true."
        )
        _record_violation(
            state,
            code="workflow_delegate_task_synchronous_blocked",
            message=message,
            tool_name=tool_name,
            args=next_args,
        )
        return WorkflowToolPolicyResult(args=next_args, error=message)

    if tool_name == "terminal" and not _is_workflow_owned_terminal_delegation(next_args):
        return WorkflowToolPolicyResult(args=next_args)

    if tool_name == "terminal" and str(next_args.get("delegation_type") or "") in {
        "workflow-guard",
        "workflow-finalize",
        "workflow-thread-resolution",
    }:
        return WorkflowToolPolicyResult(args=next_args)

    prompt_file_error = _prompt_file_policy_error(tool_name, next_args)
    if prompt_file_error:
        _record_violation(
            state,
            code="workflow_prompt_file_mode_blocked",
            message=prompt_file_error,
            tool_name=tool_name,
            args=next_args,
        )
        return WorkflowToolPolicyResult(args=next_args, error=prompt_file_error)

    for key in ("background", "notify_on_complete"):
        value = next_args.get(key, None)
        if value is None or value is False:
            next_args[key] = True
            continue
        if value is True:
            continue

        message = (
            f"Workflow policy requires {tool_name}({key}=true); "
            f"got explicit incompatible value {value!r}."
        )
        _record_violation(
            state,
            code="workflow_execution_flag_conflict",
            message=message,
            tool_name=tool_name,
            args=next_args,
            details={"flag": key, "value": repr(value)},
        )
        return WorkflowToolPolicyResult(args=next_args, error=message)

    rewrites = {
        key: next_args[key]
        for key in ("background", "notify_on_complete")
        if args.get(key, None) is None or args.get(key) is False
    }
    return WorkflowToolPolicyResult(args=next_args, rewrites=rewrites)


def _is_workflow_owned_delegate_task(args: dict[str, Any]) -> bool:
    """Return whether a delegate_task call carries explicit workflow ownership metadata."""

    if args.get("workflow_owned") is True or args.get("workflow_delegation") is True:
        return True
    return any(args.get(key) for key in ("workflow_phase", "delegation_type"))


def _is_workflow_owned_terminal_delegation(args: dict[str, Any]) -> bool:
    """Return whether a terminal call is a workflow-owned delegation launch."""

    if args.get("workflow_owned") is True or args.get("workflow_delegation") is True:
        return True
    return any(args.get(key) for key in ("workflow_phase", "delegation_type", "delivery_method"))


def _prompt_file_policy_error(tool_name: str, args: dict[str, Any]) -> str | None:
    prompt_file_keys = [key for key in _PROMPT_FILE_KEYS if args.get(key)]
    delivery_method = str(args.get("delivery_method") or "").strip().lower()
    if tool_name == "delegate_task" and (
        prompt_file_keys or delivery_method in _PROMPT_FILE_DELIVERY_VALUES
    ):
        return (
            "Workflow-owned delegate_task prompt-file execution mode is blocked; "
            "build and pass delegation prompts inline."
        )

    tasks = args.get("tasks")
    if tool_name == "delegate_task" and isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_delivery = str(task.get("delivery_method") or "").strip().lower()
            if any(task.get(key) for key in _PROMPT_FILE_KEYS) or task_delivery in _PROMPT_FILE_DELIVERY_VALUES:
                return (
                    "Workflow-owned delegate_task prompt-file execution mode is blocked; "
                    "build and pass delegation prompts inline."
                )
    if tool_name == "terminal" and _is_workflow_owned_terminal_delegation(args):
        command = str(args.get("command") or "").lower()
        if prompt_file_keys or delivery_method in _PROMPT_FILE_DELIVERY_VALUES:
            return (
                "Workflow-owned terminal delegation prompt-file execution mode is blocked; "
                "build and pass delegation prompts inline."
            )
        if "prompt_file" in command or "prompt-file" in command:
            return (
                "Workflow-owned terminal delegation prompt-file execution mode is blocked; "
                "build and pass delegation prompts inline."
            )
    return None


def _record_violation(
    state: ReviewResponseWorkflowState | None,
    *,
    code: str,
    message: str,
    tool_name: str,
    args: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> None:
    if state is None:
        return

    phase = str(args.get("workflow_phase") or getattr(state, "phase", "") or "")
    merged_details = {
        "tool_name": tool_name,
        "background": args.get("background"),
        "notify_on_complete": args.get("notify_on_complete"),
        "delivery_method": args.get("delivery_method"),
    }
    if details:
        merged_details.update(details)

    state.phase = ReviewResponsePhase.ABORTED.value
    state.finalization.aborted = True
    state.violations.append(
        ViolationRecord(
            code=code,
            message=message,
            phase=phase,
            created_at=datetime.now(timezone.utc).isoformat(),
            details=merged_details,
        )
    )
