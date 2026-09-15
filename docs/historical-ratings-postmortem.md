# The historical ratings: a postmortem

**Decided 2026-09-15.** The historical backfill and the scorer benchmarking harness are
scrapped. The product is a live instrument: current information, scored weekly. A historical
series is deferred until a paid news archive makes one possible.

**Recovering the code.** Everything removed is in git. The last commit before the cut is
**`6cd18dd`**:

```bash
git show 6cd18dd:backend/util/pilot/score.py            # read one file
git checkout 6cd18dd -- backend/util/pilot \
    backend/util/tools/bakeoff.py backend/news_fetching/adapters \
    backend/news_fetching/snapshot_select.py backend/news_fetching/wayback.py
```

The removal itself is three commits on `historical-ratings`: `63c9988` (the bake-off harness),
`ace8de6` (the payload and prompt variants), and `9988d2d` (the pilot, the harvest and the
vintage bound).

**What was kept is data, not code.** Every table and every row. The article corpus, the
`indicator_series` history and the WEO editions cost quota and weeks to acquire, cost nothing to
store, and a paid-archive attempt would want all of them. See *The data left behind* at the end.

---

## 1. Why it was scrapped

### The mechanism: a selection bug on top of a source that does not carry the material

Two findings, in the order they were made. The second is the one that ends the programme.

**First, the corpus was retrieved correctly and selected wrong** (`docs/retrieval-diagnosis.md`).
Both halves issued the same six risk-topic queries. What differed was the relevance scorer's input:
the historical path fed it **300 characters** of lede against the live path's **240 words**.
Most historical articles name their country in the body, not the first 300 characters, so policy
stories took the 0.1 floor while match reports — the one genre headlined with the country's name —
scored 0.45. The sub-threshold top-up then filled every snapshot to twenty from that inverted
ranking. Worse, **197 of 290** PT 2019 sport articles carried a non-`broad` theme, so they won
the two slots reserved per ledger. "Southampton 2-1 Watford" filled `information`, the
press-freedom ledger. "Phil Neville's son called into Under-19 squad" filled `edge`, which the
prompt says articles are its *only* instrument for.

Fixing the window and adding a Guardian section filter took sport in what the model reads from
**39.2% to 0%** on PT 2019. Mean selected relevance went from 0.191 to 0.247 with the top-up, and
to 0.505 with the relevance floor on.

**Second, and decisive: once the selection was fixed, there was not enough left.** The census over
the whole stored corpus — **526,220 articles, 893 country-year-source buckets** — puts the
Guardian's median share of articles clearing the relevance bar at **16.4%** (34.5% under the
corrected window). Football, sport and travel are **25.0%** of the Guardian corpus across all 48
countries; PT's 45% is the extreme, not the exception. With the floor on, PT 2019 scores on a
median of **7 articles** a week, and 13 of 52 weeks have fewer than six.

A hand spot-check of the stored articles put **90%+** of them as irrelevant to country risk: sport
and human interest, almost nothing on war, economics, trade or politics. The quiet PT week
2019-01-07, as selected, is twenty articles of which **not one is about Portugal** — Michelle
Obama's book, festive wines, Lucas Digne rescuing a point for Everton
(`retrieval-diagnosis.md` §3). The live weekly feed for the same country is Reuters- and
Bloomberg-grade risk coverage.

**PT 2019 was topped up below the relevance bar at 52 of 52 anchors.** The median anchor had 6 of
20 articles clearing it. Mean selected relevance per anchor ran from **0.120 to 0.394** — 0.120 is
the worst anchor, not the year's mean (that was 0.191).

### Paying for more of the same did not help

**newsapi.ai**, a paid index of ~150,000 publishers, was evaluated on 2026-08-28
(`docs/news-source-evaluation.md`). It returned twice the Guardian's volume for PT 2019 and a
worse fit to the ledgers. Its top publishers were **SAPO Desporto, O Jogo and Maisfutebol**. The
remedy — monthly windows per theme — would have cost ~$1,220 in overage, more than the scoring it
fed.

### The structural reason

A free or broad archive answers "what did an outlet publish that mentions this country". For most
countries, most weeks, that is sport, travel and culture, because that is most of what a general
outlet publishes about a country it is not based in. Relevance tuning can rank that pool. It cannot
add the finance-desk and wire coverage the pool never held. A wider index adds more local sport,
not more risk reporting.

