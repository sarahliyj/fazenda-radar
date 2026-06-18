"""
Scraper for leilaovip.com.br — rural property (Imóvel Rural) listings.

Site structure
--------------
Search endpoint : POST /pesquisa/index?handler=pesquisar  (Razor Pages)
Filter used     : Filtro.CategoriaId  = a4f50a67-bd49-43ea-b79d-b1880160b1ce  (Imóveis)
                  Filtro.SubCategoriasId = 2afaef96-d361-4d8c-b254-b188016259ea  (Imóvel Rural)

The response is an HTML fragment containing listing cards and a hidden
CurrentPage input. Currently ~11 rural lots, all on a single page; any
call with start_page > 1 returns an empty list.

Session cookie note
-------------------
A homepage visit is required to obtain the __CBCanal session cookie before
any search POST will succeed (otherwise the server redirects to /canal).

Card HTML layout (confirmed)
-----------------------------
  div.card-anuncio
    div.crd-share > a[onclick*="evento/anuncio/..."]  ← listing URL (social share)
    a[href="/evento/anuncio/..."]                      ← listing URL (inner link)
    div.card-body > div.anc-body
      div.anc-first-row
        span.anc-local     ← "Local: CITY - UF"
        span.anc-type      ← "Imóvel Rural"
      div.anc-title > h1   ← property name
      div.anc-event  (first = active/next round)
        div.anc-row-2 > span.anc-lel   ← "1º Leilão:" / "2º Leilão:" / "Leilão Único:"
        div.anc-row-3 > span.valor-atual ← "R$ X.XXX,XX"
        div.anc-row-4 > span.anc-date   ← "DD/MM/YYYY"
      div.anc-event  (second round, optional)
        div.anc-row-5 > span.anc-lel   ← round label
                       span.valor-atual ← price
        div.anc-row-6 > span.anc-date  ← date
      div.anc-footer
        div.anc-last-row > span.anc-lote ← "Lote N"
        div.anc-left-txt > p (×2)        ← "Judicial"/"Extrajudicial", "2 Leilões"/"Leilão Único"

Round ordering note
-------------------
The site shows the *active* (upcoming) round first. For lots in the 2nd
leilão the card shows "2º Leilão" in anc-row-2 and "1º Leilão" in
anc-row-5. We always read the label to determine which praça each block is.

Extracts per listing:
  - property_name  : str
  - state          : str  (2-letter UF)
  - city           : str
  - hectares       : float | None
  - auction_price  : float | None  (active/upcoming round price)
  - auction_date   : str  (ISO YYYY-MM-DD of active round)
  - listing_url    : str
  - auction_type   : str  ("Judicial" | "Extrajudicial" | "")
  - lot_id         : str
  - source         : "leilaovip"
  - date_round1    : str  (ISO)
  - price_round1   : float | None
  - date_round2    : str  (ISO)
  - price_round2   : float | None
  - active_round   : int | None
  - total_rounds   : int | None

Usage:
    from scrapers.leilaovip import scrape
    listings = scrape()
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from data.parse_area import parse_hectares as _parse_hectares, parse_hectares_with_partial as _parse_hectares_wp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.leilaovip.com.br"

# Filter IDs for rural property category
_CATEGORIA_ID = "a4f50a67-bd49-43ea-b79d-b1880160b1ce"       # Imóveis
_SUBCATEGORIA_ID = "2afaef96-d361-4d8c-b254-b188016259ea"    # Imóvel Rural

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_DATE_PATTERN = re.compile(r"(\d{2}/\d{2}/\d{4})")
_BRL_PATTERN = re.compile(r"R\$\s*([\d.,]+)")
_UF_CODES = {
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA",
    "MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN",
    "RS","RO","RR","SC","SP","SE","TO",
}

# Label text → round number
_ROUND_LABEL_MAP = {
    "1": 1,
    "2": 2,
    "3": 3,
    "único": 1,
    "unico": 1,
}


def _parse_brl(text: str) -> Optional[float]:
    m = _BRL_PATTERN.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_date_iso(text: str) -> str:
    m = _DATE_PATTERN.search(text)
    if not m:
        return ""
    day, month, year = m.group(1).split("/")
    return f"{year}-{month}-{day}"


def _extract_state(location_text: str) -> str:
    """Extract 2-letter UF from 'Local: CITY - UF' or 'CITY - UF'."""
    parts = re.split(r"[-,\s]+", location_text.strip().upper())
    for part in reversed(parts):
        if part in _UF_CODES:
            return part
    return ""


def _extract_city(location_text: str, state: str) -> str:
    """Extract city name from 'Local: CITY - UF'."""
    text = re.sub(r"(?i)\blocal\s*[:.]?\s*", "", location_text).strip()
    if state:
        text = re.sub(rf"\s*[-–]\s*{re.escape(state)}\s*$", "", text, flags=re.IGNORECASE).strip()
    return text.title()


def _label_to_round(label_text: str) -> Optional[int]:
    """Parse '1º Leilão:', '2º Leilão:', 'Leilão Único:' → int."""
    m = re.search(r"(\d+)[ºo°]", label_text)
    if m:
        return int(m.group(1))
    lc = label_text.lower()
    if "único" in lc or "unico" in lc:
        return 1
    return None


def _parse_card(card) -> Optional[dict]:
    """Parse a single .card-anuncio element into a listing dict."""
    try:
        # --- URL: find link inside card that points to /evento/anuncio/ ---
        url = ""
        for a in card.find_all("a", href=True):
            href = a["href"]
            if "/evento/anuncio/" in href:
                url = href if href.startswith("http") else f"{BASE_URL}{href}"
                break
        if not url:
            # Fallback: extract from social share onclick
            for el in card.find_all(onclick=True):
                m = re.search(
                    r"https://www\.leilaovip\.com\.br(/evento/anuncio/[^\s'\"]+)",
                    el.get("onclick", ""),
                )
                if m:
                    url = f"{BASE_URL}{m.group(1)}"
                    break
        if not url:
            return None

        # --- Lot ID from URL slug (last numeric part) ---
        slug_m = re.search(r"-(\d+)$", url)
        lot_id = slug_m.group(1) if slug_m else ""

        # --- Lote label (e.g. "Lote 6") ---
        lote_el = card.select_one(".anc-lote")
        if lote_el:
            lote_text = lote_el.get_text(strip=True)
            lot_id = lot_id or re.sub(r"(?i)lote\s*", "", lote_text).strip()

        # --- Property name ---
        h1 = card.select_one("h1")
        property_name = h1.get_text(strip=True) if h1 else ""
        if not property_name:
            img = card.select_one("img")
            property_name = img.get("alt", "") if img else ""

        # --- Location ---
        loc_el = card.select_one(".anc-local")
        location_text = loc_el.get_text(strip=True) if loc_el else ""
        state = _extract_state(location_text)
        city = _extract_city(location_text, state) if state else ""

        # --- Hectares from title ---
        hectares, is_partial = _parse_hectares_wp(property_name, include_m2=False)
        if hectares is None:  # m² in title as last resort
            hectares, is_partial = _parse_hectares_wp(property_name, include_m2=True)

        # --- Auction type ---
        tipo_ps = card.select(".anc-left-txt p")
        auction_type = ""
        total_rounds: Optional[int] = None
        for p in tipo_ps:
            txt = p.get_text(strip=True)
            if txt.lower() in ("judicial", "extrajudicial"):
                auction_type = txt
            elif re.search(r"\d+\s+leil[aã]o", txt, re.IGNORECASE):
                m = re.search(r"(\d+)", txt)
                total_rounds = int(m.group(1)) if m else None
            elif re.search(r"leil[aã]o\s+[úu]nico", txt, re.IGNORECASE):
                total_rounds = 1

        # --- Read two anc-event blocks ---
        # Each block: anc-row-2 (label+view), anc-row-3 (price), anc-row-4 (date)
        # Second block: anc-row-5 (label+price), anc-row-6 (date)
        blocks: list[dict] = []

        # First block
        row2 = card.select_one(".anc-row-2")
        row3 = card.select_one(".anc-row-3")
        row4 = card.select_one(".anc-row-4")
        if row2:
            lbl = row2.select_one(".anc-lel")
            label_txt = lbl.get_text(strip=True) if lbl else ""
            round_num = _label_to_round(label_txt)
            price_el = row3.select_one(".valor-atual") if row3 else None
            date_el = row4.select_one(".anc-date") if row4 else None
            blocks.append({
                "round": round_num,
                "price": _parse_brl(price_el.get_text()) if price_el else None,
                "date": _parse_date_iso(date_el.get_text()) if date_el else "",
            })

        # Second block
        row5 = card.select_one(".anc-row-5")
        row6 = card.select_one(".anc-row-6")
        if row5:
            lbl5 = row5.select_one(".anc-lel")
            label_txt5 = lbl5.get_text(strip=True) if lbl5 else ""
            round_num5 = _label_to_round(label_txt5)
            price_el5 = row5.select_one(".valor-atual")
            date_el6 = row6.select_one(".anc-date") if row6 else None
            blocks.append({
                "round": round_num5,
                "price": _parse_brl(price_el5.get_text()) if price_el5 else None,
                "date": _parse_date_iso(date_el6.get_text()) if date_el6 else "",
            })

        # --- Map blocks to round 1 / round 2 ---
        date_round1 = ""
        price_round1: Optional[float] = None
        date_round2 = ""
        price_round2: Optional[float] = None

        for blk in blocks:
            rn = blk["round"]
            if rn == 1:
                date_round1 = blk["date"]
                price_round1 = blk["price"]
            elif rn == 2:
                date_round2 = blk["date"]
                price_round2 = blk["price"]

        # --- Active round: the first block shown is always the active/next one ---
        # (site orders upcoming round first)
        active_round: Optional[int] = blocks[0]["round"] if blocks else None
        auction_date = blocks[0]["date"] if blocks else ""
        auction_price = blocks[0]["price"] if blocks else None

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
            "source": "leilaovip",
            "date_round1": date_round1,
            "price_round1": price_round1,
            "date_round2": date_round2,
            "price_round2": price_round2,
            "active_round": active_round,
            "total_rounds": total_rounds,
            "is_partial": is_partial,
        }

    except Exception as exc:
        logger.debug("leilaovip: failed to parse card: %s", exc)
        return None


def _get_session() -> requests.Session:
    """Create a session with the __CBCanal cookie from the homepage."""
    session = requests.Session()
    try:
        session.get(
            BASE_URL,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        logger.warning("leilaovip: homepage visit failed (continuing anyway): %s", exc)
    return session


def _fetch_page(session: requests.Session, page: int = 1) -> Optional[BeautifulSoup]:
    """POST to the search handler and return parsed HTML fragment, or None."""
    post_url = f"{BASE_URL}/pesquisa/index?handler=pesquisar"
    post_data = {
        "Filtro.CategoriaEvento": "",
        "Filtro.CategoriaId": _CATEGORIA_ID,
        "Filtro.SubCategoriasId": _SUBCATEGORIA_ID,
        "Filtro.LocalEstadoId": "",
        "Filtro.LocalCidade": "",
        "Filtro.Texto": "",
        "Filtro.Financiavel": "false",
        "Pagina": str(page),
    }
    post_headers = dict(HEADERS)
    post_headers["Referer"] = f"{BASE_URL}/pesquisa/index"
    post_headers["X-Requested-With"] = "XMLHttpRequest"
    post_headers["Accept"] = "text/html, */*; q=0.01"

    try:
        resp = session.post(post_url, data=post_data, headers=post_headers, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("leilaovip: fetch page %d failed: %s", page, exc)
        return None


def scrape(max_pages: int = 1, delay: float = 1.0, start_page: int = 1,
           **_kwargs) -> list[dict]:
    """
    Scrape rural property listings from leilaovip.com.br.

    This is a single-page source (~11 listings pre-filtered as Imóvel Rural).
    max_pages and start_page are accepted for API compatibility; any call with
    start_page > 1 returns an empty list (already fetched all on page 1).
    """
    if start_page > 1:
        logger.info("leilaovip: start_page=%d > 1, nothing more to fetch", start_page)
        return []

    session = _get_session()
    soup = _fetch_page(session, page=1)
    if soup is None:
        return []

    cards = soup.select(".card-anuncio")
    logger.info("leilaovip: found %d cards", len(cards))

    listings = []
    for card in cards:
        result = _parse_card(card)
        if result:
            listings.append(result)

    logger.info("leilaovip: %d listings parsed", len(listings))
    return listings


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = scrape()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n--- {len(results)} listings ---")
