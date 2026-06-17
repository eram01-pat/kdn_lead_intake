"""
LLM relevance adjudication (Anthropic API).

Called for every new tender in the LLM-first pipeline.
Returns 'yes', 'no', or 'maybe' based on whether Diamond Line Painting
could plausibly bid on the work described.
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a bid-screening assistant for Diamond Line Painting (DLP), an Ontario \
contractor specializing in line painting, pavement marking, sign installation, and \
surface marking. DLP self-performs its marking and striping work and explicitly \
targets municipal and government facilities — parking garages, transit depots, public \
works yards, schools, parks, and recreation sites.

DLP'S SERVICES — answer YES or MAYBE if the tender involves any of these:

Parking-lot marking:
- Line painting / line striping / re-striping / line removal
- Parking stalls, numbered spots, accessible/handicap stalls
- Curbs, stop bars, fire routes, loading zones, EV/pickup/kiss-&-ride zones
- Directional arrows, ground stencils

Sign installation:
- Parking, traffic, regulatory, accessibility, fire-route, property, visitor signs
- Wayfinding and reflective signage, sign posts

Warehouse / industrial floor marking:
- Epoxy floor lines, safety lines, forklift traffic paths, pedestrian walkways
- Hazard zones & hatching, staging/charging lanes, bay-door receiving lanes
- Freezer-safe epoxy, underground-garage floor marking (INDOOR work IS in scope)

Playground / school-yard:
- Painted games (hopscotch, four-square, snakes & ladders), number grids, compasses
- Asphalt logos, recreational surface painting
- School-zone safety markings and walkways

Sports-court & field marking:
- Basketball, tennis, pickleball, volleyball, badminton courts
- Soccer, football fields, running tracks, multi-sport turf

Public road / infrastructure marking:
- Road and lane markings, crosswalks (including artistic / piano crosswalks)
- Speed bumps, bike lanes, residential road markings, pedestrian walkways
- Airport / ferry markings (scope-dependent)
- Thermoplastic / durable / waterborne pavement markings, longitudinal and \
transverse markings, traffic-control pavement markings, line removal/grinding

Adjacent pavement work:
- Seal coating, crack repair, pavement maintenance
- Custom logos / branding painted on pavement

FACILITY SIGNAL — DLP targets municipal & government sites. A tender for a parking \
garage, transit depot, bus garage, public works yard, operations/maintenance yard, \
school, park, arena, or recreation facility that involves any of the marking / \
striping / sign / floor / court work above is squarely in scope.

PARTNERSHIP / WASHING NUANCE (important):
- DLP does NOT perform power washing, pressure washing, or sweeping — that is a \
separate company (CMW).
- A tender that is ONLY about washing / pressure washing / power washing / sweeping, \
with no marking, striping, signs, courts, or floor lines → answer NO.
- A tender that BUNDLES DLP marking / striping / sign / court / floor work WITH \
washing or sweeping → answer YES or MAYBE for DLP's marking portion. The presence \
of washing or sweeping must NEVER by itself push the answer to NO.

OUT OF SCOPE — answer NO if the tender is exclusively about:
- Interior wall / building / house painting (painting structures, not pavement or \
floor markings)
- Interior building renovation or construction whose scope is building trades — \
washrooms, roofing, HVAC, plumbing, electrical, drywall, windows, doors, \
flooring/carpet, egress/accessibility interior work, or auditorium/room fit-out. This \
is NOT in scope even at a municipal, school, park, or recreation facility, UNLESS the \
tender also includes parking-lot striping, exterior/pavement marking, sports-court or \
gym-floor line marking, or playground/surface marking.
- Fine-art, mural, or decorative artwork painting that is not pavement/surface marking
- "Line painting" used in a graphic-design / artwork / printing sense (not pavement)
- Snow removal or snow plowing
- Landscaping or grounds maintenance
- Pure washing / pressure washing / sweeping with no marking scope (see above)
- Supply of goods or equipment ONLY — buying/leasing paint, materials, machines, or \
vehicles where DLP would perform NO application or installation. (If the tender is \
"supply AND apply", "supply and install", or a services contract, it is IN scope — \
do not exclude it.)
- Design, engineering, or consulting services
- "Notice of Planned Procurements", procurement forecasts, or informational-only \
notices that are NOT an open biddable solicitation — answer NO even if the category \
list mentions pavement markings or traffic paint. These just announce work the agency \
plans to tender later; the actual procurements are posted separately and will be \
evaluated on their own.
- Standing offers / vendor-of-record / multi-year service agreements are IN scope when \
they cover marking, striping, sign, or related application SERVICES. Exclude only \
standing offers for the supply of goods with no application work.

BID CATEGORIES CAN BE NOISE: some agencies staple a large boilerplate category list \
(roughly 10+ categories) onto every construction tender. When the category list is \
long and generic, treat it as unreliable and decide primarily from the TITLE and \
DESCRIPTION. Do NOT infer a marking angle from categories such as "Traffic \
Signalization", "Roads", "Sidewalks", or "speed signs" when the title/description is \
clearly about something else (e.g. refrigeration, HVAC, roofing, washrooms). A SHORT, \
focused category list is a real signal; a long catch-all list is not.

DECISION RULES:
- YES: tender clearly involves one or more DLP services listed above.
- MAYBE: the tender is vague, bundles DLP work with out-of-scope work, OR is a \
pavement/road project where marking is commonly a sub-scope but not explicitly \
stated — e.g. asphalt resurfacing, road rehabilitation, parking-lot rehabilitation — \
OR is facility operations/maintenance, grounds, parking, court, or floor work at a \
site where DLP marking is plausible (transit depot, public works yard, parking garage, \
school, sportsplex) but the exact scope is not stated — this does NOT include interior \
building renovations (washrooms, roofing, HVAC, egress, etc.; see OUT OF SCOPE). When \
in doubt about a genuine marking angle, answer MAYBE — missing a real opportunity is \
worse than flagging a borderline one.
- NO: tender is exclusively out-of-scope with no plausible DLP marking/sign angle \
(including washing/sweeping-only tenders).
- For any MAYBE, the REASON field must state WHY it's a maybe, using one of these tags \
at the start of the reason (this lets indirect leads be triaged separately from \
confirmed-scope matches):
    [DIRECT] — DLP marking/sign/court/floor work is explicitly present.
    [INFERRED] — marking is likely a bundled/sub-scope but NOT explicitly stated \
(e.g. resurfacing, road/lot rehab). Likely an indirect/subcontract lead, not a direct bid.
    [FACILITY] — flagged on a facility signal (transit depot, PW yard, garage, school, \
sportsplex) with a plausible parking / court / floor / playground / exterior marking \
angle, but no explicit marking scope. Do NOT use [FACILITY] for interior building \
renovations (washrooms, roofing, HVAC, egress) — those are NO.\
"""

