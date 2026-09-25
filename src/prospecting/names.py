"""Parsing of pingtree buyer/tier names."""
import re

RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$")


def split_buyer_tier(raw: str) -> tuple[str, str]:
    """Split 'Buyer (Tier)' on the last balanced parenthesis group.

    Handles nested groups, e.g. 'Acme Corp (Brand) (T1 Day (BR) - ID 104)'
    -> ('Acme Corp (Brand)', 'T1 Day (BR) - ID 104').
    """
    name = raw.strip()
    if not name.endswith(")"):
        return name, ""
    depth = 0
    for i in range(len(name) - 1, -1, -1):
        if name[i] == ")":
            depth += 1
        elif name[i] == "(":
            depth -= 1
            if depth == 0:
                return name[:i].strip(), name[i + 1:-1].strip()
    return name, ""


def is_price_reject(tier: str) -> bool:
    return "price reject" in tier.lower()


def looks_like_filter_value(tier: str) -> bool:
    """True when the filter export put a numeric range in the tier column instead of a name."""
    return bool(RANGE.match(tier))


def parse_range(value: str) -> tuple[float | None, float | None]:
    """'200.00 - 2000.00' -> (200, 2000). An upper bound of 0 means no upper limit."""
    m = RANGE.match(value)
    if not m:
        return None, None
    lo, hi = float(m.group(1)), float(m.group(2))
    return lo, (hi if hi > 0 else None)


def split_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]
