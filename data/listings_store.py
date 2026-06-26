"""
Persistent rolling listings store.

Keeps the full data of every lot ever seen so the dashboard retains its
results across sessions and can tell, on each new search, which lots are
genuinely new and which changed price since they were last seen.

Backend
-------
Durable: a Supabase (Postgres) table `listings` (lot_id PK, jsonb data),
accessed over Supabase's REST/PostgREST API with plain `requests` — no
supabase SDK, to avoid its heavy/conflicting dependency tree. Used whenever
SUPABASE_URL + SUPABASE_KEY are configured (Streamlit secrets or env). This
survives Streamlit Cloud redeploys.

Fallback: a local JSON file ~/.fazenda_radar_listings.json — used for local
development when Supabase is not configured. (On Streamlit Cloud the local
filesystem is wiped on redeploy, which is why Supabase is preferred there.)

Store shape (in memory and in the jsonb column):
    { lot_id: { ...listing fields..., first_seen, last_seen, prev_price } }

Supabase table (run once in the SQL editor):
    create table if not exists listings (
        lot_id     text primary key,
        data       jsonb not null,
        updated_at timestamptz not null default now()
    );
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

import requests

_STORE_FILE = Path.home() / ".fazenda_radar_listings.json"
_LAST_SEARCH_FILE = Path.home() / ".fazenda_radar_last_search.json"
_TABLE = "listings"
_LAST_SEARCH_KEY = "__last_search__"   # reserved row in the listings table
_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Backend configuration
# ---------------------------------------------------------------------------

def _get_secret(name: str) -> Optional[str]:
    """Read a config value from Streamlit secrets, then environment."""
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name)


def _config() -> tuple[Optional[str], Optional[str]]:
    """Return (rest_base_url, key) or (None, None) when not configured."""
    url = _get_secret("SUPABASE_URL")
    key = _get_secret("SUPABASE_KEY")
    if not url or not key:
        return None, None
    base = url.rstrip("/") + "/rest/v1"
    return base, key


def _headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


@lru_cache(maxsize=1)
def _probe() -> tuple[bool, str]:
    """Check that the durable backend is usable. Cached once per session."""
    base, key = _config()
    if not base:
        missing = [n for n in ("SUPABASE_URL", "SUPABASE_KEY") if not _get_secret(n)]
        return False, f"credenciais ausentes: {', '.join(missing)}"
    try:
        resp = requests.get(
            f"{base}/{_TABLE}",
            headers=_headers(key),
            params={"select": "lot_id", "limit": "1"},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        return False, f"falha na conexão ({exc})"
    if resp.status_code == 200:
        return True, "ok"
    if resp.status_code in (401, 403):
        return False, "chave rejeitada (use a secret key, não a publishable)"
    if resp.status_code == 404:
        return False, "tabela 'listings' não encontrada (rode o SQL de criação)"
    return False, f"erro HTTP {resp.status_code}: {resp.text[:120]}"


def backend_name() -> str:
    """'supabase' when the durable backend is active, else 'local-file'."""
    return "supabase" if _probe()[0] else "local-file"


def backend_reason() -> str:
    """Why the durable backend is / isn't active — for diagnostics in the UI."""
    return _probe()[1]


