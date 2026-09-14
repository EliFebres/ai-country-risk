# The historical corpus was retrieved correctly and selected wrong

A spot-check found 90%+ of the stored historical articles irrelevant to country risk — sport
and human interest, almost nothing on war, economics, trade or politics — against a live weekly
feed that looks completely different. The hypothesis under investigation was that the historical
harvest queries for the *country* while the live path queries for *risk topics*, so the two
halves have never been the same instrument.

**That hypothesis is wrong, and the truth is worse.** Both halves ask the same six questions.
The instrument that differs is the one downstream of retrieval: the relevance scorer, fed a
fifth of the text on the historical side, ranked football above policy and the sub-threshold
top-up then padded every snapshot with it.

---

## 1. The queries, quoted

### Both halves issue the same six

`backend/news_fetching/core.py:59-72` is the single definition, and the live path and two of the
three historical adapters read it:

```python
THEME_QUERIES: dict[str, str] = {
    "friction":    '"{c}" (tax OR taxation OR customs OR permit OR licence OR '
                   'bureaucracy OR corruption OR court ruling OR regulation)',
    "order":       '"{c}" (government OR president OR prime minister OR parliament OR '
                   'election OR cabinet OR coup OR protest OR central bank OR '
                   'interest rate OR inflation OR currency OR default OR IMF)',
    "security":    '"{c}" (military OR defense OR conflict OR war OR attack OR '
                   'sanctions OR security OR terrorism OR unrest)',
    "information": '"{c}" (press freedom OR journalist OR censorship OR '
                   'statistics office OR audit OR judiciary OR court independence)',
    "edge":        '"{c}" (startup OR entrepreneur OR business registration OR '
                   'university OR research OR emigration OR skilled workers leaving)',
    "broad":       '"{c}"',
}
```

**Live** — `article_enrichment.py:86-91` into `fetch_links._gnews_url`, giving the literal wire call
`https://news.google.com/rss/search?q=<template>&hl=en-US&gl=US&ceid=US%3Aen`. No date operator;
the 30-day window is a client-side post-filter at `fetch_links.py:173`.

**Historical, Guardian** — `adapters/guardian.py:118-128` translates the same template into
Guardian's syntax, and a test (`TestNoAdapterForksTheCore::test_no_adapter_carries_its_own_theme_queries`)
forbids the adapter from carrying its own:

```python
query = core.THEME_QUERIES[theme].format(c=country_name)
return query.replace('" (', '" AND (', 1)      # '"Portugal" AND (tax OR customs OR ...)'
```

and the request, `guardian.py:263-272` as it stood:

```python
resp = _get({
    "q": query,
    "from-date": start.isoformat(),
    "to-date": end.isoformat(),
    "show-fields": "bodyText",
    "page-size": config.GUARDIAN_PAGE_SIZE,     # 100
    "order-by": "newest",                       # date, not relevance, deliberately
    "page": page,
    "api-key": _api_key(),
})
```

**Historical, NYT** — there is no query. The Archive API takes a year and a month in the path and
returns the whole paper; the only parameter is the key (`nyt.py:122-124`). Filtering is
client-side: a news-desk denylist (`_SKIP_DESKS`, 28 desks including `Sports`, `nyt.py:67-72,208`)
then a gazetteer match (`nyt.py:215`).

### So two of the four proposed fixes were already in the branch

- *NYT section / news-desk filtering* — already there, `_SKIP_DESKS`, measured at 25% of matching
  rows in 2018-08.
- *Risk-topic queries reusing the live builder* — already there, and test-enforced.

One was genuinely missing (**Guardian has no `section` parameter**) and one, *stop the
sub-threshold top-up*, would have made things worse in the order proposed. See §5.

---

## 2. The mechanism

`article_ranking.score_relevance` reads exactly two fields (`article_ranking.py:93-95`):

```python
title   = (article.get("title") or "").lower()
summary = (article.get("summary") or article.get("snippet") or "").lower()
text    = f"{title} {summary}"
```

The two paths handed it different things:

| | live | historical |
|---|---|---|
| the field that wins the `or` | `summary` = `_clip_words(body, 240)` — **240 words**, `fetch_links.py:231-233` | `snippet` = `abstract or body[:300]` — **300 characters**, `snapshot_select.py:53,82` |
| upstream ranking | Google News relevance, before any of this repo runs | none — `read_window` returns `ORDER BY published_at DESC` |

