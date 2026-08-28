# Which scorer

Whether a cheaper model can replace `gpt-4o-2024-08-06` as the pilot's scorer,
measured rather than argued. Three rounds, 2026-08-27.

The pilot's claim is that every row in a ten-year weekly series was produced by
one scorer under one prompt. So changing the scorer is not procurement that
happens to touch code — it is an **instrument change**, and the only honest way
to make one is to re-run a fixed set of anchors through both and look at what
moved. Price is read last, and only by candidates that got that far.

**Answer: stay on `gpt-4o`.** Nothing is switched.

**Two findings, and the second is the one that decides it.**

1. Of every model tested, **only the incumbent reproduces its own scored output**
   at `temperature=0`, `seed=42` — and strict grammar enforcement turns out to be
   necessary but not sufficient to explain why.
2. **Models agree when the evidence is determinate and scatter when it is not.**
   On an ordinary year for a stable country the candidates disagree with the
   incumbent by **1.3–2.1× the series' own variation**; on a crisis year that
   falls below 1.0 and the best candidate reaches ρ = 0.708. Tested on two
   windows, because one window could not tell the two apart.

The second is the more important finding and it is **not** a fact about cheap
models. It bounds what the weekly series can claim: it is most reproducible where
it is least informative, and least reproducible on the quiet weeks where a risk
rating would earn its keep. That belongs in the pilot's validation work, and it is
why the procurement question and the instrument question are answered separately
below.

---

## The two hard gates

1. **Strict schema.** The real `AI_PROMPT_V3` and the real `RISK_SCHEMA_V3` — 8
   required fields, nested objects, `additionalProperties: false` throughout —
   through `.with_structured_output(schema=..., strict=True)`, the production
   wrapper. A three-field toy schema says nothing about this one.
2. **Determinism.** Repeats at `temperature=0`, `seed=42`, compared on **scored
   fields**. See *The gate that failed the reference* below for why that
   qualifier is load-bearing.

`backend/util/tools/bakeoff.py` runs both, and reports failure rather than
routing around it.

---

## Round 1 — the third-party candidates, on price

MiniMax M3 and DeepSeek V4 were chosen on rate: MiniMax at $0.30/$1.20 per 1M
against gpt-4o's $2.50/$10.00, roughly an eighth. Both failed gate 1 outright.

| candidate | strict `json_schema` | why |
|---|---|---|
| **gpt-4o** *(control)* | **PASS** | — |
| minimax-m3 | FAIL | rejects `"type": ["integer","null"]` — wants a string, not an array |
| deepseek-v4-pro | FAIL | `"This response_format type is unavailable now"` |
| deepseek-v4-flash | FAIL | same |

The incumbent was smoked as a **control** deliberately: without it, "all three
failed" is indistinguishable from a broken harness. Total spend $0.02, all of it
the control — the candidates were rejected at HTTP 400 before a token billed.

`/v1` and `/beta` behave identically on DeepSeek, and every model id was verified
against each vendor's own `/models` listing rather than assumed.

---

## Round 2 — the same candidates, on a measured gate

A binary "must hold strict schema" is the wrong instrument if a candidate can
produce valid output reliably by another route. So the gate was replaced with a
measurement: run it, and report the **failure and retry rate**. Zero retries is
as good as strict mode; a few percent is a cost note, not an automatic out.

Both vendors serve `json_object`, so each ran 10 repeats of one input with local
validation against the real `RISK_SCHEMA_V3`.

| candidate | invalid output | scored-determinism | `score_12m` range | cache |
|---|---|---|---|---|
| **gpt-4o** *(strict)* | 0% | **exact, 9/9** | 50 | 64% |
| deepseek-v4-pro | **0%** | no — 8 distinct/10 | 48–55 (spread 7) | 98% |
| deepseek-v4-flash | **0%** | no — 4 distinct/10 | 55 ×10 | 98% |
| minimax-m3 | **40%** | no — 6 distinct/6 | 37–47 (spread 10) | 100% |

DeepSeek cleared the validity bar outright. **Neither vendor cleared
determinism.**

### The portable schema, and why it was abandoned

Only four nodes in `RISK_SCHEMA_V3` use a union type, all of them ledger scores:
`["integer", "null"]`. Rewriting those as `anyOf: [{integer}, {null}]` is
mechanically equivalent JSON Schema, and MiniMax accepts it (it also requires a
user message; system-only returns *"chat content is empty"*).

It was tested on **gpt-4o first**, before any candidate was scored through it —
because a variant that changes the instrument measures the variant. Nine samples
per configuration:

| configuration | `score_12m` | stable? |
|---|---|---|
| `v3` + system *(production)* | 50 ×9 | **yes** |
| `portable` + system | 52 ×7, 50 ×2 | **no** |
| `v3` + human | 52 ×9 | yes, but shifted |

The variant does not merely shift the level — it **destroys determinism on the
reference model**. So it cannot be the measurement instrument, and no candidate
was scored through it. Nullability is load-bearing rather than decorative: the
reference genuinely returns `edge_vitality: null`.

Weakening the grammar constraint made a deterministic model non-deterministic.
That is a real result and it survives — but it is only half the story, and
round 3 supplies the other half. See *What we cannot explain* below.

### Thinking mode bills as output

MiniMax M3 reasons by default and returns the reasoning **in message content**,
so an unpinned run pollutes the payload as well as the bill: 15 of 17 output
tokens on a one-word question. It is suppressible —
`{"thinking": {"type": "disabled"}}` or `reasoning_effort: none` — and
suppression took a full scoring call from 55s to 3s.

It was smoked once unpinned before this was noticed. The test that was supposed
to catch it looped a hardcoded pair of DeepSeek names, so a third thinking model
could be added beside it and inherit nothing. It now asserts the rule for the
family.

### Why the third-party candidates were dropped

`gpt-4.1-mini` costs **$0.0069** a snapshot against DeepSeek V4 Pro's **$0.0104**
at its *off-peak half-rate* — with strict schema intact and no time-of-day
scheduling. Beaten on both axes at once, they were removed from the run list
rather than left to be run by accident. Their prices remain in
`llm/usage.py`, which is a price table and not a run list.

