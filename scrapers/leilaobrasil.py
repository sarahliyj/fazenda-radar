"""
Scraper for leilaobrasil.com.br
================================
Strategy:
  - Server-side rendered HTML at /buscador?subcategoria={id}&page={n}
  - Rural subcategory IDs: 19=Fazenda, 20=Chácara, 44=Sítio, 58=Imóvel Rural
  - Terreno (18) is included but filtered by _is_rural() keyword check
  - Each listing card: <article class="lote-main bem-index-{lot_id}">
  - Lot detail URL: /eventos/leilao/{slug}/lote/{id}/{slug}
  - Detail page embeds: var lote = {...}; with full JSON (no extra HTTP call needed
    — all data we need is in the listing card itself)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from data.parse_area import parse_hectares as _parse_hectares, parse_hectares_with_partial as _parse_hectares_wp

logger = logging.getLogger(__name__)

BASE_URL = "https://leilaobrasil.com.br"
BUSCADOR_URL = f"{BASE_URL}/buscador"

# Rural subcategory IDs (from /api/buscadorMount?)
RURAL_SUBCATS: dict[int, str] = {
    19: "Fazenda",
    20: "Chácara",
    44: "Sítio",
    58: "Imóvel Rural",
    18: "Terreno",      # broad — filtered by _is_rural() below
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": BASE_URL + "/",
}

# ── Rural keyword filters ────────────────────────────────────────────────────
_RURAL_KEEP = re.compile(
    r"fazenda|sítio|sitio|chácara|chacara|gleba|rural|agrícola|agricola|"
    r"eucalipto|reflorestamento|teca|soja|cana|pastagem|pasto|lavoura|"
    r"cerrado|mata|floresta|imóvel rural|imovel rural",
    re.IGNORECASE,
)
_URBAN_REJECT = re.compile(
    r"apartamento|apto|edifício|edificio|prédio|predio|sala comercial|"
    r"loja|condomínio|condominio|flat|studio|kitnet|sobrado urbano|"
    r"box de garagem|vaga de garagem",
    re.IGNORECASE,
)

# ── Hectares extraction ──────────────────────────────────────────────────────
# Matches "município de Guaraçaí" or "municipio de Guaraçaí, comarca de ..."
# Captures the first city name after "município de"
_MUNICIPIO_PATTERN = re.compile(
    r"munic[íi]pio\s+de\s+([A-ZÀ-Ú][a-zà-ú]+(?:\s+[A-ZÀ-Úa-zà-ú]+){0,4})"
    r"(?:\s*[,/]|\s+comarca)?",
    re.IGNORECASE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _parse_brl(text: str) -> Optional[float]:
    m = re.search(r"R\$\s*([\d.,]+)", text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_city_from_description(text: str) -> Optional[str]:
    """
    Extract the true municipality from a property description.
    Descriptions often say "no município de Guaraçaí, comarca de Mirandópolis"
    where the card-level city shows the comarca (court seat), not the property location.
    Returns the first municipality name found, or None.
    """
    m = _MUNICIPIO_PATTERN.search(text)
    if not m:
        return None
    # Strip trailing stopwords that may have been captured
    city = m.group(1).strip()
    # Remove trailing "comarca", "estado", "SP", "MG" etc. if captured
    city = re.sub(r"\s+(comarca|estado|UF|[A-Z]{2})$", "", city, flags=re.IGNORECASE).strip()
    return city if len(city) >= 3 else None


def _is_rural(title: str, subcat_id: int, description: str = "") -> bool:
    """
    True for subcategories that are always rural (19/20/44/58).
    For Terreno (18), apply keyword filtering.
    """
    if subcat_id != 18:
        return True
    combined = f"{title} {description}"
    if _URBAN_REJECT.search(combined) and not _RURAL_KEEP.search(combined):
        return False
    if _RURAL_KEEP.search(combined):
        return True
    # Terreno with no keywords: skip (likely urban plot)
    return False


def _parse_card(article, subcat_id: int) -> Optional[dict]:
    """Parse an <article class='lote-main'> card into a listing dict."""
    # ── lot_id from class name ─────────────────────────────────────────────
    cls = article.get("class", [])
    lot_id = None
    for c in cls:
        m = re.match(r"bem-index-(\d+)", c)
        if m:
            lot_id = m.group(1)
            break

    # ── URL ───────────────────────────────────────────────────────────────
    link_tag = article.find("a", href=re.compile(r"/eventos/leilao/"))
    if not link_tag:
        return None
    listing_url = urljoin(BASE_URL, link_tag["href"])

    # ── Title ─────────────────────────────────────────────────────────────
    h3 = article.find("h3")
    subtitle_tag = article.find("p")  # subtitle below h3
    title = h3.get_text(strip=True) if h3 else ""
    subtitle = subtitle_tag.get_text(strip=True) if subtitle_tag else ""
    property_name = f"{title} — {subtitle}" if subtitle else title

    # ── City / State: span inside div.r2 → "Antonina - PR" ───────────────
    city, state = "", ""
    city_confirmed = False  # True when city was extracted from description text
    r2 = article.find("div", class_="r2")
    if r2:
        loc_span = r2.find("span")
        if loc_span:
            loc_text = loc_span.get_text(strip=True)  # e.g. "Antonina - PR"
            parts = [p.strip() for p in loc_text.split(" - ")]
            if len(parts) >= 2:
                city = parts[0]
                state = parts[-1]
    # Fallback: parse from slug "…-em-antonina-pr/lote/…"
    if not state:
        slug_m = re.search(r"-([a-z]{2})/lote/", listing_url)
        if slug_m:
            state = slug_m.group(1).upper()

    # ── Try to extract true city from card subtitle/full_text ─────────────
    # Card city is often the comarca; subtitle may contain "município de X"
    card_text = (title + " " + subtitle).strip()
    card_city = _extract_city_from_description(card_text)
    if card_city:
        city = card_city
        city_confirmed = True

    # ── Price ─────────────────────────────────────────────────────────────
    price_tag = article.find("strong", class_="reset-colorGrid")
    auction_price: Optional[float] = None
    if price_tag:
        auction_price = _parse_brl(price_tag.get_text())

    # ── Auction dates from div.lei-datas ──────────────────────────────────
    full_text = article.get_text(" ", strip=True)
    auction_date: Optional[str] = None
    dates_div = article.find("div", class_="lei-datas")
    if dates_div:
        # First date = 1st round, last date = latest/active round
        all_dates = re.findall(r"(\d{2}/\d{2}/\d{4})", dates_div.get_text())
        if all_dates:
            d, mo, y = all_dates[-1].split("/")   # use last (most current) date
            auction_date = f"{y}-{mo}-{d}"
    if not auction_date:
        date_m = re.search(r"(\d{2}/\d{2}/\d{4})", full_text)
        if date_m:
            d, mo, y = date_m.group(1).split("/")
            auction_date = f"{y}-{mo}-{d}"

    # ── Auction round from "1º Leilão" / "2º Leilão" label ───────────────
    active_round: Optional[int] = None
    round_m = re.search(r"(\d)[ºo°]\s*[Ll]eilão", full_text)
    if round_m:
        active_round = int(round_m.group(1))

    # ── Lot number ────────────────────────────────────────────────────────
    lot_number = ""
    item_num = article.find("div", class_="item-numeroLote")
    if item_num:
        lot_number = item_num.get_text(strip=True)

    # ── Auction type / status ─────────────────────────────────────────────
    status_tag = article.find("strong", class_="strong-status")
    auction_type = status_tag.get_text(strip=True) if status_tag else ""

    # ── Hectares from full card text ──────────────────────────────────────
    hectares, is_partial = _parse_hectares_wp(full_text, include_m2=False)
    if hectares is None:  # m² as last resort
        hectares, is_partial = _parse_hectares_wp(full_text, include_m2=True)
    if hectares is not None and hectares < 0.4:
        return None

    # ── Rural filter (for Terreno subcategory) ────────────────────────────
    if not _is_rural(property_name, subcat_id):
        return None

    return {
        "property_name": property_name,
        "state": state,
        "city": city,
        "city_confirmed": city_confirmed,
        "hectares": hectares,
        "auction_price": auction_price,
        "auction_date": auction_date,
        "listing_url": listing_url,
        "auction_type": auction_type,
        "lot_id": f"lb_{lot_id}" if lot_id else None,
        "source": "leilaobrasil.com.br",
        # Extended fields
        "lot_number": lot_number,
        "subcat_label": RURAL_SUBCATS.get(subcat_id, ""),
        "active_round": active_round,
        "is_partial": is_partial,
    }


def _scrape_subcat(
    session: requests.Session,
    subcat_id: int,
    max_pages: int,
    delay: float,
    start_page: int = 1,
) -> list[dict]:
    """Scrape all pages of one subcategory."""
    results: list[dict] = []
    page = start_page
    end_page = start_page + max_pages - 1

    while page <= end_page:
        url = f"{BUSCADOR_URL}?subcategoria={subcat_id}&page={page}"
        logger.info("leilaobrasil: GET %s", url)
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("leilaobrasil: fetch error %s — %s", url, exc)
            break

        soup = BeautifulSoup(resp.text, "lxml")
        articles = soup.find_all("article", class_=re.compile(r"lote-main"))
        if not articles:
            logger.info("leilaobrasil: no cards on page %d (subcat %d)", page, subcat_id)
            break

        for art in articles:
            listing = _parse_card(art, subcat_id)
            if listing:
                results.append(listing)

        # ── Pagination: check if next page exists ──────────────────────────
        pagination = soup.find("ul", class_="default-pagination")
        has_next = False
        if pagination:
            next_links = pagination.find_all("a", href=re.compile(rf"page={page + 1}"))
            has_next = bool(next_links)

        if not has_next:
            break

        page += 1
        time.sleep(delay)

    return results


def _date_from_obj(obj) -> str:
    """Extract YYYY-MM-DD from a leilao date object {date: '2026-06-15 10:00:00', ...}."""
    if obj and isinstance(obj, dict):
        raw = obj.get("date", "")
        if raw:
            return raw[:10]
    return ""


def _extract_round_data(lote: dict) -> dict:
    """
    Extract per-round dates and prices from the lote JSON.

    Returns dict with:
        date_round1   : YYYY-MM-DD or ""
        date_round2   : YYYY-MM-DD or ""
        price_round1  : float or None  (valorInicial — 1st praça opening bid)
        price_round2  : float or None  (valorInicial2 — 2nd praça opening bid)
        active_round  : int or None    (leilao.praca field)
        appraised_value: float or None (valorAvaliacao)
        auction_date  : YYYY-MM-DD     (the active/next round date)
    """
    leilao  = lote.get("leilao") or {}
    result  = {
        "date_round1": "", "date_round2": "",
        "price_round1": None, "price_round2": None,
        "active_round": None, "appraised_value": None,
        "auction_date": "",
    }

    # Dates: leilao.data1 and leilao.data2
    result["date_round1"] = _date_from_obj(leilao.get("data1"))
    result["date_round2"] = _date_from_obj(leilao.get("data2"))

    # Prices
    try:
        v1 = lote.get("valorInicial")
        if v1 is not None:
            result["price_round1"] = float(v1)
    except (TypeError, ValueError):
        pass
    try:
        v2 = lote.get("valorInicial2")
        if v2 is not None and float(v2) > 0:
            result["price_round2"] = float(v2)
    except (TypeError, ValueError):
        pass

    # Appraised value
    try:
        va = lote.get("valorAvaliacao")
        if va is not None and float(va) > 0:
            result["appraised_value"] = float(va)
    except (TypeError, ValueError):
        pass

    # Active round from leilao.praca
    praca = leilao.get("praca")
    if praca is not None:
        try:
            result["active_round"] = int(praca)
        except (TypeError, ValueError):
            pass

    # Determine auction_date = active round's date
    r = result["active_round"]
    if r == 2 and result["date_round2"]:
        result["auction_date"] = result["date_round2"]
    elif result["date_round1"]:
        result["auction_date"] = result["date_round1"]
    elif result["date_round2"]:
        result["auction_date"] = result["date_round2"]

    return result


def _enrich_from_detail(
    session: requests.Session,
    listings: list[dict],
    delay: float,
) -> None:
    """Fetch detail pages for listings missing hectares or auction_date, and correct city from description."""
    import json as _json
    # Fetch listings that still need data (ha, date, or city not yet confirmed from description)
    to_fetch = [l for l in listings if l.get("hectares") is None or not l.get("auction_date") or not l.get("city_confirmed")]
    if not to_fetch:
        return
    logger.info("leilaobrasil: enriching %d detail pages (city/ha/date)", len(to_fetch))

    for listing in to_fetch:
        url = listing.get("listing_url", "")
        if not url:
            continue

        # Retry with back-off — site returns 500 when hit too fast
        text = None
        for attempt in range(3):
            wait = delay * (2 ** attempt)   # 0.8s, 1.6s, 3.2s
            time.sleep(wait)
            try:
                resp = session.get(url, timeout=25)
                if resp.status_code == 500:
                    logger.debug("leilaobrasil 500 on attempt %d: %s", attempt + 1, url)
                    continue
                resp.raise_for_status()
                text = resp.text
                break
            except requests.RequestException as exc:
                logger.debug("leilaobrasil detail fetch attempt %d failed %s: %s", attempt + 1, url, exc)

        if not text:
            logger.warning("leilaobrasil detail fetch gave up after 3 attempts: %s", url)
            continue

        # ── Strategy 1: parse embedded `var lote = {...};` JSON ─────────────
        m = re.search(r'var\s+lote\s*=\s*(\{)', text)
        if m:
            try:
                lote, _ = _json.JSONDecoder().raw_decode(text, m.start(1))
                bem = lote.get("bem", {})

                # ── Round data (dates, prices, active round) ───────────────
                rd = _extract_round_data(lote)
                if rd["date_round1"]:
                    listing["date_round1"] = rd["date_round1"]
                if rd["date_round2"]:
                    listing["date_round2"] = rd["date_round2"]
                if rd["price_round1"] is not None:
                    listing["price_round1"] = rd["price_round1"]
                if rd["price_round2"] is not None:
                    listing["price_round2"] = rd["price_round2"]
                if rd["appraised_value"] is not None and not listing.get("appraised_value"):
                    listing["appraised_value"] = rd["appraised_value"]
                if rd["active_round"] is not None and not listing.get("active_round"):
                    listing["active_round"] = rd["active_round"]
                if not listing.get("auction_date") and rd["auction_date"]:
                    listing["auction_date"] = rd["auction_date"]
                    logger.debug("leilaobrasil enriched date %s → %s", url, rd["auction_date"])

                # ── Hectares ───────────────────────────────────────────────
                if listing.get("hectares") is None:
                    # 1a. areaTerreno (numeric m²)
                    area_m2 = bem.get("areaTerreno")
                    if area_m2:
                        try:
                            listing["hectares"] = round(float(area_m2) / 10_000, 4)
                            logger.debug("leilaobrasil enriched areaTerreno %s → %.4f ha", url, listing["hectares"])
                        except (TypeError, ValueError):
                            pass

                    if listing.get("hectares") is None:
                        # 1b. siteDescricao / descricao free text
                        desc = bem.get("siteDescricao") or bem.get("descricao") or ""
                        desc_plain = re.sub(r"<[^>]+>", " ", desc)
                        ha, ip = _parse_hectares_wp(desc_plain)
                        if ha:
                            listing["hectares"] = ha
                            listing["is_partial"] = ip
                            logger.debug("leilaobrasil enriched siteDescricao %s → %.4f ha", url, ha)

                # ── City from description (overrides card-level city) ──────
                # Card city is often the comarca (court seat), not the actual
                # property municipality. Description says "município de Guaraçaí".
                if not listing.get("city_confirmed"):
                    desc = bem.get("siteDescricao") or bem.get("descricao") or ""
                    desc_plain = re.sub(r"<[^>]+>", " ", desc)
                    true_city = _extract_city_from_description(desc_plain)
                    if true_city:
                        listing["city"] = true_city
                        listing["city_confirmed"] = True
                        logger.debug("leilaobrasil city corrected %s → %s", url, true_city)

            except (ValueError, KeyError) as exc:
                logger.debug("leilaobrasil JSON parse failed %s: %s", url, exc)

        # ── Strategy 2: scan full page text for area mentions ───────────────
        if listing.get("hectares") is None:
            ha, ip = _parse_hectares_wp(text)
            if ha:
                listing["hectares"] = ha
                listing["is_partial"] = ip
                logger.debug("leilaobrasil enriched full-text %s → %.4f ha", url, ha)


def scrape(max_pages: int = 5, delay: float = 1.5, start_page: int = 1) -> list[dict]:
    """
    Scrape rural lots from leilaobrasil.com.br.

    Args:
        max_pages: Number of pages per subcategory to scrape.
        delay: Seconds between requests.
        start_page: First page to fetch (1-based). Use >1 to skip already-fetched pages.

    Returns list of listing dicts matching the standard schema:
      property_name, state, city, hectares, auction_price, auction_date,
      listing_url, auction_type, lot_id, source
    """
    session = _session()
    all_listings: list[dict] = []
    seen_ids: set[str] = set()

    for subcat_id in RURAL_SUBCATS:
        try:
            batch = _scrape_subcat(session, subcat_id, max_pages, delay, start_page=start_page)
        except Exception as exc:
            logger.error("leilaobrasil: error scraping subcat %d: %s", subcat_id, exc)
            batch = []

        for item in batch:
            key = item.get("lot_id") or item.get("listing_url", "")
            if key and key not in seen_ids:
                seen_ids.add(key)
                all_listings.append(item)

        time.sleep(delay)

    # Enrich city/hectares/date from detail pages
    _enrich_from_detail(session, all_listings, delay=0.8)
    # Remove internal tracking field before returning
    for l in all_listings:
        l.pop("city_confirmed", None)

    logger.info("leilaobrasil: total %d rural lots", len(all_listings))
    return all_listings


# ── Manual test ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    listings = scrape(max_pages=3)
    print(f"\nFound {len(listings)} rural lots\n")
    for l in listings[:5]:
        print(
            f"  [{l['source']}] {l['property_name'][:60]}"
            f" | {l['city']}-{l['state']}"
            f" | R${l['auction_price']:,.0f}" if l['auction_price'] else
            f"  [{l['source']}] {l['property_name'][:60]} | {l['city']}-{l['state']} | no price"
        )
