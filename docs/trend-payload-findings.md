# Two ledgers were empty, and more evidence did not help

Written 2026-08-29. Read this if you are trying to understand why the
backfilled scores look the way they do, or why `docs/payload-ab.md` has two
attempts in it.

Three findings, in the order they matter:

1. **A dating bug made ten indicators invisible to every historical anchor.**
   Two of the four ledgers resolved *nothing* — not thin, empty — for the whole
   pilot. Fixed, measured, migrated.
2. **Fixing it did not improve discrimination.** The instrument produced fewer
   distinct scores and more round numbers with the evidence restored.
3. **That is now the second independent evidence addition to fail the same
   way.** The first was `p3-context`. Different mechanisms, same direction.

---

## 1. The bug

`indicator_series.as_of` means *when this observation became public*. The column
comment says so, and the whole no-future rule for macro rests on it:
`payload._resolve` drops any observation published after the anchor, so a 2019
snapshot cannot read a number nobody had.

Three writers stamped it from the clock instead — `date.today()`, at fetch time.
Every row they wrote therefore claimed to have been published on the day it was
downloaded, and the vintage bound discarded all of them at every historical
anchor.

### What it cost, per ledger

Measured at a 2019-06-01 anchor on the pilot corpus. Indicators resolvable per
country, before the fix and after:

| ledger | in registry | before | after |
|---|---|---|---|
| friction | 14 | 5.00 | 9.67 |
| uncertainty | 16 | 9.67 | 10.00 |
| **information** | 4 | **0.00** | **1.00** |
| **edge** | 4 | **0.00** | **2.67** |
| **total** | 38 | **14.7** | **23.3** |

The information and edge scores in every backfilled snapshot were produced from
news articles and the prompt's own calibration language, with no macro evidence
underneath them at all.

Everything measured on a backfilled anchor was measured through that: the p2
reference, the GATE2 baseline, the scorer bake-off, and both arms of the p3
A/B.

### Why nothing caught it

It does not look like a failure. Nothing crashes, nothing warns, and the payload
that arrives is a valid payload — just thinner. The test suite was green
throughout, because `TestSurvivesTheVintageBound` tested `_resolve` against
synthetic observations and `panel_rows` against a literal DataFrame, and never
asserted anything about what the *other* fetcher writing to the same table
actually produced.

`restamp.py` existed to correct exactly this condition and had never been called.
It also could not have run: `read_all()` referenced a constant the ten-table
rebuild deleted, so every path into it raised `AttributeError` before touching a
row. And its `apply()` upserted re-dated rows without removing the originals —
`as_of` is in the primary key, so it would have *duplicated* every row and left
the fetch-dated copy, which carries the later date, still winning
freshest-wins. It would have reported success and changed nothing anybody reads.

### The audit: four instances, not one

| site | verdict |
|---|---|
| `wb_series_fetch` | the one found first |
| `pipeline.refresh_imf_indicators` | same bug — `monthly.py` restamps only the *backfill* path, so the daily path wrote fetch dates throughout |
| `bis_bulk_fetch.refresh_bis_series` | same bug, worst case: BIS has a **zero-day** lag, so the correct `as_of` *is* period end |
| the walkthrough notebook | same class, writing to the real table |
| `country_data_fetch.panel_rows` | **the opposite bug.** Stamps 31-December-of-the-year when WDI/WGI land 9–18 months later, so an anchor in the intervening months reads a number nobody had. Leakage, not starvation — the quiet direction, because it makes a backtest look *better*. Deliberate, pinned by tests, and left alone: see `docs/deferred.md` §24 |

Fixed at the chokepoint rather than per caller. All writers meet in
`data_push.upsert_indicator_series`, which accepted any date and defaulted
`vintage_scheme` to the value meaning "read off the clock". It now re-dates any
row whose `as_of` is implausibly late for its period — more than `MAX_LAG_DAYS`
past period end, which no publisher is — and leaves anything that declares a real
scheme alone. A row stamped *earlier* than its period end is the opposite defect
and is not touched here.

That last clause is why `curated_loader` now declares its own scheme. Its `as_of`
is typed by an operator holding the publication — the best vintage there is — but
an undeclared scheme filed it under the fetch-date name, and both `restamp.plan`
and the new guard read that as permission to overwrite.

### The migrations

| database | rows re-dated | observations lost | row delta |
|---|---|---|---|
| prod (solitary-morning) | 11,698 | **0** | −7,789 |
| dev (winter-silence) | 11,670 | **0** | −5,129 |

Both verified against the pre-change dump: every observation still present,
values intact on a sampled check. The row shrink is duplicate collapse — moves
that landed on a correctly-dated row which already existed beside the
fetch-dated one.

---

## 2. Fixing it did not make the instrument finer

Arm A′ is p2 re-scored on the corrected evidence, same 105 anchors, scorer held
at `gpt-4o`, corpus pinned per anchor beforehand.

