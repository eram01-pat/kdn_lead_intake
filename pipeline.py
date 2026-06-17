"""
DLP Tender Monitor — LLM-first pipeline.

Architecture:
  collect → store (Neon) → LLM adjudicates every new tender → dashboard

Each tender is adjudicated by Claude exactly once. The decision (yes/no/maybe)
is cached in the database. On subsequent runs the tender is skipped unless
it's a brand-new ID. This keeps API costs flat as the database grows.

Run:
  python pipeline.py                  # normal run
  python pipeline.py --dry-run        # collect + LLM but don't write DB or deploy dashboard
  python pipeline.py --source vaughan # single source (dev/debug)
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

import yaml

from src.collectors.bidsandtenders import collect
from src.dashboard.build import build
from src.matching.relevance import adjudicate
from src.notifications.slack import post_match
from src.storage.db import get_connection, init_db, save_llm_decision, upsert_tender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pipeline")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(args: argparse.Namespace) -> None:
    run_started_at = datetime.now(timezone.utc)

    sources_cfg  = load_config("config/sources.yaml")
    settings_cfg = load_config("config/settings.yaml")

    sources       = sources_cfg["sources"]
    crawl         = settings_cfg["crawl"]
    llm_cfg       = settings_cfg["llm"]
    storage_cfg   = settings_cfg["storage"]
    dashboard_cfg = settings_cfg["dashboard"]
    db_path = storage_cfg.get("db_path", "")

    if not args.dry_run:
        init_db(db_path)

    if args.source:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            logger.error("Unknown source id: %s", args.source)
            sys.exit(1)

    total_new     = 0
    total_matched = 0
    total_skipped = 0
    failed_sources: list[str] = []

    for source in sources:
        try:
            tenders = collect(
                source=source,
                user_agent=crawl["user_agent"],
                rate_limit_seconds=crawl["rate_limit_seconds"],
                max_retries=crawl["max_retries"],
                backoff_base_seconds=crawl["backoff_base_seconds"],
                timeout_seconds=crawl["timeout_seconds"],
                max_per_source=crawl["max_per_source"],
                fetch_detail_pages=crawl.get("fetch_detail_pages", True),
            )
        except Exception as exc:
            logger.error("Unhandled error collecting %s: %s", source["id"], exc)
            failed_sources.append(source["id"])
            continue

        if not tenders:
            logger.info("%s: 0 tenders returned", source["name"])
            continue

        new_count     = 0
        matched_count = 0
        skipped_count = 0

        for tender in tenders:
            if args.dry_run:
                # Dry-run: adjudicate but don't write anything
                decision, reason = adjudicate(
                    title=tender.title,
                    description=tender.description,
                    bid_categories=tender.bid_categories,
                    model=llm_cfg["model"],
                    max_tokens=llm_cfg["max_tokens"],
                )
                if decision in ("yes", "maybe"):
                    matched_count += 1
                    logger.info(
                        "DRY-RUN [%s]: %s — %s | %s",
                        decision.upper(), source["name"], tender.title, reason or "",
                    )
                continue

            with get_connection(db_path) as conn:
                is_new, has_decision = upsert_tender(conn, tender)

                if is_new:
                    new_count += 1

                if has_decision:
                    # Already adjudicated on a prior run — skip to save API calls
                    skipped_count += 1
                    continue

                decision, reason = adjudicate(
                    title=tender.title,
                    description=tender.description,
                    bid_categories=tender.bid_categories,
                    model=llm_cfg["model"],
                    max_tokens=llm_cfg["max_tokens"],
                )

                if decision:
                    save_llm_decision(conn, tender.id, decision, llm_cfg["model"], reason)
                    if decision in ("yes", "maybe"):
                        matched_count += 1
                        logger.info(
                            "MATCH [%s]: %s — %s",
                            decision.upper(), source["name"], tender.title[:80],
                        )
                        if is_new:
                            post_match(
                                title=tender.title,
                                source_name=source["name"],
                                detail_url=tender.detail_url,
                                decision=decision,
                                closing_date=tender.closing_date,
                                reference_no=tender.reference_no,
                                reason=reason,
                            )

        logger.info(
            "%s: %d tenders | %d new | %d matched | %d already decided",
            source["name"], len(tenders), new_count, matched_count, skipped_count,
        )
        total_new     += new_count
        total_matched += matched_count
        total_skipped += skipped_count

    logger.info(
        "Run complete: %d new | %d matched | %d skipped (cached) | %d sources failed",
        total_new, total_matched, total_skipped, len(failed_sources),
    )
    if failed_sources:
        logger.warning("Failed sources: %s", ", ".join(failed_sources))

    if not args.dry_run and not args.skip_dashboard:
        build(
            db_path=db_path,
            output_path=dashboard_cfg["output_path"],
            snippet_length=dashboard_cfg["snippet_length"],
            closing_soon_days=dashboard_cfg["closing_soon_days"],
            run_started_at=run_started_at,
        )

    if failed_sources and not args.ignore_errors:
        sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="DLP Tender Monitor pipeline (LLM-first)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Collect and adjudicate but do not write to DB or build dashboard",
    )
    parser.add_argument(
        "--source", metavar="SOURCE_ID",
        help="Run only for a single source ID (useful for debugging)",
    )
    parser.add_argument(
        "--skip-dashboard", action="store_true",
        help="Skip dashboard build",
    )
    parser.add_argument(
        "--ignore-errors", action="store_true",
        help="Exit 0 even if some sources failed (useful in CI)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