---

## The gate that failed the reference

The determinism gate originally compared whole canonicalised payloads, and
**gpt-4o does not pass that**. Over six repeats of one prompt at `temperature=0`,
`seed=42`, the incumbent returns identical `score_12m`, `score_3m`,
`ledger_scores`, `condition_flags`, `evidence_coverage` and `news_article_scores`
every time — and varies two fields:

| field | behaviour | why it is not gated |
|---|---|---|
| `bullet_summary` | reworded on 6 of 6 repeats | free prose. Displayed, never scored; nothing ranks on it, no ledger derives from it, `compare` never reads it |
| `subscore_evidence` | 2 distinct of 6 | *which* evidence item is cited for a ledger, not what the ledger scored. Observed alternating `a1` / `structural` for `information_capacity` while that ledger's value never moved |

A gate the reference cannot pass disqualifies every candidate for a defect the
reference shares — which is how a bake-off ends with no candidates and no
finding. The verdict now reads `scored_match_rate`, over everything except those
two fields; the whole-payload rate is still reported beside it, because a
candidate churning prose far harder than the incumbent is worth seeing even
though it is not disqualifying.

The reproducibility claim is untouched. It is made about the stored row and the
manifest that hashes what the model read, and every field either depends on is
byte-identical across repeats.

---

## Determinism is a property of the serving layer, not the model

The substantive result of all three rounds, and the one that decides the
recommendation.

### What was measured

**Identical input, `temperature=0`, `seed=42`, ten repeats.** The same payload
sent ten times, and the answers compared against each other.

This is **repeat-stability on one payload — not variation across different
weeks.** The distinction is what makes the numbers mean anything. A scorer is
*supposed* to return different numbers for different weeks; that is the signal.
Returning different numbers for the *same* week is the instrument moving under
the measurement, and there is no reading of it that is useful.

Compared on **scored fields** — every field except `bullet_summary` and
`subscore_evidence`, for the reasons in *The gate that failed the reference*.

### The results, across both rounds

| model | route | valid | distinct scored payloads | `score_12m` seen |
|---|---|---|---|---|
| **gpt-4o** | strict | 10/10 | **1 — exact, 9/9 and 10/10** | 50 |
| deepseek-v4-pro | `json_object` | 10/10 | 8 of 10 | 48–55 |
| deepseek-v4-flash | `json_object` | 10/10 | 4 of 10 | 55 ×10 (composite stable) |
| minimax-m3 | `json_object` | **6/10** | 6 of 6 | 37–47 |
| gpt-4.1 | strict | 10/10 | 3–5 | 43–45 |
| gpt-4.1-nano | strict | 10/10 | 6 | 55–60 |
| gpt-4.1-mini | strict | 10/10 | 6–8 | 55–70 |
| gpt-5.4-mini | strict | 10/10 | 10 | 35–47 |
| gpt-5.6-luna | strict | 10/10 | 9 | 38–53 |

**Only `gpt-4o` reproduces its own scored output.** Every other model tested
varies — including every OpenAI candidate, on the identical schema through the
identical wrapper.

### The control that identifies the mechanism

The obvious reading of round 2 — *the other vendors are sloppy* — is wrong, and
one control rules it out.

`gpt-4o` was made non-deterministic on demand. Rewriting the four union types in
`RISK_SCHEMA_V3` as `anyOf` — mechanically equivalent JSON Schema, same meaning —
produced this:

| configuration | `score_12m` over 9 samples |
|---|---|
| `v3` + system *(production)* | **50 ×9** |
| `anyOf` variant + system | **52 ×7, 50 ×2** |

Same model, same provider, same temperature, same seed, same prompt. **Only the
grammar changed.** So the variation is not a property of who serves the model.

### Why constrained decoding does this

At each step the model produces a probability distribution over next tokens. At
`temperature=0` it takes the highest-probability one — the argmax. That sounds
exactly reproducible and is not: GPU floating-point arithmetic is not
associative, so the order in which values are summed depends on batch size and
kernel scheduling, which depend on what else is running on the machine. Two runs
can compute very slightly different probabilities for the same token.

Almost always that is irrelevant. It matters when two tokens are near-tied: a
difference in the twelfth decimal place flips which one wins, and because each
token conditions everything after it, one flipped token diverges the whole rest
of the answer. A single near-tie early on is enough to change a score.

Strict schema enforcement compiles the schema into a grammar and **masks every
token that would make the output invalid**. After `"score_12m":` only digits are
legal; the prose tokens that would otherwise be near-tied are removed from
contention entirely. Often exactly one token is legal, and then there is nothing
to flip. The grammar does not make the arithmetic deterministic — it removes the
opportunities for non-determinism to express itself.

### The honest limit

**Grammar is necessary but not sufficient, and this is where the tidy story
breaks.**

`gpt-4.1` holds the *identical* `RISK_SCHEMA_V3`, through the *identical*
`.with_structured_output(strict=True)` wrapper, at the same `temperature=0`,
`seed=42` — and varies anyway. So do `gpt-4.1-mini`, `gpt-4.1-nano`,
`gpt-5.4-mini` and `gpt-5.6-luna`. Grammar cannot explain something that models
sharing that grammar do not share.

**So something further about how `gpt-4o` specifically is served also matters,
and we have not identified it.** What would need ruling out, none of it tested
here:

- how each model honours `seed` — many endpoints accept it and ignore it;
- batching, routing or fleet heterogeneity behind a single endpoint;
- whether the constrained-decoding implementation differs by model generation;
- whether a lower-entropy model simply survives the same numerical noise that a
  flatter one does not.

Two things follow that cut against the convenient reading. **Do not generalise
to "newer models are less deterministic"** — this is one prompt on one anchor's
evidence, a strong repeated observation about these models on this workload, not
a law. And **do not assume `gpt-4o`'s determinism is permanent**: if it is a
property of how the model is served rather than of anything we control, it can
change with no change on our side. See the determinism canary in
`docs/deferred.md`.

### What follows for this project