The live feed works for a different reason: Google News ranks for relevance upstream across wire
and finance publishers before anything in this repo runs. **Live is protected by the vendor, not by
the code** — see the two live defects in §2.

---

## 2. What was measured and no longer stands

**All of the following were computed on snapshots built from that corpus. Do not quote them as
findings about the instrument.** `docs/results-on-the-bad-corpus.md` is the item-by-item list.

| Result | Where it was written up |
|---|---|
| **A′** (`p2-rebaseline`, the post-vintage-fix reference) and `p2-rebaseline-postfix` | `payload-ab.md` attempt 2, `scorer-acceptance.md` |
| **Both `gpt-4.1` arms** (`gpt-4.1`, `gpt-4.1-postfix`), and the benchmark-incumbent decision resting on them | `elicitation-ab.md`, `deferred.md` §11 (old numbering) |
| **p3-context** — rejected for making the instrument coarser | `payload-ab.md` attempt 1 |
| **p4-trend** (arm B) and the trend-prompt arm C | `payload-ab.md` attempt 2 |
| **The elicitation variants** — V1 within-band, V2 vs-typical, and the `gpt-4.1 × V1` crossed cell | `payload-ab.md` attempt 3, `elicitation-ab.md` |
| **`GATE2_BASELINE`** — PT 2019, the pilot's regression reference; also captured before the vintage fix, a second independent reason | removed from the repo root |
| **The determinism matrices**, "real worst" column | `scorer-bakeoff.md`, `scorer-acceptance.md` §2 |
| **The real-payload noise measurements** — the 17- and 9-point spreads, by value | `scorer-bakeoff.md` |
| The rank correlations, discrimination counts, round-number shares and the event-study corrections | `scorer-bakeoff.md`, `elicitation-ab.md` |
| 157 masked `risk_snapshot` rows and 37 `snapshot_diagnostic` arm rows | both databases |

The **rejections** are the least safe conclusions on that list. An instrument reading football
would look coarse whatever the payload did, so p3 and p4 were arguably never given a fair reading.
They are not re-run here because there is no corpus to re-run them on.

**Unaffected**, because they never read the corpus: the round-2 gates on the ~2,980-token canned
payload, the macro half (`indicator_series`, `payload_census`, the vintage findings), and the
masking work itself — masking was applied to whatever arrived, it did not choose it.

**Still true, and live:** two defects the census found in shared code.

- **`score_relevance` cannot name GB or US.** It substring-matches the formal name, and the press
  writes "Britain" and "America". GB clears the bar at 6.6%, US at 10.4%, against Chile at 45.6%.
  `gazetteer.mentions` already resolves every surface form and is not wired in.
- **`classify_themes` tags match reports as `information` and `edge`**, on substring hits for
  "attack" and "war".

Both are in `docs/deferred.md`.

---

## 3. What survives as a finding regardless

These are about the instrument — the serving layer, the grammar, the model tiers, the codebase's
habits — rather than about the evidence it was fed.

### Determinism is a property of the serving layer and the grammar, not the model

(`scorer-bakeoff.md`, *Determinism is a property of the serving layer, not the model*.)

The control: rewriting the four union types in `RISK_SCHEMA_V3` as `anyOf` — equivalent JSON
Schema, same meaning — moved `gpt-4o` from **50 ×9** to **52 ×7, 50 ×2** on identical input.
Same model, provider, temperature, seed and prompt. Only the grammar changed.

The mechanism: at `temperature=0`, GPU floating-point is not associative, so near-tied tokens flip
between runs. Strict schema enforcement masks every token that would make the output invalid,
which removes most of the near-ties before they can flip.

The honest limit, which the doc states and this summary keeps: **grammar is necessary but not
sufficient.** Five other OpenAI models on the identical schema and wrapper vary anyway. So
something further about how a particular model is served matters, and it was not identified. Two
consequences follow for a live product:

- Determinism cannot be bought — `gpt-4.1` costs twenty times `gpt-4.1-nano` and is also
  non-deterministic.
- It can change with no change on our side.

Narration is never deterministic, even when scores are. `bullet_summary` was reworded on every
repeat.

### The noise floor at real payload size is large, and it hides under a stable composite

Ten repeats, `temperature=0`, `seed=42`, on real assembled payloads (11–13k tokens rather than the
~3k canned one). The worst **spread** — max minus min on `score_12m` across the ten:

