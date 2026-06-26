# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

import itertools
from typing import List, Optional, Set, Tuple

from .github import CI_STATUS_FAILING, CI_STATUS_PASSING, CI_STATUS_RUNNING, PullRequest, Review
from .config import Config


def select(
    reviewer_teams: List,
    reviews: List[Review],
    config: Config,
    number_of_approvals_required: int,
) -> Optional[str]:

    all_reviews_by_author = {
        user_login: list(author_reviews)
        for user_login, author_reviews in itertools.groupby(reviews, key=lambda review: review.user.login)
    }

    # Keep only reviews from authors belonging to the same team(s) as the reviewer
    if reviewer_teams:
        reviews_by_author = {}
        for author_login, author_reviews in all_reviews_by_author.items():
            author_user = author_reviews[0].user
            if any(t.has_in_members(author_user) for t in reviewer_teams):
                reviews_by_author[author_login] = author_reviews
    else:
        # No review map or no team match: consider all reviews
        reviews_by_author = all_reviews_by_author

    last_reviews = [reviews[-1] for reviews in reviews_by_author.values() if reviews]
    unique_states = {review.state for review in last_reviews}

    if "changes_requested" in unique_states and config.emoji_needs_change:
        return config.emoji_needs_change

    approval_count = len([review.state for review in last_reviews if review.state == "approved"])
    if (
        "approved" in unique_states
        and approval_count >= number_of_approvals_required
        and config.emoji_approved
    ):
        return config.emoji_approved

    if "commented" in unique_states and config.emoji_commented:
        return config.emoji_commented

    return None


def select_pr_state(pr: PullRequest, config: Config, event_action: str = "") -> Optional[str]:
    pr_state = pr.state.lower()
    mergeable_state = pr.mergeable_state.lower()
    event_action = event_action.lower()

    if pr.merged:
        return config.emoji_pr_merged or config.emoji_merged or None
    if pr_state == "closed":
        return config.emoji_pr_closed or config.emoji_closed or None
    if pr.draft or mergeable_state == "draft":
        return config.emoji_pr_draft or None
    if event_action == "enqueued" or mergeable_state in {"queue", "queued"}:
        return config.emoji_pr_queued or None
    if pr_state == "open":
        return config.emoji_pr_open or None
    return None


def select_ci_status(ci_status: str, config: Config) -> Optional[str]:
    if ci_status == CI_STATUS_RUNNING:
        return config.emoji_ci_running or None
    if ci_status == CI_STATUS_FAILING:
        return config.emoji_ci_failing or None
    if ci_status == CI_STATUS_PASSING:
        return config.emoji_ci_passing or None
    return None


def diff(new_emojis: Set[str], existing_emojis: Set[str]) -> Tuple[Set[str], Set[str]]:
    emojis_to_add = new_emojis - existing_emojis
    emojis_to_remove = existing_emojis - new_emojis
    return emojis_to_add, emojis_to_remove
