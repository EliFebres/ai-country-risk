# The History Machine: how a historical rating gets made

Scoring a decade one week at a time, on the live code path. This document covers
what the historical process anonymizes, what it excludes, what it costs, and how
it is measured.

The live pipeline is in [`pipeline.md`](pipeline.md); everything there still
applies. This is the half that differs.

---

## 1. Why it exists, and why it anonymizes

A model asked to rate Türkiye in 2018 may simply **remember** 2018. It knows how
the lira crisis went. A backfill scored that way is not measuring the
instrument — it is measuring recall, and no amount of care afterwards can
separate the two.

So the identity is removed and every number is kept. The scorer judges a country
it cannot name, from evidence that is exactly as published. That is what makes a
2016 backfill and tomorrow's live run **the same instrument**.

Which is also why masking is not a backfill-only trick. It is the production
regime: `scoring_mode` defaults to `'masked'` and the live prompt is
`v4.0-masked-production`. A series that changes scoring regime half way through
its own history is worse than no series at all.

## 2. One premise

> Everything below `snapshot_select` is `pipeline._process_country` with `as_of`
> pinned. The article source is the only difference.

`backend/util/pilot/score.py` drives; it does not compute. There is no second
scoring path, no historical variant of the prompt, no separate payload builder.
If the backfill had its own scoring path, the series would be measuring the
backfill.

The pin itself is one line. `_process_country` writes
`payload["_meta"]["generated_at"] = as_of.isoformat()`, and every downstream
stage — the digests, the evidence payload, the prompt, the sanctions lookup, the
upsert key — takes its date from `data_push.payload_as_of(payload)`. Pinning the
one place the date is derived from pins all of them at once.

Two things a historical run skips, both because they would import the present:

- **`resolve_and_enrich`** — historical items arrive already resolved with the
  body the harvest stored. Re-fetching would replace a vintage-stamped body with
  today's copy of the page.
- **`enrich_top_images`** — an image is decoration, not evidence, and scraping
  three publishers per country per week would be thousands of live fetches to
  decorate a backfill.

## 3. The harvest

Harvesting ten years of articles takes days of somebody else's rate limit, so it
has its own entry point rather than a job in the scheduler's tuple.

| Source | Bodies? | Pacing | Status |
|---|---|---|---|
| **Guardian Content API** | yes, `bodyText` in the search response | reads its own quota header | the spine |
| **NYT Archive API** | no — headline and two sentences | 12 s between calls | abstract tier |
| **GDELT DOC 2.0** | no, URLs only | ~1 call per multi-minute window | **dormant** |
| **Wayback Machine** | recovery for URLs with no body | 1 req/s, 429 backoff | the drain |

### The Guardian reads its quota instead of remembering it

`adapters/guardian.py` reads `X-RateLimit-Limit-Day`, `X-RateLimit-Remaining-Day`
and `Retry-After` off **every** response, and paces off `Remaining-Day` — the
number that actually hits zero. `config.GUARDIAN_DAILY_CALL_BUDGET = 500` is a
fallback for the first call and for responses that carry no headers.

The two disagree, and the code refuses to smooth it over. On 2026-08-15 the
harvest spent **1,461 page-calls** before `Remaining-Day` reached zero, against
an advertised `Limit-Day` of 500 — roughly 3×. `quota()` reports both, because a
limit that lies by 3× is exactly what quietly misinforms the next estimate.

`QuotaExhausted` is not an error, it is a scheduling fact. On hitting it the
harvester prices the remaining work from **what this run measured** per
country-year rather than from a constant. The prior version announced "roughly
one more day" computed off the six-call no-subdivision floor, when the US
actually cost 183 calls per country-year.

**Window subdivision.** Start at a calendar year at `GUARDIAN_PAGE_SIZE = 100`; a
window needing more than 5 pages splits year → quarters → months for that
country and theme only. A month that still overflows is truncated *with a
warning*. Calendar years rather than rolling spans, so a resumed harvest asks for
identical windows.

Page size is 100 rather than the documented maximum of 200 because 200 is not
reliable: one measured `(query, size, page)` triple returned 503 deterministically
at `page-size=200&page=2` while the same 594 results came back fine at
`page-size=100&page=3`. A specific triple failing every time is a server bug, not
a rate limit.

### The NYT archive returns the whole paper

One call per month covers the entire world — about 121 calls for ten years,
which does not grow with the roster. There is no query, so filtering happens
locally against `gazetteer.mentions()`: the same list used to *hide* a country is
used to *find* it.

Everything lands `tier='abstract-only'` and `body_status='degraded-title-only'` —
deliberately not `'pending'`, because a Wayback fetch of a paywalled NYT page
returns the paywall, and queueing them would put ~200k URLs into the drain for
nothing.

