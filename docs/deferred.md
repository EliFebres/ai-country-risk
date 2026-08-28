# Deferred

Decisions taken and deliberately not acted on, with the reasoning attached so the
next session does not re-derive it. Nothing here is a bug; each is a real choice
waiting for the right moment — except the frontend section at the bottom, which
is work that is genuinely owed.

Resolved items are removed rather than annotated. If it is here, it is still
true.

---

## The frontend rewrite map

**This is the one item that is broken rather than deferred.** The ten-table
rebuild dissolved the tables three frontend queries read, and nothing under
`frontend/` was touched. There are no compatibility views. Every query lives in
one file, `frontend/app/lib/risk-server.ts`.

### Survives untouched

| Route | Method | Why |
|---|---|---|
| `/api/risk` | `fetchJoinedLatestRisks` | `risk_snapshot` and `country` keep their names and every column it selects |
| `/api/risk-summary` | `fetchLatestSummaries` | `risk_snapshot.bullet_summary` only |
| `/api/prices` | `fetchMarketPrices` | `market_price` keeps its name and gains columns |
| `/api/econ-calendar` | `fetchEconCalendarEvents` | the declined merge — table unchanged |

### Breaks loudly — 500s

| Route | Method | Old → new |
|---|---|---|
| `/api/indicators` | `fetchLatestIndicatorValues` | `indicator` + `yearly_value` + `recent_indicator` → `indicator_series` |
| `/api/articles` | `fetchLatestArticlesForLatestSnapshots` | `risk_snapshot_article` → `risk_snapshot.top_articles` JSONB |
| `/api/dashboard` | composes both | inherits them |

`fetchLatestIndicatorValues` **looks** defensive and is not: its `catch` falls
back to `annualOnlySql`, which reads the same two dead tables, so the fallback
throws uncaught. Do not read the `try` as protection.

### Breaks quietly — returns empty

| Method | Effect |
|---|---|
| `fetchIndicatorAverageTrends` | `catch → {}`; the trend rail renders no lines |
| `fetchChannels` | already empty — `live_tv_channel` **has never existed** in the database, in a schema that ran for months. `terminal-seed.ts` has always been the real source for that pane. Either create the table and write to it, or delete the query and the fallback dance with it |

### The rewrites, concretely

**`fetchLatestIndicatorValues`** — the join by indicator *name* is gone.
`indicator_series` is keyed by code, and label/unit live in
`backend/util/constants.py::INDICATOR_REGISTRY`. Either hard-code the code→label
pairs in the frontend or expose them from the backend; there is no table to join.

```sql
SELECT DISTINCT ON (country_iso2, indicator_code)
       country_iso2, indicator_code, period, freq, value, source, as_of
  FROM indicator_series
 WHERE indicator_code = ANY($1::text[])
 ORDER BY country_iso2, indicator_code, period DESC, as_of DESC;
```

Shape change: the old query returned `annual_value` **and** `recent_value` side by
side and the client picked. The new one returns the winner already resolved, so
`useRecent` goes away and `year` comes from `period`.

**`fetchLatestArticlesForLatestSnapshots`** — no longer a join.

```sql
SELECT s.country_iso2, c.name, s.as_of, s.top_articles
  FROM risk_snapshot s JOIN country c ON c.iso2 = s.country_iso2
 WHERE (s.country_iso2, s.as_of) IN (
   SELECT country_iso2, max(as_of) FROM risk_snapshot GROUP BY 1)
   AND s.top_articles IS NOT NULL;
```

Shape change: three rows per country become one row with a three-element JSONB
array, ordered by rank. The `rank BETWEEN 1 AND 3` CHECK is gone, so
`article_ranking.ensure_top_three` is the only guard —
`testing/test_news_fetching.py::TestEnsureTopThree` covers it.

**`fetchIndicatorAverageTrends`** — the same table swap, grouped by `period`,
filtered to `freq = 'A'`.

---

## 1. Persist the live run's articles

The daily run still discards every article it fetches once the snapshot is
scored; only the top three survive, as JSONB on the row.

**Why it matters.** The series has a seam. The backfill's articles are stored
forever; articles the daily run fetches after the pilot's window are thrown away.
So today's news becomes tomorrow's unrecoverable history, and continuing the
series past the pilot would mean re-harvesting a period already read. Persisting
live articles is what closes that seam.

