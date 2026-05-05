import json
from typing import cast


def _workflow_agent(session_id: str = "background-policy-session"):
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


def test_background_terminal_args_are_enforced() -> None:
    from agent.workflows import ReviewResponsePhase
    from hermes_constants import get_hermes_home

    agent = _workflow_agent()

    terminal_args, terminal_error = agent._apply_workflow_tool_policy(
        "terminal",
        {
            "command": "scripts/run_tests.sh tests/workflows",
            "background": False,
            "workflow_phase": "changed-review",
        },
    )

    assert terminal_error is None
    assert terminal_args["background"] is True
    assert terminal_args["notify_on_complete"] is True
    assert agent.workflow_state is not None
    assert agent.workflow_state.violations == []

    ordinary_terminal = {"command": "gh pr view", "background": False}
    ordinary_terminal_args, ordinary_terminal_error = agent._apply_workflow_tool_policy(
        "terminal",
        ordinary_terminal,
    )

    assert ordinary_terminal_error is None
    assert ordinary_terminal_args == ordinary_terminal

    delegate_args, delegate_error = agent._apply_workflow_tool_policy(
        "delegate_task",
        {
            "goal": "review the PR",
            "delivery_method": "inline",
            "workflow_phase": "changed-review",
            "background": False,
            "notify_on_complete": False,
        },
    )

    assert delegate_args["delivery_method"] == "inline"
    assert delegate_error is not None
    assert "runs synchronously" in delegate_error
    assert agent.workflow_state.violations[-1].code == "workflow_delegate_task_synchronous_blocked"

    blocked_args, blocked_error = agent._apply_workflow_tool_policy(
        "delegate_task",
        {
            "goal": "review the PR",
            "delivery_method": "prompt_file",
            "prompt_file": "/tmp/review-prompt.txt",
            "background": True,
            "notify_on_complete": True,
            "workflow_phase": "changed-review",
        },
    )

    assert blocked_args["prompt_file"] == "/tmp/review-prompt.txt"
    assert blocked_error is not None
    assert "prompt-file execution mode is blocked" in blocked_error
    assert agent.workflow_state is not None
    assert agent.workflow_state.phase == ReviewResponsePhase.ABORTED.value
    assert agent.workflow_state.finalization.aborted is True
    assert agent.workflow_state.violations[-1].code == "workflow_prompt_file_mode_blocked"
    assert agent.workflow_state.violations[-1].phase == "changed-review"

    state_path = get_hermes_home() / "workflow_state" / "review_response" / f"{agent.session_id}.json"
    assert state_path.exists()
    persisted = cast(dict[str, object], json.loads(state_path.read_text(encoding="utf-8")))
    violations = cast(list[dict[str, object]], persisted["violations"])
    assert violations[-1]["code"] == "workflow_prompt_file_mode_blocked"

    terminal_prompt_agent = _workflow_agent("terminal-prompt-file-block")
    terminal_blocked_args, terminal_blocked_error = terminal_prompt_agent._apply_workflow_tool_policy(
        "terminal",
        {
            "command": "python run_omx.py --prompt-file /tmp/review-prompt.txt",
            "workflow_delegation": True,
            "delivery_method": "prompt_file",
            "prompt_file_path": "/tmp/review-prompt.txt",
        },
    )
    assert terminal_blocked_args["workflow_delegation"] is True
    assert terminal_blocked_error is not None
    assert "prompt-file execution mode is blocked" in terminal_blocked_error
    assert terminal_prompt_agent.workflow_state is not None
    assert terminal_prompt_agent.workflow_state.violations[-1].code == "workflow_prompt_file_mode_blocked"

    conflict_agent = _workflow_agent("background-policy-conflict")
    _, conflict_error = conflict_agent._apply_workflow_tool_policy(
        "terminal",
        {
            "command": "make test",
            "background": "false",
            "notify_on_complete": True,
            "workflow_phase": "changed-review",
        },
    )
    assert conflict_error is not None
    assert "background=true" in conflict_error
    assert conflict_agent.workflow_state is not None
    assert conflict_agent.workflow_state.violations[-1].code == "workflow_execution_flag_conflict"


def test_unrelated_delegate_task_inside_workflow_keeps_prior_behavior() -> None:
    agent = _workflow_agent("active-workflow-unrelated-delegate")

    delegate_input = {
        "goal": "summarize unrelated implementation options",
        "prompt_file": "/tmp/ordinary-prompt.txt",
        "delivery_method": "prompt_file",
        "background": False,
    }
    delegate_args, delegate_error = agent._apply_workflow_tool_policy("delegate_task", delegate_input)

    assert delegate_error is None
    assert delegate_args == delegate_input
    assert agent.workflow_state is not None
    assert agent.workflow_state.violations == []


def test_workflow_owned_delegate_task_inside_workflow_is_still_blocked() -> None:
    agent = _workflow_agent("active-workflow-owned-delegate")

    delegate_args, delegate_error = agent._apply_workflow_tool_policy(
        "delegate_task",
        {
            "goal": "review the PR",
            "delivery_method": "inline",
            "workflow_delegation": True,
            "background": False,
            "notify_on_complete": False,
        },
    )

    assert delegate_args["workflow_delegation"] is True
    assert delegate_error is not None
    assert "runs synchronously" in delegate_error
    assert agent.workflow_state is not None
    assert agent.workflow_state.violations[-1].code == "workflow_delegate_task_synchronous_blocked"


def test_non_workflow_calls_are_not_rewritten() -> None:
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.session_id = "ordinary-session"
    agent.workflow_context = None
    agent.workflow_state = None

    terminal_input = {"command": "make test", "background": False}
    terminal_args, terminal_error = agent._apply_workflow_tool_policy("terminal", terminal_input)

    assert terminal_error is None
    assert terminal_args == terminal_input
    assert "notify_on_complete" not in terminal_args

    delegate_input = {
        "goal": "review the PR",
        "prompt_file": "/tmp/ordinary-prompt.txt",
        "delivery_method": "prompt_file",
        "background": False,
    }
    delegate_args, delegate_error = agent._apply_workflow_tool_policy("delegate_task", delegate_input)

    assert delegate_error is None
    assert delegate_args == delegate_input

    from tools.terminal_tool import TERMINAL_SCHEMA

    terminal_parameters = cast(dict[str, object], TERMINAL_SCHEMA["parameters"])
    terminal_properties = cast(dict[str, object], terminal_parameters["properties"])
    assert "workflow_delegation" not in terminal_properties
    assert "workflow_phase" not in terminal_properties
    assert "delivery_method" not in terminal_properties
    assert "prompt_file_path" not in terminal_properties
