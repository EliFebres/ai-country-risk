# Checklist scorer, scored by code (step 5b)

2026-11-06, branch `backend-update`. Scorer `gpt-4o-2024-08-06`, temperature 0,
seed 42, on the checklist prompt (`prompt_version` `73b29ed1c15c…`). Every call
read a frozen step 5 payload from `backend/measurements/step5/`, unchanged, and
wrote to `backend/measurements/step5b/`. None wrote `risk_snapshot` or
`run_ledger`. Spend **$2.03** of a $10 cap.

## The finding

**M1 passes the line it was stopped on in step 5, and the score fails face
validity, for a reason that is in the data half and not in the model.** The
worst `score_12m` SD over ten identical calls fell from 3.33 to **1.58** (TW),
with four of six countries at or under 0.52 and two at exactly 0. The median
of three, computed over every triple of the ten calls, brings the worst to
**0.47**. But PT (40) scores above TW (39) and HK (37), and the ordering comes
from the **data half**: on the percentiles alone PT is 44, HK 43 and TW 40,
while on the news half alone PT is the lowest of the six at 25. PT's `order`
ledger is 54 from unemployment at the roster's 74th percentile and GDP-per-
capita growth at the 65th, with no news to pull it, because the model answered
only three of sixteen questions about Portugal with evidence. The same half
puts the US second-highest of the six on data (55) on Gini and interest
payments. Ranking raw macro levels across a roster that is half emerging
markets is not a risk ordering, and that is the registry's design, which is
Eli's to change.

Under the brief's pre-set line, face validity failed, so **the measurement
stopped after M1**: the 90-call median-of-3 confirmation, M3 and M5 were not
run. M4 and the dilution check need no new calls and are reported from the 60
M1 calls. Nothing was set for production: `SCORING["calls_per_country"]` stays
at 1, because any fix to the data half changes the instrument and M1 is re-run
on it.

Two things the model does, consistently, that the prompt told it not to:

1. **It answers 0, not null, when there is no evidence.** "F3: 0, no evidence
   on currency and capital issues this week" is the shape, on 6 to 13
   questions per call, in 10 of 10 calls for every country but RU. The
   validator nulls a level with no evidence, so the score never rewarded the
   silence, but the prompt's "null is correct and expected" is not being
   read. After validation the null share is 100% on O1, O6 and I2, and 95% on
   O4, across all 60 calls.
2. **It cites the indicator, not an article, on the questions an indicator
   answers.** F2 (inflation), F5 (corruption), F6 (debt interest), O1 (WGI
   stability), I1 (RSF), I2 (SPI) and E1 (FDI) are answered from the economics
   block, 10 of 10 calls, in every country where the indicator resolved. The
   validator drops those citations (decided with Eli: the numbers are scored
   in code and would count twice), so those levels become null too. The 284
   `evidence_dropped_non_article` across 60 calls are all of this kind.

Together these leave the news half thin: 3 (PT, TW) to 8 (HK) answered
questions per call. Most ledgers are data-only or carried by one or two
questions, which is part of why the repeat calls agree so well.

## What changed

- **The model is never asked for a number.** `RISK_PROMPT`, the anchor bands,
  the three-door test, the no-round-numbers rule and `bearing` are gone. The
  prompt is the brief's sixteen-question checklist, verbatim, in
  `backend/llm/constants.py`; the schema requires every question as
  `{level 0–4 | null, evidence [str], note}`, plus `scheduled_events`,
  `bullet_summary` and the unchanged `condition_flags`.
- **The economics are scored in code** (`backend/llm/data_score.py`). Each
  registry indicator has a `risk` direction (`higher`, `lower`, or `target`
  for inflation, |x − 2|). A country's value is ranked against the other
  roster countries' latest values: `100 × (below + ½ ties) / others`, so 0 is
  the lowest risk in the roster and 100 the highest; a ledger's data score is
  the mean of its available percentiles; missing stays missing. The roster
  table is built once per run from the local panel and `curated.csv`
  (`backend/measurements/step5b/roster_percentiles.json` for this run; sha
  in every manifest).
- **Code does the arithmetic** (`backend/llm/combine.py`): news ledger = 25 ×
  mean(non-null levels); ledger = ½ data + ½ news, or one half alone;
  `score_12m` = round(.4 friction + .4 order + .2 information), weights
  renormalised over the ledgers that have a score; `score_3m` = `score_12m` +
  clamp(Σ ±3 per dated event, ±9); `article_weight` = Σ levels of the
  questions citing the article. Edge is a stress reading now (0 = most
  vitality in the roster), combined the same way, and never in the composite.
  Every constant is in `SCORING`, inside `PROMPT_VERSION`.
