"""Parsers for the free-text spreadsheet columns. Each returns structured values or None.

The original text is always stored as its own claim; parsed values are extra claims with method 'parse:v1'.
When a non-empty cell parses to nothing, the importer raises a parse_failure review item.
"""
import re

from .normalise import find_domains, norm_domain

PARSE_METHOD = "parse:v2"  # v2: states negations ("does not lend in", "Most states; not ...") read correctly

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_NAME_TO_CODE = {v.lower(): k for k, v in US_STATES.items()}
# Longest names first so 'West Virginia' wins over 'Virginia'.
_STATE_NAME_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _NAME_TO_CODE), key=len, reverse=True)) + r")\b", re.I)
_STATE_CODE_RE = re.compile(r"\b(" + "|".join(US_STATES) + r")\b")  # case-sensitive: avoids 'in', 'or', 'me'

TRAFFIC_CATEGORY_ROLE = {
    "1": "lead_network_pingtree",
    "2": "connector_brand",
    "3": "comparison_offer_wall",
    "4": "content_affiliate",
    "5": "sponsored_publisher_agency",
    "6": "affiliate_cpa_network",
    "7": "paid_media_platform",
    "8": "fintech_decline_source",
}


def traffic_role(category: str | None) -> str | None:
    if not category:
        return None
    m = re.match(r"\s*(\d)", str(category))
    return TRAFFIC_CATEGORY_ROLE.get(m.group(1)) if m else None


def lender_roles(type_text: str | None) -> list[str]:
    """Targets 'Type' -> lender roles. 'fintech-other' maps to nothing (caller flags it)."""
    if not type_text:
        return []
    t = type_text.lower()
    roles = []
    if "tribal" in t:
        roles.append("lender_tribal")
    if "state-licensed" in t or "state licensed" in t:
        roles.append("lender_state_licensed")
    if "bank-partner" in t or "bank partner" in t:
        roles.append("lender_bank_partner")
    if "cab" in t or "cso" in t:
        roles.append("lender_cab_cso")
    if "servicer" in t:
        roles.append("servicer")
    return roles


_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k)?", re.I)


def loan_range(text: str | None) -> tuple[float | None, float | None]:
    """Smallest and largest dollar amounts in the text. 'up to $X' alone gives (None, X)."""
    if not text:
        return None, None
    amounts = []
    for m in _MONEY.finditer(str(text)):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        amounts.append(v)
    # '$100–$4,000' style with an en dash and no second '$' is caught above; '$300 - 2,500' is not:
    for m in re.finditer(r"\$\s?(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)", str(text)):
        amounts.append(float(m.group(2).replace(",", "")))
    if not amounts:
        return None, None
    lo, hi = min(amounts), max(amounts)
    has_range = re.search(r"\$\s?\d[\d,]*\s*(?:-|–|to)\s*\$?\s?\d", str(text))
    if re.search(r"up to", str(text), re.I) and not has_range:
        return None, hi  # 'up to $1,200 new / $3,000 VIP' gives a ceiling, not a minimum
    return lo, hi


