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

---

# Step 5c: the data half rebuilt, the checklist gaps closed, re-measured

2026-11-06, branch `backend-update`. Same scorer, same six frozen payloads from
`backend/measurements/step5/`, everything new under
`backend/measurements/step5c/`. Prompt version `1547a0f0cb41…` in the tree; the
call rows are stamped `03ff09e14514…`, which differs only by
`calls_per_country` having since moved out of the hashed `SCORING` block into
`CALLS_PER_COUNTRY = 3`. The prompt, the schema and every weight are
byte-identical between the two. Spend **$3.71** of an $8 cap.

## The finding

**Face validity passes on the data alone and on the full score, and M1 in
median-of-3 mode misses its line by a tenth: KW's five medians are 38, 40, 41,
SD 1.09 against a line of 1.** The other five countries are at 0.84 (US), 0.55
(HK) and 0.00 (PT, RU, TW). Under the brief's pre-set rule, that is a stop at
M1; M3, M4 and M5 are reported because M3 and M4 cost nothing and M5's calls
were already made in the same run.

The median of three did not help Kuwait because what flips is a **coin, not a
tail**: O5 (Iranian strikes near Kuwait, a5 and a16) came back 3 in eight calls
and 2 in seven, on identical evidence, and the median of three fair coin flips
is still a coin. The two other flips are new to step 5c's own rule: F1 and O1
switch between null and **0 with evidence** (a19, a14). Under 2a a null item
sits at the ledger's data level (friction 28, order 39) and a 0 with evidence
sits at 0, so the difference between "nothing to say" and "I read a19 and
nothing is wrong" is now worth about a point of score. That is the rule
working as designed; it is also the reason the line was missed.

What the data-half rebuild fixed: PT is now 19 and TW 18, HK 26, US 28, KW 40,
RU 59, in that order on the medians and in the same order on the data alone
(18.0 / 16.9 / 23.9 / 27.4 / 40.0 / 58.2). Unemployment, Gini and GDP growth
are out of the score, the four new WGI series are in, and no country's score
depends on another's data.

What the checklist fixes did:

- **The economics block left the payload** and the indicator citations went
  from 284 in 60 calls to **0 in 144**. Calls are 8.7k input tokens instead of
  10.9k, $0.028 instead of $0.034.
- **The articles move the score by 0 to 2 points** (M3). With every null
  item at the data level, a week's reading is the data half plus the
  questions the articles answered, each worth 1/n of its ledger's gap.
- **"Never invent a date" is now enforced.** Fifteen events were dropped for a
  date that is not in the cited article, all PT's `2026-10-07` for an EU
  field-test article that gives the 8th and the 14th. The US midterms and
  Taiwan's elections, whose articles give no date, are no longer listed.
- **0-for-no-evidence is unchanged and now harmless.** The model still
  answers 0 with an empty evidence list on 6 to 12 questions per call (PT 181
  of 240, TW 175, HK 117, US 93, KW 92, RU 0). The validator nulls them and
  the null takes the data level.

## 1b. The data half, before any model call

Fixed scales from the registry (`risk_scale`), linear between the ends,
clamped. The table is `backend/measurements/step5c/data_table.json`.

| | friction | order | information | edge | data-only score_12m |
|---|---:|---:|---:|---:|---:|
| RU | 44.9 (4) | 67.2 (3) | 67.0 (3) | 64.0 (2) | **58.2** |
| KW | 28.0 (3) | 39.3 (3) | 65.6 (3) | 47.4 (2) | 40.0 |
| US | 29.6 (4) | 28.7 (3) | 20.6 (3) | 39.0 (3) | 27.4 |
| HK | 6.2 (3) | 20.1 (3) | 66.8 (2) | 19.6 (3) | 23.9 |
| PT | 19.7 (4) | 23.9 (3) | 2.9 (3) | 29.4 (3) | **18.0** |
| TW | 20.3 (2) | 14.9 (3) | 14.4 (2) | 0.0 (1) | 16.9 |

