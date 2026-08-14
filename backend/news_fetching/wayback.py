"""Body recovery — turning GDELT's URLs back into text, honestly.

GDELT hands over a hundred thousand URLs and no words. This module drains that
queue: for each pending article it asks the Wayback Machine for the capture
nearest to publication, fetches it, and extracts the body with the same
extractor the live run uses.

The interesting part is what happens when there is no capture. Refetching the
live page is tempting and mostly works — but a 2018 article refetched in 2026
can carry a correction, an editor's note, an "as it turned out" paragraph added
years later. That is future text wearing a past date: the subtlest leak there
is, invisible in every count, and it would quietly teach the scorer to be right
about things nobody knew yet. So a live refetch does not count as recovered
until a cheap model has read it and confirmed it references nothing after its
own publication date. Flagged bodies are discarded and the article drops to
``degraded-title-only``.

That scan is the only OpenAI-billable thing in the whole harvest phase. It
prints a projected cost, waits for an explicit go, and aborts at
``LEAKAGE_SCAN_BUDGET_USD``.

The Wayback Machine is a free public service being asked for a hundred thousand
lookups. One request a second, a descriptive User-Agent, real backoff on 429,
and a queue that resumes — this takes hours and will be interrupted.
"""

import datetime
import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

from backend.util import http
from backend.utils.ai import client as ai_client
from backend.data_upsert import store
from backend.utils.history import config
from backend.news_fetching import core

logger = logging.getLogger(__name__)

_CDX = "https://web.archive.org/cdx/search/cdx"
# The `id_` suffix asks for the original page rather than the archive's own
# framed rendering — no toolbar, no injected banner, no rewritten asset URLs to
# confuse the extractor.
_CAPTURE = "https://web.archive.org/web/{timestamp}id_/{url}"

# Politeness, and a real address to complain to.
_UA = http.PROJECT_UA

# What the scan costs. gpt-4o-mini input pricing; the scan asks for one boolean
# back, so output tokens round to nothing against a body-sized prompt.
_SCAN_USD_PER_1M_INPUT_TOKENS = 0.15
_CHARS_PER_TOKEN = 4   # the same estimate data_retrieval uses, for the same reason

_SCAN_PROMPT = """You are checking one archived news article for time leakage.

The article was published on {published}. Read the text and answer one question:
does it reference any event, outcome, figure, or development that occurred AFTER
{published}?

Corrections, editor's notes, "updated on", retrospective framing ("as it turned
out", "would later"), and references to later events all count as YES. Ordinary
forward-looking language written at the time ("is expected to", "will meet next
month") does NOT count — that is a contemporaneous prediction, not knowledge.

Answer with references_future true or false, and quote the phrase that decided
it (empty string if false).

ARTICLE TEXT:
{text}
"""

_SCAN_SCHEMA = {
    "name": "leakage_check",
    "schema": {
        "type": "object",
        "properties": {
            "references_future": {"type": "boolean"},
            "evidence": {"type": "string"},
        },
        "required": ["references_future", "evidence"],
        "additionalProperties": False,
    },
    "strict": True,
}


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------

@http.retry_transient(frozenset({429, 500, 502, 503, 504}))
def _get(url: str, params: Optional[Dict[str, object]] = None) -> requests.Response:
    """One request to the archive, retrying transient errors and honoring 429s."""
    resp = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=45)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp


def choose_capture(rows: List[List[str]], published: datetime.datetime) -> Optional[str]:
    """Pick the capture closest to publication from a CDX response.

    Args:
        rows: the CDX ``output=json`` rows *including* its header row.
        published: the article's own publication time.

    Returns:
        The winning 14-digit timestamp, or None if nothing usable came back.

    Nearest-to-publication, not first-in-window: a page captured the day it ran
    is the page that ran, while one captured five months later has had time to
    pick up corrections — the very thing the live-refetch scan exists to catch.
    """
    if not rows or len(rows) < 2:
        return None
    header = rows[0]
    try:
        ts_col = header.index("timestamp")
        status_col = header.index("statuscode")
    except ValueError:
        return None

    best: Optional[Tuple[float, str]] = None
    for row in rows[1:]:
        if len(row) <= max(ts_col, status_col) or row[status_col] != "200":
            continue
        stamp = row[ts_col]
        try:
            when = datetime.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        distance = abs((when - published).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, stamp)
    return best[1] if best else None


def find_capture(url: str, published: datetime.datetime) -> Optional[str]:
    """The best archive timestamp for ``url``, or None if it was never captured.

    Bounded to ``[published, published + WAYBACK_WINDOW_DAYS]``. Captures before
    publication are a different article at the same URL; captures long after have
    usually been re-templated.
    """
    window_end = published + datetime.timedelta(days=config.WAYBACK_WINDOW_DAYS)
    resp = _get(_CDX, {
        "url": url,
        "from": published.strftime("%Y%m%d"),
        "to": window_end.strftime("%Y%m%d"),
        "output": "json",
        "filter": "statuscode:200",
        "fl": "timestamp,statuscode",
        "limit": 50,
    })
    if resp.status_code != 200:
        return None
    try:
        return choose_capture(resp.json(), published)
    except ValueError:
        return None


def fetch_capture(url: str, timestamp: str) -> str:
    """Body text of one archived capture, or ``""``."""
    resp = _get(_CAPTURE.format(timestamp=timestamp, url=url))
    if resp.status_code != 200:
        return ""
    return core.extract_body(resp.text, url=url)[:core.MAX_BODY_CHARS]