**Determinism cannot be shopped for.** It is not a feature with a price, and no
amount of paying more buys it — `gpt-4.1` costs twenty times `gpt-4.1-nano` and
is also non-deterministic. There is one model in the tested set that has it.

Three places it is load-bearing:

1. **The byte-for-byte rebuild check.** `rebuild_snapshot` re-derives a stored
   row from its manifest and compares. Against a non-deterministic scorer the
   check cannot distinguish "the evidence changed" from "the model wobbled", and
   it stops being a check.
2. **A gate-2 repeat.** The baseline exists so the next run is a regression test
   rather than a fresh opinion. If the scorer varies by more than the effect
   being measured, the repeat measures noise.
3. **Resuming an aborted pilot.** A multi-day run that stops and resumes must not
   have the resumed portion drift against the part already stored, or the series
   is two instruments wearing one name.

Worth being precise about what is *not* on that list: **the pilot itself does not
need determinism directly.** All 2,092 snapshots are novel inputs scored exactly
once, so repeat-stability never arises during the run. It is the verification
around the pilot that needs it.

### Two adjacent findings

**Message role is part of the instrument.** The same prompt as a `SystemMessage`
and as a `HumanMessage` gives stably different answers — 50 ×9 against 52 ×9.
Both deterministic, and not the same instrument. Whatever else moves, that must
not.

**Nullability is load-bearing, not decorative.** The reference genuinely returns
`edge_vitality: null`, so a "portable" schema that forces an integer is not a
reformatting of the constraint — it changes what the model is allowed to say.

### The caveat that keeps the claim honest

**Prose is not deterministic even under strict schema.** Over repeats of one
prompt, `bullet_summary` was reworded on every single run, and
`subscore_evidence` cited a different item on 2 of 6 — while every scored field
stayed byte-identical.

So the claim is precisely: **scores and flags reproduce; narration does not.**
Anywhere the methodology is written up, that is the sentence, not "gpt-4o is
deterministic."

---

## Price buys speed, not agreement

The noise floor orders **almost perfectly inverse to price**. The cheaper the
model, the more it disagrees with itself on identical input.

### Measured on three anchors, not one

The first pass used a single canned payload, and it was wrong in both
directions — it understated `gpt-4.1-nano` by 4× and overstated `gpt-4.1-mini` by
nearly 2×. Repeat-stability depends on where on the scale the answer lands: a
payload whose score sits near a band edge amplifies jitter, and one anchor cannot
show that. So each candidate was measured on three, spanning three bands, ten
repeats each.

| model | $/snapshot | calm (Low) | baseline (Moderate) | stressed (Extreme) | **worst** |
|---|---|---|---|---|---|
| **gpt-4o** | $0.0430 | **0** | **0** | **0** | **0 pt** |
| **gpt-4.1** | $0.0344 | 1 | 1 | 1 | **1 pt** |
| gpt-5.4-mini | $0.0150 | 7 | 6 | 6 | 7 pt |
| gpt-4.1-mini | $0.0069 | 8 | 7 | 2 | 8 pt |
| gpt-5.6-luna | $0.0040 | 11 | 9 | 3 | 11 pt |
| gpt-4.1-nano | $0.0017 | **20** | 5 | 5 | **20 pt** |

`gpt-4.1-nano` is the clearest case for measuring more than one anchor. On the
calm payload it returned 30, 35, 38, 38, 40, 30, 50, 40, 30, 40 — a 20-point
swing on identical input, where `gpt-4o` answered 12 ten times out of ten. One
anchor would have reported it at 5 points and ranked it second-best.

Only `gpt-4o` and `gpt-4.1` are flat across the range. Every other candidate's
jitter varies by anchor, which means its noise floor is not one number and the
worst case is the one that matters.

### Two yardsticks

| yardstick | value | what it is |
|---|---|---|
| week-over-week move | **0.050** (5 pt) | median \|Δ\| between consecutive anchors, over the 52 US-2019 reference snapshots |
| PT masking divergence | **0.072** (7.2 pt) | masked − named, the effect masking exists to measure |

They agree to within a couple of points, from independent sources, which is
reassuring. The week-over-week figure is the one to prefer: it is **reproducible
from data in this repository**, recomputable whenever the reference moves, while
0.072 is carried from a prior measurement this repo cannot currently regenerate.

| model | worst spread | vs 5-pt weekly move | vs 7.2-pt masking signal |
|---|---|---|---|
| gpt-4o | 0 pt | **0%** | **0%** |
| gpt-4.1 | 1 pt | **20%** | **14%** |
| gpt-5.4-mini | 7 pt | 140% | 97% |
| gpt-4.1-mini | 8 pt | 160% | 111% |
| gpt-5.6-luna | 11 pt | 220% | 153% |
| gpt-4.1-nano | 20 pt | 400% | 278% |

Four of the five candidates have a noise floor **at or above the entire signal**.
A model that disagrees with itself by more than a typical week's movement cannot
be used to detect a typical week's movement: the finding would be smaller than
the instrument's own wobble, and a week-to-week change it reported could not be
distinguished from it answering twice.

### The general lesson

**The advertised saving is not the real saving.** A model that costs a twentieth
as much per call but wobbles by more than the effect you are measuring has not
made your series cheaper — it has made it noisier, and you pay the difference
back in repeats, in wider error bars, or in a finding you cannot defend.

Price per token is a procurement number. Price per *unit of usable signal* is the
real one, and it is only knowable after measuring a model against itself on
identical input — on more than one input, across the range of answers you expect.

That measurement is cheap and almost nobody does it. Thirty repeats per model
cost well under a dollar here and reordered the entire shortlist twice.

### A hypothesis, offered as a hypothesis

This is **not established**. It reconciles this section with the determinism
section above, and it predicts both, which is the most that can be said for it.

*Determinism needs the top token to be decisively ahead at every step.* Two
things widen that gap. **Grammar enforcement** deletes competitors from the
candidate set — after `"score_12m":` the prose tokens are simply gone. **Model
confidence** puts more probability mass on the leader, so the margin to second
place exceeds the floating-point noise.

