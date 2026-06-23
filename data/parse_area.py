"""
Shared hectare-extraction utilities used by all scrapers.

Partial ownership detection
---------------------------
Brazilian auctions sometimes sell only a fractional share of a property:
  "Imóvel Rural, A.T. 382 ha, Barreirinho (Parte ideal 2,5888% ou 11,4 ha)"

parse_hectares_with_partial() handles this: when "parte ideal" / "fração ideal"
is detected it looks for an explicit "ou X ha" figure after the keyword, or
falls back to searching only the text after the keyword (to avoid returning
the total area instead of the effective share). Returns (hectares, is_partial).

Handles every number format seen in Brazilian auction descriptions:

  Integer              741 ha
  BR decimal           237,6 ha
  BR thousands         1.500 ha
  BR thousands+dec     1.234,56 ha
  EN decimal           50.5 ha
  EN thousands+dec     1,234.56 ha
  Agrarian notation    209,93,50 ha  (hectares, ares, centiares — any separator: X,YY,ZZ or X.YY,ZZ)
  No-space             50.5ha

Unit keywords (case-insensitive):
  ha / has / Ha / HA
  hectare / hectares / hec / hect  (and common misspellings via hec?tare?s?)
  alqueire / alqueires  (→ × 2.42)
  m² / m2 / metros quadrados  (→ ÷ 10,000)
  km² / km2  (→ × 100)
"""

from __future__ import annotations

import re
from typing import Optional

# 1 alqueire paulista ≈ 2.42 ha (most common in SP/PR/MG)
_ALQUEIRE_TO_HA = 2.42

# ── Compiled patterns ─────────────────────────────────────────────────────────

# Agrarian notation X,YY,ZZ ha — must be tried BEFORE the general ha pattern
# to avoid a partial match on just "YY,ZZ ha".
_HA_UNIT = r"(?:h[aá]s?\.?|hec?tare?s?|alqueire?s?)"

_HA_AGRARIAN = re.compile(
    r"(\d+)[.,](\d{2})[.,](\d{2})[\s-]*" + _HA_UNIT,
    re.IGNORECASE,
)

# General ha / alqueires — captures the numeric part; unit detected from match text.
# Number part covers: integers, BR decimals, BR thousands, EN decimals, EN thousands.
# Separator between number and unit: whitespace OR hyphen (URL slugs use hyphens).
_HA_PAT = re.compile(
    r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|[\d]+(?:[.,]\d+)?)"
    r"[\s-]*" + _HA_UNIT,
    re.IGNORECASE,
)

# km² / km2 → × 100
_KM2_PAT = re.compile(
    r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|[\d]+(?:[.,]\d+)?)"
    r"[\s-]*"
    r"(?:km[²2²]|km\s*2\b)",
    re.IGNORECASE,
)

# m² / m2 / metros quadrados → ÷ 10,000
_M2_PAT = re.compile(
    r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|[\d]+(?:[.,]\d+)?)"
    r"[\s-]*"
    r"(?:m[²2²]|m\s*2\b|metros?\s+quadrados?)",
    re.IGNORECASE,
)


# ── Number normaliser ─────────────────────────────────────────────────────────

