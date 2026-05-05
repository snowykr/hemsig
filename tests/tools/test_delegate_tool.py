from typing import Any, cast

from tools.delegate_tool import DELEGATE_TASK_SCHEMA, _strip_blocked_tools


def test_delegate_tool_does_not_advertise_durable_background_execution() -> None:
    """Workflow-owned background execution belongs to terminal policy, not delegate_task."""

    parameters = cast(dict[str, Any], DELEGATE_TASK_SCHEMA["parameters"])
    properties = cast(dict[str, Any], parameters["properties"])

    assert "background" not in properties
    assert "notify_on_complete" not in properties
    assert "workflow_phase" not in properties
    assert "workflow_delegation" not in properties


def test_child_delegation_toolset_is_stripped_for_nested_subagents() -> None:
    """Nested workflow hazards stay bounded by removing delegation from child toolsets."""

    assert _strip_blocked_tools(["terminal", "delegation", "memory", "file"]) == ["terminal", "file"]
