# The three-door event test — a proposal, not a recovered definition

**Status: written 2026-09-22 for review. Eli should check it against what he
intended.**

The v2.0 payload brief names "the three-door event test" as part of the scoring
framework and does not define it. Nothing on `backend-update` defines it either.
So the three doors currently in `RISK_PROMPT` are a **reconstruction**, written
from the ledger framework rather than recovered from anywhere. They are in the
prompt because the prompt needed something there; they are in this document
because that fact should be visible rather than buried.

This is documentation, not tuning. The wording has not been iterated on, and no
measurement has been taken of what changes if it is.

---

## The problem the test exists to solve

Most of what a country's news says in a given week is true, prominent, and
irrelevant to its risk. A minister resigns, a factory closes, a court rules, a
plane crashes. Some of those change the country; most describe it on a day.

The relevance gate already removes what is not about country risk at all. The
three-door test is the *second* question, asked of what survives: this article
is about the country's institutions or economy — does it change the reading, or
merely illustrate it?

Without a test, prominence substitutes for materiality. The loudest story moves
the score, which is how an instrument ends up tracking the news cycle instead of
the country.

## The three doors

An event moves the rating only if it passes **at least one**. An event that
passes none is news, not risk: the article's note says so and the rating stays
where it was.

### Door 1 — Capacity: does it change what the state CAN DO?

Its authority, its reach, its ability to enforce a rule or to pay a bill.

- **Passes:** a constitutional court strikes down the government's budget
  mechanism. The state's fiscal options are narrower on Tuesday than they were
  on Monday.
- **Fails:** a cabinet minister resigns over a personal scandal and is replaced
  within the week. The office, its powers and its occupant's mandate are
  unchanged; only the name is different.

### Door 2 — Cost: does it change what it COSTS to operate there?

Taxes, prices, the currency, the predictability of the rules a business plans
against.

- **Passes:** a windfall levy is imposed on a sector with immediate effect. Every
  plan written against the old rate is now wrong, and the next one will be
  written against the possibility of another levy.
- **Fails:** a single large firm announces a plant closure and 900 redundancies.
  Painful and local; the cost of operating in the country is what it was the day
  before.

### Door 3 — Knowability: does it change what can be KNOWN?

The reliability of official statistics, the freedom to report, the ability of an
outsider to verify a claim.

- **Passes:** the statistics agency's head is replaced by political appointment
  and the next inflation release is postponed. Every number from that country now
  carries a discount, including the ones already published.
- **Fails:** a newspaper loses a libel case brought by a politician. Unwelcome,
  and one outlet's problem; the country's information environment is not
  measurably different.

## What the doors deliberately do not include

**Prominence.** A story can lead every front page and pass no door. Coverage
volume is a fact about newsrooms.

**Severity of harm to people.** A disaster with a large death toll may pass no
door — that is a statement about what this instrument measures, which is
investor-facing country risk, and not a claim about what matters.

**Direction.** A door is about whether the reading changes, not which way. A
credible fiscal consolidation passes Door 2 exactly as a windfall tax does.

## Open questions for review

1. **Are three the right doors?** Capacity, cost and knowability map onto
   `order`, `friction` and `information`. `edge` — whether the system is
   learning — has no door, which is arguably correct because edge is observed
   and never penalised, and arguably a gap.
   Decided 2026-09-27: intended. Edge has no news door and runs on data (PISA,
   business formation, emigration series), so the relevance gate admitting no
   article to the `edge` ledger is correct.
2. **Should a door have a magnitude?** At present a door is binary and
   `bearing` (0–100) carries magnitude separately. A very small windfall levy
   passes Door 2 identically to a large one.
3. **Is "at least one" the right rule**, or should an event passing two doors be
   treated as materially different from one passing a single door?
4. **Is Door 3's asymmetry intended?** Knowability almost always degrades
   suddenly and improves slowly, so Door 3 will fire far more often on bad news
   than good.

None of these are settled here. The schema records which door an article passed
(`article_scores[].door`, one of `capacity` / `cost` / `knowability` / null), so
the distribution can be measured once enough runs exist to look at.
