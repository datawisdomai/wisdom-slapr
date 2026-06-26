# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

from typing import List, Tuple

import pytest

from slapr.github import (
    CI_STATUS_FAILING,
    CI_STATUS_PASSING,
    CI_STATUS_RUNNING,
    CI_STATUS_UNKNOWN,
    PullRequestCheckRun,
    summarize_ci_status,
)


@pytest.mark.parametrize(
    "check_runs, combined_status_state, combined_status_count, ignored_check_names, expected_status",
    [
        pytest.param(
            [PullRequestCheckRun(name="test", status="in_progress", conclusion=None)],
            "",
            0,
            (),
            CI_STATUS_RUNNING,
            id="running-check",
        ),
        pytest.param(
            [
                PullRequestCheckRun(name="test", status="in_progress", conclusion=None),
                PullRequestCheckRun(name="lint", status="completed", conclusion="failure"),
            ],
            "pending",
            1,
            (),
            CI_STATUS_FAILING,
            id="failure-precedes-running",
        ),
        pytest.param(
            [
                PullRequestCheckRun(name="test", status="completed", conclusion="success"),
                PullRequestCheckRun(name="lint", status="completed", conclusion="skipped"),
            ],
            "",
            0,
            (),
            CI_STATUS_PASSING,
            id="completed-passing-checks",
        ),
        pytest.param([], "pending", 1, (), CI_STATUS_RUNNING, id="legacy-status-pending"),
        pytest.param([], "failure", 1, (), CI_STATUS_FAILING, id="legacy-status-failure"),
        pytest.param([], "success", 1, (), CI_STATUS_PASSING, id="legacy-status-success"),
        pytest.param([], "pending", 0, (), CI_STATUS_UNKNOWN, id="legacy-status-empty"),
        pytest.param(
            [PullRequestCheckRun(name="slapr", status="in_progress", conclusion=None)],
            "",
            0,
            ("slapr",),
            CI_STATUS_UNKNOWN,
            id="ignored-running-check",
        ),
    ],
)
def test_summarize_ci_status(
    check_runs: List[PullRequestCheckRun],
    combined_status_state: str,
    combined_status_count: int,
    ignored_check_names: Tuple[str, ...],
    expected_status: str,
) -> None:
    assert (
        summarize_ci_status(check_runs, combined_status_state, combined_status_count, ignored_check_names)
        == expected_status
    )
