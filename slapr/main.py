# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

from collections import defaultdict
from typing import Dict, List, NamedTuple, Optional, Set

from . import emojis
from .config import Config
from .github import GithubClient, PullRequest
from .slack import SlackClient


class PullRequestEventContext(NamedTuple):
    pr_number: int
    action: str
    reviewer_login: Optional[str]
    is_status_update: bool


def main(config: Config) -> None:
    slack = config.slack_client
    github = config.github_client

    event = github.read_event()
    context = _get_pull_request_context(event)
    if context is None:
        print("Event does not reference a pull request.")
        return

    pr = github.get_pr(pr_number=context.pr_number)

    event_pull_request = event.get("pull_request", {})
    event_head_repo = event_pull_request.get("head", {}).get("repo", {})
    is_fork = pr.head_repo_fork or event_head_repo.get("fork", False)

    if is_fork:
        print("Fork PRs are not supported.")
        return

    reviews = github.get_pr_reviews(pr_number=context.pr_number)
    pr_url: str = pr.url or event_pull_request.get("html_url", "")
    ci_status = (
        github.get_pr_ci_status(pr, ignored_check_names=config.ignored_ci_check_names)
        if config.has_ci_status_emojis
        else ""
    )
    print(f"Event PR: {pr_url} - state: {pr.state} - merged: {pr.merged} - CI: {ci_status or 'disabled'}")

    # Determine target channels (with optional team slug for filtering)
    if config.review_map is not None:
        org_name = pr.head_owner_login or event_head_repo.get("owner", {}).get("login")
        requested_teams = github.get_all_requested_teams(org_name, context.pr_number)
        reviewer_login = context.reviewer_login
        reviewer = github.get_user(reviewer_login) if reviewer_login else None
        target_channels = _resolve_target_channels(
            config,
            requested_teams,
            pr,
            reviewer,
            target_all_requested_channels=context.is_status_update,
        )
    else:
        requested_teams = []
        target_channels = {ch: [] for ch in config.slack_channel_ids}

    for channel_id, teams in target_channels.items():

        review_emoji = emojis.select(
            teams,
            reviews,
            config,
            number_of_approvals_required=config.number_of_approvals_required,
        )

        new_emojis: Set[str] = set()
        if pr.state != "closed" and reviews and config.emoji_review_started:
            new_emojis.add(config.emoji_review_started)
        if review_emoji:
            new_emojis.add(review_emoji)

        pr_state_emoji = emojis.select_pr_state(pr, config, event_action=context.action)
        if pr_state_emoji:
            new_emojis.add(pr_state_emoji)

        ci_status_emoji = emojis.select_ci_status(ci_status, config)
        if ci_status_emoji:
            new_emojis.add(ci_status_emoji)

        _apply_emojis_to_channel(config, slack, new_emojis, pr_url, channel_id)

    # Broadcast review_started to ALL requested team channels, not just the reviewer's.
    # Only on the first review (len==1) — subsequent reviews already have review_started.
    if (
        config.review_map is not None
        and not pr.merged
        and pr.state != "closed"
        and len(reviews) == 1
        and config.emoji_review_started
        and not context.is_status_update
    ):
        all_channels = config.review_map.get_channels_for_requested_teams(requested_teams)
        already_processed = set(target_channels.keys())
        for channel_id in all_channels - already_processed:
            print(f"Broadcasting review_started to channel {channel_id}")
            _apply_emojis_to_channel(
                config, slack, {config.emoji_review_started}, pr_url, channel_id
            )


def _get_pull_request_context(event: dict) -> Optional[PullRequestEventContext]:
    pull_request = event.get("pull_request")
    if pull_request is not None:
        return PullRequestEventContext(
            pr_number=pull_request["number"],
            action=event.get("action", ""),
            reviewer_login=event.get("review", {}).get("user", {}).get("login"),
            is_status_update=False,
        )

    workflow_run = event.get("workflow_run")
    if workflow_run is not None:
        pull_requests = workflow_run.get("pull_requests", [])
        if not pull_requests:
            return None
        return PullRequestEventContext(
            pr_number=pull_requests[0]["number"],
            action=event.get("action", ""),
            reviewer_login=None,
            is_status_update=True,
        )

    return None


def _resolve_target_channels(
    config: Config,
    requested_teams: List,
    pr: PullRequest,
    reviewer,
    target_all_requested_channels: bool = False,
) -> Dict[str, List]:
    """Determine which Slack channels to target based on the review map.

    Use the Timeline API to get all teams ever requested, since submitted reviews
    are removed from the event payload's requested_teams list.
    When PR is closed (merged or closed), add all requested channels.
    Otherwise add only channels the reviewer belongs to

    Returns a dict of {channel_id: [team objects]}.
    """
    # Review map was already checked as not None by the caller
    review_map = config.review_map

    print(f"Reviewer: {reviewer.login if reviewer else None}")
    print(f"Requested teams (from timeline): {', '.join(t.slug for t in requested_teams)}")

    default_channel_id = config.slack_channel_ids[0] if config.slack_channel_ids else None
    target_channels = defaultdict(list)
    for team in requested_teams:
        full_team = f"@{team.organization.login}/{team.slug}".lower()
        if full_team not in review_map.team_to_channel:
            print(f"  Team {full_team}: not in review map, use default")
        channel_id = review_map.team_to_channel.get(full_team, default_channel_id)
        if channel_id is None:
            continue
        if pr.state == "closed" or target_all_requested_channels:
            target_channels[channel_id].append(team)
        else:
            is_member = reviewer and team.has_in_members(reviewer)
            print(f"  Team {full_team}: channel={channel_id}, {reviewer.login if reviewer else None} is_member={is_member}")
            if is_member:
                target_channels[channel_id].append(team)

    if target_channels:
        return target_channels
    if default_channel_id:
        print(f"No team match, falling back to default channel {default_channel_id}")
        return {default_channel_id: []}
    return {}


def _apply_emojis_to_channel(
    config: Config,
    slack: SlackClient,
    new_emojis: Set[str],
    pr_url: str,
    channel_id: str,
) -> None:
    timestamp = slack.find_timestamp_of_review_requested_message(pr_url=pr_url, channel_id=channel_id)
    print(f"Slack message timestamp for channel {channel_id}: {timestamp}")

    if timestamp is None:
        print(f"No message found requesting review for PR: {pr_url} in channel {channel_id}")
        return

    existing_emojis = slack.get_emojis_for_user(
        timestamp=timestamp, channel_id=channel_id, user_id=config.slapr_bot_user_id
    )
    print(f"Existing emojis: {', '.join(existing_emojis)}")

    emojis_to_add, emojis_to_remove = emojis.diff(new_emojis=new_emojis, existing_emojis=existing_emojis)

    sorted_emojis_to_add = sorted(emojis_to_add, key=config.emojis_by_review_step)

    print(f"Emojis to add (ordered) : {', '.join(sorted_emojis_to_add)}")
    print(f"Emojis to remove        : {', '.join(emojis_to_remove)}")

    for emoji in sorted_emojis_to_add:
        slack.add_reaction(timestamp=timestamp, emoji=emoji, channel_id=channel_id)

    for emoji in emojis_to_remove:
        slack.remove_reaction(timestamp=timestamp, emoji=emoji, channel_id=channel_id)