### GDELT is dormant, and the reason is a measurement

The DOC 2.0 endpoint answers roughly one call per multi-minute window from a
single IP, making a full harvest about **twelve days** with most windows failing.
Its own 429 body says "please limit requests to one every 5 seconds"; measured,
neither 5 s nor 30 s works. The CLI refuses to start it without `--anyway`,
because the harvest is resumable and polite and looks perfectly safe to begin.

### Every pacing constant says whether it was measured

`backend/util/config.py` labels each one **measured** or **asserted**, and the
distinction is the point — a limit nobody checked cost an hour and a wrong plan
once already.

- **Asserted**: `REQUEST_INTERVAL_SECONDS = 1.0` (a self-imposed floor for free
  services, not a vendor limit), `GUARDIAN_DAILY_CALL_BUDGET = 500`,
  `WAYBACK_WINDOW_DAYS = 180` (a judgement about archive quality — beyond six
  months a page has usually been re-templated).
- **Asserted and unverifiable from the wire**: `NYT_REQUEST_INTERVAL_SECONDS = 12.0`.
  The archive endpoint carries no rate-limit headers at all. What is known is
  that a 121-month harvest at 12 s completed without a single 429 — a *lower
  bound on politeness*, not a measurement of the limit, and the two must not be
  confused.
- **Measured**: `GUARDIAN_PAGE_SIZE = 100`, `NYT_MAX_PER_COUNTRY_MONTH = 150`.
- **Measured and known false as documented**: `GDELT_REQUEST_INTERVAL_SECONDS = 5.0`
  — the number in their error message is not the number they enforce. This is the
  constant the others should be read against.

### The harvest keeps its own clock

`store.write_checkpoint` records the duration and call count **measured by the
harvester**, stamped into `run_ledger.detail`. It was briefly *inferred* from the
gap between consecutive `completed_at` stamps, which is exact only if windows run
strictly in sequence in one uninterrupted process — and the Guardian harvest is
not, because it stops on a daily quota and resumes eight hours later. Day two's
first window read as an eight-hour window.

`reports.harvest_pacing()` reads those rows and extrapolates to 48 countries.
Untimed windows count toward the corpus and **not** toward the pacing, because a
zero would make the extrapolation optimistic in the direction that costs somebody
a day. NYT is not scaled per-country at all — one archive call returns the whole
world, so its calls are charged as a float fraction to each country covered, and
the shares sum back to the one request the archive actually saw.

### Body recovery, and the only billable step

`news_fetching/wayback.py::drain()` walks everything still owed a body: ask the
CDX index for the capture nearest publication, fetch it with the `id_` suffix
(the raw page, no archive toolbar), extract with the same extractor the live run
uses.

When there is no capture, refetching the live page is tempting and mostly works.
But a 2018 article refetched in 2026 can carry a correction, an editor's note, an
"as it turned out" paragraph added years later. That is future text wearing a
past date — the subtlest leak there is, invisible in every count. So a live
refetch does **not** count as recovered until a cheap model has read it and
confirmed it references nothing after its own publication date. Flagged bodies
are discarded and the article drops to `degraded-title-only`.

That scan is the one OpenAI-billable thing in the whole harvest phase. It prints
a projected cost, waits for an explicit yes, and aborts at
`LEAKAGE_SCAN_BUDGET_USD = 3.0`.

### Macro vintages

Three commands rebuild the macro side so an anchor reads what was published by
then, not what is published now:

- `weo` — load the 21 IMF WEO editions from `backend/data/curated/weo_vintages/`,
  each stamped with its own edition date.
- `monthly` — back-date IMF/BIS monthly prints by publication lag.
- `restamp` — migrate already-stored rows from fetch date to publication date. It
  dumps to `backend/data/backups/` first, and supports `--diff`, `--dry-run` and
  `--revert`.

### The harvest runs itself

The corpus is ~20 hours of requests across 48 countries, but wall clock is not
what makes it long: the Guardian free tier is a **daily** call budget, and it is
not a constant — 1,461 page-calls before the wall on 2026-08-15, 328 on
2026-08-28. So the harvest is a multi-week job that has to survive being stopped
and resumed dozens of times, which is what the checkpoints in `run_ledger` are
for.

Two cron entries drive it. Both wrappers live on the host in
`/home/minipc/bin/`, not in this repo — they encode where one box keeps its venv
and its logs, which is not the repo's business.

| Job | Cadence | Runs | Why that cadence |
|---|---|---|---|
| `harvest-articles.sh` | every 6h | `guardian`, `nyt`, `wayback --no-scan` | quota-bound; four windows a day chip away at whatever the allowance turns out to be |
| `harvest-macro.sh` | weekly | `weo`, `monthly` | WEO is semiannual and WB panels update a few times a year; more often is waste |

