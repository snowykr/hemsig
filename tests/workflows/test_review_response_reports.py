import json
from importlib import import_module

import pytest


def test_step_and_completion_reports_match_schema() -> None:
    reports = import_module("agent.workflows.review_response_reports")
    REPORT_SCHEMA_VERSION = reports.REPORT_SCHEMA_VERSION
    ReviewResponseCompletionReport = reports.ReviewResponseCompletionReport
    ReviewResponseReportType = reports.ReviewResponseReportType
    ReviewResponseStepReport = reports.ReviewResponseStepReport
    normalize_delegate_result_for_report = reports.normalize_delegate_result_for_report
    render_clean_pr_early_exit_report = reports.render_clean_pr_early_exit_report
    render_completion_report = reports.render_completion_report
    render_step_report = reports.render_step_report

    delegate_payload = {
        "results": [
            {
                "status": "success",
                "modified_files": ["agent/workflows/review_response_reports.py"],
                "decisions": ["Confirm final approval before resolving threads."],
                "pre_finalization_status": "ready for final approval",
                "next_recommended_step": "Request final approval.",
                "verification_summary": "targeted report tests passed",
                "summary": "Freeform delegate prose is intentionally not rendered as the report shape.",
            }
        ],
        "total_duration_seconds": 12.5,
    }
    normalized = normalize_delegate_result_for_report(json.dumps(delegate_payload))
    step_report = ReviewResponseStepReport(
        pr_number=39,
        phase="changed-review",
        delegate_result=normalized,
    )

    assert step_report.to_dict()["schema_version"] == REPORT_SCHEMA_VERSION
    assert step_report.to_dict()["report_type"] == ReviewResponseReportType.STEP.value
    assert step_report.to_snapshot("step-1").data["delegate_result"]["succeeded"] is True
    assert render_step_report(step_report) == "\n".join(
        [
            "PR #39 workflow step report",
            "",
            "## Schema",
            "- Version: 1",
            "- Type: review_response.step_report",
            "",
            "## Step",
            "- Phase: changed-review",
            "- Delegate status: succeeded",
            "",
            "## Modified files",
            "- agent/workflows/review_response_reports.py",
            "",
            "## Decisions requiring confirmation",
            "- Confirm final approval before resolving threads.",
            "",
            "## Workflow status",
            "- Pre-finalization state: ready for final approval",
            "- Local verification: targeted report tests passed",
            "- Next recommended step: Request final approval.",
        ]
    )

    early_exit_report = ReviewResponseCompletionReport(
        pr_number=39,
        unresolved_review_threads=0,
        ci_status="passing",
        working_tree="clean (no changes made)",
        local_verification="not required; no changes made",
        completion_status="no action required",
    )
    assert render_clean_pr_early_exit_report(early_exit_report) == "\n".join(
        [
            "PR #39 review-response complete — no action required.",
            "",
            "- Unresolved review threads: 0",
            "- CI status: passing",
            "- Working tree: clean (no changes made)",
            "- Local verification: not required; no changes made",
        ]
    )

    final_report = ReviewResponseCompletionReport(
        pr_number=39,
        unresolved_review_threads=0,
        ci_status="passing",
        working_tree="clean after commit and push",
        local_verification="scripts/run_tests.sh tests/workflows/test_review_response_reports.py passed",
        completion_status="finalized",
        thread_resolution="resolved inspected threads; invalid threads commented before resolution",
    )
    assert final_report.to_dict()["report_type"] == ReviewResponseReportType.COMPLETION.value
    assert final_report.to_snapshot("final-1").data["schema_version"] == REPORT_SCHEMA_VERSION
    assert render_completion_report(final_report) == "\n".join(
        [
            "PR #39 review-response complete — finalized.",
            "",
            "## Schema",
            "- Version: 1",
            "- Type: review_response.completion_report",
            "",
            "## Required status",
            "- Unresolved review threads: 0",
            "- CI status: passing",
            "- Working tree: clean after commit and push",
            "- Local verification: scripts/run_tests.sh tests/workflows/test_review_response_reports.py passed",
            "",
            "## Review thread resolution",
            "- Resolution status: resolved inspected threads; invalid threads commented before resolution",
        ]
    )


def test_malformed_delegate_output_is_normalized_or_rejected() -> None:
    from agent.workflows import ReviewResponseWorkflowStateStore
    reports = import_module("agent.workflows.review_response_reports")
    ReportValidationError = reports.ReportValidationError
    normalize_delegate_result_for_report = reports.normalize_delegate_result_for_report
    normalize_delegate_result_or_record_violation = reports.normalize_delegate_result_or_record_violation

    normalized = normalize_delegate_result_for_report(
        {
            "success": "true",
            "files_modified": "agent/workflows/review_response_reports.py",
            "verification": "diagnostics clean",
            "recommendation": "Proceed to final report.",
        }
    )
    assert normalized.succeeded is True
    assert normalized.files_modified == ["agent/workflows/review_response_reports.py"]
    assert normalized.verification_summary == "diagnostics clean"

    with pytest.raises(ReportValidationError):
        normalize_delegate_result_for_report("delegate says everything looks good")

    with pytest.raises(ReportValidationError):
        normalize_delegate_result_for_report({"results": ["not an object"]})

    store = ReviewResponseWorkflowStateStore()
    state = store.default_state("malformed-report-session")
    with pytest.raises(ReportValidationError):
        normalize_delegate_result_or_record_violation(
            {"status": "unclear", "summary": "No machine-readable success field."},
            state,
            phase="changed-review",
        )
    assert len(state.violations) == 1
    assert state.violations[0].code == "malformed_delegate_report_output"
    assert state.violations[0].phase == "changed-review"
