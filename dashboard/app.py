"""
Fazenda Radar — Streamlit Dashboard  (v3 · bilingual PT/EN)
============================================================
Tabs:
  1. Overview / Visão Geral
  2. Auction Lots / Lotes em Leilão
  3. Price Benchmarks / Benchmarks de Preço

Launch: streamlit run dashboard/app.py
"""

from __future__ import annotations

import io
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Path so imports work from any CWD ────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.megaleiloes import scrape as scrape_megaleiloes
from scrapers.leilaobrasil import scrape as scrape_leilaobrasil
from scrapers.leilaoimovel import scrape as scrape_leilaoimovel
from scrapers.grupolance import scrape as scrape_grupolance
from scrapers.leiloesjudiciais import scrape as scrape_leiloesjudiciais
from scrapers.leilaovip import scrape as scrape_leilaovip
from scrapers.superbid import scrape as scrape_superbid
from data.benchmarks import BENCHMARKS, _ALL_TYPES as ALL_LAND_TYPES, benchmarks_table
from data.sp_reference import sp_reference_table
from data.apify_enricher import enrich_hectares
from data.scorer import score_all
try:
    from data.listings_store import (
        load_store, save_store, merge_scrape, backend_name, backend_reason,
        save_last_search, load_last_search,
    )
except Exception as _ls_exc:  # pragma: no cover — keep the app alive if the
    # storage module can't be imported (e.g. a stale hot-reload cache). The
    # app still works; persistence and new/price tracking are simply off.
    from datetime import date as _date

    _LS_ERR = _ls_exc

    def load_store() -> dict:
        return {}

    def save_store(store) -> None:
        pass

    def save_last_search(delta) -> None:
        pass

    def load_last_search() -> dict:
        return {}

    def backend_name() -> str:
        return "indisponível"

    def backend_reason() -> str:
        return f"módulo de armazenamento falhou ao importar: {_LS_ERR}"

    def merge_scrape(scraped, store):
        """Pure in-memory merge (no persistence) — mirrors the real logic."""
        today = _date.today().isoformat()
        updated = dict(store)
        new_lots, price_changes = [], []
        for lot in scraped:
            lid = lot.get("lot_id")
            if not lid:
                continue
            lid = str(lid)
            new_price = lot.get("auction_price") or lot.get("price_round1")
            if lid not in updated:
                entry = {**lot, "first_seen": today, "last_seen": today,
                         "prev_price": new_price}
                updated[lid] = entry
                new_lots.append(entry)
            else:
                prev = updated[lid]
                old_price = prev.get("auction_price") or prev.get("price_round1")
                entry = {**lot, "first_seen": prev.get("first_seen", today),
                         "last_seen": today, "prev_price": old_price}
                if (new_price is not None and old_price is not None
                        and new_price != old_price):
                    price_changes.append({**entry, "old_price": old_price,
                                          "new_price": new_price})
                updated[lid] = entry
        return list(updated.values()), new_lots, price_changes, updated

try:
    from scrapers.eleiloes import scrape as scrape_eleiloes
    _ELEILOES_AVAILABLE = True
except ImportError:
    _ELEILOES_AVAILABLE = False

ALL_STATES: set[str] = {s for s in BENCHMARKS if s != "_DEFAULT"}

logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
# Starred lots — persisted to a local JSON file
# ─────────────────────────────────────────────────────────────────────────────
_STARS_FILE = Path.home() / ".fazenda_radar_stars.json"

def _load_stars() -> set[str]:
    try:
        return set(json.loads(_STARS_FILE.read_text()))
    except Exception:
        return set()

def _save_stars(stars: set[str]) -> None:
    try:
        _STARS_FILE.write_text(json.dumps(sorted(stars)))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Language strings
