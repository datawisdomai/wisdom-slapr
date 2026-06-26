# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

from typing import Callable, List, NamedTuple, Optional, Tuple

from .github import GithubClient
from .review_map import ReviewMap
from .slack import SlackClient


class Config(NamedTuple):
    slack_client: SlackClient
    github_client: GithubClient

    slack_channel_ids: List[str]
    slapr_bot_user_id: str  # TODO: document how to obtain this user ID, or automate its retrieval.

    number_of_approvals_required: int

    emoji_review_started: str
    emoji_approved: str
    emoji_needs_change: str
    emoji_merged: str
    emoji_closed: str
    emoji_commented: str

    review_map: Optional[ReviewMap] = None

    emoji_pr_open: str = ""
    emoji_pr_draft: str = ""
    emoji_pr_queued: str = ""
    emoji_pr_merged: str = ""
    emoji_pr_closed: str = ""

    emoji_ci_running: str = ""
    emoji_ci_failing: str = ""
    emoji_ci_passing: str = ""

    ignored_ci_check_names: Tuple[str, ...] = ()

    @property
    def has_ci_status_emojis(self) -> bool:
        return bool(self.emoji_ci_running or self.emoji_ci_failing or self.emoji_ci_passing)

    @property
    def emojis_by_review_step(self) -> Callable[[str], int]:
        """A key function for sorting emojis in the order of the usual review process.

        Suitable for usage with `sorted(...key=...)` or `some_list.sort(key=...)`.
        """
        review_steps_as_emojis = [
            self.emoji_pr_open,
            self.emoji_pr_draft,
            self.emoji_pr_queued,
            self.emoji_pr_merged,
            self.emoji_pr_closed,
            self.emoji_review_started,
            self.emoji_commented,
            self.emoji_needs_change,
            self.emoji_approved,
            self.emoji_closed,
            self.emoji_merged,
            self.emoji_ci_running,
            self.emoji_ci_failing,
            self.emoji_ci_passing,
        ]

        return lambda emoji: (
            review_steps_as_emojis.index(emoji)
            if emoji in review_steps_as_emojis
            else len(review_steps_as_emojis)
        )
