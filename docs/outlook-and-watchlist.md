# Country outlook, watch list and forecast ledger

Branch `backend-update`, built 2026-11-09 on top of `04d700b`. Dev database only
(`RISK_DB_TARGET=dev`, `neondb` on `ep-round-brook`). Nothing was pushed.

The Country Detail screen of the new frontend prints two blocks the backend did
not produce: "Where it is headed" and a watch list. This session adds a second
instrument beside the scorer, the **outlook**, which is a forecast with a
horizon, a range and a probability over bands; a **watch list** that persists
across weeks and resolves; and a **forecast ledger** that grades both against
what later happened and writes what it learns back into the next week's prompt
as a calibration memo. The scorer is untouched: `CHECKLIST_PROMPT`,
`QUESTIONS`, `SCORING`, the schema builder and `PROMPT_VERSION`
(`77c7868787e3…`) are byte-identical before and after.

## 0. State check, 2026-11-09

| What | Found |
|---|---|
| Branch, HEAD | `backend-update`, `04d700b` |
| `bootstrap` on dev before the change | the ten tables present, `verify` all true |
| `risk_snapshot`, test set | 15 rows: US, PT, KW, HK, TW on **2026-09-25**, **2026-11-07**, **2026-11-09**; nothing else in the table |
| Prompt versions in those rows | three distinct (`360a84…`, `982172…`, `77c786…`); the 09-25 rows predate masking and name the country in their text |
| `economic_calendar_event` | 194 rows, `event_time` 2026-11-08 to 2026-11-23, 182 forward-dated; `created_at` only on 11-07 and 11-09, and the upsert deletes rows a day after they pass, so the table has no history |
| Calendar rows for the test set | US only (`h` 21, `m` 40). PT, KW, HK and TW are not in `FMP_CALENDAR_COUNTRIES` |
| `snapshot_diagnostic` | **empty**. No `repeat` rows existed; the repeat measurement lives in `backend/measurements/step5e/merges/new_masked_<ISO>.jsonl` (five masked merges each for US, PT, KW, HK, TW, RU) |
| Where `ARTIFACT_KINDS` lives | `backend/data_upsert/schema.py`, inside a CHECK constraint that `CREATE TABLE IF NOT EXISTS` cannot widen |
| Backups taken before the schema change | `backend/data/backups/2026-11-09-neondb-pre-outlook-{risk_snapshot,run_ledger,llm_artifact}.csv` (15, 21 and 3,712 rows) |

**Maturation arithmetic on this record.** An outlook made 2026-09-25 targets
2026-10-23 at four weeks; the next snapshot is 2026-11-07, **15 days late**,
beyond the 7-day production rule. So no four-week evaluation can mature from
the stored weeks. The first production 4w evaluation is **2026-12-05** (the
11-07 outlooks, if a snapshot lands that week); the first 12w evaluation is
**2026-12-18** (the 09-25 outlooks). The ledger assumes weekly snapshots:
every skipped week widens the realization lag the same way the 09-25 to 11-07
gap did.

## 1. Schema: twelve tables

Two kinds of fact arrived that fit none of the ten.

**`country_outlook`**, key `(country_iso2, run_date)`. A forecast made on a
date. Keyed like a snapshot but a different fact: a snapshot says what the
week looked like, an outlook says what the coming weeks will look like, and
only the second is graded against later rows. Immutable once written; stamped
with `outlook_prompt_version`, `outlook_model`, `git_sha`,
`payload_fingerprint` and `lessons_version` (`OUTLOOK_STAMPED_FIELDS`, checked
by the same `_check_stamps_on` the snapshot uses). Refused without a manifest.
Columns authored by the model: `horizon_4w`, `horizon_12w` (each `{direction,
score_low, score_high, p_band}`), `ledger_direction`, `drivers`, `base_case`,
`upside_risk`, `downside_risk`, `headed_text`, `confidence`,
`key_uncertainties`, `changes_since_last`. Computed: `inputs_fingerprint`,
`payload_tokens`, `manifest` (block provenance and row counts, calendar
availability, every redaction, the violations and counts, the sent payload).

