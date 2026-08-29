# News source evaluation — newsapi.ai (Event Registry)

**Decision: not adopted. Evaluated 2026-08-28, code removed 2026-08-29.**

Half the corpus has no bodies. NYT contributes ~27,000 articles that are
abstract-only by design and GDELT is dormant, so the Guardian carries the body
burden alone — and it only covers what the Guardian covers. newsapi.ai indexes
~150,000 publishers with full article text and an archive to 2014, which looked
like the obvious way to buy bodies for countries the Guardian reaches only as
foreign news.

It was measured on a live 5K-plan key: 294 tokens, PT 2019 both ways plus BR
2019, **nothing written to the store**. Two findings came out of it, and the
second is the one that decided the purchase.

The adapter is preserved in history rather than in the tree. `git show 458140a`
is the adapter and its tests, `d707a8e` the evaluation harness and the measured
results, `4d97c63` the Brazil finding. Reviving it is one `git revert` away
from working code.

---

## 1. Volume and fit are different things

**A broad concept query against a local-media-heavy index returns sport.**

newsapi.ai supplied roughly twice the Guardian's article count for a
country-year and fit the ledgers worse. Measured on PT 2019, percent of the 48
weekly anchors where a theme fell short of the two-per-snapshot floor that
`core.select_with_theme_floor` reserves:

| source | n | friction | order | security | information | edge | broad |
|---|---|---|---|---|---|---|---|
| guardian | 642 | 0 | 0 | 0 | 10% | 0 | 0 |
| **newsapi.ai** | **1,200** | 4% | 4% | 4% | **46%** | **56%** | 4% |
| nyt | 57 | 100% | 98% | 100% | 100% | 100% | 10% |

Against a failure line of 25% agreed **before** the data was seen, `information`
and `edge` both fail. The Guardian fills every floor with 642 articles where
newsapi.ai fails two floors with 1,200.

The top publishers say why: **SAPO Desporto, O Jogo, Maisfutebol.** Ask an index
of general news for "Portugal" and it answers with the football, because that is
what most publishers write most often about most countries.

The remedy works and was measured rather than assumed. A keyword-directed
top-up on the failing themes only — the country concept AND that theme's own
`core.THEME_QUERIES` terms — returned **62 information-classified articles in
one month against roughly 1.7** from the broad query, and 67 for `edge`. Two
extra searches a month. It costs 120 tokens per country-year on top of a
60-token baseline, which across 48 countries and a decade is **~$1,220 in
overage — more than the scoring the corpus would feed.**

**150,000 publishers of general news is not automatically better evidence for a
governance rating.** More articles that fit the ledgers worse is not a corpus
improvement. This is the claim `adapters/guardian.py` has always made in its
docstring — that collapsing six per-theme queries into one would be cheaper and
*"would break the point"* — now measured instead of asserted.

---

## 2. The gap was a failed HTTP request

Brazil was the entire case for buying this, and Brazil is the better cautionary
tale.

BR looked starved: 242 articles for 2019 against Turkey's 1,653, failing all
five theme floors where every other country passed. It looked exactly like a
country the Guardian could not reach.

It had **zero Guardian rows.** All 242 were NYT abstract-only, which fails all
five floors everywhere by design. `run_ledger` had the story:

| iso2 | done | failed | never attempted | articles | calls |
|---|---|---|---|---|---|
| PT | 11 | 0 | 2 | 9,059 | 364 |
| TR | 11 | 0 | 2 | 15,495 | 768 |
| US | 8 | 3 | 2 | 26,380 | 1,450 |
| KR | 7 | 4 | 2 | 5,875 | 305 |
| **BR** | **0** | **11** | **2** | **0** | **11** |

Eleven windows, every one `status='failed', note='request error'`, one call and
~20 seconds each. Nothing retried them and nothing reported them. A single live
call on 2026-08-28 returned **15 pages** for BR 2019 — the Guardian covers
Brazil perfectly well, and only `done` is skipped on resume, so the work had
been one command away the entire time.

Re-harvested: **1,421 rows in 65 calls and 143 seconds, free.** BR now passes
every theme floor at 0% short, the cleanest result in the store.

**The gap a paid source was nearly bought to fill was a failed HTTP request.**

---

## 3. The roster check — six for six

Percent of 2019 weekly anchors short of the floor, every country with a
completed Guardian harvest:

| country | n | short |  |
|---|---|---|---|
| US | 3,344 | none | PASS |
| TR | 1,277 | none | PASS |
| BR | 1,067 | none | PASS |
| PT | 642 | information 10% | PASS |
| KR | 462 | information 23% | PASS |
| **ID** | **432** | information 2%, edge 2% | **PASS** |

