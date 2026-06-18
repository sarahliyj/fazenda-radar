"""
Opportunity score calculator for distressed land auction listings.

Score methodology (0–100):

  Component 1 — Discount to Market (weight 60%)
    Measures how far below mid-market value the auction price is.
    discount_ratio = (market_mid - auction_price) / market_mid
    Points: 0–60 (linear, capped at 60% discount → 60 pts)

  Component 2 — Price Certainty (weight 20%)
    Penalises listings with unknown hectares or price (can't calculate true
    discount). Full 20 pts when both are known and plausible.

  Component 3 — Auction Urgency (weight 10%)
    Higher score for auctions happening in the next 0–30 days.
    Scores 0 if date unknown or > 90 days away.

  Component 4 — Benchmark Confidence (weight 10%)
    Higher when the state + land_type combination has a specific entry in
    BENCHMARKS (not a _DEFAULT fallback).

Final score = sum of components, clipped to [0, 100].

Grade mapping:
  80–100 : A  (Excellent)
  60–79  : B  (Good)
  40–59  : C  (Fair)
  20–39  : D  (Weak)
   0–19  : F  (Poor / insufficient data)
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Optional

from data.benchmarks import (
    BENCHMARKS,
    detect_land_type,
    get_benchmark,
    market_value_estimate,
)
from data.sp_reference import get_sp_reference


def _discount_score(auction_price: float, market_mid: float) -> float:
    """Return 0–60 pts based on discount to mid-market."""
    if market_mid <= 0:
        return 0.0
    discount_ratio = (market_mid - auction_price) / market_mid
    # Linear: 0% discount → 0 pts; 60%+ discount → 60 pts
    return min(60.0, max(0.0, discount_ratio * 100))


def _urgency_score(auction_date_str: str) -> float:
    """Return 0–10 pts. Peak at 0–30 days; 0 at 90+ days or unknown."""
    if not auction_date_str:
        return 0.0
    try:
        auction_dt = datetime.fromisoformat(auction_date_str).date()
    except (ValueError, TypeError):
        return 0.0
    days_away = (auction_dt - date.today()).days
    if days_away < 0:
        return 3.0   # already past — still scored (2nd praça may be pending)
    if days_away <= 30:
        return 10.0
    if days_away <= 60:
        return 7.0
    if days_away <= 90:
        return 4.0
    return 1.0


def _certainty_score(hectares: Optional[float], auction_price: Optional[float]) -> float:
    """Return 0–20 pts based on data completeness."""
    pts = 0.0
    if auction_price is not None and auction_price > 0:
        pts += 10.0
    if hectares is not None and hectares > 0:
        pts += 10.0
    return pts


def _benchmark_confidence_score(state: str, land_type: str) -> float:
    """Return 0–10 pts. 10 if state has specific entry; 5 if using default."""
    state_data = BENCHMARKS.get(state.upper())
    if state_data is None:
        return 3.0
    if land_type in state_data:
        return 10.0
    return 5.0


def score_listing(listing: dict) -> dict:
    """
    Enrich a listing dict with opportunity score and supporting analytics.

    Input dict keys (from scraper):
        property_name, state, city, hectares, auction_price, auction_date,
        listing_url, auction_type, lot_id, source

    Added keys:
        land_type            : str
        price_per_ha_low     : float | None
        price_per_ha_mid     : float | None
        price_per_ha_high    : float | None
        market_value_low     : float | None
        market_value_mid     : float | None
        market_value_high    : float | None
        sp_price_per_ha_low  : float | None  (S&P reference, Baixa)
        sp_price_per_ha_mid  : float | None  (S&P reference, average)
        sp_price_per_ha_high : float | None  (S&P reference, Alta)
        sp_match_level       : str | None    ("municipio" / "estado" / None)
        auction_price_per_ha : float | None
        discount_to_mid_pct  : float | None  (positive = below market)
        score                : float  (0–100)
        grade                : str   (A/B/C/D/F)
        score_breakdown      : dict  (component scores)
    """
    result = dict(listing)

    state = (listing.get("state") or "").upper()
    property_name = listing.get("property_name") or ""
    hectares: Optional[float] = listing.get("hectares")
    auction_price: Optional[float] = listing.get("auction_price")
    auction_date: str = listing.get("auction_date") or ""

    # 1. Detect land type
    land_type = detect_land_type(property_name)
    result["land_type"] = land_type

    # 2. Market value estimates
    if hectares and hectares > 0 and state:
        mv = market_value_estimate(state, land_type, hectares)
        result["price_per_ha_low"] = mv["price_per_ha_low"]
        result["price_per_ha_mid"] = mv["price_per_ha_mid"]
        result["price_per_ha_high"] = mv["price_per_ha_high"]
        result["market_value_low"] = mv["value_low"]
        result["market_value_mid"] = mv["value_mid"]
        result["market_value_high"] = mv["value_high"]
    else:
        result["price_per_ha_low"] = None
        result["price_per_ha_mid"] = None
        result["price_per_ha_high"] = None
        result["market_value_low"] = None
        result["market_value_mid"] = None
        result["market_value_high"] = None

    # 3. S&P reference values (R$/ha benchmark — independent of hectares;
    #    a property's per-hectare reference price doesn't depend on its size).
    #    City is used when available — get_sp_reference() tries an exact
    #    (UF, município, subgrupo) match first and only falls back to a
    #    (UF, subgrupo) state-wide average when the município isn't in the
    #    700+ S&P datapoints (or city is unknown). Either way we still want
    #    to surface the benchmark — hectares is only needed to convert this
    #    R$/ha rate into a discount-to-market percentage (component 6 below).
    city = listing.get("city") or ""
    sp_ref = get_sp_reference(state, city, land_type) if state else None
    if sp_ref:
        sp_low_ha, sp_mid_ha, sp_high_ha, sp_match_level = sp_ref
        result["sp_price_per_ha_low"]  = sp_low_ha
        result["sp_price_per_ha_mid"]  = sp_mid_ha
        result["sp_price_per_ha_high"] = sp_high_ha
        result["sp_match_level"] = sp_match_level
    else:
        result["sp_price_per_ha_low"]  = None
        result["sp_price_per_ha_mid"]  = None
        result["sp_price_per_ha_high"] = None
        result["sp_match_level"] = None

    # 5. Price per hectare at auction
    if auction_price and hectares and hectares > 0:
        result["auction_price_per_ha"] = round(auction_price / hectares, 2)
    else:
        result["auction_price_per_ha"] = None

    # 6. Discount: (S&P R$/ha Médio - auction R$/ha) / S&P R$/ha Médio × 100
    # Always reset first — scraper pre-computed values must not leak through.
    # Only populated when S&P city reference data is available.
    auction_per_ha = result.get("auction_price_per_ha")
    sp_mid_ha = result.get("sp_price_per_ha_mid")
    result["discount_to_mid_pct"] = None
    if auction_per_ha and sp_mid_ha:
        discount = (sp_mid_ha - auction_per_ha) / sp_mid_ha * 100
        result["discount_to_mid_pct"] = round(discount, 1)

    # 7. Per-round price/ha and discount vs S&P mid
    price_r1: Optional[float] = listing.get("price_round1")
    price_r2: Optional[float] = listing.get("price_round2")

    result["price_per_ha_round1"] = round(price_r1 / hectares, 2) if (price_r1 and hectares and hectares > 0) else None
    result["price_per_ha_round2"] = round(price_r2 / hectares, 2) if (price_r2 and hectares and hectares > 0) else None

    result["discount_round1_pct"] = None
    result["discount_round2_pct"] = None
    if sp_mid_ha:
        if result["price_per_ha_round1"]:
            result["discount_round1_pct"] = round((sp_mid_ha - result["price_per_ha_round1"]) / sp_mid_ha * 100, 1)
        if result["price_per_ha_round2"]:
            result["discount_round2_pct"] = round((sp_mid_ha - result["price_per_ha_round2"]) / sp_mid_ha * 100, 1)

    # 7. Score components
    market_mid = result.get("market_value_mid") or 0.0
    disc_score = _discount_score(auction_price or 0.0, market_mid) if (auction_price and market_mid) else 0.0
    cert_score = _certainty_score(hectares, auction_price)
    urg_score = _urgency_score(auction_date)
    conf_score = _benchmark_confidence_score(state, land_type)

    total = disc_score + cert_score + urg_score + conf_score
    total = max(0.0, min(100.0, total))

    result["score"] = round(total, 1)
    result["score_breakdown"] = {
        "discount_component": round(disc_score, 1),
        "certainty_component": round(cert_score, 1),
        "urgency_component": round(urg_score, 1),
        "confidence_component": round(conf_score, 1),
    }

    # Grade
    if total >= 80:
        result["grade"] = "A"
    elif total >= 60:
        result["grade"] = "B"
    elif total >= 40:
        result["grade"] = "C"
    elif total >= 20:
        result["grade"] = "D"
    else:
        result["grade"] = "F"

    return result


def score_all(listings: list[dict]) -> list[dict]:
    """Score a list of listings and return sorted by score descending."""
    scored = [score_listing(l) for l in listings]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