def _price_of(listing: dict) -> Optional[float]:
    """Canonical price for change detection: active-round price, else round 1."""
    p = listing.get("auction_price")
    if p is None:
        p = listing.get("price_round1")
    return p


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_store() -> dict[str, dict]:
    """Load the listings store. Returns {} when empty, missing, or on error."""
    base, key = _config()
    if base and _probe()[0]:
        try:
            rows: list[dict] = []
            offset = 0
            page = 1000
            while True:
                resp = requests.get(
                    f"{base}/{_TABLE}",
                    headers={**_headers(key),
                             "Range-Unit": "items",
                             "Range": f"{offset}-{offset + page - 1}"},
                    params={"select": "lot_id,data"},
                    timeout=_TIMEOUT,
                )
                if resp.status_code not in (200, 206):
                    break
                batch = resp.json()
                rows.extend(batch)
                if len(batch) < page:
                    break
                offset += page
            # Skip reserved meta rows (e.g. the last-search delta).
            return {r["lot_id"]: r["data"] for r in rows
                    if r.get("data") and not str(r["lot_id"]).startswith("__")}
        except Exception:
            return {}

    # Local-file fallback (listings file never contains meta keys)
    try:
        data = json.loads(_STORE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_store(store: dict[str, dict]) -> None:
    """Persist the listings store. Silently swallows errors."""
    base, key = _config()
    if base and _probe()[0]:
        try:
            now = datetime.now(timezone.utc).isoformat()
            rows = [{"lot_id": lid, "data": entry, "updated_at": now}
                    for lid, entry in store.items()]
            headers = {**_headers(key),
                       "Prefer": "resolution=merge-duplicates,return=minimal"}
            for i in range(0, len(rows), 500):
                requests.post(
                    f"{base}/{_TABLE}",
                    headers=headers,
                    params={"on_conflict": "lot_id"},
                    data=json.dumps(rows[i:i + 500]),
                    timeout=_TIMEOUT,
                )
        except Exception:
            pass
        return

    # Local-file fallback
    try:
        _STORE_FILE.write_text(json.dumps(store, ensure_ascii=False))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Last-search delta (new lots + price changes) — persisted so the two summary
# sections survive a browser refresh, not just the in-memory session.
# ---------------------------------------------------------------------------

def save_last_search(delta: dict) -> None:
    """Persist the most recent search's new-lot / price-change summary."""
    base, key = _config()
    if base and _probe()[0]:
        try:
            now = datetime.now(timezone.utc).isoformat()
            row = [{"lot_id": _LAST_SEARCH_KEY, "data": delta, "updated_at": now}]
            headers = {**_headers(key),
                       "Prefer": "resolution=merge-duplicates,return=minimal"}
            requests.post(
                f"{base}/{_TABLE}",
                headers=headers,
                params={"on_conflict": "lot_id"},
                data=json.dumps(row),
                timeout=_TIMEOUT,
            )
        except Exception:
            pass
        return

    try:
        _LAST_SEARCH_FILE.write_text(json.dumps(delta, ensure_ascii=False))
    except Exception:
        pass


def load_last_search() -> dict:
    """Load the most recent search's summary. Returns {} when none/error."""
    base, key = _config()
    if base and _probe()[0]:
        try:
            resp = requests.get(
                f"{base}/{_TABLE}",
                headers=_headers(key),
                params={"select": "data", "lot_id": f"eq.{_LAST_SEARCH_KEY}"},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows and rows[0].get("data"):
                    return rows[0]["data"]
            return {}
        except Exception:
            return {}

    try:
        d = json.loads(_LAST_SEARCH_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_scrape(
    scraped: list[dict],
    store: dict[str, dict],
) -> tuple[list[dict], list[dict], list[dict], dict[str, dict]]:
    """
    Merge a fresh scrape into the rolling store.

    Returns (merged, new_lots, price_changes, updated_store):
      - merged        : every lot in the store after merging (the full dataset
                        to display) — lots not in this scrape are retained.
      - new_lots      : lots seen for the first time in this scrape.
      - price_changes : lots already in the store whose price differs now;
                        each carries extra keys old_price / new_price.
      - updated_store : the new store dict (caller persists it).

    A lot is keyed by lot_id. Lots without a lot_id are skipped (can't track).
    """
    today = date.today().isoformat()
    updated = dict(store)
    new_lots: list[dict] = []
    price_changes: list[dict] = []

    for lot in scraped:
        lid = lot.get("lot_id")
        if not lid:
            continue
        lid = str(lid)
        new_price = _price_of(lot)

        if lid not in updated:
            entry = {**lot, "first_seen": today, "last_seen": today,
                     "prev_price": new_price}
            updated[lid] = entry
            new_lots.append(entry)
        else:
            prev = updated[lid]
            old_price = _price_of(prev)
            entry = {
                **lot,
                "first_seen": prev.get("first_seen", today),
                "last_seen": today,
                "prev_price": old_price,
            }
            if (new_price is not None and old_price is not None
                    and new_price != old_price):
                price_changes.append({**entry,
                                      "old_price": old_price,
                                      "new_price": new_price})
            updated[lid] = entry

    merged = list(updated.values())
    return merged, new_lots, price_changes, updated
