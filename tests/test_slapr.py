# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

from typing import Dict, List, Optional, Set, Tuple

import pytest

import slapr
from slapr.config import Config
from slapr.github import (
    CI_STATUS_FAILING,
    CI_STATUS_PASSING,
    CI_STATUS_RUNNING,
    CI_STATUS_UNKNOWN,
    GithubBackend,
    GithubClient,
    PullRequest,
    Review,
)
from slapr.review_map import ReviewMap
from slapr.slack import Message, Reaction, SlackBackend, SlackClient


# --- Mock GitHub objects (stand-ins for PyGithub types) ---


class MockUser:
    def __init__(self, login: str):
        self.login = login

    def __repr__(self):
        return f"MockUser({self.login!r})"


class MockTeam:
    def __init__(self, slug: str, members: List[str], organization: "MockOrganization" = None):
        self.slug = slug
        self._members = members
        self.organization = organization

    def has_in_members(self, user) -> bool:
        return user.login in self._members


class MockOrganization:
    def __init__(self, login: str, teams: Dict[str, MockTeam]):
        self.login = login
        self._teams = teams

    def get_team_by_slug(self, slug: str) -> MockTeam:
        return self._teams[slug]


# --- Mock backends ---


class MockSlackBackend(SlackBackend):
    def __init__(
        self,
        messages: List[Message],
        target_message: Message,
        reactions: List[Reaction],
        channel_messages: Optional[Dict[str, List[Message]]] = None,
        channel_reactions: Optional[Dict[str, List[Reaction]]] = None,
    ) -> None:
        self.messages = messages
        self.target_message = target_message
        self.reactions = reactions
        self.emojis = [reaction.emoji for reaction in reactions]  # Retain order.
        # Multi-channel support: per-channel messages and emoji tracking
        self.channel_messages = channel_messages or {}
        self.channel_emojis: Dict[str, List[str]] = {}
        self._channel_reactions: Dict[str, List[Reaction]] = channel_reactions or {}
        if channel_reactions:
            for ch_id, ch_reactions in channel_reactions.items():
                self.channel_emojis[ch_id] = [r.emoji for r in ch_reactions]

    def get_latest_messages(self, channel_id: str) -> List[Message]:
        if channel_id in self.channel_messages:
            return self.channel_messages[channel_id]
        return self.messages

    def get_reactions(self, timestamp: str, channel_id: str) -> List[Reaction]:
        if channel_id in self._channel_reactions:
            return list(self._channel_reactions[channel_id])
        return list(self.reactions)

    def add_reaction(self, timestamp: str, emoji: str, channel_id: str) -> None:
        if channel_id in self.channel_emojis:
            emojis_list = self.channel_emojis[channel_id]
            if emoji in emojis_list:
                raise RuntimeError(f"Emoji already present: {emoji!r}")
            emojis_list.append(emoji)
        else:
            if emoji in self.emojis:
                raise RuntimeError(f"Emoji already present: {emoji!r}")  # Mimick behavior of real Slack.
            self.emojis.append(emoji)

    def remove_reaction(self, timestamp: str, emoji: str, channel_id: str) -> None:
        if channel_id in self.channel_emojis:
            emojis_list = self.channel_emojis[channel_id]
            if emoji in emojis_list:
                emojis_list.remove(emoji)
        else:
            if emoji not in self.emojis:
                return  # Mimick behavior of real Slack.
            self.emojis.remove(emoji)

    def resolve_channel_names(self, names: Set[str]) -> Dict[str, str]:
        return {}


