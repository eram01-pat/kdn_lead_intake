"""
PostgreSQL storage layer (Neon).

Connection string is read from the DATABASE_URL environment variable.
Schema is intentionally simple — llm_decision on the tenders row IS the match record.
No separate matches table; the dashboard queries tenders directly.
"""

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# psycopg2 connection type alias (for type hints only)
PgConn = psycopg2.extensions.connection

DDL = """
CREATE TABLE IF NOT EXISTS tenders (
    id              TEXT PRIMARY KEY,
    source_id       TEXT        NOT NULL,
    source_name     TEXT        NOT NULL,
    title           TEXT        NOT NULL,
    description     TEXT,
    category        TEXT,
    reference_no    TEXT,
    detail_url      TEXT        NOT NULL,
    status          TEXT,
    posted_date     DATE,
    closing_date    DATE,
    raw             JSONB,
    bid_categories  JSONB,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    llm_decision    TEXT,       -- 'yes' | 'no' | 'maybe'  (NULL = not yet adjudicated)
    llm_decided_at  TIMESTAMPTZ,
    llm_model       TEXT,
    llm_reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenders_source       ON tenders(source_id);
CREATE INDEX IF NOT EXISTS idx_tenders_status       ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_tenders_closing_date ON tenders(closing_date);
CREATE INDEX IF NOT EXISTS idx_tenders_llm_decision ON tenders(llm_decision);
"""


@contextmanager
def get_connection(db_path: str = "") -> Generator[PgConn, None, None]:
    """
    Yield a psycopg2 connection.
    db_path is accepted for interface compatibility but ignored —
    the connection string comes from DATABASE_URL.
    """
    url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = "") -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            # Migrate existing tables that pre-date llm_reason column
            cur.execute("""
                ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_reason TEXT
            """)


def upsert_tender(conn: PgConn, tender) -> tuple[bool, bool]:
    """
    Insert or update a tender row.

    Returns (is_new, has_llm_decision):
      is_new           — True if this tender has never been seen before
      has_llm_decision — True if LLM has already adjudicated this tender ID
                         (even if description has changed; we preserve the cached decision
                         to avoid re-spending API calls on tenders we've already decided)
    """
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, llm_decision FROM tenders WHERE id = %s",
            (tender.id,),
        )
        row = cur.fetchone()

        if row:
            has_decision = row["llm_decision"] is not None
            # Update mutable fields but preserve llm_decision / llm_decided_at
            cur.execute(
                """UPDATE tenders SET
                    title          = %s,
                    description    = %s,
                    category       = %s,
                    reference_no   = %s,
                    detail_url     = %s,
                    status         = %s,
                    posted_date    = %s,
                    closing_date   = %s,
                    raw            = %s,
                    bid_categories = %s,
                    last_seen_at   = %s
                WHERE id = %s""",
                (
                    tender.title, tender.description, tender.category,
                    tender.reference_no, tender.detail_url, tender.status,
                    tender.posted_date, tender.closing_date,
                    json.dumps(tender.raw, default=str),
                    json.dumps(tender.bid_categories),
                    now,
                    tender.id,
                ),
            )
            return False, has_decision
        else:
            cur.execute(
                """INSERT INTO tenders (
                    id, source_id, source_name, title, description, category,
                    reference_no, detail_url, status, posted_date, closing_date,
                    raw, bid_categories, first_seen_at, last_seen_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    tender.id, tender.source_id, tender.source_name,
                    tender.title, tender.description, tender.category,
                    tender.reference_no, tender.detail_url, tender.status,
                    tender.posted_date, tender.closing_date,
                    json.dumps(tender.raw, default=str),
                    json.dumps(tender.bid_categories),
                    now, now,
                ),
            )
            return True, False


def save_llm_decision(
    conn: PgConn, tender_id: str, decision: str, model: str, reason: str | None = None
) -> None:
    """Persist the LLM adjudication result for a tender."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE tenders
               SET llm_decision = %s, llm_decided_at = %s, llm_model = %s, llm_reason = %s
             WHERE id = %s""",
            (decision, now, model, reason, tender_id),
        )


def get_weekly_stats(conn: PgConn) -> dict:
    """
    Return adjudication counts for the past 7 days and top open matched tenders
    sorted by closing date for the weekly Slack report.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                COUNT(*)                                             AS total,
                COUNT(*) FILTER (WHERE llm_decision = 'yes')        AS yes_count,
                COUNT(*) FILTER (WHERE llm_decision = 'maybe')      AS maybe_count,
                COUNT(*) FILTER (WHERE llm_decision = 'no')         AS no_count
            FROM tenders
            WHERE llm_decided_at >= NOW() - INTERVAL '7 days'"""
        )
        counts = dict(cur.fetchone())

        cur.execute(
            """SELECT title, source_name, detail_url, closing_date, llm_decision, llm_reason
            FROM tenders
            WHERE status = 'Open'
              AND llm_decision IN ('yes', 'maybe')
            ORDER BY
                CASE llm_decision WHEN 'yes' THEN 0 ELSE 1 END,
                closing_date ASC NULLS LAST
            LIMIT 5"""
        )
        top = [dict(r) for r in cur.fetchall()]

    return {"counts": counts, "top_tenders": top}


def get_open_matched_tenders(conn: PgConn) -> list[dict]:
    """
    Return all open tenders where LLM said yes or maybe, ordered for the dashboard.
    Columns are shaped to match what build.py / the Jinja template expect.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                id, source_id, source_name, title, description,
                category, reference_no, detail_url, status,
                posted_date, closing_date, first_seen_at,
                bid_categories, llm_decision, llm_reason
            FROM tenders
            WHERE status = 'Open'
              AND llm_decision IN ('yes', 'maybe')
            ORDER BY
                CASE llm_decision WHEN 'yes' THEN 0 ELSE 1 END,
                closing_date ASC NULLS LAST"""
        )
        return [dict(r) for r in cur.fetchall()]
