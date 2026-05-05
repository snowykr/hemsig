import json
import subprocess
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from agent.workflows import (
    ReviewResponsePhase,
    ReviewResponseWorkflowStateStore,
    WorkflowDelegationPhase,
)

_runner_module = import_module("agent.workflows.review_response_phase_runner")
ActionableStateCheck = _runner_module.ActionableStateCheck
ChangedReviewResult = _runner_module.ChangedReviewResult
PRAnalysisResult = _runner_module.PRAnalysisResult
ReviewResponsePhaseRunner = _runner_module.ReviewResponsePhaseRunner
ReviewResponseRunnerAction = _runner_module.ReviewResponseRunnerAction
ReviewResponseRunnerActionType = _runner_module.ReviewResponseRunnerActionType
ReviewResponseRunnerInputs = _runner_module.ReviewResponseRunnerInputs


def _pr_analysis_action():
    store = ReviewResponseWorkflowStateStore()
    state = store.default_state("workflow-execution-action")
    state.phase = ReviewResponsePhase.ACTIONABLE_STATE_CHECK.value
    runner = ReviewResponsePhaseRunner()
    return runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            pr_url="https://github.com/example/repo/pull/42",
            repo_path="/tmp/repo",
            pr_number=42,
            actionable_state=ActionableStateCheck(unresolved_review_threads=1),
        ),
    )


def _workflow_execution_agent(
    session_id: str,
    *,
    enabled_tools: set[str] | None = None,
    max_iterations: int = 3,
):
    from agent.tool_guardrails import ToolGuardrailDecision
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent, IterationBudget

    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None

    agent: Any = object.__new__(AIAgent)
    agent.session_id = session_id
    agent.workflow_context = None
    agent.workflow_state = None
    agent._workflow_state_store = ReviewResponseWorkflowStateStore()
    agent.activate_workflow(activation)
    agent.max_iterations = max_iterations
    agent.iteration_budget = IterationBudget(max_iterations)
    agent.valid_tool_names = enabled_tools if enabled_tools is not None else {"terminal"}
    agent._interrupt_requested = False
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent.log_prefix = ""
    agent.log_prefix_chars = 120
    agent.tool_progress_callback = None
    agent.tool_start_callback = None
    agent.tool_complete_callback = None
    agent.tool_delay = 0
    agent._checkpoint_mgr = SimpleNamespace(enabled=False)
    agent._context_engine_tool_names = set()
    agent._memory_manager = None
    agent._current_tool = None
    agent._api_call_count = 0
    agent._tool_guardrail_halt_decision = None
    agent._subdirectory_hints = SimpleNamespace(check_tool_call=lambda *_args, **_kwargs: "")
    agent._should_emit_quiet_tool_messages = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    agent._vprint = lambda *_args, **_kwargs: None
    agent._safe_print = lambda *_args, **_kwargs: None
    agent._touch_activity = lambda *_args, **_kwargs: None
    agent._print_fn = lambda *_args, **_kwargs: None

    class _AllowingToolGuardrails:
        def before_call(self, *_args, **_kwargs):
            return ToolGuardrailDecision()

        def after_call(self, *_args, **_kwargs):
            return ToolGuardrailDecision()

    agent._tool_guardrails = _AllowingToolGuardrails()
    return agent


