"""
Scraper for leiloesjudiciais.com.br — judicial / extrajudicial rural auction listings.
=======================================================================================

Site structure (confirmed by live HTML inspection):
  Categories:
    /imoveis/sitios           — sítios (all kept)
    /imoveis/fazendas         — fazendas (all kept)
    /imoveis/terrenos-e-lotes — terrenos/lotes (filtered: ≥1 hectare only)

  Card element : div.base-card  (42 per page)
  Pagination   : ?pagina=N  (last page in "Página X de Y" inside div.contagem_pagina)

Card HTML layout (confirmed)
-----------------------------
  div.base-card
    a.card-lote-leilao[href="/lote/{auction_id}/{lot_id}"]
      div.card-header > span              ← property title
      div.base-infos
        div.card-body
          div.cidade-estado > span        ← "City/UF"
          div.icon-label-valor × 3        ← Avaliação, Lance mínimo, Lance Atual
        div.card-footer                   ← (no useful data)

NUXT data on listing page (used for dates — avoids per-lot detail fetches)
---------------------------------------------------------------------------
  The listing page is Nuxt 3 SSR. All 42 lot objects are pre-hydrated in
  <script id="__NUXT_DATA__"> as a flat-reference JSON array.

  The top-level data dict is at nuxt_array[3]:
    "lotesData-sitios-1": idx   ← list of lot-object indices for this page
  (key format: "lotesData-{slug}-{page}")

  Each lot object (resolved via _resolve_obj) contains:
    lote_id                     → lot numeric ID
    leilao_id                   → parent auction numeric ID
    nu_ordem                    → lot's DISPLAY-ORDER position within its auction
                                  (NOT the auction round — verified by live
                                  inspection: e.g. lot 201921 has nu_ordem=3 but
                                  is actually on Ciclo 1, while lot 201602 has
                                  nu_ordem=1 but is actually on Ciclo 3)
    dt_fechamento               → "YYYY-MM-DD HH:MM:SS-03"  (ACTIVE round's close datetime)
    vl_lanceminimo              → "NNN.NN"   (active round minimum bid)
    vl_lanceinicialsegundoleilao → "NNN.NN" (2nd round starting price, >0 if 2nd round exists)

  The authoritative round number/total is NOT present on listing-page card data.
  It must be derived by fetching the auction's round schedule (a `datas` array of
  {nu_ordemrotulo, status_rotulo_nm, dt} entries — only present on the auction
  detail page /leilao/{id}) and matching the lot's `dt_fechamento` against it —
  see _apply_round_schedules() / _resolve_round_from_schedule().

SSL note
--------
Python 3.9 on macOS uses LibreSSL which may fail TLS handshake with this server.
We use subprocess.run(["curl", ...]) which uses macOS system curl (OpenSSL-based).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from bs4 import BeautifulSoup

from data.parse_area import parse_hectares as _parse_hectares, parse_hectares_with_partial as _parse_hectares_wp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.leiloesjudiciais.com.br"

# Categories to scrape — sitios and fazendas are always kept; terrenos filtered to ≥1 ha
PAGE_TYPES: list[tuple[str, str]] = [
    ("sitios",           "rural"),
    ("fazendas",         "rural"),
    ("terrenos-e-lotes", "terreno"),
]

_CURL_HEADERS = [
    "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "-H", "Accept-Language: pt-BR,pt;q=0.9",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
]

# ── Number / date parsers ─────────────────────────────────────────────────────
_DATE_DMY   = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_DATE_ISO   = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_BRL        = re.compile(r"R\$\s*([\d.,]+)")
_PAGE_COUNT = re.compile(r"Página\s*(\d+)\s*de\s*(\d+)", re.IGNORECASE)

_UF_CODES = {
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA",
    "MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN",
    "RS","RO","RR","SC","SP","SE","TO",
}

# Minimum hectares for terrenos-e-lotes
_MIN_HA_TERRENO = 1.0


def _fetch(url: str, timeout: int = 25) -> str:
    """Fetch URL using system curl (bypasses Python 3.9 LibreSSL TLS issue)."""
    cmd = ["curl", "-s", "--compressed", "--max-time", str(timeout), "-L"] + _CURL_HEADERS + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("leiloesjudiciais: curl failed for %s: %s", url, exc)
        return ""




def _normalise_number(raw: str) -> Optional[float]:
    """Convert Brazilian-formatted number string to float."""
    raw = raw.strip()
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
    """Extract YYYY-MM-DD from ISO or DD/MM/YYYY text."""
    # Try ISO first: "2026-07-20 13:00:00-03"
    m = _DATE_ISO.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Fall back to DD/MM/YYYY
    m2 = _DATE_DMY.search(text)
    if m2:
        return f"{m2.group(3)}-{m2.group(2)}-{m2.group(1)}"
    return ""


def _last_page(soup: BeautifulSoup) -> int:
    """Parse "Página 1 de N" to get last page number."""
    cp = soup.select_one("div.contagem_pagina")
    if cp:
        m = _PAGE_COUNT.search(cp.get_text())
        if m:
            return int(m.group(2))
    for el in soup.find_all(string=_PAGE_COUNT):
        m = _PAGE_COUNT.search(el)
        if m:
            return int(m.group(2))
    return 1


# ── NUXT data extraction ──────────────────────────────────────────────────────

def _resolve_val(nuxt_array: list, ref) -> object:
    """Resolve a value in the NUXT flat-reference array (one level)."""
    if not isinstance(ref, int):
        return ref
    if ref < 0 or ref >= len(nuxt_array):
        return None
    return nuxt_array[ref]


def _resolve_obj(nuxt_array: list, obj_idx: int) -> dict:
    """
    Resolve a NUXT object stored at obj_idx.
    The object is a plain dict {key: value_ref, ...} where each value is an index
    into nuxt_array. Resolve all values one level deep.
    """
    raw = nuxt_array[obj_idx] if 0 <= obj_idx < len(nuxt_array) else None
    if not isinstance(raw, dict):
        return {}
    return {k: _resolve_val(nuxt_array, v) for k, v in raw.items()}


def _parse_nuxt(html: str) -> Optional[list]:
    """Extract and parse the __NUXT_DATA__ JSON array from page HTML."""
    m = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
                  html, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_lot_nuxt_map(html: str, category_slug: str, page: int) -> dict[str, dict]:
    """
    Parse __NUXT_DATA__ from a listing page and return a dict mapping
    lot_id_str → resolved lot fields dict.

    Confirmed structure (via live inspection):
      nuxt_array[3] = {
        "buscaData-/imoveis/{slug}?pagina={page}": busca_idx,
        ...
      }
      nuxt_array[busca_idx] = {"lotes": lotes_ref, ...}
      nuxt_array[lotes_ref] = ["Reactive", lot_list_ref]
      nuxt_array[lot_list_ref] = [idx1, idx2, ...]   ← 42 lot-object indices
      nuxt_array[idx] = {"lote_id": ref, "dt_fechamento": ref,
                         "vl_lanceminimo": ref,
                         "vl_lanceinicialsegundoleilao": ref, ...}

    Each value in the lot dict is an int index into nuxt_array (resolved one level).
    """
    nuxt_array = _parse_nuxt(html)
    if not nuxt_array or len(nuxt_array) < 4:
        return {}

    top = nuxt_array[3]
    if not isinstance(top, dict):
        return {}

    # Build the expected key: "buscaData-/imoveis/{slug}?pagina={page}"
    busca_key = f"buscaData-/imoveis/{category_slug}?pagina={page}"
    if busca_key not in top:
        # Fallback: find any buscaData key
        busca_key = next((k for k in top if k.startswith("buscaData-")), None)
        if not busca_key:
            return {}

    busca_idx = top[busca_key]
    if not isinstance(busca_idx, int) or busca_idx >= len(nuxt_array):
        return {}

    busca_raw = nuxt_array[busca_idx]
    if not isinstance(busca_raw, dict):
        return {}

    # busca_raw["lotes"] → int → ["Reactive", lot_list_ref] → lot_list_ref → [idx, ...]
    lotes_ref = busca_raw.get("lotes")
    if not isinstance(lotes_ref, int) or lotes_ref >= len(nuxt_array):
        return {}

    lotes_val = nuxt_array[lotes_ref]   # ["Reactive", 228]
    if not isinstance(lotes_val, list) or len(lotes_val) < 2:
        return {}

    lot_list_ref = lotes_val[-1]        # last element is the actual list index
    if not isinstance(lot_list_ref, int) or lot_list_ref >= len(nuxt_array):
        return {}

    lot_indices = nuxt_array[lot_list_ref]   # [229, 273, 316, ...]
    if not isinstance(lot_indices, list):
        return {}

    result: dict[str, dict] = {}
    for item_idx in lot_indices:
        if not isinstance(item_idx, int):
            continue
        lot_obj = _resolve_obj(nuxt_array, item_idx)
        if not lot_obj:
            continue
        lot_id_val = lot_obj.get("lote_id")
        if not lot_id_val:
            continue

        # Resolve the `datas` list of date-entry objects (Venda Direta / 3+ rounds)
        datas_ref = lot_obj.get("datas")
        resolved_datas: list[dict] = []
        if isinstance(datas_ref, int) and 0 <= datas_ref < len(nuxt_array):
            datas_list = nuxt_array[datas_ref]
            if isinstance(datas_list, list):
                for entry_ref in datas_list:
                    entry = _resolve_obj(nuxt_array, entry_ref) if isinstance(entry_ref, int) else {}
                    if entry:
                        resolved_datas.append(entry)
        lot_obj["_datas"] = resolved_datas  # store resolved list in our dict

        result[str(lot_id_val)] = lot_obj

    return result


# ── Auction round-schedule resolution ─────────────────────────────────────────
#
# `nu_ordem` on the lot card object is NOT the active auction round — it is the
# lot's display-order position within its auction (verified by live inspection:
# e.g. lot 201921/leilão 95117 has nu_ordem=3 but is actually on the 1st "Ciclo"
# of its Venda Direta phase, while lot 201602/leilão 94945 has nu_ordem=1 but is
# actually on the 3rd "Ciclo").
#
# The authoritative source is the auction's round schedule — a list of date
# entries (each with `nu_ordemrotulo` = round number within its phase and
# `status_rotulo_nm` = phase label, e.g. "Encerramento" for 1º/2º Leilão or
# "Ciclo" for Venda Direta cycles). This schedule is embedded in the auction
# detail page (/leilao/{id}) but NOT in listing-page card data, so we fetch it
# once per unique auction (cached + fetched in parallel) and match each lot's
# `dt_fechamento` (its active round's closing datetime) against the schedule
# entries to find the correct round number.

_schedule_cache: dict[int, list[dict]] = {}
_schedule_cache_lock = threading.Lock()


def _parse_round_schedule(html: str) -> list[dict]:
    """Extract the auction round schedule from a /leilao/{id} page's NUXT data.

    Returns an ordered list of {"ordem": int, "status": str, "dt": "<raw dt str>"}.
    """
    arr = _parse_nuxt(html)
    if not arr:
        return []
    for i, obj in enumerate(arr):
        if not (isinstance(obj, dict) and "datas" in obj and "nm_statusleilao" in obj):
            continue
        # `datas` is stored as an int reference to the actual index list
        # (one level of indirection — same pattern as `lotes` in _extract_lot_nuxt_map)
        datas_ref = obj.get("datas")
        datas_list = _resolve_val(arr, datas_ref) if isinstance(datas_ref, int) else datas_ref
        if not isinstance(datas_list, list):
            continue
        schedule: list[dict] = []
        for entry_ref in datas_list:
            entry = _resolve_obj(arr, entry_ref) if isinstance(entry_ref, int) else {}
            ordem_val = entry.get("nu_ordemrotulo")
            status_val = entry.get("status_rotulo_nm") or ""
            dt_val = entry.get("dt")
            if isinstance(ordem_val, (int, float)) and isinstance(dt_val, str) and dt_val:
                schedule.append({"ordem": int(ordem_val), "status": str(status_val), "dt": dt_val})
        if schedule:
            return schedule
    return []


def _fetch_auction_schedule(leilao_id: int) -> list[dict]:
    """Fetch (with caching) the round schedule for one auction by its leilão ID."""
    with _schedule_cache_lock:
        cached = _schedule_cache.get(leilao_id)
        if cached is not None:
            return cached
    html = _fetch(f"{BASE_URL}/leilao/{leilao_id}")
    schedule = _parse_round_schedule(html) if html else []
    with _schedule_cache_lock:
        _schedule_cache[leilao_id] = schedule
    return schedule


def _resolve_round_from_schedule(
    dt_fechamento_raw: str,
    schedule: list[dict],
) -> tuple[Optional[int], Optional[int]]:
    """Match a lot's closing datetime against its auction's round schedule.

    Returns (active_round, total_rounds): active_round is the `nu_ordemrotulo`
    of the schedule entry whose date matches the lot's `dt_fechamento`, and
    total_rounds is the number of entries sharing that entry's phase label
    (e.g. all "Ciclo" entries, or all "Encerramento" entries).
    Returns (None, None) when no exact match is found (caller should fall back).
    """
    if not schedule or not isinstance(dt_fechamento_raw, str):
        return None, None
    target = dt_fechamento_raw.strip()
    if not target:
        return None, None
    for entry in schedule:
        if entry["dt"].strip() == target:
            status = entry["status"]
            siblings = [e for e in schedule if e["status"] == status]
            return entry["ordem"], len(siblings)
    return None, None


def _apply_round_schedules(
    listings: list[dict],
    nuxt_lots: list[Optional[dict]],
) -> None:
    """Override active_round/total_rounds/dates using fetched auction schedules.

    `nuxt_lots[i]` must correspond to `listings[i]`. Mutates `listings` in place.
    Lots whose schedule can't be resolved (or whose dt_fechamento doesn't match
    any schedule entry) keep the heuristic values computed by `_parse_card`.
    """
    leilao_ids: set[int] = set()
    for nuxt_lot in nuxt_lots:
        if not nuxt_lot:
            continue
        lid = nuxt_lot.get("leilao_id")
        if isinstance(lid, (int, float)) and lid:
            leilao_ids.add(int(lid))
        elif isinstance(lid, str) and lid.isdigit():
            leilao_ids.add(int(lid))

    # Drop ones we already have cached so we only spin up threads for new ones
    with _schedule_cache_lock:
        to_fetch = [lid for lid in leilao_ids if lid not in _schedule_cache]

    if to_fetch:
        with ThreadPoolExecutor(max_workers=min(8, len(to_fetch))) as executor:
            futures = {executor.submit(_fetch_auction_schedule, lid): lid for lid in to_fetch}
            for future in as_completed(futures):
                lid = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.debug("leiloesjudiciais: schedule fetch failed for leilão %s: %s", lid, exc)
                    with _schedule_cache_lock:
                        _schedule_cache.setdefault(lid, [])

    for listing, nuxt_lot in zip(listings, nuxt_lots):
        if not nuxt_lot:
            continue
        lid_raw = nuxt_lot.get("leilao_id")
        try:
            lid = int(lid_raw) if lid_raw is not None else None
        except (ValueError, TypeError):
            lid = None
        if lid is None:
            continue

        with _schedule_cache_lock:
            schedule = _schedule_cache.get(lid) or []

        dt_raw = nuxt_lot.get("dt_fechamento")
        active, total = _resolve_round_from_schedule(dt_raw, schedule)
        if active is None:
            continue   # keep heuristic values from _parse_card

        matched_date = _parse_date_iso(dt_raw) if isinstance(dt_raw, str) else ""
        listing["active_round"] = active
        listing["total_rounds"] = total
        listing["auction_date"] = matched_date or listing.get("auction_date", "")
        if active == 1:
            listing["date_round1"] = matched_date
            listing["date_round2"] = ""
        elif active == 2:
            listing["date_round1"] = ""
            listing["date_round2"] = matched_date
        else:
            listing["date_round1"] = matched_date
            listing["date_round2"] = ""


# ── Card parsing ──────────────────────────────────────────────────────────────

# Lots manually excluded from results (and all future fetches) — e.g. listings
# with known unresolvable data-quality issues (round/praça can't be determined
# correctly because the auction's schedule data doesn't match the lot's own
# closing time). Keyed by the lot's numeric ID (the second path segment in
# /lote/{auction_id}/{lot_id}).
_EXCLUDED_LOT_IDS: set[str] = {
    "199247",  # leilão 94088 — active_round resolves to nonsensical "10" (nu_ordem
               # display-position misread as round number; auction schedule has no
               # matching dt_fechamento entry to override it correctly)
}


def _parse_card(
    card,
    page_type: str,
    nuxt_lot: Optional[dict] = None,
) -> Optional[dict]:
    """Parse one div.base-card into a listing dict."""
    link = card.select_one("a.card-lote-leilao")
    if not link:
        return None

    href = link.get("href", "")
    if not href:
        return None

    # Parse lot_id and auction_id from href: /lote/{auction_id}/{lot_id}
    href_m = re.match(r"/lote/(\d+)/(\d+)", href)
    if not href_m:
        return None
    auction_id_str = href_m.group(1)
    lot_id_str     = href_m.group(2)
    if lot_id_str in _EXCLUDED_LOT_IDS:
        return None
    lot_id         = f"lj_{lot_id_str}"
    url            = BASE_URL + href

    # ── Title ─────────────────────────────────────────────────────────────────
    header = card.select_one("div.card-header span")
    property_name = header.get_text(strip=True) if header else ""
    if not property_name:
        img = card.select_one("img.imagem__lote")
        property_name = img.get("alt", "").strip() if img else ""
    if not property_name:
        property_name = f"Imóvel leiloesjudiciais #{lot_id_str}"

    # ── Hectares: title → NUXT nm_titulo_lote → NUXT nm_descricao ──────────
    hectares, is_partial = _parse_hectares_wp(property_name, include_m2=False)
    if hectares is None and nuxt_lot:
        titulo_raw = nuxt_lot.get("nm_titulo_lote") or ""
        if isinstance(titulo_raw, str) and titulo_raw and titulo_raw != property_name:
            hectares, is_partial = _parse_hectares_wp(titulo_raw, include_m2=False)
    if hectares is None and nuxt_lot:
        desc_raw = nuxt_lot.get("nm_descricao") or ""
        if isinstance(desc_raw, str) and desc_raw:
            desc_text = re.sub(r"<[^>]+>", " ", desc_raw)
            hectares, is_partial = _parse_hectares_wp(desc_text)
    if hectares is None:  # m² fallback after all text sources exhausted
        hectares, is_partial = _parse_hectares_wp(property_name, include_m2=True)

    # ── Hectare filters ───────────────────────────────────────────────────────
    if hectares is not None and hectares <= 0:
        return None
    if page_type == "terreno":
        if hectares is None or hectares < _MIN_HA_TERRENO:
            return None

    # ── Location from div.cidade-estado > span: "City/UF" ────────────────────
    loc_el = card.select_one("div.cidade-estado span")
    state = ""
    city  = ""
    if loc_el:
        loc_text = loc_el.get_text(strip=True)
        if "/" in loc_text:
            parts = loc_text.rsplit("/", 1)
            city  = parts[0].strip()
            state = parts[1].strip().upper()
            if state not in _UF_CODES:
                state = ""
        else:
            city = loc_text

    # ── Prices from div.icon-label-valor × 3 ─────────────────────────────────
    price_divs = card.select("div.icon-label-valor")
    avaliacao    : Optional[float] = None
    lance_minimo : Optional[float] = None
    lance_atual  : Optional[float] = None

    for pdiv in price_divs:
        spans = pdiv.select("div.label-valor span")
        if len(spans) < 2:
            continue
        label = spans[0].get_text(strip=True).lower()
        value_text = spans[1].get_text(strip=True)
        val = _parse_brl(value_text)
        if "avalia" in label:
            avaliacao = val
        elif "m\u00ednimo" in label or "minimo" in label:
            lance_minimo = val
        elif "atual" in label:
            lance_atual = val

    # auction_price = current bid (or minimum if no current bid)
    auction_price = lance_atual if (lance_atual and lance_atual > 0) else lance_minimo

    # ── Round prices and dates from NUXT data ────────────────────────────────
    # nu_ordem = active round number (authoritative — 1, 2, 3, …)
    # dt_fechamento = close date for the active round
    # vl_lanceminimo = minimum bid for the active round
    # vl_lanceinicialsegundoleilao = 2nd round opening price (>0 when 2nd round exists)
    # _datas = resolved list of per-round date entries (Venda Direta / 3+ rounds)
    price_round1: Optional[float] = None
    price_round2: Optional[float] = None
    auction_date: str = ""
    active_round: Optional[int] = None
    total_rounds: Optional[int] = None
    date_round1: str = ""
    date_round2: str = ""

    if nuxt_lot:
        vl1_raw   = nuxt_lot.get("vl_lanceminimo")
        vl2_raw   = nuxt_lot.get("vl_lanceinicialsegundoleilao")
        dt_raw    = nuxt_lot.get("dt_fechamento")
        nu_ordem  = nuxt_lot.get("nu_ordem")

        if isinstance(vl1_raw, str):
            v1 = _normalise_number(vl1_raw)
            if v1 and v1 > 0:
                price_round1 = v1

        if isinstance(vl2_raw, str):
            v2 = _normalise_number(vl2_raw)
            if v2 and v2 > 0:
                price_round2 = v2

        if isinstance(dt_raw, str):
            auction_date = _parse_date_iso(dt_raw)

        # Use nu_ordem as authoritative active round number
        if isinstance(nu_ordem, (int, float)) and nu_ordem > 0:
            active_round = int(nu_ordem)
        elif isinstance(nu_ordem, str):
            try:
                v = int(nu_ordem)
                if v > 0:
                    active_round = v
            except ValueError:
                pass

        # Build per-round date/price mapping from _datas if available
        resolved_datas: list[dict] = nuxt_lot.get("_datas") or []
        if resolved_datas:
            # Each entry has nu_ordemrotulo (round label number) and dt (datetime str)
            # status_rotulo_nm can be "Encerramento" (regular round) or "Ciclo" (Venda Direta)
            round_dates: dict[int, str] = {}
            for entry in resolved_datas:
                ordem_val = entry.get("nu_ordemrotulo")
                dt_entry  = entry.get("dt") or entry.get("dt_fechamento") or ""
                if isinstance(ordem_val, (int, float)) and isinstance(dt_entry, str) and dt_entry:
                    round_dates[int(ordem_val)] = _parse_date_iso(dt_entry)

            if round_dates:
                # Total rounds = highest round label in datas
                total_rounds = max(round_dates.keys())
                # Assign dates to round slots we expose
                date_round1 = round_dates.get(1, "")
                date_round2 = round_dates.get(2, "")
                # auction_date = date for the active round (fallback to dt_fechamento)
                if active_round and active_round in round_dates:
                    auction_date = round_dates[active_round]
            # If no round_dates resolved, fall through to simple logic below

    # Fall back to card prices if NUXT didn't give us anything
    if not price_round1:
        price_round1 = lance_minimo

    # ── Simple 1-or-2 round assignment when datas didn't give us dates ────────
    if not date_round1 and not date_round2:
        if active_round is None:
            # No NUXT data at all — infer from prices (legacy fallback)
            if price_round2 and price_round1 and price_round2 < price_round1:
                active_round = 2
                total_rounds = 2
            elif price_round2 and price_round1:
                active_round = 1
                total_rounds = 2
            else:
                active_round = 1 if auction_date else None
                total_rounds = 1 if auction_date else None

        if active_round == 1:
            date_round1 = auction_date
            date_round2 = ""
            if total_rounds is None:
                total_rounds = 2 if price_round2 else 1
        elif active_round == 2:
            date_round1 = ""
            date_round2 = auction_date
            if total_rounds is None:
                total_rounds = 2
        else:
            # Round 3+ with only dt_fechamento available — store in date_round1 as fallback
            date_round1 = auction_date

    # site_appraised_value = "Avaliação" shown on the card
    site_appraised_value: Optional[float] = avaliacao

    return {
        "property_name":        property_name,
        "state":                state,
        "city":                 city,
        "hectares":             hectares,
        "auction_price":        auction_price,
        "auction_date":         auction_date,
        "listing_url":          url,
        "auction_type":         "Judicial",
        "lot_id":               lot_id,
        "source":               "leiloesjudiciais.com.br",
        "appraised_value":      avaliacao,
        "site_appraised_value": site_appraised_value,
        "active_round":         active_round,
        "total_rounds":         total_rounds,
        "date_round1":          date_round1,
        "price_round1":         price_round1,
        "date_round2":          date_round2,
        "price_round2":         price_round2,
        "is_partial":           is_partial,
    }


# ── Category scraping ─────────────────────────────────────────────────────────

def _scrape_category(
    category_slug: str,
    page_type: str,
    max_pages: int,
    delay: float,
    seen_ids: set[str],
    start_page: int = 1,
) -> tuple[list[dict], list[Optional[dict]]]:
    """Returns (listings, nuxt_lots) — nuxt_lots[i] is the raw NUXT data dict
    used to build listings[i] (needed afterwards for round-schedule resolution)."""
    results: list[dict] = []
    nuxt_lots: list[Optional[dict]] = []
    total_pages: Optional[int] = None
    end_page = start_page + max_pages - 1

    for page in range(start_page, end_page + 1):
        url = f"{BASE_URL}/imoveis/{category_slug}?pagina={page}"
        logger.info("leiloesjudiciais: GET %s", url)

        html = _fetch(url)
        if not html:
            logger.warning("leiloesjudiciais: empty response on page %d (%s)", page, category_slug)
            break

        soup = BeautifulSoup(html, "lxml")

        # Detect total pages on every request (so start_page > 1 still works)
        if total_pages is None:
            total_pages = _last_page(soup)
            logger.info("leiloesjudiciais: %s — %d total pages, fetching %d–%d",
                        category_slug, total_pages, start_page, min(end_page, total_pages))

        cards = soup.select("div.base-card")
        if not cards:
            logger.info("leiloesjudiciais: no cards on page %d (%s) — stopping",
                        page, category_slug)
            break

        # Extract lot data map from NUXT (lot_id_str → lot fields with dates/prices)
        nuxt_map = _extract_lot_nuxt_map(html, category_slug, page)
        logger.debug("leiloesjudiciais: NUXT map has %d lot entries", len(nuxt_map))

        page_new = 0
        for card in cards:
            # Get lot_id_str from card href to look up NUXT data
            link = card.select_one("a.card-lote-leilao")
            nuxt_lot: Optional[dict] = None
            if link:
                href_m = re.match(r"/lote/(\d+)/(\d+)", link.get("href", ""))
                if href_m:
                    nuxt_lot = nuxt_map.get(href_m.group(2))

            listing = _parse_card(card, page_type, nuxt_lot=nuxt_lot)
            if not listing:
                continue
            key = listing.get("lot_id") or listing.get("listing_url", "")
            if key and key not in seen_ids:
                seen_ids.add(key)
                results.append(listing)
                nuxt_lots.append(nuxt_lot)
                page_new += 1

        logger.info("leiloesjudiciais: page %d/%s — %d cards, %d new kept",
                    page, str(total_pages or "?"), len(cards), page_new)

        if total_pages and page >= min(end_page, total_pages):
            break

        time.sleep(delay)

    return results, nuxt_lots


def scrape(
    max_pages: int = 5,
    delay: float = 1.5,
    start_page: int = 1,
    max_results: Optional[int] = None,
    **_kwargs,  # accept (and ignore) legacy detail_delay / fetch_detail kwargs
) -> list[dict]:
    """
    Scrape rural / land auction listings from leiloesjudiciais.com.br.

    Categories scraped (in order):
      1. sitios           — sítios (all kept)
      2. fazendas         — fazendas (all kept)
      3. terrenos-e-lotes — terrenos/lotes filtered to ≥1 hectare

    Uses system curl via subprocess to work around Python 3.9/LibreSSL TLS issue.
    Extracts dates and prices from listing-page NUXT data (no per-lot detail fetches),
    then resolves the *active auction round* authoritatively by fetching each
    unique auction's round schedule (cached + parallel — see _apply_round_schedules).

    Args:
      max_pages:   Number of listing pages per category to scrape.
      delay:       Seconds between listing page requests.
      start_page:  First page to fetch (1-based). Use >1 to skip already-fetched pages.
      max_results: If given, trims the combined results to this many lots BEFORE
                   round-schedule resolution — avoids wasting auction-detail
                   fetches on lots the caller will discard anyway.

    Returns standard fazenda_radar listing dicts.
    """
    listings: list[dict] = []
    nuxt_lots: list[Optional[dict]] = []
    seen_ids: set[str] = set()

    for category_slug, page_type in PAGE_TYPES:
        batch, batch_nuxt = _scrape_category(category_slug, page_type, max_pages, delay, seen_ids,
                                              start_page=start_page)
        listings.extend(batch)
        nuxt_lots.extend(batch_nuxt)
        if batch:
            time.sleep(delay)

    if max_results is not None and len(listings) > max_results:
        listings = listings[:max_results]
        nuxt_lots = nuxt_lots[:max_results]

    # Authoritative round resolution — overrides the heuristic active_round/
    # total_rounds/dates from _parse_card using each auction's actual schedule.
    try:
        _apply_round_schedules(listings, nuxt_lots)
    except Exception as exc:
        logger.warning("leiloesjudiciais: round-schedule resolution failed: %s", exc)

    logger.info("leiloesjudiciais: total %d rural/land lots", len(listings))
    return listings


# ── CLI convenience ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = scrape(max_pages=1, delay=1.0)
    print(_json.dumps(results[:3], ensure_ascii=False, indent=2))
    print(f"\n--- {len(results)} listings ---")
    for r in results[:10]:
        ha    = f"{r['hectares']:.2f} ha" if r.get("hectares") else "? ha"
        price = f"R${r['auction_price']:,.0f}" if r.get("auction_price") else "no price"
        appr  = f"R${r['site_appraised_value']:,.0f}" if r.get('site_appraised_value') else "—"
        r1    = r.get("date_round1") or "—"
        r2    = r.get("date_round2") or "—"
        print(f"  {r['property_name'][:50]:50s} | {r['city'][:15]}-{r['state']} "
              f"| {ha:10s} | {price:15s} | appr:{appr} | 1ª:{r1} 2ª:{r2}")
