"""
Self-provisioning DDL for the tables the payload work needs.

This project has no migration tool: every table is created idempotently by the
code that writes it (see the five `CREATE TABLE IF NOT EXISTS` constants in
`data_push.py`). These follow that convention rather than inventing a second
one, and `create_all` is safe to run on every startup.

Three tables, and the reason each exists:

`article` — the evidence, kept. Today a run fetches fifty-odd articles per
country, scores from them, persists the top three, and throws the rest away. That
makes a score unauditable after the fact and makes re-running last week's
scoring impossible: without the articles as they stood, a re-run is a re-fetch of
a different week. Keyed by URL, because that is what identity means for an
article, and carrying the hash of the body actually read.

`llm_artifact` — model output, content-addressed. Relevance labels and digests
are pure functions of (this exact text, this prompt version), so they are cached
on precisely that. A same-day re-run should cost almost nothing, and a prompt
edit should invalidate every row it touches without anyone having to remember to
clear a cache.

`mode` is in the artifact's primary key and carries the single value 'named'
throughout. **This is where a masking layer would sit.** The same text digested
under a masked and a named regime gives two different answers, and serving one
for the other would put a country's name into a prompt that was supposed not to
have it. Keeping `mode` in the key now means masking can be switched on later
without a migration and without a stale cache. Whether to mask at all is a
decision for Eli, and is deliberately not made here.

`payload_census` — what the registry promised against what reached the model,
one row per country per run. It is the countermeasure to this codebase's
recurring failure: code that ran, wrote something plausible, and had no
consumer, so every count looked right. A census nobody stores cannot be compared
against last week, and comparison is the only way to tell a source that broke
from a country that was quiet.
"""

from __future__ import annotations

from typing import Dict, List

__all__ = [
    "ARTICLE",
    "LLM_ARTIFACT",
    "PAYLOAD_CENSUS",
    "INDEXES",
    "create_all",
    "table_names",
    "verify",
]


# --- Article bodies, as retrieved ------------------------------------------

# What state the body is in, and therefore how much of the article the model
# actually read. The scorer is told this per article: a judgement made from a
# headline is not a judgement made from the reporting, and a payload that hides
# the difference invites the model to treat them alike.
BODY_STATUSES = ("full", "clipped", "digest-only", "title-only")

_STATUSES_SQL = ", ".join(f"'{s}'" for s in BODY_STATUSES)

