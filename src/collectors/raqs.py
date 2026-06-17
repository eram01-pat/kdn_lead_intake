"""
Collector for MTO RAQS contracts (public Ontario MTO / MERX RAQS site).

This is a SECOND data source for the same pipeline — it returns the same
``Tender`` objects as the bids&tenders municipal collector, so the rest of the
pipeline (storage, Claude adjudication, Slack) treats RAQS contracts identically
to municipal tenders. It does NOT share the bids&tenders collector: RAQS is a
completely different platform (a JSF app), so it has its own scraping logic here.

Flow
----
1. Load the "Contracts By Regional Map" page once in headless Chromium.
   The page lists every open contract in one table (the region map is decorative
   — clicking a region does NOT filter the list, confirmed against the live site).
   Contract rows link to ``contractsByRegionView.jsf?contractId=<ID>``.
2. Visit each contract's detail page and extract the scope fields. The detail
   page carries the authoritative ``Region`` field, so region is read from there
   (NOT from the map) and used to filter to the requested regions. Volume is tiny
   (~16 contracts, single page, no pagination), so we visit every contract.

Politeness: detail-page visits are throttled by ``rate_limit_seconds`` (same
crawl setting as the municipal collector). We read public pages only and never
download bid documents (those require login).

# TODO (phase 2b): the per-contract Item List (bulletin/articleView.jsf?articleId=...)
# lists individual line items including pavement-marking spec codes — a stronger
# relevance signal. It requires mapping contract № → article ID across a different
# page hierarchy (fragile), so it is intentionally NOT scraped here. The detail
# page's "Classification of Work" is a qualification TABLE (work class + financial
# rating + max workload), not a clean single value, so it is also left for later;
# v1 adjudicates on the Contract Description, which is rich and reliable.
"""

import logging
import re
import time
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

from src.storage.models import Tender

logger = logging.getLogger(__name__)

_CONTRACT_ID_RE = re.compile(r"[?&](?:id|contractId|contract_id)=([^&#]+)", re.IGNORECASE)
# How long to let the contract table finish loading (rows arrive after networkidle).
_DISCOVERY_MAX_WAIT_S = 16
_DISCOVERY_POLL_S = 2.0


# ── Date parsing ────────────────────────────────────────────────────────────────

