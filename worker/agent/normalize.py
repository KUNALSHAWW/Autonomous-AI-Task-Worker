"""Value normalisation so checks compare meaning, not formatting.

"$4,107.41" == "4107.41", "October 10, 2026" == "2026-10-10" == "10 Oct 2026".
Ambiguous numeric dates (03/04/2026) are compared under both readings.
"""
from __future__ import annotations

import re
from datetime import datetime

_DATE_FORMATS = ["%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d-%b-%Y", "%Y/%m/%d",
                 "%B %d %Y", "%b %d %Y", "%d.%m.%Y"]


def dates(s: str) -> set[str]:
    s = re.sub(r"\s+", " ", str(s).strip().rstrip("."))
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    out = set()
    for f in _DATE_FORMATS:
        try:
            out.add(datetime.strptime(s, f).date().isoformat())
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        for d, mo in ((a, b), (b, a)):
            try:
                out.add(datetime(y, mo, d).date().isoformat())
            except ValueError:
                pass
    return out


def number(s) -> float | None:
    t = re.sub(r"\b(USD|EUR|GBP|INR|US\$)\b", "", str(s).strip(), flags=re.I)
    if not re.search(r"\d", t) or re.search(r"[a-zA-Z]", t):
        return None
    if re.search(r"\d\s*-\s*\d", t):  # ranges, ids and phone numbers are not amounts
        return None
    t = re.sub(r"[^\d.\-]", "", t.replace(",", ""))
    try:
        return float(t)
    except ValueError:
        return None


def text(s) -> str:
    return re.sub(r"[\s\-_.,:;'\"]+", " ", str(s)).strip().lower()


def same(a, b) -> bool:
    if a is None or b is None:
        return False
    da, db_ = dates(str(a)), dates(str(b))
    if da and db_:
        return bool(da & db_)
    na, nb = number(a), number(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 0.005
    return text(a) == text(b)
