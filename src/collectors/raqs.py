"""
Collector for MTO RAQS contracts (public Ontario MTO / MERX RAQS site).

This is a SECOND data source for the same pipeline — it returns the same
``Tender`` objects as the bids&tenders municipal collector, so the rest of the
pipeline (storage, Claude adjudication, Slack) treats RAQS contracts identically
to municipal tenders. It does NOT share the bids&tenders collector: RAQS is a
completely different platform (a JSF app), so it has its own scraping logic here.

Flow
----
1. Load the region-contract-map page once in headless Chromium (Playwright).
2. Discover contract-detail links. The page is a "contracts by region map", so
   contracts may already be listed on load, or may require clicking a region to
   reveal a table (JSF server postback — no distinct URL per region). We handle
   both: scan for links on load, and also try clicking each requested region.
3. Visit each contract's detail page (a stable GET-able contractView.jsf?id=URL)
   and extract the scope fields. Volume is tiny (~4-6 per region, ~12-18 total,
   single page, no pagination), so we visit every contract.

Politeness: detail-page visits are throttled by ``rate_limit_seconds`` (same
crawl setting as the municipal collector). We read public pages only and never
download bid documents (those require login).

NOTE on page-structure assumptions: the live RAQS HTML could not be byte-verified
at build time (the site 403s anonymous non-browser clients). The selectors and
field labels below are best-effort; when a region yields no rows the collector
dumps the live page structure (``RAQS DIAG`` log lines) so the real DOM can be
read off a dry-run and the selectors tightened.

# TODO (phase 2b): the per-contract Item List (bulletin/articleView.jsf?articleId=...)
# lists individual line items including pavement-marking spec codes — a stronger
# relevance signal. It requires mapping contract № → article ID across a different
# page hierarchy (fragile), so it is intentionally NOT scraped here. v1 adjudicates
# on the detail-page Contract Description + Classification of Work, which is enough.
"""

import logging
import re
import time
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin, urlsplit

from src.storage.models import Tender

logger = logging.getLogger(__name__)

_CONTRACT_ID_RE = re.compile(r"[?&](?:id|contractId|contract_id)=([^&#]+)", re.IGNORECASE)
# How long to wait for a region's table to settle after a click (kept short so a
# misfiring selector doesn't stall the whole run for the full nav timeout).
_TABLE_WAIT_MS = 8000


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

# Known field labels on the contract detail page. Used both to locate a field's
# value and as delimiters (the value of one field ends where the next label
# begins). ASSUMPTION: these label strings match the live page; verify on first run.
_KNOWN_LABELS = [
    "Contract Number", "Contract No", "Contract #",
    "Tender Owner", "Contract Type", "Owner",
    "Region", "MTO Region",
    "Location", "Highway", "Length",
    "Contract Description", "Description",
    "Classification of Work", "Classification",
    "Tender Opening Date", "Tender Opening", "Opening Date",
    "Tender Advertise Date", "Tender Advertise", "Advertise Date",
    "Tender Closing Date", "Tender Closing", "Closing Date",
]


