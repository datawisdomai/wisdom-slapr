# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .github import Review
from .config import Config

# A comment is not a verdict: it must not clear the author's earlier approval or change request.
DECISIVE_STATES = ("approved", "changes_requested", "dismissed")


def _latest_decisive_state(author_reviews: List[Review]) -> str:
    decisive = [review for review in author_reviews if review.state in DECISIVE_STATES]
    return (decisive or author_reviews)[-1].state


def select(
    reviewer_teams: List,
    reviews: List[Review],
    config: Config,
    number_of_approvals_required: int,
) -> Optional[str]:

    all_reviews_by_author: Dict[str, List[Review]] = defaultdict(list)
    for review in reviews:
        all_reviews_by_author[review.user.login].append(review)

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

    last_states = [
        _latest_decisive_state(author_reviews) for author_reviews in reviews_by_author.values() if author_reviews
    ]
    unique_states = set(last_states)

    if "changes_requested" in unique_states and config.emoji_needs_change:
        return config.emoji_needs_change

    if (
        "approved" in unique_states
        and last_states.count("approved") >= number_of_approvals_required
        and config.emoji_approved
    ):
        return config.emoji_approved

    if "commented" in unique_states and config.emoji_commented:
        return config.emoji_commented

    return None


def diff(new_emojis: Set[str], existing_emojis: Set[str]) -> Tuple[Set[str], Set[str]]:
    emojis_to_add = new_emojis - existing_emojis
    emojis_to_remove = existing_emojis - new_emojis
    return emojis_to_add, emojis_to_remove
