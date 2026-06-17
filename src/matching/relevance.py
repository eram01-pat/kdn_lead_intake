"""
LLM relevance adjudication (Anthropic API).

Called for every new tender in the LLM-first pipeline.
Returns 'yes', 'no', or 'maybe' based on whether KDN
could plausibly bid on the work described.
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a bid-screening assistant for KDN, an Ontario contractor that self-performs \
pavement markings on public roads and infrastructure — municipal roads, regional roads, \
and highways. KDN's equipment is set up for road work, not facility/parking-lot work.

KDN'S SERVICES — answer YES or MAYBE if the tender involves any of these:

Road & infrastructure pavement marking:
- Road and lane markings — centre lines, edge lines, longitudinal and transverse \
markings
- Crosswalks, stop bars, directional arrows, pavement stencils / legends
- Bike-lane markings and traffic symbols (e.g. elephant's feet, shark's teeth)
- High-visibility markings; temporary markings; water- or oil-based traffic paint; \
thermoplastic / durable / long-term traffic markings

Pavement-marking removal / obliteration (KDN self-performs this):
- Pavement-marking removal, obliteration, or grinding — by water blasting, soda \
blasting, or rotary grinding. NOTE: this is removal of road MARKINGS and is IN scope. \
Do not confuse it with (a) general surface power/pressure washing or cleaning, or \
(b) concrete pavement diamond grinding / grooving for surface texture or smoothness — \
both of those are out of scope (see below).

Road reconstruction / resurfacing (marking is a standard sub-scope):
- Road reconstruction, rehabilitation, widening, or resurfacing projects — pavement \
marking is a standard sub-scope on these, so treat them as YES or MAYBE even when \
marking is not explicitly spelled out.

PARKING LOTS — scope-dependent:
- A parking lot is IN scope ONLY when it is bundled into a larger road / infrastructure \
tender.
- A standalone parking-lot striping tender with no road scope is NO — that is not KDN's \
road equipment / market.

OUT OF SCOPE — answer NO if the tender is exclusively about any of these (KDN does NOT \
do them):
- Standalone parking-lot striping (no road scope)
- Playgrounds / school-yards / painted games
- Sports courts & fields — basketball, tennis, pickleball, running tracks, turf lines
- Warehouse / indoor / factory floor marking — epoxy lines, forklift lanes
- Sign installation or sign supply
- Adjacent pavement work KDN does NOT self-perform — seal coating, crack \
repair/sealing (including "rout and seal" / "rout & seal"), asphalt patching / \
pothole repair, and paving / asphalt placement itself. A paving, patching, or \
rout-and-seal tender with NO marking scope is NO; but a road \
reconstruction / resurfacing tender that will include marking is YES or MAYBE.
- Concrete pavement diamond grinding and/or grooving (surface texturing, \
profiling, smoothness or skid-resistance work on the pavement surface) — this is \
NOT pavement-marking removal, so a diamond-grinding/grooving tender with NO \
marking scope is NO.
- Interior / building / house painting and building construction trades — washrooms, \
roofing, HVAC, plumbing, electrical, drywall, windows, doors, flooring
- Fine-art, mural, or decorative artwork painting; "line painting" used in a \
graphic-design / artwork / printing sense (not pavement)
- Snow removal / plowing, sweeping, and general surface power / pressure washing or \
cleaning (washing a surface clean — NOT water/soda blasting to remove pavement \
markings, which is in scope above)
- Landscaping or grounds maintenance
- Supply of goods or equipment ONLY — buying/leasing paint, materials, machines, or \
vehicles where KDN would perform NO application. (If the tender is "supply AND apply", \
"supply and install", or a services contract, it is IN scope — do not exclude it.)
- Design, engineering, or consulting services
- "Notice of Planned Procurements", procurement forecasts, or informational-only \
notices that are NOT an open biddable solicitation — answer NO even if the category \
list mentions pavement markings or traffic paint. These just announce work the agency \
plans to tender later; the actual procurements are posted separately and will be \
evaluated on their own.
- Standing offers / vendor-of-record / multi-year service agreements are IN scope when \
they cover marking application SERVICES. Exclude only standing offers for the supply of \
goods with no application work.

BID CATEGORIES CAN BE NOISE: some agencies staple a large boilerplate category list \
(roughly 10+ categories) onto every construction tender. When the category list is \
long and generic, treat it as unreliable and decide primarily from the TITLE and \
DESCRIPTION. Do NOT infer a marking angle from categories such as "Traffic \
Signalization", "Roads", "Sidewalks", or "speed signs" when the title/description is \
clearly about something else (e.g. refrigeration, HVAC, roofing, washrooms). A SHORT, \
focused category list is a real signal; a long catch-all list is not.

DECISION RULES:
- YES: tender clearly involves road/infrastructure pavement marking listed above.
- MAYBE: the tender is vague, OR is a road reconstruction / rehabilitation / widening / \
resurfacing project where marking is commonly a sub-scope but not explicitly stated. \
When in doubt about a genuine road-marking angle, answer MAYBE — missing a real \
opportunity is worse than flagging a borderline one.
- NO: tender is exclusively out-of-scope with no plausible KDN road-marking angle.
- For any MAYBE, the REASON field must state WHY it's a maybe, using one of these tags \
at the start of the reason (this lets indirect leads be triaged separately from \
confirmed-scope matches):
    [DIRECT] — road/infrastructure pavement marking is explicitly present.
    [INFERRED] — marking is likely a sub-scope of a road reconstruction / rehab / \
widening / resurfacing project but NOT explicitly stated. Likely an indirect/subcontract \
lead, not a direct bid.\
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

Could KDN plausibly bid on this tender (for its road / infrastructure pavement-marking \
scope)?
Answer with:
DECISION: yes / no / maybe
REASON: one sentence (max 20 words) naming the KDN scope you identified (e.g. \
road/lane marking, crosswalks, resurfacing sub-scope) — only include this line if \
DECISION is yes or maybe. For a MAYBE, begin the reason with the \
[DIRECT] / [INFERRED] tag defined in the decision rules.\
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
