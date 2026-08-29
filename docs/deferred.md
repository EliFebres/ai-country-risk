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

## 3. HIGH — thirteen indicators have no source, and one ledger runs on a single one

**Raised 2026-08-29 from a footnote to the top of this list.** The count was
never the point. The distribution is: of the `information` ledger's four
registry codes, three are curated (`RSF.PRESS.SCORE`, `OBS.SCORE`, `UN.EGDI`)
and `curated.csv` has never held a data row, so that ledger scores on **one**
indicator — `IQ.SPI.OVRL`, and only since the vintage fix made it visible at
all. Before that fix it scored on **none**, at every backfilled anchor, for the
length of the pilot. `edge` is second thinnest at 2.7 of 4.

`friction` and `uncertainty` resolve about ten each. So two of the four ledgers
the whole instrument is built on are carrying an order of magnitude less
evidence than the other two, and nothing in the output said so until
`payload_health` started counting.

That is a hole in the instrument rather than a missing input to any one
experiment, and it may bear on why the ledgers behave oddly — `edge_vitality`
resolving a whole year into three distinct values (`docs/scorer-bakeoff.md`)
reads differently once you know it had at most three indicators underneath it.

Filling `curated.csv` is a research task with sources to cite, not a coding one,
and is deliberately not squeezed into a session that was doing something else.
The ranked fill order with per-source instructions is `backend/README.md:217`;
`RESERVES.USD` and `STAT.TAX.TOP.RATE` first, `RSF.PRESS.SCORE` third.

### The original item



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

## 11. The scorer choice, reopened on an axis that was never weighed

**The 2026-08-27 decision was to stay on `gpt-4o-2024-08-06`, and on the axes it
weighed it was right.** Migration cost ~$747, rank agreement 0.708, no constant
offset to remove. Nothing below overturns any of those numbers.

**What changed on 2026-08-29 is that a third axis got measured.** The scorer
bake-off compared candidates on determinism, rank correlation and price. It never
compared them on *discrimination* — how many distinct values the instrument can
produce on a window where the evidence does not decide — because at the time
nobody had a reason to think the scorer was what limited it. Four experiments
then spent $25.72 looking for that limit in the payload and the prompt, and the
answer was in `backend/bakeoff/US-2019/gpt-4.1.json` the whole time.

`docs/elicitation-ab.md` has the full arc. The one comparison, on byte-identical
payload, prompt, digest model, gazetteer, sweep, seed and `git_sha`:

| US 2019 | distinct | round share | bands occupied | longest run |
|---|---|---|---|---|
| `gpt-4o` (A′) | 8 | 76.9% | `Moderate` **52 of 52** | 4 |
| `gpt-4.1` | **18** | **5.8%** | LowMod 6 · Mod 43 · High 3 | 2 |

This is a fork, not a recommendation. Both sides, stated as fairly as the
evidence allows:

**For moving.** Eighteen distinct values against eight, and thirteen against nine
on TR. A round-number share of 5.8% against 76.9%, on a prompt that instructs
against rounding. Three bands used against one — the incumbent has never, in any
arm, put a single US 2019 anchor outside `Moderate`. And it is **~22% cheaper per
snapshot on tokens sent** ($0.0309 against $0.0397, cache-neutral). Six
interventions on payload and prompt could not buy any of that; one model swap
bought all of it at no prompt cost.

**Against moving.** Repeat-stability of ±1 point where `gpt-4o` is exactly 0. A
re-score of ~$747 rather than a recalibration, because there is no constant
offset to remove — `score.FROZEN_FIELDS` will refuse the resume, correctly. And
the one that actually decides it:

**No evidence yet that the finer output is the more correct output.** On TR 2018,
which contains a large unambiguous crisis, every `gpt-4o` cell rises into
August–September (+0.078, +0.051, +0.047) and both `gpt-4.1` cells drift *down*
through it (−0.019, −0.014). `gpt-4.1` opens the year above where the incumbent
peaks and never distinguishes the lira collapse from January. Its five largest
weekly moves land in January, April and late December; the incumbent's largest
lands on the week of 2018-08-13.

