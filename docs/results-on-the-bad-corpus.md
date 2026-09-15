# Results computed on the unfiltered corpus

> **Superseded 2026-09-15** — see [`historical-ratings-postmortem.md`](historical-ratings-postmortem.md). Every result listed here was removed with the historical programme. The list stands as the record of what not to quote.

**Read this before quoting any number from the pilot, the bake-off, or the A/B arms.**

Every historical snapshot this project has ever assembled was selected by a relevance
scorer reading a fifth of the text the live path feeds it, from a corpus that was 45%
football for the country the reference baseline was captured on. `docs/retrieval-diagnosis.md`
has the mechanism; this file is only the list of what was measured on it, so nobody reads
those numbers later without knowing what they were taken on.

Nothing here has been re-run. This is a labelling exercise, not a correction.

## What changed underneath them

Three facts, all measured on the stored DEV corpus (the one every arm below actually read):

| | value |
|---|---|
| sport as a share of the articles the model was handed, PT 2019 | **39.2%** (408 of 1,040) |
| anchors topped up from below the relevance bar, PT 2019 | **52 of 52** |
| sport articles carrying a non-`broad` theme, and so competing for the reserved per-theme slots | **197 of 290** |

The third is the one that reaches furthest. `select_with_theme_floor` reserves two slots per
ledger so a quiet theme cannot be crowded out. Those slots were being filled by match reports:
"Southampton 2-1 Watford: Premier League – as it happened" was tagged `information` — the
ledger about press freedom and judicial independence — and "Phil Neville's son Harvey called
into Republic of Ireland Under-19 squad" was tagged `edge`, the skilled-departure ledger the
prompt states articles are its **only** instrument for. So the ledger scores were not merely
noisy; two of the four were reading football by construction.

## The list

### Invalidated — computed on real corpus payloads

| result | where | why it is on this list |
|---|---|---|
| **`GATE2_BASELINE.md` / `.json`** | repo root | PT 2019, the worst-documented case: padded at 52 of 52 anchors, 39.2% sport. It is the pilot's regression reference, so everything compared against it inherits this. Already invalidated once by the vintage fix (`deferred.md` item 27) — this is a second, independent reason. |
| **`backend/bakeoff/US-2019/*.json`** | 16 arms × 52 anchors | The *opposite* failure. US is saturated: 0 anchors topped up, relevance spread 0.081 across the year, so which 20 a US anchor sees is near-arbitrary. Not padded — undiscriminated. |
| **`backend/bakeoff/TR-2018/*.json`** | 16 arms × 53 anchors | 10 of 53 anchors topped up. The only window of the three whose evidence weight actually varies, and still selected by the narrow scorer. |
| **both `gpt-4.1` arms** | `*/gpt-4.1.json`, `*/gpt-4.1-postfix.json` | The benchmark-incumbent decision (`deferred.md` §11) rests on these. |
| **`p3-context`, `p4-trend`** | `docs/payload-ab.md` | Both rejected on distinct-value and round-share criteria measured over these payloads. A rejection on bad evidence is not a safe rejection. |
| **`p2-rebaseline`, `p2-rebaseline-postfix`** | `backend/bakeoff/*/` | Exist precisely because the vintage fix split the experiment. Now split again. |
| **`within-band`, `vs-typical`, `trend-prompt`, `gpt-4.1-x-elicitation`** | `docs/elicitation-ab.md` | Four interventions and one crossed cell, all scored on these snapshots. |
| **the 17-point noise floor** | `gpt-4o-postfix` / `gpt-4.1-postfix` gates, `docs/scorer-acceptance.md` §2 | Measured on real payloads from `_SMOKE_ANCHORS` — PT 2019-06-03, US 2019-03-11, TR 2018-08-13 — all three corpus anchors. This is the number the acceptance bar was rewritten around. |
| **the determinism matrix, "real worst" column only** | `docs/scorer-acceptance.md` §2 | Same anchors. The "canned worst" column is unaffected — see below. |
| **157 `risk_snapshot` rows** | both DBs | The production masked series. |
| **`snapshot_diagnostic`** | 37 `kind='arm'` rows, plus the `kind='probe'` identifiability rows | Divergence and identifiability were both measured over these article sets. |

### Not invalidated

- **Every `gates` block written before 2026-08-30**, and all of `backend/bakeoff/round2-gates/*.json`
  (`deepseek-v4-pro`, `deepseek-v4-flash`, `minimax-m3`). These ran on a canned ~2,980-token
  payload and never read the corpus. They carry their own separate caveat — every number in
  them is a lower bound against a real 11,142–13,038-token prompt — but not this one.
- **The macro half of everything.** `indicator_series`, `payload_census`, the vintage findings
  in `docs/trend-payload-findings.md`. Those are about the indicator panel, which this does not
  touch.
- **The masking work.** Gazetteer, mask map, rewrite. Masking was applied to whatever articles
  arrived; it did not choose them.

## What this means

The comparison in `docs/retrieval-diagnosis.md` is material, so the plain statement is owed:

**These results describe an instrument reading mostly-irrelevant evidence.** For PT 2019 the
model was handed twenty articles of which eight were football or travel, six more were below
the relevance bar, and the reserved slots for two of the four ledgers were filled with match
reports. They will need re-deriving on the fixed corpus before any of them means anything.

Two consequences worth stating separately, because they point in opposite directions:

1. **The rejections are the least safe conclusions here.** `p3-context` and `p4-trend` were
   rejected for making the instrument coarser. An instrument scoring football would be expected
   to look coarse whatever the payload did, so those arms were arguably never given a fair
   reading. They are the first candidates for re-running, not the last.
2. **The 17-point noise floor probably survives in direction, if not in value.** It was measured
   as repeat-spread on identical inputs, and identical bad inputs are still identical. The figure
   should be re-taken, but the finding that the incumbent's noise swamps the largest real effect
   is not obviously an artefact of the corpus.

## What is *not* claimed

That the fixed corpus scores differently. Nothing has been re-scored — this session spent $0 on
model calls by instruction. What is established is that the evidence reaching the model changes
composition completely (39.2% sport to 0%, mean selected relevance 0.191 to 0.247 with the
top-up, 0.505 with the floor). Whether the scores move, and by how much, is the next experiment
and it is not this one.
