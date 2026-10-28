from .config import Config
from .main import main


def refresh(config: Config, repo: str) -> None:
    """Reconcile recent review posts because thread resolution cannot trigger Actions."""
    if not config.comment_status_channel_ids:
        raise ValueError("Periodic refresh requires explicit SLAPR_COMMENT_STATUS_CHANNEL_IDS")
    for channel_id in config.comment_status_channel_ids:
        if channel_id not in config.slack_channel_ids:
            raise ValueError(f"Refresh channel {channel_id} is not in SLACK_CHANNEL_ID")
        for pr_number, timestamp in config.slack_client.get_review_requests(repo, channel_id).items():
            event = config.github_client.get_pr_event(pr_number)
            main(config, event=event, message_timestamps={channel_id: timestamp})