def _parse_date(value: Optional[str]) -> Optional[date]:
    """Best-effort date parse across the formats MERX/RAQS pages tend to use."""
    if not value:
        return None
    s = value.strip()
    # Drop a leading weekday token only (e.g. "Mon Jun 8, 2026 3:00 PM" -> "Jun 8, ...").
    # Restricted to real weekday names so it never eats a leading month like "July".
    s = re.sub(
        r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+", "", s, flags=re.IGNORECASE
    )

    # ISO date anywhere: YYYY-MM-DD
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # RAQS form: "24-Jun-2026" (DD-Mon-YYYY), often trailed by " at 11:00:00 AM"
    m = re.search(r"(\d{1,2})-([A-Za-z]{3,9})-(\d{4})", s)
    if m:
        for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
            try:
                return datetime.strptime(
                    f"{m.group(1)}-{m.group(2)}-{m.group(3)}", fmt
                ).date()
            except ValueError:
                continue

    # Month-name form anywhere: "July 15, 2026" / "Jun 1 2026"
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(
                    f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt
                ).date()
            except ValueError:
                continue

    # Numeric slash form: assume D/M/Y then M/D/Y
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        for fmt in ("%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(m.group(0), fmt).date()
            except ValueError:
                continue
    return None


# ── Field extraction from rendered detail-page text ───────────────────────────

# Known field labels on the contract detail page. The page renders each as a
# label line followed by its value line (no colon), e.g. "Region\nNorthwestern".
# These labels also act as delimiters: a value runs until the next known label.
_KNOWN_LABELS = [
    "Contract Number", "Contract No", "Contract #",
    "Tender Owner", "Contract Type", "Owner",
    "Region", "MTO Region",
    "Location", "Highway", "Length",
    "QMS Declaration", "Option A or B bidding",
    "Contract Description", "Description",
    "Tender Instructions", "Contract Dates",
    "Classification of Work", "Classification",
    "Tender Opening Date", "Tender Opening", "Opening Date",
    "Tender Advertise Date", "Tender Advertise", "Advertise Date",
    "TRF Submission Opening", "TRF Submission Closing",
    "Tender Closing Date", "Tender Closing", "Closing Date",
]


def _extract_field(text: str, labels: list[str]) -> str:
    """
    Find the value for the first matching label in ``labels``.

    Handles both "Label: value" (inline) and "Label\\nvalue" (label and value on
    separate lines, which is how this detail page renders). The value runs until
    the next known field label or a blank line.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    other_labels = [lbl.lower() for lbl in _KNOWN_LABELS]

    def _is_label_line(s_low: str) -> bool:
        # A line counts as the next field's label only if it IS a known label or a
        # "label:" prefix — NOT merely starts with the word (so a value like
        # "Region of Durham" is not mistaken for the short "Region" label).
        return any(
            s_low == ol or s_low == ol + ":" or s_low.startswith(ol + ":")
            for ol in other_labels
        )

    for i, line in enumerate(lines):
        low = line.lower()
        for label in labels:
            ll = label.lower()
            if low == ll or low == ll + ":" or low.startswith(ll + ":"):
                # Inline value after a colon on the same line?
                if ":" in line:
                    inline = line.split(":", 1)[1].strip()
                    if inline:
                        return inline
                # Otherwise gather following non-empty lines until the next label
                collected: list[str] = []
                for nxt in lines[i + 1:]:
                    if not nxt:
                        if collected:
                            break
                        continue
                    if _is_label_line(nxt.lower()):
                        break
                    collected.append(nxt)
                    # Descriptions can span lines; single-value fields are one line.
                    if label.lower() not in ("contract description", "description"):
                        break
                if collected:
                    return " ".join(collected).strip()
    return ""


# ── Link discovery (frame-aware) ──────────────────────────────────────────────

def _collect_contract_links(page) -> list[str]:
    """Find unique contract-detail links across all frames. Returns [absolute_url]."""
    out: list[str] = []
    seen: set[str] = set()
    for fr in page.frames:
        try:
            anchors = fr.query_selector_all("a")
        except Exception:
            continue
        for a in anchors:
            href = a.get_attribute("href") or ""
            low = href.lower()
            is_contract = "contractview" in low or (
                "contract" in low and _CONTRACT_ID_RE.search(href)
            )
            if not is_contract:
                continue
            abs_url = urljoin(fr.url, href)
            if abs_url in seen:
                continue
            seen.add(abs_url)
            out.append(abs_url)
    return out


def _discover_contracts(page) -> list[str]:
    """
    Return all contract-detail URLs once the table has finished loading. Rows
    arrive shortly after networkidle, so poll until the count is stable.
    """
    prev = -1
    stable = 0
    links: list[str] = []
    waited = 0.0
    while waited < _DISCOVERY_MAX_WAIT_S:
        links = _collect_contract_links(page)
        if links and len(links) == prev:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        prev = len(links)
        time.sleep(_DISCOVERY_POLL_S)
        waited += _DISCOVERY_POLL_S
    return links


def _dump_diagnostics(page, label: str) -> None:
    """Log the live page structure (used only when discovery finds nothing)."""
    try:
        logger.warning(
            "RAQS DIAG [%s]: title=%r url=%r frames=%d",
            label, page.title(), page.url, len(page.frames),
        )
    except Exception:
        pass
    for i, fr in enumerate(page.frames):
        try:
            hrefs = [(a.get_attribute("href") or "") for a in fr.query_selector_all("a")]
            contractish = sorted({h for h in hrefs if "contract" in h.lower()})
            logger.warning(
                "RAQS DIAG [%s]: frame[%d] url=%r anchors=%d contract_hrefs(%d)=%s",
                label, i, fr.url, len(hrefs), len(contractish), contractish[:15],
            )
        except Exception as exc:
            logger.warning("RAQS DIAG [%s]: frame[%d] inspect failed: %s", label, i, exc)
    try:
        body = re.sub(r"\s+", " ", page.inner_text("body"))
        logger.warning("RAQS DIAG [%s]: body text[:700]=%s", label, body[:700])
    except Exception:
        pass


# ── Detail page ───────────────────────────────────────────────────────────────

def _scrape_detail(page, detail_url: str, timeout_ms: int) -> dict:
    """Navigate to a contract detail page and return a dict of extracted fields."""
    page.goto(detail_url, timeout=timeout_ms, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    text = page.inner_text("body")

    return {
        "contract_no":    _extract_field(text, ["Contract Number", "Contract No", "Contract #"]),
        "contract_type":  _extract_field(text, ["Tender Owner", "Contract Type", "Owner"]),
        "region":         _extract_field(text, ["Region", "MTO Region"]),
        "location":       _extract_field(text, ["Location"]),
        "highway":        _extract_field(text, ["Highway"]),
        "length":         _extract_field(text, ["Length"]),
        "description":    _extract_field(text, ["Contract Description", "Description"]),
        "opening_date":   _extract_field(text, ["Tender Opening Date", "Tender Opening", "Opening Date",
                                                "Tender Closing Date", "Tender Closing", "Closing Date"]),
        "advertise_date": _extract_field(text, ["Tender Advertise Date", "Tender Advertise", "Advertise Date"]),
    }


def _build_tender(fields: dict, detail_url: str, source_id: str) -> Optional[Tender]:
    contract_no = fields.get("contract_no", "").strip()
    if not contract_no:
        # Fall back to the URL id so we still get a stable dedup key
        m = _CONTRACT_ID_RE.search(detail_url)
        contract_no = f"id-{m.group(1)}" if m else ""
    if not contract_no:
        logger.warning("RAQS: skipping contract with no number/id at %s", detail_url)
        return None

    region = fields.get("region", "").strip() or "Unknown"
    location = fields.get("location", "").strip()
    highway = fields.get("highway", "").strip()
    length = fields.get("length", "").strip()
    scope = fields.get("description", "").strip()
    contract_type = fields.get("contract_type", "").strip()

    # title: "<Contract No> — <Hwy N | Location> — <short scope>".
    # Classification of Work is a qualification table here, not a clean value, so
    # it is deliberately not used in the title (see module TODO).
    where = f"Hwy {highway}" if highway and highway not in ("0", "0 ") else location
    where = where.strip()[:50]
    title_bits = [contract_no]
    if where:
        title_bits.append(where)
    if scope:
        title_bits.append(scope[:90].strip())
    title = " — ".join(title_bits)

    # description passed to Claude + surfaced in Slack: scope is the primary signal,
    # location/highway/length let a human make the Eastern distance call.
    desc_parts = []
    if scope:
        desc_parts.append(scope)
    if location:
        desc_parts.append(f"Location: {location}")
    if highway:
        desc_parts.append(f"Highway: {highway}")
    if length:
        desc_parts.append(f"Length: {length}")
    desc_parts.append(f"Region: {region}")
    if contract_type:
        desc_parts.append(f"Contract Type: {contract_type}")
    description = " | ".join(desc_parts)

    closing = _parse_date(fields.get("opening_date"))
    posted = _parse_date(fields.get("advertise_date"))

    return Tender(
        id=f"raqs:{contract_no}",
        source_id=source_id,
        source_name=f"MTO RAQS — {region}",
        title=title,
        description=description,
        category=contract_type or "MTO",
        reference_no=contract_no,
        detail_url=detail_url,
        status="Open",
        posted_date=posted,
        closing_date=closing,
        raw={**fields},
        bid_categories=[],
    )


# ── Public entry point ──────────────────────────────────────────────────────────

def collect_raqs(
    region_map_url: str,
    regions: list[str],
    user_agent: str,
    source_id: str = "raqs",
    rate_limit_seconds: float = 3.0,
    timeout_seconds: int = 30,
) -> list[Tender]:
    """
    Collect open MTO RAQS contracts for the requested regions.

    Returns a list of Tender objects (one per in-region contract). Mirrors the
    municipal collector's contract: any unrecoverable error raises (the pipeline
    wraps this call in try/except for fault isolation); per-contract errors are
    swallowed so one bad contract never aborts the whole source.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    timeout_ms = timeout_seconds * 1000
    allowed = {r.strip().lower() for r in regions}
    tenders: list[Tender] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=user_agent)
        page = ctx.new_page()

        # Load the contracts map robustly. The site has shown a transient
        # "An error occurred while executing an action" banner on first load,
        # so retry once if the page never settles.
        loaded = False
        for attempt in range(2):
            try:
                page.goto(region_map_url, timeout=timeout_ms, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
                loaded = True
                break
            except PWTimeout:
                logger.warning("RAQS: contracts map load timed out (attempt %d/2)", attempt + 1)
                time.sleep(rate_limit_seconds)
        if not loaded:
            browser.close()
            raise RuntimeError("RAQS contracts map page failed to load")

        # The map lists every open contract in one table (region tabs don't
        # filter). Discover all contract links, then read each detail page.
        contract_urls = _discover_contracts(page)
        if not contract_urls:
            logger.warning("RAQS: no contract rows found on the map")
            _dump_diagnostics(page, "no-contracts")
            browser.close()
            return []

        logger.info("RAQS: %d contracts on the map; reading detail pages", len(contract_urls))

        skipped_region = 0
        for url in contract_urls:
            try:
                fields = _scrape_detail(page, url, timeout_ms)
                region = fields.get("region", "").strip()
                # Region is authoritative from the detail page. Filter to the
                # requested regions; keep contracts whose region we can't read.
                if region and allowed and region.lower() not in allowed:
                    skipped_region += 1
                    logger.info("RAQS: skip %s — region %r not in %s", fields.get("contract_no") or url, region, regions)
                    continue
                tender = _build_tender(fields, url, source_id)
                if tender:
                    tenders.append(tender)
            except Exception as exc:
                logger.warning("RAQS: failed to read contract %s: %s", url, exc)
            finally:
                time.sleep(rate_limit_seconds)

        browser.close()

    logger.info(
        "RAQS: %d contracts collected (%d skipped — out of region)",
        len(tenders), skipped_region,
    )
    if tenders:
        sample = tenders[0]
        logger.info(
            "RAQS: sample [%s] %s — desc[%d chars]: %s",
            sample.reference_no, sample.title[:90], len(sample.description),
            (sample.description[:120] + "…") if len(sample.description) > 120 else sample.description,
        )
    return tenders
