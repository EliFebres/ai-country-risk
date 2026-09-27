# Scorer measurement (step 5)

2026-09-27, branch `backend-update`. Scorer `gpt-4o-2024-08-06` on the current
`RISK_PROMPT` (`prompt_version` `360a8483…`), temperature 0, seed 42, which are
the production settings. Gate `gpt-4o-mini-2024-07-18`, `body` mode. Every call
read a frozen payload from `backend/measurements/step5/`, and none wrote
`risk_snapshot` or `run_ledger`.

## The finding

**The scorer does not choose a number. It picks from a short menu, and on an
identical payload it moves between menu items from call to call.** Across 60
calls on six frozen payloads, `score_12m` took ten values in total: 38, 43, 47,
48, 54, 67, 68 and 72, plus 37 and 58 from the stored snapshots. HK, KW, TW and US
all land on 47, and KW and TW both jump to 54, the top of the prompt's 39–54 band.
PT moves between 38 (the top of 23–38) and 43. `score_3m` is `score_12m − 2` in
all 60 calls.

The spread comes from those jumps, not from scatter around a centre. KW gave 47
six times, 54 three times and 48 once. US gave 38, 43 and 47. Averaging calls would
average across menu items. The fix belongs in the anchors, which the model uses
as buckets (M2), and not in the number of calls.

Under the pre-set decision, M1's worst SD is 3.33 (KW), which is above 3, so
**the measurement stopped after M1**. M3 and M5 were not run. M2, M4 and the
dilution check need no new calls, so they are computed from the 60 M1 calls
and reported below. Treat them as readings on six payloads, not as the full
design.

## Part 1: prep fixes

### 1a. One story, one article

`relevance.dedupe_stories` runs after the gate and before selection. It makes one
`gpt-4o-mini` call per country over the eligible articles' `what_happened`
sentences, keeps one article per story (a `full` body first, then the newest,
then an unused publisher, then the URL), and gives the freed slots to the next
eligible article. The manifest records each dropped article as `same_story_as`.
The scorer is not told how many outlets carried a story.

**The schema changed from the brief's.** A list of clusters cannot require every
id, and on the first run `gpt-4o-mini` left 1 to 5 ids out in all five test
countries, so every country fell back to title overlap. The schema now makes
every id a required key whose value is the id of the first item telling the same
story. Code joins those links into clusters. The grammar enforces the partition,
and the title-Jaccard fallback (≥ 0.6) is still there, stamped in the manifest,
for an unreadable answer or a failed call. An enum on the values would have
tidied it further, but strict mode allows 1,000 enum values per schema and 48
ids need 2,304.

`body_quality` is known at this point only from the digest cache, as it is for
the gate, so a story that no earlier run digested is kept on date.

| country | eligible | stories | multi-article stories | set aside | slots freed | method |
|---|---:|---:|---:|---:|---:|---|
| US | 38 | 36 | 2 | 2 | 1 | llm |
| PT | 31 | 28 | 2 | 3 | 2 | llm |
| KW | 48 | 43 | 3 | 5 | 2 | llm |
| HK | 39 | 30 | 6 | 9 | 5 | llm |
| TW | 38 | 27 | 5 | 11 | 7 | llm |
| RU | 56 | 56 | 0 | 0 | 0 | llm |

"Slots freed" counts the set-aside articles that selection would otherwise have
put in the 20. The clusters read correctly on inspection: one inflation release
from four outlets, the Tiananmen-vigil sentences and the EU call for sanctions
over them, and the Iranian strikes on Kuwait. The widest is TW's Xi–Trump meeting
cluster of six, which includes two follow-ups on US arms for Taiwan. That call is
arguable.

### 1b. Gate wording: adopted

E8 now reads "when the article's main subject is the people or groups outside
{country}, not events inside it", and T1 lists "bills introduced in the
legislature". The re-check used the same method as before: 60 labels, 3 passes,
with the first pass cached (`backend/measurements/step5/gate_recheck.json`).

| set | agreement | recall (relevant) | flips |
|---|---:|---:|---:|
| 46 not examples (decides) | **40/46 (87%)** | **33/34 (97%)** | — |
| all 60 | 53/60 | 40/41 | **0** |

All three lines pass. h16 now agrees with Eli. **b18, the worked example the E8
edit was meant to settle, still comes out T1**, so the wording change did not fix
the conflict it was written for. It does not count toward the 46.