That predicts both results. Weakening `gpt-4o`'s grammar broke its determinism by
letting competitors back into contention. Cheaper, smaller models wobble because
flatter output distributions make near-ties common, so the same numerical noise
flips more of them — and it predicts the anchor-dependence too, since a payload
whose evidence genuinely sits between two bands is one where the model is least
confident.

**What it does not establish.** No logits were inspected — this is inference from
behaviour. Three payloads were tested, all canned. And it does not explain the
thing that most needs explaining: why `gpt-4.1` varies where `gpt-4o` does not,
given identical grammar and a plausibly similar confidence profile. Something
about the serving path remains unaccounted for, and this hypothesis does not
reach it.

---

## Round 3 — the rank correlations

The reference: **`gpt-4o`, US 2019, 52 weekly anchors**, scored through the
production path at a cost of $2.09. Series range 0.420–0.700, sd 0.063, median
week-over-week move 0.050. All five candidates scored against it on identical
evidence — same digests, same masked payloads, scorer as the only variable.

### Spearman ρ against the reference

| model | composite | score_3m | friction | order_unc. | info_cap. | edge_vit. | $/snapshot |
|---|---|---|---|---|---|---|---|
| **gpt-4.1** | **0.377** | 0.433 | 0.383 | **0.571** | 0.521 | **−0.100** | $0.0298 |
| gpt-5.6-luna | 0.337 | 0.409 | 0.291 | 0.221 | 0.551 | — *(n=12)* | $0.0034 |
| gpt-4.1-mini | 0.300 | 0.303 | −0.120 | 0.492 | 0.316 | 0.278 | $0.0059 |
| gpt-4.1-nano | 0.240 | 0.286 | −0.228 | 0.296 | 0.121 | 0.069 | $0.0014 |
| gpt-5.4-mini | 0.100 | 0.142 | 0.165 | 0.405 | 0.189 | 0.251 | $0.0125 |

**Every one of these is low.** The best candidate agrees with the incumbent on
week ordering about as well as ρ = 0.38 describes — which is to say, it does not.
Two candidates have *negative* correlation on `friction`, and the best candidate
has negative correlation on `edge_vitality`: they order those weeks backwards.

### It is not jitter — this is the important part

The obvious defence of a low ρ is that the candidate's own noise floor caps it.
That defence does not survive the arithmetic.

If a candidate returns `truth + noise`, its correlation against the reference is
attenuated by roughly `1 / sqrt(1 + σ²noise / σ²signal)`. With the series sd at
6.31 points, and σ_noise estimated from each candidate's measured worst-case
spread over ten repeats:

| model | worst spread | σ_noise | **ρ ceiling from noise alone** | ρ observed | unexplained gap |
|---|---|---|---|---|---|
| gpt-4.1 | 1 pt | 0.32 | **0.999** | 0.377 | **0.62** |
| gpt-5.4-mini | 7 pt | 2.27 | 0.941 | 0.100 | **0.84** |
| gpt-4.1-mini | 8 pt | 2.60 | 0.925 | 0.300 | **0.62** |
| gpt-5.6-luna | 11 pt | 3.57 | 0.870 | 0.337 | **0.53** |
| gpt-4.1-nano | 20 pt | 6.50 | 0.697 | 0.240 | **0.46** |

Every ceiling sits far above every observed value. Even `gpt-4.1-nano`, the
noisiest model tested, could reach ρ ≈ 0.70 if it agreed with the incumbent about
which weeks were risky. It reaches 0.24.

**So the disagreement is judgement, not noise.** These models are not failing to
reproduce `gpt-4o`'s ranking because they are unsteady; they are steadily ranking
the weeks differently. That is a stronger finding than "they are noisy", and it
is the one that decides the question — noise can be averaged down, and a
different opinion cannot.

`gpt-4.1` is the clearest case. Its noise floor is negligible (ρ ceiling 0.999),
so essentially none of its 0.62 shortfall is attributable to jitter.

### And it is not a level offset either

A constant offset would be survivable — move the prompt's calibration anchors and
the whole series shifts with them. `gpt-4.1`'s **signed** mean shift is **−0.008**
and its **absolute** mean shift is **0.089**. Those two numbers together are the
finding: it is not scoring uniformly higher or lower, it is scoring *individual
weeks* differently in both directions, cancelling to near zero on average.

Band migration says the same thing. All 52 reference anchors sit in **Moderate**,
so the matrix degenerates to one row — the reference never leaves the band. Of
those 52, `gpt-4.1` moves 6 to Low-Moderate and 3 to High; `gpt-4.1-nano` moves 21
to High and 3 to Extreme. The diagonal is not an offset to recalibrate. It is
scatter.

**There is nothing here to recalibrate away.** A migration to `gpt-4.1` is not a
level shift plus a constant; it is a different set of opinions about which weeks
in 2019 were risky.

### Observation-only flags

`gpt-4.1` agrees with the incumbent on `war_on_territory` (1.000) and
`sovereign_stress` (1.000), and slips on `internal_conflict_level` (0.942) and
`emergency_rule` (0.962). `gpt-4.1-nano` is the outlier and the warning:
`sovereign_stress` agreement **0.462**, worse than a coin flip on a flag that is
false on nearly every US 2019 week. `internal_conflict_level` 0.615.

Lint fired on neither side for any candidate.

### Cost, for completeness — and it is last for a reason

Scorer-only, 52 anchors, standard rates. Batch halves each; digests are extra and
shared.

| model | $/snapshot | 2,092-snapshot pilot | 25,104-snapshot backfill |
|---|---|---|---|
| gpt-4o *(reference)* | $0.0430 | $89.94 | $1,079 |
| gpt-4.1 | $0.0298 | $62.27 | $747 |
| gpt-5.4-mini | $0.0125 | $26.07 | $313 |
| gpt-4.1-mini | $0.0059 | $12.43 | $149 |
| gpt-5.6-luna | $0.0034 | $7.14 | $86 |
| gpt-4.1-nano | $0.0014 | $2.93 | $35 |

