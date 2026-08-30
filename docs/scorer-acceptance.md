# The scorer acceptance bar

**What a candidate scorer has to clear to be adopted, and what is merely
reported about it.** Applies to any candidate — hosted, local, a different
vendor, or a new version of the incumbent behind the same model id.

**Written 2026-08-29, before any local candidate existed.** That sentence is the
only thing that makes this document worth anything. Criteria written after
results are in hand are not criteria; they are a description of the winner. This
project has already paid for that lesson three times — `docs/payload-ab.md`
attempt 2 pre-registered a criterion against a field nothing wrote
(`deferred.md` §28), attempt 3's cost criterion measured the provider's prompt
cache for three attempts running (§32), and the one piece of correctness
evidence in existence turned out to have an undetected crisis inside its
baseline period (§29). All three were computable from day one. None of them was
computed against a stored row before the arms were paid for.

So: **compute every criterion below against a stored row or a dry run before
spending on a candidate.** If a criterion cannot be evaluated, it is not a
criterion yet.

---

## The reference sets

Two different jobs, deliberately not the same model. See `deferred.md` §11.

| role | what | why |
|---|---|---|
| **Production scorer** | `gpt-4o-2024-08-06` | what the series is built from. A candidate does not have to beat it to be interesting; it has to beat it to replace it. |
| **Benchmark incumbent** | **`gpt-4.1`** — `backend/bakeoff/US-2019/gpt-4.1.json`, `backend/bakeoff/TR-2018/gpt-4.1.json` | the discrimination and prompt-compliance reference. It is the best-resolving arm anyone has measured (18 distinct values, 5.8% round share), and a bar set by what we happen to ship is not a bar. |
| **ρ disaster detector** | **A′** — `p2-rebaseline`, the incumbent on the current payload | an inversion matters against the series actually stored, and A′ is that series' configuration. |

Both windows, always. **US 2019** is the ambiguous one — 3,344 Guardian articles
with bodies, and every `gpt-4o` arm ever run puts all 52 anchors in `Moderate`.
**TR 2018** is the determinate one — a cross-border offensive in Q1 and a
currency crisis in Q3. A candidate measured on one window has been measured on
half the question, and the two windows disagree about almost everything.

---

## Hard gates

A candidate that fails any of these is out. Not adjusted for, not weighted
against a strength elsewhere — out.

### 1. Schema adherence

**Line: zero invalid outputs over the anchor set, and the route is recorded.**

Run `python -m backend.util.tools.bakeoff smoke <candidate>` first — nine calls,
and it runs the real `AI_PROMPT_V3` on a real assembled payload against the real
`RISK_SCHEMA_V3` through the production wrapper. A three-field toy schema says
nothing about one with ten required fields, nested objects and
`additionalProperties: false` throughout.

`schema.route` says *how* it held and the two are not the same fact:

| route | meaning |
|---|---|
| `strict` | the endpoint compiled our schema and constrained decoding to it. The production contract. |
| `json_object` | the endpoint has no strict mode; it returned JSON and `_validate_locally` checked it here. **A weaker fact wearing the same word.** |
| `none` | neither route answered. Out. |

A `json_object` pass is not disqualifying — it is how DeepSeek was measured, at
0% invalid — but it must be **stated in any comparison**, because a candidate
that needs local validation and a retry is not the same instrument as one that
cannot emit an invalid answer.

Then measure over the anchor set rather than assuming: report the **failure and
retry rate**. Zero retries is as good as strict mode. A few percent is a cost
note. `minimax-m3` returned 40% invalid through `json_object` while DeepSeek
returned 0%, so the spread between endpoints on the identical route is wide
enough that it has to be observed.