**`watch_item`**, key `(country_iso2, item_id)`, `item_id =
content_hash(iso2 | kind | normalized title | anchor week)` where the anchor
is the Monday of the due date's week, or of the first-seen week for an undated
development. One row per thing to watch, living across weeks:
`kind` (`scheduled_event | calendar | development | deadline`), `title`,
`due_date`, `window_end` (due + 14 days, or first seen + 56 for developments),
`question`, `expected_direction` (`up | down | unclear`), `importance`,
`source`, `first_seen_run_date`, `last_seen_run_date`, `seen_count`,
`ever_shown` (made the displayed top five at some point; the ledger's
"was it in front of the analyst" metric), `status`, `resolved_run_date`,
`resolution_note`, `resolution_evidence`, `superseded_by`. Status is the only
part the resolver rewrites; a week re-run is the same sighting, not a second
one.

Evaluations are measurements of the instrument and go in
`snapshot_diagnostic`, which also gained its first writer:

| `kind` | `variant` | what |
|---|---|---|
| `repeat` | `step5e-new_masked` | the step 5e masked merges, one row per country, the noise floor |
| `outlook-eval` | `4w`, `12w` | a production forecast graded at most 7 days late |
| `outlook-eval` | `4w-late`, `12w-late` | the same grade 8 to 45 days late: a plumbing check, excluded from the memo, the API track record and the export |
| `outlook-eval` | `<arm>\|4w` etc. | a diagnostic prompt variant graded the same way |
| `outlook-arm` | `<arm>` | the variant's forecast row itself |
| `watch-eval` | `''` | the watch list's week, per country |

`llm_artifact` gained `kind='lessons'` for the calibration memo, reconciled
onto the existing CHECK by a `DO` block in `schema.RECONCILIATIONS` that
`create_all` runs after the indexes.

## 2. The outlook instrument

`backend/llm/outlook.py`, constants appended to `backend/llm/constants.py`
(`OUTLOOK_PROMPT`, `OUTLOOK_SCORING`, `build_outlook_schema`,
`OUTLOOK_PROMPT_VERSION = content_hash(prompt + schema version +
json.dumps(OUTLOOK_SCORING))`). The memo text is outside the version by
design and stamped on the row as `lessons_version`.

