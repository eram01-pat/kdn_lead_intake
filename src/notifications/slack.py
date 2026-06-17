"""
Slack notifier — posts a message when a new matched tender is found.

Webhook URL is read from the SLACK_WEBHOOK_URL environment variable.
Failures are logged but never crash the pipeline.
"""

import json
import logging
import os
import urllib.request
from datetime import date, datetime

logger = logging.getLogger(__name__)

CLOSING_SOON_DAYS = 14


def _webhook_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def _days_until(closing_date) -> int | None:
    if closing_date is None:
        return None
    if isinstance(closing_date, str):
        try:
            closing_date = date.fromisoformat(closing_date)
        except ValueError:
            return None
    if isinstance(closing_date, datetime):
        closing_date = closing_date.date()
    return (closing_date - date.today()).days


def post_match(
    title: str,
    source_name: str,
    detail_url: str,
    decision: str,
    closing_date=None,
    reference_no: str = "",
    reason: str | None = None,
) -> None:
    """
    Post a single tender match to Slack.
    decision is 'yes' or 'maybe'.
    Only called for newly-seen tenders so the team never gets duplicate alerts.
    """
    url = _webhook_url()
    if not url:
        logger.debug("SLACK_WEBHOOK_URL not set — skipping notification")
        return

    confidence = "High" if decision == "yes" else "Medium"
    emoji      = "🟢" if decision == "yes" else "🟡"

    days = _days_until(closing_date)
    closing_soon = days is not None and 0 <= days <= CLOSING_SOON_DAYS

    lines = [f"{emoji} *NEW TENDER — {confidence} Confidence*"]
    lines.append(f"*{title}*")
    if reason:
        lines.append(f"_{reason}_")
    lines.append(f"📍 {source_name}")
    if reference_no:
        lines.append(f"Ref: {reference_no}")
    if closing_date:
        if closing_soon:
            lines.append(f"⏳ *Closes in {days} day{'s' if days != 1 else ''}* — {closing_date}")
        else:
            lines.append(f"Closes: {closing_date}")
    lines.append(f"<{detail_url}|View Tender →>")

    payload = {"text": "\n".join(lines)}

    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Slack returned %s", resp.status)
    except Exception as exc:
        logger.warning("Slack notification failed: %s", exc)


def post_weekly_report(counts: dict, top_tenders: list[dict]) -> None:
    """
    Post a Friday weekly accuracy/activity report to Slack.
    counts: {total, yes_count, maybe_count, no_count}
    top_tenders: up to 5 open matched tenders sorted by closing date.
    """
    url = _webhook_url()
    if not url:
        logger.debug("SLACK_WEBHOOK_URL not set — skipping weekly report")
        return

    total    = counts.get("total", 0)
    yes_c    = counts.get("yes_count", 0)
    maybe_c  = counts.get("maybe_count", 0)
    flagged  = yes_c + maybe_c
    rejected = counts.get("no_count", 0)

    lines = [f"📊 *DLP Weekly Tender Report — {date.today().strftime('%B %d, %Y')}*"]
    lines.append(f"Claude reviewed *{total}* tender{'s' if total != 1 else ''} this week")
    lines.append(
        f"• *{flagged} flagged* ({yes_c} high confidence, {maybe_c} medium confidence)"
    )
    lines.append(f"• {rejected} rejected as out of scope")

    if top_tenders:
        lines.append("")
        lines.append("*Open opportunities — closest deadlines:*")
        for i, t in enumerate(top_tenders, 1):
            days = _days_until(t.get("closing_date"))
            emoji = "🟢" if t.get("llm_decision") == "yes" else "🟡"
            if days is None:
                deadline = "no closing date"
            elif days == 0:
                deadline = "⏳ closes TODAY"
            elif days <= CLOSING_SOON_DAYS:
                deadline = f"⏳ closes in {days}d"
            else:
                deadline = f"closes {t['closing_date']}"
            lines.append(f"{i}. {emoji} <{t['detail_url']}|{t['title']}> ({t['source_name']}) — {deadline}")
    else:
        lines.append("")
        lines.append("_No open matched tenders at this time._")

    payload = {"text": "\n".join(lines)}

    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Slack weekly report returned %s", resp.status)
        logger.info("Slack weekly report posted")
    except Exception as exc:
        logger.warning("Slack weekly report failed: %s", exc)


def post_digest(tenders: list[dict]) -> None:
    """
    Post a daily digest of all open matched tenders, sorted by closing date.
    Each item in tenders should have: title, source_name, detail_url,
    closing_date, llm_decision, llm_reason, reference_no.
    """
    url = _webhook_url()
    if not url:
        logger.debug("SLACK_WEBHOOK_URL not set — skipping digest")
        return

    if not tenders:
        logger.info("No matched tenders for digest — skipping")
        return

    high = [t for t in tenders if t.get("llm_decision") == "yes"]
    medium = [t for t in tenders if t.get("llm_decision") == "maybe"]

    lines = [f"📋 *DLP Tender Digest — {date.today().strftime('%B %d, %Y')}*"]
    lines.append(f"{len(tenders)} open opportunit{'y' if len(tenders) == 1 else 'ies'} "
                 f"({len(high)} high confidence, {len(medium)} medium)")
    lines.append("")

    for section_label, section in [("🟢 High Confidence", high), ("🟡 Medium Confidence", medium)]:
        if not section:
            continue
        lines.append(f"*{section_label}*")
        for t in section:
            days = _days_until(t.get("closing_date"))
            closing_str = ""
            if days is not None:
                if days == 0:
                    closing_str = " — ⏳ *closes TODAY*"
                elif 0 < days <= CLOSING_SOON_DAYS:
                    closing_str = f" — ⏳ closes in {days}d"
                else:
                    closing_str = f" — closes {t['closing_date']}"
            reason_str = f"\n   _{t['llm_reason']}_" if t.get("llm_reason") else ""
            lines.append(
                f"• <{t['detail_url']}|{t['title']}> "
                f"({t['source_name']}){closing_str}{reason_str}"
            )
        lines.append("")

    payload = {"text": "\n".join(lines)}

    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Slack digest returned %s", resp.status)
        logger.info("Slack digest posted — %d tenders", len(tenders))
    except Exception as exc:
        logger.warning("Slack digest failed: %s", exc)