Historical items carry no `summary` key at all (`core._ITEM_KEYS`), so the `or` falls through.
**The scorer read a ~5× narrower window on the historical side.** Three failures stack on that
one cut, and all three are artefacts of the window rather than statements about the article:

1. **The 0.1 cliff.** `if country_lower not in text: return 0.1`. Measured on a real PT window
   (63 Guardian articles): "Portugal" appears in **0 titles, 6 ledes, 59 bodies**. At 300
   characters most of the corpus takes the floor for a reason unrelated to its content.
2. **Starved keyword counts.** `+min(high_count * 0.15, 0.5)` needs ~4 HIGH keywords to saturate.
   300 characters of lede rarely holds two.
3. **`_BODY_MENTION_CAP` caps the wrong half.** `if country_lower not in title: score = min(score, 0.55)`.
   A British paper does not put "Portugal" in the headline of a eurozone story — capped. A match
   report *is* headlined "Portugal 3-1 Switzerland" — **the cap never applies.** Sport is the one
   genre that reliably names the country in the headline.

The result is an inverted ranking. Same country, same source, same window:

| item | old window | new window |
|---|---|---|
| Guardian policy story (country named at char 504) | **0.100** | **0.550** |
| Guardian match report | **0.450** | 0.450 |

**And 0.3 was never a relevance bar.** `score = 0.3  # Base score for mentioning country`
(`article_ranking.py:102`) is identical to `_RELEVANCE_THRESHOLD = 0.3`. "Clears the bar" meant
precisely "the country name appears in title + 300 characters".

Then `apply_threshold` filled the remaining fourteen of twenty slots from a pool where everything
was tied at 0.1, so the real tiebreak was `read_window`'s `published_at DESC, url ASC` — recency.

### How far back

`_SNIPPET_CHARS = 300` entered in `50c0600` on **2026-08-02** and was never changed.
`score_relevance` dates to `1f2f8ea`, **2025-11-05**, written for Google News items. The scorer
was calibrated on one input shape and, nine months later, handed a different one. Every
historical snapshot ever assembled went through it.

### Is the live path affected?

**The defect is shared; the inversion does not currently fire live, and not because the code
prevents it.** `score_relevance` is one function called by both paths, so the 0.3-base/0.3-bar
coincidence and `_BODY_MENTION_CAP`'s genre bias are fully live-reachable. Two things mask them:
the 240-word window means failures 1 and 2 rarely bite, and **Google News' own relevance ranking
pre-filters the pool before anything here runs.**

Live is protected by an upstream vendor, not by the scorer. The measured live week below still
carries three of twenty that are noise — a travel diary, a "New 7 Wonders" listicle, and a World
Cup hosting story that scored 0.550 under `security` because "tug-of-war" contains "war". That is
a much better ratio than 8 of 20, and it is the same failure mode, unfixed. **Any country whose
coverage names it in the body but not the headline would degrade the same way.** This is a latent
production defect; it is reported, not fixed, here.

---

## 3. The two halves, side by side

**Live path, Portugal, week of 2026-09-14** — `fetch_relevant_news("Portugal", 20)`, actually run.
Publisher shows as `news.google.com` because the unwrapping stage runs later; the publisher is in
the title suffix.