One call per country per week, `gpt-4o-2024-08-06` (the scorer's model),
strict structured output in generation order: `drivers`, `ledger_direction`,
`horizon_4w`, `horizon_12w`, `key_uncertainties`, `confidence`, the prose,
`headed_text` last, then `watch_resolutions` (the open item ids are an enum in
the schema, so a verdict cannot name an item the model was not shown) and
`new_watch`.

`assemble_outlook_payload(iso2, run_date)` builds seven blocks, every reader
filtering `run_date <= as_of`, so one function serves the live run and the
backfill:

1. **reading**: the snapshot's scores, ledger integers, condition flags, bullet
   summary, coverage, the merged checklist (level, evidence, quote, note,
   carried flag per question) and the article list (id, redacted title,
   publisher, date, themes).
2. **trend**: up to 16 weeks of scores with the 4-week and 12-week changes
   computed by code (nearest snapshot at or before the target, with the real
   gap in days stated, or "not available"), and per-question levels by week.
3. **data**: the resolved indicators from `manifest.scoring` (value, period,
   as-of, risk score) with the change against the previous snapshot's
   manifest. The stored form of the block `assemble_country` built, so live and
   backfill see the same thing.
4. **dated items**: this week's scheduled events, every open watch item with
   weeks open, and calendar rows up to 90 days out with importance `h` or `m`,
   filtered to rows the database held on the as-of date.
5. **memory**: the previous outlook in full and any matured production grade
   of it.
6. **base rates**: across every country and week on record up to the as-of
   date, the distribution of score changes over pairs 21 to 35 days apart
   (4w) and 70 to 98 days apart (12w), and the noise floor.
7. **calibration memo**: the newest `lessons` artifact that existed on the
   as-of date, or "no evaluated history yet".

Then masking: every string is passed through `gazetteer.redact`, the
replacements are recorded in the manifest, and `gazetteer.assert_clean` runs on
the result. A surviving term raises `MaskLeak` and that country-week is
skipped, never sent.

The validator (`backend/llm/outlook_validate.py`) enforces ranges in 0..100
with low ≤ high, `p_band` summing to 1 ± 0.01 with the midpoint's band modal,
drivers citing payload ids or a checklist question, strict ISO dates inside 90
days, prose under 400 characters and `headed_text` under 600, `headed_text`
quoting a `NN-NN` range equal to a horizon together with that horizon's
direction word, at most six new items, every open item resolved or
`still_open`, and no country term in the output. Every repair is a counted
violation written into the manifest.

## 3. The watch list

`backend/llm/watch.py`. After the outlook, per country:

```
new id --ingest--> open <--re-seen (seen_count+1, last_seen, importance)
                    |
   model happened --+--> happened       (evidence: note + article ids)
   model did_not ---+--> did_not_happen
   same title+question, later date ingested --> superseded (superseded_by)
   window_end passed, no verdict ---------------> expired
   calendar: actual seen before the prune ------> happened; else expired
```

Order within a week: ingest (scorer events, outlook proposals, calendar rows),
the model's verdicts (calendar items excepted: a release is confirmed by the
feed's `actual`, not by a reading of an article), supersession, the clock.
Terminal states never reopen.

`confirm_calendar` runs in `run_etl` **before** the calendar upsert, because
that upsert deletes rows a day after they pass and the `actual` is visible only
then. With weekly runs that is roughly one day in seven; everything else
expires with the note "calendar row pruned before confirmation". The backfill
confirms nothing.

Importance, computed by code and stored every week:

```
importance = 0.4 × nearness + 0.3 × question_weight + 0.2 × persistence + 0.1 × clarity
nearness        = 1 at ≤ 7 days to due, falling linearly to 0 at 90 days; developments 0.5
question_weight = composite weight of the question's ledger / 0.4 (friction, order 1.0;
                  information 0.5; edge 0.1 so it shows but ranks last; calendar rows 0.5)
persistence     = min(seen_count, 4) / 4
clarity         = 1 for up or down, 0 for unclear
```

The display takes the top five open items, dated before undated on ties, and
marks them `ever_shown`. The week's counts go to `run_ledger` as
`job_type='watch'`.

## 4. The forecast ledger

`backend/llm/forecast_eval.py`, `python -m backend.main forecast-report`, and
at the end of every `etl` before the quality report.

A forecast matures when a snapshot exists at or after `run_date + 28` (or
`+ 84`) days; the first such snapshot is the realization. At most 7 days late
it is a production grade; 8 to 45 days late it is written as `-late`. Per
grade:

```
change        = realized.score_12m − origin.score_12m
realized dir  = flat if |change| ≤ floor, else up or down
range_hit     = low ≤ realized ≤ high
direction_hit = forecast direction == realized direction
within_noise  = direction_hit or |change| ≤ floor
abs_error     = |realized − (low + high) / 2|
brier         = Σ over bands (p_band − 1[realized band])²
log_loss      = −ln(max(p[realized band], 0.001))
ledger hits   = same direction rule per ledger, against that ledger's floor
```

The noise floor is the median over `repeat` rows of (max − min) of
`score_12m` across that row's runs; per ledger likewise. From the step 5e
masked merges it is **0.5 points** (worst spread 2; PT, KW and HK were
identical across five runs; the ledger integers did not move at all). That is
a measured fact about the scorer, reported as is: it makes almost any
non-zero change a direction, and the base-rate block tells the model so.

`watch-eval` per country per week: items resolved this week by status,
trailing-12-week precision `happened / (happened + did_not_happen + expired)`,
median lead days from first seen to resolution, and the share of happened
items that had been shown.

`forecast-report` prints coverage (with the first possible maturation date per
horizon), the 4w and 12w tables (range hit rate, direction hit rate, the same
within noise, mean Brier, MAE, mean width, mean confidence, calibration by
confidence bucket) cut overall, by country, by `outlook_prompt_version` and by
`lessons_version`; the arms side by side; the plumbing table of late rows
with the lag on each; the watch table; and the systematic errors (mean signed
error by driver ledger, direction bias, width against hit rate, clustering by
country). It records itself in `run_ledger` as `job_type='forecast-report'`,
which is what `/api/outlook/report` serves.

## 5. The feedback loop

**Memo rule.** `write_lessons` turns the systematic-errors block into plain
sentences with numbers, under 1,200 characters, by template, not by a model
call. Stored as `llm_artifact kind='lessons'` keyed by the hash of its text,
`version = OUTLOOK_PROMPT_VERSION`. Production grades only; `-late` rows never
feed it. Unchanged text keeps its hash, so `lessons_version` on the rows
moves only when a new evaluation changed the numbers. With nothing matured
the memo is not written and block 7 says "no evaluated history yet".

**Variants.** `OUTLOOK_VARIANTS` in `outlook.py`: `memo` (production) and
`memo+anchors` (adds an instruction to start every range from the base-rate
block). The non-production variant runs on the test set each week into
`outlook-arm` and is graded under `<arm>|<horizon>`.

**Export.** `python -m backend.main forecast-export` writes
`backend/data/exports/outlook-<date>.jsonl` (gitignored): one line per
production-matured outlook and horizon with the masked payload that was sent,
the structured forecast, the realized outcome and the grade. Deletes nothing.

**What is not done.** No fine-tuning, no model switch, no scorer change, no
votes. The ledger needs four matured 4w weeks (first possible 2026-12-05) and
twelve 12w weeks before any of that means anything.

## 6. Backfill

`python -m backend.llm.outlook backfill [--dry-run] [--limit N] [--no-arms]`.
Loads the step 5e repeats, lists the stored country-weeks oldest first,
assembles and counts tokens before paying (the dry run measures every
country-week), projects with `usage.project` against the cap, then for each
week runs the outlook, the watch list and the arm, checking the meter after
every call. Then `forecast_eval.run`, the report, and a `run_ledger` line
(`job_type='outlook-backfill'`).

_Measured numbers are in section 8._

## 7. Pipeline and contract

`run_etl`: `watch.confirm_calendar` before the calendar upsert; after
`upsert_snapshot`, `outlook.run_week` (outlook, watch list, and the arm when
the country is in the test set), with a failure written to `run_ledger` as
`job_type='outlook', status='error'` and the country still counted as scored;
after the run ledger line, `forecast_eval.run` and `write_lessons`, then the
quality report. `main.py` gained `forecast-report` and `forecast-export`.

`GET /api/outlook?iso2=PT` (`frontend/app/api/outlook/route.ts`, SQL in
`risk-server.ts: fetchOutlook`, cached 12 hours per country):

```
{ iso2, run_date,
  headed: { text, horizon_4w: {direction, low, high, p_band}, horizon_12w: {...},
            confidence, drivers: [...], changes_since_last },
  watch:  [ { title, kind, due_date, question, ledger, direction, importance,
              status, first_seen, weeks_open } ],            // top 5 open
  track_record: { n_4w, hit_rate_4w, brier_4w, mae_4w, n_12w, hit_rate_12w,
                  brier_12w, watch_precision_12wk, lessons_version } }
```

Scores are integers 0-100. `headed.text` is `headed_text` verbatim. 400 for
a bad `iso2`, 404 for a country without an outlook. `GET /api/outlook/report`
returns the latest recorded `forecast-report`. The track record counts the
production variants only.

## 8. What the first run measured

**Backfill, 2026-11-09, commit `c5933ff`.** Fifteen country-weeks (US, PT,
KW, HK, TW on 09-25, 11-07, 11-09), production plus the `memo+anchors` arm.
A first pass had run under the schema commit `b4ce39d` and was backed up
(`backend/data/backups/2026-11-09-neondb-first-backfill-b4ce39d-*.csv`) and
wiped, because `git_sha` is a stamp and a forecast should name the code that
made it; the second pass is the record.

| Measure | Value |
|---|---|
| Calls | 30 on `gpt-4o-2024-08-06` (15 production, 15 arm) |
| Projected before paying | $1.33 (largest payload, 11,715 tokens in, 1,500 out, over 30 calls) |
| Actual | **$0.91** (268k in, 24k out) |
| Mean production call | **$0.0275** |
| Range | $0.016 at 3.0k payload tokens (the 09-25 weeks, whose old manifests carry no quotes) to $0.066 at 19.3k tokens (US 11-09, with 60 open calendar items) |
| Per country-week, production only | about $0.03; about $1.40 a week for the 48-country roster, on top of the scorer's $0.12 a country |
| Validator violations | 65 over 15 rows: 53 are US 11-09's open calendar speeches the model did not address (fixed after the run: calendar items are the feed's to resolve and left the required set); 10 are `p_band` putting the modal probability on a band other than the one holding the range's midpoint (TW and US on 11-07; PT, TW and US on 11-09); 2 drivers cited an article id not in the payload (KW, PT) |
| Mask leaks sent | 0. `assert_clean` passed on all 15; the named-era 09-25 rows needed 15 to 26 redactions each |
| Noise floor | 0.5 points on `score_12m` (6 repeat rows, 5 runs each, worst spread 2); every ledger floor 0 |
| Forecasts graded | 0 production-matured; 5 `4w-late` (the 09-25 outlooks against 11-07, 15 days late) plus the 5 arm equivalents; 25 pending |
| Watch list after the run | 100 items: 60 calendar (all US; one superseded), 37 developments, 3 scheduled events; open 70 US, 10 PT, 7 HK, 7 TW, 6 KW; nothing expired yet because the 09-25 manifests had no dated events and developments expire 56 days after first seen |
| Export | `backend/data/exports/outlook-2026-11-09.jsonl`, 0 lines, as it must be with nothing production-matured |