def test_runner_advances_by_phase_table() -> None:
    store = ReviewResponseWorkflowStateStore()
    state = store.default_state("phase-table-session")
    runner = ReviewResponsePhaseRunner()
    base = ReviewResponseRunnerInputs(
        pr_url="https://github.com/example/repo/pull/42",
        repo_path="/tmp/repo",
        pr_number=42,
    )

    action = runner.next_action(state, base)
    assert action.action_type == ReviewResponseRunnerActionType.RUN_DUPLICATE_SESSION_CHECK
    assert state.phase == ReviewResponsePhase.DUPLICATE_SESSION_CHECK.value

    action = runner.next_action(state, ReviewResponseRunnerInputs(**{**base.__dict__, "duplicate_session_active": False}))
    assert action.action_type == ReviewResponseRunnerActionType.RUN_ACTIONABLE_STATE_CHECK
    assert state.phase == ReviewResponsePhase.ACTIONABLE_STATE_CHECK.value

    action = runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            **{
                **base.__dict__,
                "actionable_state": ActionableStateCheck(unresolved_review_threads=2),
            }
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_PR_ANALYSIS
    assert action.prompt is not None
    assert action.prompt.delegation_type == "pr-analysis"
    assert action.prompt.delivery_method == "inline"
    assert "PROMPT_B64=" in action.terminal_args["command"]
    assert "RUNNER_B64=" in action.terminal_args["command"]
    assert "omx --madmax --high exec" not in action.terminal_args["command"]
    assert action.terminal_args["background"] is True
    assert action.terminal_args["workflow_delegation"] is True
    assert "prompt" not in action.terminal_args
    assert "workflow_owned" not in action.terminal_args
    assert state.phase == ReviewResponsePhase.PR_ANALYSIS.value

    action = runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            **{
                **base.__dict__,
                "pr_analysis": PRAnalysisResult(
                    completed=True,
                    unresolved_review_threads=1,
                    failing_ci=False,
                    modifications_made=True,
                    working_tree_clean=False,
                    root_cause_no_valid_issues=False,
                ),
            }
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW
    assert action.prompt is not None
    assert action.prompt.delegation_type == "changed-review"
    assert state.phase == ReviewResponsePhase.CHANGED_REVIEW.value

    action = runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            **{
                **base.__dict__,
                "changed_review": ChangedReviewResult(
                    completed=True,
                    succeeded=True,
                    blocking_findings=("missing regression test",),
                    clean_recommendation=False,
                    review_findings="missing regression test",
                ),
            }
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_REFIX
    assert action.prompt is not None
    assert action.prompt.delegation_type == "resolve-problem"
    assert state.phase == ReviewResponsePhase.FIX_LOOP.value
    assert state.approvals.ready_for_final_report is False

    action = runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            **{
                **base.__dict__,
                "refix_phase": WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM,
                "refix_findings": "missing regression test",
            }
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_REFIX

    state.phase = ReviewResponsePhase.VERIFICATION.value
    action = runner.next_action(state, base)
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW
    assert state.phase == ReviewResponsePhase.CHANGED_REVIEW.value

    action = runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            **{
                **base.__dict__,
                "changed_review": ChangedReviewResult(
                    completed=True,
                    succeeded=True,
                    clean_recommendation=True,
                ),
            }
        ),
    )
    assert action.action_type == ReviewResponseRunnerActionType.FINALIZATION_LOCKED
    assert state.phase == ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    assert state.approvals.ready_for_final_report is True


def test_finalization_locked_until_gate_is_clean() -> None:
    state = ReviewResponseWorkflowStateStore().default_state("locked-finalization-session")
    runner = ReviewResponsePhaseRunner()

    state.phase = ReviewResponsePhase.FINALIZATION.value
    action = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=True))
    assert action.action_type == ReviewResponseRunnerActionType.FINALIZATION_LOCKED
    assert state.finalization.finalized is False
    assert state.violations[-1].code == "workflow_finalization_gate_locked"

    state.violations.clear()
    state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    state.loop_counters["changed_review"] = 1
    state.approvals.ready_for_final_report = True
    state.loop_gates.terminal_state = "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
    }

    action = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=False))
    assert action.action_type == ReviewResponseRunnerActionType.FINALIZATION_LOCKED
    assert state.finalization.finalized is False
    assert not state.violations

    action = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=True))
    assert action.action_type == ReviewResponseRunnerActionType.FINALIZE
    assert state.phase == ReviewResponsePhase.FINALIZATION.value

    action = runner.next_action(state, ReviewResponseRunnerInputs())
    assert action.action_type == ReviewResponseRunnerActionType.FINALIZE
    assert state.finalization.finalized is False
    assert state.phase == ReviewResponsePhase.FINALIZATION.value

    action = runner.next_action(state, ReviewResponseRunnerInputs(finalization_succeeded=False))
    assert action.action_type == ReviewResponseRunnerActionType.WAIT
    assert state.finalization.finalized is False
    assert state.phase == ReviewResponsePhase.FINALIZATION.value

    state.loop_gates.terminal_state = "finalization_approved"
    action = runner.next_action(state, ReviewResponseRunnerInputs(finalization_succeeded=True))
    assert action.action_type == ReviewResponseRunnerActionType.RESOLVE_THREADS
    assert state.finalization.finalized is True
    assert state.phase == ReviewResponsePhase.THREAD_RESOLUTION.value

    action = runner.next_action(state, ReviewResponseRunnerInputs(threads_resolved=True))
    assert action.action_type == ReviewResponseRunnerActionType.COMPLETE
    assert state.phase == ReviewResponsePhase.COMPLETED.value


