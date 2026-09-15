# Deferred

Decisions taken and deliberately not acted on, with the reasoning attached so the
next session does not re-derive it. Items marked HIGH or broken are owed rather
than elective.

Resolved items are removed rather than annotated. If it is here, it is still
true.

**Rewritten 2026-09-15 for the live product**, when the historical backfill was
scrapped. The items that pivot made moot are listed, with their old numbers, at
the end of [`historical-ratings-postmortem.md`](historical-ratings-postmortem.md).
The old list is in git at `6cd18dd`.

---

## 1. HIGH — the published score is not repeatable

**The top open issue.** A live rating that reads 58 this week and 75 on a re-run
of the same week, from the same evidence, is a product defect. The measurement
says that can happen.

Ten repeats of the production call — `gpt-4o-2024-08-06`, `temperature=0`,
`seed=42`, strict schema — on real assembled payloads of 11–13k tokens:

| model | worst `score_12m` spread | by band (calm / moderate / stressed) | reproduced its own scored output |
|---|---|---|---|
| `gpt-4o` (production) | **17 points** | 5 / 17 / 0 | 0 of 10, every band |
| `gpt-4.1` | 9 points | 4 / 9 / 0 | 0 of 10, every band |

These are max-minus-min spreads, not ±. The weekly move the instrument exists to
detect is about 5 points. The composite also hides movement: on the stressed band
`score_12m` was 82 ten times while `sovereign_stress` flipped and
`evidence_coverage` alternated 75/85.
See `docs/historical-ratings-postmortem.md` §3.

**Caveat on the number, not the problem.** It was measured on payloads built from
the retired historical corpus. Repeat-spread on identical input should not depend
much on what the input says, so the direction is expected to hold. Re-take the
value on live payloads before quoting it. That re-measurement is the first step
whichever option is chosen, and costs a few dollars: ten repeats × three live
countries × two models.

**Options, as already identified:**

- **(a) Repeat and average.** Score N times and publish the mean or median.
  - Noise falls roughly as 1/√N, so five repeats take a 17-point spread to about
    7–8.
  - Cost and latency scale by N on the scoring call only; digests are cached, and
    the mask rewrite could be cached too.
  - Median rather than mean, because the draws are integer and bunch.
  - Also yields a per-week spread that can be published as its own uncertainty.
- **(b) Change the reported unit.**
  - **Bands:** Low / Low-Moderate / Moderate / High / Extreme, which the prompt
    already defines. A 17-point spread still crosses band edges, so bands alone
    do not solve it and probably want (a) underneath.
  - **Ledger direction:** improving / holding / decaying per ledger, week on week.
    Coarseness damages a direction far less than a level, and the stressed-band
    example above is the composite being stable while a flag was not. Direction
    of what moved may be the more honest unit.
- **(c) Change scorer.** `gpt-4.1` measured 9 points. It is cheaper per snapshot,
  and nine points is still above the weekly move. It changes the instrument
  rather than fixing its noise, so it wants (a) or (b) anyway.

**What must not happen** is publishing the integer as it stands while this is
open.

**Absorbs:** the old determinism canary (§10) and "report with an uncertainty
band" (§30). A canary — one stored live payload re-scored on a schedule,
comparing scored fields only — is how option (a) or (b) would stay honest after
it ships. Determinism is a property of how the model is served, so it can move
with no change on our side.

## 2. DECISION — masking is now a choice, not a necessity

**Not changed. Written down so it is decided rather than inherited.**

Scoring runs masked: every name, city, person, party, currency and institution
becomes the role it plays; every number is kept (`docs/pipeline.md` §7). It was
built because a **backfill** must not leak hindsight — a model scoring Türkiye in
2018 remembers how 2018 went — and because a series must be one instrument from
end to end. With no backfill, the hindsight reason is gone.

**The case for keeping it.** Masking also stops the model scoring a country from
its stored *reputation* rather than from this week's evidence. "Turkey" carries a
prior; "the country, with these numbers and these events" does not. The claim the
product rests on — this rating reflects this week's evidence — is arguably only
true under masking. Dropping it would make the rating partly an opinion about the
country's name.

