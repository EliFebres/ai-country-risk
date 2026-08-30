# Pipeline audit — is the pipeline ready for a new scorer?

**Run 2026-08-30. Read-only, $0 spent, no model call, and no request to any
upstream source** — not to the Guardian, NYT, IMF, World Bank, BIS, FMP or
OpenAI, so no rate limit was consumed and nothing was written anywhere. The only
network used was read-only connections to the two Neon databases
(`set_session(readonly=True)` on both, so Postgres refuses a write server-side).
Every number below comes from stored rows, committed files, or code read in this
tree. Where a claim could not be established without spending, it is named as
unanswered rather than estimated — see §6.

Written for someone who was not here. The question it answers is narrow: **if
Phase 1 starts tomorrow — frontier open-weight candidates through the existing
bake-off harness against `docs/scorer-acceptance.md` — is the brief the pipeline
hands a candidate correct?**

The short answer is **no, and the reason is one day old**. The vintage fix of
2026-08-29 added roughly nine indicators to every payload, and the two arms
`docs/scorer-acceptance.md` names as the reference were scored the day before
it. Neither file can say so, because every version stamp they carry is identical
on both sides of the fix. §3 has the measurement; §4 has the five things that
must change before a candidate is paid for.

Nothing else found here blocks Phase 1. Several things block the pilot, and they
are kept in a separate list on purpose.

---

> ## Status — updated 2026-08-30, later the same day
>
> **All five blockers in §4 are closed** (commits `bd69dee` … `34eef79`), and
> both references have been re-captured on the fixed payload. **Phase 1 is still
> not clear**, for a reason this audit did not find: closing blocker 4 — the
> smoke gate that ran at a fifth of production context — revealed that the
> determinism figures the acceptance bar was written against were measured on
> the wrong instrument. The production scorer's real repeat spread is **17
> points** against a published 2 and a line of 3.
>
> **§7 is the record of what changed.** Sections 0–6 are left exactly as written
> so the before state survives; where this audit was wrong or under-called,
> §7C says so rather than editing the finding.

---

## 0. Scope, and which database is which

| | `PROD_DATABASE_URL` — the corpus | `DEV_DATABASE_URL` — the pilot |
|---|---|---|
| `article` | 212,994 | 80,485 |
| `indicator_series` | 227,465 | 203,515 |
| `run_ledger` | 6,786 — **harvest only** | 831 — harvest + 157 snapshot + 1 freeze |
| `risk_snapshot` | 157 | 157 |
| `snapshot_diagnostic` | 37 | 37 |
| `llm_artifact` | 1,649 | 1,671 |

`RISK_DB_TARGET=dev`. **The pilot project is the system of record for this
audit** — that is where scoring runs and where a candidate would be measured.
The corpus project is audited for Stage 1 coverage only.

**The snapshot rows on prod with no ledger are intended, and confirmed.** The
Part 3 merge moved `risk_snapshot` and `snapshot_diagnostic` into the corpus DB
and deliberately excluded the snapshot ledger rows, because
`store.completed_runs` reads `status='complete'` and prod would otherwise skip
those anchors as done. The exclusion is recorded in the undo lists:
`merge_run_ledger_20260829T163235.csv` carries 43 harvest + 169 snapshot + 1
`pilot-freeze` rows, and the two later merges (`…163348`, `…163833`) carry **43
harvest rows and nothing else**. The first merge was reverted; prod's
`run_ledger` today holds only `harvest`.

Its consequence is real and is on the second list, not the first: those 157 rows
are invisible to `completed_runs()` on prod, so a pilot run pointed there would
**re-score them rather than skip them**. Correct today; wrong the moment prod is
re-scored on a final payload.

`MEMORY.md`'s `which-neon-db-is-which` entry is out of date — it records prod as
having "never produced a snapshot" and dev as holding 7 countries. Both halves
have moved.

---

## 1. Stage-by-stage table

Three columns, kept distinct. **Unverified is not fine.**

### Stage 1 — Data fetching

**Verified working.** **NYT**: 48/48 countries, 140 windows each,
`2015-01-01 → 2026-08-01`, **zero failed checkpoints** on prod. **WEO**: 21
editions present, `2016-04` through `2026-04`, each stamped with its own edition
date — `deferred.md` §4's "13 of 19 recovered" describes a fresh-clone
re-fetch, not the stored archive, which is complete. **IMF CPI**: 46 of 48
countries, 6,769 rows, latest period `2026-07` — §17's "7 of 48" is
**resolved**; the weekly job half-converged as that item predicted. **BIS**: XRU
48 countries, CBPOL 32 (BIS publishes 32; not a failure). The restamp migration
ran, with its backups on disk.

**Broken.** **Guardian has never been attempted for 42 of 48 countries.**
`config.HARVEST_ROSTER` is all 48; `run_ledger` holds Guardian checkpoints for
six — KR, PT, TR, US (13 windows each), BR (2 done / 11 failed), ID (1). The
other 42 are not `failed`, they were never attempted, so nothing retries them.
Since **NYT supplies zero bodies** (0 of 131,992 rows), those 42 countries have
abstract-only corpora with no article text at all. **28 failed Guardian
windows, never retried** — table below. **`curated.csv` is a bare header line**:
13 of 38 registry codes have zero rows on both databases; 25 codes are stored.
**Structural facts cover 5 of 48 countries** (BR, KR, PT, TR, US), so 43
countries get no structural block under masking.

**Unverified.** Whether the NUC cron is *currently* healthy. The wrappers live
on the host in `/home/minipc/bin/`, not in this repo, so nothing here can assert
a missed tick. The only in-band evidence is a `run_ledger` row completed
**2026-08-30 16:00:19** — so something ran today — and it failed with
`"note": "quota exhausted"`. Nothing anywhere reports a run of consecutive
failed checkpoints; §20a says exactly this and it is still true.

**The 28 failed Guardian windows, in full.** No retry and no probe was run — the
NUC shares the Guardian key and its allowance is not audit budget.

| DB | country | windows | recorded note | completed |
|---|---|---|---|---|
| prod | BR | 2016-01-01 | **`quota exhausted`**, 1 call, 1.8 s | **2026-08-30 16:00** |
| prod | BR | 2016-08-03, 2017-01-01, 2018-01-01, 2020-01-01, 2021-01-01, 2022-01-01, 2023-01-01, 2024-01-01, 2025-01-01, 2026-01-01 | `request error`, 1 call, ~20 s each | 2026-08-15 23:48–23:52 |
| dev | BR | 2016-08-03 … 2026-01-01 (10 windows) | `request error`, 1 call, ~20 s each | 2026-08-15 23:48–23:52 |
| dev | KR | 2023-01-01, 2024-01-01, 2025-01-01, 2026-01-01 | `request error`, 1 call, ~21 s each | 2026-08-15 23:47–23:48 |
| dev | US | 2024-01-01, 2025-01-01, 2026-01-01 | `request error`, 1 call, ~20 s each | 2026-08-15 23:52–23:53 |

Each window runs to its year end (`window_end` in the ledger detail), and every
one wrote zero items.

**Two distinct failure patterns, and they want different responses.** The 27
rows from 2026-08-15 are all `request error` at ~20 seconds having spent one
call — the transient-timeout signature §20a diagnosed on BR and cleared with a
free retry. The single prod row from **today** is different: `quota exhausted`
in 1.8 seconds, which is the daily wall arriving, not a transient. So a retry
started today, got one window in, and stopped. The rest is a repair to run from
the NUC across two or three days' allowance (§14: the allowance is not a
constant — 1,461 page-calls on 2026-08-15, 328 on 2026-08-28).

### Stage 2 — Processing and storage

**Verified working.** **Vintage stamping is correct for three of the four annual
paths.** Each WEO edition carries `as_of` = its own edition date (`IMF WEO
2019-04` → `2019-04-01`; 21 editions, exactly one `as_of` value each). BIS and
IMF monthly prints are back-dated by publication lag. The chokepoint guard added
in `fd3fa8d` re-dates any row stamped implausibly late.
**Idempotency**: every `INSERT` in `data_push.py` and `store.py` carries an `ON
CONFLICT` clause — 20 across 15 targets. Nothing found is unsafe to re-run.
**The `source_system` merge (§18) has never fired**: zero URLs appear under more
than one `source_system` on either database, confirming the branch is latent
exactly as that item says.

**Broken.** **The fourth fetcher still leaks (§24), and the data shows it.**
`World Bank panel` rows have a minimum `as_of` of **1789-12-31** —
`country_data_fetch.panel_rows` stamps `as_of = min(31 December of the value's
own year, today)`, so a WGI 2018 score claims to have been public on 2018-12-31
when WGI publishes around September 2019. `World Bank WDI` has a maximum `as_of`
of **2026-08-28**, the `today` cap, giving an `as_of` earlier than its own
period end. Both are deliberately outside the chokepoint guard and pinned by
`test_invariants.py:485`; the exemption is recorded, not accidental.
**Body clipping is unchanged**: 8,925 of 80,975 Guardian bodies on prod
(**11.02%**) and 6,165 of 53,368 on dev (**11.55%**) are exactly 24,000
characters — `core.MAX_BODY_CHARS`. `content_sha256` hashes the clipped body, so
the hash identifies "the first 24k of this article", and the manifest does not
say so (§23, still true).

**Written and read by nobody.** **Four columns with no reader anywhere.**
`friction_score`, `order_uncertainty_score`, `information_score` and
`edge_vitality` are written on every snapshot (157/157 populated) and are read
by **no backend module and no frontend query** — `frontend/app/lib/risk-server.ts`
never names them. They duplicate the contents of `risk_snapshot.ledger_scores`,
which stores `{ledger_scores, subscore_evidence}` as a deliberate wrapper; so
`subscore_evidence` is stored twice.
**`legal_gate` and `lint` are 0/157** — never populated on any stored row,
though `lint` has eight backend readers and one frontend one.
**`trend_1y`/`trend_5y` (§25) still have no consumer on the production
contract**: `payload.py:463` writes them onto every indicator, `payload_health`
counts them, and the only thing that *reads* them is `llm/trend.py` under
`PAYLOAD_VARIANT=p4-trend` — a measured and rejected arm. Under p2, which is
what production and every reference arm run, they are serialized into every
prompt and named by nothing in `AI_PROMPT_V3`.

Two small things, recorded and not pursued: four `content_sha256` values are
each shared by two rows (same country, same source — duplicate content under
different URLs), and one NYT article on prod is dated **2007-07-07**, nine years
below `HARVEST_FLOOR`.

### Stage 3 — Assembling the payload

**Verified working.** **Look-ahead: zero violations.** Across all 157 stored
snapshots, no article in any `input_manifest` has `published_at >= as_of`, and
the age of every selected article is between **1 and 30 days** before its
anchor, for all three countries. The 30-day bound holds exactly.
**`payload_health` runs on every scoring path** — `pipeline._process_country:581`,
which both the live run and the backfill go through — **and it fires**: starving
the four `information` codes at TR 2018-08-13 produces
`empty_ledgers: ['information']` and `information: {expected: 4, resolved: 0}`.
A ledger resolving zero today would be caught.
**The probe's identifiability baseline is current, not superseded**: all 25
stored probe rows carry variant `g5:9f4aee55:gpt-4o-mini-2024-07-18:d089c696`,
and this tree is `MASK_MAP_VERSION=g5`, `SWEEP_VERSION=9f4aee55`.
**Country masking holds.** Scanning the three dispatched prompts in §8 for
names, demonyms, capitals, cities, currencies, statutes and named operations
across the roster returned nothing: no `Turkey`/`Turkish`/`Ankara`/`Istanbul`/
`Erdoğan`/`lira`, no `America`/`Washington`, no `Olive Branch`/`Afrin`.
`assert_clean` passed on all three. The one hit for `Central Bank` is the
prompt template's own worked example ("Australia Central Bank Holds Rates
Steady"), which is an instruction rather than evidence about anyone.

**Broken.** **Outlet identity is disclosed on every article, on every anchor.**
Each digest entry carries `"source": "guardian"` verbatim — 12 to 15 occurrences
per prompt. On US 2019-03-11 the Guardian's own footer boilerplate reaches the
model inside the full-text block: *"Join the debate – email
guardian.letters@theguardian.com • Read more Guardian letters – click here to
visit gu.com/letters • Do you have a photo you'd like to share with Guardian
readers?"*. `rewrite.assert_clean` scans for roster terms, not for publishers,
so this is outside the gate by construction. It is the "outlet fingerprinting"
half of the comparison test deleted in §7 of `deferred.md`.

**The selector's behaviour on PT is worse than §34 records.** Measured over
every anchor of each window, by re-running the real `snapshot_select` path:

```
                candidate pool     clearing 0.3     SELECTED   topped up    mean selected relevance
TR 2018 (53)   88 / 125 / 243    11 /  32 /  69    18–20      10 of 53    0.304 – 0.750  (spread 0.446)
US 2019 (52)  321 / 402 / 465    52 /  76 /  98    20 always   0 of 52    0.471 – 0.552  (spread 0.081)
PT 2019 (52)   39 /  56 /  96     2 /   6 /  16    20 always  52 of 52    0.120 – 0.394  (spread 0.274)
                min/med/max        min/med/max
```

**PT is topped up below the threshold at every single anchor of the year.** The
median anchor has six articles clearing 0.3 and is filled to twenty with
fourteen that do not; at the worst anchor the mean relevance of the twenty the
model scores is **0.120**, well under the 0.3 bar. US is the saturation §34
describes — the pool is large, nothing is topped up, and mean relevance moves by
0.081 across a whole year, so which twenty a US anchor sees is close to
arbitrary. Only TR has evidence weight that actually varies.

Worth putting beside the round-number shares, computed here from the stored
rows: **US 69.2%, TR 18.9%, PT 84.6%**. The first two reproduce
`docs/scorer-acceptance.md` §3 exactly, which is a useful cross-check on both.
**PT's 84.6% has never been reported**, and PT is both the thinnest-evidence
window and the one `GATE2_BASELINE` was captured on. Evidence that is genuinely
indeterminate and evidence that has been flattened by a saturated selector look
identical from inside the prompt — §34 raises that as a possibility, and PT is
the strongest case for it yet measured.

**Unverified → now answered: the `PILOT_START` lead-in, and it differs by
database.** `PILOT_START = 2016-08-03`, `SNAPSHOT_WINDOW_DAYS = 30`. The 30-day
window below the first anchor (`2016-07-04 → 2016-08-02`) holds, **on prod**:
US 556, TR 281, PT 196, KR 55, BR 29 articles — comfortable. **On dev, the
database scoring actually runs against**, the same window holds **US 3, TR 7,
PT 1, KR 1, BR 5**, and the earliest article anywhere on dev is 2016-08-01. So
`deferred.md` §22 is **resolved on prod and still open on dev**. The suite's
`TestTheFirstAnchorsHaveTrailingCorpusBeneathThem` does check a version of this,
but only for the p3 trailing-context block — a rejected variant — and it asserts
against the `HARVEST_FLOOR` constant, not against stored rows. Nothing checks
the 30-day window against the corpus.

**Are the invariant tests running against real assemblies or fixtures?** **Both,
and the split is deliberate.** `test_invariants.py` is 1,551 lines. The
look-ahead and vintage classes (`TestNoFutureSurvivesAssembly`,
`TestSurvivesTheVintageBound`, `TestTheVintageRuleInThePayload`) call the
**real** `payload.build_evidence_payload` and the real `snapshot_select` window
logic against hand-built rows — real code, synthetic data. The idempotency and
body-status classes below line 1,153 need a real Postgres and **skip unless
`HISTORY_TEST_DATABASE_URL` is set**; a bare `pytest` skips 19 tests, which is
what happened here (737 passed, 19 skipped). So the invariants are enforced
against real assembly code and never against the real corpus. The zero-violation
result above is this audit's own query, not a test that runs.

### Stage 4 — The prompt and the scoring call

**Verified working.** **The committed prompt is the one production sends, and
the smoke gate now sends it too.** `bakeoff.smoke_prompt` renders
`ai_constants.AI_PROMPT_V3` and appends the rule blocks through
`langchain_llm._prompt_rules_and_version` — the same resolver
`country_llm_score` uses, so a prompt variant cannot certify a contract the run
would not render. The gap closed in `0f5a27b` and `0ee8e75` has no remainder
found here. Verified by capture rather than by reading: the prompts in §7 were
taken by replacing `ai_client.build_chat` with a recording stub inside the real
`country_llm_score`, so they are the exact bytes that would have gone out.
**`grammar_risks(RISK_SCHEMA_V3)` reports 21 constraints** a context-free
grammar cannot express — every `minimum`/`maximum` on the four ledgers, on
`impact`, `score_3m`, `score_12m` and `evidence_coverage`, the four
`["integer","null"]` unions, and `bullet_summary`'s `maxLength: 800`. LangChain
forwards them under `strict: true` and they are not in the enforced subset, so
production carries the same hole; `_from_100`'s clamp is what actually holds the
line. **Timeouts are adequate**, though by accident rather than by decision:
`client._chat` sets no timeout, so the effective values are openai-python
2.48.0's defaults — `connect=5 s, read=600 s`. Ten minutes is enough for a
self-served model at multi-minute latency.

**Broken.** **A schema violation leaves no record on the scoring path.**
`langchain_llm._from_100` does `max(0.0, min(1.0, float(value)/100.0))` and
returns `None` on anything non-numeric. A `score_12m` of 250 becomes 1.0; a
garbage value becomes `None`. Nothing logs it, nothing stores it, no column and
no manifest field counts it. The only place a bound is *checked* is
`bakeoff._validate_locally` in the smoke gate — nine calls on canned payloads.

**The freeze cannot see the change that matters.** `score.FROZEN_FIELDS` is nine
names: `SWEEP_VERSION, REWRITE_VERSION, GAZETTEER_VERSION, MASK_MAP_VERSION,
PROMPT_VERSION, PAYLOAD_VERSION, SCORING_MODEL, DIGEST_MODEL, SEED`. `git_sha`
is **not** among them. Running `score.drift()` with the stored `pilot-freeze`
detail (2026-08-28 01:50, `git_sha ed4c845`) against `score.versions()` today
returns **`{}`** — a resume would be **allowed, with no warning**, onto a
payload carrying nine more indicators per country than the 157 rows already
stored. §3 is the measurement.

**Determinism as it stands today, per band, for the incumbent.** Re-derived
2026-08-29 and stored in the `gates` block of all four `gpt-4o` / `gpt-4.1` arm
files, per-repeat draws included. Read off those files:

| band | `gpt-4o` | `gpt-4.1` |
|---|---|---|
| calm | spread **0**, but `edge_vitality` alternates `60` / `null` across ten identical calls | spread 0 |
| moderate | spread **0**, exact 10/10 — the published result, and the only payload it had ever been measured on | spread 2 |
| stressed | spread **2**, `score_12m` returns 90 once in ten | spread 0 |
| **worst** | **2** | **2** |

The `edge_vitality` null-alternation on the calm anchor is confirmed present in
the stored draws. Note that the `gates` block is byte-identical between
`US-2019/` and `TR-2018/` for a given candidate: `smoke` runs the three canned
`_SMOKE_BANDS` payloads and is window-independent, so a gates block describes the
instrument and never the window.

**Prompt / payload consistency after the vintage fix restored ten indicators.**
**Consistent, and one thing to watch.** The prompt's calibration anchors, five
bands and observation-only framing describe *ledgers*, not individual
indicators, so restoring ten indicators changes what the ledgers are computed
from without contradicting anything the prompt asserts. The one live mismatch is
the reverse of the brief's worry: `AI_PROMPT_V3` explains `as_of` and
`staleness_days`, and **still never mentions `trend_1y`/`trend_5y`**, which are
on every indicator in every payload. That is §25, unchanged.

### Stage 5 — Ready for a candidate?

**Verified working.** **Every criterion in `docs/scorer-acceptance.md` computes
against a stored row today, except the one the document itself marks
provisional** — computed in this audit, results in §2. **`local-template` is
correctly excluded** from `compare_all` (`bakeoff.py:1807`), so the template
cannot be mistaken for a candidate. **Cost is answerable without a vendor
price**: `usage.is_priced` is False for an unlisted model, `cost_summary` then
withholds dollars and reports `input_tokens_per_snapshot`,
`output_tokens_per_snapshot` and `seconds_per_snapshot`. **Concurrency is safe
for a self-served scorer**: the only `.batch()` in the tree is
`digest_engine`'s, at `_MAX_CONCURRENCY = 8`, and it runs on the *digest* model.
The scoring path is one call per anchor, serial. Pointing `SCORING_BASE_URL` at
a rented instance leaves digests on the hosted mini model and sends the local
endpoint no concurrency at all.

**Broken.** **The reference arms are stale and cannot say so** — §3. **The two
hard gates are measured at a quarter of production context**: `smoke_prompt`
renders `full_text_block="(no full-text articles supplied)"` unconditionally, so
the gate prompt is **2,962 / 2,980 / 2,987 tokens** for calm / moderate /
stressed. The prompt the run actually dispatches is **11,264 / 12,734 / 13,459
tokens** (measured, §8). Gates 1 and 2 — the two the acceptance doc says to
*stop on* — exercise ~23% of the context a candidate will really be asked for.

**Unverified.** Whether `max_retries = 0` is right for a self-served endpoint.
It is a deliberate choice for a hosted API (callers degrade, and
`completed_runs` retries anything not `complete`), but a rented instance that
503s while loading weights, or refuses a connection inside the 5-second
`connect` budget, loses that anchor to a `failed` ledger row with no retry.
Unmeasurable without an endpoint to point at.

**Adding a candidate today: the concrete walk-through.** ✓ marks a step that is
already a template; the rest is the work.

1. ✓ Start any OpenAI-compatible server. `docs/scorer-acceptance.md` has the
   `llama.cpp` and vLLM command lines.
2. ✓ Copy `local-template` in `bakeoff.CANDIDATES`, change `SCORING_MODEL` and
   `SCORING_BASE_URL`, set `SCORING_LOCAL_KEY` in `backend/.env`. `arm` must be
   `scoring`; `test_util.TestEveryCandidateIsAScorer` enforces it.
3. ✓ `grammar_risks` — free, 21 lines, and it changes how everything after it
   reads.
4. ✓ `bakeoff smoke <name> --repeats 10`. The route falls back to `json_object`
   automatically and is recorded. **Not a template: the gate runs at ~3k
   tokens.** Nothing in the harness will smoke a candidate at the context the
   run uses.
5. ✓ `bakeoff score <name> --country US …` and `--country TR …`, then `bakeoff`
   to render.
6. **Not a template: the comparison.** `compare_all` will put the candidate
   beside `gpt-4.1` and `gpt-4o` for discrimination and prompt compliance, and
   beside `p2-rebaseline` for ρ. The first two were scored on a different
   payload from the one the candidate will get, and nothing in the render says
   so.
7. **Not a template: nothing prints the evidence depth of either side.**
   `payload_health` is computed on every scoring run and lands in
   `input_manifest`, but **no bake-off arm row carries it**. An arm row is
   exactly `articles, as_of, cached_tokens, calls, condition_flags, error,
   input_tokens, ledger_scores, lint, llm_score, model_id, offpeak_usd,
   output_tokens, score_3m, seconds, spend_usd, status, utc_hour` — and nothing
   else.

---

## 2. Every acceptance criterion, computed

Run against the committed arms in this audit, at $0. This is the dry run
`docs/scorer-acceptance.md` asks for in its own opening. Candidate = `gpt-4.1`,
baseline = `p2-rebaseline` (A′).

| criterion | US 2019 | TR 2018 | computable? |
|---|---|---|---|
| **1 Schema adherence** | `route: strict`, `passed: true`, `error: null` | same block | ✅ from `gates` |
| **2 Determinism** (worst band ≤ 3) | calm 0 · moderate 2 · stressed 0 → **worst 2** | identical (window-independent) | ✅ |
| **3 Round share** (≤ 40%) | **5.8%** | **3.8%** | ✅ `series_shape` |
| **4 Discrimination** | 18 distinct · 5.8% round · longest run 2 · **3 bands** (LowMod 6 / Mod 43 / High 3) | 13 distinct · 3.8% · run 2 · **2 bands** (Mod 26 / High 27) | ⚠️ see below |
| **5 Cost**, cache-neutral | **$0.030872** / snapshot | **$0.029913** / snapshot | ✅ `cache_neutral_per_snapshot`, `priced: true` |
| **6 Event validity** | — | — | ❌ **PROVISIONAL by the document itself.** Needs a dated event list from a source independent of the payload, for two or three countries. No such list exists in the repo. |
| **7 ρ vs A′** (≥ −0.10) | **passed**, worst gated **0.4437**. Gated: `llm_score` 0.4937, `score_3m` 0.4437, `order_uncertainty` 0.5293. Excluded as too coarse: `friction` (4 distinct), `information_capacity` (3), `edge_vitality` (2) | **passed**, worst gated **0.4078**. Gated: `llm_score` 0.7025, `score_3m` 0.8589, `friction` 0.4078, `order_uncertainty` 0.4115, `information_capacity` 0.7380. Excluded: `edge_vitality` (3 distinct, **ρ = −0.1644**) | ✅ `rho_gate` |

