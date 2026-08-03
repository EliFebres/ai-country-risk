"""Assembling one historical snapshot's articles, with the no-future line drawn.

This is the seam. Everything below it — digest, evidence payload, scoring,
upsert — is the live daily pipeline with ``as_of`` pinned. Everything above it
is the article store. This module is the only difference between a historical
run and a live one, so it is the only place a leak can enter, and it is written
to be read with that in mind.

The rules it enforces, in order of how badly each one bites:

1. **The window is strict at the top.** ``[as_of - 30d, as_of)``. An article
   published *on* the anchor is same-day news; the live run's ``now() - 30d``
   cutoff would not reliably have had it either.

2. **A body may not be younger than the anchor.** An article published in June
   and captured by the Wayback Machine in August is a *June article with an
   August body* — publishers edit, append and re-headline. Scoring a June week
   on it is scoring on two months of hindsight. When the capture is younger than
   the anchor the body is dropped and the article stays as title and abstract:
   thinner evidence, honestly thin, rather than richer evidence that is a lie.

3. **A live refetch enters only if the leakage scan cleared it.** A page fetched
   today is by definition younger than any historical anchor, so the only thing
   making it usable is step 4's scan, which asks the model whether the text
   knows anything that happened after publication and fails closed. A flagged
   body was already discarded at recovery time and arrives here with no body at
   all.

Selection itself is not reimplemented. Relevance is
``article_ranking.score_relevance``, the floor is ``core.select_with_theme_floor``,
and the threshold and its fallback are ``article_enrichment``'s own constants —
two copies of "the 20 articles" would be a silent disagreement about what the
historical series is comparable to.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

from backend.utils.history import config, store
from backend.utils.news_fetching import article_enrichment, article_ranking, core

logger = logging.getLogger(__name__)

# How much of a body stands in for the feed blurb when an article has no
# abstract. `score_relevance` reads title + snippet, and the historical sources
# do not all carry one: Guardian rows have a full body and no abstract, NYT rows
# an abstract and no body. Feeding a whole body to a function tuned on one-line
# blurbs would inflate its keyword counts and score historical articles on a
# different curve than live ones — so the length stays fixed, and only *which*
# 300 characters changes.
_SNIPPET_CHARS = 300

def relevance_snippet(row: Dict[str, Any], body: Optional[str],
                      country_name: str) -> str:
    """The short summary `score_relevance` reads: the abstract, or the lede.

    ``country_name`` is accepted and deliberately unused, and this is the
    interesting part of the module.

    A measured PT window (2018-05-05 → 2018-06-04, 63 Guardian articles) has
    "Portugal" in 0 titles, 6 ledes and 59 bodies, so scoring on the lede
    selects only 6 articles where the live run would select 20. The obvious fix
    — excerpt from wherever the body first names the country — was tried, and it
    is wrong. It lifts every article that mentions Portugal in passing to the
    body-mention ceiling, and the resulting "Portugal" snapshot is twenty
    articles about the Dutch government, UK farmers, Venezuela and José
    Mourinho. That is precisely the failure ``_BODY_MENTION_CAP`` exists to
    prevent, defeated by feeding the scorer a snippet chosen to beat it.

    Both readings agree on the actual fact: the Guardian, a British paper,
    barely covers Portugal. Zero titles is not a scoring artefact. The thinness
    is real, and the honest response is a thin week plus a loud report, not a
    heuristic tuned until the number looks like the live one.

    It is also a symptom of an unfinished harvest rather than of selection:
    GDELT and the NYT are the sources meant to carry non-Anglophone coverage of
    smaller countries, and neither has landed yet. Re-measure once they have —
    if PT is still thin then, the corpus is telling the truth about Portugal.
    """
    return row.get("abstract") or (body or "")[:_SNIPPET_CHARS]


def window(as_of: datetime.date) -> tuple:
    """The ``[start, end)`` a snapshot on this anchor may read from.

    Returns:
        Timezone-aware UTC bounds. The end is the anchor's own midnight, which
        is what makes the upper bound strict.
    """
    end = datetime.datetime.combine(as_of, datetime.time.min,
                                    tzinfo=datetime.timezone.utc)
    return end - datetime.timedelta(days=config.SNAPSHOT_WINDOW_DAYS), end


def capture_date(body_vintage: Optional[str]) -> Optional[datetime.date]:
    """The date a ``wayback-YYYYMMDD`` body was captured, if it says.

    Returns None for ``api-native`` (the body came with the article, so it is
    the article's own age) and for ``live-refetch`` (which carries no date and
    is governed by the leakage scan instead).
    """
    if not body_vintage or not body_vintage.startswith("wayback-"):
        return None
    stamp = body_vintage[len("wayback-"):][:8]
    try:
        return datetime.datetime.strptime(stamp, "%Y%m%d").date()
    except ValueError:
        # An unparseable vintage is not a licence to use the body. Treated as
        # "unknown age" by the caller, which refuses it.
        return None


def usable_body(row: Dict[str, Any], as_of: datetime.date) -> Optional[str]:
    """The article's body, or None when using it would be hindsight.

    The one function where the no-future rule actually bites. Everything it
    refuses still reaches the scorer as title and abstract — a thinner article,
    never a missing one.
    """
    body = row.get("body")
    if not body:
        return None

    vintage = row.get("body_vintage")

    if vintage == "api-native":
        # The body arrived inside the search response, as the article itself.
        return body

    if vintage == "live-refetch":
        # Younger than any historical anchor by construction. Usable only
        # because step 4's leakage scan cleared it of post-publication
        # knowledge, and post-publication covers post-anchor: the article was
        # published before the anchor or it would not be in this window.
        return body if row.get("body_status") == "recovered" else None

    captured = capture_date(vintage)
    if captured is None:
        logger.debug("dropping body with unreadable vintage %r on %s", vintage, row.get("url"))
        return None
    if captured >= as_of:
        # A June article captured in August, read as of June. The capture is a
        # later edition of the page, not the page as it stood.
        return None
    return body


def to_item(row: Dict[str, Any], as_of: datetime.date,
            country_name: str = "") -> Dict[str, Any]:
    """One stored row as the canonical item the live pipeline consumes.

    Themes collapse to the first of ``themes[]``: the store writes the
    retrieving query's theme first and the classifier's guesses after it, and a
    live item carries exactly one ``_theme``. Passing the strongest one keeps
    the theme floor rationing the same thing in both paths.
    """
    body = usable_body(row, as_of)
    themes = row.get("themes") or []
    published = row.get("published_at")
    return core.normalize_item(
        title=row.get("title") or "",
        link=row.get("url") or "",
        publisher_link=row.get("publisher_link") or row.get("url") or "",
        published=published.isoformat() if published else None,
        source=row.get("source_system") or "",
        snippet=relevance_snippet(row, body, country_name),
        text=body or "",
        theme=themes[0] if themes else None,
        # Provenance the manifest needs and the invariant tests assert on.
        source_system=row.get("source_system"),
        body_vintage=row.get("body_vintage") if body else None,
        tier=row.get("tier"),
    )


def select(iso2: str, as_of: datetime.date,
           max_articles: int = 20) -> List[Dict[str, Any]]:
    """The articles a snapshot for this country on this date is scored on.

    The historical counterpart of ``article_enrichment.fetch_relevant_news``,
    and deliberately the same shape: score, threshold, per-theme floor, cap.

    Args:
        iso2: pilot country.
        as_of: the snapshot anchor. Nothing published on or after it is read.
        max_articles: the same budget the live run spends.

    Returns:
        Up to ``max_articles`` canonical items, most relevant first. Empty is a
        legitimate answer for a thin week and must stay one — inventing
        articles to fill a quota is the failure this whole machine exists to
        avoid.
    """
    start, end = window(as_of)
    rows = store.read_window(iso2, start, end)
    if not rows:
        logger.info("[%s %s] no articles in window", iso2, as_of)
        return []

    country_name = config.country_name(iso2)
    items = [to_item(row, as_of, country_name) for row in rows]
    for item in items:
        item["relevance_score"] = article_ranking.score_relevance(item, country_name)

    # The live rule, and its live fallback: if too few articles clear the bar,
    # the bar comes off rather than the country losing its week.
    clearing = [i for i in items
                if i["relevance_score"] >= article_enrichment._RELEVANCE_THRESHOLD]
    if len(clearing) < article_ranking.TOP_N:
        logger.info("[%s %s] only %d of %d cleared the relevance threshold; "
                    "taking the best available", iso2, as_of, len(clearing), len(items))
        clearing = items

    return core.select_with_theme_floor(
        clearing, max_articles, article_enrichment._PER_THEME_FLOOR)
