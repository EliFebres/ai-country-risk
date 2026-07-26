# Country Coverage

**The rule:** every country in the MSCI Developed Markets and Emerging Markets indices, plus Russia. That is 48 countries.

This document explains the rule, lists the roster, and — more usefully — records why the countries that *aren't* here were left out.

*Classification verified against MSCI's market classification as of the June 2025 review. MSCI reassesses membership annually; when it changes, update `COUNTRY_ROSTER` and this file together.*
*Source: <https://www.msci.com/our-solutions/indexes/market-classification>*

---

## Why MSCI

The roster used to be a hand-maintained list of 57 countries with no stated reason. A comment claimed "50 countries: 25 Developed + 25 Emerging" while the list actually held 57, and nobody could say why Mongolia was in and Czechia was out.

Delegating the boundary to MSCI fixes that:

- **It matches how the audience already thinks.** The dashboard is for investors, and MSCI DM/EM is the vocabulary institutional investors use to describe an investable universe.
- **It is maintained by a third party.** "Is this country in scope?" has an answer we don't have to defend, and it changes on someone else's published schedule rather than our whim.
- **It is a coverage rule, not a risk judgement.** Membership reflects market accessibility and size, not how risky a country is. A country being absent says nothing about its risk — only that it is outside the investable universe this product covers.

The one deliberate exception is documented below.

---

## The roster (48)

### MSCI Developed Markets — 23

| | | | |
|---|---|---|---|
| Australia `AU` | Austria `AT` | Belgium `BE` | Canada `CA` |
| Denmark `DK` | Finland `FI` | France `FR` | Germany `DE` |
| Hong Kong `HK` | Ireland `IE` | Israel `IL` | Italy `IT` |
| Japan `JP` | Netherlands `NL` | New Zealand `NZ` | Norway `NO` |
| Portugal `PT` | Singapore `SG` | Spain `ES` | Sweden `SE` |
| Switzerland `CH` | United Kingdom `GB` | United States `US` | |

### MSCI Emerging Markets — 24

| | | | |
|---|---|---|---|
| Brazil `BR` | Chile `CL` | China `CN` | Colombia `CO` |
| Czechia `CZ` | Egypt `EG` | Greece `GR` | Hungary `HU` |
| India `IN` | Indonesia `ID` | Kuwait `KW` | Malaysia `MY` |
| Mexico `MX` | Peru `PE` | Philippines `PH` | Poland `PL` |
| Qatar `QA` | Saudi Arabia `SA` | South Africa `ZA` | South Korea `KR` |
| Taiwan `TW` | Thailand `TH` | Turkey `TR` | UAE `AE` |

### Outside both indices — 1

**Russia `RU`.** MSCI removed Russia from the Emerging Markets index in **March 2022** following the invasion of Ukraine; it is currently unclassified rather than demoted. It is kept anyway because it remains systemically important to energy and commodity markets and generates a continuous stream of risk-relevant news — excluding it would leave an obvious hole in a geopolitical risk product.

Russia will always score **1.0**. The sanctions gate in `backend/utils/ai/legal_restrictions.yaml` overrides the model for countries where US persons are legally barred from holding securities. That is intended, not a bug: for the audience this product serves, a market you cannot lawfully invest in is maximum risk regardless of its macro data.

---

## Why these were removed

The previous 57-country roster included 12 countries outside MSCI DM/EM. Each was dropped for a stated reason, not by oversight:

| Country | MSCI status | Note |
|---|---|---|
| Argentina `AR` | **Standalone** (reclassified 2021) | Removed from EM after prolonged capital controls |
| Pakistan `PK` | **Frontier** (demoted from EM, 2021) | |
| Nigeria `NG` | Frontier | |
| Kenya `KE` | Frontier | |
| Morocco `MA` | Frontier | Demoted from EM in 2013 |
| Romania `RO` | Frontier | |
| Kazakhstan `KZ` | Frontier | |
| Bangladesh `BD` | Frontier | |
| Mongolia `MN` | Frontier | |
| Ukraine `UA` | Unclassified / Frontier | |
| Venezuela `VE` | Unclassified | Not covered by MSCI |
| Luxembourg `LU` | Not in the DM index | Developed economy, but too small a market to be an index constituent |

