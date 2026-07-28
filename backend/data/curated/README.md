# Curated drop folder

Data the friction framework needs that has no free, stable, no-auth API. You
download each file by hand, drop it here with the documented schema, and
`curated_loader.py` picks it up on the next run.

**Every file here ships empty — a header row and nothing else.** That is
deliberate. A template with plausible-looking sample rows is worse than an empty
one: it loads silently, reaches the model as evidence, and produces a confident
score built on invented numbers. An empty file loads to nothing and the payload
honestly says the indicator is absent.

The loader's contract, in three rules:

- **Absent files are silent.** Every file below is expected to be missing until
  you fill it. No warning, no error.
- **Malformed files are loud.** Wrong columns, unparseable numbers, or a bad
  country code raise. A file that is present is a file you meant to be used, so
  a mistake in it must not degrade quietly into missing evidence.
- **Empty files (header only) load zero rows and are not malformed.** That is the
  shipped state.

## The common CSV shape

Most files here are one numeric series per country, so they share one schema:

```csv
country_iso2,period,value
PT,2025,31.5
```

| Column | Meaning |
|---|---|
| `country_iso2` | ISO-3166-1 alpha-2, uppercase. Must be in `constants.COUNTRY_ROSTER`. |
| `period` | `YYYY` for annual, `YYYY-MM` for monthly, `YYYYQn` for quarterly. Must match the file's declared frequency. |
| `value` | A number. Blank means "reported as unavailable" and is stored as NULL — different from the row being absent entirely. |

Rows for countries outside the roster are skipped with a logged count (a
published dataset covering 190 countries is not malformed for being wider than
this project). Anything else wrong with the file raises.

`as_of` is stamped as the file's modification time, and `source` as the label in
the table below. Both land in `indicator_series` so the payload can tell the
model how stale a curated value is.

## Files, ranked by impact

Fill them in this order. The first two unlock the most.

### 1. `fx_regimes.yaml` — currency regime per country

Unlocks `suppressed_vol_flag`, which is the only input that tells the model
measured calm might be manufactured. Without it, a defended peg reads as a
genuinely stable currency.

Not a numeric series, so it does not go into `indicator_series`; the payload
reads it directly as a lookup.

```yaml
# One entry per country. Regime is one of: peg | managed | float
version: 1
as_of: "2026-07-01"
source: "IMF AREAER"
regimes:
  # PT: float
```

- **Where:** IMF *Annual Report on Exchange Arrangements and Exchange
  Restrictions* (AREAER), the de-facto classification table.
- **Cadence:** annual. A regime change mid-year is worth an out-of-band edit.
- **Mapping:** collapse the AREAER categories onto three values — hard pegs,
  currency boards and conventional pegs → `peg`; crawling arrangements, bands and
  "other managed" → `managed`; floating and free floating → `float`.

### 2. `reserves_monthly.csv` — total reserves, monthly

The other half of `suppressed_vol_flag`. FX volatility arrives automatically from
BIS, and the regime comes from the file above, but the flag also needs to know
whether the calm is being paid for out of reserves — so it stays `None` until
this file exists too. **These two files together are what turn the flag on.**

- **Schema:** the common shape, `period` monthly (`YYYY-MM`), `value` = total
  reserve assets in USD.
- **Where:** IMF *International Reserves and Foreign Currency Liquidity*
  (IRFCL), table I.A, line 1 "official reserve assets".
- **Why this is manual:** IRFCL is reachable on the same SDMX client the CPI
  refresh already uses (`api.imf.org/.../data/IRFCL/<ISO3>..`), but it publishes
  roughly 800 monthly series per country and the reserve-assets line cannot be
  identified from the codes without domain knowledge. Picking the wrong line
  would silently produce a plausible but wrong reserves trend, which is worse
  than an absent one — so it is curated until someone pins the exact indicator
  code. If you do pin it, wire it into `constants.IMF_SERIES_INDICATORS` and
  delete this file.
- **Cadence:** monthly.
- **Source label:** `IMF IRFCL (manual)`

### 3. `statutory_rates.csv` — top statutory tax rate

Unlocks `rome_gap`: the gap between what the statute claims and what the state
actually collects. Pair it with `informal_economy.csv` for the full picture.

- **Schema:** the common shape. `value` = top combined statutory corporate rate
  in percent, annual.
- **Where:** OECD Corporate Tax Statistics, table II.1.
- **Cadence:** annual.
- **After filling this, compute the frozen reference ratio** — see
  `reference_constants.yaml` below. `rome_gap` reports its raw ratio without the
  reference, but the gap itself stays null until you set it.
- **Source label:** `OECD Corporate Tax Statistics`

### 4. `press_freedom_rsf.csv` — RSF press freedom score

Half of `instrument_quality`'s required core pair, so the whole information
ledger's computed metric is absent without it. (`IQ.SPI.OVRL` supplies the other
half automatically from the World Bank.)

- **Schema:** the common shape. `value` = RSF global score, 0-100, higher =
  freer. Annual.
- **Where:** Reporters Without Borders, World Press Freedom Index — the score
  column, not the rank.
- **Cadence:** annual, published each May.
- **Source label:** `RSF World Press Freedom Index`

### 5. `policy_rates.csv` — central bank policy rates

Feeds `real_policy_rate`. **Only needed if the BIS fetcher is unavailable** —
`data_fetching/bis_bulk_fetch.py` pulls the same series from BIS's no-auth bulk
file automatically, and covers 41 of the 48 rostered countries (euro-area
members share the ECB rate and have no national series). Keep this as the manual
fallback, and as the way to fill those seven.

