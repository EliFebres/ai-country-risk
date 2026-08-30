# How a country gets a risk score

From raw data to a number on the map, end to end. Companion to
[`historical-ratings.md`](historical-ratings.md), which covers the same pipeline
pointed at the past.

The authoritative sources are the code: `backend/util/pipeline.py` orchestrates,
`backend/data_upsert/schema.py` defines every table, `backend/llm/constants.py`
holds the prompt. Where this document and the code disagree, the code is right.

---

## 1. What the thing produces

For each of the **48 countries** in `constants.COUNTRY_ROSTER` (the MSCI
Developed and Emerging Markets indices plus Russia), once a week:

- a **0–1 investor-risk score** over a 12-month horizon, plus a 3-month one
- a bullet summary written by the model
- four ledger scores, condition flags, per-article impacts, and an input
  manifest recording exactly what the model saw

Everything lands in Postgres. The Next.js frontend reads that database directly —
there is no API layer between the two halves.

**There is no trained model and no weighted formula.** The score is one
frontier-LLM judgement under a versioned prompt. All the deterministic
arithmetic in this pipeline is *input* to that judgement, never applied on top of
its output.

## 2. One process

`backend/main.py` is the scheduler, not a subcommand of one. It ticks every 30
minutes, asks Postgres when each job last finished, and runs whatever is overdue.

| Job | Cadence | What it does |
|---|---|---|
| `prices` | every tick | FMP quotes for whichever markets are open; a no-op outside session hours |
| `etl` | first tick of a new ISO week | the whole pipeline below, all 48 countries |
| `panels` | every 30 days | refetch every World Bank annual so revisions land |

"When did this last run" lives in the `run_ledger` table, not in memory, so a box
that was down for ten days comes back and catches up. A job is stamped **only on
success**, so a failure retries next tick instead of waiting out its whole
interval. Deleting a job's `run_ledger` row forces a re-run.

```bash
python backend/main.py            # run forever — this is what production runs
python backend/main.py --once     # one pass over every due job, then exit
```

Six subcommands share the same executable. Each dispatches to a module that owns
its own arguments, so `--help` after a subcommand is that module's own help.

| Command | What it is for |
|---|---|
| `bootstrap` | build an empty database into a working one — six idempotent steps |
| `backfill` | the History Machine CLI (see the companion doc) |
| `rebuild PT 2019-06-03` | re-derive a stored snapshot and diff it against the stored row |
| `probe --recorded` | re-probe stored bundles for identifiability |
| `census PT` | every registry indicator against what actually arrives |
| `weo-fetch` | download IMF WEO editions |

The weekly `etl` job, in order (`main._run_etl`):

```
upsert_countries(COUNTRY_ROSTER)         # 0   seed the roster + structural facts
backfill_missing_panels()                # 0a  World Bank annuals for new countries
pipeline.refresh_calendar()              # 0b  FMP econ calendar, AI-ranked
pipeline.refresh_imf_indicators()        # 0c  IMF sub-annual prints
pipeline.refresh_ledger_sources()        # 0d  extra WB codes, BIS, curated.csv
pool = pipeline.process_all_countries()  # 1-7 per-country risk snapshots
pipeline.publish_global_alerts(pool)     # 8   the global alerts feed
```

Each phase owns its own resilience boundary: a failure is logged with a full
traceback and the run continues, so one flaky upstream or one bad country costs a
single phase rather than the whole week.

## 3. Getting the country data

Every macro observation, from every source, at every frequency, ends up as a row
in **one** table — `indicator_series` — through
`data_push.upsert_indicator_series`. There used to be three overlapping stores
with different vintage semantics, and the same number could be stale in one and
current in another.

| Source | Auth | Module | What it supplies |
|---|---|---|---|
| **World Bank** | none | `data_fetching/country_data_fetch.py`, `wb_series_fetch.py` | the annual macro panel plus the extra WDI/WGI/SPI/HCI registry codes |
| **IMF SDMX 2.1** | none | `data_fetching/imf_macro_fetch.py` | monthly/quarterly prints, so a fast-moving economy is not stuck on a year-old annual |
| **IMF WEO** | files on disk | `data_fetching/vintage/weo.py` | 21 per-edition vintages of inflation, growth, debt, net lending, current account |
| **BIS bulk** | none | `data_fetching/bis_bulk_fetch.py` | policy rates (`WS_CBPOL`) and USD exchange rates (`WS_XRU`) |
| **Our World in Data / V-Dem** | none | `data_fetching/political_corruption_fetch.py` | the political-corruption index |
| **Financial Modeling Prep** | `FMP_API_KEY` | `fmp_calendar_fetch.py`, `fmp_prices_fetch.py` | the ~14-day economic calendar, live quotes, treasury yields |
| **Google News RSS** | none | `news_fetching/fetch_links.py` | the headlines |
| **Crawlbase** | optional token | `news_fetching/advanced_scraper.py` | JS-rendered thumbnails, Top-3 only |
| **OpenAI** | `OPENAI_API_KEY` | `llm/client.py` | digests, scoring, masking, ranking |
| **Hand-typed** | — | `data_fetching/curated_loader.py` | `backend/data/curated.csv` and `curated/structural_facts.yaml` |

