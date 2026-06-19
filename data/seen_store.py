"""
Persistent "seen listings" store.

Saves a dict of {lot_id: first_seen_date} to ~/.fazenda_radar_seen.json so
the dashboard can flag listings that are genuinely new since the last saved
baseline.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

_SEEN_FILE = Path.home() / ".fazenda_radar_seen.json"


def load_seen() -> dict[str, str]:
    """Load the seen store from disk. Returns {} on missing or corrupt file."""
    try:
        return json.loads(_SEEN_FILE.read_text())
    except Exception:
        return {}


def save_seen(seen: dict[str, str]) -> None:
    """Write the seen store to disk. Silently swallows errors."""
    try:
        _SEEN_FILE.write_text(json.dumps(seen))
    except Exception:
        pass


def mark_new(
    listings: list[dict],
    seen: dict[str, str],
) -> tuple[list[dict], dict[str, str]]:
    """
    Add is_new: bool to each listing and return updated seen dict.

    A listing is new if its lot_id is not already in seen.
    New lot_ids are added to the returned seen dict with today's date —
    the caller should update st.session_state.seen_store but NOT auto-save
    to disk (saving is the user's explicit action via "Salvar como referência").
    """
    today = date.today().isoformat()
    updated = dict(seen)
    out = []
    for listing in listings:
        lid = listing.get("lot_id") or ""
        is_new = bool(lid) and lid not in seen
        if is_new and lid:
            updated[lid] = today
        out.append({**listing, "is_new": is_new})
    return out, updated