class MockGithubBackend(GithubBackend):
    def __init__(
        self,
        reviews: List[Review],
        event: dict,
        pr: PullRequest,
        team_members: Optional[Dict[str, List[str]]] = None,
        requested_teams_timeline: Optional[List[str]] = None,
        ci_status: str = CI_STATUS_UNKNOWN,
    ) -> None:
        self.reviews = reviews
        self.event = event
        self.pr = pr
        self.pr_number = _event_pr_number(event)
        self.ci_status = ci_status
        # team_members: {"team-slug": ["user1", "user2"]}
        self.team_members = team_members or {}
        # requested_teams_timeline: list of team slugs from timeline API
        self.requested_teams_timeline = requested_teams_timeline or []

    def read_event(self) -> dict:
        return self.event

    def get_pr(self, pr_number: int) -> PullRequest:
        assert pr_number == self.pr_number
        return self.pr

    def get_pr_reviews(self, pr_number: int) -> List[Review]:
        assert pr_number == self.pr_number
        return list(self.reviews)

    def get_pr_ci_status(self, pr: PullRequest, ignored_check_names: Tuple[str, ...] = ()) -> str:
        assert pr == self.pr
        return self.ci_status

    def get_organization(self, org: str):
        teams = {
            slug: MockTeam(slug, members)
            for slug, members in self.team_members.items()
        }
        mock_org = MockOrganization(org, teams)
        # Back-link teams to their org (PyGithub Team objects have .organization)
        for team in teams.values():
            team.organization = mock_org
        return mock_org

    def get_user(self, username: str):
        return MockUser(username)

    def get_all_requested_teams(self, org_name: str, pr_number: int) -> List:
        org = self.get_organization(org_name)
        return [org.get_team_by_slug(slug) for slug in self.requested_teams_timeline]


# --- Helper to create mock users for Review construction ---


def _user(login: str) -> MockUser:
    return MockUser(login)


def _event_pr_number(event: dict) -> int:
    if "pull_request" in event:
        return event["pull_request"]["number"]
    return event["workflow_run"]["pull_requests"][0]["number"]


# --- Test data ---


MOCK_EVENT = {
    "pull_request": {
        "number": 42,
        "html_url": "https://github.com/example/repo/pull/42",
        "head": {"repo": {"fork": False, "owner": {"login": "datadog"}}},
    },
    "review": {
        "user": {"login": "alice"},
    },
}

MOCK_WORKFLOW_RUN_EVENT = {
    "action": "completed",
    "workflow_run": {
        "pull_requests": [
            {
                "number": 42,
            }
        ],
    },
}


