"""
Persistent rolling listings store.

Keeps the full data of every lot ever seen so the dashboard retains its
results across sessions and can tell, on each new search, which lots are
genuinely new and which changed price since they were last seen.

Backend
-------
Durable: a Supabase (Postgres) table `listings` (lot_id PK, jsonb data).
Used whenever SUPABASE_URL + SUPABASE_KEY are configured (Streamlit secrets
or environment variables). This survives Streamlit Cloud redeploys.

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

_STORE_FILE = Path.home() / ".fazenda_radar_listings.json"
_TABLE = "listings"


# ---------------------------------------------------------------------------
# Backend selection
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


@lru_cache(maxsize=1)
def _get_client():
    """Return a cached Supabase client, or None when not configured."""
    url = _get_secret("SUPABASE_URL")
    key = _get_secret("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception:
        return None


def backend_name() -> str:
    """'supabase' when the durable backend is active, else 'local-file'."""
    return "supabase" if _get_client() is not None else "local-file"


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
    client = _get_client()
    if client is not None:
        try:
            resp = client.table(_TABLE).select("lot_id, data").execute()
            return {row["lot_id"]: row["data"] for row in (resp.data or [])
                    if row.get("data")}
        except Exception:
            return {}

    # Local-file fallback
    try:
        data = json.loads(_STORE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_store(store: dict[str, dict]) -> None:
    """Persist the listings store. Silently swallows errors."""
    client = _get_client()
    if client is not None:
        try:
            now = datetime.now(timezone.utc).isoformat()
            rows = [{"lot_id": lid, "data": entry, "updated_at": now}
                    for lid, entry in store.items()]
            if rows:
                # Upsert in chunks to keep each request small.
                for i in range(0, len(rows), 500):
                    client.table(_TABLE).upsert(rows[i:i + 500]).execute()
        except Exception:
            pass
        return

    # Local-file fallback
    try:
        _STORE_FILE.write_text(json.dumps(store, ensure_ascii=False))
    except Exception:
        pass


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