**What the gate does not check, and nothing else does either.**
`bakeoff.grammar_risks(RISK_SCHEMA_V3)` names 21 constraints — every numeric
bound, `maxLength`, and the four `["integer", "null"]` unions — that no
context-free grammar can express. LangChain forwards them to OpenAI verbatim
under `strict: true` and they are not in the enforced subset, so **production
has this hole too**. `langchain_llm._from_100` clamps to 0–1 and that clamp is
what actually holds the line. Run `grammar_risks` before pointing the harness at
any grammar-constrained endpoint and read the output as a list of things the
local validator is solely responsible for.

### 2. Determinism — **the line is PROVISIONAL**

**Line: worst-band `score_spread` ≤ 3 points on the 0–100 scale, and
`scored_match_rate` reported alongside it.**

> ## ⚠ The 3-point line was derived on the wrong instrument
>
> **Marked provisional 2026-08-30. Deliberately not re-derived here** — that
> wants writing cold, the way §12's criteria were, and it needs both numbers in
> hand first. They now are.
>
> Every figure this line was calibrated against — `gpt-4o` at 2, `gpt-4.1` at 2,
> and the four prose-only rows at 7, 8, 11 and 20 — was measured on a **canned
> ~2,980-token payload**. The prompt the run dispatches is **11,142 to 13,038
> tokens**. Re-measured on real assembled payloads:
>
> | model | canned worst | **real worst** | real by band |
> |---|---|---|---|
> | `gpt-4o` — **production** | 2 | **17** | 5 / 17 / 0 |
> | `gpt-4.1` — benchmark incumbent | 2 | **9** | 4 / 9 / 0 |
>
> `scored_match_rate` is **0.000 on all three bands for both**. Neither model
> reproduces its own scored output once in ten on the payload it is actually
> sent.
>
> **Do not fail a candidate on this line until it is re-derived.** As written it
> disqualifies the production scorer and the benchmark incumbent, and a gate the
> reference cannot pass disqualifies every candidate for a defect the reference
> shares — the mistake this criterion was already rescued from once.
>
> The *reasoning* below is unchanged and is still the right way to set the
> number: repeat noise has to be small against the **smallest** effect anyone
> would call real, which is TR 2018's crisis response at +7.3 to +14.9 points.
> What has to be redone is the measurement it was set against.
>
> **The question the re-derivation has to answer**, and it is not "what is a
> looser line": if the instrument's own noise is 17 points and the largest real
> effect ever measured is 14.9, then on the composite **there is no threshold
> that admits the signal and excludes the noise**. The honest conclusions
> available are that the composite is the wrong unit and §30's uncertainty band
> is the reporting, or that repeats must be averaged rather than required to
> match, or that the ledgers and flags are the instrument and the composite is a
> summary of them. Choosing between those is the work, and it is not a threshold
> tweak.
>
> **What is not in doubt**: per-field draws, not just spreads. On `gpt-4o`'s
> stressed band the composite holds at 82 across all ten calls while
> `sovereign_stress` flips `False`/`True` and `evidence_coverage` alternates
> 75/85. A spread of 0 reads as reproducible there, and the instrument is
> disagreeing with itself about whether the country is in sovereign stress.
> Whatever line replaces this one has to be read alongside `moved_fields`.

Three anchors × ten repeats at `temperature=0`, `seed=42`, compared on **scored
fields only** (`bakeoff._scored_only`): `bullet_summary`, `subscore_evidence`,
`band_placement` and `typical_week` are prose and are excluded, because gating
on them fails `gpt-4o` itself and a gate the reference cannot pass disqualifies
every candidate for a defect the reference shares.

```
python -m backend.util.tools.bakeoff smoke <candidate> --repeats 10
```

**Three bands, worst wins.** `calm` / `moderate` / `stressed`, because the
candidates do not behave alike across the range: `gpt-4.1-nano` swung 20 points
on a calm payload and 5 on a stressed one, so a mean would have reported it four
times steadier than it is.

**Why 3 points, stated in the units of the effect being measured rather than as
a round number.** The thing this instrument exists to detect is a country's risk
moving. The largest such effect this project has ever measured is TR 2018's
crisis response against a quiet baseline: **+0.149 for A′, +0.115 for `gpt-4o`,
+0.073 for `gpt-4.1`** — that is, 7 to 15 points on the 0–100 scale. Repeat
noise has to be small against the *smallest* effect anyone would want to call
real, not the largest:

| candidate | worst spread | as a share of a +7.3 pt effect | verdict | source |
|---|---|---|---|---|
| `gpt-4o` | **2** | 27% | passes | **re-derived** 2026-08-29 |
| `gpt-4.1` | **2** | 27% | passes | **re-derived** 2026-08-29 |
| `gpt-5.4-mini` | 7 | 96% | **fails** — noise the size of the signal | prose only |
| `gpt-4.1-mini` | 8 | 110% | **fails** | prose only |
| `gpt-5.6-luna` | 11 | 151% | **fails** | prose only |
| `gpt-4.1-nano` | 20 | 274% | **fails** | prose only |

**The first two rows are not the published ones, and the difference is the
reason this criterion is written on the worst band.** `gpt-4o` was recorded at 0
points across all three bands. Re-run on three payloads with the per-repeat
draws kept, it is exact on the moderate payload — the only one it had ever been
measured on — and on neither of the others: `edge_vitality` alternates between
`60` and `null` on the calm payload while `score_12m` holds at 12, and
`score_12m` returns 90 once in ten on the stressed one. `gpt-4.1` is 2 as
published, so the two candidates are **level on determinism**, not 0 against 2.
`docs/scorer-bakeoff.md` carries the full correction.

A candidate is therefore compared against 2 points, not against 0. Nothing in
the tested set has ever been exact across all three bands.

Three points is 41% of the smallest effect. Above that, a repeat of a measurement
is measuring the instrument rather than the country, and the three things
determinism is load-bearing for stop working: the byte-for-byte
`rebuild_snapshot` check cannot tell "the evidence changed" from "the model
wobbled"; a gate-2 repeat measures noise; a resumed pilot becomes two
instruments wearing one name. Note what is *not* on that list — the pilot itself
scores 2,092 novel inputs exactly once, so repeat-stability never arises during
the run. It is the verification around the pilot that needs this.

**Determinism cannot be shopped for.** It is not a feature with a price:
`gpt-4.1` costs twenty times `gpt-4.1-nano` and both are non-deterministic. And
it does not transfer across grammar compilers — rewriting the four union types
as `anyOf`, mechanically equivalent JSON Schema, made `gpt-4o` non-deterministic
on demand (50×9 became 52×7, 50×2). **A local candidate must be measured on its
own serving stack**; no result from a hosted endpoint carries over.

### 3. Prompt compliance

**Line: round-number share ≤ 40% on both windows, and the gap between them
reported.**

The share of scores that are multiples of 5, against a prompt that instructs
against exactly that. This has been the sharpest single diagnostic in the
project — the one measure that moved when six payload and prompt interventions
did not, and the one that separates the candidates most cleanly.

Chance floor is 20%. The observed range is 3.8% to 96.2%, so 40% is a real line
rather than a nominal one:

| scorer | US 2019 | TR 2018 |
|---|---|---|
| `gpt-4.1` | 5.8% | 3.8% |
| `gpt-5.6-luna` | 21.2% | 7.5% |
| `gpt-5.4-mini` | 23.1% | 15.1% |
| `gpt-4o` | 69.2% | 18.9% |
| `gpt-4.1-nano` | 88.5% | 96.2% |
| `gpt-4.1-mini` | 92.3% | 94.3% |

**The incumbent fails this on US 2019 and is grandfathered, explicitly.** That is
uncomfortable and it is the honest position: the bar is for what we would adopt,
and a 69.2% round share is the single clearest statement that the instrument is
not resolving on the ambiguous window. Recording it as a pass would be writing
the criterion around the incumbent.

Report the **gap** as well as the levels. Only `gpt-4o` shows a large
window-dependent gap (50.3 points); two models show it reversed. It is a property
of that scorer, not a law about models under ambiguity, and `deferred.md` §34
raises a further possibility nobody has ruled out — that the US relevance
heuristic saturates, so "ambiguous evidence" and "evidence flattened by the
selector" look identical from inside the prompt.