**The case against.**

- It costs money and latency on every run: a model sweep over every fresh digest,
  and three body rewrites per country per run, which live does not cache.
- The rewrite fails closed, so a failed rewrite silently costs the scorer one of
  its three full texts.
- Masking removes legitimate priors along with illegitimate ones. Only 5 of 48
  countries have the `structural_facts.yaml` block that states them back.
- `assert_clean` raising `MaskLeak` costs a country its whole weekly score.

**What the decision needs, and does not have.**

- **Evidence masking works live.** The identifiability probe runs on about one
  country in six per run and writes `snapshot_diagnostic`, and **nothing reads
  it**. Its only reader, `probe_bundles`, went with the backfill. So there is
  currently no evidence either way. Before deciding, write the query: guess rate
  per country over the live rows, against the null-bundle prior. The prior
  function is recoverable from `6cd18dd:backend/llm/probe.py`. If masking stays,
  that query becomes a report; if it goes, the probe goes with it.
- **What masking changes about a live score.** Never measured on live evidence.
  The old masked-vs-named divergence (7.2 points on PT) was on the historical
  corpus.

Absorbs the old two-run masking comparison test (§7).

## 3. Broken — the frontend rewrite map

**This is broken rather than deferred.** The ten-table rebuild dissolved the tables three frontend queries read, and nothing under
`frontend/` was touched. There are no compatibility views. Every query lives in
one file, `frontend/app/lib/risk-server.ts`.

The deployed dashboard reads a third database that neither backend target is and
that still has the old tables, so whether a route breaks depends on which
database it points at.

### Survives untouched

| Route | Method | Why |
|---|---|---|
| `/api/risk` | `fetchJoinedLatestRisks` | `risk_snapshot` and `country` keep their names and every column it selects |
| `/api/risk-summary` | `fetchLatestSummaries` | `risk_snapshot.bullet_summary` only |
| `/api/prices` | `fetchMarketPrices` | `market_price` keeps its name and gains columns |
| `/api/econ-calendar` | `fetchEconCalendarEvents` | the declined merge — table unchanged |

`fetchJoinedLatestRisks` reads **every** `risk_snapshot` row per country into its
history arrays, so the 157 historical rows scored on the retired corpus would
appear as history on any database that holds them. The query to find them is in
the postmortem, §5.

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
| `fetchChannels` | already empty — `live_tv_channel` **has never existed** in the database. `terminal-seed.ts` has always been the real source for that pane. Either create the table and write to it, or delete the query and the fallback dance with it |

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

## 4. HIGH — the relevance scorer cannot name Britain or America

Found by the corpus census (`docs/retrieval-diagnosis.md` §4), **live-affecting**.

`article_ranking.score_relevance` tests `if country_lower not in text` — a
substring match on the roster's formal name. The press writes "Britain", "the
UK", "America", "Washington". Across the stored corpus, the median share of
articles clearing the relevance bar was:

- GB **6.6%** and US **10.4%**, the two highest-volume countries
- Chile 45.6% and New Zealand 35.8%, which the press calls by name

Live, Google News pre-ranks the pool, so this does not currently show. Live is
protected by the vendor, not by the scorer.

**The fix exists and is not wired.** `gazetteer.mentions(text, iso2)` resolves
every surface form. It lost its only caller, the NYT adapter, in the backfill
removal and is kept for this. It changes `score_relevance`'s signature from name
to ISO2, touches every caller and test, and will move live scores — so it wants
its own change and a before/after on a live week.

## 5. Persist the live run's articles

The weekly run discards every article it fetches once the snapshot is scored;
only the top three survive, as JSONB on the row. Nothing writes the `article`
table any more.

**Why it matters now.** Two reasons:

- **Scoring can be re-run.** §1's re-measurement, and any change to the prompt or
  the scorer, needs last week's evidence as it stood. Without stored articles, a
  re-run is a re-fetch of a different week.