That check is weak — one country, one crisis, article count as a crude proxy for
evidence movement, and near-zero |Δscore| correlations for *both* models, which
may indict the proxy. It is not a reason to reject `gpt-4.1`. **It is the reason
not to spend $747 before item 29 is done.** Resolution and correctness are
different properties, and only one of them has been measured. A model that
spreads noise across thirty buckets scores better on discrimination than one that
is coarse and right.

**Sequencing, which this changes.** The scorer choice must now settle **before**
the local-model screen. Payload and prompt were already required to be final
first, so that a candidate is measured against a fixed instrument; the scorer is
now on that list, because whichever model is chosen defines the bar, and the two
candidates set it ten distinct values apart on US 2019.

**Related:** item 10, the determinism canary — `gpt-4o`'s determinism appears to
be a property of how it is served, so it can move without notice and turn this
from elective into urgent. Item 29, the event study that unblocks the fork.

## 12. Closed — within-band discrimination was run, and the elicitation was not the constraint

**Run 2026-08-29, both variants rejected.** Kept as a pointer because this item
drove four sessions of work and the conclusion is the opposite of what it argued.

`docs/elicitation-ab.md` is the write-up; `docs/payload-ab.md` attempt 3 has the
pre-registered criteria and the verdicts.

The test this item specified was run almost exactly as written — an explicit
within-band instruction, same two windows, same criteria, pre-registration
written cold. It failed, and *how* it failed is the finding:

**The model obeyed.** It named a band and placed its score inside it on all 105
anchors, coherently: measured as position within the band it itself named,
`lower-middle` averages 0.38, `middle` 0.57 and `upper-middle` 0.88 of the way
through. Only 2 of 52 US rows fall outside the band they named.

**And the instrument did not resolve.** Distinct values stayed at 8, and all 52
US anchors stayed in `Moderate` — because across fifty-two weeks the model used
three placement buckets inside one band. Asked to split one coarse judgement into
two decisions, it made two coarse decisions. This item's hypothesis was that
"nothing in the prompt asks the model to separate two weeks inside one band";
something now does, and the separation is not there to be asked for.

The round-number share did fall, 76.9% → 67.3%, the first drop in six
interventions. So the instruction reached the *snapping* without reaching the
*resolution* underneath it, and cost a month of lag on TR doing it.

**The diagnosis in this item needs one correction.** It states as a general
finding that "an instruction is followed where the evidence is determinate and
ignored where it is not", measured twice. Both measurements are real and both are
`gpt-4o`'s. Across six scorers on the identical prompt, only the incumbent shows
a large window-dependent gap in round-number share (50.3 points); `gpt-4.1-nano`
and `gpt-4.1-mini` show it *reversed*, and `gpt-4.1` barely rounds on either
window. It is a property of this model, not a law about models under ambiguity,
and the difference matters because the second reading points at the scorer while
the first points at the task.

**What it argued for instead.** This item also proposed the cheaper answer —
*report the series with an uncertainty band and stop claiming resolution the
instrument does not have.* That is now the live option, and it is item 30.

The two variants stay in the tree behind `PROMPT_VARIANT`, unset. See item 11 for
where the discrimination question actually went.

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

## 15. Closed — the fetchers date their own rows, and the upsert enforces it

Kept as a pointer because the diagnosis here was right and the cost it named
turned out to be larger than it estimated.

This item said three of four macro fetchers stamped `as_of` with the fetch date,
that `restamp.py` existed to correct exactly that and nothing called it, and that
the leak was *starvation* rather than contamination — a 2019 payload reading no
number rather than a wrong one. All correct.

What it under-counted was the damage. Measured on 2026-08-29 at a 2019 anchor,
the pilot corpus resolved **14.7 of 38 indicators per country**, and the
**information and edge ledgers resolved zero** — not thin, empty, at every
backfilled anchor for the length of the pilot. After the fix: 23.3 of 38, with
information at 1.0 and edge at 2.7. Everything measured on a backfilled anchor
was measured through that, the p2 reference and the GATE2 baseline included.

