# Deferred

Decisions taken and deliberately not acted on, with the reasoning attached so
the next session does not re-derive it. Nothing here is a bug; each is a real
choice waiting for the right moment.

---

## 1. Persist live articles

`article` is currently a rename of `historical_article`. The daily run still
discards every article it fetches once the snapshot is scored — only the top
three survive, in `risk_snapshot_article`.

**Why it matters.** The series has a seam. The backfill's articles are stored
forever; articles the daily run fetches after the pilot's window are thrown
away. So today's news becomes tomorrow's unrecoverable history, and continuing
the series past the pilot would mean re-harvesting a period already read.
Persisting live articles is what closes that seam.

**Why not now.** The brief asked for both "behaviour must not change" and "the
daily run gains the provenance the backfill has". Behaviour wins; the schema
migration is the wrong place to add a new write path, and `source_system`
already distinguishes google-news / guardian / gdelt / nyt whenever it is
turned on. Roughly 50-100 rows per country per week.

## 2. An API layer between the two halves

A backend refactor breaks five frontend routes only because the frontend
queries Postgres directly, which makes column names the contract between the
halves. `frontend/app/lib/risk-server.ts` is the single file holding every
query.

Worth deciding later whether an API belongs between them. Explicitly out of
scope for this refactor.

## 3. `live_tv_channel` — resolved: it never existed

Queried at `frontend/app/lib/risk-server.ts:426`. Confirmed against the live
database before it was dropped: **the table was never created**, in a schema
that had been running for months. `fetchChannels` catches and returns `[]`, so
the pane has always silently fallen back to `terminal-seed.ts`.

Nothing to migrate. Either create the table and write to it, or delete the
query and the fallback dance with it.

## 3a. Thirteen registry indicators have no reachable source

The bootstrap builds 25 of 38 registry codes. The other 13 are all
curated-source — nobody has produced the values, and `curated.csv` is committed
with a header and **zero data rows**:

```
GOV.DEBT.DOMESTIC.SHARE   National debt agencies / IMF Article IV
GOV.DEBT.FX.SHARE         National debt agencies / IMF Article IV
INFORMAL.PCT.GDP          IMF WP/18/17 informal economy
NIIP.GDP                  IMF Balance of Payments / IIP
OBS.SCORE                 IBP Open Budget Survey
OECD.PISA.MEAN            OECD PISA
OECD.TAX.WEDGE            OECD Taxing Wages
RESERVES.USD              IMF IRFCL (manual)
RSF.PRESS.SCORE           RSF World Press Freedom Index
STAT.TAX.TOP.RATE         OECD Corporate Tax Statistics
UN.EGDI                   UN EGDI
UNWPP.DPND.OL.PROJ        UN WPP medium variant
WUI.INDEX                 World Uncertainty Index
```

Nothing was lost in the rebuild — the old database had no rows for these
either, for the same reason. But the brief's "my research ships with the repo"
is not yet true for these thirteen: the research does not exist. Each is either
a manual entry into `curated.csv` or a fetcher nobody has written.

`payload_census` is the tool that shows this per country; the friction ledgers
score on the 25 that do arrive.

## 4. WEO vintage dataflows may retire `weo_vintages/` entirely

The two unreachable editions are `IMF.RES:WEO_2025_OCT_VINTAGE(1.0.0)` and
`IMF.RES:WEO(9.0.0)`. If vintage-specific dataflows exist for older editions
too, all nineteen downloaded `.xls` files become fetchable and the folder can
go.

**One query in phase 4 answers it — report, do not act.** The hard condition if
it is ever wired up: it must be the *vintage* dataflow. Reaching only the
current edition is not a substitute — stamping today's values as an
October-2025 vintage injects present knowledge into past anchors, which is the
exact failure the vintage store exists to prevent, and it would be invisible in
the data. Verify against ground truth: TUR `NGDP_RPCH` must read 2024 = 3.328
and 2025 = 3.494 with a last-actual-year marker of 2024. If the SDMX response
carries no last-actual-year field at all, stop — the projection-exclusion logic
has nothing to key on.

## 5. `risk_snapshot.raw_subscores`

Created in `_RISK_SNAPSHOT_DDL` and never written. Drop it in the schema phase
or start writing it.

## 6. `data_upsert` and `news_fetching` form a package-level cycle

`data_upsert.store.article_row` calls `news_fetching.core.classify_themes` so a
row with no query provenance still gets themed; the three adapters in
`news_fetching` import `data_upsert.store` to write.

The module graph is acyclic and `check_imports.py` is clean, so Python is
happy. Inlining the classifier into `store` would break the cycle and also fork
the shared core, which `test_news_fetching.TestNoAdapterForksTheCore` forbids by
name. The alternative is moving `classify_themes` somewhere both can depend on.
Not worth a move on its own; revisit if a third module needs it.

## 7. `testing/test_llm.py` is past 1,000 lines

1,142 after the probe scoring was restored. The agreed rule is to split only
when a file passes ~1,000 lines *and* has a genuine seam. There is one — the
probe measures the instrument rather than the country, the same line the schema
phase draws for `snapshot_diagnostic` — but six folder files plus one
invariants file is the agreed shape, so it stays whole for now.
