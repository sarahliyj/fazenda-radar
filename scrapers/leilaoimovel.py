"""
Scraper for leilaoimovel.com.br
================================
Brazil's largest real estate auction aggregator (80k+ listings).

Site structure (confirmed by live HTML inspection):
  Listing URL : /leilao-de-imoveis-tipo/terreno?pag=N
               /leilao-de-imoveis-tipo/area-rural?pag=N
  Card element: div.place-box  (20 per page)
  Pagination  : ?pag=N, last page number in pagination links href

Card HTML layout
----------------
  div.place-box
    div.image
      a.Link_Redirecter[href=/imovel/...]
      div.tag > span          ← "Data de encerramento: DD/MM/YYYY HH:MM"
    div.categories > a        ← modality tags (Financiamento, Leilão, etc.)
    a.Link_Redirecter          ← second link = same URL
      div.prices > div.price
        span.discount-price   ← auction price (current / active round bid)
        span.last-price       ← appraised value
        span.down > b         ← discount % e.g. "36%"
      div.address > p
        b                     ← property name/title
        span                  ← full street address
      div.infos > div > span  ← "1ª Praça: DD/MM/YYYY HH:MM R$ X  2ª Praça: ..."
                                  OR just "Encerra em: DD/MM/YYYY HH:MM"

Strategy
--------
  Primary  : direct requests (site returns 200 with full HTML — no Cloudflare block
             for standard browser UA; confirmed in live test).
  Fallback : Apify actor gio21/leilaoimovel-scraper if direct returns 403.

Type priority (user requirement)
---------------------------------
  1. terreno   — largest pool (5,349 listings, 282 pages), includes many rural plots
  2. area-rural — explicitly rural (1,913 listings)

Rural filter for terreno
-------------------------
  Many terreno listings are urban plots. We keep only those that pass _is_rural()
  (has rural keywords OR lacks strong urban keywords).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import requests

from data.parse_area import parse_hectares as _parse_hectares, parse_hectares_with_partial as _parse_hectares_wp
try:
    import cloudscraper as _cloudscraper_mod
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False

logger = logging.getLogger(__name__)

BASE_URL = "https://www.leilaoimovel.com.br"

# Type slugs — terreno first (priority per user requirement)
PAGE_TYPES: list[tuple[str, str]] = [
    ("terreno",    "terreno"),
    ("area-rural", "area-rural"),
]

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

# ── Apify ─────────────────────────────────────────────────────────────────────
APIFY_ACTOR   = "gio21~leilaoimovel-scraper"
APIFY_RUN_URL = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/runs"

# ── Rural keyword filter ──────────────────────────────────────────────────────
_RURAL_KEEP = re.compile(
    r"fazenda|s[íi]tio|ch[áa]cara|gleba|rural|agr[íi]cola|"
    r"eucalipto|reflorestamento|teca|soja|cana\b|pastagem|pasto|lavoura|"
    r"cerrado|mata\b|floresta|imóvel rural|imovel rural|"
    r"hectares?\b|\bha\b|alqueire",
    re.IGNORECASE,
)
_URBAN_REJECT = re.compile(
    r"\bapartamento\b|\bapto\b|edif[íi]cio|pr[ée]dio\b|sala comercial|"
    r"\bloja\b|condom[íi]nio|flat\b|studio|kitnet|sobrado urbano|"
    r"box de garagem|vaga de garagem|\bbairro\b",
    re.IGNORECASE,
)

# ── Number / price / date parsers ────────────────────────────────────────────
_DATE_DMY = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_STATE_FROM_URL = re.compile(r"/imovel/([a-z]{2})/")
_IMOVEL_ID = re.compile(r"-imovel-[^-]+-(\d+)$|imovel-(\d+)")

# Matches "1ª Praça: 25/06/2026 10:30R$ 1.869.640,00" in div.infos
_PRACA_PATTERN = re.compile(
    r"(\d)[ªo°a]\s*Pra[çc]a[:\s]+(\d{2}/\d{2}/\d{4})(?:[^R]*)(R\$\s*[\d.,]+)",
    re.IGNORECASE,
)


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
    m = re.search(r"R\$\s*([\d.,]+)", text)
    if not m:
        return None
    return _normalise_number(m.group(1))


def _parse_date_iso(text: str) -> str:
    # Strip "Novo!" prefix and similar tags before searching
    text = re.sub(r"Novo!\s*", "", text, flags=re.IGNORECASE)
    m = _DATE_DMY.search(text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


def _is_rural(title: str, page_type: str, extra_text: str = "") -> bool:
    """area-rural always passes. terreno needs rural keyword OR no urban keyword."""
    if page_type == "area-rural":
        return True
    combined = f"{title} {extra_text}"
    if _URBAN_REJECT.search(combined) and not _RURAL_KEEP.search(combined):
        return False
    if _RURAL_KEEP.search(combined):
        return True
    # No strong signal either way — keep for terreno (user wants land plots too)
    return True


# ── Card parser ───────────────────────────────────────────────────────────────

def _parse_card(box, page_type: str) -> Optional[dict]:
    """
    Parse one div.place-box into a listing dict.

    div.place-box structure:
      div.image > a.Link_Redirecter[href]   ← URL
      div.image > div.tag > span             ← closing date
      div.categories > a+                   ← modality labels
      a.Link_Redirecter (second)
        span.discount-price                  ← auction price
        span.last-price                      ← appraised value
        span.down > b                        ← discount %
        div.address > p > b                  ← property title
        div.address > p > span               ← street address
        div.infos > div > span               ← round info
    """
    # ── URL ───────────────────────────────────────────────────────────────────
    link = box.find("a", href=re.compile(r"/imovel/[a-z]{2}/"))
    if not link:
        return None
    href = link["href"]
    url = BASE_URL + href if href.startswith("/") else href

    # ── State from URL ────────────────────────────────────────────────────────
    state = ""
    m_st = _STATE_FROM_URL.search(url)
    if m_st:
        state = m_st.group(1).upper()

    # ── City from URL slug (/imovel/{uf}/{city-slug}/) ─────────────────────
    city = ""
    city_m = re.search(r"/imovel/[a-z]{2}/([a-z0-9-]+)/", url)
    if city_m:
        city = city_m.group(1).replace("-", " ").title()

    # ── Lot ID ────────────────────────────────────────────────────────────────
    # URL ends with e.g. "...-imovel-caixa-economica-federal-cef-2711494-..."
    # We need the numeric property ID embedded in the slug
    id_m = re.search(r"-imovel[^/]*?-(\d{5,})", url)
    lot_id = f"li_{id_m.group(1)}" if id_m else None

    # ── Closing / auction date from div.tag ───────────────────────────────────
    tag_div = box.find("div", class_="tag")
    closing_date = ""
    if tag_div:
        tag_span = tag_div.find("span")
        if tag_span:
            closing_date = _parse_date_iso(tag_span.get_text())

    # ── Modality categories ───────────────────────────────────────────────────
    cats_div = box.find("div", class_="categories")
    categories: list[str] = []
    if cats_div:
        categories = [a.get_text(strip=True) for a in cats_div.find_all("a")]

    # ── Prices ───────────────────────────────────────────────────────────────
    auction_price:   Optional[float] = None
    appraised_value: Optional[float] = None
    discount_pct:    Optional[float] = None

    price_span = box.find("span", class_=re.compile(r"discount-price"))
    if price_span:
        auction_price = _parse_brl(price_span.get_text())

    last_span = box.find("span", class_="last-price")
    if last_span:
        appraised_value = _parse_brl(last_span.get_text())

    down_b = box.find("b")   # first <b> inside span.down is always the discount %
    if down_b:
        pct_m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", down_b.get_text())
        if pct_m:
            try:
                discount_pct = -float(pct_m.group(1).replace(",", "."))
            except ValueError:
                pass

    # ── Title ─────────────────────────────────────────────────────────────────
    addr_div = box.find("div", class_="address")
    title = ""
    street = ""
    if addr_div:
        p = addr_div.find("p")
        if p:
            b_tag = p.find("b")
            span_tag = p.find("span")
            title  = b_tag.get_text(strip=True)  if b_tag  else ""
            street = span_tag.get_text(strip=True) if span_tag else ""

    # ── Round data from div.infos ─────────────────────────────────────────────
    # div.infos text: "1ª Praça: 25/06/2026 10:30R$ 1.869.640,002ª Praça: 02/07/2026 10:30R$ 934.820,00"
    date_round1:   str            = ""
    price_round1:  Optional[float] = None
    date_round2:   str            = ""
    price_round2:  Optional[float] = None
    active_round:  Optional[int]   = None
    total_rounds:  Optional[int]   = None

    infos_div = box.find("div", class_="infos")
    if infos_div:
        infos_text = infos_div.get_text(" ", strip=True)
        pracas = _PRACA_PATTERN.findall(infos_text)
        for (round_num, date_str, price_str) in pracas:
            n = int(round_num)
            d = _parse_date_iso(date_str)
            p = _parse_brl(price_str)
            if n == 1:
                date_round1 = d
                price_round1 = p
            elif n == 2:
                date_round2 = d
                price_round2 = p
        if pracas:
            total_rounds = len(pracas)
            # Active = first round whose close date is today or in the future.
            today_str = time.strftime("%Y-%m-%d")
            if date_round1 and date_round1 >= today_str:
                active_round = 1
            elif date_round2 and date_round2 >= today_str:
                active_round = 2
            else:
                # Both past — show the latest one that existed
                active_round = 2 if date_round2 else 1

    # If no praça breakdown, use closing date + auction price
    if not date_round1 and not date_round2:
        # Determine round from categories
        if any("2" in c and ("leilão" in c.lower() or "praça" in c.lower()) for c in categories):
            active_round  = 2
            date_round2   = closing_date
            price_round2  = auction_price
            price_round1  = appraised_value
            total_rounds  = 2
        else:
            active_round  = 1
            date_round1   = closing_date
            price_round1  = appraised_value or auction_price
            total_rounds  = 1

    # Canonical auction_date = whichever active round's date
    if active_round == 2 and date_round2:
        auction_date = date_round2
    elif date_round1:
        auction_date = date_round1
    elif date_round2:
        auction_date = date_round2
    else:
        auction_date = closing_date

    # If auction_price came from discount-price it's already the active round bid
    # Set price_round1/2 from it if not already set from infos
    if auction_price and not price_round1 and not price_round2:
        if active_round == 2:
            price_round2 = auction_price
            price_round1 = appraised_value
        else:
            price_round1 = auction_price if auction_price else appraised_value

    # ── Hectares (from title / street — will be enriched later if missing) ────
    hectares, is_partial = _parse_hectares_wp(f"{title} {street}", include_m2=False)
    if hectares is not None and hectares < 0.4:
        return None

    # ── Rural filter ──────────────────────────────────────────────────────────
    if not _is_rural(title, page_type, street):
        return None

    auction_type = ", ".join(categories) if categories else ""

    return {
        "property_name":   title or f"Imóvel leilaoimovel #{lot_id}",
        "state":           state,
        "city":            city,
        "hectares":        hectares,
        "auction_price":   auction_price,
        "auction_date":    auction_date,
        "listing_url":     url,
        "auction_type":    auction_type,
        "lot_id":          lot_id,
        "source":          "leilaoimovel.com.br",
        # Extended
        "appraised_value": appraised_value,
        "discount_to_mid_pct": discount_pct,   # pre-computed by site (re-used or overwritten by scorer)
        "active_round":    active_round,
        "total_rounds":    total_rounds,
        "date_round1":     date_round1,
        "price_round1":    price_round1,
        "date_round2":     date_round2,
        "price_round2":    price_round2,
        "is_partial":      is_partial,
    }


# ── Detail-page enrichment ────────────────────────────────────────────────────

def _enrich_from_detail(
    session: requests.Session,
    listings: list[dict],
    delay: float,
) -> None:
    """
    Fetch individual property detail pages for listings missing hectares.

    Detail page structure (confirmed by live inspection):
      div.imovel-details.row.pb-2
        div.detail  ← "Área Útil: 886,00 m²"
        div.detail  ← "Área Terreno: 20.000,00 m²"  ← we want this one

    Also extracts praça round data if present (judicial auctions):
      div.infos text: "1ª Praça: DD/MM/YYYY HH:MM R$ X  2ª Praça: ..."
    """
    from bs4 import BeautifulSoup

    to_fetch = [l for l in listings if l.get("hectares") is None and l.get("listing_url")]
    if not to_fetch:
        return

    logger.info("leilaoimovel: enriching %d detail pages for hectares/praça", len(to_fetch))

    for listing in to_fetch:
        url = listing["listing_url"]
        time.sleep(delay)
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code != 200:
                logger.debug("leilaoimovel detail %s → %d", url, resp.status_code)
                continue
        except requests.RequestException as exc:
            logger.debug("leilaoimovel detail fetch error %s: %s", url, exc)
            continue

        soup = BeautifulSoup(resp.content, "lxml")

        # ── Hectares from div.imovel-details ──────────────────────────────────
        # Priority: Área Terreno > Área Total > Área Útil (last resort)
        details_div = soup.find("div", class_=re.compile(r"imovel-details"))
        if details_div:
            detail_items = details_div.find_all("div", class_="detail")
            area_terreno = None
            area_util    = None
            for item in detail_items:
                txt = item.get_text(" ", strip=True)
                if re.search(r"[áa]rea\s+terreno|[áa]rea\s+total", txt, re.I):
                    area_terreno, _ip = _parse_hectares_wp(txt)
                    if area_terreno is None:
                        m2_m = re.search(r"([\d.,]+)\s*m[²2]", txt)
                        if m2_m:
                            v = _normalise_number(m2_m.group(1))
                            if v and v > 0:
                                area_terreno = round(v / 10_000, 4)
                                _ip = False
                elif re.search(r"[áa]rea\s+[úu]til", txt, re.I):
                    area_util, _ip = _parse_hectares_wp(txt)
                    if area_util is None:
                        m2_m = re.search(r"([\d.,]+)\s*m[²2]", txt)
                        if m2_m:
                            v = _normalise_number(m2_m.group(1))
                            if v and v > 0:
                                area_util = round(v / 10_000, 4)
                                _ip = False

            ha = area_terreno or area_util
            if ha:
                listing["hectares"] = ha
                listing["is_partial"] = _ip
                logger.debug("leilaoimovel enriched %s → %.4f ha", url, ha)

        # If still missing, try parsing from full body text
        if listing.get("hectares") is None:
            body = soup.get_text(" ", strip=True)
            ha, ip = _parse_hectares_wp(body)
            if ha:
                listing["hectares"] = ha
                listing["is_partial"] = ip
                logger.debug("leilaoimovel enriched (body) %s → %.4f ha", url, ha)

        # ── Praça data from detail page (for judicial auctions) ───────────────
        # Only enriches if listing page didn't already have round breakdown
        if not listing.get("date_round2"):
            infos_div = soup.find("div", class_="infos")
            if infos_div:
                infos_text = infos_div.get_text(" ", strip=True)
                pracas = _PRACA_PATTERN.findall(infos_text)
                if pracas:
                    for (round_num, date_str, price_str) in pracas:
                        n = int(round_num)
                        d = _parse_date_iso(date_str)
                        p = _parse_brl(price_str)
                        if n == 1:
                            listing["date_round1"]  = d
                            listing["price_round1"] = p
                        elif n == 2:
                            listing["date_round2"]  = d
                            listing["price_round2"] = p
                    listing["total_rounds"] = len(pracas)
                    # Recalculate active round and auction_date
                    today_str = time.strftime("%Y-%m-%d")
                    dr2 = listing.get("date_round2", "")
                    dr1 = listing.get("date_round1", "")
                    if dr2 and dr2 >= today_str:
                        listing["active_round"] = 2
                        listing["auction_date"] = dr2
                        listing["auction_price"] = listing.get("price_round2") or listing.get("auction_price")
                    elif dr1 and dr1 >= today_str:
                        listing["active_round"] = 1
                        listing["auction_date"] = dr1
                        listing["auction_price"] = listing.get("price_round1") or listing.get("auction_price")


# ── Direct scraper ────────────────────────────────────────────────────────────

def _get_session() -> requests.Session:
    """Return a cloudscraper session (bypasses Cloudflare JS challenges) if available,
    otherwise fall back to a plain requests.Session with browser headers."""
    if _HAS_CLOUDSCRAPER:
        s = _cloudscraper_mod.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "mobile": False}
        )
        s.headers.update(HEADERS)
        return s  # type: ignore[return-value]
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _last_page(soup) -> int:
    """Find the last page number from pagination links."""
    pag_links = soup.find_all("a", href=re.compile(r"pag=\d+"))
    max_page = 1
    for a in pag_links:
        m = re.search(r"pag=(\d+)", a["href"])
        if m:
            max_page = max(max_page, int(m.group(1)))
    return max_page


def _scrape_type(
    session: requests.Session,
    type_slug: str,
    page_type: str,
    max_pages: int,
    delay: float,
    seen_ids: set[str],
    start_page: int = 1,
) -> list[dict]:
    """Scrape pages [start_page .. start_page+max_pages-1] of one property type."""
    from bs4 import BeautifulSoup

    results: list[dict] = []
    total_pages: Optional[int] = None
    end_page = start_page + max_pages - 1

    for page in range(start_page, end_page + 1):
        url = f"{BASE_URL}/leilao-de-imoveis-tipo/{type_slug}?pag={page}"
        logger.info("leilaoimovel: GET %s", url)
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 403:
                logger.warning("leilaoimovel: 403 on %s — Cloudflare blocking", url)
                break
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("leilaoimovel: fetch error %s — %s", url, exc)
            break

        soup = BeautifulSoup(resp.content, "lxml")

        # Discover total pages on first request of this batch
        if total_pages is None:
            total_pages = _last_page(soup)
            logger.info("leilaoimovel: %s — %d total pages, fetching %d–%d",
                        type_slug, total_pages, start_page, min(end_page, total_pages))

        boxes = soup.find_all("div", class_="place-box")
        if not boxes:
            logger.info("leilaoimovel: no cards on page %d (%s) — stopping", page, type_slug)
            break

        for box in boxes:
            listing = _parse_card(box, page_type)
            if not listing:
                continue
            key = listing.get("lot_id") or listing.get("listing_url", "")
            if key and key not in seen_ids:
                seen_ids.add(key)
                results.append(listing)

        logger.info("leilaoimovel: page %d/%s — %d new cards (total so far: %d)",
                    page, str(total_pages or "?"), len(boxes), len(results))

        # Stop if we've reached the last page
        if total_pages and page >= min(end_page, total_pages):
            break

        time.sleep(delay)

    return results


# ── Apify path ────────────────────────────────────────────────────────────────

def _run_apify(api_token: str, max_items: int, timeout_secs: int = 600) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "propertyTypes": ["terreno", "area-rural"],
        "maxItems": max_items,
    }
    logger.info("leilaoimovel: starting Apify actor %s (maxItems=%d)", APIFY_ACTOR, max_items)
    try:
        resp = requests.post(
            APIFY_RUN_URL, json=payload, headers=headers,
            params={"token": api_token}, timeout=30,
        )
        if resp.status_code == 403:
            err = resp.json().get("error", {})
            approval_url = err.get("data", {}).get("approvalUrl", "https://console.apify.com")
            raise RuntimeError(
                f"Apify actor needs one-time approval.\n"
                f"Visit this URL and click Approve, then retry:\n{approval_url}"
            )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        raise RuntimeError(f"Apify run start failed ({exc.response.status_code}): {body}") from exc

    data   = resp.json().get("data", {})
    run_id = data["id"]
    ds_id  = data["defaultDatasetId"]
    logger.info("leilaoimovel: Apify run %s started", run_id)

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    deadline   = time.time() + timeout_secs
    status     = "RUNNING"
    while time.time() < deadline:
        time.sleep(8)
        r = requests.get(status_url, headers=headers,
                         params={"token": api_token}, timeout=15)
        status = r.json().get("data", {}).get("status", "UNKNOWN")
        logger.info("leilaoimovel: Apify %s → %s", run_id, status)
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    else:
        logger.warning("leilaoimovel: Apify timed out — fetching partial results")

    if status in ("FAILED", "ABORTED"):
        logger.error("leilaoimovel: Apify run %s ended with %s", run_id, status)
        return []

    items_resp = requests.get(
        f"https://api.apify.com/v2/datasets/{ds_id}/items",
        headers=headers,
        params={"token": api_token, "limit": max_items + 100, "format": "json"},
        timeout=30,
    )
    items_resp.raise_for_status()
    return items_resp.json()


def _map_apify_item(item: dict) -> Optional[dict]:
    """Map Apify actor output → standard listing dict."""
    url = item.get("url") or item.get("link") or ""
    if not url:
        return None

    title = (item.get("title") or item.get("name") or "").strip()

    state = (item.get("stateCode") or item.get("state") or "").upper().strip()
    if not state or len(state) > 2:
        m = _STATE_FROM_URL.search(url)
        if m:
            state = m.group(1).upper()

    city = (item.get("city") or item.get("municipio") or "").strip()
    if not city:
        city_m = re.search(r"/imovel/[a-z]{2}/([a-z0-9-]+)/", url)
        if city_m:
            city = city_m.group(1).replace("-", " ").title()

    # Prices
    def _coerce_price(v) -> Optional[float]:
        if v is None:
            return None
        if isinstance(v, str):
            return _parse_brl(v)
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    auction_price   = _coerce_price(item.get("price") or item.get("minimumBid"))
    appraised_value = _coerce_price(item.get("appraisalValue") or item.get("valorAvaliacao"))

    hectares = item.get("area") or item.get("hectares")
    is_partial = False
    if isinstance(hectares, str):
        hectares, is_partial = _parse_hectares_wp(hectares)
    elif hectares is not None:
        try:
            hectares = float(hectares) or None
        except (TypeError, ValueError):
            hectares = None
    if hectares is None:
        hectares, is_partial = _parse_hectares_wp(f"{title} {item.get('address', '')}")

    raw_date = item.get("closingDate") or item.get("auctionDate") or item.get("date") or ""
    auction_date = ""
    if raw_date:
        iso_m = re.match(r"(\d{4}-\d{2}-\d{2})", str(raw_date))
        auction_date = iso_m.group(1) if iso_m else _parse_date_iso(str(raw_date))

    modalities = item.get("modalities") or item.get("modalidades") or []
    auction_type = ", ".join(str(m) for m in modalities) if isinstance(modalities, list) else str(modalities)

    active_round: Optional[int] = None
    mods_str = str(modalities).lower()
    if "2" in mods_str and ("leilão" in mods_str or "praça" in mods_str):
        active_round = 2
    elif "1" in mods_str and ("leilão" in mods_str or "praça" in mods_str):
        active_round = 1

    id_m = re.search(r"-imovel[^/]*?-(\d{5,})", url)
    lot_id = f"li_{id_m.group(1)}" if id_m else None

    prop_type = (item.get("propertyType") or "").lower()
    page_type = "area-rural" if "rural" in prop_type else "terreno"
    if not _is_rural(title, page_type):
        return None

    return {
        "property_name":   title,
        "state":           state,
        "city":            city,
        "hectares":        hectares,
        "auction_price":   auction_price,
        "auction_date":    auction_date,
        "listing_url":     url,
        "auction_type":    auction_type,
        "lot_id":          lot_id,
        "source":          "leilaoimovel.com.br",
        "appraised_value": appraised_value,
        "active_round":    active_round,
        "total_rounds":    2 if active_round else None,
        "date_round1":     auction_date if active_round == 1 else "",
        "price_round1":    auction_price if active_round == 1 else appraised_value,
        "date_round2":     auction_date if active_round == 2 else "",
        "price_round2":    auction_price if active_round == 2 else None,
        "is_partial":      is_partial,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def scrape(
    max_pages: int = 5,
    delay: float = 1.5,
    api_token: Optional[str] = None,
    start_page: int = 1,
) -> list[dict]:
    """
    Scrape rural/terreno lots from leilaoimovel.com.br.

    Priority:
      1. Direct HTTP (requests) — site returns 200 with full HTML, no Cloudflare block
         Scrapes terreno first (5,349 listings, 282 pages), then area-rural
      2. Apify actor gio21/leilaoimovel-scraper — if direct fails with 403
         Requires api_token. Handles Cloudflare automatically.

    Args:
        max_pages:  Number of pages per category (terreno + area-rural, split evenly).
        start_page: First page to fetch (1-based). Use >1 to skip already-fetched pages.

    Returns standard fazenda_radar listing dicts.
    """
    try:
        from bs4 import BeautifulSoup  # noqa — just checking it's available
    except ImportError:
        logger.error("leilaoimovel: beautifulsoup4 not installed")
        return []

    session  = _get_session()
    results:  list[dict] = []
    seen_ids: set[str]   = set()
    blocked              = False

    # Split the page budget across both categories so the total result count
    # stays comparable to single-category scrapers (megaleiloes, leiloesjudiciais).
    # e.g. max_pages=5 → 3 pages terreno + 2 pages area-rural ≤ 100 cards total.
    import math
    pages_terreno    = math.ceil(max_pages / 2)
    pages_area_rural = max_pages - pages_terreno
    pages_per_type   = {"terreno": max(1, pages_terreno), "area-rural": max(1, pages_area_rural)}

    for type_slug, page_type in PAGE_TYPES:
        type_pages = pages_per_type.get(type_slug, max_pages)
        batch = _scrape_type(session, type_slug, page_type, type_pages, delay, seen_ids,
                             start_page=start_page)
        if not batch:
            # Check if first page gave a 403
            test_url = f"{BASE_URL}/leilao-de-imoveis-tipo/{type_slug}?pag=1"
            try:
                r = session.get(test_url, timeout=10)
                if r.status_code == 403:
                    blocked = True
                    logger.warning("leilaoimovel: 403 detected — will fall back to Apify")
                    break
            except Exception:
                pass
        results.extend(batch)

    # Enrich hectares (and praça detail for judicial auctions) from detail pages
    if results and not blocked:
        _enrich_from_detail(session, results, delay=1.0)

    if blocked:
        token = (api_token or os.environ.get("APIFY_TOKEN", "")).strip()
        if not token:
            logger.warning("leilaoimovel: Cloudflare blocking and no Apify token — returning 0 results")
            return []

        max_items = max_pages * 20  # budget matches single-category scrapers
        logger.info("leilaoimovel: falling back to Apify (max_items=%d)", max_items)
        try:
            raw_items = _run_apify(token, max_items=max_items)
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("leilaoimovel: Apify fallback failed: %s", exc)
            return []

        for item in raw_items:
            listing = _map_apify_item(item)
            if not listing:
                continue
            key = listing.get("lot_id") or listing.get("listing_url", "")
            if key and key not in seen_ids:
                seen_ids.add(key)
                results.append(listing)

    logger.info("leilaoimovel: total %d rural/terreno lots", len(results))
    return results


# ── Manual test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("APIFY_TOKEN", "")
    listings = scrape(max_pages=2, api_token=token or None)
    print(f"\nFound {len(listings)} rural/terreno lots\n")
    for l in listings[:10]:
        price = f"R${l['auction_price']:,.0f}" if l.get("auction_price") else "no price"
        ha    = f"{l['hectares']:.2f} ha"      if l.get("hectares")      else "? ha"
        r1    = l.get("date_round1") or "—"
        r2    = l.get("date_round2") or "—"
        print(f"  {l['property_name'][:50]:50s} | {l['city'][:15]}-{l['state']} "
              f"| {ha:10s} | {price:15s} | 1ª:{r1} 2ª:{r2}")
