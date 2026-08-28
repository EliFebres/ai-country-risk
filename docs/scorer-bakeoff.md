# Which scorer

Whether a cheaper model can replace `gpt-4o-2024-08-06` as the pilot's scorer,
measured rather than argued. Three rounds, 2026-08-27.

The pilot's claim is that every row in a ten-year weekly series was produced by
one scorer under one prompt. So changing the scorer is not procurement that
happens to touch code — it is an **instrument change**, and the only honest way
to make one is to re-run a fixed set of anchors through both and look at what
moved. Price is read last, and only by candidates that got that far.

**The finding, in one line:** of every model tested, only the incumbent
reproduces its own scored output — and **strict grammar enforcement turns out to
be necessary but not sufficient to explain why.**

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

## What we cannot explain

It would be tidy to say determinism comes from strict grammar enforcement. The
evidence does not support it, and the tempting version of this finding is wrong.

**Necessary.** Weakening the grammar breaks determinism on a model that
otherwise has it. Rewriting four union types as `anyOf` — mechanically
equivalent JSON Schema — took gpt-4o from 50 ×9 to 52 ×7 / 50 ×2. Dropping to
`json_object` broke it on every third-party candidate.

**Not sufficient.** `gpt-4.1` holds the *identical* `RISK_SCHEMA_V3`, under the
*identical* `.with_structured_output(strict=True)` wrapper, at the same
`temperature=0, seed=42` — and still varies its scored fields. So does
`gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-5.4-mini` and `gpt-5.6-luna`. Same grammar,
same constraint, same request shape; different behaviour. Grammar cannot be the
explanation for something models with the same grammar do not share.

So **something further about how `gpt-4o` specifically is served also matters,
and we have not identified it.** Candidates worth ruling out, none of them
tested here: how each model honours `seed` (many endpoints accept it and ignore
it); batching, routing or fleet heterogeneity behind the endpoint; whether the
constrained-decoding implementation differs by model generation; or simple
numerical non-associativity that a lower-entropy model happens to survive.

Two consequences worth stating plainly:

- **Do not generalise this to "newer models are less deterministic."** It is one
  prompt on one anchor's evidence. It is a strong, repeated observation about
  these models on this workload, not a law.
- **Do not assume `gpt-4o`'s determinism is permanent.** If it is a property of
  how the model is served rather than of the schema we control, it can change
  under us without any change on our side — which is an argument for the freeze
  in `score.FROZEN_FIELDS` catching model moves, not an argument for trusting the
  current one forever.

---

## Reading a rank correlation against the noise floor

A candidate that returns a different score for the *same* input is arguing with
itself, and no correlation measured across 52 *different* anchors can be read
below that level. So each candidate's spread is reported in the divergence
meter's own units rather than in model points.

The benchmark is **PT's masking divergence, 0.072** on the stored 0-1 scale —
what a country's identity was worth to the scorer, and the only substantive
signal this project has put a number on. Models answer on 0-100 and the store
keeps 0-1, so a spread of N points is N/100 against 0.072.

| same-input spread | on the stored scale | share of the masking signal |
|---|---|---|
| 0.5 pt | 0.005 | **7%** — invisible |
| 2 pt | 0.02 | **28%** — a quarter of the signal spent on nothing |
| 5 pt | 0.05 | **69%** |
| 12 pt | 0.12 | **167%** — exceeds the whole signal |
| 15 pt | 0.15 | **208%** |

This, not the determinism gate's pass/fail, is what decides *reproducible within
tolerance*. The gate answers whether a candidate is exactly reproducible; this
answers whether its irreproducibility is large enough to matter. A candidate
whose noise floor exceeds 100% cannot be used to measure masking at all — the
finding would be smaller than the instrument's own wobble.

`bakeoff compare` prints this line directly beneath each candidate's rank
correlations, so the two are read in one register.

The benchmark is carried as `bakeoff.PT_MASKING_DIVERGENCE`, sourced from a prior
measurement and **not currently reproducible in this repo**: the `named` and
`masked_nostructural` arms have never been run and `snapshot_diagnostic` is
empty. It should be recomputed and moved the first time a real divergence lands.