ARTICLE = f"""
CREATE TABLE IF NOT EXISTS article (
    -- The Google News wrapper URL is not identity: the same story arrives under
    -- several wrappers. `url` is the resolved publisher link where resolution
    -- succeeded, which is why dedupe happens after resolution and not before.
    url                 TEXT PRIMARY KEY,
    wrapper_url         TEXT,
    country_iso2        TEXT NOT NULL,
    -- Which retrieval path produced this row. One table per source would make
    -- "everything we hold on this country" a union query forever.
    source_system       TEXT NOT NULL DEFAULT 'google-news',
    publisher           TEXT,
    -- The feed's date. About one item in twenty-five is a republication the feed
    -- dates to today, so this is not trusted alone.
    published_at        TIMESTAMPTZ,
    -- The date the article's own page carries, where it carries one. Preferred
    -- over `published_at` when they disagree, and kept beside it so the
    -- disagreement stays visible rather than being silently resolved.
    page_published_at   TIMESTAMPTZ,
    title               TEXT,
    abstract            TEXT,
    body                TEXT,
    -- The hash of the text actually read, not of the text fetched. When a body
    -- is clipped, this covers the clipped text: a cache keyed on the full
    -- article would serve a digest of words the model never saw.
    content_sha256      TEXT,
    body_status         TEXT NOT NULL DEFAULT 'title-only'
                        CHECK (body_status IN ({_STATUSES_SQL})),
    body_chars_original INT,
    body_clipped        BOOLEAN NOT NULL DEFAULT FALSE,
    -- Which retrieval themes turned this article up. An array because a story
    -- about a central bank under political pressure is genuinely both.
    themes              TEXT[],
    harvested_at        TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# --- Model output, content-addressed ---------------------------------------

# 'relevance' is the gate's label for one (article, country) pair; 'digest' is
# the structured summary of one body. Both are pure functions of their input
# text and their prompt version, which is what makes the cache sound.
ARTIFACT_KINDS = ("relevance", "digest")

_KINDS_SQL = ", ".join(f"'{k}'" for k in ARTIFACT_KINDS)

LLM_ARTIFACT = f"""
CREATE TABLE IF NOT EXISTS llm_artifact (
    -- For a digest, the hash of the body read. For a relevance label, the hash
    -- of the body *and the country being asked about*: the same article is
    -- structural for Germany and irrelevant for Portugal, so one row per text
    -- would be wrong.
    content_sha256  TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ({_KINDS_SQL})),
    -- The hash of the prompt text that produced this, never a hand-written
    -- number. Editing a prompt then forgetting to bump its version is how a
    -- cache serves answers to a question nobody is asking any more.
    version         TEXT NOT NULL,
    -- 'masked' | 'named'. Always 'named' for now; see the module docstring for
    -- why it is in the key regardless.
    mode            TEXT NOT NULL DEFAULT 'named'
                    CHECK (mode IN ('masked', 'named')),
    model           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (content_sha256, kind, version, mode)
)
"""



# --- What reached the model, per country per run ----------------------------

PAYLOAD_CENSUS = """
CREATE TABLE IF NOT EXISTS payload_census (
    country_iso2    TEXT NOT NULL,
    as_of           DATE NOT NULL,
    -- A hash over the resolved indicator codes and their vintages plus the
    -- ordered selected article ids. Two runs with the same fingerprint read the
    -- same evidence, which is the only way to tell a score that moved because
    -- the country moved from one that moved because the model did.
    payload_fingerprint TEXT,
    -- Computed from the census, never authored by the model.
    evidence_coverage   DOUBLE PRECISION,
    coverage_components JSONB,
    indicators      JSONB NOT NULL,
    articles        JSONB NOT NULL,
    versions        JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (country_iso2, as_of)
)
"""


INDEXES = (
    # The run asks "what did I hold for this country, this week".
    "CREATE INDEX IF NOT EXISTS article_country_published_idx "
    "ON article (country_iso2, published_at DESC)",
    # The census counts artifacts by kind per run.
    "CREATE INDEX IF NOT EXISTS llm_artifact_kind_idx "
    "ON llm_artifact (kind, mode)",
    # The alarm reads a country's own recent runs.
    "CREATE INDEX IF NOT EXISTS payload_census_country_idx "
    "ON payload_census (country_iso2, as_of DESC)",
)


_TABLES: Dict[str, str] = {
    "article": ARTICLE,
    "llm_artifact": LLM_ARTIFACT,
    "payload_census": PAYLOAD_CENSUS,
}


def table_names() -> List[str]:
    """Return the tables this module provisions, in creation order."""
    return list(_TABLES)


def create_all(cur) -> List[str]:
    """Create every table and index this module owns, if they do not exist.

    Idempotent, and cheap enough to run on every startup. Takes a cursor rather
    than opening a connection so the caller owns the transaction — provisioning
    that commits on its own would leave a half-built schema behind on a failure
    further down.

    Args:
        cur: An open psycopg2 cursor.

    Returns:
        The table names provisioned.
    """
    for ddl in _TABLES.values():
        cur.execute(ddl)
    for idx in INDEXES:
        cur.execute(idx)
    return table_names()


def verify(cur) -> Dict[str, bool]:
    """Report which of this module's tables the database actually has.

    The consumer side of :func:`create_all`. A provisioning step that returns
    without raising is not evidence that anything exists.

    Args:
        cur: An open psycopg2 cursor.

    Returns:
        ``{table_name: exists}``.
    """
    out: Dict[str, bool] = {}
    for name in table_names():
        cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
        out[name] = cur.fetchone()[0] is not None
    return out