Both take a `flock -n` lock and run under a `timeout` shorter than their own
interval, so a run can never outlive its window and lock out the next tick.
**Neither spends money**: no scoring call, and `wayback` runs `--no-scan`, so the
billable leakage scan is never reached.

The live pipeline — prices, econ calendar, the weekly ETL's scoring — is
deliberately **not** in either job. The harvest converges and the ETL recurs, so
sharing a process means an unrelated price-fetch failure aborts a harvest run;
and nothing consumes the live output while the dashboard is unlaunched.

Because the harvest is finite, both harvesters say so when they are done —
`nothing to harvest — roster complete through <date>` — and the Guardian run
reports what is still outstanding when it stops. Without that a converged
harvest and a stuck one produce the same silence in a log nobody is watching.

## 4. What we anonymize

Two layers, one gate, and a meter. In the order they run.

| | What it is | What it catches | What it cannot |
|---|---|---|---|
| **1** | `gazetteer` — a hand-written list, deterministic and offline | names, demonyms, currencies, capitals, central banks, statistics offices, regions — and identification by elimination | anything nobody wrote down |
| **2** | `rewrite` — two model passes, one over digests + headlines, one over the bodies read end to end | this year's finance minister, this year's party, a named law, a named crisis | anything the model misses |
| **gate** | `assert_clean` — scans the whole outbound payload, keys as well as values, against the whole roster | a leak either layer missed | nothing. It raises. |
| **meter** | `probe` — asks a cheap model to name the country, and records the answer | how identifiable the bundle actually is | it never acts on what it finds |

### Layer 1 — the gazetteer (`backend/llm/gazetteer.py`)

No model, no network. Every surface form that identifies a roster country, mapped
to the **functional role it plays**:

| Category | Becomes |
|---|---|
| names | "the country" |
| demonyms | "the country's" |
| people | "the country's citizens" |
| currency | "the local currency" |
| capital | "the capital" |
| cities | "a major city" |
| central bank | "the central bank" |
| statistics office | "the national statistics office" |
| neighbours | "a neighbouring country" |
| regions | "the region" |
| every *other* roster country | "another country" |

Three properties the replacements are chosen for:

- **Numbers survive.** "inflation hit 85.5%" stays "inflation hit 85.5%". A
  masked run that also lost the magnitudes would be measuring something else
  entirely.
- **Roles survive.** "the Central Bank of the Republic of Türkiye" becomes "the
  central bank", not a hole. The scorer still needs to know a central bank did
  the thing.
- **Region stays coarse.** A country becomes "the country"; its neighbours become
  "a neighbouring country". Turning "North Korea" into "the country" would be
  both wrong and a giveaway.

**Two passes, and the order is load-bearing.** `mask()` first, so the scored
country's central bank survives as "the central bank"; then `mask_foreign()`, so
every other roster country collapses to "another country". Running the flat pass
first would eat the specific one.

**Two tiers, deliberately uneven.** Five pilot countries are hand-curated down to
their statistics offices and wine regions. The other forty-three get a thin entry
— name, demonym, capital, currency — assembled from the roster and from `babel`,
which already knows every territory's name and currency. The gap between the
tiers is one of the things the probe measures.

**Longest form first, across all categories.** The collisions that matter are
cross-category: "Bank of Korea" contains "Korea", and so does "North Korea". A
shorter form matching first leaves a fragment behind, and a fragment is a leak.

**Payload as well as prose.** `rewrite.mask_payload` walks the nested evidence
payload and masks **dict keys as well as values** — the payload is serialized to
JSON before it reaches the model, so a label is as visible as a number, and the
evidence really does carry `"Exchange rate vs USD"` as a key. `mask_item` masks
every article field *except* an explicit short list (ids, links, dates, scores).
The polarity matters more than the contents: it began as an allow-list of the
three fields somebody remembered, and two other text fields went to the model
unmasked while the list looked complete.

A payload string that **is exactly** a roster ISO2 code (`"PT"`) is masked too.
ISO2 codes are never masked inside prose — "IT" is information technology, "NO"
is no, "IN" and "AT" are ordinary words — but a field whose whole value is the
country code is the loudest possible leak.

#### What is deliberately *not* masked

- **"real" and "won"** — ordinary English far more often than they are money.
  Masking them turns "real GDP" into "the local currency GDP" and "the party won
  the election" into gibberish. The unambiguous forms *are* masked: "Brazilian
  real", "reais", "BRL", "R$", "Korean won", "KRW".
- **Ambiguous ISO codes** — `PEN` is a pen, `COP` is a police officer, `PHP` is a
  programming language, `SAR` is search-and-rescue, `CAD` is computer-aided
  design.
- **Bare "Amazon"** — a company far more often than a rainforest in this corpus.
  The qualified forms are masked.
