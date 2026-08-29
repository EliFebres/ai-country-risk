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

## 11. The `gpt-4o` migration is priced: ~$747, and it is a re-score

**Decision taken 2026-08-27: stay on `gpt-4o-2024-08-06`.** No cheaper model was
adopted. This item exists so the *next* decision — the one deprecation forces —
is made from a measurement rather than under time pressure.

`gpt-4o` is a 2024 model and this series is meant to run for years, so the move
is not optional, only unscheduled. `gpt-4.1` is the stated successor: $2/$8
against $2.50/$10, better instruction-following, 1M context.

**What it costs, measured over 52 US-2019 anchors** (`docs/scorer-bakeoff.md`):

| | |
|---|---|
| Repeat-stability | **±1 point**, flat across Low, Moderate and Extreme — 20% of a typical week's move. Good enough to be an instrument. |
| Level offset | **−0.008 signed** — essentially none |
| Week ordering | **ρ = 0.377, τ = 0.297** composite; `edge_vitality` **−0.100** |
| Per-week disagreement | **0.089** absolute, against a 0.050 median weekly move |
| Re-score cost | **~$747** for 25,104 snapshots at $0.0298 each (~$62 for the 2,092-snapshot pilot) |
| Running cost after | **31% cheaper** than the incumbent |

**It is not a recalibrate-and-go migration, and that is the load-bearing part.**
A constant level offset would be survivable — move the prompt's calibration
anchors and the series shifts with them. `gpt-4.1` instead moves individual weeks
in both directions and cancels to −0.008 on average. There is no constant to
remove, so **switching means re-scoring the history rather than adjusting it.**

Two consequences for whoever picks this up:

- Budget the **$747 and the wall-clock**, not just the price difference. The
  articles and digests are already stored, so a re-score is scorer-only.
- A series assembled half on `gpt-4o` and half on `gpt-4.1` is two instruments
  wearing one name. `score.FROZEN_FIELDS` refuses that resume, which is correct
  and will look like an obstacle on the day. It is not; it is the guard working.

**The ρ figure is window-dependent — use 0.708, not 0.377.** US 2019 is an
ordinary year for a stable country, where models scatter because the right answer
is underdetermined; the same comparison on TR 2018 gives **ρ = 0.708, τ-b 0.602,
`score_3m` 0.865**, with per-week disagreement of 0.046 sd against 0.108. So
`gpt-4.1` largely tracks the incumbent where the evidence is determinate. The
migration is still a re-score rather than a recalibration — there is no constant
offset to remove on either window — but the series it produces is recognisably
the same series. See `docs/scorer-bakeoff.md`.

**Related:** item 10, the determinism canary. `gpt-4o`'s determinism appears to
be a property of how it is served, so it can move without notice — which would
turn this from an elective migration into an urgent one. The canary is what would
tell us.

## 12. Within-band discrimination is a prompt problem, not a payload one

The pre-registered fallback from the payload A/B (`docs/payload-ab.md`), proposed
and deliberately not run.

**What the A/B established.** Trailing quarterly context was added to fix
coarseness — nine distinct scores across fifty-two weeks, a third of them exactly
0.50 — and it made coarseness *worse*: seven distinct values, round-number share
up from 69% to 75%, and not one of fifty-two anchors changing band. The model was
handed a year of history in the form it asked for and became less discriminating,
not more. More evidence is not the lever.

**Why the prompt is the remaining suspect**, on evidence already collected:

- It says *"use precise values (37, 62, 81) — never round to multiples of 5"* and
  is disobeyed on **69–75%** of US 2019 anchors against a 20% chance floor. An
  instruction that is ignored three times in four is not an instruction.
- Its five calibration anchors (12/38/58/85/95) sit near band centres, so the
  worked examples pull toward exactly the values the series over-produces.
- **Nothing in it asks the model to separate two weeks inside one band.** Every
  US 2019 anchor is "Moderate", and the prompt gives no vocabulary for
  "Moderate, and worse than last week".
