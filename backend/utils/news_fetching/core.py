"""Everything about an article that does not depend on where it came from.

The live run gets its articles from Google News RSS. The historical run gets
them from the Guardian Content API, GDELT, and the NYT archive. Those are four
different retrieval problems and exactly one shared one: once you hold a list of
article items, what happens next must be identical, or the series validated
against history is not the series produced tomorrow.

So this module owns the source-agnostic half — the canonical item shape, body
extraction, theme tagging, the theme-floor selection, and the two dedupe keys —
and every path imports it rather than carrying a copy. ``fetch_links`` and
``article_enrichment`` keep their old private names as aliases so nothing that
imported them has to change.

The one behavior here that the live path did not previously have is
:func:`classify_themes`. Live items are tagged by *which query returned them*,
which is better evidence and stays primary; the classifier is the fallback for
items that arrive with no query provenance at all — which is most of the
historical corpus. :func:`ensure_theme` is where the two meet, and it is a no-op
on any item that already carries a theme.

Bodies here are always raw and unmasked. Masking is a transform applied at the
scoring boundary; anything derived from article text at harvest time — themes,
relevance, hashes — is derived from the unmasked text and stored beside it.
"""

import logging
from typing import Dict, List, Optional

import trafilatura

from backend.util.dates import parse_date_for_sort

logger = logging.getLogger(__name__)

# Quiet noisy warnings from trafilatura. Lives here because this module is the
# only place that calls it.
logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("trafilatura.core").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------
# Query themes, one per ledger the prompt actually scores plus a catch-all.
#
# The previous set — broad, government, economic, security — predated the
# friction framework and covered two ledgers out of four: nothing looked for
# taxes, permits, courts, press freedom, the statistics office, business
# formation or education. The prompt asks for evidence on all four, and for
# skilled departure it says outright that articles are its ONLY instrument,
# there being no data series for it. A retrieval layer that cannot surface those
# stories leaves the model to score that ledger on the macro panel alone.
#
# Order matters: `broad` runs LAST. De-duplication in the callers is
# first-seen-wins, so a story a specific theme also found keeps the specific tag
# — and the per-theme floor is only meaningful if the specific themes get to
# claim their own articles before the catch-all does.
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

# The catch-all. Named rather than repeated as a literal, because it is also the
# answer `classify_themes` gives when no specific theme fires.
BROAD_THEME = "broad"

# How much of one article's body any path keeps. Shared so a historical body and
# a live one are the same size of evidence: a harvester storing 100k characters
# where the daily run stores 24k would be scoring history on a different
# instrument, which is the whole thing the History Machine is trying not to do.
MAX_BODY_CHARS = 24000


def _terms_of(template: str) -> tuple[str, ...]:
    """The OR-terms inside one theme's query template, lowercased.

    Derived from the template rather than typed out a second time: a term added
    to a query but not to the classifier would mean an article the live path
    tags one way and the historical path tags another, for the same words.

    ``broad`` has no parenthesised group and yields no terms, which is correct —
    "broad" is the absence of a specific signal, not a signal of its own.
    """
    start, end = template.find("("), template.rfind(")")
    if start == -1 or end <= start:
        return ()
    return tuple(t.strip().lower() for t in template[start + 1:end].split(" OR ") if t.strip())


THEME_TERMS: dict[str, tuple[str, ...]] = {
    theme: _terms_of(template) for theme, template in THEME_QUERIES.items()
}

# How much of a body `classify_themes` reads. Roughly the first few paragraphs:
# far enough in to know what the story is about, short enough that a page's
# navigation, related-links block and comment section cannot vote.
_CLASSIFY_CHARS = 2000


