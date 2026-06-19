"""
Scraper for superbid.net — rural property (Imóveis Rurais) and terrain (Terrenos) listings.

Data source
-----------
The site is Next.js SSR. All listing data is embedded in __NEXT_DATA__ JSON and also
available directly from the offer-query microservice API:

  https://offer-query.superbid.net/seo/offers/
    ?locale=pt_BR
    &portalId=[2,15]
    &requestOrigin=marketplace
    &timeZoneId=UTC
    &preOrderBy=orderByFirstOpenedOffersAndSecondHasPhoto
    &filter=
    &orderBy=score:desc
    &searchType=opened
    &urlSeo=https://www.superbid.net/categorias/imoveis/<slug>
    &start=<offset>   ← pagination: 0, 30, 60, …

Categories scraped
------------------
  imoveis-rurais  — Imóveis Rurais (Fazendas, Glebas, Sítios, etc.)   ~120 listings
  terrenos        — Terrenos                                             ~108 listings

Returns per listing:
  - property_name      : str
  - state              : str  (2-letter UF)
  - city               : str
  - hectares           : float | None  (from template property 'areatotal')
  - auction_price      : float | None  (currentMinBid — lowest active bid value)
  - auction_date       : str  (ISO YYYY-MM-DD of auction endDate)
  - listing_url        : str  (https://www.superbid.net/lote/<id>)
  - auction_type       : str  (modalityDesc, e.g. "Leilão", "Tomada de preço")
  - lot_id             : str  (offer id)
  - source             : "superbid"
  - appraised_value    : float | None  (referenceValue — avaliação)
  - price_round1       : float | None  (initialBidValue)
  - date_round1        : str  (beginDate ISO)
  - date_round2        : str  (endDate ISO — same as auction_date)
  - price_round2       : float | None  (currentMinBid)
  - active_round       : int | None
  - total_rounds       : None  (superbid does not use praça rounds)

Usage:
    from scrapers.superbid import scrape
    listings = scrape(max_pages=4, start_page=1)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

from data.parse_area import parse_hectares as _parse_hectares_from_text, parse_hectares_with_partial as _parse_hectares_wp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.superbid.net"
_EXCHANGE_URL = "https://exchange.superbid.net"
_API_URL = "https://offer-query.superbid.net/seo/offers/"
_PAGE_SIZE = 30

# Category slugs to scrape
_CATEGORIES = [
    "imoveis-rurais",
    "terrenos",
]

_UF_BY_STATE: dict[str, str] = {
    "Acre": "AC", "Alagoas": "AL", "Amapá": "AP", "Amazonas": "AM",
    "Bahia": "BA", "Ceará": "CE", "Distrito Federal": "DF",
    "Espírito Santo": "ES", "Goiás": "GO", "Maranhão": "MA",
    "Mato Grosso": "MT", "Mato Grosso do Sul": "MS", "Minas Gerais": "MG",
    "Pará": "PA", "Paraíba": "PB", "Paraná": "PR", "Pernambuco": "PE",
    "Piauí": "PI", "Rio de Janeiro": "RJ", "Rio Grande do Norte": "RN",
    "Rio Grande do Sul": "RS", "Rondônia": "RO", "Roraima": "RR",
    "Santa Catarina": "SC", "São Paulo": "SP", "Sergipe": "SE",
    "Tocantins": "TO",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.superbid.net/",
}


def _slugify(text: str) -> str:
    """Convert a property name to a URL slug matching superbid's format.

    Example: "Imóvel Rural 0,59939 ha na Fazenda Santa Maria, Serra do Salitre/MG"
          →  "imovel-rural-059939-ha-na-fazenda-santa-maria-serra-do-salitre-mg"

    Steps:
      1. NFKD normalise → strip accent combining chars
      2. Lowercase
      3. "/" and "\\" → "-"
      4. Remove commas and dots (numbers like "0,59939" become "059939")
      5. Replace any remaining non-alphanumeric chars with "-"
      6. Collapse consecutive dashes, strip leading/trailing dashes
    """
    import unicodedata
    # Step 1: strip accents
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Step 2: lowercase
    text = text.lower()
    # Step 3: slashes → dash
    text = text.replace("/", "-").replace("\\", "-")
    # Step 4: remove commas and dots
    text = text.replace(",", "").replace(".", "")
    # Step 5: non-alphanumeric → dash
    text = re.sub(r"[^a-z0-9]+", "-", text)
    # Step 6: collapse and strip
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def _parse_date_iso(dt_str: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD' to 'YYYY-MM-DD'."""
    if not dt_str:
        return ""
    return dt_str[:10]