- **Region names that are ordinary phrases** — "the South", "the West", "the
  North".

One rule behind all four: **a corpus that reads as damaged tells the scorer that
something was removed, which is a worse leak than the word it was hiding.**

### Layer 2 — what a list cannot know (`backend/llm/rewrite.py`)

Ten years of news is ten years of politicians, parties, companies, laws and
stadiums, and no hand-written list survives that. So a model is asked to replace
what remains with the role it plays, under one non-negotiable instruction: keep
every number exactly as written.

Both passes share one rule block, so they cannot drift apart:

1. Keep every number exactly as written. Numbers are evidence.
2. Every proper noun becomes the functional role it plays — a named person
   becomes their office, a named party becomes "the governing party", a named
   company becomes "a large domestic bank".
3. This applies to *every* country, not only the one being described.
4. Keep the region coarse — never a continent, ocean, bloc, currency, language,
   nationality or demonym.
5. Named *things* count as proper nouns and are the easiest to miss: laws and
   treaties, events and referendums, buildings and rooms, sports teams, scandals
   and government programmes.
6. Change nothing else.

Rules 3 and 5 exist because a probe measured them. With people and countries
gone, six of six bundles were still identified at 0.80–0.90, and the evidence the
probe quoted was "the Help America Vote Act", "the White House Situation Room",
"as bad as Brexit", "the Iranian people" and "Europe". A statute, a building, an
event, a non-roster demonym and a continent — none of them reachable from a list
of roster countries, and extending the list would mean enumerating every proper
noun on earth. This is the layer that can generalise, so this is where the scope
belongs.

**Two passes, with deliberately asymmetric failure modes:**

| | Runs on | On failure |
|---|---|---|
| `sweep_digest` | every article's digest, plus its headline | **fails open** — keeps the unswept digest. A digest is not sent whole, and dropping it silently would cost the article |
| `rewrite_body` | the two or three bodies the scorer reads end to end | **fails closed** — returns `""`, and the article degrades to its masked title |

Failing closed is the right trade only while it is rare. It stopped being rare
once: `rewrite_body` shares a chat builder with the digest and inherited a
1,024-token ceiling sized for a five-field summary, while its own prompt says
"changing nothing else, do not summarise". 71% of harvested bodies could not fit,
and every one degraded quietly, on the articles the scorer reads whole. The
budget is now sized from the text being rewritten.

Both passes are cached on a hash of the **masked** text, which is what makes a
backfill reproducible: `input_manifest` hashes the bytes the model read, and for
these articles those bytes are generated prose kept nowhere else.

### The gate — `assert_clean`

The gazetteer is a list somebody wrote and the sweep is a model, so neither is
trusted. Before anything leaves for the API, the whole outbound payload is walked
— nested dicts, lists, dict *keys* as well as values — and every string is
scanned against the **whole roster**.

The whole roster, not just the scored country, because an article naming a
*different* roster country lets the probe rule countries out by elimination. That
is the same leak wearing a hat. It is also why the gazetteer masks the whole
roster: a gate that fires on every real snapshot is a gate somebody turns off.

A hit raises `MaskLeak`, and it is **fatal on purpose**. A masked snapshot that
names its country is not a degraded result, it is a wrong one, and in a ten-year
series it would sit there looking exactly like a right one forever after.

One deliberate narrowing: the gate scans the four serialized evidence strings,
not the whole prompt. The prompt template's own worked examples name real
countries, and those are instructions, not evidence.

### The meter — `probe` (never a gate)

`assert_clean` proves the *list* found nothing. It cannot prove the bundle is
unidentifiable — a wine region identifies a country as precisely as its central
bank does. A live masked run passed the integrity scan with zero flagged tokens
and the probe still named Portugal at 0.9 confidence, citing the Douro Valley and
the Algarve. Both are now in the gazetteer *because* the probe found them.

So the probe asks a cheap model, straight out: which country is this? It reads
the bundle **as the scorer saw it** — built from `prompt_entries`, so title plus
digest, and full text only for the articles the scorer read whole. An earlier
version read raw bodies and would have reported the instrument leakier than it
is, forever.

The answer is **recorded and never enforced**. It could not be: the United States
is expected to be identified nearly every time from coverage volume alone, and
refusing to score the US would be answering the wrong question. In production it
runs on one country in six, seeded deterministically on `(country, date)` with
`crc32` — not Python's `hash()`, which is salted per process and would have
re-sampled on every restart while the comment claimed otherwise.

It runs in production rather than only in the pilot because identifiability is
not a property of the method, it is a property of *this week's evidence*. A quiet
week masks well; a week whose only story is a named central bank governor does
not.

