# What the test cut removed, and what it costs

The suite went from 9,244 lines of `test_*.py` across 37 files to 3,824 across
6, to hit an under-4,000 target. This is the ledger of what stopped being
guarded, so it can be re-added selectively rather than re-derived.

Everything here **passed** when it was deleted. Nothing was removed because it
was wrong; it was removed because a budget required it. Commits `68773a5`
through `13a65a1` carry the same reasoning grouped by rationale.

A note on the premise, recorded because it affects how this list should be
read: the brief assumed a suite of producer-side characterisation slop. Sixteen
of sixteen files read end-to-end carried a docstring naming a specific
production failure the test prevents. The six defects that shipped this week
(WEO rows nothing read, a digest cache never called, `Meter.check()` never
invoked, probe results nothing stored, `Estimates Start After` never parsed,
`input_manifest` never rebuilt) had **no** test on those seams — and the
cross-seam tests written in response are all still here. The cut did not remove
the cause of those defects, because the suite was not the cause.

---

## Highest risk

### 1. The identifiability probe's scoring
**Was:** `test_masking.py` — the four outcomes (correct-confident, wrong-confident,
declined, insufficient-information), the null control arm, outlet fingerprinting,
per-country hit rates and the spread that is the actual meter, and the two-run
comparison behind `scripts/probe_bundles`.

**Still guarded:** that a probe result is *stored* rather than only logged, and
that a failed probe never blocks a snapshot (`test_llm.py`).

**Risk:** the probe is the instrument that says whether masking held. How it
scores an answer is now unpinned, so a change that makes it systematically
lenient would report clean masking on a leaking corpus. Nothing else measures
this. **Re-add first if you re-add anything.**

### 2. The prompt's required-phrase inventory
**Was:** `test_prompt_v3.py` — the three-door event test, edge protection,
the five bands and calibration anchors, ledger definitions and directions,
`information_capacity` being explicitly inverted, the horizon split.

**Still guarded:** `TestForbiddenLanguage` (enforcement language must never
return) and the schema's strictness, both in `test_llm.py`.

**Risk:** the prompt is product logic with no compiler. A deleted paragraph
costs nothing at import time and changes every score in the roster. The
tripwire against re-added enforcement survives; the guard against silent
deletion does not.

### 3. Per-metric arithmetic
**Was:** `test_metrics.py` — hand-computed cases for doom loop, Rome gap,
monetary dilution, real policy rate, precommitted share, wage-productivity gap,
dependency trajectory, rolling vol, FX monthly returns, suppressed-vol flag and
instrument quality. Each was computed from the docstring's stated definition
rather than from the code, so it failed if the definition drifted.

**Still guarded:** the coercion guard (absent → `None`, never a fabricated
zero; `True` is not a number; NaN is absence), no metric consulting another
country, and oracle cases for `conversion_loss` and `frictional_extraction`
(`test_util.py`).

**Risk:** a slip in any of the eleven unguarded families produces a well-formed
payload and a confident score built on a wrong wedge.

---

## Medium risk

### 4. Stage-1 digest internals
`test_digest_engine.py` — the cache decision matrix keyed on
`(content_sha256, digest_model, mode)`, the masking version in the key, the
runaway retry that re-sends only the failures, and the stamp marking a
recovered digest as being of a *truncated* article rather than of the article.
The token caps themselves survive in `test_invariants.py`.

### 5. Adapter payload normalisation
`test_adapters.py` — Guardian/GDELT/NYT raw payload → canonical item for each
harvester, Guardian query construction and window walking, GDELT failure
isolation, NYT desk filtering, month walking, harvest economics and the volume
cap, and the retry that respects a stated rate limit. `TestNoAdapterForksTheCore`
survives in `test_news_fetching.py`.

### 6. Relevance scoring
`test_article_ranking.py` — `score_relevance`'s 0.1 no-mention floor and its
keyword caps, the topic-representative and backfill branches in detail.
`ensure_top_three` returning exactly three survives.

### 7. Payload stamping and windows
`test_payload_v2.py` — staleness stamps measured against the snapshot's `as_of`
rather than the clock, missing series omitted rather than nulled, the computed
block, frequency-aware volatility windows, the token budget for a fully
populated country, and elections. The loader-to-payload contract and
freshest-value-wins survive in `test_util.py`.

### 8. Postgres round-trips
`test_history_store.py` — `history_run_ledger` and `history_digest_cache`
write-then-read against a real database. Body-beats-stub and idempotent
checkpoints survive (DB-gated) in `test_invariants.py`.

---

## Lower risk, listed for completeness

- **Masking detail** (`test_gazetteer.py`): ordinary English not being mangled
  ("real GDP", "TRY the verb vs TRY the code", "us" vs "US", "Latin America"),
  currency-symbol pattern paths, region masking, institution-before-country
  ordering, longest-form-wins. The 48-country integrity loop and the gate
  survive.
- **Pure helpers**: `utc_minute_iso` / `parse_date_for_sort` / `date_prefix`
  (`test_dates.py`); `_pct`, `_quarter_start`, `_first_close_on_or_after`
  (`test_prices_math.py`); `_normalize_host` / `_host_of`
  (`test_source_filter.py`); `_normalize_event` against the CHECK constraints
  (`test_fmp_calendar.py`); `_parse_iso_date` (`test_langchain_helpers.py`).
- **`core.extract_body`** on real HTML and on empty input; dedupe-key detail.
- **Wayback capture selection** arithmetic — choosing the nearest capture inside
  the window. The leak policy and the dollar cap survive.
- **Digest prompt blocks** — the `.format()` brace-escaping checks on both
  templates, the legacy summary shape, the FULL_TEXT block's header and order.

---

## Deleted now, must be re-created in the schema phase

Both of these die with the parquet panel and were the migration's only safety
net. They were kept through group 5 deliberately and removed in group 6.

| Was | Asserted |
|---|---|
| `test_country_data_fetch.py` | `has_country_partition`; and that the writer's `PANEL_DIR` equals the reader's `DATA_DIR` — a false positive leaves a country with no data at all |
| `test_payload_shape.py` | per-indicator anchoring: anchoring `latest` and the deltas on the panel's newest *shared* row reported null for 5 of 9 indicators for Portugal, with the values sitting one row above |

**Action:** when `indicator_series` absorbs the parquet panel, re-assert
per-indicator anchoring against Postgres before deleting the panel.