Realised cache share was **4%** on the candidates and **0%** on `gpt-4.1`, far
below the 91–99% seen in the repeat tests. That is expected and worth stating:
the repeat tests sent one identical prompt over and over, which is the best case
for a prefix cache; a real run sends 52 different payloads, and only the constant
prompt prefix is reusable. **The cache-hit share measured on repeats is not the
one a backfill will get.**

---

## Is ρ ≈ 0.3 a fact about the candidates, or about the test window?

The round-3 correlations have **two readings, and the US window cannot separate
them.** This matters far more than the procurement decision that produced it, so
it gets tested rather than asserted.

**Reading A — the candidates disagree.** Cheaper models genuinely rank the weeks
differently from the incumbent, and a cheaper scorer buys a different series.

**Reading B — the window had no ordering to reproduce.** All 52 US-2019 anchors
sit in the **Moderate** band; the series runs 0.420–0.700 with a standard
deviation of 0.063 and a median week-to-week move of 0.050. When the true spread
between weeks is that narrow, most pairwise comparisons are decided by whatever
noise remains, and **Spearman ρ is depressed by construction** — not because the
models disagree about risk, but because there is barely an ordering to agree
about. A near-flat series is close to the worst case for rank correlation.

If B is right, ρ ≈ 0.3 says little about the candidates and something
uncomfortable about the ratings: that on an ordinary year for a stable country,
the week-to-week ordering the series reports may be largely unreproducible even
by a competent model. That is a validation finding, not a procurement one, and it
would belong in the pilot's methodology rather than here.

### The test

Re-run the **identical candidate set, prompt, digests and harness** on a
deliberately volatile country-year, changing only the window.

**Turkey 2018** — the lira crisis: a currency losing roughly a third of its value
inside a year, an emergency-rule transition, and a policy-rate response, so the
weeks genuinely differ. *(Chosen as the volatile window. It turned out not to be
more volatile week-to-week than US 2019 — see the correction below. The test
worked anyway, by a different mechanism.)* The corpus was harvested for this comparison and is
comparable in kind to US 2019 rather than thinner: **1,941 articles in the window,
1,494 of them with recovered bodies**, median **125 per 30-day anchor** against
US 2019's 20-article selection pool, and no empty anchors.

Holding evidence *type* constant was deliberate. Before the Guardian harvest, TR
2018 held 447 articles and **none with bodies**, so running it then would have
moved two variables at once — volatility *and* whether `rewrite_body` ever
fires — and left the result as ambiguous as the thing it was meant to settle.

**What each outcome means:**

| if TR 2018 gives… | then |
|---|---|
| ρ **rises sharply** (say ≥ 0.7) | Reading B. The flat window explained it. The candidates track the incumbent when there is something to track, and round 3's numbers are a floor produced by the test window, not a property of the models. The procurement conclusion survives on determinism and cost; the disagreement claim does not. |
| ρ **stays near 0.3** | Reading A, and worse than a procurement finding. Two competent models under one prompt on identical evidence do not reproduce each other's week ordering even when the weeks genuinely differ — which bounds what the weekly series can claim, and belongs in the validation work. |
| ρ rises **modestly** (0.4–0.6) | Both, partially. The flat window depressed the figure and real disagreement remains. The honest statement is a range, and the validation question stays open. |

Reference and candidates are scored exactly as in round 3 — `upsert=False` for
candidates, digests held on `gpt-4o-mini`, scorer the only variable — and filed
under `bakeoff/TR-2018/` so neither comparison overwrites the other.

---

### The answer: mostly Reading B, by a mechanism neither reading named

**Turkey 2018, identical candidate set, prompt, digests and harness. Only the
window changed.** Reference: `gpt-4o`, 53 anchors, $2.02.

| candidate | ρ on US 2019 | ρ on TR 2018 | change |
|---|---|---|---|
| **gpt-4.1** | 0.377 | **0.708** | **+0.331** |
| gpt-5.4-mini | 0.100 | 0.422 | +0.322 |
| gpt-4.1-nano | 0.240 | 0.418 | +0.178 |
| gpt-4.1-mini | 0.300 | 0.384 | +0.084 |
| gpt-5.6-luna | 0.337 | **0.084** | **−0.253** |

`gpt-4.1` clears the pre-registered ≥ 0.7 threshold — ρ **0.708**, τ-b 0.602, and
`score_3m` at **0.865**. **So ρ ≈ 0.3 was substantially an artifact of the test
window, and the unqualified claim "the models disagree" does not survive.**

### First, a correction to the test's own premise

TR 2018 was chosen as the *volatile* window and it is **not** more volatile than
US 2019 on the obvious measure. It is less:

| | US 2019 | TR 2018 |
|---|---|---|
| series sd | 0.0631 | **0.0551** |
| median week-over-week move | 0.0500 | **0.0200** |
| bands spanned | 1 (all Moderate) | 2 (39 Moderate, 14 High) |
| distinct values in 52–53 anchors | 9 | 9 |

So the prediction "a wide-range window will raise ρ" was right about the outcome
and wrong about the reason. Week-to-week movement did not increase. Something
else did.

### What actually moved: disagreement, not signal

Expressing each candidate's deviation from the reference as a standard deviation,
in the same units as the series, and dividing by the series' own spread:

| candidate | US 2019 disagreement sd | ÷ signal | TR 2018 disagreement sd | ÷ signal |
|---|---|---|---|---|
| **gpt-4.1** | 0.1081 | **1.71×** | **0.0456** | **0.83×** |
| gpt-4.1-nano | 0.1290 | 2.04× | 0.0581 | 1.05× |
| gpt-4.1-mini | 0.0852 | 1.35× | 0.0600 | 1.09× |
| gpt-5.4-mini | 0.1312 | 2.08× | 0.0530 | 0.96× |
| gpt-5.6-luna | 0.0806 | 1.28× | 0.0632 | 1.15× |

The signal did not grow — TR's is *smaller*. **The disagreement shrank**, by more
than half for `gpt-4.1` (0.108 → 0.046). On US 2019 every candidate disagreed with
the incumbent by more than the entire variation the series reports; on TR 2018
none does by much, and the best is comfortably below it.