For reference, the same shape statistics for the other two named arms:

| window | arm | distinct | round share | longest run | bands occupied |
|---|---|---|---|---|---|
| US 2019 | `gpt-4.1` | 18 | 5.8% | 2 | LowMod 6 · Mod 43 · High 3 |
| US 2019 | `gpt-4o` | 9 | 69.2% | 5 | **Moderate 52** |
| US 2019 | `p2-rebaseline` (A′) | 8 | 76.9% | 4 | **Moderate 52** |
| TR 2018 | `gpt-4.1` | 13 | 3.8% | 2 | Mod 26 · High 27 |
| TR 2018 | `gpt-4o` | 9 | 18.9% | 7 | Mod 39 · High 14 |
| TR 2018 | `p2-rebaseline` (A′) | 9 | 26.4% | 5 | Mod 30 · High 23 |

**Criterion 4 has one component its named computer does not compute.** §4 of the
acceptance doc says "Distinct composite values, round-number share, bands
occupied, and the longest run … `bakeoff.series_shape` computes it."
`series_shape` returns `n, distinct, lag1_autocorr, longest_run, round_share` —
**no `bands`**. The bands column above was produced by calling `bakeoff.band()`
per row, which is one line and correct, but it is not what the document says.
Same family as §28 and §32: a criterion component whose stated writer does not
write it. Trivial to close; recorded because that family is why this document
exists.

**One thing worth sitting with in the ρ table.** On TR 2018 `edge_vitality`
correlates at **−0.1644** against A′ — below the −0.10 floor — and is excluded,
correctly, for having three distinct values. So the ledger the vintage bug
starved hardest is also the one the ρ gate declines to check, on the grounds
that it is too coarse to rank. Both facts have the same cause.

---

## 3. The finding that blocks Phase 1

### What happened

The vintage fix landed on **2026-08-29** — `f8db7d7` at 10:31, `d4a1017` at
10:32, `fd3fa8d` at 11:18. It re-dated stored `indicator_series` rows from fetch
date to publication date and closed the root cause at the upsert.

The arms `docs/scorer-acceptance.md` names as the reference were scored
**before it**.

| role in the acceptance doc | arm file | scored under | when | vs. the fix |
|---|---|---|---|---|
| **Benchmark incumbent** — the discrimination and prompt-compliance reference | `{US-2019,TR-2018}/gpt-4.1.json` | `d063fc4` / `30e07ef` | 08-27 22:26 / 23:44 | **before** |
| **Production scorer** reference | `{US-2019,TR-2018}/gpt-4o.json` | `d063fc4` / `30e07ef` | 08-27 | **before** |
| **ρ disaster detector** — A′ | `{US-2019,TR-2018}/p2-rebaseline.json` | `2bd63a2` / `e3dc94c` | 08-29 16:12 / 16:20 | **after** |
| the whole stored pilot series | `risk_snapshot`, 157 rows | `ed4c845`, `3c0fa16`, `f04da68` | 08-28 01:52 → 16:40 | **before** |

### What the difference actually is

Measured, not inferred. The restamp's own backup
(`backend/data/backups/indicator_series_20260829T102920.csv`, 11,698 moved rows)
was used to put every `as_of` back where it was, and the payload rebuilt at each
anchor through the real `build_evidence_payload`:

| anchor | payload the pre-fix arms saw | payload a candidate gets today |
|---|---|---|
| **TR 2018-08-13** | **15 / 38** — friction 5/14, uncertainty 10/16, **information 0/4, edge 0/4** | **24 / 38** — friction 10/14, uncertainty 11/16, information 1/4, edge 2/4 |
| **US 2019-03-11** | **15 / 38** — friction 5/14, uncertainty 10/16, **information 0/4, edge 0/4** | **23 / 38** — friction 10/14, uncertainty 10/16, information 1/4, edge 2/4 |
| **PT 2019-06-03** | **14 / 38** — friction 5/14, uncertainty 9/16, **information 0/4, edge 0/4** | **22 / 38** — friction 9/14, uncertainty 9/16, information 1/4, edge 3/4 |

**Two of the four ledgers the acceptance bar gates on had no indicator evidence
at all** when the benchmark incumbent scored them. `information_capacity` and
`edge_vitality` in `gpt-4.1.json` and `gpt-4o.json` are the model's reading of
twenty articles and nothing else. A candidate scored tomorrow gets one and two
or three indicators under those same ledgers, and is compared against numbers
produced without them.

### Why nothing detects it

Both sides declare `PAYLOAD_VERSION: p2`. `PAYLOAD_VERSION` names the payload
*contract* — which blocks exist — and not its *content*, so restoring nine
indicators moved no stamp. All nine `FROZEN_FIELDS` are identical across the
fix, which is why `score.drift()` returns `{}` and a resume is allowed.

And the one stamp that *would* have shown it was overwritten. `git_sha` sits in
`captured_under` but not in `FROZEN_FIELDS`, and commit `b128aad` (08-29 22:36)
added the re-derived `gates` block to `gpt-4.1.json` and `gpt-4o.json` — a
gates-only write, the 52 anchor rows untouched, 481 insertions and 2 deletions —
and rewrote the version block with it:

```diff
-    "git_sha": "d063fc4fc9a57bf79ae4ba89a288d1e6df06a1ee"   <- the tree that scored the rows, 08-27
+    "git_sha": "b47b2b2b4341cfcbec5d1323bbc7949b4e01d14a"   <- the tree that added the gates, 08-29 22:24
-  "gates": {},
+  "gates": { … 481 lines … }
```

So the two reference files now name a **post-fix** tree for **pre-fix** rows.
Read at face value, `captured_under` says the benchmark incumbent and A′ were
scored under the same payload. They were not.

This is the exact shape `deferred.md` §27 names for `GATE2_BASELINE` — "the
file's 'Captured under' block records nine version stamps and none of them
moved, which is precisely the problem" — reproduced in the reference arms
themselves, and made worse by a stamp that moved in the wrong direction.

### What it would do to Phase 1

Criterion 3 compares a candidate's round-number share against `gpt-4.1`'s 5.8% /
3.8%. Criterion 4 compares distinct values, bands and run length against
`gpt-4.1`'s 18 / 3 bands / run 2. Criterion 7 compares ρ against A′. The first
two references are on one payload, the third is on another, and the candidate
will be on the third. **Any difference a screen measures on criteria 3 and 4 is
the vintage fix plus the candidate, with no way to separate them** — and the
direction is unhelpful: a candidate that resolves better because it has nine
more indicators to work with will read as a better scorer.

---

## 4. Must change before Phase 1 — ranked

Strict. Each entry says how leaving it would corrupt a screen.

### 1. Re-score the two reference arms on the current payload, or stop calling them the reference

`{US-2019,TR-2018}/gpt-4.1.json` and `gpt-4o.json` — 105 anchors each.
**How it corrupts a screen:** criteria 3 and 4 are measured against them, and
they saw 15 of 38 indicators with two ledgers empty while the candidate sees
23–24 with those ledgers populated. Every discrimination and prompt-compliance
verdict becomes a comparison between two payloads. This is the one item that
cannot be worked around by reading more carefully — the numbers are not
comparable, and they are the numbers the gates are written on.

The cheaper alternative, if the re-score is not affordable: promote **A′
(`p2-rebaseline`)** to benchmark incumbent for criteria 3 and 4 as well as 7. It
is on the current payload, it is `gpt-4o`, and its figures are already computed
(US: 8 distinct, 76.9% round, 1 band; TR: 9 distinct, 26.4%, 2 bands). That
lowers the discrimination bar to the incumbent's — which §11 explicitly did not
want, on the argument that "a bar set by what we happen to ship is not a bar" —
but it is at least a bar measured on the brief the candidate is handed. Say
which was chosen in the write-up either way.

### 2. Make a payload-content change visible to the freeze

`PAYLOAD_VERSION` did not move when the payload gained nine indicators, so
`drift()` returns `{}` and a resume proceeds silently. **How it corrupts a
screen:** it is why (1) happened, and it will happen again on the next payload
change — including any change made in response to this document. The durable
form already exists and is not being used at the comparison layer:
`payload_health.indicators.resolved` is computed on every scoring run. Carrying
it onto the bake-off arm row, and refusing a comparison between arms whose
resolved counts differ, would have caught this before a dollar was spent.
Bumping `PAYLOAD_VERSION` when content moves is the blunter version and also
works.

### 3. Stop `captured_under` being rewritten by a run that scored nothing

A gates-only write moved `git_sha` from the scoring tree to the gates tree on
both reference files. **How it corrupts a screen:** it destroys the only field
that could have distinguished (1), and it does so silently, inside a commit
whose message is about something else. A version block should be written once,
by the run that produced the rows, with any later block appended beside it
carrying its own stamp.

### 4. Smoke the candidate at production context, not a quarter of it

Gates 1 and 2 run at ~2,980 tokens; the dispatched prompt is 11,264–13,459.
**How it corrupts a screen:** the acceptance doc says "stop here if either
fails", so these two gates decide whether a candidate is screened at all. For a
self-served model they test precisely the properties that degrade with context —
grammar-constrained decoding gets harder, and determinism under batching and
KV-cache pressure is a different question at 13k than at 3k. A candidate that
passes at 3k and fails at 13k is admitted and then disqualified after the
expensive part. `_SMOKE_BANDS` already holds three payloads; giving them a
realistic `full_text_block` is the whole change.

### 5. Give a schema violation somewhere to land on the scoring path

`_from_100` clamps to [0,1] and returns `None` on garbage, with no log, no
column and no manifest field. **How it corrupts a screen:** criterion 1 is "zero
invalid outputs **over the anchor set**, and the route is recorded". Over the
anchor set, an out-of-range score is currently unobservable — it is clamped into
a plausible value before anything sees it. The criterion is really only measured
by the smoke gate's nine calls. On a local endpoint with no strict mode and 21
unenforceable bounds, that is exactly the wrong place for the blind spot.

---

## 5. Should change, but does not block Phase 1

Kept separate on purpose. Each is labelled with the gate it actually blocks.

| # | finding | blocks |
|---|---|---|
| 1 | **The 28 failed Guardian windows**, and the fact that nothing reports a run of failed checkpoints. `reports.harvest_pacing` already reads the ledger. The retry belongs on the NUC where the quota lives; today's `quota exhausted` row says one has started. | the backfill |
| 2 | **Guardian never attempted for 42 of 48 countries.** With NYT supplying zero bodies, those countries are abstract-only — the condition §20a says makes BR fail all five theme floors. `HARVEST_ROSTER` is all 48 and expects them. | the backfill, and any roster-wide scoring |
| 3 | **The 157 prod snapshot rows have no ledger.** Intended by the merge (§0), but a pilot run against prod would re-score them instead of skipping them. Correct today; wrong the moment prod is re-scored on a final payload. | the pilot |
| 4 | **The dev corpus has no `PILOT_START` lead-in.** The 30-day window below 2016-08-03 holds 1–7 articles per country on dev against 55–556 on prod. The first year of anchors would be scored on almost nothing. | the pilot |
| 5 | **PT is topped up below threshold at 52 of 52 anchors**, median 6 of 20 clearing 0.3, mean selected relevance as low as 0.120 — alongside an **84.6%** round-number share, the worst of the three windows and previously unreported. §34's "the selector tops up with noise" is understated for PT. | the pilot, and any claim about PT |
| 6 | **Outlet identity reaches the model on every article** — `"source": "guardian"` in every digest entry, plus Guardian letters boilerplate in the US full text. Constant across candidates, so it does not corrupt a comparison; it does mean the identifiability probe is partly reading the newspaper. §7's deleted comparison test is what would catch a regression here. | nothing yet; the masking claim |
| 7 | **`curated.csv` is still empty** — 13 registry codes with no rows, `information` scoring on one indicator and `edge` on two or three. §3, unchanged, and now visible in the §8 dumps. | the instrument's credibility, not a screen |
| 8 | **Four unread ledger columns** (`friction_score`, `order_uncertainty_score`, `information_score`, `edge_vitality`), `legal_gate` and `lint` at 0/157, and `trend_1y`/`trend_5y` still unnamed by the prompt. Further instances of the pattern. | nothing |
| 9 | **`series_shape` does not return `bands`** though §4 of the acceptance doc says it computes it. One line. | nothing |
| 10 | **`GATE2_BASELINE` was captured pre-fix** (§27) and is now doubly stale: it is PT, the window with the thinnest evidence and the highest round share. | any regression check against it |
| 11 | **`MEMORY.md`'s DB entry is wrong** on both halves — see §0. | nothing |
| 12 | **§4 (WEO 13 of 19) and §17 (IMF CPI 7 of 48) are stale** and should be closed: the stored WEO archive is complete at 21 editions, and IMF CPI now covers 46 of 48 countries with 6,769 rows. | nothing |

---

## 6. What could not be verified without spending

Named explicitly, so the gaps are visible rather than assumed away.

1. **Determinism of any open-weight candidate.** Not measurable here at all, and
   the acceptance doc is right that it does not transfer: rewriting the four
   union types as `anyOf` — mechanically equivalent JSON Schema — made `gpt-4o`
   non-deterministic on demand. Every candidate must be measured on its own
   serving stack. Phase 2's whole point.
2. **Re-measured determinism for the incumbent on the *fixed* payload.** The
   three-band matrix in the `gates` blocks was captured on `_SMOKE_BANDS`, which
   are canned payloads and therefore unaffected by the vintage fix — so those
   numbers survive. But nothing has re-measured repeat spread on a *real*
   post-fix anchor, and §27's warning applies. ~$2, not spent.
3. **Whether the four prose-only noise-floor rows** (`gpt-5.4-mini` 7,
   `gpt-4.1-mini` 8, `gpt-5.6-luna` 11, `gpt-4.1-nano` 20) hold on three bands.
   `7800836` argues each is already a lower bound far above the line, so
   re-running could not change a verdict. Not spent, and agreed.
4. **Whether the failed Guardian windows answer today.** Deliberately not
   probed — the NUC shares the key. The ledger says 27 windows failed with a
   ~20-second `request error` (the transient signature) and one with
   `quota exhausted`. Which is which cannot be settled without a call.
5. **Whether the NUC cron is currently healthy.** The wrappers are not in this
   repo. The only in-band signal is one ledger row from today.
6. **Real latency, throughput and cost per snapshot on rented hardware.**
   Phase 2. What this audit contributes is the measured input size, so those can
   be sized against a number rather than an estimate: **11,264 / 12,734 / 13,459
   tokens** at the crisis, ambiguous and quiet anchors respectively
   (`o200k_base`, measured on the exact dispatched prompt). Output is ~650–700
   tokens per snapshot from the stored arm rows. `payload._CHARS_PER_TOKEN = 4`
   is a good estimate — the measured ratio is 3.95–4.08 — but note that
   `_TOKEN_BUDGET = 2800` governs the **evidence block only** and is not a bound
   on the prompt; the full-text block is roughly three quarters of what is sent.
   A context window of 16k is the floor; 32k is the sane target.
7. **Whether `max_retries = 0` and `connect = 5 s` survive a rented endpoint.**
   Needs an endpoint.

---

## 7. What happened next — the session that acted on this

**Written 2026-08-30, hours after the audit above.** Sections 0–6 are the state
of the pipeline when the audit ran and are deliberately unedited. This section
records what changed, what this audit got wrong, and what came out of the work
that the audit had no way to see.

### 7A. The five blockers, closed

| # | blocker | closed by | how it is now guarded |
|---|---|---|---|
| 1 | reference arms stale, on a degraded payload | `34eef79` | `gpt-4.1-postfix` and `p2-rebaseline-postfix`, both windows, on the current payload and prompt |
| 2 | a payload-content change invisible to the freeze | `e8b7260` | `payload.content_fingerprint` on every row and in `FROZEN_FIELDS`; `compare_one` refuses a cross-fingerprint comparison |
| 3 | `captured_under` rewritable by a run that scored nothing | `bd69dee` | one write chokepoint holds an existing stamp; the four SHAs restored from `git show b128aad^:` |
| 4 | the gate ran at ~23% of production context | `f73feca` | three pinned anchors, assembled from cache, **no fallback** — and the gate records its own realised token count |
| 5 | a schema violation left no record | `ad5a9fb` | `jsonschema` validation after decoding and before the rescale, into `payload_health.schema_violations` |

Each has a consumer-side test. The fingerprint is verified against the restamp's
own backup rather than asserted: rebuilding PT 2019-06-03 on both sides of the
vintage fix reproduces §3's numbers exactly — 14 of 38 indicators before, 22
after, `p2` on both sides, and the fingerprint moves.

Two further blockers named in §4 were prompt hygiene and landed before the
re-score, in `d604048`: the `"source": "guardian"` field is gone from every
article entry, and publisher boilerplate is stripped at
`digest_engine.article_input_text`. `PROMPT_VERSION` moved to
`v4.5-no-publisher`, which is the point — unlike the vintage fix, this contract
change is visible to `drift`, to `captured_under` and to `compare_one`.

### 7B. A sixth blocker, and closing the fourth is what found it

The gate now sends what the run sends. Measured at that size, on real assembled
payloads, ten repeats, `temperature=0`, `seed=42`, scored fields only:

| model | role | canned worst (2,980 tok) | **real worst (~12,570 tok)** | real by band |
|---|---|---|---|---|
| `gpt-4o` | **production scorer** | 2 | **17** | 5 / 17 / 0 |
| `gpt-4.1` | benchmark incumbent | 2 | **9** | 4 / 9 / 0 |

Against `docs/scorer-acceptance.md` §2's line of **≤ 3**. `scored_match_rate` is
**0.000 on all three bands for both models** — on the payload the pipeline
actually sends, neither reproduces its own scored output once in ten.

**The per-field draws are the finding, not the spread.** On `gpt-4o`'s stressed
band the composite is perfectly stable — `score_12m` is 82 in all ten calls —
while `condition_flags.sovereign_stress` flips `False`/`True`,
`evidence_coverage` alternates 75/85 and `edge_vitality` 30/40. A spread of 0
reads as reproducible there, and the instrument is disagreeing with itself about
whether the country is in sovereign stress. On the moderate band
`emergency_rule` flips too, `internal_conflict_level` alternates between a level
and `none`, and all four ledgers move.

`docs/scorer-acceptance.md` §2 is marked **PROVISIONAL** and deliberately not
re-derived: as written the line disqualifies both the production scorer and the
benchmark incumbent, and a gate the reference cannot pass disqualifies every
candidate for a defect the reference shares. `docs/scorer-bakeoff.md` marks
every determinism number in it as a lower bound measured at 2,980 tokens.

The constraint the re-derivation has to confront is not a threshold choice:
**17 > 14.9**, the largest crisis response ever measured, so on the composite no
line admits the signal and excludes the noise.

### 7C. Where this audit was wrong, or right without joining it up

- **§6 item 2 under-called the determinism question, and its reasoning was the
  problem.** It said the canned three-band matrix "survives" the vintage fix
  because canned payloads are unaffected by it. True, and beside the point. The
  same document had already measured the gate at 2,980 tokens against a
  dispatched 11,264–13,459 (§1 stage 5, §4 blocker 4) — so the determinism
  numbers in §2 and §6 were taken on an instrument this audit itself had shown
  to be unrepresentative, and the link was never made. Both halves were on the
  page; only the join was missing. That is the same shape as the defects this
  audit was written to find.
- **§2's criterion table is superseded.** It was computed against the 08-27
  `gpt-4.1` arm, which §3 then showed to be on a degraded payload. The current
  computation is against `gpt-4.1-postfix` with `p2-rebaseline-postfix` as
  baseline; both return `comparable: same`.
- **§5 item 9 is still open.** `series_shape` still does not return `bands`
  though §4 of the acceptance doc says it computes it. One line, not done.
- **§1 stage 3's "zero look-ahead violations" was right about what it checked
  and did not check enough.** It compared `published_at` against the anchor,
  which is clean across all 157 snapshots. It could not see text *inside* a body
  that postdates the anchor — see 7D.

### 7D. Findings the audit did not have

- **Removing one constant field from the prompt moved the incumbent more than
  predicted.** `"source": "guardian"` was identical on every article of every
  anchor, so the expectation was that removing it could not move a ranking. The
  ranking held (ρ 0.799 US, 0.811 TR) and the magnitude did not: **27 of 52** US
  anchors and **28 of 53** TR anchors changed score, mean 2.5–2.9 points, max
  **15**. That is at or above the instrument's own noise floor, and it is why
  re-scoring A′ rather than documenting the gap was the right call.
- **A look-ahead vector inside article bodies.** `snapshot_select.usable_body`
  returns `api-native` bodies unconditionally — 53,361 of 53,368 on the pilot DB
  — on the premise that the body "arrived inside the search response, as the
  article itself". The Guardian Content API serves the *current* version:
  **2,405 bodies carry a `This article was amended on <date>` footer**, and in a
  400-row sample 284 postdate their own publication. Measured against the
  anchor: **0 of 53** TR 2018 anchors, **0 of 52** US 2019, **6 of 52** PT 2019,
  two of those in the top-3 full-text slots. Zero on both Phase 1 windows.
  Filed as `deferred.md` §36; the part-5 strip removes the symptom, not the
  mechanism.
- **The letters block had siblings, and the one nobody looked for is nine times
  larger.** Amendment/correction footers on 2,405 bodies (2.97%) against the
  letters template's 253 (0.31%). Found by searching, after the first was found
  by reading a dump.
- **PT 2019's round-number share is 84.6%**, the worst of the three windows and
  previously unreported (US 69.2%, TR 18.9% both reproduce §3 of the acceptance
  doc exactly). Filed as `deferred.md` §35 with the GATE2 baseline consequence.

### 7E. The Phase 1 verdict, revised

**Still not clear, and for a different reason than this audit gave.**

The reason in §3 — a stale reference nothing could detect — is closed. Both
references are current, both carry fingerprints, and a comparison across a
payload or prompt change is now refused rather than silently performed.

What blocks Phase 1 now is that **the acceptance bar's own determinism criterion
was calibrated on measurements taken at a quarter of production context**, and
re-measured honestly it fails the production scorer and the benchmark incumbent
alike. A candidate cannot be judged against a line that the references do not
clear. Re-deriving that line — cold, with both numbers in hand — is the
remaining work, and it is a question about what the instrument *is* rather than
about where to put a threshold.

**Spend: $7.56 against an $8 cap for the re-score, plus $0.68 of a separate $2
for the incumbent's re-measurement. 210 anchors scored, zero errors, zero schema
violations.**

## 8. The payload dumps, verbatim

Three anchors, all from stored rows, assembled at $0 through the production code
path: `snapshot_select.select` → `rewrite.mask_items` → `digest_engine`
(cache-served; coverage checked first and the anchor refused on any miss) →
`pipeline._rewrite_fulltext` (cache-served) → `payload.build_evidence_payload` →
`langchain_llm.country_llm_score` with `ai_client.build_chat` replaced by a stub
that records the messages and raises. Nothing was sent.

Each dump carries the `payload_health` census, the evidence payload with every
indicator and its vintage, and then **the exact prompt string that would have
been dispatched**, rule blocks included.

- **TR 2018-08-13** — the crisis anchor. Peak of the lira collapse; the stored
  score is 0.82, tied for the year's maximum. 20 articles, full text on
  a1/a2/a8, **11,264 tokens**.
- **TR 2018-05-21** — the quiet anchor, inside the 7 May – 18 June period
  verified quiet during the Q1 investigation. Stored score 0.68, flat across the
  whole window. Same country, same year, same corpus, so any difference between
  this and the dump above is the *week*. **13,459 tokens.**
- **US 2019-03-11** — the ambiguous window, where `gpt-4o` placed all 52 anchors
  in `Moderate`. This is the year's maximum (0.70) and the anchor six models
  independently agreed on. **12,734 tokens.**


---

### Appendix — TR_2018-08-13

