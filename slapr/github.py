# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

import json
from typing import List, NamedTuple, Optional, Tuple

from github import Github

CI_STATUS_UNKNOWN = "unknown"
CI_STATUS_RUNNING = "running"
CI_STATUS_FAILING = "failing"
CI_STATUS_PASSING = "passing"

FAILING_CHECK_CONCLUSIONS = {"action_required", "cancelled", "failure", "startup_failure", "timed_out"}
PASSING_CHECK_CONCLUSIONS = {"neutral", "skipped", "success"}
FAILING_COMMIT_STATUS_STATES = {"error", "failure"}
RUNNING_COMMIT_STATUS_STATES = {"pending"}
PASSING_COMMIT_STATUS_STATES = {"success"}


class Review(NamedTuple):
    state: str
    user: object  # NamedUser in production, MockUser in tests


class PullRequest(NamedTuple):
    state: str
    merged: bool
    mergeable_state: str
    url: str = ""
    draft: bool = False
    head_sha: str = ""
    head_repo_fork: bool = False
    head_owner_login: str = ""


class PullRequestCheckRun(NamedTuple):
    name: str
    status: str
    conclusion: Optional[str]


def summarize_ci_status(
    check_runs: List[PullRequestCheckRun],
    combined_status_state: str,
    combined_status_count: int = 0,
    ignored_check_names: Tuple[str, ...] = (),
) -> str:
    ignored_check_names_set = set(ignored_check_names)
    has_completed_passing_check = False
    has_running_check = False

    for check_run in check_runs:
        if check_run.name in ignored_check_names_set:
            continue

        status = (check_run.status or "").lower()
        conclusion = (check_run.conclusion or "").lower()

        if status != "completed":
            has_running_check = True
            continue

        if conclusion in FAILING_CHECK_CONCLUSIONS:
            return CI_STATUS_FAILING
        if conclusion in PASSING_CHECK_CONCLUSIONS:
            has_completed_passing_check = True
            continue
        if conclusion:
            return CI_STATUS_FAILING

    combined_status_state = (combined_status_state or "").lower() if combined_status_count > 0 else ""
    if combined_status_state in FAILING_COMMIT_STATUS_STATES:
        return CI_STATUS_FAILING
    if has_running_check or combined_status_state in RUNNING_COMMIT_STATUS_STATES:
        return CI_STATUS_RUNNING
    if has_completed_passing_check or combined_status_state in PASSING_COMMIT_STATUS_STATES:
        return CI_STATUS_PASSING
    return CI_STATUS_UNKNOWN


class GithubBackend:
    def read_event(self) -> dict:
        raise NotImplementedError  # pragma: no cover

    def get_pr_reviews(self, pr_number: int) -> List[Review]:
        raise NotImplementedError  # pragma: no cover

    def get_pr(self, pr_number: int) -> PullRequest:
        raise NotImplementedError  # pragma: no cover

    def get_pr_ci_status(self, pr: PullRequest, ignored_check_names: Tuple[str, ...] = ()) -> str:
        raise NotImplementedError  # pragma: no cover

    def get_organization(self, org: str):
        raise NotImplementedError  # pragma: no cover

    def get_user(self, username: str):
        raise NotImplementedError  # pragma: no cover

    def get_all_requested_teams(self, org_name: str, pr_number: int) -> List:
        raise NotImplementedError  # pragma: no cover


class WebGithubBackend(GithubBackend):
    def __init__(self, gh: Github, event_path: str, repo: str) -> None:
        self._gh = gh
        self.event_path = event_path
        self.repo = repo

    def read_event(self) -> dict:
        with open(self.event_path) as f:
            return json.load(f)

    def get_pr_reviews(self, pr_number: int) -> List[Review]:
        reviews = self._gh.get_repo(self.repo).get_pull(pr_number).get_reviews()
        return [Review(state=review.state.lower(), user=review.user) for review in reviews]

    def get_pr(self, pr_number: int) -> PullRequest:
        pr = self._gh.get_repo(self.repo).get_pull(pr_number)
        return PullRequest(
            state=pr.state,
            merged=pr.merged,
            mergeable_state=pr.mergeable_state or "",
            url=pr.html_url,
            draft=pr.draft,
            head_sha=pr.head.sha,
            head_repo_fork=pr.head.repo.fork,
            head_owner_login=pr.head.repo.owner.login,
        )

    def get_pr_ci_status(self, pr: PullRequest, ignored_check_names: Tuple[str, ...] = ()) -> str:
        if not pr.head_sha:
            return CI_STATUS_UNKNOWN

        commit = self._gh.get_repo(self.repo).get_commit(pr.head_sha)
        check_runs = [
            PullRequestCheckRun(name=check_run.name, status=check_run.status or "", conclusion=check_run.conclusion)
            for check_run in commit.get_check_runs()
        ]
        combined_status = commit.get_combined_status()
        return summarize_ci_status(
            check_runs, combined_status.state, combined_status.total_count or 0, ignored_check_names
        )

    def get_organization(self, org: str):
        return self._gh.get_organization(org)

    def get_user(self, username: str):
        return self._gh.get_user(username)

    def get_all_requested_teams(self, org_name: str, pr_number: int) -> List:
        """Get all teams ever requested for review using the Timeline API."""
        teams = {}
        pr = self._gh.get_repo(self.repo).get_pull(pr_number)
        org = self.get_organization(org_name)
        for event in pr.get_issue_events():
            if event.event == "review_requested" and "requested_team" in event.raw_data:
                slug = event.raw_data["requested_team"]["slug"]
                if slug not in teams:
                    teams[slug] = org.get_team_by_slug(slug)
        return list(teams.values())


class GithubClient:
    def __init__(self, backend: GithubBackend) -> None:
        self._backend = backend

    def read_event(self) -> dict:
        return self._backend.read_event()

    def get_pr_reviews(self, pr_number: int) -> List[Review]:
        return self._backend.get_pr_reviews(pr_number)

    def get_pr(self, pr_number: int) -> PullRequest:
        return self._backend.get_pr(pr_number)

    def get_pr_ci_status(self, pr: PullRequest, ignored_check_names: Tuple[str, ...] = ()) -> str:
        return self._backend.get_pr_ci_status(pr, ignored_check_names)

    def get_organization(self, org: str):
        return self._backend.get_organization(org)

    def get_user(self, username: str):
        return self._backend.get_user(username)

    def get_all_requested_teams(self, org_name: str, pr_number: int) -> List:
        return self._backend.get_all_requested_teams(org_name, pr_number)