def classify_themes(title: Optional[str], body: Optional[str]) -> List[str]:
    """Tag an article by content, most strongly matched theme first.

    The fallback for items with no query provenance. Matching is naive
    lowercased substring containment, deliberately the same shape as
    ``article_ranking._HIGH_KEYWORDS``: this is triage feeding a model, not a
    classifier anyone should trust on its own.

    Args:
        title: the headline, may be None.
        body: the article text, may be None. Only the head of it is read —
            themes live in what a story is about, and a long tail of boilerplate
            and related-links markup matches everything.

    Returns:
        Matched themes, ordered by match count then by ``THEME_QUERIES`` order.
        ``["broad"]`` when nothing matches, so the return is never empty and a
        caller can always take ``[0]``.

    ponytail: substring matching, so 'war' also matches 'warehouse'. Upgrade to
    word-boundary regex if the mis-tags ever show up in the theme-floor counts;
    for a tag that only decides which slot an article competes for, a wrong tag
    costs one slot, not a wrong score.
    """
    text = f"{title or ''} {(body or '')[:_CLASSIFY_CHARS]}".lower()
    scored = [
        (sum(1 for term in terms if term in text), order, theme)
        for order, (theme, terms) in enumerate(THEME_TERMS.items()) if terms
    ]
    matched = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
    return [theme for _, _, theme in matched] or [BROAD_THEME]


def ensure_theme(item: Dict) -> Dict:
    """Fill ``_theme`` from the content classifier, but only if it is missing.

    Query provenance stays primary wherever it exists: which query returned an
    article is stronger evidence than which words it contains. This is the
    fallback, and both paths call it — the live one where it is a no-op (the
    fetch loop tags every item first), the historical one where it does the
    actual work. Mutates in place and returns the same dict, matching the
    ``resolve_and_enrich`` convention.
    """
    if not item.get("_theme"):
        item["_theme"] = classify_themes(item.get("title"), item.get("text"))[0]
    return item


# ---------------------------------------------------------------------------
# The canonical item
# ---------------------------------------------------------------------------
# The key names are the live pipeline's, not new ones: `provenance`,
# `article_ranking` and `digest_engine` all read `link`/`text`/`published`, and
# renaming them to match the historical_article column names would have been a
# live-path behavior change to buy nothing. The store maps item keys to column
# names at its own boundary, which is the one place the two vocabularies meet.

_ITEM_KEYS = ("title", "link", "publisher_link", "published", "source", "snippet", "text", "_theme")


def normalize_item(
    *,
    title: str = "",
    link: str = "",
    publisher_link: str = "",
    published: Optional[str] = None,
    source: str = "",
    snippet: str = "",
    text: str = "",
    theme: Optional[str] = None,
    **extra,
) -> Dict:
    """Build the canonical article item every path downstream consumes.

    Args:
        title: headline.
        link: the URL as the source gave it (a Google News wrapper, for RSS).
        publisher_link: the resolved publisher URL. Defaults to ``link`` when a
            source hands back only one URL, which is every historical adapter —
            so :func:`dedupe_key` behaves the same for all of them.
        published: ISO8601 timestamp string, or None.
        source: publisher name.
        snippet: the feed's own blurb, plain text.
        text: the extracted body. Empty is normal at construction time; the live
            path fills it in a later concurrent pass.
        theme: the theme that retrieved this item, or None to let
            :func:`ensure_theme` classify it later.
        **extra: source-specific fields (``snippet_html``, ``abstract``, …)
            passed through untouched.

    Returns:
        A dict carrying every key in ``_ITEM_KEYS`` plus whatever ``extra`` held.
        Always the same shape, so no consumer has to guard for a missing key.
    """
    return {
        "title": title or "",
        "link": link or "",
        "publisher_link": publisher_link or link or "",
        "published": published,
        "source": source or "",
        "snippet": snippet or "",
        "text": text or "",
        "_theme": theme,
        **extra,
    }


def validate_item(item: Dict) -> Dict:
    """Return ``item`` if it is a usable article, raise otherwise.

    The contract every adapter is held to, so a malformed payload fails at the
    adapter that produced it rather than three stages later in a store write
    with no idea which source is at fault.

    Raises:
        TypeError: if ``item`` is not a dict.
        ValueError: if a canonical key is missing, or if there is no URL to key
            the article on.
    """
    if not isinstance(item, dict):
        raise TypeError(f"item must be a dict, got {type(item).__name__}")
    missing = [k for k in _ITEM_KEYS if k not in item]
    if missing:
        raise ValueError(f"item is missing canonical keys {missing}: {item!r}")
    if not dedupe_key(item):
        raise ValueError(f"item has neither publisher_link nor link: {item!r}")
    return item


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------

