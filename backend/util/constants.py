"""
Shared constants for the AI Country Risk Dashboard.

Only literals—no runtime imports—to avoid circular dependencies.
"""

# ---------------------------------------------------------------------------
# External data source
# ---------------------------------------------------------------------------

WB_ENDPOINT: str = ("https://api.worldbank.org/v2/country/{code}/indicator/{ind}")

# Financial Modeling Prep (FMP) economic calendar. Queried with from/to date
# params (span <= 3 months); timestamps are UTC. If the account's plan exposes
# the legacy slug instead, swap to "https://financialmodelingprep.com/api/v3/economic_calendar".
FMP_ECON_CALENDAR_ENDPOINT: str = "https://financialmodelingprep.com/stable/economic-calendar"

# FMP batch quote (Prices feed). The stable `batch-quote` endpoint accepts a
# comma-separated `symbols` param of MIXED types (indices like ^GSPC, ETFs,
# crypto *USD pairs, commodity futures) and returns one array, so a single call
# fetches every non-yield asset per tick. (The legacy v3 `quote` path is 403 on
# this plan; stable is the one to use — same as the economic-calendar feed.)
FMP_QUOTE_ENDPOINT: str = "https://financialmodelingprep.com/stable/batch-quote"

# FMP daily historical EOD closes (Prices feed). Used at most once/day to read
# the quarter-start and year-start reference closes for the 1Q/YTD calcs. Queried
# with `symbol` + from/to date params; returns a list of {date, close, ...}.
FMP_HISTORICAL_ENDPOINT: str = "https://financialmodelingprep.com/stable/historical-price-eod/full"

# FMP US Treasury par yields (Prices feed — the Bonds rows). One from/to call
# returns a daily history with all tenors as columns (year2/year10/year30/…),
# from which px and the 1D/1Q/YTD POINT changes are derived. Refreshed once/day.
# (Foreign sovereign yields are not offered by FMP and have no clean free daily
# source, so the Bonds pane tracks US tenors only.)
FMP_TREASURY_ENDPOINT: str = "https://financialmodelingprep.com/stable/treasury-rates"

# ---------------------------------------------------------------------------
# Monetary regime — declared, because no machine-readable source states it.
# ---------------------------------------------------------------------------
# A 7% policy rate means something different in a country that sets its own rate
# than in one that imports Frankfurt's, and a current-account deficit means
# something different under a currency board than under a float. The scorer is
# told this before it reads a single number.
#
# The IMF publishes its exchange-rate-arrangement classification once a year, as
# a table in a PDF. There is no endpoint. So this is written down once, with the
# reasoning in the field itself, rather than scraped badly.
#
#   regime       — what the exchange rate actually does
#   sovereignty  — full | constrained | none. Whether the country can set policy
#                  for its own conditions. `none` is not a judgement about
#                  competence; it is a statement that the instrument is not
#                  theirs to use.
#
# Reviewed 2026-09-22 against the IMF AREAER categories.

_EURO = {"regime": "shared currency (euro area)", "sovereignty": "none"}
_FLOAT = {"regime": "own currency, floating", "sovereignty": "full"}
_USD_PEG = {"regime": "conventional peg to the US dollar", "sovereignty": "constrained"}