Indonesia is the load-bearing row: mid-size, non-anglophone, not in the pilot,
**never harvested before**. It took 26 calls and 60 seconds and passed on first
contact.

Every country whose Guardian harvest completed passes every floor.

**Honestly stated: this is one year on six countries, not 48.** The other 42 are
unharvested for 2019, and the whole lesson of §2 is that an unharvested country
looks identical to an uncoverable one until you check. Six for six moves the
prior a long way; it does not close the question. The order of operations is
what changed: **finish the free harvest before pricing a paid one.**

---

## 4. Measured mechanics

These cost tokens to learn and would cost tokens to relearn, so they are
recorded whatever happens to the adapter.

**Billing.** 1 token per page inside the last ~30 days; a flat **5 per calendar
year the range touches** beyond that, never prorated. A five-day range in 2025
bills the same as all of 2025. Their documentation says so once you know to read
it that way — *"the number of used tokens per search will depend on the number of
years included in the search."*

That inverts the attractive hypothesis. Twelve monthly windows cost **12× an
annual window per page**, not a twelfth.

**Monthly wins anyway, on shape rather than price.** A year-wide window with a
10-page cap put **800 of 1,000 PT articles in December**: `articlesSortBy=date`
under a cap returns the newest N, so eleven of twelve anchors read a near-empty
window. An annual total cannot see this. Monthly costs 60 tokens against 50 and
returns 1,200 articles against 1,000, 100 in every month. **Any page cap on this
source has to be expressed per window, never per country-year.**

**Archive access is an account checkbox, not a request parameter.** Before it
was enabled, every pre-30-day query returned `totalResults: 0`, billed a token,
and raised no error of any kind. Unguarded, a 48-country decade would have spent
~480 tokens writing nothing and checkpointed all 480 windows `done` — recording
a completed backfill that never happened.

**The API does not truncate bodies.** 1.0–1.4% end in an ellipsis or "read
more", with no pile-up on any exact length and no empty bodies: publisher
teasers, not clips. Body length is **continuous rather than bimodal**, so no
natural floor exists in the data and any threshold is a judgement call.

**Our own clip is bigger than theirs.** Ten newsapi.ai bodies came back at
exactly 24,000 characters — `core.MAX_BODY_CHARS`, applied in the adapter, not a
vendor limit. Checking the same thing against the existing corpus found **6,165
of 53,377 Guardian rows clipped at exactly 24,000**, 11.6%. That is a finding
about this repo rather than about the vendor, and it outlives the evaluation:
`deferred.md` item 23.

**Overlap with the existing corpus is ~zero** — 0 of 1,200 URLs for PT, 1 of
1,200 for BR. The index does not collide with the Guardian's URLs.

**Dates were clean** — 0 outside the requested window, 0 unparseable, across
both country-years.

**Masking exposure.** Local outlets were **19.7% of the PT corpus and 15.6% of
BR**, against a Guardian mix that is almost entirely foreign desks. That is a
masking risk rather than a quality one — `llm.gazetteer` masks names, not
register, and a regional paper writing "the government" identifies a country far
more sharply than the Guardian writing the same words. Portugal is already
`llm.probe`'s hardest case: a masked bundle that passed the integrity scan with
zero flagged tokens was still named at 0.9 confidence off the Douro Valley and
the Algarve. Adopting this corpus would have required restoring the outlet
fingerprinting comparison (`deferred.md` item 7) first.

---

## 5. The limitation this exposed and did not fix

**There is no full-text second source behind the Guardian.**

NYT contributes ~27,000 articles at `body_status='degraded-title-only'`,
deliberately — queueing ~200k paywalled URLs into the Wayback drain would buy
nothing. GDELT is dormant on measured pacing grounds. So every body in the
corpus is a Guardian body or a Wayback recovery of one, and BR-on-NYT-alone
failing all five floors is what that looks like when the Guardian is absent.

The evaluation did not fix this. It established that the Guardian is sufficient
for six countries in 2019 and that the obvious paid substitute is a worse fit at
a cost exceeding the scoring it feeds. If a second body source is ever needed,
the finding in §1 is the specification: **it has to be retrievable per theme, or
its volume is worth nothing.**

Adjacent, and unchanged by this: the Guardian adapter applies no body-length
floor at all — `if item.get("text")`, so a one-character body is stored as
`recovered`. Audited over all 51,872 Guardian rows marked `recovered`, **131
(0.25%) fall under 1,000 characters and 6 under 400**, mean 8,234, median 5,599,
min 206. A latent risk, not a realised one; Gate 2 was not overstated.
