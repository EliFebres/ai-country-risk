# The payload: what has to be in it before the model can discriminate?

Two attempts, against the same criteria, recorded together so they can be read
against each other. The scorer is settled at `gpt-4o`; this is the other half of
the instrument.

1. **[p3-context](#attempt-1-trailing-context)** — four quarters of masked prose
   history. Measured and **rejected**: it made the instrument coarser.
2. **[the vintage fix, then p4-trend](#attempt-2-the-baseline-was-wrong)** — and
   the discovery that attempt 1 was measured on a payload that was missing two
   of its four ledgers entirely.

---

## Attempt 1: trailing context

Whether the pilot runs on **p2** (30-day window only) or **p3-context** (plus four
quarters of masked history).

**Written before any p3 data existed.** The criteria below, including their
numeric thresholds, were committed before the arm was run. That is the only thing
that makes a verdict against them worth anything.

---

## The problem being tested

The 30-day window measures evidence **flow**. Institutional decay is a **stock**.
A judiciary compromised eighteen months ago is still compromised, but if nothing
was published about it this month the payload carries no trace of it — so a quiet
week has nothing in it but the prompt's own calibration language, and the model
answers with the calibration language.

That is measurable, and it was measured. On US 2019 `gpt-4o` produced **9 distinct
scores across 52 weeks**, with **a third of anchors on exactly 0.50** and **69% on
a multiple of 5** — against a prompt that says *"use precise values (37, 62, 81) —
never round to multiples of 5."* On TR 2018, a determinate window, the same model
on the same prompt sits at **19%**, which is within a point of the 20% floor you
would get by chance.

p3-context adds history **as evidence, never as prior scores**. Feeding a model
its own earlier scores would make the series autocorrelated by construction, with
no way to distinguish that from a country actually deteriorating.

## The arms

| arm | what it is | cost |
|---|---|---|
| **A — p2** | the existing reference rows, US 2019 + TR 2018, 105 anchors | **$0** — read from `risk_snapshot` |
| **B — p3-context** | the same anchors re-scored with the block, scorer held at `gpt-4o` | ~$4.20 + ~$0.50 context |

Run through `bakeoff`, which never calls `freeze()` and writes no
`risk_snapshot`, `run_ledger` or lint rows. Going through `pilot.run score` would
fight the version freeze: it is a single global sentinel and
`--override-version-drift` *re-pins*, so an A/B would leave whichever arm ran last
pinned for the real pilot — the "guard that is always overridden catches nothing"
failure `drift()`'s own docstring warns about.

## Criteria

| # | Criterion | The line |
|---|---|---|
| **(a)** | **Discrimination** | distinct composite values on US 2019 **≥ 15** (from 9) **and** round-number share on US 2019 **≤ 44%** (from 69.2%) |
| **(b)** | **No lag** | on TR 2018, the first week the score moves **≥ 0.05 above its Q1 baseline** is **no later** in the context arm than in p2 |
| **(c)** | **Refinement, not rewrite** | between-arm Spearman ρ on TR 2018 **≥ 0.80** |
| **(d)** | **Report only** | lag-1 autocorrelation and longest identical run, both arms both windows, against p2's **0.299 / 0.564** and **5 / 7**. No threshold. |
| **(e)** | **Cost** | per-snapshot delta **≤ ~15%** against measured p2 ($0.0402 US, $0.0381 TR) |

**Decision rule: adopt p3 iff (a), (b) and (c) all hold.**

- If **(b)** lags, **reject or redesign — do not rationalise it.** Context that
  makes the model sticky, anchored on last quarter's mood and late to the turn, is
  the failure mode in the other direction, and it is worse than amnesia because it
  is invisible in every aggregate.
- If **(a)** doesn't move, context is not the cure for snapping. **Propose, do not
  run,** a within-band-discrimination prompt test as the alternative.
- **(d)** is expected to rise. Evidence stock is persistent, so a series that
  knows about last quarter *should* be more autocorrelated than one that does not.
  A monotone-sticky series is a different thing, which is what the longest-run
  figure is there to expose, and why (d) carries no threshold — a number with a
  threshold attached invites the threshold to be met.

### The one criterion that was changed, and why

Criterion (a) originally read: *distinct values rise materially (≥15 vs 9), and
**the band-midpoint gravity share drops**.*

**Its premise was false.** Measuring band-midpoint gravity — the share of anchors
landing on the midpoint of one of the prompt's five bands — returned **0.0% for
every model on both windows**. The prompt's calibration anchors (12/38/58/85/95)
took 15–35%, about what proximity alone yields. There was nothing for the clause
to drop from; it would have been satisfied vacuously on any result.

It was replaced with **round-number share ≤ 44%**. Same intent — does context
reduce snapping — but a quantity that can actually move, with a principled null
(20% under a uniform distribution over integers) and an explicit prompt
instruction behind it.

**The 44% is not a judgement call made after the fact.** TR 2018 sits at 18.9%,
which is what this model on this prompt does when the evidence is determinate;
chance is 20%. Requiring at least half of the 69.2 → 18.9 gap to close puts the
line at 44.05%, rounded to 44%. The number is written here so that no
"materially" judgement gets made once the result is visible.

**The swap happened before the p3 arm was run**, so it could not have been chosen
to suit an outcome. The original criterion, the measurement that falsified it, the
replacement and its derivation are all recorded above rather than quietly
substituted — which is the only version of this that leaves the pre-registration
meaning anything.

## What is deliberately not a criterion

**Absolute score level.** p3 may shift the whole series and that is survivable —
the prompt's calibration anchors can be moved and the series shifts with them.
Reordering cannot be recalibrated away, which is why (c) is a rank correlation and
there is no level test.

**Agreement with p2 on US 2019.** If context works, the arms *should* disagree
there — that window is where p2 is least informative. Demanding agreement on the
window the change is meant to fix would be a criterion that only a no-op could
pass. (c) is scoped to TR 2018 for exactly that reason: the determinate window is
where p2 is trustworthy, so it is where the two arms ought to agree.

---

## The result: p3-context is rejected

**Two of the three adoption criteria fail. The pilot runs on p2.**

Arm B: 105 anchors re-scored with the block, scorer held at `gpt-4o`, $3.90.

### Every criterion against its pre-registered line

| # | Criterion | Line | Measured | Verdict |
|---|---|---|---|---|
| **(a)** | distinct values, US 2019 | **≥ 15** (from 9) | **7** | **FAIL** |
| | round-number share, US 2019 | **≤ 44%** (from 69.2%) | **75.0%** | **FAIL** |
| **(b)** | first ≥0.05 move above Q1 baseline, TR 2018 | no later than p2 | **identical** (2018-02-05 by Q1 mean; 2018-01-22 by first anchor) | **PASS** |
| **(c)** | between-arm ρ, TR 2018 | **≥ 0.80** | **0.777** | **FAIL** |
| **(d)** | autocorrelation / longest run | report only | see below | — |
| **(e)** | per-snapshot cost | **≤ +15%** | **−5.9% US, −3.9% TR** | **PASS** |

**Decision rule: adopt iff (a), (b) and (c) all hold.** (a) and (c) do not.
Rejected.

### (a) did not merely fail to move — it moved backwards

This is the finding, and it was the thing p3 existed to fix.

| US 2019 | p2 | p3-context |
|---|---|---|
| distinct composite values | 9 | **7** |
| round-number share | 69.2% | **75.0%** |
| band migration | — | **52 of 52 anchors stayed in Moderate** |

Given four quarters of trailing evidence, the model resolved the year into
*fewer* distinct scores and snapped to round numbers *more often*. Not one anchor
changed band. The same pattern holds on TR 2018 (9 → 7 distinct, round share
unchanged at 18.9%).

So the amnesia diagnosis was right about the symptom and wrong about the cause.
Coarseness is not the payload failing to carry history — the model was given the
history, in the form it asked for, and became *more* coarse. Something about how
it converts evidence into a number is doing this, and more evidence is not the
lever.

### (b) and (d): the feared failure did not happen either

The other direction — context making the model sticky, anchored on last quarter's
mood and late to a turn — was the tripwire in (b), and there is no sign of it.
TR 2018's first ≥0.05 move above the Q1 baseline lands on the **same anchor** in
both arms under either definition of the baseline.

Autocorrelation *fell* in both windows, which is the opposite of what was
predicted for it:

| | US 2019 | TR 2018 |
|---|---|---|
| lag-1 autocorrelation | 0.299 → **0.142** | 0.564 → **0.493** |
| longest identical run | 5 → 5 | 7 → **8** |

(d) was written expecting autocorrelation to rise, because evidence stock is
persistent. It did not. That is worth recording as a failed prediction rather
than passed over: the block is not being read as a prior at all. Combined with
(a), the most economical reading is that the trailing paragraphs are being
largely **ignored** — they neither sharpened the series nor anchored it.

### (e): cheaper, for a reason worth knowing

p3 costs **less** per snapshot despite a larger payload — the evidence block grew
from ~1,430 to ~2,130 tokens on US. Input rose and **output fell** (≈605 vs ≈650
tokens), and `gpt-4o` bills output at four times input, so the output drop
dominates. A longer prompt producing a shorter answer is consistent with the rest
of the picture: the model wrote less, and discriminated less.

### What follows

**Rejected, and the pre-registered fallback applies.** Criterion (a)'s failure
clause reads: *if (a) doesn't move, context is not the cure for snapping —
propose, do not run, a within-band-discrimination prompt test.* That is what is
proposed, and deliberately not run, in `docs/deferred.md`.

The evidence now points at the prompt rather than the payload. The prompt already
forbids multiples of 5 and is disobeyed on 69–75% of US anchors; it offers five
calibration anchors that are themselves near band centres; and it never asks the
model to distinguish two weeks that sit in the same band. A payload change cannot
reach any of that.

**The code stays.** `p3-context` remains available behind `PAYLOAD_VARIANT`,
unset by default, so the measurement is reproducible and a later prompt change
can be tested against the same block rather than rebuilding it. What was learned
cost $3.90 and is worth more than the block: *more evidence did not make this
instrument finer, and the next attempt should not assume it will.*

---

# Attempt 2: the baseline was wrong

**Written 2026-08-29, before any arm of this attempt was read.** Arm A′ was
executing when the criteria below were committed and no result had been seen.
That is the only thing that makes a verdict against them worth anything, and it
is the same discipline attempt 1 was held to.

## What changed underneath attempt 1

`indicator_series.as_of` means "when this observation became public" — the whole
no-future rule for macro rests on it. Three writers stamped it from the clock
instead, so `payload._resolve`'s vintage bound dropped every row they wrote at
every historical anchor. Ten indicators vanished from every backfilled payload.

Measured at a 2019-06-01 anchor on the pilot corpus, indicators resolvable per
country, before the fix and after:

| ledger | registry | before | after |
|---|---|---|---|
| friction | 14 | 5.00 | 9.67 |
| uncertainty | 16 | 9.67 | 10.00 |
| **information** | 4 | **0.00** | **1.00** |
| **edge** | 4 | **0.00** | **2.67** |
| total | 38 | 14.7 | 23.3 |

**Two of the four ledgers resolved nothing at all.** Not a thin ledger — an
empty one, at every anchor, for the entire pilot. The information and edge
scores in every backfilled snapshot were produced from news articles and the
prompt's own calibration language, with no macro evidence beneath them.

Everything measured on backfilled anchors was measured through that: the p2
reference, the GATE2 baseline, the scorer bake-off, and both arms of attempt 1.

## What that does to attempt 1's verdict

p3-context was rejected on the finding that *more evidence made the instrument
coarser* — distinct values 9 → 7, round-number share 69.2% → 75.0%. That result
is not withdrawn, and it should not be treated as settled either.

The honest reading: p3 added four quarters of prose to a payload whose
information and edge ledgers were empty, and the model got coarser. Whether it
would do the same to a payload with all four ledgers populated is **not a
question attempt 1 answered**, because that payload did not exist when it ran.
The *conclusion* — that more evidence is not automatically the lever — survives.
The *measurement* was taken on a degraded instrument.

Re-running p3 on the fixed payload is not planned here. It is recorded as a
question the branch can no longer claim to have closed.

## The arms

| arm | what it is | cost |
|---|---|---|
| **A** | the stored p2 rows, scored **before** the fix | $0 — read from `risk_snapshot` |
| **A′** | p2 re-scored **after** the fix. The new reference | ~$4.20 |
| **C** | A′ plus one prompt rule pointing at `trend_1y` / `trend_5y` | ~$4.20 |
| **B** | A′ plus the computed p4 trend block | ~$4.20 |

**A′ against A isolates the vintage fix**, and is a supporting illustration
rather than the evidence for it — the per-ledger indicator counts above are the
measurement, and they are already in hand.

**Why arm C exists.** `_stamp` has emitted `trend_1y` and `trend_5y` on every
indicator since p1. They are serialized into every prompt. Nothing reads them:
no consumer, no test, and `AI_PROMPT_V3` — which explains `as_of` and
`staleness_days` — never mentions them. Measured on the fixed payload they are
populated on 22 of 23 resolved indicators for US and 24 of 25 for TR.

So the model has been handed a one-year and five-year change on nearly every
indicator for the life of the project and was never told the fields were there.
C is a **pure prompt change** — no new data — and it separates *"the model
needed more evidence"* from *"the model was never told what it already had"*.
Attempt 1 could not tell those apart, which is why its criterion (d) existed.

**The corpus is pinned.** The selected article set is recorded per anchor for
all 105 before arm A′ ran, and every later arm asserts an identical set. A
three-arm comparison where the arms read different evidence measures nothing, so
a mismatch is a hard failure rather than a footnote.

## Criteria

Baselined on **A′**, not on the old p2 rows.

| # | Criterion | The line |
|---|---|---|
| **(a)** | **Discrimination** | distinct composite values on US 2019 **≥ 15** and round-number share **≤ 44%** |
| **(b)** | **No lag** | on TR 2018, first move **≥ 0.05** above the Q1 baseline **no later** than A′ |
| **(c)** | **Not a rewrite** | between-arm Spearman ρ on TR 2018 **≥ 0.65** |
| **(d)** | **Trajectory is read** | share of `bullet_summary` outputs referencing direction. **No threshold** |
| **(e)** | **Cost** | per-snapshot delta **≤ ~15%** against A′ |
| **(f)** | **Report only** | lag-1 autocorrelation and run-length, all arms, both windows |

**Decision rule: adopt iff (a), (b) and (c) hold.**

- **If C alone clears them, that is the answer** and B's machinery is optional.
  Report it that way rather than adopting the larger change by default: a
  paragraph of prompt that works is worth more than a block of code that also
  works.
- **(c) is loosened from attempt 1's 0.80 to 0.65, deliberately.** A payload
  that genuinely discriminates on quiet weeks *must* disagree with A′ where A′
  is weakest, so the tighter line would have penalised the improvement being
  bought. 0.65 still catches an arm that has thrown the determinate window away.
- **(d) is the diagnostic attempt 1 lacked.** Near-zero means the block is being
  ignored, and an ignored block and a diluting block need different next steps.
- **(a) is unchanged from attempt 1**, thresholds and derivation included. It is
  the right test and this is the second attempt at it.

## What is deliberately not a criterion

**Absolute score level.** The fix adds ten indicators and the level may move;
that is survivable and recalibratable. Reordering is not, which is why (c) is a
rank correlation.

**Agreement between A and A′.** They *should* disagree — A was scored without
two of its ledgers. Demanding agreement would be a criterion only a no-op could
pass, and would amount to requiring that the bug fix changed nothing.

---

## The result: arm A′ and arm C

Corpus pinned and verified: article counts identical on all 105 anchors across
both arms, so the difference between them is the payload and the prompt and
nothing else. 105/105 scored in each arm, every arm-C row stamped
`v4.1-trend-fields`.

### A′ against the old p2 rows — the vintage fix, isolated

| | US 2019 | | TR 2018 | |
|---|---|---|---|---|
| | old p2 | A′ | old p2 | A′ |
| distinct values | 9 | **8** | 9 | **9** |
| round-number share | 69.2% | **76.9%** | 18.9% | **26.4%** |
| lag-1 autocorrelation | 0.299 | 0.433 | 0.564 | 0.642 |
| longest run | 5 | 4 | 7 | 5 |
| ρ vs old p2 | — | 0.582 | — | 0.808 |
| cost/snapshot | $0.0402 | $0.0376 (−6.5%) | $0.0382 | $0.0352 (−7.9%) |

Ten indicators restored, two of them entire previously-empty ledgers, and the
series got coarser. What did move: **10 of 53 TR anchors went Moderate → High**,
and `edge_vitality` correlates at 0.06 (TR) and −0.29 (US) between the arms —
the ledgers that had no macro evidence were not producing a weak signal, they
were producing noise.

### Arm C against A′, against its pre-registered lines

| # | Criterion | The line | Measured | Verdict |
|---|---|---|---|---|
| **(a)** | distinct, US 2019 | **≥ 15** | **8** (from 8) | **FAIL** |
| | round share, US 2019 | **≤ 44%** | **82.7%** (from 76.9%) | **FAIL** |
| **(b)** | first ≥0.05 move, TR | no later than A′ | **earlier** — 2018-01-22 vs 2018-02-05 by Q1 mean; identical by first anchor | **PASS** |
| **(c)** | ρ vs A′, TR 2018 | **≥ 0.65** | **0.833** | **PASS** |
| **(d)** | direction-mention share | report only | **not measured — see below** | — |
| **(e)** | cost delta | **≤ +15%** | **+2.8% US, −10.2% TR** | **PASS** |
| **(f)** | autocorrelation / run | report only | 0.433→0.235 US, 0.642→0.670 TR; runs 4→3, 5→6 | — |

**Adopt iff (a), (b) and (c). (a) fails. Arm C is rejected.**

### (d) was unmeasurable, and that is a pre-registration failure

The criterion asked for the share of `bullet_summary` outputs referencing
direction. Bake-off arm rows carry every number the run produced and not one
word of its prose, so the field the criterion reads does not exist on the arm it
was written for.

Recorded as a failure rather than quietly dropped. It is the same class of
mistake this whole session has been chasing — a value nothing reads, except
here the criterion was written against a field nothing *writes*. `bullet_summary`
is now captured on every arm row, which fixes it for the next attempt and not
for this one.

### The finding: three interventions, one direction

| US 2019 | distinct | round share |
|---|---|---|
| p2, as it stood | 9 | 69.2% |
| p3-context | 7 | 75.0% |
| A′ — vintage fixed, ten indicators restored | 8 | 76.9% |
| C — told the trend fields exist | 8 | **82.7%** |

Three independent interventions by three different mechanisms — masked prose
summaries, ten real vintage-corrected indicators, and one paragraph of
instruction carrying no new data at all. **Round-number share rose every time.**
Distinct values never came near fifteen and never exceeded nine.

Whatever produces nine-ish distinct scores across fifty-two weeks is not
downstream of how much evidence the payload carries, and not downstream of
whether the model is told the evidence is there.

### The one place it worked, and what that says

On **TR 2018 — the determinate window — arm C moved distinct values 9 → 12**,
the best discrimination figure any arm has produced on either window, at a
round-number share that did not budge and a cost 10% *lower*. It also turned
earlier, not later: the tripwire in (b) fired in the good direction.

So the trend instruction is read, and it helps — on the window where the
evidence already resolves. On the ambiguous window it changed nothing and the
snapping got worse.

That is the same shape the bake-off found for the prompt's own
"never round to multiples of 5" line: obeyed at 18.9% where evidence is
determinate, ignored at 69% where it is not. **The instruction holds exactly
where it is least needed.** Two independent instructions now show that
signature, which points at how the model converts ambiguous evidence into a
number rather than at what the prompt tells it to do with clear evidence.

`docs/deferred.md` §12 — the within-band discrimination test — is the successor
this argues for, and it is still proposed and not run.

---

## Arm B: the computed trend block

105/105 scored, every row stamped `v4.2-trend-block`, article counts identical
to A′ on every anchor.

| # | Criterion | Line | Measured | Verdict |
|---|---|---|---|---|
| **(a)** | distinct, US 2019 | ≥ 15 | **7** (from 8) | **FAIL** |
| | round share, US 2019 | ≤ 44% | **90.4%** (from 76.9%) | **FAIL** |
| **(b)** | first ≥0.05 move, TR | no later than A′ | **earlier** — 01-22 vs 02-05 | **PASS** |
| **(c)** | ρ vs A′, TR 2018 | ≥ 0.65 | **0.788** | **PASS** |
| **(d)** | direction-mention share | report only | not measured — see above | — |
| **(e)** | cost delta | ≤ +15% | +13.6% US, **+17.0% TR** | **FAIL on TR** |
| **(f)** | autocorrelation / run | report only | 0.433→0.243 US, 0.642→0.556 TR; runs 4→3, 5→5 | — |

**Adopt iff (a), (b) and (c). (a) fails. Arm B is rejected**, and (e) fails on
TR as well — the block adds ~2,500 input tokens per snapshot, which is the one
cost the p3 arm avoided by producing shorter output.

## All four arms, on one page

**US 2019 — the ambiguous window**

| arm | distinct | round share | lag-1 | longest run | ρ vs A′ | $/snapshot |
|---|---|---|---|---|---|---|
| p2, as it stood | 9 | 69.2% | 0.299 | 5 | 0.582 | $0.0402 |
| A′ — ten indicators restored | 8 | 76.9% | 0.433 | 4 | ref | $0.0376 |
| C — told the fields exist | 8 | 82.7% | 0.235 | 3 | 0.790 | $0.0387 |
| B — computed trend block | **7** | **90.4%** | 0.243 | 3 | 0.618 | $0.0427 |

**TR 2018 — the determinate window**

| arm | distinct | round share | lag-1 | longest run | ρ vs A′ | $/snapshot |
|---|---|---|---|---|---|---|
| p2, as it stood | 9 | 18.9% | 0.564 | 7 | 0.808 | $0.0382 |
| A′ | 9 | 26.4% | 0.642 | 5 | ref | $0.0352 |
| C | **12** | 26.4% | 0.670 | 6 | 0.833 | $0.0316 |
| B | 10 | 24.5% | 0.556 | 5 | 0.788 | $0.0412 |

### The result

On the **ambiguous** window, round-number share rises monotonically with every
intervention — **69.2 → 76.9 → 82.7 → 90.4** — and distinct values fall to
seven. Four payloads, ordered from least to most evidence about trajectory, and
the instrument snaps harder at each step. The most explicit version, which
states directions in words so no inference is required, is the worst of all.

On the **determinate** window the same interventions help: C reaches twelve
distinct values, the highest figure in this table, and B ten, both above p2's
nine, at a round-number share that stays near chance.

Same prompts, same model, same corpus, opposite directions. **What separates
them is whether the evidence resolves**, and neither more evidence nor a clearer
explanation of it changes that.

## The decision

**The pilot runs on p2.** No variant is adopted. `p3-context`, `p4-trend` and
the `trend` prompt variant all stay in the tree behind their flags, unset by
default, because the measurement is worth reproducing and the next prompt
experiment can be tested against the same blocks rather than rebuilding them.

Three payload rejections cost **$12.36**, and the finding that came out of them
is not about payloads: an instruction is followed where the evidence is
determinate and ignored where it is not, and adding evidence does not move that
line. `docs/deferred.md` §12 — the within-band discrimination test — is what
this argues for, and it is deliberately **not** run here. It changes the
prompt's scoring mechanics rather than its inputs, and it deserves a
pre-registration written cold rather than appended to the session that motivated
it.

---

# Attempt 3: the elicitation, and a measurement that came first

**Written 2026-08-29, before any arm of this attempt was run.** Nothing below
was chosen with a result in view, and the two figures that *were* known before
writing — both computed from rows already on disk, at no cost — are stated here
rather than discovered later, so that they cannot be presented afterwards as
predictions.

This is §12, the fallback attempt 1 pre-registered and three sessions declined
to run. It changes neither the evidence nor what the model is told about the
evidence. It changes what the model is asked to decide.

## The dry run, which §28 bought and which changed the experiment

§28's lesson was narrow and specific: *a pre-registered criterion should be
computed once against a stored row before the arms are paid for.* Attempt 2's
(d) was written against `bullet_summary`, a field bake-off arm rows did not
carry, and that was discovered only after all three arms had been paid for.

Every criterion below was computed against A′'s stored rows before any arm was
run. All six return a real number. The exercise also reproduced every figure in
the tables above exactly, which is the strongest available evidence that the
harness and the write-ups still agree.

It found three things worth recording, and two of them changed the design.

**1. `bullet_summary` is on `p4-trend` rows only** (52/52 US, 53/53 TR). The
§28 fix landed with arm B, and A′ and C had already been scored, so the field
exists on exactly one of the three arms it was meant to serve. Criterion (d)
here is the lag criterion and does not read it — but the gap is real and is
recorded in `docs/deferred.md` rather than rediscovered by attempt 4.

**2. The coarseness tracks the scorer, not the payload and not the prompt.**
`backend/bakeoff/*/gpt-4.1.json` was captured under `PAYLOAD_VERSION=p2`,
`PROMPT_VERSION=v4.0-masked-production`, the same digest model, gazetteer,
sweep, seed and `git_sha` as the incumbent. One variable differs.

| US 2019 | distinct | round share | bands occupied | longest run | $/snapshot |
|---|---|---|---|---|---|
| A′ (`gpt-4o`) | 8 | 76.9% | Moderate **52 of 52** | 4 | $0.0376 |
| C / B (`gpt-4o`) | 8 / 7 | 82.7% / 90.4% | Moderate **52 of 52** | 3 / 3 | $0.0387 / $0.0427 |
| **`gpt-4.1`**, same prompt and payload | **18** | **5.8%** | LowMod 6 · Mod 43 · High 3 | 2 | **$0.0298** |

`gpt-4.1` clears criterion (a) — the line five payload and prompt arms failed —
with the elicitation untouched, and on TR 2018 it reaches 13 distinct values at
a 3.8% round share. So the coarseness is not a property of the elicitation
format, and it is also not intrinsic to the task. It is a property of `gpt-4o`.

That is recorded here, before the arms run, because it changes what a pass or a
failure below can mean. The question is no longer *can this instrument
discriminate on an ambiguous window* — something already does. It is **whether
the elicitation can buy `gpt-4o` that resolution**, which is worth $8 to learn
because `docs/deferred.md` §11 declined the `gpt-4.1` migration at ~$747.

**3. Components-then-composite is answerable from stored rows, and fails.** The
third variant §12 proposed — have the model emit only the four ledgers and let
the pipeline compute the composite — needs no arm, because every arm row already
carries the four ledgers. On A′:

| A′ composite, computed from the ledgers | distinct | round share | ρ vs A′'s judged score, TR 2018 |
|---|---|---|---|
| mean of four | 17 | 1.9% | **0.533** |
| mean, `edge_vitality` inverted | 16 | 1.9% | **0.511** |
| mean of the three risk-bearing ledgers | 16 | 5.8% | **0.527** |

It fails (c) under every plausible definition of the composite, and its apparent
pass on (a) is mechanical exactly as feared: the individual US ledgers are
*coarser* than the composite they would replace — 2, 4, 3 and 8 distinct values,
three of them on a multiple of five at 100% of anchors. Averaging four coarse
grids that sit at different offsets manufactures intermediate values; it does
not add judgement. **It is not run.** Building it would also require inventing a
ledger weighting this project has deliberately never defined, and relaxing the
tripwire that keeps `score_12m` the only assignment to a score.

## The arms

Payload byte-identical to p2 in all of them, asserted by
`test_a_prompt_arm_carries_no_evidence_the_others_lack` rather than assumed.

- **A′** — the stored reference, both windows. **$0.**
- **V1 `within-band`** — name the band, state where inside it and why, then emit
  the number. The reasoning: on US 2019 `gpt-4o` puts 52 of 52 anchors in
  `Moderate`, so the band is the decision it is actually making and the digit is
  an afterthought. Forcing the placement to be a second decision with its own
  stated reason may recover resolution. ~$3.90.
- **V2 `vs-typical`** — describe this country's ordinary week from the evidence
  already in the payload, then score this week's departure from it. The
  elicitation used where absolute magnitude judgements are unreliable, and the
  closest of the three to a reading that says improving or decaying rather than
  restating a level. ~$3.95.
- **The crossed cell** — whichever of V1/V2 scores better, re-run under
  `gpt-4.1`. ~$3.10. Three cells and one comparison: **scorer alone** (free, on
  disk), **prompt alone**, **both**. It is the only candidate in the harness
  that moves two axes, and it is readable only because the other two corners
  exist, which the one-axis test now requires it to name.

In both variants `score_12m` remains the model's own number. The added fields
are elicitation scaffolding and diagnostics; nothing computes a score from
`delta_vs_typical`.

## Criteria

Baselined on **A′**, both windows.

| # | Criterion | The line |
|---|---|---|
| **(a)** | **Discrimination**, US 2019 | distinct composite values **≥ 15** (A′: 8) **and** round-number share **≤ 44%** (A′: 76.9%) |
| **(b)** | **Determinate window not damaged** | TR 2018 distinct values **≥ 9** (A′: 9; arm C reached 12) |
| **(c)** | **Not a rewrite** | Spearman ρ against A′ on TR 2018 **≥ 0.65** |
| **(d)** | **No lag** | on TR 2018, first move **≥ 0.05** above the Q1 baseline **no later** than A′ (2018-02-05) |
| **(e)** | **Cost** | per-snapshot delta **≤ ~15%** against A′ |
| **(f)** | **Report only** | lag-1 autocorrelation, run-length distribution, band distribution, every arm both windows. No threshold |

**Decision rule: adopt a variant iff (a), (b), (c) and (d) all hold. If more
than one qualifies, prefer the simplest.**

- **The letters are not comparable across attempts.** Attempt 2's (b) was the
  lag test and its (d) was the `bullet_summary` diagnostic; here (b) is the
  determinate window and (d) is the lag. Same instrument, renumbered, and saying
  so is cheaper than a reader assuming a continuity that is not there.
- **(a) is unchanged since attempt 1**, thresholds and derivation included —
  ≥15 against nine, and 44% as the midpoint of the 69.2 → 18.9 gap between what
  this model does on an ambiguous window and what it does on a determinate one.
  This is the third attempt at the same test and the line has never moved.
- **(b) is new, and it is the lesson of arm C.** C bought twelve distinct values
  on TR while making US worse. A variant that buys ambiguous-window resolution
  by wrecking the determinate window is not an improvement, and attempt 2 had no
  criterion that would have caught it.
- **(d) had never existed as code** until this attempt. Both previous attempts
  computed it by hand from the committed arm files and kept only the verdict. It
  is now `bakeoff.first_move`, and it reproduces all four published TR 2018
  readings.
- **(e) will move against these arms, and that is expected.** V1 and V2 both add
  output tokens by construction. 15% is the same line as before.

### Stated before the run, so that it cannot be claimed afterwards

**`gpt-4.1` on the base prompt already passes (a), (b) and (c), and fails (d) by
one week** — first move 2018-02-12 against A′'s 2018-02-05. That failure is an
artifact of the criterion rather than a finding about the model: `gpt-4.1`'s Q1
baseline is 0.829 against A′'s 0.740, which makes a +0.05 move a longer reach
from a higher floor, and its longest identical run is 2 against A′'s 5 — the
opposite of a sticky series. It is reported as a criterion artifact. It is not
waived, and the criterion is not rewritten now that its behaviour on a known
result is visible.

## What is deliberately not a criterion

**Absolute score level.** V2 in particular may shift the whole series, because a
score reached through a stated departure from a baseline need not sit where an
abstract judgement sat. That is recalibratable; reordering is not, which is why
(c) is a rank correlation and there is no level test.

**Agreement with A′ on US 2019.** If the elicitation works, the arms *should*
disagree there — that window is the one A′ is least informative about. Demanding
agreement on the window the change exists to fix would be a criterion only a
no-op could pass.

**Whether the finer output is the more correct output.** It is the question that
decides everything downstream, and nothing here answers it. Distinct values and
round-number share measure whether an instrument *resolves*, never whether it
resolves onto anything real, and a model spreading noise across thirty buckets
would score better on (a) than one that is coarse and right. This attempt
reports an indicative check on TR 2018 and nothing stronger; the event study
that would settle it is Phase E work and is recorded as such.