**The control arm is what makes the number mean anything.** A probe forced to
name a country will name the one its prior favours, and on a roster containing
the United States that is the United States — so "US identified at 0.85" and "the
model always says US" produce identical output. `probe.null_bundle()` is a bundle
with no country in it at all, hand-written, with plausible numbers kept.

The probe fails open: a failed call records `ZZ` at 0.0 confidence with
`insufficient_information`, because a failed measurement must not read as a
successful identification. `classify()` distinguishes four outcomes —
`identified`, `wrong`, `uncertain`, `no_guess` — since a bundle placed
*confidently in the wrong country* is neither a hit nor a clean miss.

### What replaces identity

Masking removes legitimate priors along with the illegitimate recall. A debt
burden means one thing for a country that borrows in a currency it can issue and
something entirely different for one that cannot, and that is a fact a rater
should have.

So those priors are **stated instead of implied**, in
`backend/data/curated/structural_facts.yaml`: region, income group, commodity
dependence, monetary sovereignty, reserve-currency status. Every value carries
its source and retrieval date; uncited values are dropped. The prompt tells the
model to read the block directly and, when it is absent, to treat the structure
as unknown rather than substitute a guess.

The file acknowledges its own cost: "Europe, high income, currency-union member"
narrows forty-eight candidates to about twenty. The probe is what measures that
cost. Three time-varying fields were deliberately moved out into
`indicator_series` — a single current value stamped on a 2016 snapshot is a
future leak.

Only 5 of 48 countries have a structural block today, and that asymmetry is
countable in the data rather than only in a comment:
`input_manifest.masking.structural_fields`.

### What is *not* anonymized

- **Stored article bodies are raw and unmasked, always.** Masking is a transform
  at the scoring boundary, never at harvest — a masked body would make the mask
  map unversionable and the store useless the day the gazetteer improves.
  `content_sha256` therefore hashes unmasked text.
- **The database and the frontend show real headlines.** `mask_item` is
  non-mutating; the masked copy exists only on the way to the model.
- **The sanctions lookup keeps the real ISO2 code** — it runs before masking, so
  the legal question is answered about the actual country while the prompt never
  learns which one it is.
- Article URLs, ids, dates, relevance scores and severities are never masked, and
  the URLs are never sent — `prompt_entries` carries ids. A slug like
  `.../2018/aug/13/turkey-lira-crisis` would hand over the answer, and masking it
  would produce nonsense.

## 5. What we exclude

### The no-future line

`backend/news_fetching/snapshot_select.py` is the seam — the only difference
between a historical run and a live one, and therefore the only place hindsight
can enter. Three rules, in order of how badly each one bites:

1. **The window is strict at the top.** `[as_of − 30d, as_of)`. An article
   published *on* the anchor is same-day news, which the live run's own
   `now() − 30d` cutoff would not reliably have had either.
2. **A body may not be younger than the anchor.** An article published in June
   and captured by the Wayback Machine in August is a June article with an
   *August body* — publishers edit, append and re-headline. When the capture is
   younger than the anchor the body is dropped and the article stays as title and
   abstract. An unparseable vintage is treated as unknown age and refused; an
   unknown age is not a licence.
3. **A live refetch enters only if the leakage scan cleared it.** A page fetched
   today is younger than any historical anchor by construction. Flagged bodies
   were already discarded at recovery time and arrive here with no body at all.

**Nothing here drops an article.** Everything it refuses still reaches the scorer
as title and abstract — thinner evidence, honestly thin, rather than richer
evidence that is a lie.

### The no-future line for macro

`payload._resolve` drops any observation whose `as_of` is after the anchor, or
whose period ends after it. Only historical runs pass `vintage_as_of`; the daily
run passes `None`, because handing it today's date would drop the current year's
annuals, whose period ends in December.

A **real vintage outranks a synthesized one**. The annual panel stamps 31
December of its own year, which would otherwise beat the WEO edition that
actually existed in the January-to-March window.

WEO editions drop every column past the file's own per-row "Estimates Start
After" marker. Projections are **dropped rather than marked**, because nothing
downstream can carry a marker today, and a forecast the payload cannot label is a
forecast the model reads as an observation.

`restamp` only touches rows stamped `as-published-latest`; a row carrying a
publisher's own edition date keeps it. Rows whose new date would precede period
end, or exceed a two-year lag, are reported and skipped rather than guessed at.

### Selection exclusions

- **The relevance threshold orders the pool, it does not cap it.** Articles over
  0.3 come first, then the rest by rank until the 20-article budget is full. Read
  as a cap it produced three-article weeks next to twenty-article ones over a
  one-article difference in how many cleared the bar.
