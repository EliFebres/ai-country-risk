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

# Step 5e: eight scorer fixes, and the benchmark set re-frozen

2026-11-08, branch `backend-update`. Seven defects read out of the first
production week's Kuwait record and the frozen Kuwait payload, each an
instruction the model ignored or a gap in selection, moved into code; one
decision, that the scorer reads an anonymous payload; one measurement on six
re-frozen payloads under `backend/measurements/step5e/`; and a second
production week. Prompt version `77c7868787e3…` (was `982172f001dc…`),
questions version `e018391fae57…` (unchanged, so carry-forward still works
across this change). Session spend **$9.10** of an $18 cap (raised from $15
with Eli, see §30).

**The six masked payloads under `step5e/payload_<iso2>.json` and the first
masked merge per country under `step5e/reference_<iso2>.json` are the
reference set for step 6, the model benchmark.** A prompt change after step 6
records its reference answers would force step 6 to be redone, which is why
all eight went in now.

## §28. The eight fixes

Each fix is a rule the code enforces. The prompt keeps saying the same thing;
the gate taught that an instruction the model does not follow becomes code.

- **Fix 1, one story, one article, checked again after selection.** The
  first story check works from the gate's `what_happened` sentences, which
  can be wrong for a headline-only article (the live week's a2 read
  "Kuwait's federal cabinet" when the story was Pakistan's). After the
  twenty are chosen, `relevance.dedupe_selected` sends the selected
  **titles and** sentences to the same story model with the same schema
  and validation; one article per story survives by the existing keep rule
  (`_keep_order`, now with every body's digest quality known), each freed
  slot takes the next eligible article in ranking order
  (`relevance.refill`), and the check runs again, at most three times.
  Drops are recorded in the manifest's `same_story.articles` with `pass:
  "post_selection"`.
- **Fix 2, no headline-only article reaches the scorer.** After the digests
  say what each body is, every selected article whose `body_status` is
  `title-only` (no body, or a body the digest calls `not_article`) is
  dropped and refilled, and the newcomers are digested, until the set is
  stable or the ranking runs out. Nothing tops up from ineligible articles:
  three of the six payloads are short of twenty (§29). The validator keeps a
  tripwire, `levels_nulled_title_only`: a non-null level whose only evidence
  is headline-only articles is nulled and counted. On the new payloads it
  is 0 by construction; on the old frozen payloads it fires (KW's F3 = 1 on
  a2's headline, in every call).
- **Fix 3, each answer quotes its evidence.** Every question's answer has a
  required `quote`: up to 25 words copied from one of the cited articles.
  `validate.quote_found` normalises both sides (lower case, whitespace,
  curly quotes and dashes, edge punctuation) and requires at least four
  words and a substring match against the text the scorer was sent for at
  least one cited article (`validate.article_texts`, so the masked text).
  A miss nulls the level and counts `levels_nulled_quote_not_found`. The
  pre-set fallback (90% of the words in order) is implemented as
  `QUOTE_MATCH = "ordered_90"` and `step5e revalidate` can re-apply it to
  stored raw answers without a call; §30 says whether it was needed.
- **Fix 4, evidence before the level.** The schema's property order, and
  its `required` order, is `evidence`, `quote`, `note`, `level`. Structured
  outputs generate in schema order, so the model writes what it has before
  it commits to a number.
- **Fix 5, the model knows the date and the window.** The payload opens
  with `as_of` and `window` ("articles published in the 30 days before
  as_of"), `prompt_for(country, as_of)` writes "Today is {as_of}" into the
  prompt, and the three prompt edits are the brief's, verbatim: the opening
  paragraph (judge each condition as it stands today; an earlier event
  counts only if it happened in the last 30 days or is said to be going
  on), the "For every question, answer in this order" block (evidence,
  quote, note, level; use 0 only when an article shows normal functioning,
  never because nothing was reported), and the SCHEDULED EVENTS window
  ("the 90 days after today"). `score_country` refuses to run without a
  date rather than reading the clock.
- **Fix 6, at most two spillover articles.** In `relevance.select` a T5
  article sorts behind every T1–T4 article in its ledger bucket, and
  `_fill` lets at most `T5_MAX_SELECTED = 2` into the selected set, refills
  included; a high-impact event keeps its place at the front whatever its
  test. `select` now ranks the whole eligible pool and returns it as
  `ranked`, which is what `refill` draws from, so a drop after selection
  never re-runs selection. The census records `t5_selected`.
- **Fix 7, a split vote gets more calls, not the lower level.**
  `scoring.run_scoring` is the one scoring loop, used by production and by
  every measurement: three calls; if every question is unanimous (null
  counts as a vote) the median of three stands; otherwise two more calls
  and `combine.vote_merge` over five, a question non-null once three calls
  answered it, its level the most common one; a tie gets one more call and
  a recount, to seven at most, and a tie at seven takes the median of the
  answered levels, upper middle on an even count. Scheduled events need
  more than half the distinct calls (≥2/3, ≥3/5, ≥4/7); flags stay `true >
  false`. `manifest.scoring.merge` carries `k`, the votes, `extended` and
  the questions still tied before the final recount.
- **Fix 8, the scorer reads an anonymous payload.** Only the scorer's
  input is masked; retrieval, the gate, the story checks, selection, the
  database and the frontend keep real names. For every selected article a
  second digest is generated under `MASKED_DIGEST_PROMPT` (the digest
  prompt plus the brief's masking block, verbatim, plus a `masked_title`),
  cached in `llm_artifact` under `mode = 'masked'`, `country_iso2 = iso2`
  and the masked prompt's version, keyed on the title and body together;
  the up to three full texts are rewritten by gpt-4o-mini
  (`REWRITE_PROMPT`: keep every sentence and number, change only names)
  and cached as `kind = 'rewrite'`. The masked payload drops `country` and
  the structural facts' `name`, sends `masked_title` as the title and the
  masked digest, and keeps publishers. `prompt_for` substitutes "the
  country". `backend/util/gazetteer.py` holds, for all 48 roster
  countries, names, codes, demonyms, capital, largest cities, currency
  names, symbols and ISO code, a few proper nouns that name the country
  without its name (its parliament, its national companies and agencies,
  the rival its news is about), and the other roster countries' names.
  Whole-word, case-insensitive, codes case-sensitive, currency symbols
  without a trailing boundary so `KD602m` and `€250m` match. A hit in a
  freshly generated digest regenerates it once naming the leaked terms,
  then replaces the term by code (`[the country]`, `[a city]`, `[local
  currency]`, `[another country]`); a hit in a rewrite is replaced by
  code; only clean text is cached, so a re-run reproduces the same bytes.
  The leak check runs again on the whole serialized payload in
  `assemble_country` and in `score_country`: a surviving term raises
  `MaskLeak` and the country fails into `run_ledger`. Publishers and the
  structural facts (region, regime) are skipped by the check and counted
  as `publisher_mentions`. `bullet_summary` is stored as returned.

  Two things the brief did not pre-decide. The named digest is still
  generated (it carries `body_quality`, which Fix 2 needs before masking
  can start, and it is already cached from past weeks), so each article
  costs one extra gpt-4o-mini call. And the gazetteer's `extra` lists hold
  a few office-holders (Putin, Trump, Powell, Lavrov) for the test-set
  countries: not a person list, which the brief rules out, but the handful
  whose surname alone is the country's name.

Fix 2 also closed a gap the brief did not list: the denylist filter after
selection used to rebind `items` while the payload was built from
`gate["selected"]`, so a blocked article could still reach the scorer. The
final list is now the one list, for the payload, the census and the manifest.

## §29. Selection on fresh candidates, old rules against new

`python -m backend.llm.step5e select-check`, 2026-11-08. Retrieval, gate and
the first story check ran once per country; the old selection rules (kept
only inside the script) and the new ones ran on the same candidates. 434
gpt-4o-mini calls, $0.19, plus $0.03 to re-freeze HK and TW after a
gazetteer correction (Beijing, Chinese and PLA were in Taiwan's own term
list, so code replacement wrote `[the country]` where `[another country]`
belonged; they now have a `rival` kind).

| | multi-article stories | title-only sent | T5 sent | articles sent | refill digests |
|---|---|---|---|---|---|
| US | 1 → 0 | 7 → 0 | 3 → 2 | 20 → 20 | 6 |
| PT | 2 → 0 | 5 → 0 | 2 → 2 | 20 → 12 (short 8) | 0 |
| KW | 0 → 0 | 3 → 0 | 6 → 2 | 20 → 16 (short 4) | 0 |
| HK | 1 → 0 | 3 → 0 | 1 → 1 | 20 → 16 (short 4) | 0 |
| TW | 0 → 0 | 4 → 0 | 2 → 1 | 20 → 20 | 3 |
| RU | 1 → 0 | 7 → 0 | 2 → 2 | 20 → 20 | 7 |

(Old → new. "Multi-article stories" is what the post-selection story check
finds in each final set; the new column is the confirming check on the final
twenty.) Every pass line holds: zero multi-article stories, zero headline-only
articles, at most two spillover articles, in all six. The shortfalls are the
rule working: PT had 20 relevant articles, five of them headline-only and
three of them second tellings the first story check had not caught (PBS, Al
Jazeera and Amnesty on the housing protests); KW had 22 unique stories, four
of them spillover beyond the cap. The hand check holds too: the Pakistan
defence pact is one article (a8), five copies set aside before selection and
the sixth, the headline-only tvoneusa copy whose gate sentence read "Kuwait's
federal cabinet", dropped by Fix 2.

Mask census (hits in freshly generated masked text, regenerated, replaced by
code, publisher names carrying a term): US 28 / 13 / 20 / 5; PT 14 / 5 / 4 /
3; KW 28 / 10 / 10 / 12; HK 36 / 10 / 28 / 7; TW 44 / 17 / 27 / 12; RU 37 /
15 / 29 / 2. Zero gazetteer terms in any payload sent. The most common
survivors of the first rewrite were the subject's name and demonym (Kuwait,
Russian, Taiwan) and other countries' names in sanctions and trade stories;
"US dollars" is exempt by design. The masked payloads are 5–8% smaller than
the named twins (5.1k–8.4k tokens).

## §30. Re-measurement: 280 calls, $8.22, every pre-set decision passes

All calls in production mode (Fixes 3, 4, 5 and 7 active), through the same
`scoring.run_scoring` the weekly run uses, under
`backend/measurements/step5e/calls/` and `merges/`. Three arms: the six
step 5 frozen payloads under the new instrument, named, with their own run
date as `as_of` (**old payloads**); the six new payloads as production
sends them (**masked**); and their named twins (**named**). The brief asked
for 5 merges in every arm; at 3 to 7 calls a merge that projected $14–20
against a $15 cap, so with Eli the cap moved to $18, the masked arm kept 5
merges and the two comparison arms ran 3, with a stop line of $12 on the
masked arm alone (it cost $3.58). A call is $0.029 (8.8k in, 720 out);
61% of merges split at three and went to five, one to seven; a merge is
4.2 calls on average.

Pre-set decisions, one line each.

- **Quote rule (Fix 3): 47 of 1,918 non-null levels nulled, 2.5%, keep the
  rule as is.** Every failure is a near-miss: a quote that stitches two
  `numbers` entries together ("[local currency]62.3 billion Canadian
  deficit for 2026-27, 2.5% of economy", US F6 in 7 calls), or one word
  changed ("detected" for "detecting", TW O5). The 90%-in-order fallback is
  implemented and was not needed.
- **No-evidence nulls (Fix 4): 5.6 per call before, 2.6 after**, on the same
  six payloads (step 5d's 30 calls against this session's 80). KW 3.1 → 0,
  HK 8.0 → 0, TW 11.7 → 0.3, RU 0 → 0; US 7.7 → 6.4 and PT 13.0 → 8.1 are
  the two payloads where the model still writes 0 with nothing behind it,
  now mostly for the information questions. On the new masked payloads it is
  0.07 per call.
- **Split votes (Fix 7): 40 of 66 merges extended** (old payloads 13/18,
  masked 16/30, named 11/18); one masked US merge went to seven on an O3
  tie. The lower middle never decided a question.
- **3a, old payloads, new instrument against step 5d: no composite moved by
  more than a point.** US 29 → 29, PT 19 → 19, KW 39 → 40, HK 25 → 25, TW
  18 → 18, RU 58 → 58. The questions behind them did move: 17 modal levels
  changed. KW's F3 = 1 on a2's headline is gone (the title-only tripwire,
  every call), O1 0 → 2 and O3 1 → 0 on the court rulings, O5 3 → 2 (the
  Iranian strike is read as near, not on). US O5 1 → null is the quote
  rule (13 nulls: the quote was paraphrased). HK's F1, F3, F4 and E1 went
  from 0 to null in every call: the model now leaves a question alone
  rather than writing 0 because nothing was reported, which is the prompt
  edit working. Rule A hands each of those to the data half, so the
  composite stands.
- **M1 (masked, 5 merges each): worst `score_12m` SD 0.71, pass.** US
  27/28/28/28/29 (F6, O2 and O3 each flip once in five), TW 23/23/24/24/23
  (O3 null or 1), RU 56/56/57/57/56 (F3 0 or 1); PT 20, KW 40 and HK 25 in
  all five, unanimous at three calls every time.
- **Face validity (masked medians): RU 56 > KW 40 > US 28 > HK 25 > TW 23 >
  PT 20, pass.** RU highest, PT lowest.
- **M4 (masked): 8 merges carry a 3-month gap, every one traces, pass.** US
  −3 in all five merges on a13's Opportunity Zones designations taking
  effect 2027-01-01 (down); PT +3 in three of five on a1's student march
  on 2026-11-18 (up; the other two merges did not reach a majority for the
  event). Dropped: 41 "date not in article" (nine distinct events, the model
  giving Taiwan's local elections 11-30, 12-01 and 12-31 when a5 names no
  date, and inventing the end of a US airspace restriction) and 6 "past";
  no routine deadlines. Masking did not eat a date: none of the nine dates
  is in the named text either.
- **Selection: zero multi-article stories, zero headline-only articles, at
  most two spillover articles, in all six payloads, pass** (§29).
- **Masking cost (median masked − median named): US 0, PT −1, KW +1, HK 0,
  TW −1, RU −1; every country within 5 points, masking is adopted.** The
  composites agree; the questions underneath agree less. US differs on 9
  of 16: masked, the model answered F1, F3, F5, F6, O3, O4 and O6 where
  named it answered null, and four of those cite a6, a Canadian budget
  story admitted under the spillover test whose masked digest opens "A
  government unveiled a budget". Masked, a spillover article about another
  country reads as the subject's own. TW differs on 4: named O5 = 2 and O6
  = 2 (Chinese sorties, the detention of a party leader) went null masked,
  and masked O1 = 2 and F5 = 2 (election fraud indictments) went null
  named; KW O5 2 masked against 1 named on the same two articles; RU F3 0
  against 1 on the crypto opening. See §32.
- **Leaks: zero gazetteer terms in any payload sent**, checked in
  `assemble_country` and again in `score_country` before every masked call.

| | score_12m (masked) | SD | friction | order | information | edge | answered / 16 | k |
|---|---|---:|---|---|---|---|---:|---|
| RU | 56 | 0.55 | 41 | 64 | 68 | 64 | 4 | 5,5,5,5,5 |
| KW | 40 | 0.00 | 27 | 40 | 66 | 24 | 4 | 3,3,3,3,3 |
| US | 28 | 0.71 | 31 | 29 | 21 | 19 | 13 | 5,5,5,7,5 |
| HK | 25 | 0.00 | 9 | 20 | 67 | 10 | 3 | 3,3,3,3,3 |
| TW | 23 | 0.55 | 28 | 24 | 14 | 0 | 6 | 5,5,5,5,5 |
| PT | 20 | 0.00 | 23 | 26 | 3 | 15 | 4 | 3,3,3,3,3 |

(Medians over five merges; ledger medians.)

**3c, identifiability (report only).** One gpt-4o-mini call per masked
payload, asked which country it is. With the payload as sent: US identified
(0.7: "National Guard and Customs and Border Protection"), KW identified
(0.7: "articles published by Kuwait Times and Arab Times Kuwait News"), TW
identified (0.6: the APEC summit and a protocol complaint), PT, HK and RU
not enough to tell (PT 0.4 on "a shared currency (euro area)", HK 0.5 on
"one country, two systems" and the SCMP as publisher, RU's top guess
Ukraine at 0.4 on the strikes). A second run with the publisher names
blanked, to measure what the outlets alone give away: only the US is still
identified; KW, TW and HK fall to 0.4 and "not enough to tell", RU's top
guess becomes Ukraine at 0.6. Events identifying a country is what the brief
accepts. Publishers identifying one is the brief's own decision to keep
them, and it is the single largest tell: whether "Kuwait Times" should
travel to the scorer as "[publisher]" is Eli's call, not made here. The two
agency names in the US payload are proper nouns the gazetteer does not hold;
adding them is a one-line change left for after this session, so that the
reference payloads and the production week share one gazetteer version.

**The reference set.** `step5e/reference_<iso2>.json` is the first masked
merge per country (US k=5 score 28, PT k=3 20, KW k=3 40, HK k=3 25, TW k=5
23, RU k=5 56), with the payload's sha, the prompt and questions versions,
the model, the seed and the merge rule. Step 6 compares every candidate
model against these answers on `step5e/payload_<iso2>.json`.

## §31. The second production week

`python -m backend.main etl --test-set` against the dev database, after
every Part 3 decision passed. Started 2026-11-08 23:52 UTC and crossed
midnight, so every row is stamped `run_date = 2026-11-09`; 14.2 minutes
wall-clock, 167 s median per country (US 272 s with four refill rounds).
Written: 5 `risk_snapshot` rows, 7 `run_ledger` rows (5 `etl`, 1 `run`, 1
`report`), 15 `news_alert` rows. KW's row, manifest and all, is
`backend/measurements/step5e/week2_manifest_KW.json`; the five rows'
headline fields are in `week2_table.json`. `git_sha` 3451310 is the commit
every scoring change was in.

| | score_12m | score_3m | friction | order | information | edge | answered / 16 | carried | k | quote-null | title-only | T5 | sent | cost |
|---|---:|---:|---|---|---|---|---:|---:|---|---:|---:|---:|---:|---:|
| KW | 40 | 40 | 29 (28 / 30) | 38 (39 / 37) | 66 (66 / 66) | 24 (47 / 0) | 4 | 2 | 5 | 0 | 0 | 2 | 16 | $0.137 |
| US | 28 | 25 | 30 (30 / 30) | 31 (29 / 33) | 21 (21 / 21) | 19 (39 / 0) | 7 | 5 | 5 | 0 | 0 | 2 | 20 | $0.172 |
| HK | 25 | 25 | 9 (6 / 11) | 21 (20 / 23) | 67 (67 / 67) | 10 (20 / 0) | 4 | 3 | 3 | 0 | 0 | 1 | 17 | $0.089 |
| PT | 21 | 24 | 25 (20 / 31) | 26 (24 / 28) | 3 (3 / 3) | 15 (29 / 0) | 5 | 1 | 5 | 0 | 0 | 2 | 12 | $0.134 |
| TW | 21 | 21 | 24 (20 / 28) | 22 (15 / 28) | 14 (14 / 14) | 0 (0 / 0) | 4 | 5 | 3 | 3 | 0 | 1 | 20 | $0.089 |

(Ledger = score (data / news). "Answered" is this week's own non-null
levels; "carried" the levels the carry-forward took from the 2026-11-07
row, which it could because `QUESTIONS_VERSION` did not move. k is the
number of scorer calls: three where the three were unanimous, five where
they split.) Three of five votes split and extended (PT, US, KW), none
tied. The quote rule nulled 3 levels, all TW's O5 ("detected" for
"detecting" again, every call); the title-only tripwire 0, as it must now;
no-evidence nulls 0 in 21 calls, against 27 in the first week's Kuwait row
alone. Headline-only articles dropped and refilled: KW 2, US 11, HK 4, PT
2, TW 4; second tellings set aside after selection: HK 4, TW 2; spillover
sent: 1 or 2 everywhere; PT (12), KW (16) and HK (17) short of twenty. Two
scheduled events kept (US a11, Opportunity Zones 2027-01-01, down; PT a1,
the student march 2026-11-18, up), three dropped (two "date not in
article", one "past"). Masking: 85 gazetteer hits across the five, 32
regenerated, 55 replaced by code, 0 leaks in any payload sent, 0 fallbacks.
The quality report counts the 8 remaining validation notes as "schema
violations" and still needs to learn the new counters (deferred §24).

**Cost.** $0.621 for five countries from the ledger, **$0.124 per
country-week**, of which the scorer is $0.115 (21 calls, $0.575) and the
gate, digests, masked digests, rewrites and story checks $0.009 (124
gpt-4o-mini calls, $0.046; most digests were cache hits from this
session's own freeze). On a fresh week the small-model half is about $0.06
a country (the first week's $0.033 plus a masked digest per article and
three rewrites), and the scorer at the measured 4.2 calls a merge is
$0.116. **About $8.50 a week for 48 countries**, against $5.60 under the
median of three; the extra $2.90 is Fix 7's price, and the ledger's own
$5.96 this week understates it because the caches were warm.

## §32. What the model still misreads

- **A spillover article about another country, masked, becomes the
  subject's own (E1, F1, F6).** US a6 is Canada's budget, admitted under
  T5. Its masked digest opens "A government unveiled a budget aimed at
  reducing dependence on a major trading partner", and masked US F6 = 0
  and F1 = 1 cite it in most calls (the quote rule nulled F6 whenever the
  quote stitched two numbers together, which it usually did). Named, the
  same calls leave F1 and F6 null. The composite did not move, because
  the levels it produced sat near the data half, but the mechanism is
  wrong: the masking block tells the digest to write other countries by
  their relation, and it does, but "a government" with no relation stated
  reads as the country's. The fix is one sentence in the masking block
  ("if the article is mainly about another country, say so in the first
  words: 'A neighbouring country's government…'") and it moves the masked
  digest prompt version, so it waits for a session that can re-freeze.
- **O5 counts a near-strike as 2 and a quote as a paraphrase.** TW O5 = 2
  on Chinese sorties around the island in every named call; masked, the
  quote "detected four military aircraft sorties and eight vessels" failed
  against "detecting four military aircraft sorties and eight vessels",
  so the level was nulled in the masked arm and in production. The
  ordered-90 fallback would have passed it (9 of 10 words) in the long
  form and failed it in the short; the rate (2.5%) did not reach the band
  that switches it on. KW O5 reads the Houthi strikes on a Gulf oil
  facility as 2 masked and 1 named on the same two articles: "a
  neighbouring country" is nearer than "Saudi Arabia".
- **Scheduled events still get invented dates (O2, O4).** Nine distinct
  events in the masked arm carried dates that appear in neither the masked
  nor the named text: Taiwan's local elections as 11-30, 12-01 and 12-31
  (a5 names no date), the end of a US airspace restriction as 11-11. The
  validator dropped every one (41 of 124 calls listed one), so `score_3m`
  moved only on the two events whose dates are written in their articles.
- **F3 and O3 are the flipping questions.** RU F3 is 0 or 1 on the central
  bank opening crypto to retail (the named twin says 1); US O3 is 1 or 2 on
  sanctions and a university no-confidence vote; TW O3 null or 1. Each
  flip costs a point of SD and no more.
- **The information questions (I1–I3) are answered 0 with nothing behind
  them on US and PT**, 6 and 8 times a call on the old payloads, down from
  8 and 13; on the new payloads 0.07 a call. Rule A hands them to the data
  half either way.

## §33. Spend and files

Session spend **$9.10** of the $18 cap: selection check and freezes $0.25
(523 gpt-4o-mini calls, including one Kuwait trial run and the HK/TW
re-freeze), the three scoring arms $8.22 (280 gpt-4o calls), two probes
$0.01, the production week $0.62.

- `backend/measurements/step5e/payload_<iso2>.json`: the six masked
  payloads, the step 6 benchmark set, with `selection`, `mask`, `data` and
  the check row; `payload_<iso2>_named.json` the same articles named.
- `selection_check.json`: §29's table, old against new.
- `calls/<arm>_<iso2>.jsonl`: every call (280) with its raw answer, the
  validated answer, violations, counts and scoring; `merges/<arm>_<iso2>.jsonl`:
  every merge (66) with k, votes, extended, the merged answer and its score.
- `analysis.json`: every line of §30, with the per-question detail behind
  3a and the masking cost; `probe.json`: both identifiability runs.
- `reference_<iso2>.json`: the first masked merge per country, the
  reference answers for step 6.
- `week2_manifest_KW.json`, `week2_table.json`, `week2_etl.log`: the
  production week.
- `backend/llm/step5e.py`: the script (`select-check`, `score`,
  `revalidate`, `probe`, `analyse`, `reference`). `scorer_measure.py` is
  untouched, so step 5d stays reproducible.

The eight fixes are in; the six masked payloads and their reference merges
are frozen for step 6; masking is adopted on a cost of at most one point;
and the second production week wrote what the measurement said it would.
Open: the masking block's handling of articles about other countries, the
two US agency names and the publisher question from §30's probe, and the
quality report's counters (deferred §24).