def test_finalization_requires_completed_structured_pr_analysis() -> None:
    state = ReviewResponseWorkflowStateStore().default_state("structured-pr-analysis-gate")
    runner = ReviewResponsePhaseRunner()
    state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    state.loop_counters["changed_review"] = 1
    state.approvals.ready_for_final_report = True
    state.loop_gates.terminal_state = "approval_gate_ready"

    missing = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=True))
    assert missing.action_type == ReviewResponseRunnerActionType.FINALIZATION_LOCKED
    assert state.violations[-1].code == "workflow_finalization_gate_locked"

    state.violations.clear()
    state.approvals.ready_for_final_report = True
    state.loop_gates.terminal_state = "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 1,
    }
    unresolved = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=True))
    assert unresolved.action_type == ReviewResponseRunnerActionType.FINALIZE

    state.violations.clear()
    state.approvals.ready_for_final_report = True
    state.loop_gates.terminal_state = "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
    }
    allowed = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=True))
    assert allowed.action_type == ReviewResponseRunnerActionType.FINALIZE


def test_no_llm_reasoning_between_deterministic_steps() -> None:
    state = ReviewResponseWorkflowStateStore().default_state("no-llm-deterministic-step-session")
    runner = ReviewResponsePhaseRunner()
    base = ReviewResponseRunnerInputs(
        pr_url="https://github.com/example/repo/pull/42",
        repo_path="/tmp/repo",
        pr_number=42,
    )

    state.phase = ReviewResponsePhase.PR_ANALYSIS.value
    action = runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            **{
                **base.__dict__,
                "pr_analysis": PRAnalysisResult(
                    completed=True,
                    unresolved_review_threads=1,
                    failing_ci=False,
                    modifications_made=True,
                    working_tree_clean=False,
                    root_cause_no_valid_issues=False,
                ),
            }
        ),
    )

    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW
    assert action.prompt is not None
    assert action.prompt.delegation_type == "changed-review"
    assert state.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert action.action_type != ReviewResponseRunnerActionType.WAIT
    assert "No transition rule matched" not in action.reason

    state.phase = ReviewResponsePhase.VERIFICATION.value
    action = runner.next_action(state, base)

    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW
    assert action.prompt is not None
    assert action.prompt.delegation_type == "changed-review"
    assert state.phase == ReviewResponsePhase.CHANGED_REVIEW.value
    assert state.loop_gates.requires_verification_pass is True
    assert action.action_type != ReviewResponseRunnerActionType.WAIT
    assert "No transition rule matched" not in action.reason


def test_workflow_execution_preserves_standard_guards(monkeypatch) -> None:
    action = _pr_analysis_action()
    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_PR_ANALYSIS

    disabled_agent = _workflow_execution_agent(
        "workflow-disabled-tool-session",
        enabled_tools={"read_file"},
    )
    disabled_messages: list[dict[str, object]] = []
    assert disabled_agent.execute_review_response_workflow_action(
        action,
        disabled_messages,
        "workflow-disabled-tool-task",
    ) is True
    disabled_result = json.loads(str(disabled_messages[-1]["content"]))
    assert "not enabled" in disabled_result["error"]
    assert disabled_agent.iteration_budget.used == 0

    empty_enabled_agent = _workflow_execution_agent(
        "workflow-empty-enabled-tools-session",
        enabled_tools=set(),
    )
    empty_enabled_messages: list[dict[str, object]] = []
    assert empty_enabled_agent.execute_review_response_workflow_action(
        action,
        empty_enabled_messages,
        "workflow-empty-enabled-tools-task",
    ) is True
    empty_enabled_result = json.loads(str(empty_enabled_messages[-1]["content"]))
    assert "not enabled" in empty_enabled_result["error"]
    assert empty_enabled_agent.iteration_budget.used == 0

    from hermes_cli import plugins

    pre_tool_calls: list[tuple[str, dict[str, object], str]] = []

    def block_workflow_terminal(tool_name, args, task_id="", **_kwargs):
        pre_tool_calls.append((tool_name, args, task_id))
        return "blocked by test plugin"

    monkeypatch.setattr(plugins, "get_pre_tool_call_block_message", block_workflow_terminal)

    plugin_agent = _workflow_execution_agent("workflow-plugin-block-session")
    plugin_messages: list[dict[str, object]] = []
    assert plugin_agent.execute_review_response_workflow_action(
        action,
        plugin_messages,
        "workflow-plugin-block-task",
    ) is True
    plugin_result = json.loads(str(plugin_messages[-1]["content"]))
    assert plugin_result["error"] == "blocked by test plugin"
    assert pre_tool_calls
    assert pre_tool_calls[-1][0] == "terminal"
    assert pre_tool_calls[-1][1]["workflow_delegation"] is True
    assert pre_tool_calls[-1][2] == "workflow-plugin-block-task"
    assert plugin_agent.iteration_budget.used == 1

    monkeypatch.setattr(plugins, "get_pre_tool_call_block_message", lambda *_args, **_kwargs: None)

    from tools import terminal_tool

    approval_calls: list[tuple[str, str]] = []

    def deny_at_approval(command: str, env_type: str) -> dict[str, object]:
        approval_calls.append((command, env_type))
        return {
            "approved": False,
            "message": "BLOCKED: approval denied by test",
            "description": "test approval denial",
        }

    monkeypatch.setattr(terminal_tool, "_check_all_guards", deny_at_approval)

    approval_agent = _workflow_execution_agent("workflow-approval-denied-session")
    approval_messages: list[dict[str, object]] = []
    assert approval_agent.execute_review_response_workflow_action(
        action,
        approval_messages,
        "workflow-approval-denied-task",
    ) is True
    approval_result = json.loads(str(approval_messages[-1]["content"]))
    assert approval_result["status"] == "blocked"
    assert "approval denied" in approval_result["error"]
    assert approval_calls
    assert "PROMPT_B64=" in approval_calls[-1][0]
    assert "RUNNER_B64=" in approval_calls[-1][0]
    assert "omx --madmax --high exec" not in approval_calls[-1][0]
    assert approval_agent.iteration_budget.used == 1