| model | canned worst | real worst | real by band (calm / moderate / stressed) |
|---|---|---|---|
| `gpt-4o` (production) | 2 pts | **17 pts** | 5 / 17 / 0 |
| `gpt-4.1` | 2 pts | **9 pts** | 4 / 9 / 0 |

These are spreads, not ±. A 17-point spread is roughly ±8.5 around the middle.
`scored_match_rate` was **0.000 on all three bands** for both models: on a real payload neither
reproduced its own scored output once in ten.

**The composite can be perfectly stable while the answer underneath it is not.** On `gpt-4o`'s
stressed band `score_12m` was 82 in all ten calls, while `condition_flags.sovereign_stress` flipped
between `False` and `True`, `evidence_coverage` alternated 75/85 and `edge_vitality` 30/40. A
spread of 0 would read as reproducible while the instrument disagreed with itself about whether
the country was in sovereign stress.

**The value was measured on bad-corpus payloads; the direction was not an artefact of them.** It
is repeat-spread on identical inputs, and identical bad inputs are still identical
(`results-on-the-bad-corpus.md`). The figure should be re-taken on a live payload before it is
quoted. The finding that the noise swamps the effects this instrument exists to detect should be
assumed to hold until then. That is now the top live issue: `docs/deferred.md` §1.

### Price and noise invert across model tiers

On the canned payload the noise floor ordered almost perfectly inverse to price: `gpt-4o` 0 points
($0.0430 per snapshot), `gpt-4.1` 1, `gpt-5.4-mini` 7, `gpt-4.1-mini` 8, `gpt-5.6-luna` 11,
`gpt-4.1-nano` 20 ($0.0017). Four of five cheaper candidates wobbled by more than a typical week's
5-point movement.

**The lesson is price per unit of usable signal, not price per token.** A model at a twentieth of
the price that wobbles by more than the effect has made the series noisier, not cheaper. Measuring
it takes thirty repeats and well under a dollar.

On real payloads the order partly flips: the dearer `gpt-4o` (17) is noisier than `gpt-4.1` (9).
So "pay more for stability" is not a rule either. The durable claim is only that noise has to be
measured per model, per payload size, before any tier is chosen.

### Code that ran, wrote, and was read by nobody

The project's recurring failure was not code that crashed. It was code that ran, wrote something
plausible, and had no consumer — so every count looked right. The docs numbered it to
**thirteen** by 2026-08-29 (`deferred.md`, old item 28). No single numbered list existed; the
compilation below is from commits and docs. It reaches fifteen with the last three, which are
counted here for the first time.

| # | What was written | What should have read it | Source |
|---|---|---|---|
| 1 | Nineteen WEO editions, 16k rows, into codes not in the registry | the payload builder | `3e4084a` |
| 2 | The WEO file's per-row `Estimates Start After` — never parsed, so forecasts loaded as fact | the edition reader | `921bae3` |
| 3 | A content-addressed digest cache the history path never called | the historical digest stage | `26fb8b7` |
| 4 | `Meter.check()`, the usage meter's check, never invoked | the pilot run | `26fb8b7` |
| 5 | Twenty probe results that lived only in a commit message | a re-probe, to measure the sweep fix | `26fb8b7` |
| 6 | `risk_lint`, written every run | anything; lint enforcement had been deleted on the promise it would be read | `a0605ea` |
| 7 | The manifest's `stage1` degradation block | a run summary | `a0605ea` |
| 8 | One snapshot in six paid for a probe whose write raised, nulling the whole manifest | the manifest | `642752d` |
| 9 | `restamp.py`, the correction for fetch-dated rows — never called, and could not have run | the ETL | `deferred.md` old §15 |
| 10 | Ten World Bank indicators stamped with the fetch date, invisible to every historical payload; information and edge resolved zero for the whole pilot | the payload | `trend-payload-findings.md` §1 |
| 11 | `trend_1y` / `trend_5y` on every indicator in every prompt, named by nothing in the prompt | the scorer | `deferred.md` old §25 |
| 12 | Four `risk_snapshot` ledger columns, 157/157 populated, read by no backend module and no frontend query | the frontend | `pipeline-audit.md` §1 |
| 13 | A pre-registered criterion reading `bullet_summary` from arm rows that never stored it — a consumer with no writer | the A/B verdict | `deferred.md` old §28 |
| 14 | The outlet-fingerprinting check's `nyt_share_gap` (−0.056), quoted in docs but computed from per-bundle `sources` the probe never stored | anyone re-deriving it | `historical-ratings.md` §4 |
| 15 | **The live identifiability probe**, still writing `snapshot_diagnostic` every week, whose only reader went with this cut | anything; filed in `deferred.md` §2 | this postmortem |