The first `forecast-report` (the report also runs at the end of the backfill
and recorded itself in `run_ledger`):

```
=== Forecast ledger - 2026-11-09 ===

coverage: 15 outlooks over 5 countries and 3 run dates (2026-09-25, 2026-11-07, 2026-11-09)
  4w: 0 production-matured, 5 late (plumbing); first possible 2026-12-05 (HK, KW, PT, TW, US); 5 target(s) already passed without a qualifying snapshot
  12w: 0 production-matured, 0 late (plumbing); first possible 2026-12-18 (HK, KW, PT, TW, US)
  noise floor: a median absolute spread of 0.5 points on score_12m across 6 countries run 5 times each (worst spread 2)

  short term (4 weeks): no production-matured forecasts yet
  long term (12 weeks): no production-matured forecasts yet
  arms: no matured arm evaluations yet

  PLUMBING CHECK: realizations 8 to 45 days late. Excluded from the memo, the API track record and the export; they prove the eval path, not the instrument.
    HK 2026-09-25 4w: forecast flat 53-55 | realized 24 (-30) on 2026-11-07, 15 days late | range miss, direction miss, brier 1.460
    KW 2026-09-25 4w: forecast flat 53-55 | realized 40 (-14) on 2026-11-07, 15 days late | range miss, direction miss, brier 0.060
    PT 2026-09-25 4w: forecast up 38-40 | realized 17 (-21) on 2026-11-07, 15 days late | range miss, direction miss, brier 1.340
    TW 2026-09-25 4w: forecast flat 36-38 | realized 20 (-17) on 2026-11-07, 15 days late | range miss, direction miss, brier 1.140
    US 2026-09-25 4w: forecast up 58-60 | realized 26 (-32) on 2026-11-07, 15 days late | range miss, direction miss, brier 1.260
    arm memo+anchors|4w-late HK 2026-09-25: forecast flat 53-55 | realized 24 (-30), 15 days late | range miss, brier 1.460
    arm memo+anchors|4w-late KW 2026-09-25: forecast flat 53-55 | realized 40 (-14), 15 days late | range miss, brier 0.060
    arm memo+anchors|4w-late PT 2026-09-25: forecast up 37-39 | realized 17 (-21), 15 days late | range miss, brier 1.460
    arm memo+anchors|4w-late TW 2026-09-25: forecast flat 36-38 | realized 20 (-17), 15 days late | range miss, brier 1.140
    arm memo+anchors|4w-late US 2026-09-25: forecast flat 56-60 | realized 26 (-32), 15 days late | range miss, brier 1.460

  watch list (latest week per country)
       open wk + wk - wk exp wk sup prec12w  lead shown
  HK      7    0    0      0      0       -     -     -
  PT     10    0    0      0      0       -     -     -
  US     70    0    0      0      1       -     -     -
  KW      6    0    0      0      0       -     -     -
  TW      7    0    0      0      0       -     -     -

  systematic errors (production-matured only)
    insufficient: no production-matured evaluations
```