# ─────────────────────────────────────────────────────────────────────────────
STRINGS: dict[str, dict[str, str]] = {
    # ── Sidebar ───────────────────────────────────────────────────────────────
    "app_subtitle":         {"pt": "Radar de Oportunidades em Terras Rurais",
                             "en": "Rural Land Opportunity Radar"},
    "data_collection":      {"pt": "Coleta de Dados",        "en": "Data Collection"},
    "batch_size_label":     {"pt": "Lotes por busca",
                             "en": "Lots per batch"},
    "batch_size_help":      {"pt": "Quantidade alvo de lotes por busca, distribuída igualmente entre as fontes ativas.",
                             "en": "Target lots per batch, distributed equally across active sources."},
    "scrape_button":        {"pt": "Buscar Leilões", "en": "Search Auctions"},
    "last_scraped":         {"pt": "Última coleta:",          "en": "Last scraped:"},
    "lots_in_memory":       {"pt": "lotes carregados",        "en": "lots loaded"},
    "footer":               {"pt": "Fazenda Radar v3.0\nDados: FNP/INCRA 2024",
                             "en": "Fazenda Radar v3.0\nData: FNP/INCRA 2024"},
    "language_label":       {"pt": "Idioma / Language",       "en": "Idioma / Language"},

    # ── Tab names ─────────────────────────────────────────────────────────────
    "tab_overview":         {"pt": "Visão Geral",             "en": "Overview"},
    "tab_lots":             {"pt": "Lotes em Leilão",         "en": "Auction Lots"},
    "tab_bench":            {"pt": "Benchmarks de Preço",     "en": "Price Benchmarks"},

    # ── Scrape spinner / messages ─────────────────────────────────────────────
    "sources_label":        {"pt": "Fontes de dados",          "en": "Data sources"},
    "src_all":              {"pt": "Todas",                    "en": "All"},
    "src_lj":               {"pt": "leiloesjudiciais.com.br",  "en": "leiloesjudiciais.com.br"},
    "src_lim":              {"pt": "leilaoimovel.com.br",      "en": "leilaoimovel.com.br"},
    "src_mega":             {"pt": "megaleiloes.com.br",       "en": "megaleiloes.com.br"},
    "src_gl":               {"pt": "grupolance.com.br",        "en": "grupolance.com.br"},
    "src_lb":               {"pt": "leilaobrasil.com.br",      "en": "leilaobrasil.com.br"},
    "src_lvip":             {"pt": "leilaovip.com.br",         "en": "leilaovip.com.br"},
    "src_none_warn":        {"pt": "Selecione ao menos uma fonte.",
                             "en": "Select at least one source."},

    "scraping_spinner":     {"pt": "Buscando e coletando lotes…",
                             "en": "Searching and fetching lots…"},
    "apify_label":          {"pt": "Token Apify (opcional)",    "en": "Apify Token (optional)"},
    "apify_help":           {"pt": "Se preenchido, busca hectares faltantes via Apify.",
                             "en": "If set, fetches missing hectares via Apify."},
    "apify_enriching":      {"pt": "Buscando hectares via Apify…",
                             "en": "Fetching missing hectares via Apify…"},
    "scrape_success":       {"pt": "lotes coletados.",
                             "en": "lots collected."},
    "scrape_error":         {"pt": "Erro na coleta:",         "en": "Scrape error:"},
    "no_data_info":         {"pt": "Nenhum dado carregado. Use **{btn}** no menu lateral.",
                             "en": "No data loaded. Click **{btn}** in the sidebar."},

    # ── Overview tab ──────────────────────────────────────────────────────────
    "overview_header":      {"pt": "Resumo Geral",            "en": "General Summary"},
    "kpi_total":            {"pt": "Total de lotes",          "en": "Total lots"},
    "kpi_avg_discount":     {"pt": "Desconto médio",          "en": "Avg. discount"},
    "kpi_avg_price_ha":     {"pt": "R$/ha médio (leilão)",   "en": "Avg. R$/ha (auction)"},
    "chart_scatter":        {"pt": "Preço Leilão vs. Valor de Mercado (R$)",
                             "en": "Auction Price vs. Market Value (R$)"},
    "chart_scatter_insuff": {"pt": "Dados insuficientes para o gráfico.",
                             "en": "Insufficient data for chart."},
    "chart_by_state":       {"pt": "Distribuição por Estado", "en": "Distribution by State"},
    "chart_by_type":        {"pt": "Distribuição por Tipo de Terra",
                             "en": "Distribution by Land Type"},
    "col_state":            {"pt": "Estado",                  "en": "State"},
    "col_lots":             {"pt": "Lotes",                   "en": "Lots"},
    "col_type":             {"pt": "Tipo",                    "en": "Type"},

    # ── Lots tab — filters ────────────────────────────────────────────────────
    "filters_header":       {"pt": "Filtros",                 "en": "Filters"},
    "filter_state":         {"pt": "Estado (UF)",             "en": "State (UF)"},
    "filter_all":           {"pt": "Todos",                   "en": "All"},
    "filter_land_type":     {"pt": "Tipo de Terra",           "en": "Land Type"},
    "filter_modality":      {"pt": "Modalidade",              "en": "Modality"},
    "filter_modality_help": {"pt": "Judicial · Extrajudicial · Venda Direta",
                             "en": "Judicial · Extrajudicial · Direct Sale"},
    "filter_price_range":   {"pt": "Preço leilão (R$)",       "en": "Auction price (R$)"},
    "filter_ha_range":      {"pt": "Hectares",                "en": "Hectares"},
    "filter_ha_bucket":     {"pt": "Tamanho (ha)",            "en": "Size (ha)"},
    "filter_date_from":     {"pt": "Data leilão — de",        "en": "Auction date — from"},
    "filter_date_to":       {"pt": "Data leilão — até",       "en": "Auction date — to"},

    # ── Lots tab — sort/group ─────────────────────────────────────────────────
    "sort_group_header":    {"pt": "Ordenação e Agrupamento", "en": "Sort & Group"},
    "sort_by":              {"pt": "Ordenar por",             "en": "Sort by"},
    "sort_discount":        {"pt": "Desconto ao mercado %",   "en": "Market discount %"},
    "sort_price":           {"pt": "Preço do leilão",         "en": "Auction price"},
    "sort_price_ha":        {"pt": "R$/ha no leilão",         "en": "R$/ha at auction"},
    "sort_hectares":        {"pt": "Tamanho (hectares)",      "en": "Size (hectares)"},
    "sort_date":            {"pt": "Data do leilão",          "en": "Auction date"},
    "sort_state":           {"pt": "Estado (UF)",             "en": "State (UF)"},
    "sort_land_type":       {"pt": "Tipo de terra",           "en": "Land type"},
    "sort_dir":             {"pt": "Direção",                 "en": "Direction"},
    "sort_desc":            {"pt": "Maior → Menor",           "en": "High → Low"},
    "sort_asc":             {"pt": "Menor → Maior",           "en": "Low → High"},
    "group_by":             {"pt": "Agrupar por (visualização resumida)",
                             "en": "Group by (summary view)"},
    "group_none":           {"pt": "— sem agrupamento —",     "en": "— no grouping —"},
    "group_state":          {"pt": "Estado (UF)",             "en": "State (UF)"},
    "group_land_type":      {"pt": "Tipo de Terra",           "en": "Land Type"},
    "group_modality":       {"pt": "Modalidade de Leilão",    "en": "Auction Modality"},

    # ── Active filter chips ───────────────────────────────────────────────────
    "chip_uf":              {"pt": "UF:",                     "en": "State:"},
    "chip_type":            {"pt": "Tipo:",                   "en": "Type:"},
    "chip_modality":        {"pt": "Modalidade:",             "en": "Modality:"},
    "chip_from":            {"pt": "De",                      "en": "From"},
    "chip_to":              {"pt": "Até",                     "en": "To"},
    "active_filters":       {"pt": "Filtros ativos:",         "en": "Active filters:"},
    "lots_label":           {"pt": "lotes",                   "en": "lots"},
    "no_active_filters":    {"pt": "sem filtros ativos",      "en": "no active filters"},

    # ── Grouped summary ───────────────────────────────────────────────────────
    "grouped_header":       {"pt": "Resumo Agrupado",         "en": "Grouped Summary"},
    "grp_col_state":        {"pt": "Estado",                  "en": "State"},
    "grp_col_land_type":    {"pt": "Tipo de Terra",           "en": "Land Type"},
    "grp_col_modality":     {"pt": "Modalidade",              "en": "Modality"},
    "grp_lots":             {"pt": "Lotes",                   "en": "Lots"},
    "grp_avg_discount":     {"pt": "Desconto Médio",          "en": "Avg Discount"},
    "grp_avg_price":        {"pt": "Preço Médio (R$)",        "en": "Avg Price (R$)"},
    "grp_avg_ha":           {"pt": "R$/ha Médio",             "en": "Avg R$/ha"},
    "grp_avg_hectares":     {"pt": "Hectares Médio",          "en": "Avg Hectares"},

    # ── Export ────────────────────────────────────────────────────────────────
    "export_header":        {"pt": "Exportar",                "en": "Export"},
    "export_lots_btn":      {"pt": "Exportar para Excel (2 abas)",
                             "en": "Export to Excel (2 sheets)"},
    "export_lots_caption":  {"pt": "lotes + aba Benchmarks", "en": "lots + Benchmarks sheet"},
    "export_no_lots":       {"pt": "Nenhum lote para exportar com os filtros atuais.",
                             "en": "No lots to export with current filters."},
    "export_grp_btn":       {"pt": "Exportar Resumo Agrupado",
                             "en": "Export Grouped Summary"},
    "csv_col_starred":      {"pt": "Salvo",                    "en": "Starred"},
    "csv_starred_mark":     {"pt": "★ Sim",                    "en": "★ Yes"},
    "col_sp_ref":       {"pt": "Referência S&P",           "en": "S&P Reference"},
    "sp_ref_mun":       {"pt": "Município",                "en": "Municipality"},
    "sp_ref_reg":       {"pt": "Região",                   "en": "Region"},
    "sp_ref_state":     {"pt": "Média estadual (UF)",      "en": "State average (fallback)"},

    # ── Lots table ────────────────────────────────────────────────────────────
    "lots_table_header":    {"pt": "Lotes",                   "en": "Lots"},
    "lots_results":         {"pt": "resultados",              "en": "results"},
    "lots_no_match":        {"pt": "Nenhum lote corresponde aos filtros selecionados.",
                             "en": "No lots match the selected filters."},
    "col_property":         {"pt": "Propriedade",             "en": "Property"},
    "col_uf":               {"pt": "UF",                      "en": "UF"},
    "col_city":             {"pt": "Cidade",                  "en": "City"},
    "col_land_type_short":  {"pt": "Tipo",                    "en": "Type"},
    "col_round":            {"pt": "Praça",                   "en": "Round"},
    "col_hectares":         {"pt": "Hectares",                "en": "Hectares"},
    "col_starred":          {"pt": "Salvo",                   "en": "Starred"},
    "filter_starred":       {"pt": "Apenas salvos",           "en": "Starred only"},
    "new_lots_header":      {"pt": "🆕 Novos lotes desta busca", "en": "🆕 New lots this search"},
    "price_changes_header": {"pt": "💰 Atualizações de preço",   "en": "💰 Price updates"},
    "no_new_lots":          {"pt": "Nenhum lote novo nesta busca.", "en": "No new lots this search."},
    "no_price_changes":     {"pt": "Nenhuma mudança de preço nesta busca.", "en": "No price changes this search."},
    "col_old_price":        {"pt": "Preço anterior",          "en": "Previous price"},
    "col_new_price":        {"pt": "Preço atual",             "en": "Current price"},
    "col_auction_price":    {"pt": "Preço Leilão",            "en": "Auction Price"},
    "col_price_ha":         {"pt": "R$/ha Leilão",            "en": "R$/ha Auction"},
    "col_market_val":       {"pt": "Val. Mercado",            "en": "Market Value"},
    "col_sp_low":           {"pt": "S&P R$/ha Baixa",        "en": "S&P R$/ha Low"},
    "col_sp_mid":           {"pt": "S&P R$/ha Médio",        "en": "S&P R$/ha Mid"},
    "col_sp_high":          {"pt": "S&P R$/ha Alta",         "en": "S&P R$/ha High"},
    "sp_state_fallback":    {"pt": "⚠️ Estimado pela média estadual ({uf}) — município não encontrado na base S&P",
                             "en": "⚠️ Estimated from {uf} state-wide average — município not in S&P database"},
    "sp_col_help":          {"pt": "Quando o município não consta na base S&P, o valor exibido é a média estadual (veja o aviso ⚠️ no painel de detalhes do lote).",
                             "en": "When the município isn't in the S&P database, the value shown is a state-wide average instead (see the ⚠️ note in the lot's detail panel)."},
    "col_discount":         {"pt": "Desc. S&P %",             "en": "Disc. S&P %"},
    "col_date":             {"pt": "Próximo Leilão",           "en": "Next Auction"},
    "col_date_r1":          {"pt": "1ª Praça Data",           "en": "Round 1 Date"},
    "col_price_r1":         {"pt": "Preço Praça Anterior",    "en": "Prev. Round Price"},
    "col_date_r2":          {"pt": "2ª Praça Data",           "en": "Round 2 Date"},
    "col_price_r2":         {"pt": "2ª Praça Preço",          "en": "Round 2 Price"},
    "col_preco_r1":         {"pt": "Preço 1ª Praça",          "en": "Price Round 1"},
    "col_pha_r1":           {"pt": "R$/ha 1ª Praça",          "en": "R$/ha Round 1"},
    "col_desc_r1":          {"pt": "Desconto 1ª Praça",       "en": "Discount Round 1"},
    "col_preco_r2":         {"pt": "Preço 2ª Praça",          "en": "Price Round 2"},
    "col_pha_r2":           {"pt": "R$/ha 2ª Praça",          "en": "R$/ha Round 2"},
    "col_desc_r2":          {"pt": "Desconto 2ª Praça",       "en": "Discount Round 2"},
    "col_site_appraisal":   {"pt": "Val. Avaliação (Site)",   "en": "Site Appraisal"},
    "col_modality":         {"pt": "Modalidade",              "en": "Modality"},
    "col_url":              {"pt": "URL",                     "en": "URL"},
    "col_partial":          {"pt": "Parte",                   "en": "Partial"},

    # ── Detail panel ──────────────────────────────────────────────────────────
    "detail_prefix":        {"pt": "Detalhes:",               "en": "Details:"},
    "detail_auction_price": {"pt": "Preço Leilão",            "en": "Auction Price"},
    "detail_market_mid":    {"pt": "Valor Mercado (médio)",   "en": "Market Value (mid)"},
    "detail_discount":      {"pt": "Desconto ao Mercado",     "en": "Market Discount"},
    "detail_size":          {"pt": "Tamanho",                 "en": "Size"},
    "detail_price_ha_auc":  {"pt": "R$/ha no Leilão",        "en": "R$/ha at Auction"},
    "detail_price_ha_mkt":  {"pt": "R$/ha Mercado (médio)",  "en": "R$/ha Market (mid)"},
    "detail_state":         {"pt": "Estado",                  "en": "State"},
    "detail_land_type":     {"pt": "Tipo de Terra",           "en": "Land Type"},
    "detail_auction_date":  {"pt": "Data do Leilão",          "en": "Auction Date"},
    "detail_disc_comp":     {"pt": "Desconto (60 pts)",       "en": "Discount (60 pts)"},
    "detail_cert_comp":     {"pt": "Dados (20 pts)",          "en": "Data (20 pts)"},
    "detail_urg_comp":      {"pt": "Urgência (10 pts)",       "en": "Urgency (10 pts)"},
    "detail_conf_comp":     {"pt": "Confiança (10 pts)",      "en": "Confidence (10 pts)"},
    "detail_open_link":     {"pt": "Abrir no megaleiloes.com.br",
                             "en": "Open on megaleiloes.com.br"},

    # ── Benchmarks tab ────────────────────────────────────────────────────────
    "bench_header":         {"pt": "Tabela de Referência S&P — Preço da Terra (R$/ha)",
                             "en": "S&P Reference Table — Land Price (R$/ha)"},
    "bench_caption":        {"pt": ("Preços de mercado por município e tipo de uso, conforme LAND-BRZ25 "
                                    "(S&P Global Market Intelligence, Jan-Mar 2025). "
                                    "Valores em R$/ha, terra nua, referência março/2025."),
                             "en": ("Market prices by municipality and land use type, from LAND-BRZ25 "
                                    "(S&P Global Market Intelligence, Jan-Mar 2025). "
                                    "Values in R$/ha, bare land, March 2025 reference.")},
    "bench_filter_region":  {"pt": "Região",                  "en": "Region"},
    "bench_all_regions":    {"pt": "Todas as regiões",        "en": "All regions"},
    "bench_filter_state":   {"pt": "Estado (UF)",             "en": "State (UF)"},
    "bench_all_states":     {"pt": "Todos",                   "en": "All"},
    "bench_filter_type":    {"pt": "Tipo de Terra",           "en": "Land Type"},
    "bench_all_types":      {"pt": "Todos os tipos",          "en": "All types"},
    "bench_combos":         {"pt": "entradas município × tipo de terra",
                             "en": "municipality × land type entries"},
    "bench_col_region":     {"pt": "Região",                  "en": "Region"},
    "bench_col_uf":         {"pt": "UF",                      "en": "UF"},
    "bench_col_state":      {"pt": "Estado",                  "en": "State"},
    "bench_col_mun":        {"pt": "Município",               "en": "Municipality"},
    "bench_col_type":       {"pt": "Tipo de Terra",           "en": "Land Type"},
    "bench_col_low":        {"pt": "R$/ha Baixa Cap.",        "en": "R$/ha Low Cap."},
    "bench_col_mid":        {"pt": "R$/ha Médio",             "en": "R$/ha Mid"},
    "bench_col_high":       {"pt": "R$/ha Alta Cap.",         "en": "R$/ha High Cap."},
    "bench_col_row_type":   {"pt": "Tipo de Referência",      "en": "Reference Type"},
    "bench_row_type_mun":   {"pt": "Município",               "en": "Municipality"},
    "bench_row_type_state": {"pt": "Média Estadual",          "en": "State Average"},
    "bench_state_avg_note": {"pt": ("Linhas em cinza (\"— Média estadual —\") mostram a média de todo o "
                                    "estado para aquele tipo de terra — é o valor usado como referência "
                                    "quando o município de um lote não consta na base S&P."),
                             "en": ("Grey rows (\"— Média estadual —\") show the state-wide average for "
                                    "that land type — this is the figure used as a fallback reference "
                                    "whenever a lot's município isn't in the S&P database.")},
    "bench_export_btn":     {"pt": "Exportar Referência S&P para Excel",
                             "en": "Export S&P Reference to Excel"},
    "bench_export_caption": {"pt": "Exporta {n} linhas com os filtros ativos.",
                             "en": "Exports {n} rows with active filters."},
    "bench_methodology":    {"pt": "Metodologia e Fontes",    "en": "Methodology & Sources"},
    "bench_method_body_pt": {"pt": """
**Fontes dos dados de referência:**
- **FNP Consultoria & Agroinformativos** — Agrianual e Anualpec (publicações anuais de preço de terra)
- **INCRA** — Tabelas de valor de referência da terra para fins de tributação (ITR 2023/2024)
- **EMBRAPA** — Levantamentos regionais de valor da terra agrícola
- **Scot Consultoria** — Mercado de pastagens e pecuária bovina

**Critérios:**
- Valores em R$/ha, sem benfeitorias (terra nua)
- Faixa *baixa*: percentil ~25 da região; *média*: mediana regional; *alta*: percentil ~75
- Estimativas conservadoras para análise de ativos distressed em leilão judicial/extrajudicial
- Última revisão: 2024

**Tipos de terra:**
| Tipo | Descrição |
|---|---|
| Soja | Lavoura de grãos de alta produtividade (Cerrado e Sul) |
| Cana-de-açúcar | Canavial próximo a usinas (SP, PR, MG, GO) |
| Café | Lavoura cafeeira (MG, SP, ES, PR) |
| Arroz | Várzea irrigada (RS, MT, PA) |
| Fruticultura | Pomares e culturas permanentes |
| Pastagem | Pastagem melhorada / pecuária bovina |
| Mata / Reserva | Vegetação nativa / reserva legal / potencial madeireiro |
| Misto / Outros | Propriedades mistas ou tipo não identificado |
""", "en": ""},  # EN version below
    "bench_method_body_en": {"pt": "", "en": """
**Reference data sources:**
- **FNP Consultoria & Agroinformativos** — Agrianual and Anualpec (annual land price publications)
- **INCRA** — Land reference value tables for rural property tax (ITR 2023/2024)
- **EMBRAPA** — Regional agricultural land value surveys
- **Scot Consultoria** — Pasture and cattle market reports

**Methodology:**
- Values in R$/ha, bare land only (no improvements)
- *Low* band: ~25th percentile for the region; *Mid*: regional median; *High*: ~75th percentile
- Conservative estimates for distressed-asset analysis (judicial/extrajudicial auctions)
- Last revised: 2024

**Land types:**
| Type | Description |
|---|---|
| Soja (Soy) | High-productivity grain cropland (Cerrado & South) |
| Cana-de-açúcar (Sugarcane) | Sugarcane land near mills (SP, PR, MG, GO) |
| Café (Coffee) | Coffee farming land (MG, SP, ES, PR) |
| Arroz (Rice) | Irrigated lowland rice (RS, MT, PA) |
| Fruticultura (Orchards) | Permanent crops and fruit orchards |
| Pastagem (Pasture) | Improved pasture / cattle ranching |
| Mata / Reserva (Forest) | Native forest / legal reserve / timber potential |
| Misto / Outros (Mixed) | Mixed-use or unclassified rural property |
"""},

    # ── Excel column headers (bilingual) ──────────────────────────────────────
    "xl_property":          {"pt": "Propriedade",             "en": "Property"},
    "xl_uf":                {"pt": "UF",                      "en": "UF"},
    "xl_city":              {"pt": "Cidade",                  "en": "City"},
    "xl_land_type":         {"pt": "Tipo de Terra",           "en": "Land Type"},
    "xl_hectares":          {"pt": "Hectares",                "en": "Hectares"},
    "xl_auction_price":     {"pt": "Preço Leilão (R$)",       "en": "Auction Price (R$)"},
    "xl_price_ha_auc":      {"pt": "R$/ha Leilão",            "en": "R$/ha Auction"},
    "xl_mkt_mid":           {"pt": "Val. Mercado Médio (R$)", "en": "Market Value Mid (R$)"},
    "xl_mkt_low":           {"pt": "Val. Mercado Baixo (R$)", "en": "Market Value Low (R$)"},
    "xl_mkt_high":          {"pt": "Val. Mercado Alto (R$)",  "en": "Market Value High (R$)"},
    "xl_price_ha_mkt":      {"pt": "R$/ha Mercado (médio)",  "en": "R$/ha Market (mid)"},
    "xl_sp_low":            {"pt": "S&P Ref. Baixa Cap. (R$)", "en": "S&P Ref. Low Cap. (R$)"},
    "xl_sp_mid":            {"pt": "S&P Ref. Médio (R$)",    "en": "S&P Ref. Mid (R$)"},
    "xl_sp_high":           {"pt": "S&P Ref. Alta Cap. (R$)","en": "S&P Ref. High Cap. (R$)"},
    "xl_discount":          {"pt": "Desconto ao Mercado (%)", "en": "Market Discount (%)"},
    "xl_auction_date":      {"pt": "Data do Leilão",          "en": "Auction Date"},
    "xl_modality":          {"pt": "Modalidade",              "en": "Modality"},
    "xl_lot_id":            {"pt": "ID Lote",                 "en": "Lot ID"},
    "xl_url":               {"pt": "URL",                     "en": "URL"},
    "xl_source":            {"pt": "Fonte",                   "en": "Source"},
    "xl_sheet_lots":        {"pt": "Oportunidades",           "en": "Opportunities"},
    "xl_sheet_bench":       {"pt": "Benchmarks",              "en": "Benchmarks"},
    "xl_sheet_grouped":     {"pt": "Agrupado",                "en": "Grouped"},
}


