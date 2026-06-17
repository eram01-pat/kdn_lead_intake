"""
Static dashboard builder. Reads matched tenders from Neon and renders index.html.
Published to GitHub Pages by the Actions workflow.
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from src.storage.db import get_connection, get_open_matched_tenders

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _snippet(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return (
            text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
    clipped = text[:max_len]
    # Don't cut mid-word
    last_space = clipped.rfind(" ")
    if last_space > max_len - 40:
        clipped = clipped[:last_space]
    return (
        (clipped + "…")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _days_until(d) -> int | None:
    if d is None:
        return None
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return None
    if isinstance(d, datetime):
        d = d.date()
    return (d - date.today()).days


def build(
    db_path: str = "",
    output_path: str = "docs/index.html",
    snippet_length: int = 400,
    closing_soon_days: int = 7,
    run_started_at: datetime | None = None,
) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if run_started_at is None:
        run_started_at = datetime.now(timezone.utc)

    with get_connection(db_path) as conn:
        rows = get_open_matched_tenders(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM tenders WHERE status = 'Open'")
            total_open = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(DISTINCT source_id) AS n FROM tenders WHERE status = 'Open'"
            )
            source_count = cur.fetchone()["n"]

    new_cutoff = run_started_at.replace(hour=0, minute=0, second=0, microsecond=0)

    template_data: list[dict] = []
    for row in rows:
        llm_decision = row["llm_decision"]                   # 'yes' or 'maybe'
        confidence   = "High" if llm_decision == "yes" else "Medium"

        bid_cats = row.get("bid_categories") or []
        if isinstance(bid_cats, str):
            bid_cats = json.loads(bid_cats)

        days = _days_until(row["closing_date"])

        first_seen = row["first_seen_at"]
        if isinstance(first_seen, str):
            try:
                first_seen = datetime.fromisoformat(first_seen)
            except ValueError:
                first_seen = None

        template_data.append({
            "title":            row["title"],
            "detail_url":       row["detail_url"],
            "source_name":      row["source_name"],
            "reference_no":     row.get("reference_no") or "",
            "category":         row.get("category") or "",
            "bid_categories":   bid_cats,
            "posted_date":      row.get("posted_date"),
            "closing_date":     row.get("closing_date"),
            "confidence":       confidence,
            "llm_decision":     llm_decision,
            "llm_reason":       row.get("llm_reason") or "",
            "snippet":          _snippet(row.get("description") or "", snippet_length),
            "is_new":           bool(first_seen and first_seen >= new_cutoff),
            "days_until_close": days,
            "closing_soon":     bool(days is not None and 0 <= days <= closing_soon_days),
        })

    all_sources = sorted({t["source_name"] for t in template_data})

    env  = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=False)
    tmpl = env.get_template("index.html.j2")

    html = tmpl.render(
        generated_at=run_started_at.strftime("%Y-%m-%d %H:%M UTC"),
        total_open=total_open,
        source_count=source_count,
        tenders=template_data,
        sources=all_sources,
    )

    Path(output_path).write_text(html, encoding="utf-8")
    logger.info("Dashboard written to %s (%d matched tenders)", output_path, len(template_data))
