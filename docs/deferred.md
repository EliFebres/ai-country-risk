# Deferred

Decisions taken and deliberately not acted on, with the reasoning attached so
the next session does not re-derive it. Items marked HIGH or **broken** are owed
rather than elective.

Resolved items are removed rather than annotated. If it is here, it is still
true.

Started 2026-09-22, during the v2.0 payload work (Parts 1–6).

---

## 1. The two curated series go stale on their own cadence, and nothing notices

`RSF.PRESS.SCORE` and `OECD.PISA.MEAN` are acquired by running
`python -m backend.data_fetching.curated_build` and committed as data. Nothing
polls them, nothing warns when they age, and `payload_health` will report them
as resolved right up until someone notices the year is wrong.

This is the same shape as every other "written once, read forever" problem in
this codebase: the file loads, the census counts it, and the number is from
three years ago.

| Series | Cadence | Current edition | Next expected |
|---|---|---|---|
| RSF World Press Freedom Index | annual, published on World Press Freedom Day | 2026 index (assesses 2025) | **early May 2027** |
| OECD PISA | triennial | PISA 2022 (published 5 Dec 2023) | **PISA 2025 results, early December 2026** |

PISA 2025 is the nearer one and it lands inside this project's normal working
life. When it does, `curated_build.PISA_*` constants need the new round, the new
statlink, and a new set of spot checks — the validation anchors are per-edition
by design, so a new round fails loudly rather than loading against the old
table's expectations.

The cheap fix, when someone wants it: have `payload_health` compare each curated
indicator's `period` against the edition the registry says is current, and say
so in the run output. That is a consumer for a fact nobody currently reads.

## 2. Taiwan resolves almost nothing, and only from curated sources

Taiwan is in the roster (MSCI EM) and is not a World Bank member. It is absent
from the World Bank API, from IMF SDMX and from most OWID series, so nine of the
eleven panel-fed registry codes will resolve to nothing for it, permanently.

What it does have: **RSF 2026 gives Taiwan 75.44** and **PISA 2022 gives Chinese
Taipei a mean of 533.21**, so `information` and `edge` are the two ledgers Taiwan
can actually score on — which is the opposite of every other country, where
those two are the thin ones.

Where the missing data would come from, if someone wants to close it:

- **DGBAS** (Directorate-General of Budget, Accounting and Statistics) —
  national accounts, CPI, unemployment, the bulk of what WDI would give.
- **Taiwan Ministry of Finance** — fiscal position, tax revenue, interest
  payments.
- **Central Bank of the Republic of China (Taiwan)** — policy rate, reserves.

Governance z-scores (WGI political stability, rule of law) have no Taiwan
equivalent at all; V-Dem does cover Taiwan, so `OWID.VDEM.CORRUPTION` may be
recoverable through a different OWID join.

**Handled, not fixed:** the census shows Taiwan's zeros in every run, because
state that is always the same is still information. The alarm is separate and
fires on *change* against a country's own recent runs, so Taiwan resolving zero
does not shout every week. An alarm that always fires stops being read.

**Also check:** whether any other roster country resolves under about half the
registry. That is the same problem at a smaller scale and nobody has looked.

## 3. GB and US retrieve more `information` coverage under their colloquial names

`roster_name_probe`, 2026-09-22, ten per theme over a 30-day window:

| | roster name | colloquial | total | `information` theme |
|---|---|---|---|---|
| GB | United Kingdom 47 | Britain 54 | ×1.15 | **0 → 4** |
| US | United States 53 | America 58 | ×1.09 | **3 → 8** |

Neither total clears the 1.25 materiality bar, so neither is overridden. But
`information` is the thinnest ledger, and both names roughly double or better
its retrieval — GB retrieves *nothing at all* for press freedom, transparency
and official statistics under "United Kingdom".

Why it is not simply changed: recall is not the whole question. "America" also
matches Latin America, South America and Central America, and the totals cannot
see the precision cost. The relevance gate can — it labels every candidate —
so the honest way to settle this is to run both names through the gate on a live
week and compare *eligible* counts rather than fetched ones.

That belongs with Part 7's verification, where the gate is being measured
anyway.

## 4. The three candidate gate models agree with each other on only 69–75% of articles

`gate_bakeoff.py`, 2026-09-22, 99 articles from US, PT and KW. Each model is
96–98% self-consistent across three repeats, and they agree with each other far
less than that.

