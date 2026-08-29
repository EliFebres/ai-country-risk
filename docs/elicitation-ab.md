# The instrument: four experiments, and what actually moved it

Six interventions across four sessions tried to make this scorer discriminate on
an ambiguous window. Three changed the evidence, three changed the question. All
six failed the same criterion. The seventh thing tried — changing the model,
holding evidence and question byte-identical — cleared it on the first attempt
and produced an answer nobody wants.

This document is the whole arc in one place, so that the next person does not
re-derive it. The pre-registered criteria and per-arm verdicts live in
`docs/payload-ab.md`; the scorer measurements in `docs/scorer-bakeoff.md`. This
is what they add up to.

---

## The short version

1. **Elicitation is not the constraint.** Two prompt variants that changed what
   the model was asked to decide, and in what order, failed the same way the
   payload changes did. One of them made the instrument worse than any payload
   ever did.
2. **The scorer is the constraint.** `gpt-4.1`, on a byte-identical prompt and
   payload, goes from 8 distinct values to 18 and from a 76.9% round-number
   share to 5.8%.
3. **The two do not add up. They subtract.** Elicitation on top of `gpt-4.1`
   *loses* six distinct values against `gpt-4.1` alone.
4. **And the extra resolution does not obviously track anything real.** On the
   one window containing a large unambiguous crisis, every `gpt-4o` arm rises
   into it and both `gpt-4.1` cells drift down through it.

So the coarseness is a property of `gpt-4o`, the cure is a different model, and
there is not yet evidence that the cure is an improvement. That is a worse
position than "elicitation fixes it" and a better one than the four sessions of
payload work that preceded it, because it is finally pointed at the right
variable.

---

## Every arm, on one page

Costs are **cache-neutral** — the tokens each arm sent, priced at list, rather
than what it happened to be billed. See *The cost criterion was measuring the
cache*, below; three previously published cost verdicts are corrected here.

### US 2019 — the ambiguous window

| arm | what moved | distinct | round share | lag-1 | run | ρ vs A′ | $/snap | Δ |
|---|---|---|---|---|---|---|---|---|
| p2, as it stood | — | 9 | 69.2% | 0.299 | 5 | 0.582 | n/a | — |
| p3-context | payload | 7 | 75.0% | 0.142 | 5 | 0.681 | $0.0404 | +1.9% |
| **A′** | payload (vintage fix) | 8 | 76.9% | 0.433 | 4 | ref | $0.0397 | ref |
| C — trend-prompt | prompt | 8 | 82.7% | 0.235 | 3 | 0.790 | $0.0401 | +1.0% |
| B — trend block | payload | 7 | **90.4%** | 0.243 | 3 | 0.618 | $0.0451 | +13.8% |
| **V1 — within-band** | prompt | 8 | **67.3%** | 0.477 | 2 | 0.698 | $0.0399 | +0.6% |
| **V2 — vs-typical** | prompt | **6** | 90.4% | 0.188 | **16** | 0.338 | $0.0405 | +2.0% |
| `gpt-4.1` | **scorer** | **18** | **5.8%** | 0.338 | 2 | 0.494 | $0.0309 | −22.2% |
| `gpt-4.1` × V1 | scorer + prompt | 12 | 0.0% | 0.217 | 3 | 0.287 | $0.0331 | −16.5% |

Every `gpt-4o` row above puts **all 52 anchors in `Moderate`**. `gpt-4.1` uses
three bands.

### TR 2018 — the determinate window