def test_workflow_execution_preserves_interrupt_and_budget_behavior(monkeypatch) -> None:
    action = _pr_analysis_action()

    interrupted_agent = _workflow_execution_agent("workflow-interrupted-session")
    interrupted_agent._interrupt_requested = True
    interrupted_messages: list[dict[str, object]] = []
    assert interrupted_agent.execute_review_response_workflow_action(
        action,
        interrupted_messages,
        "workflow-interrupted-task",
    ) is True
    assert "user interrupt" in str(interrupted_messages[-1]["content"])
    assert interrupted_agent.iteration_budget.used == 0

    budget_agent = _workflow_execution_agent("workflow-budget-session", max_iterations=1)
    assert budget_agent.iteration_budget.consume() is True
    budget_messages: list[dict[str, object]] = []
    assert budget_agent.execute_review_response_workflow_action(
        action,
        budget_messages,
        "workflow-budget-task",
    ) is True
    budget_result = json.loads(str(budget_messages[-1]["content"]))
    assert "iteration budget exhausted" in budget_result["error"]
    assert budget_agent.iteration_budget.used == 1

    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "get_pre_tool_call_block_message", lambda *_args, **_kwargs: "blocked after budget consume")
    counted_agent = _workflow_execution_agent("workflow-budget-counted-session", max_iterations=2)
    counted_messages: list[dict[str, object]] = []
    assert counted_agent.execute_review_response_workflow_action(
        action,
        counted_messages,
        "workflow-budget-counted-task",
    ) is True
    counted_result = json.loads(str(counted_messages[-1]["content"]))
    assert counted_result["error"] == "blocked after budget consume"
    assert counted_agent.iteration_budget.used == 1


def test_workflow_terminal_command_does_not_embed_prompt_in_shell() -> None:
    from agent.workflows import InlineDelegationPrompt

    malicious_prompt = "review this $(touch /tmp/owned) and `id` without executing shell syntax"
    action = ReviewResponseRunnerAction(
        ReviewResponseRunnerActionType.DELEGATE_PR_ANALYSIS,
        ReviewResponsePhase.PR_ANALYSIS,
        "test malicious prompt transport",
        prompt=InlineDelegationPrompt(
            phase=WorkflowDelegationPhase.PR_ANALYSIS,
            delegation_type="pr-analysis",
            body=malicious_prompt,
        ),
    )

    terminal_args = action.terminal_args
    command = str(terminal_args["command"])

    assert terminal_args["workflow_delegation"] is True
    assert "prompt" not in terminal_args
    assert "workflow_owned" not in terminal_args
    assert "PROMPT_B64=" in command
    assert "RUNNER_B64=" in command
    assert malicious_prompt not in command
    assert "$(touch /tmp/owned)" not in command
    assert "`id`" not in command