That ratio, not ρ, is the mechanism. Rank correlation is what you observe when
disagreement exceeds signal; the ratio is why.

### The reading the evidence actually supports

Neither "the candidates disagree" nor "the window was flat" is quite right. What
the two windows show is:

> **Models agree when the evidence is determinate, and disagree when it is not —
> and the disagreement is measured against a signal that does not grow to meet
> it.**

TR 2018 is the lira crisis: a currency down roughly a third, an emergency-rule
transition, a policy-rate response. The right answer is *legible*, and five models
of very different sizes converge on it — TR's scores sit high and tight (0.600 to
0.820, 40% of anchors on one value). US 2019 is an ordinary year for a stable
country: no crisis, everything Moderate, and the correct score for any given week
is genuinely underdetermined. There the models scatter by 1.3–2.1× the signal.

`gpt-4o`'s own behaviour fits this. It emits **9 distinct values across 52 weeks**,
with 0.50 alone on a third of them, while `gpt-4.1` emits 18 — the incumbent
resolves ambiguity by snapping to round anchors, which is stable and is not the
same as being right.

### What this costs the ratings, and what it does not

**It does not undermine the procurement conclusion.** Determinism, cost and the
`gpt-4.1` migration price are unaffected; those were never measured through ρ.

**It does bound what the weekly series can claim, and the bound has an awkward
shape.** The series is most reproducible exactly where it is least informative — a
crisis any observer would call a crisis — and least reproducible on ordinary
weeks for stable countries, which is where a risk rating would earn its keep.
A week-to-week movement reported for a quiet country-year is, on this evidence,
substantially instrument rather than signal.

**This belongs in the pilot's validation work, not here.** Three things follow,
and none is a scorer decision:

1. **Report the series with an uncertainty band, not as a point estimate**, at
   least for low-volatility country-years. The band is measurable: it is the
   disagreement sd, ~0.10 on an ordinary year.
2. **Treat the masking divergence of 0.072 with corresponding caution.** It was
   measured on PT — a quiet country — and it is smaller than the between-model
   disagreement observed on a quiet country. That does not make it wrong, but it
   means it needs a second scorer to be confidently distinguished from instrument
   noise.
3. **The four ledgers are weaker than the composite.** `edge_vitality` is negative
   or near-zero for every candidate on both windows, and `information_capacity`
   flips sign between windows for `gpt-4.1-nano`. The prompt already warns these
   two run counter-intuitively; the data says they do not survive a change of
   scorer, and they should not be reported at the same confidence as the
   composite.

### One candidate got worse

`gpt-5.6-luna` fell from ρ 0.337 to **0.084** — the only candidate to move
backwards, and it also returned `edge_vitality` on just 9 of 53 anchors. A model
whose agreement is not merely low but *unstable across windows* cannot be
characterised by one number at all, which is a further argument for measuring on
more than one window before adopting anything.

---

## Determinacy: models agree where the evidence decides, and scatter where it does not

The thesis this project's measurements keep arriving at, stated as a thesis:

> **Models agree when the evidence is determinate and scatter when it is not —
> and the disagreement is measured against a signal that does not grow to meet
> it.**

Four independent lines of evidence, none of which was collected to test it.

### 1. Between-model disagreement halves on a determinate window

The same five candidates against the same reference, changing only the window:

| | US 2019 | TR 2018 |
|---|---|---|
| `gpt-4.1` ρ vs reference | 0.377 | **0.708** |
| disagreement sd (gpt-4.1) | 0.108 | **0.046** |
| ÷ that window's own signal | **1.71×** | **0.83×** |
| all candidates ÷ signal | 1.28–2.08× | 0.96–1.15× |

**The signal did not grow — TR's is smaller** (sd 0.055 against 0.063, median
weekly move 0.020 against 0.050). The *disagreement* shrank, by more than half.
TR 2018 is a legible crisis: a currency down a third, an emergency-rule
transition, a policy-rate response. Five models of very different sizes converge
on it. US 2019 is an ordinary year for a stable country, where the correct score
for a given week is genuinely underdetermined, and there they scatter by more
than the entire variation the series reports.

This also corrects the tempting reading of the volatility test: TR was chosen as
the *volatile* window and is not more volatile week-to-week. The prediction was
right about the outcome and wrong about the mechanism.

### 2. The instrument has about nine levels, whatever you point it at

Measured on three countries, three periods, 157 anchors, one model, one prompt:

| window | anchors | distinct composite values | sd |
|---|---|---|---|
| US 2019 | 52 | **9** | 0.063 |
| TR 2018 | 53 | **9** | 0.055 |
| PT 2019 | 52 | **9** | 0.078 |

Nine, three times. That is not a fact about any country — it is the resolution of
the instrument. A weekly series reported to two decimals is being produced by
something with roughly nine usable levels.

### 3. The coarseness is not an aggregation artifact

If the composite were flat because averaging washed out finer components, the
ledgers underneath would be finer. They are **coarser**:

| window | composite | friction | order_unc | info_cap | edge_vit |
|---|---|---|---|---|---|
| US 2019 | 9 | 5 | 9 | 4 | **3** |
| TR 2018 | 9 | 5 | 5 | 7 | **3** |

`edge_vitality` resolves a year into **three values**. The composite is finer than
any of its parts only because combining coarse components manufactures values
none of them holds. There is no finer signal surviving underneath to recover.

### 4. An explicit instruction holds where evidence is determinate and fails where it is not

This is the clearest mechanism in the document, and the most direct.

The prompt says: *"All scores are INTEGERS 0-100. Use precise values (37, 62, 81)
— **never round to multiples of 5**."* Under a uniform distribution over integers,
20% would land on one anyway. Measured, `gpt-4o` on that prompt:

| window | round-number share | what the window is |
|---|---|---|
| TR 2018 | **18.9%** | crisis — determinate |
| US 2019 | **69.2%** | ordinary year |
| PT 2019 | **84.6%** | quiet country — most ambiguous |

**Monotone in determinacy, and it is the same model reading the same instruction
every time.** At 18.9% it obeys — within a point of chance. At 84.6% the
instruction is simply not operating. Nothing about the prompt changed between
those rows; only how much the evidence decided.