MONETARY_REGIME: dict[str, dict[str, str]] = {
    # --- Euro area: the policy rate is set in Frankfurt for the whole area ---
    "AT": _EURO, "BE": _EURO, "DE": _EURO, "ES": _EURO, "FI": _EURO,
    "FR": _EURO, "GR": _EURO, "IE": _EURO, "IT": _EURO, "NL": _EURO,
    "PT": _EURO,

    # --- Pegged, to varying degrees of hardness -----------------------------
    # A currency board: the monetary base is fully backed and the rate is
    # defended mechanically, so there is no domestic policy rate to speak of.
    "HK": {"regime": "currency board (linked to the US dollar)", "sovereignty": "none"},
    # Conventional dollar pegs. The rate follows the Fed; fiscal policy carries
    # the adjustment instead.
    "AE": _USD_PEG, "QA": _USD_PEG, "SA": _USD_PEG,
    # A basket peg whose weights are not published, which is its own kind of
    # opacity and belongs in the `information` reading as well as this one.
    "KW": {"regime": "peg to an undisclosed currency basket", "sovereignty": "constrained"},
    # Denmark holds the krone inside a narrow band against the euro under
    # ERM II, so it follows ECB decisions without a vote on them.
    "DK": {"regime": "peg to the euro (ERM II)", "sovereignty": "constrained"},
    # Singapore's policy instrument IS the exchange rate: MAS targets a band for
    # the trade-weighted dollar rather than setting an interest rate. Real
    # sovereignty, exercised through a different lever.
    "SG": {"regime": "managed exchange rate as the policy instrument",
           "sovereignty": "constrained"},
    # A managed float against an undisclosed basket, with capital controls.
    "CN": {"regime": "managed float against a basket, with capital controls",
           "sovereignty": "constrained"},

    # --- Own currency, own policy rate --------------------------------------
    # Includes several heavily managed floats (Egypt, India, Türkiye): the
    # instrument is theirs, whatever they choose to do with it.
    "AU": _FLOAT, "BR": _FLOAT, "CA": _FLOAT, "CH": _FLOAT, "CL": _FLOAT,
    "CO": _FLOAT, "CZ": _FLOAT, "EG": _FLOAT, "GB": _FLOAT, "HU": _FLOAT,
    "ID": _FLOAT, "IL": _FLOAT, "IN": _FLOAT, "JP": _FLOAT, "KR": _FLOAT,
    "MX": _FLOAT, "MY": _FLOAT, "NO": _FLOAT, "NZ": _FLOAT, "PE": _FLOAT,
    "PH": _FLOAT, "PL": _FLOAT, "RU": _FLOAT, "SE": _FLOAT, "TH": _FLOAT,
    "TR": _FLOAT, "TW": _FLOAT, "US": _FLOAT, "ZA": _FLOAT,
}

# ---------------------------------------------------------------------------
# The indicator registry — the one map every consumer reads.
# ---------------------------------------------------------------------------
# Keyed by the SOURCE'S OWN CODE, because that is the identifier that survives:
# a friendly name is ours to change, `FP.CPI.TOTL.ZG` is the World Bank's and
# means the same thing in five years. A code that is not in this registry is a
# code nothing reads.
#
# Each entry carries:
#   label      — display name, also what the payload calls it
#   unit       — how to read the number
#   ledger     — friction | order | information | edge; which section of the
#                evidence payload this indicator appears under
#   source     — who publishes it; also what `vintage` uses to date it
#   freq       — A | Q | M, the series' own cadence, which is what staleness is
#                measured against
#   panel_col  — the column it occupies in the World Bank parquet panel, or None
#                for a series that arrives some other way
#
# The four ledgers, and what each is asking:
#   friction     — what is taken, and how well it converts
#   order        — doubt about the load-bearing rules
#   information  — whether the country's own instruments can be trusted
#   edge         — whether the system is learning
#
# `security` is deliberately absent. It is a retrieval theme, because security
# reporting needs its own query; conflict reaches the score through `order`.

