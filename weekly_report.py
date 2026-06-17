"""
DLP Weekly Tender Report — posts a Friday summary to Slack.

Run:
  python weekly_report.py
"""

import logging
import sys

import yaml

from src.notifications.slack import post_weekly_report
from src.storage.db import get_connection, get_weekly_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("weekly_report")


def main() -> None:
    storage_cfg = yaml.safe_load(open("config/settings.yaml"))["storage"]
    db_path = storage_cfg.get("db_path", "")

    with get_connection(db_path) as conn:
        stats = get_weekly_stats(conn)

    logger.info(
        "Weekly stats: %d reviewed, %d flagged, %d rejected",
        stats["counts"].get("total", 0),
        stats["counts"].get("yes_count", 0) + stats["counts"].get("maybe_count", 0),
        stats["counts"].get("no_count", 0),
    )
    post_weekly_report(stats["counts"], stats["top_tenders"])


if __name__ == "__main__":
    main()