**The countermeasure that survives:** `payload_health` in every live manifest records what the
registry promised against what reached the model, and how many articles cleared the relevance
bar. **The standing rule:** every writer needs a consumer-side test, and a criterion is computed
once against a stored row before anything is paid for.

---

## 4. What a paid-archive attempt would need

Written down so the route back is not re-derived. Nothing here is priced from a quote.

### Sources

| Source | Why it is the candidate | Archive |
|---|---|---|
| **Dow Jones Factiva** | Dow Jones Newswires, WSJ and licensed business press; subject and region coding on articles | long; confirm depth per country in the quote |
| **LSEG / Reuters** (Machine Readable News and its archive) | the wire much of the live feed's best coverage comes from; topic codes | long; confirm in the quote |
| **Bloomberg** news archive licensing | finance-desk coverage of every roster country | confirm in the quote |
| **LexisNexis Nexis Data+** | broad licensed news with topic indexing; search, monitor, firehose and archive delivery | advertised as ~45 years |

The test any of them has to pass is the one newsapi.ai failed: **retrievable per theme, or its
volume is worth nothing** (`news-source-evaluation.md`). Run it on PT 2019 before signing anything,
since PT is where the free sources were worst.

### What retrieval would have to do differently

1. **Filter by the vendor's subject and section codes in the request**, not by keyword
   afterwards. Paid archives code articles by topic (economic news, government, conflict,
   commodities) and region. That coding is the thing the free archives lacked and the thing being
   bought.
2. **Feed the relevance scorer exactly what the live path feeds it.** The 300-character window was
   the first bug. Keep the scorer's input one constant shared by both paths, with a test.
3. **Match countries through `gazetteer.mentions`, not a formal-name substring.** Otherwise GB and
   US floor at 0.1 again.
4. **Do not let a theme floor be met by a substring classifier.** Use the vendor's codes for the
   floor, or turn `RELEVANCE_FLOOR_ENFORCED` on and accept thin weeks.
5. **Respect body vintage.** Old item §36 found the Guardian API serves the *current* version of an
   article (2,405 bodies carry post-publication amendment footers). Ask each vendor whether it
   serves as-published text, and store that answer per source.
6. **Persist the live feed from now on** (`deferred.md` §5). A backfill needs an overlap window
   where the same weeks exist from both the archive and the live path, to show the two are the same
   instrument. Nothing currently stores the live articles.

The vintage machinery a backfill needs, recoverable from `6cd18dd`:

- `snapshot_select`'s no-future rules
- the `vintage_as_of` bound in `payload._resolve`
- `data_fetching/vintage/monthly.py`

`data_fetching/lags.py` and `data_fetching/weo.py` are still in the tree.

### What it would cost

**No vendor publishes a price for archive or API access.** Factiva, LexisNexis Nexis Data+, LSEG
and Bloomberg all quote per customer, by content set, archive depth, delivery method and volume.
Third-party estimates for Factiva's *web* product run around $2,000–3,500 per user per year, and
the same sources say API and archive access cost more. Treat those as indicative of the tier, not
a quote.

What this project has measured, which a quote has to be set against:

| | measured |
|---|---|
| Scoring, per snapshot, `gpt-4o` | $0.043 (canned); real payloads are 4× the tokens |
| A 48-country × 10-year weekly backfill | ~25,000 snapshots — about **$1,100** of scoring at the canned rate, plus digests and masking passes; several thousand dollars at real payload size |
| The free Guardian harvest, 576 country-years | 35,712–55,152 calls, **25–39 days** of quota |
| The paid alternative evaluated | newsapi.ai: ~$1,220 overage for a worse corpus |

The licence will dominate. A decision to buy one belongs to a version of this project with
traction, and it should come with the PT 2019 per-theme test result attached.

---

## 5. The data left behind

**No table was dropped.** `CREATE TABLE IF NOT EXISTS` means removing code drops nothing, and every
table either has a live writer or holds data worth keeping.

