import json
from types import SimpleNamespace

from pytest import MonkeyPatch


def _workflow_agent(session_id: str):
    from agent.workflows import ReviewResponseWorkflowStateStore, activation_for_skill
    from run_agent import AIAgent

    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None

    agent = object.__new__(AIAgent)
    agent.session_id = session_id
    agent.workflow_context = None
    agent.workflow_state = None
    agent._workflow_state_store = ReviewResponseWorkflowStateStore()
    agent.activate_workflow(activation)
    return agent


def test_workflow_state_persists_and_reloads(monkeypatch: MonkeyPatch) -> None:
    from agent.workflows import (
        BackgroundHandle,
        ReportSnapshot,
        ReviewResponsePhase,
        ReviewResponseWorkflowStateStore,
        ViolationRecord,
        activation_for_skill,
    )
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent

    session_id = "review-session-1"
    store = ReviewResponseWorkflowStateStore()
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None

    first_agent = object.__new__(AIAgent)
    first_agent.session_id = session_id
    first_agent.workflow_context = None
    first_agent.workflow_state = None
    first_agent._workflow_state_store = store
    first_agent.activate_workflow(activation)

    assert first_agent.workflow_state is not None
    first_agent.workflow_state.phase = ReviewResponsePhase.FIX_LOOP.value
    first_agent.workflow_state.loop_counters["fix_verify"] = 2
    first_agent.workflow_state.delegated_task_ids.append("delegate-task-1")
    first_agent.workflow_state.last_delegated_step = "changed-review"
    first_agent.workflow_state.background_handles.append(
        BackgroundHandle(handle_id="proc_1", kind="terminal", status="running", task_id="task-1")
    )
    first_agent.workflow_state.report_snapshots.append(
        ReportSnapshot(report_id="phase-report-1", phase="changed_review", data={"clean": False})
    )
    first_agent.workflow_state.approvals.ready_for_final_report = True
    first_agent.workflow_state.finalization.finalized = False
    first_agent.workflow_state.violations.append(
        ViolationRecord(code="foreground_delegate", message="Delegation must run in background")
    )
    first_agent._persist_workflow_state()

    state_path = get_hermes_home() / "workflow_state" / "review_response" / f"{session_id}.json"
    assert state_path.exists()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["phase"] == "fix_loop"
    assert persisted["loop_counters"] == {"fix_verify": 2}
    assert persisted["delegated_task_ids"] == ["delegate-task-1"]
    assert persisted["background_handles"][0]["handle_id"] == "proc_1"
    assert persisted["report_snapshots"][0]["report_id"] == "phase-report-1"
    assert persisted["approvals"]["ready_for_final_report"] is True
    assert persisted["finalization"]["finalized"] is False
    assert persisted["violations"][0]["code"] == "foreground_delegate"

    second_agent = object.__new__(AIAgent)
    second_agent.session_id = session_id
    second_agent.workflow_context = None
    second_agent.workflow_state = None
    second_agent._workflow_state_store = ReviewResponseWorkflowStateStore()
    second_agent.activate_workflow(activation)

    assert second_agent.workflow_state is not None
    assert second_agent.workflow_state.phase == ReviewResponsePhase.FIX_LOOP.value
    assert second_agent.workflow_state.loop_counters == {"fix_verify": 2}
    assert second_agent.workflow_state.delegated_task_ids == ["delegate-task-1"]
    assert second_agent.workflow_state.last_delegated_step == "changed-review"
    assert second_agent.workflow_state.background_handles[0].handle_id == "proc_1"
    assert second_agent.workflow_state.report_snapshots[0].data == {"clean": False}
    assert second_agent.workflow_state.approvals.ready_for_final_report is True
    assert second_agent.workflow_state.violations[0].code == "foreground_delegate"

    other_session = "review-session-2"
    monkeypatch.setattr(first_agent, "session_id", other_session)
    first_agent.activate_workflow(activation)
    assert first_agent.workflow_state is not None
    assert first_agent.workflow_state.session_id == other_session
    assert first_agent.workflow_state.loop_counters == {}


