# Gate-2 baseline

Gate 2 on the winning payload. p3-context was measured and rejected (docs/payload-ab.md), so this baseline is p2 — the contract the pilot will actually run. PT 2019: 52 masked anchors, 6 named and 6 masked_nostructural diagnostic dates.

What the pilot measured on the anchors gate 2 scores, kept so the next run is a regression check rather than a fresh opinion. Regenerate with `python -m backend.util.pilot.run pilot-report --export`; the machine-readable copy is `GATE2_BASELINE.json` beside this file.

## Captured under

A divergence measured under different masking is a different number. Compare against this baseline only when these match — and when they do not, that is the finding.

| version | value |
|---|---|
| `DIGEST_MODEL` | `gpt-4o-mini-2024-07-18` |
| `GAZETTEER_VERSION` | `aa63700b` |
| `MASK_MAP_VERSION` | `g5` |
| `PAYLOAD_VERSION` | `p2` |
| `PROMPT_VERSION` | `v4.0-masked-production` |
| `REWRITE_VERSION` | `7078d4f6` |
| `SCORING_MODEL` | `gpt-4o-2024-08-06` |
| `SEED` | `42` |
| `SWEEP_VERSION` | `9f4aee55` |
| `git_sha` | `f04da6804b161eb0faf85f82e8612083e2d420a5` |

## The meters

```
=== 1. divergence: masked - named on paired dates ===
  (signed, then |.|. Positive = masking scored it riskier than its name
   did, so the name was carrying reassurance; negative, alarm. `recovery`
   = how much of the gap the structural block closed, off the magnitudes,
   from the no-structural arm.)
          n      pre     post  overall    |pre|   |post| |overall|  no-struct  |no-str|  recovery
  PT      6    0.075        —    0.075    0.075        —     0.075      0.080     0.080     0.005

=== 2. identifiability: can the cheap model name the country? ===
  (the US is expected near the ceiling — a decade of coverage volume
   gives it away. The spread is the meter; a high floor is the failure.)
   `wrong` is its own column on purpose: a bundle placed confidently in
   the wrong country is neither a hit nor a clean miss. Masking held and
   the text was still legible enough to commit — read `placed` as what
   the evidence gave away, and `overall` as what identity did.)
  PT   overall= 0.000  placed= 0.111  pre= 0.000 (n=  9)  post=     — (n=  0)  confidence=0.089
       identified=0    wrong=1    uncertain=0    no_guess=8
  ceiling=0.000  floor=0.000  spread=0.000

=== 3. evidence texture: source mix and tier split ===
  PT 2019  snapshots= 52 articles/snapshot=20.000  abstract= 0.078  guardian=  959 nyt=   81

=== 4. spend ===
  masked                 157 complete,   0 failed, $   6.59  ($0.042/snapshot)
  named                    6 complete,   0 failed, $   0.27  ($0.045/snapshot)
  masked_nostructural      6 complete,   0 failed, $   0.23  ($0.038/snapshot)
  TOTAL                $7.09 of $130.00 — $122.91 left

=== 5. ranked: where identity carried a fact the payload does not state ===
  (divergence the structural block did NOT close. The fix is a new
   structural field, not a retreat to named scoring.)
  1. PT  unexplained=0.070  of |divergence|=0.075  signed=0.075  n=6

=== 6. lint: contradictions the run wrote down ===
  (advisory — nothing here moved a score. One country on one rule is a
   country to look at; a rule firing across the roster is a threshold
   to move or a prompt to fix.)
  No findings.

=== 7. stage-1 degradation: snapshots scored on truncated bodies ===
  (a stage-1 failure is silent — the article still reaches the model in
   the pre-digest shape. Read this before reading divergence: a country
   that is both divergent and degraded is telling you about its evidence,
   not about masking.)
  PT     30 snapshot(s) affected,    5/600   degraded (0.008),   31 truncated-retry

=== 8. harvest pacing: what the corpus cost in time ===
  (the input to the 48-country backfill decision. NYT is not scaled —
   one archive call returns the whole world, so it does not grow with
   the roster the way Guardian and Wayback do.)
  guardian BR      11 window(s)      3.7 min        0 article(s)     11.0 call(s)     1.000/window  11 failed
  guardian KR      11 window(s)     14.6 min     5875 article(s)    305.0 call(s)    27.730/window  4 failed
  guardian PT      11 window(s)     15.3 min     9059 article(s)    364.0 call(s)    33.090/window
  guardian TR      11 window(s)     31.7 min    15495 article(s)    768.0 call(s)    69.820/window
  guardian US      11 window(s)     61.4 min    26380 article(s)   1450.0 call(s)   131.820/window  3 failed
  nyt BR          121 window(s)      2.3 min     2179 article(s)     24.2 call(s)     0.200/window
  nyt KR          121 window(s)      2.3 min     4642 article(s)     24.2 call(s)     0.200/window
  nyt PT          121 window(s)      2.3 min      709 article(s)     24.2 call(s)     0.200/window
  nyt TR          121 window(s)      2.3 min     2430 article(s)     24.2 call(s)     0.200/window
  nyt US          121 window(s)      2.3 min    18000 article(s)     24.2 call(s)     0.200/window
  total 138.1 min; 11.5 of it roster-wide (flat in the roster size)
  ~20.5h for 48 countries, scaling the per-country sources off 5 measured country/ies
```
