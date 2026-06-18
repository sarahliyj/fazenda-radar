"""
S&P (Land-BRZ25) reference price lookup.

Source: LAND-BRZ25 Brazil Farmland Market Analysis Ed.117, Jan-Mar 2025.
Prices are in R$/ha as of March 2025.

Lookup hierarchy:
  1. Exact (UF, municipio, subgrupo) match
  2. (UF, subgrupo) state-level average — used when the município itself
     has no S&P rows for the relevant subgrupo(s)
  3. None (no data available)

Returns (low, mid, high, match_level) in R$/ha where:
  - low         = Baixa capacidade average
  - mid         = average of all capacidade tiers
  - high        = Alta capacidade average
    (Média is used to fill gaps if Alta or Baixa absent)
  - match_level = "municipio" if the figures come from the property's own
                  município, or "estado" if they are a state-wide average
                  fallback (município not present in the S&P database)
"""

from __future__ import annotations

import unicodedata
import os
from collections import defaultdict
from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------
# Land type → S&P subgrupo mapping
# Our internal land_type keys → S&P SUBGRUPO values
# ---------------------------------------------------------------------------
_LANDTYPE_TO_SUBGRUPO: dict[str, list[str]] = {
    "soja":          ["Grãos", "Produção Diversificada"],
    "grãos":         ["Grãos", "Produção Diversificada"],
    "lavoura":       ["Grãos", "Produção Diversificada"],
    "milho":         ["Grãos", "Produção Diversificada"],
    "cana":          ["Cana"],
    "café":          ["Café"],
    "cafe":          ["Café"],
    "arroz":         ["Arroz"],
    "fruticultura":  ["Fruticultura"],
    "pastagem":      ["Pastagem"],
    "pecuária":      ["Pastagem"],
    "pecuaria":      ["Pastagem"],
    "mata":          ["Cerrado", "Floresta Amazonica", "Mata Atlântica", "Floresta de Transição"],
    "floresta":      ["Florestas Plantadas", "Floresta Amazonica"],
    "silvicultura":  ["Florestas Plantadas"],
    "misto":         ["Produção Diversificada", "Grãos"],
    # fallback
    "_default":      ["Grãos", "Produção Diversificada", "Pastagem"],
}