The plumbing rows all miss, and they should: the 09-25 to 11-07 move is the
scorer's prompt change between step 5c and step 5e (the roster dropped 14 to
32 points on an unchanged world), not a four-week outcome. That is exactly why
those rows are excluded from everything downstream.

**What the model wrote.** The 09-25 weeks, whose manifests predate quotes and
dated events, produced narrow `flat` ranges of two or three points at 0.7
confidence; the 11-07 and 11-09 weeks produced three-to-six-point ranges with
`up` calls driven by the order ledger (PT housing protests, US defence-budget
standoff, TW). The two weeks two days apart gave near-identical forecasts, as
they should. PT on 11-09, as the API returns it:

```
run_date: 2026-11-09
headed_text: 4 weeks, 20-24, up: Protests and inflation concerns are driving an increase in friction and disorder.
horizon_4w:  up 20-24, p_band low 0.5 / elevated 0.5 / high 0
horizon_12w: up 22-28, p_band low 0.3 / elevated 0.7 / high 0   (midpoint 25 sits in low: the flagged violation)
confidence 0.7; changes_since_last: protests and inflation concerns have intensified
watch (top five open):
  0.84  scheduled_event 2026-11-18 O4 up   University students will march to protest rising housing costs ...
  0.65  development     undated    F1 down Changes in tax policy
  0.65  development     undated    F4 down Government fiscal policy effectiveness
  0.65  development     undated    O4 up   Impact of police violence on civil unrest
  0.65  development     undated    F2 up   Inflation trends and impact on economy
track_record: n_4w 0, n_12w 0, watch_precision_12wk null, lessons_version null
```