Credentials live in `backend/.env` (see the annotated `backend/.env.example`).
Endpoints and tuning live in `backend/util/constants.py`.

`INDICATOR_REGISTRY` in that file is the catalogue: 38 codes, each with its
label, unit, frequency, source and the ledger it feeds. A code that is not in the
registry is a code nothing reads. `python backend/main.py census PT` prints the
registry against what actually arrives for one country — thirteen codes have no
reachable source today, which is recorded in [`deferred.md`](deferred.md).

### `as_of` is when it became public, not what period it covers

`indicator_series.as_of` is the day a reader could first have seen a number. It
is a different fact from `period`, which is what the value describes, and the
whole no-future rule rests on this column being honest.

For the daily run the two are easy: every observation genuinely did arrive today.
For anything historical they are not, and `data_fetching/vintage/lags.py` holds
the per-indicator publication lags that re-date a row from "when we fetched it"
to "when it was published". **Every lag errs long**, and is floored at period end
so an observation can never predate the period it describes. Where a source
publishes a real release date — the WEO editions do — that date wins over the
estimate, and the row is stamped `vintage_scheme = 'as-published-edition'` rather
than `'publication-lag-estimate'`.

## 4. Getting the news

`article_enrichment.fetch_relevant_news(country_name, max_articles=20)`:

1. **Six Google News RSS queries** per country — one per theme the prompt scores
   (`friction`, `order`, `security`, `information`, `edge`) plus a broad
   catch-all. One query reliably misses whole categories of news, and a query set
   that does not match the ledgers misses whole ledgers.
2. **Dedupe** by resolved URL and headline key.
3. **Relevance score** — `article_ranking.score_relevance`, a keyword heuristic
   that filters out the sport and entertainment a bare country query drags in, so
   tokens are not spent having the model reject them. The threshold is 0.3, and
   it *orders* the pool rather than capping it.
4. **Per-theme floor** of 2 inside the 20-article budget, so an election week
   cannot crowd out the tax and press-freedom stories the friction and
   information ledgers need.

Then `resolve_and_enrich`: unwrap the `news.google.com` redirect wrappers to real
publisher URLs, drop denylisted publishers
(`news_fetching/blocked_sources.txt`), and one GET per article recovers a
summary, body text (trafilatura, capped at 24k chars) and a thumbnail.

Every stage degrades rather than raises. A country with thin coverage still
produces a snapshot.

## 5. Two-stage scoring

Feeding twenty full article bodies to the scoring model is unaffordable; feeding
it twenty headlines throws away the reporting. So the funnel narrows twice.

**Stage 1 — digest every article** (`llm/digest_engine.py`,
`gpt-4o-mini-2024-07-18`). A cheap model reads each article's full text and
returns strict JSON: `what_happened`, `actors`, `numbers`, `transmission`,
`directly_about_country`, and a 0–100 `stage1_severity`. It is an **extraction
engine, not an analyst** — told to use only the text in front of it, and to write
`"not stated"` rather than fill a gap from outside knowledge. It never sees the
macro payload, the other articles, or the scoring rubric, and it never produces a
risk score. Digests are cached in `llm_artifact` keyed on a hash of the digested
text, so a same-day re-run makes roughly zero stage-1 calls.

`stage1_severity` is used for exactly one thing: picking which three articles the
scorer reads in full.

**Stage 2 — score** (`llm/langchain_llm.py`, `gpt-4o-2024-08-06`, temperature 0,
`seed=42`, one structured-output call). The prompt carries:

- `EVIDENCE_JSON` — the four-ledger evidence payload
- `ARTICLES_JSON` — **every** article's digest
- `FULL_TEXT` — the two or three highest-severity bodies, capped at 12k chars each

Breadth from the digests, depth from three.

## 6. The evidence payload

`llm/payload.py::build_evidence_payload` assembles what the model scores on. Two
stores feed it, and for each indicator the **freshest period wins**:
`indicator_series` for everything observed, and `structural_facts.yaml` for the
static half — currency-union membership, reserve-currency status, monetary
sovereignty. Structure rather than reputation.

Every value is stamped with `period`, `freq`, `as_of`, `staleness_days`, `source`
and `unit`, and the prompt is told to weigh a fresh reading over a stale one.
**An indicator with no observation is omitted entirely** — absence is absence,
never a zero and never a padded null, because a zero reads as reassurance.

