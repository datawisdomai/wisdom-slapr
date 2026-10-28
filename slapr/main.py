# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

from collections import defaultdict
from typing import Dict, List, Optional, Set

from . import emojis
from .config import Config
from .github import GithubClient, PullRequest
from .slack import SlackClient


def main(config: Config, event: Optional[dict] = None, message_timestamps: Optional[Dict[str, str]] = None) -> None:
    slack = config.slack_client
    github = config.github_client

    if event is None:
        event = github.read_event()

    is_fork: bool = event["pull_request"]["head"]["repo"]["fork"]

    if is_fork:
        print("Fork PRs are not supported.")
        return

    pr_number: int = event["pull_request"]["number"]
    pr = github.get_pr(pr_number=pr_number)
    terminal_emoji = config.emoji_merged if pr.merged else config.emoji_closed if pr.state == "closed" else ""
    reviews = [] if terminal_emoji else github.get_pr_reviews(pr_number=pr_number)
    unresolved_review_authors = set()
    if pr.state != "closed" and (config.emoji_approved_with_comments or config.emoji_unresolved_comments):
        unresolved_review_authors = github.get_unresolved_review_authors(pr_number)
    pr_url: str = event["pull_request"]["html_url"]
    print(f"Event PR: {pr_url} - Is merged: {pr.merged}")

    # Determine target channels (with optional team slug for filtering)
    if config.review_map is not None:
        org_name: str = event["pull_request"]["head"]["repo"]["owner"]["login"]
        requested_teams = github.get_all_requested_teams(org_name, pr_number)
        reviewer_login = event.get("review", {}).get("user", {}).get("login")
        reviewer = github.get_user(reviewer_login) if reviewer_login else None
        target_channels = _resolve_target_channels(
            config, requested_teams, pr, reviewer, refresh=message_timestamps is not None
        )
    else:
        requested_teams = []
        target_channels = {ch: [] for ch in config.slack_channel_ids}

    for channel_id, teams in target_channels.items():
        if message_timestamps is not None and channel_id not in message_timestamps:
            continue

        review_emoji = emojis.select(
            teams,
            reviews,
            config,
            number_of_approvals_required=config.number_of_approvals_required,
            channel_id=channel_id,
            unresolved_review_authors=unresolved_review_authors,
        )

        # Keep exactly one reaction per message: terminal state wins, then the
        # review outcome, then the review-started marker.
        new_emojis: Set[str] = set()
        if pr.merged and config.emoji_merged:
            new_emojis.add(config.emoji_merged)
        elif pr.state == "closed" and not pr.merged and config.emoji_closed:
            new_emojis.add(config.emoji_closed)
        elif review_emoji:
            new_emojis.add(review_emoji)
        elif pr.state != "closed" and config.emoji_review_started:
            new_emojis.add(config.emoji_review_started)

        _apply_emojis_to_channel(
            config, slack, new_emojis, pr_url, channel_id,
            timestamp=(message_timestamps or {}).get(channel_id),
        )

    # Broadcast review_started to ALL requested team channels, not just the reviewer's.
    # Only on the first review (len==1) — subsequent reviews already have review_started.
    if (
        config.review_map is not None
        and message_timestamps is None
        and not pr.merged
        and pr.state != "closed"
        and len(reviews) == 1
        and config.emoji_review_started
    ):
        all_channels = config.review_map.get_channels_for_requested_teams(requested_teams)
        already_processed = set(target_channels.keys())
        for channel_id in all_channels - already_processed:
            print(f"Broadcasting review_started to channel {channel_id}")
            _apply_emojis_to_channel(
                config, slack, {config.emoji_review_started}, pr_url, channel_id
            )


def _resolve_target_channels(
    config: Config, requested_teams: List, pr: PullRequest, reviewer, refresh: bool = False
) -> Dict[str, List]:
    """Determine which Slack channels to target based on the review map.

    Use the Timeline API to get all teams ever requested, since submitted reviews
    are removed from the event payload's requested_teams list.
    When PR is closed or refreshing, add all requested channels.
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
        if pr.state == "closed" or refresh:
            target_channels[channel_id].append(team)
        else:
            is_member = reviewer and team.has_in_members(reviewer)
            print(f"  Team {full_team}: channel={channel_id}, {reviewer.login if reviewer else None} is_member={is_member}")
            if is_member:
                target_channels[channel_id].append(team)

    if refresh and default_channel_id:
        # Posts reached by an outside-team review use the unfiltered fallback.
        target_channels.setdefault(default_channel_id, [])
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
    timestamp: Optional[str] = None,
) -> None:
    if timestamp is None:
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
