from types import SimpleNamespace

import pytest

import slapr
from slapr.config import Config
from slapr.github import GithubClient, PullRequest, Review, WebGithubBackend
from slapr.slack import Message, Reaction, SlackClient
from tests.test_slapr import MOCK_EVENT, MockGithubBackend, MockSlackBackend, MockTeam, MockUser


@pytest.mark.parametrize(
    "states, unresolved, pr_state, merged, required, expected",
    [
        pytest.param(["commented"], {"reviewer"}, "open", False, 1, "added-comments", id="before-approval"),
        pytest.param(
            ["commented", "approved"],
            {"reviewer"},
            "open",
            False,
            1,
            "approved-with-comments",
            id="separate-comment-then-approval",
        ),
        pytest.param(
            ["approved", "commented"],
            {"reviewer"},
            "open",
            False,
            1,
            "approved-with-comments",
            id="comment-after-approval",
        ),
        pytest.param(
            ["commented", "approved"], set(), "open", False, 1, "white_check_mark", id="resolved-after-approval"
        ),
        pytest.param(["commented"], set(), "open", False, 1, None, id="resolved-before-approval"),
        pytest.param(
            ["approved", "dismissed"], {"reviewer"}, "open", False, 1, "added-comments", id="dismissed-approval"
        ),
        pytest.param(["approved"], {"reviewer"}, "open", False, 2, "added-comments", id="insufficient-approvals"),
        pytest.param(
            ["changes_requested", "commented"], {"reviewer"}, "open", False, 1, "no_entry", id="changes-requested-wins"
        ),
        pytest.param(["approved"], {"reviewer"}, "closed", True, 1, "github-merged", id="merge-wins"),
        pytest.param(["approved"], {"reviewer"}, "closed", False, 1, "github-closed", id="close-wins"),
    ],
)
def test_should_update_only_opted_in_channel(states, unresolved, pr_state, merged, required, expected) -> None:
    message = Message(text=f'<{MOCK_EVENT["pull_request"]["html_url"]}>', timestamp="review-post")
    slack = MockSlackBackend(
        messages=[],
        target_message=message,
        reactions=[],
        channel_messages={"C_DOMAINS": [message], "C_CHAT": [message]},
        channel_reactions={
            "C_DOMAINS": [
                Reaction(
                    emoji="white_check_mark" if expected == "approved-with-comments" else "approved-with-comments",
                    user_ids=["U_BOT"],
                )
            ],
            "C_CHAT": [],
        },
    )
    github = MockGithubBackend(
        reviews=[Review(state, MockUser("reviewer")) for state in states],
        event=MOCK_EVENT,
        pr=PullRequest(pr_state, merged, "clean"),
        unresolved_review_authors=unresolved,
    )
    config = Config(
        slack_client=SlackClient(backend=slack),
        github_client=GithubClient(backend=github),
        slack_channel_ids=["C_DOMAINS", "C_CHAT"],
        slapr_bot_user_id="U_BOT",
        number_of_approvals_required=required,
        emoji_review_started="",
        emoji_approved="white_check_mark",
        emoji_needs_change="no_entry",
        emoji_merged="github-merged",
        emoji_closed="github-closed",
        emoji_commented="",
        emoji_approved_with_comments="approved-with-comments",
        emoji_unresolved_comments="added-comments",
        comment_status_channel_ids=["C_DOMAINS"],
    )

    slapr.main(config)

    assert slack.channel_emojis["C_DOMAINS"] == ([expected] if expected else [])
    # The other channel retains the pre-feature verdict, including no comments reaction.
    ordinary = {"approved-with-comments": "white_check_mark", "added-comments": None}.get(expected, expected)
    assert slack.channel_emojis["C_CHAT"] == ([ordinary] if ordinary else [])