```
  #   rel  theme        title
  1 0.980  security     Mozambique seeks access to Portugal's military archives on shared history
  2 0.980  friction     Constitutional Court rules 'Return Law' for immigrants is not unconstitutional
  3 0.910  friction     Portugal Faces Constitutional Court Ruling on Coelho Budget - Forex Factory
  4 0.800  broad        Diesel in Portugal hits record high, Spain introduces new discount
  5 0.760  broad        Portugal decriminalized drugs 25 years ago, but the real lesson is what came after
  6 0.700  security     War and drought put Portugal in tight spot - Portugal Resident
  7 0.680  security     Renewables investment could strengthen energy security - The Portugal News
  8 0.680  security     Cloud Security - Portugal - Statista
  9 0.630  security     Portugal's FREMM EVO Frigates Cost EUR1.3B Each, Nearly Double Italy's
 10 0.600  broad        Portugal, Greece or Switzerland? Americans on the pros and cons of moving
 11 0.600  security     To a Delegation of the Military Ordinariate for Portugal - The Holy See
 12 0.550  security     The US military is the single largest institutional consumer of petroleum
 13 0.550  security     Who will host FIFA World Cup 2030 final? Tug-of-war escalates
 14 0.530  broad        Portugal wants EU cash to pay for firefighting goats - politico.eu
 15 0.450  broad        We spent 2 weeks in Portugal. It was great, but our trip would've been better
 16 0.400  security     Portugal Energy Infrastructure Gains Strategic Weight as State Takes Stake
 17 0.400  friction     Portugal to pay FC Porto EUR15,300 after European Court ruling - OneFootball
 18 0.380  security     Joint Statement of the Foreign Ministers of Canada, Denmark, Finland...
 19 0.300  broad        Portugal says Israel undermining Palestinian state through settlements
 20 0.300  broad        Here are Portugal's New 7 Wonders - Portugal Resident

clearing 0.3: 20 of 20
```

**Historical, same country, stored week 2019-01-07** — the same code path with `as_of` pinned:

```
  #   rel  section        src       title
  1 0.300  books          nyt       Michelle Obama's Book Is No. 1 Here - and No. 1 in Finland, Singapore
  2 0.300  food           guardian  Christmas spirit: sweet and fortified wines for festive afters
  3 0.100  lifeandstyle   guardian  Seven stages of January | Eva Wiseman
  4 0.100  world          guardian  Is the tide at last on the turn for the world's 'strongman' leaders?
  5 0.100* travel         guardian  Healthy holidays and a sunscreen rethink: top five travel trends for 2019
  6 0.100  books          nyt       Back to the Bayou: James Lee Burke's Latest Novel
  7 0.100  books          guardian  Mr Five Per Cent by Jonathan Conlin review - the world's richest man
  8 0.100  global         guardian  From showbiz to spaceships, what we can expect in 2019
  9 0.100* sport          guardian  The alternative sport review of 2018, from Kante's curry to Salah's statue
 10 0.100  books          guardian  Resistance by Julian Fuks review - battling with the past
 11 0.100  world          guardian  Shared goals: Blyth Spartans football team partners with Visit North Korea
 12 0.100* football       guardian  Flashpoints of 2018: Julen Lopetegui sacked by Spain on eve of World Cup
 13 0.100  business       guardian  World stocks volatile as Wall Street boom fades - as it happened
 14 0.100  business       guardian  Gatwick airport: majority stake sold to French group
 15 0.100  books          nyt       James Lee Burke: By the Book
 16 0.100  world          guardian  Japan shrinking as birthrate falls to lowest level in history
 17 0.100  food           guardian  The best supermarket wines for under GBP10
 18 0.100* football       guardian  Moussa Sissoko: 'I never wanted to quit Spurs. I knew I could succeed'
 19 0.100  business       nyt       Unrest in France Hinders Macron's Push to Revive Economy
 20 0.100* football       guardian  Lucas Digne rescues point for Everton after Watford's crazy five minutes

clearing 0.3: 2 of 20
```

Not one of those twenty is about Portugal. That is the reported 90%, and it is the whole
evidence base for one scored week.

---

## 4. The scale of it

### Corpus-wide, DEV (the corpus every stored result was computed on)

Share of stored articles clearing the 0.3 bar, under the narrow window and the live one:

| country-year-source | n | old | new | top sections |
|---|---|---|---|---|
| PT 2019 guardian | 642 | **11.5%** | 29.9% | football 241, business 25, commentisfree 25, food 19 |
| PT 2022 guardian | 863 | 13.1% | 28.9% | football 396, environment 55, business 41 |
| PT 2026 guardian | 785 | 10.3% | 24.6% | **football 424**, environment 28, business 25 |
| US 2019 guardian | 3344 | 10.1% | 32.6% | commentisfree 454, australia-news 195, football 178 |
| TR 2018 guardian | 1232 | 15.8% | 37.8% | commentisfree 150, football 118, business 53 |
| KR 2020 guardian | 1100 | 10.9% | 29.1% | commentisfree 120, australia-news 69, business 63 |
| BR 2019 guardian | 1067 | 24.1% | 38.0% | football 277, environment 101, commentisfree 68 |

