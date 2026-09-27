# Deferred

Decisions taken and deliberately not acted on, with the reasoning attached so
the next session does not re-derive it. Items marked HIGH or **broken** are owed
rather than elective.

Resolved items are removed rather than annotated. If it is here, it is still
true.

Started 2026-09-22, during the v2.0 payload work (Parts 1–6).

---

## 1. The two curated series go stale on their own cadence, and nothing notices

`RSF.PRESS.SCORE` and `OECD.PISA.MEAN` are acquired by running
`python -m backend.data_fetching.curated_build` and committed as data. Nothing
polls them, nothing warns when they age, and `payload_health` will report them
as resolved right up until someone notices the year is wrong.

This is the same shape as every other "written once, read forever" problem in
this codebase: the file loads, the census counts it, and the number is from
three years ago.

| Series | Cadence | Current edition | Next expected |
|---|---|---|---|
| RSF World Press Freedom Index | annual, published on World Press Freedom Day | 2026 index (assesses 2025) | **early May 2027** |
| OECD PISA | triennial | PISA 2022 (published 5 Dec 2023) | **PISA 2025 results, early December 2026** |

PISA 2025 is the nearer one and it lands inside this project's normal working
life. When it does, `curated_build.PISA_*` constants need the new round, the new
statlink, and a new set of spot checks — the validation anchors are per-edition
by design, so a new round fails loudly rather than loading against the old
table's expectations.

The cheap fix, when someone wants it: have `payload_health` compare each curated
indicator's `period` against the edition the registry says is current, and say
so in the run output. That is a consumer for a fact nobody currently reads.

## 2. Taiwan resolves almost nothing, and only from curated sources

Taiwan is in the roster (MSCI EM) and is not a World Bank member. It is absent
from the World Bank API, from IMF SDMX and from most OWID series, so nine of the
eleven panel-fed registry codes will resolve to nothing for it, permanently.

What it does have: **RSF 2026 gives Taiwan 75.44** and **PISA 2022 gives Chinese
Taipei a mean of 533.21**, so `information` and `edge` are the two ledgers Taiwan
can actually score on — which is the opposite of every other country, where
those two are the thin ones.

Where the missing data would come from, if someone wants to close it:

- **DGBAS** (Directorate-General of Budget, Accounting and Statistics) —
  national accounts, CPI, unemployment, the bulk of what WDI would give.
- **Taiwan Ministry of Finance** — fiscal position, tax revenue, interest
  payments.
- **Central Bank of the Republic of China (Taiwan)** — policy rate, reserves.

Governance z-scores (WGI political stability, rule of law) have no Taiwan
equivalent at all; V-Dem does cover Taiwan, so `OWID.VDEM.CORRUPTION` may be
recoverable through a different OWID join.

**Handled, not fixed:** the census shows Taiwan's zeros in every run, because
state that is always the same is still information. The alarm is separate and
fires on *change* against a country's own recent runs, so Taiwan resolving zero
does not shout every week. An alarm that always fires stops being read.

**Also check:** whether any other roster country resolves under about half the
registry. That is the same problem at a smaller scale and nobody has looked.

## 3. GB and US retrieve more `information` coverage under their colloquial names

`roster_name_probe`, 2026-09-22, ten per theme over a 30-day window:

| | roster name | colloquial | total | `information` theme |
|---|---|---|---|---|
| GB | United Kingdom 47 | Britain 54 | ×1.15 | **0 → 4** |
| US | United States 53 | America 58 | ×1.09 | **3 → 8** |

Neither total clears the 1.25 materiality bar, so neither is overridden. But
`information` is the thinnest ledger, and both names roughly double or better
its retrieval — GB retrieves *nothing at all* for press freedom, transparency
and official statistics under "United Kingdom".

Why it is not simply changed: recall is not the whole question. "America" also
matches Latin America, South America and Central America, and the totals cannot
see the precision cost. The relevance gate can — it labels every candidate —
so the honest way to settle this is to run both names through the gate on a live
week and compare *eligible* counts rather than fetched ones.

That belongs with Part 7's verification, where the gate is being measured
anyway.