- **The abstract tier is rationed** to `ABSTRACT_TIER_SHARE = 0.4` of the budget —
  eight of twenty. The NYT archive returns no bodies and is overwhelmingly about
  the United States: in a measured month the roster matched 1,824 NYT articles
  and 1,687 of them were US. Left uncapped, a US snapshot fills with headlines
  while a Portugal one keeps full bodies, and the two stop being the same
  instrument pointed at different countries. The cap is applied *before* the
  theme floor so the floor rations what survives, and the cost is accepted: an
  abstract that was its theme's only article can be cut, and that theme forfeits
  its quota.
- **The relevance snippet is capped at 300 characters** and deliberately does not
  excerpt from wherever the body first names the country. That was tried. It
  lifts every article that mentions the country in passing to the ceiling, and
  the resulting "Portugal" snapshot was twenty articles about the Dutch
  government, UK farmers, Venezuela and José Mourinho. A measured PT window has
  "Portugal" in 0 titles, 6 ledes and 59 bodies — the thinness is real, and the
  honest response is a thin week plus a loud report, not a heuristic tuned until
  the number looks like the live one.
- **NYT volume cap** of 150 per country-month, cut by relevance score and logged.
  A cap nobody reports reads afterwards as "we harvested everything".
- **NYT desk denylist** — Sports, Culture, Style, Dining, Travel, Arts, Games and
  the rest, about a quarter of everything that matched a roster country. A
  denylist rather than an allowlist, because a quarter of archive rows carry no
  desk at all.
- **Publisher denylist** for the live path, in `blocked_sources.txt`.

### Roster exclusions

`PILOT_ROSTER` is **US, TR, PT, KR** — four countries chosen for how differently
they behave rather than for size. The US is mandated and is the hardest case for
window subdivision; TR is a crisis EM; PT a quiet DM; KR calm-with-one-shock. If
the machine produces a defensible series for all four it is not overfitted to
loud countries.

**BR is harvested and not scored.** It was the fifth and came out when the
projection needed to lose one — Turkey already carries the crisis-EM case, and
Brazil was the most differentiated country's most redundant twin. Its corpus
still exists; no code path treats a country present in `article` but absent from
`PILOT_ROSTER` as an error. Harvesting spends somebody else's rate limit and
scoring spends money, so the two are worth keeping separable.

`config.country_name()` raises for a code not in the live roster, so a typo in
`PILOT_ROSTER` fails before a harvest starts rather than producing an empty query.

### An empty week is a legitimate answer

`select()` returning nothing is a real answer for a thin week and must stay one —
inventing articles to fill a quota is the failure this whole machine exists to
avoid. It is recorded `complete` with zero spend so a resume does not retry it
forever, and it shows up in the report.

## 6. Three arms, one of which is production

| Mode | Writes to | What it is for |
|---|---|---|
| `masked` | `risk_snapshot` | the continuous weekly series — this is production |
| `named` | `run_ledger` + `snapshot_diagnostic` | the diagnostic twin: what identity was worth |
| `masked_nostructural` | `run_ledger` + `snapshot_diagnostic` | masked, with the `structural` block withheld |

The two diagnostic arms share `(country, as_of)` with their masked twin and would
overwrite the production series on its own primary key, so they never touch
`risk_snapshot`. This is enforced in `score_one` (`upsert=False`), by the
schema's own CHECK constraint, and by an invariant test.

The third arm exists because masked-vs-named divergence is ambiguous on its own.
A small gap could mean the structural facts recovered what the name was carrying,
or that the name never carried anything. Only withholding the block separates
those — and because the block sits nowhere near the digests, the two masked arms
share their cached digests and the third arm costs about a dollar.

> One inconsistency worth knowing: `config.SCORING_MODES` lists three modes, but
> `risk_snapshot.scoring_mode`'s CHECK admits only `'masked'` and `'named'`. That
> is why the bake-off below writes to a file rather than to a third variant.

## 7. The version freeze

A ten-year series assembled across a masking change is two series wearing one
name, and the damage is invisible: every row still scores and every row still
carries its own manifest. The manifests were already being written — what was
missing was anything that *reads* them. A stamp nobody checks is a comment.

`score.freeze()` pins the version set on the first run and guards every resume
after it. Nine fields:

```
SWEEP_VERSION  REWRITE_VERSION  GAZETTEER_VERSION  MASK_MAP_VERSION
PROMPT_VERSION  PAYLOAD_VERSION  SCORING_MODEL  DIGEST_MODEL  SEED
```

A moved field raises `VersionDrift` **before anything is scored and before
anything is spent**. `--override-version-drift` proceeds and *re-pins*, so the
move is recorded rather than merely tolerated.

`git_sha` is recorded and deliberately **never compared**. The pilot runs for days
and is committed to while it runs; a docs commit would refuse the resume, the
override flag would become reflex, and a guard that is always overridden catches
nothing. The SHA move is logged as a warning instead.