**This is not PT-specific.** Every Guardian country-year in the corpus sits between 8% and 24%
under the narrow window. NYT rows are unaffected by the widening — they carry an `abstract` and
no body, so the `or` short-circuits, correctly — and sit much higher (23–79%) because an NYT
abstract names its subject.

### Corpus-wide, PROD, all 48 countries

The question the DEV table cannot answer: is this universal? Census over the whole live corpus,
**526,220 articles, 893 country-year-source buckets** (buckets of >=50 articles):

| source | buckets | articles | old median | old min | old max | new median |
|---|---|---|---|---|---|---|
| guardian | 316 | 394,227 | **16.4%** | 3.6% | 56.6% | **34.5%** |
| nyt | 376 | 126,827 | 44.6% | 0.0% | 99.2% | 44.6% |

Top sections in the stored corpus:

```
guardian (293,572 rows)  world:21.4% australia-news:15.6% sport:12.6% football:12.1%
                         commentisfree:9.7% us-news:6.0% business:5.7%
nyt      (108,501 rows)  world:43.4% us:12.6% opinion:11.8% business:11.3% briefing:3.1%
```

**Guardian corpus-wide, football + sport + travel is 73,374 of 293,572 rows — 25.0%.** PT is the
extreme case at 45%, not the only one. The widening more than doubles the Guardian median (16.4%
to 34.5%) and leaves NYT untouched, exactly as expected.

### A third defect, found by this census and not fixed here

The worst country-years in the entire corpus are not the small countries. They are:

| iso | `country_name` the scorer matches on | median share clearing |
|---|---|---|
| GB | United Kingdom | **6.6%** |
| CH | Switzerland | 9.8% |
| US | United States | **10.4%** |
| PT | Portugal | 11.1% |
| ... | | |
| IL | Israel | 35.4% |
| AU | Australia | 35.5% |
| NZ | New Zealand | 35.8% |
| CL | Chile | 45.6% |

`score_relevance` tests `if country_lower not in text` — an exact lowercase substring match on
`config.country_name(iso2)`. **The press does not call these countries by their formal names.**
The Guardian writes "Britain", "the UK", "America", "Washington"; it rarely writes "United
Kingdom" or "United States". So the two highest-volume countries in the corpus floor at 0.1 for a
naming mismatch, while Chile and New Zealand — which the press calls by name — clear at 35–46%.

This is live-affecting: `score_relevance` is called with the same `country_name` on both paths.

**The fix already exists in this repo and is not wired up.** `gazetteer.mentions(text, iso2)`
resolves every surface form — names, demonyms, cities, institutions — and is already used to find
NYT articles in a bulk archive and to prove a masked payload no longer names a country:

```python
>>> gazetteer.mentions("Britain raised taxes", "GB")     # True
>>> gazetteer.mentions("America raised taxes", "US")     # True
```

Not done here because it changes `score_relevance`'s signature from `country_name` to `iso2`,
touches both paths and every test that calls it, and would move live scores materially. It wants
its own change and its own measurement. It is the single highest-value item left in this area.

### Guardian vs NYT

They fail differently and both were already partly mitigated:

- **NYT** filters 28 desks at harvest and caps at 150 per country-month by the same scorer. Its
  stored rows are pre-selected, which is why its clearing share is 2–4× the Guardian's.
- **Guardian** filtered nothing. Its `broad` theme is a bare `"{c}"`, and for a small country a
  British paper's coverage is largely football: **45% of the stored PT 2019 corpus** (290 of 642).

### Is the pool thin, or is retrieval returning the wrong things?

**Neither.** From `docs/pipeline-audit.md:219-227`, re-running the real selector per anchor:

```
                candidate pool     clearing 0.3     SELECTED   topped up    mean selected relevance
TR 2018 (53)   88 / 125 / 243    11 /  32 /  69    18-20      10 of 53    0.304 - 0.750
US 2019 (52)  321 / 402 / 465    52 /  76 /  98    20 always   0 of 52    0.471 - 0.552
PT 2019 (52)   39 /  56 /  96     2 /   6 /  16    20 always  52 of 52    0.120 - 0.394
                min/med/max        min/med/max
```