| arm | distinct | round share | lag-1 | run | ρ vs A′ | first move | $/snap | Δ |
|---|---|---|---|---|---|---|---|---|
| p2, as it stood | 9 | 18.9% | 0.564 | 7 | 0.808 | 2018-02-05 | n/a | — |
| p3-context | 7 | 18.9% | 0.493 | 8 | 0.696 | 2018-02-05 | $0.0524 | **+37.1%** |
| **A′** | 9 | 26.4% | 0.642 | 5 | ref | 2018-02-05 | $0.0382 | ref |
| C — trend-prompt | **12** | 26.4% | 0.670 | 6 | 0.833 | 2018-01-22 | $0.0388 | +1.5% |
| B — trend block | 10 | 24.5% | 0.556 | 5 | 0.788 | 2018-01-22 | $0.0439 | +14.9% |
| **V1 — within-band** | 10 | 39.6% | 0.516 | 3 | 0.709 | 2018-03-05 | $0.0388 | +1.4% |
| **V2 — vs-typical** | 9 | 50.9% | 0.456 | 5 | 0.652 | 2018-04-02 | $0.0390 | +2.1% |
| `gpt-4.1` | 13 | 3.8% | 0.738 | 2 | 0.703 | 2018-02-12 | $0.0299 | −21.7% |
| `gpt-4.1` × V1 | **15** | 18.9% | 0.630 | 5 | 0.568 | 2018-02-05 | $0.0317 | −17.0% |

**Verdicts against the criteria pre-registered in `docs/payload-ab.md`**
(adopt iff (a), (b), (c) and (d) hold):

| arm | (a) | (b) | (c) | (d) | (e) | adopted |
|---|---|---|---|---|---|---|
| V1 within-band | FAIL 8 / 67.3% | PASS 10 | PASS 0.709 | FAIL 03-05 | PASS +1.4% | no |
| V2 vs-typical | FAIL 6 / 90.4% | PASS 9 | PASS 0.652 | FAIL 04-02 | PASS +2.1% | no |
| `gpt-4.1` × V1 | FAIL 12 / 0.0% | PASS 15 | FAIL 0.568 | PASS 02-05 | PASS −17.0% | no |
| *(`gpt-4.1` alone)* | *PASS 18 / 5.8%* | *PASS 13* | *PASS 0.703* | *FAIL 02-12* | *PASS −21.7%* | *not an arm of this attempt* |

**No variant is adopted.** Both elicitation variants stay in the tree behind
`PROMPT_VARIANT`, unset by default.

---

## V1: the model obeyed, and it bought nothing

This is the finding, and it is worth more than the verdict.

V1 required the model to name a band, say where inside that band the week sits
and why, and only then emit the number — enforced through schema order, so the
band is generated before either horizon exists rather than rationalised
afterwards. **It complied on all 52 US anchors and all 53 TR anchors.**

It also complied *coherently*. Measured as position within the band the model
itself named, where 0.0 is the band floor and 1.0 the ceiling:

| placement | US 2019 | TR 2018 |
|---|---|---|
| `lower-middle` | 0.38 mean (n=35) | 0.44 mean (n=26) |
| `middle` | 0.57 mean (n=9) | 0.60 mean (n=4) |
| `upper-middle` | 0.88 mean (n=8) | 0.88 mean (n=22) |

Monotone, well separated, and only 2 of 52 US rows land outside the band they
named. The instruction was followed as written.

And the instrument did not resolve. Distinct values stayed at **8**. All 52
anchors stayed in `Moderate`.

The reason is visible in the same table: across 52 weeks the model used **three
placement buckets inside one band**. Asked to decompose one coarse judgement
into two decisions, it produced two coarse decisions. Three buckets is what
eight distinct values looks like from the inside.

Two things did move, and they are the first honest gains in six interventions:
the round-number share **fell from 76.9% to 67.3%** — every payload change had
pushed it up — and the longest identical run halved from 4 to 2. So the
elicitation reached the *snapping*, which is a real instruction-following
effect, without reaching the *resolution* underneath it. It also cost a month of
lag on TR: first move 2018-03-05 against A′'s 2018-02-05, which is what
committing to a band before looking at the number does to a series in motion.

## V2: the worst arm this project has measured

V2 asked the model to describe the country's ordinary week and then score the
departure from it. Across **105 anchors, `delta_vs_typical` took two values: 5
and 10.** Never zero, never negative, nothing else. US collapsed to six distinct
values with a run of **sixteen consecutive identical weeks**, and TR's crisis
response nearly vanished — 48 of 53 anchors in `Moderate` against A′'s 30.

The comparative anchoring did not fail because the model ignored it. It failed
because "how far is this from ordinary" is a judgement of exactly the same kind
as "how risky is this", made by the same model on the same evidence, and it came
out coarser. Some of that may be the rule's own wording — it says most weeks are
ordinary, which a model can read as permission to answer "ordinary" — and a
rewrite might recover some range. It would still be the third attempt to solve a
resolution problem by rephrasing the request.

