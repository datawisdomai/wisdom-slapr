# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/)
# Copyright 2023-present Datadog, Inc.

import os

import github
import slack_sdk

from .config import Config
from .github import GithubClient, WebGithubBackend
from .main import main
from .review_map import ReviewMap
from .slack import SlackClient, WebSlackBackend

slack_backend = WebSlackBackend(client=slack_sdk.WebClient(os.environ["SLACK_API_TOKEN"]))
slack_client = SlackClient(backend=slack_backend)

slack_channel_ids = [
    ch.strip() for ch in os.environ["SLACK_CHANNEL_ID"].split(",") if ch.strip()
]

review_map = None
review_map_path = os.environ.get("SLAPR_REVIEW_MAP")
if review_map_path:
    review_map = ReviewMap.load(
        file_path=review_map_path,
        slack_client=slack_client,
        default_channel_id=slack_channel_ids[0] if slack_channel_ids else "",
    )

ignored_ci_check_names_env = os.environ.get("SLAPR_IGNORE_CI_CHECK_NAMES") or os.environ.get("GITHUB_JOB", "")
ignored_ci_check_names = tuple(name.strip() for name in ignored_ci_check_names_env.split(",") if name.strip())

config = Config(
    slack_client=slack_client,
    github_client=GithubClient(
        backend=WebGithubBackend(
            gh=github.Github(auth=github.Auth.Token(os.environ["GITHUB_TOKEN"])),
            event_path=os.environ["GITHUB_EVENT_PATH"],
            repo=os.environ["GITHUB_REPOSITORY"],
        )
    ),
    slack_channel_ids=slack_channel_ids,
    slapr_bot_user_id=os.environ["SLAPR_BOT_USER_ID"],
    number_of_approvals_required=max(1, int(os.environ.get("SLAPR_NUMBER_OF_APPROVALS_REQUIRED", 1))),
    emoji_review_started=os.environ.get("SLAPR_EMOJI_REVIEW_STARTED", ""),
    emoji_approved=os.environ.get("SLAPR_EMOJI_APPROVED", ""),
    emoji_needs_change=os.environ.get("SLAPR_EMOJI_CHANGES_REQUESTED", ""),
    emoji_merged=os.environ.get("SLAPR_EMOJI_MERGED", ""),
    emoji_closed=os.environ.get("SLAPR_EMOJI_CLOSED", ""),
    emoji_commented=os.environ.get("SLAPR_EMOJI_COMMENTED", ""),
    review_map=review_map,
    emoji_open=os.environ.get("SLAPR_EMOJI_OPEN", ""),
    emoji_draft=os.environ.get("SLAPR_EMOJI_DRAFT", ""),
    emoji_queue=os.environ.get("SLAPR_EMOJI_QUEUE", ""),
    emoji_ci_running=os.environ.get("SLAPR_EMOJI_CI_RUNNING", ""),
    emoji_ci_failing=os.environ.get("SLAPR_EMOJI_CI_FAILING", ""),
    emoji_ci_passing=os.environ.get("SLAPR_EMOJI_CI_PASSING", ""),
    ignored_ci_check_names=ignored_ci_check_names,
)

main(config)