- **The validator** (`backend/llm/validate.py`) accepts article ids only as
  evidence; a level with none becomes null and is counted; an event needs a
  strict ISO date inside 90 days, an article that was sent and a known
  question.
- **Production** (`backend/util/pipeline.py`) builds the roster table once,
  calls the model `calls_per_country` times, merges by per-question median if
  more than once, carries a null question forward from the last 8 snapshots
  within 70 days (original levels only), and stores every intermediate in
  `manifest.scoring`. `risk_snapshot` keeps its columns; `article_scores` is
  now `[{id, weight, questions}]` and `subscore_evidence` one computed line
  per ledger. No ETL was run; the wiring is covered by the 254 tests and the
  invariants, not by a live week.

## The six countries

Median over ten calls. Each ledger is `score (data / news)`; `—` is a half
with nothing in it.

| country | score_12m, distinct | SD | friction | order | information | edge | answered / 16 |
|---|---|---:|---|---|---|---|---:|
| RU | 64 · 65 · 67 | 0.97 | 57 (63 / 50) | 63 (64 / 62) | 82 (89 / 75) | 58 (65 / 50) | 6 |
| KW | 48 | 0.00 | 38 (45 / 31) | 37 (37 / 38) | 89 (89 / —) | 33 (41 / 25) | 7 |
| US | 42 · 43 | 0.52 | 45 (66 / 25) | 38 (51 / 25) | 42 (42 / —) | 40 (40 / —) | 6 |
| PT | 40 | 0.00 | 36 (47 / 25) | 54 (54 / —) | 20 (20 / —) | 37 (37 / —) | 3 |
| TW | 34 · 39 | 1.58 | 42 (60 / 25) | 36 (21 / 50) | 38 (38 / —) | 2 (2 / —) | 3 |
| HK | 37 · 38 | 0.42 | 23 (38 / 8) | 33 (28 / 38) | 73 (83 / 62) | 14 (29 / 0) | 8 |

TW's friction data score is one indicator, V-Dem corruption at the 60th
percentile; its edge is PISA alone. KW's and US's `information` is RSF and
SPI with every information question null.

## Measurements

### M1. Repeatability: **1 < SD ≤ 3, median of 3**

Ten calls per payload. Per question, how many of the ten gave the modal level:

| | score_12m SD | score_3m SD | ledger SDs (F / O / I / E) | questions that flipped |
|---|---:|---:|---|---|
| HK | 0.42 | 1.42 | 0 / 0.84 / 0 / 0 | O4: null ×8, 2 ×2 |
| KW | 0.00 | 0.00 | 0 / 0 / 0 / 0 | none |
| PT | 0.00 | 0.00 | 0 / 0 / 0 / 0 | none |
| RU | 0.97 | 0.97 | 0 / 1.93 / 0 / 3.38 | O3: null ×9, 3 ×1; O5: 3 ×9, 4 ×1; E1: 2 ×7, null ×3 |
| TW | **1.58** | 1.58 | 0 / 4.11 / 0 / 0 | O2: null ×9, 0 ×1 |
| US | 0.52 | 1.55 | 0 / 1.96 / 0 / 0 | F3: 1 ×6, null ×4; O2: 1 ×6, 2 ×4; O4: null ×9, 1 ×1 |

Eleven of the sixteen questions never flipped anywhere. Of the eight flips,
six are between null and a level, which is whether the model cited an
article that call, not which level it chose; only US O2 (1/2) and RU O5
(3/4) are level flips. The brief's prediction held: one flip moves
`score_12m` by about a point, except where a ledger's news half is one
question, as TW's `order` is (O5 alone, so O2 switching on moves the ledger
by 4 and the score by 5).

**Decision:** worst SD 1.58, so production should take the per-question
median of three. Over all 120 triples of each country's ten calls the worst
SD is **0.47** (US), with four countries at 0, so the mode does what it is
meant to. The fresh 90-call confirmation was not run, because of the next
line.

### Face validity: **fail**

By median `score_12m`: RU 64, KW 48, US 42, PT 40, TW 39, HK 37. RU is
highest. PT is fourth of six, not lowest or second lowest.

The cause is the `order` ledger's data half. PT's order is 54, from
unemployment 6.16% (74th percentile of the roster) and GDP-per-capita growth
0.83% (65th), against WGI stability and rule of law at the 36th and 40th; HK's
order data is 28 and TW's 21 (WGI only). On the news half PT's order is null
(O1–O6 all nulled: four answered 0 with no evidence, two from the WGI
indicator), so nothing offsets it. The same half puts the US at 55 on data
alone, from Gini (85th) and interest payments (93rd). No question causes
this; the questions for PT, HK and TW are mostly null.