That is what "scatter where the evidence is indeterminate" looks like from
inside: not noise around a correct answer, but a retreat to round numbers, and on
US 2019 specifically to **0.50 — the midpoint of the scale — on a third of all
anchors.** Faced with a week it cannot resolve, the model answers "the middle".

Notably it is *not* the prompt's bands doing this: band midpoints attract **0.0%**
of anchors on every model and every window, and the five calibration anchors take
15–35%, about what proximity alone yields.

### What was tried, and did not work

**More evidence is not the lever.** `p3-context` added four quarters of masked
history to attack exactly this, and made it worse — US 2019 went from 9 distinct
values to 7 and from 69% round to 75%, with not one of 52 anchors changing band.
See `docs/payload-ab.md`. The model was handed the history in the form it asked
for and became *less* discriminating.

So the remaining suspect is the prompt, which forbids rounding and is disobeyed
three times in four, and which never asks the model to separate two weeks inside
one band. That test is filed, and deliberately not run, as `docs/deferred.md` §12.

### What this costs the ratings

**It does not touch the procurement conclusions.** Determinism, cost, the
`gpt-4.1` migration price and the payload decision were none of them measured
through ρ.

**It bounds what the weekly series can claim, and the bound has an awkward
shape.** The series is most reproducible exactly where it is least informative — a
crisis any observer would call a crisis — and least reproducible on quiet weeks
for stable countries, which is where a risk rating would earn its keep. A
week-to-week movement reported for a quiet country-year is substantially
instrument rather than signal.

Three consequences, all validation work rather than scorer decisions:

1. **Report an uncertainty band, not a point estimate**, at least for
   low-volatility country-years. The band is measurable: it is the disagreement
   sd, ~0.10 on an ordinary year — wider than most of the movement being reported.
2. **Treat the 0.072 masking divergence with matching caution.** It was measured
   on PT, the quietest country in the roster and the one that rounds on 84.6% of
   anchors, and it is *smaller* than between-model disagreement on such a window.
   That does not make it wrong; it means it needs a second scorer to be
   distinguished from instrument noise.
3. **Stop reporting the four ledgers at the composite's confidence.**
   `edge_vitality` has three levels and is negative or near-zero against every
   candidate on both windows.

---

## Finding: the cheapest model was the worst one, and a cost table would have hidden it

`gpt-4.1-nano` costs **$0.0017 a snapshot against the incumbent's $0.0430** — one
thirtieth. On a procurement slide it is the obvious answer, and it would take the
48-country backfill from $1,079 to $35.

It is the worst candidate in the set on every axis that matters.

| | `gpt-4.1-nano` | reference |
|---|---|---|
| composite ρ | **0.240** | — |
| `friction` ρ | **−0.228** *(ordered backwards)* | — |
| `information_capacity` ρ | 0.121 | — |
| `sovereign_stress` agreement | **0.462** | — |
| worst same-input spread | **20 pt** | 0 pt |
| band migration | 21 of 52 anchors moved to High, 3 to Extreme | — |

Two of those deserve reading twice. **`friction` at −0.228 is not weak agreement,
it is inverted** — the weeks it calls most frictional are, mildly, the ones the
incumbent calls least. And **`sovereign_stress` agreement of 0.462 is worse than a
coin flip** on a flag that is false on nearly every US 2019 week; a model that
guessed "false" every time would have scored near 1.0.

**How it would have been adopted.** Three of the four things that condemn it are
invisible to the process that would normally pick a model:

- **Price says buy it.** It is cheapest by 30×, and cost is the number that gets
  into a decision document.
- **The schema gate passes.** It holds `RISK_SCHEMA_V3` under strict structured
  output, 10/10. A gate-based shortlist keeps it.
- **A single-anchor noise measurement understates it 4×.** Measured on the
  baseline payload alone it spreads 5 points, which looks tolerable and ranked it
  second-best. Only the calm anchor — where it returned 30, 35, 38, 38, 40, 30,
  **50**, 40, 30, 40 while `gpt-4o` returned 12 ten times out of ten — shows the
  20-point swing.

Only the rank correlation against a reference catches it, and that is the
measurement nobody runs because it requires already having scored the window
twice.

**The transferable lesson:** a cheap model that passes your schema gate has
demonstrated that it can *fill in your fields*. It has demonstrated nothing about
whether it agrees with you. Those are different questions, and the cheap one is
the one everybody measures.

---

## Finding: four of six meters printed `n=0` and the report looked healthy

The first full comparison ran, rendered, and was read — with **four of its six
rank-correlation meters blank**.

`capture_baseline` reads `risk_snapshot.ledger_scores` and copies the column
straight onto the row. That column is a JSONB holding *two* things:

```json
{"ledger_scores": {"friction": 0.38, ...}, "subscore_evidence": {...}}
```

while the candidate arm returns the four scores flat, off `llm_output`. So the
baseline's ledgers sat one level too deep, every lookup found nothing, and
`_paired` — correctly, by its own contract — dropped the `None` rather than
raising.

The output was this:

```
metric                    n  spearman   kendall    signed   |shift|   max|d|
llm_score                52     0.377     0.297    -0.008     0.089    0.250
score_3m                 52     0.433     0.358    -0.025     0.093    0.260
friction                  0         —         —         —         —        —
order_uncertainty         0         —         —         —         —        —
information_capacity      0         —         —         —         —        —
edge_vitality             0         —         —         —         —        —
```

**Nothing in that is an error.** `n=0` with an em dash is exactly what this
codebase renders for "not measured", deliberately, so that an unmeasured pair and
a perfectly agreeing one never look the same. The composite and `score_3m`
populated correctly, so the table read as a working report with four metrics
nobody had got round to filling in yet.

It was caught only because the four ledgers had been asked for explicitly and
their absence was noticed — not by any check.

**Why it is worth a section.** This is the same failure the project has already
found six times, in a seventh place: *a stamp that records what somebody wrote
down rather than what actually happened.* The rendering convention that makes
missing data honest also makes missing data quiet. An em dash tells you a number
is absent; it cannot tell you the number was absent because of a bug.

