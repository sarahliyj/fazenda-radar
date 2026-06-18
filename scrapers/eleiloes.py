"""
Scraper for e-leiloes.com.br — rural property lot listings.

Uses Playwright (headless Chromium) because the site is a Nuxt.js SPA
that renders all listing data client-side via JavaScript.

Strategy:
  1. Load the rural category page and intercept the Nuxt _payload.json.
  2. Parse the reference-graph payload to extract lot card data directly
     from the rendered page text (faster than resolving the full graph).
  3. For each lot card, extract: title, city, state, hectares, auction price,
     appraised value, auction date, lot ID, auction type, and listing URL.
  4. Handle pagination by incrementing ?page=N until no new lots appear.

Output dict keys (same schema as megaleiloes.py):
  property_name, state, city, hectares, auction_price, auction_date,
  listing_url, auction_type, lot_id, source

Extra keys specific to this source:
  appraised_value  — "Valor avaliado" (R$)
  discount_pct     — advertised discount % shown on card (e.g. 40)
  ide              — internal IDE identifier shown on the site
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from data.parse_area import parse_hectares as _parse_hectares, parse_hectares_with_partial as _parse_hectares_wp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.e-leiloes.com.br"
RURAL_URL = f"{BASE_URL}/leilao/imoveis/area-rural-fazendas-ou-sitio"
LOT_BASE = f"{BASE_URL}/lotes"

# "R$ 1.234.567,89" → float
_BRL_PATTERN = re.compile(r"R\$\s*([\d.,]+)")

# Dates: "28/04/2026", "2026-05-28 12:10:00"
_DATE_ISO_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATE_BR_PATTERN  = re.compile(r"(\d{2}/\d{2}/\d{4})")

_UF_CODES = {
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA",
    "MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN",
    "RS","RO","RR","SC","SP","SE","TO",
}

# Chácara subcategory also on e-leiloes — include it
RURAL_SUBCATEGORY_URLS = [
    f"{BASE_URL}/leilao/imoveis/area-rural-fazendas-ou-sitio",
    f"{BASE_URL}/leilao/imoveis/chacara",
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_brl(text: str) -> Optional[float]:
    m = _BRL_PATTERN.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None



def _parse_date_iso(text: str) -> Optional[str]:
    # Try ISO format first
    m = _DATE_ISO_PATTERN.search(text)
    if m:
        return m.group(1)
    # Try Brazilian format
    m = _DATE_BR_PATTERN.search(text)
    if m:
        day, month, year = m.group(1).split("/")
        return f"{year}-{month}-{day}"
    return None


def _extract_uf(text: str) -> str:
    """Pull 2-letter UF from strings like 'Piedade - SP' or 'Tremembé - SP'."""
    parts = [p.strip().upper() for p in re.split(r"[\s\-,/]+", text)]
    for part in reversed(parts):
        if part in _UF_CODES:
            return part
    return ""


def _extract_city(location: str, uf: str) -> str:
    """Return city name given a 'City - UF' or 'City/UF' string."""
    location = location.strip()
    if uf:
        # Remove the UF suffix
        cleaned = re.sub(r"[\s\-,/]+%s$" % re.escape(uf), "", location, flags=re.I).strip()
        return cleaned or location
    return location


# ─────────────────────────────────────────────────────────────────────────────
# Lot card parser — operates on the rendered page text blocks
# ─────────────────────────────────────────────────────────────────────────────

def _parse_lot_block(block: str, url: str) -> Optional[dict]:
    """
    Parse a rendered lot card text block into a listing dict.

    Expected block format (from page.inner_text on each card):
        Terreno rural, área de 3,9545 hectares - Sítio Santa Terezinha - Piedade/SP
        Piedade - SP
        Lote 12 |
        IDE 33084
        Valor avaliado: R$ 900.000,00
        40%
        Leilão Único: R$ 540.000,00
        Aberto para Lances
    """
    lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return None

    try:
        # Line 0: property name (title)
        property_name = lines[0]

        # Line 1: location "Cidade - UF"
        location_line = lines[1] if len(lines) > 1 else ""
        uf = _extract_uf(location_line)
        city = _extract_city(location_line, uf)

        # Lot number and IDE
        lot_number = ""
        ide = ""
        auction_type = ""
        for line in lines:
            if re.match(r"Lote\s+\d+", line, re.I):
                lot_number = line.split("|")[0].strip()
            if re.match(r"IDE\s+\d+", line, re.I):
                ide = re.search(r"\d+", line).group()
            if re.search(r"judicial|extrajudicial|venda\s+direta|leilão\s+único|online", line, re.I):
                if re.search(r"extrajudicial", line, re.I):
                    auction_type = "Extrajudicial"
                elif re.search(r"judicial", line, re.I):
                    auction_type = "Judicial"
                elif re.search(r"venda\s+direta", line, re.I):
                    auction_type = "Venda Direta"

        lot_id = f"IDE-{ide}" if ide else lot_number

        # Prices
        appraised_value = None
        auction_price = None
        discount_pct = None
        for line in lines:
            if re.search(r"valor\s+avaliado", line, re.I):
                appraised_value = _parse_brl(line)
            elif re.search(r"leilão\s+único|1[aª]\s*praça|2[aª]\s*praça|lance\s+mínimo", line, re.I):
                p = _parse_brl(line)
                if p and (auction_price is None or p < auction_price):
                    auction_price = p  # take the lowest praça price
            elif re.match(r"\d+%$", line):
                try:
                    discount_pct = float(line.replace("%", ""))
                except ValueError:
                    pass

        # Fallback: if no labelled price found, take the first R$ amount that isn't appraised
        if auction_price is None:
            for line in lines:
                p = _parse_brl(line)
                if p and p != appraised_value:
                    auction_price = p
                    break

        # Hectares: ha/km² first across all lines, then m² as last resort
        hectares, is_partial = _parse_hectares_wp(property_name, include_m2=False)
        if hectares is None:
            for line in lines:
                hectares, is_partial = _parse_hectares_wp(line, include_m2=False)
                if hectares:
                    break
        if hectares is None:  # m² fallback
            hectares, is_partial = _parse_hectares_wp(property_name, include_m2=True)
            if hectares is None:
                for line in lines:
                    hectares, is_partial = _parse_hectares_wp(line, include_m2=True)
                    if hectares:
                        break
        if hectares is not None and hectares < 0.4:
            return None

        # Date — look for any date in the block
        auction_date = ""
        full_text = " ".join(lines)
        auction_date = _parse_date_iso(full_text) or ""

        return {
            "property_name": property_name,
            "state": uf,
            "city": city,
            "hectares": hectares,
            "auction_price": auction_price,
            "appraised_value": appraised_value,
            "discount_pct": discount_pct,
            "auction_date": auction_date,
            "listing_url": url,
            "auction_type": auction_type,
            "lot_id": lot_id,
            "ide": ide,
            "source": "e-leiloes",
            "is_partial": is_partial,
        }

    except Exception as exc:
        logger.debug("Failed to parse lot block: %s | block: %s", exc, block[:100])
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Playwright scrape engine
# ─────────────────────────────────────────────────────────────────────────────

async def _scrape_category_async(
    category_url: str,
    max_pages: int,
    delay: float,
) -> list[dict]:
    """Scrape one category URL across all pages."""
    from playwright.async_api import async_playwright

    listings: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )
        page = await context.new_page()

        for page_num in range(1, max_pages + 1):
            url = category_url if page_num == 1 else f"{category_url}?page={page_num}"
            logger.info("e-leiloes: loading page %d — %s", page_num, url)

            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                await asyncio.sleep(delay)
            except Exception as exc:
                logger.warning("e-leiloes: page %d load failed: %s", page_num, exc)
                break

            # ── Extract lot cards from rendered DOM ──────────────────────────
            # Each lot card contains: title, location line, Lote N, IDE N,
            # Valor avaliado, discount %, price, status.
            # We grab the card elements by selecting the repeating structure.
            # Cards are identified by containing "IDE" text and a price.
            card_data = await page.evaluate("""
                () => {
                    const results = [];
                    // Find all share links — each lot has a WhatsApp share link
                    // that embeds the lot URL. This is the most reliable selector.
                    const shareLinks = document.querySelectorAll('a[href*="api.whatsapp.com"]');
                    for (const link of shareLinks) {
                        // Extract the lot URL from the WhatsApp share URL
                        const href = decodeURIComponent(link.href);
                        const lotMatch = href.match(/e-leiloes\\.com\\.br\\/lotes\\/[^\\s]+/);
                        const lotUrl = lotMatch ? 'https://www.' + lotMatch[0] : '';

                        // Walk up to the card container and grab its text
                        let el = link.parentElement;
                        for (let i = 0; i < 8; i++) {
                            if (!el) break;
                            const text = el.innerText || '';
                            // A real card has IDE and a price
                            if (text.includes('IDE') && text.includes('R$')) {
                                results.push({ text: text.trim(), url: lotUrl });
                                break;
                            }
                            el = el.parentElement;
                        }
                    }
                    return results;
                }
            """)

            if not card_data:
                logger.info("e-leiloes: no cards on page %d — stopping.", page_num)
                break

            page_listings = []
            for card in card_data:
                result = _parse_lot_block(card["text"], card["url"])
                if result:
                    page_listings.append(result)

            logger.info("e-leiloes: %d lots parsed from page %d", len(page_listings), page_num)
            listings.extend(page_listings)

            # Check for a "next page" element
            has_next = await page.evaluate("""
                () => {
                    // Look for pagination next button that is not disabled
                    const btns = document.querySelectorAll(
                        'a[aria-label*="next"], a[aria-label*="próxima"], '
                        + '.pagination a:last-child, [class*="pagination"] a:last-child'
                    );
                    for (const b of btns) {
                        if (!b.hasAttribute('disabled') && b.getAttribute('aria-disabled') !== 'true') {
                            const t = (b.textContent || '').trim();
                            if (t === '>' || t === '›' || t.toLowerCase().includes('próx')) return true;
                        }
                    }
                    // Alternatively check if a page=N+1 link exists
                    const nextPageLinks = document.querySelectorAll('a[href*="page="]');
                    for (const l of nextPageLinks) {
                        const m = l.href.match(/page=(\\d+)/);
                        if (m && parseInt(m[1]) > """ + str(page_num) + """) return true;
                    }
                    return false;
                }
            """)

            if not has_next:
                logger.info("e-leiloes: no next page after page %d.", page_num)
                break

        await browser.close()

    return listings


def scrape(max_pages: int = 10, delay: float = 2.0) -> list[dict]:
    """
    Scrape rural property lots from e-leiloes.com.br.

    Covers two rural subcategories:
      - Área rural | Fazenda ou Sítio
      - Chácara

    Args:
        max_pages: Max pages per subcategory.
        delay: Seconds to wait after each page load (be polite).

    Returns:
        Deduplicated list of listing dicts (schema matches megaleiloes.py).
    """
    all_listings: list[dict] = []
    seen_ids: set[str] = set()

    for cat_url in RURAL_SUBCATEGORY_URLS:
        logger.info("e-leiloes: scraping category %s", cat_url)
        try:
            cat_listings = asyncio.run(
                _scrape_category_async(cat_url, max_pages, delay)
            )
        except Exception as exc:
            logger.error("e-leiloes: category scrape failed for %s: %s", cat_url, exc)
            cat_listings = []

        for listing in cat_listings:
            dedup_key = listing.get("lot_id") or listing.get("listing_url") or listing.get("property_name")
            if dedup_key and dedup_key not in seen_ids:
                seen_ids.add(dedup_key)
                all_listings.append(listing)

    logger.info("e-leiloes: total unique rural lots scraped: %d", len(all_listings))
    return all_listings


# ─────────────────────────────────────────────────────────────────────────────
# CLI convenience
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = scrape(max_pages=3, delay=2.0)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n--- {len(results)} listings from e-leiloes ---")
