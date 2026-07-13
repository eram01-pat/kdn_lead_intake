"""
Parameterized collector for the bids&tenders.ca (eSolutionsGroup) platform.

All 25 municipalities share this one collector — only base_url differs.

The platform requires JavaScript execution before the search AJAX call will
succeed (JS sets additional cookies / prepares state). We use Playwright
(headless Chromium) to load the listing page once, intercept the AJAX
response, and capture the JSON. The search endpoint returns 25 items per
page, so when the reported total exceeds the first page we replay the same
search POST from inside the page with an incremented page parameter until
every item is captured. Subsequent detail-page fetches use requests.
"""

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.storage.models import Tender

logger = logging.getLogger(__name__)

_LISTING_PATH = "/Module/Tenders/en"
_DETAIL_PATH  = "/Module/Tenders/en/Tender/Detail"
_CACHE_FILE   = "data/module_endpoints.yaml"
_MAX_SEARCH_PAGES = 40  # safety cap on pagination requests per source

_REF_PREFIX_RE = re.compile(r"^([A-Z]{1,8}\d{2}-\d{2,5}[A-Z]?)\s*[-–]\s*", re.IGNORECASE)
_GUID_RE       = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_aspnet_date(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value)
    m = re.search(r"/Date\((-?\d+)(?:[+-]\d+)?\)/", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


# ── HTML helpers ──────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(p.strip() for p in self._parts if p.strip())


def _strip_html(html: str) -> str:
    if not html:
        return ""
    p = _HTMLStripper()
    p.feed(html)
    return p.get_text()


def _extract_module_guid(html: str) -> Optional[str]:
    m = re.search(
        r'/Module/Tenders/en/Tender/Search/(' + _GUID_RE.pattern + r')',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'["\'](?:/[^"\']*)?/Tender/Search/(' + _GUID_RE.pattern + r')["\']',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else None


# ── GUID cache ────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    p = Path(_CACHE_FILE)
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def _save_cache(cache: dict) -> None:
    Path(_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(_CACHE_FILE).write_text(yaml.dump(cache, default_flow_style=False))


# ── Search pagination helpers ─────────────────────────────────────────────────

_PAGE_NUMBER_KEYS = {"page", "pagenumber", "pagenum", "currentpage", "pageindex"}
_PAGE_OFFSET_KEYS = {"start", "skip", "offset", "startindex"}


def _decode_search_payload(post_data: Optional[str]) -> tuple[Any, str]:
    """
    Decode a captured search POST body.
    Returns (payload, kind) where kind is 'json' or 'form'; (None, '') if
    the body can't be decoded.
    """
    if not post_data:
        return None, ""
    try:
        return json.loads(post_data), "json"
    except ValueError:
        pass
    from urllib.parse import parse_qs
    try:
        parsed = parse_qs(post_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None, ""
    return {k: v[0] for k, v in parsed.items()}, "form"


def _apply_paging(payload: Any, page_no: int, page_size: int) -> bool:
    """
    Recursively set every recognizable paging field in a decoded payload to
    request page `page_no` (1-based). Sets ALL matching fields — e.g. a
    Kendo-style request carries both page=N and skip=M, and they must stay
    consistent. Returns True if at least one field was set.
    """
    found = False
    if isinstance(payload, dict):
        for key, value in payload.items():
            norm = key.lower().replace("_", "").replace("-", "")
            if norm in _PAGE_NUMBER_KEYS:
                # Respect the site's numbering base: first page captured as 0 → 0-based.
                try:
                    zero_based = int(str(value)) == 0
                except (TypeError, ValueError):
                    zero_based = False
                new_value: Any = page_no - 1 if zero_based else page_no
                payload[key] = str(new_value) if isinstance(value, str) else new_value
                found = True
            elif norm in _PAGE_OFFSET_KEYS:
                new_value = (page_no - 1) * page_size
                payload[key] = str(new_value) if isinstance(value, str) else new_value
                found = True
            else:
                found = _apply_paging(value, page_no, page_size) or found
    elif isinstance(payload, list):
        for value in payload:
            found = _apply_paging(value, page_no, page_size) or found
    return found


def _build_page_request(
    url: str, post_data: str, page_no: int, page_size: int
) -> Optional[tuple[str, str]]:
    """
    Rewrite the captured first-page search request to ask for page `page_no`
    (1-based). The paging fields may live in the URL query string (Kendo-style
    ?page=1&skip=0&take=25) or in the POST body. Returns (url, body), or None
    if neither carries a recognizable paging field.
    """
    from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.query:
        params = {
            k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()
        }
        if _apply_paging(params, page_no, page_size):
            return urlunsplit(parts._replace(query=urlencode(params))), post_data

    payload, kind = _decode_search_payload(post_data)
    if payload is not None and _apply_paging(payload, page_no, page_size):
        body = json.dumps(payload) if kind == "json" else urlencode(payload)
        return url, body
    return None


# ── Playwright search ─────────────────────────────────────────────────────────

_FETCH_PAGE_JS = """async ([url, body, contentType]) => {
    const headers = {'X-Requested-With': 'XMLHttpRequest'};
    if (contentType) headers['Content-Type'] = contentType;
    const resp = await fetch(url, {method: 'POST', headers, body, credentials: 'include'});
    if (!resp.ok) return {error: 'HTTP ' + resp.status};
    await resp.text();  // consume; the response listener captures the items
    return {ok: true};
}"""

# Candidate selectors for the listing's next-page control, tried in order.
# Covers PagedList (ASP.NET), Kendo UI pagers, Bootstrap pagination, and
# generic "Next" / "Load More" buttons.
_NEXT_PAGE_SELECTORS = [
    "li.PagedList-skipToNext a",
    "a[rel='next']",
    "a[aria-label='Next']",
    "a[aria-label='Next page']",
    "a[aria-label='Go to the next page']",
    "a.k-link[title='Go to the next page']",
    "li.next:not(.disabled) a",
    "button[aria-label*='Next']",
    "a:has-text('Next')",
    "button:has-text('Next')",
    "a:has-text('»')",
    "button:has-text('Load More')",
    "a:has-text('Load More')",
]

# Diagnostic dump of pagination-ish markup, logged when no control matches so
# the real structure can be identified from CI logs.
_PAGER_MARKUP_JS = """() => {
    const els = document.querySelectorAll(
        "[class*='pag' i], [id*='pag' i], .k-pager-wrap, nav ul");
    return Array.from(els).slice(0, 5)
        .map(e => e.outerHTML.slice(0, 300)).join('\\n---\\n');
}"""


def _click_through_pages(
    page, source_id: str, total: int, count_items, rate_limit_seconds: float
) -> None:
    """
    Drive the listing's own pagination UI: click the next-page control and let
    the response listener capture each page's AJAX response. Works regardless
    of how the request encodes the page number.
    """
    clicks = 0
    while count_items() < total and clicks < _MAX_SEARCH_PAGES:
        target = None
        for sel in _NEXT_PAGE_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    target = loc
                    break
            except Exception:
                continue
        if target is None:
            try:
                markup = page.evaluate(_PAGER_MARKUP_JS)
            except Exception:
                markup = ""
            logger.warning(
                "%s: no next-page control recognized — candidate pager markup:\n%s",
                source_id, (markup or "(none found)")[:2000],
            )
            return
        before = count_items()
        try:
            with page.expect_response(
                lambda r: "/Tender/Search/" in r.url and r.request.method == "POST",
                timeout=15_000,
            ):
                target.click()
            page.wait_for_timeout(500)  # let the response listener process the body
        except Exception as exc:
            logger.warning("%s: next-page click failed: %s", source_id, exc)
            return
        if count_items() <= before:
            logger.warning(
                "%s: next-page click produced no new items — stopping", source_id
            )
            return
        clicks += 1
        logger.info(
            "%s: clicked to page %d: %d/%d items",
            source_id, clicks + 1, count_items(), total,
        )
        time.sleep(rate_limit_seconds)


def _fetch_via_playwright(
    base_url: str,
    source_id: str,
    user_agent: str,
    timeout_seconds: int,
    max_per_source: int,
    rate_limit_seconds: float,
) -> tuple[list[dict], str, dict]:
    """
    Load the listing page in headless Chromium, intercept every AJAX search
    response, page through any remaining results, and return
    (raw_items, module_guid, cookies_dict).
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    all_items: list[dict] = []
    seen_ids: set[str] = set()
    guid_found: list[str] = []
    search_request: dict = {}   # url / post_data / content_type of the first search POST
    total_reported: list[int] = [0]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=user_agent)
        page = ctx.new_page()

        # Accumulates every search AJAX response — the initial page load, our
        # replayed page fetches, and UI-driven pagination alike. Dedupes by
        # platform Id so re-requests of an already-seen page are harmless.
        def on_response(response):
            url = response.url
            if "/Tender/Search/" in url and response.request.method == "POST":
                if not guid_found:
                    m = _GUID_RE.search(url)
                    if m:
                        guid_found.append(m.group(0))
                try:
                    body = response.json()
                    items = body.get("data") or []
                    try:
                        total_reported[0] = max(total_reported[0], int(body.get("total") or 0))
                    except (TypeError, ValueError):
                        pass
                    new_items = [it for it in items if str(it.get("Id")) not in seen_ids]
                    if new_items:
                        seen_ids.update(str(it.get("Id")) for it in new_items)
                        all_items.extend(new_items)
                        if not search_request:
                            search_request.update(
                                url=url,
                                post_data=response.request.post_data or "",
                                content_type=response.request.headers.get("content-type", ""),
                            )
                        logger.info(
                            "%s: captured %d items (total reported: %s)",
                            source_id, len(new_items), body.get("total"),
                        )
                except Exception as exc:
                    logger.debug("%s: could not parse AJAX response: %s", source_id, exc)

        page.on("response", on_response)

        try:
            page.goto(
                f"{base_url}{_LISTING_PATH}",
                timeout=timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
            page.wait_for_load_state("networkidle", timeout=timeout_seconds * 1000)
        except PWTimeout:
            logger.warning("%s: page load timed out — using whatever was captured", source_id)

        # The listing AJAX returns one page (25 items). First try replaying the
        # search request with an incremented page parameter (in the URL query
        # or POST body); if the request carries no recognizable paging field,
        # fall back to clicking the listing's own next-page control.
        total = total_reported[0]
        if all_items and total > len(all_items) and search_request:
            page_size = len(all_items)
            page_no = 2
            while len(all_items) < total and page_no <= _MAX_SEARCH_PAGES:
                paged = _build_page_request(
                    search_request["url"], search_request["post_data"], page_no, page_size
                )
                if paged is None:
                    logger.info(
                        "%s: no paging field in search request (url: %s) — "
                        "falling back to UI pagination",
                        source_id, search_request["url"][:200],
                    )
                    break
                before = len(all_items)
                try:
                    result = page.evaluate(
                        _FETCH_PAGE_JS,
                        [paged[0], paged[1], search_request["content_type"]],
                    )
                except Exception as exc:
                    logger.warning("%s: page %d fetch failed: %s", source_id, page_no, exc)
                    break
                if not isinstance(result, dict) or result.get("error"):
                    logger.warning(
                        "%s: page %d fetch returned %s", source_id, page_no,
                        result.get("error") if isinstance(result, dict) else type(result),
                    )
                    break
                page.wait_for_timeout(500)  # let the response listener process the body
                if len(all_items) <= before:
                    logger.warning(
                        "%s: replayed page %d returned no new items — stopping replay",
                        source_id, page_no,
                    )
                    break
                page_no += 1
                time.sleep(rate_limit_seconds)

            if len(all_items) < total:
                _click_through_pages(
                    page, source_id, total, lambda: len(all_items), rate_limit_seconds
                )

        page.remove_listener("response", on_response)
        pw_cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        browser.close()

    if total_reported[0] and len(all_items) < total_reported[0]:
        logger.warning(
            "%s: captured only %d of %d reported items — some tenders were NOT collected",
            source_id, len(all_items), total_reported[0],
        )

    guid = guid_found[0] if guid_found else ""

    if max_per_source and len(all_items) > max_per_source:
        all_items = all_items[:max_per_source]

    return all_items, guid, pw_cookies


# ── Detail page ───────────────────────────────────────────────────────────────

def _make_session(user_agent: str, cookies: dict) -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    s.mount("http://",  HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    s.headers.update({
        "User-Agent":      user_agent,
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    for name, value in cookies.items():
        s.cookies.set(name, value)
    return s


def _extract_categories_from_html(html: str) -> list[str]:
    m = re.search(
        r'<div[^>]*\bid="divCat"[^>]*>(.*?)</div>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    div_content = m.group(1)
    seen: set[str] = set()
    categories: list[str] = []
    for hit in re.finditer(
        r"<li[^>]*>(.*?)(?=<ul|</li>)", div_content, re.DOTALL | re.IGNORECASE
    ):
        text = _strip_html(hit.group(1)).strip()
        if text and text not in seen:
            seen.add(text)
            categories.append(text)
    return categories


def _extract_description_from_html(html: str) -> str:
    """
    Extract the tender scope description from the detail page.

    Tries several patterns used by different versions of the eSolutionsGroup
    bids&tenders platform, falling back gracefully.
    """
    raw = ""

    # Pattern 1: named div like categories uses (id="divDesc" or "divDescription")
    for div_id in ("divDesc", "divDescription", "divScope"):
        m = re.search(
            r'<div[^>]*\bid="' + div_id + r'"[^>]*>(.*?)</div>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            raw = m.group(1)
            break

    # Pattern 2: <td> label/value layout  "<td>Description:</td><td>...</td>"
    if not raw:
        m = re.search(
            r'<td[^>]*>\s*Description\s*:?\s*</td>\s*<td[^>]*>(.*?)</td>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            raw = m.group(1)

    # Pattern 3: labelled <span> or <p> following a "Description" heading
    if not raw:
        m = re.search(
            r'(?:Description|Scope\s+of\s+Work)\s*:?\s*</[^>]+>\s*<[^>]+>(.*?)</(?:p|span|div|td)>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            raw = m.group(1)

    if not raw:
        # Emit a one-line debug snippet so we can identify the real structure next run
        snippet = re.sub(r'\s+', ' ', html[:3000])
        logger.debug("Description field not found; HTML head: %s", snippet[:500])
        return ""

    text = _strip_html(raw)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _fetch_detail_page(
    session: requests.Session,
    detail_url: str,
    timeout: int,
    rate_limit: float,
) -> tuple[list[str], str]:
    """Returns (bid_categories, description_text). Both empty on error."""
    try:
        r = session.get(detail_url, timeout=timeout, headers={"Accept": "text/html,*/*"})
        if r.status_code != 200:
            logger.debug("Detail page %s returned %s", detail_url, r.status_code)
            return [], ""
        time.sleep(rate_limit)
        categories = _extract_categories_from_html(r.text)
        description = _extract_description_from_html(r.text)
        if not description and logger.isEnabledFor(logging.DEBUG):
            # Emit surrounding context for any "description" occurrence so we can
            # identify the correct HTML pattern from CI logs
            for m in re.finditer(r'description', r.text, re.IGNORECASE):
                pos = m.start()
                logger.debug(
                    "DESC-PROBE %s pos=%d: %r",
                    detail_url, pos, r.text[max(0, pos-80):pos+200],
                )
        return categories, description
    except Exception as exc:
        logger.debug("Detail fetch error %s: %s", detail_url, exc)
        return [], ""


# ── Item parsing ──────────────────────────────────────────────────────────────

def _tender_id(source_id: str, platform_id: str) -> str:
    return hashlib.sha256(f"{source_id}:{platform_id}".encode()).hexdigest()[:32]


def _extract_ref_no(title: str) -> str:
    m = _REF_PREFIX_RE.match(title)
    return m.group(1).upper() if m else ""


def _parse_item(
    item: dict,
    source_id: str,
    source_name: str,
    base_url: str,
    session: requests.Session,
    timeout: int,
    rate_limit: float,
    fetch_details: bool,
) -> Optional[Tender]:
    try:
        platform_id = str(item.get("Id") or "").strip()
        if not platform_id:
            return None
        title = str(item.get("Title") or "").strip()
        if not title:
            return None

        ref_no     = _extract_ref_no(title)
        detail_url = f"{base_url}{_DETAIL_PATH}/{platform_id}"
        bid_type   = ref_no.split("-")[0] if ref_no else ""

        bid_categories: list[str] = []
        description = ""
        if fetch_details:
            bid_categories, description = _fetch_detail_page(session, detail_url, timeout, rate_limit)

        return Tender(
            id=_tender_id(source_id, platform_id),
            source_id=source_id,
            source_name=source_name,
            title=title,
            description=description,
            category=bid_type,
            reference_no=ref_no,
            detail_url=detail_url,
            status=str(item.get("Status") or "Open").strip(),
            posted_date=_parse_aspnet_date(item.get("DateAvailable")),
            closing_date=_parse_aspnet_date(item.get("DateClosing")),
            raw=item,
            bid_categories=bid_categories,
        )
    except Exception as exc:
        logger.warning("Failed to parse item %r: %s", item.get("Id"), exc)
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def collect(
    source: dict,
    user_agent: str,
    rate_limit_seconds: float = 3.0,
    max_retries: int = 3,
    backoff_base_seconds: float = 5.0,
    timeout_seconds: int = 30,
    max_per_source: int = 0,
    fetch_detail_pages: bool = True,
) -> list[Tender]:
    """
    Collect open tenders from one bids&tenders.ca municipality.
    Uses Playwright (headless Chromium) to load the listing page so that
    page JavaScript runs and the AJAX search succeeds.
    Returns [] on unrecoverable error so the pipeline continues.
    """
    source_id   = source["id"]
    source_name = source["name"]
    base_url    = source["base_url"].rstrip("/")

    logger.info("Collecting %s", source_name)

    try:
        raw_items, guid, pw_cookies = _fetch_via_playwright(
            base_url=base_url,
            source_id=source_id,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            max_per_source=max_per_source,
            rate_limit_seconds=rate_limit_seconds,
        )
    except Exception as exc:
        logger.error("%s: Playwright fetch failed: %s — skipping", source_name, exc)
        return []

    if not raw_items:
        logger.warning("%s: no items captured from AJAX response", source_name)
        return []

    # Cache the GUID if discovered
    if guid:
        cache = _load_cache()
        if source_id not in cache:
            cache[source_id] = guid
            _save_cache(cache)

    session = _make_session(user_agent, pw_cookies)

    tenders: list[Tender] = []
    for item in raw_items:
        t = _parse_item(
            item=item,
            source_id=source_id,
            source_name=source_name,
            base_url=base_url,
            session=session,
            timeout=timeout_seconds,
            rate_limit=rate_limit_seconds,
            fetch_details=fetch_detail_pages,
        )
        if t:
            tenders.append(t)

    logger.info("%s: %d tenders collected", source_name, len(tenders))
    # Log description extraction quality for the first tender (diagnostic)
    if tenders:
        sample = tenders[0]
        logger.info(
            "%s: sample description [%d chars]: %s",
            source_name, len(sample.description),
            (sample.description[:120] + "…") if len(sample.description) > 120 else (sample.description or "(empty)"),
        )
    return tenders