def normalise_number(raw: str) -> Optional[float]:
    """
    Parse a numeric string from Brazilian or English notation into a float.

    Rules:
      Both . and , present → whichever comes last is the decimal separator.
        "1.234,56" → 1234.56   (BR: dot=thousands, comma=decimal)
        "1,234.56" → 1234.56   (EN: comma=thousands, dot=decimal)
      Only , present:
        Followed by exactly 3 digits at end → EN thousands ("1,234" → 1234)
        Otherwise → BR decimal ("237,6" → 237.6)
      Only . present:
        Followed by exactly 3 digits at end → BR thousands ("1.500" → 1500)
        Otherwise → EN/plain decimal ("50.5" → 50.5)
    """
    raw = raw.strip()
    has_dot   = "." in raw
    has_comma = "," in raw

    try:
        if has_dot and has_comma:
            last_dot   = raw.rfind(".")
            last_comma = raw.rfind(",")
            if last_comma > last_dot:
                # BR format: "1.234,56"
                return float(raw.replace(".", "").replace(",", "."))
            else:
                # EN format: "1,234.56"
                return float(raw.replace(",", ""))
        elif has_comma:
            # Only commas — check if it looks like EN thousands ("1,234")
            after_last_comma = raw.rsplit(",", 1)[-1]
            if len(after_last_comma) == 3 and after_last_comma.isdigit():
                return float(raw.replace(",", ""))   # EN thousands
            return float(raw.replace(",", "."))       # BR decimal
        elif has_dot:
            # Only dots — check if it looks like BR thousands ("1.500")
            after_last_dot = raw.rsplit(".", 1)[-1]
            if len(after_last_dot) == 3 and after_last_dot.isdigit():
                return float(raw.replace(".", ""))    # BR thousands
            return float(raw)                         # plain decimal
        else:
            return float(raw)
    except ValueError:
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_hectares(text: str, include_m2: bool = True) -> Optional[float]:
    """
    Extract the first plausible hectare value from free text.

    Priority order:
      1. Agrarian notation   X,YY,ZZ ha
      2. ha / hectares / alqueires
      3. km²  (× 100)
      4. m²   (÷ 10,000)  — skipped when include_m2=False

    Returns None if nothing is found or the result is zero / negative.
    """
    # 1. Agrarian notation: must come first to avoid partial matches
    for m in _HA_AGRARIAN.finditer(text):
        try:
            val = int(m.group(1)) + int(m.group(2)) / 100 + int(m.group(3)) / 10_000
        except ValueError:
            continue
        if val > 0:
            return round(val, 4)

    # 2. ha / hectares / alqueires
    for m in _HA_PAT.finditer(text):
        val = normalise_number(m.group(1))
        if val is None or val <= 0:
            continue
        if re.search(r"alqueire", m.group(0), re.IGNORECASE):
            val = round(val * _ALQUEIRE_TO_HA, 4)
        if val > 0:
            return round(val, 4)

    # 3. km² → ha
    for m in _KM2_PAT.finditer(text):
        val = normalise_number(m.group(1))
        if val is None or val <= 0:
            continue
        val = round(val * 100, 4)
        if val > 0:
            return val

    # 4. m² → ha (optional — skip when caller wants ha-priority-only pass)
    if include_m2:
        for m in _M2_PAT.finditer(text):
            val = normalise_number(m.group(1))
            if val is None or val <= 0:
                continue
            val = round(val / 10_000, 4)
            if val > 0:
                return val

    return None


# ── Partial ownership detection ───────────────────────────────────────────────

_PARTIAL_KW = re.compile(
    r"parte\s+ideal|fra[çc][aã]o\s+ideal",
    re.IGNORECASE,
)

# "ou X ha" — the explicit effective-area figure that follows the percentage
_PARTIAL_OU_HA = re.compile(
    r"\bou\s+([\d][,.\d]*)\s*(?:h[aá]s?\.?|hec?tare?s?)",
    re.IGNORECASE,
)


def parse_hectares_with_partial(text: str, include_m2: bool = True) -> tuple:
    """
    Return (hectares, is_partial) where:
      - hectares  : effective area being sold (not necessarily the total property)
      - is_partial: True when "parte ideal" / "fração ideal" was detected

    When partial ownership is detected:
      1. Look for an explicit "ou X ha" figure after the keyword → use that.
      2. Otherwise search only the substring from the keyword onward, so the
         total-area figure that precedes the keyword is ignored.
      3. If still nothing found, fall back to the full text (better than None).

    include_m2: passed through to parse_hectares. Set False when parsing a
    title so that m² in the title doesn't block a better ha value in the
    description (callers should do a second pass with include_m2=True).
    """
    m_kw = _PARTIAL_KW.search(text)
    if not m_kw:
        return parse_hectares(text, include_m2=include_m2), False

    # 1. Explicit "ou X ha" anywhere after the keyword
    after = text[m_kw.start():]
    m_ou = _PARTIAL_OU_HA.search(after)
    if m_ou:
        val = normalise_number(m_ou.group(1))
        if val and val > 0:
            return round(val, 4), True

    # 2. First ha/alqueires/etc. value in the text after the keyword
    ha = parse_hectares(after, include_m2=include_m2)
    if ha:
        return ha, True

    # 3. Full-text fallback (e.g. only total area present, no explicit share ha)
    return parse_hectares(text, include_m2=include_m2), True