def _extract_field(text: str, labels: list[str]) -> str:
    """
    Find the value for the first matching label in ``labels``.

    Handles both "Label: value" (inline) and "Label\\nvalue" (label and value on
    separate lines, as JSF table cells often render via inner_text). The value
    runs until the next known field label or a blank line.
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

def _collect_contract_links(page) -> list[tuple[str, str]]:
    """
    Find contract-detail links across ALL frames on the page (the contract table
    may be rendered inside an iframe). Returns [(absolute_url, row_text), ...].
    """
    out: list[tuple[str, str]] = []
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
            out.append((abs_url, (a.inner_text() or "").strip()))
    return out


def _dump_diagnostics(page, label: str) -> None:
    """Log the live page structure so the real DOM can be read off a dry-run."""
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
        srcs = [(f.get_attribute("src") or "") for f in page.query_selector_all("iframe")]
        if srcs:
            logger.warning("RAQS DIAG [%s]: iframe srcs=%s", label, srcs[:10])
    except Exception:
        pass
    try:
        body = re.sub(r"\s+", " ", page.inner_text("body"))
        logger.warning("RAQS DIAG [%s]: body text[:700]=%s", label, body[:700])
    except Exception:
        pass


def _dump_detail_diagnostics(page, row_url: str, bulletin_url: str) -> None:
    """
    One-time: dump candidate contract detail pages so we can confirm which URL
    carries clean per-contract fields and what its labels are. Compares the row
    link (contractsByRegionView) with the canonical bulletin/contractView page.
    """
    candidates = [("regionView", row_url)]
    if bulletin_url:
        candidates.append(("bulletin", bulletin_url))
    for label, url in candidates:
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=_TABLE_WAIT_MS)
            except Exception:
                pass
            body = re.sub(r"\s+", " ", page.inner_text("body"))
        except Exception as exc:
            logger.warning("RAQS DETAIL DIAG [%s]: load failed %s: %s", label, url, exc)
            continue
        present = [lbl for lbl in _KNOWN_LABELS if re.search(re.escape(lbl), body, re.IGNORECASE)]
        logger.warning("RAQS DETAIL DIAG [%s]: url=%s title=%r labels_present=%s",
                       label, url, page.title(), present)
        logger.warning("RAQS DETAIL DIAG [%s]: body[:1400]=%s", label, body[:1400])


# ── Playwright scraping ───────────────────────────────────────────────────────

def _click_region(page, region: str) -> bool:
    """Try to click the control for a region. Returns True if something was clicked."""
    # ASSUMPTION: the region is a clickable element whose visible text is the
    # region name. Try a few strategies before giving up.
    for selector in (
        f"a:has-text(\"{region}\")",
        f"button:has-text(\"{region}\")",
        f"text=\"{region}\"",
        f":text(\"{region}\")",
    ):
        try:
            el = page.locator(selector).first
            if el.count() > 0:
                el.click(timeout=_TABLE_WAIT_MS)
                return True
        except Exception as exc:
            logger.debug("RAQS: region %s selector %r failed: %s", region, selector, exc)
    return False


def _scrape_region_links(page, region: str) -> list[tuple[str, str]]:
    """Click the region control, let it settle, and return its contract links."""
    if not _click_region(page, region):
        logger.warning("RAQS: could not find a clickable control for region %s", region)
        return []
    try:
        page.wait_for_load_state("networkidle", timeout=_TABLE_WAIT_MS)
    except Exception:
        pass
    links = _collect_contract_links(page)
    if not links:
        logger.warning("RAQS: no contract rows appeared for region %s", region)
        _dump_diagnostics(page, f"region={region}")
        return []
    ids = sorted({
        (_CONTRACT_ID_RE.search(u).group(1) if _CONTRACT_ID_RE.search(u) else u)
        for u, _ in links
    })
    logger.info("RAQS: region %s — %d contract rows; ids=%s", region, len(links), ids)
    return links


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
        "region_field":   _extract_field(text, ["Region", "MTO Region"]),
        "location":       _extract_field(text, ["Location"]),
        "highway":        _extract_field(text, ["Highway"]),
        "length":         _extract_field(text, ["Length"]),
        "description":    _extract_field(text, ["Contract Description", "Description"]),
        "classification": _extract_field(text, ["Classification of Work", "Classification"]),
        "opening_date":   _extract_field(text, ["Tender Opening Date", "Tender Opening", "Opening Date",
                                                "Tender Closing Date", "Tender Closing", "Closing Date"]),
        "advertise_date": _extract_field(text, ["Tender Advertise Date", "Tender Advertise", "Advertise Date"]),
    }


def _build_tender(fields: dict, region: str, detail_url: str, source_id: str) -> Optional[Tender]:
    contract_no = fields.get("contract_no", "").strip()
    if not contract_no:
        # Fall back to the URL id so we still get a stable dedup key
        m = _CONTRACT_ID_RE.search(detail_url)
        contract_no = f"id-{m.group(1)}" if m else ""
    if not contract_no:
        logger.warning("RAQS: skipping contract with no number/id at %s", detail_url)
        return None

    # Prefer the region we navigated under; fall back to the detail-page field.
    region = (region or "").strip() or fields.get("region_field", "").strip() or "Unknown"

    classification = fields.get("classification", "").strip()
    location = fields.get("location", "").strip()
    highway = fields.get("highway", "").strip()
    length = fields.get("length", "").strip()
    scope = fields.get("description", "").strip()
    contract_type = fields.get("contract_type", "").strip()

    # title: "<Contract No> — <Classification> — <Location/Highway>"
    where = highway or location
    title_bits = [contract_no]
    if classification:
        title_bits.append(classification)
    if where:
        title_bits.append(where)
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
        raw={"region": region, **fields},
        bid_categories=[classification] if classification else [],
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

    Returns a list of Tender objects (one per contract). Mirrors the municipal
    collector's contract: any unrecoverable error raises (the pipeline wraps this
    call in try/except for fault isolation); per-contract errors are swallowed so
    one bad contract never aborts the whole source.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    timeout_ms = timeout_seconds * 1000
    allowed = {r.strip().lower() for r in regions}
    tenders: list[Tender] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=user_agent)
        page = ctx.new_page()

        # Load the region map robustly. The site has shown a transient
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
                logger.warning("RAQS: region map load timed out (attempt %d/2)", attempt + 1)
                time.sleep(rate_limit_seconds)
        if not loaded:
            browser.close()
            raise RuntimeError("RAQS region map page failed to load")

        sp = urlsplit(region_map_url)
        base = f"{sp.scheme}://{sp.netloc}"

        # Always dump the map structure once so a dry-run reveals the real DOM.
        _dump_diagnostics(page, "initial-load")

        # Attribute each contract to a region by clicking that region's tab and
        # recording which contracts appear under it. First region wins; we log any
        # contract that shows up under more than one region so we can see whether
        # the tabs actually filter (the per-region ids= log lines reveal this).
        region_of: dict[str, str] = {}
        for region in regions:
            try:
                links = _scrape_region_links(page, region)
            except Exception as exc:
                logger.error("RAQS: failed to scrape region %s: %s", region, exc)
                continue
            for url, _txt in links:
                if url in region_of and region_of[url] != region:
                    logger.warning(
                        "RAQS: %s appears under both %s and %s — tabs may not filter",
                        url, region_of[url], region,
                    )
                region_of.setdefault(url, region)

        # Fallback: if no region tab yielded anything, use links present on load
        # (region then comes from the detail page's Region field).
        if not region_of:
            for url, _txt in _collect_contract_links(page):
                region_of[url] = ""

        logger.info("RAQS: %d candidate contracts discovered", len(region_of))

        # One-time detail-page diagnostics on the first contract.
        if region_of:
            first_url = next(iter(region_of))
            m = _CONTRACT_ID_RE.search(first_url)
            bulletin_url = (
                f"{base}/public/bulletin/contractView.jsf?id={m.group(1)}" if m else ""
            )
            try:
                _dump_detail_diagnostics(page, first_url, bulletin_url)
            except Exception as exc:
                logger.warning("RAQS: detail diagnostics failed: %s", exc)

        # Visit each contract detail page (throttled — be polite to a gov site).
        for url, region in region_of.items():
            try:
                fields = _scrape_detail(page, url, timeout_ms)
                eff_region = (region or fields.get("region_field", "")).strip()
                # Filter to requested regions when we can determine the region.
                if allowed and eff_region and eff_region.lower() not in allowed:
                    logger.info("RAQS: skipping %s — region %r not in %s", url, eff_region, regions)
                    continue
                tender = _build_tender(fields, eff_region, url, source_id)
                if tender:
                    tenders.append(tender)
            except Exception as exc:
                logger.warning("RAQS: failed to read contract %s: %s", url, exc)
            finally:
                time.sleep(rate_limit_seconds)

        browser.close()

    logger.info("RAQS: %d contracts collected", len(tenders))
    if tenders:
        sample = tenders[0]
        logger.info(
            "RAQS: sample [%s] %s — desc[%d chars]: %s",
            sample.reference_no, sample.title[:80], len(sample.description),
            (sample.description[:120] + "…") if len(sample.description) > 120 else sample.description,
        )
    return tenders
