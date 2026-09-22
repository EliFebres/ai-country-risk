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
# Economic / governance indicators (World Bank series)
# ---------------------------------------------------------------------------

# World Bank series only — every value here is fetched from the World Bank API.
# Non-WB sources (e.g. the OWID Political Corruption Index) live in EXTRA_INDICATORS
# so the World Bank fetch loop never sees a non-WB code.
INDICATORS = {
    "INFLATION":          "FP.CPI.TOTL.ZG",         # Consumer-price inflation, % y/y
    "UNEMPLOYMENT":       "SL.UEM.TOTL.ZS",         # Unemployment rate, % labour force
    "FDI_PCT_GDP":        "BX.KLT.DINV.WD.GD.ZS",   # FDI net inflows, % GDP
    "POL_STABILITY":      "GOV_WGI_PV.EST",         # Political stability (z-score)
    "RULE_OF_LAW":        "GOV_WGI_RL.EST",         # Rule of law (z-score)
    "GINI_INDEX":         "SI.POV.GINI",            # Income inequality (0 – 100)
    "GDP_PC_GROWTH":      "NY.GDP.PCAP.KD.ZG",      # GDP per-capita growth, % y/y
    "INT_PAYM_PCT_REV":   "GC.XPN.INTP.RV.ZS",      # Interest payments / revenue, %
}

# Non-World-Bank indicators. The value is a sentinel (never sent to the WB API);
# these are merged into each country's panel after the WB fetch (see
# backend/utils/data_fetching/political_corruption_fetch.py and
# country_data_fetch.merge_extra_indicators).
EXTRA_INDICATORS = {
    "POL_CORRUPTION":     "OWID:political-corruption-index",  # V-Dem via Our World in Data
}

# Full set used by the read/DB side (data_retrieval + data_push). The fetch side
# uses INDICATORS (WB-only) so the WB loop never tries to fetch the sentinel.
ALL_INDICATORS = {**INDICATORS, **EXTRA_INDICATORS}

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
# A standalone long-running daemon (backend/prices_daemon.py) polls these assets
# on PRICES_POLL_SECONDS and upserts them to the `market_price` table. Live
# prices (stocks/crypto/commodities) come from FMP's batch-quote endpoint; US
# Treasury yields come from FMP's treasury-rates endpoint. To minimize API hits,
# FMP quote classes are fetched only while their market is open (see
# backend/utils/market_hours.py); the yields and the 1Q/YTD reference closes
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
# Coverage universe (50 countries: 25 Developed + 25 Emerging)
# ---------------------------------------------------------------------------

SELECTED_COUNTRIES: list[str] = [
    # --- Developed Markets ---
    "United States", "Canada", "Germany", "France", "United Kingdom",
    "Japan", "Australia", "Austria", "Belgium", "Denmark",
    "Finland", "Ireland", "Italy", "Netherlands", "New Zealand",
    "Norway", "Portugal", "Singapore", "Spain", "Sweden",
    "Switzerland", "Israel", "Hong Kong SAR, China", "Greece", "Luxembourg",
    # --- Emerging Markets ---
    "Argentina", "Brazil", "Chile", "China", "Colombia",
    "Hungary", "India", "Indonesia", "Malaysia", "Mexico",
    "Pakistan", "Peru", "Philippines", "Poland", "Qatar",
    "Romania", "Saudi Arabia", "South Africa", "Thailand", "United Arab Emirates",
    "Ukraine", "Morocco", "Kenya", "Nigeria", "Bangladesh",
]

# ---------------------------------------------------------------------------
# Country roster (hardcoded). Source of truth for the run universe — replaces
# the former backend/data/country_data.xlsx read. Each entry carries the
# display name, ISO-2 (World Bank / DB key) and ISO-3 (OWID join key).
# ---------------------------------------------------------------------------