Reproducible is not the same as right. Nothing measured so far establishes which
model is correct, only that each is reliably itself. Part 7's hand-labelled
sample is the thing that decides, and the per-article labels and reasons are
written out so the two can be joined.

The pattern in the disagreements is worth reading before labelling: the cases
that split are policy-speech and institutional-change stories — a defence deal,
a court's approval rating, a president's position on regulation — where
"an event happened" and "the country's position changed" are both true readings.

## 5. `_rank_ids_by` raises on mixed timezone-awareness

A pinned bug, recorded rather than fixed, and inherited rather than introduced.
Two articles with equal impact and differing tz-awareness reach a datetime
comparison and raise `TypeError`. Equal impacts are routine, because the caller
fills missing scores with `setdefault(aid, 0.0)`.

`backend/testing/test_util.py::test_rank_ids_by_mixed_tz_raises` pins it, so a
fix is a deliberate, visible act rather than an accident. The Top-3 selection
this function serves is being replaced when the scorer is rewritten, which may
delete the problem rather than fix it.

## 6. Masking is not built, and the seam is marked

Not a decision this session was asked to make. The seam is `llm_artifact.mode`,
which is in the artifact primary key and carries `'named'` throughout, plus the
point between payload assembly and the scorer call where a `mask_payload()`
would sit.

Keeping `mode` in the key now means masking can be switched on later without a
migration and without a stale cache serving a named digest to a masked run —
which would put a country's name into a prompt that was supposed not to have
one, with every other check passing.

## 7. `risk_snapshot.score` changes meaning and the front-end has not been told

When the scorer is rewritten, `score` goes from a 0–1 float to a 0–100 integer,
and the dashboard reads that column directly
(`frontend/app/lib/risk-server.ts`). The dashboard will show wrong numbers until
the front-end session catches up. This was accepted deliberately rather than
worked around; it is recorded here so it is not rediscovered as a bug.

## 8. Both READMEs now describe an instrument that no longer exists

`README.md` and `backend/README.md` still document the pre-v2.0 product, and
several sections are now actively wrong rather than merely incomplete:

- the coverage universe is given as 56 countries; it is 48
- the AI prompt is reproduced in full, and it is the old 0-1 `conflict_war`
  prompt, not `RISK_PROMPT`
- the schema table lists `indicator` / `yearly_value` / `recent_indicator` and
  omits `article`, `llm_artifact` and `payload_census`
- `backend/README.md` reproduces the DDL for seven tables, and that copy is now
  stale: `country` and `risk_snapshot` have both gained columns

The DDL half of this is **fixed**, and not by choice. Prose does not run: the
first end-to-end run on a fresh database reached the upsert and died on
`relation "indicator" does not exist`. `indicator`, `yearly_value`,
`risk_snapshot` and `risk_snapshot_article` are now provisioned by
`data_upsert/schema.py` like everything else, so the code is the record.

What is left is the prose itself. Both READMEs still describe the old
instrument, and a README rewrite belongs with the front-end work, where
`risk_snapshot.score` changing meaning has to be dealt with anyway (item 7).

## 9. The no-round-numbers instruction is followed about two-thirds of the time

The prompt says never to return a multiple of 5, because a 55 or a 70 usually
means a band was picked rather than a country assessed. Measured on the first
two live runs:

| | round numbers returned |
|---|---|
| first wording (framed around "a rating") | 5 of 8 |
| after tightening it to cover every score returned | 4 of 12 |

The composites obey it (38, 36, 54, 52). The residue is entirely in the **ledger
scores** — 20, 45, 40 — which suggests the model treats a ledger reading as a
coarser judgement than the rating, whatever the prompt says.

Left as measured rather than tuned further. Two more prompt rewrites would move
the number without anyone knowing whether the scores got better or just less
round, and the round-number share is exactly the kind of thing that should be a
line in the determinism study rather than something chased by hand. Re-measure
it there, across more than two countries.

## 10. Kuwait scored 58 and then 54 on consecutive runs a few minutes apart

Same week, near-identical evidence — the article set differed by one, and the
payload fingerprint changed accordingly, so this is not a clean repeat. It is
recorded because a 4-point move on an unchanged week is the shape of the problem
the determinism study exists to size, and because it is the first live
observation of it on the v2 payload.

Do not quote it as a noise measurement. A real one needs the same fingerprint
scored N times, which is Session B's smoke check and a later session's proper
study.

