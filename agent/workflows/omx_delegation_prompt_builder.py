"""Inline prompt builders for review-response OMX delegations.

These helpers are intentionally narrow: they cover only the workflow-owned
delegated phases used by ``github-pr-review-response``.  The normal path returns
prompt text in memory so callers can pass it inline to the delegation execution
layer.  Builder failures are fail-closed and record a structured workflow
violation instead of falling back to prompt files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .review_response_state import (
    ReviewResponsePhase,
    ReviewResponseWorkflowState,
    ReviewResponseWorkflowStateStore,
    ViolationRecord,
)


class WorkflowDelegationPhase(StrEnum):
    """Workflow-owned delegated phases for PR review response."""

    PR_ANALYSIS = "pr-analysis"
    CHANGED_REVIEW = "changed-review"
    REFIX_RESOLVE_PROBLEM = "refix-resolve-problem"
    REFIX_CODE_EDIT = "refix-code-edit"
    REFIX_CODE_ANALYSIS = "refix-code-analysis"


class WorkflowPromptBuildError(RuntimeError):
    """Raised when a workflow prompt cannot be built safely inline."""


@dataclass(frozen=True)
class WorkflowDelegationPromptRequest:
    """Inputs needed to build a phase-specific inline delegation prompt."""

    phase: WorkflowDelegationPhase | str
    pr_url: str = ""
    repo_path: str = ""
    review_findings: str = ""
    fix_scope: str = ""
    finalized_plan: str = ""


@dataclass(frozen=True)
class InlineDelegationPrompt:
    """Prompt payload for inline-only workflow delegation."""

    phase: WorkflowDelegationPhase
    delegation_type: str
    body: str
    delivery_method: str = "inline"
    prompt_file_path: str | None = None

    def to_delegate_task_kwargs(self) -> dict[str, object]:
        """Return an inline-only shape suitable for delegation callers."""

        return {
            "goal": self.body,
            "context": None,
            "prompt_file": None,
            "delivery_method": self.delivery_method,
            "workflow_phase": self.phase.value,
            "delegation_type": self.delegation_type,
        }


GITHUB_PR_PREFINALIZATION_SAFETY_BLOCK = """IMPORTANT SAFETY CONSTRAINTS:
- Do NOT commit or push.
- Do NOT resolve review threads on GitHub.
- Do NOT merge the PR or modify remote review state.
- Leave all changes in the working tree for later review and atomic finalization.
- If a thread appears invalid, report it for later handling rather than resolving it now."""

_PR_ANALYSIS_TEMPLATE = """{pr_url}
Use the GitHub CLI (`gh` CLI: `pr`, `api`, `graphql`, etc.) to check the review details above. In particular, check for any unresolved reviews. Also check whether there are any CI failures.

Based on the review details above, create a concrete code modification plan to resolve the issues. You must deeply validate the validity of the issues from multiple angles and present a valid solution. Accurately identify the issues, determine the root causes, and establish a strategy to resolve them.

Actively use the various agents you can call to verify the work. If necessary, create and run reproducible tests to confirm the failures, then perform the bug fixes, and finally run the tests again after the fixes to confirm that they pass.

Create a very granular Todo list and proceed with the code modification work according to the plan until the task is fully completed.

You may invoke the following OMX skills to assist your work: $ultrawork, $plan, $ralplan, $analyze, $autopilot."""

_CHANGED_REVIEW_TEMPLATE = """cd {repo_path}

PR URL: {pr_url}

Conduct a multifaceted review of the changes made. Verify whether all planned tasks have been completed successfully. Run the full CI, all tests, and whole-codebase LSP checks, and introduce new tests if necessary.

This phase is review-only. Do NOT modify, create, delete, stage, or rewrite files as part of this changed-review pass. Do NOT implement fixes during review. If you identify a problem, report the finding, explain the root cause, and recommend the follow-up fix work for the separate re-fix phase.

You may invoke the following OMX skills to assist your review: $code-review, $verifier, $security-review."""

_RESOLVE_PROBLEM_TEMPLATE = """PR URL: {pr_url}
Repository: {repo_path}

Follow-up review findings to fix:
{review_findings}

Accurately identify the issue, determine the root cause, and establish a strategy to resolve it. A concrete plan is required.

Reflecting the above, create a final, comprehensive work plan. If necessary, create reproducible tests. Generate a very granular Todo list and proceed with the code modification work according to the plan until the task is fully completed.

Once the work is complete, run the full CI and all tests, and introduce new tests if necessary.

You may invoke the following OMX skills to assist your work: $ultrawork, $autopilot, $pipeline, $plan, $ralplan, $executor, $verifier."""

_CODE_EDIT_TEMPLATE = """PR URL: {pr_url}
Repository: {repo_path}

Finalized narrow re-fix plan:
{finalized_plan}

Implement the code changes required to resolve the issue according to the finalized plan. Create a granular Todo list and proceed step by step until the task is fully completed.

Make focused, minimal, and maintainable changes that address the root cause rather than only treating the symptoms. Preserve the existing architecture, conventions, public behavior, and compatibility unless a deliberate change is required. Continuously verify that the implementation does not introduce side effects or regressions.

Add or update reproducible tests as needed, including regression tests and important edge cases. After the implementation is complete, run the relevant tests, full test suite, and CI-equivalent checks where possible. Confirm that the issue has been resolved, all planned work has been completed, and all tests pass.

