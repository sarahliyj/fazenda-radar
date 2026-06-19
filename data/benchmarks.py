"""
Regional land price benchmarks for Brazil.

Sources / methodology:
  - FNP Consultoria & Agroinformativos (Agrianual / Anualpec)
  - INCRA land value reference tables (2023/2024)
  - EMBRAPA regional surveys
  - Scot Consultoria cattle market reports

All values are in BRL per hectare (R$/ha).
These are CONSERVATIVE mid-range estimates for distressed-asset analysis.
Update annually or replace with a live API call to FNP / CONAB.

Land types mapped:
  "soja"       – soy farmland (high productivity, Cerrado/Sul)
  "cana"       – sugarcane land (SP, PR, MG, GO)
  "pastagem"   – improved pasture / cattle
  "mata"       – native forest / legalised reserve
  "misto"      – mixed / unknown rural property (conservative)
  "café"       – coffee land (MG, SP, ES, PR)
  "arroz"      – rice paddy land (RS, MT, PA)
  "fruticultura" – fruit orchards
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# State-level price per hectare (R$/ha) by land type
# Format: BENCHMARKS[state_uf][land_type] = (low, mid, high)
# ---------------------------------------------------------------------------

BENCHMARKS: dict[str, dict[str, tuple[float, float, float]]] = {
    # ── São Paulo ────────────────────────────────────────────────────────────
    "SP": {
        "soja":        (25_000, 35_000, 55_000),
        "cana":        (30_000, 45_000, 70_000),
        "pastagem":    (12_000, 18_000, 28_000),
        "mata":        ( 5_000,  8_000, 15_000),
        "misto":       (15_000, 22_000, 35_000),
        "café":        (20_000, 30_000, 50_000),
        "fruticultura":(18_000, 28_000, 45_000),
    },
    # ── Mato Grosso ─────────────────────────────────────────────────────────
    "MT": {
        "soja":        (18_000, 28_000, 42_000),
        "pastagem":    ( 6_000, 10_000, 16_000),
        "mata":        ( 2_000,  4_000,  8_000),
        "misto":       ( 8_000, 14_000, 22_000),
        "arroz":       (10_000, 16_000, 25_000),
        "cana":        (12_000, 18_000, 28_000),
        "fruticultura":(10_000, 16_000, 26_000),
    },
    # ── Mato Grosso do Sul ──────────────────────────────────────────────────
    "MS": {
        "soja":        (14_000, 22_000, 34_000),
        "pastagem":    ( 6_000, 10_000, 16_000),
        "mata":        ( 2_500,  5_000,  9_000),
        "misto":       ( 8_000, 13_000, 20_000),
        "cana":        (12_000, 18_000, 28_000),
        "fruticultura":( 8_000, 14_000, 22_000),
    },
    # ── Goiás ────────────────────────────────────────────────────────────────
    "GO": {
        "soja":        (15_000, 24_000, 38_000),
        "cana":        (14_000, 22_000, 35_000),
        "pastagem":    ( 7_000, 11_000, 18_000),
        "mata":        ( 2_000,  4_500,  8_000),
        "misto":       ( 9_000, 15_000, 24_000),
        "fruticultura":( 8_000, 14_000, 22_000),
    },
    # ── Minas Gerais ─────────────────────────────────────────────────────────
    "MG": {
        "soja":        (12_000, 20_000, 32_000),
        "café":        (18_000, 28_000, 48_000),
        "pastagem":    ( 6_000,  9_500, 16_000),
        "mata":        ( 2_000,  4_000,  7_000),
        "misto":       ( 8_000, 13_000, 22_000),
        "fruticultura":(10_000, 17_000, 28_000),
    },
    # ── Paraná ───────────────────────────────────────────────────────────────
    "PR": {
        "soja":        (22_000, 35_000, 55_000),
        "cana":        (18_000, 28_000, 45_000),
        "café":        (16_000, 26_000, 42_000),
        "pastagem":    ( 9_000, 14_000, 22_000),
        "mata":        ( 4_000,  7_000, 12_000),
        "misto":       (12_000, 19_000, 30_000),
        "fruticultura":(12_000, 20_000, 32_000),
    },
    # ── Rio Grande do Sul ────────────────────────────────────────────────────
    "RS": {
        "soja":        (20_000, 32_000, 50_000),
        "arroz":       (15_000, 24_000, 38_000),
        "pastagem":    ( 8_000, 13_000, 20_000),
        "mata":        ( 3_000,  6_000, 10_000),
        "misto":       (10_000, 17_000, 27_000),
        "fruticultura":(10_000, 18_000, 30_000),
    },
    # ── Santa Catarina ───────────────────────────────────────────────────────
    "SC": {
        "soja":        (18_000, 28_000, 44_000),
        "pastagem":    ( 8_000, 13_000, 20_000),
        "mata":        ( 4_000,  7_000, 12_000),
        "misto":       (10_000, 17_000, 27_000),
        "fruticultura":(12_000, 20_000, 32_000),
    },
    # ── Bahia ────────────────────────────────────────────────────────────────
    "BA": {
        "soja":        ( 8_000, 14_000, 24_000),
        "café":        (10_000, 17_000, 30_000),
        "pastagem":    ( 2_500,  5_000,  9_000),
        "mata":        ( 1_000,  2_500,  5_000),
        "misto":       ( 3_000,  6_500, 12_000),
        "fruticultura":( 4_000,  8_000, 14_000),
        "cana":        ( 6_000, 10_000, 16_000),
    },
    # ── Piauí ────────────────────────────────────────────────────────────────
    "PI": {
        "soja":        ( 5_000, 10_000, 18_000),
        "pastagem":    ( 1_500,  3_000,  6_000),
        "mata":        (   800,  1_800,  3_500),
        "misto":       ( 2_000,  4_500,  9_000),
        "fruticultura":( 3_000,  6_000, 11_000),
    },
    # ── Maranhão ─────────────────────────────────────────────────────────────
    "MA": {
        "soja":        ( 4_000,  8_000, 15_000),
        "pastagem":    ( 1_200,  2_800,  5_500),
        "mata":        (   600,  1_500,  3_000),
        "misto":       ( 1_800,  4_000,  8_000),
        "arroz":       ( 3_000,  6_000, 11_000),
    },
    # ── Tocantins ────────────────────────────────────────────────────────────
    "TO": {
        "soja":        ( 5_000,  9_000, 16_000),
        "pastagem":    ( 1_500,  3_200,  6_500),
        "mata":        (   800,  2_000,  4_000),
        "misto":       ( 2_500,  5_000, 10_000),
    },
    # ── Pará ─────────────────────────────────────────────────────────────────
    "PA": {
        "soja":        ( 3_500,  7_000, 13_000),
        "pastagem":    ( 1_000,  2_500,  5_000),
        "mata":        (   500,  1_200,  2_500),
        "misto":       ( 1_500,  3_500,  7_000),
        "arroz":       ( 2_500,  5_000, 10_000),
    },
    # ── Rondônia ─────────────────────────────────────────────────────────────
    "RO": {
        "soja":        ( 5_000,  9_500, 17_000),
        "pastagem":    ( 2_000,  4_500,  8_500),
        "mata":        (   800,  2_000,  4_000),
        "misto":       ( 3_000,  6_000, 11_000),
    },
    # ── Mato Grosso (Cerrado expansion) — already covered above
    # ── Rio de Janeiro ───────────────────────────────────────────────────────
    "RJ": {
        "pastagem":    ( 6_000, 12_000, 22_000),
        "mata":        ( 3_000,  6_000, 12_000),
        "misto":       ( 8_000, 15_000, 28_000),
        "fruticultura":(10_000, 18_000, 30_000),
    },
    # ── Espírito Santo ───────────────────────────────────────────────────────
    "ES": {
        "café":        (15_000, 24_000, 40_000),
        "pastagem":    ( 6_000, 11_000, 18_000),
        "mata":        ( 3_000,  6_000, 11_000),
        "misto":       ( 8_000, 14_000, 22_000),
    },
    # ── Ceará ────────────────────────────────────────────────────────────────
    "CE": {
        "pastagem":    ( 1_500,  3_500,  7_000),
        "misto":       ( 2_000,  4_500,  9_000),
        "mata":        (   600,  1_500,  3_000),
        "fruticultura":( 4_000,  8_000, 15_000),
    },
    # ── Pernambuco ───────────────────────────────────────────────────────────
    "PE": {
        "cana":        ( 8_000, 14_000, 22_000),
        "pastagem":    ( 2_000,  4_500,  9_000),
        "mata":        ( 1_000,  2_500,  5_000),
        "misto":       ( 3_000,  6_000, 11_000),
        "fruticultura":( 5_000, 10_000, 18_000),
    },
    # ── Default (unknown state) ──────────────────────────────────────────────
    "_DEFAULT": {
        "soja":        ( 6_000, 12_000, 22_000),
        "cana":        ( 8_000, 14_000, 24_000),
        "pastagem":    ( 2_500,  6_000, 12_000),
        "mata":        (   800,  2_500,  5_000),
        "misto":       ( 4_000,  8_000, 15_000),
        "café":        ( 8_000, 16_000, 28_000),
        "arroz":       ( 5_000, 10_000, 18_000),
        "fruticultura":( 6_000, 12_000, 22_000),
    },
}

# Fill missing states with _DEFAULT values so every state has every type
_ALL_TYPES = list(BENCHMARKS["_DEFAULT"].keys())
for _state in list(BENCHMARKS.keys()):
    for _ltype in _ALL_TYPES:
        if _ltype not in BENCHMARKS[_state]:
            BENCHMARKS[_state][_ltype] = BENCHMARKS["_DEFAULT"][_ltype]


# ---------------------------------------------------------------------------
# Land type detection from property name / description
# ---------------------------------------------------------------------------

_TYPE_KEYWORDS: dict[str, list[str]] = {
    "soja":        ["soja", "soy", "lavoura", "grão", "grao", "plantio", "cerrado lavour"],
    "cana":        ["cana", "canavial", "usina", "açúcar", "acucar"],
    "café":        ["café", "cafe", "cafeeiro", "cafeicultura"],
    "arroz":       ["arroz", "várzea", "varzea", "irrigado"],
    "fruticultura":["fruta", "pomar", "uva", "vinhedo", "manga", "melão", "laranja", "citros"],
    "mata":        ["mata", "floresta", "reserva", "ambiental", "nativa", "amazônia", "amazonia"],
    "pastagem":    ["pastagem", "pasto", "pecuária", "pecuaria", "gado", "boi", "bovino", "fazenda"],
}


def detect_land_type(text: str) -> str:
    """
    Infer land type from property name or description.
    Returns one of the BENCHMARKS keys, defaulting to 'misto'.
    Uses word-boundary matching to avoid false positives from city/place
    names (e.g. "Laranjal Paulista" must not trigger 'laranja' → fruticultura).
    """
    import re
    import unicodedata
    norm = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()
    for ltype, keywords in _TYPE_KEYWORDS.items():
        for kw in keywords:
            kw_norm = unicodedata.normalize("NFD", kw).encode("ascii", "ignore").decode().lower()
            if re.search(r"\b" + re.escape(kw_norm) + r"\b", norm):
                return ltype
    return "misto"


def get_benchmark(state: str, land_type: str) -> tuple[float, float, float]:
    """
    Return (low, mid, high) R$/ha benchmark for a state and land type.
    Falls back to _DEFAULT if state not found.
    """
    state = state.upper()
    land_type = land_type.lower()
    state_data = BENCHMARKS.get(state, BENCHMARKS["_DEFAULT"])
    return state_data.get(land_type, BENCHMARKS["_DEFAULT"].get(land_type, (4_000, 8_000, 15_000)))


def benchmarks_table() -> "pd.DataFrame":
    """
    Return the full benchmark matrix as a tidy pandas DataFrame.

    Columns: state, land_type, price_low, price_mid, price_high, range_spread
    Excludes the internal _DEFAULT row.
    Suitable for display in the dashboard Benchmarks tab.
    """
    import pandas as pd

    STATE_NAMES = {
        "SP": "São Paulo", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
        "GO": "Goiás", "MG": "Minas Gerais", "PR": "Paraná",
        "RS": "Rio Grande do Sul", "SC": "Santa Catarina", "BA": "Bahia",
        "PI": "Piauí", "MA": "Maranhão", "TO": "Tocantins", "PA": "Pará",
        "RO": "Rondônia", "RJ": "Rio de Janeiro", "ES": "Espírito Santo",
        "CE": "Ceará", "PE": "Pernambuco",
    }
    REGION = {
        "SP": "Sudeste", "MG": "Sudeste", "RJ": "Sudeste", "ES": "Sudeste",
        "PR": "Sul",     "RS": "Sul",     "SC": "Sul",
        "MT": "Centro-Oeste", "MS": "Centro-Oeste", "GO": "Centro-Oeste",
        "BA": "Nordeste", "PI": "Nordeste", "MA": "Nordeste",
        "CE": "Nordeste", "PE": "Nordeste",
        "PA": "Norte",  "TO": "Norte", "RO": "Norte",
    }
    TYPE_LABELS = {
        "soja": "Soja", "cana": "Cana-de-açúcar", "café": "Café",
        "arroz": "Arroz", "fruticultura": "Fruticultura",
        "mata": "Mata / Reserva", "pastagem": "Pastagem", "misto": "Misto / Outros",
    }

    rows = []
    for state, types in BENCHMARKS.items():
        if state == "_DEFAULT":
            continue
        for ltype, (low, mid, high) in types.items():
            rows.append({
                "region":        REGION.get(state, "Outros"),
                "state":         state,
                "state_name":    STATE_NAMES.get(state, state),
                "land_type":     ltype,
                "land_type_label": TYPE_LABELS.get(ltype, ltype.title()),
                "price_low":     low,
                "price_mid":     mid,
                "price_high":    high,
                "range_spread":  high - low,
                "source":        "FNP/INCRA 2024 (estimativa)",
            })

    df = pd.DataFrame(rows).sort_values(["region", "state", "land_type"]).reset_index(drop=True)
    return df


def market_value_estimate(state: str, land_type: str, hectares: float) -> dict:
    """
    Return a dict with low/mid/high market value estimates for a property.

    Args:
        state: 2-letter UF code.
        land_type: One of the keys in BENCHMARKS.
        hectares: Property size.

    Returns:
        {
            "price_per_ha_low": float,
            "price_per_ha_mid": float,
            "price_per_ha_high": float,
            "value_low": float,
            "value_mid": float,
            "value_high": float,
            "land_type": str,
            "state": str,
        }
    """
    low_ha, mid_ha, high_ha = get_benchmark(state, land_type)
    return {
        "price_per_ha_low": low_ha,
        "price_per_ha_mid": mid_ha,
        "price_per_ha_high": high_ha,
        "value_low": round(low_ha * hectares, 2),
        "value_mid": round(mid_ha * hectares, 2),
        "value_high": round(high_ha * hectares, 2),
        "land_type": land_type,
        "state": state,
    }
