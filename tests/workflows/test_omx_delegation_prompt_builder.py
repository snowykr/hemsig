import json
from typing import cast

import pytest


def test_delegate_prompt_is_inline() -> None:
    from agent.workflows import (
        WorkflowDelegationPhase,
        WorkflowDelegationPromptRequest,
        build_review_response_delegation_prompt,
    )
    from hermes_constants import get_hermes_home

    prompt = build_review_response_delegation_prompt(
        WorkflowDelegationPromptRequest(
            phase=WorkflowDelegationPhase.PR_ANALYSIS,
            pr_url="https://github.com/example/project/pull/39",
        )
    )

    assert prompt.delivery_method == "inline"
    assert prompt.prompt_file_path is None
    assert prompt.delegation_type == "pr-analysis"
    assert "https://github.com/example/project/pull/39" in prompt.body
    assert "Use the GitHub CLI" in prompt.body
    assert "Do NOT commit or push" in prompt.body
    assert "PROMPT_FILE" not in prompt.body
    assert "-prompt.txt" not in prompt.body

    delegate_kwargs = prompt.to_delegate_task_kwargs()
    assert delegate_kwargs["goal"] == prompt.body
    assert delegate_kwargs["delivery_method"] == "inline"
    assert delegate_kwargs["prompt_file"] is None

    changed_review_prompt = build_review_response_delegation_prompt(
        WorkflowDelegationPromptRequest(
            phase="changed-review",
            pr_url="https://github.com/example/project/pull/39",
            repo_path="/workspace/project",
        )
    )
    assert changed_review_prompt.delivery_method == "inline"
    assert changed_review_prompt.prompt_file_path is None
    assert changed_review_prompt.delegation_type == "changed-review"
    assert "cd /workspace/project" in changed_review_prompt.body
    assert "This phase is review-only" in changed_review_prompt.body
    assert "Do NOT modify, create, delete, stage, or rewrite files" in changed_review_prompt.body
    assert "Do NOT implement fixes during review" in changed_review_prompt.body
    assert "$code-review" in changed_review_prompt.body

    refix_prompt = build_review_response_delegation_prompt(
        WorkflowDelegationPromptRequest(
            phase=WorkflowDelegationPhase.REFIX_RESOLVE_PROBLEM,
            pr_url="https://github.com/example/project/pull/39",
            repo_path="/workspace/project",
            review_findings="Regression test still fails for invalid review-thread IDs.",
        )
    )
    assert refix_prompt.delivery_method == "inline"
    assert refix_prompt.prompt_file_path is None
    assert refix_prompt.delegation_type == "resolve-problem"
    assert "Regression test still fails" in refix_prompt.body
    assert "$verifier" in refix_prompt.body

    assert not (get_hermes_home() / "workflow_state" / "review_response" / "prompt.txt").exists()


def test_inline_prompt_failure_records_violation_and_aborts() -> None:
    from agent.workflows import (
        ReviewResponsePhase,
        ReviewResponseWorkflowStateStore,
        WorkflowDelegationPhase,
        WorkflowDelegationPromptRequest,
        WorkflowPromptBuildError,
        build_review_response_delegation_prompt_or_abort,
    )

    session_id = "prompt-failure-session"
    store = ReviewResponseWorkflowStateStore()
    state = store.default_state(session_id)

    with pytest.raises(WorkflowPromptBuildError):
        _ = build_review_response_delegation_prompt_or_abort(
            WorkflowDelegationPromptRequest(
                phase=WorkflowDelegationPhase.CHANGED_REVIEW,
                pr_url="https://github.com/example/project/pull/39",
                repo_path="",
            ),
            state,
            store=store,
        )

    assert state.phase == ReviewResponsePhase.ABORTED.value
    assert state.finalization.aborted is True
    assert len(state.violations) == 1
    violation = state.violations[0]
    assert violation.code == "inline_prompt_build_failed"
    assert violation.phase == "changed-review"
    assert violation.details["delivery_method"] == "inline"
    assert "prompt-file fallback is blocked" in violation.message

    state_path = store.path_for_session(session_id)
    assert state_path.exists()
    persisted = cast(dict[str, object], json.loads(state_path.read_text(encoding="utf-8")))
    assert persisted["phase"] == "aborted"
    finalization = cast(dict[str, object], persisted["finalization"])
    violations = cast(list[dict[str, object]], persisted["violations"])
    assert finalization["aborted"] is True
    assert violations[0]["code"] == "inline_prompt_build_failed"

    prompt_files = list(state_path.parent.glob("*-prompt.txt"))
    prompt_files.extend(state_path.parent.glob("prompt*.txt"))
    assert prompt_files == []