---

## The organising finding: determinate versus ambiguous, and who it applies to

The project's diagnosis, stated in `deferred.md` §12, is that **an instruction is
followed where the evidence is determinate and ignored where it is not.** It
rests on two independent measurements, and both hold for `gpt-4o`:

- **The "never round to multiples of 5" rule.** 18.9% compliance-violation on TR
  2018 (crisis, determinate), 69.2% on US 2019 (ordinary year), 84.6% on PT 2019
  (quiet country, most ambiguous). Monotone in determinacy, same model, same
  instruction, 20% chance floor.
- **The trend instruction.** Arm C reaches 12 distinct values on TR — the highest
  figure this project has measured on `gpt-4o` — and does nothing on US.

V1 is now a third measurement of the same shape: 10 distinct on TR against A′'s
9, and 8 on US against A′'s 8.

**But it is a property of `gpt-4o`, not a law about models under ambiguity.**
Six scorers on the identical prompt:

| scorer | US 2019 round share | TR 2018 | gap |
|---|---|---|---|
| `gpt-4o` | 69.2% | 18.9% | **50.3 pts** |
| `gpt-4.1-nano` | 88.5% | 96.2% | −7.7 (reversed) |
| `gpt-4.1-mini` | 92.3% | 94.3% | −2.0 (reversed) |
| `gpt-4.1` | 5.8% | 3.8% | 2.0 |
| `gpt-5.4-mini` | 23.1% | 15.1% | 8.0 |
| `gpt-5.6-luna` | 21.2% | 7.5% | 13.7 |

The small models round almost always regardless of determinacy; `gpt-4.1` almost
never does. **Only the incumbent shows the large window-dependent gap the whole
§12 thesis is built on**, and two models show it reversed. The finding is real
and it is about this scorer. Generalising it to "models retreat to round numbers
when evidence is ambiguous" is not supported by the six rows above.

---

## The 2×2: the scorer does all the work, and the prompt subtracts

The confirmation cell exists to separate three explanations. All four corners,
US 2019:

| | base prompt | V1 within-band |
|---|---|---|
| **`gpt-4o`** | 8 distinct, 76.9% round, 1 band | 8 distinct, 67.3% round, 1 band |
| **`gpt-4.1`** | **18 distinct, 5.8% round, 3 bands** | 12 distinct, 0.0% round, 2 bands |

- **Prompt alone** buys no distinct values on either scorer.
- **Scorer alone** buys ten.
- **Both together** give back six of them, and drop ρ against A′ to 0.568,
  failing (c) — the only cell of the four to do so.

On TR the interaction is mildly positive (13 → 15 distinct), but the US column
is the one the whole exercise was about. The elicitation instruction constrains
a model that did not need constraining: told to commit to a band first, `gpt-4.1`
narrows from three bands to two and from eighteen values to twelve.

**Answer to the question the cell was bought to settle: the scorer explains
essentially all of it, and elicitation on top is a cost rather than an
addition.**

---

## Does the finer output track anything real?

This decides whether a migration buys signal or decoration, and **nothing here
settles it.** What follows is an indicative check on stored rows, not an event
study. It is reported because its direction is not the convenient one.

**The level response to a known crisis.** TR 2018 contains a large, unambiguous
deterioration — the lira crisis of August–September.

| cell | Jan–Feb mean | Aug–Sep mean | move into the crisis |
|---|---|---|---|
| A′ (`gpt-4o`) | 0.712 | 0.790 | **+0.078** |
| V1 (`gpt-4o`) | 0.736 | 0.786 | +0.051 |
| V2 (`gpt-4o`) | 0.666 | 0.713 | +0.047 |
| `gpt-4.1` | 0.811 | 0.792 | **−0.019** |
| `gpt-4.1` × V1 | 0.747 | 0.732 | **−0.014** |

**Every `gpt-4o` cell rises into the crisis. Both `gpt-4.1` cells drift down
through it.** `gpt-4.1` opens the year already at 0.811 — above where the
incumbent ends up at the crisis peak — and never distinguishes August from
January.

