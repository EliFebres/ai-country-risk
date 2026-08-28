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
2. Every candidate **ranks the weeks differently** — Spearman ρ from 0.100 to
   0.377 against the reference — and that disagreement is *not* explained by
   their jitter. Noise can be averaged down. A different opinion cannot.

The second matters more because it survives every fix for the first. A cheaper
model here does not buy the same series for less; it buys a different series.

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

Not on price, and not on determinism alone. On **agreement**: the cheapest four
candidates rank the 2019 weeks differently from the incumbent — ρ between 0.100
and 0.337 on the composite, with negative correlation on `friction` for two of
them — and that disagreement is **not** explained by their jitter (see the ρ
ceilings above). A cheaper model here does not buy the same series for less; it
buys a different series.

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
| Week ordering | **ρ = 0.377, τ = 0.297** on the composite. `edge_vitality` **−0.100**. |
| Per-week disagreement | **0.089 absolute** mean shift against a 0.050 median weekly move |
| Cost | $0.0298/snapshot, 31% below the incumbent |

**It is not a recalibrate-and-go migration.** That was the outcome worth hoping
for and the numbers do not support it. A constant level offset would be
survivable — move the prompt's calibration anchors and the whole series shifts
with them. What `gpt-4.1` actually does is score individual weeks differently in
both directions, cancelling to −0.008 on average while moving each week by 0.089.
There is no constant to remove. Recalibration cannot fix a reordering, and this
is a reordering.

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

- A candidate clearing **ρ ≥ 0.9** on the composite with a level-only offset. None
  came close; the best was 0.377.
- The reference re-measured on a **body-rich, more volatile country-year**. US
  2019 is a low-variance window — all 52 anchors sit in one band — and a series
  that never leaves Moderate is a hard place to demonstrate agreement on
  ordering. This is the most likely way the numbers understate the candidates,
  and it is cheap to test.
- Evidence that `gpt-4o`'s determinism has moved, which flips the migration from
  elective to urgent and makes §10 the thing that told us.