INDICATOR_REGISTRY: dict[str, dict] = {
    # --- friction: what is taken, and how well it converts ------------------
    "FP.CPI.TOTL.ZG": {
        "label": "Inflation (% y/y)", "unit": "% y/y",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": "INFLATION",
    },
    "SI.POV.GINI": {
        "label": "Income inequality (Gini)", "unit": "index",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": "GINI_INDEX",
    },
    "GC.XPN.INTP.RV.ZS": {
        "label": "Interest payments (% revenue)", "unit": "% revenue",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": "INT_PAYM_PCT_REV",
    },
    "OWID.VDEM.CORRUPTION": {
        "label": "Political corruption index (0-1, higher = more corrupt)",
        "unit": "index (0-1)",
        "ledger": "friction", "source": "V-Dem via OWID", "freq": "A",
        "panel_col": "POL_CORRUPTION",
    },

    # --- order: doubt about the load-bearing rules --------------------------
    "GOV_WGI_PV.EST": {
        "label": "Political stability (z-score)", "unit": "z-score",
        "ledger": "order", "source": "World Bank WGI", "freq": "A",
        "panel_col": "POL_STABILITY",
    },
    "GOV_WGI_RL.EST": {
        "label": "Rule of law (z-score)", "unit": "z-score",
        "ledger": "order", "source": "World Bank WGI", "freq": "A",
        "panel_col": "RULE_OF_LAW",
    },
    "SL.UEM.TOTL.ZS": {
        "label": "Unemployment (% labour force)", "unit": "%",
        "ledger": "order", "source": "World Bank WDI", "freq": "A",
        "panel_col": "UNEMPLOYMENT",
    },
    "NY.GDP.PCAP.KD.ZG": {
        "label": "GDP per-capita growth (% y/y)", "unit": "% y/y",
        "ledger": "order", "source": "World Bank WDI", "freq": "A",
        "panel_col": "GDP_PC_GROWTH",
    },

    # --- information: can the country's own instruments be trusted ----------
    # This ledger was the thinnest, and a ledger with one indicator is a ledger
    # that cannot disagree with itself. SPI is a World Bank series and costs
    # nothing to add; RSF is curated because RSF publishes no API.
    "IQ.SPI.OVRL": {
        "label": "Statistical performance (0-100)", "unit": "score",
        "ledger": "information", "source": "World Bank SPI", "freq": "A",
        "panel_col": "STAT_PERFORMANCE",
    },
    "RSF.PRESS.SCORE": {
        "label": "Press freedom (0-100, higher = freer)", "unit": "score",
        "ledger": "information", "source": "RSF World Press Freedom Index",
        "freq": "A", "panel_col": None,
    },

    # --- edge: is the system learning ---------------------------------------
    "BX.KLT.DINV.WD.GD.ZS": {
        "label": "FDI inflow (% GDP)", "unit": "% GDP",
        "ledger": "edge", "source": "World Bank WDI", "freq": "A",
        "panel_col": "FDI_PCT_GDP",
    },
    "SE.XPD.TOTL.GD.ZS": {
        "label": "Education spending (% GDP)", "unit": "% GDP",
        "ledger": "edge", "source": "World Bank WDI", "freq": "A",
        "panel_col": "EDU_SPEND_PCT_GDP",
    },
    "OECD.PISA.MEAN": {
        "label": "PISA mean score (maths/reading/science)", "unit": "score",
        "ledger": "edge", "source": "OECD PISA", "freq": "A",
        "panel_col": None,
    },
}

LEDGERS: tuple[str, ...] = ("friction", "order", "information", "edge")

# Derived, so the fetch loop and the registry cannot disagree about which codes
# are World Bank codes. The panel column is the key because that is what the
# parquet panel and `prepare_llm_payload_pretty` use.
INDICATORS: dict[str, str] = {
    spec["panel_col"]: code
    for code, spec in INDICATOR_REGISTRY.items()
    if spec["panel_col"] and spec["source"].startswith("World Bank")
}

# Non-World-Bank indicators that still arrive through the panel. The value is a
# sentinel and is never sent to the WB API; these are merged into each country's
# panel after the WB fetch (see data_fetching/political_corruption_fetch.py and
# country_data_fetch.merge_extra_indicators).
EXTRA_INDICATORS: dict[str, str] = {
    "POL_CORRUPTION": "OWID:political-corruption-index",
}

# Full set used by the read/DB side (data_retrieval + data_push). The fetch side
# uses INDICATORS (WB-only) so the WB loop never tries to fetch the sentinel.
ALL_INDICATORS: dict[str, str] = {**INDICATORS, **EXTRA_INDICATORS}

# Codes that reach the payload from `backend/data/curated.csv` rather than from
# a panel column.
CURATED_CODES: tuple[str, ...] = tuple(
    code for code, spec in INDICATOR_REGISTRY.items() if spec["panel_col"] is None
)

# Display label -> registry code. `IMF_RECENT_INDICATORS` is keyed by the
# World Bank display name, which is how the sub-annual refresh used to join to
# `recent_indicator`; this is what turns that name back into a code the series
# can be keyed on.
CODE_BY_LABEL: dict[str, str] = {
    spec["label"]: code for code, spec in INDICATOR_REGISTRY.items()
}