The median PT window holds **56 candidates for 20 slots**. Retrieval delivered; the selector could
not rank what it was handed. US is the opposite failure — saturated, spread 0.081 across a year,
so which twenty a US anchor sees is near-arbitrary.

### The finding that reaches furthest

`select_with_theme_floor` reserves two slots per ledger so a quiet theme cannot be crowded out.
**197 of 290 PT 2019 sport articles carried a non-`broad` theme**, so they were competing for —
and winning — those reserved slots. `core.classify_themes` does naive substring matching, and a
match report is full of "attack", "defence", "war":

```
  [security   ] Aaron Ramsey's double sends Wales to Euro 2020 with win over Hungary
  [information] Southampton 2-1 Watford: Premier League - as it happened
  [edge       ] Phil Neville's son Harvey called into Republic of Ireland Under-19 squad
  [friction   ] Wolves' Diogo Jota earns draw at Brighton after quickfire goal exchange
```

`information` is the press-freedom and judicial-independence ledger. `edge` is skilled departure,
which the prompt states articles are its **only** instrument for. The theme floor was not failing
— it was being satisfied with football.

---

## 5. What was fixed, and in what order

Committed on `historical-ratings`, in mechanism order:

1. **`bbb18fd` — the scorer input.** `relevance_snippet` now clips to
   `core.RELEVANCE_SUMMARY_WORDS` (240), the live path's own budget, from one shared constant.
   `clip_words` moves to `core` so the two windows cannot drift again.
2. **`5215f77` — the Guardian `section` filter.** `SECTION_FILTER` excludes 15 sections in the
   request, the direct counterpart of `nyt._SKIP_DESKS` and a denylist for the same stated reason.
   Verified against the live API: 676 results unfiltered on the PT 2019 broad query, 426 with
   `-football`, 376 with `-football|-sport`.
3. **`81a0602` — one threshold rule and a floor behind a switch.** The top-up existed twice, in
   `article_enrichment` and `snapshot_select`; `core.apply_threshold` is now the only copy.
   `config.RELEVANCE_FLOOR_ENFORCED` stops the fill at the bar, **off by default**, read inside
   the shared function so both paths change together. `payload_health` now records
   `cleared_threshold` beside `articles`.

### Why the floor is off, and why order mattered

Stopping the top-up first — as originally proposed — would have **cut the evidence and kept the
football**: policy scored 0.100 and sport 0.450. The floor is only safe once the pool is worth
drawing on.

The top-up also fixed a real discontinuity that must stay fixed: read as a cap, two articles over
the bar gave a country a full twenty by rank and three gave it exactly three — evidence falling as
relevance rose. A test now holds both settings to that. **The question was never top-up versus no
top-up. It is whether the pool being topped up from is worth drawing on**, which is what fixes 1
and 2 address.

One operational consequence: `completed_windows` skips country-years already checkpointed `done`,
so the section filter reaches only unharvested windows until those `run_ledger` rows are cleared.
The harvest is currently **332 of 576 country-years done**, so roughly 42% of the roster will
harvest clean on merge and 58% needs a deliberate re-run.

---

## 6. Before and after, PT 2019

Replayed over all 52 stored anchors on DEV. Zero API calls, zero model calls, zero writes.
The `OLD` column reproduces `GATE2_BASELINE.json` exactly — 1,040 articles, 20.00 per snapshot,
guardian 959, nyt 81 — which is what makes the other two columns trustworthy.

| | OLD | NEW + top-up | NEW + floor |
|---|---|---|---|
| articles selected across 52 anchors | 1,040 | 1,003 | **358** |
| per anchor | 20.00 | 19.29 | 6.88 |
| clearing 0.3 | 344 (33.1%) | 358 (35.7%) | 358 (100%) |
| **sport/travel in what the model reads** | **408 (39.2%)** | **0 (0.0%)** | **0 (0.0%)** |
| mean selected relevance | 0.191 | 0.247 | **0.505** |
| median selected relevance | 0.100 | 0.100 | **0.550** |
| scoring 0.1 (never names the country) | 65.8% | 62.2% | 0.0% |
| guardian / nyt | 959 / 81 | 836 / 167 | 334 / 24 |

### Section mix of what the model actually reads

