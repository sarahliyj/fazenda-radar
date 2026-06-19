"""
Scraper for grupolance.com.br — rural / land auction listings.
==============================================================

Site structure (confirmed by live HTML inspection):
  Categories:
    /imoveis/imoveis-rurais    — rural properties (fazenda, sítio, etc.)
    /imoveis/glebas            — land parcels
    /imoveis/terrenos-e-lotes  — land plots (mixed urban/rural — filtered)

  Card element : div.card.mb-4  (32 per page on terrenos, ~11 on rurais)
  Pagination   : ?pagina=N  (last page in "1-32de74itens. Página1de3" text)

Card HTML layout (confirmed)
-----------------------------
  div.card.mb-4
    div.card-image-holder > a.card-image[href]          ← full URL
    div.card-body
      a.card-title[href, title]                          ← property name (title attr preferred)
      div.card-price                                     ← auction price "R$ X.XXX,00"
      button.card-gavel-card[data-batch]                 ← lot numeric ID
      div.card-info
        div.float-left.text-uppercase > a                ← auction type (Judicial/Extrajudicial)
        a.card-locality[title]                           ← "City, UF"
      div.card-dates
        div.card-date-row  (one per praça)
          div.card-instance-label > span.badge           ← "1ª Praça" / "2ª Praça"
          ol.card-instance-date
            li[0]  ← opens date  "DD/MM/YYYY às HH:MM"
            li[1]  ← closes date "DD/MM/YYYY às HH:MM"
            li[2]  ← starting price "R$ X"

SSL note
--------
Python 3.9 on macOS uses LibreSSL which fails TLS handshake with this server.
We use subprocess.run(["curl", ...]) which uses macOS system curl (OpenSSL-based).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Optional

from bs4 import BeautifulSoup

from data.parse_area import parse_hectares as _parse_hectares, parse_hectares_with_partial as _parse_hectares_wp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.grupolance.com.br"

# Categories to scrape — rurais and glebas are always kept; terrenos filtered
PAGE_TYPES: list[tuple[str, str]] = [
    ("imoveis-rurais",   "rural"),
    ("glebas",           "rural"),
    ("terrenos-e-lotes", "terreno"),
]

_CURL_HEADERS = [
    "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "-H", "Accept-Language: pt-BR,pt;q=0.9",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
]

# ── Rural keyword filter for terrenos-e-lotes ────────────────────────────────
_RURAL_KEEP = re.compile(
    r"\bfazenda\b|\bs[íi]tio\b|\bch[áa]cara\b|\bgleba\b|\bharas\b|"
    r"\brural\b|\bagr[íi]cola\b|\bpastagem\b|\blavoura\b|\bpasto\b|"
    r"\bgado\b|\bbovino\b|\bpecuária\b|\beucalipto\b|\breflorestamento\b|"
    r"\bhectares?\b|\b\d+[.,]?\d*\s*ha\b|\balqueire",
    re.IGNORECASE,
)
_URBAN_REJECT = re.compile(
    r"\bapartamento\b|\bapto\b|\bedif[íi]cio\b|\bpr[ée]dio\b|"
    r"\bsala\s+comercial\b|\bloja\b|\bcondom[íi]nio\b|\bflat\b|"
    r"\bstudio\b|\bkitnet\b|\bgalpão\b|\bdep[oó]sito\s+de\s+garagem\b|"
    r"\bcasa\b|\bsobrado\b|\bcobertura\b",
    re.IGNORECASE,
)

# ── Number/date parsers ───────────────────────────────────────────────────────
_DATE_DMY = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_BRL      = re.compile(r"R\$\s*([\d.,]+)")
_PAGE_COUNT = re.compile(r"Página\s*(\d+)\s*de\s*(\d+)", re.IGNORECASE)
_LOT_ID_URL = re.compile(r"-(\d{5,})$")

_UF_CODES = {
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA",
    "MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN",
    "RS","RO","RR","SC","SP","SE","TO",
}


def _fetch(url: str, timeout: int = 20) -> str:
    """Fetch URL using system curl (bypasses Python 3.9 LibreSSL TLS issue)."""
    cmd = ["curl", "-s", "--max-time", str(timeout), "-L"] + _CURL_HEADERS + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("grupolance: curl failed for %s: %s", url, exc)
        return ""


def _normalise_number(raw: str) -> Optional[float]:
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = re.sub(r"\.(\d{3})(?!\d)", r"\1", raw)
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_brl(text: str) -> Optional[float]:
    m = _BRL.search(text)
    if not m:
        return None
    return _normalise_number(m.group(1))


def _parse_date_iso(text: str) -> str:
    m = _DATE_DMY.search(text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


def _is_rural_terreno(title: str) -> bool:
    """Filter terrenos-e-lotes: keep if rural keyword present, reject if hard urban."""
    if _RURAL_KEEP.search(title):
        return True
    if _URBAN_REJECT.search(title):
        return False
    # No strong signal — keep (may be a rural land plot without explicit keywords)
    return True


def _parse_card(card, page_type: str) -> Optional[dict]:
    """Parse one div.card.mb-4 into a listing dict."""
    # ── URL ───────────────────────────────────────────────────────────────────
    img_link = card.select_one("a.card-image")
    title_link = card.select_one("a.card-title")
    if not title_link and not img_link:
        return None

    href = (title_link or img_link)["href"]
    url = BASE_URL + href if href.startswith("/") else href

    # ── Property name (title attr is cleaner than text) ───────────────────────
    property_name = ""
    if title_link:
        property_name = (title_link.get("title") or title_link.get_text(strip=True))
    if not property_name and img_link:
        property_name = img_link.get("alt", "")
    property_name = property_name.strip()

    # ── Rural filter for terrenos ─────────────────────────────────────────────
    if page_type == "terreno" and not _is_rural_terreno(property_name):
        return None

    # ── Lot numeric ID from data-batch on the gavel button ───────────────────
    gavel_btn = card.select_one("button.card-gavel-card")
    batch_id = gavel_btn["data-batch"] if gavel_btn and gavel_btn.get("data-batch") else ""
    if not batch_id:
        m = _LOT_ID_URL.search(url)
        batch_id = m.group(1) if m else ""
    lot_id = f"gl_{batch_id}" if batch_id else None

    # ── Location from a.card-locality title attr: "City, UF" ─────────────────
    loc_link = card.select_one("a.card-locality")
    state = ""
    city  = ""
    if loc_link:
        loc_title = loc_link.get("title", loc_link.get_text(strip=True))
        parts = [p.strip() for p in loc_title.split(",")]
        if len(parts) >= 2:
            city  = parts[0].strip()
            state = parts[-1].strip().upper()
            if state not in _UF_CODES:
                state = ""

    # ── Auction price from div.card-price ────────────────────────────────────
    price_div = card.select_one("div.card-price")
    auction_price = _parse_brl(price_div.get_text()) if price_div else None

    # ── Auction type (Judicial / Extrajudicial / etc.) ────────────────────────
    type_div = card.select_one("div.card-info div.float-left.text-uppercase")
    auction_type = type_div.get_text(strip=True) if type_div else ""

    # ── Hectares from property name ───────────────────────────────────────────
    hectares, is_partial = _parse_hectares_wp(property_name, include_m2=False)
    if hectares is None:  # m² in title as last resort (no description available)
        hectares, is_partial = _parse_hectares_wp(property_name, include_m2=True)
    if hectares is not None and hectares < 0.4:
        return None

    # ── Round data from div.card-dates ────────────────────────────────────────
    date_round1:   str            = ""
    price_round1:  Optional[float] = None
    date_round2:   str            = ""
    price_round2:  Optional[float] = None
    active_round:  Optional[int]   = None
    total_rounds:  Optional[int]   = None
    appraised_value: Optional[float] = None

    date_rows = card.select("div.card-date-row")
    for row in date_rows:
        # Round number from badge label: "1ª Praça", "2ª Praça", or "P. Única"
        label_el = row.select_one("div.card-instance-label span.badge")
        label_text = label_el.get_text(strip=True) if label_el else ""
        round_m = re.search(r"(\d)[ªo°]", label_text)
        if round_m:
            round_num = int(round_m.group(1))
        elif re.search(r"[Úu]nica|[Úu]nico", label_text):
            round_num = 1  # "P. Única" = single praça, treat as round 1
        else:
            continue

        # Dates and price from ol.card-instance-date li items
        # li[0] = auction open date (when bidding starts)
        # li[1] = auction close date / "Encerramento" (bidding deadline — the
        #         date the site shows as "TERMINA EM"; this is the auction date)
        # li[2] = starting price (Valor inicial)
        items = row.select("ol.card-instance-date li")
        close_date  = _parse_date_iso(items[1].get_text()) if len(items) > 1 else ""
        if not close_date and len(items) > 0:
            close_date = _parse_date_iso(items[0].get_text())
        round_price = _parse_brl(items[2].get_text()) if len(items) > 2 else None

        if round_num == 1:
            date_round1  = close_date
            price_round1 = round_price
        elif round_num == 2:
            date_round2  = close_date
            price_round2 = round_price

    if date_rows:
        total_rounds = len(date_rows)
        today_str = time.strftime("%Y-%m-%d")
        # Active round = first round whose close date is today or in the future.
        # A round whose close date has passed is over; the next round is active.
        if date_round1 and date_round1 >= today_str:
            active_round = 1
        elif date_round2 and date_round2 >= today_str:
            active_round = 2
        else:
            # All rounds closed — show the latest one that existed
            active_round = 2 if date_round2 else (1 if date_round1 else None)

    # Canonical auction date = active round's close date
    if active_round == 2 and date_round2:
        auction_date = date_round2
    elif date_round1:
        auction_date = date_round1
    elif date_round2:
        auction_date = date_round2
    else:
        auction_date = ""

    # auction_price from card-price is the current active bid;
    # if no praça data, use it directly
    if not price_round1 and not price_round2:
        if active_round == 2:
            price_round2 = auction_price
        else:
            price_round1 = auction_price or appraised_value

    # site_appraised_value will be fetched from the detail page later
    site_appraised_value: Optional[float] = None

    return {
        "property_name":        property_name or f"Imóvel grupolance #{lot_id}",
        "state":                state,
        "city":                 city,
        "hectares":             hectares,
        "auction_price":        auction_price,
        "auction_date":         auction_date,
        "listing_url":          url,
        "auction_type":         auction_type,
        "lot_id":               lot_id,
        "source":               "grupolance.com.br",
        "appraised_value":      appraised_value,
        "site_appraised_value": site_appraised_value,
        "active_round":         active_round,
        "total_rounds":         total_rounds,
        "date_round1":          date_round1,
        "price_round1":         price_round1,
        "date_round2":          date_round2,
        "price_round2":         price_round2,
        "is_partial":           is_partial,
    }


def _fetch_detail(url: str) -> tuple[Optional[float], str]:
    """
    Fetch the detail page and return (appraisal_value, description_text).

    Description is the text inside <div class="text-justify"> under "Descrição
    do lote" — this is where hectare figures appear when they're not in the title.
    """
    html = _fetch(url, timeout=15)
    if not html:
        return None, ""

    # Appraisal value
    appraisal: Optional[float] = None
    m = re.search(
        r"Valor de avalia[cç][aã]o\s*<span[^>]*>\s*(R\$[\s\d.,]+)\s*</span>",
        html, re.IGNORECASE | re.DOTALL,
    )
    if m:
        appraisal = _parse_brl(m.group(1))

    # Description text: <div class="text-justify"> under "Descrição do lote"
    desc_text = ""
    desc_m = re.search(
        r'Descri[çc][aã]o do lote.*?<div[^>]*class="[^"]*text-justify[^"]*"[^>]*>(.*?)</div>',
        html, re.IGNORECASE | re.DOTALL,
    )
    if desc_m:
        raw = desc_m.group(1)
        desc_text = re.sub(r"<[^>]+>", " ", raw).strip()

    return appraisal, desc_text


def _last_page(soup: BeautifulSoup) -> int:
    """Parse "Página 1 de 3" text to get last page number."""
    for el in soup.find_all(string=_PAGE_COUNT):
        m = _PAGE_COUNT.search(el)
        if m:
            return int(m.group(2))
    return 1


def _scrape_category(
    category_slug: str,
    page_type: str,
    max_pages: int,
    delay: float,
    detail_delay: float,
    seen_ids: set[str],
    start_page: int = 1,
) -> list[dict]:
    results: list[dict] = []
    total_pages: Optional[int] = None
    end_page = start_page + max_pages - 1

    for page in range(start_page, end_page + 1):
        url = f"{BASE_URL}/imoveis/{category_slug}?pagina={page}"
        logger.info("grupolance: GET %s", url)

        html = _fetch(url)
        if not html:
            logger.warning("grupolance: empty response on page %d (%s)", page, category_slug)
            break

        soup = BeautifulSoup(html, "lxml")

        # Detect total pages on every request (so start_page > 1 still works)
        if total_pages is None:
            total_pages = _last_page(soup)
            logger.info("grupolance: %s — %d total pages, fetching %d–%d",
                        category_slug, total_pages, start_page, min(end_page, total_pages))

        cards = soup.select("div.card.mb-4")
        if not cards:
            logger.info("grupolance: no cards on page %d (%s) — stopping", page, category_slug)
            break

        for card in cards:
            listing = _parse_card(card, page_type)
            if not listing:
                continue
            key = listing.get("lot_id") or listing.get("listing_url", "")
            if key and key not in seen_ids:
                seen_ids.add(key)
                # Fetch detail page for appraisal value AND description (for hectares)
                detail_url = listing.get("listing_url", "")
                if detail_url:
                    appraisal, desc_text = _fetch_detail(detail_url)
                    listing["site_appraised_value"] = appraisal
                    if appraisal is not None:
                        listing["appraised_value"] = appraisal
                    # If hectares not found from title, try description text
                    if listing.get("hectares") is None and desc_text:
                        ha, ip = _parse_hectares_wp(desc_text, include_m2=False)
                        if ha is None:
                            ha, ip = _parse_hectares_wp(desc_text, include_m2=True)
                        if ha is not None and ha >= 0.4:
                            listing["hectares"] = ha
                            listing["is_partial"] = ip
                            logger.debug("grupolance: description ha=%.4f for %s", ha, detail_url)
                    logger.debug("grupolance: %s appraisal=%s ha=%s", detail_url, appraisal, listing.get("hectares"))
                    time.sleep(detail_delay)
                results.append(listing)

        logger.info("grupolance: page %d/%s — %d cards, %d new kept",
                    page, str(total_pages or "?"), len(cards), len(results))

        if total_pages and page >= min(end_page, total_pages):
            break

        time.sleep(delay)

    return results


def scrape(max_pages: int = 5, delay: float = 1.5, detail_delay: float = 0.8,
           start_page: int = 1, **_kwargs) -> list[dict]:
    """
    Scrape rural / land listings from grupolance.com.br.

    Categories scraped (in order):
      1. imoveis-rurais   — rural properties (always kept)
      2. glebas           — land parcels (always kept)
      3. terrenos-e-lotes — land plots (filtered by _is_rural_terreno)

    Uses system curl via subprocess to work around Python 3.9/LibreSSL TLS issue.
    Fetches each detail page to get the real "Valor de avaliação".

    Args:
        max_pages: Number of pages per category to scrape.
        start_page: First page to fetch (1-based). Use >1 to skip already-fetched pages.

    Returns standard fazenda_radar listing dicts.
    """
    listings: list[dict] = []
    seen_ids: set[str] = set()

    for category_slug, page_type in PAGE_TYPES:
        batch = _scrape_category(category_slug, page_type, max_pages, delay, detail_delay,
                                 seen_ids, start_page=start_page)
        listings.extend(batch)
        if batch:
            time.sleep(delay)

    logger.info("grupolance: total %d rural/land lots", len(listings))
    return listings


# ── CLI convenience ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = scrape(max_pages=2)
    print(json.dumps(results[:5], ensure_ascii=False, indent=2))
    print(f"\n--- {len(results)} listings ---")
    for r in results[:10]:
        ha    = f"{r['hectares']:.2f} ha" if r.get("hectares") else "? ha"
        price = f"R${r['auction_price']:,.0f}" if r.get("auction_price") else "no price"
        r1    = r.get("date_round1") or "—"
        r2    = r.get("date_round2") or "—"
        print(f"  {r['property_name'][:50]:50s} | {r['city'][:15]}-{r['state']} "
              f"| {ha:10s} | {price:15s} | 1ª:{r1} 2ª:{r2}")