def segment(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower().replace("non-prime", "nonprime").replace("near prime", "near-prime")
    hits = [(t.find(k), v) for k, v in (
        ("subprime", "subprime"), ("sub-prime", "subprime"), ("nonprime", "nonprime"),
        ("near-prime", "near-prime"), ("all credit", "all"), ("prime", "prime"),
    ) if k in t]
    if not hits:
        return None
    return min(hits)[1]


def online_or_branch(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    if t.startswith("both") or ("branch" in t and "online" in t) or "locations plus online" in t:
        return "both"
    if "online" in t:
        return "online"
    if "branch" in t or "store" in t:
        return "branch"
    return None


def states(text: str | None) -> dict:
    """Return {'basis': 'lends'|'excluded'|None, 'states': [...], 'count': int|None, 'nationwide': bool}."""
    out = {"basis": None, "states": [], "count": None, "nationwide": False}
    if not text:
        return out
    t = str(text)
    if re.search(r"complaint states", t, re.I):
        return out  # where complaints came from, not where it lends
    m = re.search(r"(\d+)\s+states", t, re.I)
    if m:
        out["count"] = int(m.group(1))
    if re.search(r"\bnationwide\b|all 50 states", t, re.I) and not re.search(r"not available nationwide", t, re.I):
        out["nationwide"] = True
    found = []
    for m in _STATE_NAME_RE.finditer(t):
        found.append(_NAME_TO_CODE[m.group(1).lower()])
    for m in _STATE_CODE_RE.finditer(t):
        found.append(m.group(1))
    seen = []
    for s in found:
        if s not in seen:
            seen.append(s)
    out["states"] = seen
    if seen:
        negated = re.search(
            r"not available in|not offered in|does not lend|doesn'?t lend|not lend|no loans in|not licensed in|"
            r"\bexcept\b|excluding|excludes|\bnot\s+(?=[A-Z]{2}\b)|;\s*not\b", t, re.I)
        out["basis"] = "excluded" if negated else "lends"
        if negated:
            # only the states named after the negation are excluded ('all 50 states, DC and PR except WV' -> WV)
            tail = t[negated.start():]
            after = [_NAME_TO_CODE[x.group(1).lower()] for x in _STATE_NAME_RE.finditer(tail)] + \
                    [x.group(1) for x in _STATE_CODE_RE.finditer(tail)]
            out["states"] = [st for st in seen if st in after] or seen
            out["nationwide"] = False  # an exclusion list, not 'everywhere'
        if re.search(r"minimums in|rates pages", t, re.I):
            out["basis"] = None  # mentions states without saying it lends/excludes there
            out["states"] = []
    return out


def brands(text: str | None) -> list[dict]:
    """'A (a.com) - A Finance, LLC; B - b.com' -> [{'name','domains','entity','detail'}]."""
    if not text:
        return []
    out = []
    for part in re.split(r";\s*", str(text)):
        part = part.strip()
        if not part:
            continue
        doms = find_domains(part)
        name = re.split(r"\s+\(|\s+-\s+|\s+–\s+|:", part, maxsplit=1)[0].strip()
        if norm_domain(name) and name.lower() in doms:  # 'acme.com' as the whole name
            name = name.split(".")[0]
        rest = part[len(name):]
        rest = re.sub(r"^\s*\([^)]*\)", "", rest)
        rest = re.sub(r"^\s*[-–:,]\s*", "", rest).strip()
        rest = re.sub(r"^(?:operated|owned|run|issued|made)\s+by\s+", "", rest, flags=re.I).strip()
        entity = None
        if rest and re.fullmatch(r"[A-Z0-9][\w&.,' -]*\b(?:LLC|L\.L\.C\.|Inc\.?|Corp\.?|Corporation|Ltd\.?|LP)", rest) \
                and not find_domains(rest):
            entity = rest
        out.append({"name": name, "domains": doms, "entity": entity, "detail": rest or None})
    return out


def list_appearances(text: str | None) -> list[dict]:
    """'badcreditloans.com (+5 sites), bankrate.com' -> [{'domain':'badcreditloans.com','family_size':6}, ...]."""
    if not text:
        return []
    out = []
    for part in str(text).split(","):
        d = norm_domain(part.split("(")[0].strip())
        if not d:
            continue
        m = re.search(r"\+(\d+)\s+sites?", part)
        out.append({"domain": d, "family_size": 1 + (int(m.group(1)) if m else 0)})
    return out


def compliance_flags(text: str | None) -> list[str]:
    """Flag types mentioned in a litigation / risk note."""
    if not text:
        return []
    t = text.lower()
    found = []
    rules = [
        ("rent_a_tribe_litigation", r"rent-a-tribe|rent a tribe|tribal (?:lending )?(?:scheme|lawsuit)"),
        ("cfpb_action", r"\bcfpb\b.*\b(action|consent|order|sued|lawsuit|settle|enforcement)|consumer financial protection bureau"),
        ("ftc_action", r"\bftc\b"),
        ("state_ag", r"attorney general|\bag\b|state ag"),
        ("state_licence_alert", r"\bdfi\b|not licensed|licen[cs]e (?:alert|revoked|suspended)|consumer alert|cease and desist"),
        ("class_action", r"class action"),
    ]
    for flag, pat in rules:
        if re.search(pat, t):
            found.append(flag)
    if not found and re.search(r"lawsuit|litigation|sued|settle|fine|penalt|enforcement|regulator", t):
        found.append("other")
    return found


def hedge_status(text: str | None, confidence: str | None = None) -> str:
    """Edge status from the wording of an operator-group cell."""
    t = (text or "").lower()
    if re.search(r"possible|\bcheck\b|\?|rumou?r", t):
        return "rumoured"
    if "inferred" in t or "likely" in t or "appears" in t:
        return "inferred"
    return "confirmed" if confidence == "high" else "inferred"


def strip_hedge(text: str) -> str:
    """'Heights Finance (inferred)' -> 'Heights Finance'; 'POSSIBLE X (note) - check' -> 'X'."""
    s = re.sub(r"\((?:inferred|likely|possible)[^)]*\)", "", text, flags=re.I)
    s = re.sub(r"^\s*possible\s+", "", s, flags=re.I)
    s = re.sub(r"\s*-\s*check\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    return s.strip()


def source_type_for_url(url: str) -> str:
    u = url.lower()
    host = norm_domain(u) or ""
    if "sec.gov" in host:
        return "sec_filing"
    if "consumerfinance.gov" in host:
        return "cfpb"
    if host.endswith(".gov") or "nmls" in host:
        return "regulator"
    if any(k in host for k in ("courtlistener", "justia", "pacer", "casetext", "law360", "classaction")):
        return "court"
    if any(k in host for k in ("indeed", "linkedin.com/jobs", "glassdoor", "greenhouse", "lever.co", "workday")):
        return "job_post"
    if any(k in host for k in (
        "news", "times", "post", "journal", "reuters", "bloomberg", "businesswire", "prnewswire",
        "globenewswire", "montanan", "american-banker", "americanbanker", "fortune", "cnbc", "tribune",
    )):
        return "news"
    return "site_read"


def urls(text: str | None) -> list[str]:
    if not text:
        return []
    return [u.rstrip(".,);") for u in re.findall(r"https?://\S+", str(text))]