- **The route back to history needs an overlap.** If a paid archive is ever
  bought, the backfill has to be shown to be the same instrument as live on weeks
  where both exist. Those weeks are being thrown away every Monday
  (postmortem §4).

**Why not yet.** `article.source_system` already has `google-news`, so this is a
new write path rather than a schema change. Roughly 50–100 rows per country per
week. The removed `store.upsert_articles` (body beats stub, idempotent) is
recoverable from `6cd18dd`.

## 6. HIGH — thirteen indicators have no source, and one ledger runs on a single one

The count was never the point. The distribution is: of the `information`
ledger's four registry codes, three are curated (`RSF.PRESS.SCORE`, `OBS.SCORE`,
`UN.EGDI`) and `curated.csv` has never held a data row, so that ledger scores on
**one** indicator, `IQ.SPI.OVRL`. `edge` is second thinnest at 2.7 of 4.
`friction` and `uncertainty` resolve about ten each.

So two of the four ledgers the whole instrument is built on carry an order of
magnitude less evidence than the other two, and `payload_health` in every live
manifest now says so.

Filling `curated.csv` is a research task with sources to cite, not a coding one.
The ranked fill order with per-source instructions is in `backend/README.md`;
`RESERVES.USD` and `STAT.TAX.TOP.RATE` first, `RSF.PRESS.SCORE` third.

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

The empty CSV is deliberate: a template with plausible-looking sample rows loads
silently, reaches the model as evidence, and produces a confident score built on
invented numbers. `python backend/main.py census PT` shows the gap per country.

## 7. The theme classifier and the relevance heuristic both mistake noise for evidence

Two shared-code defects the historical corpus made loud. Both are live-reachable.

**`core.classify_themes` tags match reports as `information` and `edge`.** It does
substring matching, and a match report is full of "attack", "defence" and "war".
On the old corpus "Southampton 2-1 Watford" was tagged `information`. Live items
are tagged by the query that found them, so the classifier is the fallback — but
the Google News query itself matches "war" inside "tug-of-war". A World Cup hosting
story scored 0.550 under `security` in a measured live week. Its own `ponytail:`
note names the word-boundary upgrade.

**`score_relevance` saturates on large countries and ties at the bar.**

- `score = 0.3` for mentioning the country is identical to the 0.3 threshold, so
  "clears the bar" means "the name appears".
- `_BODY_MENTION_CAP = 0.55` caps any article whose title does not name the
  country, which is most policy coverage and almost no sport.
- On a big country the pool ties at the cap and recency breaks the tie.

**`RELEVANCE_FLOOR_ENFORCED`** (off, `news_fetching/core.py`) stops the top-up at
the bar instead of padding to twenty. Turn it on only after measuring what live
weeks look like with it: `payload_health.articles.cleared_threshold` is already
recorded on every snapshot, so that measurement is a query.

## 8. `live_country_check.py` is broken against the current schema

`backend/util/tools/live_country_check.py` runs the real `_process_country` and
then verifies the write. Its `CORE_SCHEMA` still creates `indicator`,
`yearly_value` and `risk_snapshot_article`, and its checks query them — the same
dissolved tables as §3. So its checks fail against a database `bootstrap` built.
The README still points at it as the way to test a snapshot write.

Either rewrite its checks against `risk_snapshot` and `indicator_series`, or delete
it. It is not a `main.py` subcommand.

## 9. The `as_of` rules were built for a backfill, and live inherits them

`indicator_series.as_of` means "when this number became public".
`data_fetching/lags.py` re-dates rows from fetch date to period end plus a
publication lag, and `upsert_indicator_series` enforces that on every write. That
rule existed so a *historical* anchor could not read a number published after it.

Live, it still decides what `staleness_days` reports to the model. **Every lag
errs long**, so a live payload can call a reading older than it is.
`country_data_fetch.panel_rows` goes the other way: it stamps 31 December of the
value's own year, earlier than WDI/WGI actually publish. That was a leak for a
backtest and is harmless live.