def test_should_filter_resolved_bot_and_author_threads_across_pages() -> None:
    def thread(login, kind="User", resolved=False):
        author = {"login": login, "__typename": kind} if login else None
        return {"isResolved": resolved, "comments": {"nodes": [{"author": author}]}}

    class Requester:
        def graphql_query(self, query, variables):
            pages = {
                None: (
                    [thread("reviewer", resolved=True), thread("bot", "Bot"), thread("pr-author"), thread(None)],
                    True,
                    "page-two",
                ),
                "page-two": ([thread("another-reviewer"), thread("another-reviewer")], False, None),
            }
            nodes, has_next, cursor = pages[variables["cursor"]]
            return {}, {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "author": {"login": "pr-author"},
                            "reviewThreads": {
                                "nodes": nodes,
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                            },
                        }
                    }
                }
            }

    backend = WebGithubBackend(SimpleNamespace(requester=Requester()), "unused", "example/repo")

    assert backend.get_unresolved_review_authors(42) == {"another-reviewer"}


@pytest.mark.parametrize(
    "unresolved, expected",
    [
        pytest.param(set(), "white_check_mark", id="resolved"),
        pytest.param({"reviewer"}, "approved-with-comments", id="reopened"),
    ],
)
def test_should_refresh_recent_posts_when_conversations_change(unresolved, expected) -> None:
    from slapr.refresh import refresh

    message = Message(text=f'<{MOCK_EVENT["pull_request"]["html_url"]}#top|Review>', timestamp="review-post")
    other_repo = Message(text="<https://github.com/other/repo/pull/99>", timestamp="other-repo")
    slack = MockSlackBackend(
        messages=[],
        target_message=message,
        reactions=[],
        channel_messages={"C_DOMAINS": [message, other_repo]},
        channel_reactions={
            "C_DOMAINS": [Reaction("approved-with-comments" if not unresolved else "white_check_mark", ["U_BOT"])]
        },
    )
    github = MockGithubBackend(
        reviews=[Review("approved", MockUser("reviewer"))],
        event=MOCK_EVENT,
        pr=PullRequest("open", False, "clean"),
        unresolved_review_authors=unresolved,
    )
    config = Config(
        slack_client=SlackClient(backend=slack),
        github_client=GithubClient(backend=github),
        slack_channel_ids=["C_DOMAINS", "C_OTHER"],
        slapr_bot_user_id="U_BOT",
        number_of_approvals_required=1,
        emoji_review_started="",
        emoji_approved="white_check_mark",
        emoji_needs_change="no_entry",
        emoji_merged="github-merged",
        emoji_closed="github-closed",
        emoji_commented="",
        emoji_approved_with_comments="approved-with-comments",
        comment_status_channel_ids=["C_DOMAINS"],
    )

    refresh(config, "example/repo")

    assert slack.channel_emojis == {"C_DOMAINS": [expected]}


def test_should_require_explicit_refresh_channels() -> None:
    from slapr.refresh import refresh

    config = Config(None, None, ["C_DOMAINS"], "U_BOT", 1, "", "approved", "", "", "", "")
    with pytest.raises(ValueError, match="requires explicit"):
        refresh(config, "example/repo")
    with pytest.raises(ValueError, match="is not in SLACK_CHANNEL_ID"):
        refresh(config._replace(comment_status_channel_ids=["C_OTHER"]), "example/repo")


def test_should_ignore_other_team_comments_and_disabled_feature() -> None:
    from slapr.emojis import select

    config = Config(None, None, [], "U_BOT", 1, "", "approved", "", "", "", "")
    reviews = [Review("approved", MockUser("alice")), Review("commented", MockUser("bob"))]
    assert select([], reviews, config, 1, unresolved_review_authors={"bob"}) == "approved"
    enabled = config._replace(emoji_approved_with_comments="approved-with-comments")
    assert select([], reviews, enabled, 1, unresolved_review_authors={"bob"}) == "approved-with-comments"
    assert select([MockTeam("team", ["alice"])], reviews, enabled, 1, unresolved_review_authors={"bob"}) == "approved"