@pytest.mark.parametrize(
    "messages, reviews, reactions, expected_emojis",
    [
        pytest.param(
            [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")],
            [Review(state="approved", user=_user("alice"))],
            [],
            ["test_review_started", "test_approved"],
            id="approval",
        ),
        pytest.param(
            [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")],
            [Review(state="changes_requested", user=_user("alice"))],
            [],
            ["test_review_started", "test_needs_change"],
            id="changes_requested",
        ),
        pytest.param(
            [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")],
            [Review(state="commented", user=_user("alice"))],
            [],
            ["test_review_started", "test_commented"],
            id="comment",
        ),
        pytest.param(
            [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")],
            [Review(state="changes_requested", user=_user("alice")), Review(state="approved", user=_user("alice"))],
            [Reaction(emoji="test_needs_change", user_ids=["U1234"])],
            ["test_review_started", "test_approved"],
            id="approved-from-changes-requested",
        ),
        pytest.param(
            [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")],
            [Review(state="commented", user=_user("alice")), Review(state="approved", user=_user("alice"))],
            [Reaction(emoji="test_commented", user_ids=["U1234"])],
            ["test_review_started", "test_approved"],
            id="approved-from-commented",
        ),
        pytest.param(
            [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")],
            [Review(state="changes_requested", user=_user("alice")), Review(state="commented", user=_user("alice"))],
            [],
            ["test_review_started", "test_commented"],
            id="commented-after-changes-requested-same-reviewer",
        ),
        pytest.param(
            [Message(text="Need :eyes: but I've got no PR URL", timestamp="yyyy-mm-dd")],
            [Review(state="approved", user=_user("alice"))],
            [],
            [],
            id="message-not-found",
        ),
    ],
)
def test_on_pull_request_review(
    messages: List[Message], reviews: List[Review], reactions: List[Reaction], expected_emojis: set
) -> None:
    slack_backend = MockSlackBackend(messages=messages, target_message=messages[0], reactions=reactions)
    github_backend = MockGithubBackend(
        reviews=reviews,
        event=MOCK_EVENT,
        pr=PullRequest(state="open", merged=False, mergeable_state="clean"),
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C1234"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
    )
    slapr.main(config)

    assert slack_backend.emojis == expected_emojis


@pytest.mark.parametrize(
    "event, pr, reactions, expected_emojis",
    [
        pytest.param(
            MOCK_EVENT,
            PullRequest(state="closed", merged=True, mergeable_state="clean"),
            [
                Reaction(emoji="test_review_started", user_ids=["U1234"]),
                Reaction(emoji="test_approved", user_ids=["U1234"]),
            ],
            ["test_approved", "test_merged"],
            id="merge-approved-pr",
        ),
        pytest.param(
            MOCK_EVENT,
            PullRequest(state="closed", merged=False, mergeable_state="clean"),
            [
                Reaction(emoji="test_review_started", user_ids=["U1234"]),
                Reaction(emoji="test_approved", user_ids=["U1234"]),
            ],
            ["test_approved", "test_closed"],
            id="close-approved-pr",
        ),
    ],
)
def test_on_pull_request(event: dict, pr: PullRequest, reactions: List[Reaction], expected_emojis: set) -> None:
    messages = [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")]
    reviews = [Review(state="approved", user=_user("alice"))]

    slack_backend = MockSlackBackend(messages=messages, target_message=messages[0], reactions=reactions)
    github_backend = MockGithubBackend(
        reviews=reviews,
        event=event,
        pr=pr,
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C1234"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
    )
    slapr.main(config)

    assert slack_backend.emojis == expected_emojis


@pytest.mark.parametrize(
    "pr, event_action, existing_reactions, expected_emojis",
    [
        pytest.param(
            PullRequest(state="open", merged=False, mergeable_state="clean"),
            "",
            [Reaction(emoji="test_draft", user_ids=["U1234"])],
            ["test_open"],
            id="open",
        ),
        pytest.param(
            PullRequest(state="open", merged=False, mergeable_state="clean", draft=True),
            "",
            [Reaction(emoji="test_open", user_ids=["U1234"])],
            ["test_draft"],
            id="draft",
        ),
        pytest.param(
            PullRequest(state="open", merged=False, mergeable_state="clean"),
            "enqueued",
            [Reaction(emoji="test_open", user_ids=["U1234"])],
            ["test_queue"],
            id="queued",
        ),
        pytest.param(
            PullRequest(state="closed", merged=True, mergeable_state="clean"),
            "",
            [Reaction(emoji="test_open", user_ids=["U1234"])],
            ["test_merged"],
            id="merged",
        ),
        pytest.param(
            PullRequest(state="closed", merged=False, mergeable_state="clean"),
            "",
            [Reaction(emoji="test_open", user_ids=["U1234"])],
            ["test_closed"],
            id="closed",
        ),
    ],
)
def test_pr_state_emoji_replaces_previous_state(
    pr: PullRequest,
    event_action: str,
    existing_reactions: List[Reaction],
    expected_emojis: List[str],
) -> None:
    messages = [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")]

    slack_backend = MockSlackBackend(messages=messages, target_message=messages[0], reactions=existing_reactions)
    github_backend = MockGithubBackend(
        reviews=[],
        event={**MOCK_EVENT, "action": event_action},
        pr=pr,
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C1234"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        emoji_open="test_open",
        emoji_draft="test_draft",
        emoji_queue="test_queue",
    )
    slapr.main(config)

    assert slack_backend.emojis == expected_emojis


@pytest.mark.parametrize(
    "ci_status, existing_reaction, expected_emojis",
    [
        pytest.param(CI_STATUS_RUNNING, "test_ci_failing", ["test_ci_running"], id="running"),
        pytest.param(CI_STATUS_FAILING, "test_ci_running", ["test_ci_failing"], id="failing"),
        pytest.param(CI_STATUS_PASSING, "test_ci_running", ["test_ci_passing"], id="passing"),
        pytest.param(CI_STATUS_UNKNOWN, "test_ci_running", [], id="unknown"),
    ],
)
def test_ci_status_emoji(ci_status: str, existing_reaction: str, expected_emojis: List[str]) -> None:
    messages = [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")]

    slack_backend = MockSlackBackend(
        messages=messages,
        target_message=messages[0],
        reactions=[Reaction(emoji=existing_reaction, user_ids=["U1234"])],
    )
    github_backend = MockGithubBackend(
        reviews=[],
        event=MOCK_EVENT,
        pr=PullRequest(state="open", merged=False, mergeable_state="clean"),
        ci_status=ci_status,
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C1234"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        emoji_ci_running="test_ci_running",
        emoji_ci_failing="test_ci_failing",
        emoji_ci_passing="test_ci_passing",
    )
    slapr.main(config)

    assert slack_backend.emojis == expected_emojis


def test_on_workflow_run_updates_pr_and_ci_status_emojis() -> None:
    messages = [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")]

    slack_backend = MockSlackBackend(
        messages=messages,
        target_message=messages[0],
        reactions=[Reaction(emoji="test_ci_failing", user_ids=["U1234"])],
    )
    github_backend = MockGithubBackend(
        reviews=[],
        event=MOCK_WORKFLOW_RUN_EVENT,
        pr=PullRequest(
            state="open",
            merged=False,
            mergeable_state="clean",
            url="https://github.com/example/repo/pull/42",
            head_repo_fork=True,
            head_owner_login="datadog",
            head_repo_full_name="example/repo",
            base_repo_full_name="example/repo",
        ),
        ci_status=CI_STATUS_RUNNING,
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C1234"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        emoji_open="test_open",
        emoji_draft="test_draft",
        emoji_queue="test_queue",
        emoji_ci_running="test_ci_running",
        emoji_ci_failing="test_ci_failing",
        emoji_ci_passing="test_ci_passing",
    )
    slapr.main(config)

    assert slack_backend.emojis == ["test_open", "test_ci_running"]


def test_external_fork_pr_is_skipped() -> None:
    event = {
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/example/repo/pull/42",
            "head": {
                "repo": {
                    "fork": True,
                    "full_name": "someone/repo",
                    "owner": {"login": "someone"},
                }
            },
            "base": {"repo": {"full_name": "example/repo"}},
        },
    }
    messages = [Message(text="Need :eyes: <https://github.com/example/repo/pull/42>", timestamp="yyyy-mm-dd")]

    slack_backend = MockSlackBackend(messages=messages, target_message=messages[0], reactions=[])
    github_backend = MockGithubBackend(
        reviews=[],
        event=event,
        pr=PullRequest(
            state="open",
            merged=False,
            mergeable_state="clean",
            url="https://github.com/example/repo/pull/42",
            head_repo_fork=True,
            head_repo_full_name="someone/repo",
            base_repo_full_name="example/repo",
        ),
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C1234"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        emoji_open="test_open",
    )
    slapr.main(config)

    assert slack_backend.emojis == []


# --- Review Map integration tests ---

MOCK_EVENT_WITH_TEAMS = {
    "pull_request": {
        "number": 42,
        "html_url": "https://github.com/example/repo/pull/42",
        "head": {"repo": {"fork": False, "owner": {"login": "datadog"}}},
        "requested_teams": [{"slug": "agent-apm"}],
    },
    "review": {
        "user": {"login": "alice"},
    },
}

MOCK_EVENT_MERGE_WITH_OWNER = {
    "pull_request": {
        "number": 42,
        "html_url": "https://github.com/example/repo/pull/42",
        "head": {"repo": {"fork": False, "owner": {"login": "datadog"}}},
        "state": "closed",
        "requested_teams": [],
    },
}


def test_review_with_review_map_routes_to_team_channel():
    """When a reviewer is a member of a mapped team, emoji goes to that team's channel."""
    messages = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts-apm")]

    slack_backend = MockSlackBackend(
        messages=[],
        target_message=messages[0],
        reactions=[],
        channel_messages={"C_APM": messages},
        channel_reactions={"C_APM": []},
    )
    github_backend = MockGithubBackend(
        reviews=[Review(state="approved", user=_user("alice"))],
        event=MOCK_EVENT_WITH_TEAMS,
        pr=PullRequest(state="open", merged=False, mergeable_state="clean"),
        team_members={"agent-apm": ["alice"]},
        requested_teams_timeline=["agent-apm"],
    )

    review_map = ReviewMap(
        team_to_channel={"@datadog/agent-apm": "C_APM"},
        default_channel_id="C_DEFAULT",
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C_DEFAULT"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        review_map=review_map,
    )
    slapr.main(config)

    assert slack_backend.channel_emojis["C_APM"] == ["test_review_started", "test_approved"]


def test_review_with_review_map_falls_back_to_default():
    """When no team matches, falls back to the default channel."""
    messages = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts-default")]

    event_no_teams = {
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/example/repo/pull/42",
            "head": {"repo": {"fork": False, "owner": {"login": "datadog"}}},
            "requested_teams": [],
        },
        "review": {
            "user": {"login": "bob"},
        },
    }

    slack_backend = MockSlackBackend(
        messages=messages,
        target_message=messages[0],
        reactions=[],
    )
    github_backend = MockGithubBackend(
        reviews=[Review(state="approved", user=_user("bob"))],
        event=event_no_teams,
        pr=PullRequest(state="open", merged=False, mergeable_state="clean"),
    )

    review_map = ReviewMap(
        team_to_channel={"@datadog/agent-apm": "C_APM"},
        default_channel_id="C_DEFAULT",
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C_DEFAULT"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        review_map=review_map,
    )
    slapr.main(config)

    # Falls back to default channel (tracked in self.emojis)
    assert slack_backend.emojis == ["test_review_started", "test_approved"]


def test_merge_with_review_map_targets_requested_team_channels():
    """On merge, emoji is applied to channels of all teams that were ever requested."""
    messages_apm = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts-apm")]

    slack_backend = MockSlackBackend(
        messages=[],
        target_message=messages_apm[0],
        reactions=[],
        channel_messages={"C_APM": messages_apm},
        channel_reactions={"C_APM": [Reaction(emoji="test_review_started", user_ids=["U1234"])]},
    )
    github_backend = MockGithubBackend(
        reviews=[Review(state="approved", user=_user("alice"))],
        event=MOCK_EVENT_MERGE_WITH_OWNER,
        pr=PullRequest(state="closed", merged=True, mergeable_state="clean"),
        requested_teams_timeline=["agent-apm"],
        team_members={"agent-apm": ["alice"]},
    )

    review_map = ReviewMap(
        team_to_channel={"@datadog/agent-apm": "C_APM"},
        default_channel_id="C_DEFAULT",
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C_DEFAULT"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        review_map=review_map,
    )
    slapr.main(config)

    assert slack_backend.channel_emojis["C_APM"] == ["test_approved", "test_merged"]


def test_review_map_uses_reviewer_state_only():
    """Only reviews from the reviewer's team members determine the emoji."""
    messages = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts-apm")]

    event = {
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/example/repo/pull/42",
            "head": {"repo": {"fork": False, "owner": {"login": "datadog"}}},
            "requested_teams": [{"slug": "agent-apm"}],
        },
        "review": {
            "user": {"login": "bob"},  # bob is a team member, submitting a comment
        },
    }

    slack_backend = MockSlackBackend(
        messages=[],
        target_message=messages[0],
        reactions=[],
        channel_messages={"C_APM": messages},
        channel_reactions={"C_APM": []},
    )
    github_backend = MockGithubBackend(
        # alice approved (not a team member), bob commented (team member)
        reviews=[Review(state="approved", user=_user("alice")), Review(state="commented", user=_user("bob"))],
        event=event,
        pr=PullRequest(state="open", merged=False, mergeable_state="clean"),
        team_members={"agent-apm": ["bob"]},  # only bob is in agent-apm
        requested_teams_timeline=["agent-apm"],
    )

    review_map = ReviewMap(
        team_to_channel={"@datadog/agent-apm": "C_APM"},
        default_channel_id="C_DEFAULT",
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C_DEFAULT"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        review_map=review_map,
    )
    slapr.main(config)

    # Only bob's review (commented) counts — alice's approval is ignored
    assert slack_backend.channel_emojis["C_APM"] == ["test_review_started", "test_commented"]


def test_review_started_broadcast_to_all_requested_team_channels():
    """When a reviewer from team-A approves, review_started should also appear
    on team-B's channel if team-B was requested for review."""
    messages_apm = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts-apm")]
    messages_build = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts-build")]

    event = {
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/example/repo/pull/42",
            "head": {"repo": {"fork": False, "owner": {"login": "datadog"}}},
            "requested_teams": [{"slug": "agent-apm"}, {"slug": "agent-build"}],
        },
        "review": {
            "user": {"login": "alice"},
        },
    }

    slack_backend = MockSlackBackend(
        messages=[],
        target_message=messages_apm[0],
        reactions=[],
        channel_messages={"C_APM": messages_apm, "C_BUILD": messages_build},
        channel_reactions={"C_APM": [], "C_BUILD": []},
    )
    github_backend = MockGithubBackend(
        reviews=[Review(state="approved", user=_user("alice"))],
        event=event,
        pr=PullRequest(state="open", merged=False, mergeable_state="clean"),
        team_members={"agent-apm": ["alice"], "agent-build": ["bob"]},
        requested_teams_timeline=["agent-apm", "agent-build"],
    )

    review_map = ReviewMap(
        team_to_channel={"@datadog/agent-apm": "C_APM", "@datadog/agent-build": "C_BUILD"},
        default_channel_id="C_DEFAULT",
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C_DEFAULT"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
        review_map=review_map,
    )
    slapr.main(config)

    # alice is in agent-apm: full review status
    assert slack_backend.channel_emojis["C_APM"] == ["test_review_started", "test_approved"]
    # agent-build was also requested: should get review_started even though alice is not a member
    assert slack_backend.channel_emojis["C_BUILD"] == ["test_review_started"]


# --- Multi-channel + empty-emoji-disable tests ---


def test_multi_channel_ids_post_to_each():
    """Without a review map, each channel in slack_channel_ids gets the same emojis."""
    messages = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts")]

    slack_backend = MockSlackBackend(
        messages=[],
        target_message=messages[0],
        reactions=[],
        channel_messages={"C_ONE": messages, "C_TWO": messages},
        channel_reactions={"C_ONE": [], "C_TWO": []},
    )
    github_backend = MockGithubBackend(
        reviews=[Review(state="approved", user=_user("alice"))],
        event=MOCK_EVENT,
        pr=PullRequest(state="open", merged=False, mergeable_state="clean"),
    )

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C_ONE", "C_TWO"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        emoji_review_started="test_review_started",
        emoji_approved="test_approved",
        emoji_needs_change="test_needs_change",
        emoji_merged="test_merged",
        emoji_closed="test_closed",
        emoji_commented="test_commented",
    )
    slapr.main(config)

    assert slack_backend.channel_emojis["C_ONE"] == ["test_review_started", "test_approved"]
    assert slack_backend.channel_emojis["C_TWO"] == ["test_review_started", "test_approved"]


@pytest.mark.parametrize(
    "emoji_overrides, reviews, pr, expected_emojis",
    [
        pytest.param(
            {"emoji_review_started": ""},
            [Review(state="approved", user=_user("alice"))],
            PullRequest(state="open", merged=False, mergeable_state="clean"),
            ["test_approved"],
            id="empty-review-started-skipped",
        ),
        pytest.param(
            {"emoji_approved": ""},
            [Review(state="approved", user=_user("alice"))],
            PullRequest(state="open", merged=False, mergeable_state="clean"),
            ["test_review_started"],
            id="empty-approved-skipped",
        ),
        pytest.param(
            {"emoji_needs_change": ""},
            [Review(state="changes_requested", user=_user("alice"))],
            PullRequest(state="open", merged=False, mergeable_state="clean"),
            ["test_review_started"],
            id="empty-needs-change-skipped",
        ),
        pytest.param(
            {"emoji_commented": ""},
            [Review(state="commented", user=_user("alice"))],
            PullRequest(state="open", merged=False, mergeable_state="clean"),
            ["test_review_started"],
            id="empty-commented-skipped",
        ),
        pytest.param(
            {"emoji_merged": ""},
            [Review(state="approved", user=_user("alice"))],
            PullRequest(state="closed", merged=True, mergeable_state="clean"),
            ["test_approved"],
            id="empty-merged-skipped",
        ),
        pytest.param(
            {"emoji_closed": ""},
            [Review(state="approved", user=_user("alice"))],
            PullRequest(state="closed", merged=False, mergeable_state="clean"),
            ["test_approved"],
            id="empty-closed-skipped",
        ),
    ],
)
def test_empty_emoji_is_skipped(emoji_overrides, reviews, pr, expected_emojis):
    messages = [Message(text="Need review <https://github.com/example/repo/pull/42>", timestamp="ts")]
    slack_backend = MockSlackBackend(messages=messages, target_message=messages[0], reactions=[])
    github_backend = MockGithubBackend(reviews=reviews, event=MOCK_EVENT, pr=pr)

    base_emojis = {
        "emoji_review_started": "test_review_started",
        "emoji_approved": "test_approved",
        "emoji_needs_change": "test_needs_change",
        "emoji_merged": "test_merged",
        "emoji_closed": "test_closed",
        "emoji_commented": "test_commented",
    }
    base_emojis.update(emoji_overrides)

    config = Config(
        slack_client=SlackClient(backend=slack_backend),
        github_client=GithubClient(backend=github_backend),
        slack_channel_ids=["C1234"],
        slapr_bot_user_id="U1234",
        number_of_approvals_required=1,
        **base_emojis,
    )
    slapr.main(config)

    assert sorted(slack_backend.emojis) == sorted(expected_emojis)