**Decision:** stop. The fix is in the registry, not the prompt: which
indicators belong in a risk ordering (unemployment and Gini are not what an
investor prices), whether a developed-market cross-section should be ranked
against emerging markets at all, and whether the data half deserves half the
ledger when the news half is null. Each is a `PROMPT_VERSION` change.

### M3. Do the articles move the score? **Not run**

Stopped by face validity. The `no_articles` arm would have tested the
validator's "every level null with no articles" line; on the full payloads
the model's 0-for-no-evidence habit says that line will be met by the
validator, not by the model.

### M4. The 3-month score: **13 of 60 calls differ, every one traced**

`score_12m − score_3m` is 0 in 47 calls and −3 in 13. Every −3 traces to one
listed, dated, `up` event:

- HK ×7: a18, 2026-10-22, "Deadline for offshore trust tax payment", on F1 or
  F4. A tax deadline read as more stress; arguable.
- US ×4: a14, 2026-10-01, "Supreme Court decision on voter verification
  database", O2. US ×2: a8, 2026-11-01, "Midterm elections", O2.

PT listed two or three events every call, all `unclear` (EU OPEX field
tests), so its gap is 0. TW listed its local elections every call, `unclear`.
Seven events were dropped as beyond the 90-day horizon (2027 dates), all
correctly.

**"Never invent a date" is not followed.** Step 5 recorded that neither the US
midterms article (a8) nor the TW local-elections article (a10) gives a date.
The model gave 2026-11-01 for the midterms (held 2026-11-03) and 2026-11-01 or
2026-12-01 for Taiwan's elections (2026-11-28), first-of-month placeholders
that vary between calls. The arithmetic is right and the dates are made up.

### M5. Article count: **not run**

Stopped by face validity.

### Dilution: **T1 weighs little, and two-thirds of articles weigh nothing**

`article_weight` by the gate's test code, over the 60 M1 calls (1,200
article readings):

| test | appearances | mean weight | share with weight 0 |
|---|---:|---:|---:|
| T1 | 510 | 0.89 | 63% |
| T2 | 170 | 0.34 | 72% |
| T3 | 90 | 1.18 | 59% |
| T4 | 280 | 0.76 | 64% |
| T5 | 150 | 0.27 | 80% |

On average **66% of the articles sent are cited by no question**. T1's zero
share is 63%, over the rule's 0.5, so the rule says rank T1 below T2–T5, but
T1 is the second most-cited test and every test is over 0.5. The reading is
not that T1 dilutes; it is that the model cites two or three articles per
answered question and answers few questions, so most of the twenty go
unread. That is a cheaper payload before it is a selection problem.

## What the model consistently misreads

1. **0 is not null.** Every question with nothing in the week comes back 0
   with an empty evidence list and a note saying there is no evidence. The
   validator corrects it; the prompt did not.
2. **The economics block is evidence** for F2, F5, F6, O1, I1, I2 and E1. The
   model answers those from the number it was shown, every time.
3. **Dates are rounded to the first of the month** when the article gives
   none (US a8, TW a10).
4. **HK a18**, a tax-deadline article, is a scheduled `up` event on F1/F4.
5. **TW O5 = 2** ("military threats, incidents or strikes near the
   territory") in 10 of 10 calls, from the arms-sale and PLA articles, is the
   one order question answered for Taiwan and carries its whole order news
   half.

## Spend

| step | calls | tokens in / out | USD |
|---|---:|---|---:|
| M1 `full` ×10 × 6 (incl. the first RU check) | 60 | 655,830 / 38,810 | 2.028 |
| Roster table, triples, analysis | 0 | — | 0 |
| **Total** | 60 | | **2.03** |

One call is about 10.9k input and 650 output tokens, $0.034, under the step
5 call because the answer is shorter. Not run: `full_confirm` (90 calls,
~$3.1), M3 (36, ~$0.9), M5 (54, ~$1.7).

## Files

- `backend/measurements/step5b/roster_percentiles.json`: the table and every
  country's percentile per code.
- `backend/measurements/step5b/calls/full_<iso2>.jsonl`: every call, raw,
  validated, counted and scored.
- `backend/measurements/step5b/analysis.json`: M1, face validity, the triple
  medians, M4, dilution, spend.
- The step 5 payloads in `backend/measurements/step5/` are unchanged.