## 4. The three candidate gate models agree with each other on only 69–75% of articles — **closed 2026-09-27**

Closed by Eli's blind labels (`docs/blind-labels.csv`, v2 draw, 30 full
articles): 2 structural, 17 incident, 11 irrelevant. Which model is right is
not the question the labels answer, because none of them draws Eli's line.

| labeller | vs Eli, all rows | vs Eli, Eli's structural+incident rows | vs Claude's sealed labels |
|---|---:|---:|---:|
| `gpt-4o-mini-2024-07-18` | 7/30 | 5/19 | 21/30 |
| `gpt-4.1-mini-2025-04-14` | 0/9 | 0/5 | 5/9 |
| `gpt-4.1-nano-2025-04-14` | 3/9 | 0/5 | 1/9 |
| Claude, sealed | 7/30 | 5/19 | — |

The two 4.1 models have labels on the nine "models disagreed" rows only;
`blind_sample` ran them over eight fixed countries, and the other 21 rows are
`gpt-4o-mini` alone. On those nine, `gpt-4o-mini` is 1/9.

**The mechanism is the bar, not the model.** Of the articles each model called
structural, Eli called none structural: `gpt-4o-mini` 0 of 8 (6 incident, 2
irrelevant), `gpt-4.1-mini` 0 of 7, `gpt-4.1-nano` 0 of 4. Eli's two structural
rows (b20, b24: Russian drone strikes at the Poland–Ukraine border) are ones the
gate called `incident`, by the prompt's own rule that a single occurrence
"however dramatic" is an incident. Claude's sealed labels, read against the
prompt's definitions, agree with `gpt-4o-mini` 21/30 and with Eli 7/30. The gate
is executing its prompt faithfully, and the prompt does not describe Eli's line.
7/30 is below the ~11 that chance would give from the two label mixes. The
labelling page is id-keyed and its ids match the key, and no shifted alignment
does better, so this is not a misalignment.

The decision rule, applied as written, picks `gpt-4.1-nano` (3/9 against 1/9
and 0/9). It was not applied. The comparison covers nine contested rows, nano's
three agreements are all `irrelevant`, and it uses `incident` on none of the
nine, which is the collapse the tie-break exists to prevent. A model switch
does not fix a bar that no model meets. `DEFAULT_MODEL` stays
`gpt-4o-mini-2024-07-18`, already pinned, so the relevance cache key is
unchanged. The open question moves to §15.

## 5. `_rank_ids_by` raises on mixed timezone-awareness

A pinned bug, recorded rather than fixed, and inherited rather than introduced.
Two articles with equal impact and differing tz-awareness reach a datetime
comparison and raise `TypeError`. Equal impacts are routine, because the caller
fills missing scores with `setdefault(aid, 0.0)`.

`backend/testing/test_util.py::test_rank_ids_by_mixed_tz_raises` pins it, so a
fix is a deliberate, visible act rather than an accident. The Top-3 selection
this function serves is being replaced when the scorer is rewritten, which may
delete the problem rather than fix it.

## 6. Masking is not built, and the seam is marked

Not a decision this session was asked to make. The seam is `llm_artifact.mode`,
which is in the artifact primary key and carries `'named'` throughout, plus the
point between payload assembly and the scorer call where a `mask_payload()`
would sit.

Keeping `mode` in the key now means masking can be switched on later without a
migration and without a stale cache serving a named digest to a masked run —
which would put a country's name into a prompt that was supposed not to have
one, with every other check passing.

## 7. `risk_snapshot.score` changes meaning and the front-end has not been told

When the scorer is rewritten, `score` goes from a 0–1 float to a 0–100 integer,
and the dashboard reads that column directly
(`frontend/app/lib/risk-server.ts`). The dashboard will show wrong numbers until
the front-end session catches up. This was accepted deliberately rather than
worked around; it is recorded here so it is not rediscovered as a bug.

## 8. Both READMEs now describe an instrument that no longer exists

`README.md` and `backend/README.md` still document the pre-v2.0 product, and
several sections are now actively wrong rather than merely incomplete:

- the coverage universe is given as 56 countries; it is 48
- the AI prompt is reproduced in full, and it is the old 0-1 `conflict_war`
  prompt, not `RISK_PROMPT`
- the schema table lists `indicator` / `yearly_value` / `recent_indicator` and
  omits `article`, `llm_artifact` and `payload_census`
- `backend/README.md` reproduces the DDL for seven tables, and that copy is now
  stale: `country` and `risk_snapshot` have both gained columns

The DDL half of this is **fixed**, and not by choice. Prose does not run: the
first end-to-end run on a fresh database reached the upsert and died on
`relation "indicator" does not exist`. `indicator`, `yearly_value`,
`risk_snapshot` and `risk_snapshot_article` are now provisioned by
`data_upsert/schema.py` like everything else, so the code is the record.

What is left is the prose itself. Both READMEs still describe the old
instrument, and a README rewrite belongs with the front-end work, where
`risk_snapshot.score` changing meaning has to be dealt with anyway (item 7).

## 9. The no-round-numbers instruction is followed about two-thirds of the time

The prompt says never to return a multiple of 5, because a 55 or a 70 usually
means a band was picked rather than a country assessed. Measured on the first
two live runs:

| | round numbers returned |
|---|---|
| first wording (framed around "a rating") | 5 of 8 |
| after tightening it to cover every score returned | 4 of 12 |

The composites obey it (38, 36, 54, 52). The residue is entirely in the **ledger
scores** — 20, 45, 40 — which suggests the model treats a ledger reading as a
coarser judgement than the rating, whatever the prompt says.

Left as measured rather than tuned further. Two more prompt rewrites would move
the number without anyone knowing whether the scores got better or just less
round, and the round-number share is exactly the kind of thing that should be a
line in the determinism study rather than something chased by hand. Re-measure
it there, across more than two countries.

## 10. Kuwait scored 58 and then 54 on consecutive runs a few minutes apart

Same week, near-identical evidence — the article set differed by one, and the
payload fingerprint changed accordingly, so this is not a clean repeat. It is
recorded because a 4-point move on an unchanged week is the shape of the problem
the determinism study exists to size, and because it is the first live
observation of it on the v2 payload.

Do not quote it as a noise measurement. A real one needs the same fingerprint
scored N times, which is Session B's smoke check and a later session's proper
study.

## 11. The front-end now reads three things that no longer exist

The ten-table schema renamed and removed columns the dashboard queries directly.
`frontend/app/lib/risk-server.ts` is the only file involved and nothing under
`frontend/` was touched this session, by instruction.

| Query | Breaks on |
|---|---|
| `fetchJoinedLatestRisks` (`/api/risk`) | `risk_snapshot.as_of` is now `run_date`, and `score` is a 0–100 integer rather than a 0–1 float |
| `fetchLatestSummaries` (`/api/risk-summary`) | the same `as_of` rename |
| `fetchLatestArticlesForLatestSnapshots` (`/api/articles`) | `risk_snapshot_article` is gone; the top three are `risk_snapshot.top_articles` JSONB |
| `fetchLatestIndicatorValues` (`/api/indicators`) | `indicator`, `yearly_value` and `recent_indicator` are all gone; values are in `indicator_series`, keyed by code rather than joined by display name |
| `fetchIndicatorAverageTrends` | the same table swap |
| `fetchMarketPrices` (`/api/prices`) | `market_price` is keyed `(symbol, ts)` now, so the query needs `DISTINCT ON (symbol) ... WHERE role = 'quote' ORDER BY symbol, ts DESC` |
| `fetchEconCalendarEvents` | `id` is now a derived `event_id`; the query selects columns rather than the key, so this one may survive |

The first two are the ones that previously worked, so this session broke them.
That was accepted deliberately when the key was decided — the brief specifies
`(country, run_date)` — rather than discovered afterwards.

The indicator labels the front-end used to join on live in
`constants.INDICATOR_REGISTRY` now. Either hard-code the code→label pairs in the
front end or expose them from the backend; there is no table to join to.

## 12. `snapshot_diagnostic` ships empty, on purpose