**Why not yet.** `article.source_system` already distinguishes google-news /
guardian / gdelt / nyt whenever it is turned on, so this is a new write path
rather than a schema change. Roughly 50–100 rows per country per week.

There is a general lesson underneath this one, learned the expensive way: a cost
estimate that depends on stored data should say *which* data, because the data
can be deleted by work that has no idea the estimate exists.

## 2. An API layer between the two halves

A backend refactor breaks frontend routes only because the frontend queries
Postgres directly, which makes column names the contract between the halves. The
section at the top of this file is what that costs.

`frontend/app/lib/risk-server.ts` is the single file holding every query, so an
API would have exactly one caller to replace. Worth deciding later whether it
belongs; explicitly out of scope so far.

## 3. Thirteen registry indicators have no reachable source

`bootstrap` builds 25 of the 38 codes in `INDICATOR_REGISTRY`. The other thirteen
are all curated-source, and `backend/data/curated.csv` ships with a header and
**zero data rows**:

```
GOV.DEBT.DOMESTIC.SHARE   National debt agencies / IMF Article IV
GOV.DEBT.FX.SHARE         National debt agencies / IMF Article IV
INFORMAL.PCT.GDP          IMF WP/18/17 informal economy
NIIP.GDP                  IMF Balance of Payments / IIP
OBS.SCORE                 IBP Open Budget Survey
OECD.PISA.MEAN            OECD PISA
OECD.TAX.WEDGE            OECD Taxing Wages
RESERVES.USD              IMF IRFCL (manual)
RSF.PRESS.SCORE           RSF World Press Freedom Index
STAT.TAX.TOP.RATE         OECD Corporate Tax Statistics
UN.EGDI                   UN E-Government Survey
UNWPP.DPND.OL.PROJ        UN WPP medium variant
WUI.INDEX                 World Uncertainty Index
```

Each is either a manual entry into `curated.csv` or a fetcher nobody has written.
The ledgers score on the 25 that do arrive, and an absent indicator is absent
from the payload rather than zeroed — so this degrades honestly. But it does
degrade.

The empty CSV is deliberate: a template with plausible-looking sample rows loads
silently, reaches the model as evidence, and produces a confident score built on
invented numbers.

`python backend/main.py census PT` shows this per country. The ranked fill order,
with the source for each, is in `backend/README.md`.

## 4. The WEO fetch recovers 13 of 19 editions

The clone-and-run acceptance test, run for real: the 19 `.xls` editions were
renamed aside and `fetch_editions` was run against an empty directory.

**Recovered (13):** 2016-04 → 2019-10 complete, plus 2020-04, 2021-04, 2021-10,
2022-04, 2023-04. All thirteen **byte-identical** to the originals, verified by
SHA-256 — the live IMF path and the Wayback fallback return the published bytes,
not a re-render.

**Not recovered (6):** 2020-10, 2022-10, 2023-10, 2024-04, 2024-10, 2025-04.

The gaps are *scattered*, which is worse than a clean cut-off. The vintage rule
picks the newest edition not after the anchor, so a missing 2023-10 means every
anchor from October 2023 to April 2024 reads April-2023 macro instead. Honest —
the stamps say so — but staler than intended, and invisible unless somebody diffs
the edition list.

A fresh clone therefore gets a WEO archive with holes. The acceptance test passes
for the schema, the roster, the World Bank panels, the BIS and IMF series and the
curated files. It is **partial for WEO**.

## 5. WEO vintage dataflows may retire `weo_vintages/` entirely

Two vintage-specific SDMX dataflows are known. If they exist for older editions
too, all nineteen `.xls` files become fetchable and the folder can go.

**One query answers it — report, do not act.** The hard condition if it is ever
wired up: it must be the *vintage* dataflow. Reaching only the current edition is
not a substitute — stamping today's values as an October-2025 vintage injects
present knowledge into past anchors, which is the exact failure the vintage store
exists to prevent, and it would be invisible in the data.

Verify against ground truth: TUR `NGDP_RPCH` must read 2024 = 3.328 and
2025 = 3.494 with a last-actual-year marker of 2024. If the SDMX response carries
no last-actual-year field at all, stop — the projection-exclusion logic has
nothing to key on.

## 6. `data_upsert` and `news_fetching` form a package-level cycle

`data_upsert.store.article_row` calls `news_fetching.core.classify_themes` so a
row with no query provenance still gets themed; the three adapters in
`news_fetching` import `data_upsert.store` to write.