| Table | State after the cut | Last recorded size |
|---|---|---|
| `article` | **no writer.** The corpus. | prod 526,220 (2026-09-14, harvest 332 of 576 Guardian country-years); dev 80,485 |
| `indicator_series` | live; keeps its full revision history and all 21 WEO editions | prod 227,465 rows (2026-08-30) |
| `llm_artifact` | `digest` is live; `rewrite` and `context` rows have no reader | dev 1,671 |
| `run_ledger` | scheduler rows are live; `harvest`, `snapshot` and `pilot-freeze` rows record what was done | prod 6,786 harvest rows (2026-08-30) |
| `snapshot_diagnostic` | `probe` is written live and read by nothing; `arm` rows are historical | 37 arm rows |
| `risk_snapshot` | live; also holds the 157 bad-corpus historical rows | 157 historical |

**Find the historical scores** before they are ever read as a series:

```sql
SELECT country_iso2, as_of, score
  FROM risk_snapshot
 WHERE country_iso2 IN ('PT', 'TR', 'US')
   AND as_of BETWEEN '2018-01-01' AND '2019-12-30';
```

**The harvest crons.** Two jobs on the harvest host called the removed CLI:

- `harvest-articles.sh`, every 6h (Guardian, NYT, Wayback)
- `harvest-macro.sh`, weekly (WEO, monthly restamp)

Both fail from `9988d2d` and should be disabled. The WEO editions can still be fetched with
`python backend/main.py weo-fetch` and loaded with `bootstrap`.

---

## Superseded documents

Each carries a banner pointing here. They are kept as the evidence behind this page.

| Doc | What it was | Still applies live |
|---|---|---|
| `historical-ratings.md` | how the backfill worked | the masking description now lives in `pipeline.md` §7 |
| `retrieval-diagnosis.md` (+ `-anchors.txt`) | the selection finding and the census | §2 *Is the live path affected?* and §8 *Two things left* |
| `results-on-the-bad-corpus.md` | what was measured on the corpus | — |
| `news-source-evaluation.md` | newsapi.ai, rejected | the per-theme retrievability test |
| `scorer-bakeoff.md` | which scorer | the determinism mechanism, the noise and price findings in §3 |
| `scorer-acceptance.md` | the adoption bar for a candidate scorer | the local-endpoint walkthrough, if a scorer is ever swapped |
| `payload-ab.md`, `elicitation-ab.md` | the payload and prompt arms | — |
| `trend-payload-findings.md` | the vintage bug and instruction-following under ambiguity | §4, the tooling that makes it visible |
| `pipeline-audit.md` | readiness for a candidate scorer | the unread-columns finding (§1, stage 2) |

## The deferred list, before and after

`docs/deferred.md` was rewritten for the live product. Items made moot by the pivot, by their old
numbers:

| Old § | Item | Why moot |
|---|---|---|
| 5 | WEO vintage dataflows may retire `weo_vintages/` | only mattered for point-in-time vintages |
| 6 | `data_upsert` / `news_fetching` package cycle | the adapters were one side of it |
| 7 | Two-run masking comparison test | its consumer, `probe_bundles`, is gone; folded into the masking decision |
| 8 | `SCORING_MODES` vs the schema | the diagnostic modes are gone |
| 11 | Production stays `gpt-4o`, benchmark incumbent `gpt-4.1` | the benchmark is gone; production stays `gpt-4o` |
| 12 | Within-band discrimination | variant removed |
| 14 | The Guardian daily allowance | no harvest |
| 16 | `util/pilot/` placement | deleted |
| 18 | `source_system` carries two facts | only matters with a second body source writing `article` |
| 19 | Guardian body-length floor | no harvest |
| 20, 20a | `--until`; BR's failed Guardian windows | no harvest |
| 21 | newsapi.ai | recorded here |
| 22 | The 2015–2016 lead-in | no backfill |
| 23 | 11.6% of Guardian bodies clipped at 24k | no Guardian bodies are read |
| 27 | GATE2 on the degraded payload | GATE2 is gone |
| 28, 32, 33 | Pre-registration defects in the A/B arms | the arms are gone; the lessons are in §3 |
| 29 | The event study | no historical series to study; revisit with a paid archive |
| 31 | Phase C sample size | no Phase C |
| 35 | The pilot's regression baseline | the pilot is gone |
| 36 | `usable_body` trusts `api-native` | `snapshot_select` is gone; the lesson is in §4 |