def test_finalization_terminal_command_is_explicit_and_does_not_stage_all() -> None:
    state = ReviewResponseWorkflowStateStore().default_state("finalization-command-session")
    runner = ReviewResponsePhaseRunner()
    state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    state.loop_counters["changed_review"] = 1
    state.approvals.ready_for_final_report = True
    state.loop_gates.terminal_state = "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
    }

    action = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=True))
    command = str(action.terminal_args["command"])

    assert action.action_type == ReviewResponseRunnerActionType.FINALIZE
    assert action.terminal_args["delegation_type"] == "workflow-finalize"
    assert "SCRIPT_B64" not in command
    assert "git add -A" not in command
    assert 'run("git", "add"' not in command
    assert 'run("git", "diff", "--cached", "--name-only")' in command
    assert 'run("git", "ls-files", "--others", "--exclude-standard")' not in command
    assert "candidate_files" in command
    assert "blocked_unstaged" in command
    assert "line.startswith(\"?? \")" in command


def test_finalization_terminal_command_does_not_stage_unrelated_untracked_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)
    tracked.write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    evidence = repo / ".sisyphus" / "evidence" / "task-1.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("do not stage\n", encoding="utf-8")

    state = ReviewResponseWorkflowStateStore().default_state("finalization-command-exec-session")
    runner = ReviewResponsePhaseRunner()
    state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    state.loop_counters["changed_review"] = 1
    state.approvals.ready_for_final_report = True
    state.loop_gates.terminal_state = "approval_gate_ready"
    state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
    }

    action = runner.next_action(state, ReviewResponseRunnerInputs(final_report_approved=True))
    result = subprocess.run(str(action.terminal_args["command"]), cwd=repo, shell=True, capture_output=True, text=True)

    assert result.returncode == 0
    marker = "WORKFLOW_FINALIZATION_RESULT="
    payload = json.loads(result.stdout.split(marker, 1)[1].strip())
    assert payload["candidate_files"] == ["tracked.txt"]
    assert payload["untracked_files"] == [".sisyphus/"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True)
    assert "?? .sisyphus/" in status.stdout
    cached = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, check=True, capture_output=True, text=True)
    assert ".sisyphus" not in cached.stdout


def test_changed_review_runs_in_sandbox_and_reports_mutation_attempts(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_omx = fake_bin / "omx"
    fake_omx.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "Path('reviewer-touched.txt').write_text('sandbox only\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_omx.chmod(0o755)

    state = ReviewResponseWorkflowStateStore().default_state("changed-review-sandbox-session")
    runner = ReviewResponsePhaseRunner()
    state.phase = ReviewResponsePhase.CHANGED_REVIEW.value
    action = runner.next_action(
        state,
        ReviewResponseRunnerInputs(
            pr_url="https://github.com/example/repo/pull/42",
            repo_path=str(repo),
            pr_number=42,
            pr_analysis=PRAnalysisResult(
                completed=True,
                unresolved_review_threads=1,
                failing_ci=False,
                modifications_made=True,
                working_tree_clean=False,
                root_cause_no_valid_issues=False,
            ),
        ),
    )

    assert action.action_type == ReviewResponseRunnerActionType.DELEGATE_CHANGED_REVIEW
    command = str(action.terminal_args["command"])
    assert "REPO_B64=" in command

    env = {"PATH": f"{fake_bin}:{__import__('os').environ.get('PATH', '')}"}
    result = subprocess.run(command, cwd=repo, shell=True, capture_output=True, text=True, env=env)

    assert result.returncode == 0
    assert not (repo / "reviewer-touched.txt").exists()
    marker = "STRUCTURED_RESULT="
    payload = json.loads(result.stdout.split(marker, 1)[1].splitlines()[0].strip())
    assert payload["workflow_phase"] == "changed_review"
    assert payload["delegation_type"] == "changed-review"
    assert payload["blocking_findings"]
    assert "review sandbox" in payload["review_findings"]
    assert payload["clean_recommendation"] is False


def test_run_conversation_invokes_runner_after_activation(monkeypatch) -> None:
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        enabled_toolsets=["terminal"],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=5,
    )
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None
    agent.activate_workflow(activation)

    executed_actions: list[str] = []

    def fake_execute(action, messages, effective_task_id):
        del effective_task_id
        action_name = str(action.action_type)
        executed_actions.append(action_name)
        if action_name.endswith("duplicate_session_check"):
            payload = {"output": 'WORKFLOW_GUARD_RESULT={"guard":"duplicate_session","duplicate_session_active":false}'}
        elif action_name.endswith("actionable_state_check"):
            payload = {
                "output": (
                    'WORKFLOW_GUARD_RESULT={"guard":"actionable_state",'
                    '"unresolved_review_threads":1,"failing_ci":false,'
                    '"other_actionable_state":false,"ci_status":"passing",'
                    '"local_verification":"guard"}'
                )
            }
        else:
            payload = {"session_id": "proc_runtime_runner", "output": "started"}
        messages.append({"role": "tool", "tool_call_id": f"fake-{len(executed_actions)}", "content": json.dumps(payload)})
        return True

    monkeypatch.setattr(agent, "execute_review_response_workflow_action", fake_execute)
    monkeypatch.setattr(agent, "_sync_external_memory_for_turn", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "_spawn_background_review", lambda **_kwargs: None)

    result = agent.run_conversation("/github-pr-review-response https://github.com/example/repo/pull/42")

    assert result["final_response"]
    assert "pr-analysis" in result["final_response"]
    assert executed_actions == [
        "run_duplicate_session_check",
        "run_actionable_state_check",
        "delegate_pr_analysis",
    ]
    assert agent.workflow_state is not None
    assert agent.workflow_state.phase == ReviewResponsePhase.PR_ANALYSIS.value


