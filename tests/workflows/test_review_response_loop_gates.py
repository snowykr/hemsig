from importlib import import_module

from agent.workflows import ReviewResponsePhase, ReviewResponseWorkflowStateStore

_runner_module = import_module("agent.workflows.review_response_phase_runner")
ChangedReviewResult = _runner_module.ChangedReviewResult
ReviewResponsePhaseRunner = _runner_module.ReviewResponsePhaseRunner
ReviewResponseRunnerActionType = _runner_module.ReviewResponseRunnerActionType
ReviewResponseRunnerInputs = _runner_module.ReviewResponseRunnerInputs


def _base_inputs(**overrides):
    data = {
        "pr_url": "https://github.com/example/repo/pull/42",
        "repo_path": "/tmp/repo",
        "pr_number": 42,
    }
    data.update(overrides)
    return ReviewResponseRunnerInputs(**data)


def test_fix_verify_loop_repeats_until_gate() -> None:
    state = ReviewResponseWorkflowStateStore().default_state("loop-repeats-until-gate")
    state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
    runner = ReviewResponsePhaseRunner()

    action = runner.next_action(
        state,
        _base_inputs(
            changed_review=ChangedReviewResult(
                completed=True,
                succeeded=True,
                blocking_findings=("missing regression test",),
                clean_recommendation=False,
                review_findings="missing regression test",
            )
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_REFIX
    assert state.phase == ReviewResponsePhase.FIX_LOOP.value
    assert state.loop_gates.requires_fix_pass is True
    assert state.loop_gates.requires_verification_pass is False
    assert state.loop_gates.approval_decision == "denied"
    assert state.approvals.ready_for_final_report is False
    assert runner.finalization_allowed(state) is False

    state.phase = ReviewResponsePhase.VERIFICATION.value
    state.loop_gates.requires_fix_pass = False
    action = runner.next_action(state, _base_inputs())
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW
    assert state.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert state.loop_gates.requires_verification_pass is True
    assert runner.finalization_allowed(state) is False

    action = runner.next_action(
        state,
        _base_inputs(
            changed_review=ChangedReviewResult(
                completed=True,
                succeeded=True,
                blocking_findings=("edge case still fails",),
                clean_recommendation=False,
                review_findings="edge case still fails",
            )
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_REFIX
    assert state.phase == ReviewResponsePhase.FIX_LOOP.value
    assert state.loop_gates.loop_count >= 2
    assert state.approvals.ready_for_final_report is False
    assert runner.finalization_allowed(state) is False

    state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
    action = runner.next_action(
        state,
        _base_inputs(
            changed_review=ChangedReviewResult(
                completed=True,
                succeeded=True,
                clean_recommendation=True,
                review_findings="clean",
            )
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.FINALIZATION_LOCKED
    assert state.phase == ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    assert state.loop_gates.requires_fix_pass is False
    assert state.loop_gates.requires_verification_pass is False
    assert state.loop_gates.terminal_state == "approval_gate_ready"
    assert state.approvals.ready_for_final_report is True
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
    }
    assert runner.finalization_allowed(state) is True


def test_verification_edit_resets_loop() -> None:
    state = ReviewResponseWorkflowStateStore().default_state("verification-edit-resets-loop")
    state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
    runner = ReviewResponsePhaseRunner()

    action = runner.next_action(
        state,
        _base_inputs(
            changed_review=ChangedReviewResult(
                completed=True,
                succeeded=True,
                made_changes=True,
                clean_recommendation=True,
                review_findings="added a missing regression test while reviewing",
            )
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW
    assert action.prompt is not None
    assert action.prompt.delegation_type == "changed-review"
    assert state.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert state.loop_gates.latest_verification_had_edits is True
    assert state.loop_gates.requires_verification_pass is True
    assert state.loop_gates.requires_fix_pass is False
    assert state.loop_gates.terminal_state == "verification_modified"
    assert state.approvals.ready_for_final_report is False
    assert runner.finalization_allowed(state) is False

    action = runner.next_action(
        state,
        _base_inputs(
            changed_review=ChangedReviewResult(
                completed=True,
                succeeded=True,
                clean_recommendation=True,
                review_findings="clean after self-modifying review follow-up",
            )
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.FINALIZATION_LOCKED
    assert state.loop_gates.requires_verification_pass is False
    assert state.loop_gates.consecutive_no_edit_passes == 1
    assert state.loop_gates.terminal_state == "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
    }
    assert runner.finalization_allowed(state) is True


def test_finalization_gate_allows_thread_resolution_workflow_when_threads_start_unresolved() -> None:
    state = ReviewResponseWorkflowStateStore().default_state("approval-gate-allows-unresolved-pre-finalization")
    state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    state.loop_counters["changed_review"] = 1
    state.approvals.ready_for_final_report = True
    state.loop_gates.approval_decision = "pending"
    state.loop_gates.terminal_state = "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 3,
    }

    assert ReviewResponsePhaseRunner().finalization_allowed(state) is True