def test_background_completion_advances_phase() -> None:
    from agent.workflows import ReviewResponsePhase, ReviewResponseWorkflowStateStore, activation_for_skill
    from run_agent import AIAgent

    session_id = "review-background-advance"
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None

    agent = object.__new__(AIAgent)
    agent.session_id = session_id
    agent.workflow_context = None
    agent.workflow_state = None
    agent._workflow_state_store = ReviewResponseWorkflowStateStore()
    agent.activate_workflow(activation)
    assert agent.workflow_state is not None

    agent.workflow_state.phase = ReviewResponsePhase.PR_ANALYSIS.value
    agent._persist_workflow_state()

    agent._record_workflow_tool_result(
        "terminal",
        {
            "command": "hermes delegated review",
            "workflow_phase": ReviewResponsePhase.PR_ANALYSIS.value,
            "delegation_type": "pr-analysis",
            "notify_on_complete": True,
        },
        json.dumps({"session_id": "proc_review_1", "output": "Background process started"}),
        "task-review-1",
    )

    stored = agent._workflow_state_store.load(session_id)
    assert stored.background_handles[0].handle_id == "proc_review_1"
    assert stored.background_handles[0].status == "running"
    assert stored.phase == ReviewResponsePhase.PR_ANALYSIS.value

    completion = (
        "[IMPORTANT: Background process proc_review_1 completed (exit code 0).\n"
        "Command: hermes delegated review\n"
        "Output:\nreview complete]"
    )
    agent._ingest_workflow_background_completion_message(completion)

    reloaded = agent._workflow_state_store.load(session_id)
    assert reloaded.background_handles[0].status == "completed"
    assert reloaded.background_handles[0].metadata["completion"]["source"] == "watcher_message"
    assert reloaded.phase == ReviewResponsePhase.CHANGED_REVIEW.value


def test_structured_clean_changed_review_completion_reaches_approval_gate() -> None:
    from agent.workflows import BackgroundHandle, ReviewResponsePhase, ReviewResponseWorkflowStateStore

    agent = _workflow_agent("review-structured-clean-changed")
    assert agent.workflow_state is not None
    agent.workflow_state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
    agent.workflow_state.background_handles.append(
        BackgroundHandle(
            handle_id="proc_clean_changed_review",
            kind="terminal",
            status="running",
            metadata={
                "workflow_phase": ReviewResponsePhase.CHANGED_REVIEW.value,
                "delegation_type": "changed-review",
                "next_phase": ReviewResponsePhase.FIX_LOOP.value,
            },
        )
    )
    agent._persist_workflow_state()

    output = json.dumps(
        {
            "workflow_phase": "changed_review",
            "delegation_type": "changed-review",
            "completed": True,
            "succeeded": True,
            "blocking_findings": [],
            "made_changes": False,
            "requires_human_confirmation": False,
            "clean_recommendation": True,
            "review_findings": "clean",
        }
    )
    agent._ingest_workflow_background_completion_message(
        "[IMPORTANT: Background process proc_clean_changed_review completed (exit code 0).\n"
        f"Command: hermes delegated changed-review\nOutput:\n{output}]"
    )

    reloaded = ReviewResponseWorkflowStateStore().load("review-structured-clean-changed")
    assert reloaded.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert reloaded.structured_results[ReviewResponsePhase.CHANGED_REVIEW.value]["clean_recommendation"] is True
    assert reloaded.background_handles[0].metadata["structured_result"]["result_kind"] == "changed_review"

    agent.workflow_state = reloaded
    action = agent.next_review_response_workflow_action(
        agent._build_review_response_runner_inputs("https://github.com/example/repo/pull/42")
    )
    assert action is not None
    assert action.action_type.value == "finalization_locked"
    assert agent.workflow_state.phase == ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    assert agent.workflow_state.approvals.ready_for_final_report is True


def test_restart_reconciles_completed_background_handle(monkeypatch: MonkeyPatch) -> None:
    from agent.workflows import (
        BackgroundHandle,
        ReviewResponsePhase,
        ReviewResponseWorkflowStateStore,
        activation_for_skill,
    )
    from run_agent import AIAgent
    from tools.process_registry import process_registry

    session_id = "review-background-restart"
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None

    store = ReviewResponseWorkflowStateStore()
    state = store.default_state(session_id, workflow_id="review_response")
    state.phase = ReviewResponsePhase.PR_ANALYSIS.value
    state.background_handles.append(
        BackgroundHandle(
            handle_id="proc_finished_after_restart",
            kind="terminal",
            status="running",
            task_id="task-review-2",
            metadata={
                "workflow_phase": ReviewResponsePhase.PR_ANALYSIS.value,
                "next_phase": ReviewResponsePhase.CHANGED_REVIEW.value,
            },
        )
    )
    store.save(state)

    monkeypatch.setattr(
        process_registry,
        "get",
        lambda session_id: SimpleNamespace(
            id=session_id,
            exited=True,
            exit_code=0,
            output_buffer="finished while Hermes was restarting",
            command="hermes delegated review",
        ),
    )

    restarted_agent = object.__new__(AIAgent)
    restarted_agent.session_id = session_id
    restarted_agent.workflow_context = None
    restarted_agent.workflow_state = None
    restarted_agent._workflow_state_store = store
    restarted_agent.activate_workflow(activation)

    assert restarted_agent.workflow_state is not None
    assert restarted_agent.workflow_state.background_handles[0].status == "completed"
    assert restarted_agent.workflow_state.phase == ReviewResponsePhase.CHANGED_REVIEW.value

    reloaded = store.load(session_id)
    assert reloaded.background_handles[0].metadata["completion"]["source"] == "process_registry_reconcile"
    assert reloaded.phase == ReviewResponsePhase.CHANGED_REVIEW.value


