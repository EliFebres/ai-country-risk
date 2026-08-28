# The payload: does trailing context restore discrimination?

Whether the pilot runs on **p2** (30-day window only) or **p3-context** (plus four
quarters of masked history). The scorer is settled at `gpt-4o`; this is the other
half of the instrument.

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