---

## Reported, not gated

Real inputs to the decision. Not pass/fail, because each has a defensible reading
in both directions.

### 4. Discrimination

Distinct composite values, round-number share, bands occupied, and the longest
run of identical consecutive weeks — **on both windows**. `bakeoff.series_shape`
computes it.

Reported rather than gated because **a variant that buys one window by wrecking
the other is not an improvement**, and this project has measured exactly that:
`gpt-4.1` × within-band reaches 15 distinct on TR (the highest of any arm) while
*losing* six values on US against `gpt-4.1` alone. A single-window threshold
would have adopted it.

And because resolution is not correctness. A model that spreads noise across
thirty buckets scores better here than one that is coarse and right. Read this
criterion only alongside §6.

Benchmark: `gpt-4.1`'s 18 distinct / 5.8% round / 3 bands on US 2019.

### 5. Cost

**Priced on tokens sent, at list, never on billed spend.**
`bakeoff.cache_neutral_per_snapshot` is the only number that goes in a
comparison.

Realised spend depends on the provider's prompt cache, and the cache depends on
which arm ran immediately before on the same anchors. `vs-typical` ran straight
after `within-band` and hit a 90.8% cache share against A′'s 3.9%, reporting
−36% per snapshot **while sending more tokens than A′ in both directions**. On
tokens it is +2%. Three published verdicts were corrected for this; see §32.

**A candidate with no list price reports no dollars.** `usage.is_priced` is
False for any model absent from `PRICES_USD_PER_1M`, and `cost_summary` then
withholds `spend_usd`, `per_snapshot_usd` and
`cache_neutral_per_snapshot_usd` rather than let `_FALLBACK_PRICE` — which is
gpt-4o's rate, and exists to stop a run early rather than to describe one —
produce a figure that looks measured. What is reported instead is real:
`input_tokens_per_snapshot`, `output_tokens_per_snapshot`, and
`seconds_per_snapshot`.

For a locally served model that is the whole cost answer, and it is the right
one: the cost of a local model is hardware and wall-clock, and neither is
comparable to a per-token list price. **Do not convert it.** A GPU-hour divided
by snapshots is a number about your machine, not about the model.

### 6. Event validity — **PROVISIONAL**

**Do not use the measure as it currently stands.** It produced a published sign
error.

The measure was: TR 2018 Jan–Feb mean against Aug–Sep mean, read as "the move
into the lira crisis". It said `gpt-4.1` drifted *down* through a currency
collapse, and that number is what blocked `deferred.md` §11 for a day. It was
wrong three separate ways:

1. **No control period.** Jan–Feb 2018 was assumed quiet. Operation Olive Branch
   ran 20 January to 24 March and dominates the selected twenty at every
   February and March anchor. The statistic compared one crisis to another.
   Against a period *checked* to be quiet (7 May – 18 June), every arm rises
   into the lira crisis, `gpt-4.1` included.
2. **No negative control.** The same statistic on US 2019, a window with no
   crisis, returns a mean |Δ| of 0.039 against TR 2018's 0.054. A measure that
   cannot tell a crisis year from an ordinary one is not measuring crisis
   response.
3. **An evidence proxy with no variance.** `articles` is 20 at all 676 stored US
   rows, because `snapshot_select.select` tops up to twenty by rank. The
   reported ρ of −0.068 / −0.114 against |Δarticle count| was computed against a
   constant.

**What this criterion becomes non-provisional on** — pre-registered here, so
that it is fixed before the study that satisfies it is run:

- a **control period verified quiet** by inspecting the selected articles, not
  assumed from the calendar;
- a **negative-control window** with no crisis, which the measure must
  distinguish from a crisis window by a stated margin;
- an **evidence proxy that varies** — mean selected relevance and the count
  clearing the threshold both do, and both are computed during selection today
  and thrown away (`deferred.md` §34);