# panel column -> registry code, for the payload builder.
CODE_BY_PANEL_COL: dict[str, str] = {
    spec["panel_col"]: code
    for code, spec in INDICATOR_REGISTRY.items()
    if spec["panel_col"]
}

# Display maps, derived from the registry so a label lives in exactly one place.
NICE_NAME: dict[str, str] = {
    spec["panel_col"]: spec["label"]
    for spec in INDICATOR_REGISTRY.values()
    if spec["panel_col"]
}
UNITS: dict[str, str] = {
    spec["label"]: spec["unit"] for spec in INDICATOR_REGISTRY.values()
}

# ---------------------------------------------------------------------------
# IMF higher-frequency refresh (new IMF Data API, SDMX 2.1)
# ---------------------------------------------------------------------------
# The World Bank series above are ANNUAL and published with a 1–2 year lag, so a
# country in the middle of a fast-moving shock (e.g. Argentina inflation) shows a
# badly stale headline. A handful of those indicators DO exist at monthly/quarterly
# frequency from the IMF, so we refresh just those into the `recent_indicator`
# table; the front-end prefers that fresher value and falls back to the WB annual
# one when absent.
#
# NOTE: the legacy IFS SDMX host (dataservices.imf.org) was RETIRED. The current
# IMF Data API is SDMX 2.1 at api.imf.org/external/sdmx/2.1, where the country
# dimension is ISO-3 (e.g. ARG, USA — see COUNTRY_ROSTER iso3) and data is returned
# as SDMX-ML. Series key order for the CPI dataset is
#   COUNTRY.INDEX_TYPE.COICOP_1999.TYPE_OF_TRANSFORMATION.FREQUENCY
# and IMF PRE-COMPUTES the year-over-year percent change (YOY_PCH_PA_PT), so no
# manual y/y math is needed.
IMF_DATA_ENDPOINT: str = "https://api.imf.org/external/sdmx/2.1/data"

# Map of WB display name (matches `indicator.name` / NICE_NAME) -> IMF query spec:
#   dataflow — SDMX dataflow id (dataset)
#   key      — dot-separated series key with an "{iso3}" placeholder
#   freq     — observation frequency code stored alongside the value ('M'|'Q'|'A')
#   unit     — unit string persisted to recent_indicator
# Only Inflation is wired today. GDP and Unemployment are deliberately NOT included:
# IMF quarterly GDP (QGDP_WCA) is a group-based, multi-attribute cube with no
# pre-computed y/y and patchy emerging-market coverage, and annual national
# accounts (NA_MAIN) is a 14-dimension cube — neither is a clean per-country fetch.
# They can be added here once a dependable series is chosen; the rest of the
# pipeline is indicator-agnostic.
IMF_RECENT_INDICATORS: dict[str, dict[str, str]] = {
    "Inflation (% y/y)": {
        "dataflow": "CPI",                          # IMF.STA Consumer Price Index dataset
        "key": "{iso3}.CPI._T.YOY_PCH_PA_PT.M",     # headline (CPI), all-items (_T), y/y %, monthly
        "freq": "M",
        "unit": "% y/y",
    },
}

# ---------------------------------------------------------------------------
# Economic calendar (FMP) — major global decisions/releases for the front-end
# "Econ Calendar" pane.
# ---------------------------------------------------------------------------

# Rolling forward window (days) fetched on each run.
FMP_CALENDAR_DAYS_AHEAD: int = 14

# AI importance-ranking horizon (days). Events within this window — up to the
# full FMP_CALENDAR_DAYS_AHEAD fetch — are scored by the LLM ranker each run.
CAL_RANK_HORIZON_DAYS: int = 14

# The ranker buckets events into weeks of this many days and scores each week
# RELATIVE TO ITSELF, so a quiet week still gets its own full high→low spread
# instead of being flattened by a busier adjacent week.
CAL_RANK_WEEK_DAYS: int = 7