(n scored indicators in brackets.) **Check: RU highest, PT second lowest:
pass.** The scales were not adjusted. Things worth knowing from the full
table: RU's RSF 23.15 clamps to 100 and its interest-payments figure is from
2020 (1.3); inflation near 2% clamps to 0 for KW, US, PT and HK; SPI above 90
clamps to 0 for US and PT; HK's FDI 30.8% of GDP clamps to 0 on edge (the
entrepôt effect, §19); TW's edge is PISA alone at 533, clamped to 0. The
four new WGI series carry WGI's 2025 update, PV and RL are still 2024 (§22).

## The six countries

Median of five median-of-3 results. Each ledger is `score (data / news)`.

| country | score_12m, distinct | SD (median-of-3) | SD (single call) | friction | order | information | edge | answered / 16 |
|---|---|---:|---:|---|---|---|---|---:|
| RU | 59 | 0.00 | 0.00 | 45 (45 / 46) | 66 (67 / 66) | 70 (67 / 72) | 64 (64 / 64) | 5 |
| KW | 38 · 40 · 41 | **1.09** | 1.07 | 29 (28 / 30) | 39 (39 / 39) | 66 (66 / 66) | 36 (47 / 25) | 7 |
| US | 27 · 28 · 29 | 0.84 | 0.96 | 30 (30 / 31) | 32 (29 / 35) | 21 (21 / 21) | 32 (39 / 25) | 8 |
| HK | 25 · 26 | 0.55 | 0.90 | 7 (6 / 7) | 23 (20 / 26) | 65 (67 / 64) | 10 (20 / 0) | 7 |
| PT | 19 | 0.00 | 0.00 | 23 (20 / 27) | 24 (24 / 24) | 3 (3 / 3) | 27 (29 / 25) | 4 |
| TW | 18 | 0.00 | 0.00 | 19 (20 / 18) | 18 (15 / 21) | 14 (14 / 14) | 0 (0 / 0) | 4 |

## Measurements

### M1 (confirmation, median of 3): **worst SD 1.09, stop**

Fifteen calls per country, every three consecutive calls one median.

| | medians | SD | questions flipping across the five medians |
|---|---|---:|---|
| KW | 38, 40, 41 | **1.09** | O5 3/2 (same evidence, 8:7 over the 15 calls); F1 null/0 (a19); O1 null/0/2 (a14, a19) |
| US | 27, 28, 29 | 0.84 | O2 2/1 and O3 2/1 (both on a14); E1 1/2; O1 and I2 null/level |
| HK | 25, 26 | 0.55 | F4 1/2; O3 3/2; F3, O1, O5 null/0 or null/1 |
| PT | 19 | 0.00 | F4, E1 flip without moving the score |
| RU | 59 | 0.00 | none |
| TW | 18 | 0.00 | none |

The single-call SDs are 1.07, 0.96, 0.90, 0, 0, 0: the median of three
removed about a third where the flip is a tail (HK) and nothing where it is
a coin (KW).

### Face validity: **pass**

RU 59 > KW 40 > US 28 > HK 26 > PT 19 > TW 18. PT second lowest.

### M3: do the articles move the score? **By 0 to 2 points**