`util/metrics.py` does the deterministic arithmetic so the model does not have to
do it in its head: `conversion_loss`, `frictional_extraction`, `doom_loop`,
`real_policy_rate`, `rome_gap`, `monetary_dilution`, `suppressed_vol_flag`,
`instrument_quality` and the rest. Each is a pure function, and **any missing
input yields `None` rather than a fabricated zero** — a metric absent from the
payload is a statement about our evidence, not about the country.

Everything is passed *into* the builder rather than read inside it, so the
builder is pure and re-runnable over history, and each read degrades on its own.
No database, or no curated rows, costs the country that evidence and not its
score.

## 7. The country is not named

**Masking is the production regime, not an experiment.** `scoring_mode` defaults
to `'masked'` and `PROMPT_VERSION` is `v4.5-no-publisher`. Before the
payload is sent, every country name, city, person, party, currency and
institution is replaced by the role it plays — "the country", "the capital", "the
central bank" — while **every number is left exactly as written**.

The reason is the historical series: a model asked to rate Türkiye in 2018 may
simply remember 2018. Scoring on evidence rather than on recall is what makes a
2016 backfill and tomorrow's live run the same instrument, and doing it only in
the backfill would defeat the point.

The priors a name would have carried are supplied instead, by the `structural`
block. The three masking layers, the gate that refuses to send a leaky payload,
and the meter that measures whether any of it works are covered in
[`historical-ratings.md`](historical-ratings.md).

## 8. The score

The prompt (`llm/constants.py::AI_PROMPT_V3`) asks for four ledger scores rather
than a list of risk factors:

| Ledger | The question it asks | A high number means |
|---|---|---|
| **Friction** | what the state extracts, times how much of it fails to convert into capability | a worse wedge |
| **Order-uncertainty** | are the load-bearing rules — contracts, currency, statistics, succession — legible? | less underwritable |
| **Information capacity** | can the country's own instruments be trusted to measure it? | **weaker** instruments |
| **Edge vitality** | is the system still learning — firms forming, failing, inventing? | more vitality, and **this one may never raise a risk score** |

Edge vitality is explicitly protected in the prompt. A country where firms are
born and die quickly is discovering what works; a country where nothing is
created and nothing fails is not stable, it is inert. Failure counts as vitality.

The model answers **integers 0–100**, because that grid has the cross-sectional
rank resolution a 48-country roster needs. `langchain_llm` converts every one
back to 0–1 the moment the call returns, so the 0–100 scale never escapes that
module — the database and the frontend speak 0–1.

```python
score = _from_100(data["score_12m"])   # backend/llm/langchain_llm.py
```

That is the **only** assignment to `score` anywhere in the backend, and there is
a tripwire test (`test_llm.TestNothingElseAssignsAScore`) that greps for exactly
that.

Bands, on the model's own 0–100 scale: `5–20` Low, `20–40` Low-Moderate, `40–75`
Moderate, `75–90` High, `90–98` Extreme.

## 9. What no longer edits the score

Two earlier designs did. First the floors lived in the prompt, then they moved to
a versioned enforcement layer that overwrote the model's numbers after the call —
condition-flag floors, inflation tiers, a political-stability cap, and a
sanctions gate that forced a score to `1.0`. All of it is deleted, at
`POLICY_VERSION = "p2.0-observe-only"`.

Two things survived, and the distinction between them is the design:

- **A badge, not a floor.** Whether US persons may lawfully hold a country's
  securities is a fact about the sanctions regime, not an opinion about risk.
  `util/policy.py` returns `non_investable` plus the triggering rule, stored
  *beside* the score; the frontend renders a RESTRICTED badge. A sanctioned
  country keeps whatever score its evidence earned — which is also the only way
  its series stays readable across the date a sanctions regime starts or ends.
- **An observation, not a correction.** When the model flags an active war and
  then scores the country 44, `util/lint.py` writes both down next to each other
  and lets a human look. Nothing reads a lint finding back to change a score, and
  `pipeline.log_run_summary` prints them at the end of every run — an advisory
  tripwire nobody reads is indistinguishable from no tripwire.

## 10. Provenance

`util/provenance.py::build_input_manifest` records what the model actually saw,
so a stored score can be reproduced — or found to be irreproducible — later. Per
article: a hash of the body we held, and a hash of the exact text the prompt
carried. Plus the macro vintage, the model id, the prompt version, the policy
version, the seed, and under masking the five masking version stamps and the
identifiability probe's answer.

Hashes rather than copies: the point is to *detect* that an input changed.

Provenance is metadata, not the product. The whole assembly is wrapped — a bug in
building the manifest degrades it to `NULL` and the snapshot still writes.