The last three fields were the largest hole. Everything above them versioned the
*evidence*; nothing versioned the *instrument*. `MODEL_NAME` was a module literal,
so swapping the scorer mid-pilot resumed over the old rows without a warning — the
one change this module exists to refuse, walking straight through it.
`score.versions()` now reads the **effective** model ids, environment overrides
included, because an override is exactly the case this has to catch.

The masking instrument is pinned regardless of the environment. The body rewrite,
the digest sweep, the identifiability probe and the Wayback leakage scan all stay
on the pinned digest model whatever `DIGEST_MODEL` says. Moving them is a change
to what the pilot is measuring, not a change to what it costs.

### Five stamps on every masked row

Each was earned by a specific failure:

| Stamp | What it versions | Why it exists separately |
|---|---|---|
| `mask_map_version` | the gazetteer's **data**, hand-maintained | its limit is that a human has to remember to bump it |
| `gazetteer_version` | sha256 of `gazetteer.py` | a euro fix changed masking *code* and moved neither the data nor its version. A hash cannot forget |
| `sweep_version` | sha256 of the digest-sweep prompt **and the model** | the digest cache keys on masked text and the sweep runs after digesting, so the sweep changed twice while the key sat still |
| `rewrite_version` | sha256 of the body-rewrite prompt **and the model** | separate from the sweep because a body rewrite cannot change a digest; folding them together threw away every cached digest whenever the body prompt moved |
| `identifiability` | the probe's own answer | the meter, stored beside the thing it measures |

The model is in the last two hashes. Before that the key recorded which
*instructions* produced a row and never which *model* obeyed them — point stage 1
somewhere else and every previously rewritten body comes back as a cache hit,
produced by the old model, under the same version label.

## 8. Money, and resuming

- **Spend is metered from the API's own usage fields**, not projected.
  `llm/usage.py` hooks LangChain's callback mechanism from underneath, so every
  call inside the block is metered — including calls inside `.batch()` and inside
  structured-output wrappers — without threading a usage argument through the
  daily run's code to serve a backfill.
- **The governor's memory lives in the ledger, not the runner.** Stopping and
  resuming a multi-hour pilot cannot reset the budget to zero and quietly spend it
  twice.
- **`PILOT_BUDGET_USD = 130` is a runaway guard, not a budget.** Authorization is
  the gate; this only decides how far a run that has gone wrong gets before it
  stops.
- **Resume skips only `complete` rows.** A country that died half way through is
  retried, never silently skipped.
- **A projection with nothing to project from is a refusal.** `score.projection`
  raises `NoObservedCost` rather than returning a constant. It used to return
  `n × 0.036`, measured before a selector fix moved the median snapshot from 6.5
  articles to twenty — low by about a third, and returned as a float
  indistinguishable from a measurement, straight into the line that asks somebody
  to approve a spend.
- **A body always beats a stub.** The article upsert expresses that in its
  `ON CONFLICT` rather than in each adapter, so a re-run can never lower a status
  and buy a second billable leakage scan.

Every command that spends money prints its projection and waits for a yes.
`--approved` gives the same consent up front, for a run with no terminal to ask
on; it is deliberately not a default.

## 9. What gets measured

`python -m backend.util.pilot.run pilot-report` renders the meters, and the
ordering is not arbitrary — each is needed to read the one before it.

1. **Divergence** — `masked − named` on the dates both arms scored, split either
   side of the model's knowledge cutoff. Reported **signed and absolute**. Signed
   because direction is the finding: masking scoring a country *riskier* than its
   name means the name was carrying reassurance, *safer* means it was carrying
   alarm, and those are opposite defects with opposite fixes. Absolute because a
   country whose weeks diverge in both directions averages to a clean-looking zero.
   Decomposed against the third arm, so "the structural facts recovered it" and
   "the name never mattered" stop looking the same.
2. **Identifiability** — the probe's hit rate, the four outcomes, and the number
   that actually matters: the **spread** between the ceiling and the floor. The US
   is expected near the ceiling; a high *floor* is the failure.
3. **Evidence texture** — source mix and the abstract share per country-year. A
   divergence that tracks the abstract share is a statement about thin evidence,
   not about masking.
4. **Spend**, against the governor.
5. **Ranked structural candidates** — where identity was carrying a fact the
   payload does not state. The step that turns the report into work: the fix is a
   new structural field, not a retreat to named scoring.
6. **Lint findings**, by rule and country. Advisory; nothing moved a score.
7. **Stage-1 degradation** — snapshots scored partly on truncated bodies rather
   than digests. Read this *before* reading divergence.
8. **Harvest pacing** — minutes and calls per source-country, extrapolated to 48.

Throughout, an empty sample renders as an em dash and never as `0.0`: an
unmeasured pair and a perfectly agreeing one are different facts.