COUNTRY_ROSTER: list[dict[str, str]] = [
    {"name": "United Arab Emirates",  "iso2": "AE", "iso3": "ARE"},
    {"name": "Argentina",             "iso2": "AR", "iso3": "ARG"},
    {"name": "Australia",             "iso2": "AU", "iso3": "AUS"},
    {"name": "Austria",               "iso2": "AT", "iso3": "AUT"},
    {"name": "Belgium",               "iso2": "BE", "iso3": "BEL"},
    {"name": "Bangladesh",            "iso2": "BD", "iso3": "BGD"},
    {"name": "Brazil",                "iso2": "BR", "iso3": "BRA"},
    {"name": "Canada",                "iso2": "CA", "iso3": "CAN"},
    {"name": "Switzerland",           "iso2": "CH", "iso3": "CHE"},
    {"name": "Chile",                 "iso2": "CL", "iso3": "CHL"},
    {"name": "China",                 "iso2": "CN", "iso3": "CHN"},
    {"name": "Colombia",              "iso2": "CO", "iso3": "COL"},
    {"name": "Germany",               "iso2": "DE", "iso3": "DEU"},
    {"name": "Denmark",               "iso2": "DK", "iso3": "DNK"},
    {"name": "Egypt",                 "iso2": "EG", "iso3": "EGY"},
    {"name": "Spain",                 "iso2": "ES", "iso3": "ESP"},
    {"name": "Finland",               "iso2": "FI", "iso3": "FIN"},
    {"name": "France",                "iso2": "FR", "iso3": "FRA"},
    {"name": "United Kingdom",        "iso2": "GB", "iso3": "GBR"},
    {"name": "Greece",                "iso2": "GR", "iso3": "GRC"},
    {"name": "Hong Kong SAR, China",  "iso2": "HK", "iso3": "HKG"},
    {"name": "Hungary",               "iso2": "HU", "iso3": "HUN"},
    {"name": "Indonesia",             "iso2": "ID", "iso3": "IDN"},
    {"name": "India",                 "iso2": "IN", "iso3": "IND"},
    {"name": "Ireland",               "iso2": "IE", "iso3": "IRL"},
    {"name": "Israel",                "iso2": "IL", "iso3": "ISR"},
    {"name": "Italy",                 "iso2": "IT", "iso3": "ITA"},
    {"name": "Japan",                 "iso2": "JP", "iso3": "JPN"},
    {"name": "Kazakhstan",            "iso2": "KZ", "iso3": "KAZ"},
    {"name": "Kenya",                 "iso2": "KE", "iso3": "KEN"},
    {"name": "South Korea",           "iso2": "KR", "iso3": "KOR"},
    {"name": "Luxembourg",            "iso2": "LU", "iso3": "LUX"},
    {"name": "Morocco",               "iso2": "MA", "iso3": "MAR"},
    {"name": "Mexico",                "iso2": "MX", "iso3": "MEX"},
    {"name": "Mongolia",              "iso2": "MN", "iso3": "MNG"},
    {"name": "Malaysia",              "iso2": "MY", "iso3": "MYS"},
    {"name": "Nigeria",               "iso2": "NG", "iso3": "NGA"},
    {"name": "Netherlands",           "iso2": "NL", "iso3": "NLD"},
    {"name": "Norway",                "iso2": "NO", "iso3": "NOR"},
    {"name": "New Zealand",           "iso2": "NZ", "iso3": "NZL"},
    {"name": "Pakistan",              "iso2": "PK", "iso3": "PAK"},
    {"name": "Peru",                  "iso2": "PE", "iso3": "PER"},
    {"name": "Philippines",           "iso2": "PH", "iso3": "PHL"},
    {"name": "Poland",                "iso2": "PL", "iso3": "POL"},
    {"name": "Portugal",              "iso2": "PT", "iso3": "PRT"},
    {"name": "Qatar",                 "iso2": "QA", "iso3": "QAT"},
    {"name": "Romania",               "iso2": "RO", "iso3": "ROU"},
    {"name": "Russia",                "iso2": "RU", "iso3": "RUS"},
    {"name": "Saudi Arabia",          "iso2": "SA", "iso3": "SAU"},
    {"name": "Singapore",             "iso2": "SG", "iso3": "SGP"},
    {"name": "Sweden",                "iso2": "SE", "iso3": "SWE"},
    {"name": "Thailand",              "iso2": "TH", "iso3": "THA"},
    {"name": "Turkey",                "iso2": "TR", "iso3": "TUR"},
    {"name": "Ukraine",               "iso2": "UA", "iso3": "UKR"},
    {"name": "United States",         "iso2": "US", "iso3": "USA"},
    {"name": "Venezuela",             "iso2": "VE", "iso3": "VEN"},
    {"name": "South Africa",          "iso2": "ZA", "iso3": "ZAF"},
]

# Convenience lookups derived from the roster.
ISO3_BY_ISO2: dict[str, str] = {c["iso2"]: c["iso3"] for c in COUNTRY_ROSTER}
COUNTRY_NAME_BY_ISO2: dict[str, str] = {c["iso2"]: c["name"] for c in COUNTRY_ROSTER}

# ---------------------------------------------------------------------------
# Display names for indicators
# ---------------------------------------------------------------------------

NICE_NAME: dict[str, str] = {
    "INFLATION":          "Inflation (% y/y)",
    "UNEMPLOYMENT":       "Unemployment (% labour force)",
    "FDI_PCT_GDP":        "FDI inflow (% GDP)",
    "POL_STABILITY":      "Political stability (z-score)",
    "RULE_OF_LAW":        "Rule of law (z-score)",
    "GINI_INDEX":         "Income inequality (Gini)",
    "GDP_PC_GROWTH":      "GDP per-capita growth (% y/y)",
    "INT_PAYM_PCT_REV":   "Interest payments (% revenue)",
    "POL_CORRUPTION":     "Political corruption index (0–1, higher = more corrupt)",
}

# ---------------------------------------------------------------------------
# Units for the pretty labels above
# ---------------------------------------------------------------------------

UNITS: dict[str, str] = {
    "Inflation (% y/y)":               "% y/y",
    "Unemployment (% labour force)":   "%",
    "FDI inflow (% GDP)":              "% GDP",
    "Political stability (z-score)":   "z-score",
    "Rule of law (z-score)":           "z-score",
    "Income inequality (Gini)":        "index",
    "GDP per-capita growth (% y/y)":   "% y/y",
    "Interest payments (% revenue)":   "% revenue",
    "Political corruption index (0–1, higher = more corrupt)": "index (0–1)",
}
