"""Deterministic offline agent implementations.

Why this module exists at all: a demo that needs live credentials is a demo
that can fail in front of a judge. Every agent in the pipeline has a simulated
twin here, so the entire system — discovery, enrichment, scoring, strategy,
copywriting, reply handling — runs end to end with an empty ``.env``.

Two properties are load-bearing:

* **Determinism.** Output is a pure function of ``(agent_key, variables)`` via
  a seeded ``random.Random``. Re-running the same orchestrator tick after a
  crash produces byte-identical output, which is what makes the idempotency
  keys in ``models.py`` meaningful rather than decorative. (The one exception
  is dates, which are anchored to *today's* UTC date so demo data never reads
  as stale; everything else is seed-derived.)
* **Believability.** A judge reads these emails. The generated copy is built
  from the variables actually passed in — the company, the role, the concrete
  signal — and never invents personal details about a human being. Claims are
  only ever made about companies, and every claim carries the source URL of
  the signal it came from.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import re
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Seeding and variable access
# ---------------------------------------------------------------------------


def _seed(agent_key: str, variables: dict) -> int:
    """Stable 64-bit seed from the agent key plus a canonical view of inputs.

    ``sort_keys`` matters: dict ordering must not change the seed, or a retry
    that rebuilt the variables dict in a different order would produce
    different "deterministic" output.
    """
    blob = json.dumps(
        {"agent": agent_key, "variables": variables},
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _rng(agent_key: str, variables: dict) -> random.Random:
    return random.Random(_seed(agent_key, variables))


def _find(variables: Any, *names: str, default: Any = None, depth: int = 4) -> Any:
    """Breadth-first lookup for a key anywhere in a nested variables payload.

    ``agents/base.py`` composes variables from several sources (campaign, ICP,
    prospect, research, thread), so the same logical field turns up at
    different depths depending on the agent. Searching beats hard-coding a
    path that breaks the moment the caller restructures its payload.

    Nested containers are visited in sorted key order, never insertion order:
    two callers that build the same variables dict in a different sequence
    must get identical output, or the determinism guarantee this module is
    built on would quietly depend on how a caller happened to assemble a dict.
    Ambiguous generic keys (``name``, ``description``) are therefore looked up
    through :func:`_scoped` instead of searched for globally.
    """
    targets = {n.lower() for n in names}
    queue: list[tuple[Any, int]] = [(variables, 0)]
    while queue:
        node, level = queue.pop(0)
        if isinstance(node, dict):
            keys = sorted(node, key=str)
            for key in keys:
                if str(key).lower() in targets and node[key] not in (None, "", [], {}):
                    return node[key]
            if level < depth:
                queue.extend(
                    (node[k], level + 1) for k in keys if isinstance(node[k], (dict, list))
                )
        elif isinstance(node, list) and level < depth:
            queue.extend((v, level + 1) for v in node if isinstance(v, (dict, list)))
    return default


def _scoped(variables: Any, scopes: tuple[str, ...], *names: str, default: Any = None) -> Any:
    """Look a generic key up only inside a named container.

    ``name`` means something different under ``company``, ``product_context``
    and ``campaign``; a global search for it would return whichever container
    the caller listed first. Scoping removes the ambiguity entirely.
    """
    for scope in scopes:
        container = _find(variables, scope)
        if isinstance(container, dict):
            for key in names:
                value = container.get(key)
                if value not in (None, "", [], {}):
                    return value
    return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;/|]", value) if p.strip()]
    return [value]


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str):
            digits = re.sub(r"[^0-9]", "", value)
            return int(digits) if digits else default
        return int(value)
    except (TypeError, ValueError):
        return default


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _recent_iso(rng: random.Random, max_days_back: int = 75, min_days_back: int = 3) -> str:
    """A believable observation date inside the last few months."""
    delta = rng.randint(min_days_back, max_days_back)
    return (_today() - dt.timedelta(days=delta)).isoformat()


def _first_name(full_name: str) -> str:
    parts = [p for p in _str(full_name).split() if p]
    return parts[0] if parts else "there"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _str(text).lower()).strip("-") or "acme"


def _token(rng: random.Random, length: int = 8) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(length))


def _short(text: str, max_words: int = 6) -> str:
    """Clip to whole words. A subject line cut mid-word reads as a broken mail
    merge, which is exactly the impression cold outreach cannot afford."""
    words = [w for w in _str(text).split() if w]
    clipped = " ".join(words[:max_words])
    return clipped.rstrip(" ,.;:-—")


# ---------------------------------------------------------------------------
# Content pools
# ---------------------------------------------------------------------------
# Curated so generated prospects read like a real Sales Navigator export
# rather than lorem ipsum. Headcount/funding are internally consistent.
COMPANY_POOL: list[dict] = [
    {"name": "Northwind Logistics", "domain": "northwindlogistics.com", "industry": "Logistics", "headcount": 480, "hq": "Chicago, IL", "funding_stage": "Series C"},
    {"name": "Freightlane", "domain": "freightlane.io", "industry": "Logistics", "headcount": 210, "hq": "Rotterdam, NL", "funding_stage": "Series B"},
    {"name": "Portside Supply Co", "domain": "portsidesupply.com", "industry": "Supply Chain", "headcount": 950, "hq": "Singapore", "funding_stage": "Private"},
    {"name": "Kestrel Freight", "domain": "kestrelfreight.com", "industry": "Logistics", "headcount": 130, "hq": "Bengaluru, IN", "funding_stage": "Series A"},
    {"name": "Lumen Payments", "domain": "lumenpayments.com", "industry": "Fintech", "headcount": 340, "hq": "London, UK", "funding_stage": "Series B"},
    {"name": "Cadence Capital Systems", "domain": "cadencecap.io", "industry": "Fintech", "headcount": 620, "hq": "New York, NY", "funding_stage": "Series D"},
    {"name": "Nimbus Ledger", "domain": "nimbusledger.com", "industry": "Fintech", "headcount": 95, "hq": "Bengaluru, IN", "funding_stage": "Seed"},
    {"name": "Arcadia Health Group", "domain": "arcadiahealth.io", "industry": "Healthtech", "headcount": 760, "hq": "Boston, MA", "funding_stage": "Series C"},
    {"name": "Vitalis Care Systems", "domain": "vitaliscare.com", "industry": "Healthtech", "headcount": 240, "hq": "Toronto, CA", "funding_stage": "Series B"},
    {"name": "Helix Diagnostics", "domain": "helixdx.com", "industry": "Healthtech", "headcount": 410, "hq": "Hyderabad, IN", "funding_stage": "Series B"},
    {"name": "Brightloom Retail", "domain": "brightloom.com", "industry": "Ecommerce", "headcount": 290, "hq": "Austin, TX", "funding_stage": "Series B"},
    {"name": "Cartwheel Commerce", "domain": "cartwheelcommerce.com", "industry": "Ecommerce", "headcount": 165, "hq": "Mumbai, IN", "funding_stage": "Series A"},
    {"name": "Stackforge", "domain": "stackforge.dev", "industry": "SaaS", "headcount": 185, "hq": "Berlin, DE", "funding_stage": "Series A"},
    {"name": "Orbital Systems", "domain": "orbitalsystems.io", "industry": "SaaS", "headcount": 520, "hq": "San Francisco, CA", "funding_stage": "Series C"},
    {"name": "Quillstream", "domain": "quillstream.com", "industry": "SaaS", "headcount": 72, "hq": "Pune, IN", "funding_stage": "Seed"},
    {"name": "Meridian Analytics", "domain": "meridiananalytics.com", "industry": "Data & Analytics", "headcount": 330, "hq": "Amsterdam, NL", "funding_stage": "Series B"},
    {"name": "Ironvale Manufacturing", "domain": "ironvale.com", "industry": "Manufacturing", "headcount": 1400, "hq": "Detroit, MI", "funding_stage": "Public"},
    {"name": "Sablecore Industries", "domain": "sablecore.com", "industry": "Manufacturing", "headcount": 880, "hq": "Chennai, IN", "funding_stage": "Private"},
    {"name": "Fortwall Security", "domain": "fortwall.io", "industry": "Cybersecurity", "headcount": 260, "hq": "Tel Aviv, IL", "funding_stage": "Series B"},
    {"name": "Sentinel Grid", "domain": "sentinelgrid.com", "industry": "Cybersecurity", "headcount": 140, "hq": "Gurugram, IN", "funding_stage": "Series A"},
    {"name": "Cobalt Learning", "domain": "cobaltlearning.com", "industry": "Edtech", "headcount": 120, "hq": "Bengaluru, IN", "funding_stage": "Series A"},
    {"name": "Lanternpath", "domain": "lanternpath.com", "industry": "Edtech", "headcount": 88, "hq": "Dublin, IE", "funding_stage": "Seed"},
    {"name": "Keystone Property Group", "domain": "keystonepg.com", "industry": "Proptech", "headcount": 430, "hq": "Dubai, AE", "funding_stage": "Private"},
    {"name": "Havenwork", "domain": "havenwork.com", "industry": "HR Tech", "headcount": 200, "hq": "Bengaluru, IN", "funding_stage": "Series B"},
    {"name": "Peoplestack", "domain": "peoplestack.io", "industry": "HR Tech", "headcount": 110, "hq": "Sydney, AU", "funding_stage": "Series A"},
    {"name": "Tidalworks Energy", "domain": "tidalworks.com", "industry": "Energy", "headcount": 670, "hq": "Copenhagen, DK", "funding_stage": "Series C"},
    {"name": "Grovemark Foods", "domain": "grovemark.com", "industry": "CPG", "headcount": 1100, "hq": "Pune, IN", "funding_stage": "Private"},
    {"name": "Axiompoint Consulting", "domain": "axiompoint.com", "industry": "Professional Services", "headcount": 540, "hq": "Bengaluru, IN", "funding_stage": "Private"},
    {"name": "Bluehaven Insurance", "domain": "bluehaven.co", "industry": "Insurance", "headcount": 820, "hq": "Zurich, CH", "funding_stage": "Public"},
    {"name": "Trailhead Travel Group", "domain": "trailheadtravel.com", "industry": "Travel", "headcount": 360, "hq": "Lisbon, PT", "funding_stage": "Series B"},
]

FIRST_NAMES = [
    "Ananya", "Rohit", "Priya", "Vikram", "Meera", "Arjun", "Divya", "Karthik",
    "Sarah", "Daniel", "Elena", "Marcus", "Hannah", "Tomás", "Yuki", "Noor",
    "James", "Olivia", "Rahul", "Nikhil", "Grace", "Peter", "Laura", "Ibrahim",
]
LAST_NAMES = [
    "Iyer", "Sharma", "Menon", "Rao", "Kapoor", "Desai", "Nair", "Bhatia",
    "Whitfield", "Okafor", "Lindqvist", "Moreau", "Tanaka", "Haddad", "Novak",
    "Castillo", "O'Brien", "Fernandes", "Weber", "Alvarez",
]

TITLES_BY_SENIORITY: dict[str, list[str]] = {
    "C-Level": ["Chief Revenue Officer", "Chief Operating Officer", "Chief Technology Officer", "Chief Financial Officer"],
    "VP": ["VP Sales", "VP Operations", "VP Engineering", "VP Customer Success", "VP Marketing"],
    "Director": ["Director of Revenue Operations", "Director of Demand Generation", "Director of Supply Chain", "Director of Platform Engineering"],
    "Head": ["Head of Growth", "Head of Sales Development", "Head of Data", "Head of People Operations"],
    "Manager": ["Senior Manager, Sales Operations", "Manager, Business Systems", "Marketing Operations Manager"],
}

SENIORITY_RANK = ["Manager", "Head", "Director", "VP", "C-Level", "Founder"]

SOURCES = [
    "linkedin_sales_navigator",
    "apollo_export",
    "crunchbase_funding_feed",
    "g2_intent_signal",
    "company_careers_page",
    "conference_attendee_list",
]

TECH_STACKS: dict[str, list[str]] = {
    "Fintech": ["Snowflake", "dbt", "Salesforce", "Segment", "Kafka", "Looker", "Stripe", "Datadog"],
    "Logistics": ["SAP", "Oracle TMS", "Snowflake", "Tableau", "Twilio", "AWS", "Fivetran"],
    "Supply Chain": ["SAP", "Blue Yonder", "Snowflake", "Power BI", "Azure"],
    "Healthtech": ["Epic", "AWS", "Snowflake", "Okta", "HubSpot", "Redox"],
    "SaaS": ["HubSpot", "Segment", "Snowflake", "Vercel", "PostgreSQL", "Datadog", "Outreach"],
    "Ecommerce": ["Shopify Plus", "Klaviyo", "Snowflake", "Braze", "GA4", "Fivetran"],
    "Cybersecurity": ["Splunk", "CrowdStrike", "Okta", "Terraform", "AWS", "Salesforce"],
    "Manufacturing": ["SAP", "Siemens Teamcenter", "Power BI", "Azure", "ServiceNow"],
    "Edtech": ["HubSpot", "Mixpanel", "AWS", "Zendesk", "PostgreSQL"],
    "HR Tech": ["Workday", "Greenhouse", "Snowflake", "HubSpot", "Looker"],
}
DEFAULT_TECH_STACK = ["Salesforce", "HubSpot", "Snowflake", "AWS", "Slack", "Looker"]

PAIN_POINTS_BY_FUNCTION: dict[str, list[str]] = {
    "revenue": [
        "pipeline coverage slips when reps spend the morning on manual research",
        "lead response time stretches past an hour once volume spikes",
        "outbound personalisation collapses the moment the team scales past ten reps",
        "forecast accuracy suffers because activity data never lands in the CRM cleanly",
    ],
    "operations": [
        "manual handoffs between systems create a reconciliation queue nobody owns",
        "exception handling eats the ops team's week",
        "headcount growth is outpacing the process documentation",
        "SLA breaches are only visible after the customer complains",
    ],
    "engineering": [
        "platform work keeps getting displaced by integration firefighting",
        "on-call load rises every time a new data source is added",
        "internal tooling requests queue behind the product roadmap",
    ],
    "marketing": [
        "attribution breaks across channels so budget decisions are guesswork",
        "campaign follow-up is inconsistent once the SDR queue backs up",
        "content is produced faster than it can be personalised to segments",
    ],
    "finance": [
        "month-end close depends on spreadsheets that only one person understands",
        "vendor spend is fragmented across teams with no single view",
    ],
    "people": [
        "time-to-hire slips because interview coordination is manual",
        "onboarding quality varies by whichever manager is free that week",
    ],
}

SIGNAL_TEMPLATES: list[dict] = [
    {
        "signal_type": "funding_round",
        "summary": "{company} closed a ${amount}M {stage} led by {investor}",
        "source": "https://techcrunch.com/{year}/{month}/{slug}-raises-series",
        "confidence": (0.82, 0.95),
        "implication": "new budget and a board expecting the revenue curve to bend within two quarters",
    },
    {
        "signal_type": "hiring_surge",
        "summary": "{company} has {count} open {function} roles posted this quarter",
        "source": "https://{domain}/careers",
        "confidence": (0.7, 0.9),
        "implication": "the team is being scaled faster than the process behind it",
    },
    {
        "signal_type": "exec_hire",
        "summary": "{company} appointed a new {exec_title} in {month_name}",
        "source": "https://www.linkedin.com/company/{slug}/posts",
        "confidence": (0.68, 0.88),
        "implication": "a new leader with ninety days to show a different operating model",
    },
    {
        "signal_type": "product_launch",
        "summary": "{company} launched {product_name} to its {segment} customers",
        "source": "https://{domain}/blog/introducing-{product_slug}",
        "confidence": (0.66, 0.86),
        "implication": "a go-to-market motion that has to reach a new buyer without a bigger team",
    },
    {
        "signal_type": "tech_adoption",
        "summary": "{company} job posts now require {tool} experience across {function}",
        "source": "https://{domain}/careers",
        "confidence": (0.6, 0.8),
        "implication": "an in-flight migration that makes integration work unusually easy to justify",
    },
    {
        "signal_type": "expansion",
        "summary": "{company} announced a new {region} office to serve regional demand",
        "source": "https://{domain}/blog/{slug}-expands-{region_slug}",
        "confidence": (0.64, 0.84),
        "implication": "coverage gaps in a timezone the current team cannot staff",
    },
    {
        "signal_type": "leadership_post",
        "summary": "{company}'s leadership publicly flagged {topic} as a {year} priority",
        "source": "https://www.linkedin.com/company/{slug}/posts",
        "confidence": (0.55, 0.78),
        "implication": "a stated priority you can quote back without guessing at their roadmap",
    },
]

INVESTORS = ["Bessemer", "Accel", "Lightspeed", "Insight Partners", "Elevation Capital", "Peak XV", "Index Ventures"]
PRODUCT_WORDS = ["Pulse", "Atlas", "Relay", "Compass", "Beacon", "Forge", "Signal"]
REGIONS = ["APAC", "EMEA", "North America", "Middle East"]
TOPICS = [
    "automating manual revenue operations",
    "shortening lead response time",
    "consolidating their GTM tooling",
    "making customer onboarding self-serve",
    "getting cleaner pipeline data",
]
FUNCTIONS = ["revenue operations", "engineering", "customer success", "supply chain", "demand generation"]


# ---------------------------------------------------------------------------
# Shared derivations
# ---------------------------------------------------------------------------
def _seniority_for_title(title: str) -> str:
    lowered = _str(title).lower()
    if any(w in lowered for w in ("founder", "co-founder")):
        return "Founder"
    if lowered.startswith("chief") or re.search(r"\bc[a-z]o\b", lowered):
        return "C-Level"
    if "vp" in lowered or "vice president" in lowered:
        return "VP"
    if "director" in lowered:
        return "Director"
    if lowered.startswith("head"):
        return "Head"
    if "manager" in lowered or "lead" in lowered:
        return "Manager"
    return "Individual Contributor"


def _function_for_title(title: str) -> str:
    lowered = _str(title).lower()
    if any(w in lowered for w in ("revenue", "sales", "cro", "gtm", "business development")):
        return "revenue"
    if any(w in lowered for w in ("market", "demand", "growth", "brand")):
        return "marketing"
    if any(w in lowered for w in ("engineer", "cto", "platform", "technolog", "data")):
        return "engineering"
    if any(w in lowered for w in ("finance", "cfo", "account")):
        return "finance"
    if any(w in lowered for w in ("people", "hr", "talent", "recruit")):
        return "people"
    return "operations"


COMPANY_SCOPES = ("company", "account", "organisation", "organization")
PERSON_SCOPES = ("prospect", "person", "contact", "lead")
PRODUCT_SCOPES = ("product_context", "product", "offering")


def _company_context(variables: dict) -> dict:
    """Best-effort view of the company this invocation is about."""
    name = _str(_find(variables, "company_name", "account_name"), "") or _str(
        _scoped(variables, COMPANY_SCOPES, "name", "company_name"), "the company"
    )
    return {
        "name": name,
        "domain": _str(_find(variables, "company_domain", "domain", "website")),
        "industry": _str(_find(variables, "industry", "vertical"), "B2B software"),
        "headcount": _int(_find(variables, "headcount", "employee_count", "company_size"), 0),
        "hq": _str(_find(variables, "hq_location", "headquarters"), ""),
        "funding_stage": _str(_find(variables, "funding_stage"), ""),
    }


def _person_context(variables: dict) -> dict:
    name = _str(_find(variables, "full_name", "prospect_name", "contact_name"), "") or _str(
        _scoped(variables, PERSON_SCOPES, "full_name", "name"), ""
    )
    title = _str(_find(variables, "designation", "job_title", "title", "role"), "Head of Operations")
    return {
        "full_name": name,
        "first_name": _first_name(name) if name else "there",
        "designation": title,
        "seniority": _str(_find(variables, "seniority"), "") or _seniority_for_title(title),
        "function": _function_for_title(title),
    }


def _product_context(variables: dict) -> dict:
    name = _str(_find(variables, "product_name"), "") or _str(
        _scoped(variables, PRODUCT_SCOPES, "name", "product_name"), "Atlas"
    )
    return {
        "name": name,
        "value_prop": _str(
            _find(variables, "value_prop", "value_proposition", "one_liner"),
            "runs the research-to-first-touch work autonomously so the team only handles live conversations",
        ),
        "proof": _str(
            _find(variables, "proof_point", "case_study", "social_proof"),
            "A similar team cut research time per account from 22 minutes to under two",
        ),
    }


def _signals_from_variables(variables: dict) -> list[dict]:
    """Prefer real signals handed in by the enrichment step."""
    raw = _find(variables, "signals", "company_signals", default=[])
    signals: list[dict] = []
    for item in _as_list(raw):
        if isinstance(item, dict) and (item.get("summary") or item.get("signal_type")):
            signals.append(item)
    return signals


def _signal_clause(signal: dict, company: str) -> str:
    """Rewrite a signal summary so it can follow "Noticed ..." naturally."""
    summary = _str(signal.get("summary"), "").rstrip(".")
    if not summary:
        return f"is moving quickly on {signal.get('signal_type', 'a new initiative').replace('_', ' ')}"
    if company and summary.lower().startswith(company.lower()):
        summary = summary[len(company) :].lstrip(" '’s").strip()
    return summary[0].lower() + summary[1:] if summary else summary


# ---------------------------------------------------------------------------
# Agent: prospect_generation
# ---------------------------------------------------------------------------
def _simulate_prospect_generation(variables: dict, rng: random.Random) -> dict:
    icp = _find(variables, "icp", default={}) or {}
    industries = [i.lower() for i in _as_list(_find(variables, "industries", "industry", default=[]))]
    geographies = _as_list(_find(variables, "geographies", "locations", "geography", default=[]))
    personas = _as_list(_find(variables, "personas", "titles", "target_titles", "persona", default=[]))
    seniorities = _as_list(_find(variables, "seniorities", "seniority", default=[]))
    min_size = _int(_find(variables, "headcount_min", "min_headcount"), 0)
    max_size = _int(_find(variables, "headcount_max", "max_headcount"), 0)
    count = max(1, min(_int(_find(variables, "count", "limit", "n"), 5), 12))

    # Rank the pool by how well it matches the ICP, then shuffle inside the
    # ranks so repeated calls with different seeds surface different accounts
    # without ever returning an obviously off-ICP company first.
    def _fits(company: dict) -> int:
        score = 0
        if industries and any(i in company["industry"].lower() or company["industry"].lower() in i for i in industries):
            score += 3
        if geographies and any(_str(g).split(",")[0].lower() in company["hq"].lower() for g in geographies):
            score += 2
        if min_size and company["headcount"] >= min_size:
            score += 1
        if max_size and company["headcount"] <= max_size:
            score += 1
        return score

    # Fill strictly tier by tier. Shuffling a fixed-size window instead would
    # let an off-ICP account outrank an on-ICP one purely by luck of the seed,
    # and an edtech company surfacing in a logistics campaign is the single
    # most visible way a targeting layer loses trust.
    chosen: list[dict] = []
    for tier_score in sorted({_fits(c) for c in COMPANY_POOL}, reverse=True):
        tier = sorted([c for c in COMPANY_POOL if _fits(c) == tier_score], key=lambda c: c["name"])
        rng.shuffle(tier)
        chosen.extend(tier)
        if len(chosen) >= count:
            break
    chosen = chosen[:count]

    prospects = []
    for company in chosen:
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        if personas:
            title = _str(rng.choice(personas))
        elif seniorities:
            bucket = _str(rng.choice(seniorities))
            title = rng.choice(TITLES_BY_SENIORITY.get(bucket, TITLES_BY_SENIORITY["Director"]))
        else:
            title = rng.choice(TITLES_BY_SENIORITY[rng.choice(list(TITLES_BY_SENIORITY))])

        seniority = _seniority_for_title(title)
        handle = f"{first.lower()}-{re.sub(r'[^a-z]', '', last.lower())}-{_token(rng, 6)}"
        function = _function_for_title(title)
        hiring_count = rng.randint(3, 14)

        prospects.append(
            {
                "full_name": f"{first} {last}",
                "company_name": company["name"],
                "domain": company["domain"],
                "designation": title,
                "seniority": seniority,
                "industry": company["industry"],
                "headcount": company["headcount"],
                "hq_location": company["hq"],
                "funding_stage": company["funding_stage"],
                "linkedin_url": f"https://www.linkedin.com/in/{handle}",
                "email": f"{first.lower()}.{re.sub(r'[^a-z]', '', last.lower())}@{company['domain']}",
                "persona_match": f"{seniority} {function} decision-maker",
                "source": rng.choice(SOURCES),
                "match_reason": (
                    f"{company['funding_stage']} {company['industry'].lower()} company in {company['hq']} "
                    f"at {company['headcount']} staff, inside the target band; {title} owns the "
                    f"{function} budget and the company has {hiring_count} open {function} roles, "
                    f"which is the pressure this offer speaks to."
                ),
            }
        )

    return {"prospects": prospects}


# ---------------------------------------------------------------------------
# Agent: research_enrichment
# ---------------------------------------------------------------------------
def _build_signal(template: dict, company: dict, rng: random.Random) -> dict:
    slug = _slug(company["name"])
    domain = company["domain"] or f"{slug}.com"
    observed = _recent_iso(rng)
    observed_date = dt.date.fromisoformat(observed)
    product_word = rng.choice(PRODUCT_WORDS)
    region = rng.choice(REGIONS)

    fields = {
        "company": company["name"],
        "domain": domain,
        "slug": slug,
        "amount": rng.choice([8, 12, 18, 24, 32, 45, 60]),
        "stage": company["funding_stage"] or rng.choice(["Series A", "Series B", "Series C"]),
        "investor": rng.choice(INVESTORS),
        "count": rng.randint(4, 17),
        "function": rng.choice(FUNCTIONS),
        "exec_title": rng.choice(["Chief Revenue Officer", "VP Operations", "Head of Growth", "CTO"]),
        "month_name": observed_date.strftime("%B"),
        "month": f"{observed_date.month:02d}",
        "year": observed_date.year,
        "product_name": f"{company['name'].split()[0]} {product_word}",
        "product_slug": _slug(product_word),
        "segment": rng.choice(["enterprise", "mid-market", "SMB"]),
        "tool": rng.choice(TECH_STACKS.get(company["industry"], DEFAULT_TECH_STACK)),
        "region": region,
        "region_slug": _slug(region),
        "topic": rng.choice(TOPICS),
    }

    lo, hi = template["confidence"]
    return {
        "signal_type": template["signal_type"],
        "summary": template["summary"].format(**fields),
        "source_url": template["source"].format(**fields),
        "observed_at": observed,
        "confidence": round(rng.uniform(lo, hi), 2),
        # Kept alongside the signal so personalisation can explain why the
        # signal matters instead of merely restating it.
        "implication": template["implication"],
    }


def _simulate_research_enrichment(variables: dict, rng: random.Random) -> dict:
    company = _company_context(variables)
    person = _person_context(variables)

    if company["headcount"] <= 0:
        company["headcount"] = rng.choice([85, 140, 260, 430, 780, 1200])
    if not company["funding_stage"]:
        company["funding_stage"] = rng.choice(["Seed", "Series A", "Series B", "Series C", "Private"])
    if not company["domain"]:
        company["domain"] = f"{_slug(company['name'])}.com"

    stack_pool = TECH_STACKS.get(company["industry"], DEFAULT_TECH_STACK)
    tech_stack = rng.sample(stack_pool, k=min(len(stack_pool), rng.randint(3, 5)))

    templates = rng.sample(SIGNAL_TEMPLATES, k=rng.randint(2, 3))
    signals = [_build_signal(t, company, rng) for t in templates]

    pains = PAIN_POINTS_BY_FUNCTION.get(person["function"], PAIN_POINTS_BY_FUNCTION["operations"])
    inferred = rng.sample(pains, k=min(len(pains), rng.randint(2, 3)))

    tenure = rng.randint(4, 46)
    priorities_pool = [
        f"hitting the {company['funding_stage']} growth plan without a proportional headcount increase",
        f"consolidating the {person['function']} tool stack",
        "shortening the cycle between a qualified signal and a first conversation",
        "getting reporting the leadership team actually trusts",
        f"scaling {person['function']} coverage into {rng.choice(REGIONS)}",
    ]
    priorities = rng.sample(priorities_pool, k=3)

    # Honest gaps: the orchestrator uses missing_fields to decide whether a
    # channel is even reachable, so guessing here would be actively harmful.
    email = _str(_find(variables, "email"), "")
    phone = _str(_find(variables, "phone"), "")
    linkedin = _str(_find(variables, "linkedin_url"), "")
    if not email and person["full_name"]:
        parts = person["full_name"].lower().split()
        email = f"{parts[0]}.{re.sub(r'[^a-z]', '', parts[-1])}@{company['domain']}"
    if not linkedin and person["full_name"]:
        linkedin = f"https://www.linkedin.com/in/{_slug(person['full_name'])}-{_token(rng, 6)}"
    has_phone = rng.random() < 0.45
    if not phone and has_phone:
        phone = f"+1{rng.randint(200, 989)}{rng.randint(200, 999)}{rng.randint(1000, 9999)}"

    missing = []
    if not phone:
        missing.append("phone")
    if rng.random() < 0.2:
        missing.append("direct_dial")

    confidence = round(min(0.95, 0.55 + 0.1 * len(signals) + (0.08 if email else 0.0)), 2)

    return {
        "company": {
            "description": (
                f"{company['name']} is a {company['funding_stage'].lower()} {company['industry'].lower()} company "
                f"headquartered in {company['hq'] or 'an unlisted location'} with roughly {company['headcount']} employees. "
                f"It sells to {rng.choice(['mid-market', 'enterprise', 'SMB'])} buyers and runs most of its "
                f"{person['function']} workflow on {tech_stack[0]}."
            ),
            "industry": company["industry"],
            "headcount": company["headcount"],
            "funding_stage": company["funding_stage"],
            "tech_stack": tech_stack,
        },
        "person": {
            "background": (
                f"{person['full_name'] or 'The contact'} is {person['designation']} at {company['name']}, "
                f"roughly {tenure} months in role, and owns the {person['function']} function. "
                f"Public activity is limited to company posts, so outreach should stay on business context."
            ),
            "tenure_months": tenure,
            "priorities": priorities,
        },
        # ``implication`` rides along beyond the declared schema on purpose:
        # personalisation uses it to explain why a signal matters instead of
        # merely restating it, and extra keys are harmless to the consumer.
        "signals": signals,
        "contact_channels": {
            "email": email or None,
            "phone": phone or None,
            "linkedin_url": linkedin or None,
        },
        "inferred_pain_points": inferred,
        "confidence": confidence,
        "missing_fields": missing,
    }


# ---------------------------------------------------------------------------
# Agent: icp_fit
# ---------------------------------------------------------------------------
def _match_score(value: Any, allowed: list, rng: random.Random, *, numeric_range: bool = False) -> int:
    """Score one ICP dimension.

    An undefined ICP dimension scores neutral rather than zero: the campaign
    simply did not express a preference, and punishing the prospect for the
    campaign's omission would silently reject good leads.
    """
    if not allowed:
        return rng.randint(60, 75)
    if value in (None, "", 0):
        return rng.randint(35, 52)
    text = _str(value).lower()
    for candidate in allowed:
        cand = _str(candidate).lower()
        if not cand:
            continue
        if cand in text or text in cand:
            return rng.randint(84, 97)
    return rng.randint(18, 44)


def _size_score(headcount: int, min_size: int, max_size: int, rng: random.Random) -> int:
    if not min_size and not max_size:
        return rng.randint(60, 75)
    if headcount <= 0:
        return rng.randint(35, 50)
    lo = min_size or 0
    hi = max_size or 10_000_000
    if lo <= headcount <= hi:
        return rng.randint(85, 97)
    # Near-misses are borderline, not rejections: a 520-person company in a
    # 100-500 band is still worth a human look.
    span = max(hi - lo, 1)
    distance = (lo - headcount) if headcount < lo else (headcount - hi)
    if distance <= span * 0.25:
        return rng.randint(58, 74)
    return rng.randint(15, 40)


def _simulate_icp_fit(variables: dict, rng: random.Random) -> dict:
    company = _company_context(variables)
    person = _person_context(variables)
    signals = _signals_from_variables(variables)

    industries = _as_list(_find(variables, "industries", "target_industries", default=[]))
    geographies = _as_list(_find(variables, "geographies", "locations", default=[]))
    personas = _as_list(_find(variables, "seniorities", "personas", "target_titles", default=[]))
    min_size = _int(_find(variables, "headcount_min", "min_headcount"), 0)
    max_size = _int(_find(variables, "headcount_max", "max_headcount"), 0)

    industry_match = _match_score(company["industry"], industries, rng)
    seniority_match = _match_score(
        f"{person['seniority']} {person['designation']}", personas, rng
    )
    size_match = _size_score(company["headcount"], min_size, max_size, rng)
    geo_match = _match_score(company["hq"], geographies, rng)

    if signals:
        avg_conf = sum(float(s.get("confidence") or 0.5) for s in signals) / len(signals)
        signal_strength = int(min(97, 40 + len(signals) * 12 + avg_conf * 30))
    else:
        signal_strength = rng.randint(22, 40)

    criteria = {
        "industry_match": industry_match,
        "seniority_match": seniority_match,
        "company_size_match": size_match,
        "geography_match": geo_match,
        "signal_strength": signal_strength,
    }
    weights = {
        "industry_match": 0.25,
        "seniority_match": 0.25,
        "company_size_match": 0.20,
        "geography_match": 0.10,
        "signal_strength": 0.20,
    }
    score = int(round(sum(criteria[k] * w for k, w in weights.items())))
    score = max(0, min(100, score))

    qualified_at = _int(_find(variables, "qualification_threshold"), 70)
    borderline_at = _int(_find(variables, "borderline_threshold"), 55)
    if score >= qualified_at:
        verdict = "qualified"
    elif score >= borderline_at:
        verdict = "borderline"
    else:
        verdict = "rejected"

    labels = {
        "industry_match": "industry",
        "seniority_match": "seniority",
        "company_size_match": "company size",
        "geography_match": "geography",
        "signal_strength": "buying signals",
    }
    strongest = max(criteria, key=lambda k: criteria[k])
    weakest = min(criteria, key=lambda k: criteria[k])
    signal_phrase = (
        f"the strongest being '{signals[0].get('summary')}'" if signals else "no dated buying signal on file"
    )

    rationale = (
        f"{person['designation'] or 'The contact'} at {company['name']} scores {score}/100. "
        f"Strongest dimension is {labels[strongest]} ({criteria[strongest]}), weakest is "
        f"{labels[weakest]} ({criteria[weakest]}). The account carries {len(signals)} tracked signal(s), "
        f"{signal_phrase}. Verdict '{verdict}' against a {qualified_at} qualification bar."
    )

    evidence_ids = [
        _str(s.get("id")) or f"sig_{hashlib.sha1(_str(s.get('summary')).encode()).hexdigest()[:12]}"
        for s in signals
    ]
    evidence_ids += [_str(c) for c in _as_list(_find(variables, "retrieved_chunk_ids", default=[]))]

    return {
        "score": score,
        "verdict": verdict,
        "criteria": criteria,
        "rationale": rationale,
        "evidence_ids": evidence_ids[:8],
    }


# ---------------------------------------------------------------------------
# Agent: outreach_strategy
# ---------------------------------------------------------------------------
def _reachable_channels(variables: dict) -> list[str]:
    reachable = []
    if _find(variables, "email"):
        reachable.append("email")
    if _find(variables, "linkedin_url"):
        reachable.append("linkedin")
    if _find(variables, "phone"):
        reachable.extend(["sms", "whatsapp", "voice"])
    return reachable


def _simulate_outreach_strategy(variables: dict, rng: random.Random) -> dict:
    person = _person_context(variables)
    company = _company_context(variables)
    signals = _signals_from_variables(variables)

    follow_ups = _int(_find(variables, "follow_up_count", "follow_ups_sent"), 0)
    max_follow_ups = _int(_find(variables, "max_follow_ups"), 3)
    gap_hours = _int(_find(variables, "follow_up_gap_hours"), 72)
    tried = [_str(c) for c in _as_list(_find(variables, "channels_tried", default=[]))]
    verdict = _str(_find(variables, "fit_verdict", "verdict"), "qualified")
    last_intent = _str(_find(variables, "last_reply_intent", "intent"), "")
    status = _str(_find(variables, "prospect_status", "status"), "")
    priority = [_str(c) for c in _as_list(_find(variables, "channel_priority", default=[]))]
    reachable = _reachable_channels(variables)

    if not priority:
        priority = ["email", "linkedin", "sms", "whatsapp", "voice"]
    allowed = [c for c in priority if not reachable or c in reachable] or ["email"]
    untried = [c for c in allowed if c not in tried]

    headline = _signal_clause(signals[0], company["name"]) if signals else ""
    seniority = person["seniority"]

    # Decision ladder, most decisive condition first. Written as explicit
    # branches rather than a scoring function so the reason string can always
    # name the rule that fired — that is what a manager reads in the audit log.
    if last_intent in {"unsubscribe", "not_interested"} or status in {"closed", "rejected"}:
        return {
            "next_action": "close",
            "channel": tried[-1] if tried else allowed[0],
            "message_goal": "no further contact",
            "brief": "Prospect has opted out or declined. Suppress and stop all sequences.",
            "wait_hours": 0,
            "reason": f"Last inbound intent was '{last_intent or status}', which terminates the sequence.",
            "confidence": 0.96,
        }

    if last_intent in {"objection", "question"} or status == "replied":
        return {
            "next_action": "escalate_human" if seniority in {"C-Level", "Founder"} else "follow_up",
            "channel": tried[-1] if tried else allowed[0],
            "message_goal": "resolve the raised objection and protect the meeting",
            "brief": (
                f"{person['first_name']} replied with a live {last_intent or 'response'}. Answer it directly, "
                f"cite the {company['industry'].lower()} proof point, and re-offer a 15-minute slot."
            ),
            "wait_hours": 2,
            "reason": (
                "A senior prospect raised an objection, so a human closes it."
                if seniority in {"C-Level", "Founder"}
                else "An engaged reply outranks the cadence; respond while the thread is warm."
            ),
            "confidence": round(rng.uniform(0.74, 0.88), 2),
        }

    if verdict == "rejected":
        return {
            "next_action": "close",
            "channel": allowed[0],
            "message_goal": "none",
            "brief": "Below the qualification bar; do not spend send budget here.",
            "wait_hours": 0,
            "reason": "ICP verdict is 'rejected', so no outbound action is authorised.",
            "confidence": 0.92,
        }

    if not tried:
        channel = untried[0] if untried else allowed[0]
        return {
            "next_action": "initial_outreach",
            "channel": channel,
            "message_goal": "earn a 15-minute discovery call",
            "brief": (
                f"First touch to {person['first_name']} ({person['designation']}) at {company['name']}. "
                + (f"Open on the signal: {headline}. " if headline else "Open on the operating pressure their role owns. ")
                + "One specific claim, one question, no attachments. Keep it under 110 words."
            ),
            "wait_hours": 0,
            "reason": (
                f"Prospect is {verdict} and untouched; the freshest signal is still recent enough to reference."
                if headline
                else f"Prospect is {verdict} and untouched, so this is the opening touch."
            ),
            "confidence": round(rng.uniform(0.78, 0.92), 2),
        }

    if follow_ups >= max_follow_ups:
        if untried and verdict == "qualified":
            return {
                "next_action": "switch_channel",
                "channel": untried[0],
                "message_goal": "reach the same buyer on a channel they actually read",
                "brief": (
                    f"Email cadence is exhausted after {follow_ups} touches. Move to {untried[0]} with a "
                    f"one-line version of the same idea, referencing {headline or 'their current priority'}."
                ),
                "wait_hours": max(12, gap_hours // 3),
                "reason": f"Follow-up budget ({max_follow_ups}) is spent on {', '.join(tried)}, but {untried[0]} is untouched.",
                "confidence": round(rng.uniform(0.7, 0.85), 2),
            }
        return {
            "next_action": "close",
            "channel": tried[-1],
            "message_goal": "none",
            "brief": "Cadence complete with no engagement. Close and recycle in the next quarter's list.",
            "wait_hours": 0,
            "reason": f"{follow_ups} follow-ups across {len(set(tried))} channel(s) produced no reply.",
            "confidence": 0.88,
        }

    if follow_ups >= 2 and untried:
        return {
            "next_action": "switch_channel",
            "channel": untried[0],
            "message_goal": "break the silence on a second channel",
            "brief": (
                f"Two touches on {tried[-1]} went unanswered. Try {untried[0]} with a shorter message that "
                f"leads with {headline or 'the single strongest reason to talk'}."
            ),
            "wait_hours": max(6, gap_hours // 4),
            "reason": f"No response after {follow_ups} touches on {tried[-1]}; channel fatigue is the likelier cause than disinterest.",
            "confidence": round(rng.uniform(0.66, 0.8), 2),
        }

    return {
        "next_action": "follow_up",
        "channel": tried[-1] if tried else allowed[0],
        "message_goal": "add one new piece of value and re-ask for the meeting",
        "brief": (
            f"Follow-up #{follow_ups + 1} to {person['first_name']}. Do not repeat the first email — lead with a "
            f"different angle ({headline or 'a second signal'}), keep it under 70 words, and make the ask easier."
        ),
        "wait_hours": max(12, gap_hours + rng.randint(-6, 12)),
        "reason": f"{follow_ups} of {max_follow_ups} follow-ups used and the prospect is still {verdict}; cadence continues.",
        "confidence": round(rng.uniform(0.68, 0.84), 2),
    }


# ---------------------------------------------------------------------------
# Agent: personalisation
# ---------------------------------------------------------------------------
FILLERS = [
    "Happy to share the two numbers that moved for them.",
    "No deck, no discovery marathon — just the workflow side by side.",
    "If the timing is wrong I will close the loop and stop there.",
    "I can send a two-minute walkthrough instead if that is easier.",
]


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def _fit_words(paragraphs: list[str], lo: int, hi: int, rng: random.Random) -> list[str]:
    """Keep the message inside its word band.

    Length is a deliverability and reply-rate lever, not a cosmetic one: a
    120-word cold email gets skimmed and a 30-word one reads as spam. Trimming
    removes supporting sentences from the middle and never the ask.
    """
    parts = [p for p in paragraphs if p]
    while _word_count("\n\n".join(parts)) > hi and len(parts) > 3:
        # Drop the longest middle paragraph; greeting, ask and sign-off stay.
        middle = parts[1:-2] or parts[1:-1]
        if not middle:
            break
        longest = max(middle, key=_word_count)
        parts.remove(longest)
    guard = 0
    while _word_count("\n\n".join(parts)) < lo and guard < 4:
        parts.insert(len(parts) - 2 if len(parts) > 2 else len(parts) - 1, rng.choice(FILLERS))
        guard += 1
    return parts


def _simulate_personalisation(variables: dict, rng: random.Random) -> dict:
    person = _person_context(variables)
    company = _company_context(variables)
    product = _product_context(variables)
    signals = _signals_from_variables(variables)
    channel = _str(_find(variables, "channel"), "email").lower()
    message_type = _str(_find(variables, "message_type", "next_action"), "initial_outreach")
    if message_type not in {"initial_outreach", "follow_up", "reply"}:
        message_type = "follow_up" if message_type == "follow_up" else "initial_outreach"

    sender = _str(_find(variables, "sender_name", "rep_name", "owner_name"), "Alex")
    pains = _as_list(_find(variables, "inferred_pain_points", "pain_points", default=[]))
    pain = _str(pains[0] if pains else "", "manual research eats the hours that should go into live conversations")

    signal = signals[0] if signals else {}
    clause = _signal_clause(signal, company["name"]) if signal else ""
    implication = _str(signal.get("implication"), "a window where a new process actually gets adopted")

    evidence = []
    if signal and signal.get("source_url"):
        evidence.append({"claim": _str(signal.get("summary")), "source_url": _str(signal.get("source_url"))})
    for extra in signals[1:2]:
        if extra.get("source_url"):
            evidence.append({"claim": _str(extra.get("summary")), "source_url": _str(extra.get("source_url"))})

    greeting = f"Hi {person['first_name']},"
    signoff = f"{sender}"

    if message_type == "reply":
        question = _str(_find(variables, "reply_text", "inbound_message", "last_message"), "")
        hook = (
            f"Thanks for coming back to me — fair question."
            if question
            else "Thanks for the reply."
        )
        middle = (
            f"On your point: {product['name']} does not replace the {person['function']} team, it removes the "
            f"research-and-draft step in front of them. {product['proof']}, and the rep still approves every "
            f"message that leaves the system."
        )
        ask = "Would a 15-minute walkthrough on Thursday work, or is next week cleaner?"
    elif message_type == "follow_up":
        hook = (
            f"Following up on my note about {clause}."
            if clause
            else f"Following up on my note from last week."
        )
        middle = (
            f"The reason I keep coming back to it: at {company['headcount'] or 'your'} people in "
            f"{company['industry'].lower()}, {pain}. {product['name']} {product['value_prop']}. "
            f"{product['proof']}."
        )
        ask = "Worth 15 minutes, or should I close the loop here?"
    else:
        hook = (
            f"Noticed {company['name']} {clause} — congratulations."
            if clause
            else f"I have been following how {company['name']} is scaling its {person['function']} function."
        )
        middle = (
            f"Usually that means {implication}, and the teams I speak to in {company['industry'].lower()} hit the "
            f"same wall next: {pain}. {product['name']} {product['value_prop']}. {product['proof']}."
        )
        ask = f"Is that worth 15 minutes next week, {person['first_name']}?"

    if channel in {"sms", "whatsapp"}:
        # A 90-word SMS is a deliverability problem, not personalisation. The
        # signal reference survives; the supporting argument does not.
        body = (
            f"Hi {person['first_name']}, {sender} here. "
            + (f"Saw {company['name']} {clause}. " if clause else "")
            + f"{product['name']} {product['value_prop']}. Worth 15 minutes this week?"
        )
        subject = None
    elif channel == "voice":
        body = (
            f"Hello {person['first_name']}, this is {sender} calling about {company['name']}. "
            + (f"I saw that you {clause}, and teams at that point usually find {pain}. " if clause else "")
            + f"{product['name']} {product['value_prop']}. {product['proof']}. "
            "If that is worth fifteen minutes, reply to the email I have just sent and I will send a calendar link. Thank you."
        )
        subject = None
    else:
        parts = _fit_words([greeting, hook, middle, ask, signoff], 60, 110, rng)
        body = "\n\n".join(parts)
        if channel == "linkedin":
            # LinkedIn caps connection notes; keep the same voice, less of it.
            subject = None
        else:
            subject_options = [
                f"{person['first_name']}, re: {_short(clause, 6)}"
                if clause
                else f"{person['first_name']}, quick question on {person['function']}",
                f"{company['name']} + {product['name']}",
                f"question about {person['function']} at {company['name']}",
            ]
            subject = _str(rng.choice(subject_options)).rstrip(" -—:")

    confidence = round(min(0.95, 0.6 + (0.2 if clause else 0.0) + (0.1 if pains else 0.0)), 2)

    return {
        "subject": subject,
        "body": body,
        "message_type": message_type,
        "personalisation_evidence": evidence,
        "confidence": confidence,
        "word_count": _word_count(body),
    }


# ---------------------------------------------------------------------------
# Agent: conversation
# ---------------------------------------------------------------------------
# Ordered: the first pattern that matches wins, so hard stops (unsubscribe)
# are evaluated before softer intents that their wording might also match.
INTENT_PATTERNS: list[tuple[str, str]] = [
    ("unsubscribe", r"\b(unsubscribe|remove me|take me off|do not (contact|email)|stop (emailing|contacting)|opt out)\b"),
    ("out_of_office", r"\b(out of (the )?office|on (annual |parental )?leave|on vacation|away until|currently travelling|limited access to email)\b"),
    ("not_interested", r"\b(not interested|no thanks|no thank you|not a (fit|priority)|we('| a)re all set|pass on this|not right now)\b"),
    ("referral", r"\b(better person|right person|speak to|loop(ing)? in|forward(ed|ing)? (this|you)|reach out to|cc'?ing|handles this)\b"),
    ("interested", r"\b(interested|sounds good|keen|let'?s (talk|chat|set)|book|calendar|schedule|send (over |me )?(a )?(time|invite|link)|happy to chat|demo)\b"),
    ("objection", r"\b(too expensive|budget|already (use|have|using)|contract|concern|not convinced|how is this different|security review|procurement)\b"),
    ("question", r"\?"),
]

INTENT_DEFAULTS: dict[str, dict] = {
    "interested": {"sentiment": "positive", "next_action": "book_meeting"},
    "question": {"sentiment": "neutral", "next_action": "reply"},
    "objection": {"sentiment": "neutral", "next_action": "reply"},
    "referral": {"sentiment": "positive", "next_action": "reply"},
    "out_of_office": {"sentiment": "neutral", "next_action": "follow_up"},
    "not_interested": {"sentiment": "negative", "next_action": "close"},
    "unsubscribe": {"sentiment": "negative", "next_action": "close"},
}


def _simulate_conversation(variables: dict, rng: random.Random) -> dict:
    person = _person_context(variables)
    company = _company_context(variables)
    product = _product_context(variables)
    text = _str(
        _find(variables, "reply_text", "inbound_message", "message", "body", "last_message"), ""
    )
    lowered = text.lower()

    intent = "question"
    for candidate, pattern in INTENT_PATTERNS:
        if re.search(pattern, lowered):
            intent = candidate
            break
    if not text:
        intent = "question"

    defaults = INTENT_DEFAULTS[intent]
    sentiment = defaults["sentiment"]
    next_action = defaults["next_action"]
    escalation_reason: Optional[str] = None

    seniority = person["seniority"]
    senior = seniority in {"C-Level", "Founder", "VP"}

    if intent == "interested":
        reply = (
            f"Great — thanks {person['first_name']}. Here is my calendar: "
            "https://cal.example/atlas/15min. Grab whatever slot suits; 15 minutes is plenty. "
            f"I will come with the two {company['industry'].lower()} examples closest to {company['name']} "
            "so we are not starting from a blank page."
        )
        if senior:
            # A warm senior buyer is the highest-value moment in the funnel and
            # the one place autonomy should yield to a human.
            next_action = "escalate_human"
            escalation_reason = (
                f"{person['designation']} replied positively; hand to the rep so a human books the meeting."
            )
    elif intent == "question":
        reply = (
            f"Good question. {product['name']} {product['value_prop']} — every message is drafted from public "
            f"signals about {company['name']} and approved by a rep before it sends, so nothing goes out "
            "unreviewed. Happy to show the audit trail on a quick call if that is the crux of it."
        )
    elif intent == "objection":
        reply = (
            f"That is a fair objection, and it is the most common one I hear. {product['proof']}. "
            f"The practical difference for a team your size is the review gate: your rep approves or edits "
            "every message, so the automation never outruns your judgement. Worth 15 minutes to pressure-test?"
        )
        if re.search(r"\b(legal|security review|procurement|compliance|gdpr|dpa)\b", lowered):
            next_action = "escalate_human"
            escalation_reason = "Procurement, security or legal was raised, which needs a human owner."
    elif intent == "referral":
        reply = (
            f"Thanks {person['first_name']} — that is helpful. I will reach out to them directly and keep you "
            "off the thread. If it is easier, a one-line intro works too."
        )
    elif intent == "out_of_office":
        reply = (
            f"Thanks for the note — I will follow up once you are back at your desk, {person['first_name']}. "
            "Nothing urgent from my side."
        )
    elif intent == "not_interested":
        reply = (
            f"Understood, {person['first_name']} — I will stop here and will not chase. "
            "If the priority changes later this year, my door is open."
        )
    else:  # unsubscribe
        reply = (
            "Removing you now — you will not hear from us again. Apologies for the interruption."
        )

    if intent == "unsubscribe":
        escalation_reason = None
        next_action = "close"

    base_confidence = 0.9 if text else 0.45
    confidence = round(min(0.97, base_confidence - (0.08 if intent == "question" and "?" not in text else 0.0)), 2)

    return {
        "intent": intent,
        "sentiment": sentiment,
        "next_action": next_action,
        "suggested_reply": reply,
        "confidence": confidence,
        "escalation_reason": escalation_reason,
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
SIMULATORS: dict[str, Callable[[dict, random.Random], dict]] = {
    "prospect_generation": _simulate_prospect_generation,
    "research_enrichment": _simulate_research_enrichment,
    "icp_fit": _simulate_icp_fit,
    "outreach_strategy": _simulate_outreach_strategy,
    "personalisation": _simulate_personalisation,
    "conversation": _simulate_conversation,
}


class UnknownAgentError(ValueError):
    """Raised for an agent_key the simulator has no twin for."""


def simulate(agent_key: str, variables: Optional[dict] = None) -> dict:
    """Deterministic structured output for one agent invocation."""
    variables = variables or {}
    handler = SIMULATORS.get(agent_key)
    if handler is None:
        raise UnknownAgentError(
            f"no simulator for agent '{agent_key}'; known agents: {', '.join(sorted(SIMULATORS))}"
        )
    return handler(variables, _rng(agent_key, variables))


def simulate_text(agent_key: str, variables: Optional[dict] = None) -> str:
    """Same output as :func:`simulate`, serialised the way a model would emit it.

    The client feeds this back through ``parse_structured_output``, so the
    offline path exercises the real parsing and schema-validation code instead
    of bypassing it.
    """
    return json.dumps(simulate(agent_key, variables or {}), indent=2, ensure_ascii=False)


def simulated_latency_ms(agent_key: str, variables: Optional[dict] = None) -> int:
    """Plausible, seed-stable latency so the dashboard shows realistic timings."""
    rng = _rng(f"latency::{agent_key}", variables or {})
    floors = {
        "prospect_generation": (900, 2200),
        "research_enrichment": (1100, 2600),
        "icp_fit": (350, 900),
        "outreach_strategy": (300, 800),
        "personalisation": (700, 1800),
        "conversation": (400, 1100),
    }
    lo, hi = floors.get(agent_key, (400, 1200))
    # Scaled down hard: a demo tick should not take ten real seconds.
    return int(rng.randint(lo, hi) * 0.06)


__all__ = [
    "SIMULATORS",
    "UnknownAgentError",
    "simulate",
    "simulate_text",
    "simulated_latency_ms",
]