You may invoke the following OMX skills to assist your implementation: $ultrawork, $ralph, $executor, $build-fixer."""

_CODE_ANALYSIS_TEMPLATE = """PR URL: {pr_url}
Repository: {repo_path}

Follow-up issue needing renewed diagnosis:
{fix_scope}

Conduct a comprehensive analysis of the relevant code before making any changes. Identify the exact issue, intended behavior, current behavior, affected code paths, dependencies, and possible regression risks.

Deeply validate whether the issue is real and reproducible. Trace the root cause through the implementation, including control flow, data flow, state management, boundary conditions, and error handling. Do not stop at symptoms; determine the underlying cause that must be fixed.

If necessary, create and run a reproducible test or minimal failing case to confirm the issue. Then propose a concrete, technically sound fix strategy. The analysis must clearly explain the problem, root cause, affected scope, risks, and recommended implementation approach.

You may invoke the following OMX skills to assist your analysis: $analyze, $deep-interview, $explore, $debugger."""

_DELEGATION_TYPES = {
    WorkflowDelegationPhase.PR_ANALYSIS: "pr-analysis",
    WorkflowDelegationPhase.CHANGED_REVIEW: "changed-review",
    WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM: "resolve-problem",
    WorkflowDelegationPhase.REFIX_CODE_EDIT: "code-edit",
    WorkflowDelegationPhase.REFIX_CODE_ANALYSIS: "code-analysis",
}


def build_review_response_delegation_prompt(
    request: WorkflowDelegationPromptRequest,
) -> InlineDelegationPrompt:
    """Build an inline-only prompt for a targeted review-response phase."""

    phase = _coerce_phase(request.phase)
    if phase is WorkflowDelegationPhase.PR_ANALYSIS:
        body = _PR_ANALYSIS_TEMPLATE.format(pr_url=_required(request.pr_url, "pr_url"))
    elif phase is WorkflowDelegationPhase.CHANGED_REVIEW:
        body = _CHANGED_REVIEW_TEMPLATE.format(
            repo_path=_required_path(request.repo_path, "repo_path"),
            pr_url=_required(request.pr_url, "pr_url"),
        )
    elif phase is WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM:
        body = _RESOLVE_PROBLEM_TEMPLATE.format(
            pr_url=_required(request.pr_url, "pr_url"),
            repo_path=_required_path(request.repo_path, "repo_path"),
            review_findings=_required(request.review_findings, "review_findings"),
        )
    elif phase is WorkflowDelegationPhase.REFIX_CODE_EDIT:
        body = _CODE_EDIT_TEMPLATE.format(
            pr_url=_required(request.pr_url, "pr_url"),
            repo_path=_required_path(request.repo_path, "repo_path"),
            finalized_plan=_required(request.finalized_plan, "finalized_plan"),
        )
    elif phase is WorkflowDelegationPhase.REFIX_CODE_ANALYSIS:
        body = _CODE_ANALYSIS_TEMPLATE.format(
            pr_url=_required(request.pr_url, "pr_url"),
            repo_path=_required_path(request.repo_path, "repo_path"),
            fix_scope=_required(request.fix_scope, "fix_scope"),
        )
    else:  # pragma: no cover - _coerce_phase exhausts known enum values.
        raise WorkflowPromptBuildError(f"Unsupported workflow delegation phase: {phase}")

    body = _append_safety_block(body)
    if not body.strip():
        raise WorkflowPromptBuildError(f"Built empty prompt for {phase.value}")
    return InlineDelegationPrompt(
        phase=phase,
        delegation_type=_DELEGATION_TYPES[phase],
        body=body,
    )


def build_review_response_delegation_prompt_or_abort(
    request: WorkflowDelegationPromptRequest,
    state: ReviewResponseWorkflowState,
    *,
    store: ReviewResponseWorkflowStateStore | None = None,
) -> InlineDelegationPrompt:
    """Build a prompt or record a fail-closed violation and abort the workflow."""

    try:
        return build_review_response_delegation_prompt(request)
    except Exception as exc:
        phase_text = str(getattr(request, "phase", "") or "")
        state.phase = ReviewResponsePhase.ABORTED.value
        state.finalization.aborted = True
        state.violations.append(
            ViolationRecord(
                code="inline_prompt_build_failed",
                message="Workflow-owned delegation prompt could not be built inline; prompt-file fallback is blocked.",
                phase=phase_text,
                created_at=_utc_now_iso(),
                details={"error": str(exc), "delivery_method": "inline"},
            )
        )
        if store is not None:
            _ = store.save(state)
        raise WorkflowPromptBuildError(str(exc)) from exc


def _append_safety_block(body: str) -> str:
    return f"{body.strip()}\n\n{GITHUB_PR_PREFINALIZATION_SAFETY_BLOCK}"


def _coerce_phase(value: WorkflowDelegationPhase | str) -> WorkflowDelegationPhase:
    try:
        return value if isinstance(value, WorkflowDelegationPhase) else WorkflowDelegationPhase(str(value))
    except ValueError as exc:
        raise WorkflowPromptBuildError(f"Unsupported workflow delegation phase: {value}") from exc


def _required(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowPromptBuildError(f"Missing required prompt field: {field_name}")
    return text


def _required_path(value: str, field_name: str) -> str:
    text = _required(value, field_name)
    return str(Path(text).expanduser())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