def test_runtime_clean_exit_uses_report_renderer(monkeypatch) -> None:
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        enabled_toolsets=["terminal"],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=5,
    )
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None
    agent.activate_workflow(activation)

    def fake_execute(action, messages, effective_task_id):
        del effective_task_id
        action_name = str(action.action_type)
        if action_name.endswith("duplicate_session_check"):
            payload = {"output": 'WORKFLOW_GUARD_RESULT={"guard":"duplicate_session","duplicate_session_active":false}'}
        elif action_name.endswith("actionable_state_check"):
            payload = {
                "output": (
                    'WORKFLOW_GUARD_RESULT={"guard":"actionable_state",'
                    '"unresolved_review_threads":0,"failing_ci":false,'
                    '"other_actionable_state":false,"ci_status":"passing",'
                    '"local_verification":"pre-delegation guard only"}'
                )
            }
        else:
            raise AssertionError(f"unexpected workflow action: {action_name}")
        messages.append({"role": "tool", "tool_call_id": "fake-clean", "content": json.dumps(payload)})
        return True

    monkeypatch.setattr(agent, "execute_review_response_workflow_action", fake_execute)
    monkeypatch.setattr(agent, "_sync_external_memory_for_turn", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "_spawn_background_review", lambda **_kwargs: None)

    result = agent.run_conversation("/github-pr-review-response https://github.com/example/repo/pull/42")

    assert result["final_response"] == "\n".join(
        [
            "PR #42 review-response complete — no action required.",
            "",
            "- Unresolved review threads: 0",
            "- CI status: passing",
            "- Working tree: clean (no changes made)",
            "- Local verification: pre-delegation guard only",
        ]
    )
    assert "Structured pre-delegation guard" not in result["final_response"]


def test_runtime_final_completion_uses_completion_report_renderer(monkeypatch) -> None:
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        enabled_toolsets=["terminal"],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=5,
    )
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None
    agent.activate_workflow(activation)
    assert agent.workflow_state is not None
    agent.workflow_state.phase = ReviewResponsePhase.FINAL_REPORT.value
    agent.workflow_state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    agent.workflow_state.loop_counters["changed_review"] = 1
    agent.workflow_state.approvals.ready_for_final_report = True
    agent.workflow_state.approvals.final_report_approved = True
    agent.workflow_state.finalization.finalized = True
    agent.workflow_state.loop_gates.terminal_state = "threads_resolved"
    agent.workflow_state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
        "ci_status": "passing",
        "local_verification": "targeted runtime report tests passed",
    }
    agent.workflow_state.structured_results[ReviewResponsePhase.CHANGED_REVIEW.value] = {
        "review_findings": "clean changed-review recommendation",
    }
    agent._workflow_state_store.save(agent.workflow_state)

    monkeypatch.setattr(agent, "_sync_external_memory_for_turn", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "_spawn_background_review", lambda **_kwargs: None)

    result = agent.run_conversation("/github-pr-review-response https://github.com/example/repo/pull/42")

    assert result["final_response"] == "\n".join(
        [
            "PR #42 review-response complete — finalized.",
            "",
            "## Schema",
            "- Version: 1",
            "- Type: review_response.completion_report",
            "",
            "## Required status",
            "- Unresolved review threads: 0",
            "- CI status: passing",
            "- Working tree: clean after finalization",
            "- Local verification: clean changed-review recommendation",
            "",
            "## Review thread resolution",
            "- Resolution status: resolved inspected threads after finalization",
        ]
    )
    assert "Post-approval finalization" not in result["final_response"]
    assert agent.workflow_state.report_snapshots[-1].data["report_type"] == "review_response.completion_report"