def test_structured_completion_survives_restart_reconcile(monkeypatch: MonkeyPatch) -> None:
    from agent.workflows import (
        BackgroundHandle,
        ReviewResponsePhase,
        ReviewResponseWorkflowStateStore,
        activation_for_skill,
    )
    from run_agent import AIAgent
    from tools.process_registry import process_registry

    session_id = "review-structured-restart"
    store = ReviewResponseWorkflowStateStore()
    state = store.default_state(session_id, workflow_id="review_response")
    state.phase = ReviewResponsePhase.PR_ANALYSIS.value
    state.background_handles.append(
        BackgroundHandle(
            handle_id="proc_clean_pr_after_restart",
            kind="terminal",
            status="running",
            metadata={
                "workflow_phase": ReviewResponsePhase.PR_ANALYSIS.value,
                "delegation_type": "pr-analysis",
                "next_phase": ReviewResponsePhase.CHANGED_REVIEW.value,
            },
        )
    )
    store.save(state)

    structured_output = json.dumps(
        {
            "workflow_phase": "pr_analysis",
            "delegation_type": "pr-analysis",
            "completed": True,
            "unresolved_review_threads": 0,
            "failing_ci": False,
            "modifications_made": False,
            "working_tree_clean": True,
            "root_cause_no_valid_issues": True,
            "ci_status": "passing",
            "local_verification": "not needed",
        }
    )
    monkeypatch.setattr(
        process_registry,
        "get",
        lambda session_id: SimpleNamespace(
            id=session_id,
            exited=True,
            exit_code=0,
            output_buffer=structured_output,
            command="hermes delegated pr-analysis",
        ),
    )

    restarted_agent = object.__new__(AIAgent)
    restarted_agent.session_id = session_id
    restarted_agent.workflow_context = None
    restarted_agent.workflow_state = None
    restarted_agent._workflow_state_store = store
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None
    restarted_agent.activate_workflow(activation)

    persisted = store.load(session_id)
    assert persisted.phase == ReviewResponsePhase.PR_ANALYSIS.value
    assert persisted.background_handles[0].status == "completed"
    assert persisted.structured_results[ReviewResponsePhase.PR_ANALYSIS.value]["working_tree_clean"] is True

    restarted_agent.workflow_state = persisted
    action = restarted_agent.next_review_response_workflow_action(
        restarted_agent._build_review_response_runner_inputs("https://github.com/example/repo/pull/42")
    )
    assert action is not None
    assert action.action_type.value == "emit_early_exit_report"
    assert restarted_agent.workflow_state.phase == ReviewResponsePhase.COMPLETED.value