It is created by `bootstrap` and nothing writes it. That is the one case where a
table with no writer is correct: it is where the measurement session puts repeat
runs, benchmark arms and probe results, and creating it now means that session
is not also a schema change. If it is still empty after the measurement session,
that is a finding.

## 13. Four legacy tables remain until the week is verified — **closed 2026-09-25**

Week one passed its six checks on dev on 2026-09-25 (final run at `37dd309`), and the
four tables were dumped to `backend/data/backups/2026-09-25-neondb-legacy-*.csv`
and dropped from dev (`neondb` on `ep-round-brook`): `indicator` 11 rows,
`yearly_value` 4,765, `recent_indicator` 46, `payload_census` 48. The database
now holds exactly the ten tables in `schema.py`.

## 14. Dead relevance rows in `llm_artifact` — **closed 2026-09-25**

The relevance artifacts written before `country_iso2` joined the key were
dumped to `backend/data/backups/2026-09-25-neondb-dead-relevance-llm_artifact.csv`
and deleted from dev: **2,489 before, 0 after**. All of them went, not only the
ones week one had re-labelled. Their country existed only inside the content
hash, so which of them the five-country run replaced cannot be established, and
no lookup could reach any of them. The 909 digest rows, where `''` is correct,
were not touched.

## 15. The gate's structural bar is not Eli's — decision owed by Eli

Found closing §4. On the 48-country roster run of 2026-09-22/23 the gate called
1,236 of 2,444 candidates structural and 919 were selected, which puts the median
country at the full budget of 20. Applying the rates from Eli's labels (0 of 8
gate-structural rows kept, 2 of 14 gate-incident rows promoted) projects a median
of about 1 structural article per country, 3 at most (SA), and no country near
20. If the true keep rate on gate-structural were 1 in 8, the median would be about
4.5. Both of Eli's structural rows are one kind of story from one country, in the
disagreement stratum, so the promotion rate is not general.

The score is sensitive to volume, so a stricter bar changes what every country
is scored on and not only which articles pass. The prompt is unchanged until Eli
decides where the line is. Any prompt edit changes `RELEVANCE_PROMPT_VERSION`,
and every verdict is re-earned on the next run.

### The binary gate: pass line, pre-registered 2026-09-27

Written and committed before any held-out article was scored.

The gate is now binary (`relevant` / `irrelevant`), against Eli's definition:
material to the target country's investable risk. The prompt carries ten of the
v2 thirty as worked examples, and was tuned against the other twenty (the dev
set). Neither can report accuracy. The held-out set is the v3 draw
(`docs/blind-labels-v3-key.json`, sealed), labelled by Eli on
`docs/blind-labels-v3.html`.

The grid is run **once**: four models (`gpt-4o-mini-2024-07-18`,
`gpt-4.1-mini-2025-04-14`, `gpt-4.1-2025-04-14`, `gpt-4o-2024-08-06`) by two input
modes (`snippet`, `body`), with the old gate's stored labels as the baseline row.

- **Pass** means agreement with Eli of at least **24 of 30** (80%) **and** recall on
  Eli's `relevant` of at least **90%**. A failed call counts as `irrelevant`, as it
  does in the pipeline.
- Recall is the guard because a missed relevant article is evidence the scorer
  never sees. A false admit costs one digest call.
- Among the cells that pass, adopt the **cheapest** by projected weekly cost for 48
  countries. The volume is the 2026-09-22/23 roster run's 2,444 candidates, and the
  cost is the cell's metered cost per call. A tie on cost goes to higher agreement.
- A winning `body` cell needs bodies before the gate. Retrieval already fetches
  them, so report the added fetches per week, which should be none, and check it.
- If **no** cell passes, report the table and the disagreements and stop. The line
  is not loosened. The held-out thirty are not re-run under a revised prompt,
  because a revised prompt needs a new held-out draw.

### The binary gate: result, 2026-09-27 — **fail**

Run once on Eli's v3 labels (`docs/blind-labels-v3.csv`: 22 relevant, 8
irrelevant). Cost per call is metered. The weekly projection is 2,444 candidates.