# Global news-alert ranking: after the per-country loop pools every country's
# Top-3 articles, the LLM ranks them by importance to the global economy and
# only the top-N are persisted to the `news_alert` table each run.
ALERTS_TOP_N: int = 30

# FMP "impact" -> front-end importance code ('h'/'m'/'l').
FMP_IMPACT_TO_CODE: dict[str, str] = {"High": "h", "Medium": "m", "Low": "l"}

# Only these impacts are kept (drop "Low"/"None" noise; the pane is small).
FMP_CALENDAR_KEEP_IMPACTS: frozenset[str] = frozenset({"High", "Medium"})

# Curated allowlist of major economies (G20 + Euro Area). Maps FMP's 2-letter
# country code -> display name and DOUBLES AS THE COUNTRY FILTER: any event whose
# code is not a key here is dropped. "EU" (Euro Area) is intentionally included
# so ECB rate decisions survive — it has no entry in COUNTRY_ROSTER.
FMP_CALENDAR_COUNTRIES: dict[str, str] = {
    "US": "United States",
    "EU": "Euro Area",
    "GB": "United Kingdom",
    "JP": "Japan",
    "CN": "China",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "CH": "Switzerland",
    "CA": "Canada",
    "AU": "Australia",
    "NZ": "New Zealand",
    "IN": "India",
    "BR": "Brazil",
    "MX": "Mexico",
    "KR": "South Korea",
    "RU": "Russia",
    "ID": "Indonesia",
    "TR": "Turkey",
    "SA": "Saudi Arabia",
    "ZA": "South Africa",
}

# ---------------------------------------------------------------------------
# Prices feed (bottom-bar "Prices" pane)
# ---------------------------------------------------------------------------
# The prices loop (backend/data_fetching/prices_daemon.py) polls these assets
# on PRICES_POLL_SECONDS and upserts them to the `market_price` table. Live
# prices (stocks/crypto/commodities) come from FMP's batch-quote endpoint; US
# Treasury yields come from FMP's treasury-rates endpoint. To minimize API hits,
# FMP quote classes are fetched only while their market is open (see
# backend/data_fetching/market_hours.py); the yields and the 1Q/YTD reference closes
# refresh at most once per (ET) day.

# How often the daemon polls live FMP quotes (seconds).
PRICES_POLL_SECONDS: int = 300

# Market-hours windows in US Eastern decimal hours (DST handled in market_hours).
# NYSE regular session (stocks/ETFs).
NYSE_OPEN_ET: float = 9.5    # 09:30 ET
NYSE_CLOSE_ET: float = 16.0  # 16:00 ET
# CME Globex daily maintenance break (commodities are otherwise ~24h on weekdays).
GLOBEX_BREAK_START_ET: float = 17.0  # 17:00 ET
GLOBEX_BREAK_END_ET: float = 18.0    # 18:00 ET

