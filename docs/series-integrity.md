# Series integrity

**For the reader in three years.** This document says what must not change
silently, how to tell whether two stored weeks are comparable, and what the
instrument's known limits are. If you are about to change something in the first
list, add a dated entry to *Steps in the series* at the bottom — that is the
whole point of the page.

Written 2026-09-23, at week one.

---

## 1. What must never change silently

Each of these is a **stamped field** on `risk_snapshot`. A change to any is a
*step in the series*: weeks either side of it are not directly comparable, and
the change gets a dated entry below.

The stamps are immutable by construction. `data_upsert/store.py` refuses an
upsert that would alter one on an existing row and logs both values. This is not
politeness — the previous incarnation of this project lost its ability to
identify affected rows because a later commit quietly restamped a `git_sha`, and
by the time anyone looked there was no way to tell which weeks were involved.

| What | Stamped as | What happens to the series |
|---|---|---|
| **Scoring model** | `scoring_model` | Step. Different models disagree by more than a typical week's move. |
| **Scoring prompt and output schema** | `prompt_version` (hash of the prompt text plus `SCHEMA_VERSION`, the hash of the schema's shape with article ids abstracted) | Step. Both the wording and the schema's shape moved scores on the old instrument. Each call's schema lists that call's article ids, so the schema text differs by article count while the stamp does not. |
| **Gate model** | `gate_model` | Step. The gate decides what the scorer reads; a different gate is different evidence. |
| **Digest model** | `digest_model` | Step, smaller. Digests are extractive, but the scorer reads them rather than the articles. |
| **Payload shape** | `payload_fingerprint`, `payload_tokens` | Step. Adding or removing a block changes what the model weighs. |
| **Selection rule** | `manifest.selected`, and the code at `git_sha` | Step. Which twenty articles are chosen is the evidence. |
| **Retrieval queries** | `git_sha`; `country.query_name` for the name | Step. The six theme queries and a country's query name both determine the pool. Hong Kong went from 1 article to 60 on a query-name change alone. |
| **Source mix** | `article.source_system` | Step if a new source is added; the pool's composition changes. |
| **`as_of` lags** | `indicator_series.as_of_scheme`, `util/vintage.py` at `git_sha` | Step. A changed lag moves every staleness figure and therefore the freshness weighting. |

**Not a step:** a country's underlying data being revised. `indicator_series`
keys on `as_of`, so a revision arrives as a new row and the old score remains
readable against the numbers as they stood.

## 2. Are two stored weeks comparable?

Compare these fields and **nothing else**. If they all match, the two weeks are
the same instrument and the difference between them is the country.

```sql
SELECT run_date, prompt_version, scoring_model, digest_model,
       gate_model, git_sha
  FROM risk_snapshot
 WHERE country_iso2 = 'PT'
 ORDER BY run_date DESC;
```

`payload_fingerprint` is a stricter test and answers a different question: two
rows with the same fingerprint read *the same evidence*, which is what a repeat
measurement needs. Across two different weeks it will always differ, and that is
correct.

Do not compare on `evidence_coverage`, on the scores themselves, or on row
counts. Coverage moving is a fact about the week, not about the instrument.

## 3. Known limits, as of today

**These are inherited claims, not measurements on this instrument.** They are
written down so nobody quotes the scores more precisely than they deserve, and
the measurement session replaces each with a number.

- **The grid is coarse.** Composite scores take roughly nine distinct values in
  practice, so a week-to-week move under about five points is grid noise rather
  than a change in the country.
- **Repeat spread was up to 17 points** on a single anchor at real payload size
  on the *old* instrument. It has **not been measured on this one**. Until it is,
  assume a re-run of the same week can move the composite by more than a typical
  weekly change.
- **The score falls with article count**, about 0.4 points per article removed.
  Measured once, never replicated. If it holds, a country with a thin week is
  scored slightly lower for being thin, which is a bias and not a finding.
- **The no-round-numbers rule holds for composites and not for ledgers.**
  Measured 2026-09-22 across 48 countries: `score_12m` 8% multiples of five,
  `score_3m` 6%, `friction` 27%, `order` 71%, `information` 71%, `edge` 88%,
  against a 20% chance baseline. Ledger scores are coarser than they look.
- **Two ledgers run thin.** `information` resolves 2 indicators and `edge` 3,
  against 4 each for `friction` and `order`. Taiwan resolves almost nothing from
  any panel source and scores on curated series alone.

## 4. Design intent, so nobody undoes it

- **Integers are stored, 0–100, as the model returned them.** No bands, no
  rescale to 0–1 at write. Bands are a display decision for the front end, and a
  float is a conversion the reader can do. The reverse is not true: a stored band
  cannot be turned back into a number.
- **A four-week average is the smoother series for analysis, derived at read
  time.** Given the grid coarseness above, the single-week integer is noisier
  than it looks. Average it when analysing; do not store the average.
- **Nothing is compressed at write.** The manifest keeps the whole evidence
  list, `indicator_series` keeps the full five-year history rather than the
  latest value, and rejected articles keep their labels and reasons. Storage is
  cheap; a question you cannot answer later because the row was trimmed is not.
- **Absence is absence.** An indicator with no observation is omitted and named
  in the census, never written as zero. A zero reads as reassurance.
- **The census travels with the score.** It is in `risk_snapshot.manifest`, not
  a separate table, because a table joined to the score by nobody is a table
  nobody reads.

## 5. Steps in the series

**None yet. This is week one.** This page was written on 2026-09-23, but
week one actually ran on **2026-09-25**, at git SHA **`37dd309`**, against the
dev database (`neondb` on `ep-round-brook-b5w0nzpy`). The 2026-09-23 attempt
wrote no snapshots: every country it reached failed on the write, and it was
interrupted after nine. Week one covers five countries (US, PT, KW, HK, TW),
not the full roster.

When a step happens, add a row: the date, what changed, which stamped field
records it, and whether weeks either side can be compared at all.

| Date | What changed | Stamped field | Comparable across? |
|---|---|---|---|
| — | — | — | — |