- Compliance is evidence-dependent, not fixed: the same model on the same prompt
  rounds on 69% of anchors where the evidence is ambiguous and 19% where it is
  determinate. The instruction holds exactly where it is least needed.

**The shape of the test.** A prompt variant against the same two windows and the
same criteria, changed in one place: an explicit within-band instruction, e.g.
*"Two weeks in the same band must differ unless the evidence is genuinely
identical; the second decimal is where that difference goes."* Reuse the p3
harness — `PROMPT_VARIANT` alongside `PAYLOAD_VARIANT`, the same `series_shape`
meters, the same pre-registered thresholds, arm A free from stored rows.

**Why not yet.** The scorer question is settled and the payload question is now
settled; a prompt change moves `PROMPT_VERSION`, which is frozen, so it is a
third instrument change and belongs in its own session with its own
pre-registration rather than appended to this one. It should also be weighed
against the cheaper answer: **report the series with an uncertainty band and stop
claiming resolution the instrument does not have.** That costs nothing and may be
the honest fix.

## 13. A real migration mechanism, once there is a pattern

`schema.create_all` now does double duty — creation and forward migration — via
the `MIGRATIONS` tuple added for `llm_artifact.kind`. That is deliberate and
documented at the block, and it is one constraint.

Adopt a versioned mechanism when there are two or three and there is something to
generalise, not a framework for a single CHECK. The thing to watch for: a
migration that is not idempotent, or one that must run in a specific order
relative to another, is the signal that the tuple has outgrown itself.

## 14. The Guardian daily allowance is not a constant

`docs/scorer-bakeoff.md` carries a roster estimate derived from one measurement:
1,461 page-calls before `X-RateLimit-Remaining-Day` reached zero on 2026-08-15.
On 2026-08-28 the wall arrived after **328**.

So the remaining harvest — KR 2023–2026, all of BR, US 2024–2026, reported by the
harness as 18 country-years and ~774 calls — is **two to three days, not one**,
and any estimate quoting 1,461 should be re-derived from observed daily rates
rather than from a single day's ceiling. The harness already reports what it
spent and what is left every time it stops, which is the right place to read this
from.

## 15. `restamp` is a correction tool the ETL never calls