`pilot-report --export` writes `GATE2_BASELINE.json` and `.md` to the repo root,
so the next run is a regression check rather than an opinion. (Neither file
exists yet — the exporter has not been run.) The markdown embeds the terminal
render verbatim rather than re-formatting it: two renderers over one dataset is
two things to keep in agreement, and the one that drifts is always the one nobody
runs.

**Gate 2** is the fixed dry run the whole project points at: **PT, 2019, weekly
Mondays, 52 anchors.** Small enough to cost a few dollars, long enough that a rank
correlation means something.

## 10. The bake-off: which scorer

`backend/util/tools/bakeoff.py`. A cheaper model is not a cheaper instrument. The
pilot's whole claim is that every row in a ten-year series was produced by one
scorer under one prompt, so changing the scorer is an instrument change, and the
only honest way to make one is to re-run a fixed set of anchors through both and
look at what moved.

**Two hard gates run before any real spend**, and both report failure rather than
routing around it:

1. **Strict schema** — the *real* prompt and the *real* schema over canned masked
   evidence. A candidate that satisfies a three-field toy schema says nothing
   about one that has to satisfy ten required fields with
   `additionalProperties: false` at every level.
2. **Determinism** — three repeats at temperature 0 with a fixed seed, reporting
   exact-match rate and score spread. A candidate that ignores `seed` costs the
   byte-for-byte rebuild.

Then the report, ordered the way the decision is actually made and not the way it
is asked about:

```
0. the reference
1. gates              a failure here ends it; the numbers below are context
2. rank correlation   the meter: reordering cannot be recalibrated away
3. band migration     diagonal is offset, scatter is disagreement
4. observation-only flags
5. lint tripwires
6. cost
```

**Rank correlation is the meter.** A constant level offset is survivable — the
calibration anchors in the prompt can be moved and the whole series shifts with
them. Reordering is not: it means the candidate disagrees about which weeks were
risky, and no amount of recalibration fixes that. Spearman and Kendall tau-b are
both reported, because they fail differently — Spearman is moved hard by a single
anchor swapping ends of the range and Kendall is not.

A cost table read before the gates is how a cheap model that fails both of them
gets adopted.

The candidate is scored by production code (`_process_country` with
`upsert=False`, the same switch the diagnostic arms use) and lands in a file, so it
cannot overwrite the baseline it is being compared against. The digest cache is
shared on purpose: it is keyed on the digest model, so every *scoring* candidate
reads the identical digests the incumbent read, which isolates the scorer as the
only variable. The incumbent is read out of `risk_snapshot` rather than
re-scored — re-scoring it would make it a fourth candidate rather than the
reference.

## 11. The commands

```bash
# harvest — days of somebody else's rate limit, no money
python -m backend.util.pilot.run guardian --country TR
python -m backend.util.pilot.run nyt
python -m backend.util.pilot.run gdelt --anyway     # dormant; read why first
python -m backend.util.pilot.run wayback            # asks before the billable scan

# macro vintages
python -m backend.util.pilot.run weo
python -m backend.util.pilot.run monthly
python -m backend.util.pilot.run restamp --dry-run

# what the harvest produced
python -m backend.util.pilot.run report

# scoring — money; prints a projection and waits for a yes
python -m backend.util.pilot.run score --country TR --since 2018-01-01 --until 2018-12-31
python -m backend.util.pilot.run diagnostic
python -m backend.util.pilot.run pilot-report --export

# which scorer
python -m backend.util.tools.bakeoff smoke minimax-m3
python -m backend.util.tools.bakeoff capture-baseline
python -m backend.util.tools.bakeoff score minimax-m3
python -m backend.util.tools.bakeoff compare
```

`backend/notebooks/historical_rating_walkthrough.ipynb` walks one anchor through
every layer above and writes nothing — no snapshot, no probe result, no ledger
row. `country_rating_walkthrough.ipynb` walks the other half.

---

## The source list is closed, and why

A third body source was evaluated in August 2026 and rejected:
`docs/news-source-evaluation.md` has the measurement. The short version is
that a paid index of ~150,000 general-news publishers returned twice the
Guardian's volume for a country-year and a worse fit to the theme ledgers —
`information` and `edge` under their per-snapshot floors on 46% and 56% of
anchors — because a broad concept query for a country returns its football.

The evaluation's more useful finding was about this repo rather than about
the vendor. The one country that looked to need a paid source had **zero**
Guardian rows behind eleven `failed` checkpoints nobody had read; re-running
the free harvest fixed it in 65 calls. Six of six countries whose Guardian
harvest completed pass every floor.

So the four sources in the table above are the four, and the standing gap is
recorded rather than filled: **NYT is abstract-only and GDELT is dormant, so
the Guardian carries the body burden alone.** Any future candidate has to be
retrievable per theme or its volume is worth nothing.