# Ordered asset universe for the Prices pane. `sort_order` is the list index.
#   symbol        — internal stable id / DB primary key
#   label         — display label (MSCI rows are relabeled to their tracking ETF)
#   asset_class   — stocks | bonds | crypto | commodities
#   source        — 'fmp' (batch quote) | 'fmp_treasury' (treasury-rates yields)
#   source_symbol — FMP quote symbol, or the treasury-rates tenor field for bonds
#   is_yield      — bonds: changes are POINT differences shown as %, not % moves
# NOTE: the 3 MSCI indices are MSCI-licensed and not on FMP, so they are tracked
# via liquid ETF proxies and relabeled to the ETF ticker (ACWI/ACWX/EEM). Swap a
# source_symbol here if the plan returns a different symbol for any asset.
PRICE_ASSETS: list[dict] = [
    # --- Stocks (indices + relabeled MSCI ETF proxies) ---
    {"symbol": "SP500",   "label": "S&P 500",      "asset_class": "stocks",      "source": "fmp",          "source_symbol": "^GSPC",  "is_yield": False},
    {"symbol": "RUS3000", "label": "Russell 3000", "asset_class": "stocks",      "source": "fmp",          "source_symbol": "^RUA",   "is_yield": False},
    {"symbol": "ACWI",    "label": "ACWI",         "asset_class": "stocks",      "source": "fmp",          "source_symbol": "ACWI",   "is_yield": False},
    {"symbol": "ACWX",    "label": "ACWX",         "asset_class": "stocks",      "source": "fmp",          "source_symbol": "ACWX",   "is_yield": False},
    {"symbol": "EEM",     "label": "EEM",          "asset_class": "stocks",      "source": "fmp",          "source_symbol": "EEM",    "is_yield": False},
    # --- Bonds (US Treasury par yields, via FMP treasury-rates tenor fields) ---
    {"symbol": "US2Y",    "label": "US 2Y",        "asset_class": "bonds",       "source": "fmp_treasury", "source_symbol": "year2",  "is_yield": True},
    {"symbol": "US10Y",   "label": "US 10Y",       "asset_class": "bonds",       "source": "fmp_treasury", "source_symbol": "year10", "is_yield": True},
    {"symbol": "US30Y",   "label": "US 30Y",       "asset_class": "bonds",       "source": "fmp_treasury", "source_symbol": "year30", "is_yield": True},
    # --- Crypto (24/7) ---
    {"symbol": "BTC",     "label": "BTC",          "asset_class": "crypto",      "source": "fmp",          "source_symbol": "BTCUSD",  "is_yield": False},
    {"symbol": "ETH",     "label": "ETH",          "asset_class": "crypto",      "source": "fmp",          "source_symbol": "ETHUSD",  "is_yield": False},
    {"symbol": "SOL",     "label": "SOL",          "asset_class": "crypto",      "source": "fmp",          "source_symbol": "SOLUSD",  "is_yield": False},
    {"symbol": "XRP",     "label": "XRP",          "asset_class": "crypto",      "source": "fmp",          "source_symbol": "XRPUSD",  "is_yield": False},
    # --- Commodities ---
    {"symbol": "GOLD",    "label": "Gold",         "asset_class": "commodities", "source": "fmp",          "source_symbol": "GCUSD",   "is_yield": False},
    {"symbol": "SILVER",  "label": "Silver",       "asset_class": "commodities", "source": "fmp",          "source_symbol": "SIUSD",   "is_yield": False},
    {"symbol": "WTI",     "label": "WTI Crude Oil","asset_class": "commodities", "source": "fmp",          "source_symbol": "CLUSD",   "is_yield": False},
    {"symbol": "BRENT",   "label": "Brent Crude Oil","asset_class": "commodities","source": "fmp",  "source_symbol": "BZUSD",   "is_yield": False},
    {"symbol": "NATGAS",  "label": "Natural Gas",  "asset_class": "commodities", "source": "fmp",          "source_symbol": "NGUSD",   "is_yield": False},
    {"symbol": "WHEAT",   "label": "Wheat",        "asset_class": "commodities", "source": "fmp",          "source_symbol": "KEUSX",   "is_yield": False},
    {"symbol": "CORN",    "label": "Corn",         "asset_class": "commodities", "source": "fmp",          "source_symbol": "ZCUSX",   "is_yield": False},
]

# ---------------------------------------------------------------------------
# Country roster — the single source of truth for the run universe.
# ---------------------------------------------------------------------------
# The coverage rule, in one sentence: every country in the MSCI Developed and
# Emerging Markets indices, plus Russia. Anything outside those indices is out,
# however newsworthy — the rule decides membership, not case-by-case judgement.
#
# MSCI is the arbiter rather than our own judgement: it maintains the
# investable-universe classification that the dashboard's audience already
# thinks in, and it reviews membership annually, so "is this country in scope?"
# has an answer we do not have to defend.
#
# This list is the ONLY place countries are defined. It is seeded into the
# `country` table on every run (data_push.upsert_countries), and the front-end
# reads countries, names and map positions from there — so adding or removing a
# country here requires no front-end change whatsoever.
#
# Fields:
#   name  — display name, also written to country.name
#   iso2  — World Bank query code and the database primary key
#   iso3  — OWID / IMF join key
#   tier  — DM | EM | Special; why the entry is here (documentation)
#   lat/lng — map marker position, seeded to country.lat/country.lng
# ---------------------------------------------------------------------------