The module graph itself is acyclic, so Python is happy. Inlining the classifier
into `store` would break the cycle and also fork the shared core, which
`test_news_fetching.TestNoAdapterForksTheCore` forbids by name. The alternative is
moving `classify_themes` somewhere both can depend on. Not worth a move on its
own; revisit if a third module needs it.

## 7. The two-run masking comparison test is due

Deleted in the test cut, with the note "re-add before the next masking change,
not before the pilot".

**Was:** `TestComparingTwoMaskingBehaviours` — the consumer of stored probe
results behind `probe_bundles`. A bundle the sweep fixed reports as fixed, one
that got worse reports as regressed, and a bundle only one run covered is kept
rather than dropped. Plus outlet fingerprinting: whether the probe is reading the
evidence or the newspaper.

**Still guarded:** the probe's own scoring, restored in full.

**Risk:** the probe can still be verified as correct; what is no longer checked is
whether a *change* to masking made things better or worse across two runs. That
comparison is how the 2026-08-03 sweep was validated.

The bake-off moves the digest model into both masking version hashes, which is a
masking change. This is due now.

## 8. `SCORING_MODES` and the schema disagree about how many modes there are

`config.SCORING_MODES` lists three — `masked`, `named`, `masked_nostructural` —
but `risk_snapshot.scoring_mode`'s CHECK admits only two. The third arm never
writes to `risk_snapshot`, so nothing is broken, but it is why the bake-off has to
write its candidates to a file rather than to a third variant.

Either widen the CHECK or say in the schema comment that the third mode is
ledger-only by construction.

## 9. `testing/test_llm.py` is past 1,000 lines

1,270 lines. The agreed rule is to split only when a file passes ~1,000 lines
*and* has a genuine seam. There is one — the probe measures the instrument rather
than the country, the same line the schema draws for `snapshot_diagnostic` — but
six folder files plus one invariants file is the agreed shape, so it stays whole
for now.

## 10. A determinism canary, because the freeze cannot see behind a model id

`score.FROZEN_FIELDS` pins `SCORING_MODEL` and refuses to resume when it moves.
That catches *us* changing the scorer. It cannot catch the scorer changing
underneath a stable id.

**Why it matters.** The bake-off established that `gpt-4o` is the only tested
model that reproduces its own scored output at `temperature=0`, `seed=42`, and
that this is very likely a property of **how it is served** rather than of
anything in this repository — the same model went non-deterministic when only its
schema grammar was weakened, and five other OpenAI models with the identical
grammar vary anyway. See `docs/scorer-bakeoff.md`.

So the reproducibility claim this project rests on depends on a property of a
remote system that we do not control, cannot inspect, and have no notification
for. If OpenAI re-tunes, re-quantises, reroutes or rebatches `gpt-4o` behind the
id `gpt-4o-2024-08-06`, three things silently stop being true — the byte-for-byte
rebuild check, a gate-2 repeat that measures an effect rather than noise, and a
resumed pilot whose second half matches its first — and **nothing in the codebase
notices**. Every version stamp still agrees, because every version stamp is about
us.

That is the same shape as the six defects already found here: a stamp that
records what somebody wrote down rather than what actually happened.

**What it would be.** One stored payload, re-scored a handful of times on a
schedule, asserting the scored fields still match a committed expectation and
failing loudly when they do not.

- One canned payload, committed — `bakeoff._SMOKE_EVIDENCE` already is one, and
  the three-anchor noise-floor set gives calm/moderate/stressed coverage across
  three bands for a few cents.
- Five repeats, `temperature=0`, `seed=42`, through the production wrapper.
- Compare on **scored fields only** — `bullet_summary` and `subscore_evidence`
  are not deterministic even on `gpt-4o` and would make the canary cry wolf on
  its first run. `bakeoff._scored_only` already draws exactly this line.
- Fail loudly, and record *what* moved. "The scorer changed" is a different
  finding from "the scorer drifted by one point on one ledger".

**Why not yet.** It is cheap but it is not free, and it wants a schedule rather
than a test run — `pytest` must stay network-free, which is enforced and worth
more than this. The natural home is a `util/tools/` command run on a cron beside
whatever else gets scheduled, not a sixth file in `testing/`.

**The honest caveat.** A canary that fires tells you the instrument moved; it
does not tell you the stored series was wrong, and it cannot repair anything
already written. Its value is that the next claim made about reproducibility is
made knowingly. That is worth having and it is not worth over-building.