Three of the four live macro fetchers stamp `as_of` with the **fetch date**:
`wb_series_fetch.py:53` says so in its own docstring ("the date we learned these
values — the fetch date"), and `imf_macro_fetch.py` and `bis_bulk_fetch.py` both
default to `date.today()`. The fourth, `country_data_fetch.py:85`, stamps 31
December of the value's own year and escapes the problem.

Measured on 2026-08-28: **11,810 rows carry today's fetch date** — IMF CPI 2,989,
WB WDI 2,938, BIS XRU 2,880, BIS CBPOL 1,920, WB WGI 528, WB SPI 414, WB HCP 141.
The 24,844 `World Bank panel` rows are unaffected.

`vintage/restamp.py` exists to correct exactly this, and **nothing calls it**.
`apply()` is reachable only from the `backfill restamp` CLI branch; neither
`main._run_etl` nor `bootstrap` invokes it. (`monthly.restamp()` is a different,
inline function that `monthly.py` does apply to its own rows.) So every macro
fetch re-creates the condition and a human has to remember to clean up.

**Which direction it leaks.** Not contamination — starvation. `payload._resolve`
drops any observation with `as_of > anchor`, so those 11,810 rows are invisible
to *every* historical anchor. A 2019 payload does not read a wrong number from
those seven sources; it reads no number, arrives thinner, and nothing says why.
`restamp.py`'s own docstring states it: "with a single fetch date on everything
**a 2019 snapshot sees zero rows from this table**". The present-day payload is
fine, since `as_of = today ≤ today`.

This is the ninth instance of the write-a-thing-nobody-calls pattern, and it is
the load-bearing one: it sits directly under the historical scoring the whole
corpus is being harvested for. It needs a decision, not a comment — call it from
the ETL after every macro fetch, or fix the fetchers to stamp a publication date
and retire it. Deliberately not resolved in the session that found it.

## 16. `util/pilot/` holds a harvest CLI that belongs under `news_fetching/`

The folder rule is that code lives where its use lives, and `backend/util/` is
not a drawer for the awkward. `backend/util/pilot/run.py` is a 492-line CLI whose
subcommands span four packages: `guardian`/`nyt`/`gdelt`/`wayback` are
`news_fetching`, `weo`/`monthly`/`restamp` are `data_fetching.vintage`,
`score`/`diagnostic` are LLM-driven, and `report`/`pilot-report` are read-only
reporting. Only the harvest half has an obvious home elsewhere.

Splitting it is a wide, mechanical change across every doc and docstring that
names `backend.util.pilot.run`, and it was deliberately not done in the same
session that put the harvest on a cron — a rename landing at the same time as
new automation makes both harder to bisect. Worth doing once the harvest has
converged and the CLI is not being invoked four times a day.

## 17. The IMF CPI endpoint answers about one country in six

Measured on 2026-08-28, first weekly macro run across the full roster:

| source | rows | countries |
|---|---|---|
| BIS XRU | 6,912 | 48 |
| BIS CBPOL | 4,608 | 32 (BIS publishes policy rates for 32; not a failure) |
| **IMF CPI** | **919** | **7 of 48** |

41 of 48 countries returned nothing: 15 read timeouts at the 40s ceiling, 15
HTTP 503, 11 HTTP 500. `imf_macro_fetch` degrades a failure to an empty series
by design, so the run logs INFO and exits 0 having fetched almost no CPI.

**It is not rate limiting introduced by widening the roster.** Successes and
failures interleave from the first country — PT and US fail, TR succeeds, KR and
BR fail, BE succeeds — rather than clustering after a burst, so this is the
endpoint being unreliable per request. Widening the roster from four to
forty-eight only made it visible.

It half-converges: `indicator_series` upserts are idempotent and the job is
weekly, so a country that timed out this week may land next week. At ~7 a run
that is months to fill, and nothing reports the shortfall — the run says "12,439
monthly row(s) written" and the number is dominated by BIS.

Worth either a retry-with-backoff pass over the countries that came back empty
within the same run, or a coverage line in the summary that names how many of
the roster actually got a CPI print. Priority is low while the corpus is being
harvested and no scoring is running, but a payload census before the first
historical scores should check it rather than assume it.

---

## 18. `source_system` carries two facts, and a merge overwrites one of them

**Sized, and dormant again.** It is not a bug today because only one source has
ever supplied a body for a given URL — Guardian writes bodies, NYT never does,
so the branch below has never fired. It becomes one the moment a second body
source exists, and the corruption is silent and irreversible.

The third source that made this urgent was evaluated and rejected
(`docs/news-source-evaluation.md`), so this is back to latent. Left here at full
size because the sizing is the expensive part and it will be wanted verbatim the
day another source is considered.

`store.upsert_articles` resolves a URL collision with `ON CONFLICT (url) DO
UPDATE`, and one branch of that update reads:

```sql
source_system = CASE WHEN EXCLUDED.body IS NOT NULL
                     THEN EXCLUDED.source_system
                     ELSE article.source_system END,
```

So the column answers "who discovered this row" until somebody supplies a body,
and "who supplied the body" afterwards. With Guardian and NYT that never fires —
NYT never writes a body. With newsapi.ai in the mix, a newsapi.ai body landing
on a URL the Guardian already discovered **rebrands the Guardian's row**, and
every count keyed on `source_system` moves with it: `counts_by_year`,
`recovery_curve`, `reports.evidence_texture`, and `probe.source_mix_caveat`,
which reads `nyt_share` to say whether an identifiability result is confounded
by the source blend. All of those are measured against a Gate-2 baseline that
assumed a fixed mix.

**The fix is two columns.** `source_system` for discovery, immutable, set once by
whoever first inserted the row; `body_source` for whoever supplied the body
currently stored, mutable, following the existing CASE.

**Estimated ~6 files, ~30 lines**, and it is *not* only a column plus a backfill
— that is why it was estimated rather than done:

| Site | Change |
|---|---|
| `schema.py` TABLES | `body_source TEXT` on `article` |
| `schema.py` MIGRATIONS | `ADD COLUMN IF NOT EXISTS`, then `UPDATE article SET body_source = source_system WHERE body_source IS NULL AND body IS NOT NULL` — correct precisely because today's column already means "who supplied the body" for rows that have one |
| `store.article_row` + `_ROW_COLUMNS` | set and carry it |
| `store.upsert_articles` | drop the `source_system` CASE, add it on `body_source` |
| `store.recovery_curve` | group by `body_source`; it is a body-outcome curve |
| `snapshot_select.to_item` | decide which fact `source` means — the probe reads it |
| `reports.py:238,545` | the per-source bucket |

**Why it waits.** It rewrites the merge semantics of every existing row, which
does not belong in the same commit as a new adapter. And its correctness lives
entirely in the DB-gated tests (`TestBodyBeatsStub`,
`TestBodyStatusTransitions`), which skip unless `HISTORY_TEST_DATABASE_URL` is
set — so doing it without standing up a test database first would be changing
the most dangerous SQL in the codebase unverified.

**Do it before the first write from any second body source**, not on a
schedule. Nothing is at risk while Guardian is the only source writing bodies.

---

## 19. The Guardian adapter has no body-length floor, and now we know what that cost

**Measured 2026-08-28: almost nothing, and that is the finding.**

`adapters.guardian` decides a body is a body with `if item.get("text")` — bare
truthiness, so a one-character string is stored as `body_status='recovered'` and
nothing downstream re-checks it. No adapter in the tree applies a length floor.

The obvious worry was that the corpus's headline body-coverage number had never
been validated, and that Gate 2's evidence quality and the p3 context blocks
were built on stubs. Audited over all 51,872 Guardian rows marked `recovered`:

| below 1,000 chars | below 400 | null | mean | median | min |
|---|---|---|---|---|---|
| 131 (0.25%) | 6 | 0 | 8,234 | 5,599 | 206 |

So the missing check is a **latent** risk, not a realised one: the Guardian
returns whole articles, and 0.25% of rows sitting under a floor it never
promised to clear is not a corpus problem. Gate 2 was not overstated.

Two things follow. First, the floor is not worth retrofitting to Guardian for
data-quality reasons — 131 rows. Second, the distribution is **continuous, not
bimodal** (206→390: 6 rows, 800→997: 72, 1,600→1,799: 329, rising smoothly), so
there is no natural cut to read off it. Any floor is a judgement call rather
than something the data hands over, which is worth knowing before the same
question is asked of a source that aggregates 150,000 publishers.

The query is one `SELECT` over `article WHERE body_status = 'recovered'`
grouped by `source_system`, counting rows under the floor. Read-only.

---

## 20a. BR's Guardian harvest failed eleven times and nothing retried it

**Found 2026-08-28 while pricing a paid news API to fix a gap BR did not have.**

`run_ledger` holds eleven `status='failed', note='request error'` rows for
`variant='guardian', country_iso2='BR'` — every attempted window, zero articles,
one call and ~20 seconds each — and two windows never attempted at all. BR has
**zero** Guardian rows in `article`; its entire corpus is NYT abstract-only,
which is why it fails all five theme floors where every other harvested country
passes.

The Guardian answers BR fine: one live call returned `pages=15, total=1421` for
2019 alone. This is a transient failure that was checkpointed and forgotten.

**It resumed on its own** — only `done` is skipped, so `run guardian --country BR`
retried all eleven. BR 2019 alone came back with 1,421 rows in 65 calls and now
passes every theme floor at 0% short. The remaining ten BR years are ~300 calls,
comfortably inside a day's allowance, and are still owed.

The real deferred item is not the retry, it is that **nothing reports this**.
Eleven consecutive failed checkpoints on one country produced no summary line
anybody read, and the gap surfaced only because a purchase decision went looking
for it. `reports.harvest_pacing` already reads the ledger; a line naming
countries whose windows are mostly `failed` would have caught it months ago.

---

## 20. No harvest subcommand takes `--until`, so a single country-year is inexpressible

`run.py` gives `--since` and `--country` to every harvest and `--until` to none
of them, so a harvest always runs from its floor to today and **a single
country-year cannot be expressed**.

That cost real time during the 2026-08 source evaluation: comparing one
country-year across sources meant calling `guardian.harvest_window` directly and
hand-writing the checkpoint, because the CLI had no way to say "just 2019".
Adding `--until` to the shared `for name in (...)` loop and threading an `until`
parameter through `guardian.harvest` and `nyt.harvest` is a small change and it
is the difference between a one-line comparison and a scratch script.


---

## 21. Closed — newsapi.ai was evaluated and rejected

Kept as a pointer rather than deleted, because the next person to notice that
half the corpus has no bodies will have the same idea.

The keyword top-up this item used to describe is moot: the adapter is removed
(`git show 458140a`, `d707a8e`, `4d97c63`). The measurement that killed it is in
**`docs/news-source-evaluation.md`** — a broad concept query against an index of
~150,000 general-news publishers returns sport, so the source supplied twice the
Guardian's volume and a worse fit to the ledgers, and the remedy cost more than
the scoring it fed.

The one durable requirement, if a body source is ever sought again: **it has to
be retrievable per theme, or its volume is worth nothing.**

---

## 22. The 2015–2016 lead-in was never harvested — a p3 prerequisite

`config.HARVEST_FLOOR` is `2015-01-01`, but the earliest checkpoint in
`run_ledger` anywhere is **2016-08-03**. Every country is missing its 2015 and
2016-to-August windows: they are not `failed`, they were never attempted, so
nothing retries them and `completed_windows` cannot tell them from windows that
do not exist.

Something moved the floor after those runs and no migration went back for the
lead-in.

**Why it is a prerequisite rather than a nice-to-have.** The trailing-context
work (p3) reads a quarter of history behind each anchor, and `PILOT_START` is
2016-08-03 — the *same date* as the earliest checkpoint. So the first anchors in
the series have no lead-in behind them at all, and a context block built there is
reading an absence it cannot distinguish from a quiet quarter. Any p3 rebuild
should either harvest the lead-in first or refuse to build context for anchors
whose window predates the corpus.

Cheap to check, cheap to fix: `run guardian --since 2015-01-01` re-derives the
windows and skips everything already `done`.

---

## 23. 11.6% of Guardian bodies are clipped at `core.MAX_BODY_CHARS`

Measured 2026-08-28: **6,165 of 53,377 Guardian rows have a body of exactly
24,000 characters**, which is `core.MAX_BODY_CHARS`. 6,175 sit in the top 100
characters below it. That is not the API's limit, it is ours — applied in
`guardian.to_item` and in `wayback` before anything downstream sees the text.

It is very probably fine and it has never been checked. The scoring stage reads
`FULL_TEXT` for only the top two or three articles at 12k characters each, so a
24k clip is twice what the prompt uses and the truncation is invisible to the
score. What it does touch is `content_sha256`, which hashes the clipped body —
so the provenance hash identifies "the first 24k of this article", not the
article, and a future change to `MAX_BODY_CHARS` would silently re-hash 6,165
rows into apparent changes.

Worth one decision rather than one investigation: either the cap is part of the
provenance contract and should be recorded next to the hash, or bodies should be
stored whole and clipped at read time. Doing nothing is defensible; not knowing
which is not.