## Part 2: frozen payloads

Built on 2026-09-27 through `pipeline.assemble_country`, the weekly run's own
payload code, now factored out of `run_etl`. Gate prompt version `acd3855c…`.

| country | payload fingerprint | sha256 of bytes sent | payload tokens | articles |
|---|---|---|---:|---:|
| US | `a7172dae7b1e` | `c5dfd46fcdc4` | 8,775 | 20 |
| PT | `fbf2a2f47401` | `7a7ee36943d7` | 8,634 | 20 |
| KW | `32c6796f77de` | `ed7312c9930b` | 9,026 | 20 |
| HK | `0b0c565ceeeb` | `211fa1729f4b` | 9,127 | 20 |
| TW | `a9ad6182c5a1` | `05ca3c6d8bbc` | 9,297 | 20 |
| RU | `79d2d682ff51` | `e77837bb09cb` | 8,559 | 20 |

**The high-risk country is RU, not TR.** Dev's `risk_snapshot` holds only the
five test-set rows from 2026-09-25, because the 48-country scores were taken out
in `4b8360b`. That run is still on record in `docs/round-numbers-48.json`, where
RU scored the highest at 68, ahead of EG and TR at 65.

## Part 3: measurements

### M1. Repeatability: **SD > 3, stop**

Ten calls per payload. Each cell shows the distinct values, the range and the SD.

| | score_12m | score_3m | friction | order | information | edge | same flags | same JSON |
|---|---|---|---|---|---|---|---:|---:|
| HK | 47 · 0 · 0.00 | 45 · 0 · 0.00 | 35,40 · 5 · 2.42 | 50 · 0 · 0 | 60 · 0 · 0 | 40,55 · 15 · 7.25 | 10/10 | 1/10 |
| KW | 47,48,54 · 7 · **3.33** | 45,46,52 · 7 · 3.33 | 42,45 · 3 · 1.45 | 50,58 · 8 · 3.86 | 45,50,55 · 10 · 4.38 | 40 · 0 · 0 | 10/10 | 1/10 |
| PT | 38,43 · 5 · 1.58 | 36,41 · 5 · 1.58 | 32,34,39 · 7 · 2.17 | 32,35,42 · 10 · 3.03 | 20 · 0 · 0 | 35,40 · 5 · 1.58 | 5/10 | 1/10 |
| RU | 67,68,72 · 5 · 2.51 | 65,66,70 · 5 · 2.51 | 61,62,66 · 5 · 1.49 | 65,68,75 · 10 · 2.50 | 65,68,70 · 5 · 2.10 | 45 · 0 · 0 | 7/10 | 1/10 |
| TW | 47,54 · 7 · 2.95 | 45,52 · 7 · 2.95 | 39 · 0 · 0 | 50,52,55 · 5 · 2.10 | 22,25 · 3 · 0.95 | 40,45 · 5 · 2.11 | 10/10 | 1/10 |
| US | 38,43,47 · 9 · **3.29** | 36,41,45 · 9 · 3.29 | 39,45,50 · 11 · 4.03 | 35,40,45 · 10 · 3.69 | 30,35 · 5 · 2.11 | 28,30,35,40 · 12 · 4.67 | 10/10 | 1/10 |

No two of the 60 answers are byte-identical, because the prose always differs. The
fixed seed does not make the call repeatable. HK's composite held for ten calls
while its `edge` moved between 40 and 55.

**Decision:** the worst `score_12m` SD is 3.33 (KW), with US at 3.29, so **SD > 3:
stop after M1**. The instrument cannot carry a weekly number yet. The median of
three would not rescue it. KW's median of three is 47 or 54 depending on which
calls come up, so it would still move a whole band tier on an unchanged week.

### M2. The score grid: **anchors act as buckets**

Pool: the 60 M1 calls and the 5 `risk_snapshot` rows in dev.

- `score_12m` distinct values: 37, 38, 43, 47, 48, 54, 58, 67, 68, 72.
- Multiples of 5 (updating §9): composites **31 of 130 (24%)**, ledgers **180 of
  260 (69%)**. By ledger: friction 32%, order 68%, information 82%, edge 95%.