**Identifiability probe** (gpt-4o-mini, the step 5e question, on the
assembled and redacted payloads):

| Payload | Verdict | What gave it away |
|---|---|---|
| PT 2026-11-09 | not enough to tell (Portugal at 0.4) | "Largo do Carmo", a square the gazetteer does not hold |
| PT 2026-09-25 (named era, redacted by code) | not enough to tell (Brazil at 0.4) | Brazilian tourist arrivals |
| US 2026-11-09 | identified (0.6) | defence budget and operations against Iran; the US is exempt from term stripping in the gazetteer by design |
| HK 2026-09-25 (named era) | identified (0.6) | the publisher "South China Morning Post" |

Two of four identified, both through channels the scorer's masking also leaves
open (publishers, and the US exemption). Same finding as step 5e; the outlook
payload is no leakier than the scorer's.

**Observations for the next session.** The importance formula puts US's
calendar speeches (nearness 1, re-seen, no question) at 0.65, above every
undated development, so the US top five is Fed speakers; the formula gives no
weight to the feed's `h`/`m`. The `p_band`-off-midpoint violation is the most
common model error and is exactly what the Brier score will punish. The API
caches each country for twelve hours through Next's data cache, which the dev
server persists under `frontend/.next/cache/fetch-cache` across restarts, so a
row written after a fetch shows up on the next window or after that folder is
cleared.

**Verification against the brief.**

1. `bootstrap` on dev: twelve tables, `verify` all true, the `llm_artifact`
   CHECK shows `lessons`. 254 tests pass (two existing counts moved to 12 and 7).
2. The backfill ran as the ETL's outlook step would; every test-set
   country-week has a `country_outlook` row, the violation count printed per
   country-week, and `watch_item` holds the four kinds (calendar only for US).
3. PT's `headed_text`, horizons and top five printed above from
   `GET /api/outlook?iso2=PT`; probe verdicts above.
4. `forecast-report` prints five `4w-late` rows and the systematic-errors block
   marked insufficient; `forecast-export` wrote the JSONL with zero lines.
   Zero production-matured rows, first possible 2026-12-05.
5. `GET /api/outlook?iso2=PT` returns the contract; `?iso2=pt` is accepted,
   `?iso2=xyz` is a 400, `?iso2=ZZ` a 404; `/api/outlook/report` returns the
   recorded aggregate.
6. `etl --test-set`: waits for the UTC date to turn (today's snapshots exist
   and `git_sha` is a stamp, so a same-day re-run would refuse every country
   after paying the scorer). Status at the end of this doc.

## 9. How to promote a variant

1. Read `forecast-report`'s "arms side by side" tables for both horizons. The
   arm has to beat production on range hit rate without a wider mean width, or
   on Brier at the same hit rate, over at least four matured weeks.
2. Change `PRODUCTION_VARIANT` in `backend/llm/outlook.py` to the arm's name.
   The arm's suffix becomes part of the prompt the production rows are made
   with; bake it into `OUTLOOK_PROMPT` so `OUTLOOK_PROMPT_VERSION` moves, and
   give the old production prompt a variant entry if it should keep running
   as the diagnostic.
3. Commit the constant change on its own. The next ETL writes rows under the
   new version, and the report cuts by `outlook_prompt_version` from then on.
