"""
Scraper for megaleiloes.com.br — rural property (imóveis rurais) listings.

Extracts per listing:
  - property_name  : str
  - state          : str  (2-letter UF code)
  - city           : str
  - hectares       : float | None
  - auction_price  : float | None  (lowest available round price)
  - auction_date   : str  (ISO date of next/first praça, or raw string)
  - listing_url    : str
  - auction_type   : str  ("Judicial" | "Extrajudicial" | "Venda Direta" | "")
  - lot_id         : str
  - source         : "megaleiloes"

Usage:
    from scrapers.megaleiloes import scrape
    listings = scrape(max_pages=5)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from data.parse_area import parse_hectares as _parse_hectares, parse_hectares_with_partial as _parse_hectares_wp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.megaleiloes.com.br"

# ---------------------------------------------------------------------------
# Rural / farmland / timberland classification
# ---------------------------------------------------------------------------

# Keywords that positively identify farmland or timberland potential.
# A listing matching ANY of these is kept.
_RURAL_KEEP = re.compile(
    r"\bfazenda\b|\bs[íi]tio\b|\bch[áa]cara\b|\bharas\b|"
    r"\bpropriedade\s+rural\b|\bim[óo]vel\s+rural\b|"
    r"\bterra\s+rural\b|\bterreno\s+rural\b|"
    r"\bterrenos?\b|\bgleba\b|\bloteamento\b|"
    r"\bpastagem\b|\blavoura\b|\bpasto\b|\bcerrado\b|\bcaatinga\b|"
    r"\bgado\b|\bbovino\b|\bpecuária\b|\bpec[uú]aria\b|"
    r"\bsoja\b|\bmilho\b|\bcana[-\s]de[-\s]a[cç][uú]car\b|\bcafé\b|\balgodão\b|\barroz\b|"
    r"\beucalipto\b|\bpinus\b|\breflorestamento\b|\bsilvicultura\b|\bteca\b|\bseringueira\b|"
    r"\breserva\s+legal\b|\bárea\s+de\s+preserva[cç][aã]o\b|"
    r"\bhectares?\b|\b\d+[.,]\d+\s*ha\b|\b\d+\s*ha\b",
    re.IGNORECASE,
)

# Keywords that flag urban / residential listings to be rejected.
# A listing matching ANY of these is dropped, UNLESS it also strongly matches rural.
_URBAN_REJECT = re.compile(
    r"\bapartamento\b|\bapto\b|\bap\.\b|"
    r"\bcasa\b|\bresid[eê]ncia\b|\bsobrado\b|\bcobertura\b|"
    r"\bkit(inet(e)?|net)\b|\bloft\b|\bestudio\b|"
    r"\bsala\s+comercial\b|\bsala\s+de\s+estar\b|\bescrit[oó]rio\b|"
    r"\bponto\s+comercial\b|\bloja\b|\bgalpão\s+urbano\b|"
    r"\bcondom[íi]nio\b|\bedif[íi]cio\b|\bbloco\b|\btorre\b|"
    r"\bgaragem\b|\bvaga\b|\bestacionamento\b|"
    r"\brua\b|\bav\.\b|\bavenida\b|\bbairro\b|\bcep\b|"
    r"\burn[oau]\b|\bn[°º]\s*\d|\bapartamentos\b|"
    r"\bimóvel\s+urbano\b|\bterreno\s+urbano\b|\blote\s+urbano\b|"
    r"\bimóvel\s+comercial\b",
    re.IGNORECASE,
)


def _is_rural(listing: dict) -> bool:
    """
    Return True if the listing is a rural / farmland / timberland property.

    Decision logic:
      1. If the title has a strong urban signal → reject immediately.
      2. If the title has any rural keyword → keep.
      3. If hectares >= 1 ha AND no urban signal → keep (large area implies rural).
      4. Default → reject (unknown listings excluded to avoid urban contamination).

    Note: URL-based check removed — megaleiloes returns mixed content even on the
    rural subcategory URL, so we rely entirely on title text.
    """
    name = (listing.get("property_name") or "").lower()

    has_urban = bool(_URBAN_REJECT.search(name))
    has_rural = bool(_RURAL_KEEP.search(name))

    # Strong urban signals that override any incidental rural keyword match
    # (e.g. "Apartamento ... - Gleba Fazenda - PR" — neighbourhood name, not rural)
    _HARD_URBAN = re.compile(
        r"^\s*(?:apartamento|apto|casa\b|sobrado|cobertura|loft|kit(?:inet)?|"
        r"im[óo]vel\s+comercial|sala\s+comercial|ponto\s+comercial|loja\b)",
        re.IGNORECASE,
    )
    if _HARD_URBAN.search(name):
        logger.debug("Rejected hard-urban listing: %s", listing.get("property_name"))
        return False

    # Other urban keyword present and no rural keyword → reject
    if has_urban and not has_rural:
        logger.debug("Rejected urban listing: %s", listing.get("property_name"))
        return False

    if has_rural:
        # Extra check: title explicitly states a tiny m² size → urban plot using a rural word
        # e.g. "Chácara 833 m²" is a city lot, not a rural property
        tiny_m2 = re.search(r"\b(\d+(?:[.,]\d+)?)\s*m[²2²]", name, re.IGNORECASE)
        if tiny_m2:
            try:
                m2_val = float(tiny_m2.group(1).replace(",", "."))
                if m2_val < 2_000:   # < 0.2 ha in the title → urban
                    logger.debug("Rejected tiny-plot listing: %s", listing.get("property_name"))
                    return False
            except ValueError:
                pass
        return True

    # Large area (≥1 ha) with no urban signal → likely rural
    ha = listing.get("hectares")
    if ha and ha >= 1.0:
        return True

    # Unknown or tiny area (apartments parsed as m²) → reject
    logger.debug("Rejected unclassifiable listing: %s", listing.get("property_name"))
    return False
# Dedicated rural category — pre-filtered by the site, no keyword filter needed.
# This is a single flat page (~27 listings); pagination params have no effect.
RURAL_URL = f"{BASE_URL}/imoveis/imoveis-rurais"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE_URL,
}

# Matches Brazilian currency: "R$ 1.234.567,89" → float 1234567.89
_BRL_PATTERN = re.compile(r"R\$\s*([\d.,]+)")

# Matches dates like "28/04/2026" or "28/04/2026 às 11:00"
_DATE_PATTERN = re.compile(r"(\d{2}/\d{2}/\d{4})")

# State abbreviations embedded in text like "Andaraí, BA" or "BA"
_UF_CODES = {
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA",
    "MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN",
    "RS","RO","RR","SC","SP","SE","TO",
}


def _parse_brl(text: str) -> Optional[float]:
    """Convert Brazilian currency string to float."""
    m = _BRL_PATTERN.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_date_iso(text: str) -> Optional[str]:
    """Return first found date as ISO string YYYY-MM-DD, or None."""
    m = _DATE_PATTERN.search(text)
    if not m:
        return None
    day, month, year = m.group(1).split("/")
    return f"{year}-{month}-{day}"


def _extract_state(text: str) -> str:
    """Pull 2-letter UF from a string like 'Andaraí, BA' or bare 'BA'."""
    parts = [p.strip().upper() for p in re.split(r"[,\s]+", text)]
    for part in reversed(parts):
        if part in _UF_CODES:
            return part
    return ""


def _extract_city(text: str) -> str:
    """Extract city name before the UF code."""
    text = text.strip()
    # e.g. "Andaraí, BA" → "Andaraí"
    m = re.match(r"^(.+?),?\s+[A-Z]{2}$", text)
    if m:
        return m.group(1).strip()
    return text


def _parse_listing(card) -> Optional[dict]:
    """Parse a single div.card into a dict using confirmed CSS class names.

    Round data strategy
    -------------------
    The site always emits ``span.card-first-instance-date`` for round 1 and
    ``span.card-second-instance-date`` for round 2 (or 3rd praça on 3-praça lots).
    The immediately following sibling ``span.card-instance-value`` holds the price.
    This is more reliable than looking for ``div.instance.first/second`` because:
      - 3-praça lots show two ``div.instance.active`` divs (first+active AND active)
        but the ``div.instance.second`` never appears on the card for those lots.
      - P. Única lots have no "first" instance div at all.

    Active round detection
    ----------------------
    ``div.instance.first.active``  → round 1 is active
    ``div.instance.active`` without "first" class → round 2+ is active
    The active ``div`` 's first child span class tells us the ordinal.
    """
    try:
        # --- URL + lot ID ---
        title_tag = card.find("a", class_="card-title")
        if not title_tag:
            return None
        url = title_tag["href"]
        if not url.startswith("http"):
            url = f"{BASE_URL}{url}"
        property_name = title_tag.get_text(strip=True)

        # --- Lot ID ---
        num_tag = card.find("div", class_="card-number")
        lot_id = num_tag.get_text(strip=True) if num_tag else ""
        if not lot_id:
            m = re.search(r"[/-]([jej]\d{4,})", url, re.I)
            lot_id = m.group(1).upper() if m else ""

        # --- Location ---
        loc_tag = card.find("a", class_="card-locality")
        location_text = loc_tag.get_text(strip=True) if loc_tag else ""
        state = _extract_state(location_text)
        city = _extract_city(location_text) if state else ""

        # --- Hectares ---
        hectares, is_partial = _parse_hectares_wp(property_name, include_m2=False)
        if hectares is not None and hectares < 0.4:
            return None

        # --- Auction type ---
        type_link = card.select_one("div.card-instance-title > a")
        auction_type = type_link.get_text(strip=True) if type_link else ""

        # --- Total rounds from "card-instances" badge ---
        inst_tag = card.find("div", class_="card-instances")
        total_rounds_text = inst_tag.get_text(strip=True) if inst_tag else ""
        rounds_m = re.search(r"(\d+)", total_rounds_text)
        if rounds_m:
            total_rounds = int(rounds_m.group(1))
        elif re.search(r"[Úu]nica|[Úu]nico|P\.\s*[Úu]", total_rounds_text):
            total_rounds = 1
        else:
            total_rounds = None

        # ── Read ALL round dates/prices from span pairs ───────────────────────
        # card-first-instance-date  → round 1 date  (also used for P. Única)
        # card-second-instance-date → round 2 date  (or 3rd praça on 3-praça lots)
        # Each is immediately followed by span.card-instance-value with the price.
        date_round1 = ""
        price_round1: Optional[float] = None
        date_round2 = ""
        price_round2: Optional[float] = None

        span1 = card.find("span", class_="card-first-instance-date")
        if span1:
            date_round1 = _parse_date_iso(span1.get_text()) or ""
            val1 = span1.find_next_sibling("span", class_="card-instance-value")
            price_round1 = _parse_brl(val1.get_text()) if val1 else None

        span2 = card.find("span", class_="card-second-instance-date")
        if span2:
            date_round2 = _parse_date_iso(span2.get_text()) or ""
            val2 = span2.find_next_sibling("span", class_="card-instance-value")
            price_round2 = _parse_brl(val2.get_text()) if val2 else None

        # ── Active round: which div.instance has the "active" class ──────────
        # div.instance.first.active  → round 1 is the current/next praça
        # div.instance.active (no "first") → round 2+ is the current/next praça
        active_round: Optional[int] = None
        auction_date = ""
        auction_price: Optional[float] = None

        # Find the active instance div
        active_inst = card.find("div", class_=lambda c: c and "instance" in c and "active" in c)
        if active_inst:
            inst_classes = active_inst.get("class", [])
            # Determine round from class name or from span class inside it
            date_span = active_inst.find("span")
            if "first" in inst_classes:
                active_round = 1
            else:
                # Use the span class to determine ordinal (second / third / etc.)
                if date_span:
                    for cls in (date_span.get("class") or []):
                        rm = re.search(r"card-(\w+)-instance-date", cls)
                        if rm:
                            active_round = {"first": 1, "second": 2, "third": 3}.get(rm.group(1))
                if active_round is None:
                    # Last resort: not round 1 (no "first") → assume round 2
                    active_round = 2

            # auction_date / auction_price = the active round's values
            if date_span:
                auction_date = _parse_date_iso(date_span.get_text()) or ""
            price_span = active_inst.find("span", class_="card-instance-value")
            if price_span:
                auction_price = _parse_brl(price_span.get_text())

        # Fallback price from card-price div
        if auction_price is None:
            price_div = card.find("div", class_="card-price")
            if price_div:
                auction_price = _parse_brl(price_div.get_text())

        # For P. Única: date_round1 / price_round1 already set from span1 above.
        # auction_date from active div may have "Data:" prefix but _parse_date_iso
        # handles it fine. Ensure auction_date matches round 1 for P. Única.
        if not auction_date and date_round1:
            auction_date = date_round1
        if auction_price is None and price_round1:
            auction_price = price_round1

        return {
            "property_name": property_name,
            "state": state,
            "city": city,
            "hectares": hectares,
            "auction_price": auction_price,
            "auction_date": auction_date,
            "listing_url": url,
            "auction_type": auction_type,
            "lot_id": lot_id,
            "source": "megaleiloes",
            "appraised_value": price_round1,
            "date_round1": date_round1,
            "price_round1": price_round1,
            "date_round2": date_round2,
            "price_round2": price_round2,
            "active_round": active_round,
            "total_rounds": total_rounds,
            "is_partial": is_partial,
        }
    except Exception as exc:
        logger.debug("Failed to parse card: %s", exc)
        return None


def _fetch_page(session: requests.Session) -> Optional[BeautifulSoup]:
    """Fetch the rural category page (single page, no pagination)."""
    try:
        resp = session.get(RURAL_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("megaleiloes: fetch failed: %s", exc)
        return None


_AVALIACAO_PAT = re.compile(
    r"(?:valor\s+de\s+avalia[cç][aã]o|avalia[cç][aã]o)[^\d]{0,30}(R\$\s*[\d.,]+)",
    re.IGNORECASE,
)


def _enrich_from_detail(
    session: requests.Session,
    listings: list[dict],
    delay: float,
) -> None:
    """Fetch detail pages for listings missing hectares or appraised_value."""
    to_fetch = [l for l in listings if l.get("hectares") is None or l.get("appraised_value") == l.get("price_round1")]
    if not to_fetch:
        return
    logger.info("megaleiloes: enriching %d detail pages", len(to_fetch))

    for listing in to_fetch:
        url = listing.get("listing_url", "")
        if not url:
            continue

        text = None
        for attempt in range(3):
            wait = delay * (2 ** attempt)
            time.sleep(wait)
            try:
                resp = session.get(url, headers=HEADERS, timeout=25)
                if resp.status_code in (404, 410):
                    break
                if resp.status_code != 200:
                    logger.debug("megaleiloes detail %d on attempt %d: %s", resp.status_code, attempt + 1, url)
                    continue
                text = resp.text
                break
            except requests.RequestException as exc:
                logger.debug("megaleiloes detail fetch attempt %d failed %s: %s", attempt + 1, url, exc)

        if not text:
            continue

        soup = BeautifulSoup(text, "html.parser")
        page_text = soup.get_text(" ", strip=True)

        if listing.get("hectares") is None:
            ha, is_partial = _parse_hectares_wp(page_text)
            if ha:
                listing["hectares"] = ha
                listing["is_partial"] = is_partial
                logger.debug("megaleiloes enriched ha %s → %.4f ha", url, ha)

        # Try to find a real appraised value ("Valor de avaliação") on the detail page
        av_m = _AVALIACAO_PAT.search(page_text)
        if av_m:
            from data.parse_area import normalise_number
            raw = av_m.group(1).replace("R$", "").strip()
            av = normalise_number(raw)
            if av and av > 0:
                listing["appraised_value"] = av
                logger.debug("megaleiloes enriched appraised_value %s → %.0f", url, av)


def _detect_listing_cards(soup: BeautifulSoup) -> list:
    """Return individual listing cards (div.card elements that have a card-title link)."""
    cards = [
        d for d in soup.find_all("div", class_="card")
        if d.find("a", class_="card-title")
    ]
    logger.debug("Found %d cards", len(cards))
    return cards


def scrape(max_pages: int = 1, delay: float = 2.0, start_page: int = 1,
           **_kwargs) -> list[dict]:
    """
    Scrape rural property listings from megaleiloes.com.br/imoveis/imoveis-rurais.

    This is a single flat page (~27 listings) pre-filtered by the site — no
    keyword filtering needed. max_pages and start_page are accepted for API
    compatibility but the source has no pagination; any call with start_page > 1
    returns an empty list (already fetched everything on page 1).
    """
    if start_page > 1:
        # Single-page source — nothing left after the first fetch
        logger.info("megaleiloes: start_page=%d > 1, nothing more to fetch", start_page)
        return []

    session = requests.Session()
    soup = _fetch_page(session)
    if soup is None:
        return []

    cards = _detect_listing_cards(soup)
    listings = []
    for card in cards:
        result = _parse_listing(card)
        if result:
            listings.append(result)

    logger.info("megaleiloes: %d rural lots on category page", len(listings))

    _enrich_from_detail(session, listings, delay=delay)

    logger.info("megaleiloes: total %d rural lots after enrichment", len(listings))
    return listings


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = scrape(max_pages=3)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n--- {len(results)} listings ---")