```text
### CRISIS - lira collapse — TR 2018-08-13

articles selected: 20   full-text ids: ['a1', 'a2', 'a8']
prompt chars: 44437   ~tokens (chars/4): 11109
schema strict flag sent: True
payload_variant: p2   prompt_variant: 
mask_map_version: g5   sweep_version: 9f4aee55

--- payload_health ---
{
  "indicators": {
    "expected": 38,
    "resolved": 24,
    "by_ledger": {
      "friction": {
        "expected": 14,
        "resolved": 10
      },
      "uncertainty": {
        "expected": 16,
        "resolved": 11
      },
      "information": {
        "expected": 4,
        "resolved": 1
      },
      "edge": {
        "expected": 4,
        "resolved": 2
      }
    },
    "empty_ledgers": [],
    "dropped": {
      "GOV.DEBT.DOMESTIC.SHARE": "no row",
      "GOV.DEBT.FX.SHARE": "no row",
      "HD.HCI.OVRL": "vintage bound",
      "INFORMAL.PCT.GDP": "no row",
      "NIIP.GDP": "no row",
      "OBS.SCORE": "no row",
      "OECD.PISA.MEAN": "no row",
      "OECD.TAX.WEDGE": "no row",
      "RESERVES.USD": "no row",
      "RSF.PRESS.SCORE": "no row",
      "STAT.TAX.TOP.RATE": "no row",
      "UN.EGDI": "no row",
      "UNWPP.DPND.OL.PROJ": "no row",
      "WUI.INDEX": "no row"
    }
  },
  "trends": {
    "trend_1y": 23,
    "trend_5y": 13,
    "history": 2,
    "of": 24
  },
  "blocks": {
    "computed": 8,
    "edge_inputs": 2,
    "friction_inputs": 10,
    "information_inputs": 1,
    "structural": 6,
    "uncertainty_inputs": 12
  },
  "articles": {
    "articles": 20,
    "by_theme": {
      "friction": 5,
      "order": 5,
      "security": 7,
      "information": 0,
      "edge": 1,
      "broad": 2
    },
    "thin_themes": [
      "edge",
      "information"
    ],
    "theme_floor": 2,
    "by_tier": {
      "full": 12,
      "abstract-only": 8
    },
    "with_body": 12,
    "clipped_at_max": 0
  }
}

--- evidence payload (pre-mask, as built) ---
{
  "_meta": {
    "country": "TR",
    "as_of": "2018-08-13",
    "vintage_scheme": "point-in-time",
    "staleness_basis": "staleness_days counts from the end of the period a value describes to as_of: how old the reading is. `as_of` on each value is a separate fact — when it became known to us. A large staleness_days means the reading is old, not that it is wrong.",
    "next_scheduled_election": null
  },
  "friction_inputs": {
    "Government effectiveness (z-score)": {
      "value": 0.2092,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WGI",
      "unit": "z-score",
      "trend_1y": -0.1239
    },
    "Tax revenue (% GDP)": {
      "value": 18.328,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WDI",
      "unit": "% GDP",
      "trend_1y": 0.1861
    },
    "Political corruption index (0–1, higher = more corrupt)": {
      "value": 0.837,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "index (0–1)",
      "trend_1y": 0.01,
      "trend_5y": 0.151
    },
    "Interest payments (% revenue)": {
      "value": 6.542,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "% revenue",
      "trend_1y": 0.6617,
      "trend_5y": -1.8479
    },
    "Income inequality (Gini)": {
      "value": 43.5,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "index",
      "trend_1y": 1.6,
      "trend_5y": 3.3
    },
    "Government gross debt (% GDP)": {
      "value": 28.311,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 590,
      "source": "IMF WEO 2018-04",
      "unit": "% GDP",
      "trend_1y": 0.668,
      "trend_5y": -8.168
    },
    "Government net lending/borrowing (% GDP)": {
      "value": -2.328,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 590,
      "source": "IMF WEO 2018-04",
      "unit": "% GDP",
      "trend_1y": -1.063,
      "trend_5y": -1.641
    },
    "Old-age dependency ratio": {
      "value": 11.2974,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WDI",
      "unit": "% working-age population",
      "trend_1y": 0.1786
    },
    "Labour-force participation (% 15+)": {
      "value": 52.0,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WDI",
      "unit": "%",
      "trend_1y": 0.7
    },
    "Broad money growth (% y/y)": {
      "value": 17.6488,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WDI",
      "unit": "% y/y",
      "trend_1y": 1.1341
    }
  },
  "uncertainty_inputs": {
    "Real GDP growth (% y/y)": {
      "value": 3.184,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 590,
      "source": "IMF WEO 2018-04",
      "unit": "% y/y",
      "trend_1y": -2.902,
      "trend_5y": -7.929
    },
    "Current account balance (% GDP)": {
      "value": -3.838,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 590,
      "source": "IMF WEO 2018-04",
      "unit": "% GDP",
      "trend_1y": -0.102,
      "trend_5y": 5.099
    },
    "Inflation (% y/y)": {
      "value": 15.3851,
      "period": "2018-06",
      "freq": "M",
      "as_of": "2018-07-25",
      "staleness_days": 44,
      "source": "IMF CPI",
      "unit": "% y/y",
      "trend_1y": 4.484,
      "trend_5y": 6.4931,
      "history": {
        "2008": 10.44,
        "2009": 6.25,
        "2010": 8.57,
        "2011": 6.47,
        "2012": 8.89,
        "2013": 7.49,
        "2014": 8.86,
        "2015-01": 7.24,
        "2015-02": 7.55,
        "2015-03": 7.61,
        "2015-04": 7.91,
        "2015-05": 8.09,
        "2015-06": 7.2,
        "2015-07": 6.81,
        "2015-08": 7.14,
        "2015-09": 7.95,
        "2015-10": 7.58,
        "2015-11": 8.1,
        "2015": 7.67,
        "2015-12": 8.81,
        "2016-01": 9.58,
        "2016-02": 8.78,
        "2016-03": 7.46,
        "2016-04": 6.57,
        "2016-05": 6.58,
        "2016-06": 7.64,
        "2016-07": 8.79,
        "2016-08": 8.05,
        "2016-09": 7.28,
        "2016-10": 7.16,
        "2016-11": 7.0,
        "2016": 7.78,
        "2016-12": 8.53,
        "2017-01": 9.22,
        "2017-02": 10.13,
        "2017-03": 11.29,
        "2017-04": 11.87,
        "2017-05": 11.72,
        "2017-06": 10.9,
        "2017-07": 9.79,
        "2017-08": 10.68,
        "2017-09": 11.2,
        "2017-10": 11.9,
        "2017-11": 12.98,
        "2017": 11.14,
        "2017-12": 11.92,
        "2018-01": 10.35,
        "2018-02": 10.26,
        "2018-03": 10.23,
        "2018-04": 10.85,
        "2018-05": 12.15,
        "2018-06": 15.39
      }
    },
    "Political stability (z-score)": {
      "value": -1.7062,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "z-score",
      "trend_1y": 0.0739,
      "trend_5y": -0.7865
    },
    "Rule of law (z-score)": {
      "value": -0.6154,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "z-score",
      "trend_1y": -0.0695,
      "trend_5y": -0.5092
    },
    "GDP per-capita growth (% y/y)": {
      "value": 6.4366,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "% y/y",
      "trend_1y": 4.4938,
      "trend_5y": 2.9565,
      "history": {
        "2008": -0.35,
        "2009": -6.19,
        "2010": 6.91,
        "2011": 9.36,
        "2012": 3.48,
        "2013": 7.1,
        "2014": 3.2,
        "2015": 4.4,
        "2016": 1.94,
        "2017": 6.44
      }
    },
    "Unemployment (% labour force)": {
      "value": 10.919,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "%",
      "trend_1y": 0.02,
      "trend_5y": 1.709
    },
    "FDI inflow (% GDP)": {
      "value": 1.2953,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 225,
      "source": "World Bank panel",
      "unit": "% GDP",
      "trend_1y": -0.2934,
      "trend_5y": -0.2571
    },
    "Short-term external debt (% reserves)": {
      "value": 85.8088,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WDI",
      "unit": "% reserves",
      "trend_1y": -8.6073
    },
    "Exchange rate vs USD": {
      "value": 4.884,
      "period": "2018-07",
      "freq": "M",
      "as_of": "2018-07-31",
      "staleness_days": 13,
      "source": "BIS XRU",
      "unit": "local currency per USD",
      "trend_1y": 1.3611
    },
    "Policy rate (%)": {
      "value": 17.75,
      "period": "2018-07",
      "freq": "M",
      "as_of": "2018-07-31",
      "staleness_days": 13,
      "source": "BIS CBPOL",
      "unit": "% per year",
      "trend_1y": 9.75
    },
    "suppressed_vol_flag": {
      "value": null,
      "regime": null,
      "fx_volatility_24m": 4.061791,
      "reserves_trend_6m": null,
      "note": "True means measured calm is being bought with reserves under a managed or pegged regime. null means one of the three inputs is unavailable — not that the flag is false."
    }
  },
  "information_inputs": {
    "Statistical performance (0–100)": {
      "value": 72.6454,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank SPI",
      "unit": "score 0–100"
    }
  },
  "edge_inputs": {
    "New business density (per 1,000 working-age)": {
      "value": 1.1915,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WDI",
      "unit": "per 1,000 working-age adults",
      "trend_1y": -0.0732
    },
    "Government education spending (% GDP)": {
      "value": 4.6277,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 590,
      "source": "World Bank WDI",
      "unit": "% of GDP",
      "trend_1y": 0.3222
    }
  },
  "computed": {
    "conversion_loss": 0.6476,
    "frictional_extraction": 11.8692,
    "monetary_dilution": 11.2121,
    "real_policy_rate": 2.3649,
    "cpi_volatility_36m": 2.113751,
    "fx_volatility_24m": 4.061791,
    "precommitted_share": {
      "value": 6.542,
      "partial": true
    },
    "dependency_trajectory": {
      "current": 11.2974,
      "projected_10y": null,
      "delta": null
    }
  },
  "structural": {
    "region": "Europe",
    "income_group": "upper-middle",
    "commodity_exporter": false,
    "monetary_sovereignty": "full",
    "reserve_currency": "none",
    "note": "Facts about this country that do not change year to year, supplied because the country is not named. Reason from these rather than from any guess about which country this is."
  }
}

========================================================================
THE EXACT PROMPT DISPATCHED
========================================================================
You are a senior sovereign risk analyst. Assess investor risk for the country
as of 2018-08-13, using ONLY the evidence below.
Treat 2018-08-13 as today: this evidence is your complete knowledge of the
world. Do not use anything you know about events after this date.

Every value in EVIDENCE_JSON carries `as_of` and `staleness_days` — the date it
became known and how old it is on 2018-08-13. Weigh a fresh reading more than
a stale one, and say so when a stale one is carrying an argument. A missing
indicator is absent from the evidence entirely; treat absence as absence, never
as zero and never as reassurance.

# --- The country is not named, deliberately ---
This evidence describes a real country whose identity has been withheld from
you. Country names, cities, people, parties, currencies and institutions have
been replaced by the roles they play: "the country", "the capital", "the central
bank", "the finance minister", "the local currency". Every NUMBER is untouched —
inflation prints, rates, counts and dates are exactly as published.

Reason only from what is on the page. Do not try to work out which country this
is, and do not let a guess do any work in your reasoning: an inference that
depends on having identified the country is unsound here even when the guess
happens to be right, because you cannot check it and neither can anyone reading
your output.

The priors a name would have carried are supplied instead. When EVIDENCE_JSON
contains a `structural` block, it states what identity used to imply — whether
the government borrows in a currency it can issue, whether it can devalue at
all, its income group, its coarse region, whether it depends on commodity
exports. Use those facts directly. A debt burden means one thing for a
`monetary_sovereignty: full` issuer of a `reserve_currency: major` and something
different for a `constrained` borrower whose debt is in money it cannot print;
read the block, do not reconstruct it from a hunch about the name. When the
block is absent, that structure is simply unknown — treat it as absent, the same
as any missing indicator, and do not substitute a guess.

EVIDENCE_JSON
{"_meta": {"country": "the country", "as_of": "2018-08-13", "vintage_scheme": "point-in-time", "staleness_basis": "staleness_days counts from the end of the period a value describes to as_of: how old the reading is. `as_of` on each value is a separate fact — when it became known to us. A large staleness_days means the reading is old, not that it is wrong.", "next_scheduled_election": null}, "friction_inputs": {"Government effectiveness (z-score)": {"value": 0.2092, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WGI", "unit": "z-score", "trend_1y": -0.1239}, "Tax revenue (% GDP)": {"value": 18.328, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WDI", "unit": "% GDP", "trend_1y": 0.1861}, "Political corruption index (0–1, higher = more corrupt)": {"value": 0.837, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "index (0–1)", "trend_1y": 0.01, "trend_5y": 0.151}, "Interest payments (% revenue)": {"value": 6.542, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "% revenue", "trend_1y": 0.6617, "trend_5y": -1.8479}, "Income inequality (Gini)": {"value": 43.5, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "index", "trend_1y": 1.6, "trend_5y": 3.3}, "Government gross debt (% GDP)": {"value": 28.311, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 590, "source": "IMF WEO 2018-04", "unit": "% GDP", "trend_1y": 0.668, "trend_5y": -8.168}, "Government net lending/borrowing (% GDP)": {"value": -2.328, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 590, "source": "IMF WEO 2018-04", "unit": "% GDP", "trend_1y": -1.063, "trend_5y": -1.641}, "Old-age dependency ratio": {"value": 11.2974, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WDI", "unit": "% working-age population", "trend_1y": 0.1786}, "Labour-force participation (% 15+)": {"value": 52.0, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WDI", "unit": "%", "trend_1y": 0.7}, "Broad money growth (% y/y)": {"value": 17.6488, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WDI", "unit": "% y/y", "trend_1y": 1.1341}}, "uncertainty_inputs": {"Real GDP growth (% y/y)": {"value": 3.184, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 590, "source": "IMF WEO 2018-04", "unit": "% y/y", "trend_1y": -2.902, "trend_5y": -7.929}, "Current account balance (% GDP)": {"value": -3.838, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 590, "source": "IMF WEO 2018-04", "unit": "% GDP", "trend_1y": -0.102, "trend_5y": 5.099}, "Inflation (% y/y)": {"value": 15.3851, "period": "2018-06", "freq": "M", "as_of": "2018-07-25", "staleness_days": 44, "source": "IMF CPI", "unit": "% y/y", "trend_1y": 4.484, "trend_5y": 6.4931, "history": {"2008": 10.44, "2009": 6.25, "2010": 8.57, "2011": 6.47, "2012": 8.89, "2013": 7.49, "2014": 8.86, "2015-01": 7.24, "2015-02": 7.55, "2015-03": 7.61, "2015-04": 7.91, "2015-05": 8.09, "2015-06": 7.2, "2015-07": 6.81, "2015-08": 7.14, "2015-09": 7.95, "2015-10": 7.58, "2015-11": 8.1, "2015": 7.67, "2015-12": 8.81, "2016-01": 9.58, "2016-02": 8.78, "2016-03": 7.46, "2016-04": 6.57, "2016-05": 6.58, "2016-06": 7.64, "2016-07": 8.79, "2016-08": 8.05, "2016-09": 7.28, "2016-10": 7.16, "2016-11": 7.0, "2016": 7.78, "2016-12": 8.53, "2017-01": 9.22, "2017-02": 10.13, "2017-03": 11.29, "2017-04": 11.87, "2017-05": 11.72, "2017-06": 10.9, "2017-07": 9.79, "2017-08": 10.68, "2017-09": 11.2, "2017-10": 11.9, "2017-11": 12.98, "2017": 11.14, "2017-12": 11.92, "2018-01": 10.35, "2018-02": 10.26, "2018-03": 10.23, "2018-04": 10.85, "2018-05": 12.15, "2018-06": 15.39}}, "Political stability (z-score)": {"value": -1.7062, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "z-score", "trend_1y": 0.0739, "trend_5y": -0.7865}, "Rule of law (z-score)": {"value": -0.6154, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "z-score", "trend_1y": -0.0695, "trend_5y": -0.5092}, "GDP per-capita growth (% y/y)": {"value": 6.4366, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "% y/y", "trend_1y": 4.4938, "trend_5y": 2.9565, "history": {"2008": -0.35, "2009": -6.19, "2010": 6.91, "2011": 9.36, "2012": 3.48, "2013": 7.1, "2014": 3.2, "2015": 4.4, "2016": 1.94, "2017": 6.44}}, "Unemployment (% labour force)": {"value": 10.919, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "%", "trend_1y": 0.02, "trend_5y": 1.709}, "FDI inflow (% GDP)": {"value": 1.2953, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 225, "source": "World Bank panel", "unit": "% GDP", "trend_1y": -0.2934, "trend_5y": -0.2571}, "Short-term external debt (% reserves)": {"value": 85.8088, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WDI", "unit": "% reserves", "trend_1y": -8.6073}, "Exchange rate vs another country": {"value": 4.884, "period": "2018-07", "freq": "M", "as_of": "2018-07-31", "staleness_days": 13, "source": "BIS XRU", "unit": "local currency per another country", "trend_1y": 1.3611}, "Policy rate (%)": {"value": 17.75, "period": "2018-07", "freq": "M", "as_of": "2018-07-31", "staleness_days": 13, "source": "BIS CBPOL", "unit": "% per year", "trend_1y": 9.75}, "suppressed_vol_flag": {"value": null, "regime": null, "fx_volatility_24m": 4.061791, "reserves_trend_6m": null, "note": "True means measured calm is being bought with reserves under a managed or pegged regime. null means one of the three inputs is unavailable — not that the flag is false."}}, "information_inputs": {"Statistical performance (0–100)": {"value": 72.6454, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank SPI", "unit": "score 0–100"}}, "edge_inputs": {"New business density (per 1,000 working-age)": {"value": 1.1915, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WDI", "unit": "per 1,000 working-age adults", "trend_1y": -0.0732}, "Government education spending (% GDP)": {"value": 4.6277, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 590, "source": "World Bank WDI", "unit": "% of GDP", "trend_1y": 0.3222}}, "computed": {"conversion_loss": 0.6476, "frictional_extraction": 11.8692, "monetary_dilution": 11.2121, "real_policy_rate": 2.3649, "cpi_volatility_36m": 2.113751, "fx_volatility_24m": 4.061791, "precommitted_share": {"value": 6.542, "partial": true}, "dependency_trajectory": {"current": 11.2974, "projected_10y": null, "delta": null}}, "structural": {"region": "Europe", "income_group": "upper-middle", "commodity_exporter": false, "monetary_sovereignty": "full", "reserve_currency": "none", "note": "Facts about this country that do not change year to year, supplied because the country is not named. Reason from these rather than from any guess about which country this is."}}

ARTICLES_JSON
[{"id": "a1", "source": "guardian", "published_at": "2018-08-07", "title": "the country under pressure to raise interest rates as economic crisis looms", "digest": {"actors": "The central bank is expected to increase borrowing costs; the leader is interfering in monetary policy; a foreign leader is imposing sanctions on the country; the capital is sending a mission to a foreign country for diplomatic solutions.", "numbers": "the local currency is down by almost a third against another country in the past 12 months; 10-year borrowing costs hit a record level of more than 20%; the local currency hit a record low of 5.425 against another country; a 5.5% drop in a single day; the local currency recovered to 5.27 on Tuesday afternoon; inflation is running at more than 15%.", "masked_title": "the country under pressure to raise interest rates as economic crisis looms", "transmission": "The central bank's monetary policy decisions are influenced by the government, affecting the local currency and inflation.", "what_happened": "The country is facing pressure to announce an emergency rise in interest rates due to rampant inflation and a plunging currency.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a2", "source": "nyt", "published_at": "2018-08-10", "title": "Tensions Between the nation and another country Soar as the leader Orders New Sanctions", "digest": {"actors": "the leader did this to the nation", "numbers": "not stated", "masked_title": "Tensions Between the nation and another country Soar as the leader Orders New Sanctions", "transmission": "economic sanctions", "what_happened": "The leader announced economic sanctions as the nation's currency plummets.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a3", "source": "nyt", "published_at": "2018-08-03", "title": "the foreign minister Warns the country on Detained another country Pastor: The Clock Has ‘Run Out’", "digest": {"actors": "the foreign minister said harsh tactics would not work", "numbers": "not stated", "masked_title": "the foreign minister Warns the country on Detained another country Pastor: The Clock Has ‘Run Out’", "transmission": "not stated", "what_happened": "The imprisonment of an individual prompted sanctions from a foreign government.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a4", "source": "nyt", "published_at": "2018-08-10", "title": "the country's Downward Spiral", "digest": {"actors": "the leader of a foreign country and the leader of the country are feuding", "numbers": "not stated", "masked_title": "the country's Downward Spiral", "transmission": "not stated", "what_happened": "The alliance between a foreign country and the country grows ever more frayed.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a5", "source": "nyt", "published_at": "2018-08-07", "title": "the capital Cheers as the leader Takes On a Foreign Country Over Sanctions", "digest": {"actors": "the leader took retaliatory measures against a foreign country's sanctions", "numbers": "not stated", "masked_title": "the capital Cheers as the leader Takes On a Foreign Country Over Sanctions", "transmission": "not stated", "what_happened": "The leader responded to a foreign country's sanctions with retaliatory measures.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a6", "source": "nyt", "published_at": "2018-07-26", "title": "A Leader Threatens Sanctions Against the Country Over Detained Pastor", "digest": {"actors": "the leaders of the country and another country may have their relationship affected by the house arrest of a pastor accused of espionage.", "numbers": "not stated", "masked_title": "A Leader Threatens Sanctions Against the Country Over Detained Pastor", "transmission": "not stated", "what_happened": "The warming relationship between combative leaders in the country and another country may cool over the house arrest of a pastor accused of espionage.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a7", "source": "nyt", "published_at": "2018-08-04", "title": "the country's leader Orders Retaliatory Sanctions Against another nation's Officials", "digest": {"actors": "the governing party and the other nation's government", "numbers": "not stated", "masked_title": "the country's leader Orders Retaliatory Sanctions Against another nation's Officials", "transmission": "not stated", "what_happened": "Negotiations fail over the release of a religious leader, leading to further deterioration in relations between the two nations.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a8", "source": "guardian", "published_at": "2018-08-12", "title": "Global markets braced for hectic trading as the leader's crisis unfolds", "digest": {"actors": "the leader accused foreign interests of waging an economic war against the country; the finance minister described the local currency’s weakness as an attack; the central bank offered no indication of raising interest rates; public officials portrayed the financial crisis as an attack from abroad; the country had pledged to buy a missile defence system from another country.", "numbers": "20%, $350bn, 15.4%, 7.24", "masked_title": "Global markets braced for hectic trading as the leader's crisis unfolds", "transmission": "economic crisis, currency depreciation, inflation", "what_happened": "The local currency continued to fall amid an economic crisis, prompting the leader to accuse foreign interests of waging an economic war and pledge trade measures.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a9", "source": "guardian", "published_at": "2018-08-12", "title": "Q&A: Why is the local currency in freefall and should we worry?", "digest": {"actors": "the leader is defiant despite the currency crisis; a foreign leader announced increased tariffs; another country has set a deadline for the release of a detained pastor; the finance minister is appointed by the leader; the central bank is facing political interference.", "numbers": "80 million, 15.9%, 50%, 6.4, 8.1, £2.60, £1.60, under a tenner, 75 points, 2%", "masked_title": "Q&A: Why is the local currency in freefall and should we worry?", "transmission": "economic challenges, trade disputes, and political tensions affecting currency value", "what_happened": "The local currency fell significantly due to a combination of economic challenges and international tensions.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a10", "source": "guardian", "published_at": "2018-08-12", "title": "a foreign country bailout drama 'in last throes' but the hardship is not over yet", "digest": {"actors": "the leader announced the exit from the bailout programme, while the regional economics chief commented on the country's progress and challenges.", "numbers": "20 August, 15bn (£13.2bn), 180% of GDP, 2%, 2.4%, 135,000, 26%, 3.5%, 2.2%, 70%, 106-page, 288bn, four times more than five years ago.", "masked_title": "a foreign country bailout drama 'in last throes' but the hardship is not over yet", "transmission": "the exit from the bailout programme and the associated economic measures and targets set by the government", "what_happened": "Another country will exit its third bailout programme next week after nearly nine years of crisis and austerity.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a11", "source": "nyt", "published_at": "2018-08-01", "title": "a foreign country Imposes Sanctions on the governing party's Officials Over Detained a foreign pastor", "digest": {"actors": "the governing party imposed a penalty on the government of a vital NATO ally", "numbers": "not stated", "masked_title": "a foreign country Imposes Sanctions on the governing party's Officials Over Detained a foreign pastor", "transmission": "not stated", "what_happened": "A government of a vital NATO ally received an unusual penalty that is expected to inflame tensions with another country.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a12", "source": "guardian", "published_at": "2018-07-24", "title": "Leader rebuked for claiming another country's officials have 'Hitler spirit'", "digest": {"actors": "the leader condemned the nation state law passed in another country, while the head of government of another country described the country under the leader as a dark dictatorship; a top aide to the leader described the head of government of another country as lacking moral authority.", "numbers": "not stated", "masked_title": "Leader rebuked for claiming another country's officials have 'Hitler spirit'", "transmission": "not stated", "what_happened": "The leader condemned another country's nation state law, leading to a war of words with the head of government of another country.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a13", "source": "guardian", "published_at": "2018-07-19", "title": "Authoritarianism and The Path to Oppression review – the warning from the 1930s", "digest": {"actors": "Right-wing strongmen such as the leader of another country, the leader of the country, and the leader of another country are curtailing civil liberties; the president of another country is hostile to democratic institutions; the secretary of state under a former president thinks the current president is the first antidemocratic president in another country history.", "numbers": "37.4%", "masked_title": "Authoritarianism and The Path to Oppression review – the warning from the 1930s", "transmission": "not stated", "what_happened": "Democracy is under threat in a region and another country, with right-wing leaders curtailing civil liberties and undermining democratic institutions.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a14", "source": "nyt", "published_at": "2018-07-25", "title": "A religious leader, moved to house arrest in a foreign country", "digest": {"actors": "foreign country officials are seeking the freedom of a religious leader arrested after a failed coup.", "numbers": "not stated", "masked_title": "A religious leader, moved to house arrest in a foreign country", "transmission": "not stated", "what_happened": "A foreign country said the move was ‘not enough.’", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a15", "source": "guardian", "published_at": "2018-08-10", "title": "the economic crisis deepens as the leader doubles tariffs", "digest": {"actors": "the leader announced tariffs on the country's steel and aluminium, the finance minister expressed disappointment, the central bank governor is under pressure to raise interest rates, and the opposition leader warned of broader geopolitical implications.", "numbers": "20%, 14%, 50%, 15.9%, 1,000", "masked_title": "the economic crisis deepens as the leader doubles tariffs", "transmission": "the announcement of tariffs leading to currency depreciation and potential interest rate hikes", "what_happened": "The economic crisis deepened after another country announced it was doubling import tariffs on the country's steel and aluminium, causing the local currency to plunge.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a16", "source": "guardian", "published_at": "2018-07-25", "title": "the country to place a religious leader under house arrest after over 600 days in prison", "digest": {"actors": "the country's authorities released the religious leader under house arrest in response to demands from foreign policymakers", "numbers": "600 days, 35 years, 18 July", "masked_title": "the country to place a religious leader under house arrest after over 600 days in prison", "transmission": "sanctions against the capital", "what_happened": "A religious leader has been released from jail and placed under house arrest after more than 600 days of imprisonment.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a17", "source": "guardian", "published_at": "2018-07-19", "title": "Suffocating climate of fear in the country despite end of state of emergency", "digest": {"actors": "human rights campaigners criticized the capital for its crackdown on free speech; the government ended the state of emergency; the leader was sworn in for a new term; the governing party proposed an anti-terrorism bill; the opposition leader vowed to challenge the government; the government launched an investigation against the opposition leader.", "numbers": "250 people killed; 1,400 people wounded; more than 120,000 people detained or dismissed; about a quarter of the country’s judges dismissed or detained; 100 people extradited; more than 120 journalists imprisoned; 18,600 public servants dismissed; some academics sentenced to prison for signing a petition.", "masked_title": "Suffocating climate of fear in the country despite end of state of emergency", "transmission": "not stated", "what_happened": "The country’s two-year state of emergency ended, but human rights campaigners say more must be done to reverse a crackdown on free speech.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a18", "source": "guardian", "published_at": "2018-08-09", "title": "A major club leads the way as young players take centre stage in the country", "digest": {"actors": "The governing party introduced new policies affecting club presidents; the central bank governor is managing currency issues; the president of a major club has been replaced; the coach of a major club is implementing a new transfer policy; the sporting director of a major club is overseeing youth acquisitions; the president of another major club is managing transfer revenues.", "numbers": "£22.8m, £38.5m, 621m, 84.75m, 45, 2018-19", "masked_title": "A major club leads the way as young players take centre stage in the country", "transmission": "Financial Fair Play regulations and new policies from the Football Federation", "what_happened": "The country is experiencing a summer of austerity in football due to financial regulations and a focus on youth development.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a19", "source": "guardian", "published_at": "2018-08-08", "title": "We just want a large domestic travel platform to say sorry and pay our £47", "digest": {"actors": "the hotel receptionist refused to assist the family, a large domestic travel platform offered a partial refund to the customer", "numbers": "£47, £20", "masked_title": "We just want a large domestic travel platform to say sorry and pay our £47", "transmission": "not stated", "what_happened": "A family faced difficulties with hotel accommodation due to overbooking after confirming a late check-in.", "stage1_severity": 0, "directly_about_country": false}, "stage1_severity": 0.0}, {"id": "a20", "source": "guardian", "published_at": "2018-08-02", "title": "The charity and the retailer criticised for Pride T-shirts made in a country", "digest": {"actors": "Critics, including the co-founder of a foreign Pride Network and the communications director at a regional Pride event, criticized the charity and the retailer for their unethical practices regarding LGBT rights in the country.", "numbers": "20 people were detained after protesters failed to heed warnings to disperse.", "masked_title": "The charity and the retailer criticised for Pride T-shirts made in a country", "transmission": "not stated", "what_happened": "The charity and the retailer faced criticism for their Pride-related merchandise made in a country with a poor record on LGBT rights.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}]

FULL_TEXT
--- id: a1 · the country under pressure to raise interest rates as economic crisis looms ---
the country is facing mounting pressure to announce an emergency rise in interest rates as rampant inflation, a plunging currency and another country sanctions pushes one of the world’s key emerging market countries to the brink of crisis. Analysts said the country’s central bank would have no choice but to increase borrowing costs aggressively in the coming days to stem the fall in the local currency, which is down by almost a third against another country another country in the past 12 months and hit a record low this week. The currency’s weakness has been exacerbated by the increasing tendency of the country’s leader, the president, to interfere in the conduct of monetary policy by opposing the use of higher interest rates to cool an overheating economy. This week’s turbulence was triggered by news that a foreign administration was considering removing the country’s eligibility for preferential trade treatment in protest at their imprisonment of another country pastor. The threatened loss of duty-free access to the world’s biggest market for the country's exports would further weaken the local currency by removing a crucial source of another country inflow. another country has already announced asset freezes and travel bans on two the country's ministers in an attempt to secure the release of the pastor, who is facing accusations of espionage for insurgents and the movement of a another country-based preacher believed to have orchestrated a coup attempt in 2016. another country officials say the charges against the pastor are false and as 10-year borrowing costs hit a record level of more than 20% on Tuesday, the capital announced that it was sending a mission to another country to seek a diplomatic solution to the row between the two NATO countries. But a chief emerging markets economist at a financial institution said swift action by the central bank now looked unavoidable. The economist said the country’s central bank had been expected to increase a key interest rate by 2 percentage points over the coming months but it was now looking increasingly likely that this would now come in just a few days. “However, the local currency’s fall is being amplified by concerns that the central bank will not act to shore up the currency. The fact that the monetary policy committee kept interest rates unchanged at its meeting in late July, despite the rise in inflation to a 15-year high, suggested that the government is influencing monetary policy.” The local currency hit a record low of 5.425 against another country on Monday, a 5.5% drop in a single day, before recovering somewhat to 5.27 on Tuesday afternoon, after reports of the country’s delegation’s another country visit. Monday’s drop was the local currency’s worst single-day slide in ten years. another country sanctions have exacerbated a currency crisis that was already hurting the country's consumers and businesses with loans in foreign currencies. Investors were already concerned over the country’s widening current account deficit and high foreign debt even before the president demonstrated his desire to influence economic policy by appointing his son-in-law as treasury and finance minister. The central bank raised borrowing costs to support the local currency in May, but after the recent election the president – a self-styled enemy of interest rates – assumed new executive powers that investors fear will compromise its independence. The president wants lower borrowing costs to fuel credit growth and economic expansion, even though inflation is running at more than 15%.

--- id: a2 · Tensions Between the nation and another country Soar as the leader Orders New Sanctions ---
Frustrated by the country's delays in releasing an another country pastor, President Trump announced economic sanctions as the country’s currency plummets.

--- id: a8 · Global markets braced for hectic trading as the leader's crisis unfolds ---
Global markets are braced for another hectic day of trading amid the leader's unfolding economic crisis after the local currency continued its fall on Monday. The leader remained defiant over the weekend, accusing foreign interests of waging an economic war against the country and pledging trade measures to reduce reliance on another country and another country markets. He told party officials on Sunday in a major city: "We will respond to those, who declared trade war on the entire world and included the country in it, by steering toward new alliances, new markets." The currency fell 20% to record lows last week after another country's president slapped tariffs on the country's steel exports and declared via Twitter: "Our relations with the country are not good at this time." It hit a new all-time low in Asian trading on Monday morning when it touched 7.24 to another country another country before recovering slightly. Another country was down 1% and Asian stock markets were also in the red. But the country's woes spread far beyond a trade dispute. Investors are increasingly concerned about the $350bn in foreign debt held by the country's banks and companies, and their ability to finance it as the currency weakens and inflation soars. As the crisis has deepened, the country's consumers have faced rising food, fuel and medicine prices. The inflation rate is expected to jump rapidly from the current 15.4% official rate. In an interview with a local newspaper published on Sunday night, the finance minister echoed the leader by describing the local currency’s weakness as "an attack", and said the action plan was ready. "From Monday morning onwards our institutions will take the necessary steps and will share the announcements with the market," the finance minister said, without giving details on what the steps would be. The finance minister also said a plan has been prepared for banks and the real economy sector, including small to mid-sized businesses which are most affected by the foreign exchange fluctuations. "We will be taking the necessary steps with our banks and banking watchdog in a speedy manner," he said. The central bank has offered no indication that it will raise interest rates to combat high inflation or other measures to stem the drop in the value of the currency. "They are threatening us," the leader said on Saturday at a rally in a city near the Black Sea. "You cannot bring [the country's] people to their knees by using a threatening language." Referring to another country, he said: "It is a shame. You prefer a pastor to a strategic ally of yours in a military alliance." Local media, which are largely loyalist and almost entirely owned by allies of the leader, echoed the message of a nation besieged. In an article titled "Lying news agencies", the pro-government newspaper took aim at a major news outlet for their coverage of the financial crisis. Another article was headlined: "The saboteur". Silence from the central bank has highlighted investor fears over the institution’s independence from the leader, who was sworn in last month with wider executive powers that many worry will be used to wield greater influence over monetary policy. The leader opposes raising interest rates to combat high inflation, a measure many analysts have urged. And his appointment of a family member to the post of treasury and finance minister has also heightened concerns that he will exert greater influence in fiscal strategy. Statements by public officials have focused on portraying the financial crisis as an attack from abroad by another country and other agents, urging the country's citizens to rally around their government to defeat the aggression. Relations between another country and the country have been worsening for years, primarily over disagreements on a regional conflict. The capital initially wanted a more forceful intervention against a regional leader, and later took issue with another country’s alliance with certain militias in the fight against a terrorist group. The country considers those militias an extension of its own insurgency, and treats their power as a national security threat. The latest dispute was over the decision by a local court to extend the detention of a foreign pastor accused of espionage for the militias and a group accused of masterminding a coup. The country had also pledged to buy a missile defence system from another country, a step that strained its ties with the broader military alliance.

# --- The three ledgers ---

FRICTION is the wedge: what the state extracts multiplied by how much of it
fails to convert into capability. Judge the take by how it converts, not by its
size. A high tax burden that funds functioning courts, roads and registries is
not friction; a modest one that funds nothing is. `frictional_extraction` in
the computed block is that product, and `doom_loop` says whether the burden is
rising while conversion decays — trajectory matters more than level, because a
heavy but stable wedge can be carried indefinitely and a compounding one cannot.

ORDER-UNCERTAINTY is imposed doubt about the load-bearing rules — the ones
capital cannot price around: whether contracts will be enforced as written,
whether the currency will hold its function, whether the published statistics
mean what they say, and whether succession is settled. This is not the same as
volatility. A country can be turbulent and legible, or calm and unreadable; the
second is worse for an investor, because there is nothing to underwrite against.

INFORMATION is instrument quality, and it sets both trust and drift. Where the
statistical system, the auditors and the press are strong, official numbers can
be taken near face value. Where they are weak, official numbers deserve a
haircut: lean on market-observed series (exchange rates, policy rates, reserves)
and on article evidence instead, and treat measured friction as compounding
rather than mean-reverting — a state that cannot see itself does not self-correct.

# --- Edge vitality: report it, never penalize it ---

Entry-and-exit churn — startup formation AND startup failure — and human-capital
formation are the system learning. They MUST NOT raise any risk score. A country
where firms are born and die quickly is discovering what works; a country where
nothing is created and nothing fails is not stable, it is inert. Failure counts
as vitality here.

Learning outcomes lead; education spending is the effort line. Read them
together, never the spending alone. High spending with weak learning outcomes is
the wedge made visible inside a school system — money extracted and not
converted into capability. Read that gap as friction evidence, not as edge
credit.

Score `edge_vitality` as an independent reading of that adaptive capacity —
higher means more vitality — and do not let a high value raise friction,
order-uncertainty, or either horizon score.

# --- The three-door event test (apply to every article) ---

An event matters only if it passes through one of three doors:
  F — it changes the wedge (extraction, or how well extraction converts).
      Reported waves of skilled departure — doctors, engineers and founders
      leaving the country — pass through this door: the population grading the
      wedge with their feet. There is deliberately no data series for this, so
      these articles are its only instrument.
  U — it destabilizes the order (contracts, currency, statistics, succession)
  I — it changes the instruments (statistics office, auditors, courts, press)
Everything else is noise, however dramatic the headline. Natural disasters with
no fiscal or contractual aftermath, weapons demonstrations, military parades,
diplomatic insults, celebrity politics and scandal without institutional
consequence do not move a score. Name the door in your reasoning; if an article
passes through none of them, its impact is low no matter how prominent it is.

# --- Manufactured calm ---

When `suppressed_vol_flag` is true, measured calm is evidence AGAINST the
country, not for it. A currency held quiet under a managed or pegged regime
while reserves drain is accumulating fuel load, and the observed stability is
the cost of that accumulation rather than evidence of strength. Read a low
measured volatility in that state as a larger, later move — not a smaller one.
When the flag is null, one of its inputs is missing; that is not a false.

# --- Scoring mechanics ---

All scores are INTEGERS 0-100. Use precise values (37, 62, 81) — never round
to multiples of 5. Neighboring countries must be distinguishable.

Direction, stated explicitly because three of these four read as risk and one
does not:
  friction              higher = a worse wedge
  order_uncertainty     higher = less legible, less underwritable
  information_capacity  higher = WORSE instruments. Despite the name, this is
                        scored as risk: 90 means the statistics, auditors and
                        press cannot be trusted and official numbers need a
                        large haircut; 10 means they can be taken near face
                        value. A country with a strong statistical system
                        scores LOW here.
  edge_vitality         higher = MORE vitality, and this one is not risk. It is
                        the only score where a high number is a good thing, and
                        it must not raise score_3m or score_12m.

Scoring bands (guidance; use the full range):
  5-20 Low · 20-40 Low-Moderate · 40-75 Moderate · 75-90 High · 90-98 Extreme

Calibration anchors — composite scenarios, not real countries:
  ~12  Stable developed market: routine politics, ~2% inflation, no security
       events.
  ~38  EM with a contested but constitutional election, ~9% inflation,
       currency pressure, no violence.
  ~58  Sustained nationwide protests with sporadic violence, caretaker
       cabinet, ~20% inflation, FX reserves falling.
  ~85  Capital controls or default negotiations underway; unrest disrupting
       essential services.
  ~95  Interstate war on the country's territory, or nationwide shutdown.

# --- Localization & Materiality ---
Do NOT raise risk for indirect foreign tensions or rhetoric. Elevate risk
ONLY when evidence shows kinetic activity on the country's territory, imminent
hostilities, or economically binding policy affecting the country. Indirect
disputes, UN votes, or rhetoric without domestic transmission = low impact.

# --- Per-article impact and topic clustering (CRITICAL) ---
Impact is an INTEGER 0-100:
  85-100 Severe — successful kinetic activity in/against the country, mass
         kidnappings, binding economic measures, major infrastructure
         sabotage, seizure or rewriting of contracts, capture of the
         statistics office or the courts.
  60-75  Moderate — credible mobilization with specific capabilities or
         timelines, high-probability binding sanctions, a serious challenge
         to one of the load-bearing rules.
  40-55  Mixed/unclear — indirect third-country events, uncertain
         transmission.
  10-35  Low/benign — rhetoric, symbolic acts, alert-level changes without
         disruption, and anything that passes through none of the three doors.

You MUST assign the same topic_group to articles covering the same underlying
event, even when the headlines differ. Aggregation: within a topic_group take
the max impact. When calibrating ledger scores, weigh:
  • Persistence — the same topic_group across 7+ days (by published_at)
    counts one band higher.
  • Breadth — multiple independent severe topic_groups within a 30-day window
    justifies moving into High.
  • Singularity — a lone topic_group with no spread does not move the country
    into High on its own.

Example of SAME topic: "Australia Central Bank Holds Rates Steady" +
"RBA Decides Against Rate Cut" → both topic_group="australia_rba_rate_decision".
Example of DIFFERENT topics: that rate decision vs "Trade Deal with China"
(topic_group="australia_china_trade").

# --- Two horizons, scored independently ---
  score_3m  — investor risk over the next 3 months
  score_12m — investor risk over the next 12 months
Do not derive one from the other. Across both: friction sets the LEVEL,
order-uncertainty sets the WIDTH of the distribution around it, and information
sets the DRIFT — weak instruments mean a measured problem is more likely to
compound than to correct between now and the horizon.

# --- Condition flags: observations only ---
Report what the evidence shows. Nothing downstream will alter your scores, and
you must not adjust them to anticipate any rule. These flags are recorded next
to your scores, not applied to them.
  war_on_territory        sustained interstate war, or regular long-range
                          strikes on cities / critical infrastructure
  internal_conflict_level "none" | "A" recurring mass-casualty attacks
                          (20+ killed) or mass kidnappings, last 90 days,
                          across 3+ regions | "B" = A + repeated attacks on
                          critical infrastructure or major cities | "C" = B +
                          nationwide emergency effects (large displacement,
                          prolonged curfews, export shut-ins)
  emergency_rule          unconstitutional dissolution, martial law, or
                          week-long widespread violent unrest disrupting
                          essential services
  sovereign_stress        bank runs, capital controls, default negotiations
                          or missed payments

# --- Citations and coverage ---
For friction, order_uncertainty and information_capacity, cite the evidence ids
that drove the score — article ids like "a3", or indicator names exactly as
they appear in EVIDENCE_JSON.

evidence_coverage (0-100): how completely this evidence captures the country's
situation. Two thin wire stories about a G7 economy = low. Stale indicators and
absent ledgers lower it.

Return JSON exactly per the response schema: condition_flags, ledger_scores,
subscore_evidence, news_article_scores, score_3m, score_12m,
evidence_coverage, bullet_summary (at most 120 words: primary drivers and
meaningful mitigants).

bullet_summary must use role language throughout — "the country", "the central
bank", "the governing party" — and must never name a country, guess one, or hint
at which one it might be. A reader is shown this text beside the country's real
name, so a wrong guess is worse than no guess and a right one is still an
inference you were not entitled to make.
```