def _get_template_prop(product: dict, prop_id: str) -> Optional[str]:
    """Extract a value from product.template.groups[].properties[] by id."""
    for grp in product.get("template", {}).get("groups", []):
        for prop in grp.get("properties", []):
            if prop.get("id") == prop_id and prop.get("value") is not None:
                return str(prop["value"])
    return None


# ── Area patterns for free-text title parsing ─────────────────────────────────
def _parse_hectares_from_template(raw: Optional[str]) -> Optional[float]:
    """Parse areatotal/areadoterreno from template (inconsistently m² or ha).

    Superbid stores these values without units. Heuristic:
      > 5000  → treat as m², divide by 10 000
      ≤ 5000  → treat as ha directly
    """
    if raw is None:
        return None
    try:
        val = _br_to_float(raw)
    except ValueError:
        return None
    if val <= 0:
        return None
    if val > 5000:
        return round(val / 10_000, 4)
    return val


def _parse_offer(offer: dict) -> Optional[dict]:
    """Map a single offer JSON object to the standard listing dict."""
    try:
        offer_id = str(offer.get("id", ""))
        if not offer_id:
            return None

        product = offer.get("product", {})
        location = product.get("location", {})
        detail = offer.get("offerDetail", {})
        auction = offer.get("auction", {})

        # --- Property name ---
        property_name = product.get("shortDesc", "").strip()

        # --- Location ---
        city_state = location.get("city", "")   # e.g. "São Félix do Araguaia - MT"
        state_full = location.get("state", "")  # e.g. "Mato Grosso"
        state = _UF_BY_STATE.get(state_full, "")
        if not state:
            # Fallback: parse from city string "City - UF"
            m = re.search(r"-\s*([A-Z]{2})\s*$", city_state)
            if m:
                state = m.group(1)
        city = re.sub(r"\s*-\s*[A-Z]{2}\s*$", "", city_state).strip()

        # --- Hectares ---
        # 1. Parse from title (ha/km² only — m² deferred until after description)
        hectares, is_partial = _parse_hectares_wp(property_name, include_m2=False)
        # 2. Fallback: product.detailedDescription (HTML — strip tags)
        if hectares is None:
            raw_desc = (
                product.get("detailedDescription")
                or (offer.get("offerDescription") or {}).get("offerDescription")
                or ""
            )
            if raw_desc:
                desc = re.sub(r"<[^>]+>", " ", raw_desc).strip()
                hectares, is_partial = _parse_hectares_wp(desc)
        # 3. Fallback: template properties (areatotal / areadoterreno)
        if hectares is None:
            area_raw = _get_template_prop(product, "areatotal")
            if area_raw is None:
                area_raw = _get_template_prop(product, "areadoterreno")
            hectares = _parse_hectares_from_template(area_raw)
            is_partial = False  # template field has no partial-ownership context

        # --- Prices ---
        # referenceValue  = avaliação (appraised value)
        # initialBidValue = lance inicial (round 1 / first praça equivalent)
        # currentMinBid   = lance mínimo atual (active bid floor = auction_price)
        appraised_value: Optional[float] = detail.get("referenceValue") or None
        price_round1: Optional[float] = detail.get("initialBidValue") or None
        auction_price: Optional[float] = detail.get("currentMinBid") or price_round1

        # --- Dates ---
        date_round1 = _parse_date_iso(auction.get("beginDate", ""))
        auction_date = _parse_date_iso(offer.get("endDate", ""))

        # --- Auction type ---
        auction_type = auction.get("modalityDesc", "")

        # --- Listing URL ---
        # Format: https://exchange.superbid.net/oferta/{slug}-{offer_id}
        slug = _slugify(property_name)
        listing_url = f"{_EXCHANGE_URL}/oferta/{slug}-{offer_id}"

        # --- Lot ID ---
        lot_id = f"sbid-{offer_id}"

        # --- Active round ---
        # judicialPraca: 1 = 1ª Praça, 2 = 2ª Praça, None = Praça Única (treat as 1)
        judicial_praca = auction.get("judicialPraca")
        active_round: Optional[int] = int(judicial_praca) if judicial_praca else 1
        # If active round is 2 we know there are at least 2 rounds; otherwise minimum 1.
        total_rounds: int = max(active_round or 1, 1)

        return {
            "property_name": property_name,
            "state": state,
            "city": city,
            "hectares": hectares,
            "auction_price": auction_price,
            "auction_date": auction_date,
            "listing_url": listing_url,
            "auction_type": auction_type,
            "lot_id": lot_id,
            "source": "superbid",
            "appraised_value": appraised_value,
            "price_round1": price_round1,
            "date_round1": date_round1,
            "date_round2": auction_date,
            "price_round2": auction_price,
            "active_round": active_round,
            "total_rounds": total_rounds,
            "is_partial": is_partial,
        }

    except Exception as exc:
        logger.debug("superbid: failed to parse offer %s: %s", offer.get("id"), exc)
        return None


