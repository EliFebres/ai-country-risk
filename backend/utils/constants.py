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

# The same IMF series, fetched as a full history into `indicator_series` rather
# than as a single latest print into `recent_indicator`. Keyed by registry id.
#
# `recent_indicator` stores exactly one row per (country, indicator), which is
# what the front-end wants and is useless for a 36-month volatility. Rather than
# widen that table, the history lands in the generic series store and the latest
# print keeps flowing where it always did.
#
# Only CPI is wired. IMF exchange rates and reserves were both investigated and
# rejected for now: the SDMX `ER` dataflow returns no series for the roster's
# country keys (BIS covers FX instead — see data_fetching/bis_bulk_fetch), and
# `IRFCL` publishes ~800 monthly series per country whose reserve-asset line
# cannot be identified without domain spelunking. Picking the wrong IRFCL line
# would silently produce a wrong reserves trend, which is worse than an absent
# one, so reserves are curated (see "Curated inputs" in backend/README.md).
IMF_SERIES_INDICATORS: dict[str, dict[str, str]] = {
    "CPI.YOY": {
        "dataflow": "CPI",
        "key": "{iso3}.CPI._T.YOY_PCH_PA_PT.M",
        "freq": "M",
        "source": "IMF CPI",
    },
}

# ---------------------------------------------------------------------------
# Indicator registry — the friction framework's single source of indicator truth
# ---------------------------------------------------------------------------
# Everything above this line describes the ANNUAL parquet panel and the
# latest-print `recent_indicator` table, both of which predate the three-ledger
# framework and are left exactly as they are. This registry describes the wider
# set the ledgers need, and is the one map every new consumer reads:
#
#   * data_fetching/wb_series_fetch  — which World Bank codes to fetch
#   * data_fetching/bis_bulk_fetch   — which BIS datasets map to which id
#   * data_fetching/curated_loader   — the freq and source for each curated row
#   * data_retrieval.build_evidence_payload — label, unit, ledger, and where to
#     look for the freshest value
#
# It lives here rather than beside the payload builder because `constants` is
# import-free by design: the fetchers and the loader can read it without pulling
# in duckdb and pandas. There is exactly one copy; anything needing a label
# imports it from here.
#
# Entry fields:
#   label        display name shown to the model. Also the join key into UNITS
#                for the nine legacy panel indicators.
#   unit         unit string, for the payload.
#   ledger       friction | uncertainty | information | edge — which section of
#                the evidence payload this indicator appears under.
#   source       where the value comes from, for the payload's provenance stamp.
#   freq         the frequency this indicator is stored at ('A'|'Q'|'M').
#   panel_col    raw column in the parquet panel, when the indicator also lives
#                there. None means the panel has no copy.
#   recent_name  key in the `recent_indicator` table, when a latest-print copy
#                exists. None means there is none.
#
# The three location fields are what make "freshest value wins" possible: an
# indicator present in more than one store resolves to a single entry here, and
# the payload builder picks whichever copy carries the newest period.
#
# Codes were verified against the live World Bank API before being listed. Two
# gotchas found and worked around, not papered over:
#   * The WGI z-scores need the database-prefixed form. Bare `GE.EST` returns an
#     empty series; `GOV_WGI_GE.EST` returns the full history, matching the
#     `GOV_WGI_PV.EST` form INDICATORS already uses.
#   * `DT.DOD.DSTC.IR.ZS` and `FM.LBL.BMNY.ZG` have no data for several advanced
#     economies (no short-term external debt reporting; no national broad money
#     inside the euro area). That is a real absence, not a broken code, and the
#     payload reports it as absent.
#   * `HD.HCI.OVRL` covers 47 of the 48 roster countries, with vintages in 2017,
#     2018 and 2020 only — the World Bank has no Taiwan data for any indicator,
#     and the index is published on an irregular multi-year cadence rather than
#     annually. `SE.XPD.TOTL.GD.ZS` is annual and covers 46 (Egypt and Taiwan
#     absent). `OECD.PISA.MEAN` fills the learning-outcome gap on a fixed
#     triennial calendar and covers 44 of the 48: China, Egypt and Kuwait did not
#     sit the 2022 round, and the payload reports them absent, not padded.
#
# TODO: prime-age (25-54) labour-force participation via the ILO would be a
# sharper read on the friction ledger than the headline total below, which moves
# with the retirement bulge as much as with discouragement. ILOSTAT is a separate
# client and is out of scope here.
INDICATOR_REGISTRY: dict[str, dict[str, object]] = {
    # --- friction: what is taken, and how well it converts -------------------
    "GOV_WGI_GE.EST": {
        "label": "Government effectiveness (z-score)", "unit": "z-score",
        "ledger": "friction", "source": "World Bank WGI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "GC.TAX.TOTL.GD.ZS": {
        "label": "Tax revenue (% GDP)", "unit": "% GDP",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "POL_CORRUPTION": {
        "label": "Political corruption index (0–1, higher = more corrupt)", "unit": "index (0–1)",
        "ledger": "friction", "source": "V-Dem via OWID", "freq": "A",
        "panel_col": "POL_CORRUPTION", "recent_name": None,
    },
    "GC.XPN.INTP.RV.ZS": {
        "label": "Interest payments (% revenue)", "unit": "% revenue",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": "INT_PAYM_PCT_REV", "recent_name": None,
    },
    "SI.POV.GINI": {
        "label": "Income inequality (Gini)", "unit": "index",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": "GINI_INDEX", "recent_name": None,
    },
    "SP.POP.DPND.OL": {
        "label": "Old-age dependency ratio", "unit": "% working-age population",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "SL.TLF.CACT.ZS": {
        "label": "Labour-force participation (% 15+)", "unit": "%",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "FM.LBL.BMNY.ZG": {
        "label": "Broad money growth (% y/y)", "unit": "% y/y",
        "ledger": "friction", "source": "World Bank WDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "STAT.TAX.TOP.RATE": {
        "label": "Top statutory tax rate (%)", "unit": "%",
        "ledger": "friction", "source": "OECD Corporate Tax Statistics", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "INFORMAL.PCT.GDP": {
        "label": "Informal economy (% GDP)", "unit": "% GDP",
        "ledger": "friction", "source": "IMF WP/18/17 informal economy", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "OECD.TAX.WEDGE": {
        "label": "Labour tax wedge (% labour cost)", "unit": "% labour cost",
        "ledger": "friction", "source": "OECD Taxing Wages", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "UNWPP.DPND.OL.PROJ": {
        "label": "Old-age dependency, projected 10y", "unit": "% working-age population",
        "ledger": "friction", "source": "UN WPP medium variant", "freq": "A",
        "panel_col": None, "recent_name": None,
    },

    # --- order-uncertainty: doubt about the load-bearing rules ---------------
    # CPI carries a neutral id rather than its World Bank code because three
    # stores hold it — the annual panel, the monthly latest print, and the IMF
    # monthly series — and they must resolve to one logical indicator for
    # freshest-value-wins to work. The `source` column on each stored row keeps
    # the actual provenance.
    "CPI.YOY": {
        "label": "Inflation (% y/y)", "unit": "% y/y",
        "ledger": "uncertainty", "source": "World Bank WDI / IMF CPI", "freq": "M",
        "panel_col": "INFLATION", "recent_name": "Inflation (% y/y)",
    },
    "GOV_WGI_PV.EST": {
        "label": "Political stability (z-score)", "unit": "z-score",
        "ledger": "uncertainty", "source": "World Bank WGI", "freq": "A",
        "panel_col": "POL_STABILITY", "recent_name": None,
    },
    "GOV_WGI_RL.EST": {
        "label": "Rule of law (z-score)", "unit": "z-score",
        "ledger": "uncertainty", "source": "World Bank WGI", "freq": "A",
        "panel_col": "RULE_OF_LAW", "recent_name": None,
    },
    "NY.GDP.PCAP.KD.ZG": {
        "label": "GDP per-capita growth (% y/y)", "unit": "% y/y",
        "ledger": "uncertainty", "source": "World Bank WDI", "freq": "A",
        "panel_col": "GDP_PC_GROWTH", "recent_name": None,
    },
    "SL.UEM.TOTL.ZS": {
        "label": "Unemployment (% labour force)", "unit": "%",
        "ledger": "uncertainty", "source": "World Bank WDI", "freq": "A",
        "panel_col": "UNEMPLOYMENT", "recent_name": None,
    },
    "BX.KLT.DINV.WD.GD.ZS": {
        "label": "FDI inflow (% GDP)", "unit": "% GDP",
        "ledger": "uncertainty", "source": "World Bank WDI", "freq": "A",
        "panel_col": "FDI_PCT_GDP", "recent_name": None,
    },
    "DT.DOD.DSTC.IR.ZS": {
        "label": "Short-term external debt (% reserves)", "unit": "% reserves",
        "ledger": "uncertainty", "source": "World Bank WDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "BIS.FX.USD": {
        "label": "Exchange rate vs USD", "unit": "local currency per USD",
        "ledger": "uncertainty", "source": "BIS XRU", "freq": "M",
        "panel_col": None, "recent_name": None,
    },
    "BIS.POLICY.RATE": {
        "label": "Policy rate (%)", "unit": "% per year",
        "ledger": "uncertainty", "source": "BIS CBPOL", "freq": "M",
        "panel_col": None, "recent_name": None,
    },
    "RESERVES.USD": {
        "label": "Total reserves (USD)", "unit": "USD",
        "ledger": "uncertainty", "source": "IMF IRFCL (manual)", "freq": "M",
        "panel_col": None, "recent_name": None,
    },
    "WUI.INDEX": {
        "label": "World Uncertainty Index", "unit": "index",
        "ledger": "uncertainty", "source": "World Uncertainty Index", "freq": "Q",
        "panel_col": None, "recent_name": None,
    },

    # --- information: can the country's own instruments be trusted -----------
    "IQ.SPI.OVRL": {
        "label": "Statistical performance (0–100)", "unit": "score 0–100",
        "ledger": "information", "source": "World Bank SPI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "RSF.PRESS.SCORE": {
        "label": "Press freedom (0–100, higher = freer)", "unit": "score 0–100",
        "ledger": "information", "source": "RSF World Press Freedom Index", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "OBS.SCORE": {
        "label": "Open Budget Survey (0–100)", "unit": "score 0–100",
        "ledger": "information", "source": "IBP Open Budget Survey", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "UN.EGDI": {
        "label": "E-government index (0–100)", "unit": "score 0–100",
        "ledger": "information", "source": "UN EGDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },

    # --- edge: the system learning. Reported, never penalized ----------------
    # Patents and high-tech exports were removed from this ledger deliberately.
    # A patent is public disclosure: the serious end of the frontier defects to
    # trade secrets, while filing subsidies inflate junk applications. High-tech
    # exports bend to export controls. Both flip meaning with strategic context,
    # and a metric that changes sign under policy cannot be a rank input.
    # What replaced them measures the edge's fuel directly — learning outcomes
    # leading, education spending alongside. The gap between the two is read in
    # the prompt, not computed here: a numeric wedge would need a normalization
    # reference, and there isn't an honest one.
    "IC.BUS.NDNS.ZS": {
        "label": "New business density (per 1,000 working-age)", "unit": "per 1,000 working-age adults",
        "ledger": "edge", "source": "World Bank WDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    "SE.XPD.TOTL.GD.ZS": {
        "label": "Government education spending (% GDP)", "unit": "% of GDP",
        "ledger": "edge", "source": "World Bank WDI", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    # The learning-outcome line the prompt reads first. `freq` is "A" for the
    # period format only — the honest cadence is triennial, and the country-rating
    # notebook's cycles-behind chart keys that off `source` rather than off `freq`.
    "OECD.PISA.MEAN": {
        "label": "PISA mean score (math/reading/science)", "unit": "score",
        "ledger": "edge", "source": "OECD PISA", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
    # Updated on an irregular multi-year cadence — it enters as slow structure,
    # not as a current reading, and a large staleness_days on it is honest rather
    # than a defect. Never filter it for age.
    "HD.HCI.OVRL": {
        "label": "Human Capital Index (0–1)", "unit": "index 0–1",
        "ledger": "edge", "source": "World Bank Human Capital Project", "freq": "A",
        "panel_col": None, "recent_name": None,
    },
}

# The World Bank codes `wb_series_fetch` pulls into `indicator_series`. Derived
# from the registry so a new WB-sourced entry is fetched by adding one line
# above, with no second list to keep in step. The nine legacy panel indicators
# are excluded: they already arrive through the parquet path and re-fetching
# them here would give the same annual number two homes.
WB_SERIES_CODES: tuple[str, ...] = tuple(
    code for code, spec in INDICATOR_REGISTRY.items()
    if str(spec["source"]).startswith("World Bank") and spec["panel_col"] is None
)

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
# backend/main.py polls these assets once per scheduler tick (backend/utils/
# prices.py) and upserts them to the `market_price` table. Live prices
# (stocks/crypto/commodities) come from FMP's batch-quote endpoint; US Treasury
# yields come from FMP's treasury-rates endpoint. To minimize API hits, FMP
# quote classes are fetched only while their market is open (see
# backend/utils/market_hours.py); the yields and the 1Q/YTD reference closes
# refresh at most once per (ET) day.

# The scheduler's tick interval, and so how often live FMP quotes refresh
# (seconds). Also the shortest cadence main.py schedules anything on.
PRICES_POLL_SECONDS: int = 1800

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
# Emerging Markets indices, plus Russia. See COUNTRY_COVERAGE.md at the repo
# root for the full rationale, including why each excluded country is out.
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
    # volume of risk-relevant news. The sanctions gate in
    # ai/legal_restrictions.yaml forces its score to 1.0 — that is intended,
    # and is the honest answer for a US investor who cannot legally hold it.
    {"name": "Russia",                "iso2": "RU", "iso3": "RUS", "tier": "Special", "lat": 64.7, "lng": 97.7},
]

# Convenience lookup derived from the roster.
ISO3_BY_ISO2: dict[str, str] = {c["iso2"]: c["iso3"] for c in COUNTRY_ROSTER}

# ---------------------------------------------------------------------------
# Curated reference lookups — hand-maintained, roster-sized, not time series
# ---------------------------------------------------------------------------
# The numeric curated series live in `backend/data/curated.csv` and flow into
# `indicator_series`. These three are not series: they are a handful of reference
# facts per country that change once a year at most, so they live here beside the
# roster rather than in a file or a table. Being in git means they survive a
# clone and an edit shows up in review.
#
# All three ship empty, which is the correct shipped state, not a default.

# Currency regime per country: the input that lets `metrics.suppressed_vol_flag`
# tell a credibly calm currency from a defended one. Source: IMF AREAER, the
# de-facto classification table — collapse its categories onto three values
# (hard pegs, currency boards and conventional pegs -> `peg`; crawling
# arrangements, bands and "other managed" -> `managed`; floating and free
# floating -> `float`).
#
# An absent entry is NOT `float`. A country with no regime here makes the flag
# return None — "we do not know" — rather than asserting a free float.
FX_REGIMES: dict[str, str] = {
    # "PT": "float",
    # "SA": "peg",
    # "CN": "managed",
}

# Scheduled national elections, `iso2 -> [{"date": "YYYY-MM-DD", "kind": str}]`
# sorted by date. A scheduled transfer of power is a known unknown, and the model
# weighs it differently from an unscheduled one. `kind` is legislative |
# presidential | referendum. Source: IFES Election Guide or IPU Parline; worth a
# refresh quarterly.
ELECTIONS: dict[str, list[dict]] = {
    # "PT": [{"date": "2026-10-04", "kind": "legislative"}],
}

# The `rome_gap` reference: the median of (top statutory rate / tax revenue % GDP)
# across the roster, computed ONCE from a filled curated.csv and then frozen.
# It is deliberately not recomputed each run — a live roster median would make a
# country's own gap history move whenever its peers' data did, so the same
# country-year would report differently on two days its own numbers never
# changed. None until computed; `rome_gap` reports its raw ratio meanwhile.
ROME_REFERENCE_RATIO: float | None = None

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