**Decide:** keep the publication-date semantics, which is the better provenance
and costs a little staleness accuracy, or stamp fetch dates live and drop the
guard. Doing nothing is defensible; not knowing which is not. Absorbs the old
fourth-fetcher item (§24).

## 10. A fresh clone has no WEO archive

`backend/data/curated/weo_vintages/*.xls` is gitignored, so a clone starts with an
empty folder and `weo_fetch` has to recover the editions.

- The last recovery measured 13 of 19.
- **2025-10 and 2026-04 cannot be fetched at all**: the WEO database moved to
  `data.imf.org` in October 2025 and `weo_fetch.py` still points at the legacy
  path. Those two were downloaded by hand.
- The folder here holds 19 files; the two newest exist only as loaded rows.

The four `WEO.*` indicators in every live payload come only from these files.
Live reads only the newest edition, so the scattered gaps that mattered for a
backfill do not matter any more. **What matters is the next edition**, October
2026, which nothing can fetch.

**What it would take.** Point `weo_fetch` at `data.imf.org`, or check whether an
IMF SDMX dataflow serves the current WEO directly. For live the latest edition is
enough, which is much simpler than the vintage dataflow the old item wanted.

## 11. Nothing reports what a fetch failed to get

The weekly ETL reports what it wrote ("12,439 monthly row(s) written"), and that
number is dominated by BIS. A week where the IMF CPI endpoint answered 7 of 48
countries looks the same as a week where it answered all of them. That happened
on 2026-08-28 and converged on its own by 2026-08-30.

A coverage line per source — countries that got a print, out of 48 — is the
missing habit. `payload_health` covers the model's side of it; nothing covers the
fetch side.

## 12. An API layer between the two halves

A backend refactor breaks frontend routes only because the frontend queries
Postgres directly, which makes column names the contract between the halves. §3 is
what that costs.

`frontend/app/lib/risk-server.ts` is the single file holding every query, so an
API would have exactly one caller to replace. Worth deciding later whether it
belongs.

## 13. A real migration mechanism, once there is a pattern

`schema.create_all` does double duty — creation and forward migration — via the
`MIGRATIONS` tuple added for `llm_artifact.kind`. That is deliberate and it is one
constraint.

Adopt a versioned mechanism when there are two or three and something to
generalise, not a framework for a single CHECK. The signal it has outgrown the
tuple: a migration that is not idempotent, or one that must run in order relative
to another.

The CHECK constraints still admit the retired values — `run_ledger` job types
`harvest` and `snapshot`, `llm_artifact` kinds `rewrite` and `context`,
`snapshot_diagnostic` kind `arm` — because stored rows carry them. Narrowing them
means deleting those rows first.

## 14. The trend fields are computed, serialized, and read by nobody

`payload._stamp` emits `trend_1y` and `trend_5y` on every indicator — 38 per
country, every snapshot, in the JSON the prompt carries. `AI_PROMPT_V3` explains
`as_of` and `staleness_days` and never mentions them.

The two arms that tested telling the model about them (trend-prompt, p4-trend)
were measured on the retired corpus and are gone. So the question is open again,
and cheaper than it was: either name them in the prompt (a prompt version bump,
measured against §1's noise) or stop sending tokens nobody reads.

## 15. The walkthrough notebook writes to the real database

`notebooks/country_rating_walkthrough.ipynb` upserts fetched rows into
`indicator_series` with `as_of=AS_OF`, and writes the digest cache. The chokepoint
guard in `upsert_indicator_series` re-dates anything implausible, so this is
closed in effect. Recorded because a notebook that writes to production is worth
knowing about independently of what it stamps.

## 16. `testing/test_llm.py` is past 1,000 lines

1,348 lines. The agreed rule is to split only when a file passes ~1,000 lines
*and* has a genuine seam. There is one — the masking tests measure the instrument
rather than the country — but six folder files plus one invariants file is the
agreed shape, so it stays whole for now.
