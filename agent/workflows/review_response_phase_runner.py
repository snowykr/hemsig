"""Deterministic phase runner for ``github-pr-review-response``.

This module is intentionally narrow.  It owns the mechanical phase sequence for
the review-response workflow, while leaving actual tool execution on the normal
Hermes tool path and reserving judgment calls for structured delegate findings.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import StrEnum
import shlex
from typing import Callable, cast

from .omx_delegation_prompt_builder import (
    InlineDelegationPrompt,
    WorkflowDelegationPhase,
    WorkflowDelegationPromptRequest,
    build_review_response_delegation_prompt_or_abort,
)
from .review_response_reports import (
    ReviewResponseCompletionReport,
    render_clean_pr_early_exit_report,
    render_completion_report,
)
from .review_response_state import (
    ReportSnapshot,
    ReviewResponsePhase,
    ReviewResponseWorkflowState,
    ReviewResponseWorkflowStateStore,
    ViolationRecord,
)


class ReviewResponseRunnerActionType(StrEnum):
    """Actions emitted by the deterministic review-response phase table."""

    RUN_DUPLICATE_SESSION_CHECK = "run_duplicate_session_check"
    RUN_ACTIONABLE_STATE_CHECK = "run_actionable_state_check"
    DELEGATE_PR_ANALYSIS = "delegate_pr_analysis"
    EMIT_EARLY_EXIT_REPORT = "emit_early_exit_report"
    DELEGATE_CHANGED_REVIEW = "delegate_changed_review"
    DELEGATE_REFIX = "delegate_refix"
    FINALIZATION_LOCKED = "finalization_locked"
    FINALIZE = "finalize"
    RESOLVE_THREADS = "resolve_threads"
    COMPLETE = "complete"
    WAIT = "wait"
    ABORT = "abort"


@dataclass(frozen=True)
class ActionableStateCheck:
    """Structured result of the pre-delegation actionable-state guard."""

    unresolved_review_threads: int = 0
    failing_ci: bool = False
    other_actionable_state: bool = False
    ci_status: str = "not reported"
    local_verification: str = "not reported"
    automated_trigger: bool = False

    @property
    def is_clean(self) -> bool:
        return (
            self.unresolved_review_threads == 0
            and not self.failing_ci
            and not self.other_actionable_state
        )


@dataclass(frozen=True)
class PRAnalysisResult:
    """Structured findings from the delegated ``pr-analysis`` phase."""

    completed: bool = False
    unresolved_review_threads: int | None = None
    failing_ci: bool | None = None
    modifications_made: bool | None = None
    working_tree_clean: bool | None = None
    root_cause_no_valid_issues: bool | None = None
    ci_status: str = "not reported"
    local_verification: str = "not reported"

    @property
    def satisfies_clean_early_exit(self) -> bool:
        return (
            self.completed
            and self.unresolved_review_threads == 0
            and self.failing_ci is False
            and self.modifications_made is False
            and self.working_tree_clean is True
            and self.root_cause_no_valid_issues is True
        )


@dataclass(frozen=True)
class ChangedReviewResult:
    """Structured findings from an independent ``changed-review`` pass."""

    completed: bool = False
    succeeded: bool = False
    blocking_findings: tuple[str, ...] = ()
    non_blocking_watch_items: tuple[str, ...] = ()
    made_changes: bool = False
    requires_human_confirmation: bool = False
    clean_recommendation: bool = False
    review_findings: str = ""

    @property
    def blocks_finalization(self) -> bool:
        return (
            not self.completed
            or not self.succeeded
            or bool(self.blocking_findings)
            or self.made_changes
            or self.requires_human_confirmation
            or not self.clean_recommendation
        )


@dataclass(frozen=True)
class ReviewResponseRunnerInputs:
    """External facts the runner may inspect to advance one phase."""

    pr_url: str = ""
    repo_path: str = ""
    pr_number: int = 0
    duplicate_session_active: bool | None = None
    actionable_state: ActionableStateCheck | None = None
    pr_analysis: PRAnalysisResult | None = None
    changed_review: ChangedReviewResult | None = None
    final_report_approved: bool | None = None
    threads_resolved: bool | None = None
    finalization_succeeded: bool | None = None
    refix_phase: WorkflowDelegationPhase = WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM
    refix_findings: str = ""
    finalized_plan: str = ""
    fix_scope: str = ""


@dataclass(frozen=True)
class ReviewResponseRunnerAction:
    """Deterministic action selected by the phase runner."""

    action_type: ReviewResponseRunnerActionType
    phase: ReviewResponsePhase
    reason: str
    prompt: InlineDelegationPrompt | None = None
    report: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def terminal_args(self) -> dict[str, object]:
        """Return workflow metadata suitable for a normal terminal launch."""

        metadata_args = self.metadata.get("terminal_args")
        if isinstance(metadata_args, dict):
            return dict(cast(dict[str, object], metadata_args))
        if self.prompt is None:
            return {}
        return {
            "command": _build_inline_omx_terminal_command(self.prompt, self.phase),
            "workflow_delegation": True,
            "workflow_phase": self.phase.value,
            "delegation_type": self.prompt.delegation_type,
            "delivery_method": self.prompt.delivery_method,
            "background": True,
            "notify_on_complete": True,
        }


PhaseRule = tuple[ReviewResponsePhase, Callable[[ReviewResponseWorkflowState, ReviewResponseRunnerInputs], ReviewResponseRunnerAction | None]]


class ReviewResponsePhaseRunner:
    """Explicit phase-table runner for ``github-pr-review-response``."""

    def __init__(self, store: ReviewResponseWorkflowStateStore | None = None) -> None:
        self.store: ReviewResponseWorkflowStateStore | None = store
        self._rules: tuple[PhaseRule, ...] = (
            (ReviewResponsePhase.ACTIVATED, self._activated),
            (ReviewResponsePhase.DUPLICATE_SESSION_CHECK, self._duplicate_session_check),
            (ReviewResponsePhase.ACTIONABLE_STATE_CHECK, self._actionable_state_check),
            (ReviewResponsePhase.PR_ANALYSIS, self._pr_analysis),
            (ReviewResponsePhase.EARLY_EXIT_GATE, self._early_exit_gate),
            (ReviewResponsePhase.CHANGED_REVIEW, self._changed_review),
            (ReviewResponsePhase.FIX_LOOP, self._fix_loop),
            (ReviewResponsePhase.VERIFICATION, self._verification),
            (ReviewResponsePhase.FINAL_APPROVAL_GATE, self._final_approval_gate),
            (ReviewResponsePhase.FINALIZATION, self._finalization),
            (ReviewResponsePhase.THREAD_RESOLUTION, self._thread_resolution),
            (ReviewResponsePhase.FINAL_REPORT, self._final_report),
            (ReviewResponsePhase.COMPLETED, self._completed),
            (ReviewResponsePhase.ABORTED, self._aborted),
        )

    def next_action(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs | None = None,
    ) -> ReviewResponseRunnerAction:
        """Advance mechanical workflow state by explicit phase rules only."""

        data = inputs or ReviewResponseRunnerInputs()
        phase = _coerce_phase(state.phase)
        for rule_phase, handler in self._rules:
            if phase is rule_phase:
                action = handler(state, data)
                if action is not None:
                    self._save(state)
                    return action
                break
        action = ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.WAIT,
            phase,
            "No transition rule matched the available structured inputs.",
        )
        self._save(state)
        return action

    def finalization_allowed(self, state: ReviewResponseWorkflowState) -> bool:
        """Return whether finalization/thread-resolution phases are unlocked."""

        return bool(
            state.last_delegated_step == ReviewResponsePhase.CHANGED_REVIEW.value
            and state.loop_counters.get("changed_review", 0) >= 1
            and state.approvals.ready_for_final_report
            and not state.loop_gates.requires_fix_pass
            and not state.loop_gates.requires_verification_pass
            and state.loop_gates.approval_decision in {"pending", "approved"}
            and state.loop_gates.terminal_state
            in {
                "approval_gate_ready",
                "active",
                "awaiting_final_approval",
                "finalization_approved",
                "finalized",
                "threads_resolved",
                "completed",
            }
            and _structured_pr_analysis_completed(state)
            and not state.finalization.aborted
            and state.phase
            in {
                ReviewResponsePhase.FINAL_APPROVAL_GATE.value,
                ReviewResponsePhase.FINALIZATION.value,
                ReviewResponsePhase.THREAD_RESOLUTION.value,
                ReviewResponsePhase.FINAL_REPORT.value,
                ReviewResponsePhase.COMPLETED.value,
            }
        )

    def _activated(self, state: ReviewResponseWorkflowState, _: ReviewResponseRunnerInputs) -> ReviewResponseRunnerAction:
        state.phase = ReviewResponsePhase.DUPLICATE_SESSION_CHECK.value
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.RUN_DUPLICATE_SESSION_CHECK,
            ReviewResponsePhase.DUPLICATE_SESSION_CHECK,
            "Activated workflow must first prove no duplicate PR-review session is active.",
            metadata={"terminal_args": _duplicate_session_guard_terminal_args()},
        )

    def _duplicate_session_check(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        if inputs.duplicate_session_active is None:
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.RUN_DUPLICATE_SESSION_CHECK,
                ReviewResponsePhase.DUPLICATE_SESSION_CHECK,
                "Duplicate-session status is not available yet.",
                metadata={"terminal_args": _duplicate_session_guard_terminal_args()},
            )
        if inputs.duplicate_session_active:
            state.phase = ReviewResponsePhase.COMPLETED.value
            state.finalization.completion_report_emitted = True
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.COMPLETE,
                ReviewResponsePhase.COMPLETED,
                "PR review session already in progress — skipping duplicate workflow.",
                metadata={"duplicate_session_active": True},
            )
        state.phase = ReviewResponsePhase.ACTIONABLE_STATE_CHECK.value
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.RUN_ACTIONABLE_STATE_CHECK,
            ReviewResponsePhase.ACTIONABLE_STATE_CHECK,
            "No duplicate session found; actionable PR state must be checked before delegation.",
            metadata={"terminal_args": _actionable_state_guard_terminal_args(inputs)},
        )

    def _actionable_state_check(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        check = inputs.actionable_state
        if check is None:
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.RUN_ACTIONABLE_STATE_CHECK,
                ReviewResponsePhase.ACTIONABLE_STATE_CHECK,
                "Actionable PR state is not available yet.",
                metadata={"terminal_args": _actionable_state_guard_terminal_args(inputs)},
            )
        if check.is_clean:
            state.phase = ReviewResponsePhase.COMPLETED.value
            state.finalization.completion_report_emitted = True
            report = "[SILENT]" if check.automated_trigger else _render_clean_report(inputs, check)
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.EMIT_EARLY_EXIT_REPORT,
                ReviewResponsePhase.COMPLETED,
                "Structured pre-delegation guard found no unresolved threads, failing CI, or other actionable state.",
                report=report,
            )
        state.phase = ReviewResponsePhase.PR_ANALYSIS.value
        return self._delegate_pr_analysis(state, inputs, "Actionable PR state exists; launch delegated pr-analysis.")

    def _pr_analysis(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        if inputs.pr_analysis is None or not inputs.pr_analysis.completed:
            return self._delegate_pr_analysis(state, inputs, "pr-analysis has not completed yet.")
        state.phase = ReviewResponsePhase.EARLY_EXIT_GATE.value
        return self._early_exit_gate(state, inputs)

    def _early_exit_gate(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        result = inputs.pr_analysis
        if result is None or not result.completed:
            state.phase = ReviewResponsePhase.PR_ANALYSIS.value
            return self._delegate_pr_analysis(state, inputs, "Early-exit gate requires completed structured pr-analysis findings.")
        if result.satisfies_clean_early_exit:
            state.phase = ReviewResponsePhase.COMPLETED.value
            state.finalization.completion_report_emitted = True
            report = _render_pr_analysis_clean_report(inputs, result)
            state.report_snapshots.append(
                ReportSnapshot(
                    report_id="early-exit-clean-pr",
                    phase=ReviewResponsePhase.EARLY_EXIT_GATE.value,
                    data={"report": report},
                )
            )
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.EMIT_EARLY_EXIT_REPORT,
                ReviewResponsePhase.COMPLETED,
                "All structured early-exit conditions are satisfied.",
                report=report,
            )
        state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
        return self._delegate_changed_review(state, inputs, "Early-exit gate is not clean; independent changed-review is required.")

    def _changed_review(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        if inputs.changed_review is None or not inputs.changed_review.completed:
            return self._delegate_changed_review(state, inputs, "changed-review has not completed yet.")
        state.loop_counters["changed_review"] = state.loop_counters.get("changed_review", 0) + 1
        state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
        decision = _apply_changed_review_loop_policy(state, inputs.changed_review)
        if decision == "needs_verification":
            state.approvals.ready_for_final_report = False
            state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
            return self._delegate_changed_review(
                state,
                inputs,
                "changed-review modified files; it is treated as a fix pass and requires a fresh independent changed-review.",
            )
        if decision == "needs_fix":
            state.approvals.ready_for_final_report = False
            state.phase = ReviewResponsePhase.FIX_LOOP.value
            return self._delegate_refix(state, inputs, "Independent review found new blocking work; re-fix is required before another verification pass.")
        state.approvals.ready_for_final_report = True
        state.loop_gates.terminal_state = "approval_gate_ready"
        state.loop_gates.terminal_reason = "Latest independent changed-review satisfied loop gates without required follow-up work."
        state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.FINALIZATION_LOCKED,
            ReviewResponsePhase.FINAL_APPROVAL_GATE,
            "Clean changed-review reached the final approval gate; commit/push/thread resolution still require approval.",
        )

    def _fix_loop(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        state.loop_counters["refix"] = state.loop_counters.get("refix", 0) + 1
        state.loop_gates.loop_count += 1
        state.loop_gates.requires_fix_pass = False
        state.loop_gates.requires_verification_pass = True
        state.loop_gates.terminal_state = "fix_pass_required"
        state.loop_gates.terminal_reason = "A modifying fix pass is in progress and must be followed by verification."
        return self._delegate_refix(state, inputs, "Re-fix loop must delegate a follow-up fix before another review pass.")

    def _verification(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
        state.loop_gates.requires_fix_pass = False
        state.loop_gates.requires_verification_pass = True
        state.loop_gates.terminal_state = "verification_required"
        state.loop_gates.terminal_reason = "A modifying pass completed and a fresh independent changed-review is required."
        return self._delegate_changed_review(state, inputs, "Follow-up fix completed; run a fresh independent changed-review.")

    def _final_approval_gate(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        if not self.finalization_allowed(state):
            return _locked_action(state, "Final approval gate conditions are incomplete.")
        if inputs.final_report_approved is not True and not state.approvals.final_report_approved:
            state.approvals.human_approval_required = True
            state.loop_gates.approval_decision = "pending"
            state.loop_gates.terminal_state = "awaiting_final_approval"
            state.loop_gates.terminal_reason = "Loop gates passed; waiting for explicit final approval."
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.FINALIZATION_LOCKED,
                ReviewResponsePhase.FINAL_APPROVAL_GATE,
                "Finalization remains locked until the final approval signal is present.",
            )
        state.approvals.final_report_approved = True
        state.loop_gates.approval_decision = "approved"
        state.loop_gates.terminal_state = "finalization_approved"
        state.loop_gates.terminal_reason = "Explicit final approval received after clean loop gates."
        state.phase = ReviewResponsePhase.FINALIZATION.value
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.FINALIZE,
            ReviewResponsePhase.FINALIZATION,
            "Approval gate is clean; atomic commit/push finalization is now unlocked.",
            metadata={"terminal_args": _finalization_terminal_args(inputs)},
        )

    def _finalization(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        if not self.finalization_allowed(state) or not state.approvals.final_report_approved:
            return _locked_action(state, "Finalization cannot run before a clean approval gate.")
        if inputs.finalization_succeeded is False:
            state.finalization.finalized = False
            state.loop_gates.terminal_state = "finalization_failed"
            state.loop_gates.terminal_reason = "Finalization terminal command did not report success."
            state.phase = ReviewResponsePhase.FINALIZATION.value
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.WAIT,
                ReviewResponsePhase.FINALIZATION,
                "Finalization terminal command failed or reported an incomplete result; thread resolution remains locked.",
            )
        if inputs.finalization_succeeded is not True and not state.finalization.finalized:
            state.loop_gates.terminal_state = "finalization_approved"
            state.loop_gates.terminal_reason = "Finalization is approved but no successful terminal result has been recorded yet."
            state.phase = ReviewResponsePhase.FINALIZATION.value
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.FINALIZE,
                ReviewResponsePhase.FINALIZATION,
                "Run the approved finalization command before resolving review threads.",
                metadata={"terminal_args": _finalization_terminal_args(inputs)},
            )
        state.finalization.finalized = True
        state.loop_gates.terminal_state = "finalized"
        state.loop_gates.terminal_reason = "Atomic finalization completed; thread resolution remains required."
        state.phase = ReviewResponsePhase.THREAD_RESOLUTION.value
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.RESOLVE_THREADS,
            ReviewResponsePhase.THREAD_RESOLUTION,
            "Atomic finalization is complete; review threads may now be resolved.",
            metadata={"terminal_args": _thread_resolution_terminal_args(inputs)},
        )

    def _thread_resolution(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        if not self.finalization_allowed(state) or not state.finalization.finalized:
            return _locked_action(state, "Thread resolution cannot run before post-approval finalization.")
        if inputs.threads_resolved is not True:
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.RESOLVE_THREADS,
                ReviewResponsePhase.THREAD_RESOLUTION,
                "Resolve review threads after finalization using the correct GitHub API path.",
                metadata={"terminal_args": _thread_resolution_terminal_args(inputs)},
            )
        state.phase = ReviewResponsePhase.FINAL_REPORT.value
        state.loop_gates.terminal_state = "threads_resolved"
        state.loop_gates.terminal_reason = "Review threads resolved after finalization."
        return self._final_report(state, inputs)

    def _final_report(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
    ) -> ReviewResponseRunnerAction:
        if not self.finalization_allowed(state) or not state.finalization.finalized:
            return _locked_action(state, "Final report is locked until finalization and thread-resolution paths are complete.")
        state.finalization.completion_report_emitted = True
        state.phase = ReviewResponsePhase.COMPLETED.value
        state.loop_gates.terminal_state = "completed"
        state.loop_gates.terminal_reason = "Post-approval finalization, thread resolution, and final report are complete."
        report = _render_final_completion_report(inputs, state)
        state.report_snapshots.append(
            ReviewResponseCompletionReport(
                pr_number=inputs.pr_number or 1,
                unresolved_review_threads=_completion_unresolved_threads(state),
                ci_status=_completion_ci_status(state),
                working_tree="clean after finalization",
                local_verification=_completion_local_verification(state),
                completion_status="finalized",
                thread_resolution="resolved inspected threads after finalization",
            ).to_snapshot("final-completion")
        )
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.COMPLETE,
            ReviewResponsePhase.COMPLETED,
            "Post-approval finalization and thread-resolution sequence is complete.",
            report=report,
        )

    def _completed(self, state: ReviewResponseWorkflowState, inputs: ReviewResponseRunnerInputs) -> ReviewResponseRunnerAction:
        state.phase = ReviewResponsePhase.COMPLETED.value
        report = _latest_completion_report(state) or (
            _render_final_completion_report(inputs, state)
            if state.finalization.completion_report_emitted and state.finalization.finalized
            else ""
        )
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.COMPLETE,
            ReviewResponsePhase.COMPLETED,
            "Workflow is already complete.",
            report=report,
        )

    def _aborted(self, state: ReviewResponseWorkflowState, _: ReviewResponseRunnerInputs) -> ReviewResponseRunnerAction:
        state.finalization.aborted = True
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.ABORT,
            ReviewResponsePhase.ABORTED,
            "Workflow is aborted.",
        )

    def _delegate_pr_analysis(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
        reason: str,
    ) -> ReviewResponseRunnerAction:
        state.phase = ReviewResponsePhase.PR_ANALYSIS.value
        if _has_running_handle_for(state, ReviewResponsePhase.PR_ANALYSIS):
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.WAIT,
                ReviewResponsePhase.PR_ANALYSIS,
                "pr-analysis delegation is already running; wait for background completion.",
            )
        prompt = build_review_response_delegation_prompt_or_abort(
            WorkflowDelegationPromptRequest(
                phase=WorkflowDelegationPhase.PR_ANALYSIS,
                pr_url=inputs.pr_url,
                repo_path=inputs.repo_path,
            ),
            state,
            store=self.store,
        )
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.DELEGATE_PR_ANALYSIS,
            ReviewResponsePhase.PR_ANALYSIS,
            reason,
            prompt=prompt,
        )

    def _delegate_changed_review(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
        reason: str,
    ) -> ReviewResponseRunnerAction:
        state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
        if _has_running_handle_for(state, ReviewResponsePhase.CHANGED_REVIEW):
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.WAIT,
                ReviewResponsePhase.CHANGED_REVIEW,
                "changed-review delegation is already running; wait for background completion.",
            )
        prompt = build_review_response_delegation_prompt_or_abort(
            WorkflowDelegationPromptRequest(
                phase=WorkflowDelegationPhase.CHANGED_REVIEW,
                pr_url=inputs.pr_url,
                repo_path=inputs.repo_path,
            ),
            state,
            store=self.store,
        )
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW,
            ReviewResponsePhase.CHANGED_REVIEW,
            reason,
            prompt=prompt,
            metadata={
                "terminal_args": {
                    "command": _build_changed_review_terminal_command(
                        prompt,
                        repo_path=inputs.repo_path,
                    ),
                    "workflow_delegation": True,
                    "workflow_phase": ReviewResponsePhase.CHANGED_REVIEW.value,
                    "delegation_type": prompt.delegation_type,
                    "delivery_method": prompt.delivery_method,
                    "background": True,
                    "notify_on_complete": True,
                }
            },
        )

    def _delegate_refix(
        self,
        state: ReviewResponseWorkflowState,
        inputs: ReviewResponseRunnerInputs,
        reason: str,
    ) -> ReviewResponseRunnerAction:
        state.phase = ReviewResponsePhase.FIX_LOOP.value
        if _has_running_handle_for(state, ReviewResponsePhase.FIX_LOOP):
            return ReviewResponseRunnerAction(
                ReviewResponseRunnerActionType.WAIT,
                ReviewResponsePhase.FIX_LOOP,
                "re-fix delegation is already running; wait for background completion.",
            )
        refix_phase = _coerce_refix_phase(inputs.refix_phase)
        review_findings = inputs.refix_findings or _review_findings_from(inputs.changed_review)
        prompt = build_review_response_delegation_prompt_or_abort(
            WorkflowDelegationPromptRequest(
                phase=refix_phase,
                pr_url=inputs.pr_url,
                repo_path=inputs.repo_path,
                review_findings=review_findings,
                finalized_plan=inputs.finalized_plan,
                fix_scope=inputs.fix_scope or review_findings,
            ),
            state,
            store=self.store,
        )
        return ReviewResponseRunnerAction(
            ReviewResponseRunnerActionType.DELEGATE_REFIX,
            ReviewResponsePhase.FIX_LOOP,
            reason,
            prompt=prompt,
        )

    def _save(self, state: ReviewResponseWorkflowState) -> None:
        if self.store is not None:
            _ = self.store.save(state)


def _coerce_phase(value: str) -> ReviewResponsePhase:
    try:
        return ReviewResponsePhase(str(value or ReviewResponsePhase.ACTIVATED.value))
    except ValueError:
        return ReviewResponsePhase.ACTIVATED


def _coerce_refix_phase(value: WorkflowDelegationPhase | str) -> WorkflowDelegationPhase:
    try:
        phase = value if isinstance(value, WorkflowDelegationPhase) else WorkflowDelegationPhase(str(value))
    except ValueError:
        return WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM
    if phase in {
        WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM,
        WorkflowDelegationPhase.REFIX_CODE_EDIT,
        WorkflowDelegationPhase.REFIX_CODE_ANALYSIS,
    }:
        return phase
    return WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM


def _review_findings_from(result: ChangedReviewResult | None) -> str:
    if result is None:
        return "Independent changed-review reported unresolved findings."
    if result.review_findings.strip():
        return result.review_findings.strip()
    if result.blocking_findings:
        return "\n".join(f"- {item}" for item in result.blocking_findings)
    if result.made_changes:
        return "changed-review modified files; run a follow-up fix/review cycle."
    if result.requires_human_confirmation:
        return "changed-review requires human confirmation before finalization."
    return "Independent changed-review did not produce a clean finalization recommendation."


def _apply_changed_review_loop_policy(
    state: ReviewResponseWorkflowState,
    result: ChangedReviewResult,
) -> str:
    """Apply deterministic loop-continuation gates for one verification pass.

    Returns one of ``needs_verification``, ``needs_fix``, or ``ready``.
    """

    gates = state.loop_gates
    signature = _changed_review_signature(result)
    no_progress = bool(signature and signature == gates.latest_verification_signature)

    gates.latest_verification_signature = signature
    gates.latest_verification_had_edits = bool(result.made_changes)
    gates.latest_verification_had_new_issues = bool(result.blocking_findings)
    gates.consecutive_no_edit_passes = gates.consecutive_no_edit_passes + 1 if not result.made_changes else 0
    gates.consecutive_no_progress_passes = gates.consecutive_no_progress_passes + 1 if no_progress else 0

    if result.made_changes:
        gates.loop_count += 1
        gates.requires_fix_pass = False
        gates.requires_verification_pass = True
        gates.approval_decision = "pending"
        gates.terminal_state = "verification_modified"
        gates.terminal_reason = "Verification made edits and is treated as a modifying pass."
        return "needs_verification"

    if result.blocking_findings or result.requires_human_confirmation or not result.succeeded or not result.completed:
        gates.loop_count += 1
        gates.requires_fix_pass = True
        gates.requires_verification_pass = False
        gates.approval_decision = "denied"
        gates.deny_reason = _review_findings_from(result)
        gates.terminal_state = "fix_required"
        gates.terminal_reason = "Verification reported blocking findings or incomplete status."
        return "needs_fix"

    if result.clean_recommendation or (gates.consecutive_no_edit_passes >= 2 and not result.blocking_findings):
        gates.requires_fix_pass = False
        gates.requires_verification_pass = False
        gates.approval_decision = "pending"
        return "ready"

    gates.loop_count += 1
    gates.requires_fix_pass = True
    gates.requires_verification_pass = False
    gates.approval_decision = "denied"
    gates.deny_reason = "changed-review did not provide a clean finalization recommendation."
    gates.terminal_state = "fix_required"
    gates.terminal_reason = "Verification lacks a clean recommendation and has not met no-edit convergence."
    return "needs_fix"


def _changed_review_signature(result: ChangedReviewResult) -> str:
    parts = [
        "blocking:" + "|".join(sorted(item.strip() for item in result.blocking_findings if item.strip())),
        "watch:" + "|".join(sorted(item.strip() for item in result.non_blocking_watch_items if item.strip())),
        "findings:" + result.review_findings.strip(),
        f"succeeded:{result.succeeded}",
        f"clean:{result.clean_recommendation}",
        f"human:{result.requires_human_confirmation}",
    ]
    return "\n".join(parts)


def _locked_action(state: ReviewResponseWorkflowState, reason: str) -> ReviewResponseRunnerAction:
    state.approvals.ready_for_final_report = False
    state.loop_gates.terminal_state = "finalization_locked"
    state.loop_gates.terminal_reason = reason
    state.violations.append(
        ViolationRecord(
            code="workflow_finalization_gate_locked",
            message=reason,
            phase=str(state.phase or ""),
            details={"last_delegated_step": state.last_delegated_step},
        )
    )
    return ReviewResponseRunnerAction(
        ReviewResponseRunnerActionType.FINALIZATION_LOCKED,
        _coerce_phase(state.phase),
        reason,
    )


def _structured_pr_analysis_completed(state: ReviewResponseWorkflowState) -> bool:
    payload = state.structured_results.get(ReviewResponsePhase.PR_ANALYSIS.value)
    if not isinstance(payload, dict):
        return False
    return payload.get("completed") is True


def _has_running_handle_for(state: ReviewResponseWorkflowState, phase: ReviewResponsePhase) -> bool:
    for handle in state.background_handles:
        if handle.status != "running":
            continue
        if str(handle.metadata.get("workflow_phase") or "") == phase.value:
            return True
    return False


def _render_clean_report(inputs: ReviewResponseRunnerInputs, check: ActionableStateCheck) -> str:
    return render_clean_pr_early_exit_report(
        ReviewResponseCompletionReport(
            pr_number=inputs.pr_number or 1,
            unresolved_review_threads=check.unresolved_review_threads,
            ci_status=check.ci_status,
            working_tree="clean (no changes made)",
            local_verification=check.local_verification,
            completion_status="no action required",
        )
    )


def _render_pr_analysis_clean_report(inputs: ReviewResponseRunnerInputs, result: PRAnalysisResult) -> str:
    return render_clean_pr_early_exit_report(
        ReviewResponseCompletionReport(
            pr_number=inputs.pr_number or 1,
            unresolved_review_threads=result.unresolved_review_threads or 0,
            ci_status=result.ci_status,
            working_tree="clean (no changes made)",
            local_verification=result.local_verification,
            completion_status="no action required",
        )
    )


def _render_final_completion_report(
    inputs: ReviewResponseRunnerInputs,
    state: ReviewResponseWorkflowState,
) -> str:
    return render_completion_report(
        ReviewResponseCompletionReport(
            pr_number=inputs.pr_number or 1,
            unresolved_review_threads=_completion_unresolved_threads(state),
            ci_status=_completion_ci_status(state),
            working_tree="clean after finalization",
            local_verification=_completion_local_verification(state),
            completion_status="finalized",
            thread_resolution="resolved inspected threads after finalization",
        )
    )


def _latest_completion_report(state: ReviewResponseWorkflowState) -> str:
    for snapshot in reversed(state.report_snapshots):
        data = snapshot.data
        rendered = data.get("report")
        if isinstance(rendered, str) and rendered.strip():
            return rendered
        if data.get("report_type") == "review_response.completion_report":
            try:
                return render_completion_report(
                    ReviewResponseCompletionReport(
                        pr_number=int(data.get("pr_number") or 1),
                        unresolved_review_threads=int(data.get("unresolved_review_threads") or 0),
                        ci_status=str(data.get("ci_status") or "not reported"),
                        working_tree=str(data.get("working_tree") or "clean after finalization"),
                        local_verification=str(data.get("local_verification") or "clean changed-review completed"),
                        completion_status=str(data.get("completion_status") or "finalized"),
                        thread_resolution=str(data.get("thread_resolution") or "resolved inspected threads after finalization"),
                    )
                )
            except (TypeError, ValueError):
                continue
    return ""


def _completion_unresolved_threads(state: ReviewResponseWorkflowState) -> int:
    thread_payload = state.structured_results.get(ReviewResponsePhase.THREAD_RESOLUTION.value, {})
    if isinstance(thread_payload, dict):
        if thread_payload.get("threads_resolved") is True:
            return 0
        raw_remaining = thread_payload.get("remaining_unresolved")
        try:
            if raw_remaining is not None:
                return max(0, int(raw_remaining))
        except (TypeError, ValueError):
            pass

    pr_payload = state.structured_results.get(ReviewResponsePhase.PR_ANALYSIS.value, {})
    raw_value = pr_payload.get("unresolved_review_threads", 0)
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return 0


def _completion_ci_status(state: ReviewResponseWorkflowState) -> str:
    pr_payload = state.structured_results.get(ReviewResponsePhase.PR_ANALYSIS.value, {})
    ci_status = str(pr_payload.get("ci_status") or "").strip()
    if ci_status:
        return ci_status
    return "not reported"


def _completion_local_verification(state: ReviewResponseWorkflowState) -> str:
    changed_payload = state.structured_results.get(ReviewResponsePhase.CHANGED_REVIEW.value, {})
    review_findings = str(changed_payload.get("review_findings") or "").strip()
    if review_findings:
        return review_findings
    pr_payload = state.structured_results.get(ReviewResponsePhase.PR_ANALYSIS.value, {})
    verification = str(pr_payload.get("local_verification") or "").strip()
    if verification:
        return verification
    return "clean changed-review completed"


def _build_inline_omx_terminal_command(prompt: InlineDelegationPrompt, phase: ReviewResponsePhase) -> str:
    project = _shell_safe_token("review-response")
    task = _shell_safe_token(f"{phase.value}-{prompt.delegation_type}")
    prompt_b64 = base64.b64encode(prompt.body.encode("utf-8")).decode("ascii")
    runner_script = (
        "import base64, os, subprocess, sys\n"
        "log_path = os.environ['LOGFILE']\n"
        "prompt = base64.b64decode(os.environ['PROMPT_B64']).decode('utf-8')\n"
        "with open(log_path, 'w', encoding='utf-8') as log:\n"
        "    proc = subprocess.run(['omx', '--madmax', '--high', 'exec', prompt], stdout=log, stderr=subprocess.STDOUT)\n"
        "    log.write('\\n=== OMX DONE ===\\n')\n"
        "sys.exit(proc.returncode)\n"
    )
    runner_b64 = base64.b64encode(runner_script.encode("utf-8")).decode("ascii")
    return (
        f"PROJECT_NAME={shlex.quote(project)}; "
        f"TASK_ID={shlex.quote(task)}; "
        f"PROMPT_B64={shlex.quote(prompt_b64)}; "
        f"RUNNER_B64={shlex.quote(runner_b64)}; "
        "TS=$(date +%s); RAND=$$; "
        'LOGFILE="/tmp/omx-${PROJECT_NAME}-${TASK_ID}-${TS}-${RAND}.log"; '
        'TMUX_SESSION="omx-${PROJECT_NAME}-${TASK_ID}-${TS}-${RAND}"; '
        "export LOGFILE PROMPT_B64 RUNNER_B64; "
        "tmux new-session -d -s \"$TMUX_SESSION\" "
        "\"python3 -c \\\"import base64, os; exec(base64.b64decode(os.environ['RUNNER_B64']))\\\"\"; "
        'while tmux has-session -t "$TMUX_SESSION" 2>/dev/null; do sleep 5; done; '
        'cat "$LOGFILE"'
    )


def _build_changed_review_terminal_command(
    prompt: InlineDelegationPrompt,
    *,
    repo_path: str,
) -> str:
    project = _shell_safe_token("review-response")
    task = _shell_safe_token(f"{ReviewResponsePhase.CHANGED_REVIEW.value}-{prompt.delegation_type}")
    prompt_b64 = base64.b64encode(prompt.body.encode("utf-8")).decode("ascii")
    repo_b64 = base64.b64encode(repo_path.encode("utf-8")).decode("ascii")
    runner_script = (
        "import base64, json, os, shutil, subprocess, sys, tempfile\n"
        "log_path = os.environ['LOGFILE']\n"
        "prompt = base64.b64decode(os.environ['PROMPT_B64']).decode('utf-8')\n"
        "source_repo = base64.b64decode(os.environ['REPO_B64']).decode('utf-8')\n"
        "sandbox_root = tempfile.mkdtemp(prefix='review-response-changed-review-')\n"
        "sandbox_repo = os.path.join(sandbox_root, 'repo')\n"
        "mutation_message = 'changed-review attempted to modify files in the review sandbox; review must stay read-only and hand findings to the separate re-fix phase.'\n"
        "structured_result = ''\n"
        "exit_code = 1\n"
        "output_text = ''\n"
        "def _git_status(path):\n"
        "    result = subprocess.run(['git', 'status', '--porcelain'], cwd=path, text=True, capture_output=True)\n"
        "    return result.stdout if result.returncode == 0 else ''\n"
        "copy_ignore = shutil.ignore_patterns('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'htmlcov', '.coverage')\n"
        "try:\n"
        "    shutil.copytree(source_repo, sandbox_repo, symlinks=True, ignore=copy_ignore)\n"
        "    prompt = prompt.replace(source_repo, sandbox_repo)\n"
        "    before_status = _git_status(sandbox_repo)\n"
        "    with open(log_path, 'w', encoding='utf-8') as log:\n"
        "        proc = subprocess.run(['omx', '--madmax', '--high', 'exec', prompt], cwd=sandbox_repo, stdout=log, stderr=subprocess.STDOUT)\n"
        "        after_status = _git_status(sandbox_repo)\n"
        "        if after_status != before_status:\n"
        "            structured_result = 'STRUCTURED_RESULT=' + json.dumps({\n"
        "                'workflow_phase': 'changed_review',\n"
        "                'delegation_type': 'changed-review',\n"
        "                'completed': True,\n"
        "                'succeeded': False,\n"
        "                'blocking_findings': [mutation_message],\n"
        "                'made_changes': False,\n"
        "                'requires_human_confirmation': False,\n"
        "                'clean_recommendation': False,\n"
        "                'review_findings': mutation_message,\n"
        "            })\n"
        "            log.write('\\n' + structured_result + '\\n')\n"
        "        log.write('\\n=== OMX DONE ===\\n')\n"
        "    output_text = open(log_path, 'r', encoding='utf-8').read()\n"
        "    exit_code = 0 if proc.returncode == 0 else proc.returncode\n"
        "finally:\n"
        "    shutil.rmtree(sandbox_root, ignore_errors=True)\n"
        "sys.stdout.write(output_text)\n"
        "sys.exit(exit_code)\n"
    )
    runner_b64 = base64.b64encode(runner_script.encode("utf-8")).decode("ascii")
    return (
        f"PROJECT_NAME={shlex.quote(project)}; "
        f"TASK_ID={shlex.quote(task)}; "
        f"PROMPT_B64={shlex.quote(prompt_b64)}; "
        f"REPO_B64={shlex.quote(repo_b64)}; "
        f"RUNNER_B64={shlex.quote(runner_b64)}; "
        "TS=$(date +%s); RAND=$$; "
        'LOGFILE="/tmp/omx-${PROJECT_NAME}-${TASK_ID}-${TS}-${RAND}.log"; '
        "export LOGFILE PROMPT_B64 REPO_B64 RUNNER_B64; "
        "python3 -c \"import base64, os; exec(base64.b64decode(os.environ['RUNNER_B64']))\""
    )


def _duplicate_session_guard_terminal_args() -> dict[str, object]:
    script = r'''
import json, subprocess

def run(command):
    try:
        return subprocess.run(command, shell=True, text=True, capture_output=True, timeout=5)
    except Exception as exc:
        return type("Result", (), {"stdout": "", "stderr": str(exc), "returncode": 1})()

tmux = run("tmux list-sessions 2>/dev/null || true")
ps = run("ps aux 2>/dev/null || true")
review_markers = ("review", "gh pr", "git diff", "codex", "omx")
active_lines = []
for raw in (tmux.stdout + "\n" + ps.stdout).splitlines():
    lower = raw.lower()
    if any(marker in lower for marker in review_markers):
        active_lines.append(raw[:240])
print("WORKFLOW_GUARD_RESULT=" + json.dumps({
    "guard": "duplicate_session",
    "duplicate_session_active": bool(active_lines),
    "matches": active_lines[:10],
}))
'''
    return _foreground_python_guard_args(script, ReviewResponsePhase.DUPLICATE_SESSION_CHECK)


def _actionable_state_guard_terminal_args(inputs: ReviewResponseRunnerInputs) -> dict[str, object]:
    script = r'''
import json, re, subprocess, sys

pr_url = sys.argv[1]
match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
if not match:
    print("WORKFLOW_GUARD_RESULT=" + json.dumps({
        "guard": "actionable_state",
        "unresolved_review_threads": 1,
        "failing_ci": False,
        "other_actionable_state": True,
        "ci_status": "PR URL not parseable; continuing to delegated pr-analysis",
        "local_verification": "not run",
    }))
    raise SystemExit(0)

owner, repo, number = match.groups()

def run(command):
    try:
        return subprocess.run(command, shell=True, text=True, capture_output=True, timeout=20)
    except Exception as exc:
        return type("Result", (), {"stdout": "", "stderr": str(exc), "returncode": 1})()

threads_query = f"""
query {{
  repository(owner: "{owner}", name: "{repo}") {{
    pullRequest(number: {number}) {{
      reviewThreads(first: 100) {{ nodes {{ isResolved }} }}
    }}
  }}
}}"""
threads = run("gh api graphql -f query=" + repr(threads_query))
overview = run(f"gh pr view {number} --repo {owner}/{repo} --json reviewDecision,statusCheckRollup 2>/dev/null")
unresolved = 1
if threads.returncode == 0 and threads.stdout.strip():
    try:
        payload = json.loads(threads.stdout)
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        unresolved = sum(1 for node in nodes if not node.get("isResolved"))
    except Exception:
        unresolved = 1
failing_ci = "FAILURE" in overview.stdout or "ERROR" in overview.stdout
print("WORKFLOW_GUARD_RESULT=" + json.dumps({
    "guard": "actionable_state",
    "unresolved_review_threads": unresolved,
    "failing_ci": failing_ci,
    "other_actionable_state": False,
    "ci_status": "failing" if failing_ci else "not failing or unavailable",
    "local_verification": "pre-delegation guard only",
}))
'''
    return _foreground_python_guard_args(
        script,
        ReviewResponsePhase.ACTIONABLE_STATE_CHECK,
        argv=[inputs.pr_url],
    )


def _finalization_terminal_args(_inputs: ReviewResponseRunnerInputs) -> dict[str, object]:
    script = r'''
import json, subprocess

def run(*command):
    return subprocess.run(command, text=True, capture_output=True)

status_before = run("git", "status", "--porcelain")
staged = run("git", "diff", "--cached", "--name-only")
unstaged = run("git", "diff", "--name-only")
staged_files = [line for line in staged.stdout.splitlines() if line.strip()]
unstaged_files = [line for line in unstaged.stdout.splitlines() if line.strip()]
untracked_files = [line[3:] for line in status_before.stdout.splitlines() if line.startswith("?? ")]
candidate_files = sorted(staged_files)
commit_attempted = False
commit_returncode = 0
push_returncode = 0
blocked_unstaged = bool(unstaged_files or untracked_files)
if candidate_files:
    commit_attempted = True
    commit_result = run("git", "commit", "-m", "fix: address PR review feedback")
    commit_returncode = commit_result.returncode
    if commit_returncode == 0:
        push_returncode = run("git", "push").returncode
status_after = run("git", "status", "--porcelain")
print("WORKFLOW_FINALIZATION_RESULT=" + json.dumps({
    "guard": "finalization",
    "finalized": (not status_after.stdout.strip()) and commit_returncode == 0 and push_returncode == 0,
    "commit_attempted": commit_attempted,
    "working_tree_clean": not bool(status_after.stdout.strip()),
    "commit_returncode": commit_returncode,
    "push_returncode": push_returncode,
    "candidate_files": candidate_files,
    "staged_files": staged_files,
    "unstaged_files": unstaged_files,
    "untracked_files": untracked_files,
    "blocked_unstaged": blocked_unstaged,
}))
'''
    command = "python - <<'PY'\n" + script.strip() + "\nPY"
    return {
        "command": command,
        "workflow_delegation": True,
        "workflow_phase": ReviewResponsePhase.FINALIZATION.value,
        "delegation_type": "workflow-finalize",
        "delivery_method": "inline",
        "background": False,
        "notify_on_complete": False,
    }


def _thread_resolution_terminal_args(inputs: ReviewResponseRunnerInputs) -> dict[str, object]:
    script = r'''
import json, re, subprocess, sys

pr_url = sys.argv[1]
match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
if not match:
    print("WORKFLOW_THREAD_RESOLUTION_RESULT=" + json.dumps({
        "guard": "thread_resolution",
        "threads_resolved": True,
        "resolved_count": 0,
        "note": "PR URL unavailable; no thread IDs resolved by runtime harness",
    }))
    raise SystemExit(0)

owner, repo, number = match.groups()
query = f"""
query {{
  repository(owner: "{owner}", name: "{repo}") {{
    pullRequest(number: {number}) {{
      reviewThreads(first: 100) {{ nodes {{ id isResolved }} }}
    }}
  }}
}}"""
threads = subprocess.run("gh api graphql -f query=" + repr(query), shell=True, text=True, capture_output=True)
resolved = 0
remaining = 0
if threads.returncode == 0 and threads.stdout.strip():
    try:
        payload = json.loads(threads.stdout)
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        unresolved = [node["id"] for node in nodes if not node.get("isResolved")]
        remaining = len(unresolved)
        for thread_id in unresolved:
            mutation = 'mutation($thread:ID!){resolveReviewThread(input:{threadId:$thread}){thread{id isResolved}}}'
            result = subprocess.run(
                "gh api graphql -f query=" + repr(mutation) + " -F thread=" + repr(thread_id),
                shell=True,
                text=True,
                capture_output=True,
            )
            if result.returncode == 0:
                resolved += 1
        remaining = max(0, remaining - resolved)
    except Exception:
        remaining = 0
print("WORKFLOW_THREAD_RESOLUTION_RESULT=" + json.dumps({
    "guard": "thread_resolution",
    "threads_resolved": remaining == 0,
    "resolved_count": resolved,
    "remaining_unresolved": remaining,
}))
'''
    return _foreground_python_guard_args(
        script,
        ReviewResponsePhase.THREAD_RESOLUTION,
        delegation_type="workflow-thread-resolution",
        argv=[inputs.pr_url],
    )


def _foreground_python_guard_args(
    script: str,
    phase: ReviewResponsePhase,
    *,
    argv: list[str] | None = None,
    delegation_type: str = "workflow-guard",
) -> dict[str, object]:
    encoded_script = base64.b64encode(script.encode("utf-8")).decode("ascii")
    encoded_argv = base64.b64encode(json_dumps_list(argv or []).encode("utf-8")).decode("ascii")
    command = (
        f"SCRIPT_B64={shlex.quote(encoded_script)}; "
        f"ARGV_B64={shlex.quote(encoded_argv)}; "
        "python - <<'PY'\n"
        "import base64, json, sys\n"
        "script = base64.b64decode(__import__('os').environ['SCRIPT_B64']).decode('utf-8')\n"
        "sys.argv = ['workflow-guard'] + json.loads(base64.b64decode(__import__('os').environ['ARGV_B64']).decode('utf-8'))\n"
        "exec(compile(script, '<workflow-guard>', 'exec'))\n"
        "PY"
    )
    return {
        "command": command,
        "workflow_delegation": True,
        "workflow_phase": phase.value,
        "delegation_type": delegation_type,
        "delivery_method": "inline",
        "background": False,
        "notify_on_complete": False,
    }


def json_dumps_list(values: list[str]) -> str:
    import json

    return json.dumps([str(value) for value in values])


def _shell_safe_token(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.lower())
    return text.strip("-") or "workflow"