| | US 2019 | | TR 2018 | |
|---|---|---|---|---|
| | old p2 | **A′** | old p2 | **A′** |
| distinct composite values | 9 | **8** | 9 | **9** |
| round-number share | 69.2% | **76.9%** | 18.9% | **26.4%** |
| lag-1 autocorrelation | 0.299 | 0.433 | 0.564 | 0.642 |
| longest identical run | 5 | 4 | 7 | 5 |
| ρ against old p2 | — | 0.582 | — | 0.808 |
| cost per snapshot | $0.0402 | $0.0376 (−6.5%) | $0.0382 | $0.0352 (−7.9%) |

Ten indicators restored, including two ledgers that had been empty, and the
series got *coarser* on both windows: fewer distinct values on US, a higher
round-number share on both.

### Two things it did change

**The scores moved where they should.** On TR 2018 — a genuine currency crisis —
**10 of 53 anchors moved Moderate → High**. With rule-of-law and political
stability restored, the model reads that year as materially riskier. That is a
validity gain, and it is not the same thing as a resolution gain.

**The previously-empty ledgers were noise.** Between old p2 and A′,
`edge_vitality` correlates at **0.06** on TR and **−0.29** on US; `friction` at
0.28 and 0.10. The two ledgers that gained the most indicators reordered almost
completely — which is exactly what you would expect from scores that previously
had no macro evidence beneath them. The old edge series was not a weak signal.
It was not a signal.

---

## 3. What this does to the p3 verdict

`p3-context` was rejected on the finding that *more evidence made the instrument
coarser*: distinct values 9 → 7, round-number share 69.2% → 75.0%.

It was measured on a payload whose information and edge ledgers resolved zero
indicators. So the conclusion survives and the measurement does not: whether
four quarters of prose would coarsen a payload with all four ledgers populated
is a question that attempt never answered, because that payload did not exist
while it ran. Recorded as open rather than quietly re-closed.

What is now much stronger is the *pattern*. Two independent evidence additions,
by entirely different mechanisms — masked prose summaries, and ten real
vintage-corrected indicators — both failed to improve discrimination, and both
moved round-number share the wrong way:

| | distinct (US) | round share (US) |
|---|---|---|
| p2, as it stood | 9 | 69.2% |
| p3-context | 7 | 75.0% |
| A′ (vintage fixed) | 8 | 76.9% |

Whatever produces nine-ish distinct values across fifty-two weeks is not
downstream of how much evidence the payload carries.

---

## 3b. And telling the model about the evidence did not help either

Arm C is one paragraph naming `trend_1y` and `trend_5y` — fields every indicator
has carried since p1, serialized into every prompt, mentioned nowhere. No new
evidence: the payload is byte-identical with the variant set and unset, and a
test asserts it.

| US 2019 | distinct | round share |
|---|---|---|
| p2, as it stood | 9 | 69.2% |
| p3-context | 7 | 75.0% |
| A′ — ten indicators restored | 8 | 76.9% |
| C — told the fields exist | 8 | **82.7%** |

Rejected on criterion (a), like the two before it. Full verdicts in
`docs/payload-ab.md`.

**Round-number share rose on all three.** Three interventions, three mechanisms
— prose summaries, real indicators, pure instruction — and the same direction
every time.

### Except on the determinate window

On TR 2018, arm C moved **distinct values 9 → 12** — the best discrimination
figure any arm has produced on either window — at an unchanged round-number
share and 10% *lower* cost, and it turned **earlier** than A′ rather than later.

So the instruction is read and it works, on the window where the evidence
already resolves. On the ambiguous window it changed nothing and the snapping
got worse.

That is precisely the signature the bake-off already found in the prompt's own
"never round to multiples of 5" rule: obeyed 81% of the time where evidence is
determinate, ignored 69% of the time where it is not. Two independent
instructions now show it. **The instruction holds exactly where it is least
needed** — which points at how this model converts ambiguous evidence into a
number, and not at what it is told to do with clear evidence.

`docs/deferred.md` §12, the within-band discrimination test, is the successor
this argues for. It remains proposed and not run.

## 4. What was built to make this visible next time

The failure mode here is not code that crashes. It is code that runs, writes
something, and is read by nobody — and the payload census exists so that the
next instance announces itself.

Every scored run now records, in its manifest:

- **`payload_health`** — registry expectation against what the serialized
  payload actually held, grouped by ledger, with dropped indicators named and
  classified: `no row` (never fetched), `vintage bound` (rows exist, all
  postdate the anchor — *the bug that hid*), `unmapped` (resolved and still did
  not arrive). **A ledger resolving nothing is named, not printed as a zero
  among zeros.**
- **`article_set_sha256`** — the ordered selected corpus, so a later comparison
  can tell payload drift from corpus drift in one query instead of
  reconstructing the selection by hand.
- **`written_by`** — hostname and database, credentials never included. A row
  written by the six-hourly harvest cron and one written from a laptop were
  byte-identical until now, and establishing which of two Neon projects held
  which half of this project cost most of a session.

Also: `RISK_DB_TARGET` must now name `prod` or `dev` explicitly. `DATABASE_URL`
was a default wearing the shape of a config value — every tool reached for the
bare name without deciding anything, and what it reached was production.

And the harvest reports the longest *run* of consecutive failed windows, not
just the total. BR failed eleven in a row and it read as ordinary flakiness,
because a total was all there was.
