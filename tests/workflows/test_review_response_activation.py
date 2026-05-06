from pathlib import Path
import pytest
from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def skill_command_registry(monkeypatch: MonkeyPatch) -> dict[str, dict[str, str]]:
    from agent import skill_commands

    commands = {
        "/github-pr-review-response": {
            "name": "github-pr-review-response",
            "description": "Review response workflow",
            "skill_dir": "/tmp/github-pr-review-response",
        },
        "/omx-delegation": {
            "name": "omx-delegation",
            "description": "Delegation workflow",
            "skill_dir": "/tmp/omx-delegation",
        },
        "/ordinary-skill": {
            "name": "ordinary-skill",
            "description": "A normal skill",
            "skill_dir": "/tmp/ordinary-skill",
        },
    }
    monkeypatch.setattr(skill_commands, "_skill_commands", commands)
    return commands


@pytest.mark.parametrize(
    ("cmd_key", "workflow_id"),
    [
        ("/github-pr-review-response", "review_response"),
        ("/omx-delegation", "omx_delegation"),
    ],
)
def test_target_skill_activates_workflow_context(
    cmd_key: str,
    workflow_id: str,
    monkeypatch: MonkeyPatch,
) -> None:
    from agent import skill_commands
    from run_agent import AIAgent

    def fake_load_skill_payload(
        skill_identifier: str,
        task_id: str | None = None,
    ) -> tuple[dict[str, str], None, str]:
        _ = task_id
        skill_name = Path(skill_identifier).name
        return {"content": "skill instructions"}, None, skill_name

    monkeypatch.setattr(skill_commands, "_load_skill_payload", fake_load_skill_payload)

    message = skill_commands.build_skill_invocation_message(cmd_key, task_id="session-1")
    activation = skill_commands.workflow_activation_for_skill_command(cmd_key)

    assert message is not None
    assert activation is not None
    assert activation.workflow_id == workflow_id
    assert activation.skill_name == cmd_key.lstrip("/")

    agent = object.__new__(AIAgent)
    agent.workflow_context = None
    agent.activate_workflow(activation)

    assert agent.workflow_context == activation.to_dict()
    assert "workflow_id" not in message
    assert workflow_id not in message


def test_non_target_skill_does_not_activate_workflow() -> None:
    from agent.skill_commands import workflow_activation_for_skill_command
    from agent.workflows import activation_for_plain_github_pr_url, activation_for_skill

    assert workflow_activation_for_skill_command("/ordinary-skill") is None
    assert activation_for_skill("ordinary-skill") is None
    assert activation_for_skill("please use github-pr-review-response") is None
    assert activation_for_plain_github_pr_url("https://github.com/example/repo/issues/42") is None
    assert activation_for_plain_github_pr_url(
        "please review https://github.com/example/repo/pull/42"
    ) is None


def test_plain_github_pr_url_activates_review_response() -> None:
    from agent.workflows import activation_for_plain_github_pr_url

    activation = activation_for_plain_github_pr_url(
        "https://github.com/example/repo/pull/42"
    )

    assert activation is not None
    assert activation.workflow_id == "review_response"
    assert activation.skill_name == "github-pr-review-response"


def test_agent_auto_activates_review_response_for_plain_pr_url() -> None:
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.workflow_context = None
    agent.workflow_state = None
    agent.session_id = ""
    agent._ensure_workflow_state_loaded = lambda save_if_new=False: None

    agent._auto_activate_targeted_workflow_for_input(
        "https://github.com/example/repo/pull/42"
    )

    assert agent.workflow_context is not None
    assert agent.workflow_context["workflow_id"] == "review_response"


def test_agent_does_not_override_existing_workflow_activation() -> None:
    from agent.workflows import activation_for_skill
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.workflow_context = None
    agent.workflow_state = None
    agent.session_id = ""
    agent._ensure_workflow_state_loaded = lambda save_if_new=False: None
    activation = activation_for_skill("omx-delegation")

    assert activation is not None
    agent.activate_workflow(activation)
    agent._auto_activate_targeted_workflow_for_input(
        "https://github.com/example/repo/pull/42"
    )

    assert agent.workflow_context is not None
    assert agent.workflow_context["workflow_id"] == "omx_delegation"


def test_no_workflow_state_file_for_normal_sessions() -> None:
    from agent.skill_commands import workflow_activation_for_skill_command
    from hermes_constants import get_hermes_home

    assert workflow_activation_for_skill_command("/ordinary-skill") is None
    assert not (get_hermes_home() / "workflow_state").exists()
