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