`restamp` also could not have run: `read_all()` called
`data_push._INDICATOR_SERIES_DDL`, deleted in the ten-table rebuild, so every
path into the module raised `AttributeError` before touching a row. And its
`apply()` upserted re-dated rows without deleting the originals — `as_of` is in
the primary key, so it would have *duplicated* every row and left the fetch-dated
copy, which carries the later date, still winning `_resolve`'s freshest-wins
tie-break. It would have reported success and changed nothing anybody reads.

Both fixed, the migration run against both databases, and the root cause closed
at the chokepoint: `upsert_indicator_series` now re-dates any row whose `as_of`
is implausibly late for its period, so the next fetcher added is not the fourth
instance. See `git show fd3fa8d`, `d4a1017`, `f8db7d7`.

**What is still owed** is item 24 below: the fourth fetcher, which stamps too
*early* and leaks in the other direction.

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

## 24. The fourth fetcher stamps too *early*, and that leaks

`country_data_fetch.panel_rows` (`country_data_fetch.py:101,119`) escaped the
fetch-date bug by stamping `as_of = min(31 December of the value's own year,
today)`. That is why it was the one annual path that always worked, and it is
also wrong in the opposite direction.

WDI and WGI annuals land **9–18 months** after the year they describe — WGI
publishes around September of year+1, which is why `lags._DEFAULT_BY_FREQ["A"]`
is a full 365 days. Stamping 31-Dec-2018 on a WGI 2018 score claims it was
public on a date when it did not exist, so an anchor between January and
September 2019 reads a number nobody had. That is **leakage**, and it is the
quiet direction: starvation makes a payload visibly thin, while leakage makes a
backtest look good.

Its current-year row is also capped at `today`, giving an `as_of` *earlier* than
its own period end — a shape `lags.within_bounds` rejects. Deliberate, and
pinned by `test_invariants.py:485`.

Not fixed here, and deliberately outside the chokepoint guard added in
`fd3fa8d`, which only re-dates rows stamped implausibly *late*. Re-dating these
would move every backfilled annual by up to a year and change every historical
score, so it wants its own session, its own before/after measurement, and its
own re-baseline — the same treatment the late-stamping bug just got. The
guard's test asserts it is left alone, so the exemption is recorded rather than
accidental.

## 25. The trend fields were computed, serialized, and read by nobody

`_stamp` has emitted `trend_1y` and `trend_5y` on every indicator since p1 —
38 indicators per country, every snapshot, in the JSON the prompt carries. On
the fixed payload they are populated on 22 of 23 resolved indicators for US.

Nothing reads them. No consumer, no test, no report, and `AI_PROMPT_V3`, which
explains `as_of` and `staleness_days` in the same breath, never mentions them.
`docs/pipeline.md:184` lists the stamped fields and omits them. `payload.py:15`
states the intent plainly — *"a couple of change horizons are included, enough
for the model to reason about trend and level"* — so the data was put there for
exactly this and the instruction was never written.

Another instance of the write-a-thing-nobody-reads pattern (item 15 numbered it
at nine before this session added two more), and the first where the unread
artifact was the exact thing a later session set out to build from scratch: a
brief arrived proposing a computed trend block, and most of it was already in
the payload, unmentioned.

It is being measured rather than left — arm C of `docs/payload-ab.md` attempt 2
is one prompt paragraph pointing at these two fields and nothing else. The
standing rule this argues for: **every writer needs a consumer-side test**, and
`payload_health` now counts these two fields for exactly that reason.

## 26. The notebook writes clock-stamped rows to the real table

`notebooks/country_rating_walkthrough.ipynb:459-471` upserts fetched rows
straight into `indicator_series` with `as_of=AS_OF`, the snapshot anchor. Better
than `date.today()` — it cannot leak into the anchor being scored — but it is
still not a publication date, and it writes to production.

The chokepoint guard in `upsert_indicator_series` now catches it, so this is
closed in effect. Left recorded because a notebook that writes to the real
database is worth knowing about independently of what it stamps.