| section | OLD | NEW + top-up | NEW + floor |
|---|---|---|---|
| world | 146 (14.0%) | 318 (31.7%) | 164 (45.8%) |
| **football** | **355 (34.1%)** | 0 | 0 |
| business | 51 (4.9%) | 126 (12.6%) | 44 (12.3%) |
| politics | 31 (3.0%) | 94 (9.4%) | 15 (4.2%) |
| commentisfree | 36 (3.5%) | 88 (8.8%) | 29 (8.1%) |
| environment | 23 (2.2%) | 64 (6.4%) | 21 (5.9%) |
| **travel** | **70 (6.7%)** | 0 | 0 |
| **sport** | **53 (5.1%)** | 0 | 0 |
| uk-news | 10 (1.0%) | 51 (5.1%) | 19 (5.3%) |

The count barely moves; the composition changes completely. That is the finding.

### Per-theme counts against the floor of 2

Anchors where a theme came in under its reserved two slots:

| | friction | order | security | information | edge | broad |
|---|---|---|---|---|---|---|
| OLD | 0/52 | 0/52 | 13/52 | 16/52 | 8/52 | 0/52 |
| NEW + top-up | 0/52 | 0/52 | 40/52 | 41/52 | 47/52 | 4/52 |
| NEW + floor | 20/52 | 9/52 | 52/52 | 48/52 | 52/52 | 39/52 |

**Read this the right way round.** The floors did not get worse; they stopped being met with
football. OLD's `security` 13/52 and `edge` 8/52 were satisfied by "Aaron Ramsey's double sends
Wales to Euro 2020" and "Phil Neville's son called into Under-19 squad". The NEW rows are what
the Guardian's actual Portugal coverage supports, which is the number that should have been
visible all along.

### How thin is thin

`NEW + floor`, articles per anchor across 52: min 1, **median 7**, max 10.

| articles | anchors |
|---|---|
| 0 | **0** |
| 1–5 | 13 |
| 6–11 | 39 |
| 12–19 | 0 |
| 20 | 0 |

No anchor comes back empty. Thirteen of 52 would score on fewer than six articles.

### Three anchors in full

Full lists for a quiet week, the week of the 6 October legislative election, and a random anchor
are in `docs/retrieval-diagnosis-anchors.txt` beside this file. The quiet week is in §3 above.
The election week is the one that settles it:

```
OLD  (20 articles, 11 clearing)          NEW+topup (16 articles, 9 clearing)
  1 0.980 world    Portugal PM Re-Elected   1 1.000 world  Portugal election result cements gains
  2 0.830 world    Portugal election result 2 1.000 world  Portugal election: Socialists retain power
  3 0.830 world    Socialists retain power  3 1.000 world  Europe's beacon of social democracy
  4 0.750 world    Europe's beacon          4 0.980 world  Portugal PM Is Re-Elected (NYT)
  5 0.750 world    Kent couple jailed       5 0.980 world  Kent couple jailed in Portugal
  6 0.550*football Jordan Nobbs England     6 0.550 world  Greece, the eurozone crisis | Letters
  7 0.300*football Phil Neville criticism   7 0.550 world  First migrants land in Italy
  8 0.300*football Beth Mead snatches win   8 0.300 world  Portugal: British pair drown
  9 0.300*football Portugal game reminder   9 0.300 world  Hurricane Lorenzo (NYT)
 ... 10 of 20 are football ...             ... 0 of 16 are football ...
```

The election is found under both. Under OLD it shares the payload with **ten match reports**, and
the top story scores 0.830. Under NEW the same three election stories score **1.000** — the wider
window lets the keyword counts accumulate — so the fix sharpens the top of the ranking as well as
clearing the bottom, which is what feeds the Top-3 that reaches the dashboard.

---

## 7. Quota, and what a full re-harvest costs

**Spent this session: 7 Guardian calls.** No NYT calls, no model calls, **$0**.

The re-harvest of PT 2019 was **not** run, under the brief's own stop condition. The Guardian
harvest is live on PROD right now and was already at the wall:

- `run_ledger` shows **51 windows and 2,699 calls today**, plus one `failed` window at 96 calls —
  the wall it hit on its own.