def _fetch_category_page(
    session: requests.Session,
    category_slug: str,
    start: int,
) -> tuple[list[dict], int]:
    """Fetch one page of offers for a category. Returns (offers, total)."""
    params = {
        "locale": "pt_BR",
        "portalId": "[2,15]",
        "requestOrigin": "marketplace",
        "timeZoneId": "UTC",
        "preOrderBy": "orderByFirstOpenedOffersAndSecondHasPhoto",
        "filter": "",
        "orderBy": "score:desc",
        "searchType": "opened",
        "urlSeo": f"{BASE_URL}/categorias/imoveis/{category_slug}",
        "start": str(start),
    }
    try:
        resp = session.get(_API_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("offers", []), data.get("total", 0)
    except Exception as exc:
        logger.warning("superbid: fetch %s start=%d failed: %s", category_slug, start, exc)
        return [], 0


def scrape(
    max_pages: int = 4,
    delay: float = 1.0,
    start_page: int = 1,
    **_kwargs,
) -> list[dict]:
    """
    Scrape rural property and terrain listings from superbid.net.

    Fetches both 'imoveis-rurais' and 'terrenos' categories.
    Each page returns 30 listings; max_pages applies per category.

    Args:
        max_pages:  Maximum pages to fetch per category (default 4 = all ~120 lots).
        delay:      Seconds to wait between page requests.
        start_page: 1-based page number to start from (for Load More).
    """
    session = requests.Session()
    listings: list[dict] = []

    for category_slug in _CATEGORIES:
        start_offset = (start_page - 1) * _PAGE_SIZE
        pages_fetched = 0

        while pages_fetched < max_pages:
            raw_offers, total = _fetch_category_page(session, category_slug, start_offset)

            if not raw_offers:
                break

            for offer in raw_offers:
                result = _parse_offer(offer)
                if result:
                    listings.append(result)

            pages_fetched += 1
            start_offset += _PAGE_SIZE

            if start_offset >= total:
                break

            if pages_fetched < max_pages:
                time.sleep(delay)

        logger.info(
            "superbid: %s — fetched %d pages, %d listings so far",
            category_slug, pages_fetched, len(listings),
        )

    logger.info("superbid: total %d listings", len(listings))
    return listings


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = scrape(max_pages=2)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n--- {len(results)} listings ---")