def extract_body(html: Optional[str], url: Optional[str] = None) -> str:
    """Extract an article's main text from its HTML.

    Args:
        html: the raw page.
        url: the page's own URL. trafilatura uses it as a heuristic hint, so
            passing it improves extraction on sites whose markup is ambiguous.

    Returns:
        The extracted text, or ``""`` when there is nothing extractable. A
        missing body is normal — paywalls, JS-only pages, an archive capture of
        a redirect — and must never stop the surrounding batch, so this
        swallows extraction failures rather than raising.
    """
    if not html:
        return ""
    try:
        return trafilatura.extract(html, url=url) or ""
    except Exception:  # noqa: BLE001 - a page that defeats the parser is not an error
        return ""


# ---------------------------------------------------------------------------
# Dedupe keys
# ---------------------------------------------------------------------------

def dedupe_key(item: Dict) -> str:
    """The URL two copies of one story must agree on to be seen as one.

    The resolved publisher URL, not the wrapper: two queries returning the same
    story get different Google News links, and a duplicate costs a stage-1
    digest call and inflates its own ``topic_group`` in the model's clustering.
    Historical adapters set ``publisher_link`` to their only URL, so the rule is
    the same for every source.
    """
    return ((item.get("publisher_link") or item.get("link")) or "").strip()


def headline_key(title: Optional[str]) -> str:
    """Normalize a headline so syndicated copies of one wire story collapse.

    A wire story runs at a dozen outlets under the same headline and a different
    URL, so :func:`dedupe_key` cannot see them as one. Each copy costs a stage-1
    digest call. The model's ``topic_group`` clustering does merge them at
    scoring time, so the duplicates never reach the dashboard — they are pure
    spend, which is the worst kind of bug to leave in: invisible in the output.

    Google News appends " - Publisher" to every title, so the last such segment
    goes; the rest is lowercased and stripped of punctuation and whitespace
    runs. Matching is exact after that, deliberately: fuzzy matching would start
    collapsing genuinely different stories about the same subject, and two
    stories are worth more to the model than one.
    """
    text = (title or "").rsplit(" - ", 1)[0]        # drop the publisher suffix
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def by_relevance(items: List[Dict]) -> List[Dict]:
    """Most relevant first, ties broken toward the more recent article."""
    return sorted(
        items,
        key=lambda x: (x.get("relevance_score", 0.0), parse_date_for_sort(x.get("published"))),
        reverse=True,
    )


def select_with_theme_floor(items: List[Dict], max_articles: int, per_theme: int) -> List[Dict]:
    """Guarantee each theme a share of the budget, then fill by relevance.

    Without this the budget is spent by one global relevance sort, and a theme
    whose news is quiet this week loses every slot to whichever theme is loudest
    — so adding a query would buy nothing but the fetch. An election week would
    return twenty election stories and the friction and information ledgers would
    be scored on the macro panel alone.

    A theme with nothing to offer forfeits its quota rather than shrinking the
    result: the floor is a guarantee against crowding out, not a requirement that
    every theme produce news.

    This is the function the whole retrieval restructure was built around, which
    is why it lives here rather than in either path: two copies would be a silent
    disagreement about what "the 20 articles" means, and the historical series
    would stop being comparable to the live one without anything saying so.

    Args:
        items: scored articles, each tagged with ``_theme``.
        max_articles: total cap on the returned list.
        per_theme: slots reserved for each theme before the open fill.

    Returns:
        Up to ``max_articles`` items, most relevant first.
    """
    ranked = by_relevance(items)
    quota = {theme: per_theme for theme in THEME_QUERIES}
    picked: List[Dict] = []
    taken = set()

    for item in ranked:                       # first pass: spend each theme's quota
        if len(picked) >= max_articles:
            break
        theme = item.get("_theme")
        if quota.get(theme, 0) > 0:
            quota[theme] -= 1
            picked.append(item)
            taken.add(id(item))

    for item in ranked:                       # second pass: fill the remainder
        if len(picked) >= max_articles:
            break
        if id(item) not in taken:
            picked.append(item)

    # The caller assigns ids a1..aN by position, so hand back the familiar order.
    return by_relevance(picked)