## 27. GATE2_BASELINE was captured on the degraded payload

`GATE2_BASELINE.md` / `.json` (PT 2019, 52 masked anchors) were captured before
the vintage fix, so every number in them was produced with the information and
edge ledgers resolving zero indicators. The file's "Captured under" block
records nine version stamps and none of them moved, which is precisely the
problem: the contract looks identical and the evidence underneath it is not.

Re-capturing is ~$2.20 and was not in this session's budget. Until it happens,
the baseline is a regression check against a run that cannot be reproduced —
comparing a post-fix run to it will show a difference on every meter, and that
difference is the bug fix rather than a regression.

The durable fix is the one now in place: `input_manifest.payload_health` records
how many indicators each run actually resolved, so a future baseline states its
own evidence depth instead of leaving it to be inferred from a version tuple.

## 28. A criterion pre-registered against a field nothing writes

Attempt 2 of the payload A/B pre-registered criterion (d) as *"share of
`bullet_summary` outputs referencing direction"* — the diagnostic p3 lacked, and
the one meant to tell an *ignored* block apart from a *diluting* one.

Bake-off arm rows carry every number a run produced and none of its prose. So
the field the criterion reads does not exist on the arm it was written for, and
(d) went unmeasured on the run it existed to serve. It was noticed only when the
verdicts were computed — after both arms had been paid for.

The thirteenth instance of the write-a-thing-nobody-reads pattern, and the first
that this project caused in its own instrumentation rather than found in its
code: not a writer without a consumer, but a *consumer* specified without a
writer. Recorded here rather than quietly dropped, because a session spent
building `payload_health` to catch exactly this shape and then committing the
mirror image of it is the most useful kind of example.

`bullet_summary` is now captured on every arm row *scored after that commit*
(`git show c788470`) -- which turned out to be one of the three arms it was
added for, because A-prime and C had already run. See item 33. The durable lesson is narrower than
"add a test": **a pre-registered criterion should be computed once against a
dry-run or a stored row before the arms are paid for.** A criterion that cannot
be evaluated is indistinguishable, at write time, from one that can.

## 29. HIGH — the event study, which is the only thing that unblocks the scorer choice

**Raised 2026-08-29.** Item 11 is a fork with one blocker: no evidence that a
finer instrument is a more correct one. Every measurement this project has on
discrimination — distinct values, round-number share, bands occupied, run
lengths — answers *does the instrument resolve*. None answers *does it resolve
onto anything real*, and a model spreading noise across thirty buckets beats a
coarse-and-right one on all four.

The indicative check in `docs/elicitation-ab.md` points the wrong way for
`gpt-4.1` and is too weak to act on:

| TR 2018 | Jan–Feb | Aug–Sep | move into the crisis |
|---|---|---|---|
| A′ (`gpt-4o`) | 0.712 | 0.790 | **+0.078** |
| V1 (`gpt-4o`) | 0.736 | 0.786 | +0.051 |
| `gpt-4.1` | 0.811 | 0.792 | **−0.019** |
| `gpt-4.1` × V1 | 0.747 | 0.732 | **−0.014** |

Every `gpt-4o` cell rises into the lira crisis; both `gpt-4.1` cells drift down
through it. Also: neither model's |Δscore| correlates with |Δarticle count|
(−0.068, −0.114), and for both, weeks where a condition flag flipped moved *less*
than weeks where none did. And every cell peaks in Q1, which no reading of 2018
explains and which nobody has looked into.

**Why it is not conclusive.** One country, one crisis, one year. Article count is
a crude proxy and condition flags are the model's own output, so the near-zero
correlations may indict the proxy rather than the models. The Q1 peak is
unexplained and could be a payload artifact that swamps everything else.

**The shape of the test.** A dated event list for two or three countries with
real crises in the harvested range, built from a source independent of the
scoring payload, and a check of whether score moves cluster near events more than
chance — per scorer, on the same anchors. It is Phase E work in sequence but item
11 cannot close without it, and item 11 now gates the local-model screen.

