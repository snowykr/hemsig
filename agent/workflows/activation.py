"""Narrow runtime activation registry for targeted skill workflows.

This module intentionally does not implement persistence or workflow execution.
It only maps known skill invocations to internal workflow identifiers so callers
can attach runtime-only context before tools run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class WorkflowActivation:
    """Structured runtime-only workflow activation metadata."""

    workflow_id: str
    skill_name: str
    policy_profile: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


_TARGETED_SKILL_WORKFLOWS: Mapping[str, WorkflowActivation] = {
    "github-pr-review-response": WorkflowActivation(
        workflow_id="review_response",
        skill_name="github-pr-review-response",
        policy_profile="github-pr-review-response",
    ),
    "omx-delegation": WorkflowActivation(
        workflow_id="omx_delegation",
        skill_name="omx-delegation",
        policy_profile="omx-delegation",
    ),
}

_INVALID_SKILL_CHARS = re.compile(r"[^a-z0-9-]")
_MULTI_HYPHEN = re.compile(r"-{2,}")
_GITHUB_PR_URL_ONLY = re.compile(
    r"^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+/?$",
    flags=re.IGNORECASE,
)


def _normalize_skill_name(name: str | None) -> str:
    text = str(name or "").strip().lower().replace("_", "-").replace(" ", "-")
    text = text.lstrip("/")
    text = _INVALID_SKILL_CHARS.sub("", text)
    return _MULTI_HYPHEN.sub("-", text).strip("-")


def activation_for_skill(skill_name: str | None) -> WorkflowActivation | None:
    """Return activation metadata for a targeted skill name or command slug."""

    normalized = _normalize_skill_name(skill_name)
    return _TARGETED_SKILL_WORKFLOWS.get(normalized)


def activation_for_plain_github_pr_url(text: str | None) -> WorkflowActivation | None:
    """Return review-response activation for a bare GitHub PR URL input."""

    candidate = str(text or "").strip()
    if not candidate or not _GITHUB_PR_URL_ONLY.fullmatch(candidate):
        return None
    return _TARGETED_SKILL_WORKFLOWS["github-pr-review-response"]