**Week-to-week responsiveness.** Neither model's movement correlates with
evidence movement: ρ between |Δscore| and |Δarticle count| is −0.068 for A′ and
−0.114 for `gpt-4.1`, and for both, the mean |Δscore| in weeks where a condition
flag flipped is *lower* than in weeks where none did. A′'s largest weekly move
lands on the week of 2018-08-13. `gpt-4.1`'s five largest land in January, April
and late December, none within ten days of a named 2018 event.

**How much weight this carries.** Not much on its own. One country, one crisis,
one year. Article count is a crude proxy for evidence movement and condition
flags are the model's own output, so the near-zero correlations may indict the
proxy rather than the models. Both peak in Q1, which no reading of 2018 explains
and which suggests something in the Q1 payload is doing work nobody has looked
at.

But the check was chosen before the numbers were seen, it is the only
correctness evidence in existence, and it points away from the finer model. It
is not a reason to reject `gpt-4.1`. It is a reason not to spend $747 until the
event study exists.

---

## The cost criterion was measuring the cache

Criterion (e) has been computed from realised spend in all three attempts.
Realised spend depends on the provider's prompt cache, and the cache depends on
what ran immediately before — so an arm scored straight after a similar arm on
the same anchors is flattered by an effect that has nothing to do with the arm.

V2 made this unmissable: it ran directly after V1 on identical anchors, hit a
**90.8% cache share against A′'s 3.9%**, and reported **−36% per snapshot while
sending more tokens than A′ in both directions.** On tokens it is +2%.

`bakeoff.cache_neutral_per_snapshot` now prices the tokens each arm sent, at
list, so run order cannot move the number. It corrects three published verdicts:

| arm | published | cache-neutral | consequence |
|---|---|---|---|
| B — trend block, TR | +17.0%, **(e) FAIL** | **+14.9%** | inside the line; B was rejected on (a) regardless, so the decision stands and the recorded reason does not |
| C — trend-prompt, TR | −10.2% (cheaper) | **+1.5%** (dearer) | the "cheap half of the trend question" was not cheaper |
| p3-context, TR | −3.9% (cheaper) | **+37.1%** (dearer) | it made 3.53 calls per snapshot against A′'s 1.15 — the context-building calls are real work and were largely cached away at measurement time |

None of the three reverses a rejection. All three were reported as facts.

---

## What this costs and what it cost

Attempt 3 spent **$9.90** across five scoring runs, the schema gates and the
jitter probe, against a projection of $11.12 — under, because V2's cache hit is
real money even though it is not a real cost comparison. The seven arm files in
`backend/bakeoff/` now represent **$25.72** of scoring across four attempts, and
none of it has been adopted.

That is the correct outcome for six of the seven. The one it argues for was
never on the list: the axis nobody varied for four sessions was the model, and
it was sitting measured on disk the whole time, in `docs/scorer-bakeoff.md`,
rejected on cost and rank agreement without discrimination ever being weighed.

---

## What follows

**For the scorer choice** — `deferred.md` §11, rewritten. It is now a genuine
fork rather than a closed decision, and the blocker is not cost. It is that no
evidence yet shows the finer output is the more correct output.

**For the modelling phase** — Phase C inherits a measurement problem this
document does not solve. A series with nine distinct values across fifty-two
weeks, a third of weeks identical to their neighbour, and every anchor in one
band has an **effective sample size far below its row count**. Fifty-two rows
that take nine values and change in 65% of weeks are not fifty-two independent
observations of anything.

That argues for predicting rating *changes* rather than levels, and for carrying
an uncertainty band wide enough to admit what the instrument cannot resolve.
**It is not decided here.** It is the question Phase C inherits, and it should be
decided against whichever scorer is chosen, because the two candidates differ by
a factor of two in exactly the quantity that decides it.

**For sequencing** — the scorer choice now has to settle *before* the local-model
screen. Payload and prompt were already required to be final first, for the
obvious reason that a candidate must be measured against a fixed instrument. The
scorer is now on that list too: whichever model is chosen defines the bar, and
`gpt-4o` and `gpt-4.1` set that bar 10 distinct values apart.