def _normalize(s: str) -> str:
    """Strip accents and lowercase for fuzzy matching."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower().strip()


# ---------------------------------------------------------------------------
# Load and index the S&P data
# ---------------------------------------------------------------------------

_XLSX_FILENAME = "LAND-BRZ25-Brazil_Farmland_Market_Analysis_Ed_117_Jan-Mar_2025_English-Portuguese_tables_last_udpated_on_4-15-25_v2.xlsx"


def _find_xlsx() -> Optional[str]:
    """Search common locations for the S&P xlsx file.

    Checked in order:
      1. Bundled copy inside the repo (data/reference/) — this is what makes
         the app portable to Streamlit Community Cloud / any other machine,
         since the cloud has no access to the developer's local Desktop.
      2. Local-dev fallback paths (the original working setup on this machine).
    """
    candidates = [
        os.path.join(os.path.dirname(__file__), "reference", _XLSX_FILENAME),
        os.path.join(os.path.dirname(__file__), "..", "..", "project 2", _XLSX_FILENAME),
        os.path.expanduser(f"~/Desktop/project 2/{_XLSX_FILENAME}"),
    ]
    for p in candidates:
        p = os.path.normpath(p)
        if os.path.exists(p):
            return p
    return None


@lru_cache(maxsize=1)
def _load_sp_data() -> dict:
    """
    Load S&P xlsx and return indexed lookup dicts.

    Returns dict with keys:
      'mun_sub'   : {(uf, norm_mun, subgrupo) -> {cap -> [prices]}}
      'state_sub' : {(uf, subgrupo)           -> {cap -> [prices]}}
      'rows'      : list of raw row dicts for the reference table
    """
    path = _find_xlsx()
    if path is None:
        return {"mun_sub": {}, "state_sub": {}, "rows": []}

    try:
        import openpyxl
    except ImportError:
        return {"mun_sub": {}, "state_sub": {}, "rows": []}

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Base de dados consolidada"]
    raw_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Header is row index 3; data starts at index 4
    # Columns: ID_CLASSN(0) ID_REG(1) REGIAO(2) UF(3) ID_MUN(4) MUNICIPIO(5)
    #          GRUPO(6) SUBGRUPO(7) USO(8) CAPACIDADE(9) COMPLEMENTO(10)
    #          VALOR_UNI(11) UNI(12) BIOMA(13) STATUS(14) [blank](15)
    #          price_2001(16) price_dec24(17) price_mar25(18) x(19)

    mun_sub: dict = defaultdict(lambda: defaultdict(list))
    state_sub: dict = defaultdict(lambda: defaultdict(list))
    rows_out = []

    for r in raw_rows[4:]:
        if r[14] != 1:          # only active rows
            continue
        uf       = r[3]
        regiao   = r[2]
        mun      = r[5]
        subgrupo = r[7]
        cap      = r[9]
        price    = r[18]        # Mar 2025

        if not all([uf, mun, subgrupo, cap, price]):
            continue
        if not isinstance(price, (int, float)):
            continue
        uf  = str(uf).strip().upper()
        mun = str(mun).strip()
        sub = str(subgrupo).strip()
        cap = str(cap).strip()

        mun_key = (uf, _normalize(mun), sub)
        mun_sub[mun_key][cap].append(price)

        state_key = (uf, sub)
        state_sub[state_key][cap].append(price)

        rows_out.append({
            "regiao":   str(regiao).strip() if regiao else "",
            "uf":       uf,
            "municipio": mun,
            "subgrupo": sub,
            "capacidade": cap,
            "price_mar25": price,
        })

    return {
        "mun_sub":   dict(mun_sub),
        "state_sub": dict(state_sub),
        "rows":      rows_out,
    }


# ---------------------------------------------------------------------------
# Aggregate capacidade tiers into (low, mid, high)
# ---------------------------------------------------------------------------

def _agg(cap_dict: dict) -> tuple[Optional[float], float, Optional[float]]:
    """
    Given {cap -> [prices]}, return (baixa_avg, mid_avg, alta_avg).
    mid_avg = average of ALL prices regardless of tier.
    baixa / alta are None if that tier is absent.
    """
    def avg(lst):
        return round(sum(lst) / len(lst)) if lst else None

    baixa = avg(cap_dict.get("Baixa", []))
    media = avg(cap_dict.get("Média", []))
    alta  = avg(cap_dict.get("Alta",  []))

    # mid = mean of all prices across all tiers
    all_prices = [p for lst in cap_dict.values() for p in lst]
    mid = avg(all_prices)

    # Fill gaps: if no Baixa use Média, if no Alta use Média
    return (baixa or media, mid, alta or media)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_sp_reference(
    uf: str,
    city: str,
    land_type: str,
) -> Optional[tuple[float, float, float, str]]:
    """
    Return (low, mid, high, match_level) R$/ha from S&P data for the given property.

    match_level indicates how the figures were derived:
      "municipio" — exact (UF, município, subgrupo) match — most precise
      "estado"    — município not found in the S&P database; figures are a
                    (UF, subgrupo) state-wide average instead

    Match priority:
      1. (UF, city, subgrupo)  — exact municipality
      2. (UF, subgrupo)        — state-level average
      3. None

    land_type: internal key (soja, pastagem, cana, café, misto, etc.)
    """
    data = _load_sp_data()
    if not data["mun_sub"]:
        return None

    uf         = str(uf).strip().upper()
    city_norm  = _normalize(str(city))
    lt_lower   = land_type.lower().strip()

    subgrupos = _LANDTYPE_TO_SUBGRUPO.get(lt_lower, _LANDTYPE_TO_SUBGRUPO["_default"])

    # 1. Municipality-level
    for sub in subgrupos:
        key = (uf, city_norm, sub)
        if key in data["mun_sub"]:
            baixa, mid, alta = _agg(data["mun_sub"][key])
            return (baixa, mid, alta, "municipio")

    # 2. State-level fallback
    for sub in subgrupos:
        key = (uf, sub)
        if key in data["state_sub"]:
            baixa, mid, alta = _agg(data["state_sub"][key])
            return (baixa, mid, alta, "estado")

    return None


_STATE_AVG_LABEL = "— Média estadual —"


def sp_reference_table() -> "pd.DataFrame":
    """
    Return the full S&P reference dataset as a tidy DataFrame for the
    Tabela de Referência tab.

    Columns: regiao, uf, municipio, subgrupo, price_baixa, price_mid,
             price_alta, row_type
    One row per (uf, municipio, subgrupo) — capacidade tiers collapsed —
    PLUS one extra "— Média estadual —" row per (uf, subgrupo) holding the
    state-wide average that get_sp_reference() falls back to when a
    município has no data of its own. These rows have row_type == "estado"
    (vs. "municipio" for the regular rows) and are sorted to sit directly
    below the municípios that make up that (uf, subgrupo) group, so users
    can see exactly what figure listings receive when their município isn't
    in the database.
    """
    import pandas as pd

    data = _load_sp_data()
    if not data["mun_sub"]:
        return pd.DataFrame()

    STATE_NAMES = {
        "AC": "Acre", "AL": "Alagoas", "AM": "Amazonas", "AP": "Amapá",
        "BA": "Bahia", "CE": "Ceará", "ES": "Espírito Santo", "GO": "Goiás",
        "MA": "Maranhão", "MG": "Minas Gerais", "MS": "Mato Grosso do Sul",
        "MT": "Mato Grosso", "PA": "Pará", "PB": "Paraíba", "PE": "Pernambuco",
        "PI": "Piauí", "PR": "Paraná", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
        "RO": "Rondônia", "RR": "Roraima", "RS": "Rio Grande do Sul",
        "SC": "Santa Catarina", "SE": "Sergipe", "SP": "São Paulo", "TO": "Tocantins",
    }

    # Group raw rows by (uf, municipio, subgrupo) preserving regiao
    from collections import defaultdict
    grouped: dict = defaultdict(lambda: {"regiao": "", "caps": defaultdict(list)})
    for row in data["rows"]:
        k = (row["uf"], row["municipio"], row["subgrupo"])
        grouped[k]["regiao"] = row["regiao"]
        grouped[k]["caps"][row["capacidade"]].append(row["price_mar25"])

    out = []
    for (uf, mun, sub), v in grouped.items():
        baixa, mid, alta = _agg(v["caps"])
        out.append({
            "regiao":       v["regiao"],
            "uf":           uf,
            "state_name":   STATE_NAMES.get(uf, uf),
            "municipio":    mun,
            "subgrupo":     sub,
            "price_baixa":  baixa,
            "price_mid":    mid,
            "price_alta":   alta,
            "row_type":     "municipio",
        })

    # Append one "— Média estadual —" row per (uf, subgrupo) — the same
    # state-wide aggregate that get_sp_reference() uses as its fallback.
    for (uf, sub), cap_dict in data["state_sub"].items():
        baixa, mid, alta = _agg(cap_dict)
        out.append({
            "regiao":       "",
            "uf":           uf,
            "state_name":   STATE_NAMES.get(uf, uf),
            "municipio":    _STATE_AVG_LABEL,
            "subgrupo":     sub,
            "price_baixa":  baixa,
            "price_mid":    mid,
            "price_alta":   alta,
            "row_type":     "estado",
        })

    df = pd.DataFrame(out)
    # Sort so that, within each (uf, subgrupo) group, the município rows come
    # first (alphabetically) and the "— Média estadual —" row sits right
    # below them — i.e. directly underneath the data it summarises.
    df["_is_state_row"] = (df["row_type"] == "estado").astype(int)
    df = (
        df.sort_values(["uf", "subgrupo", "_is_state_row", "municipio"])
        .drop(columns="_is_state_row")
        .reset_index(drop=True)
    )
    return df