**Several of these are genuinely high-risk and newsworthy** — Ukraine and Venezuela especially. Their absence is a consequence of the coverage rule (investable universe), not a claim that they are low risk. If the product's scope ever widens beyond the investable universe, they are the obvious first additions.

---

## Data availability

The pipeline draws macro indicators from the **World Bank** (8 indicators) and **Our World in Data / V-Dem** (political corruption), with the **IMF** supplying fresher monthly inflation where available. Coverage is not uniform.

### Taiwan has a known gap

Taiwan is a member of neither the World Bank nor the IMF, so neither publishes data for it. This was verified directly against both APIs, not assumed:

| Indicator | Source | Taiwan |
|---|---|---|
| Political stability (z-score) | World Bank WGI | ✅ ~10 years |
| Rule of law (z-score) | World Bank WGI | ✅ ~10 years |
| Political corruption index | OWID / V-Dem | ✅ 126 observations |
| Income inequality (Gini) | OWID | ⚠️ sparse, ends 2021 |
| Inflation | — | ❌ no source |
| Unemployment | — | ❌ no source |
| GDP per-capita growth | — | ❌ no source |
| FDI (% GDP) | — | ❌ no source |
| Interest payments (% revenue) | — | ❌ no source |

Alternatives were investigated and rejected: the IMF returns zero observations under every Taiwan country code; OWID's macro datasets are World-Bank-derived and inherit the same gap; DBnomics carries IMF-IFS Taiwan data but only exchange rates and industrial production; Taiwan's own DGBAS and central-bank endpoints fail the TLS handshake.

**Taiwan is included anyway** — it is an MSCI EM constituent and central to any serious geopolitical risk view. Its score is driven by news, governance, and corruption rather than macro indicators. **A sparse indicator panel for Taiwan is expected, not a data bug.**

Partial coverage is normal elsewhere too: Gini and interest-payments data are patchy for many countries. Missing indicators appear as `null`, and the model scores on what is available.

---

## The roster is the only place countries are defined

`COUNTRY_ROSTER` in **`backend/utils/constants.py`** is the single source of truth. Each entry carries its display name, ISO-2 (World Bank code and database key), ISO-3 (OWID/IMF join key), tier, and map coordinates.

Every run seeds it into the `country` table (`data_push.upsert_countries`), and **the frontend reads countries, names, and map marker positions from that table** — it holds no country list of its own. Flags for all ISO-2 codes are bundled, with a fallback that hides the image if one is missing.

### To add a country

Add one line to `COUNTRY_ROSTER`:

```python
{"name": "Vietnam", "iso2": "VN", "iso3": "VNM", "tier": "EM", "lat": 16.0, "lng": 106.0},
```

That is the whole change. The next run backfills its macro panel, scores it, seeds its row, and it appears on the map. **No frontend edit is required.**

Verify a new country end to end before trusting it — this runs the real pipeline and then removes what it wrote:

```bash
python backend/tests/live_country_check.py VN
```

### To remove a country

Delete its line. The ETL stops updating it immediately.

⚠️ **Its existing rows are not deleted.** `country`, `yearly_value`, `risk_snapshot`, `risk_snapshot_article`, and `recent_indicator` rows persist, and because the frontend serves whatever is in the database, a removed country keeps rendering with a frozen score. There is deliberately no automatic cleanup — deleting historical risk data should be an explicit decision. Remove the rows by hand in foreign-key order (`risk_snapshot_article` → `risk_snapshot` → `yearly_value` → `recent_indicator` → `country`), and delete `backend/data/wb_panel_wide/country_code=XX/`.

---

## A separate list: the economic calendar

`FMP_CALENDAR_COUNTRIES` in the same file is **intentionally not** derived from this roster. It filters the Econ Calendar pane, which tracks *market-moving events* rather than scored countries. It is a different universe on purpose: it includes `EU` (the Euro Area, not a country) so ECB decisions survive, and omits rostered countries whose data releases don't move global markets. Changing the country roster does not change the calendar, and vice versa.