Two things follow, and the second is the general one:

- The fix unwraps with `.get("ledger_scores", ledgers)` rather than a bare index,
  so a future flat column does not start returning empty instead — and a test now
  asserts `n` is not zero when both sides carry ledgers, which is the regression
  itself rather than a proxy for it.
- **A report that degrades gracefully needs something that does not.** Where
  "absent" is a legitimate rendering, absence stops being a signal, and the check
  has to live somewhere the renderer cannot swallow it.

---

## Recommendation

Two questions, deliberately separated. Conflating them is how a forced migration
gets made in a hurry on the day the deprecation notice arrives.

### 1. Is a cheaper model worth it? — No.

**Stay on `gpt-4o-2024-08-06`.** Nothing is switched.

Not on price, and — after the two-window test — not primarily on ordering either.
On **determinism and reliability**.

The ordering evidence has to be stated carefully, because it changed when the
window did. On US 2019 the cheap candidates ranked between ρ 0.100 and 0.337; on
TR 2018 the same models ranked 0.084 to 0.422. No candidate but `gpt-4.1` reaches
0.5 on either window, none is stable *across* windows — `gpt-5.6-luna` moves from
0.337 to 0.084 — and `edge_vitality` is negative or near-zero for all of them on
both. So the case against the cheap models is not "they disagree on one window",
which would not have survived the second; it is that **their agreement cannot be
characterised by a single number at all**, on top of a noise floor of 7–20 points
where the incumbent's is 0.

The specific traps, each worth carrying forward:

- **`gpt-4.1-nano`** looks like the obvious win at 1/30th the cost and is the
  worst outcome in the set: ρ = 0.240, `friction` **−0.228**, `sovereign_stress`
  agreement **0.462**, and a 20-point swing on identical input on the calm anchor
  where `gpt-4o` answers 12 ten times out of ten. It would have been adopted on a
  single-anchor noise measurement and a cost table.
- **`gpt-5.4-mini`** costs 9× `gpt-4.1-nano` and correlates *worse* (ρ = 0.100).
  Price is not a proxy for agreement in either direction.
- **`gpt-5.6-luna`** returned `edge_vitality` on only 12 of 52 anchors — the
  ledger is mostly absent rather than wrong, which no cost table would show.

### 2. When do we migrate off `gpt-4o`? — Not yet, and now we know the price.

This is the question that matters more, because it is not optional. `gpt-4o` is a
2024 model, this series is meant to run for years, and it will be deprecated on
someone else's schedule. `gpt-4.1` is OpenAI's stated successor — 20% cheaper at
$2/$8, better instruction-following, 1M context — so measuring it now is not
chasing a saving. It is pricing a move that gets forced on us.

**The measured price of that migration:**

| | |
|---|---|
| Repeat-stability | **±1 point**, flat across Low, Moderate and Extreme — 20% of a typical week's move, 14% of the masking signal. **Good enough.** |
| Level offset | **−0.008 signed.** Essentially none. |
| Week ordering | **ρ = 0.708 (TR 2018), 0.377 (US 2019)**; `score_3m` 0.865 on TR. `edge_vitality` negative on both. |
| Per-week disagreement | **0.046 sd on TR, 0.108 on US** — 0.83× and 1.71× the series' own variation |
| Cost | $0.0298/snapshot, 31% below the incumbent |

**It is still not a recalibrate-and-go migration, but it is closer than US 2019
suggested.** A constant level offset would be survivable — move the prompt's
calibration anchors and the series shifts with them. `gpt-4.1` has no such
constant on either window: signed −0.008 against absolute 0.089 on US, signed
+0.058 against absolute 0.058 on TR. It moves individual weeks rather than the
level.

What the second window changes is the *size* of that movement. At ρ = 0.708 with
τ-b 0.602 and `score_3m` at 0.865, `gpt-4.1` largely tracks the incumbent where
the evidence is determinate, and diverges where it is not — which is the same
place the incumbent's own reproducibility is weakest. So the migration is a
re-score, but the series it produces is recognisably the same series, not a
different opinion.

So the honest statement of the migration cost is: **switching to `gpt-4.1`
requires re-scoring the history, not adjusting it.** At $0.0298/snapshot a full
25,104-snapshot re-score is ~$747 — affordable, and now a known number rather
than a discovery made under deprecation pressure. What it costs in *time* is the
harvest and the digests, which are already stored.

**Recording ±1 now, with three anchors behind it, is worth more than the 20%.**
When the deprecation notice arrives the decision is already made and measured:
`gpt-4.1` is stable enough to be an instrument, it is not a drop-in continuation
of the existing series, and the migration is a re-score with a price attached.

### What protects the series either way

`gpt-4o`'s determinism is very likely a property of **how it is served**, not of
anything in this repository — the same model lost it when only its schema grammar
was weakened, and five models with identical grammar do not have it. We do not
control that, cannot inspect it, and get no notification when it changes.

`score.FROZEN_FIELDS` pins `SCORING_MODEL` and so catches *us* changing the
scorer. It cannot see the scorer changing behind a stable id. **That is the gap
the determinism canary in `docs/deferred.md` §10 closes**, and it is worth
building before the pilot rather than after: one stored payload, re-scored a
handful of times on a schedule, asserting the scored fields still match and
failing loudly when they do not.

Without it, the reproducibility claim can quietly stop being true — under the
incumbent *or* under `gpt-4.1` — while every version stamp still agrees, because
every version stamp is about us.

### What would change this recommendation

- A candidate clearing **ρ ≥ 0.9** on the composite with a level-only offset. The
  best observed is `gpt-4.1` at 0.708 on TR 2018, and `score_3m` at 0.865 shows
  the 3-month horizon is closer than the 12-month one.
- **A third window.** Two are enough to show the figure is window-dependent and
  not enough to characterise it. `gpt-5.6-luna` moving 0.337 → 0.084 is the
  warning: a single window can flatter or damn a candidate.
- Evidence that `gpt-4o`'s determinism has moved, which flips the migration from
  elective to urgent and makes §10 the thing that told us.