| | data-only | full | gap | the questions that pull, as item − data level |
|---|---:|---:|---:|---|
| HK | 24 | 26 | +2 | O3 = 3 (+55 on order's 20), F4 = 1 (+19); four questions at 0 pull down |
| RU | 58 | 59 | +1 | O5, I1, I3 at 3 (+8 each); O2 = 2 pulls order **down** from 67 (−17) |
| US | 27 | 28 | +1 | F3, O2, O3 at 2 (+20 each); I2 = 0 (−21), E1 = 1 (−14) |
| PT | 18 | 19 | +1 | F2 = 2 (+30 on friction's 20) |
| TW | 17 | 18 | +1 | O5 = 2 (+35 on order's 15); F3 = 0 (−20) |
| KW | 40 | 40 | 0 | F4 = 2 (+22) and O5 = 2 (+11) against F1 = 0 (−28) and O1 = 0 (−39) |

Each answered question moves its ledger by 1/n of the gap between its level
and the data, so a 3 on one of six order questions is worth about 9 ledger
points and 2 to 4 points of score, and a 0 with evidence pulls the other way.
RU's O2 = 2 is the checklist reading "serious irregularities alleged" as less
stress than WGI's −0.90 on stability.

### M4: **15 of 90 calls have a gap; every one traces; 15 dates rejected**

`score_12m − score_3m` is 0 in 75 calls and −3 in 15, all HK: a18,
2026-10-22, "Deadline for offshore trust tax payment", `up`, on F1 or F4. The
date is in the article, so it passes the validator. The prompt now says "Do
not list routine, recurring deadlines such as tax filing dates", and the model
listed this one in 15 of 15 calls. PT lists two EU field-test dates every call,
both `unclear`, so they move nothing; its third (`2026-10-07`) was rejected
in all 15 calls as not in the article. Nine US tariff events were rejected as
beyond the horizon (2027). 45 valid events across the 90 calls.

### M5: article count. **Pooled |slope| 0.07, closed**

One median-of-3 per subset; "all" is the first `full` median.

| | 5 | 10 | 15 | 20 | slope |
|---|---:|---:|---:|---:|---:|
| HK | 22 | 24 | 24 | 26 | 0.24 |
| KW | 40 | 40 | 40 | 41 | 0.06 |
| PT | 16 | 19 | 19 | 19 | 0.18 |
| RU | 58 | 59 | 59 | 59 | 0.06 |
| TW | 18 | 19 | 18 | 18 | −0.02 |
| US | 29 | 30 | 28 | 28 | −0.10 |

Pooled within-country slope 0.07 points per article. Thirteen of sixteen
questions change their null share or mean level as articles are added, which
is what more evidence should do; the score barely moves because each is 1/n
of a ledger. The volume question is closed.

### Dilution

Over all 144 calls: T1 1,026 readings, mean weight 1.07, 53% zero; T2 0.55
/ 58%; T3 1.33 / 59%; T4 0.67 / 70%; T5 0.44 / 76%. 58% of articles sent are
cited by no question (66% in step 5b).

## What the model still misreads

1. **Routine deadlines as scheduled events.** HK a18's trust-tax payment
   deadline, 15 of 15, after the prompt said not to.
2. **0 with no evidence instead of null**, 6 to 12 questions a call. Now
   harmless because of 2a, and still not what the prompt asks.
3. **O5 for Kuwait is a coin** between 2 and 3 on the same two articles. The
   level descriptions ("military threats, incidents or strikes near the
   territory" against "attacks on the territory") are both readable for
   Iranian strikes on Kuwait, and that is where the last tenth of SD lives.
4. **RU O2 = 2** reads a stage-managed Duma election as "serious irregularities
   alleged", below the data.

## Cost

| step | calls | tokens in / out | USD |
|---|---:|---|---:|
| data table and check | 0 | — | 0 |
| M1 `full` ×15 × 6 (incl. the first RU check) | 90 | 782,250 / 53,986 | 2.495 |
| M5 `first_5/10/15` ×3 × 6 | 54 | 365,058 / 30,556 | 1.217 |
| **Total** | 144 | | **3.71** |

A call is **$0.028** (8.7k in, 600 out). Production in median-of-3 mode is
**$0.083 per country-week** and **$3.99 a week for 48 countries**, the scorer
only; the gate and digests are on top.

## Files

- `backend/measurements/step5c/data_table.json`: the economics block rebuilt
  from the panel for the six, every scored value and score, and the 1b check.
- `backend/measurements/step5c/calls/{full,first_5,first_10,first_15}_<iso2>.jsonl`.
- `backend/measurements/step5c/analysis.json`.

---

# Step 5d: close the measurement, run the first production week

2026-11-06, branch `backend-update`. Two changes to the instrument (O5's
wording, a routine-deadline validator), a 30-call re-measurement on the same
six frozen payloads under `backend/measurements/step5d/`, and the first weekly
run in production mode on the test set. Prompt version `982172f001dc…`,
questions version `e018391fae57…`. Session spend **$1.43** of a $3 cap.

## §24. O5: one geographic test per level

Step 5c's last tenth of SD was O5 for Kuwait, a coin between 2 and 3 on the
same two articles (a5, a16), because "strikes near the territory" and "attacks
on the territory" both fit an Iranian strike on Kuwait. Levels 0, 1 and 4 and
every other question are byte-identical; only the tests for 2 and 3 changed.

Before:

> O5 Armed conflict and external threat. 0 none; 1 diplomatic tension; 2 military threats, incidents or strikes near the territory; 3 attacks on the territory, or active mobilisation; 4 war on the territory.

After:

> O5 Armed conflict and external threat. 0 none; 1 diplomatic tension; 2 military threats, incidents or strikes near the territory (a strike or attack is near, never on, the territory if it landed in a neighbouring country or in international waters, was intercepted in the country's airspace, is described as near, close to, off the coast of or across the border from the country, or the article does not say where it landed); 3 a strike or attack on the territory (only if the article says it landed, hit or occurred inside the country's land borders or territorial waters), or active mobilisation; 4 war on the territory.

The stamp moved with it: `PROMPT_VERSION` 1547a0f0cb41… → 982172f001dc…,
`QUESTIONS_VERSION` 253189c035ac… → e018391fae57…. The second is what the
carry-forward compares, so no level answered under the old O5 can be carried
into a week scored under the new one.

## §25. Routine deadlines are dropped in code

The prompt's "Do not list routine, recurring deadlines" was ignored 15 of 15
times for HK a18's trust-tax payment deadline, the only thing that moved
`score_3m` in step 5c. The gate taught the lesson already: an instruction
the model does not follow becomes a rule the code enforces.
`validate.ROUTINE_DEADLINE_PATTERNS` is a module-level tuple
(`deadline`, `filing`, `due date`, `payment date`, `tax return`, `tax
payment`, `statistical release`, `data release`, `release of .* data`,
`scheduled session`, `regular session`, `quarterly`, `monthly`, `annual
report`, `earnings`), matched case-insensitively against the event's text
after the date-in-article check. A match is dropped as `routine deadline`,
counted in `events_dropped_routine_deadline`, and the violation carries the
event's text. Elections, rulings, votes, treaty and sanction dates, summits,
strikes, maturities and central-bank decisions match none of them. The
prompt sentence stays.

## §26. Re-measurement, 30 calls, $0.85

Pre-set decisions, one line each.

- **M1 (KW only, 5 fresh medians of 3): SD 0.55, pass.** Medians 38, 39, 39,
  39, 38. O5 is 3 in all five medians and all 15 calls; the one question still
  flipping across medians is F6 (0 or 1), and F1/O1 flip null↔0 in single
  calls, as accepted. Step 5c's 1.09 was the O5 coin.
- **Face validity (one median each on the other five, KW's median of five):
  pass.** RU 58 > KW 39 > US 29 > HK 25 > PT 19 > TW 18.
- **M4: 0 of 30 calls have a gap; pass.** `score_12m − score_3m` is 0 on every
  call and every median. Six valid events across the 30 calls, none routine.
  Dropped: 3 routine deadline (HK a18, "Deadline for offshore trust tax
  payment", every call), 3 date not in article (PT a2's `2026-10-07`), 2 past
  (US a14, a ruling on 2026-09-25), 1 unparseable (`2026-10-`). HK a18 is
  gone from the valid list.
- **O5 check: KW 3 in 15 of 15 calls**, citing a5 (15) and a16 (7). The
  other five are one level each as well: US 1, RU 2, TW 2, HK null, PT null.

M3 came along for free: the articles move the score by −1 to +2 (KW −1, RU
0, HK/PT/TW +1, US +2). The measurement is **closed**: the three decisions
pass and the remaining variation is a single question's tail, not a coin.

| | score_12m | SD | friction | order | information | edge | answered / 16 |
|---|---|---:|---|---|---|---|---:|
| RU | 58 | 0.00 | 45 (45 / 46) | 64 (67 / 61) | 70 (67 / 72) | 64 (64 / 64) | 5 |
| KW | 38 · 39 | 0.55 | 27 (28 / 26) | 38 (39 / 36) | 66 (66 / 66) | 36 (47 / 25) | 9 |
| US | 29 | 0.00 | 30 (30 / 31) | 32 (29 / 35) | 21 (21 / 21) | 44 (39 / 50) | 8 |
| HK | 25 | 0.00 | 6 (6 / 6) | 23 (20 / 25) | 65 (67 / 64) | 10 (20 / 0) | 8 |
| PT | 19 | 0.00 | 23 (20 / 26) | 24 (24 / 24) | 3 (3 / 3) | 27 (29 / 25) | 3 |
| TW | 18 | 0.00 | 19 (20 / 18) | 18 (15 / 21) | 14 (14 / 14) | 0 (0 / 0) | 4 |

(Ledger = score (data / news). KW's SD is over five medians; the others are
one median each.) A call is $0.028 (8.9k in, 600 out); median-of-3 is $0.085
per country-week, $4.06 a week for 48 countries, scorer only.

## §27. The first production week

`python -m backend.main etl --test-set` against the dev database, in
production mode (median of 3), fresh articles. Started 2026-11-07 02:14 UTC,
so every row is stamped `run_date = 2026-11-07`; 20 minutes wall-clock,
240 s per country. Written: 5 `risk_snapshot` rows, 7 `run_ledger` rows (5
`etl`, 1 `run`, 1 `report`), 15 `news_alert` rows, 164 economic-calendar
events. KW's row, manifest and all, is
`backend/measurements/step5d/week1_manifest_KW.json`.

| | score_12m | score_3m | friction | order | information | edge | answered / 16 | data-only |
|---|---:|---:|---|---|---|---|---:|---:|
| KW | 40 | 40 | 27 (28 / 26) | 40 (39 / 41) | 66 (66 / 66) | 24 (47 / 0) | 5 | 40 |
| US | 26 | 26 | 31 (30 / 31) | 25 (29 / 22) | 21 (21 / 21) | 19 (39 / 0) | 8 | 27 |
| HK | 24 | 24 | 7 (6 / 7) | 21 (20 / 22) | 67 (67 / 67) | 35 (20 / 50) | 6 | 24 |
| TW | 20 | 20 | 24 (20 / 28) | 17 (15 / 20) | 14 (14 / 14) | 0 (0 / 0) | 9 | 17 |
| PT | 17 | 17 | 17 (20 / 14) | 24 (24 / 24) | 3 (3 / 3) | 15 (29 / 0) | 6 | 18 |

Ledger = score (data / news). The week's reading moved every country by
at most 3 points from its data-only score, in the direction the articles
pointed (TW +3 on F2/F3/F5 = 2; PT −1 on F2/F4 = 0). KW's O5 is 2 this
week, on Basra: the new wording reads a neighbouring country as "near".

- **Carry-forward: ran, 0 carried.** Each country's `manifest.scoring.carry`
  is `{ran: true, history_rows: 1, usable_rows: 0, carried: 0}`: the
  2026-09-25 snapshot was seen and rejected (no `questions_version`, older
  instrument). Week one as it should be.
- **Dropped events: none.** One event listed and kept (TW a2, Taipei mayoral
  election 2026-11-28, `unclear`), so `score_3m = score_12m` everywhere.
  Routine-deadline, date-not-in-article and beyond-horizon all 0 this week.
- **The quality report** flags all five countries with "n schema
  violation(s)", 119 in total: every one is `levels_nulled_no_evidence`,
  the model's 0-with-no-evidence habit (deferred §24), harmless under Rule
  A. The report still calls it a schema violation and a defect; it is
  neither, and it will fire every week until the report learns the new
  counts. Coverage 72 (TW) to 87; 0 digest failures; 5 walls caught.
- **Three validator counts stay at 0** in production: no indicator
  citations, no unknown articles, no events dropped.
- **Cost, from the ledger:** $0.5829 for five countries, **$0.117 per
  country-week**, of which the scorer is $0.083 (15 calls, $0.416) and the
  gate plus digests $0.033 (319 gpt-4o-mini calls, $0.167). **$5.60 a week
  for 48 countries.** The calendar and alert rankers are not metered
  through the run meter (one gpt-4o-mini call each, cents).
- **Provenance note.** The rows' `git_sha` is d3b3e50, the data commit; the
  `carry` record in `combine.py` and the per-country carry line in
  `pipeline.py` were in the working tree at run time and committed after.
  Everything that moves a score was committed in 49c6f44 before the run.

The scorer measurement is **closed**: M1, face validity and M4 passed on
the pre-set lines, the production week wrote what the measurement said it
would, and the remaining items are in `docs/deferred.md` §24.
