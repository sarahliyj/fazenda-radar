"""
Persistent rolling listings store.

Saves the full data of every lot ever seen to ~/.fazenda_radar_listings.json
so the dashboard keeps its results across browser refreshes (no re-scrape
needed) and can tell, on each new search, which lots are genuinely new and
which ones changed price since they were last seen.

Store shape:
    { lot_id: { ...listing fields..., first_seen, last_seen, prev_price } }

Note on Streamlit Cloud: this file lives in the home dir alongside the stars
file. It survives between sessions/refreshes but resets on redeploy — the same
limitation as the stars file. Acceptable for weekly use.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

_STORE_FILE = Path.home() / ".fazenda_radar_listings.json"


def _price_of(listing: dict) -> Optional[float]:
    """Canonical price used for change detection: active-round price, else round 1."""
    p = listing.get("auction_price")
    if p is None:
        p = listing.get("price_round1")
    return p


def load_store() -> dict[str, dict]:
    """Load the listings store from disk. Returns {} on missing or corrupt file."""
    try:
        data = json.loads(_STORE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_store(store: dict[str, dict]) -> None:
    """Write the listings store to disk. Silently swallows errors."""
    try:
        _STORE_FILE.write_text(json.dumps(store, ensure_ascii=False))
    except Exception:
        pass


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