**Cheap first step, before any of that:** find out what the Q1 peak is. It shows
up in all five TR cells, and if it is an artifact of the evidence rather than the
scorers, it contaminates every crisis-response number above.

## 30. Report the series with an uncertainty band, and stop claiming resolution

**Raised 2026-08-29**, promoted out of the old item 12, where it was the
"cheaper answer" that four sessions of instrument work kept deferring. It is now
the only option on the table that costs nothing and is certainly correct.

Whatever the scorer choice, the stored series has less information than its row
count suggests. On US 2019, `gpt-4o` produces nine distinct values across
fifty-two weeks, a third of weeks are identical to their neighbour, and every
anchor sits in one band. That is not fifty-two independent observations.

Two things follow, and neither needs the fork resolved first:

- **Publish a band, not a point.** The instrument's own repeat noise (±1 point
  for `gpt-4.1`, 0 for `gpt-4o`) is the wrong width; the right one is closer to
  the granularity it actually uses, which on the ambiguous window is nearer five
  points than one.
- **Say which weeks are indistinguishable.** A run of identical scores is
  information — it means the instrument could not separate those weeks — and
  presenting it as a flat line implies a stability nobody measured.

**Related:** item 31, which is the modelling consequence.

## 31. Phase C inherits a sample-size question, not a modelling one

**Raised 2026-08-29.** Recorded so it is not rediscovered as a modelling failure.

A series with ~9 distinct values across 52 weeks, a third of weeks unchanged, and
every anchor in one band has an effective sample size far below its row count.
Fitting a level model to it will produce fit statistics computed against a
quantised target, and they will look better than the instrument deserves.

This argues for **predicting rating changes rather than levels** — a change of
zero is a real observation, and the coarseness that ruins a level model is much
less damaging to a direction model.

**It is deliberately not decided here.** It should be decided against whichever
scorer item 11 settles on, because the candidates differ by roughly a factor of
two in exactly the quantity that decides it: 8 distinct values against 18 on the
same window.

## 32. Criterion (e) was measuring the provider's prompt cache

**Found 2026-08-29, fixed, and three published verdicts are corrected.**

`cost_summary` reports realised spend, which is the right number for a budget and
the wrong one for a comparison: realised spend depends on the prompt cache, and
the cache depends on which arm ran immediately before on the same anchors.

V2 made it unmissable — it ran straight after V1, hit a **90.8% cache share
against A′'s 3.9%**, and reported −36% per snapshot while sending *more* tokens
than A′ in both directions. On tokens it is +2%.

`bakeoff.cache_neutral_per_snapshot` now prices the tokens each arm sent, at
list, so run order cannot move the number. Repriced:

| arm | published | cache-neutral |
|---|---|---|
| B — trend block, TR | +17.0%, recorded as **(e) FAIL** | **+14.9%**, inside the line |
| C — trend-prompt, TR | −10.2% (cheaper) | **+1.5%** (dearer) |
| p3-context, TR | −3.9% (cheaper) | **+37.1%** (dearer — 3.53 calls/snapshot against 1.15) |

No rejection reverses; all three were rejected on (a). But arm B's recorded
reason for failing (e) was wrong, and two arms were described as cheaper than the
baseline when they were dearer.

**The durable lesson**, and it is the same shape as §28: a criterion should be
computed against a stored row *and* checked for what else could move it. (e) was
computable from day one, which is why the §28 dry-run rule did not catch this —
it returned a real number every time, and the number was measuring something
nobody named.

## 33. `bullet_summary` is captured on one arm of the three it was added for

**Recorded 2026-08-29**, the tail of §28.

The §28 fix — capturing `bullet_summary` on bake-off arm rows — landed with arm
B. A′ and C had already been scored, so the field exists on `p4-trend.json`
(52/52 US, 53/53 TR) and on neither of the other two. Any future criterion
reading it can baseline against B only.

Not worth a re-score on its own. Worth knowing before someone pre-registers
against it a second time and discovers it after paying.