`python backend/main.py rebuild PT 2019-06-03` re-derives a stored snapshot and
diffs it against the row.

## 11. Where it all lands — ten tables

Defined in one place, `backend/data_upsert/schema.py`, created idempotently.
There is no migration tool: every statement is `IF NOT EXISTS`, so `create_all`
on a live database is a no-op and a half-finished bootstrap resumes rather than
restarts.

| Table | Holds |
|---|---|
| `country` | ISO2, name, map coordinates, and the `structural` facts masking cannot replace |
| `article` | every article from every source; `source_system` separates google-news / guardian / gdelt / nyt |
| `llm_artifact` | content-addressed model output — `kind` is digest or rewrite, `mode` is masked or named |
| `indicator_series` | **every** macro observation at any frequency, one key, one vintage rule |
| `risk_snapshot` | the product: score, summary, ledgers, flags, top articles, lint, manifest |
| `snapshot_diagnostic` | everything measuring the *instrument* rather than the country — probes and diagnostic arms |
| `run_ledger` | one row per unit of work: scheduler jobs, harvest windows, scored anchors |
| `market_price` | live prices plus their quarter/year-start reference closes |
| `news_alert` | the globally ranked alerts feed, replaced whole each run |
| `economic_calendar_event` | upcoming events with an AI importance score |

Ten, down from twenty. What absorbed what:

| Now | Absorbed | Why |
|---|---|---|
| `country` | `country` + a structural-facts YAML | roster identity plus the facts masking removes |
| `article` | `historical_article` | one article store; `source_system` already separated them |
| `llm_artifact` | `article_digest` + `history_digest_cache` + `history_rewrite_cache` | one content-addressed store, so two snapshots can no longer disagree about the digest of identical text |
| `indicator_series` | `indicator_series` + `yearly_value` + `recent_indicator` + `indicator` + a Parquet panel | one key and one freshest-wins resolution instead of a three-way merge |
| `risk_snapshot` | `risk_snapshot` + `risk_snapshot_article` + `risk_lint` | per-article impacts and lint findings are per-snapshot detail, as JSONB on the row that owns them |
| `snapshot_diagnostic` | `probe_result` + the diagnostic arms' results | the instrument/country line, drawn in the schema |
| `run_ledger` | `job_run` + `harvest_checkpoint` + `history_run_ledger` | one row per unit of work; every old query is a prefix of the new key |
| `market_price` | `market_price` + `price_reference` | same subject, different cadence |

`economic_calendar_event` was **not** merged into `news_alert`, deliberately. They
share the theme "dated thing the frontend shows" and nothing else, and the
decisive argument is the failure mode: the alerts query has no `kind` filter, so a
merge that kept the name would silently render calendar events in the AI-alerts
pane. Wrong output, no error. `live_tv_channel` was never created at all.

Three environment assumptions are stated rather than inherited: no Postgres
extensions beyond `plpgsql`, the database's own collation (reported by
`schema.verify()` so a surprise is visible rather than silent), and `timestamptz`
everywhere — never naive `timestamp`, so no value re-dates itself by the deploy
region's offset.

## 12. Out to the dashboard

The Next.js app queries Postgres directly through
`frontend/app/lib/risk-server.ts`, a pooled `pg` client behind
`unstable_cache`-wrapped fetchers. Column names are therefore the contract
between the two halves.

| Route | Serves |
|---|---|
| `/api/risk` | latest score + history per country, with map coordinates — the map's first paint |
| `/api/dashboard` | indicators, articles, summaries, trends, alerts and calendar in one composed request |
| `/api/prices` | the prices daemon's `market_price` rows |
| `/api/econ-calendar`, `/api/indicators`, `/api/articles`, `/api/risk-summary` | individual topics |

Cache TTLs live in `frontend/app/lib/cache-ttl.ts` — risk, summaries and
alerts 12h, indicators 24h, articles and calendar 6h, prices 30 min (the
scheduler's own tick).

> **Known breakage.** `/api/indicators` and `/api/articles` still query tables the
> ten-table rebuild dissolved, and therefore 500; `fetchIndicatorAverageTrends`
> and `fetchChannels` fail quietly and return empty. The replacement SQL for each
> is written out in [`deferred.md`](deferred.md).

## 13. Running it yourself

```bash
python -m venv venv && source venv/bin/activate
pip install -r backend/requirements.txt
# fill backend/.env — DATABASE_URL, OPENAI_API_KEY, FMP_API_KEY

python backend/main.py bootstrap    # empty database -> working system
python backend/main.py --once       # one pass over every due job
python backend/test.py              # the suite; no network, no database
```

`backend/notebooks/country_rating_walkthrough.ipynb` rates one country from
scratch and shows its work through the same code paths. It makes real network
calls and spends OpenAI credits, and it writes no snapshot.