def t(key: str) -> str:
    """Return the string for the current language."""
    lang = st.session_state.get("lang", "pt")
    return STRINGS[key][lang]


# ─────────────────────────────────────────────────────────────────────────────
# Page config  (must come before any other st calls)
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Fazenda Radar",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
section[data-testid="stSidebar"] { background: var(--secondary-background-color); }
.stButton>button, .stDownloadButton>button {
    background: #1a7f4b; color: white; font-weight: 600;
    border-radius: 6px; border: none; padding: 9px 22px;
}
.stButton>button:hover, .stDownloadButton>button:hover { background: #156038; }
.chip { display:inline-block; background:#e8f5ee; color:#1a7f4b;
        border:1px solid #aad6bc; border-radius:999px;
        padding:2px 10px; font-size:0.78rem; margin:2px; }
h3.section { color:#1a7f4b; border-bottom:2px solid #d0ead9;
             padding-bottom:4px; margin-top:0; }
</style>
""", unsafe_allow_html=True)

@st.cache_resource
def _get_server_run_id() -> str:
    """Returns a stable ID for this server process lifetime."""
    return str(uuid.uuid4())

_SERVER_RUN_ID = _get_server_run_id()

# ─────────────────────────────────────────────────────────────────────────────
# Server-instance guard — clears stale action flags on browser reconnect
# ─────────────────────────────────────────────────────────────────────────────
if st.session_state.get("_server_run_id") != _SERVER_RUN_ID:
    st.session_state["_server_run_id"] = _SERVER_RUN_ID
    st.session_state["scraping"] = False

# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
if "last_scraped" not in st.session_state:
    st.session_state.last_scraped: str = ""
if "scraping" not in st.session_state:
    st.session_state.scraping: bool = False
if "lang" not in st.session_state:
    st.session_state.lang: str = "pt"
if "selected_sources" not in st.session_state:
    st.session_state.selected_sources: list[str] = ["sbid", "lj", "lim", "mega", "gl", "lb", "lvip"]
if "starred" not in st.session_state:
    st.session_state.starred: set[str] = _load_stars()
if "src_seen_ids" not in st.session_state:
    st.session_state.src_seen_ids = set()
# Persistent rolling store: {lot_id: listing}. Loaded once per session so the
# table is repopulated from disk on browser refresh without re-scraping.
def _valid_lot(l: dict) -> bool:
    """Return False for lots that are almost certainly mis-parsed."""
    ha = l.get("hectares")
    price = l.get("auction_price")
    if ha is not None and ha < 0.4:
        return False
    if ha and price and price / ha < 100:
        return False
    return True

if "listings_store" not in st.session_state:
    raw_store = load_store()
    st.session_state.listings_store: dict[str, dict] = {
        lid: l for lid, l in raw_store.items() if _valid_lot(l)
    }
if "all_listings" not in st.session_state:
    st.session_state.all_listings = list(st.session_state.listings_store.values())
# Last-search summary — reload the persisted delta so the new-lots / price-
# update sections survive a browser refresh, not just the in-memory session.
if "last_new_lots" not in st.session_state:
    _delta = load_last_search()
    st.session_state.last_new_lots: list[dict] = _delta.get("new_lots", [])
    st.session_state.last_price_changes: list[dict] = _delta.get("price_changes", [])

# Initialise checkbox states on first load so widgets render correctly from the start
_all_src_keys = ["sbid", "lj", "lim", "mega", "gl", "lb", "lvip"]
if "src_all_cb" not in st.session_state:
    st.session_state["src_all_cb"] = True
    st.session_state["_prev_all_cb"] = True
    for _k in _all_src_keys:
        st.session_state[f"src_{_k}_cb"] = True

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def fmt_brl(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return "R$ {:,.0f}".format(v)

def fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+.1f}%"

def fmt_ha(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if v < 0.01:
        return "<0.01 ha"
    return "{:,.2f} ha".format(v)

def safe(v, default="—"):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    return v

def _fmt_round(row) -> str:
    """Format auction round as e.g. '2ª' or '1ª' or '—'."""
    r = row.get("active_round")
    if not r:
        return "—"
    return f"{r}ª"


def build_df(listings: list[dict]) -> pd.DataFrame:
    if not listings:
        return pd.DataFrame()
    # Drop any lot where hectares is known and below 0.4 — catches cases where
    # the scraper-level filter was bypassed (e.g. Apify enrichment filled in a
    # sub-0.4 value after the scraper already let the lot through as hectares=None).
    listings = [
        l for l in listings
        if l.get("hectares") is None or l["hectares"] >= 0.4
    ]
    if not listings:
        return pd.DataFrame()
    df = pd.DataFrame(listings)
    for col in [
        "property_name", "state", "city", "land_type", "hectares",
        "auction_price", "auction_price_per_ha", "market_value_mid",
        "market_value_low", "market_value_high", "price_per_ha_mid",
        "discount_to_mid_pct", "auction_date", "auction_type",
        "listing_url", "lot_id", "source",
        "active_round", "total_rounds", "appraised_value",
        "date_round1", "price_round1", "date_round2", "price_round2",
        "price_per_ha_round1", "discount_round1_pct",
        "price_per_ha_round2", "discount_round2_pct",
        "site_appraised_value", "is_partial",
    ]:
        if col not in df.columns:
            df[col] = None
    # Fill site_appraised_value with price_round1 when not provided by the scraper
    mask = df["site_appraised_value"].isna() & df["price_round1"].notna()
    df.loc[mask, "site_appraised_value"] = df.loc[mask, "price_round1"]

    # Compute display field
    df["round_display"] = df.apply(_fmt_round, axis=1)
    # Mark past rounds as "Encerrada"
    today = pd.Timestamp.today().normalize()
    def _mark_past(d):
        if not d or pd.isna(d):
            return d
        try:
            return "Encerrada" if pd.Timestamp(d) < today else d
        except Exception:
            return d
    df["date_round1_disp"] = df["date_round1"].apply(_mark_past)
    df["date_round2_disp"] = df["date_round2"].apply(_mark_past)

    # Starred column — True if lot_id is in the current starred set
    starred: set[str] = st.session_state.get("starred", set())
    df["starred"] = df["lot_id"].apply(lambda lid: bool(lid and lid in starred))
    return df

def _autosize(ws):
    from openpyxl.utils import get_column_letter
    for i, col in enumerate(ws.columns, 1):
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[get_column_letter(i)].width = min(max_len + 4, 60)

def to_excel_listings(df: pd.DataFrame, bench_df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    col_order = [
        "starred", "property_name", "state", "city", "land_type",
        "auction_date", "hectares", "auction_price",
        "auction_price_per_ha", "discount_to_mid_pct",
        "round_display", "date_round1", "price_round1", "date_round2", "price_round2",
        "site_appraised_value",
        "sp_price_per_ha_low", "sp_price_per_ha_mid", "sp_price_per_ha_high",
        "sp_match_level",
        "auction_type", "lot_id", "listing_url", "source",
    ]
    labels = {
        "starred": t("csv_col_starred"),
        "property_name": t("xl_property"), "state": t("xl_uf"), "city": t("xl_city"),
        "land_type": t("xl_land_type"), "hectares": t("xl_hectares"),
        "auction_price": t("xl_auction_price"), "auction_price_per_ha": t("xl_price_ha_auc"),
        "discount_to_mid_pct": t("xl_discount"),
        "round_display": t("col_round"),
        "date_round1": t("col_date_r1"), "price_round1": t("col_price_r1"),
        "date_round2": t("col_date_r2"), "price_round2": t("col_price_r2"),
        "site_appraised_value": t("col_site_appraisal"),
        "sp_price_per_ha_low": t("xl_sp_low"),
        "sp_price_per_ha_mid": t("xl_sp_mid"),
        "sp_price_per_ha_high": t("xl_sp_high"),
        "sp_match_level": t("col_sp_ref"),
        "auction_date": t("xl_auction_date"),
        "auction_type": t("xl_modality"), "lot_id": t("xl_lot_id"),
        "listing_url": t("xl_url"), "source": t("xl_source"),
    }
    sp_ref_labels = {
        "municipio": t("sp_ref_mun"),
        "regiao":    t("sp_ref_reg"),
        "estado":    t("sp_ref_state"),
    }
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        exp = df[[c for c in col_order if c in df.columns]].copy()
        if "starred" in exp.columns:
            exp["starred"] = exp["starred"].apply(
                lambda v: t("csv_starred_mark") if bool(v) else ""
            )
        if "sp_match_level" in exp.columns:
            exp["sp_match_level"] = exp["sp_match_level"].map(sp_ref_labels).fillna("")
        exp.rename(columns=labels, inplace=True)
        exp.to_excel(writer, index=False, sheet_name=t("xl_sheet_lots"))
        _autosize(writer.sheets[t("xl_sheet_lots")])

        bexp = _prep_bench_export(bench_df)
        bexp.to_excel(writer, index=False, sheet_name=t("xl_sheet_bench"))
        _autosize(writer.sheets[t("xl_sheet_bench")])
    return output.getvalue()


def _prep_bench_export(bench_df: pd.DataFrame) -> pd.DataFrame:
    """Map the S&P reference DataFrame (from sp_reference_table()) to a
    translated, export-ready frame. Column names here must match the real
    columns produced by sp_reference_table(): regiao, uf, state_name,
    municipio, subgrupo, price_baixa, price_mid, price_alta, row_type."""
    bench_cols = [
        "regiao", "uf", "state_name", "municipio", "subgrupo",
        "price_baixa", "price_mid", "price_alta", "row_type",
    ]
    bench_labels = {
        "regiao":      t("bench_col_region"),
        "uf":          t("bench_col_uf"),
        "state_name":  t("bench_col_state"),
        "municipio":   t("bench_col_mun"),
        "subgrupo":    t("bench_col_type"),
        "price_baixa": t("bench_col_low"),
        "price_mid":   t("bench_col_mid"),
        "price_alta":  t("bench_col_high"),
        "row_type":    t("bench_col_row_type"),
    }
    row_type_labels = {
        "municipio": t("bench_row_type_mun"),
        "estado":    t("bench_row_type_state"),
    }
    bexp = bench_df[[c for c in bench_cols if c in bench_df.columns]].copy()
    if "row_type" in bexp.columns:
        bexp["row_type"] = bexp["row_type"].map(row_type_labels).fillna(bexp["row_type"])
    bexp.rename(columns=bench_labels, inplace=True)
    return bexp

def to_excel_benchmarks(bench_df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        bexp = _prep_bench_export(bench_df)
        bexp.to_excel(writer, index=False, sheet_name=t("xl_sheet_bench"))
        _autosize(writer.sheets[t("xl_sheet_bench")])
    return output.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Fazenda Radar")
    st.caption(t("app_subtitle"))
    st.divider()

    # Language toggle — PT/EN
    lang_choice = st.radio(
        t("language_label"),
        options=["Português", "English"],
        index=0 if st.session_state.lang == "pt" else 1,
        horizontal=True,
        key="lang_radio",
    )
    st.session_state.lang = "pt" if lang_choice == "Português" else "en"

    st.divider()

    # ── Source selector ───────────────────────────────────────────────────────
    st.markdown(f"**{t('sources_label')}**")

    # (key, domain label, small hint)
    _src_map = [
        ("sbid", "superbid.net",            ""),
        ("lj",   "leiloesjudiciais.com.br", ""),
        ("lim",  "leilaoimovel.com.br",     ""),
        ("mega", "megaleiloes.com.br",      ""),
        ("gl",   "grupolance.com.br",       ""),
        ("lb",   "leilaobrasil.com.br",     ""),
        ("lvip", "leilaovip.com.br",        ""),
    ]
    _all_keys = [k for k, _, _ in _src_map]

    # Sync "Todas" checkbox with individual boxes before rendering
    _prev_all = st.session_state.get("_prev_all_cb", True)
    _cur_all  = st.session_state["src_all_cb"]

    if _prev_all and not _cur_all:
        for k, _, _ in _src_map:
            st.session_state[f"src_{k}_cb"] = False
    elif not _prev_all and _cur_all:
        for k, _, _ in _src_map:
            st.session_state[f"src_{k}_cb"] = True

    st.session_state["_prev_all_cb"] = _cur_all

    _all_checked = st.checkbox(t("src_all"), key="src_all_cb")
    _individual = {}
    for k, lbl, _ in _src_map:
        _individual[k] = st.checkbox(lbl, key=f"src_{k}_cb")

    if _all_checked:
        st.session_state.selected_sources = _all_keys
    else:
        st.session_state.selected_sources = [k for k, v in _individual.items() if v]

    apify_token = st.text_input(
        t("apify_label"),
        type="password",
        help=t("apify_help"),
        key="apify_token",
    )

    if st.button(t("scrape_button"), use_container_width=True):
        if not st.session_state.selected_sources:
            st.warning(t("src_none_warn"))
        else:
            st.session_state.scraping = True
            st.rerun()

    if st.session_state.last_scraped:
        st.caption(f"{t('last_scraped')} {st.session_state.last_scraped}")
        n = len(st.session_state.all_listings)
        st.caption(f"{n} {t('lots_in_memory')}")

    _backend = backend_name()
    if _backend == "supabase":
        st.caption("💾 Armazenamento: Supabase (durável)")
    else:
        st.caption(f"💾 Armazenamento: arquivo local (reinicia no redeploy) — {backend_reason()}")

    st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# Batch scrape helpers
# ─────────────────────────────────────────────────────────────────────────────
#
# Pure page-based fetching. Each source has a page cursor (next page to fetch).
# Per batch, each source is asked for exactly _BASE_PAGES[src] * multiplier pages.
# megaleiloes gets more pages per batch because its rural filter is aggressive
# (~8-15 rural lots per 48-card page vs ~15-20 for other sources).
#
# A source is marked exhausted only when a fetch returns zero results — meaning
# the scraper hit its real last page internally. Load More keeps working as long
# as any source has pages left.

_BASE_PAGES: dict[str, int] = {
    "sbid": 15,  # ~300 rural + ~300 terrain; self-terminates on empty page
    "mega": 5,
    "lb":   5,
    "lim":  5,
    "gl":   5,
    "lj":   5,
    "lvip": 1,   # single-page source
}

def _scrape_source(src_key: str, start_page: int, n_pages: int,
                   apify_token: str) -> list[dict]:
    """Call the right scraper for pages [start_page .. start_page+n_pages-1]."""
    if src_key == "mega":
        return scrape_megaleiloes(max_pages=n_pages, delay=0.5, start_page=start_page)
    elif src_key == "lb":
        return scrape_leilaobrasil(max_pages=n_pages, delay=0.5, start_page=start_page)
    elif src_key == "lim":
        return scrape_leilaoimovel(max_pages=n_pages, delay=0.5,
                                   api_token=apify_token or None, start_page=start_page)
    elif src_key == "gl":
        return scrape_grupolance(max_pages=n_pages, delay=0.5, start_page=start_page)
    elif src_key == "lj":
        return scrape_leiloesjudiciais(max_pages=n_pages, delay=0.5, start_page=start_page, max_results=50)
    elif src_key == "lvip":
        return scrape_leilaovip(max_pages=n_pages, delay=0.5, start_page=start_page)
    elif src_key == "sbid":
        return scrape_superbid(max_pages=n_pages, delay=0.5, start_page=start_page)
    return []


def _run_batch(active_sources: list[str], apify_token: str) -> None:
    """Full scrape: fetch all pages from all sources, then merge into the store.

    Previously-displayed listings are preserved if the scrape yields nothing —
    the merge only happens once we actually have results.
    """
    st.session_state.src_seen_ids = set()
    seen_ids: set = st.session_state.src_seen_ids

    new_raw: list[dict] = []

    with st.spinner(t("scraping_spinner")):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        tasks = {
            src_key: (1, _BASE_PAGES.get(src_key, 1))
            for src_key in active_sources
        }

        futures = {}
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            for src_key, (start, n_pages) in tasks.items():
                futures[executor.submit(
                    _scrape_source, src_key, start, n_pages, apify_token
                )] = src_key

        results: dict[str, list] = {}
        for future in as_completed(futures):
            src_key = futures[future]
            try:
                fetched = future.result()
            except Exception as exc:
                st.toast(f"{src_key}: {exc}", icon="⚠️")
                continue
            if fetched:
                results[src_key] = fetched

        for src_key in active_sources:
            for item in results.get(src_key, []):
                key = item.get("lot_id") or item.get("listing_url", "")
                if key and key not in seen_ids:
                    seen_ids.add(key)
                    new_raw.append(item)

    st.session_state.src_seen_ids = seen_ids

    if not new_raw:
        return

    # Apify hectares enrichment on the new batch only
    _token = apify_token.strip()
    _missing = sum(1 for l in new_raw if l.get("hectares") is None)
    if _token and _missing:
        with st.spinner(t("apify_enriching")):
            try:
                new_raw = enrich_hectares(new_raw, api_token=_token)
            except Exception as apify_exc:
                st.toast(f"Apify: {apify_exc}", icon="⚠️")

    # Drop listings that are too small or whose hectares could not be identified.
    # A lot is only kept when it has a known area of at least 0.4 ha — applies to
    # every source, since all listings flow through here before display.
    new_raw = [l for l in new_raw if l.get("hectares") is not None and l["hectares"] >= 0.4]

    # Drop lots where price-per-hectare < 100 R$/ha — this is a near-certain signal
    # that the hectares value is wrong (e.g. m²→ha mis-conversion producing a
    # 5 000 ha farm priced at 10 R$/ha).  Real rural land in Brazil never trades
    # below ~1 000 R$/ha; 100 R$/ha is a very conservative floor.
    new_raw = [
        l for l in new_raw
        if not (
            l.get("hectares") and l.get("auction_price")
            and l["auction_price"] / l["hectares"] < 100
        )
    ]

    if not new_raw:
        return

    scored_new = score_all(new_raw)

    # Merge into the persistent rolling store: detect new lots & price changes,
    # retain previously-seen lots, then auto-save (no manual baseline button).
    merged, new_lots, price_changes, store = merge_scrape(
        scored_new, st.session_state.listings_store
    )
    st.session_state.listings_store = store
    save_store(store)
    st.session_state.all_listings = merged
    st.session_state.last_new_lots = new_lots
    st.session_state.last_price_changes = price_changes
    st.session_state.last_scraped = datetime.now().strftime("%d/%m/%Y %H:%M")

    # Persist the delta so the summary sections survive a browser refresh.
    save_last_search({
        "scraped_at": st.session_state.last_scraped,
        "new_lots": [{"lot_id": l.get("lot_id"),
                      "property_name": l.get("property_name")} for l in new_lots],
        "price_changes": [{"lot_id": l.get("lot_id"),
                           "property_name": l.get("property_name"),
                           "old_price": l.get("old_price"),
                           "new_price": l.get("new_price")} for l in price_changes],
    })

    st.success(
        f"{len(new_lots)} novos · {len(price_changes)} atualizações de preço "
        f"· {len(merged)} total"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scrape trigger
# ─────────────────────────────────────────────────────────────────────────────
_apify_token = (apify_token or "").strip()
_active_srcs = st.session_state.selected_sources

if st.session_state.scraping:
    st.session_state.scraping = False
    try:
        _run_batch(_active_srcs, _apify_token)
    except Exception as exc:
        st.error(f"{t('scrape_error')} {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# S&P reference table (cached, always available)
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data
def get_sp_ref_df() -> pd.DataFrame:
    return sp_reference_table()

bench_df = get_sp_ref_df()

df_all = build_df(st.session_state.all_listings)


# ─────────────────────────────────────────────────────────────────────────────
# Shared lots-table builder — used by the main table and the new-lots /
# price-update sections so all three show the exact same full column set.
# Returns (styled_disp, column_config, st_col, disp).
# ─────────────────────────────────────────────────────────────────────────────
def _build_lots_display(df: pd.DataFrame):
    disp_cols = [
        "starred",
        "property_name", "state", "city", "land_type",
        "auction_date", "hectares", "is_partial",
        "round_display",
        "price_round1", "price_per_ha_round1", "discount_round1_pct",
        "price_round2", "price_per_ha_round2", "discount_round2_pct",
        "sp_price_per_ha_low", "sp_price_per_ha_mid", "sp_price_per_ha_high",
        "sp_match_level",
        "auction_type", "listing_url",
    ]
    disp = df[[c for c in disp_cols + ["lot_id"] if c in df.columns]].copy()

    # Município-matched rows first (most reliable), state-average last.
    if "sp_match_level" in disp.columns:
        _match_priority = {"municipio": 0, "regiao": 1, "estado": 2}
        disp["_sort_key"] = disp["sp_match_level"].map(_match_priority).fillna(3)
        disp = disp.sort_values("_sort_key", kind="stable").drop(columns="_sort_key")

    if "sp_match_level" in disp.columns:
        _sp_ref_labels = {
            "municipio": t("sp_ref_mun"),
            "regiao":    t("sp_ref_reg"),
            "estado":    t("sp_ref_state"),
        }
        disp["sp_match_level"] = disp["sp_match_level"].map(_sp_ref_labels).fillna("")

    disp.rename(columns={
        "starred": t("col_starred"),
        "property_name": t("col_property"), "state": t("col_uf"),
        "city": t("col_city"), "land_type": t("col_land_type_short"),
        "round_display": t("col_round"),
        "hectares": t("col_hectares"), "is_partial": t("col_partial"),
        "auction_date": t("col_date"),
        "price_round1":        t("col_preco_r1"),
        "price_per_ha_round1": t("col_pha_r1"),
        "discount_round1_pct": t("col_desc_r1"),
        "price_round2":        t("col_preco_r2"),
        "price_per_ha_round2": t("col_pha_r2"),
        "discount_round2_pct": t("col_desc_r2"),
        "sp_price_per_ha_low": t("col_sp_low"),
        "sp_price_per_ha_mid": t("col_sp_mid"),
        "sp_price_per_ha_high": t("col_sp_high"),
        "sp_match_level": t("col_sp_ref"),
        "auction_type": t("col_modality"), "listing_url": t("col_url"),
    }, inplace=True)

    st_col = t("col_starred")
    pr   = t("col_property")
    uf   = t("col_uf");    ci  = t("col_city");  lt = t("col_land_type_short")
    rd   = t("col_round")
    ha   = t("col_hectares"); pt = t("col_partial"); dt = t("col_date")
    pr1  = t("col_preco_r1"); pha1 = t("col_pha_r1"); dc1 = t("col_desc_r1")
    pr2  = t("col_preco_r2"); pha2 = t("col_pha_r2"); dc2 = t("col_desc_r2")
    spl  = t("col_sp_low"); spm = t("col_sp_mid"); sph = t("col_sp_high")
    spr  = t("col_sp_ref")
    mo   = t("col_modality"); ur = t("col_url")

    def _row_style(row):
        is_starred  = bool(row.get(st_col))
        match_label = row.get(spr, "")
        if is_starred:
            style = "background-color: #d6f5d6; color: #1a1a1a"   # green  — starred
        elif match_label == t("sp_ref_state"):
            style = "background-color: #e4e4e4; color: #1a1a1a"   # grey   — state avg
        elif match_label == t("sp_ref_reg"):
            style = "background-color: #fff8e1; color: #1a1a1a"   # yellow — region avg
        else:
            style = ""
        return [style] * len(row)

    styled_disp = disp.style.apply(_row_style, axis=1)

    column_config = {
        st_col: st.column_config.CheckboxColumn(st_col, width=50),
        pr:    st.column_config.TextColumn(pr, width="large"),
        uf:    st.column_config.TextColumn(uf, width=55),
        ci:    st.column_config.TextColumn(ci, width="small"),
        lt:    st.column_config.TextColumn(lt, width="small"),
        rd:    st.column_config.TextColumn(rd, width=70),
        ha:    st.column_config.NumberColumn(ha, format="%,.2f ha"),
        pt:    st.column_config.CheckboxColumn(pt, width=60, help="Parte ideal / fração ideal — only a share of the property is being sold"),
        pr1:   st.column_config.NumberColumn(pr1,  format="R$ %,.0f"),
        pha1:  st.column_config.NumberColumn(pha1, format="R$ %,.0f"),
        dc1:   st.column_config.NumberColumn(dc1,  format="%.1f%%"),
        pr2:   st.column_config.NumberColumn(pr2,  format="R$ %,.0f"),
        pha2:  st.column_config.NumberColumn(pha2, format="R$ %,.0f"),
        dc2:   st.column_config.NumberColumn(dc2,  format="%.1f%%"),
        spl:   st.column_config.NumberColumn(spl, format="R$ %,.0f", help=t("sp_col_help")),
        spm:   st.column_config.NumberColumn(spm, format="R$ %,.0f", help=t("sp_col_help")),
        sph:   st.column_config.NumberColumn(sph, format="R$ %,.0f", help=t("sp_col_help")),
        spr:   st.column_config.TextColumn(spr, width="small", help=t("sp_col_help")),
        mo:    st.column_config.TextColumn(mo, width="small"),
        ur:    st.column_config.LinkColumn(ur, width=80),
        "lot_id": None,
    }
    return styled_disp, column_config, st_col, disp


# ─────────────────────────────────────────────────────────────────────────────
# Top-bar: title + Search/Load More button in upper-right
# ─────────────────────────────────────────────────────────────────────────────
_top_left, _top_right = st.columns([5, 1])
with _top_left:
    st.markdown("### Fazenda Radar")
with _top_right:
    if st.button(t("scrape_button"), use_container_width=True, key="main_scrape_btn"):
        if not st.session_state.selected_sources:
            st.warning(t("src_none_warn"))
        else:
            st.session_state.scraping = True
            st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
tab_overview, tab_lots, tab_bench = st.tabs([
    t("tab_overview"), t("tab_lots"), t("tab_bench"),
])


# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — OVERVIEW
# ═════════════════════════════════════════════════════════════════════════════
with tab_overview:
    st.markdown(f'<h3 class="section">{t("overview_header")}</h3>', unsafe_allow_html=True)

    if df_all.empty:
        st.info(t("no_data_info").format(btn=t("scrape_button")), icon="ℹ️")
    else:
        k1, k2, k3 = st.columns(3)
        k1.metric(t("kpi_total"), f"{len(df_all):,}".replace(",", "."))
        avg_disc = df_all["discount_to_mid_pct"].dropna().mean()
        k2.metric(t("kpi_avg_discount"), f"{avg_disc:.1f}%" if pd.notna(avg_disc) else "—")
        avg_pha = df_all["auction_price_per_ha"].dropna().mean()
        k3.metric(t("kpi_avg_price_ha"), fmt_brl(avg_pha) if pd.notna(avg_pha) else "—")

        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**{t('chart_by_state')}**")
            sc2 = df_all["state"].value_counts().reset_index()
            sc2.columns = [t("col_state"), t("col_lots")]
            st.bar_chart(sc2.set_index(t("col_state")), color="#4caf7d", use_container_width=True)
        with c2:
            st.markdown(f"**{t('chart_by_type')}**")
            tc = df_all["land_type"].value_counts().reset_index()
            tc.columns = [t("col_type"), t("col_lots")]
            st.bar_chart(tc.set_index(t("col_type")), color="#1a7f4b", use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — AUCTION LOTS
# ═════════════════════════════════════════════════════════════════════════════
with tab_lots:
    if df_all.empty:
        st.info(t("no_data_info").format(btn=t("scrape_button")), icon="ℹ️")
        st.stop()

    # ── What changed in the last search: new lots + price updates ─────────────
    _new_lots      = st.session_state.get("last_new_lots", [])
    _price_changes = st.session_state.get("last_price_changes", [])
    if _new_lots or _price_changes:
        # New lots — full column set, stacked above the price updates.
        with st.expander(f"{t('new_lots_header')} ({len(_new_lots)})",
                         expanded=bool(_new_lots)):
            if _new_lots:
                _new_ids = {str(l.get("lot_id")) for l in _new_lots}
                _ndf = df_all[df_all["lot_id"].astype(str).isin(_new_ids)]
                _ns, _ncfg, _nstc, _nd = _build_lots_display(_ndf)
                st.dataframe(_ns, use_container_width=True, hide_index=True,
                             column_config=_ncfg)
            else:
                st.caption(t("no_new_lots"))

        # Price updates — full column set. Old → new price shown as a caption
        # above the table, then the same full table as everywhere else.
        with st.expander(f"{t('price_changes_header')} ({len(_price_changes)})",
                         expanded=bool(_price_changes)):
            if _price_changes:
                _chg_lines = []
                for l in _price_changes:
                    _op, _np = l.get("old_price"), l.get("new_price")
                    _chg_lines.append(
                        f"• {l.get('property_name','?')[:60]} — "
                        f"R$ {(_op or 0):,.0f} → R$ {(_np or 0):,.0f}"
                    )
                st.caption("  \n".join(_chg_lines))
                _chg_ids = {str(l.get("lot_id")) for l in _price_changes}
                _pdf = df_all[df_all["lot_id"].astype(str).isin(_chg_ids)]
                _ps, _pcfg, _pstc, _pd_ = _build_lots_display(_pdf)
                st.dataframe(_ps, use_container_width=True, hide_index=True,
                             column_config=_pcfg)
            else:
                st.caption(t("no_price_changes"))

    # ── Filters ───────────────────────────────────────────────────────────────
    st.markdown(f'<h3 class="section">{t("filters_header")}</h3>', unsafe_allow_html=True)

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        sel_states = st.multiselect(
            t("filter_state"),
            options=sorted(df_all["state"].dropna().unique().tolist()),
            placeholder=t("filter_all"), key="lot_states",
        )
    with fc2:
        sel_types = st.multiselect(
            t("filter_land_type"),
            options=sorted(df_all["land_type"].dropna().unique().tolist()),
            placeholder=t("filter_all"), key="lot_types",
        )
    with fc3:
        sel_modality = st.multiselect(
            t("filter_modality"),
            options=sorted(df_all["auction_type"].dropna().replace("", pd.NA).dropna().unique().tolist()),
            placeholder=t("filter_all"), key="lot_modality",
            help=t("filter_modality_help"),
        )

    fc5, fc6 = st.columns(2)
    with fc5:
        price_vals = df_all["auction_price"].dropna()
        if not price_vals.empty:
            pmin, pmax = int(price_vals.min()), int(price_vals.max())
            if pmin == pmax:
                pmax = pmin + 1
            price_range = st.slider(t("filter_price_range"), pmin, pmax, (pmin, pmax),
                                    format="R$ %d", key="lot_price")
        else:
            price_range = (0, 0)
    with fc6:
        _HA_BUCKETS = ["< 100 ha", "100–1000 ha", "> 1000 ha"]
        ha_sel = st.multiselect(t("filter_ha_bucket"), options=_HA_BUCKETS,
                                placeholder=t("filter_all"), key="lot_ha_bucket")

    fc8, fc9, fc10 = st.columns(3)
    with fc8:
        date_from = st.date_input(t("filter_date_from"), value=None, key="lot_date_from")
    with fc9:
        date_to = st.date_input(t("filter_date_to"), value=None, key="lot_date_to")
    with fc10:
        only_starred = st.checkbox(t("filter_starred"), value=False, key="lot_only_starred")

    # ── Sort & Group ──────────────────────────────────────────────────────────
    st.markdown(f'<h3 class="section">{t("sort_group_header")}</h3>', unsafe_allow_html=True)

    sc1, sc2, sc3 = st.columns([3, 2, 2])
    with sc1:
        sort_col_map = {
            "discount_to_mid_pct": t("sort_discount"),
            "auction_price": t("sort_price"), "auction_price_per_ha": t("sort_price_ha"),
            "hectares": t("sort_hectares"), "auction_date": t("sort_date"),
            "state": t("sort_state"), "land_type": t("sort_land_type"),
        }
        sort_by = st.selectbox(t("sort_by"), options=list(sort_col_map.keys()),
                               format_func=lambda x: sort_col_map[x], key="lot_sort_col")
    with sc2:
        sort_asc = st.radio(t("sort_dir"),
                            [t("sort_desc"), t("sort_asc")],
                            index=0, key="lot_sort_dir", horizontal=True)
    with sc3:
        group_opts = {
            t("group_none"): "__none__",
            t("group_state"): "state",
            t("group_land_type"): "land_type",
            t("group_modality"): "auction_type",
        }
        group_label = st.selectbox(t("group_by"), options=list(group_opts.keys()), key="lot_group")
        group_by = group_opts[group_label]

    # ── Apply filters ─────────────────────────────────────────────────────────
    df = df_all.copy()
    if sel_states:
        df = df[df["state"].isin(sel_states)]
    if sel_types:
        df = df[df["land_type"].isin(sel_types)]
    if sel_modality:
        df = df[df["auction_type"].isin(sel_modality)]
    if price_range != (0, 0):
        df = df[df["auction_price"].isna() | df["auction_price"].between(*price_range)]
    if ha_sel:
        _h = df["hectares"]
        _cond = pd.Series(False, index=df.index)
        if "< 100 ha" in ha_sel:
            _cond |= _h < 100
        if "100–1000 ha" in ha_sel:
            _cond |= (_h >= 100) & (_h <= 1000)
        if "> 1000 ha" in ha_sel:
            _cond |= _h > 1000
        df = df[_cond.fillna(False)]   # lots without a hectare value are excluded
    if date_from:
        df = df[df["auction_date"].isna() | (df["auction_date"] >= str(date_from))]
    if date_to:
        df = df[df["auction_date"].isna() | (df["auction_date"] <= str(date_to))]
    if only_starred:
        df = df[df["starred"] == True]

    ascending = sort_asc == t("sort_asc")
    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=ascending, na_position="last")

    # ── Active filter chips ───────────────────────────────────────────────────
    active = []
    if sel_states:
        active.append(f"{t('chip_uf')} {', '.join(sel_states)}")
    if sel_types:
        active.append(f"{t('chip_type')} {', '.join(sel_types)}")
    if sel_modality:
        active.append(f"{t('chip_modality')} {', '.join(sel_modality)}")

    if price_range != (0, 0) and price_range != (int(df_all['auction_price'].dropna().min() if not df_all['auction_price'].dropna().empty else 0), int(df_all['auction_price'].dropna().max() if not df_all['auction_price'].dropna().empty else 0)):
        active.append(f"R$ {price_range[0]:,}–{price_range[1]:,}".replace(",", "."))
    if ha_sel:
        active.append(", ".join(ha_sel))
    if date_from:
        active.append(f"{t('chip_from')} {date_from}")
    if date_to:
        active.append(f"{t('chip_to')} {date_to}")

    if active:
        chips = " ".join(f'<span class="chip">{f}</span>' for f in active)
        st.markdown(
            f"**{t('active_filters')}** {chips} — <b>{len(df)}</b> {t('lots_label')}",
            unsafe_allow_html=True,
        )
    else:
        st.caption(f"{len(df)} {t('lots_label')} ({t('no_active_filters')})")

    st.divider()

    # ── Grouped summary ───────────────────────────────────────────────────────
    grp = None
    if group_by != "__none__" and not df.empty:
        st.markdown(f'<h3 class="section">{t("grouped_header")}</h3>', unsafe_allow_html=True)

        grp_col_label = {
            "state": t("grp_col_state"), "land_type": t("grp_col_land_type"),
            "auction_type": t("grp_col_modality"),
        }[group_by]

        grp = (
            df.groupby(group_by, dropna=False)
            .agg(
                Lots=("auction_price", "count"),
                Avg_Discount=("discount_to_mid_pct", "mean"),
                Avg_Price=("auction_price", "mean"),
                Avg_PriceHa=("auction_price_per_ha", "mean"),
                Avg_Hectares=("hectares", "mean"),
            )
            .reset_index()
            .rename(columns={group_by: grp_col_label})
            .sort_values("Avg_Discount", ascending=False)
        )
        grp.columns = [
            grp_col_label, t("grp_lots"), t("grp_avg_discount"),
            t("grp_avg_price"), t("grp_avg_ha"), t("grp_avg_hectares"),
        ]

        st.dataframe(
            grp, use_container_width=True, hide_index=True, height=300,
            column_config={
                t("grp_avg_discount"): st.column_config.NumberColumn(
                    t("grp_avg_discount"), format="%.1f%%"),
                t("grp_avg_price"): st.column_config.NumberColumn(
                    t("grp_avg_price"), format="R$ %,.0f"),
                t("grp_avg_ha"): st.column_config.NumberColumn(
                    t("grp_avg_ha"), format="R$ %,.0f"),
                t("grp_avg_hectares"): st.column_config.NumberColumn(
                    t("grp_avg_hectares"), format="%,.0f ha"),
            },
        )
        st.divider()

    # ── Export ────────────────────────────────────────────────────────────────
    st.markdown(f'<h3 class="section">{t("export_header")}</h3>', unsafe_allow_html=True)

    ex1, ex2, _ = st.columns([2, 2, 3])
    with ex1:
        if not df.empty:
            excel_bytes = to_excel_listings(df, bench_df)
            st.download_button(
                label=t("export_lots_btn"), data=excel_bytes,
                file_name=f"fazenda_radar_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, key="dl_lots",
            )
            st.caption(f"{len(df)} {t('export_lots_caption')}")
        else:
            st.info(t("export_no_lots"))

    with ex2:
        if grp is not None and not df.empty:
            grp_buf = io.BytesIO()
            with pd.ExcelWriter(grp_buf, engine="openpyxl") as _w:
                grp.to_excel(_w, index=False, sheet_name=t("xl_sheet_grouped"))
                _autosize(_w.sheets[t("xl_sheet_grouped")])
            st.download_button(
                label=t("export_grp_btn"), data=grp_buf.getvalue(),
                file_name=f"fazenda_radar_grupo_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, key="dl_grp",
            )

    st.divider()

    # ── Main table ────────────────────────────────────────────────────────────
    st.markdown(
        f'<h3 class="section">{t("lots_table_header")} ({len(df)} {t("lots_results")})</h3>',
        unsafe_allow_html=True,
    )

    # Colour legend
    st.markdown(
        """<div style="font-size:0.78rem; display:flex; gap:18px; flex-wrap:wrap; margin-bottom:6px">
          <span><span style="display:inline-block;width:12px;height:12px;background:#d6f5d6;border:1px solid #aaa;border-radius:2px;vertical-align:middle"></span>&nbsp;"""
        + ("Salvo / Starred" if st.session_state.get("lang","pt")=="pt" else "Starred")
        + """</span>
          <span><span style="display:inline-block;width:12px;height:12px;background:#ffffff;border:1px solid #aaa;border-radius:2px;vertical-align:middle"></span>&nbsp;"""
        + ("Benchmark: Município (mais preciso)" if st.session_state.get("lang","pt")=="pt" else "Benchmark: Municipality (most precise)")
        + """</span>
          <span><span style="display:inline-block;width:12px;height:12px;background:#fff8e1;border:1px solid #aaa;border-radius:2px;vertical-align:middle"></span>&nbsp;"""
        + ("Benchmark: Região S&P (município não disponível)" if st.session_state.get("lang","pt")=="pt" else "Benchmark: S&P Region avg (municipality not in database)")
        + """</span>
          <span><span style="display:inline-block;width:12px;height:12px;background:#e4e4e4;border:1px solid #aaa;border-radius:2px;vertical-align:middle"></span>&nbsp;"""
        + ("Benchmark: Média estadual (menos preciso)" if st.session_state.get("lang","pt")=="pt" else "Benchmark: State average (least precise)")
        + """</span>
        </div>""",
        unsafe_allow_html=True,
    )

    if df.empty:
        st.warning(t("lots_no_match"))
    else:
        styled_disp, _main_cfg, st_col, disp = _build_lots_display(df)

        edited = st.data_editor(
            styled_disp, use_container_width=True, hide_index=True, height=520,
            column_config=_main_cfg,
            disabled=[c for c in disp.columns if c not in (st_col,)],
            key="lots_table",
        )

        # Sync checkbox edits back to session state using delta from data_editor state.
        # Delta-based (not full-column) so rapid clicks accumulate correctly and
        # rows don't jump position (which would mis-map position-indexed edits).
        _editor_state = st.session_state.get("lots_table") or {}
        _edited_rows: dict = _editor_state.get("edited_rows", {})
        if _edited_rows and "lot_id" in disp.columns:
            _changed = False
            _stars = set(st.session_state.starred)
            for _row_idx, _changes in _edited_rows.items():
                if st_col in _changes and _row_idx < len(disp):
                    _lid = disp.iloc[_row_idx].get("lot_id") if hasattr(disp.iloc[_row_idx], "get") else disp.iloc[_row_idx]["lot_id"]
                    if _lid:
                        if _changes[st_col]:
                            _stars.add(str(_lid))
                        else:
                            _stars.discard(str(_lid))
                        _changed = True
            if _changed:
                st.session_state.starred = _stars
                _save_stars(_stars)

        # ── Detail panel ──────────────────────────────────────────────────────
        st.divider()
        top = df.iloc[0]
        with st.expander(
            f"{t('detail_prefix')} {top.get('property_name','N/A')[:70]}",
            expanded=False,
        ):
            d1, d2, d3 = st.columns(3)
            with d1:
                st.metric(t("detail_auction_price"), fmt_brl(top.get("auction_price")))
                st.metric(t("detail_discount"),      fmt_pct(top.get("discount_to_mid_pct")))
            with d2:
                st.metric(t("detail_size"),          fmt_ha(top.get("hectares")))
                st.metric(t("detail_price_ha_auc"),  fmt_brl(top.get("auction_price_per_ha")))
                st.metric(t("col_sp_mid"),           fmt_brl(top.get("sp_price_per_ha_mid")))
            with d3:
                st.metric(t("detail_state"),         safe(top.get("state")))
                st.metric(t("detail_land_type"),     safe(top.get("land_type")))
                rnd = top.get("round_display") or "—"
                st.metric(t("col_round"), rnd)
                site_appr = top.get("site_appraised_value")
                if site_appr:
                    st.metric(t("col_site_appraisal"), fmt_brl(site_appr))

            if top.get("sp_match_level") == "estado":
                st.caption(t("sp_state_fallback").format(uf=safe(top.get("state"))))
            elif top.get("sp_match_level") == "regiao":
                st.caption(f"⚠️ Benchmark regional — município não encontrado na base S&P; usando média da região.")

            # ── Per-round date & price ─────────────────────────────────────────
            r1_date = top.get("date_round1_disp") or top.get("date_round1") or "—"
            r1_price = top.get("price_round1")
            r2_date = top.get("date_round2_disp") or top.get("date_round2") or "—"
            r2_price = top.get("price_round2")

            rd1, rd2 = st.columns(2)
            with rd1:
                st.markdown(f"**{t('col_date_r1')}:** {r1_date}  \n"
                            f"**{t('col_price_r1')}:** {fmt_brl(r1_price)}")
            with rd2:
                st.markdown(f"**{t('col_date_r2')}:** {r2_date}  \n"
                            f"**{t('col_price_r2')}:** {fmt_brl(r2_price)}")

            if top.get("listing_url"):
                st.link_button(t("detail_open_link"), top["listing_url"])


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — PRICE BENCHMARKS
# ═════════════════════════════════════════════════════════════════════════════
with tab_bench:
    st.markdown(f'<h3 class="section">{t("bench_header")}</h3>', unsafe_allow_html=True)
    st.caption(t("bench_caption"))

    if bench_df.empty:
        st.warning("S&P reference file not found. Place the LAND-BRZ25 xlsx in ~/Desktop/project 2/")
    else:
        bf1, bf2, bf3 = st.columns(3)
        with bf1:
            sel_regions = st.multiselect(
                t("bench_filter_region"),
                options=sorted(bench_df["regiao"].dropna().unique().tolist()),
                placeholder=t("bench_all_regions"), key="bench_region",
            )
        with bf2:
            sel_bstates = st.multiselect(
                t("bench_filter_state"),
                options=sorted(bench_df["uf"].unique().tolist()),
                placeholder=t("bench_all_states"), key="bench_state",
            )
        with bf3:
            sel_btypes = st.multiselect(
                t("bench_filter_type"),
                options=sorted(bench_df["subgrupo"].unique().tolist()),
                placeholder=t("bench_all_types"), key="bench_type",
            )

        bdf = bench_df.copy()
        if sel_regions:
            bdf = bdf[bdf["regiao"].isin(sel_regions)]
        if sel_bstates:
            bdf = bdf[bdf["uf"].isin(sel_bstates)]
        if sel_btypes:
            bdf = bdf[bdf["subgrupo"].isin(sel_btypes)]

        st.caption(f"{len(bdf)} {t('bench_combos')}")
        st.caption(t("bench_state_avg_note"))

        disp_bench = bdf[[
            "regiao", "uf", "state_name", "municipio", "subgrupo",
            "price_baixa", "price_mid", "price_alta", "row_type",
        ]].copy().rename(columns={
            "regiao":     t("bench_col_region"),
            "uf":         t("bench_col_uf"),
            "state_name": t("bench_col_state"),
            "municipio":  t("bench_col_mun"),
            "subgrupo":   t("bench_col_type"),
            "price_baixa": t("bench_col_low"),
            "price_mid":   t("bench_col_mid"),
            "price_alta":  t("bench_col_high"),
        })

        lo = t("bench_col_low"); mi = t("bench_col_mid"); hi = t("bench_col_high")

        def _grey_state_avg_rows(row):
            if row.get("row_type") == "estado":
                # Explicit dark text — keeps contrast in Dark mode too.
                return ["background-color: #e4e4e4; color: #1a1a1a"] * len(row)
            return [""] * len(row)

        styled_bench = disp_bench.style.apply(_grey_state_avg_rows, axis=1)
        st.dataframe(
            styled_bench, use_container_width=True, hide_index=True, height=520,
            column_config={
                lo: st.column_config.NumberColumn(lo, format="R$ %,.0f"),
                mi: st.column_config.NumberColumn(mi, format="R$ %,.0f"),
                hi: st.column_config.NumberColumn(hi, format="R$ %,.0f"),
                "row_type": None,  # internal flag — not displayed
            },
        )

        st.divider()
        col_dl, _ = st.columns([2, 4])
        with col_dl:
            # Build Excel for download
            import io
            _buf = io.BytesIO()
            with pd.ExcelWriter(_buf, engine="xlsxwriter") as _xw:
                disp_bench.to_excel(_xw, index=False, sheet_name="S&P Reference")
                _ws = _xw.sheets["S&P Reference"]
                _fmt_yellow = _xw.book.add_format({"bg_color": "#FFFF00", "num_format": "R$ #,##0"})
                # Highlight the three price columns (cols 5,6,7 — 0-indexed)
                for _col_idx in [5, 6, 7]:
                    _ws.set_column(_col_idx, _col_idx, 16, _fmt_yellow)
            _buf.seek(0)
            st.download_button(
                label=t("bench_export_btn"), data=_buf.getvalue(),
                file_name=f"sp_reference_{datetime.now().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, key="dl_bench",
            )
            st.caption(t("bench_export_caption").format(n=len(bdf)))