- **two or three countries**, from a dated event list built from a source
  independent of the scoring payload.

Until then, what may be reported is the interim measure with all three defects
named: crisis response against a quiet baseline. `gpt-4o` +0.115, A′ +0.149,
`gpt-4.1` +0.073, and the incumbent's larger response is the surviving point in
its favour.

### 7. ρ against the incumbent — a disaster detector, not a ranking criterion

**Demoted explicitly.** It was a ranking criterion (ρ ≥ 0.7) and it should never
have been one.

Rank agreement with the incumbent rewards a candidate for reproducing the
incumbent's judgement, **including where that judgement is wrong** — and the
whole reason a candidate is being screened is that the incumbent might be. A
candidate that agrees perfectly adds nothing; one that disagrees has either
found something or broken something, and ρ cannot tell those apart. The
attenuation analysis in `docs/scorer-bakeoff.md` settles the related question:
the ρ ceilings implied by each candidate's noise sit far above the observed
values, so the disagreement is judgement rather than noise.

**Line: ρ ≥ −0.10 on `llm_score`, `score_3m`, and every ledger with at least
five distinct values in both series. Against A′, on both windows.**

Computable today: `bakeoff.rho_gate(baseline_rows, candidate_rows)`.

Set to catch *inversions*, not to reward agreement. The failure it exists to
find is `gpt-4.1-nano`'s friction ledger at **−0.228** against `gpt-4o` on US
2019 — a candidate ranking the weeks of a year in reliably the wrong order,
which is a broken instrument rather than a differing opinion. **A ρ of 0.3 is
not a finding. A ρ of −0.2 is.** Nano passed on the composite (0.240) while
inverting that ledger, so this is computed per metric or it misses the thing it
is for.

**Why −0.10 and not 0.0.** `gpt-4.1-nano` sits at −0.036 on TR
`information_capacity` against A′, which is a coin flip on a coarse ledger
rather than an inversion. A zero floor would fail it for noise.

**Why the five-distinct guard, which is the load-bearing part.**
`edge_vitality` takes **two distinct values across all 52 US 2019 anchors** —
`deferred.md` §3, the ledger has at most three indicators underneath it — and on
that series **`gpt-4o` disagrees with itself at ρ = −0.287** across a payload
change. A rank correlation over two values is not a rank correlation, and a gate
that fails the reference disqualifies every candidate for a defect the reference
shares. That is the mistake the determinism gate already made once and had to be
rescued from, so it is guarded rather than re-learned.

Excluded metrics are **named and reported, not dropped**: a ledger too coarse to
rank is a finding about the instrument. On US 2019 that is three of the four —
`friction` (4 distinct), `information_capacity` (3), `edge_vitality` (2) — and
only `order_uncertainty` survives to be gated. Which is worth sitting with: on
the ambiguous window, three quarters of the ledger structure carries too little
range to rank a year with.

Calibration, all from committed arms and asserted in
`test_util.TestTheRhoGateAgainstTheArmsItWasCalibratedOn`:

| comparison | worst gated ρ | verdict |
|---|---|---|
| `gpt-4o` vs A′ (US) | 0.582 | the reference clears it |
| `gpt-4o` vs A′ (TR) | 0.279 | the reference clears it |
| `gpt-4.1` vs A′ (TR) | 0.408 | pass |
| `gpt-5.6-luna` vs A′ (US) | 0.106 | pass — weak agreement is not a failure |
| `gpt-4.1-nano` vs `gpt-4o` (US) | **−0.228** on friction | **fail** |

---

## Adding a local candidate: the worked example

The dict entry is genuinely the easy part. What follows is the whole procedure,
in order, with the parts that actually fail marked.

### 1. Start the server

Any OpenAI-compatible server. The two that matter:

```bash
# llama.cpp — no strict mode; use GBNF or plain json_object
llama-server -m model.gguf --port 8000 --host 127.0.0.1

# vLLM — has guided decoding, still not OpenAI's `strict` flag
vllm serve <model> --port 8000 --guided-decoding-backend xgrammar
```

### 2. Add the entry

Copy `local-template` in `backend/util/tools/bakeoff.py` and change two lines:

```python
"my-local-model": {
    "arm": "scoring",
    "note": "what it is and why it is being screened",
    "env": {"SCORING_MODEL": "the-id-the-server-answers-to",
            "SCORING_BASE_URL": "http://127.0.0.1:8000/v1"},
    # NOT OPENAI_API_KEY. A local run has to work when the vendor key is
    # absent, which is the whole situation it is for. The client insists on
    # some key; the server ignores it.
    "key_env": "SCORING_LOCAL_KEY",
    "key_target": "SCORING_API_KEY",
},
```

and in `backend/.env`:

```
SCORING_LOCAL_KEY='not-a-real-key'
```

`arm` must be `scoring` — a candidate that also moves the payload or the prompt
produces a number with two causes and no way to separate them, which
`test_util.TestEveryCandidateIsAScorer` enforces.

The `local-template` entry itself points at `http://127.0.0.1:1/v1`, a port
nothing listens on, deliberately: a template that resolves is a template
somebody runs by accident.

### 3. Check the schema against the grammar backend — **before** anything else

```python
from backend.util.tools import bakeoff
from backend.llm import constants
for risk in bakeoff.grammar_risks(constants.RISK_SCHEMA_V3):
    print(risk)
```

21 lines today. The four `["integer", "null"]` unions are the ones to look at:
compilers differ on them, and this schema's determinism has already been
destroyed once by an equivalent rewrite. **Do not carry a determinism result
across backends.**

### 4. Smoke it

```bash
python -m backend.util.tools.bakeoff smoke my-local-model --repeats 10
```

Expect `route: json_object` rather than `strict` — no local server implements
OpenAI's strict flag, and the harness falls back and says so. Read the three
per-band spreads, not just the worst.

**It needs a database, and it will refuse without one.** Since 2026-08-30 the
three bands are real payloads assembled from three pinned anchors — PT
2019-06-03 (calm), US 2019-03-11 (moderate), TR 2018-08-13 (stressed) — entirely
out of the digest and rewrite caches, so no model call is made to build them. An
unreachable corpus or an uncached digest raises `SmokePayloadUnavailable` rather
than dropping to a canned payload: a gate that quietly runs small reports a pass
for a request the candidate was never asked to satisfy, and on disk that pass
looks exactly like a real one.

**What it now costs, and why that is worth paying.** The canned payloads
rendered at 2,962 / 2,980 / 2,987 tokens; the real ones render at 13,168 /
12,732 / 11,262 — within two tokens of what the scoring call dispatches at the
same anchors. That is **4.2× the input tokens**, and roughly:

| candidate | before | after |
|---|---|---|
| `gpt-4.1` | $0.158 | **$0.465** |
| `gpt-4o` | $0.231 | **$0.723** |

Three tenths of a dollar to stop certifying a fifth of the request. Gates 1 and
2 are the two this document says to stop on, and for a self-served model they
test exactly the properties that degrade with context — grammar-constrained
decoding gets harder, and determinism under batching and KV-cache pressure is a
different question at 13k than at 3k. `docs/pipeline-audit.md` §4 blocker 4.

The gate result now carries a `payload` block naming each band's anchor, its
realised token count and how it was counted, so payload size drifting away from
what the gate exercises is visible in the next run rather than needing an audit
to find.

The gate is **stricter than the run** on one point: `_validate_locally` rejects
a `score_12m` of 250 that `_from_100` would clamp to 1.0. That is the right
direction — a model answering 250 has misunderstood the scale — but a candidate
failing on a bound alone deserves that noted rather than read as "cannot hold
the schema".

### 5. Score both windows