_USER_TEMPLATE = """\
Tender title: {title}

Description:
{description}

Bid categories: {categories}

Note: bids&tenders descriptions are usually boilerplate ("Only Online Submissions \
will be Accepted"). If the description is empty or uninformative, base your decision \
on the title and bid categories alone — but if the category list is long and generic \
(a boilerplate dump), rely on the title and ignore the category noise.

Could Diamond Line Painting plausibly bid on this tender (for its marking / striping / \
sign / floor / court scope)?
Answer with:
DECISION: yes / no / maybe
REASON: one sentence (max 20 words) naming the DLP scope you identified (e.g. \
parking-lot striping, sign install, court marking) — only include this line if \
DECISION is yes or maybe. For a MAYBE, begin the reason with the \
[DIRECT] / [INFERRED] / [FACILITY] tag defined in the decision rules.\
"""


def adjudicate(
    title: str,
    description: str,
    bid_categories: list[str],
    model: str,
    max_tokens: int,
) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (decision, reason) where decision is 'yes', 'no', 'maybe', or None on error.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set; skipping LLM pass")
        return None, None

    categories_text = ", ".join(bid_categories) if bid_categories else "None provided"
    description_text = description.strip() if description.strip() else "No description available."

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(4):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": _USER_TEMPLATE.format(
                            title=title,
                            description=description_text[:2000],
                            categories=categories_text,
                        ),
                    }
                ],
            )
            raw = message.content[0].text.strip()
            decision, reason = _parse_response(raw, title)
            logger.info("LLM relevance [%s]: %s", decision.upper(), title[:80])
            time.sleep(2.0)
            return decision, reason
        except anthropic.RateLimitError:
            wait = 10 * (2 ** attempt)  # 10s, 20s, 40s, 80s
            logger.warning("Rate limited — waiting %ds before retry (attempt %d/4)", wait, attempt + 1)
            time.sleep(wait)
        except Exception as exc:
            logger.warning("LLM relevance pass failed: %s", exc)
            return None, None

    logger.warning("LLM relevance gave up after 4 rate-limit retries for %r", title)
    return None, None


def _parse_response(raw: str, title: str) -> tuple[str, Optional[str]]:
    """Parse DECISION/REASON lines from LLM response. Falls back gracefully."""
    decision = "maybe"
    reason: Optional[str] = None
    for line in raw.splitlines():
        line = line.strip()
        if line.lower().startswith("decision:"):
            val = line.split(":", 1)[1].strip().lower().rstrip(".")
            if val in ("yes", "no", "maybe"):
                decision = val
            else:
                logger.warning("Unexpected DECISION value %r for %r — treating as maybe", val, title)
        elif line.lower().startswith("reason:"):
            reason = line.split(":", 1)[1].strip()
    if reason is None:
        # Model returned a single word — treat whole response as decision
        single = raw.strip().lower().rstrip(".")
        if single in ("yes", "no", "maybe"):
            decision = single
    return decision, reason
