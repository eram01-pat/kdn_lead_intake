# DLP Tender Monitor

Automated monitor for Diamond Line Painting (DLP) that watches 17 Ontario
municipal procurement portals, adjudicates each open tender for relevance to
DLP's services, and publishes a daily dashboard of relevant opportunities.

## What it does

1. **Collects** open tenders from 17 bids&tenders.ca portals (all same platform — one collector)
2. **Adjudicates** every new tender once with Claude (`src/matching/relevance.py`) → `yes` / `no` / `maybe`
3. **Publishes** a static HTML dashboard to GitHub Pages — no server required

> **Architecture note:** relevance is decided entirely by the Claude adjudication
> prompt in `src/matching/relevance.py`. There is no keyword pre-filter — that prompt
> is the whole relevance engine. Edit it to change what counts as in-scope.

## What it does NOT do

- No bidding, no form submission, no document downloads, no login
- Does not cover MERX, City of Ottawa, or City of Toronto (deliberately excluded)
- Only reads publicly available pages while logged out

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/eram01-pat/dlp_lead_intake.git
cd dlp_lead_intake
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure GitHub Secrets / environment variables

These are the only environment variables the code reads:

| Secret / env var    | Required | Read by | Purpose                                                    |
|---------------------|----------|---------|------------------------------------------------------------|
| `DATABASE_URL`      | **Yes**  | `src/storage/db.py` | Neon (Postgres) connection string. Pipeline crashes without it. |
| `ANTHROPIC_API_KEY` | **Yes**\*| `src/matching/relevance.py` | Claude key for the adjudication pass. Without it every tender returns no decision and the dashboard is empty. |
| `SLACK_WEBHOOK_URL` | No       | `src/notifications/slack.py` | Slack alerts + weekly report. Skipped silently if unset. |

\* Optional in code (calls are guarded), but the pipeline produces nothing useful without it — treat as required.

### 3. Enable GitHub Pages

In repository Settings → Pages → Source: **GitHub Actions**.

### 4. Run manually

```bash
# Full run (collect, adjudicate, build dashboard)
python pipeline.py

# Single source (for testing/debugging)
python pipeline.py --source vaughan

# Dry run (no DB writes; adjudicates and logs matches to stdout — still calls the Anthropic API)
python pipeline.py --dry-run
```

---

## Configuration

| File                          | What to edit                                                 |
|-------------------------------|--------------------------------------------------------------|
| `config/sources.yaml`         | Add/remove a municipality (one-line change)                  |
| `config/settings.yaml`        | Rate limits, LLM model/toggle                                |
| `src/matching/relevance.py`   | The adjudication prompt — what counts as in-scope (see below) |

### Adding a municipality

Edit `config/sources.yaml` — add one line:
```yaml
- {id: newcity, name: "City of New City", base_url: "https://newcity.bidsandtenders.ca"}
```
That's all. The collector handles it automatically.

### Tuning relevance

Relevance is decided by the `_SYSTEM_PROMPT` / `_USER_TEMPLATE` in
`src/matching/relevance.py`. The prompt returns `yes` / `no` / `maybe` for each tender.

**If the dashboard is too noisy:** tighten the OUT-OF-SCOPE rules in the prompt.
**If real opportunities are being missed:** broaden the in-scope service list or the
MAYBE rule.

---

## State persistence

State lives in **Neon (Postgres)**, reached via the `DATABASE_URL` environment
variable (`src/storage/db.py`). The `tenders` table stores each tender plus its
cached `llm_decision` (`yes` / `no` / `maybe`) — there is no separate matches table;
the decision on the row *is* the match record. The dashboard and weekly report query
this table directly.

Each tender is adjudicated by Claude exactly once; the decision is cached so repeat
runs don't re-spend API calls on tenders already seen.

---

## Relevance engine (Claude)

Relevance is decided by a single Claude call per new tender in
`src/matching/relevance.py` (`adjudicate()`), configured under `llm:` in
`config/settings.yaml` (model + max tokens). It runs on **every** new tender — there
is no keyword pre-filter.

The prompt asks whether the tender is plausibly in scope for Diamond Line Painting's
services (line painting, pavement marking, sign installation, warehouse/floor marking,
playground/school-yard, sports-court & field marking, public-road marking, and adjacent
pavement work) and returns:
- `yes` → High confidence (main feed)
- `maybe` → Medium confidence (main feed)
- `no` → dropped

> Diamond does **not** self-perform power washing / pressure washing / sweeping (that is
> a separate company, CMW). A washing-only tender is `no`. A tender that bundles Diamond
> marking/striping/sign/court/floor work *with* washing or sweeping is still relevant —
> the washing mention alone never forces a `no`.

---

## Discovery / troubleshooting

See `ACCESS_NOTES.md` for platform discovery notes and the verification checklist
that must be completed before first production run.

If a source breaks (structure change), it logs an error and continues with remaining
sources. The pipeline exits with code 2 if any sources failed (unless `--ignore-errors`
is passed in CI).

---

## Phase-2 upgrade paths (not built in v1)

- **Interactive dashboard**: mark tenders reviewed/dismissed, shared state across
  team — requires a small backend (FastAPI) over the existing Neon DB
- **Email digest**: daily summary of High/Medium matches — thin add-on over same data
- **LLM detail-page enrichment**: fetch tender detail pages for tenders that only had
  a listing-level description