```bash
python -m backend.util.tools.bakeoff score my-local-model --country US --since 2019-01-01 --until 2019-12-31
python -m backend.util.tools.bakeoff score my-local-model --country TR --since 2018-01-01 --until 2018-12-31
python -m backend.util.tools.bakeoff              # render
```

Cost comes back as tokens and wall-clock with `priced: false`. That is correct
and is not a bug to work around.

### What will surprise you, in order of likelihood

1. **`strict` 400s.** Handled — the fallback is automatic and recorded.
2. **Determinism is worse than the hosted result for the same model.** Expected.
   Batching, kernel scheduling and the grammar compiler all differ. Measure it
   here; do not import it.
3. **Numeric bounds are not enforced.** The local validator is the only thing
   catching them. See step 3.
4. **The cost table is empty.** By design. See §5.

---

## What in `docs/scorer-bakeoff.md` can be re-derived, and what cannot

Asked for explicitly, because a reader has no way to tell a number that can be
recomputed from one that survives only as prose — and this project has now been
bitten by that twice.

| figure | status |
|---|---|
| Per-anchor scores, distinct values, round share, run lengths, bands, ρ, cost per snapshot | **Reproducible.** `backend/bakeoff/{US-2019,TR-2018}/*.json` hold every anchor; `bakeoff.compare_all()` recomputes the tables. |
| Crisis response, period means, evidence correlations | **Reproducible** from the same files plus a read-only corpus query. |
| The schema and determinism gates for `vs-typical` and `within-band` | **Reproducible** — the only two files that ever carried a `gates` block. |
| The three-band noise matrix, **`gpt-4o` and `gpt-4.1` rows** | **Re-derived 2026-08-29** and stored in the `gates` block of all four of their arm files, per-repeat draws included. Both are 2 points, not the published 0 and 1. |
| The same matrix, **the other four rows** (`gpt-5.4-mini` 7, `gpt-4.1-mini` 8, `gpt-5.6-luna` 11, `gpt-4.1-nano` 20) | **Prose only — and deliberately not re-run.** They came off the same one-payload instrument that reported `gpt-4o` as exact. But those four were already worst-of-three-anchors, and adding bands can only *raise* a worst-band figure, so each is a **lower bound** and all four are already far above the 3-point line. Re-running would tidy the table and could not change a verdict. About $0.20 each if a reason appears. |
| `gpt-4.1-nano`'s calm-payload draw (30, 35, 38, 38, 40, 30, 50, 40, 30, 40) | **Prose only.** Per-repeat scores are persisted now; they were not then. |
| The round-2 `json_object` validity rates (DeepSeek 0%, MiniMax 40%) | **Prose only.** Those candidates were removed from `CANDIDATES` after round 2 and `round2-gates/*.json` carries `repeats: 0`. |
| The `anyOf` determinism control (50×9 → 52×7, 50×2) | **Prose only**, and the portable schema variant is not in the tree. |
| Cache shares measured on repeats (64%, 98%, 100%) | **Prose only**, and explicitly not the share a backfill would get. |

The rule this argues for is the one §25 and §28 already state, pointed at
measurements rather than fields: **a number that will be quoted needs a writer
that persists it.** Five of the eight rows above are prose because the code that
produced them ran once, in a terminal, and reported a summary.

---

## Running order

1. `grammar_risks` — free, and it changes how you read everything after it.
2. `smoke --repeats 10` — **~$0.47 on gpt-4.1, ~$0.72 on gpt-4o**, at the ~61%
   prompt-cache share ten repeats of one prompt actually get. Gates 1 and 2.
   **Stop here if either fails.**
3. Score US 2019 — the ambiguous window. Criteria 3, 4, 5, 7.
4. Score TR 2018 — the determinate window. Same criteria, plus interim 6.
5. Compare against `gpt-4.1` for discrimination, A′ for ρ.

Print the projection before step 3 and wait for a yes. `bakeoff.projection`
scales a per-snapshot figure to the pilot's 2,092 and the backfill's 25,104, and
it is scorer-only — digests are on top.
