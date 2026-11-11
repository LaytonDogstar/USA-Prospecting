"""Name and domain normalisation used for dedup and matching."""
import re
import unicodedata
from difflib import SequenceMatcher

LEGAL_SUFFIXES = {
    "llc", "l l c", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "lp", "llp", "plc", "pllc", "na", "n a", "fsb",
}
DBA = re.compile(r"\s+(?:d/b/a|dba|d\.b\.a\.?|doing business as)\s+", re.I)
DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|net|org|co|io|us|loans|loan|money|finance|"
    r"credit|cash|biz|info|ai|app|vi|ca|uk|news|media|tv|fm)(?:\.[a-z]{2})?)\b",
    re.I,
)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def split_dba(name: str) -> tuple[str, list[str]]:
    """'Ningo Lending LLC dba Spotloan' -> ('Ningo Lending LLC', ['Spotloan'])."""
    parts = DBA.split(name.strip())
    return parts[0].strip(), [p.strip() for p in parts[1:] if p.strip()]


def norm_name(name: str | None) -> str:
    """Lowercase, ASCII, punctuation removed, '&' -> 'and', legal suffixes stripped from the end."""
    if not name:
        return ""
    s = _ascii(name).lower().replace("&", " and ").replace("`", "").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    words = s.split()
    while words and (words[-1] in LEGAL_SUFFIXES or " ".join(words[-2:]) in LEGAL_SUFFIXES):
        if " ".join(words[-2:]) in LEGAL_SUFFIXES and len(words) > 2:
            words = words[:-2]
        else:
            words = words[:-1]
    if words and words[0] == "the" and len(words) > 1:
        words = words[1:]
    return " ".join(words)


def compact(name: str | None) -> str:
    """norm_name without spaces, so 'Acme Loans' == 'AcmeLoans' == 'acmeloans'."""
    return norm_name(name).replace(" ", "")


def norm_domain(value: str | None) -> str | None:
    if not value:
        return None
    s = str(value).strip().lower()
    if s in {"—", "-", "–", "n/a", "none", ""}:
        return None
    s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split("?")[0].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s if "." in s and " " not in s else None


def find_domains(text: str | None) -> list[str]:
    if not text:
        return []
    out = []
    for m in DOMAIN_RE.finditer(str(text)):
        d = norm_domain(m.group(1))
        if d and d not in out:
            out.append(d)
    return out


def domain_stem(domain: str) -> str:
    """'cashadvancenow.com' -> 'cashadvancenow'."""
    return domain.split(".")[0]


GENERIC_WORDS = {
    "credit", "lending", "lender", "loan", "loans", "finance", "financial", "financing", "cash", "money", "capital",
    "funding", "fund", "funds", "group", "holdings", "holding", "services", "service", "solutions", "online", "direct",
    "advance", "payday", "installment", "personal", "consumer", "bank", "union", "national", "american", "first",
    "us", "usa", "company", "corp", "of", "and", "the", "net", "my", "now", "today", "express", "quick", "fast", "easy",
}


def is_generic(name: str | None) -> bool:
    """True when every word of the name is a generic industry word ('Credit Corp', 'Fast Cash Loans')."""
    words = norm_name(name).split() if name and " " in str(name).strip() else [str(name or "").lower()]
    return all(w in GENERIC_WORDS or len(w) <= 2 for w in words) or compact(name) in GENERIC_WORDS


def contains_name(a: str, b: str) -> bool:
    """One compact name inside the other, ignoring short or generic names."""
    ca, cb = compact(a), compact(b)
    if not ca or not cb or ca == cb:
        return False
    short, long_ = sorted((ca, cb), key=len)
    return len(short) >= 7 and short not in GENERIC_WORDS and short in long_


def name_similarity(a: str, b: str) -> float:
    """0..1. Containment of one compact name in the other counts as 0.9 (see contains_name)."""
    ca, cb = compact(a), compact(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if contains_name(a, b):
        return 0.9
    return SequenceMatcher(None, ca, cb).ratio()


def raw_key(name: str | None) -> str:
    """Lowercase letters and digits only, legal suffixes kept: 'Acme, Inc.' -> 'acmeinc'."""
    return re.sub(r"[^a-z0-9]", "", _ascii(name or "").lower())