def test_interrupted_background_work_resumes_without_losing_progress(monkeypatch: MonkeyPatch) -> None:
    from agent.workflows import BackgroundHandle, ReportSnapshot, ReviewResponsePhase, ReviewResponseWorkflowStateStore
    from run_agent import AIAgent
    from tools.process_registry import process_registry

    session_id = "review-interrupted-background"
    store = ReviewResponseWorkflowStateStore()
    state = store.default_state(session_id, workflow_id="review_response")
    state.phase = ReviewResponsePhase.FIX_LOOP.value
    state.loop_counters["refix"] = 3
    state.loop_gates.loop_count = 3
    state.last_delegated_step = ReviewResponsePhase.FIX_LOOP.value
    state.report_snapshots.append(
        ReportSnapshot(
            report_id="fix-loop-report-before-interrupt",
            phase=ReviewResponsePhase.FIX_LOOP.value,
            data={"files_modified": ["run_agent.py"], "next": "changed-review"},
        )
    )
    state.background_handles.append(
        BackgroundHandle(
            handle_id="proc_still_running_after_interrupt",
            kind="terminal",
            status="running",
            task_id="task-interrupt-1",
            metadata={
                "workflow_phase": ReviewResponsePhase.FIX_LOOP.value,
                "next_phase": ReviewResponsePhase.VERIFICATION.value,
            },
        )
    )
    store.save(state)

    monkeypatch.setattr(process_registry, "get", lambda _session_id: None)

    restarted_agent = object.__new__(AIAgent)
    restarted_agent.session_id = session_id
    restarted_agent.workflow_context = None
    restarted_agent.workflow_state = None
    restarted_agent._workflow_state_store = store
    activation = __import__("agent.workflows", fromlist=["activation_for_skill"]).activation_for_skill(
        "github-pr-review-response"
    )
    assert activation is not None
    restarted_agent.activate_workflow(activation)

    assert restarted_agent.workflow_state is not None
    assert restarted_agent.workflow_state.phase == ReviewResponsePhase.FIX_LOOP.value
    assert restarted_agent.workflow_state.loop_counters == {"refix": 3}
    assert restarted_agent.workflow_state.loop_gates.loop_count == 3
    assert restarted_agent.workflow_state.report_snapshots[0].data["next"] == "changed-review"
    assert restarted_agent.workflow_state.background_handles[0].status == "running"

    completion = (
        "[IMPORTANT: Background process proc_still_running_after_interrupt completed (exit code 0).\n"
        "Command: hermes delegated fix\n"
        "Output:\nfix complete after interrupt]"
    )
    restarted_agent._ingest_workflow_background_completion_message(completion)
    reloaded = store.load(session_id)
    assert reloaded.background_handles[0].status == "completed"
    assert reloaded.phase == ReviewResponsePhase.VERIFICATION.value
    assert reloaded.loop_counters == {"refix": 3}
    assert reloaded.report_snapshots[0].report_id == "fix-loop-report-before-interrupt"


def test_nested_delegate_completion_is_ingested_without_cross_session_leakage() -> None:
    from agent.workflows import ReviewResponsePhase, ReviewResponseWorkflowStateStore

    parent = _workflow_agent("review-nested-parent")
    sibling = _workflow_agent("review-nested-sibling")
    assert parent.workflow_state is not None
    assert sibling.workflow_state is not None

    parent.workflow_state.phase = ReviewResponsePhase.PR_ANALYSIS.value
    parent.workflow_state.loop_counters["analysis"] = 1
    sibling.workflow_state.phase = ReviewResponsePhase.FIX_LOOP.value
    sibling.workflow_state.loop_counters["refix"] = 5
    sibling._persist_workflow_state()

    parent._record_workflow_tool_result(
        "delegate_task",
        {
            "workflow_phase": ReviewResponsePhase.PR_ANALYSIS.value,
            "delegation_type": "nested-pr-analysis",
        },
        json.dumps(
            {
                "results": [
                    {
                        "task_index": 0,
                        "status": "completed",
                        "summary": "nested delegation completed cleanly",
                        "child_session_id": "child-review-session",
                    }
                ]
            }
        ),
        "nested-delegate-task",
    )

    parent_state = ReviewResponseWorkflowStateStore().load("review-nested-parent")
    sibling_state = ReviewResponseWorkflowStateStore().load("review-nested-sibling")

    assert parent_state.background_handles[0].kind == "delegate_task"
    assert parent_state.background_handles[0].handle_id == "child-review-session"
    assert parent_state.background_handles[0].status == "completed"
    assert parent_state.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert parent_state.loop_counters == {"analysis": 1}
    assert sibling_state.background_handles == []
    assert sibling_state.phase == ReviewResponsePhase.FIX_LOOP.value
    assert sibling_state.loop_counters == {"refix": 5}