def fetch_live(url: str) -> str:
    """Body text of the page as it stands today, or ``""``.

    Whatever this returns is suspect until the leakage scan clears it.
    """
    try:
        resp = _get(url)
    except Exception:  # noqa: BLE001 - a dead link is the normal case here
        return ""
    if resp.status_code != 200:
        return ""
    return core.extract_body(resp.text, url=url)[:core.MAX_BODY_CHARS]


# ---------------------------------------------------------------------------
# The leakage scan
# ---------------------------------------------------------------------------

def scan_cost_usd(bodies: List[str]) -> float:
    """Projected spend for scanning these bodies. Deliberately an estimate.

    Character-count over a fixed divisor rather than a real tokenizer, for the
    same reason ``data_retrieval`` does it: ``tiktoken`` downloads a BPE file on
    first use, and a cost estimate that reaches the network to compute itself is
    a cost estimate that fails offline.
    """
    chars = sum(len(_SCAN_PROMPT) + len(b) for b in bodies)
    return (chars / _CHARS_PER_TOKEN) / 1_000_000 * _SCAN_USD_PER_1M_INPUT_TOKENS


def references_future(text: str, published: datetime.datetime, api_key: str) -> bool:
    """Does this text know about anything that happened after it was published?

    Fails **closed**: any error scanning is treated as a leak. A body nobody
    could verify must not be scored as if it had been — being short one article
    costs a week a little evidence, while one leaked body teaches the scorer to
    be right about things nobody knew yet.
    """
    prompt = _SCAN_PROMPT.format(published=published.date().isoformat(),
                                 text=text[:core.MAX_BODY_CHARS])
    try:
        result = ai_client.build_digest_chat(api_key).with_structured_output(
            schema=_SCAN_SCHEMA, strict=True).invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("leakage scan failed (%s); treating as leaked", exc)
        return True
    if not isinstance(result, dict):
        return True
    if result.get("references_future") and result.get("evidence"):
        logger.info("  leaked: %r", str(result["evidence"])[:120])
    return bool(result.get("references_future"))


# ---------------------------------------------------------------------------
# The drain
# ---------------------------------------------------------------------------

class BudgetExhausted(RuntimeError):
    """The leakage scan hit its dollar cap. Stops the drain, keeps the work."""


def recover_one(row: Dict, api_key: Optional[str], spent: List[float]) -> str:
    """Recover one pending article's body. Returns the status it ended at.

    Args:
        row: a ``store.read_pending`` row.
        api_key: the OpenAI key, or None to skip live refetches entirely — which
            is what "the scan was not approved" means. Without the scan a live
            refetch cannot be trusted, so it is not attempted at all rather than
            stored unverified.
        spent: one-element running total of scan dollars, so the budget cap is
            enforced across the whole drain rather than per article.
    """
    url, published = row["url"], row["published_at"]

    timestamp = find_capture(url, published)
    if timestamp:
        body = fetch_capture(url, timestamp)
        if body:
            store.mark_body(url, body=body, body_status="recovered",
                            body_vintage=f"wayback-{timestamp[:8]}",
                            wayback_url=_CAPTURE.format(timestamp=timestamp, url=url))
            return "recovered"

    if api_key is None:
        store.mark_body(url, body=None, body_status="failed", body_vintage=None)
        return "failed"

    body = fetch_live(url)
    if not body:
        store.mark_body(url, body=None, body_status="failed", body_vintage=None)
        return "failed"

    cost = scan_cost_usd([body])
    if spent[0] + cost > config.LEAKAGE_SCAN_BUDGET_USD:
        raise BudgetExhausted(f"leakage scan would exceed "
                              f"${config.LEAKAGE_SCAN_BUDGET_USD:.2f} (spent "
                              f"${spent[0]:.2f})")
    spent[0] += cost

    if references_future(body, published, api_key):
        store.mark_body(url, body=None, body_status="degraded-title-only",
                        body_vintage="live-refetch")
        return "degraded-title-only"

    store.mark_body(url, body=body, body_status="recovered",
                    body_vintage="live-refetch")
    return "recovered-live"


def drain(limit: Optional[int] = None, api_key: Optional[str] = None) -> Dict[str, int]:
    """Work the pending queue until it is empty or the budget stops it.

    Every article is marked before the next one starts, so an interruption at
    any point loses at most one in-flight fetch. A permanent failure is recorded
    as ``failed`` and never retried in a loop — the snapshot layer simply sees
    fewer articles that week, which is an honest thin week rather than an
    invented one.

    Returns:
        Counts by outcome.
    """
    pending = store.read_pending(limit=limit)
    logger.info("[wayback] %d pending article(s); ~%d minutes at %.1fs each",
                len(pending),
                round(len(pending) * 2 * config.REQUEST_INTERVAL_SECONDS / 60),
                config.REQUEST_INTERVAL_SECONDS)

    outcomes: Dict[str, int] = {}
    spent = [0.0]
    for i, row in enumerate(pending, start=1):
        time.sleep(config.REQUEST_INTERVAL_SECONDS)
        try:
            result = recover_one(row, api_key, spent)
        except BudgetExhausted as exc:
            logger.warning("[wayback] %s; stopping after %d of %d. Re-run to resume.",
                           exc, i - 1, len(pending))
            break
        except Exception:  # noqa: BLE001 - one bad URL must not end an hours-long drain
            logger.exception("[wayback] %s failed", row["url"])
            store.mark_body(row["url"], body=None, body_status="failed", body_vintage=None)
            result = "failed"
        outcomes[result] = outcomes.get(result, 0) + 1
        if i % 100 == 0:
            logger.info("[wayback] %d/%d — %s (scan spend $%.2f)",
                        i, len(pending), outcomes, spent[0])

    logger.info("[wayback] done: %s, leakage scan spent $%.2f", outcomes, spent[0])
    return outcomes