---

### Appendix — TR_2018-05-21

```text
### QUIET - verified-quiet window — TR 2018-05-21

articles selected: 20   full-text ids: ['a2', 'a3', 'a9']
prompt chars: 54798   ~tokens (chars/4): 13699
schema strict flag sent: True
payload_variant: p2   prompt_variant: 
mask_map_version: g5   sweep_version: 9f4aee55

--- payload_health ---
{
  "indicators": {
    "expected": 38,
    "resolved": 24,
    "by_ledger": {
      "friction": {
        "expected": 14,
        "resolved": 10
      },
      "uncertainty": {
        "expected": 16,
        "resolved": 11
      },
      "information": {
        "expected": 4,
        "resolved": 1
      },
      "edge": {
        "expected": 4,
        "resolved": 2
      }
    },
    "empty_ledgers": [],
    "dropped": {
      "GOV.DEBT.DOMESTIC.SHARE": "no row",
      "GOV.DEBT.FX.SHARE": "no row",
      "HD.HCI.OVRL": "vintage bound",
      "INFORMAL.PCT.GDP": "no row",
      "NIIP.GDP": "no row",
      "OBS.SCORE": "no row",
      "OECD.PISA.MEAN": "no row",
      "OECD.TAX.WEDGE": "no row",
      "RESERVES.USD": "no row",
      "RSF.PRESS.SCORE": "no row",
      "STAT.TAX.TOP.RATE": "no row",
      "UN.EGDI": "no row",
      "UNWPP.DPND.OL.PROJ": "no row",
      "WUI.INDEX": "no row"
    }
  },
  "trends": {
    "trend_1y": 23,
    "trend_5y": 13,
    "history": 2,
    "of": 24
  },
  "blocks": {
    "computed": 8,
    "edge_inputs": 2,
    "friction_inputs": 10,
    "information_inputs": 1,
    "structural": 6,
    "uncertainty_inputs": 12
  },
  "articles": {
    "articles": 20,
    "by_theme": {
      "friction": 4,
      "order": 7,
      "security": 2,
      "information": 2,
      "edge": 2,
      "broad": 3
    },
    "thin_themes": [],
    "theme_floor": 2,
    "by_tier": {
      "full": 15,
      "abstract-only": 5
    },
    "with_body": 15,
    "clipped_at_max": 0
  }
}

--- evidence payload (pre-mask, as built) ---
{
  "_meta": {
    "country": "TR",
    "as_of": "2018-05-21",
    "vintage_scheme": "point-in-time",
    "staleness_basis": "staleness_days counts from the end of the period a value describes to as_of: how old the reading is. `as_of` on each value is a separate fact — when it became known to us. A large staleness_days means the reading is old, not that it is wrong.",
    "next_scheduled_election": null
  },
  "friction_inputs": {
    "Government effectiveness (z-score)": {
      "value": 0.2092,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WGI",
      "unit": "z-score",
      "trend_1y": -0.1239
    },
    "Tax revenue (% GDP)": {
      "value": 18.328,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WDI",
      "unit": "% GDP",
      "trend_1y": 0.1861
    },
    "Political corruption index (0–1, higher = more corrupt)": {
      "value": 0.837,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "index (0–1)",
      "trend_1y": 0.01,
      "trend_5y": 0.151
    },
    "Interest payments (% revenue)": {
      "value": 6.542,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "% revenue",
      "trend_1y": 0.6617,
      "trend_5y": -1.8479
    },
    "Income inequality (Gini)": {
      "value": 43.5,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "index",
      "trend_1y": 1.6,
      "trend_5y": 3.3
    },
    "Government gross debt (% GDP)": {
      "value": 28.311,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 506,
      "source": "IMF WEO 2018-04",
      "unit": "% GDP",
      "trend_1y": 0.668,
      "trend_5y": -8.168
    },
    "Government net lending/borrowing (% GDP)": {
      "value": -2.328,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 506,
      "source": "IMF WEO 2018-04",
      "unit": "% GDP",
      "trend_1y": -1.063,
      "trend_5y": -1.641
    },
    "Old-age dependency ratio": {
      "value": 11.2974,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WDI",
      "unit": "% working-age population",
      "trend_1y": 0.1786
    },
    "Labour-force participation (% 15+)": {
      "value": 52.0,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WDI",
      "unit": "%",
      "trend_1y": 0.7
    },
    "Broad money growth (% y/y)": {
      "value": 17.6488,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WDI",
      "unit": "% y/y",
      "trend_1y": 1.1341
    }
  },
  "uncertainty_inputs": {
    "Real GDP growth (% y/y)": {
      "value": 3.184,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 506,
      "source": "IMF WEO 2018-04",
      "unit": "% y/y",
      "trend_1y": -2.902,
      "trend_5y": -7.929
    },
    "Current account balance (% GDP)": {
      "value": -3.838,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-04-01",
      "staleness_days": 506,
      "source": "IMF WEO 2018-04",
      "unit": "% GDP",
      "trend_1y": -0.102,
      "trend_5y": 5.099
    },
    "Inflation (% y/y)": {
      "value": 10.2346,
      "period": "2018-03",
      "freq": "M",
      "as_of": "2018-04-25",
      "staleness_days": 51,
      "source": "IMF CPI",
      "unit": "% y/y",
      "trend_1y": -1.0572,
      "trend_5y": 1.3426,
      "history": {
        "2008": 10.44,
        "2009": 6.25,
        "2010": 8.57,
        "2011": 6.47,
        "2012": 8.89,
        "2013": 7.49,
        "2014": 8.86,
        "2015-01": 7.24,
        "2015-02": 7.55,
        "2015-03": 7.61,
        "2015-04": 7.91,
        "2015-05": 8.09,
        "2015-06": 7.2,
        "2015-07": 6.81,
        "2015-08": 7.14,
        "2015-09": 7.95,
        "2015-10": 7.58,
        "2015-11": 8.1,
        "2015": 7.67,
        "2015-12": 8.81,
        "2016-01": 9.58,
        "2016-02": 8.78,
        "2016-03": 7.46,
        "2016-04": 6.57,
        "2016-05": 6.58,
        "2016-06": 7.64,
        "2016-07": 8.79,
        "2016-08": 8.05,
        "2016-09": 7.28,
        "2016-10": 7.16,
        "2016-11": 7.0,
        "2016": 7.78,
        "2016-12": 8.53,
        "2017-01": 9.22,
        "2017-02": 10.13,
        "2017-03": 11.29,
        "2017-04": 11.87,
        "2017-05": 11.72,
        "2017-06": 10.9,
        "2017-07": 9.79,
        "2017-08": 10.68,
        "2017-09": 11.2,
        "2017-10": 11.9,
        "2017-11": 12.98,
        "2017": 11.14,
        "2017-12": 11.92,
        "2018-01": 10.35,
        "2018-02": 10.26,
        "2018-03": 10.23
      }
    },
    "Political stability (z-score)": {
      "value": -1.7062,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "z-score",
      "trend_1y": 0.0739,
      "trend_5y": -0.7865
    },
    "Rule of law (z-score)": {
      "value": -0.6154,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "z-score",
      "trend_1y": -0.0695,
      "trend_5y": -0.5092
    },
    "GDP per-capita growth (% y/y)": {
      "value": 6.4366,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "% y/y",
      "trend_1y": 4.4938,
      "trend_5y": 2.9565,
      "history": {
        "2008": -0.35,
        "2009": -6.19,
        "2010": 6.91,
        "2011": 9.36,
        "2012": 3.48,
        "2013": 7.1,
        "2014": 3.2,
        "2015": 4.4,
        "2016": 1.94,
        "2017": 6.44
      }
    },
    "Unemployment (% labour force)": {
      "value": 10.919,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "%",
      "trend_1y": 0.02,
      "trend_5y": 1.709
    },
    "FDI inflow (% GDP)": {
      "value": 1.2953,
      "period": "2017",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 141,
      "source": "World Bank panel",
      "unit": "% GDP",
      "trend_1y": -0.2934,
      "trend_5y": -0.2571
    },
    "Short-term external debt (% reserves)": {
      "value": 85.8088,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WDI",
      "unit": "% reserves",
      "trend_1y": -8.6073
    },
    "Exchange rate vs USD": {
      "value": 4.048,
      "period": "2018-04",
      "freq": "M",
      "as_of": "2018-04-30",
      "staleness_days": 21,
      "source": "BIS XRU",
      "unit": "local currency per USD",
      "trend_1y": 0.5022
    },
    "Policy rate (%)": {
      "value": 8.0,
      "period": "2018-04",
      "freq": "M",
      "as_of": "2018-04-30",
      "staleness_days": 21,
      "source": "BIS CBPOL",
      "unit": "% per year",
      "trend_1y": 0.0
    },
    "suppressed_vol_flag": {
      "value": null,
      "regime": null,
      "fx_volatility_24m": 3.648354,
      "reserves_trend_6m": null,
      "note": "True means measured calm is being bought with reserves under a managed or pegged regime. null means one of the three inputs is unavailable — not that the flag is false."
    }
  },
  "information_inputs": {
    "Statistical performance (0–100)": {
      "value": 72.6454,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank SPI",
      "unit": "score 0–100"
    }
  },
  "edge_inputs": {
    "New business density (per 1,000 working-age)": {
      "value": 1.1915,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WDI",
      "unit": "per 1,000 working-age adults",
      "trend_1y": -0.0732
    },
    "Government education spending (% GDP)": {
      "value": 4.6277,
      "period": "2016",
      "freq": "A",
      "as_of": "2017-12-31",
      "staleness_days": 506,
      "source": "World Bank WDI",
      "unit": "% of GDP",
      "trend_1y": 0.3222
    }
  },
  "computed": {
    "conversion_loss": 0.6476,
    "frictional_extraction": 11.8692,
    "monetary_dilution": 11.2121,
    "real_policy_rate": -2.2346,
    "cpi_volatility_36m": 1.82695,
    "fx_volatility_24m": 3.648354,
    "precommitted_share": {
      "value": 6.542,
      "partial": true
    },
    "dependency_trajectory": {
      "current": 11.2974,
      "projected_10y": null,
      "delta": null
    }
  },
  "structural": {
    "region": "Europe",
    "income_group": "upper-middle",
    "commodity_exporter": false,
    "monetary_sovereignty": "full",
    "reserve_currency": "none",
    "note": "Facts about this country that do not change year to year, supplied because the country is not named. Reason from these rather than from any guess about which country this is."
  }
}

========================================================================
THE EXACT PROMPT DISPATCHED
========================================================================
You are a senior sovereign risk analyst. Assess investor risk for the country
as of 2018-05-21, using ONLY the evidence below.
Treat 2018-05-21 as today: this evidence is your complete knowledge of the
world. Do not use anything you know about events after this date.

Every value in EVIDENCE_JSON carries `as_of` and `staleness_days` — the date it
became known and how old it is on 2018-05-21. Weigh a fresh reading more than
a stale one, and say so when a stale one is carrying an argument. A missing
indicator is absent from the evidence entirely; treat absence as absence, never
as zero and never as reassurance.

# --- The country is not named, deliberately ---
This evidence describes a real country whose identity has been withheld from
you. Country names, cities, people, parties, currencies and institutions have
been replaced by the roles they play: "the country", "the capital", "the central
bank", "the finance minister", "the local currency". Every NUMBER is untouched —
inflation prints, rates, counts and dates are exactly as published.

Reason only from what is on the page. Do not try to work out which country this
is, and do not let a guess do any work in your reasoning: an inference that
depends on having identified the country is unsound here even when the guess
happens to be right, because you cannot check it and neither can anyone reading
your output.

The priors a name would have carried are supplied instead. When EVIDENCE_JSON
contains a `structural` block, it states what identity used to imply — whether
the government borrows in a currency it can issue, whether it can devalue at
all, its income group, its coarse region, whether it depends on commodity
exports. Use those facts directly. A debt burden means one thing for a
`monetary_sovereignty: full` issuer of a `reserve_currency: major` and something
different for a `constrained` borrower whose debt is in money it cannot print;
read the block, do not reconstruct it from a hunch about the name. When the
block is absent, that structure is simply unknown — treat it as absent, the same
as any missing indicator, and do not substitute a guess.

EVIDENCE_JSON
{"_meta": {"country": "the country", "as_of": "2018-05-21", "vintage_scheme": "point-in-time", "staleness_basis": "staleness_days counts from the end of the period a value describes to as_of: how old the reading is. `as_of` on each value is a separate fact — when it became known to us. A large staleness_days means the reading is old, not that it is wrong.", "next_scheduled_election": null}, "friction_inputs": {"Government effectiveness (z-score)": {"value": 0.2092, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WGI", "unit": "z-score", "trend_1y": -0.1239}, "Tax revenue (% GDP)": {"value": 18.328, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WDI", "unit": "% GDP", "trend_1y": 0.1861}, "Political corruption index (0–1, higher = more corrupt)": {"value": 0.837, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "index (0–1)", "trend_1y": 0.01, "trend_5y": 0.151}, "Interest payments (% revenue)": {"value": 6.542, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "% revenue", "trend_1y": 0.6617, "trend_5y": -1.8479}, "Income inequality (Gini)": {"value": 43.5, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "index", "trend_1y": 1.6, "trend_5y": 3.3}, "Government gross debt (% GDP)": {"value": 28.311, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 506, "source": "IMF WEO 2018-04", "unit": "% GDP", "trend_1y": 0.668, "trend_5y": -8.168}, "Government net lending/borrowing (% GDP)": {"value": -2.328, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 506, "source": "IMF WEO 2018-04", "unit": "% GDP", "trend_1y": -1.063, "trend_5y": -1.641}, "Old-age dependency ratio": {"value": 11.2974, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WDI", "unit": "% working-age population", "trend_1y": 0.1786}, "Labour-force participation (% 15+)": {"value": 52.0, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WDI", "unit": "%", "trend_1y": 0.7}, "Broad money growth (% y/y)": {"value": 17.6488, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WDI", "unit": "% y/y", "trend_1y": 1.1341}}, "uncertainty_inputs": {"Real GDP growth (% y/y)": {"value": 3.184, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 506, "source": "IMF WEO 2018-04", "unit": "% y/y", "trend_1y": -2.902, "trend_5y": -7.929}, "Current account balance (% GDP)": {"value": -3.838, "period": "2016", "freq": "A", "as_of": "2018-04-01", "staleness_days": 506, "source": "IMF WEO 2018-04", "unit": "% GDP", "trend_1y": -0.102, "trend_5y": 5.099}, "Inflation (% y/y)": {"value": 10.2346, "period": "2018-03", "freq": "M", "as_of": "2018-04-25", "staleness_days": 51, "source": "IMF CPI", "unit": "% y/y", "trend_1y": -1.0572, "trend_5y": 1.3426, "history": {"2008": 10.44, "2009": 6.25, "2010": 8.57, "2011": 6.47, "2012": 8.89, "2013": 7.49, "2014": 8.86, "2015-01": 7.24, "2015-02": 7.55, "2015-03": 7.61, "2015-04": 7.91, "2015-05": 8.09, "2015-06": 7.2, "2015-07": 6.81, "2015-08": 7.14, "2015-09": 7.95, "2015-10": 7.58, "2015-11": 8.1, "2015": 7.67, "2015-12": 8.81, "2016-01": 9.58, "2016-02": 8.78, "2016-03": 7.46, "2016-04": 6.57, "2016-05": 6.58, "2016-06": 7.64, "2016-07": 8.79, "2016-08": 8.05, "2016-09": 7.28, "2016-10": 7.16, "2016-11": 7.0, "2016": 7.78, "2016-12": 8.53, "2017-01": 9.22, "2017-02": 10.13, "2017-03": 11.29, "2017-04": 11.87, "2017-05": 11.72, "2017-06": 10.9, "2017-07": 9.79, "2017-08": 10.68, "2017-09": 11.2, "2017-10": 11.9, "2017-11": 12.98, "2017": 11.14, "2017-12": 11.92, "2018-01": 10.35, "2018-02": 10.26, "2018-03": 10.23}}, "Political stability (z-score)": {"value": -1.7062, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "z-score", "trend_1y": 0.0739, "trend_5y": -0.7865}, "Rule of law (z-score)": {"value": -0.6154, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "z-score", "trend_1y": -0.0695, "trend_5y": -0.5092}, "GDP per-capita growth (% y/y)": {"value": 6.4366, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "% y/y", "trend_1y": 4.4938, "trend_5y": 2.9565, "history": {"2008": -0.35, "2009": -6.19, "2010": 6.91, "2011": 9.36, "2012": 3.48, "2013": 7.1, "2014": 3.2, "2015": 4.4, "2016": 1.94, "2017": 6.44}}, "Unemployment (% labour force)": {"value": 10.919, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "%", "trend_1y": 0.02, "trend_5y": 1.709}, "FDI inflow (% GDP)": {"value": 1.2953, "period": "2017", "freq": "A", "as_of": "2017-12-31", "staleness_days": 141, "source": "World Bank panel", "unit": "% GDP", "trend_1y": -0.2934, "trend_5y": -0.2571}, "Short-term external debt (% reserves)": {"value": 85.8088, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WDI", "unit": "% reserves", "trend_1y": -8.6073}, "Exchange rate vs another country": {"value": 4.048, "period": "2018-04", "freq": "M", "as_of": "2018-04-30", "staleness_days": 21, "source": "BIS XRU", "unit": "local currency per another country", "trend_1y": 0.5022}, "Policy rate (%)": {"value": 8.0, "period": "2018-04", "freq": "M", "as_of": "2018-04-30", "staleness_days": 21, "source": "BIS CBPOL", "unit": "% per year", "trend_1y": 0.0}, "suppressed_vol_flag": {"value": null, "regime": null, "fx_volatility_24m": 3.648354, "reserves_trend_6m": null, "note": "True means measured calm is being bought with reserves under a managed or pegged regime. null means one of the three inputs is unavailable — not that the flag is false."}}, "information_inputs": {"Statistical performance (0–100)": {"value": 72.6454, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank SPI", "unit": "score 0–100"}}, "edge_inputs": {"New business density (per 1,000 working-age)": {"value": 1.1915, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WDI", "unit": "per 1,000 working-age adults", "trend_1y": -0.0732}, "Government education spending (% GDP)": {"value": 4.6277, "period": "2016", "freq": "A", "as_of": "2017-12-31", "staleness_days": 506, "source": "World Bank WDI", "unit": "% of GDP", "trend_1y": 0.3222}}, "computed": {"conversion_loss": 0.6476, "frictional_extraction": 11.8692, "monetary_dilution": 11.2121, "real_policy_rate": -2.2346, "cpi_volatility_36m": 1.82695, "fx_volatility_24m": 3.648354, "precommitted_share": {"value": 6.542, "partial": true}, "dependency_trajectory": {"current": 11.2974, "projected_10y": null, "delta": null}}, "structural": {"region": "Europe", "income_group": "upper-middle", "commodity_exporter": false, "monetary_sovereignty": "full", "reserve_currency": "none", "note": "Facts about this country that do not change year to year, supplied because the country is not named. Reason from these rather than from any guess about which country this is."}}

ARTICLES_JSON
[{"id": "a1", "source": "guardian", "published_at": "2018-05-14", "title": "the leader blames a foreign leader for returning the world to 'dark days'", "digest": {"actors": "the leader criticized the governing party's decisions and called for the main opposition party to do more for refugees", "numbers": "3.5 million, 24 June, 2015", "masked_title": "the leader blames a foreign leader for returning the world to 'dark days'", "transmission": "refugee deal", "what_happened": "The leader criticized international decisions and called for more support for refugees during a state visit.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a2", "source": "guardian", "published_at": "2018-05-18", "title": "the country protests: the leader accuses the international community of hypocrisy - Friday 7 June", "digest": {"actors": "the leader accused the international community; the regional enlargement commissioner suggested the country's prospects for regional accession depend on the leader's actions; the police have been accused of brutality towards media workers; at least 14 journalists have been injured; the leader vowed to press ahead with redevelopment despite protests.", "numbers": "14", "masked_title": "the country protests: the leader accuses the international community of hypocrisy - Friday 7 June", "transmission": "the country's prospects for regional accession depend on the leader's actions regarding the protests", "what_happened": "The country's leader has accused the international community of hypocrisy over its criticism of his government's handling of anti-government protests, while the regional enlargement commissioner suggested that the country's prospects for regional accession depend on how the leader deals with the protests.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a3", "source": "nyt", "published_at": "2018-04-21", "title": "Tiny Islands Make for Big Tensions Between a Foreign Country and the Country", "digest": {"actors": "the military ships and jets of the country did incursions into the territory of another country", "numbers": "20 years", "masked_title": "Tiny Islands Make for Big Tensions Between a Foreign Country and the Country", "transmission": "not stated", "what_happened": "Incursions by the military ships and jets of the country into the territory of another country have spiked.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a4", "source": "nyt", "published_at": "2018-05-18", "title": "Ties With a foreign country Sour as the leader Seizes Gaza Issue Before Election", "digest": {"actors": "the leader hopes to position himself as a champion of the Palestinians and leader of the Muslim world", "numbers": "not stated", "masked_title": "Ties With a foreign country Sour as the leader Seizes Gaza Issue Before Election", "transmission": "not stated", "what_happened": "The country’s leader hopes to position himself as a champion of the Palestinians and leader of the Muslim world through a rally, harsh words, and a recall of ambassadors.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a5", "source": "nyt", "published_at": "2018-05-16", "title": "the country's financial institution leader in sanctions-busting case sentenced to 32 months", "digest": {"actors": "the trial depicted high-level corruption in the country, and strained that country’s relations with another country", "numbers": "32", "masked_title": "the country's financial institution leader in sanctions-busting case sentenced to 32 months", "transmission": "not stated", "what_happened": "The trial depicted high-level corruption in the country and strained that country’s relations with another country.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a6", "source": "guardian", "published_at": "2018-05-15", "title": "the leader ends another state visit by calling jailed journalists 'terrorists'", "digest": {"actors": "the leader insisted that journalists locked in the country's jails were terrorist criminals, the finance minister warned the leader not to lose sight of democratic values, the finance minister urged the leader to extradite exiles, the leader stated that the judiciary is prosecuting individuals associated with terrorism, the finance minister hailed an agreement to improve cooperation over extradition, the leader's remarks caused the local currency to plunge, and protesters demonstrated against the leader’s visit.", "numbers": "more than 160 journalists, 6,000 foreign fighters, $15bn, $20bn, 24 June, double-digit inflation", "masked_title": "the leader ends another state visit by calling jailed journalists 'terrorists'", "transmission": "trade increase and extradition cooperation", "what_happened": "the leader ended a state visit by insisting that journalists in jail were terrorists, while ignoring warnings about democratic values.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a7", "source": "guardian", "published_at": "2018-05-13", "title": "Campaigners call for a foreign country to act on rights as the leader arrives", "digest": {"actors": "the leader insisted that relations were improving, while human rights campaigners and opposition politicians called for denouncement of the government's actions against journalists and opposition figures.", "numbers": "$16bn, £11.8bn, $20bn, £14.7bn, 24 June, two years", "masked_title": "Campaigners call for a foreign country to act on rights as the leader arrives", "transmission": "economic relations and trade deals", "what_happened": "The leader began a three-day state visit to a foreign country amid claims of human rights abuses in pursuit of a trade deal.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a8", "source": "guardian", "published_at": "2018-04-23", "title": "a foreign country angrily rejects the proposed soldier swap", "digest": {"actors": "the leader proposed an exchange to the other leader, who rejected it; the leader claimed the soldiers fled after a coup; the military command argued the guards accidentally crossed the border; the other leader described the exchange idea as inconceivable.", "numbers": "1,500", "masked_title": "a foreign country angrily rejects the proposed soldier swap", "transmission": "not stated", "what_happened": "The leader proposed exchanging two detained border guards for eight officers seeking asylum in a foreign country, which was rejected by the other leader.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a9", "source": "guardian", "published_at": "2018-05-08", "title": "'I'll continue writing the truth': the editor taking on the leader", "digest": {"actors": "the editor criticized the leader and faced attacks from a mob incited by the leader; the prime minister appealed to the counterpart to control dissent; the leader denounced the newspaper at a rally; the police intervened after the leader of the enclave intervened; thousands of citizens protested against the violence.", "numbers": "150 journalists jailed; 35,000 troops stationed; circulation of the newspaper is about 2,000; six attackers sentenced to jail terms of between two and six months; nine attackers remain at large; 40-mile channel separates the country from a neighbouring country.", "masked_title": "'I'll continue writing the truth': the editor taking on the leader", "transmission": "not stated", "what_happened": "An editor of a newspaper continues to criticize the country's leader despite facing violent attacks and threats.", "stage1_severity": 60, "directly_about_country": true}, "stage1_severity": 60.0}, {"id": "a10", "source": "guardian", "published_at": "2018-05-03", "title": "the leader's iron lady: 'It's time for the men in power to feel fear'", "digest": {"actors": "the leader of the main opposition party criticized the ruling party of the leader and addressed farmers in the crowd, while the leader's party has dominated the country's politics for 16 years.", "numbers": "24 June, 2016, 2002, 17 months", "masked_title": "the leader's iron lady: 'It's time for the men in power to feel fear'", "transmission": "not stated", "what_happened": "The leader of the main opposition party emerged as a credible challenger to the incumbent leader ahead of snap elections.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a11", "source": "nyt", "published_at": "2018-05-09", "title": "Five Top Militant Group Officials Captured in a Sting Operation", "digest": {"actors": "the intelligence agency and local intelligence used the aide to lure other operatives", "numbers": "not stated", "masked_title": "Five Top Militant Group Officials Captured in a Sting Operation", "transmission": "not stated", "what_happened": "An aide to the leader of a militant group was captured and used to lure other operatives.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a12", "source": "guardian", "published_at": "2018-04-23", "title": "the region bounces back as package holiday favourite", "digest": {"actors": "A large travel company reported that families account for 61% of bookings to the region, and the managing director of the large travel company stated that it is the standout destination for summer 2018.", "numbers": "84%, 61%, 2016, 2017, 2018, 89%, 2015, 11, 7.5%, 1, 1 June, 300km", "masked_title": "the region bounces back as package holiday favourite", "transmission": "economic uncertainty and the popularity of package holidays", "what_happened": "The region is experiencing a significant increase in package holiday bookings, with a year-on-year growth of 84%.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a13", "source": "guardian", "published_at": "2018-05-18", "title": "A recall is a decadent pick that’s perfect for our times | a sports columnist", "digest": {"actors": "the governing party and the selectors made decisions regarding the cricket team, while the central bank governor commented on the popularity of cricket among children.", "numbers": "5 deaths, 10 million in damage, 7 gallons of oil, 389 runs, 258 balls, 97 average, 17 average in four games, 6th biggest sporting league", "masked_title": "A recall is a decadent pick that’s perfect for our times | a sports columnist", "transmission": "not stated", "what_happened": "A long-running corruption investigation highlights the dangers of deep-frying traditions in a foreign country, leading to fatalities and significant damage.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a14", "source": "nyt", "published_at": "2018-05-06", "title": "the leading independent newspaper perseveres with a smile", "digest": {"actors": "the leading independent newspaper went back to work after 14 of its staff members were convicted of aiding terrorism", "numbers": "14", "masked_title": "the leading independent newspaper perseveres with a smile", "transmission": "not stated", "what_happened": "The leading independent newspaper resumed operations after 14 of its staff members were convicted of aiding terrorism.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a15", "source": "guardian", "published_at": "2018-05-06", "title": "The Displaced; Migrant Brothers; Lights in the Distance – reviews", "digest": {"actors": "the governing party and the opposition leader responded to public sentiment regarding refugees and migrants, while various writers and journalists documented the experiences of displaced people.", "numbers": "8,500 people drowned or disappeared trying to cross a major sea; 65 million displaced people in the world; 15 countries had walls or fences at their borders in 1990, which rose to 70 by 2016.", "masked_title": "The Displaced; Migrant Brothers; Lights in the Distance – reviews", "transmission": "not stated", "what_happened": "The body of a three-year-old boy washed ashore, prompting a temporary shift in public empathy towards refugees in a region.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a16", "source": "guardian", "published_at": "2018-05-17", "title": "World Cup Fiver | a foreign country’s proud and totally sacrosanct national football team", "digest": {"actors": "the governing party and the president of the Football Federation defended the inclusion of the player against critics, while the player expressed gratitude for support from fans.", "numbers": "78, 26, 5, 30 March, 16 May, 1,057, 143, 3,975", "masked_title": "World Cup Fiver | a foreign country’s proud and totally sacrosanct national football team", "transmission": "not stated", "what_happened": "The inclusion of a player in the national pre-World Cup training squad has raised questions about commercial interests influencing team selections.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a17", "source": "guardian", "published_at": "2018-05-15", "title": "the governing body denies a player included in World Cup squad for commercial reasons", "digest": {"actors": "the governing body denied the selection of a player is linked to a commercial partnership after the player was revealed as the focus of a marketing campaign for the fuel company, a day before the coach confirmed a list of players still in contention for the World Cup.", "numbers": "38, 26, 3, 100, 10, 48, 2013, 32, 26, 8, 13", "masked_title": "the governing body denies a player included in World Cup squad for commercial reasons", "transmission": "not stated", "what_happened": "The governing body has denied that the selection of a player in the national team's World Cup squad is linked to a commercial partnership with a fuel company.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a18", "source": "guardian", "published_at": "2018-05-14", "title": "In-form striker axed from national team World Cup squad", "digest": {"actors": "the coach cut the striker from the preliminary squad before the tournament camp; the striker hinted his season was over; the coach informed players about their inclusion in the squad; the coach confirmed his selections; the coach admitted the decisions were not easy; the coach expressed confidence in the squad's strength; players will head to a training camp and play friendlies before the tournament.", "numbers": "32, 26, 23, 4, 1, 9, 33, 36, 12, 22, 3", "masked_title": "In-form striker axed from national team World Cup squad", "transmission": "not stated", "what_happened": "The striker was cut from the preliminary squad for the World Cup despite a recent hat-trick and strong performance.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a19", "source": "guardian", "published_at": "2018-05-03", "title": "World Cup stunning moments: the Miracle of a neighbouring country", "digest": {"actors": "the national football team from one country lost to the national football team from a neighbouring country", "numbers": "2-0, 8-3, 11, 1943, 1944, 1949, 1950, 1954, 400,000, 18, 49, 1933-45", "masked_title": "World Cup stunning moments: the Miracle of a neighbouring country", "transmission": "not stated", "what_happened": "A national football team from one country lost to a team from a neighbouring country in the World Cup final after leading 2-0 early in the match.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a20", "source": "guardian", "published_at": "2018-04-25", "title": "A school that shows good food is not just for privileged children", "digest": {"actors": "the headteacher ensures children receive meals, the dinner lady serves food, and the head of the school dinner department manages the meal quality and sourcing.", "numbers": "11, 30, 14, 65, 2.10, 20, 17, 40, 60, 25, 96, 70, 86, 125, 3, 6, 7, 40, 20, 60", "masked_title": "A school that shows good food is not just for privileged children", "transmission": "not stated", "what_happened": "A primary school in a town is providing high-quality meals to children despite budget constraints and local poverty.", "stage1_severity": 25, "directly_about_country": false}, "stage1_severity": 25.0}]

FULL_TEXT
--- id: a2 · the country protests: the leader accuses the international community of hypocrisy - Friday 7 June ---
Here's a summary of the main events today: • The country's leader has accused the international community of hypocrisy over its criticism of his government's handling of anti-government protests. Speaking at an EU conference in a major city, he asked "where was the outrage over tear gas" used at another country's Occupy movement, as well as in another country and in another country. • The EU's enlargement commissioner has suggested that the country's prospects for EU accession hang on how the leader deals with the protests. Speaking minutes before the leader, he said: "Peaceful demonstrations constitute a legitimate way for these groups to express their views in a democratic society. Excessive use of force by police against these demonstrations has no place in such a democracy." • Former another country presidential candidate has portrayed the protest movement in the country as a secular rebellion. In remarks to a seminar, he said: "It’s pretty clear that this was a rebellion against the leader’s push of the country's people towards Islam." • The leader delivered a fiery speech on his return to the country, telling supporters who thronged to greet him that the protests that have swept the country must end. Addressing crowds at a major city airport from an open-top bus after returning from a trip to another region, the leader called on his ruling party faithful to show restraint and distance themselves from "dirty games" and "lawless protests." • Earlier, the leader vowed to press ahead with the controversial redevelopment of a square in a major city, in a move that puts him on a collision course with tens of thousands of anti-government protesters and could provoke further unrest across the country. Speaking in another city before flying back to a major city, the leader acknowledged that some of those who had defended a major city's park had acted for genuine environmental reasons. But he also said "terror groups" were behind the country's biggest demonstrations in years and hinted at a plot involving radical Marxist-Leninists. • At least 14 journalists have been injured, some seriously, since the outbreak of violent protests in the country. The offices of media organisations have also come under attack. The police have been accused of brutality towards media workers who have been covering the demonstrations against the development of the park on a central square. Journalists report suffering from the effects of tear gas and water hoses. offers this digested version of the leader's speech: Here's some other instant summaries: The leader compares how the country's police handled the protests with how another country and another country authorities dealt with the Occupy movement. On the protests, the leader continues his attack on social media. The leader began his speech by criticising the EU's treatment of the country. Here's a selection of updates from some of those live tweeting the speech: and have more on the commissioner's speech in a major city: The full text of the commissioner's speech is available here. These are the key passages: It is difficult not to mention events that have been taking place since over a week only a few hundred metres from where we convene today. The duty of all of us, EU Members as much as those countries that wish to become one, is to aspire to the highest possible democratic standards and practices. These include the freedom to express one's opinion, the freedom to assemble peacefully and freedom of media to report on what is happening as it is happening. Best practices include close attention to the needs and expectations of society, including that of groups that don't feel represented by the Parliamentary majority. Peaceful demonstrations constitute a legitimate way for these groups to express their views in a democratic society. Excessive use of force by police against these demonstrations has no place in such a democracy. I am happy that even the government admitted that. What is important now, is not only to launch swift and transparent investigation but also to bring those responsible to account. Democracy is a demanding discipline – not only during election campaigns, but every day. It requires debates, consultation and compromise. Since the beginning of my mandate, I have admired the openness and passion of debates in the country. I sincerely wish this to be preserved, but also translated into harmonious and effective decision making. Energising the EU accession process and strengthening democracy by respecting rights and freedoms are two sides of the same coin ... EU values, EU accession - everything is linked. And here I stand, in front of you, and today let me – by repeating your own words – call on the country “not to give up on its values” of freedom and fundamental rights. And let me assure you that we, on our side, have no intention to “give up on the country's EU accession". The country must investigate whether police used excessive force in the crackdown on protesters and hold those responsible to account, according to the EU enlargement commissioner, reports. "Peaceful demonstrations constitute a legitimate way for ... groups to express their views in a democratic society. Excessive use of force by police against these demonstrations has no place in such a democracy," the commissioner said in a speech at a conference attended by the country's leader. "I am happy that even the government admitted that. What is important now, is not only to launch a swift and transparent investigation but also to bring those responsible to account." On Wednesday, the commissioner urged the country's government to listen to the protesters. has more from the conference: Former another country presidential candidate has portrayed the protest movement in the country as a secular rebellion against the leader's "push toward Islam". In remarks to a seminar, he said: It’s pretty clear that this was a rebellion against the leader’s push of the country's people towards Islam … I think this was a rebellion against what the leader was trying, to push a very modern nation and democracy in a direction which they did not want to go … The restrictions on alcohol, children in Islamic-oriented schools ... And there are more journalists jailed in the country than any other country in the region. There is no doubt he has intimidated both print media as well as other media by this business of suing them … I hope he [the leader] understands that some of the tactics used by the police are way over the top. I think the leader, in the view of many of the country's people, is becoming more like a dictator than a leader ... The leader is due to make another speech within the hour. While we wait, has an English translation of the leader's speech to his supporters. The leader is to convene a meeting of the ruling party's key decision-making group to discuss the protests, according to a local news outlet. The leader is scheduled to hold consultations with the Central Decision and Administration Board on Saturday to seek ways to respond to the ongoing protests that have gone viral in the country following a modest environmental sit-in protest in a major city's park. Senior figures in the party have appeared divided over the approach to the party. The head of state and a deputy leader have taken a more conciliatory line than the leader. snaps the scene in the park. The leader's refusal to compromise is being blamed for the worst weekly decline on the country's stock market since 2008, reports. “Over the weekend we will probably see more confrontation between police and demonstrators as no conciliatory comments were received from the leader,” a hedge fund manager said in an e-mailed note. The country’s main equity gauge, the national stock exchange index, fell 0.5% to a six-month low, extending this week’s slide to 12%, the most since November 2008. The leader was critical of the financial institutions in his speech to supporters on his return. quoted him saying: The interest rate lobby thinks they can threaten us by entering into speculations in the stock exchange. They should know we will not let them abuse the nation's wealth. explains what is spooking the markets. Hundreds of billions of another country of short-term loans have been flowing into the country from investors in search of higher yielding assets, financing the very malls and skyscrapers that have so dismayed the small but growing coalition of secular intellectuals, left-of-center political activists and a smattering of the professional classes. What worries financial experts is that this so-called hot money can leave the country just as quickly as it arrived, touching off a currency crisis and, eventually, a collapse in the property markets that could threaten the nation’s banks. Here are some of the latest accounts of the protests submitted by readers to a local news outlet. highlights some of the religious language used by the leader when he addressed supporters at the airport. "No power but Allah can stop the country's rise," it quotes him saying. Speaking from an open-top bus at the airport, his spouse at his side, the leader acknowledged police might have used excessive force in crushing a small demonstration against a building project last Friday - the action that triggered nationwide protests against his long-standing rule. "However, no-one has the right to attack us through this. May Allah preserve our fraternity and unity. We will have nothing to do with fighting and vandalism...The secret to our success is not tension and polarisation." "The police are doing their duty. These protests, which have turned into vandalism and utter lawlessness must end immediately," the leader told the crowd. He gave no indication of any immediate plans to remove the makeshift protest camps that have appeared on the central square and a park in the capital. But the gatherings mark a clear challenge to his declarations. The agency also noted the reaction among protesters: At a major city's square, centre of the protests now occupied by thousands around the clock, some chanted "the leader resign" as they watched a broadcast of the address. In the capital, a local park echoed to anti-government slogans, while protesters danced or sang the national anthem. It also pointed out that not all the country's newspapers parroted the leader's words. A leftist publication's headline read: "The Deaf Sultan," accusing the leader of refusing to understand protesters' demands. A whistle-blower and anti-army publication said "the leader is burning the country," while a liberal outlet said "He doesn't give up." At the square, the mood remained defiant. Users have noted the remarkable similarity of headlines in the country's press following the leader’s return. Many of the front pages of the newspapers go with a leader quote pointing to his democratic legitimacy. We think his remark translates as: “We will die gladly for democratic demands.” If you have a better translation please let us know. A resident finds seven newspapers with the same headline. “Pravdaesque coincidence”, suggests a local commentator. We will be following developments in the country throughout the day after the leader’s defiant return to a major city. Here’s a summary of the latest developments: • The country's leader has delivered a fiery speech on his return to the country, telling supporters who thronged to greet him that the protests that have swept the country must end. Addressing crowds at a major city airport from an open-top bus after returning from a trip to another region, the leader called on his ruling party faithful to show restraint and distance themselves from "dirty games" and "lawless protests." • Earlier, the leader vowed to press ahead with the controversial redevelopment of a square in a major city, in a move that puts him on a collision course with tens of thousands of anti-government protesters and could provoke further unrest across the coun

--- id: a3 · Tiny Islands Make for Big Tensions Between a Foreign Country and the Country ---
Incursions by the country's military ships and jets into another country territory have spiked and the potential for conflict is the greatest it has been in 20 years.

--- id: a9 · 'I'll continue writing the truth': the editor taking on the leader ---
A determined editor is on a mission. He has survived being set upon by a mob and two gun attacks and has had a dead dog left at his door, but none has stopped the editor from taking the country’s leader to task. With evident pride, he says his the country's newspaper is the only the country's-language daily to denounce the capital’s military offensive against fighters in a neighbouring region. “I’ll continue writing the truth,” insisted the editor, who narrowly escaped being lynched in January when a mob of ultranationalists, incited by the leader, attacked the publication’s premises in the country-run area. “The the country's army went into the neighbouring region to commit a massacre and occupy the area, just as they did here.” Such defiance has not gone unnoticed. Media repression is at the root of growing international concern over the leader’s increasingly authoritarian style. More than 150 journalists have been jailed, many on tenuous terror charges, following a failed coup in July 2016. Criticism of the country’s cross-border operation is high on the list of perceived infractions, which is why the editor’s newspaper is in the leader’s sights. “It is a crime now to be a good person in the country because if you are good you are imprisoned,” he lamented, sitting behind a chaotic, paper-stacked desk. “It is even a crime to say ‘no’ to war in the country. No country is my enemy, but I am absolutely against the leader’s regime. Why did he start this war? Because he needs this war to consolidate his power.” The newspaper is one of 20 dailies produced in the area, where the capital has stationed 35,000 troops since invading in response to a coup aimed at union with another country in 1974. Run on a shoestring budget, its circulation is about 2,000. Its web presence is similarly limited. Even among the country’s citizens, the editor’s contrarian views – he refuses to submit to the capital’s line that 1974 was a “peace operation” – are prone to eliciting derision. His own office, down a long, bare, dark corridor, is decorated with tapestries of historical figures and other leftwing heroes who like him, the 70-year-old bohemian laughs, were once dismissed as “marginals” and “provocateurs”. The newspaper had originally been called a name meaning 'Europe' but was renamed after the area’s then-hardline regime forced it to close in 2001. “I wanted to call it New Europe, but they wouldn’t let me so I called it a name meaning 'Africa' to convey we have jungle rules here,” he said mischievously. But while the editor clearly delights in his role as a gadfly, the country’s citizens have rallied around him. As the leader seeks to crush dissent in the run-up to snap legislative and presidential elections in June, many are alarmed that the newspaper should be singled out. Within hours of ordering the assault on the neighbouring region, the leader spoke at a rally and denounced the newspaper as “cheap and nasty”. He criticised the publication for comparing the offensive to the country’s actions in 1974 and exhorted his “brothers in the area to give the necessary response”. The next day, the newspaper’s first-floor premises were viciously attacked as flag-waving protesters, hurling bottles and stones, took up his call. The country’s prime minister again raised the issue of the newspaper in March, appealing to his counterpart to bring “the unpleasant voices” under control. All of which leaves the editor, who was born and raised on the island when it was a colony, a little stunned. “It is crazy that such a powerful man should be afraid of such a small newspaper,” he said, his voice gravelly from cigarettes. “I am very proud that we are the only the country's-language newspaper writing about what is really happening in the neighbouring region.” The building housing the newspaper still bears scars from the attack. For more than a month after, the editor had the office’s smashed windows encased in thick chipboard, forcing staff to work in the semi-darkness. The balcony, which protesters tried to scale, was barricaded, enhancing the sense of siege. There is still blood on the wall and a gash in the office’s front door, a stark reminder of the bullet that pierced it when a would-be assassin turned up in 2011. The editor now has a CCTV monitor on his desk so he can keep an eye on the main entrance. Since the attack in January, he comes to work with a weapon. “I thought there’ll be a battle here if they come again,” he said. He said he is still shocked that it had taken so long for the police to stop the assault given the building’s location between the parliament, presidential office and the country’s embassy. Police only responded when the area’s president intervened. While distancing himself from the editor’s views, the president deplored the attack as an assault on freedom of expression. Soon after, thousands of the country’s citizens took to the streets to protest the country’s role in agitating “fascist” segments to silence the newspaper and other voices of dissent. Six of the attack’s protagonists were rounded up and sentenced to jail terms of between two and six months. Another nine remain at large. His case has been raised with a foreign entity by the government in the internationally-recognised area south. Although laws are suspended in the north, because of the island’s divided status, the country’s citizens are citizens of the foreign entity. The editor acknowledges that the channel that separates the island from the country offers a measure of protection that mainland critics do not have, but he worries that the light sentences will invite further violence at a time when even a wrongly worded tweet in the country is considered a crime. “Every day I ask, where is justice? Why haven’t the others been arrested?” he said. “An attack can come tonight, tomorrow, any time. “I have a particular philosophy. I am not afraid,” he said, adding that a famous speech about readiness guides him when the going gets tough. “A person lives once and dies once. If you are to die, at least die with honour.”

# --- The three ledgers ---

FRICTION is the wedge: what the state extracts multiplied by how much of it
fails to convert into capability. Judge the take by how it converts, not by its
size. A high tax burden that funds functioning courts, roads and registries is
not friction; a modest one that funds nothing is. `frictional_extraction` in
the computed block is that product, and `doom_loop` says whether the burden is
rising while conversion decays — trajectory matters more than level, because a
heavy but stable wedge can be carried indefinitely and a compounding one cannot.

ORDER-UNCERTAINTY is imposed doubt about the load-bearing rules — the ones
capital cannot price around: whether contracts will be enforced as written,
whether the currency will hold its function, whether the published statistics
mean what they say, and whether succession is settled. This is not the same as
volatility. A country can be turbulent and legible, or calm and unreadable; the
second is worse for an investor, because there is nothing to underwrite against.

INFORMATION is instrument quality, and it sets both trust and drift. Where the
statistical system, the auditors and the press are strong, official numbers can
be taken near face value. Where they are weak, official numbers deserve a
haircut: lean on market-observed series (exchange rates, policy rates, reserves)
and on article evidence instead, and treat measured friction as compounding
rather than mean-reverting — a state that cannot see itself does not self-correct.

# --- Edge vitality: report it, never penalize it ---

Entry-and-exit churn — startup formation AND startup failure — and human-capital
formation are the system learning. They MUST NOT raise any risk score. A country
where firms are born and die quickly is discovering what works; a country where
nothing is created and nothing fails is not stable, it is inert. Failure counts
as vitality here.

Learning outcomes lead; education spending is the effort line. Read them
together, never the spending alone. High spending with weak learning outcomes is
the wedge made visible inside a school system — money extracted and not
converted into capability. Read that gap as friction evidence, not as edge
credit.

Score `edge_vitality` as an independent reading of that adaptive capacity —
higher means more vitality — and do not let a high value raise friction,
order-uncertainty, or either horizon score.

# --- The three-door event test (apply to every article) ---

An event matters only if it passes through one of three doors:
  F — it changes the wedge (extraction, or how well extraction converts).
      Reported waves of skilled departure — doctors, engineers and founders
      leaving the country — pass through this door: the population grading the
      wedge with their feet. There is deliberately no data series for this, so
      these articles are its only instrument.
  U — it destabilizes the order (contracts, currency, statistics, succession)
  I — it changes the instruments (statistics office, auditors, courts, press)
Everything else is noise, however dramatic the headline. Natural disasters with
no fiscal or contractual aftermath, weapons demonstrations, military parades,
diplomatic insults, celebrity politics and scandal without institutional
consequence do not move a score. Name the door in your reasoning; if an article
passes through none of them, its impact is low no matter how prominent it is.

# --- Manufactured calm ---

When `suppressed_vol_flag` is true, measured calm is evidence AGAINST the
country, not for it. A currency held quiet under a managed or pegged regime
while reserves drain is accumulating fuel load, and the observed stability is
the cost of that accumulation rather than evidence of strength. Read a low
measured volatility in that state as a larger, later move — not a smaller one.
When the flag is null, one of its inputs is missing; that is not a false.

# --- Scoring mechanics ---

All scores are INTEGERS 0-100. Use precise values (37, 62, 81) — never round
to multiples of 5. Neighboring countries must be distinguishable.

Direction, stated explicitly because three of these four read as risk and one
does not:
  friction              higher = a worse wedge
  order_uncertainty     higher = less legible, less underwritable
  information_capacity  higher = WORSE instruments. Despite the name, this is
                        scored as risk: 90 means the statistics, auditors and
                        press cannot be trusted and official numbers need a
                        large haircut; 10 means they can be taken near face
                        value. A country with a strong statistical system
                        scores LOW here.
  edge_vitality         higher = MORE vitality, and this one is not risk. It is
                        the only score where a high number is a good thing, and
                        it must not raise score_3m or score_12m.

Scoring bands (guidance; use the full range):
  5-20 Low · 20-40 Low-Moderate · 40-75 Moderate · 75-90 High · 90-98 Extreme

Calibration anchors — composite scenarios, not real countries:
  ~12  Stable developed market: routine politics, ~2% inflation, no security
       events.
  ~38  EM with a contested but constitutional election, ~9% inflation,
       currency pressure, no violence.
  ~58  Sustained nationwide protests with sporadic violence, caretaker
       cabinet, ~20% inflation, FX reserves falling.
  ~85  Capital controls or default negotiations underway; unrest disrupting
       essential services.
  ~95  Interstate war on the country's territory, or nationwide shutdown.

# --- Localization & Materiality ---
Do NOT raise risk for indirect foreign tensions or rhetoric. Elevate risk
ONLY when evidence shows kinetic activity on the country's territory, imminent
hostilities, or economically binding policy affecting the country. Indirect
disputes, UN votes, or rhetoric without domestic transmission = low impact.

# --- Per-article impact and topic clustering (CRITICAL) ---
Impact is an INTEGER 0-100:
  85-100 Severe — successful kinetic activity in/against the country, mass
         kidnappings, binding economic measures, major infrastructure
         sabotage, seizure or rewriting of contracts, capture of the
         statistics office or the courts.
  60-75  Moderate — credible mobilization with specific capabilities or
         timelines, high-probability binding sanctions, a serious challenge
         to one of the load-bearing rules.
  40-55  Mixed/unclear — indirect third-country events, uncertain
         transmission.
  10-35  Low/benign — rhetoric, symbolic acts, alert-level changes without
         disruption, and anything that passes through none of the three doors.

You MUST assign the same topic_group to articles covering the same underlying
event, even when the headlines differ. Aggregation: within a topic_group take
the max impact. When calibrating ledger scores, weigh:
  • Persistence — the same topic_group across 7+ days (by published_at)
    counts one band higher.
  • Breadth — multiple independent severe topic_groups within a 30-day window
    justifies moving into High.
  • Singularity — a lone topic_group with no spread does not move the country
    into High on its own.

Example of SAME topic: "Australia Central Bank Holds Rates Steady" +
"RBA Decides Against Rate Cut" → both topic_group="australia_rba_rate_decision".
Example of DIFFERENT topics: that rate decision vs "Trade Deal with China"
(topic_group="australia_china_trade").

# --- Two horizons, scored independently ---
  score_3m  — investor risk over the next 3 months
  score_12m — investor risk over the next 12 months
Do not derive one from the other. Across both: friction sets the LEVEL,
order-uncertainty sets the WIDTH of the distribution around it, and information
sets the DRIFT — weak instruments mean a measured problem is more likely to
compound than to correct between now and the horizon.

# --- Condition flags: observations only ---
Report what the evidence shows. Nothing downstream will alter your scores, and
you must not adjust them to anticipate any rule. These flags are recorded next
to your scores, not applied to them.
  war_on_territory        sustained interstate war, or regular long-range
                          strikes on cities / critical infrastructure
  internal_conflict_level "none" | "A" recurring mass-casualty attacks
                          (20+ killed) or mass kidnappings, last 90 days,
                          across 3+ regions | "B" = A + repeated attacks on
                          critical infrastructure or major cities | "C" = B +
                          nationwide emergency effects (large displacement,
                          prolonged curfews, export shut-ins)
  emergency_rule          unconstitutional dissolution, martial law, or
                          week-long widespread violent unrest disrupting
                          essential services
  sovereign_stress        bank runs, capital controls, default negotiations
                          or missed payments

# --- Citations and coverage ---
For friction, order_uncertainty and information_capacity, cite the evidence ids
that drove the score — article ids like "a3", or indicator names exactly as
they appear in EVIDENCE_JSON.

evidence_coverage (0-100): how completely this evidence captures the country's
situation. Two thin wire stories about a G7 economy = low. Stale indicators and
absent ledgers lower it.

Return JSON exactly per the response schema: condition_flags, ledger_scores,
subscore_evidence, news_article_scores, score_3m, score_12m,
evidence_coverage, bullet_summary (at most 120 words: primary drivers and
meaningful mitigants).

bullet_summary must use role language throughout — "the country", "the central
bank", "the governing party" — and must never name a country, guess one, or hint
at which one it might be. A reader is shown this text beside the country's real
name, so a wrong guess is worse than no guess and a right one is still an
inference you were not entitled to make.
```

