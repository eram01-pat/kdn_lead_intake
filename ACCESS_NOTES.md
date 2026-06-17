# Phase-0 Discovery Notes — bids&tenders.ca Platform

## Platform Overview

All 17 DLP source municipalities run the **eSolutionsGroup bids&tenders.ca** SaaS platform.
One collector handles all 17 sources; only `base_url` differs.

---

## Confirmed API (verified on vaughan.bidsandtenders.ca, 2026-06-08)

### Step 1 — GET listing page (obtain session cookies + CSRF token)

```
GET https://{municipality}.bidsandtenders.ca/Module/Tenders/en
```

The HTML response contains two things needed for Step 2:

1. **CSRF token** — hidden input field in the page:
   ```html
   <input name="__RequestVerificationToken" value="a8-tzIg7_..." type="hidden" />
   ```

2. **MODULE_GUID** — the per-municipality module instance ID, embedded in the page
   HTML (form action or JS variable), e.g.:
   ```
   /Module/Tenders/en/Tender/Search/83b40e99-2f9a-4b20-9444-9cc522b4c6f5
   ```
   Vaughan's GUID: `83b40e99-2f9a-4b20-9444-9cc522b4c6f5`
   Each municipality has a different GUID. Discovered GUIDs are cached in
   `data/module_endpoints.yaml` so the listing page is only fetched once per
   municipality (once cached, only the search POST is needed).

### Step 2 — POST search

```
POST https://{municipality}.bidsandtenders.ca/Module/Tenders/en/Tender/Search/{MODULE_GUID}
     ?status=Open&limit=100&start=0&dir=ASC&from=&to=&sort=DateClosing+ASC%2CId

Content-Type: application/x-www-form-urlencoded

Body (form-encoded, NOT JSON):
  status=Open
  limit=100
  start=0
  dir=ASC
  from=
  to=
  sort=DateClosing ASC,Id
  __RequestVerificationToken={TOKEN}
```

- **Auth**: none — public endpoint confirmed accessible logged out
- **Pagination**: offset-based via `start` (not page number). `start=0`, `start=100`, etc.

### Response shape

```json
{
  "success": true,
  "data": [ ... ],
  "total": 16
}
```

### Confirmed field names (from `data` array items)

| Field                 | Type          | Notes                                                    |
|-----------------------|---------------|----------------------------------------------------------|
| `Id`                  | GUID string   | Platform tender ID; used as dedup key                    |
| `Title`               | string        | Includes ref prefix: `"T26-180 - Some Title"`            |
| `Status`              | string        | `"Open"`, `"Closed"`, `"Awarded"`                        |
| `Description`         | HTML string   | **Boilerplate** — "Only Online Submissions..." (see note)|
| `DateAvailable`       | `/Date(ms)/`  | ASP.NET JSON date, Unix milliseconds                     |
| `DateClosing`         | `/Date(ms)/`  | Closing date/time                                        |
| `DateClosingDisplay`  | string        | Human-readable, e.g. `"Mon Jun 8, 2026 3:00:00 PM"`     |
| `DaysLeft`            | int           | Days until closing (calculated server-side)              |
| `Scope`               | string        | Always `"Public"` — not a useful category field          |

**Fields NOT present at listing level:**
- No `referenceNumber` — reference prefix is embedded in `Title`, parsed via regex
- No `category` / `TenderType`
- No `detailUrl` — constructed from `Id`

### ⚠️ Descriptions are boilerplate at BOTH listing and detail level

Confirmed on vaughan.bidsandtenders.ca: the `Description` field in the listing API
**and** the "Description" section on the detail page both contain only:
> "Only Online Submissions will be Accepted for this Proposal/Tender"

The actual scope of work is inside the PDF bid documents (e.g. `RFP26-144.pdf`),
which are behind a document fee / login wall. We do not download these.

**Consequence: matching is title-only for this platform.**

This is workable because DLP-relevant tenders will have descriptive titles
("Line Painting Services", "Pavement Marking for Municipal Parking Lots", etc.).
The matching engine is aware of this and operates correctly on titles alone.

`fetch_detail_pages` is disabled by default in `config/settings.yaml` to avoid
unnecessary rate-limited requests that return no useful text.

---

## Detail page

**Confirmed URL pattern:**
```
GET https://{municipality}.bidsandtenders.ca/Module/Tenders/en/Tender/Detail/{tender_Id}
```
Example: `https://vaughan.bidsandtenders.ca/Module/Tenders/en/Tender/Detail/7184877f-3ef6-4fe5-a4fc-0a7c854dcfe6`

Note: singular `Detail`, not `Details`.

---

## robots.txt

`GET /robots.txt` returned HTTP 403 from the automated environment. Load manually
in a browser and record here before going live.

Provisional stance: **polite crawl of public listing + detail pages only**, per
`config/settings.yaml` rate limits.

---

## Generalization

All 17 municipalities use the same platform. The endpoint pattern, field names,
CSRF token mechanism, and `/Date(ms)/` format apply to all 17. The only
per-municipality variable is the MODULE_GUID.

---

## Per-site Quirk Log

| Source ID | Quirk | Resolution |
|-----------|-------|------------|
| *(none yet — add here as each site is verified)* | | |

---

## Remaining Verification Checklist

- [x] **Endpoint URL and method** — `POST /Module/Tenders/en/Tender/Search/{GUID}`
- [x] **POST body format** — form-encoded (not JSON), includes CSRF token
- [x] **Response shape** — `{"success": true, "data": [...], "total": N}`
- [x] **Field names** — `Id`, `Title`, `Status`, `Description`, `DateAvailable`, `DateClosing`
- [x] **Date format** — `/Date(ms)/` Unix milliseconds
- [x] **Detail URL** — `GET /Module/Tenders/en/Tender/Detail/{Id}` (singular)
- [ ] **robots.txt** — load in browser, record contents
- [x] **Second municipality** — Brampton confirmed 2026-06-08:
      GUID `1a0b8c31-b337-4cba-b5c4-db8e6c14d026`, identical URL structure and params.
      "One collector, 17 sources" design validated.
- [ ] **Pagination** — find a municipality with >100 open tenders, confirm `start=100`
      works correctly
- [x] **Detail page content** — description is boilerplate even on detail page;
      real scope is in PDFs. `fetch_detail_pages` disabled by default.
- [ ] **Bid Classification field** — "Services" / "Goods" / "Construction" appears on
      the detail page HTML. If we later want to use it for pre-filtering, probe whether
      it is also available via a detail JSON endpoint (try Accept: application/json on
      the detail URL).