def test_concurrent_workflow_sessions_keep_state_files_isolated() -> None:
    from agent.workflows import BackgroundHandle, ReportSnapshot, ReviewResponsePhase, ReviewResponseWorkflowStateStore
    from hermes_constants import get_hermes_home

    store = ReviewResponseWorkflowStateStore()
    alpha = store.default_state("review-concurrent-alpha", workflow_id="review_response")
    beta = store.default_state("review-concurrent-beta", workflow_id="review_response")

    alpha.phase = ReviewResponsePhase.PR_ANALYSIS.value
    alpha.loop_counters["analysis"] = 1
    alpha.report_snapshots.append(ReportSnapshot(report_id="alpha-report", phase="pr_analysis", data={"session": "alpha"}))
    alpha.background_handles.append(
        BackgroundHandle(
            handle_id="proc_alpha",
            kind="terminal",
            status="running",
            metadata={"workflow_phase": ReviewResponsePhase.PR_ANALYSIS.value, "next_phase": ReviewResponsePhase.CHANGED_REVIEW.value},
        )
    )

    beta.phase = ReviewResponsePhase.FIX_LOOP.value
    beta.loop_counters["refix"] = 2
    beta.report_snapshots.append(ReportSnapshot(report_id="beta-report", phase="fix_loop", data={"session": "beta"}))
    beta.background_handles.append(
        BackgroundHandle(
            handle_id="proc_beta",
            kind="terminal",
            status="running",
            metadata={"workflow_phase": ReviewResponsePhase.FIX_LOOP.value, "next_phase": ReviewResponsePhase.VERIFICATION.value},
        )
    )
    store.save(alpha)
    store.save(beta)

    alpha_agent = _workflow_agent("review-concurrent-alpha")
    beta_agent = _workflow_agent("review-concurrent-beta")
    alpha_agent._ingest_workflow_background_completion_message(
        "[IMPORTANT: Background process proc_alpha completed (exit code 0).\nCommand: alpha\nOutput:\nalpha done]"
    )

    alpha_reloaded = store.load("review-concurrent-alpha")
    beta_reloaded = store.load("review-concurrent-beta")
    assert alpha_reloaded.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert alpha_reloaded.background_handles[0].status == "completed"
    assert alpha_reloaded.report_snapshots[0].data == {"session": "alpha"}
    assert beta_reloaded.phase == ReviewResponsePhase.FIX_LOOP.value
    assert beta_reloaded.background_handles[0].status == "running"
    assert beta_reloaded.report_snapshots[0].data == {"session": "beta"}

    state_root = get_hermes_home() / "workflow_state" / "review_response"
    assert (state_root / "review-concurrent-alpha.json").exists()
    assert (state_root / "review-concurrent-beta.json").exists()
    assert beta_agent.workflow_state is not None
    assert beta_agent.workflow_state.session_id == "review-concurrent-beta"


def test_approval_denial_keeps_finalization_fail_closed_and_persisted() -> None:
    from agent.workflows import ReviewResponsePhase, ReviewResponseWorkflowStateStore
    from agent.workflows.review_response_phase_runner import ReviewResponsePhaseRunner, ReviewResponseRunnerInputs

    store = ReviewResponseWorkflowStateStore()
    state = store.default_state("review-approval-denied", workflow_id="review_response")
    state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    state.loop_counters["changed_review"] = 1
    state.approvals.ready_for_final_report = True
    state.loop_gates.terminal_state = "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
    }
    store.save(state)

    reloaded = store.load("review-approval-denied")
    action = ReviewResponsePhaseRunner().next_action(reloaded, ReviewResponseRunnerInputs(final_report_approved=False))
    store.save(reloaded)

    assert action.action_type.value == "finalization_locked"
    assert reloaded.phase == ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    assert reloaded.approvals.human_approval_required is True
    assert reloaded.approvals.final_report_approved is False
    assert reloaded.finalization.finalized is False
    assert reloaded.loop_gates.terminal_state == "awaiting_final_approval"

    persisted = store.load("review-approval-denied")
    assert persisted.finalization.finalized is False
    assert persisted.approvals.final_report_approved is False
    assert persisted.phase == ReviewResponsePhase.FINAL_APPROVAL_GATE.value


def test_malformed_delegate_completion_payload_records_violation_and_does_not_advance() -> None:
    import pytest

    from agent.workflows import ReviewResponsePhase, ReviewResponseWorkflowStateStore
    from agent.workflows.review_response_reports import ReportValidationError, normalize_delegate_result_or_record_violation

    store = ReviewResponseWorkflowStateStore()
    state = store.default_state("review-malformed-delegate", workflow_id="review_response")
    state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
    state.loop_counters["changed_review"] = 1
    store.save(state)

    reloaded = store.load("review-malformed-delegate")
    with pytest.raises(ReportValidationError):
        normalize_delegate_result_or_record_violation(
            "delegate finished but emitted prose instead of JSON",
            reloaded,
            phase=ReviewResponsePhase.CHANGED_REVIEW.value,
        )
    store.save(reloaded)

    persisted = store.load("review-malformed-delegate")
    assert persisted.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert persisted.loop_counters == {"changed_review": 1}
    assert persisted.approvals.ready_for_final_report is False
    assert persisted.finalization.finalized is False
    assert persisted.violations[-1].code == "malformed_delegate_report_output"
    assert "structured JSON" in persisted.violations[-1].details["error"]