COUNTRY_ROSTER: list[dict] = [
    # --- MSCI Developed Markets (23) ---------------------------------------
    {"name": "Australia",             "iso2": "AU", "iso3": "AUS", "tier": "DM", "lat": -24.6809, "lng": 134.53},
    {"name": "Austria",               "iso2": "AT", "iso3": "AUT", "tier": "DM", "lat": 47.6082,  "lng": 14.3738},
    {"name": "Belgium",               "iso2": "BE", "iso3": "BEL", "tier": "DM", "lat": 50.6003,  "lng": 4.7},
    {"name": "Canada",                "iso2": "CA", "iso3": "CAN", "tier": "DM", "lat": 60.9215,  "lng": -108.007},
    {"name": "Denmark",               "iso2": "DK", "iso3": "DNK", "tier": "DM", "lat": 55.6761,  "lng": 10.5683},
    {"name": "Finland",               "iso2": "FI", "iso3": "FIN", "tier": "DM", "lat": 63.3,     "lng": 25.62},
    {"name": "France",                "iso2": "FR", "iso3": "FRA", "tier": "DM", "lat": 46.6,     "lng": 2.0},
    {"name": "Germany",               "iso2": "DE", "iso3": "DEU", "tier": "DM", "lat": 51.2,     "lng": 10.5},
    {"name": "Hong Kong SAR, China",  "iso2": "HK", "iso3": "HKG", "tier": "DM", "lat": 22.3193,  "lng": 114.1694},
    {"name": "Ireland",               "iso2": "IE", "iso3": "IRL", "tier": "DM", "lat": 52.8,     "lng": -8.0},
    {"name": "Israel",                "iso2": "IL", "iso3": "ISR", "tier": "DM", "lat": 31.0,     "lng": 35.0},
    {"name": "Italy",                 "iso2": "IT", "iso3": "ITA", "tier": "DM", "lat": 42.6,     "lng": 12.8},
    {"name": "Japan",                 "iso2": "JP", "iso3": "JPN", "tier": "DM", "lat": 36.5,     "lng": 139.2},
    {"name": "Netherlands",           "iso2": "NL", "iso3": "NLD", "tier": "DM", "lat": 52.25,    "lng": 5.7},
    {"name": "New Zealand",           "iso2": "NZ", "iso3": "NZL", "tier": "DM", "lat": -41.5,    "lng": 173.0},
    {"name": "Norway",                "iso2": "NO", "iso3": "NOR", "tier": "DM", "lat": 61.2,     "lng": 8.7},
    {"name": "Portugal",              "iso2": "PT", "iso3": "PRT", "tier": "DM", "lat": 39.7,     "lng": -8.0},
    {"name": "Singapore",             "iso2": "SG", "iso3": "SGP", "tier": "DM", "lat": 1.3521,   "lng": 103.8198},
    {"name": "Spain",                 "iso2": "ES", "iso3": "ESP", "tier": "DM", "lat": 39.4,     "lng": -4.8},
    {"name": "Sweden",                "iso2": "SE", "iso3": "SWE", "tier": "DM", "lat": 59.65,    "lng": 14.5},
    {"name": "Switzerland",           "iso2": "CH", "iso3": "CHE", "tier": "DM", "lat": 46.75,    "lng": 8.0},
    {"name": "United Kingdom",        "iso2": "GB", "iso3": "GBR", "tier": "DM", "lat": 54.75,    "lng": -3.5},
    {"name": "United States",         "iso2": "US", "iso3": "USA", "tier": "DM", "lat": 39.75,    "lng": -100.5},

    # --- MSCI Emerging Markets (24) ----------------------------------------
    {"name": "Brazil",                "iso2": "BR", "iso3": "BRA", "tier": "EM", "lat": -10.3,    "lng": -53.3},
    {"name": "Chile",                 "iso2": "CL", "iso3": "CHL", "tier": "EM", "lat": -31.8,    "lng": -71.1},
    {"name": "China",                 "iso2": "CN", "iso3": "CHN", "tier": "EM", "lat": 35.0,     "lng": 105.0},
    {"name": "Colombia",              "iso2": "CO", "iso3": "COL", "tier": "EM", "lat": 4.0,      "lng": -73.0},
    {"name": "Czechia",               "iso2": "CZ", "iso3": "CZE", "tier": "EM", "lat": 49.82,    "lng": 15.47},
    {"name": "Egypt",                 "iso2": "EG", "iso3": "EGY", "tier": "EM", "lat": 26.2,     "lng": 29.3},
    {"name": "Greece",                "iso2": "GR", "iso3": "GRC", "tier": "EM", "lat": 39.0,     "lng": 22.3},
    {"name": "Hungary",               "iso2": "HU", "iso3": "HUN", "tier": "EM", "lat": 47.15,    "lng": 19.5},
    {"name": "India",                 "iso2": "IN", "iso3": "IND", "tier": "EM", "lat": 22.35,    "lng": 78.5},
    {"name": "Indonesia",             "iso2": "ID", "iso3": "IDN", "tier": "EM", "lat": -2.5,     "lng": 118.0},
    {"name": "Kuwait",                "iso2": "KW", "iso3": "KWT", "tier": "EM", "lat": 29.31,    "lng": 47.48},
    {"name": "Malaysia",              "iso2": "MY", "iso3": "MYS", "tier": "EM", "lat": 4.5,      "lng": 102.2},
    {"name": "Mexico",                "iso2": "MX", "iso3": "MEX", "tier": "EM", "lat": 23.6,     "lng": -102.0},
    {"name": "Peru",                  "iso2": "PE", "iso3": "PER", "tier": "EM", "lat": -7.0,     "lng": -75.0},
    {"name": "Philippines",           "iso2": "PH", "iso3": "PHL", "tier": "EM", "lat": 13.0,     "lng": 122.5},
    {"name": "Poland",                "iso2": "PL", "iso3": "POL", "tier": "EM", "lat": 52.2297,  "lng": 19.0},
    {"name": "Qatar",                 "iso2": "QA", "iso3": "QAT", "tier": "EM", "lat": 25.2854,  "lng": 51.031},
    {"name": "Saudi Arabia",          "iso2": "SA", "iso3": "SAU", "tier": "EM", "lat": 25.56,    "lng": 42.35},
    {"name": "South Africa",          "iso2": "ZA", "iso3": "ZAF", "tier": "EM", "lat": -28.9,    "lng": 25.0},
    {"name": "South Korea",           "iso2": "KR", "iso3": "KOR", "tier": "EM", "lat": 36.6,     "lng": 127.83},
    {"name": "Taiwan",                "iso2": "TW", "iso3": "TWN", "tier": "EM", "lat": 23.7,     "lng": 120.96},
    {"name": "Thailand",              "iso2": "TH", "iso3": "THA", "tier": "EM", "lat": 15.0,     "lng": 101.0},
    {"name": "Turkey",                "iso2": "TR", "iso3": "TUR", "tier": "EM", "lat": 39.3,     "lng": 35.3},
    {"name": "United Arab Emirates",  "iso2": "AE", "iso3": "ARE", "tier": "EM", "lat": 24.0,     "lng": 54.0},

    # --- Outside both indices (1) ------------------------------------------
    # Russia was removed from MSCI EM in March 2022 and is currently
    # unclassified. Kept for its weight in energy/commodity markets and its
    # volume of risk-relevant news. `llm/legal_restrictions.yaml` marks it
    # non-investable, which renders a RESTRICTED badge and leaves the score
    # alone — the observe-only rewrite turned that gate into a badge, so the
    # number stays the model's own and the legal fact sits beside it.
    {"name": "Russia",                "iso2": "RU", "iso3": "RUS", "tier": "Special", "lat": 64.7, "lng": 97.7},
]

# Convenience lookups derived from the roster.
ISO3_BY_ISO2: dict[str, str] = {c["iso2"]: c["iso3"] for c in COUNTRY_ROSTER}
COUNTRY_NAME_BY_ISO2: dict[str, str] = {c["iso2"]: c["name"] for c in COUNTRY_ROSTER}
TIER_BY_ISO2: dict[str, str] = {c["iso2"]: c["tier"] for c in COUNTRY_ROSTER}