def test_run_conversation_executes_finalization_and_thread_resolution(monkeypatch) -> None:
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        enabled_toolsets=["terminal"],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=5,
    )
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None
    agent.activate_workflow(activation)
    assert agent.workflow_state is not None
    agent.workflow_state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    agent.workflow_state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    agent.workflow_state.loop_counters["changed_review"] = 1
    agent.workflow_state.approvals.ready_for_final_report = True
    agent.workflow_state.approvals.final_report_approved = True
    agent.workflow_state.loop_gates.approval_decision = "approved"
    agent.workflow_state.loop_gates.terminal_state = "approval_gate_ready"
    agent.workflow_state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 2,
        "ci_status": "passing",
        "local_verification": "tests passed",
    }
    agent.workflow_state.structured_results[ReviewResponsePhase.CHANGED_REVIEW.value] = {
        "completed": True,
        "succeeded": True,
        "clean_recommendation": True,
        "review_findings": "clean changed-review completed",
    }
    agent._persist_workflow_state()

    executed_actions: list[str] = []

    def fake_execute(action, messages, effective_task_id):
        del effective_task_id
        action_name = str(action.action_type)
        executed_actions.append(action_name)
        if action_name.endswith("finalize"):
            assert action.terminal_args["delegation_type"] == "workflow-finalize"
            payload = {
                "output": (
                    'WORKFLOW_FINALIZATION_RESULT={"guard":"finalization",'
                    '"finalized":true,"commit_attempted":true,'
                    '"working_tree_clean":true,"commit_returncode":0,"push_returncode":0}'
                )
            }
        elif action_name.endswith("resolve_threads"):
            assert action.terminal_args["delegation_type"] == "workflow-thread-resolution"
            payload = {
                "output": (
                    'WORKFLOW_THREAD_RESOLUTION_RESULT={"guard":"thread_resolution",'
                    '"threads_resolved":true,"resolved_count":2,"remaining_unresolved":0}'
                )
            }
        else:
            raise AssertionError(f"unexpected workflow action: {action_name}")
        messages.append({"role": "tool", "tool_call_id": f"fake-final-{len(executed_actions)}", "content": json.dumps(payload)})
        return True

    monkeypatch.setattr(agent, "execute_review_response_workflow_action", fake_execute)
    monkeypatch.setattr(agent, "_sync_external_memory_for_turn", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "_spawn_background_review", lambda **_kwargs: None)

    result = agent.run_conversation("/github-pr-review-response https://github.com/example/repo/pull/42")

    assert executed_actions == ["finalize", "resolve_threads"]
    assert result["final_response"]
    assert "PR #42 review-response complete — finalized." in result["final_response"]
    assert "- Unresolved review threads: 0" in result["final_response"]
    assert "## Review thread resolution" in result["final_response"]
    assert agent.workflow_state.phase == ReviewResponsePhase.COMPLETED.value
    assert agent.workflow_state.finalization.finalized is True
    assert agent.workflow_state.finalization.completion_report_emitted is True