---

### Appendix — US_2019-03-11

```text
### AMBIGUOUS - the flat-band year — US 2019-03-11

articles selected: 20   full-text ids: ['a12', 'a8', 'a13']
prompt chars: 51389   ~tokens (chars/4): 12847
schema strict flag sent: True
payload_variant: p2   prompt_variant: 
mask_map_version: g5   sweep_version: 9f4aee55

--- payload_health ---
{
  "indicators": {
    "expected": 38,
    "resolved": 23,
    "by_ledger": {
      "friction": {
        "expected": 14,
        "resolved": 10
      },
      "uncertainty": {
        "expected": 16,
        "resolved": 10
      },
      "information": {
        "expected": 4,
        "resolved": 1
      },
      "edge": {
        "expected": 4,
        "resolved": 2
      }
    },
    "empty_ledgers": [],
    "dropped": {
      "DT.DOD.DSTC.IR.ZS": "no row",
      "GOV.DEBT.DOMESTIC.SHARE": "no row",
      "GOV.DEBT.FX.SHARE": "no row",
      "IC.BUS.NDNS.ZS": "no row",
      "INFORMAL.PCT.GDP": "no row",
      "NIIP.GDP": "no row",
      "OBS.SCORE": "no row",
      "OECD.PISA.MEAN": "no row",
      "OECD.TAX.WEDGE": "no row",
      "RESERVES.USD": "no row",
      "RSF.PRESS.SCORE": "no row",
      "STAT.TAX.TOP.RATE": "no row",
      "UN.EGDI": "no row",
      "UNWPP.DPND.OL.PROJ": "no row",
      "WUI.INDEX": "no row"
    }
  },
  "trends": {
    "trend_1y": 22,
    "trend_5y": 13,
    "history": 2,
    "of": 23
  },
  "blocks": {
    "computed": 8,
    "edge_inputs": 2,
    "friction_inputs": 10,
    "information_inputs": 1,
    "structural": 6,
    "uncertainty_inputs": 11
  },
  "articles": {
    "articles": 20,
    "by_theme": {
      "friction": 4,
      "order": 7,
      "security": 4,
      "information": 1,
      "edge": 0,
      "broad": 4
    },
    "thin_themes": [
      "edge",
      "information"
    ],
    "theme_floor": 2,
    "by_tier": {
      "abstract-only": 8,
      "full": 12
    },
    "with_body": 12,
    "clipped_at_max": 0
  }
}

--- evidence payload (pre-mask, as built) ---
{
  "_meta": {
    "country": "US",
    "as_of": "2019-03-11",
    "vintage_scheme": "point-in-time",
    "staleness_basis": "staleness_days counts from the end of the period a value describes to as_of: how old the reading is. `as_of` on each value is a separate fact — when it became known to us. A large staleness_days means the reading is old, not that it is wrong.",
    "next_scheduled_election": null
  },
  "friction_inputs": {
    "Government effectiveness (z-score)": {
      "value": 1.5989,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank WGI",
      "unit": "z-score",
      "trend_1y": 0.0068
    },
    "Tax revenue (% GDP)": {
      "value": 11.5039,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank WDI",
      "unit": "% GDP",
      "trend_1y": 0.6494
    },
    "Political corruption index (0–1, higher = more corrupt)": {
      "value": 0.097,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "index (0–1)",
      "trend_1y": -0.003,
      "trend_5y": 0.044
    },
    "Interest payments (% revenue)": {
      "value": 12.2634,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "% revenue",
      "trend_1y": 2.4698,
      "trend_5y": 2.9426
    },
    "Income inequality (Gini)": {
      "value": 41.8,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "index",
      "trend_1y": 0.4,
      "trend_5y": 0.9
    },
    "Government gross debt (% GDP)": {
      "value": 105.199,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-10-01",
      "staleness_days": 435,
      "source": "IMF WEO 2018-10",
      "unit": "% GDP",
      "trend_1y": -1.639,
      "trend_5y": 1.861
    },
    "Government net lending/borrowing (% GDP)": {
      "value": -3.846,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-10-01",
      "staleness_days": 435,
      "source": "IMF WEO 2018-10",
      "unit": "% GDP",
      "trend_1y": 0.065,
      "trend_5y": 3.803
    },
    "Old-age dependency ratio": {
      "value": 22.5933,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank WDI",
      "unit": "% working-age population",
      "trend_1y": 0.556
    },
    "Labour-force participation (% 15+)": {
      "value": 62.548,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank WDI",
      "unit": "%",
      "trend_1y": 0.117
    },
    "Broad money growth (% y/y)": {
      "value": 4.802,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank WDI",
      "unit": "% y/y",
      "trend_1y": 0.9525
    }
  },
  "uncertainty_inputs": {
    "Real GDP growth (% y/y)": {
      "value": 2.217,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-10-01",
      "staleness_days": 435,
      "source": "IMF WEO 2018-10",
      "unit": "% y/y",
      "trend_1y": 0.65,
      "trend_5y": -0.032
    },
    "Current account balance (% GDP)": {
      "value": -2.314,
      "period": "2016",
      "freq": "A",
      "as_of": "2018-10-01",
      "staleness_days": 800,
      "source": "IMF WEO 2018-10",
      "unit": "% GDP",
      "trend_1y": -0.076,
      "trend_5y": 0.553
    },
    "Inflation (% y/y)": {
      "value": 1.5512,
      "period": "2019-01",
      "freq": "M",
      "as_of": "2019-02-25",
      "staleness_days": 39,
      "source": "IMF CPI",
      "unit": "% y/y",
      "trend_1y": -0.5193,
      "trend_5y": 0.0852,
      "history": {
        "2009": -0.32,
        "2010": 1.64,
        "2011": 3.14,
        "2012": 2.07,
        "2013": 1.47,
        "2014": 1.61,
        "2015-01": -0.09,
        "2015-02": -0.03,
        "2015-03": -0.07,
        "2015-04": -0.2,
        "2015-05": -0.04,
        "2015-06": 0.12,
        "2015-07": 0.17,
        "2015-08": 0.2,
        "2015-09": -0.04,
        "2015-10": 0.17,
        "2015-11": 0.5,
        "2015": 0.12,
        "2015-12": 0.73,
        "2016-01": 1.37,
        "2016-02": 1.02,
        "2016-03": 0.85,
        "2016-04": 1.13,
        "2016-05": 1.02,
        "2016-06": 1.0,
        "2016-07": 0.83,
        "2016-08": 1.06,
        "2016-09": 1.46,
        "2016-10": 1.64,
        "2016-11": 1.69,
        "2016": 1.27,
        "2016-12": 2.07,
        "2017-01": 2.5,
        "2017-02": 2.74,
        "2017-03": 2.38,
        "2017-04": 2.2,
        "2017-05": 1.87,
        "2017-06": 1.63,
        "2017-07": 1.73,
        "2017-08": 1.94,
        "2017-09": 2.23,
        "2017-10": 2.04,
        "2017-11": 2.2,
        "2017": 2.14,
        "2017-12": 2.11,
        "2018-01": 2.07,
        "2018-02": 2.21,
        "2018-03": 2.36,
        "2018-04": 2.46,
        "2018-05": 2.8,
        "2018-06": 2.87,
        "2018-07": 2.95,
        "2018-08": 2.7,
        "2018-09": 2.28,
        "2018-10": 2.52,
        "2018-11": 2.18,
        "2018": 2.44,
        "2018-12": 1.91,
        "2019-01": 1.55
      }
    },
    "Political stability (z-score)": {
      "value": 0.2905,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "z-score",
      "trend_1y": 0.1804,
      "trend_5y": -0.289
    },
    "Rule of law (z-score)": {
      "value": 1.1721,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "z-score",
      "trend_1y": -0.1479,
      "trend_5y": -0.1295
    },
    "GDP per-capita growth (% y/y)": {
      "value": 2.3644,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "% y/y",
      "trend_1y": 0.6143,
      "trend_5y": 1.0163,
      "history": {
        "2009": -3.43,
        "2010": 1.83,
        "2011": 0.76,
        "2012": 1.48,
        "2013": 1.35,
        "2014": 1.71,
        "2015": 2.13,
        "2016": 1.02,
        "2017": 1.75,
        "2018": 2.36
      }
    },
    "Unemployment (% labour force)": {
      "value": 3.896,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "%",
      "trend_1y": -0.459,
      "trend_5y": -3.479
    },
    "FDI inflow (% GDP)": {
      "value": 1.0395,
      "period": "2018",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 70,
      "source": "World Bank panel",
      "unit": "% GDP",
      "trend_1y": -0.9023,
      "trend_5y": -0.6674
    },
    "Exchange rate vs USD": {
      "value": 1.0,
      "period": "2019-02",
      "freq": "M",
      "as_of": "2019-02-28",
      "staleness_days": 11,
      "source": "BIS XRU",
      "unit": "local currency per USD",
      "trend_1y": 0.0
    },
    "Policy rate (%)": {
      "value": 2.375,
      "period": "2019-02",
      "freq": "M",
      "as_of": "2019-02-28",
      "staleness_days": 11,
      "source": "BIS CBPOL",
      "unit": "% per year",
      "trend_1y": 1.0
    },
    "suppressed_vol_flag": {
      "value": null,
      "regime": null,
      "fx_volatility_24m": 0.0,
      "reserves_trend_6m": null,
      "note": "True means measured calm is being bought with reserves under a managed or pegged regime. null means one of the three inputs is unavailable — not that the flag is false."
    }
  },
  "information_inputs": {
    "Statistical performance (0–100)": {
      "value": 87.1092,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank SPI",
      "unit": "score 0–100",
      "trend_1y": -0.8608
    }
  },
  "edge_inputs": {
    "Government education spending (% GDP)": {
      "value": 5.093,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank WDI",
      "unit": "% of GDP",
      "trend_1y": 0.3097
    },
    "Human Capital Index (0–1)": {
      "value": 0.762,
      "period": "2017",
      "freq": "A",
      "as_of": "2018-12-31",
      "staleness_days": 435,
      "source": "World Bank Human Capital Project",
      "unit": "index 0–1"
    }
  },
  "computed": {
    "conversion_loss": 0.1386,
    "frictional_extraction": 1.5944,
    "monetary_dilution": 2.4375,
    "real_policy_rate": 0.8238,
    "cpi_volatility_36m": 0.604519,
    "fx_volatility_24m": 0.0,
    "precommitted_share": {
      "value": 12.2634,
      "partial": true
    },
    "dependency_trajectory": {
      "current": 22.5933,
      "projected_10y": null,
      "delta": null
    }
  },
  "structural": {
    "region": "Americas",
    "income_group": "high",
    "commodity_exporter": false,
    "monetary_sovereignty": "full",
    "reserve_currency": "major",
    "note": "Facts about this country that do not change year to year, supplied because the country is not named. Reason from these rather than from any guess about which country this is."
  }
}

========================================================================
THE EXACT PROMPT DISPATCHED
========================================================================
You are a senior sovereign risk analyst. Assess investor risk for the country
as of 2019-03-11, using ONLY the evidence below.
Treat 2019-03-11 as today: this evidence is your complete knowledge of the
world. Do not use anything you know about events after this date.

Every value in EVIDENCE_JSON carries `as_of` and `staleness_days` — the date it
became known and how old it is on 2019-03-11. Weigh a fresh reading more than
a stale one, and say so when a stale one is carrying an argument. A missing
indicator is absent from the evidence entirely; treat absence as absence, never
as zero and never as reassurance.

# --- The country is not named, deliberately ---
This evidence describes a real country whose identity has been withheld from
you. Country names, cities, people, parties, currencies and institutions have
been replaced by the roles they play: "the country", "the capital", "the central
bank", "the finance minister", "the local currency". Every NUMBER is untouched —
inflation prints, rates, counts and dates are exactly as published.

Reason only from what is on the page. Do not try to work out which country this
is, and do not let a guess do any work in your reasoning: an inference that
depends on having identified the country is unsound here even when the guess
happens to be right, because you cannot check it and neither can anyone reading
your output.

The priors a name would have carried are supplied instead. When EVIDENCE_JSON
contains a `structural` block, it states what identity used to imply — whether
the government borrows in a currency it can issue, whether it can devalue at
all, its income group, its coarse region, whether it depends on commodity
exports. Use those facts directly. A debt burden means one thing for a
`monetary_sovereignty: full` issuer of a `reserve_currency: major` and something
different for a `constrained` borrower whose debt is in money it cannot print;
read the block, do not reconstruct it from a hunch about the name. When the
block is absent, that structure is simply unknown — treat it as absent, the same
as any missing indicator, and do not substitute a guess.

EVIDENCE_JSON
{"_meta": {"country": "the country", "as_of": "2019-03-11", "vintage_scheme": "point-in-time", "staleness_basis": "staleness_days counts from the end of the period a value describes to as_of: how old the reading is. `as_of` on each value is a separate fact — when it became known to us. A large staleness_days means the reading is old, not that it is wrong.", "next_scheduled_election": null}, "friction_inputs": {"Government effectiveness (z-score)": {"value": 1.5989, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank WGI", "unit": "z-score", "trend_1y": 0.0068}, "Tax revenue (% GDP)": {"value": 11.5039, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank WDI", "unit": "% GDP", "trend_1y": 0.6494}, "Political corruption index (0–1, higher = more corrupt)": {"value": 0.097, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "index (0–1)", "trend_1y": -0.003, "trend_5y": 0.044}, "Interest payments (% revenue)": {"value": 12.2634, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "% revenue", "trend_1y": 2.4698, "trend_5y": 2.9426}, "Income inequality (Gini)": {"value": 41.8, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "index", "trend_1y": 0.4, "trend_5y": 0.9}, "Government gross debt (% GDP)": {"value": 105.199, "period": "2017", "freq": "A", "as_of": "2018-10-01", "staleness_days": 435, "source": "IMF WEO 2018-10", "unit": "% GDP", "trend_1y": -1.639, "trend_5y": 1.861}, "Government net lending/borrowing (% GDP)": {"value": -3.846, "period": "2017", "freq": "A", "as_of": "2018-10-01", "staleness_days": 435, "source": "IMF WEO 2018-10", "unit": "% GDP", "trend_1y": 0.065, "trend_5y": 3.803}, "Old-age dependency ratio": {"value": 22.5933, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank WDI", "unit": "% working-age population", "trend_1y": 0.556}, "Labour-force participation (% 15+)": {"value": 62.548, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank WDI", "unit": "%", "trend_1y": 0.117}, "Broad money growth (% y/y)": {"value": 4.802, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank WDI", "unit": "% y/y", "trend_1y": 0.9525}}, "uncertainty_inputs": {"Real GDP growth (% y/y)": {"value": 2.217, "period": "2017", "freq": "A", "as_of": "2018-10-01", "staleness_days": 435, "source": "IMF WEO 2018-10", "unit": "% y/y", "trend_1y": 0.65, "trend_5y": -0.032}, "Current account balance (% GDP)": {"value": -2.314, "period": "2016", "freq": "A", "as_of": "2018-10-01", "staleness_days": 800, "source": "IMF WEO 2018-10", "unit": "% GDP", "trend_1y": -0.076, "trend_5y": 0.553}, "Inflation (% y/y)": {"value": 1.5512, "period": "2019-01", "freq": "M", "as_of": "2019-02-25", "staleness_days": 39, "source": "IMF CPI", "unit": "% y/y", "trend_1y": -0.5193, "trend_5y": 0.0852, "history": {"2009": -0.32, "2010": 1.64, "2011": 3.14, "2012": 2.07, "2013": 1.47, "2014": 1.61, "2015-01": -0.09, "2015-02": -0.03, "2015-03": -0.07, "2015-04": -0.2, "2015-05": -0.04, "2015-06": 0.12, "2015-07": 0.17, "2015-08": 0.2, "2015-09": -0.04, "2015-10": 0.17, "2015-11": 0.5, "2015": 0.12, "2015-12": 0.73, "2016-01": 1.37, "2016-02": 1.02, "2016-03": 0.85, "2016-04": 1.13, "2016-05": 1.02, "2016-06": 1.0, "2016-07": 0.83, "2016-08": 1.06, "2016-09": 1.46, "2016-10": 1.64, "2016-11": 1.69, "2016": 1.27, "2016-12": 2.07, "2017-01": 2.5, "2017-02": 2.74, "2017-03": 2.38, "2017-04": 2.2, "2017-05": 1.87, "2017-06": 1.63, "2017-07": 1.73, "2017-08": 1.94, "2017-09": 2.23, "2017-10": 2.04, "2017-11": 2.2, "2017": 2.14, "2017-12": 2.11, "2018-01": 2.07, "2018-02": 2.21, "2018-03": 2.36, "2018-04": 2.46, "2018-05": 2.8, "2018-06": 2.87, "2018-07": 2.95, "2018-08": 2.7, "2018-09": 2.28, "2018-10": 2.52, "2018-11": 2.18, "2018": 2.44, "2018-12": 1.91, "2019-01": 1.55}}, "Political stability (z-score)": {"value": 0.2905, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "z-score", "trend_1y": 0.1804, "trend_5y": -0.289}, "Rule of law (z-score)": {"value": 1.1721, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "z-score", "trend_1y": -0.1479, "trend_5y": -0.1295}, "GDP per-capita growth (% y/y)": {"value": 2.3644, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "% y/y", "trend_1y": 0.6143, "trend_5y": 1.0163, "history": {"2009": -3.43, "2010": 1.83, "2011": 0.76, "2012": 1.48, "2013": 1.35, "2014": 1.71, "2015": 2.13, "2016": 1.02, "2017": 1.75, "2018": 2.36}}, "Unemployment (% labour force)": {"value": 3.896, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "%", "trend_1y": -0.459, "trend_5y": -3.479}, "FDI inflow (% GDP)": {"value": 1.0395, "period": "2018", "freq": "A", "as_of": "2018-12-31", "staleness_days": 70, "source": "World Bank panel", "unit": "% GDP", "trend_1y": -0.9023, "trend_5y": -0.6674}, "Exchange rate vs the local currency": {"value": 1.0, "period": "2019-02", "freq": "M", "as_of": "2019-02-28", "staleness_days": 11, "source": "BIS XRU", "unit": "local currency per the local currency", "trend_1y": 0.0}, "Policy rate (%)": {"value": 2.375, "period": "2019-02", "freq": "M", "as_of": "2019-02-28", "staleness_days": 11, "source": "BIS CBPOL", "unit": "% per year", "trend_1y": 1.0}, "suppressed_vol_flag": {"value": null, "regime": null, "fx_volatility_24m": 0.0, "reserves_trend_6m": null, "note": "True means measured calm is being bought with reserves under a managed or pegged regime. null means one of the three inputs is unavailable — not that the flag is false."}}, "information_inputs": {"Statistical performance (0–100)": {"value": 87.1092, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank SPI", "unit": "score 0–100", "trend_1y": -0.8608}}, "edge_inputs": {"Government education spending (% GDP)": {"value": 5.093, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank WDI", "unit": "% of GDP", "trend_1y": 0.3097}, "Human Capital Index (0–1)": {"value": 0.762, "period": "2017", "freq": "A", "as_of": "2018-12-31", "staleness_days": 435, "source": "World Bank Human Capital Project", "unit": "index 0–1"}}, "computed": {"conversion_loss": 0.1386, "frictional_extraction": 1.5944, "monetary_dilution": 2.4375, "real_policy_rate": 0.8238, "cpi_volatility_36m": 0.604519, "fx_volatility_24m": 0.0, "precommitted_share": {"value": 12.2634, "partial": true}, "dependency_trajectory": {"current": 22.5933, "projected_10y": null, "delta": null}}, "structural": {"region": "Americas", "income_group": "high", "commodity_exporter": false, "monetary_sovereignty": "full", "reserve_currency": "major", "note": "Facts about this country that do not change year to year, supplied because the country is not named. Reason from these rather than from any guess about which country this is."}}

ARTICLES_JSON
[{"id": "a1", "source": "nyt", "published_at": "2019-03-07", "title": "a foreign country Officials Becoming Wary of a Quick Trade Deal", "digest": {"actors": "the country and a foreign country made progress toward a compromise; a foreign country is leery of holding a summit without concluding a deal first.", "numbers": "not stated", "masked_title": "a foreign country Officials Becoming Wary of a Quick Trade Deal", "transmission": "not stated", "what_happened": "While the country and a foreign country have made progress toward a compromise, a foreign country is leery of holding a summit without concluding a deal first.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a2", "source": "nyt", "published_at": "2019-03-05", "title": "As the leader Moves to End Trade War With another country, Business Asks: Was It Worth It?", "digest": {"actors": "the country and another country", "numbers": "not stated", "masked_title": "As the leader Moves to End Trade War With another country, Business Asks: Was It Worth It?", "transmission": "not stated", "what_happened": "the country is poised to roll back most of its tariffs as part of a trade deal with another country.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a3", "source": "guardian", "published_at": "2019-03-03", "title": "If the leader loses, we know what to expect: anger, fear and disruption", "digest": {"actors": "the former personal lawyer of the leader warned that if the leader loses the election, there may not be a peaceful transition of power; the leader claimed the election was rigged in a previous election and refused to commit to honoring the results; the campaign manager at the time expressed distrust in federal officials to prevent voter fraud; the leader established a commission to find evidence of voter fraud but dissolved it when no evidence emerged; the leader's emissaries have predicted violence and division in the country if he is impeached or removed from office; civic and religious leaders must prepare to assert the primacy of the system of government over the leader's will.", "numbers": "56% of the leader's supporters believed the election would be rigged; among all voters, 34% predicted a rigged election; 60% rejected the idea; the supreme court ruled 5-4 in favor of a previous leader; the year 1861 was mentioned as a time of civil war; predictions were made about 2019 being the most vitriolic year in the country's politics since the civil war.", "masked_title": "If the leader loses, we know what to expect: anger, fear and disruption", "transmission": "not stated", "what_happened": "Concerns are raised about the potential for a lack of peaceful transition of power in the upcoming presidential election due to the current leader's refusal to accept defeat.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a4", "source": "guardian", "published_at": "2019-02-27", "title": "It’s not enough to defend democracy – now is the time to advance it", "digest": {"actors": "the leader declares the free press an enemy; the new leadership of the legislative body makes a voting rights law a priority; others go to court to defend old norms; the governing party and the main opposition party are involved in discussions about democracy; the economist proposes new voting systems; major cities experiment with radical ideas; the legislative body passes legislation on employee-owned companies; users of online networks are suggested to choose board members.", "numbers": "not stated", "masked_title": "It’s not enough to defend democracy – now is the time to advance it", "transmission": "not stated", "what_happened": "Democracy is in retreat globally, with a rise in authoritarianism and declining faith in democratic institutions.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a5", "source": "nyt", "published_at": "2019-02-27", "title": "A Fraudulent Election in a Major City", "digest": {"actors": "the governing party conducted an investigation into a congressional seat won by a member of the governing party.", "numbers": "not stated", "masked_title": "A Fraudulent Election in a Major City", "transmission": "not stated", "what_happened": "The investigation into a congressional seat narrowly won by a member of the governing party reveals a detailed playbook for how election fraud can happen in the country.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a6", "source": "nyt", "published_at": "2019-02-27", "title": "the leader Undermines Top Trade Adviser as He Pushes for another country Deal", "digest": {"actors": "the trade representative defended a trade pact", "numbers": "not stated", "masked_title": "the leader Undermines Top Trade Adviser as He Pushes for another country Deal", "transmission": "not stated", "what_happened": "the trade representative must defend a trade pact that is shaping up to be less ambitious than hoped.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a7", "source": "nyt", "published_at": "2019-02-26", "title": "‘Aim, I Say, at the City of the capital!’ a foreign country Revives Cold War Playbook", "digest": {"actors": "a news show discusses nuclear submarines and a church choir sings about nuclear threats to the country", "numbers": "not stated", "masked_title": "‘Aim, I Say, at the City of the capital!’ a foreign country Revives Cold War Playbook", "transmission": "not stated", "what_happened": "A prime-time news show discusses the deployment of nuclear submarines along the coast of the country, while a church choir performs a song about nuking the country.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a8", "source": "guardian", "published_at": "2019-02-20", "title": "Much to fear from post-referendum trade deals with ISDS mechanisms | Letters", "digest": {"actors": "the trade secretary reaffirmed the government’s support for ISDS mechanisms; charities, trade unions and faith groups are campaigning against ISDS; the leader should take seriously the concerns of civil society.", "numbers": "not stated", "masked_title": "Much to fear from post-referendum trade deals with ISDS mechanisms | Letters", "transmission": "trade and investment agreements", "what_happened": "Legislators will debate post-referendum trade agreements that may include controversial investor-state dispute settlement mechanisms.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a9", "source": "nyt", "published_at": "2019-02-18", "title": "A Foreign Leader Won’t Say if He Nominated the Country President for a Nobel Prize", "digest": {"actors": "a foreign leader courted the country president", "numbers": "not stated", "masked_title": "A Foreign Leader Won’t Say if He Nominated the Country President for a Nobel Prize", "transmission": "not stated", "what_happened": "A nomination would align with a foreign leader’s careful courting of the country president.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a10", "source": "nyt", "published_at": "2019-02-15", "title": "a military officer defector to a foreign country severely damaged the country's intelligence efforts, ex-officials say", "digest": {"actors": "a former military officer had access to the names of double agents working for the country's government and knew the workings of the country's military operations.", "numbers": "not stated", "masked_title": "a military officer defector to a foreign country severely damaged the country's intelligence efforts, ex-officials say", "transmission": "not stated", "what_happened": "a former military officer had access to sensitive information regarding double agents and military operations.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a11", "source": "nyt", "published_at": "2019-02-13", "title": "Anti-Regional Message Seeps Into Leader Forum Billed as Focusing on Security", "digest": {"actors": "the region's allies expressed anxiety about mixed signals and arrangements for the gathering", "numbers": "not stated", "masked_title": "Anti-Regional Message Seeps Into Leader Forum Billed as Focusing on Security", "transmission": "not stated", "what_happened": "Some of the region's allies are anxious about mixed signals and ad hoc arrangements for the gathering.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a12", "source": "guardian", "published_at": "2019-03-08", "title": "A blackout caused by 'an external attack', defense minister claims", "digest": {"actors": "The defense minister accused an external entity of orchestrating the power cut against the government, while the vice-president denounced it as part of a plan to overthrow the administration.", "numbers": "23 states, 4.52pm on Thursday, 9 hours delay, 24 years old mother, countless deaths, millions of citizens, 2 tweets, 2017 split, 10 years of crisis.", "masked_title": "A blackout caused by 'an external attack', defense minister claims", "transmission": "not stated", "what_happened": "A severe power cut affected nearly all of the country's 23 states, leading to widespread darkness and fears of escalating crisis.", "stage1_severity": 85, "directly_about_country": true}, "stage1_severity": 85.0}, {"id": "a13", "source": "guardian", "published_at": "2019-03-06", "title": "Teenagers are being killed. But more policing is too simple an answer", "digest": {"actors": "The media reported on the deaths of young people; the finance minister and the governing party responded to public sentiment; the opposition leader and community advocates called for a public health approach to violence.", "numbers": "17-year-old Jodie was killed; 17-year-old Yousef was killed; 150 kids a day are turned away from mental health services; one in five victims of knife crime is female; half the victims are outside the country; black kids are a minority of those killed nationally; 16-year-old Ben was killed in 2008.", "masked_title": "Teenagers are being killed. But more policing is too simple an answer", "transmission": "The text discusses the impact of funding cuts on youth services and the need for public investment in children's welfare.", "what_happened": "The tragic deaths of young people from violence have sparked discussions about media bias and the societal response to such incidents.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a14", "source": "guardian", "published_at": "2019-02-22", "title": "Leader nominates foreign envoy as nation's representative to the UN", "digest": {"actors": "the leader announced his choice to the nation’s envoy to a foreign country, who has donated to the governing party, after the former choice withdrew her candidacy.", "numbers": "not stated", "masked_title": "Leader nominates foreign envoy as nation's representative to the UN", "transmission": "not stated", "what_happened": "The leader nominated the nation’s envoy to a foreign country to be the representative to the United Nations.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a15", "source": "guardian", "published_at": "2019-02-19", "title": "I can’t wait for the striking schoolchildren to grab the reins of power", "digest": {"actors": "The governing party and the main opposition party are criticized for their age and political views regarding climate change protests led by informed teenage girls.", "numbers": "12", "masked_title": "I can’t wait for the striking schoolchildren to grab the reins of power", "transmission": "not stated", "what_happened": "A discussion on the generational divide in politics and the response to climate change protests.", "stage1_severity": 25, "directly_about_country": true}, "stage1_severity": 25.0}, {"id": "a16", "source": "guardian", "published_at": "2019-03-08", "title": "a professional golfer well set to mix and match his way to victory at a golf tournament", "digest": {"actors": "a professional golfer is competing against another professional golfer and others in a golf tournament.", "numbers": "66, 9, 2, 4, 70, 12", "masked_title": "a professional golfer well set to mix and match his way to victory at a golf tournament", "transmission": "not stated", "what_happened": "a professional golfer is competing for a maiden victory at a golf tournament in the country.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a17", "source": "guardian", "published_at": "2019-03-08", "title": "Chess: another country start well at World Team Championship and draw 2-2 with another country", "digest": {"actors": "Another country beat the host nation and a neighbouring country, drew with another country and the top-seeded another country; the tournament favourites rested their normal No 1 in favour of another player; another country countered by aiming to draw every game; players made various strategic decisions during their matches; the governing party's president was asked about contacts with a business magnate.", "numbers": "7 match points (10 game points) for another country, 6 match points for another country (11 game points), 6 match points for the country (9.5 game points), 6 match points for another country (9 game points), 5 match points for another country (9.5 game points), 20% increase to another country 500,000 for the prize fund.", "masked_title": "Chess: another country start well at World Team Championship and draw 2-2 with another country", "transmission": "not stated", "what_happened": "Another country performed well at the World Team Championship, beating the host nation and a neighbouring country, and drawing with another country and the top-seeded another country.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a18", "source": "guardian", "published_at": "2019-03-08", "title": "A writer: 'Children chase after life, even if it ends up killing them'", "digest": {"actors": "the writer documented the plight of undocumented child migrants in the country", "numbers": "500, 9, 10, 200, 90, 4th Estate (£16.99), 19 March, £12", "masked_title": "A writer: 'Children chase after life, even if it ends up killing them'", "transmission": "not stated", "what_happened": "A writer documented the plight of undocumented child migrants in the country through her novel.", "stage1_severity": 40, "directly_about_country": true}, "stage1_severity": 40.0}, {"id": "a19", "source": "guardian", "published_at": "2019-03-04", "title": "Monday’s best TV: a documentary filmmaker – The Night in Question; a comedy series", "digest": {"actors": "the leader meets students whose universities have found them responsible for crimes; the central bank governor speaks to victims and a lawyer; the governing party's former presenter leaves a job for a sombre tribute.", "numbers": "not stated", "masked_title": "Monday’s best TV: a documentary filmmaker – The Night in Question; a comedy series", "transmission": "not stated", "what_happened": "a documentary filmmaker explores college campuses in the country, focusing on students found responsible for sexual assault, including one who was not guilty of rape in a criminal trial.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}, {"id": "a20", "source": "guardian", "published_at": "2019-03-02", "title": "the country women honor influential figures on kits for a match against another country", "digest": {"actors": "The country soccer players wore jerseys with the names of women who inspired them, including a defender, a midfielder, and a forward.", "numbers": "2-2", "masked_title": "the country women honor influential figures on kits for a match against another country", "transmission": "not stated", "what_happened": "The country soccer team will wear jerseys honoring women who inspired them during a match.", "stage1_severity": 0, "directly_about_country": true}, "stage1_severity": 0.0}]

FULL_TEXT
--- id: a12 · A blackout caused by 'an external attack', defense minister claims ---
The defense minister has accused another country of masterminding a crippling power cut that has left virtually the entire southern region of the country without electricity and stirred fears that its crisis could be entering a volatile new phase. In a televised address from the leader's crisis room in the capital, the defense minister claimed the "northern empire" was behind a "criminal aggression" designed to "disrupt and attack" the beleaguered administration. Nearly all of the country’s 23 states were cast into darkness on Thursday afternoon after the most severe power cut in the country’s recent history. "No one can be so naive to think this was the result of bad luck or chance," the defense minister said on Friday as millions of citizens prepared for a second night in the dark. "This is an aggression designed to destabilise the people and the state." The defense minister claimed the alleged attack – supposedly conducted against the hydroelectric plant in the southern region that supplies much of the country’s electricity – had been "prepared, planned and well-defined" in the capital and admitted it had caused "difficulties". But the defense minister insisted the officials and the armed forces were fighting back. "We are here to transmit a message of peace to all of the people … all is calm." Earlier, the vice-president denounced the incident – which experts attribute to mismanagement, corruption and poor maintenance – as part of "a perverse plan" to overthrow the administration. The political heir is facing a battle to retain power after the opposition leader declared himself the rightful interim leader on 23 January and was recognized by most western governments, including the country and another country. But the leader has hardly been seen or heard from since the lights went out, his only public statements coming in the form of two tweets in which he blamed another country and vowed: "We will prevail!" In contrast, the opposition leader appeared at a rally in the capital where he urged his supporters to return to the streets for fresh protests on Saturday and claimed they were "very, very close" to forcing the leader from power. The capital was eerily quiet on Friday as fears grew over the human cost of the blackout and its potential to further unsettle the crisis-stricken country. "It’s a tinderbox, and the leader’s survival thus far gives a false sense of stability. A sustained blackout could … spark widespread dissent," warned a national security council adviser during a previous administration. Harrowing video footage posted on social media showed doctors trying to keep children breathing at the capital’s paediatric hospital after it lost power. At one of the city’s maternity wards, an Associated Press reporter saw crying mothers watch nurses use candles to monitor the vital signs of their premature babies after backup generators shut off. A doctor in a nearby state warned lives would be lost as a result of the devastating outage. "This is terrible. There will end up being countless deaths." A young mother who was at a hospital in the capital with her son when the lights went out described scenes of chaos as nurses sprinted down corridors in search of manual resuscitators that might keep its young patients alive. "It was insanity," she recalled. "We are adrift because we just do not know what is going on." A grocer from downtown said: "This is a total collapse." "I don’t believe it was sabotage," he added. "What I believe is that there has been no maintenance at the plants." Opponents of the leader also rubbished suggestions the outage was the result of an anti-government conspiracy. "The hydroelectric plant has collapsed because of a lack of maintenance, just like the thermoelectric plants and the transmission and distribution lines," tweeted a former oil minister, who went into exile after splitting with the leader in 2017. "It is the incapacity and the indolence of this government that have led us to this total collapse." An editor of a local blog said the government routinely blamed political foes for such increasingly common failures. "But they’ve never come close to providing any kind of evidence. It is much more likely that this is one of the symptoms of an electrical system that we know has been in crisis for at least a decade." The editor said the power crisis was the result of "neglect, disrepair and corruption at the highest levels of the government – precisely the same things that are leading to the crisis in the healthcare sector, the economic crisis. This is one of the facets of the crisis." A senior adviser at a regional think tank said he doubted ordinary citizens would buy into claims another country had caused the blackout. "The infrastructure is in shambles, and people suffering from the blackout are unlikely to blame outside actors. That said, the country has taken an aggressive posture [towards the leader] that is bound to fuel conspiracy theories," he said. Local newspapers said the blackout began at about 4.52pm on Thursday and affected nearly the whole country. Flights in and out of the decaying airports were suspended and at the airport in another country’s capital, would-be passengers grew impatient and frustrated as staff announced that their flight to the capital would be delayed by at least nine hours. "I’ve never heard of a power cut this big and lasting this long," complained one traveller. "Every time I go back home it gets worse, but what can we do? This is the country we have." The land and maritime borders were closed following last month’s humanitarian aid showdown, and with air travel now crippled, the country is effectively locked down. Additional reporting by a local journalist in another country.

--- id: a8 · Much to fear from post-referendum trade deals with ISDS mechanisms | Letters ---
On Thursday in parliament, MPs will get their first chance to debate post-referendum trade deals, including with a foreign country. There is every sign that these deals will contain controversial investor-state dispute settlement (ISDS) mechanisms, after the trade secretary reaffirmed the government’s support two weeks ago. ISDS clauses in trade deals allow foreign investors to sue national governments for any measures that harm their profits. These cases take place in secretive private arbitration courts and can cost the taxpayer billions. Previous cases brought against governments using ISDS include a foreign energy firm suing a foreign country for introducing policies to curb water pollution; a domestic pharmaceutical giant suing a foreign country for trying to keep medicines affordable; and a foreign multinational suing a foreign country for increasing its national minimum wage. ISDS is a dangerous threat to human rights, health and the environment. It can make it difficult for governments to introduce policies on these issues, even when they have democratic support. This is particularly problematic for developing countries, which face a high proportion of cases, further depleting the resources they have available to implement the sustainable development goals. ISDS courts give international investors a legal system that neither ordinary people nor domestic businesses can access, with low levels of transparency, no appeals system and high costs. The referendum means that a foreign country is likely to adopt an independent trade policy for the first time in over 40 years. As charities, trade unions and faith groups representing civil society, we are campaigning against ISDS in current and future trade and investment agreements. The trade secretary should take seriously the concerns of civil society and set out a trade policy that puts people and planet before corporate interests. Sign up at www.stopisds.org.uk A general secretary, a CEO, a CEO, a deputy director of advocacy, a director, a CEO, a director, an executive director, a senior advisor, a director, a director, a trustee, an executive director, a coordinator, an advocacy manager, a director • Join the debate – email guardian.letters@theguardian.com • Read more Guardian letters – click here to visit gu.com/letters • Do you have a photo you’d like to share with Guardian readers? Click here to upload it and we’ll publish the best submissions in the letters spread of our print edition.

--- id: a13 · Teenagers are being killed. But more policing is too simple an answer ---
While researching my book about all the young people and teens who were killed by guns on a random day in the region, I would call the journalist who wrote the original story of the shooting and ask if they had any leads. With a handful of exceptions they didn’t. The victims were overwhelmingly working-class individuals from marginalized communities killed in poor neighborhoods. The stories were often little more than rewrites of police reports. “People are desensitised to it,” said one journalist. “They figure that’s where bad things happen.” “Unfortunately, homicides are not uncommon in that area,” said another. “Unless something unexpected happened, it just wouldn’t be the kind of story we’d follow up on,” said a third. After a while, it was difficult to escape the notion that there were places where the shooting of a child did not challenge the general understanding of how a city or a society works, but confirmed it. The death was not news in the conventional sense. How could it be, when it was expected? And then there were places where children were not supposed to get shot – schools, suburbs, malls, cinemas and universities. A fatal shooting of a child in one of those places would contradict how people thought a city or society should work. This was news. How could it not be, when it had shocked so many people? The value of a life, when weighted for race and class, can often be measured in column inches. The media did not create that value system. But it has a role in amplifying and entrenching it. Politicians respond accordingly, pursuing policies not to solve the problems and real needs of their constituents, but to chime with the mood and the headlines. Before you know it, the political and media classes are reinforcing each other’s narratives – a fetid echo chamber where thoughtful discussion about the root causes of these fatalities is laid to rest with the bodies of adolescents. As it is with gun deaths in the region, so it is with knife crime in another region, where the tragic deaths of two young people last weekend has sparked a row about police funding that ignores or misunderstands pretty much all the evidence in a self-righteous race for tabloid approval. First came 17-year-old individual A, who was stabbed to death in a park on Friday in east another region. An explorer scout, she had been at a significant government building at a Remembrance Day event just a few months ago. A man has been arrested in connection with her murder. The next day 17-year-old individual B was fatally stabbed in a village. By most accounts individual B was a bright and ambitious student who received a bursary to attend a fee-paying school in a major city. Two 17-year-old boys have been charged in connection with his death. These are not the kind of young people we have been conditioned to expect to die from knife crime. This is partly because of media bias. “The media’s response to the murder of young people is inconsistent,” a community leader told me two years ago. The trust is named after a 16-year-old boy who was stabbed to death in 2008. “The media are more likely to report on the murder when a bright, educated young person from a privileged background is killed, and we all think: ‘How did this happen to them?’” the community leader added. “But we don’t hear about or ask the same questions about the murders of young people from more vulnerable backgrounds.” It is also partly about ignorance. In 2017, it was only after a year of freedom of information requests that a major publication prised from the relevant authority details about the young people killed by knives in another region over the previous 40 years. It revealed a far more complicated picture than had been painted – with one in five victims being female, half the victims being outside the region, and individuals from marginalized communities comprising a minority of those killed nationally, even if they are over-represented in the capital, particularly. The point here is not to denigrate the amount of attention that has been given to the deaths of individuals A and B – their lives were precious, their deaths are an abomination and their families and communities deserve justice. It is to wonder how we would understand this problem differently if all of those who were killed were seen as young people of promise and potential. The memories of all of them deserve better than a sterile debate on police numbers that almost entirely misses the point. “Almost”, because of course policing matters. There’s going to be no solution to the knife-crime problem without them, and the budget cuts they have suffered are unsustainable. But few people – including many in law enforcement – believe that when it comes to knife crime this is where the emphasis should lie. “You can arrest as many people as you like,” says a public health advocate, who co-founded a movement against violence in another region, where the adoption of a public health approach has prompted a significant decline in youth violence. “You can search as many people as you like. You can throw away the key if you want to. It just won’t solve the problem.” There is almost complete consensus, including from a senior law enforcement official, that we should understand knife crime as a public health issue. “That means trying to make it not just about what happened with the stabbing on that day, but looking at the life story of the person in front of you, and the whole of the community in which that one day happened,” explains a health professional at a major hospital in another region. “It means looking at all the ways you can modify things in that life story, and that community, to make that day less likely to come.” If you drastically reduce funding for youth services and education, treat children in the care system like cattle, turn away around 150 kids a day from mental health services, close schools in some areas early on a Friday because you can’t pay teachers for the full week, routinely reject children who are eligible for special educational needs support because you have no resources, reward and rank schools that exclude pupils who either do not excel academically or display challenging behaviour, then you are deliberately and wilfully creating a crisis that makes that day more, not less, likely. A public health approach needs public funding. If the sole response to this state of affairs is to call for more policing, stiffer penalties and longer sentences then you are simply demanding that the criminal justice system adjudicate and manage the crisis created by the government. But instead of a public debate about how we are underfunding our children, we are now set for a crude morality play about angels and villains that will explain little about why the last child died and not assist us at all with the kind of understanding we need to prevent the next death. If the government truly cared about the children who are dying, it would invest in the children who are living.

# --- The three ledgers ---

FRICTION is the wedge: what the state extracts multiplied by how much of it
fails to convert into capability. Judge the take by how it converts, not by its
size. A high tax burden that funds functioning courts, roads and registries is
not friction; a modest one that funds nothing is. `frictional_extraction` in
the computed block is that product, and `doom_loop` says whether the burden is
rising while conversion decays — trajectory matters more than level, because a
heavy but stable wedge can be carried indefinitely and a compounding one cannot.

ORDER-UNCERTAINTY is imposed doubt about the load-bearing rules — the ones
capital cannot price around: whether contracts will be enforced as written,
whether the currency will hold its function, whether the published statistics
mean what they say, and whether succession is settled. This is not the same as
volatility. A country can be turbulent and legible, or calm and unreadable; the
second is worse for an investor, because there is nothing to underwrite against.

INFORMATION is instrument quality, and it sets both trust and drift. Where the
statistical system, the auditors and the press are strong, official numbers can
be taken near face value. Where they are weak, official numbers deserve a
haircut: lean on market-observed series (exchange rates, policy rates, reserves)
and on article evidence instead, and treat measured friction as compounding
rather than mean-reverting — a state that cannot see itself does not self-correct.

# --- Edge vitality: report it, never penalize it ---

Entry-and-exit churn — startup formation AND startup failure — and human-capital
formation are the system learning. They MUST NOT raise any risk score. A country
where firms are born and die quickly is discovering what works; a country where
nothing is created and nothing fails is not stable, it is inert. Failure counts
as vitality here.

Learning outcomes lead; education spending is the effort line. Read them
together, never the spending alone. High spending with weak learning outcomes is
the wedge made visible inside a school system — money extracted and not
converted into capability. Read that gap as friction evidence, not as edge
credit.

Score `edge_vitality` as an independent reading of that adaptive capacity —
higher means more vitality — and do not let a high value raise friction,
order-uncertainty, or either horizon score.

# --- The three-door event test (apply to every article) ---

An event matters only if it passes through one of three doors:
  F — it changes the wedge (extraction, or how well extraction converts).
      Reported waves of skilled departure — doctors, engineers and founders
      leaving the country — pass through this door: the population grading the
      wedge with their feet. There is deliberately no data series for this, so
      these articles are its only instrument.
  U — it destabilizes the order (contracts, currency, statistics, succession)
  I — it changes the instruments (statistics office, auditors, courts, press)
Everything else is noise, however dramatic the headline. Natural disasters with
no fiscal or contractual aftermath, weapons demonstrations, military parades,
diplomatic insults, celebrity politics and scandal without institutional
consequence do not move a score. Name the door in your reasoning; if an article
passes through none of them, its impact is low no matter how prominent it is.

# --- Manufactured calm ---

When `suppressed_vol_flag` is true, measured calm is evidence AGAINST the
country, not for it. A currency held quiet under a managed or pegged regime
while reserves drain is accumulating fuel load, and the observed stability is
the cost of that accumulation rather than evidence of strength. Read a low
measured volatility in that state as a larger, later move — not a smaller one.
When the flag is null, one of its inputs is missing; that is not a false.

# --- Scoring mechanics ---

All scores are INTEGERS 0-100. Use precise values (37, 62, 81) — never round
to multiples of 5. Neighboring countries must be distinguishable.

Direction, stated explicitly because three of these four read as risk and one
does not:
  friction              higher = a worse wedge
  order_uncertainty     higher = less legible, less underwritable
  information_capacity  higher = WORSE instruments. Despite the name, this is
                        scored as risk: 90 means the statistics, auditors and
                        press cannot be trusted and official numbers need a
                        large haircut; 10 means they can be taken near face
                        value. A country with a strong statistical system
                        scores LOW here.
  edge_vitality         higher = MORE vitality, and this one is not risk. It is
                        the only score where a high number is a good thing, and
                        it must not raise score_3m or score_12m.

Scoring bands (guidance; use the full range):
  5-20 Low · 20-40 Low-Moderate · 40-75 Moderate · 75-90 High · 90-98 Extreme

Calibration anchors — composite scenarios, not real countries:
  ~12  Stable developed market: routine politics, ~2% inflation, no security
       events.
  ~38  EM with a contested but constitutional election, ~9% inflation,
       currency pressure, no violence.
  ~58  Sustained nationwide protests with sporadic violence, caretaker
       cabinet, ~20% inflation, FX reserves falling.
  ~85  Capital controls or default negotiations underway; unrest disrupting
       essential services.
  ~95  Interstate war on the country's territory, or nationwide shutdown.

# --- Localization & Materiality ---
Do NOT raise risk for indirect foreign tensions or rhetoric. Elevate risk
ONLY when evidence shows kinetic activity on the country's territory, imminent
hostilities, or economically binding policy affecting the country. Indirect
disputes, UN votes, or rhetoric without domestic transmission = low impact.

# --- Per-article impact and topic clustering (CRITICAL) ---
Impact is an INTEGER 0-100:
  85-100 Severe — successful kinetic activity in/against the country, mass
         kidnappings, binding economic measures, major infrastructure
         sabotage, seizure or rewriting of contracts, capture of the
         statistics office or the courts.
  60-75  Moderate — credible mobilization with specific capabilities or
         timelines, high-probability binding sanctions, a serious challenge
         to one of the load-bearing rules.
  40-55  Mixed/unclear — indirect third-country events, uncertain
         transmission.
  10-35  Low/benign — rhetoric, symbolic acts, alert-level changes without
         disruption, and anything that passes through none of the three doors.

You MUST assign the same topic_group to articles covering the same underlying
event, even when the headlines differ. Aggregation: within a topic_group take
the max impact. When calibrating ledger scores, weigh:
  • Persistence — the same topic_group across 7+ days (by published_at)
    counts one band higher.
  • Breadth — multiple independent severe topic_groups within a 30-day window
    justifies moving into High.
  • Singularity — a lone topic_group with no spread does not move the country
    into High on its own.

Example of SAME topic: "Australia Central Bank Holds Rates Steady" +
"RBA Decides Against Rate Cut" → both topic_group="australia_rba_rate_decision".
Example of DIFFERENT topics: that rate decision vs "Trade Deal with China"
(topic_group="australia_china_trade").

# --- Two horizons, scored independently ---
  score_3m  — investor risk over the next 3 months
  score_12m — investor risk over the next 12 months
Do not derive one from the other. Across both: friction sets the LEVEL,
order-uncertainty sets the WIDTH of the distribution around it, and information
sets the DRIFT — weak instruments mean a measured problem is more likely to
compound than to correct between now and the horizon.

# --- Condition flags: observations only ---
Report what the evidence shows. Nothing downstream will alter your scores, and
you must not adjust them to anticipate any rule. These flags are recorded next
to your scores, not applied to them.
  war_on_territory        sustained interstate war, or regular long-range
                          strikes on cities / critical infrastructure
  internal_conflict_level "none" | "A" recurring mass-casualty attacks
                          (20+ killed) or mass kidnappings, last 90 days,
                          across 3+ regions | "B" = A + repeated attacks on
                          critical infrastructure or major cities | "C" = B +
                          nationwide emergency effects (large displacement,
                          prolonged curfews, export shut-ins)
  emergency_rule          unconstitutional dissolution, martial law, or
                          week-long widespread violent unrest disrupting
                          essential services
  sovereign_stress        bank runs, capital controls, default negotiations
                          or missed payments

# --- Citations and coverage ---
For friction, order_uncertainty and information_capacity, cite the evidence ids
that drove the score — article ids like "a3", or indicator names exactly as
they appear in EVIDENCE_JSON.

evidence_coverage (0-100): how completely this evidence captures the country's
situation. Two thin wire stories about a G7 economy = low. Stale indicators and
absent ledgers lower it.

Return JSON exactly per the response schema: condition_flags, ledger_scores,
subscore_evidence, news_article_scores, score_3m, score_12m,
evidence_coverage, bullet_summary (at most 120 words: primary drivers and
meaningful mitigants).

bullet_summary must use role language throughout — "the country", "the central
bank", "the governing party" — and must never name a country, guess one, or hint
at which one it might be. A reader is shown this text beside the country's real
name, so a wrong guess is worse than no guess and a right one is still an
inference you were not entitled to make.
```