- `score_12m` within 2 points of a band edge (22/23, 38/39, 54/55, 69/70, 84/85):
  **36 of 65 (55%)**, against 33% if values were spread evenly over 8–98.

**Decision:** more than half sit near a band edge, so **the anchors are acting as
buckets.** This is the first scoring-prompt fix for step 5b. The two readings
agree: the composites avoid multiples of 5 by sitting one point inside a band
edge (38, 54, 68), and the ledgers ignore the instruction altogether.

### M3. Do the articles move the score? **Not run**

Stopped by M1's decision. The 54/52 question is partly answered anyway: on the
new payloads HK scores 47 every time, and TW and KW move between 47 and 54
between calls on unchanged evidence. So 54 in week one is not evidence that
those three weeks read alike.

### M4. The 3-month score: **no information**

`score_12m − score_3m` is **2 in 60 of 60 calls** (M1 only, since M3 was not
run). Two payloads mention a scheduled event inside three months: US (the
November midterms, a8) and TW (the 2026 local elections, a10). Neither article
gives the date. The gap is 2 for those two payloads and 2 for the four without
one.

**Decision:** the gap takes one value in more than 80% of calls, so `score_3m`
carries no information. The 5b fix is to require `score_3m` to cite a dated
event in the payload, and otherwise to equal `score_12m`.

### M5. Article count: **not run**

Stopped by M1's decision. The volume question stays open.

### Dilution: routine official news

The scorer's `bearing` joined to the gate's test code, over all 60 M1 calls:

| test | appearances | mean bearing | share with bearing 0 |
|---|---:|---:|---:|
| T1 | 510 | 4.43 | 16% |
| T2 | 170 | 2.60 | 44% |
| T3 | 90 | 4.38 | 29% |
| T4 | 280 | 3.11 | 58% |
| T5 | 150 | 3.16 | 36% |

**Decision:** T1 has `bearing` 0 in 16% of appearances, under half, so under the
rule T1 is informing the score and the selection order stays. The check is weak.
`bearing` is supposed to run 0–100, but across 1,200 article scores the model used
only 0 to 15, and 0, 5 and 10 cover 83% of them. A scale that stops at 15 cannot
separate a routine hearing from a coup. By zero-share, T4 (outside states acting
on the country) comes out least informative. That is surprising, and it is the
reading to check first once `bearing` is fixed.

## Step 5b: the fix list, in order

1. **Anchors as buckets (M2).** Rewrite the calibration anchors so the model
   cannot use band tops and midpoints as a menu. This is the likeliest mechanism
   behind M1: the unstable countries jump between exactly those values.
2. **`score_3m` must cite a dated event in the payload, or equal `score_12m`
   (M4).**
3. **Re-run M1 on these same frozen payloads** after 1 and 2, with the same
   decision lines. Until the worst SD is ≤ 3, no weekly number should be
   published.
4. **Then run M3 and M5**, which were skipped here, on the same files. The harness
   and the arms are built (`python -m backend.llm.scorer_measure score --arm
   no_articles|no_economics|first_5|first_10|first_15 --n 3`).
5. **`bearing` uses 0–15 of its 0–100 scale.** Anchor it before trusting the
   dilution check or ranking by it.

## Spend

| step | USD |
|---|---:|
| Gate re-check (180 calls, gpt-4o-mini) | 0.111 |
| First freeze attempt (gate on the new prompt and digests, crashed on RU; not metered to the end) | ~0.20 est. |
| Freeze (111 calls, gpt-4o-mini) | 0.059 |
| M1 (60 calls, gpt-4o) | 2.183 |
| **Total** | **~2.55** |

One production scoring call costs $0.036 at about 11,400 input and 790 output
tokens. The metered figure ignores OpenAI's cached-input discount, so it
overstates the cost slightly. A single-call week for 48 countries is about $1.75.

## Files

- `backend/measurements/step5/payload_<iso2>.json`: the frozen payload, with its
  fingerprint, per-article gate metadata and the dedup record.
- `backend/measurements/step5/calls/full_<iso2>.jsonl`: every M1 call, raw and
  validated.
- `backend/measurements/step5/analysis.json`: M1, M2, M4 and dilution, computed.
- `backend/measurements/step5/part1_dedup.json` and `gate_recheck.json`.
- `snapshot_diagnostic` (§12) is still empty. The brief put measurement results in
  files, so the table's first intended writer did not use it.