def test_run_conversation_does_not_resolve_threads_after_failed_finalization(monkeypatch) -> None:
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        enabled_toolsets=["terminal"],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=5,
    )
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None
    agent.activate_workflow(activation)
    assert agent.workflow_state is not None
    agent.workflow_state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    agent.workflow_state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    agent.workflow_state.loop_counters["changed_review"] = 1
    agent.workflow_state.approvals.ready_for_final_report = True
    agent.workflow_state.approvals.final_report_approved = True
    agent.workflow_state.loop_gates.approval_decision = "approved"
    agent.workflow_state.loop_gates.terminal_state = "approval_gate_ready"
    agent.workflow_state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
        "ci_status": "passing",
        "local_verification": "tests passed",
    }
    agent.workflow_state.structured_results[ReviewResponsePhase.CHANGED_REVIEW.value] = {
        "completed": True,
        "succeeded": True,
        "clean_recommendation": True,
        "review_findings": "clean changed-review completed",
    }
    agent._persist_workflow_state()

    executed_actions: list[str] = []

    def fake_execute(action, messages, effective_task_id):
        del effective_task_id
        action_name = str(action.action_type)
        executed_actions.append(action_name)
        if action_name.endswith("resolve_threads"):
            raise AssertionError("thread resolution must not run after failed finalization")
        payload = {
            "output": (
                'WORKFLOW_FINALIZATION_RESULT={"guard":"finalization",'
                '"finalized":false,"commit_attempted":true,'
                '"working_tree_clean":false,"commit_returncode":1,"push_returncode":0}'
            )
        }
        messages.append({"role": "tool", "tool_call_id": f"fake-final-fail-{len(executed_actions)}", "content": json.dumps(payload)})
        return True

    monkeypatch.setattr(agent, "execute_review_response_workflow_action", fake_execute)
    monkeypatch.setattr(agent, "_sync_external_memory_for_turn", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "_spawn_background_review", lambda **_kwargs: None)

    result = agent.run_conversation("/github-pr-review-response https://github.com/example/repo/pull/42")

    assert executed_actions == ["finalize"]
    assert result["final_response"]
    assert "thread resolution remains locked" in result["final_response"]
    assert agent.workflow_state.phase == ReviewResponsePhase.FINALIZATION.value
    assert agent.workflow_state.finalization.finalized is False
    assert agent.workflow_state.loop_gates.terminal_state == "finalization_failed"


def test_run_conversation_consumes_explicit_final_approval_text(monkeypatch) -> None:
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        enabled_toolsets=["terminal"],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=5,
    )
    activation = activation_for_skill("github-pr-review-response")
    assert activation is not None
    agent.activate_workflow(activation)
    assert agent.workflow_state is not None
    agent.workflow_state.phase = ReviewResponsePhase.FINAL_APPROVAL_GATE.value
    agent.workflow_state.last_delegated_step = ReviewResponsePhase.CHANGED_REVIEW.value
    agent.workflow_state.loop_counters["changed_review"] = 1
    agent.workflow_state.approvals.ready_for_final_report = True
    agent.workflow_state.approvals.human_approval_required = True
    agent.workflow_state.loop_gates.terminal_state = "awaiting_final_approval"
    agent.workflow_state.structured_results[ReviewResponsePhase.PR_ANALYSIS.value] = {
        "completed": True,
        "unresolved_review_threads": 0,
        "ci_status": "passing",
        "local_verification": "approval path test",
    }
    agent.workflow_state.structured_results[ReviewResponsePhase.CHANGED_REVIEW.value] = {
        "completed": True,
        "succeeded": True,
        "clean_recommendation": True,
        "review_findings": "clean changed-review completed",
    }
    agent._persist_workflow_state()

    executed_actions: list[str] = []

    def fake_execute(action, messages, effective_task_id):
        del effective_task_id
        action_name = str(action.action_type)
        executed_actions.append(action_name)
        if action_name.endswith("finalize"):
            payload = {"output": 'WORKFLOW_FINALIZATION_RESULT={"guard":"finalization","finalized":true,"working_tree_clean":true}'}
        elif action_name.endswith("resolve_threads"):
            payload = {"output": 'WORKFLOW_THREAD_RESOLUTION_RESULT={"guard":"thread_resolution","threads_resolved":true}'}
        else:
            raise AssertionError(f"unexpected workflow action: {action_name}")
        messages.append({"role": "tool", "tool_call_id": f"fake-approval-{len(executed_actions)}", "content": json.dumps(payload)})
        return True

    monkeypatch.setattr(agent, "execute_review_response_workflow_action", fake_execute)
    monkeypatch.setattr(agent, "_sync_external_memory_for_turn", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "_spawn_background_review", lambda **_kwargs: None)

    result = agent.run_conversation("I approve the final report for finalization")

    assert executed_actions == ["finalize", "resolve_threads"]
    assert agent.workflow_state.approvals.final_report_approved is True
    assert agent.workflow_state.approvals.human_approval_required is False
    assert agent.workflow_state.phase == ReviewResponsePhase.COMPLETED.value
    assert "PR #1 review-response complete — finalized." in result["final_response"]