- My seven probe calls returned `X-RateLimit-Remaining-Day` of 25, 24, **0**, 8, 23, 7, **0** —
  an erratic, shared counter with two outright refusals.

Taking more would have contended with the NUC cron for the same key. PT 2019 costs ~36 calls
(2.5% of a day's observed allowance) and can be run as a first step whenever the harvest is idle.

Measured from `run_ledger`, 332 completed Guardian country-years:

| | calls |
|---|---|
| per country-year, min / median / mean / max | 6 / **62** / 96 / 375 |
| PT, all 13 stored years | 441 total, median 36 |
| observed sustained daily spend (median of last 14 days) | **1,414** |

The advertised `X-RateLimit-Limit-Day` is 500 and the harvest routinely spends ~1,400, which is
the disagreement `guardian._QUOTA["observed_calls"]` exists to record. Planning against the
observed figure:

| full re-harvest, 48 countries × 12 years = 576 country-years | calls | days at 1,414/day |
|---|---|---|
| at the median country-year (62) | 35,712 | **25** |
| at the mean country-year (96) | 55,152 | **39** |

Both are upper bounds: the section filter returns fewer results, so fewer pages, so fewer calls.
Against 676 → 376 on the measured PT query, expect roughly 40–45% fewer. **Call it 15–22 days of
uncontended harvesting**, and it cannot run concurrently with the ongoing backfill.

---

## 8. Recommendation

**Re-harvest, but not all of it, and not first.**

The sources are not the limit. The median PT window already holds 56 candidates for 20 slots, and
after removing sport it holds enough to fill a 7-article honest snapshot with `world`-section
reporting. This was a selection failure end to end, and the selection is fixed in code — which
means **most of the value is already available without spending a single call.** The corpus on
disk is a superset of the corpus that should have been used; the section filter can be applied at
read time to what is already stored, which is exactly how §6 was produced.

So, in order:

1. **Re-derive the invalidated results on the existing corpus, read-filtered.** No quota, no
   waiting. This is the experiment that says whether the scores actually move, and it is the one
   thing nobody yet knows. Everything below is premature until it is done. Start with the
   rejections (`p3-context`, `p4-trend`) — an instrument scoring football would look coarse
   whatever the payload did, so those arms were arguably never given a fair reading.
2. **Let the running harvest finish on the fixed code.** 244 of 576 country-years are still
   outstanding and will retrieve clean on merge at no extra cost. Merging promptly is worth real
   money here: at ~30–65k items/day, every day the fix waits is another day of harvesting football.
3. **Then re-harvest selectively** — the pilot five, or whichever country-years the step-1
   re-derivation shows are actually evidence-starved after read-filtering. Clearing those
   `run_ledger` checkpoints is a deliberate act and should be one.
4. **Only then consider the full 48 × 12.** 15–22 days of uncontended harvesting to recover
   articles that were already retrieved once and are already on disk is a poor trade against
   (1), which costs nothing.

**Turn `RELEVANCE_FLOOR_ENFORCED` on after step 1, not before.** The honest cost is now known —
median 7 articles per anchor, 13 of 52 under six, none empty — but whether 7 real articles score
better than 20 padded ones is an empirical question, and `cleared_threshold` in `payload_health`
is the meter that will answer it.

### Two things left, both live-affecting, neither done here

**1. The scorer should ask the gazetteer, not do a substring match.** See §4. GB clears at 6.6%
and US at 10.4% because the press says "Britain" and "America"; `gazetteer.mentions(text, iso2)`
already resolves both and is already used elsewhere in this repo. This is the highest-value item
left in this area and it affects the two highest-volume countries in the corpus — on both paths.

**2. `classify_themes` tags match reports as `information` and `edge`.** The section filter
removes the articles that were tripping it at the Guardian; it does not fix the classifier. A
country whose live feed carries sport will still have its ledger floors filled by substring
matches on "attack" and "war" — and `information` and `edge` are the two ledgers with the least
macro data behind them, so a wrong tag there costs the most. `classify_themes` already documents
its own `ponytail:` note about word-boundary matching; this is the evidence that it now matters.

## What is not claimed

Nothing has been re-scored. This session spent $0 on model calls by instruction. What is
established is that the evidence reaching the model changes composition completely; whether the
scores move, and by how much, is step 1 above.
