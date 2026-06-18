"""
Apify enrichment — fills in missing `hectares` values for listings where our
scrapers couldn't extract the area from card text or detail pages.

Uses Apify's cheerio-scraper actor to render and extract body text from pages
that may require JavaScript or are otherwise inaccessible to plain requests.

Only called when:
  1. A valid APIFY_TOKEN is provided (sidebar input or env var).
  2. At least one listing still has hectares = None after our scrapers run.

Usage:
    from data.apify_enricher import enrich_hectares
    listings = enrich_hectares(listings, api_token="apify_api_xxxx")
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Apify actor ──────────────────────────────────────────────────────────────
# cheerio-scraper is free-tier compatible and handles server-rendered HTML
ACTOR_ID   = "apify~cheerio-scraper"
RUNS_URL   = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs"

# ── Hectares parser (same logic as scrapers) ─────────────────────────────────
_HA_PATTERN = re.compile(
    r"([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)\s*"
    r"(?:ha\b|has\b|hec?tare?s?|alqueire?s?)",
    re.IGNORECASE,
)
_M2_PATTERN = re.compile(
    r"([\d]+(?:[.,]\d+)*)\s*(?:m[²2²]|metros?\s+quadrados?)",
    re.IGNORECASE,
)
_ALQUEIRE_TO_HA = 2.42


def _normalise_number(raw: str) -> Optional[float]:
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = re.sub(r"\.(\d{3})(?!\d)", r"\1", raw)
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_hectares(text: str) -> Optional[float]:
    """Return first non-zero hectare value found in text (skips zeros and implausible values)."""
    for m in _HA_PATTERN.finditer(text):
        raw = m.group(1)
        unit = m.group(0).lower()
        val = _normalise_number(raw)
        if val is None or val <= 0:
            continue
        if "alqueire" in unit:
            val *= _ALQUEIRE_TO_HA
        return round(val, 4)
    for m2 in _M2_PATTERN.finditer(text):
        val = _normalise_number(m2.group(1))
        if val is None or val <= 0:
            continue
        return round(val / 10_000, 4)
    return None


# ── Apify actor input ─────────────────────────────────────────────────────────
# pageFunction runs in Cheerio (server-side jQuery) context.
# Returns the full visible text of the page body.
_PAGE_FUNCTION = """\
async function pageFunction(context) {
    const { $, request } = context;
    const bodyText = $('body').text().replace(/\\s+/g, ' ').trim().slice(0, 10000);
    return { url: request.url, bodyText };
}
"""


def _run_actor(urls: list[str], api_token: str, timeout_secs: int = 180) -> list[dict]:
    """Submit URLs to cheerio-scraper and return dataset items."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "startUrls": [{"url": u} for u in urls],
        "pageFunction": _PAGE_FUNCTION,
        "maxRequestRetries": 3,
        "maxConcurrency": 3,
        "maxPagesPerCrawl": len(urls) + 5,
    }

    # Start async run
    logger.info("Apify: POST %s (token prefix: %s...)", RUNS_URL, api_token[:12])
    try:
        resp = requests.post(
            RUNS_URL,
            json=payload,
            headers=headers,
            params={"token": api_token},
            timeout=30,
        )
        logger.info("Apify run start response: %d — %s", resp.status_code, resp.text[:200])
        if resp.status_code == 403:
            err = resp.json().get("error", {})
            approval_url = err.get("data", {}).get("approvalUrl", "https://console.apify.com")
            raise RuntimeError(
                f"Apify actor needs one-time permission approval.\n"
                f"Visit this URL in your browser, click Approve, then retry:\n{approval_url}"
            )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        raise RuntimeError(f"Apify run start failed ({exc.response.status_code}): {body}") from exc

    data = resp.json().get("data", {})
    run_id     = data["id"]
    dataset_id = data["defaultDatasetId"]
    logger.info("Apify run %s started (%d URLs)", run_id, len(urls))

    # Poll until finished
    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    status = "RUNNING"
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        time.sleep(6)
        r = requests.get(status_url, headers=headers, params={"token": api_token}, timeout=15)
        status = r.json().get("data", {}).get("status", "UNKNOWN")
        logger.info("Apify run %s status: %s", run_id, status)
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    else:
        logger.warning("Apify run %s timed out after %ds — fetching partial results", run_id, timeout_secs)

    if status not in ("SUCCEEDED",):
        logger.warning("Apify run %s ended with: %s", run_id, status)
        if status in ("FAILED", "ABORTED"):
            return []

    # Fetch dataset items
    items_resp = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers=headers,
        params={"token": api_token, "limit": len(urls) + 20, "format": "json"},
        timeout=30,
    )
    items_resp.raise_for_status()
    return items_resp.json()


def enrich_hectares(
    listings: list[dict],
    api_token: Optional[str] = None,
) -> list[dict]:
    """
    For every listing where hectares is None, fetch its detail page via Apify
    cheerio-scraper and extract hectares from the body text.

    Returns the same list (mutated in-place) with hectares filled where found.
    Only calls Apify if there are listings that need enrichment AND a token is set.
    """
    token = (api_token or os.environ.get("APIFY_TOKEN", "")).strip()
    if not token:
        logger.info("No Apify token — skipping Apify enrichment")
        return listings

    to_enrich = [
        l for l in listings
        if l.get("hectares") is None and l.get("listing_url")
    ]
    if not to_enrich:
        logger.info("All listings already have hectares — skipping Apify")
        return listings

    logger.info("Apify: enriching %d listings missing hectares", len(to_enrich))
    urls = [l["listing_url"] for l in to_enrich]

    try:
        items = _run_actor(urls, token)
    except RuntimeError:
        raise   # surface meaningful errors (permissions, auth) to the caller
    except Exception as exc:
        logger.error("Apify enrichment failed: %s", exc)
        return listings

    if not items:
        logger.warning("Apify returned 0 items")
        return listings

    # Map URL → body text
    url_to_text: dict[str, str] = {}
    for item in items:
        url_to_text[item.get("url", "")] = item.get("bodyText", "")

    filled = 0
    for listing in to_enrich:
        body = url_to_text.get(listing["listing_url"], "")
        if body:
            ha = _parse_hectares(body)
            if ha:
                listing["hectares"] = ha
                filled += 1
                logger.debug("Apify filled %s → %.4f ha", listing.get("property_name", ""), ha)

    logger.info("Apify enrichment: filled %d/%d missing hectares", filled, len(to_enrich))
    return listings