| cell | agree | recall (rel) | precision (rel) | cost / 30 | weekly, 48 countries |
|---|---:|---:|---:|---:|---:|
| old gate (3-label, `structural` admitted) | 19/30 | 13/22 | 13/15 | ~$0.009 est. | ~$0.76 est. |
| gpt-4o-mini · snippet | 20/30 | 17/22 | 17/22 | $0.0103 | $0.84 |
| gpt-4o-mini · body | 22/30 | 20/22 | 20/26 | $0.0116 | $0.95 |
| gpt-4.1-mini · snippet | 18/30 | 14/22 | 14/18 | $0.0277 | $2.26 |
| gpt-4.1-mini · body | 21/30 | 18/22 | 18/23 | $0.0312 | $2.54 |
| gpt-4.1 · snippet | 17/30 | 12/22 | 12/15 | $0.1391 | $11.34 |
| gpt-4.1 · body | 22/30 | 19/22 | 19/24 | $0.1572 | $12.81 |
| gpt-4o · snippet | 22/30 | 17/22 | 17/20 | $0.1718 | $13.99 |
| gpt-4o · body | 23/30 | 20/22 | 20/25 | $0.1929 | $15.72 |

No cell reaches 24/30. The line is not loosened and these thirty are not
re-scored. Nothing was adopted, and the pipeline's gate settings are unchanged
(`gpt-4o-mini-2024-07-18`, `body`).

Of the best cell's seven misses, the written definition sides with the model on
four (h12, h13, h23, h30), with Eli on one (h01), and leans to Eli on two that
are one story (h11, h16). The definition is most of the gap. Edits proposed for
the next prompt version, which needs its own held-out draw:

- Press freedom and violence against journalists count when {country}'s own
  authorities act. Another state's treatment of a {country} national is not
  about {country}'s institutions (h12; b18 and b19 in v2).
- Proposals, recommendations and advocacy that no authority has adopted are
  irrelevant (h13).
- A {country} company's investment abroad is company news (h01).
- An event that only happened on {country}'s territory, with its consequences
  falling on other states, is a passing mention (h11, h16).
- Loosen spillover: a story about a major partner's or region's economy or trade
  policy counts when the consequence for {country} is evident, even if unnamed,
  such as energy demand for an energy exporter or tariffs {country} imposed (h23,
  h30; b27 and b30 in v2).

## 16. Selection lost the structural-events-first rule

The binary schema has no `is_structural_event`, so a coup or a default no longer
takes the first slots. Round-robin across ledgers is the fallback. Restore it
with the next gate prompt version and its own held-out draw. Adding an output
field now would change the prompt that was just measured.

## 17. Truncated bodies pass as `full`

The digest's `body_quality` can call a wall `full`. Evidence: v3 held-out **h27**
(PT, The Times), 193 characters, which is a headline, a standfirst and "Previous
Article Next Article". It stays in the held-out set because it was already
labelled. No length floor was added: 37dd309 chose the digest's judgement over a
phrase list, and a floor would be the same kind of rule.

### The actor-test gate, 2026-09-27

The model is fixed at `gpt-4o-mini-2024-07-18` in `body` mode, and only the
prompt and the harness change. The prompt is Eli's actor test, used verbatim. It
asks who acted and on whom, as eight exclusions and five tests. The label is
computed in code: `relevant` only when no exclusion applies and a test passes.
Fourteen of Eli's articles are worked examples, in contrasting pairs.

**Measured on tuned data.** The rule was derived from all 60 labels, so no
held-out set exists, and Eli declined further labelling. The decision is taken on
the 46 articles that are not worked examples. All 60 are reported separately.

- **Adopt** if, on the 46, agreement is at least 80% **and** recall on relevant is
  at least 90%, **and** at most 2 of the 60 labels flip across three passes. The
  first pass reads and writes the cache, and passes 2 and 3 bypass it.
- If it fails, report it and stop. The prompt is not edited and re-run.

## 18. Exposure cards go stale

`backend/llm/exposure_cards.json` holds each country's main trade partners,
exports, neighbours and security rivals, for the gate's spillover test (T5).
Refresh the cards yearly: rerun `python -m backend.data_fetching.exposure_cards_build`
and re-read the hand-written rivals and sea neighbours.

