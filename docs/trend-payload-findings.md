# Two findings: an empty instrument, and an instruction that stops working

Written 2026-08-29. Read this if you are trying to understand why the
backfilled scores look the way they do, or why `docs/payload-ab.md` has two
attempts in it.

There are two findings here and they are of equal weight. The first is a bug
and its cost. The second is not about this codebase at all.

**One — two of the four ledgers were empty.** A dating bug made ten indicators
invisible to every historical anchor. The information and edge ledgers resolved
*nothing* — not thin, empty — for the entire pilot, and every backfilled score
ever produced was made that way. Fixed, measured, migrated to both databases.

**Two — instruction-following degrades under ambiguity, and that is the
interesting result.** Three separate attempts to improve discrimination by
changing what the payload carries all failed, and one of them succeeded
spectacularly on exactly one window. Arm C — a single paragraph of instruction
carrying no new data — moved TR 2018 from **9 to 12 distinct values**, the best
figure any arm has produced anywhere, at unchanged round-number share and 10%
*lower* cost. On US 2019 the same paragraph changed nothing and made round-number
snapping worse.

That is the same signature the scorer bake-off already found in the prompt's own
*"never round to multiples of 5"* rule: obeyed where the evidence is
determinate, ignored where it is not. **Two independent instructions now show
it.** The instruction holds exactly where it is least needed, which is a fact
about how this model behaves under ambiguity rather than a fact about the
payload — and it is more useful than the three payload rejections that surround
it.

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

## 3b. Four payloads, and the instrument snapped harder at every step

Three interventions were run against the corrected payload, ordered by how much
they tell the model about trajectory: **A′** restored ten indicators, **C** added
one paragraph naming `trend_1y`/`trend_5y` and no new data at all, and **B**
added a computed block stating every direction in words so that no inference is
required.

**US 2019 — the ambiguous window**

| arm | distinct | round share |
|---|---|---|
| p2, as it stood | 9 | 69.2% |
| p3-context | 7 | 75.0% |
| A′ — ten indicators restored | 8 | 76.9% |
| C — told the fields exist | 8 | 82.7% |
| B — directions stated in words | **7** | **90.4%** |

**Round-number share rises monotonically with every intervention.** The most
explicit payload — the one that does the arithmetic for the model and hands it
conclusions — produces the worst snapping of all, at 90.4%, and the fewest
distinct values.

**TR 2018 — the determinate window**

| arm | distinct | round share |
|---|---|---|
| p2, as it stood | 9 | 18.9% |
| A′ | 9 | 26.4% |
| C | **12** | 26.4% |
| B | 10 | 24.5% |

The same interventions, the same prompts, the same model, the same corpus — and
here they *work*. C reaches twelve distinct values, the highest figure anywhere
in this project, and B ten, both above the nine that p2 has produced on every
window it has ever been run on.

All three arms passed (b) and (c): none of them lagged, and none rewrote the
determinate window. B additionally failed (e) at +17.0% on TR — the block costs
~2,500 extra input tokens, the one price p3 avoided by producing shorter output.

## 3c. What separates the two windows is not the payload

Same instructions, opposite outcomes, and the only thing that differs is whether
the underlying evidence resolves.

This is the second time this project has measured that shape. The scorer
bake-off found the prompt's own *"never round to multiples of 5"* rule obeyed on
**18.9%** of TR 2018 anchors — within a point of the 20% you would get by
chance — and violated on **69%** of US 2019 anchors. Monotone in determinacy,
same model, same instruction.

Now a second, independently written instruction shows it: pointing at the trend
fields moves TR from nine distinct values to twelve, and does nothing on US
except make the snapping worse.

**An instruction is followed where the evidence is determinate and ignored where
it is not, and adding evidence does not move that line.** Three payloads that
carried progressively more trajectory information failed to move it; the one
that carried the most made it worst. Whatever converts an ambiguous week into
0.50 is downstream of neither the evidence nor the explanation of it.

That is the finding worth carrying forward, and it is a fact about the model
under ambiguity rather than about this payload. `docs/deferred.md` §12 — an
explicit within-band discrimination instruction — is what it argues for. It is
deliberately not run here: it changes the prompt's scoring mechanics rather than
its inputs, and it deserves a pre-registration written cold rather than appended
to the session that motivated it.

**Run 2026-08-29 and rejected** (`docs/elicitation-ab.md`). Two corrections to
the paragraph above. The instruction was *followed* — the model named a band and
placed its score inside it on all 105 anchors, coherently — and the instrument
still produced eight distinct values, because it had three placement buckets
inside one band. And "a fact about the model under ambiguity" is narrower than it
reads: across six scorers on this prompt, only `gpt-4o` shows the large
window-dependent gap in round-number share, and two of the six show it reversed.
It is a fact about this model.

## 3d. Criterion (d) could not be measured, and that is the same pattern

The A/B pre-registered (d) as *"share of `bullet_summary` outputs referencing
direction"* — the diagnostic p3 lacked, meant to tell an *ignored* block apart
from a *diluting* one, which is exactly the distinction §3b turns on.

Bake-off arm rows carry every number a run produces and none of its prose. The
field the criterion reads does not exist on the arm it was written for, and this
was noticed when the verdicts were computed, after all three arms had been paid
for.

The thirteenth instance of the write-a-thing-nobody-reads pattern, and the first
this project caused in its own instrumentation rather than found in its code:
not a writer without a consumer but a **consumer specified without a writer**. A
session spent building `payload_health` to catch that shape, committing its
mirror image, is the most useful kind of example. `bullet_summary` is now
captured on every arm row — which fixes the next attempt and not this one.

The narrow lesson, in `docs/deferred.md` §28: **compute a pre-registered
criterion once against a stored row before paying for the arms.** At write time,
a criterion that cannot be evaluated looks exactly like one that can.

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