- **Schema:** the common shape, `period` monthly (`YYYY-MM`), `value` in percent.
- **Where:** BIS statistics, *Policy rate (monthly)* — dataset `WS_CBPOL`.
- **Cadence:** monthly.
- **Source label:** `BIS CBPOL (manual)`

### 6. `informal_economy.csv` — informal economy share

Pairs with `rome_gap`: a large statutory-to-collection gap means something
different when a third of the economy is informal.

- **Schema:** the common shape. `value` = informal output as a percent of GDP,
  annual.
- **Where:** IMF Working Paper WP/18/17 database (Medina & Schneider), or the
  World Bank Informal Economy Database.
- **Cadence:** irregular; these are research datasets that update every few
  years. Re-check annually.
- **Source label:** `IMF WP/18/17 informal economy`

### 7. `wui_quarterly.csv` — World Uncertainty Index

Order-uncertainty evidence that is measured rather than inferred from articles.

- **Schema:** the common shape, `period` quarterly (`YYYYQn`).
- **Where:** worlduncertaintyindex.com, the per-country panel.
- **Cadence:** quarterly.
- **Source label:** `World Uncertainty Index`

### 8. `unwpp_old_age_projection.csv` — projected old-age dependency

Supplies the ten-year projection half of `dependency_trajectory`. The current
level already arrives from the World Bank, so without this the metric reports
level only.

- **Schema:** the common shape. `period` = the year being *projected to*
  (i.e. current year + 10), `value` = projected old-age dependency ratio.
- **Where:** UN World Population Prospects, medium variant.
- **Cadence:** WPP revises every two years.
- **Source label:** `UN WPP medium variant`

### 9. `open_budget_survey.csv` — Open Budget Survey score

Optional supplement to `instrument_quality` — sharpens the reading, cannot
substitute for the core pair.

- **Schema:** the common shape. `value` = OBS transparency score, 0-100, annual.
- **Where:** International Budget Partnership, Open Budget Survey.
- **Cadence:** biennial.
- **Source label:** `IBP Open Budget Survey`

### 10. `un_egdi.csv` — UN E-Government Development Index

Optional supplement to `instrument_quality`.

- **Schema:** the common shape. `value` = EGDI **rescaled to 0-100** (the UN
  publishes 0-1; multiply by 100 so it shares a scale with the other three
  components). Annual.
- **Where:** UN E-Government Survey.
- **Cadence:** biennial.
- **Source label:** `UN EGDI`

### 11. `oecd_tax_wedge.csv` — labour tax wedge

Supplementary friction evidence: the wedge on labour specifically, alongside the
economy-wide measure.

- **Schema:** the common shape. `value` = total tax wedge as a percent of labour
  cost, single average worker, annual.
- **Where:** OECD Taxing Wages, table 0.1.
- **Cadence:** annual.
- **Note:** OECD members only. Absent for most of the EM roster by construction.
- **Source label:** `OECD Taxing Wages`

### 12. `election_calendar.yaml` — scheduled national elections

Order-uncertainty context: a scheduled transfer of power is a known unknown, and
the model should weigh an unscheduled one differently.

Not a numeric series; read directly as a lookup.

```yaml
version: 1
as_of: "2026-07-01"
source: "IFES Election Guide"
elections:
  # PT:
  #   - date: "2026-10-04"
  #     kind: legislative      # legislative | presidential | referendum
```

- **Where:** IFES Election Guide, or IPU Parline.
- **Cadence:** rolling. Worth a refresh quarterly.

### 13. `weo_vintages/` — IMF WEO forecast vintages

Feeds `forecast_instability`: how much the IMF keeps revising its view of the
same country-year. One file per vintage, so revisions can be differenced.

- **Filename:** `weo_<YYYY><MM>.csv` — e.g. `weo_202604.csv` for the April 2026
  WEO. The vintage date comes from the filename; do not rename files.
- **Schema:** `country_iso2,target_year,value` where `value` is the forecast of
  real GDP growth for `target_year` as of that vintage.
- **Where:** IMF World Economic Outlook database, published each April and
  October. Past vintages stay available in the WEO archive.
- **Cadence:** twice yearly. `forecast_instability` needs at least two vintages
  to report anything.
- **Source label:** `IMF WEO <vintage>`

### 14. `reference_constants.yaml` — frozen reference levels

Scalars that must not drift day to day. Currently one: the `rome_gap` reference
ratio.

Computed **once**, from a filled `statutory_rates.csv`, then frozen and
versioned. It is not recomputed on each run on purpose — a live roster median
would make a country's own gap history move whenever its peers' data did, so the
same country-year would report differently on two days its own numbers never
changed.

```yaml
version: 1
rome_reference_ratio: null   # median of (top statutory rate / tax revenue % GDP)
computed_from: null          # e.g. "statutory_rates.csv @ 2026-07-27, 38 countries"
notes: "Bump `version` when recomputed; never edit in place without doing so."
```

## Sources with no template here, and why

Two metrics have no free source worth the plumbing, so they degrade honestly
rather than getting a template nobody will fill:

- `wage_productivity_gap` needs real wage growth and output-per-worker growth.
  Returns `None`; supplementary by design.
- `precommitted_share` needs social protection as a percent of revenue for its
  second half. It returns the interest-only figure marked `partial: true`, which
  is the behavior the function was written for. It never imputes the missing
  half.

If you later find a usable source for either, add it as a common-shape CSV plus
one row in `curated_loader._CURATED_SERIES`.
